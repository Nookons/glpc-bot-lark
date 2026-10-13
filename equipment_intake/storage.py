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

from .tree_config import (
    ASK_CAUSE,
    DEFAULT_TREE,
    ROBOT_MODULES,
    cause_node_id,
    error_node_id,
    module_of_error_node,
    module_tail_for_option,
)
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
#: Набор с пометкой удаления (`0115`). **Своя миграция — своя проба:** PostgREST
#: отвергает запрос целиком из-за одной неизвестной колонки, поэтому без `0115`
#: чтение обязано продолжить работать по прежнему набору, а не упасть.
_COLUMNS_V2_DELETED = _COLUMNS_V2_LINK + ",deleted"
_COLUMNS_V1 = "node_id,option_id,label,next_node,description,icon,only_for"
_NODE_COLUMNS = "node_id,title,description,placeholder,stub_hint,next_node,hidden"
#: Колонки узла вместе с координатами холста (миграция `0114`). **Отдельный
#: набор:** PostgREST отвергает запрос целиком из-за одной неизвестной колонки,
#: поэтому без `0114` редактор обязан читать узлы по старому набору, а не
#: остаться без них вовсе. Тот же класс дефекта, что у `_COLUMNS_V2_LINK`.
_NODE_COLUMNS_POSITION = _NODE_COLUMNS + ",position_x,position_y"
#: Набор с типом узла и пометкой удаления (`0116`) — то, что нужно для **новых**
#: блоков вопросов, создаваемых из веба. Своя проба: PostgREST отвергает запрос
#: целиком из-за одной неизвестной колонки, поэтому без `0116` чтение узлов
#: обязано продолжить работать по прежнему набору.
_NODE_COLUMNS_TYPED = _NODE_COLUMNS_POSITION + ",node_type,deleted"

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

#: Есть ли колонка пометки удаления (`0115`). Своя память по той же причине.
_deleted_state: Optional[bool] = None
_deleted_checked_at: float = 0.0

#: Есть ли колонки координат холста (миграция `0114`). **Своя память** — по той
#: же причине, что у `_link_state`: наборы колонок приезжают разными миграциями,
#: и отсутствие координат не должно выглядеть как «редактор не готов».
_position_state: Optional[bool] = None
_position_checked_at: float = 0.0

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


def _deleted_supported() -> bool:
    """
    Есть ли колонка пометки удаления (`0115`).

    **Своя проба, а не общая с v2.** Наборы колонок приезжают разными
    миграциями, а PostgREST отвергает запрос целиком из-за одной неизвестной
    колонки. Общая проверка означала бы: нет `0115` — «редактор не готов», хотя
    правки дерева работают.
    """
    global _deleted_state, _deleted_checked_at

    if _deleted_state is not None and time.time() - _deleted_checked_at < _V2_TTL_SECONDS:
        return _deleted_state

    from sendToDataBase import rest_get

    rows = rest_get(OPTION_TABLE, {"select": "deleted", "limit": "1"}, optional=True)
    supported = rows is not None

    _remember_deleted(supported)

    if not supported:
        logger.warning(
            "intake storage: колонки deleted нет — примените миграцию "
            "0115_intake_option_deleted.sql, чтобы удалять встроенные кнопки"
        )

    return supported


