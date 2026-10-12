"""
Слой переопределений дерева приёма ошибок поверх встроенного конфига.

Встроенное дерево (`tree_config.DEFAULT_TREE`) — авторитетная бизнес-логика,
заданная кодом. Редактор из Telegram НЕ переписывает её, а накладывает сверху
правки из Supabase:

  * `telegram_intake_options` — варианты узлов. Строка может быть
    добавленным вариантом (is_builtin = false) или переопределением
    встроенного (is_builtin = true, тот же option_id): новая подпись,
    описание, иконка, цель перехода, порядок, скрытие.
  * `telegram_intake_nodes` — переопределения самих узлов: заголовок,
    описание, подсказка, переход и скрытие.

Почему так, а не «дерево целиком в БД»: приём ошибок — боевой путь, и он не
должен зависеть от доступности базы. Если Supabase молчит, `apply_overlay`
возвращает встроенное дерево, и бот продолжает работать. Плюс владелец всегда
видит исходную логику в коде и может сравнить её с правками.

Обратная совместимость: до применения `sql/intake_editor_v2.sql` таблица без
новых колонок. Чтение тогда идёт по старому набору колонок, а операции правки
сообщают «не поддерживается» (`overlay_supported()`), не ломая добавление
вариантов, которое работало и раньше.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .tree_config import DEFAULT_TREE
from .types import DecisionTree, Node, NodeType, Option

logger = logging.getLogger(__name__)

OPTION_TABLE = "telegram_intake_options"
NODE_TABLE = "telegram_intake_nodes"

# Узлы, к которым вообще применимы варианты. У INPUT/NUMBER/FINAL кнопок нет.
_OPTION_NODE_TYPES = (NodeType.CHOICE, NodeType.MULTI, NodeType.YESNO)

MAX_LABEL_LENGTH = 60
MAX_DESCRIPTION_LENGTH = 500
MAX_ICON_LENGTH = 10
MAX_TITLE_LENGTH = 120

# Шаг нумерации при перемещении: 10, 20, 30… Оставляем запас, чтобы при
# желании вставить вариант между двумя без пересчёта всего узла.
ORDER_STEP = 10

# Колонки v2 (после миграции) и v1 (до неё). Пробуем v2, при ошибке
# откатываемся на v1: бот обязан работать и до применения SQL.
#
# **`template_id` — отдельный набор, а не часть v2.** Связь «ошибка дерева →
# справочник `issue_templates`» приезжает **своей** миграцией (`0109`), отдельно
# от колонок редактора v2. PostgREST отвергает запрос целиком, если в `select`
# есть хотя бы одна неизвестная колонка, поэтому общий набор сделал бы так:
# неприехавшая `0109` сняла бы заодно `is_builtin`/`hidden`/`sort_order`, и
# редактор сообщил бы «база не готова» там, где на самом деле готова. Ровно на
# этом уже теряли аналитику журнала (1663 строки без `cause`, см.
# `report_writer.SPLIT_COLUMNS`) — второй раз тот же класс дефекта не заводим.
_COLUMNS_V2 = (
    "node_id,option_id,label,next_node,description,icon,only_for,"
    "is_builtin,hidden,sort_order"
)
_COLUMNS_V2_LINK = _COLUMNS_V2 + ",template_id"
_COLUMNS_V1 = "node_id,option_id,label,next_node,description,icon,only_for"
_NODE_COLUMNS = "node_id,title,description,placeholder,stub_hint,next_node,hidden"

#: Справочник готовых описаний ошибок — цель связи варианта дерева.
TEMPLATES_TABLE = "issue_templates"

#: Поля шаблона, которые читает и пишет редактор.
TEMPLATE_COLUMNS = (
    "id,employee_title,issue_type,issue_sub_type,issue_description,"
    "recovery_title,solving_time,equipment_type"
)

# Насколько долго помним результат проверки «есть ли колонки v2». Короткий
# TTL, чтобы после применения миграции бот подхватил её без перезапуска.
_V2_TTL_SECONDS = 120
_v2_state: Optional[bool] = None
_v2_checked_at: float = 0.0

#: Есть ли колонка связи `template_id` (миграция `0109`). **Отдельная память**:
#: связь приезжает своей миграцией, и её отсутствие не должно выглядеть как
#: «редактор не готов» (см. `_COLUMNS_V2`).
_link_state: Optional[bool] = None
_link_checked_at: float = 0.0

#: Кэш карты `option_id → template_id` (см. `option_template_links`).
_links_cache: Optional[Dict[str, Any]] = None
_links_checked_at: float = 0.0

_UNSET = object()


# ============================================================
# ЧТЕНИЕ
# ============================================================

def _remember_v2(supported: bool) -> None:
    global _v2_state, _v2_checked_at

    _v2_state = supported
    _v2_checked_at = time.time()


def _remember_link(supported: bool) -> None:
    """Запомнить, есть ли колонка связи (миграция `0109`)."""
    global _link_state, _link_checked_at

    _link_state = supported
    _link_checked_at = time.time()


def _v2_known_unsupported() -> bool:
    """Точно ли известно, что колонок v2 нет (и память ещё свежа).

    Нужно, чтобы не пробовать v2-запрос заново на каждое сообщение. Проверка
    «есть ли колонки» делается запросом, который при отсутствии колонок
    завершается ошибкой 400 — и `rest_get` пишет об этом в лог. До этой правки
    живой бот без применённой миграции засорял логи: на каждое сообщение по
    одной строке ERROR. Само чтение при этом работало (был откат на v1), но по
    логам нельзя было понять, что происходит на самом деле.
    """
    return (
        _v2_state is False
        and time.time() - _v2_checked_at < _V2_TTL_SECONDS
    )


def _read_options() -> Optional[List[dict]]:
    """Строки оверлея вариантов. None — база недоступна.

    **Порядок проб — от полного набора к усечённому.** Сначала v2 со ссылкой на
    шаблон (`0109`), затем v2 без неё, затем v1. Каждый промах ожидаем: наборы
    приезжают разными миграциями, и более новый не должен прятать более старый
    (см. комментарий у `_COLUMNS_V2`).
    """
    from sendToDataBase import rest_get

    # Если уже знаем, что v2-колонок нет, идём сразу к v1: иначе каждый вызов
    # даёт ожидаемую ошибку 400 в логе.
    if not _v2_known_unsupported():
        order = "node_id.asc,sort_order.asc,id.asc"

        # `optional=True`: отсутствие колонок — ожидаемое состояние, пока
        # миграция не применена. Иначе каждый прогон проверки даёт ERROR.
        rows = rest_get(
            OPTION_TABLE,
            {"select": _COLUMNS_V2_LINK, "order": order},
            optional=True,
        )

        if rows is not None:
            _remember_v2(True)
            _remember_link(True)
            return [row for row in rows if isinstance(row, dict)]

        rows = rest_get(
            OPTION_TABLE,
            {"select": _COLUMNS_V2, "order": order},
            optional=True,
        )

        if rows is not None:
            _remember_v2(True)
            _remember_link(False)
            return [row for row in rows if isinstance(row, dict)]

        _remember_v2(False)

    rows = rest_get(OPTION_TABLE, {"select": _COLUMNS_V1, "order": "created_at.asc"})

    if rows is not None:
        _remember_v2(False)
        return [row for row in rows if isinstance(row, dict)]

    return None


def load_option_rows() -> Optional[List[dict]]:
    """Публичное чтение строк оверлея (нужно редактору и тестам)."""
    return _read_options()


def load_node_rows() -> Optional[List[dict]]:
    """
    Строки правок узлов. Пустой список — таблицы нет или она пуста.

    Отсутствие таблицы — это не «база упала»: редактор просто покажет
    встроенные узлы без правок. Но спрашивать её на каждое сообщение не нужно:
    пока миграция не применена, каждый запрос даёт 404 в логе, и настоящие
    проблемы в нём не видны.

    Пропуск запроса привязан к тому же признаку, что и проверка колонок v2:
    таблица и колонки создаются **одной** миграцией. Отдельного «помню, что
    таблицы нет» здесь намеренно нет: если бы этот признак расходился с
    `_v2_state`, `node_overrides()` вернул бы пусто и правка заголовка
    показывала бы скрытый узел — ровно тот дефект, который уже исправлялся.
    """
    if _v2_known_unsupported():
        return []

    from sendToDataBase import rest_get

    # `optional=True`: таблица появляется вместе с миграцией; до неё 404 — норма.
    rows = rest_get(
        NODE_TABLE, {"select": _NODE_COLUMNS, "order": "id.asc"}, optional=True
    )

    if rows is None:
        # Таблицы нет — значит и миграции v2 нет: отметить это, чтобы не
        # спрашивать её снова на каждое сообщение. Признак общий с колонками
        # намеренно: так не бывает состояния «колонки есть, а таблицы нет».
        _remember_v2(False)
        return []

    return [row for row in rows if isinstance(row, dict)]


def overlay_supported() -> bool:
    """
    Доступны ли операции правки (применена ли миграция v2).

    Проверяем не записью, а дешёвым чтением одной строки: редактор должен
    честно сказать «база не готова», а не делать вид, что сохранил правку.
    """
    global _v2_state, _v2_checked_at

    if _v2_state is not None and time.time() - _v2_checked_at < _V2_TTL_SECONDS:
        return _v2_state

    from sendToDataBase import rest_get

    # `optional=True`: отсутствие колонок v2 — ожидаемое состояние, пока
    # миграция не применена. Без флага `rest_get` писал ERROR на каждую проверку
    # (раз в TTL, то есть пока админ открыт редактор — регулярно), и настоящие
    # проблемы тонули в этом шуме. Уровень сообщения о состоянии — WARNING ниже.
    rows = rest_get(
        OPTION_TABLE, {"select": "is_builtin", "limit": "1"}, optional=True
    )
    supported = rows is not None
    _remember_v2(supported)

    if not supported:
        logger.warning(
            "intake storage: колонок v2 нет — примените sql/intake_editor_v2.sql "
            "для полной настройки дерева"
        )

    return supported


def reset_capability_cache() -> None:
    """Сброс памяти о возможностях схемы — только для тестов."""
    global _v2_state, _v2_checked_at, _link_state, _link_checked_at

    _v2_state = None
    _v2_checked_at = 0.0
    _link_state = None
    _link_checked_at = 0.0
    reset_links_cache()


# ============================================================
# СВЯЗЬ СО СПРАВОЧНИКОМ `issue_templates`
# ============================================================

def link_supported() -> bool:
    """
    Есть ли колонка связи `template_id` (миграция `0109`).

    **Отдельная проверка, а не общая с v2.** Наборы колонок приезжают разными
    миграциями, и PostgREST отвергает запрос целиком из-за одной неизвестной
    колонки. Общая проверка означала бы: нет `0109` — «редактор не готов»,
    хотя правки дерева работают. Этот класс дефекта уже стоил аналитики журнала
    (1663 строки без причины), поэтому память здесь своя.
    """
    global _link_state, _link_checked_at

    if _link_state is not None and time.time() - _link_checked_at < _V2_TTL_SECONDS:
        return _link_state

    from sendToDataBase import rest_get

    rows = rest_get(
        OPTION_TABLE, {"select": "template_id", "limit": "1"}, optional=True
    )
    supported = rows is not None
    _remember_link(supported)

    if not supported:
        logger.warning(
            "intake storage: колонки template_id нет — примените миграцию "
            "0109_intake_option_templates.sql, чтобы связать ошибки дерева "
            "со справочником шаблонов"
        )

    return supported


def load_templates() -> List[dict]:
    """
    Строки справочника `issue_templates`.

    Пустой список — это **и** «шаблонов нет», **и** «база недоступна»: для
    показа списка и выбора это одно и то же (нечего показать), а различать их
    вызывающий код не обязан. Различие важно только при **записи**, и там оно
    проверяется по результату самой записи.
    """
    from sendToDataBase import rest_get

    rows = rest_get(
        TEMPLATES_TABLE,
        {"select": TEMPLATE_COLUMNS, "order": "employee_title.asc", "limit": "500"},
        optional=True,
    )

    return [row for row in (rows or []) if isinstance(row, dict)]


def option_template_links() -> Dict[str, Any]:
    """
    Карта `option_id → template_id` по строкам оверлея.

    Нужна писателю журнала: при выборе ошибки он обязан взять **точную** связь,
    а не подбирать шаблон по тексту. Ошибка чтения — пустая карта (подбор
    остаётся запасным путём, как и раньше).

    **Кэш на 2 минуты.** Карта нужна на каждом отчёте, а дерево меняется редко;
    без кэша каждая запись делала бы лишнее чтение всей таблицы вариантов в
    момент приёма. TTL короткий по той же причине, что у остальных проверок
    схемы: связанный в редакторе шаблон должен заработать без перезапуска бота.
    """
    global _links_cache, _links_checked_at

    now = time.time()

    if _links_cache is not None and now - _links_checked_at < _V2_TTL_SECONDS:
        return dict(_links_cache)

    links: Dict[str, Any] = {}

    try:
        rows = _read_options() or []
    except Exception:  # noqa: BLE001
        logger.exception("intake storage: не удалось прочитать связи вариантов")
        return links

    for row in rows:
        option_id = str(row.get("option_id") or "")
        template_id = row.get("template_id")

        if option_id and template_id not in (None, ""):
            links[option_id] = template_id

    _links_cache = dict(links)
    _links_checked_at = now

    return links


def reset_links_cache() -> None:
    """Сброс кэша связей — только для тестов и после правки связи в редакторе."""
    global _links_cache, _links_checked_at

    _links_cache = None
    _links_checked_at = 0.0


def create_template(
    label: str,
    module: str = "",
    description: str = "",
    recovery: str = "",
    solving_time=None,
    created_by=None,
) -> Optional[int]:
    """
    Заводит **заготовку** в `issue_templates` и возвращает её id.

    Требование владельца 12.10.2026: при добавлении новой ошибки бот должен
    сразу создать заготовку, «чтобы потом были поля которые будут подставлены
    автоматически». Поэтому строка создаётся **всегда**, даже если человек
    ничего не заполнил кроме названия: пустые поля допустимы, их дозаполнят.

    **Что заполняется, а что нет.** Название (его ввёл человек) и модуль
    (следует из узла) — заполняются; `issue_description` дублирует название,
    потому что оно и есть описание ошибки словами владельца. `recovery_title` и
    `solving_time` остаются пустыми: «что было сделано» и время решения знает
    только человек, а выдуманное число испортило бы метрику «сколько решается
    ошибка» (в справочнике значения 2–8 минут).

    `None` — заготовка не создалась (база недоступна или запись отвергнута).
    Вызывающий код в этом случае **всё равно** добавляет вариант: ошибка не
    должна пропасть из дерева из-за недоступного справочника.
    """
    from equipment_intake.error_templates import stub_row

    row = stub_row(label, module=module, created_by=created_by)

    if not row.get("employee_title"):
        return None

    if description:
        row["issue_description"] = _clean(description, MAX_DESCRIPTION_LENGTH)

    if recovery:
        row["recovery_title"] = _clean(recovery, MAX_DESCRIPTION_LENGTH)

    if solving_time not in (None, ""):
        row["solving_time"] = _solving_time(solving_time)

    # Пустое значение в справочнике — это `NULL`, а не пустая строка: `example`
    # показал бы «поле есть, но пустое» там, где значение не задавали.
    row = {key: value for key, value in row.items() if value != ""}

    from sendToDataBase import rest_post

    created = rest_post(TEMPLATES_TABLE, row)

    if not created:
        logger.error(
            "intake storage: не удалось создать заготовку шаблона %r", label
        )
        return None

    if isinstance(created, list):
        first = created[0] if created else None
    else:
        first = created

    template_id = (first or {}).get("id") if isinstance(first, dict) else None

    if template_id is None:
        # `rest_post` вернул успех без тела (или без `id`) — заготовку не
        # теряем: находим её по названию, единственному уникальному полю
        # справочника (проверено: 0 дублей на 29 записях).
        template_id = find_template_id(label)

    if template_id is not None:
        logger.info(
            "intake storage: создана заготовка шаблона %r (id=%s, модуль=%r)",
            label, template_id, module or "—",
        )

    return template_id


def find_template_id(title: str) -> Optional[int]:
    """id шаблона по точному названию, или `None`."""
    from sendToDataBase import rest_get

    wanted = str(title or "").strip()

    if not wanted:
        return None

    rows = rest_get(
        TEMPLATES_TABLE,
        {"select": "id", "employee_title": f"eq.{wanted}", "limit": "1"},
        optional=True,
    )

    if not rows:
        return None

    first = rows[0] if isinstance(rows, list) else rows
    value = (first or {}).get("id") if isinstance(first, dict) else None

    return value


def update_template(
    template_id,
    title=_UNSET,
    module=_UNSET,
    description=_UNSET,
    recovery=_UNSET,
    solving_time=_UNSET,
    updated_by=None,
) -> bool:
    """
    Правка полей заготовки из редактора.

    **Только переданные поля.** `_UNSET` означает «не трогать»: редактор
    (`editor.py`) правит по одному полю за раз, и запись всего объекта затирала
    бы соседние значения — этот дефект уже был у правки узлов и исправлен там
    тем же приёмом.

    Пустая строка — «очищено» и остаётся пустой; `solving_time` приводится к
    целому (в колонке `bigint`, и мусор из справочника не должен ронять
    сохранение).
    """
    if template_id in (None, ""):
        return False

    from sendToDataBase import rest_patch

    payload: Dict[str, Any] = {}

    if title is not _UNSET:
        value = _clean(title, MAX_TITLE_LENGTH)

        if not value:
            return False

        payload["employee_title"] = value

    if module is not _UNSET:
        payload["issue_type"] = _clean(module, MAX_TITLE_LENGTH)

    if description is not _UNSET:
        payload["issue_description"] = _clean(description, MAX_DESCRIPTION_LENGTH)

    if recovery is not _UNSET:
        payload["recovery_title"] = _clean(recovery, MAX_DESCRIPTION_LENGTH)

    if solving_time is not _UNSET:
        payload["solving_time"] = _solving_time(solving_time)

    if not payload:
        return False

    if updated_by is not None:
        payload["updated_by"] = updated_by

    updated = rest_patch(
        TEMPLATES_TABLE, params={"id": f"eq.{template_id}"}, payload=payload
    )

    if not updated:
        logger.error(
            "intake storage: не удалось сохранить шаблон id=%s (%s)",
            template_id, ", ".join(payload),
        )
        return False

    logger.info(
        "intake storage: шаблон id=%s обновлён (%s)", template_id, ", ".join(payload)
    )

    return True


def _solving_time(value) -> int:
    """
    Время решения в минутах — целое, неотрицательное.

    Нечисловое значение приводим к нулю, а не роняем сохранение: испорченная
    строка справочника не повод отказать человеку в правке. Формула совпадает с
    `report_writer._solving_time` намеренно — у одной колонки не должно быть
    двух разных способов приведения.
    """
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def link_option_to_template(
    node_id: str, option_id: str, template_id, base: DecisionTree = None,
    updated_by=None,
) -> bool:
    """
    Связать вариант дерева с шаблоном (снять связь — `template_id=None`).

    Это **точная** связь, заменяющая подбор по тексту. Она же нужна кнопке
    «выбрать шаблон» в карточке варианта: админ указывает, какая заготовка
    соответствует ошибке, и при выборе ошибки поля подставляются автоматически.
    """
    if not link_supported():
        return False

    base = base or DEFAULT_TREE

    if base.node(node_id) is None:
        return False

    current = effective_options(base, node_id, _read_options() or [])
    entry = next((item for item in current if item["id"] == option_id), None)

    if entry is None:
        return False

    row: Dict[str, Any] = {
        "node_id": node_id,
        "option_id": option_id,
        "label": entry["label"],
        "next_node": entry["next_node"],
        "description": entry["description"],
        "icon": entry["icon"],
        "only_for": list(entry["only_for"]),
        "is_builtin": bool(entry["is_builtin"]),
        "hidden": bool(entry["hidden"]),
        "sort_order": entry["index"] * ORDER_STEP + ORDER_STEP,
        "template_id": template_id if template_id not in (None, "") else None,
    }

    if updated_by is not None:
        row["updated_by"] = str(updated_by)

    saved = _upsert_option_row(row)

    if saved:
        logger.info(
            "intake storage: вариант %s узла %s связан с шаблоном %s",
            option_id, node_id, row["template_id"],
        )

    return saved


def option_template_id(node_id: str, option_id: str, base: DecisionTree = None):
    """`template_id` варианта, или `None` (нет связи либо нет колонки)."""
    base = base or DEFAULT_TREE

    for row in _read_options() or []:
        if (
            str(row.get("node_id")) == str(node_id)
            and str(row.get("option_id")) == str(option_id)
        ):
            return row.get("template_id")

    return None


# ============================================================
# ЭФФЕКТИВНЫЙ ВИД УЗЛА (чистая логика — тестируется без сети)
# ============================================================

def _row_builtin(row: Dict[str, Any]) -> bool:
    return bool(row.get("is_builtin"))


def _row_hidden(row: Dict[str, Any]) -> bool:
    return bool(row.get("hidden"))


def _row_order(row: Dict[str, Any]) -> int:
    try:
        return int(row.get("sort_order") or 0)
    except (TypeError, ValueError):
        return 0


def _clean(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def effective_options(
    base: DecisionTree,
    node_id: str,
    option_rows: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Варианты узла в том порядке и с теми правками, что увидит сотрудник.

    Возвращает и скрытые варианты (hidden = true): редактору они нужны,
    чтобы вариант можно было вернуть. Рабочее дерево скрытые отбрасывает
    (см. `build_tree`).

    Каждый элемент:
        id, label, next_node, description, icon, only_for,
        is_builtin, hidden, kind ("builtin" | "edited" | "added"),
        index (позиция в списке), template_id (связь со справочником).

    `template_id` идёт из строки-переопределения. У встроенного варианта без
    строки он `None` — но связь всё равно может быть найдена по карте
    `error_templates.py`; здесь показывается только то, что явно записано в базе,
    чтобы редактор не выдавал догадку за сохранённую связь.
    """
    node = base.node(node_id)

    if node is None:
        return []

    by_id: Dict[str, Dict[str, Any]] = {}

    for row in option_rows:
        if str(row.get("node_id") or "") != str(node_id):
            continue

        option_id = str(row.get("option_id") or "")

        if option_id:
            by_id[option_id] = row

    base_ids = {option.id for option in node.options}
    items: List[Tuple[float, int, Dict[str, Any]]] = []
    sequence = 0

    for position, option in enumerate(node.options):
        row = by_id.get(option.id)
        hidden = bool(row and _row_hidden(row))
        explicit_order = _row_order(row) if row else 0
        order = float(explicit_order) if row and explicit_order else float(position)

        label = _clean(row.get("label"), MAX_LABEL_LENGTH) if row else ""
        target = row.get("next_node") if row and row.get("next_node") else option.next_node
        description = row.get("description") if row and row.get("description") else option.description
        icon = row.get("icon") if row and row.get("icon") else option.icon

        items.append((order, sequence, {
            "id": option.id,
            "label": label or option.label,
            "next_node": target,
            "description": str(description or ""),
            "icon": str(icon or ""),
            # Пустой список в строке = «не переопределено»: иначе правка
            # подписи встроенного варианта стёрла бы его only_for.
            "only_for": tuple(row.get("only_for") or ()) if row and row.get("only_for") else tuple(option.only_for),
            # value и status живут только в коде: редактор их не меняет,
            # но терять нельзя (например, Yes = True у YESNO).
            "value": option.value,
            "status": option.status,
            "is_builtin": True,
            "hidden": hidden,
            "kind": "edited" if row else "builtin",
            # Связь со справочником шаблонов (миграция `0109`). У строки без
            # колонки (старая база) её просто нет — `None`, а не выдумка.
            "template_id": row.get("template_id") if row else None,
        }))
        sequence += 1

    for row in option_rows:
        if str(row.get("node_id") or "") != str(node_id):
            continue

        option_id = str(row.get("option_id") or "")

        if not option_id or option_id in base_ids or _row_builtin(row):
            continue

        label = _clean(row.get("label"), MAX_LABEL_LENGTH)

        if not label:
            continue

        explicit_order = _row_order(row)
        order = float(explicit_order) if explicit_order else 1000.0 + sequence
        target = row.get("next_node") if row.get("next_node") else None

        if target and target not in base.nodes:
            # Висячая ссылка (например, цель поправили руками в базе) — не
            # тащим её в рабочее дерево: вариант останется, но ветка кончится.
            target = None

        items.append((order, sequence, {
            "id": option_id,
            "label": label,
            "next_node": target,
            "description": str(row.get("description") or ""),
            "icon": str(row.get("icon") or ""),
            "only_for": tuple(row.get("only_for") or ()),
            "value": None,
            "status": "",
            "is_builtin": False,
            "hidden": _row_hidden(row),
            "kind": "added",
            "template_id": row.get("template_id"),
        }))
        sequence += 1

    items.sort(key=lambda item: (item[0], item[1]))

    result: List[Dict[str, Any]] = []

    for index, (_order, _seq, item) in enumerate(items):
        item["index"] = index
        result.append(item)

    return result


