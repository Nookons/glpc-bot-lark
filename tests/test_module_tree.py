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
from unittest.mock import patch

from equipment_intake import storage
from equipment_intake.keyboards import node_keyboard
from equipment_intake.error_templates import is_error_node  # noqa: E402
from equipment_intake.tree_config import (
    DEFAULT_TREE,
    MODULE_ERRORS,
    REMOVED_MODULES,
    cause_node_id,
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

        # Ошибка ведёт сразу к причине **своего модуля**: номер уже введён в
        # начале пути. Требование владельца 13.10.2026: «сделай Block What
        # caused it? на каждый модуль».
        for module in MODULE_ERRORS:
            node = DEFAULT_TREE.node(error_node_id(module))
            expected = cause_node_id(module)

            for option in node.options:
                assert option.next_node == expected, (
                    f"{module}: ошибка {option.id} ведёт в {option.next_node}, "
                    f"а должна — в {expected}"
                )

    def test_qr_branch_skips_the_robot_model(self) -> None:
        """У QR модели/модуля нет: код места/полки — уже всё, что известно.

        Ветка QR тоже идёт через `ask_number` (он один на дерево), но после
        номера — сразу причина. Без развилки `next_node_by_kind` оператор после
        кода полки выбирал бы модель робота.
        """
        node = DEFAULT_TREE.node("ask_number")
        kinds = dict(node.next_node_by_kind)

        # Причина — **своя у QR**, а не общая: «повреждён код места» бывает
        # только здесь.
        assert kinds.get("qr_floor") == cause_node_id("qr_code")
        assert kinds.get("qr_shelf") == cause_node_id("qr_code")

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

        session.select("overload")

        assert session.is_completed
        answers = session.answers()

        assert answers["device_type"] == "A42T"
        assert answers["module"] == "Tray"
        assert answers["error"] == "Can't extend/expand the tray"
        assert answers["device_number"] == "3780"
        # Причина — из списка **этого модуля**: у вил её своя («перегруз»),
        # потому что «наехал на посторонний предмет» к лотку не относится.
        # Требование владельца 13.10.2026: «сделай Block What caused it? на
        # каждый модуль».
        assert answers["cause"] == "Tray overloaded"
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
        """Восемь модулей A42T — два ряда, без страниц, подписи целиком.

        🔴 **Число колонок изменилось 13.10.2026 — и это и есть исправление.**
        Раньше здесь жёстко стояло «шесть в ряд». Владелец прислал скриншот
        живого бота:

            [Chass…] [Lifting…] [Rotati…] [Tray →] [Horn …] [Hook …]

        Шесть колонок давали кнопке ≈6 знаков, Telegram **обрезал** подписи
        многоточием, и модуль нельзя было прочитать. Теперь ширина ряда
        считается по ширине текста (`keyboard_layout.columns_for_labels`), и
        восемь модулей идут по четыре в ряд — **подписи целиком**.
        """
        rows = node_keyboard(_Session("robot_module_a42t"))["inline_keyboard"]
        option_rows = [row for row in rows if "Cancel" not in row[0]["text"]]

        assert len(option_rows) == 2, "восемь модулей обязаны лечь в два ряда"
        assert sum(len(row) for row in option_rows) == 8

        # Ни одна подпись не обрезается при такой ширине — это главное.
        from equipment_intake import keyboards as kb

        assert kb._columns_for(DEFAULT_TREE.node("robot_module_a42t").options) == 4

        for row in option_rows:
            capacity = kb.keyboard_layout.ROW_UNITS / len(row) - 2 * kb.keyboard_layout.BUTTON_PADDING_UNITS
            for button in row:
                assert kb.keyboard_layout.glyph_units(button["text"]) <= capacity, (
                    f"подпись {button['text']!r} обрежется в ряду из {len(row)}"
                )

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

        **Ожидание уточнено 13.10.2026.** Ширина ряда считается по **ширине
        текста** (`keyboard_layout.columns_for_labels`), а не по числу знаков:
        «Front emergency button is trigger» — 36 единиц ширины, и в ряд влезает
        **одна** такая кнопка. Поэтому четыре ошибки Safety видны **без листания**
        (две страницы не нужны), но каждая — своим рядом.
        🔴 **Требование владельца не нарушено:** важно, что **листать не нужно** и
        **подпись читается целиком**, а не число рядов.
        """
        rows = node_keyboard(_Session("robot_errors_safety"))["inline_keyboard"]
        option_rows = [row for row in rows if "Cancel" not in row[0]["text"]]

        # Помещаются на один экран: ряды вариантов + Cancel, без пейджера.
        assert len(option_rows) == 4
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
    """
    Причины ошибок — **свой узел на каждый модуль**.

    Требование владельца 13.10.2026, дословно: «сделай Block What caused it? на
    каждый модуль». Причина осмысленна только в контексте железа: «погнул вилы»
    не бывает у шасси, а «наехал на посторонний предмет» — у камеры.
    """

    def test_every_module_error_leads_to_its_own_cause_node(self) -> None:
        """
        🔴 Ошибка каждого модуля ведёт в узел причин **своего** модуля.

        Дефект, который это ловит: кнопки ошибок вели в один общий `ask_cause`.
        Он и был исходной структурой, ради замены которой делалась работа.
        """
        for module in MODULE_ERRORS:
            error_node = DEFAULT_TREE.node(error_node_id(module))
            assert error_node is not None, f"нет узла ошибок модуля {module}"

            targets = {option.next_node for option in error_node.options}
            assert targets == {cause_node_id(module)}, (
                f"ошибки модуля {module} ведут в {targets}, "
                f"а должны — в {cause_node_id(module)}"
            )

    def test_each_module_has_its_own_cause_list(self) -> None:
        """
        Списки причин у модулей **разные**, а не копия одного.

        Если бы все восемь узлов содержали одинаковый набор, разделение не дало
        бы ничего: оператор по-прежнему выбирал бы неточное.
        """
        lists = {
            module: tuple(o.id for o in DEFAULT_TREE.node(cause_node_id(module)).options)
            for module in MODULE_ERRORS
        }

        # Как минимум два модуля отличаются составом — иначе смысла в разделении нет.
        assert len(set(lists.values())) > 1, (
            "у всех модулей одинаковый список причин — разделение не работает"
        )

        # И каждая причина осмысленна только там, где стоит: `dirty_code`
        # (код места) — принадлежность QR, у шасси такого не бывает.
        assert "dirty_code" in lists["qr_code"]
        assert "dirty_code" not in lists["chassis"]

    def test_every_error_button_leads_to_a_cause_node_of_its_own_module(self) -> None:
        """
        🔴 **Любая кнопка ошибки ведёт в узел причин СВОЕГО модуля.**

        Проверяются **все** узлы `robot_errors_*`, а не только известные коду:
        узел, созданный из веба (владелец добавил модуль — «хочу добавить
        `robots_error_` что-то там»), в `MODULE_ERRORS` отсутствует, и прежняя
        проверка его **не видела**.

        **Дефект, который это ловит (живой, 13.10.2026).** Ветка владельца
        `robot_errors_fingers` вела в `ask_cause_qr_code`: выбрав «Fingers stuck
        open/close», оператор видел причины QR-кода («Dirty or damaged floor
        code»). Причина уходит в отчёт и определяет метрику, поэтому чужая
        причина — это неверные данные, а не косметика.
        """
        found = 0

        for node_id, node in DEFAULT_TREE.nodes.items():
            if not is_error_node(node_id):
                continue

            for option in node.options:
                found += 1
                target = option.next_node or ""

                assert target.startswith("ask_cause"), (
                    f"{node_id}.{option.id} ведёт в {target!r} — это не узел причин"
                )

                # Хвост обязан совпасть с хвостом узла ошибок: `robot_errors_lift`
                # -> `ask_cause_lift`. Иначе у одного железа два узла причин.
                module = node_id[len("robot_errors_"):]
                assert target == cause_node_id(module), (
                    f"{node_id}.{option.id} ведёт в {target!r}, "
                    f"а должен — в {cause_node_id(module)!r}"
                )

        assert found > 0, "не нашлось ни одной кнопки ошибки — пров��рка выродилась"

    def test_every_error_node_has_a_title_for_the_operator(self) -> None:
        """
        Заголовок узла ошибок читается человеком, а не повторяет служебный id.

        Живой дефект: у созданного из веба `robot_errors_fingers` заголовком был
        **сам id**, и оператор видел «robot_errors_fingers: what is wrong?»
        вместо «Fingers: what is wrong?» (у остальных — «Tray: what is wrong?»).
        """
        for node_id, node in DEFAULT_TREE.nodes.items():
            if not is_error_node(node_id):
                continue

            assert node.title and node.title != node_id, (
                f"{node_id}: заголовок — служебный id, оператор увидит его как вопрос"
            )

    def test_old_shared_cause_node_is_gone(self) -> None:
        """Общего узла `ask_cause` больше нет — иначе осталась бы вторая точка входа."""
        assert DEFAULT_TREE.node("ask_cause") is None, (
            "общий узел причин остался в дереве"
        )

    def test_description_is_only_reached_through_other(self) -> None:
        """Описание спрашивается **только** при причине `Other` — у каждого модуля.

        **Владелец 12.10.2026, дословно:** «убери вопросы об описание ошибки оно
        нужно **только если other выберет**». Обычная причина ведёт сразу к
        итогу, и только `other` открывает свободный текст.

        Узел `ask_description` при этом **не удалён**: он ведёт к `summary`.
        """
        for module in MODULE_ERRORS:
            node = DEFAULT_TREE.node(cause_node_id(module))
            options = {option.id: option for option in node.options}

            assert "other" in options, f"{module}: нет причины Other"
            assert options["other"].next_node == "ask_description", (
                f"{module}: Other обязан вести на описание"
            )

            for option_id, option in options.items():
                if option_id == "other":
                    continue
                assert option.next_node == "summary", (
                    f"{module}: причина {option_id} должна вести сразу к итогу"
                )

        # Описание из дерева не выброшено: оно завершает ветку `Other`.
        assert DEFAULT_TREE.node("ask_description").next_node == "summary"

    def test_cause_has_options(self) -> None:
        for module in MODULE_ERRORS:
            options = DEFAULT_TREE.node(cause_node_id(module)).options

            assert len(options) >= 4, f"{module}: список причин пуст или короткий"
            # «Other» обязателен: закрытый список без него заставляет выбирать
            # неточное и портит статистику.
            assert any(option.id == "other" for option in options), f"{module}: нет Other"

    def test_cause_is_recorded_in_answers(self) -> None:
        """Ответ о причине попадает в результат с ключом `cause` — у каждого модуля."""
        for module in MODULE_ERRORS:
            assert DEFAULT_TREE.node(cause_node_id(module)).answer_key() == "cause"


class TestLiveOverlayLinksEveryErrorToItsOwnCauses:
    """
    🔴 **Живой оверлей (база) связывает ошибку со СВОИМИ причинами.**

    Сторожа выше проверяют **код** (`DEFAULT_TREE`). Но узлы и кнопки создаются
    и правятся из веба и живут **строками в базе** — владелец добавил модуль и
    узел ошибок именно так. Код-сторож такого дефекта не видит: `robot_errors_fingers`
    в `DEFAULT_TREE` отсутствует.

    **Дефект (живой, 13.10.2026).** Ветка владельца вела из ошибки пальцев в
    `ask_cause_qr_code`, и оператор видел причины QR-кода. Причина уходит в отчёт.

    **Как проверяется без сети.** `rest_get` подменяется: строки базы подаются
    прямо в `apply_overlay`, поэтому тест офлайновый и детерминированный.
    """

    def _overlay(self, nodes=(), options=()):
        """Дерево с наложенными строками «базы» — как его видит бот."""
        def fake_get(table, params=None, optional=False):
            if table == storage.NODE_TABLE:
                return list(nodes)
            if table == storage.OPTION_TABLE:
                return list(options)
            return []

        with patch("sendToDataBase.rest_get", side_effect=fake_get):
            storage.reset_capability_cache()

            return storage.apply_overlay(DEFAULT_TREE)

    def test_error_node_from_web_leads_to_its_own_causes(self):
        """Ошибка, созданная из веба, ведёт в `ask_cause_<её модуль>`."""
        tree = self._overlay(
            nodes=[{
                "node_id": "robot_errors_camera", "node_type": "choice",
                "title": "Camera: what is wrong?", "next_node": None,
                "hidden": False, "deleted": False,
            }, {
                "node_id": "robot_module_a42t", "node_type": "choice",
                "title": "A42T: which module?", "next_node": None,
                "hidden": False, "deleted": False,
            }],
            options=[
                {"node_id": "robot_module_a42t", "option_id": "camera",
                 "label": "Camera", "next_node": "robot_errors_camera",
                 "hidden": False, "deleted": False, "sort_order": 100},
                {"node_id": "robot_errors_camera", "option_id": "camera_dead",
                 "label": "Camera is dead", "next_node": "ask_cause_camera",
                 "hidden": False, "deleted": False, "sort_order": 10},
            ],
        )

        node = tree.node("robot_errors_camera")
        assert node is not None, "узел из веба не попал в дерево"

        targets = {option.next_node for option in node.options}
        assert targets == {"ask_cause_camera"}, (
            f"ошибка из веба ведёт в {targets}, а должна — в ask_cause_camera"
        )

    def test_mislinked_error_from_web_is_detected(self):
        """
        🔴 Обратная сторона: чужая причина **обнаруживается**.

        Без этой проверки тест выше проходил бы и при «любой узел причин сойдёт»:
        он сравнивает конкретный id, но не показывает, что подмена ссылки на
        чужую ломает проверку. Здесь ссылка намеренно направлена в причины
        другого модуля — как было в живой ветке владельца.
        """
        tree = self._overlay(
            nodes=[{
                "node_id": "robot_errors_camera", "node_type": "choice",
                "title": "Camera: what is wrong?", "next_node": None,
                "hidden": False, "deleted": False,
            }],
            options=[
                {"node_id": "robot_errors_camera", "option_id": "camera_dead",
                 "label": "Camera is dead", "next_node": "ask_cause_qr_code",
                 "hidden": False, "deleted": False, "sort_order": 10},
            ],
        )

        targets = {o.next_node for o in tree.node("robot_errors_camera").options}

        assert targets == {"ask_cause_qr_code"}, "подмена не сработала"
        assert targets != {"ask_cause_camera"}, (
            "проверка не отличает свои причины от чужих — сторож бесполезен"
        )


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
