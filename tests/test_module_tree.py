"""Модули робота разделены по моделям, клавиатура — в две колонки.

**Владелец 07.10.2026:** «нужно как-то сделать модуля отдельно на роботов,
потому что например на K50H мы имеем только 3 модуля это lift, chassis and safety,
больше нету и нету смысла показывать все в таком случае. Нужно сделать кнопки в
два столбца на модулях, так будет лучше, так как модулей много».

**Что было.** Один узел `robot_module` с четырьмя вариантами показывал их **всем
моделям**: у K50H предлагали Rotation и Tray, которых у него нет. Кладовщик либо
выбирал чужой модуль, либо писал «не знаю» — оба варианта портят статистику.

**Почему отдельные узлы, а не `only_for`.** Механизм `only_for` в дереве есть и
работает, но валидатор запрещает **одинаковые id вариантов внутри узла**, а
`lifting` нужен и K50H, и A42T. Отдельные узлы снимают ограничение честно.
"""

from __future__ import annotations

from typing import Any

from equipment_intake.keyboards import node_keyboard
from equipment_intake.tree_config import (
    DEFAULT_TREE,
    MODULE_ERRORS,
    REMOVED_MODULES,
    error_node_id,
)


class _Session:
    """Минимальная сессия для сборки клавиатуры: дерево и текущий узел."""

    def __init__(self, node_id: str) -> None:
        self._node_id = node_id
        self.draft: set[str] = set()

    def current_node(self) -> Any:
        return DEFAULT_TREE.node(self._node_id)

    def visible_options(self, node: Any = None) -> list[Any]:
        return list((node or self.current_node()).options)


def _labels(node_id: str) -> list[str]:
    return [option.label for option in DEFAULT_TREE.node(node_id).options]


class TestModulesPerModel:
    def test_k50h_has_its_own_four_modules(self) -> None:
        """K50H: Chassis, QR Code, Safety, Lifting — по схеме владельца.

        **Ожидание изменилось 12.10.2026.** Прежний тест требовал **три**
        модуля (Lifting, Chassis, Safety) — это была редакция 07.10.2026, когда
        владелец сказал «на K50H мы имеем только 3 модуля». Схема 12.10.2026
        называет **четыре**: добавлен `QR Code`, и он есть в рамке K50H на
        рисунке. Порядок — как на схеме.

        Поворот, полка, рог и крюк у этой модели по-прежнему отсутствуют.
        """
        labels = _labels("robot_module_k50h")

        assert labels == ["Chassis", "QR Code", "Safety", "Lifting"]

        for absent in ("Rotation", "Tray", "Horn", "Hook", "Lift"):
            assert absent not in labels, f"у K50H нет модуля {absent}"

    def test_a42t_has_its_own_eight_modules(self) -> None:
        """A42T: восемь модулей, порядок — со схемы владельца.

        **Дубли со схемы убраны.** На рисунке `ROTATE` и `QR CODE` написаны
        дважды; это ошибка рисования, а не два разных модуля. Порядок сохранён:
        сначала основные модули, затем `QR CODE` и `SAFETY`.
        """
        labels = _labels("robot_module_a42t")

        assert labels == [
            "Chassis", "Lifting", "Rotation", "Tray",
            "Horn", "Hook", "QR Code", "Safety",
        ]
        assert labels.count("Rotation") == 1, "на схеме дубль — в дереве его быть не должно"
        assert labels.count("QR Code") == 1, "на схеме дубль — в дереве его быть не должно"

    def test_a42t_c2_is_a_separate_branch(self) -> None:
        """A42T C2 — **свой** узел модулей, а не слияние с A42T.

        Решение владельца 12.10.2026: «A42T C2 — ОТДЕЛЬНАЯ ветка (не слияние с
        A42T), со своим списком модулей». Прежний тест требовал обратного —
        `a42t_c2 -> robot_module_a42t`; ожидание изменено по решению владельца.
        Списки совпадают по составу, но узлы **разные**: иначе в отчёте нельзя
        отличить C2 от A42T, а правка редактора для одной модели меняла бы обе.
        """
        assert _labels("robot_module_a42t_c2") == _labels("robot_module_a42t")
        assert DEFAULT_TREE.node("robot_module_a42t_c2") is not DEFAULT_TREE.node("robot_module_a42t")

    def test_legacy_modules_are_removed_not_hidden(self) -> None:
        """Старые модули удалены **совсем**, а не скрыты.

        Решение владельца 12.10.2026: «Старые модули УДАЛИТЬ СОВСЕМ (не
        скрыть): Not a module, Personally, Bummer, Ground camera, Lidar,
        Fingers». Прежний тест требовал обратного — что «Not a module» **есть**
        у обеих моделей; ожидание изменено по решению владельца.

        История журнала при этом не трогается: записи (42 «Not a module», 12
        «Personally», 11 «Bummer») остаются в базе как есть.
        """
        for node_id in ("robot_module_k50h", "robot_module_a42t", "robot_module_a42t_c2"):
            ids = {option.id for option in DEFAULT_TREE.node(node_id).options}

            for gone in REMOVED_MODULES:
                assert gone not in ids, f"{gone} всё ещё в {node_id}"

    def test_model_leads_to_its_own_module_list(self) -> None:
        """Каждая модель ведёт в **свой** список модулей."""
        node = DEFAULT_TREE.node("robot_type")
        targets = {option.id: option.next_node for option in node.options}

        assert targets["k50h"] == "robot_module_k50h"
        assert targets["a42t"] == "robot_module_a42t"
        assert targets["a42t_c2"] == "robot_module_a42t_c2"
        assert len(set(targets.values())) == 3, "у каждой модели свой узел"

    def test_model_order_follows_the_scheme(self) -> None:
        """Порядок моделей на схеме: K50H, A42T, A42T C2."""
        ids = [option.id for option in DEFAULT_TREE.node("robot_type").options]

        assert ids == ["k50h", "a42t", "a42t_c2"]