def hidden_node_target(
    base: DecisionTree,
    node_id: str,
    option_rows: Sequence[Dict[str, Any]] = (),
) -> Optional[str]:
    """
    Куда ведёт ссылка, если узел `node_id` скрыть. None — скрывать нельзя.

    Варианты могут уходить в разные места: у «robot_module» все четыре ведут
    на номер, а у узла с ответвлениями скрыть нельзя — ссылки сойдутся в одну
    и часть дерева потеряется. В таком случае возвращаем None.

    Явный `next_node` узла (текстовые шаги) — прямой ответ, спорить не о чем.
    """
    node = base.node(node_id)

    if node is None or node.type == NodeType.FINAL or node_id == base.root_id:
        return None

    outgoing = [
        item["next_node"]
        for item in effective_options(base, node_id, option_rows)
        if not item["hidden"] and item["next_node"]
    ]

    if not outgoing:
        # Узел без вариантов (INPUT/NUMBER): ведёт туда, куда сказано.
        return node.next_node

    unique = {str(target) for target in outgoing}

    return unique.pop() if len(unique) == 1 else None


def _redirector(hidden_targets: Dict[str, Optional[str]]):
    """
    Строит функцию «ссылка с учётом скрытых узлов».

    В дереве нет обратных ссылок, поэтому удалять узлы нельзя — остались бы
    висячие `next_node`. Скрытие эмулируется перенаправлением: ссылка на
    скрытый узел ведёт туда, куда вёл он сам. Цепочку скрытых узлов проходим
    до конца; цикл обрываем.

    Состояние держим в замыкании, а не в модуле: сборка дерева обязана быть
    потокобезопасной (её вызывают обработчики Telegram из разных потоков).
    """
    def resolve(target: Optional[str]) -> Optional[str]:
        seen = set()

        while target is not None and target in hidden_targets and target not in seen:
            seen.add(target)
            target = hidden_targets.get(target)

        return target

    return resolve


