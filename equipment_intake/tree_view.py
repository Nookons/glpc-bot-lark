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
                # 🔴 **Переходы узла — чтобы карта могла нарисовать рёбра.**
                #
                # Переход в дереве живёт **тремя** способами, и раньше карта
                # знала только кнопки. Из-за этого цепочка от фото обрывалась:
                # после «Device number» ничего не было видно, хотя диалог идёт
                # дальше.
                #
                # `next_node` — текстовый шаг (номер → следующий блок);
                # `next_node_by_kind` — ветка, идущая по-разному в зависимости от
                # ранее выбранного варианта (код полки и код места ведут в разные
                # места). Ни то, ни другое кнопкой **не является**, поэтому в
                # `options` их нет.
                "next_node": (
                    str(node.next_node) if getattr(node, "next_node", None) else None
                ),
                # Вид вводимого значения (`robot`, `station`, `workstation`,
                # `qr_floor`, `qr_shelf`). Нужен карте **для подписи условного
                # перехода**: если у узла есть переходы по виду, его собственный
                # переход относится к этому виду. Без подписи линия от «Device
                # number» к модели робота читалась как «так идёт всегда».
                "answer_kind": str(getattr(node, "answer_kind", "") or ""),
                "next_node_by_kind": [
                    [str(kind), str(target)]
                    for kind, target in (getattr(node, "next_node_by_kind", ()) or ())
                    if target
                ],
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
    # 🔴 **Рисуются ТРИ вида переходов, а не один** (дефект найден 14.10.2026:
    # владелец — «с момента фотографии логика канваса немного поломана, Device
    # number и QR code number так неправильно соединено»).
    #
    # Переход в дереве живёт **тремя** способами, и раньше карта знала только
    # первый:
    #
    # 1. **кнопка** (`option.next_node`) — рисуется по подписи;
    # 2. **переход самого узла** (`node.next_node`) — так идут **текстовые** шаги:
    #    ввёл номер → следующий блок. Кнопки у них нет, поэтому карта не рисовала
    #    ничего, и цепочка «Device number → … → итог» **обрывалась**;
    # 3. **переход по виду** (`next_node_by_kind`) — так одна и та же ветка идёт
    #    по-разному: после кода **полки** и **места** диалог завершается по-своему
    #    (`ask_number` → `ask_cause_qr_code`). Это тоже не кнопка.
    #
    # Замер до правки: **8** переходов самого узла и **2** по виду не рисовались —
    # десять связей из ста восемнадцати. Выглядело это ровно как «логика
    # поломана»: блоки есть, а как они соединены — не видно.
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
                        "kind": "option",
                    }
                )

    # Переход **самого узла** — текстовые шаги: ввёл номер → следующий блок.
    #
    # 🔴 **Если у узла есть переходы по виду, его переход — УСЛОВНЫЙ, и это
    # важно показать.** Дефект найден 14.10.2026 со слов владельца: «Device
    # number и QR code number так неправильно соединено».
    #
    # `ask_number` ведёт на `robot_type`, **но только для ветки робота**: для кода
    # полки и кода места срабатывает переход по виду (`next_node_by_kind`). Пока
    # карта рисовала этот переход **сплошной линией без подписи**, выглядело так,
    # будто после ввода кода места бот спрашивает модель робота. На самом деле не
    # спрашивает.
    #
    # Поэтому такой переход помечается как условный (`kind`) и подписывается
    # **видом**, к которому относится: у `ask_number` это `robot` (его
    # `answer_kind`). Веб рисует условные переходы пунктиром, и разница видна.
    for node in nodes:
        target = node.get("next_node")
        if not target:
            continue

        conditional = bool(node.get("next_node_by_kind"))
        # Вид для подписи: у `ask_number` это `robot` — тот самый случай, когда
        # переход по виду не срабатывает и работает переход узла.
        default_kind = str(node.get("answer_kind") or "").strip() if conditional else ""

        links.append(
            {
                "from": node["id"],
                "option_id": default_kind or "",
                "label": default_kind or "",
                "to": str(target),
                "kind": "kind" if conditional else "node",
            }
        )

    # Переход **по виду** — одна ветка идёт по-разному в зависимости от
    # выбранного ранее варианта. `kind` в подписи нужен человеку: без него
    # два ребра от одного блока к разным целям выглядели бы ошибкой.
    for node in nodes:
        for kind, target in node.get("next_node_by_kind") or ():
            if not target:
                continue
            links.append(
                {
                    "from": node["id"],
                    "option_id": str(kind),
                    "label": str(kind),
                    "to": str(target),
                    "kind": "kind",
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