class TestErrorLayer:
    """**Под каждым модулем — список его ошибок.** Главное требование 12.10.2026.

    До этой правки в дереве было 13 узлов и **ни одного** узла ошибок: модуль
    вёл сразу на `ask_number`, и «что сломалось» оператор описывал только
    свободным текстом.
    """

    def test_every_module_leads_to_an_error_node(self) -> None:
        """Ни один модуль не ведёт прямо на номер: сначала ошибка."""
        for node_id in ("robot_module_k50h", "robot_module_a42t", "robot_module_a42t_c2"):
            for option in DEFAULT_TREE.node(node_id).options:
                assert option.next_node.startswith("robot_errors_"), (
                    f"{node_id}/{option.id} ведёт на {option.next_node} — "
                    "слой ошибок пропущен"
                )
                assert DEFAULT_TREE.node(option.next_node) is not None

    def test_error_ids_are_prefixed_by_module(self) -> None:
        """id вариантов ошибок — `<module>_<slug>` (контракт для шаблонов)."""
        for module, options in MODULE_ERRORS.items():
            node = DEFAULT_TREE.node(error_node_id(module))

            for option_id, label in options:
                assert option_id.startswith(module + "_"), option_id
                assert node.option(option_id) is not None, option_id
                assert node.option(option_id).label == label

    def test_error_texts_are_the_owners_wording(self) -> None:
        """Тексты ошибок — дословно со схемы владельца (по ним идёт связь)."""
        expected = {
            "robot_errors_chassis": ["Went off the road"],
            "robot_errors_lift": ["Can't lift up/down the fork"],
            "robot_errors_rotate": ["Can't rotate fork"],
            "robot_errors_tray": ["Can't extend/expand the tray"],
            "robot_errors_horn": ["Can't extend/expand the HORN"],
            "robot_errors_hook": ["Can't extend/expand the Hook"],
            "robot_errors_qr_code": ["QR Code dirty"],
            "robot_errors_safety": [
                "Front emergency button is trigger",
                "Rear emergency button is trigger",
                "Safety bumper trigger",
                "Safety system disconnect",
            ],
        }

        for node_id, labels in expected.items():
            assert _labels(node_id) == labels, node_id

    def test_k50h_lifting_carries_the_lift_error(self) -> None:
        """У K50H модуль `Lifting`, а ошибка — как у `Lift` A42T.

        Прямое указание владельца: «Для `Lifting` у K50H — ошибка Can't
        lift up/down the fork (как LIFT у A42T)». Узел ошибок при этом **один на
        модуль**: одинаковое железо не должно иметь двух расходящихся списков.
        """
        lifting = DEFAULT_TREE.node("robot_module_k50h").option("lifting")
        lift = DEFAULT_TREE.node("robot_module_a42t").option("lifting")

        assert lifting.next_node == lift.next_node == "robot_errors_lift"
        assert _labels("robot_errors_lift") == ["Can't lift up/down the fork"]

    def test_shared_modules_share_the_error_node(self) -> None:
        """Одинаковые модули разных моделей ведут в один узел ошибок.

        Железо и неисправности те же, и два узла с тем же содержимым разошлись
        бы при первой же правке.
        """
        for module_id in ("chassis", "safety", "qr_code"):
            targets = {
                DEFAULT_TREE.node(node_id).option(module_id).next_node
                for node_id in ("robot_module_k50h", "robot_module_a42t", "robot_module_a42t_c2")
            }

            assert targets == {f"robot_errors_{module_id}"}, module_id

    def test_error_is_asked_after_the_device_number(self) -> None:
        """**Новый порядок 12.10.2026:** номер сразу после категории, затем модель,
        модуль и ошибка.

        Схема владельца: «Ask device number» стоит **сразу после** выбора
        категории, «Check device number on server» — **между** номером и моделью.
        Владелец, дословно: «после фото **не спрашивает сразу номер робота**».

        Прежний тест кодировал обратный порядок (ошибка → номер) и требовал
        `next_node == "ask_number"` от ошибок. Ожидание **обновлено осмысленно**:
        оно противоречило новой схеме, а не подтверждало её.
        """
        # Корень робота спрашивает номер ПЕРВЫМ шагом.
        assert DEFAULT_TREE.node("root").option("robot").next_node == "ask_number"

        # После номера — модель, а не описание: проверка на сервере стоит между.
        assert DEFAULT_TREE.node("ask_number").next_node == "robot_type"

        # Ошибка ведёт сразу к причине: номер уже введён в начале пути.
        for module in MODULE_ERRORS:
            node = DEFAULT_TREE.node(error_node_id(module))

            for option in node.options:
                assert option.next_node == "ask_cause"

    def test_qr_branch_skips_the_robot_model(self) -> None:
        """У QR модели/модуля нет: код места/полки — уже всё, что известно.

        Ветка QR тоже идёт через `ask_number` (он один на дерево), но после
        номера — сразу причина. Без развилки `next_node_by_kind` оператор после
        кода полки выбирал бы модель робота.
        """
        node = DEFAULT_TREE.node("ask_number")
        kinds = dict(node.next_node_by_kind)

        assert kinds.get("qr_floor") == "ask_cause"
        assert kinds.get("qr_shelf") == "ask_cause"

    def test_error_answer_is_recorded_under_its_own_key(self) -> None:
        """Ответ об ошибке попадает в результат с ключом `error`."""
        node = DEFAULT_TREE.node("robot_errors_chassis")

        assert node.answer_key() == "error"
        assert node.summary_label == "Error"

    def test_full_walk_records_module_and_error(self) -> None:
        """Сквозной проход: **номер → модель → модуль → ошибка → причина**.

        **Порядок изменился 12.10.2026** (схема владельца): номер спрашивается
        сразу после категории, а не после ошибки. Прежний проход начинался с
        модели (`robot → a42t → tray → …`) и на новом дереве падал уже на втором
        шаге: `a42t` — это вариант **модели**, а после `robot` бот спрашивает
        номер. Ожидание обновлено вслед за деревом.
        """
        from equipment_intake.engine import Session

        session = Session(tree=DEFAULT_TREE)

        session.select("robot")
        session.submit_text("3780")

        for step in ("a42t", "tray", "tray_cant_extend"):
            session.select(step)

        session.select("obstacle")

        assert session.is_completed
        answers = session.answers()

        assert answers["device_type"] == "A42T"
        assert answers["module"] == "Tray"
        assert answers["error"] == "Can't extend/expand the tray"
        assert answers["device_number"] == "3780"
        assert answers["cause"] == "Obstacle on the path"
        # Описание на обычном пути не спрашивается — оно только для `Other`.
        assert "description" not in answers