def build_tree(
    base: DecisionTree,
    option_rows: Sequence[Dict[str, Any]],
    node_rows: Sequence[Dict[str, Any]] = (),
) -> DecisionTree:
    """
    Собирает рабочее дерево: встроенное + правки вариантов + правки узлов.

    Чистая функция: ни сети, ни Telegram. Всё, что делает `apply_overlay`,
    сводится к чтению строк и вызову этой функции.
    """
    nodes: Dict[str, Node] = dict(base.nodes)

    node_overrides: Dict[str, Dict[str, Any]] = {}

    for row in node_rows:
        node_id = str(row.get("node_id") or "")

        if node_id:
            node_overrides[node_id] = row

    # Скрытые узлы: корень и финальный узел скрывать нельзя — дерево без
    # входа или без итогового экрана перестанет работать. Узел с расходящимися
    # ветками тоже: одна ссылка потеряла бы часть дерева. Такие правки
    # игнорируем и пишем в лог — редактор их и не предлагает.
    hidden_targets: Dict[str, Optional[str]] = {}

    for node_id, row in node_overrides.items():
        if not _row_hidden(row) or node_id not in nodes:
            continue

        target = hidden_node_target(base, node_id, option_rows)

        if target is None:
            logger.warning(
                "intake storage: узел %s скрыть нельзя — правка проигнорирована",
                node_id,
            )
            continue

        hidden_targets[node_id] = target

    # Скрытый узел не должен вести на ещё один скрытый узел: схлопываем цепочку.
    for node_id in list(hidden_targets):
        seen = {node_id}
        target = hidden_targets[node_id]

        while target in hidden_targets and target not in seen:
            seen.add(target)
            target = hidden_targets[target]

        hidden_targets[node_id] = target

    resolve_target = _redirector(hidden_targets)

    rebuilt: Dict[str, Node] = {}

    for node_id, node in nodes.items():
        row = node_overrides.get(node_id)

        options: List[Option] = []

        for item in effective_options(base, node_id, option_rows):
            if item["hidden"]:
                continue

            options.append(Option(
                id=item["id"],
                label=item["label"],
                next_node=resolve_target(item["next_node"]),
                description=item["description"],
                icon=item["icon"],
                status=item.get("status", "") or "",
                value=item.get("value"),
                only_for=tuple(item["only_for"]),
            ))

        title = _clean(row.get("title"), MAX_TITLE_LENGTH) if row else ""
        description = row.get("description") if row and row.get("description") is not None else None
        placeholder = row.get("placeholder") if row and row.get("placeholder") is not None else None
        stub_hint = row.get("stub_hint") if row and row.get("stub_hint") is not None else None
        override_next = row.get("next_node") if row and row.get("next_node") else None
        resolved_next = resolve_target(override_next or node.next_node)

        changed = row is not None or tuple(options) != node.options or resolved_next != node.next_node

        if not changed:
            rebuilt[node_id] = node
            continue

        rebuilt[node_id] = replace(
            node,
            title=title or node.title,
            description=node.description if description is None else str(description),
            placeholder=node.placeholder if placeholder is None else str(placeholder),
            stub_hint=node.stub_hint if stub_hint is None else str(stub_hint),
            next_node=resolved_next,
            options=tuple(options),
        )

    for node_id in hidden_targets:
        rebuilt.pop(node_id, None)

    return DecisionTree(root_id=base.root_id, nodes=rebuilt)


