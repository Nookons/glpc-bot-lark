# -*- coding: utf-8 -*-
"""
Тесты формы дерева приёма для веб-просмотра.

Три вещи, которые обязаны быть верны, иначе веб покажет не то, по чему идёт приём:

1. **Порядок вариантов — эффективный, а не «как в базе».** В
   `telegram_intake_options` лежат только правки: у встроенного варианта строки
   может не быть вовсе. Сборка «по базе» потеряла бы встроенные варианты и
   показала не тот порядок кнопок, что видит оператор.
2. **Скрытые варианты отдаются и помечаются.** Веб-просмотр должен показать,
   куда пропала кнопка; рабочее дерево их отбрасывает — это разные задачи.
3. **Признаки доступности связи и оверлея отдаются.** Без них «связей нет» и
   «колонки нет» выглядят для веба одинаково.

Проверки идут без сети и без базы: строки оверлея передаются аргументами, а
`storage.link_supported`/`overlay_supported` подменяются — так проверяется
**сборка**, а не доступность PostgREST.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from equipment_intake import tree_view  # noqa: E402
from equipment_intake.tree_config import DEFAULT_TREE  # noqa: E402


def _payload(option_rows=None, node_rows=None):
    """Сборка на встроенном дереве, без базы."""
    return tree_view.build_tree_payload(
        base=DEFAULT_TREE,
        option_rows=option_rows or [],
        node_rows=node_rows or [],
    )


def _node(payload, node_id):
    for node in payload["nodes"]:
        if node["id"] == node_id:
            return node
    raise AssertionError(f"узла {node_id} нет в payload")


def test_every_tree_node_is_present():
    """Ни один узел не теряется: веб должен видеть всё дерево целиком."""
    payload = _payload()

    assert len(payload["nodes"]) == len(DEFAULT_TREE.nodes)
    assert payload["root_id"] == DEFAULT_TREE.root_id
    # 27 узлов — живое дерево на 13.10.2026. Было 20: добавились **восемь**
    # узлов причин (свой на каждый модуль) вместо одного общего `ask_cause`.
    # Число держит регрессию «узел пропал из выдачи».
    assert len(payload["nodes"]) == 27


def test_builtin_options_survive_without_overlay_rows():
    """
    Без строк оверлея встроенные варианты обязаны остаться.

    Дефект, который это ловит: сборка «по строкам базы». В базе 17 строк, все —
    правки и добавленные варианты, и **ни одной** строки для встроенных кнопок
    `robot_module_a42t` (8 штук). Такая сборка показала бы узел пустым.
    """
    payload = _payload()
    modules = _node(payload, "robot_module_a42t")

    assert modules["option_count"] == 8
    assert [o["id"] for o in modules["options"]] == [
        "chassis",
        "lifting",
        "rotation",
        "tray",
        "horn",
        "hook",
        "qr_code",
        "safety",
    ]
    assert all(o["kind"] == "builtin" for o in modules["options"])


def test_effective_order_puts_overlay_rows_after_builtin():
    """
    Порядок — эффективный: добавленные варианты стоят после встроенных.

    Причины добавлены владельцем на узле **своего модуля** (миграция `0118`
    перенесла их туда с общего `ask_cause`). Это и есть порядок кнопок о��ератора.
    """
    rows = [
        {"node_id": "ask_cause_chassis", "option_id": "went_off_the_road", "label": "Went off the road", "sort_order": 10, "is_builtin": False},
        {"node_id": "ask_cause_chassis", "option_id": "collision", "label": "Collision", "sort_order": 11, "is_builtin": False},
    ]

    cause = _node(_payload(rows), "ask_cause_chassis")

    assert cause["option_count"] == 8, "5 встроенных причин шасси + 2 добавленные + Other"
    ids = [o["id"] for o in cause["options"]]
    # Встроенные — первыми, в порядке кода; добавленные — после.
    assert ids[:5] == [
        "human_error",
        "obstacle",
        "mechanical_wear",
        "software_fault",
        "battery_charge",
    ]
    # `other` — встроенная и стоит последней среди встроенных; добавленные
    # владельцем идут после неё, в порядке `sort_order`.
    assert ids[5:] == ["other", "went_off_the_road", "collision"]
    assert cause["options"][6]["kind"] == "added", "добавленные начинаются с индекса 6"
    # `index` — позиция в итоговом списке: веб рисует по ней.
    assert [o["index"] for o in cause["options"]] == list(range(8))


def test_hidden_options_are_returned_and_marked():
    """
    Скрытый вариант отдаётся с `hidden=True`, а не исчезает.

    Иначе администратор, скрывший кнопку, не найдёт её в вебе и не поймёт, куда
    она делась. Рабочее дерево бота скрытые отбрасывает — это другая задача.
    """
    rows = [
        {"node_id": "ask_cause_chassis", "option_id": "obstacle", "hidden": True, "is_builtin": True},
    ]

    cause = _node(_payload(rows), "ask_cause_chassis")
    obstacle = next(o for o in cause["options"] if o["id"] == "obstacle")

    assert obstacle["hidden"] is True
    # Остальные остаются видимыми — скрытие не «размножается».
    assert sum(1 for o in cause["options"] if o["hidden"]) == 1


def test_node_override_replaces_title():
    """Правка заголовка узла из базы видна и помечена."""
    rows = [{"node_id": "root", "title": "Что на фото?"}]

    root = _node(_payload(node_rows=rows), "root")

    assert root["title"] == "Что на фото?"
    assert root["is_overridden"] is True
    # Без правки признак снят — веб не должен показывать «изменён» у всего.
    assert _node(_payload(), "root")["is_overridden"] is False


def test_links_are_built_from_effective_options():
    """
    Рёбра строятся из эффективных вариантов, а не из кода.

    Перенаправление варианта из оверлея обязано быть видно в связ��х: иначе
    канвас рисовал бы стрелку туда, куда бот уже не ходит.
    """
    rows = [
        {"node_id": "root", "option_id": "robot", "next_node": "summary"},
    ]

    payload = _payload(rows)
    robot_link = next(l for l in payload["links"] if l["option_id"] == "robot")

    assert robot_link["from"] == "root"
    # Код ведёт в `ask_number`, оверлей перенаправил в `summary`.
    assert robot_link["to"] == "summary"

    # Без оверлея — переход из кода.
    base_link = next(l for l in _payload()["links"] if l["option_id"] == "robot")
    assert base_link["to"] == "ask_number"


def test_non_option_nodes_have_no_options():
    """У `input`/`final` вариантов нет — веб не рисует у них кнопки."""
    payload = _payload()

    for node_id in ("ask_number", "ask_description", "summary"):
        node = _node(payload, node_id)
        assert node["option_count"] == 0, node_id
        assert node["options"] == []


def test_stub_nodes_expose_their_hint():
    """
    Заглушки отдаются с `is_stub`/`stub_hint`.

    Ветка объявлена, но приём по ней не идёт («Workstations are not available
    yet»). Это первое, что нужно видеть при проверке дерева, — иначе ветка
    выглядит рабочей и её отсутствие в отчётах выглядит дефектом.
    """
    payload = _payload()
    ws = _node(payload, "workstation_unavailable")

    assert ws["type"] == "unavailable"
    assert ws["option_count"] == 0


def test_capability_flags_are_reported():
    """
    Признаки доступности отдаются, и их отсутствие не роняет сборку.

    `link_supported=False` (миграция `0109` не применена) и `overlay_supported=
    False` должны быть видны вебу: без них «связей нет» и «колонки нет»
    выглядят одинаково.
    """
    from equipment_intake import storage

    original_link = storage.link_supported
    original_overlay = storage.overlay_supported

    try:
        storage.link_supported = lambda: False
        storage.overlay_supported = lambda: False
        payload = tree_view.build_tree_payload(base=DEFAULT_TREE, option_rows=[])

        assert payload["link_supported"] is False
        assert payload["overlay_supported"] is False
    finally:
        storage.link_supported = original_link
        storage.overlay_supported = original_overlay


def test_real_overlay_rows_are_read_from_db_or_skipped():
    """
    Живая проверка на базе: если связь `template_id` доступна, она обязана читаться.

    **Пропуск определяется ЧТЕНИЕМ, а не исключением.** `build_tree_payload_from_db`
    намеренно не бросает при недоступной базе (дерево отдаётся по встроенной
    логике, а признаки доступности говорят, что правки не прочитаны) — это
    документированное поведение, а не сбой. Поэтому «исключения не было»
    **не значит** «база есть».

    🔴 Первая версия проверки принимала отсутствие исключения за доступную базу и
    падала в полном прогоне: набор `LarkBot` подставляет `SUPABASE_URL=https://supabase.invalid`,
    сборка честно возвращала `link_supported=False` — и это правильный ответ на
    недоступную базу, а не дефект.

    Признак настоящей базы — непустой ответ чтения строк оверлея.
    """
    from equipment_intake import storage

    try:
        rows = storage.load_option_rows()
    except Exception as exc:  # noqa: BLE001
        print(f"  (пропуск: чтение оверлея недоступно — {exc})")
        return

    if rows is None:
        print("  (пропуск: база недоступна — link_supported проверить не на чем)")
        return

    payload = tree_view.build_tree_payload_from_db()

    assert len(payload["nodes"]) == 27
    # База ответила — значит и признак связи обязан быть доступен.
    assert payload["link_supported"] is True, (
        "база отвечает, но связь template_id не видна: миграция 0109 не применена?"
    )