def _remember_deleted(supported: bool) -> None:
    """Запомнить, есть ли колонка пометки удаления (`0115`)."""
    global _deleted_state, _deleted_checked_at

    _deleted_state = supported
    _deleted_checked_at = time.time()


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
            {"select": _COLUMNS_V2_DELETED, "order": order},
            optional=True,
        )

        if rows is not None:
            _remember_v2(True)
            _remember_link(True)
            _remember_deleted(True)
            return [row for row in rows if isinstance(row, dict)]

        rows = rest_get(
            OPTION_TABLE,
            {"select": _COLUMNS_V2_LINK, "order": order},
            optional=True,
        )

        if rows is not None:
            _remember_v2(True)
            _remember_link(True)
            _remember_deleted(False)
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

    # Сначала — набор с координатами холста (`0114`). Он может не приехать:
    # тогда координат у узлов нет, но правки узлов обязаны остаться читаемыми
    # (заголовки, подсказки, скрытие). Поэтому вторым пробом идёт старый набор,
    # а не возврат пустоты.
    rows = rest_get(
        NODE_TABLE, {"select": _NODE_COLUMNS_TYPED, "order": "id.asc"}, optional=True
    )

    if rows is None:
        rows = rest_get(
            NODE_TABLE, {"select": _NODE_COLUMNS_POSITION, "order": "id.asc"}, optional=True
        )

    if rows is None:
        rows = rest_get(
            NODE_TABLE, {"select": _NODE_COLUMNS, "order": "id.asc"}, optional=True
        )

        if rows is not None:
            logger.info(
                "intake storage: координат холста нет — примените миграцию "
                "0114_intake_node_positions.sql, чтобы блоки держали позиции"
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
    # Координаты холста — своя миграция, поэтому и память своя: без сброса
    # тест, проверивший работу до `0114`, оставил бы `False` для всех следующих.
    reset_position_cache()
    reset_deleted_cache()


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

        if not option_id or template_id in (None, ""):
            continue

        node_id = str(row.get("node_id") or "")

        # 🔴 **Голый ключ `option_id` — только для слоя ошибок.** Иначе связь
        # «утекает» между модулями: id причин **повторяются** (`mechanical_wear`
        # есть у восьми модулей, `human_error` и `software_fault` — у девяти), и
        # связь, выставленная на причине безопасности, применялась бы к причине
        # рога, лотка и крюка. Дефект воспроизведён на живой базе 13.10.2026:
        # **32 утечки** — шасси с причиной «Mechanical wear» получало шаблон
        # безопасности 65 («Robot safety device triggered»).
        #
        # Почему у ошибок ключ безопасен: их id **уникальны** (проверено: 11
        # кнопок, 0 повторов), потому что узел ошибки у каждого модуля свой, и
        # заготовка так и заводилась. Голый ключ оставлен **для совместимости** —
        # уже сохранённые связи ошибок должны работать без миграции.
        if node_id.startswith("robot_errors"):
            links[option_id] = template_id

        # 🔴 **Ключ с узлом — обязателен и он первый.** С 13.10.2026 у каждого
        # модуля свой узел причин, и только этот ключ различает одинаковые id
        # причин у разных модулей.
        if node_id:
            links[f"{node_id}::{option_id}"] = template_id

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

    🔴 **Узлы, созданные из веба, тоже принимаются.** Проверка только по коду
    отклоняла их: в карточке новой кнопки выбор шаблона молча не сохранялся
    (воспроизведено 13.10.2026 — блок и кнопка создаются, а связь вернула
    `False`). Требование владельца прямо про это: «на лету добавлять новые блоки
    с вопросами или кнопками» и привязывать к ним заготовки.
    """
    if not link_supported():
        return False

    base = base or DEFAULT_TREE

    if node_id not in _known_node_ids(base):
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
        # 🔴 **`is_builtin` — по факту, а не по словарю `effective_options`.**
        #
        # Живой дефект 13.10.2026: кнопка `robot_errors_fingers
        # .fingers_stuck_open_close` создана из веба (в коде её нет), но получила
        # `is_builtin=true` — потому что словарь, собранный по **базовому**
        # дереву, не нашёл её среди встроенных и пометил как «правку
        # встроенной». Следствие было видимым: `effective_options` считает такую
        # строку правкой, а не добавлением, и **карта веба п��казывала у блока
        # 0 кнопок**, тогда как рабочее дерево — 1. Человек видел пустой блок и
        # не мог привязать заготовку.
        #
        # Признак обязан следовать факту: кнопка встроенная ровно тогда, когда
        # она объявлена в коде.
        "is_builtin": bool(_find_base_option(base, node_id, option_id)),
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


def _row_deleted(row: Dict[str, Any]) -> bool:
    """Помечена ли кнопка удалённой (`0115`). До миграции колонки нет — `False`."""
    return bool(row.get("deleted"))


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
        # Узел создан из веба: в коде его нет, но варианты у него уже могут быть.
        # Собираем временный узел из строк, чтобы кнопки нового блока были видны
        # и рабочему дереву, и редактору.
        node = _web_node(node_id, option_rows)

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
            # Удаление — отдельное состояние: человек различает «временно скрыл»
            # и «удалил насовсем», и сводить их в одно значило бы потерять эту
            # разницу (в том числе при восстановлении).
            "deleted": _row_deleted(row) if row else False,
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

        if target and target not in _known_node_ids(base):
            # Висячая ссылка (например, цель поправили руками в базе) — не
            # тащим её в рабочее дерево: вариант останется, но ветка кончится.
            #
            # «Известные» узлы включают созданные из веба: без этого кнопка,
            # направленная в новый блок, теряла бы цель и ветка обрывалась.
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
            "deleted": _row_deleted(row),
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

    if node is None:
        # Узел создан из веба — в коде его нет, но скрывать/удалять его тоже
        # нужно, и правила те же.
        node = _web_node(node_id, option_rows)

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


#: Типы узлов, которые **разрешено создавать из веба**.
#:
#: 🔴 **`final` здесь нет — и это не упущение.** `final` — это запись отчёта:
#: `final_keyboard` даёт ему «Confirm», и подтверждение пишет строку в журнал.
#: Свободно создавая такие узлы, человек получил бы **несколько «итоговых
#: экранов»**, то есть одну ошибку можно было бы записать дважды. `unavailable`
#: безопасен: он показывает сообщение и завершает диалог **без** записи.
NEW_NODE_TYPES = ("choice", "input", "unavailable")

#: Типы, которым нужен хотя бы один вариант, чтобы работать.
_NEEDS_OPTIONS = (NodeType.CHOICE, NodeType.MULTI, NodeType.YESNO)


def node_from_rows(
    node_id: str, node_row: Dict[str, Any], option_rows: Sequence[Dict[str, Any]] = ()
) -> Optional[Node]:
    """
    Узел, созданный из веба, по его строке. Публичная обёртка `_node_from_row`.

    Нужна `tree_view`: карта обязана показывать блоки, созданные из веба, и
    собирает их тем же правилом, что и рабочее дерево — иначе форма узла в карте
    и в боте разошлась бы.
    """
    # Передаём пустой набор известных узлов: `_node_from_row` использует его
    # только для проверки `next_node`, а здесь форма узла нужна без неё — цель
    # проверяется тем же правилом, что и в рабочем дереве.
    #
    # 🔴 **Строки кнопок передаются.** Без них узел считался бы «черновым» даже
    # с готовыми кнопками, и карта показывала бы «ветка не в работе» на рабочей
    # ветке (дефект `Fingers` у владельца, 13.10.2026).
    return _node_from_row(node_row, {}, option_rows)


def _node_from_row(
    row: Dict[str, Any],
    nodes: Dict[str, Node],
    option_rows: Sequence[Dict[str, Any]] = (),
) -> Optional[Node]:
    """
    Узел, созданный из веба, или `None`, если строку создать нельзя.

    **Почему узел не берётся «как есть».** Тип приходит текстом из базы, а движок
    знает только `NodeType`. Неизвестное значение (чужой писатель, опечатка)
    обязано быть отброшено здесь: узел с несуществующим типом попал бы в дерево и
    упал бы при показе — то есть сломал бы приём ошибок у оператора.
    """
    node_id = str(row.get("node_id") or "")
    node_type = str(row.get("node_type") or "").strip().casefold()

    if not node_id or node_type not in NEW_NODE_TYPES:
        return None

    try:
        parsed_type = NodeType(node_type)
    except ValueError:
        return None

    title = _clean(row.get("title"), MAX_TITLE_LENGTH) or node_id

    # `next_node` — переход текстового узла. Проверяем, что цель существует:
    # висячая ссылка оборвала бы ветку молча.
    target = row.get("next_node")
    target = str(target) if target else None

    if target and target not in nodes:
        target = None

    return Node(
        id=node_id,
        title=title,
        type=parsed_type,
        description=str(row.get("description") or ""),
        placeholder=str(row.get("placeholder") or ""),
        # Новый узел-выбор начинается **без вариантов**: кнопки к нему добавят
        # следующим шагом, а показывать вопрос с пустым списком нельзя. Поэтому
        # такой узел объявляется черновым (`is_stub`) — бот попросит описать
        # проблему текстом, и приём не сломается, пока человек наполняет ветку.
        #
        # 🔴 **Черновой — только пока кнопок НЕТ.** Здесь стояло
        # `is_stub=parsed_type == NodeType.CHOICE`, то есть признак ставился
        # навсегда: кнопки добавляли, а `is_stub` оставался `True`. Бот это
        # учитывал (`is_stub and not node.options`), а **веб смотрел только
        # `is_stub`** — и показывал владельцу «This branch is not in use: the bot
        # does not write a report from it» на **рабочей** ветке `Fingers`,
        # которая отчёт исправно пишет. Ложная тревога хуже молчания: по ней
        # человек идёт чинить то, что не сломано.
        # 🔴 **Черновой — только если кнопок нет ИМЕННО У ЭТОГО УЗЛА.**
        # Проверять `not option_rows` нельзя: вызывающие передают **всю**
        # таблицу кнопок (`tree_view` зовёт так), и тогда условие ложно всегда —
        # настоящий черновой узел переставал помечаться. Поймано проверкой на
        # узле-черновике: `is_stub` выходил `False`.
        is_stub=(
            parsed_type == NodeType.CHOICE
            and not [
                item for item in option_rows
                if str(item.get("node_id") or "") == node_id
            ]
        ),
        stub_hint=str(row.get("stub_hint") or ""),
        next_node=target,
    )


def _known_node_ids(base: DecisionTree) -> set:
    """
    Все существующие id узлов: из кода **и** созданные из веба.

    Нужно там, где проверяется «ведёт ли ссылка в существующий узел». Проверка
    только по коду отвергала бы ссылки на блоки, созданные в вебе.
    """
    known = set(base.nodes)

    for row in load_node_rows() or []:
        node_id = str(row.get("node_id") or "")

        if not node_id or _row_deleted(row):
            continue

        # Учитываем только те строки, что **создают** узел: строка правки
        # встроенного узла уже есть в `base.nodes`.
        if row.get("node_type") or node_id not in known:
            known.add(node_id)

    return known


def _web_node(node_id: str, option_rows: Sequence[Dict[str, Any]]) -> Optional[Node]:
    """
    Узел, созданный из веба, — по его строке и строкам его кнопок.

    Нужен там, где функция получает **базовое** дерево (без оверлея) и не может
    найти узел: `effective_options`, `add_option`, `update_node`,
    `hidden_node_target`. Без этого кнопки нового блока не добавить, а сам блок
    нельзя ни скрыть, ни удалить.

    🔴 **Возвращает узел и БЕЗ вариантов.** Первая версия требовала хотя бы одну
    кнопку и на пустом новом блоке возвращала `None` — из-за этого его нельзя
    было удалить («варианты ведут в разные места»), и создание нового блока
    откатывалось сразу после записи. Пустой блок — нормальное состояние: кнопки к
    нему добавят следующим шагом.

    Тип берётся из строки (`node_type`); при её отсутствии — `choice`, потому что
    спрашивают в основном «применимы ли кнопки».
    """
    options: List[Option] = []
    node_row: Optional[Dict[str, Any]] = None

    for row in load_node_rows() or []:
        if str(row.get("node_id") or "") == str(node_id):
            node_row = row
            break

    for row in option_rows:
        if str(row.get("node_id") or "") != str(node_id):
            continue

        option_id = str(row.get("option_id") or "")

        if not option_id:
            continue

        options.append(Option(
            id=option_id,
            label=_clean(row.get("label"), MAX_LABEL_LENGTH) or option_id,
            next_node=(str(row.get("next_node")) if row.get("next_node") else None),
            description=str(row.get("description") or ""),
            icon=str(row.get("icon") or ""),
            only_for=tuple(row.get("only_for") or ()),
        ))

    # Ни строки узла с типом, ни кнопок — такого узла не существует.
    if node_row is None and not options:
        return None

    if node_row is not None and _row_deleted(node_row):
        return None

    node_type = NodeType.CHOICE

    if node_row is not None and node_row.get("node_type"):
        try:
            node_type = NodeType(str(node_row["node_type"]).strip().casefold())
        except ValueError:
            return None

    return Node(
        id=node_id,
        title=_clean(node_row.get("title") if node_row else "", MAX_TITLE_LENGTH)
        or node_id,
        type=node_type,
        description=str((node_row or {}).get("description") or ""),
        next_node=(
            str(node_row["next_node"])
            if node_row is not None and node_row.get("next_node")
            else None
        ),
        options=tuple(options),
    )


def build_tree(
    base: DecisionTree,
    option_rows: Sequence[Dict[str, Any]],
    node_rows: Sequence[Dict[str, Any]] = (),
) -> DecisionTree:
    """
    Собирает рабочее дерево: встроенное + правки вариантов + правки узлов
    + **новые узлы, созданные из веба**.

    Чистая функция: ни сети, ни Telegram. Всё, что делает `apply_overlay`,
    сводится к чтению строк и вызову этой функции.

    **Новые узлы добавляются ДО правок.** Строка, созданная из веба, сама
    описывает и узел (`node_type`), и его заголовок; те же поля могут быть
    переопределены правкой. Порядок поэтому такой: сначала узлы (чтобы `next_node`
    новых на новые находились), затем правки поверх.
    """
    nodes: Dict[str, Node] = dict(base.nodes)

    node_overrides: Dict[str, Dict[str, Any]] = {}

    for row in node_rows:
        node_id = str(row.get("node_id") or "")

        if not node_id:
            continue

        # 🔴 **Удалённый узел попадает в `node_overrides`, но не в `nodes`.**
        # Через `node_overrides` он доходит до общей ветки удаления/скрытия ниже,
        # где ссылки на него схлопываются. Если бы строку пропустить здесь
        # целиком (первая версия так и делала), узел остался бы в дереве со всеми
        # своими кнопками — то есть «удаление не сработало».
        if not _row_deleted(row):
            # Строки кнопок нужны, чтобы узел **с готовыми кнопками** не считался
            # черновым: иначе карта показывала бы «ветка не в работе» на рабочей
            # ветке (дефект `Fingers`, 13.10.2026).
            created = _node_from_row(
                row, nodes,
                [item for item in option_rows
                 if str(item.get("node_id") or "") == node_id],
            )

            if created is not None:
                nodes[node_id] = created

        node_overrides[node_id] = row

    # Скрытые узлы: корень и финальный узел скрывать нельзя — дерево без
    # входа или без итогового экрана перестанет работать. Узел с расходящимися
    # ветками тоже: одна ссылка потеряла бы часть дерева. Такие правки
    # игнорируем и пишем в лог — редактор их и не предлагает.
    hidden_targets: Dict[str, Optional[str]] = {}

    for node_id, row in node_overrides.items():
        # 🔴 **Удалённый узел убирается так же, как скрытый.** Разница только в
        # намерении человека и в том, показывать ли его в редакторе: для движка
        # «узла нет» — одно и то же состояние. Ссылки на него схлопываются на его
        # собственный следующий узел (`_redirector`), поэтому ветка не обрывается.
        #
        # Без этой общей ветки удаление ломало бы дерево: 11 кнопок ошибок ведут
        # в `ask_cause`, и ровно столько ссылок ушло бы «в никуда» — оператор
        # нажал бы кнопку, и диалог повис бы (проверено запуском).
        if not (_row_hidden(row) or _row_deleted(row)) or node_id not in nodes:
            continue

        target = hidden_node_target(base, node_id, option_rows)

        if target is None:
            # Причина одна из двух, и они требуют разных действий: узел ветвится
            # (ссылки разошлись бы в одну) либо он вход/финал. Сообщаем прямо,
            # иначе «не удаляется» выглядит дефектом.
            logger.warning(
                "intake storage: узел %s убрать нельзя (%s) — правка проигнорирована",
                node_id,
                "варианты ведут в разные места"
                if _row_deleted(row)
                else "скрытие запрещено",
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
            # Удалённая кнопка не попадает в рабочее дерево так же, как скрытая:
            # для оператора между ними разницы нет — нет кнопки и нет. Разница
            # только для человека в редакторе (восстановить или показать).
            if item["hidden"] or item.get("deleted"):
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
            # 🔴 **Признак «черновая ветка» пересчитывается, а не наследуется.**
            # Узел, созданный из веба, родился без кнопок и был помечен
            # черновым; после добавления кнопок он **рабочий**. Пока признак
            # копировался как есть (`replace` без этого поля), ветка
            # `Fingers` владельца навсегда оставалась «не в работе» в вебе —
            # при том что бот по ней писал отчёт.
            is_stub=node.is_stub and not options,
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

    if node is None:
        # Узел, созданный из веба: в коде его нет, но кнопки к нему добавлять
        # нужно — иначе новый блок остался бы навсегда пустым.
        node = _web_node(node_id, _read_options() or [])

    if node is None:
        # Ни в коде, ни в базе — значит блока не существует. Кнопку в никуда
        # создать нельзя: она не попадёт ни в одно дерево.
        return None

    if node.type not in _OPTION_NODE_TYPES or not label:
        return None

    from collections import Counter

    targets = Counter(o.next_node for o in node.options if o.next_node)
    next_node = next_node or (targets.most_common(1)[0][0] if targets else node.next_node)

    # 🔴 **Цель проверяется по ОБОИМ источникам узлов.** Раньше проверка шла
    # только по `base.nodes` (узлы кода), и кнопку нельзя было направить в блок,
    # созданный из веба: `add_option` молча возвращал `None`, а человек видел
    # «не удалось добавить». Цели собираются из кода **и** из строк узлов.
    if next_node and next_node not in _known_node_ids(base):
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
        "only_for": _clean_only_for(only_for),
        "created_by": str(created_by) if created_by is not None else None,
    }

    return option_id if rest_post(OPTION_TABLE, row) is not None else None


def _upsert_option_row(row: Dict[str, Any]) -> bool:
    from sendToDataBase import rest_upsert

    return rest_upsert(OPTION_TABLE, row, on_conflict="node_id,option_id") is not None


#: Максимум значений в `only_for`: защита от «выбрали всё». Больше модулей, чем
#: существует, быть не может, а испорченный запрос не должен раздувать строку.
MAX_ONLY_FOR = 24


def _clean_only_for(value) -> List[str]:
    """
    Значения `only_for`: список непустых строк без повторов.

    Строка принимается наравне со списком: редактор бота передаёт её так же
    (`add_option`), и приведение в одном месте избавляет от расхождений.
    """
    if value is None or value == "":
        return []

    if isinstance(value, str):
        raw = [value]
    else:
        try:
            raw = list(value)
        except TypeError:
            return []

    cleaned: List[str] = []

    for item in raw:
        text = _clean(item, MAX_LABEL_LENGTH)

        if text and text not in cleaned:
            cleaned.append(text)

        if len(cleaned) >= MAX_ONLY_FOR:
            break

    return cleaned


def module_choices(base: DecisionTree = None) -> List[str]:
    """
    Значения выбора **модуля** — то, на что можно ограничить причину.

    Берутся из узлов модулей (`robot_module_*`), а не списком в коде: модули
    добавляет редактор, и зашитый список разошёлся б�� с деревом. Значения — это
    `label` варианта (движок кладёт в путь именно его, `Option.answer_value`),
    поэтому «Lifting», а не `lifting`.
    """
    base = base or DEFAULT_TREE
    values: List[str] = []

    for node in base.nodes.values():
        if not str(node.id).startswith("robot_module"):
            continue

        for option in node.options:
            value = str(option.answer_value() or "")

            if value and value not in values:
                values.append(value)

    return values


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
    deleted=_UNSET,
    only_for=_UNSET,
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
        # Узел создан из веба — править его кнопки тоже нужно.
        node = _web_node(node_id, _read_options() or [])

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

    # 🔴 Цель проверяется по ОБОИМ источникам узлов: иначе кнопку нельзя было бы
    # направить в блок, созданный из веба, — `update_option` молча отвечал бы
    # `False`, и вставка нового блока всегда откатывалась.
    if target and target not in _known_node_ids(base):
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
        # 🔴 **`only_for` — «показывать вариант только для этих значений пути».**
        # Именно этим причины привязываются к модулям: `["Lifting"]` у причины
        # означает «��оказывать её, только если оператор выбрал модуль Lifting».
        # Пишется только при явном значении: правка подписи не должна сбрасывать
        # ограничение, иначе причина «расползлась» бы по всем модулям.
        "only_for": (
            list(entry["only_for"]) if only_for is _UNSET
            else _clean_only_for(only_for)
        ),
        "is_builtin": bool(is_builtin),
        "hidden": entry["hidden"] if hidden is _UNSET else bool(hidden),
        # 🔴 **Пишется только при явном значении.** `merge-duplicates` сохраняет
        # незаписанные колонки, поэтому правка подписи не сбрасывает удаление, а
        # `restore_option` включает кнопку обратно (`deleted=False`).
        #
        # Колонки может не быть (миграция `0115` не применена) — тогда набор
        # чтения её не принёс, `deleted` в запрос не попадает, и правки
        # продолжают работать по-старому.
        "deleted": (entry.get("deleted", False) if deleted is _UNSET else bool(deleted)),
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

    # Е��ли колонки `deleted` в схеме нет, её нельзя посылать: PostgREST отверг
    # бы запрос целиком, и все правки дерева сломались бы. Признак спрашиваем
    # только когда поле реально нужно для изменения состояния.
    if deleted is not _UNSET and not _deleted_supported():
        logger.warning(
            "intake storage: колонки deleted нет — примените 0115, чтобы удалять "
            "встроенные кнопки"
        )
        return False

    if deleted is _UNSET:
        row.pop("deleted", None)

    saved = _upsert_option_row(row)

    if saved:
        logger.info(
            "intake storage: вариант %s узла %s обновлён (builtin=%s hidden=%s deleted=%s)",
            option_id, node_id, row["is_builtin"], row["hidden"], row.get("deleted"),
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
    """
    Скрыть/показать вариант (встроенный или добавленный).

    🔴 **Последнюю видимую кнопку скрыть нельзя — как и удалить.** Тупик от
    скрытия тот же: узел-выбор без вариантов показывает оператору только
    «Cancel», и ошибку записать нечем. Проверка стоит **здесь**, а не только в
    `delete_option`, потому что это два разных пути к одному и тому же
    состоянию — и защита обязана стоять на обоих.
    """
    base = base or DEFAULT_TREE

    if hidden:
        rows = _read_options() or []
        node = base.node(node_id)

        if (
            node is not None
            and node.type in _OPTION_NODE_TYPES
            and visible_options_count(node_id, base, rows) <= 1
        ):
            logger.warning(
                "intake storage: последнюю кнопку узла %s скрыть нельзя — узел "
                "остался бы без вариантов", node_id,
            )
            return False

    return update_option(
        node_id,
        option_id,
        hidden=bool(hidden),
        is_builtin=is_builtin,
        base=base,
        updated_by=updated_by,
    )


def visible_options_count(
    node_id: str, base: DecisionTree = None, rows: Optional[Sequence[Dict[str, Any]]] = None
) -> int:
    """
    Сколько кнопок узла реально увидит оператор (без скрытых и удалённых).

    Нужно, чтобы **не дать удалить последнюю кнопку**: узел-выбор без вариантов
    для оператора тупик — бот покажет только «Cancel», и ошибку записать нечем.
    """
    base = base or DEFAULT_TREE

    if rows is None:
        rows = _read_options() or []

    return sum(
        1
        for item in effective_options(base, node_id, rows)
        if not item["hidden"] and not item.get("deleted")
    )


def delete_option(
    node_id: str,
    option_id: str,
    base: DecisionTree = None,
    updated_by=None,
) -> bool:
    """
    Удаляет кнопку — **и встроенную, и добавленную**.

    🔴 **Почему встроенную нельзя удалить строкой.** Её определение лежит в коде
    бота (`tree_config.py`), а в базе — только правки. Удаление строки
    возвращало бы кнопку к исходному виду: со стороны это выглядело бы как
    «удаление не сработало». Поэтому встроенная кнопка **помечается** как
    удалённая (`deleted = true`, миграция `0115`) и остаётся в базе — так
    удаление можно отменить (`restore_option`).

    Добавленная кнопка живёт только строкой в базе, и её строку убираем совсем:
    восстанавливать нечего, а лишняя строка копила бы мусор.

    **Последнюю видимую кнопку узла удалить нельзя** (`EmptyNode`): без вариантов
    узел-выбор перестаёт работать — бот покажет только «Cancel», и оператор не
    сможет записать ошибку. Это не перестраховка: собственный валидатор бота
    отвергает `choice` без вариантов (`types.py::validate`).

    Возвращает `True`, если кнопка удалена (или уже была удалена).
    """
    base = base or DEFAULT_TREE

    if not overlay_supported():
        return False

    rows = _read_options() or []
    entry = next(
        (item for item in effective_options(base, node_id, rows) if item["id"] == option_id),
        None,
    )

    if entry is None:
        return False

    # Уже удалена — повторное нажатие не ошибка: результат тот же.
    if entry.get("deleted"):
        return True

    node = base.node(node_id)

    if (
        node is not None
        and node.type in _OPTION_NODE_TYPES
        and visible_options_count(node_id, base, rows) <= 1
    ):
        logger.warning(
            "intake storage: последнюю кнопку узла %s удалить нельзя — узел остался "
            "бы без вариантов", node_id,
        )
        return False

    if not entry["is_builtin"]:
        from sendToDataBase import rest_delete

        removed = rest_delete(
            OPTION_TABLE,
            params={"node_id": f"eq.{node_id}", "option_id": f"eq.{option_id}"},
        )

        if removed:
            logger.info(
                "intake storage: добавленная кнопка %s узла %s удалена", option_id, node_id
            )

        return removed

    return update_option(
        node_id,
        option_id,
        deleted=True,
        is_builtin=True,
        base=base,
        updated_by=updated_by,
    )


def restore_option(
    node_id: str,
    option_id: str,
    base: DecisionTree = None,
    updated_by=None,
) -> bool:
    """
    Возвращает удалённую кнопку: `deleted = false`.

    Существует ровно потому, что удаление встроенной кнопки — пометка, а не
    потеря: человек, удаливший кнопку по ошибке, должен иметь возможность её
    вернуть, не заходя в базу руками.
    """
    return update_option(
        node_id,
        option_id,
        deleted=False,
        base=base,
        updated_by=updated_by,
    )


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
    deleted=_UNSET,
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
        # Узел, созданный из веба: в коде его нет, но править его нужно.
        node = _web_node(node_id, _read_options() or [])

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

    if target and target not in _known_node_ids(base):
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
        # Удаление узла — пометка (для встроенного это единственный способ).
        # Тот же приём, что у кнопок: непереданное поле сохраняет прежнее
        # значение, иначе правка заголовка «воскрешала» бы удалённый блок.
        "deleted": _row_deleted(override) if deleted is _UNSET else bool(deleted),
    }
    # Колонки может не быть (миграция `0116` не применена) — тогда её нельзя
    # посылать: PostgREST отверг бы запрос целиком, и все правки узлов сломались
    # бы. Признак спрашиваем только когда поле реально меняется.
    if deleted is not _UNSET and not _deleted_supported():
        logger.warning(
            "intake storage: колонки deleted у узлов нет — примените 0116"
        )
        return False

    if deleted is _UNSET:
        row.pop("deleted", None)

    if updated_by is not None:
        row["updated_by"] = str(updated_by)

    from sendToDataBase import rest_upsert

    saved = rest_upsert(NODE_TABLE, row, on_conflict="node_id") is not None

    if saved:
        logger.info("intake storage: узел %s обновлён (hidden=%s)", node_id, row["hidden"])

    return saved


#: Предел координаты холста. **Широкий намеренно:** холст бесконечен, и упереться
#: в потолок при перетаскивании блока в сторону было бы дефектом. Ограничение
#: нужно только чтобы отсечь заведомо битые значения; то же число стоит в
#: `CHECK` миграции `0114`, чтобы база отвергла мусор и от чужого писателя.
MAX_POSITION = 1_000_000.0


def _clean_position(value) -> Optional[float]:
    """
    Координата холста или `None`.

    `None` — «человек этот блок ещё не двигал»: раскладку считает веб. Пустая
    строка и нечисловое значение тоже дают `None`, а не `0`: ноль — это
    координата, и скопление блоков в углу выглядело бы как «человек так
    расставил». `NaN` отсекается отдельно — он проходит сравнения, но ломает
    и раскладку, и `JSON` в ответе API.
    """
    if value is None or value == "":
        return None

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    if number != number or number in (float("inf"), float("-inf")):
        return None

    if abs(number) >= MAX_POSITION:
        return None

    return number


def update_node_position(
    node_id: str,
    position_x=None,
    position_y=None,
    base: DecisionTree = None,
    updated_by=None,
) -> bool:
    """
    Сохраняет положение блока на холсте веб-редактора.

    **Пишутся только координаты.** Строка узла при этом не переписывается
    целиком намеренно: `rest_upsert` идёт через `merge-duplicates`, то есть
    `insert … on conflict do update` со **списком колонок из payload**, поэтому
    незаписанные поля (заголовок, подсказка, скрытие) остаются прежними. Если
    бы позиция писалась полным набором, перетаскивание блока затирало бы
    правки, сделанные в Telegram-редакторе, — ровно тот дефект, который уже
    исправлялся в `update_node` (правка заголовка стирала описание).

    `base` нужен только для проверки, что узел существует: координаты у
    несуществующего узла создали бы строку-сироту, которую редактор не покажет.

    🔴 **Проверка идёт по живым узлам, а не по коду.** Блок, созданный из веба,
    в `DEFAULT_TREE` отсутствует, и проверка только по коду **не давала его
    перетащить**: холст отвечал `400 unknown node`, хотя блок на карте виден.
    Дефект воспроизведён 13.10.2026 — владелец создал блок и не смог его
    сдвинуть (5 отказов подряд в журнале бота).
    """
    if not overlay_supported():
        return False

    base = base or DEFAULT_TREE

    if node_id not in _known_node_ids(base):
        return False

    row: Dict[str, Any] = {
        "node_id": node_id,
        "position_x": _clean_position(position_x),
        "position_y": _clean_position(position_y),
    }

    if updated_by is not None:
        row["updated_by"] = str(updated_by)

    from sendToDataBase import rest_upsert

    saved = rest_upsert(NODE_TABLE, row, on_conflict="node_id") is not None

    if saved:
        logger.info(
            "intake storage: узел %s поставлен на (%s, %s)",
            node_id, row["position_x"], row["position_y"],
        )

    return saved


def position_supported() -> bool:
    """
    Есть ли колонки координат холста (миграция `0114`).

    **Своя проверка, а не общая с v2.** Наборы колонок приезжают разными
    миграциями, а PostgREST отвергает запрос целиком из-за одной неизвестной
    колонки. Общая проверка означала бы: нет `0114` — «редактор не готов»,
    хотя правки дерева и связи работают. Этот класс дефекта уже стоил
    аналитики журнала (1663 строки без причины), поэтому память здесь своя —
    как у `link_supported`.
    """
    global _position_state, _position_checked_at

    if _position_state is not None and time.time() - _position_checked_at < _V2_TTL_SECONDS:
        return _position_state

    from sendToDataBase import rest_get

    rows = rest_get(
        NODE_TABLE, {"select": "position_x", "limit": "1"}, optional=True
    )
    supported = rows is not None

    _position_state = supported
    _position_checked_at = time.time()

    if not supported:
        logger.warning(
            "intake storage: колонок координат нет — примените миграцию "
            "0114_intake_node_positions.sql, чтобы блоки держали позицию на холсте"
        )

    return supported


def reset_deleted_cache() -> None:
    """Сбросить память о колонке удаления (после применения миграции)."""
    global _deleted_state, _deleted_checked_at

    _deleted_state = None
    _deleted_checked_at = 0.0


def reset_position_cache() -> None:
    """Сбросить память о наличии координат (после применения миграции)."""
    global _position_state, _position_checked_at

    _position_state = None
    _position_checked_at = 0.0


def node_overrides() -> Dict[str, Dict[str, Any]]:
    """Правки узлов по node_id — для редактора."""
    return {
        str(row.get("node_id")): row
        for row in load_node_rows()
        if row.get("node_id")
    }


# ============================================================
# СОЗДАНИЕ И УДАЛЕНИЕ УЗЛОВ (блоков вопросов)
# ============================================================

#: Максимальная длина id узла. Тот же порядок, что у прочих текстовых полей:
#: длинный id не влез бы в callback_data Telegram (лимит 64 байта).
MAX_NODE_ID = 40


def _node_id_for_insertion(
    title: str,
    known: set,
    after_node_id: Optional[str] = None,
    after_option_id: Optional[str] = None,
    base: DecisionTree = None,
) -> str:
    """
    id нового узла: по **месту вставки**, если оно задаёт контракт имён.

    🔴 **Зачем.** Владелец: «если я допустим добавил новый модуль на робота и
    теперь хочу добавить `robots_error_` что то там». Узлы ошибок и причин
    связаны **именами**: `robot_errors_<модуль>` → `ask_cause_<модуль>`. Пока id
    выводился только из заголовка, вставка блока за кнопкой ошибки давала
    `fingers_what_is_wrong` вместо `ask_cause_fingers`, и причины для нового
    модуля **не находились** — оператор попадал в чужие (это и случилось с
    вашей веткой `Fingers`).

    **Правило.** Смотрим, за какой **кнопкой** вставляем:

      * кнопка узла `robot_errors_<модуль>` → создаём узел **причин** этого
        модуля, id `ask_cause_<модуль>`;
      * кнопка узла модулей модели → создаём узел **ошибок**, и модуль берём из
        `option_id` (он уже назван по контракту: `fingers`, `chassis`, …), id
        `robot_errors_<модуль>`;
      * во всех остальных случаях — прежнее поведение (id из заголовка).

    **Заголовок остаётся приоритетным, если он уже даёт правильный id:** человек,
    набравший `ask_cause_fingers` руками, получает ровно его. Это важно и для
    совместимости — старые блоки создавались именно так.
    """
    base = base or DEFAULT_TREE

    # 1. Явный контракт в заголовке — уважаем его.
    from_title = _slug_node_id(title, known)

    if from_title.startswith("robot_errors_") or from_title.startswith(ASK_CAUSE):
        return from_title

    if not after_node_id:
        return from_title

    # 2. Вставка за кнопкой узла ОШИБОК -> это узел ПРИЧИН того же модуля.
    module = module_of_error_node(after_node_id)

    if module:
        candidate = cause_node_id(module)

        return candidate if candidate not in known else _slug_node_id(title, known)

    # 3. Вставка за кнопкой узла МОДУЛЕЙ -> это узел ОШИБОК этого модуля.
    #
    # 🔴 **Хвост берётся из карты модулей, а не из `option_id`.** У K50H модуль
    # называется `Lifting`, а узел ошибок — `robot_errors_lift`: подпись и хвост
    # **намеренно разные** (`cause_node_id` предупреждает об этом же). Слепое
    # `error_node_id("lifting")` дало бы `robot_errors_lifting` — второй узел для
    # того же железа, и правки разошлись бы.
    #
    # **Модуль, добавленный из веба, в карте отсутствует** — тогда хвост берётся
    # из `option_id` (его и задал человек). Ровно ваш случай: «добавил новый
    # модуль на робота и хочу добавить `robot_errors_…`».
    if after_node_id in ROBOT_MODULES and after_option_id:
        tail = module_tail_for_option(after_node_id, str(after_option_id))
        candidate = error_node_id(tail)

        return candidate if candidate not in known else _slug_node_id(title, known)

    return from_title


def _slug_node_id(title: str, taken: set) -> str:
    """
    Идентификатор узла из заголовка: `Мой вопрос?` → `moy_vopros`.

    **Почему из заголовка, а не случайный.** id виден в редакторе и попадает в
    статистику; `node_7f3a` ничего не сказал бы человеку. Кириллица
    транслитерируется грубо (похожие буквы → латиница), потому что id обязан
    состоять из безопасных символов: он уходит в callback_data.

    Совпадение разрешается суффиксом (`_2`), а не отказом: два разных вопроса с
    одинаковым заголовком — обычное дело.
    """
    translit = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
        "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
        "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
        "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ы": "y",
        "э": "e", "ю": "yu", "я": "ya", "ь": "", "ъ": "",
    }
    lowered = str(title or "").casefold()
    mapped = "".join(translit.get(ch, ch) for ch in lowered)
    base = re.sub(r"[^a-z0-9]+", "_", mapped).strip("_")[:MAX_NODE_ID] or "block"

    node_id = base
    suffix = 2

    while node_id in taken:
        node_id = f"{base[:MAX_NODE_ID - 3]}_{suffix}"
        suffix += 1

    return node_id


def add_node(
    title: str,
    node_type: str = "choice",
    description: str = "",
    next_node: Optional[str] = None,
    after_node_id: Optional[str] = None,
    after_option_id: Optional[str] = None,
    created_by=None,
    base: DecisionTree = None,
) -> Optional[str]:
    """
    Создаёт новый блок вопроса и (если сказано) вставляет его в дерево.

    Возвращает id нового узла или `None`, если создать нельзя.

    **Почему вставка `after_*` — часть создания, а не отдельный шаг.** Новый узел
    сам по себе **недостижим**: в дереве нет обратных ссылок, и никто на него не
    ссылается. Создав узел и не подключив его, человек получил бы блок, которого
    оператор никогда не увидит, — и это выглядело бы как «не сохранилось».
    Поэтому подключение обязательно и делается атомарно с созданием.

    **Куда подключаем.** Заменяем переход выбранной кнопки: было
    `кнопка → старый_узел`, стало `кнопка → новый_узел → старый_узел`. Так новая
    ветка аккуратно встаёт в существующий путь, ничего не теряя: продолжение
    остаётся тем же.
    """
    if not overlay_supported():
        return None

    base = base or DEFAULT_TREE
    wanted_type = str(node_type or "").strip().casefold()

    if wanted_type not in NEW_NODE_TYPES:
        logger.warning("intake storage: тип узла %r создать нельзя", node_type)
        return None

    title = _clean(title, MAX_TITLE_LENGTH)

    if not title:
        return None

    known = _known_node_ids(base)

    # 🔴 **id выводится из МЕСТА ВСТАВКИ, а не только из заголовка.**
    #
    # Контракт имён в дереве: узел ошибок модуля — `robot_errors_<модуль>`, узел
    # причин — `ask_cause_<модуль>`. От этих имён зависят **связи**:
    # `error_node_id`/`cause_node_id` собирают их программно, редактор `/tree`
    # ищет причины по префиксу `ask_cause`, а заготовки (`error_templates`)
    # проверяют префикс `robot_errors_`.
    #
    # Раньше id всегда выводился из заголовка, и это ломало вашу задачу
    # «добавил модуль — хочу добавить `robot_errors_…`»: заголовок
    # «Fingers: what is wrong?» давал id `fingers_what_is_wrong`, а не
    # `robot_errors_fingers`, и связь с причинами по имени не складывалась.
    #
    # **Заголовок остаётся приоритетным**, если он уже даёт правильный id: так
    # человек, набравший `robot_errors_fingers` руками, получает ровно его.
    node_id = _node_id_for_insertion(
        title, known,
        after_node_id=after_node_id,
        after_option_id=after_option_id,
        base=base,
    )

    # Продолжение ветки: если не сказано — ведём в финал, чтобы узел не был
    # тупиком (иначе оператор застрянет на новом вопросе).
    target = _clean(next_node, MAX_TITLE_LENGTH) if next_node else None

    if target and target not in known:
        return None

    if not target:
        target = _final_node_id(base)

    row: Dict[str, Any] = {
        "node_id": node_id,
        "node_type": wanted_type,
        "title": title,
        "description": _clean(description, MAX_DESCRIPTION_LENGTH),
        "next_node": target,
        "hidden": False,
        "deleted": False,
    }

    if created_by is not None:
        row["updated_by"] = str(created_by)

    from sendToDataBase import rest_upsert

    if rest_upsert(NODE_TABLE, row, on_conflict="node_id") is None:
        return None

    if after_node_id and after_option_id:
        # Подключаем: кнопка `after_option_id` узла `after_node_id` теперь ведёт
        # в новый узел, а сам новый узел — туда, куда вела кнопка.
        old_target = option_target(base, after_node_id, after_option_id)

        if old_target and not update_node(node_id, next_node=old_target, base=base):
            # Не удалось перевести продолжение — откатываем узел, чтобы не
            # оставить неподключённый блок.
            delete_node(node_id, base=base)
            return None

        if not update_option(
            after_node_id, after_option_id, next_node=node_id, base=base
        ):
            delete_node(node_id, base=base)
            return None

    logger.info("intake storage: создан узел %s (%s)", node_id, wanted_type)

    return node_id


def _final_node_id(base: DecisionTree) -> Optional[str]:
    """Первый финальный узел дерева — куда вести новую ветку по умолчанию."""
    for node in base.nodes.values():
        if node.type == NodeType.FINAL:
            return node.id

    return None


def option_target(
    base: DecisionTree, node_id: str, option_id: str
) -> Optional[str]:
    """Куда ведёт кнопка сейчас — для вставки нового узла в её переход."""
    for item in effective_options(base, node_id, _read_options() or []):
        if item["id"] == option_id:
            return item["next_node"]

    return None


def delete_node(
    node_id: str,
    base: DecisionTree = None,
    updated_by=None,
) -> bool:
    """
    Удаляет блок вопроса.

    **Встроенный узел** (из кода) убирается пометкой `deleted` — его определение
    лежит в `tree_config.py`, и строку из базы не убрать; удаление обратимо
    (`restore_node`).

    **Созданный из веба** удаляется строкой полностью: восстанавливать нечего.

    🔴 **Узел нельзя убрать, если его варианты ведут в разные места.** Тогда
    ссылки на него не сходятся в одну, и «схлопнуть» их некуда: часть дерева
    стала бы недостижимой. Такой отказ — не перестраховка, а сохранение ветки
    (см. `hidden_node_target`). Проверяется **до** записи.
    """
    if not overlay_supported():
        return False

    base = base or DEFAULT_TREE
    rows = load_node_rows() or []
    row = next((r for r in rows if str(r.get("node_id")) == str(node_id)), None)

    if base.node(node_id) is None and row is None:
        return False

    if base.node(node_id) is not None and base.node(node_id).type == NodeType.FINAL:
        # Финал — это запись отчёта. Без него дереву некуда прийти, и подтвердить
        # ошибку стало бы нечем.
        logger.warning("intake storage: финальный узел %s удалить нельзя", node_id)
        return False

    if node_id == base.root_id:
        logger.warning("intake storage: корневой узел удалить нельзя")
        return False

    if hidden_node_target(base, node_id, _read_options() or ()) is None:
        logger.warning(
            "intake storage: узел %s удалить нельзя — варианты ведут в разные "
            "места, ветка потерялась бы", node_id
        )
        return False

    # Созданный из веба: убираем строку целиком. Признак — `node_type` в строке.
    if row is not None and row.get("node_type") and base.node(node_id) is None:
        # 🔴 **Перед удалением переводим ссылки на продолжение ветки.** Узел
        # создавали через `add_node`, который вставил его в переход кнопки
        # (`кнопка → новый узел → продолжение`). Если просто убрать строку,
        # кнопки останутся указывать на несуществующий узел: рабочее дерево
        # обороняется (цель отбрасывается), но **карта показывала бы ссылку в
        # пустоту**, а восстановить исходный переход было бы неоткуда.
        #
        # Куда вести: узел знает своё продолжение (`next_node`), поэтому ссылки
        # возвращаются именно туда — ветка остаётся целой.
        self_target = row.get("next_node")
        self_target = str(self_target) if self_target else _final_node_id(base)

        _repoint_references_to(node_id, self_target, base)

        from sendToDataBase import rest_delete

        # 🔴 **Кнопки удалённого блока убираются вместе с ним.** Строка узла и
        # строки его кнопок — разные таблицы, и удаление узла их не задевает:
        # строки кнопок остались бы в базе навсегда, а их `node_id` указывал бы
        # на несуществующий блок. Дефект найден на живой базе: после удаления
        # семи тестовых блоков осталось **семь сиротских строк**, и они копились
        # бы при каждом создании-удалении.
        for item in effective_options(base, node_id, _read_options() or []):
            rest_delete(
                OPTION_TABLE,
                params={"node_id": f"eq.{node_id}", "option_id": f"eq.{item['id']}"},
            )

        removed = rest_delete(NODE_TABLE, params={"node_id": f"eq.{node_id}"})

        if removed:
            logger.info("intake storage: узел %s удалён (создан из веба)", node_id)

        return removed

    return update_node(node_id, deleted=True, base=base, updated_by=updated_by)


def _repoint_references_to(
    removed_id: str, target: Optional[str], base: DecisionTree
) -> int:
    """
    Переводит все ссылки с удаляемого узла на `target`. Возвращает число правок.

    Ссылки бывают двух видов, и оба нужно перевести:
      * переход **кнопки** (`telegram_intake_options.next_node`);
      * переход **узла** (`telegram_intake_nodes.next_node`) — у текстовых шагов.

    Правки идут через обычные функции записи, поэтому «кто и когда» остаётся в
    базе, а `merge-duplicates` не трогает соседние колонки.
    """
    if not target:
        return 0

    fixed = 0

    for item in effective_options(base, removed_id, _read_options() or []):
        pass  # кнопки удаляемого узла уходят вместе с ним

    # Ищем, кто ссылается на удаляемый узел.
    for node in base.nodes.values():
        if node.id == removed_id:
            continue

        for item in effective_options(base, node.id, _read_options() or []):
            if item["next_node"] == removed_id:
                if update_option(node.id, item["id"], next_node=target, base=base):
                    fixed += 1

        if node.next_node == removed_id:
            if update_node(node.id, next_node=target, base=base):
                fixed += 1

    # И узлы, созданные из веба.
    for row in load_node_rows() or []:
        other = str(row.get("node_id") or "")

        if not other or other == removed_id or base.node(other) is not None:
            continue

        if str(row.get("next_node") or "") == removed_id:
            if update_node(other, next_node=target, base=base):
                fixed += 1

    if fixed:
        logger.info(
            "intake storage: ссылки на узел %s переведены в %s (%d)",
            removed_id, target, fixed,
        )

    return fixed


def restore_node(
    node_id: str,
    base: DecisionTree = None,
    updated_by=None,
) -> bool:
    """Возвращает удалённый узел: `deleted = false`."""
    return update_node(node_id, deleted=False, base=base, updated_by=updated_by)