# ============================================================
# НАЛОЖЕНИЕ НА РАБОЧЕЕ ДЕРЕВО
# ============================================================

def apply_overlay(base: DecisionTree = None) -> DecisionTree:
    """
    Читает правки один раз и накладывает их на встроенное дерево.

    Любая ошибка чтения — возвращаем базу. Приём ошибок не должен ломаться
    из-за недоступной базы или неприменённой миграции.
    """
    base = base or DEFAULT_TREE

    try:
        option_rows = _read_options()
    except Exception:  # noqa: BLE001
        logger.exception("intake storage: не удалось прочитать оверлей вариантов")
        return base

    if option_rows is None:
        return base

    try:
        node_rows = load_node_rows()
    except Exception:  # noqa: BLE001
        logger.exception("intake storage: не удалось прочитать оверлей узлов")
        node_rows = []

    try:
        return build_tree(base, option_rows, node_rows)
    except Exception:  # noqa: BLE001
        logger.exception("intake storage: не удалось собрать дерево с правками")
        return base


def options_for(node_id: str, base: DecisionTree = None) -> List[dict]:
    """
    Строки оверлея для одного узла (и добавленные, и правки встроенных).

    Оставлено для совместимости: рабочему дереву нужен `apply_overlay`,
    а этот вызов полезен точечно. None от базы читается как «правок нет».
    """
    base = base or DEFAULT_TREE
    rows = _read_options()

    if rows is None:
        rows = []

    return [dict(item) for item in effective_options(base, node_id, rows)]


