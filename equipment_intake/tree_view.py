# -*- coding: utf-8 -*-
"""
Дерево приёма ошибок в форме, пригодной для веб-просмотра.

**Зачем модуль.** Веб (`tk-new`) должен показывать **то же** дерево, по которому
реально идёт приём ошибок, — иначе он превращается в красивую картинку, не
имеющую отношения к боту. Единственная реализация слияния «код + оверлей» живёт
здесь (`storage.apply_overlay`), поэтому и сборка формы для веба живёт здесь же:
копия в API или в вебе неизбежно разошлась бы, а это ровно тот класс дефекта,
который в проекте уже ловили сторожем `test_cross_repo_parity`.

**Что отдаётся.** Не `DecisionTree.to_dict()` как есть, а форма, удобная
читателю: узлы списком, у каждого — варианты **в эффективном порядке** (с
учётом `sort_order` из оверлея), пометкой происхождения и признаком скрытия.
Порядок вариантов — это то, что видит оператор в кнопках, и веб должен
показывать именно его.

**Почему порядок берётся из `effective_options`, а не из базы напрямую.** В
`telegram_intake_options` лежат **только правки**: у встроенного варианта может
не быть строки вовсе, а `sort_order` задан лишь там, где его меняли. Сортировка
«по базе» потеряла бы встроенные варианты и показала не тот порядок, что в
бота. `effective_options` уже решает это правило — и оно покрыто тестами.

Модуль **без побочных эффектов**: сеть и база читаются вызывающим
(`load_option_rows`, `load_node_rows`) и передаются аргументами, поэтому сборку
можно проверить без базы.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from . import storage
from .tree_config import ASK_CAUSE, DEFAULT_TREE
from .types import DecisionTree, NodeType

#: Типы узлов, у которых есть варианты (кнопки). У `input`/`final` их нет, и
#: веб не должен рисовать у них пустой список кнопок.
OPTION_NODE_TYPES = (NodeType.CHOICE, NodeType.MULTI, NodeType.YESNO)


def _node_type(node: Any) -> str:
    """Тип узла строкой: `NodeType.CHOICE` → `"choice"`."""
    value = getattr(node, "type", None)

    return getattr(value, "value", None) or str(value or "")


def _tree_with_web_nodes(
    base: DecisionTree,
    option_rows: Sequence[Dict[str, Any]],
    node_rows: Sequence[Dict[str, Any]],
) -> DecisionTree:
    """
    Дерево, в которое **добавлены** узлы, созданные из веба.

    Отличие от `storage.build_tree`: ничего не отбрасывается. Скрытые кнопки и
    удалённые узлы остаются — их наличие нужно карте, чтобы человек видел, что он
    скрыл или удалил, и мог вернуть. Рабочее дерево (для оператора) собирает
    `storage.build_tree`, и там фильтрация уместна.

    Удалённые узлы тоже остаются в наборе: карта показывает их как удалённые.
    """
    nodes = dict(base.nodes)

    for row in node_rows:
        node_id = str(row.get("node_id") or "")

        if not node_id or node_id in nodes:
            continue

        created = storage.node_from_rows(node_id, row, option_rows)

        if created is not None:
            nodes[node_id] = created

    return DecisionTree(root_id=base.root_id, nodes=nodes)


def build_tree_payload(
    base: Optional[DecisionTree] = None,
    option_rows: Optional[Sequence[Dict[str, Any]]] = None,
    node_rows: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Собирает дерево для веба: узлы с эффективными вариантами и правками.

    `base` — дерево **без** оверлея (`DEFAULT_TREE`): правки накладываются
    здесь по строкам, чтобы веб видел происхождение каждого значения
    (`kind`: `builtin` / `edited` / `added`), а не только итог.

    Скрытые варианты **включаются** и помечаются `hidden`: веб-просмотр должен
    показывать и их — иначе администратор не поймёт, куда пропала кнопка,
    которую он скрыл в редакторе. Рабочее дерево бота их отбрасывает, и это
    разные задачи.

    Узлы-заглушки (`unavailable`, например «Workstations are not available yet»)
    отдаются как есть: `is_stub` и `stub_hint` объясняют, почему ветка не
    работает, и это первое, что нужно видеть при проверке дерева.

    🔴 **Список узлов берётся из ОБЪЕДИНЁННОГО дерева, а не из кода.** Первая
    версия перебирала `base` (только узлы кода), поэтому блок, созданный в вебе,
    был виден рабочему дереву бота, но **не был виден в самой карте**: создав
    блок, человек не находил его на странице и решал, что создание не работает.
    """
    code_tree = base or DEFAULT_TREE
    rows: Sequence[Dict[str, Any]] = option_rows or []
    node_rows_all: Sequence[Dict[str, Any]] = node_rows or []

    # 🔴 **Набор узлов: код + созданные из веба. Без `build_tree`.**
    #
    # Первая версия брала узлы из `storage.build_tree`, и это оказалось
    # **дефектом**: `build_tree` собирает **рабочее** дерево для оператора и
    # намеренно отбрасывает скрытые кнопки и удалённые узлы. Карте же нужно
    # обратное — она обязана показывать скрытое и удалённое, иначе человек не
    # найдёт кнопку, которую сам скрыл, и не поймёт, куда она делась.
    #
    # Поэтому здесь только **добавляются** узлы, созданные из веба; скрытие и
    # удаление обрабатываются ниже, при разборе строк (как и было до правки).
    tree = _tree_with_web_nodes(code_tree, rows, node_rows_all)
    overrides = {
        str(row.get("node_id")): row
        for row in node_rows_all
        if row.get("node_id")
    }

    nodes: List[Dict[str, Any]] = []

    for node_id, node in tree.nodes.items():
        node_type = _node_type(node)
        override = overrides.get(str(node_id)) or {}

        options: List[Dict[str, Any]] = []

        if getattr(node, "type", None) in OPTION_NODE_TYPES:
            options = storage.effective_options(tree, node_id, rows)

        # Позиция блока на холсте. `None` — человек этот блок ещё не двигал, и
        # раскладку считает веб (слоями по удалённости от корня). Подставлять
        # `0,0` нельзя: существующие узлы собрались бы в один угол при первом
        # открытии карты.
        position_x = storage._clean_position(override.get("position_x"))
        position_y = storage._clean_position(override.get("position_y"))

        nodes.append(
            {
                "id": str(node_id),
                "title": str(
                    override.get("title") or getattr(node, "title", "") or ""
                ),
                "type": node_type,
                "description": str(
                    override.get("description")
                    or getattr(node, "description", "")
                    or ""
                ),
                "key": str(getattr(node, "key", "") or ""),
                # Правка узла из базы: затирает значение из кода.
                "is_overridden": bool(override),
                # Заглушка: ветка объявлена, но приём по ней не идёт.
                "is_stub": bool(getattr(node, "is_stub", False)),
                "stub_hint": str(getattr(node, "stub_hint", "") or ""),
                "option_count": len(options),
                "options": options,
                # Координаты холста: `None` — позиция не задана, считает веб.
                "position_x": position_x,
                "position_y": position_y,
                # Блок создан из веба (в коде бота его нет) — такие блоки
                # удаляются строкой и восстанавливаются из редактора.
                "is_custom": bool(override.get("node_type")),
                # Блок удалён: карта показывает его отдельно, чтобы человек мог
                # вернуть. Рабочее дерево бота удалённые не показывает вовсе.
                "is_deleted": storage._row_deleted(override),
            }
        )

    # Связи отдельным списком: канвасу нужны рёбра, а не только узлы. Строятся
    # из эффективных вариантов, поэтому перенаправление варианта из оверлея
    # видно и здесь.
    links: List[Dict[str, str]] = []
    for node in nodes:
        for option in node["options"]:
            target = option.get("next_node")
            if target:
                links.append(
                    {
                        "from": node["id"],
                        "option_id": str(option.get("id") or ""),
                        "label": str(option.get("label") or ""),
                        "to": str(target),
                    }
                )

    return {
        "root_id": str(tree.root_id),
        "nodes": nodes,
        "links": links,
        # Признаки доступности связи: веб показывает, применена ли миграция
        # `0109`. Без них «нет связей» и «нет колонки» выглядели бы одинаково.
        "link_supported": bool(storage.link_supported()),
        "overlay_supported": bool(storage.overlay_supported()),
        # Доступны ли координаты холста (`0114`). Без них перетаскивание
        # сохранить некуда — веб обязан сказать это прямо, а не терять позицию
        # молча при следующем открытии.
        "position_supported": bool(storage.position_supported()),
        # Доступна ли пометка удаления (`0115`). Без неё встроенную кнопку
        # удалить нельзя (её определение в коде), и веб обязан это показать, а не
        # предлагать кнопку «Удалить», которая молча ничего не сделает.
        "delete_supported": bool(storage._deleted_supported()),
        # 🔴 **Узлы причин — свой на каждый модуль** (`ask_cause_chassis`, …).
        # Отдаём списком, чтобы веб знал, какие блоки считать причинами: по
        # префиксу id это тоже выводимо, но список приходит из дерева и не
        # разойдётся, если узлы переименуют.
        "cause_nodes": sorted(
            node_id for node_id in tree.nodes if str(node_id).startswith(ASK_CAUSE)
        ),
    }


def build_tree_payload_from_db() -> Dict[str, Any]:
    """
    То же, но строки оверлея читаются из базы.

    Ошибка чтения — **не** исключение: дерево отдаётся по встроенной логике
    (`option_rows=None`), а признаки доступности скажут вебу, что правки не
    прочитаны. Приём ошибок и просмотр не должны падать из-за недоступной базы.
    """
    try:
        option_rows = storage.load_option_rows()
    except Exception:  # noqa: BLE001
        option_rows = None

    try:
        node_rows = storage.load_node_rows()
    except Exception:  # noqa: BLE001
        node_rows = None

    return build_tree_payload(
        base=DEFAULT_TREE,
        option_rows=option_rows,
        node_rows=node_rows,
    )