class TestKeyboardColumns:
    """Раскладка вариантов — требование владельца 12.10.2026.

    «Ошибки на модулях должны идти в два ряда по 6 за раз и должна быть кнопка
    далее на два ряда внизу если их больше». Значит: до шести вариантов — один
    ряд, 7–12 — два ряда по шесть, больше 12 — страницы с кнопкой Next.

    **Числа изменились вместе с деревом.** У A42T теперь **восемь** модулей
    (было четыре): семь попадают в первый ряд по шесть, восьмой — во второй.
    Проверки ниже считают фактические числа, а не прежние.
    """

    def test_eight_options_use_two_rows(self) -> None:
        """Восемь модулей A42T — два ряда по шесть, без страниц."""
        rows = node_keyboard(_Session("robot_module_a42t"))["inline_keyboard"]
        option_rows = [row for row in rows if "Cancel" not in row[0]["text"]]

        assert len(option_rows) == 2, "восемь модулей обязаны лечь в два ряда"
        assert len(option_rows[0]) == 6, "первый ряд — шесть кнопок"
        assert len(option_rows[1]) == 2, "во втором ряду — остаток"
        assert all(len(row) <= 6 for row in rows), "в ряду не больше шести кнопок"

    def test_three_options_stay_single_row(self) -> None:
        """Три варианта — один ряд, без дробления."""
        rows = node_keyboard(_Session("robot_type"))["inline_keyboard"]
        option_rows = [row for row in rows if "Cancel" not in row[0]["text"]]

        assert len(option_rows) == 1
        assert len(option_rows[0]) == 3

    def test_layout_keeps_every_option(self) -> None:
        """Раскладка меняет вид, но **не теряет** варианты."""
        rows = node_keyboard(_Session("robot_module_a42t"))["inline_keyboard"]
        # Последняя строка — Cancel, она есть на любом шаге и вариантом не
        # является. Считаю только строки вариантов.
        option_rows = [row for row in rows if "Cancel" not in row[0]["text"]]
        shown = sum(len(row) for row in option_rows)

        assert shown == len(_labels("robot_module_a42t"))

    def test_safety_stays_on_one_screen(self) -> None:
        """Четыре ошибки Safety видны **без листания** — и читаемы целиком.

        **Ожидание изменилось 12.10.2026.** Раньше здесь было `len == 1`: все
        четыре ошибки помещались в **один ряд из шести** колонок. Но владелец
        прислал скриншот живого бота, где причины показывались как `Huma…`,
        `Obsta…`, `Softw…` — **прочитать выбор было невозможно**: у Safety
        подписи до **33 символов** («Front emergency button is trigger»), а в
        шести колонках влезает ≈ 7 знаков.

        Подписи длиннее 12 символов теперь идут **по три в ряд** (`keyboards.py`,
        `SINGLE_LINE_LABEL_LIMIT`), поэтому четыре ошибки занимают **два ряда**.
        🔴 **Требование владельца не нарушено:** «два ряда» и было его условием —
        важно, что **листать не нужно**, а не число рядов.
        """
        rows = node_keyboard(_Session("robot_errors_safety"))["inline_keyboard"]
        option_rows = [row for row in rows if "Cancel" not in row[0]["text"]]

        # Помещаются на один экран: два ряда вариантов + Cancel, без пейджера.
        assert len(option_rows) == 2
        assert sum(len(row) for row in option_rows) == 4

        labels = [button["text"] for row in rows for button in row]

        assert not any("Next" in text or "Back" in text for text in labels)

        # 🔴 **Главное — подписи читаемы.** Ровно это и сломалось в проде:
        # обрезка превращала выбор в `Huma…`, и оператор выбирал вслепую.
        texts = [button["text"] for row in option_rows for button in row]

        assert all("…" not in text for text in texts), (
            "подпись обрезана — оператор не прочитает, что выбирает"
        )