# ============================================================
# ЗАПИСЬ: ВАРИАНТЫ
# ============================================================

def add_option(
    node_id: str,
    label: str,
    description: str = "",
    icon: str = "",
    only_for=(),
    next_node: Optional[str] = None,
    created_by=None,
    base: DecisionTree = None,
) -> Optional[str]:
    """
    Добавляет НОВЫЙ вариант к узлу (прежнее поведение редактора).

    Пишем только колонки v1: добавление вариантов работает и до применения
    миграции v2. Порядок — «после всех», чтобы новые варианты не прыгали
    вверх списка.
    """
    base = base or DEFAULT_TREE
    node_id = _clean(node_id, MAX_TITLE_LENGTH)
    node = base.node(node_id)
    label = _clean(label, MAX_LABEL_LENGTH)

    if not node or node.type not in _OPTION_NODE_TYPES or not label:
        return None

    from collections import Counter

    targets = Counter(o.next_node for o in node.options if o.next_node)
    next_node = next_node or (targets.most_common(1)[0][0] if targets else node.next_node)

    if next_node and next_node not in base.nodes:
        return None

    base_id = re.sub(r"[^a-z0-9]+", "_", label.casefold()).strip("_")[:24] or "option"

    from sendToDataBase import rest_get, rest_post

    existing = rest_get(OPTION_TABLE, {"select": "option_id", "node_id": f"eq.{node_id}"})

    if existing is None:
        return None

    taken = {o.id for o in node.options} | {
        str(row.get("option_id")) for row in existing if isinstance(row, dict)
    }
    option_id = base_id
    suffix = 2

    while option_id in taken:
        option_id = f"{base_id[:20]}_{suffix}"
        suffix += 1

    row = {
        "node_id": node_id,
        "option_id": option_id,
        "label": label,
        "next_node": next_node,
        "description": _clean(description, MAX_DESCRIPTION_LENGTH),
        "icon": _clean(icon, MAX_ICON_LENGTH),
        "only_for": list(only_for) if not isinstance(only_for, str) else [only_for],
        "created_by": str(created_by) if created_by is not None else None,
    }

    return option_id if rest_post(OPTION_TABLE, row) is not None else None


