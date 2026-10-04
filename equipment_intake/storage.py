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
_COLUMNS_V2 = (
    "node_id,option_id,label,next_node,description,icon,only_for,"
    "is_builtin,hidden,sort_order"
)
_COLUMNS_V1 = "node_id,option_id,label,next_node,description,icon,only_for"
_NODE_COLUMNS = "node_id,title,description,placeholder,stub_hint,next_node,hidden"

# Насколько долго помним результат проверки «есть ли колонки v2». Короткий
# TTL, чтобы после применения миграции бот подхватил её без перезапуска.
_V2_TTL_SECONDS = 120
_v2_state: Optional[bool] = None
_v2_checked_at: float = 0.0

_UNSET = object()


# ============================================================
# ЧТЕНИЕ
# ============================================================

def _remember_v2(supported: bool) -> None:
    global _v2_state, _v2_checked_at

    _v2_state = supported
    _v2_checked_at = time.time()


def _read_options() -> Optional[List[dict]]:
    """Строки оверлея вариантов. None — база недоступна."""
    from sendToDataBase import rest_get

    rows = rest_get(
        OPTION_TABLE,
        {"select": _COLUMNS_V2, "order": "node_id.asc,sort_order.asc,id.asc"},
    )

    if rows is not None:
        _remember_v2(True)
        return [row for row in rows if isinstance(row, dict)]

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
    встроенные узлы без правок.
    """
    from sendToDataBase import rest_get

    rows = rest_get(NODE_TABLE, {"select": _NODE_COLUMNS, "order": "id.asc"})

    if rows is None:
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

    rows = rest_get(OPTION_TABLE, {"select": "is_builtin", "limit": "1"})
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
    global _v2_state, _v2_checked_at

    _v2_state = None
    _v2_checked_at = 0.0


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
        index (позиция в списке).
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

    # Непереданный `hidden` означает «не трогать», а не «показать». Раньше здесь
    # стояло `False`, и правка заголовка молча возвращала скрытый узел в дерево:
    # редактор правит заголовок, не передавая `hidden` (`editor.py`). У вариантов
    # (`update_option`) такое же правило работало с самого начала — здесь оно
    # было упущено.
    current_hidden = False
    override = node_overrides().get(node_id)
    if override is not None:
        current_hidden = _row_hidden(override)

    row: Dict[str, Any] = {
        "node_id": node_id,
        "title": None if title is _UNSET else (_clean(title, MAX_TITLE_LENGTH) or None),
        "description": None if description is _UNSET else _clean(description, MAX_DESCRIPTION_LENGTH),
        "placeholder": None if placeholder is _UNSET else _clean(placeholder, MAX_DESCRIPTION_LENGTH),
        "stub_hint": None if stub_hint is _UNSET else _clean(stub_hint, MAX_DESCRIPTION_LENGTH),
        "next_node": target,
        "hidden": current_hidden if hidden is _UNSET else bool(hidden),
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