class TestCauseQuestion:
    def test_description_is_only_reached_through_other(self) -> None:
        """Описание спрашивается **только** при причине `Other`.

        **Владелец 12.10.2026, дословно:** «убери вопросы об описание ошибки оно
        нужно **только если other выберет**». Обычная причина ведёт сразу к
        итогу, и только `other` открывает свободный текст.

        Узел `ask_description` при этом **не удалён**: он ведёт к `summary`.
        """
        options = {option.id: option for option in DEFAULT_TREE.node("ask_cause").options}

        assert options["other"].next_node == "ask_description", (
            "Other обязан вести на описание — иначе вопрос пропал бы совсем"
        )

        for option_id, option in options.items():
            if option_id == "other":
                continue
            assert option.next_node == "summary", (
                f"причина {option_id} должна вести сразу к итогу"
            )

        # Описание из дерева не выброшено: оно завершает ветку `Other`.
        assert DEFAULT_TREE.node("ask_description").next_node == "summary"

    def test_cause_has_options(self) -> None:
        options = DEFAULT_TREE.node("ask_cause").options

        assert len(options) >= 4, "список причин пуст или слишком короткий"
        # «Other» обязателен: закрытый список без него заставляет выбирать
        # неточное и портит статистику.
        assert any(option.id == "other" for option in options)

    def test_cause_is_recorded_in_answers(self) -> None:
        """Ответ о причине попадает в результат с ключом `cause`."""
        assert DEFAULT_TREE.node("ask_cause").answer_key() == "cause"