def _upsert_option_row(row: Dict[str, Any]) -> bool:
    from sendToDataBase import rest_upsert

    return rest_upsert(OPTION_TABLE, row, on_conflict="node_id,option_id") is not None


def _find_base_option(base: DecisionTree, node_id: str, option_id: str) -> Optional[Option]:
    node = base.node(node_id)
    return node.option(option_id) if node else None


def update_option(
    node_id: str,
    option_id: str,
    label=_UNSET,
    description=_UNSET,
    icon=_UNSET,
    next_node=_UNSET,
    hidden=_UNSET,
    is_builtin: Optional[bool] = None,
    base: DecisionTree = None,
    updated_by=None,
) -> bool:
    """
    Правит вариант: подпись, описание, иконку, цель, видимость.

    Для встроенного варианта (`is_builtin = true`) создаётся/обновляется
    строка-переопределение с тем же option_id: исходный код не меняется.
    Для добавленного — обновляется его строка.

    Переданные аргументы заменяют значения; непереданные не трогаются, но
    строка пишется целиком (иначе merge-duplicates нечего обновлять).
    """
    if not overlay_supported():
        return False

    base = base or DEFAULT_TREE
    node = base.node(node_id)

    if node is None:
        return False

    builtin_option = _find_base_option(base, node_id, option_id)

    if builtin_option is None and is_builtin is None:
        # Правка добавленного варианта: проверим, что строка существует.
        rows = _read_options() or []
        known = any(
            str(r.get("node_id")) == node_id and str(r.get("option_id")) == option_id
            for r in rows
        )

        if not known:
            return False

        is_builtin = False
    elif builtin_option is not None:
        is_builtin = True

    current = effective_options(base, node_id, _read_options() or [])
    entry = next((item for item in current if item["id"] == option_id), None)

    if entry is None:
        return False

    label_value = entry["label"] if label is _UNSET else _clean(label, MAX_LABEL_LENGTH)

    if not label_value:
        return False

    target = entry["next_node"] if next_node is _UNSET else next_node

    if target and target not in base.nodes:
        return False

    row: Dict[str, Any] = {
        "node_id": node_id,
        "option_id": option_id,
        "label": label_value,
        "next_node": target,
        "description": (
            entry["description"] if description is _UNSET
            else _clean(description, MAX_DESCRIPTION_LENGTH)
        ),
        "icon": entry["icon"] if icon is _UNSET else _clean(icon, MAX_ICON_LENGTH),
        "only_for": list(entry["only_for"]),
        "is_builtin": bool(is_builtin),
        "hidden": entry["hidden"] if hidden is _UNSET else bool(hidden),
        # Порядок по текущей позиции варианта: правка не должна менять
        # место варианта в списке.
        "sort_order": entry["index"] * ORDER_STEP + ORDER_STEP,
    }

    # 🔴 **`template_id` здесь НЕ пишется — и это осознанно.**
    #
    # Связь со шаблоном при правке других полей не теряется: `rest_upsert` идёт
    # через `resolution=merge-duplicates`, то есть `insert … on conflict do update`
    # со **списком колонок из payload**. Незаписанная колонка остаётся прежней.
    #
    # Я сначала добавил сюда `row["template_id"] = entry["template_id"]` — и
    # негативная проверка показала, что удаление этой строки ничего не меняет.
    # Значит, строка была не защитой, а **риском**: `entry` приходит из кэша
    # чтения, и устаревшее значение перезаписало бы свежую связь, выставленную в
    # редакторе. Единственное место, где `template_id` пишется, —
    # `link_option_to_template`, и только явным выбором человека.
    if updated_by is not None:
        row["updated_by"] = str(updated_by)

    saved = _upsert_option_row(row)

    if saved:
        logger.info(
            "intake storage: вариант %s узла %s обновлён (builtin=%s hidden=%s)",
            option_id, node_id, row["is_builtin"], row["hidden"],
        )

    return saved


def set_option_hidden(
    node_id: str,
    option_id: str,
    hidden: bool,
    is_builtin: Optional[bool] = None,
    base: DecisionTree = None,
    updated_by=None,
) -> bool:
    """Скрыть/показать вариант (встроенный или добавленный)."""
    return update_option(
        node_id,
        option_id,
        hidden=bool(hidden),
        is_builtin=is_builtin,
        base=base,
        updated_by=updated_by,
    )


def delete_option(node_id: str, option_id: str) -> bool:
    """
    Удаляет добавленный вариант. Встроенные не удаляются — только скрываются.

    Вызывающий код обязан сам проверить, что вариант добавленный: удаление
    строки встроенного варианта просто вернёт его исходный вид из кода.
    """
    from sendToDataBase import rest_delete

    removed = rest_delete(
        OPTION_TABLE,
        params={"node_id": f"eq.{node_id}", "option_id": f"eq.{option_id}"},
    )

    if removed:
        logger.info("intake storage: вариант %s узла %s удалён", option_id, node_id)

    return removed


def move_option(
    node_id: str,
    option_id: str,
    direction: str,
    base: DecisionTree = None,
    updated_by=None,
) -> bool:
    """
    Двигает вариант вверх/вниз внутри узла.

    Порядок хранится числом (sort_order). После обмена перенумеровываем все
    варианты узла шагом 10 — так позиции однозначны и не зависят от того,
    был ли у встроенного варианта явный порядок.
    """
    if direction not in ("up", "down") or not overlay_supported():
        return False

    base = base or DEFAULT_TREE
    current = effective_options(base, node_id, _read_options() or [])
    index = next((i for i, item in enumerate(current) if item["id"] == option_id), None)

    if index is None:
        return False

    swap = index - 1 if direction == "up" else index + 1

    if swap < 0 or swap >= len(current):
        return False

    current[index], current[swap] = current[swap], current[index]

    for position, item in enumerate(current):
        row = {
            "node_id": node_id,
            "option_id": item["id"],
            "label": item["label"],
            "next_node": item["next_node"],
            "description": item["description"],
            "icon": item["icon"],
            "only_for": list(item["only_for"]),
            "is_builtin": bool(item["is_builtin"]),
            "hidden": bool(item["hidden"]),
            "sort_order": position * ORDER_STEP + ORDER_STEP,
            "updated_by": str(updated_by) if updated_by is not None else None,
        }

        # `template_id` здесь тоже **не** пишется: `merge-duplicates` сохраняет
        # незаписанные колонки, а значение из кэша чтения могло бы откатить
        # свежую связь (разбор — в `update_option`).
        if not _upsert_option_row(row):
            # Частичный порядок хуже старого: сообщаем о неудаче наверх.
            logger.error(
                "intake storage: не удалось сохранить порядок варианта %s узла %s",
                item["id"], node_id,
            )
            return False

    logger.info(
        "intake storage: вариант %s узла %s перемещён %s", option_id, node_id, direction
    )

    return True