class TestEditorExplainsTheStructure:
    """Редактор показывает, **чем** достигается узел и для какой он модели.

    **Владелец 07.10.2026:** «не понятно как настроить модули на роботах, текста
    одинаковые и не гибкий бот получается. Нужно улучшить логику этого всего
    чтобы было просто и понятно но и эффективно».

    **Причина.** Разделив модули по моделям, я дал обоим узлам **один и тот же**
    заголовок «Which robot module?». В редакторе они выглядели как две
    одинаковые строки, различимые только техническим id в скобках, �� **не было
    видно, чем узел достигается** — то есть что модули K50H открываются после
    выбора модели K50H.
    """

    def test_module_nodes_have_distinct_titles(self) -> None:
        """Заголовки узлов модулей **различаются** и называют модель."""
        k50h = DEFAULT_TREE.node("robot_module_k50h").title
        a42t = DEFAULT_TREE.node("robot_module_a42t").title

        assert k50h != a42t, "заголовки не должны совпадать"

        for title, model in ((k50h, "K50H"), (a42t, "A42T")):
            assert model in title, f"заголовок обязан называть модель: {title}"

    def test_structure_shows_how_a_node_is_reached(self) -> None:
        """Список структуры говорит, после какого шага открывается узел."""
        from equipment_intake.editor import _structure_screen

        text, _ = _structure_screen(DEFAULT_TREE, 0)

        assert "after K50H" in text, "не видно, чем достигается узел K50H"
        assert "after A42T" in text, "не видно, чем достигается узел A42T"

    def test_node_card_shows_the_context(self) -> None:
        """Карточка правки узла называет, где этот шаг показывается."""
        from equipment_intake.editor import _node_edit_caption

        node = DEFAULT_TREE.node("robot_module_k50h")
        caption = _node_edit_caption(DEFAULT_TREE, node, "admin")

        assert "shown after K50H" in caption

    def test_root_has_no_parent_context(self) -> None:
        """У корня нет «после чего» — и это не должно выглядеть поломкой."""
        from equipment_intake.editor import _context_label, _node_parents

        parents = _node_parents(DEFAULT_TREE)

        assert _context_label(parents, DEFAULT_TREE, DEFAULT_TREE.root_id) == "start"

    def test_module_lists_stay_independent(self) -> None:
        """У моделей **разные** списки модулей.

        Без этого «настройка модулей» снова превратилась бы в один общий список,
        где у K50H предлагают поворот, которого у него нет.

        **Ожидание изменилось 12.10.2026.** Прежняя редакция утверждала, что
        `Safety` есть **только** у K50H. По схеме владельца `SAFETY` есть у всех
        трёх моделей, и это единственный набор, который у них общий; различие
        K50H — в отсутствии поворота, полки, рога и крюка.
        """
        k50h = {o.label for o in DEFAULT_TREE.node("robot_module_k50h").options}
        a42t = {o.label for o in DEFAULT_TREE.node("robot_module_a42t").options}

        # Общее у моделей — chassis, QR Code и safety.
        assert {"Chassis", "QR Code", "Safety"} <= k50h
        assert {"Chassis", "QR Code", "Safety"} <= a42t

        # Различие K50H: этих модулей у него нет.
        assert "Rotation" in a42t
        assert "Rotation" not in k50h, "Rotation — только у A42T"
        assert "Tray" not in k50h
        assert "Horn" not in k50h
        assert "Hook" not in k50h

        # Наборы не совпадают: иначе разделение по моделям потеряло бы смысл.
        assert k50h != a42t