# ============================================================
# ЗАПИСЬ: УЗЛЫ
# ============================================================

def update_node(
    node_id: str,
    title=_UNSET,
    description=_UNSET,
    placeholder=_UNSET,
    stub_hint=_UNSET,
    next_node=_UNSET,
    hidden=_UNSET,
    base: DecisionTree = None,
    updated_by=None,
) -> bool:
    """
    Правит узел: заголовок, описание, подсказку, переход, видимость.

    Создание и удаление узлов намеренно не поддержано: в дереве нет обратных
    ссылок, и удаление узла оставило бы висячие `next_node` у родителей.
    Скрытие узла — безопасная замена удалению: ссылки на него «схлопываются»
    на его собственный следующий узел (см. `_redirector`).
    """
    if not overlay_supported():
        return False

    base = base or DEFAULT_TREE
    node = base.node(node_id)

    if node is None:
        return False

    if hidden is not _UNSET and bool(hidden):
        if node_id == base.root_id or node.type == NodeType.FINAL:
            logger.warning(
                "intake storage: узел %s скрыть нельзя (корень или финал)", node_id
            )
            return False

        if hidden_node_target(base, node_id, _read_options() or ()) is None:
            logger.warning(
                "intake storage: узел %s скрыть нельзя — его варианты ведут "
                "в разные места, ветка потерялась бы", node_id
            )
            return False

    target = node.next_node if next_node is _UNSET else next_node

    if target and target not in base.nodes:
        return False

    # Непереданное поле означает «не трогать», а не «сбросить». Редактор правит
    # по одному полю за раз (`editor.py`), поэтому раньше каждое сохранение
    # затирало соседние: правка заголовка стирала описание, подсказку и
    # placeholder, а скрытый узел возвращался в дерево (`hidden` писался как
    # false). Очистка делается явно — редактор передаёт пустую строку, когда
    # пользователь нажал «очистить». У вариантов (`update_option`) это правило
    # работало с самого начала; здесь оно было упущено.
    override = node_overrides().get(node_id) or {}

    def _keep(field: str, value, limit: int):
        # `_UNSET` — оставить как есть; иначе текущая правка (пустая строка
        # означает «очищено» и остаётся пустой).
        return override.get(field) if value is _UNSET else _clean(value, limit)

    row: Dict[str, Any] = {
        "node_id": node_id,
        "title": _keep("title", title, MAX_TITLE_LENGTH) or None,
        "description": _keep("description", description, MAX_DESCRIPTION_LENGTH),
        "placeholder": _keep("placeholder", placeholder, MAX_DESCRIPTION_LENGTH),
        "stub_hint": _keep("stub_hint", stub_hint, MAX_DESCRIPTION_LENGTH),
        "next_node": target,
        "hidden": _row_hidden(override) if hidden is _UNSET else bool(hidden),
    }

    if updated_by is not None:
        row["updated_by"] = str(updated_by)

    from sendToDataBase import rest_upsert

    saved = rest_upsert(NODE_TABLE, row, on_conflict="node_id") is not None

    if saved:
        logger.info("intake storage: узел %s обновлён (hidden=%s)", node_id, row["hidden"])

    return saved


def node_overrides() -> Dict[str, Dict[str, Any]]:
    """Правки узлов по node_id — для редактора."""
    return {
        str(row.get("node_id")): row
        for row in load_node_rows()
        if row.get("node_id")
    }
