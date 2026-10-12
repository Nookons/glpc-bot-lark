"""
Сторожа валидации приёма ошибок: данные не должны быть битыми.

**Требование владельца 12.10.2026, дословно:** «сейчас очень много некорректных
данных идёт через бота, нужно сделать всё правильно чтобы у юзеров было
максимально минимально шансов ввести некорректные данные», «главное правило что
данные не должны быть битые».

Файл закрывает **четыре** дефекта, найденные на данных владельца:

1. **Подсказка номера была общей.** Узел `ask_number` один на всё дерево, и его
   `placeholder` — пример **номера робота** (`3780 or H108/1834`). На ветке
   `QR Code → Floor` оператор видел ту же подсказку и вводил номер робота:
   **16 записей** `object_type='qr_code'` с `device_number` `3492`, `2668`…
2. **Номер принимался как есть.** В поле попадало описание ошибки:
   **103 невалидных номера из 2340** за октябрь — `'—'`, `'Unable to drive'`,
   `'Driver component exception. 3528'`.
3. **Зарядка спрашивала один номер.** Владелец: «если зарядка то сначала
   спрашиваем зарядку потом номер робота». Теперь в записи оба: станция в
   `device_number`, робот в `robot_number`.
4. **Деление номера по `/` применялось ко всем типам.** Для QR слэш запрещён:
   код места/полки содержит **дефисы**, и слэш — ошибка оператора.

Каждый тест падает, если дефект вернуть.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["SUPABASE_URL"] = "http://supabase.invalid"
os.environ["SUPABASE_SERVICE_KEY"] = "offline-test-key"
os.environ["TELEGRAM_DRY_RUN"] = "1"
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000:TEST-TOKEN")

from equipment_intake import report_writer as rw  # noqa: E402
from equipment_intake import validation as v  # noqa: E402


def restore_real_send_to_database():
    """
    Вернуть **настоящий** `sendToDataBase`, если его подменили.

    `tests/test_intake_editor_storage.py` кладёт в `sys.modules` урезанную
    заглушку (`rest_get`, `rest_upsert`, `rest_post`) и **не убирает её**.
    После него в `sendToDataBase` нет ни `warehouse_id`, ни `table_exists`, и
    любой следующий вызов `build_rows`/`lookup` падает с `AttributeError` —
    в одиночном прогоне тесты зелёные, в общем красные. Это загрязнение
    существует независимо от этой работы; здесь оно лишь **изолируется**, чтобы
    сторожа валидации проверяли код, а не порядок чужих тестов.
    """
    module = sys.modules.get("sendToDataBase")

    if module is not None and hasattr(module, "warehouse_id"):
        return

    sys.modules.pop("sendToDataBase", None)

    import importlib

    importlib.import_module("sendToDataBase")
from equipment_intake.engine import EngineError, Session  # noqa: E402
from equipment_intake.tree_config import (  # noqa: E402
    DEFAULT_TREE,
    number_example,
    number_kind_for,
)


# ============================================================
# ДЕФЕКТ №1: ПОДСКАЗКА НОМЕРА ЗАВИСИТ ОТ ВЕТКИ
# ============================================================

class BranchDependentHintChecks(unittest.TestCase):
    """Бот не должен спрашивать про полку, а подсказывать робота.

    **Дефект на данных.** `object_type='qr_code'`, `device_type='Floor'`, а
    `device_number='3492'` — это номер робота. Причина: узел `ask_number` один,
    и `placeholder` у него был общий — пример номера робота.
    """

    def hint(self, options):
        session = Session(tree=DEFAULT_TREE)

        for option in options:
            session.select(option)

        return session.text_example()

    def test_qr_floor_hints_the_floor_code_not_a_robot(self):
        """`QR Code → Floor` подсказывает код места, а не номер робота."""
        hint = self.hint(["qr", "qr_floor"])

        self.assertIn("SS512-020118-0101", hint)
        self.assertNotIn("3780", hint, "вернулся пример номера робота")

    def test_qr_shelf_hints_the_shelf_code(self):
        hint = self.hint(["qr", "qr_shelf"])

        self.assertIn("Shelf-138-039-1", hint)
        self.assertNotIn("3780", hint, "вернулся пример номера робота")

    def test_robot_branch_still_hints_a_robot_number(self):
        """Ветка робота не должна потерять свой пример — проверка обеих сторон.

        **Путь изменился 12.10.2026:** между модулем и номером появился выбор
        **ошибки**, поэтому модель и модуль теперь надо отвечать вместе с ней.
        Прежний `["robot", "a42t", "lifting"]` останавливался на узле ошибок, и
        `text_example()` был пуст.
        """
        hint = self.hint(["robot", "a42t", "lifting", "lift_cant_lift"])

        self.assertIn("3780", hint)
        self.assertIn("H108/1834", hint)

    def test_closed_branches_do_not_ask_for_a_number(self):
        """Закрытые ветки **не спрашивают номер**: диалог по ним не идёт.

        Решение владельца 12.10.2026: Workstation и Charging station «ПОКА НЕ
        РАБОТАЮТ». Прежние проверки требовали, чтобы эти ветки подсказывали
        номер станции (`137`) и код рабочей станции (`C2-H143`). Ожидание
        изменено: теперь выбор категории показывает сообщение о недоступности и
        останавливается — номера в этих ветках нет вовсе.
        """
        for option in ("workstation", "charger"):
            with self.subTest(option=option):
                session = Session(tree=DEFAULT_TREE)
                session.select(option)

                self.assertFalse(session.is_number_step())
                self.assertIsNone(session.current_node().options or None)
                self.assertEqual(session.text_example(), "")
                self.assertIn("not available yet", session.current_node().description)

    def test_every_branch_has_its_own_kind(self):
        """Вид номера различается по веткам — иначе подсказка снова станет общей.

        **Ожидание изменилось 12.10.2026.** Ветки рабочей станции и зарядки
        закрыты владельцем, поэтому номер по ним не вводится: проверять на них
        «вид номера» больше нечего. Зарядная ветка осталась в карте кода (она
        вернётся), и её вид проверяется **напрямую** по карте, а не через дерево.
        """
        cases = {
            ("robot", "a42t"): v.KIND_ROBOT,
            ("robot", "k50h", "safety", "safety_bumper"): v.KIND_ROBOT,
            ("robot", "a42t_c2", "tray", "tray_cant_extend"): v.KIND_ROBOT,
            ("qr", "qr_floor"): v.KIND_QR_FLOOR,
            ("qr", "qr_shelf"): v.KIND_QR_SHELF,
        }

        for options, expected in cases.items():
            with self.subTest(options=options):
                session = Session(tree=DEFAULT_TREE)

                for option in options:
                    session.select(option)

                self.assertEqual(session.answer_kind(), expected)

    def test_error_step_keeps_the_robot_number_kind(self):
        """Ошибка модуля — самый глубокий шаг, и номер на ней остаётся робочим.

        Вид номера ищется от последнего шага пути к первому. Без явной записи об
        ошибках в карте (`_NUMBER_KIND_BY_OPTION`) вид брался бы у модели выше —
        сегодня это тот же `robot`, но ветка «ошибка → номер» не должна зависеть
        от того, что модель случайно тоже робот.
        """
        from equipment_intake.tree_config import number_kind_for

        step = mock.Mock(option_id="safety_front_button")

        self.assertEqual(number_kind_for([step], None), v.KIND_ROBOT)

    def test_examples_are_not_all_the_same(self):
        """Разные ветки — разные примеры: общий `placeholder` и был дефектом."""
        examples = {
            number_example(kind) for kind in (
                v.KIND_ROBOT, v.KIND_STATION, v.KIND_WORKSTATION,
                v.KIND_QR_FLOOR, v.KIND_QR_SHELF,
            )
        }

        self.assertEqual(len(examples), 5)

    def test_the_caption_shown_to_the_operator_uses_the_branch_example(self):
        """Сторож на **видимый текст**, а не только на помощник.

        Первая версия проверки смотрела `session.text_example()` и оставалась
        зелёной, даже когда `flow._waiting_hint` снова показывал общий
        `placeholder` узла: помощник был верный, а оператор видел прежний
        пример. Поэтому здесь собирается **подпись шага** — то, что реально
        приходит в Telegram.
        """
        from equipment_intake import flow as intake_flow

        session = Session(tree=DEFAULT_TREE)

        for option in ("qr", "qr_floor"):
            session.select(option)

        caption = intake_flow.step_caption(session)

        self.assertIn("SS512-020118-0101", caption)
        self.assertNotIn("3780", caption, "оператор снова видит пример робота на ветке QR")

    def test_kind_is_derived_from_the_option_id(self):
        """Карта веток живёт в коде и не теряется правками редактора."""
        self.assertEqual(number_kind_for([], None), v.KIND_ROBOT)
        self.assertEqual(
            number_kind_for([mock.Mock(option_id="qr_floor")], None),
            v.KIND_QR_FLOOR,
        )


# ============================================================
# ДЕФЕКТ №2: ПРОВЕРКА ФОРМАТА — МУСОР НЕ ПРОХОДИТ
# ============================================================

class FormatValidationChecks(unittest.TestCase):
    """В поле номера не должно попадать описание ошибки.

    Значения ниже — **реальные из выгрузки владельца**, 103 невалидных номера
    из 2340 за октябрь.
    """

    #: Мусор, который попадал в поле номера робота.
    GARBAGE_NAMES = (
        "—",
        "Unable to drive",
        "Unable to rotate",
        "Driver component exception.  3528",
        "EX: 驱动组件异常 Driver component exception. 3563",
        "3699 ,rotation",
        "3685 tray problem",
    )

    def test_error_descriptions_are_refused_as_robot_numbers(self):
        for value in self.GARBAGE_NAMES:
            with self.subTest(value=value):
                self.assertIsNotNone(v.validate_format(v.KIND_ROBOT, value))

    def test_real_robot_numbers_pass(self):
        for value in ("3780", "3490", "0", "H108", "3561/3528", "H108/1834"):
            with self.subTest(value=value):
                self.assertIsNone(v.validate_format(v.KIND_ROBOT, value))

    def test_non_ascii_digits_are_not_robot_numbers(self):
        """`٣٧٨٠` не должно стать роботом 3780 — это разные номера."""
        for value in ("٣٧٨٠", "１２３", "١٢٣"):
            with self.subTest(value=value):
                self.assertIsNotNone(v.validate_format(v.KIND_ROBOT, value))

    def test_qr_floor_format(self):
        self.assertIsNone(v.validate_format(v.KIND_QR_FLOOR, "SS512-020118-0101"))
        self.assertIsNone(v.validate_format(v.KIND_QR_FLOOR, "SS512-020118-0101_A1"))

        for value in ("3492", "SS512-020118", "SS51-020118-0101"):
            with self.subTest(value=value):
                self.assertIsNotNone(v.validate_format(v.KIND_QR_FLOOR, value))

    def test_qr_shelf_format(self):
        self.assertIsNone(v.validate_format(v.KIND_QR_SHELF, "Shelf-138-039-1"))

        for value in ("Shelf-138-39-1", "Shelf-138-039-12", "shelf-138-039-1"):
            with self.subTest(value=value):
                self.assertIsNotNone(v.validate_format(v.KIND_QR_SHELF, value))

    def test_station_is_digits_only(self):
        self.assertIsNone(v.validate_format(v.KIND_STATION, "137"))
        self.assertIsNotNone(v.validate_format(v.KIND_STATION, "station 137"))

    def test_workstation_code(self):
        self.assertIsNone(v.validate_format(v.KIND_WORKSTATION, "C2-H143"))
        self.assertIsNotNone(v.validate_format(v.KIND_WORKSTATION, "C2 H143"))

    def test_empty_is_refused_for_every_kind(self):
        for kind in (
            v.KIND_ROBOT, v.KIND_STATION, v.KIND_WORKSTATION,
            v.KIND_QR_FLOOR, v.KIND_QR_SHELF,
        ):
            with self.subTest(kind=kind):
                self.assertIsNotNone(v.validate_format(kind, "   "))

    def test_engine_refuses_a_description_in_the_number_step(self):
        """Проверка стоит **на вводе**: сессия жива, шаг не пройден."""
        session = Session(tree=DEFAULT_TREE)

        for option in ("qr", "qr_floor"):
            session.select(option)

        with self.assertRaises(EngineError):
            session.submit_text("3492")

        # Отказ не сжёг ветку: верный ответ принимается следом.
        session.submit_text("SS512-020118-0101")
        self.assertEqual(session.answers()["device_number"], "SS512-020118-0101")

    def test_description_step_is_not_format_checked(self):
        """Свободный текст описания проверять нельзя — иначе отчёт не сдать."""
        session = Session(tree=DEFAULT_TREE)

        # Путь изменился 12.10.2026: между модулем и номером стоит выбор ошибки.
        for option in ("robot", "k50h", "safety", "safety_front_button"):
            session.select(option)

        session.submit_text("3780")
        session.submit_text("Unable to drive: safety. 3780")

        self.assertIn("Unable to drive", session.answers()["description"])


# ============================================================
# ДЕФЕКТ №2 (мягкая часть): СПРАВОЧНИК — ПРЕДУПРЕЖДЕНИЕ, НЕ ОТКАЗ
# ============================================================

class RegistryLookupChecks(unittest.TestCase):
    """Нет в справочнике — предупредить, но **дать сохранить**.

    Владелец: «Robot 9999 is not in the registry. Save anyway?» Данные не
    теряются, но запись помечается на разбор.
    """

    def test_known_number_is_found(self):
        with mock.patch("sendToDataBase.rest_get", return_value=[{"id": 1}]):
            self.assertTrue(v.lookup(v.KIND_ROBOT, "GLP-C", "3490"))

    def test_unknown_number_is_not_found(self):
        with mock.patch("sendToDataBase.rest_get", return_value=[]):
            self.assertFalse(v.lookup(v.KIND_ROBOT, "GLP-C", "9999"))

    def test_lookup_is_scoped_to_the_warehouse(self):
        """Номера роботов повторяются между складами — иначе чужая зона."""
        calls = []

        def fake(table, params, optional=False):
            calls.append(params)
            return [{"id": 1}]

        with mock.patch("sendToDataBase.rest_get", side_effect=fake):
            v.lookup(v.KIND_ROBOT, "SMALL-P3", "13")

        self.assertEqual(calls[0]["warehouse"], "eq.SMALL-P3")
        self.assertEqual(calls[0]["category"], "eq.robot")

    def test_read_failure_is_unknown_not_missing(self):
        """Сбой чтения — «неизвестно»: обвинять верный номер нельзя."""
        with mock.patch("sendToDataBase.rest_get", return_value=None):
            self.assertIsNone(v.lookup(v.KIND_ROBOT, "GLP-C", "3490"))

    def test_unavailable_qr_table_degrades_softly(self):
        """Отсутствующей таблицы может ещё не быть — проверка выключается.

        `PostgREST` на неизвестную таблицу отвечает `404`, и
        `rest_get(optional=True)` даёт `None`. Это «неизвестно», а не «нет в
        справочнике»: помечать запись на разбор из-за отсутствия справочника
        нельзя. Проверка **формата** при этом работает всегда — она не зависит
        от базы (`test_qr_floor_format`).
        """
        with mock.patch("sendToDataBase.rest_get", return_value=None):
            self.assertIsNone(v.lookup(v.KIND_QR_FLOOR, "GLP-C", "SS512-020118-0101"))

    def test_qr_lookup_uses_the_map_table(self):
        calls = []

        def fake(table, params, optional=False):
            calls.append(table)
            return [{"id": 1}]

        with mock.patch("sendToDataBase.rest_get", side_effect=fake):
            self.assertTrue(v.lookup(v.KIND_QR_SHELF, "GLP-C", "Shelf-138-039-1"))

        self.assertEqual(calls, ["map_shelves"])

    def test_floor_place_lookup_uses_base_code(self):
        """Код места может нести суффикс (`_coop_haiflex`) — ищем по `base_code`.

        Проверено на живой базе 12.10.2026: `SS512-069045-0102` лежит в
        `base_code`, а `code` — `SS512-069045-0102_coop_haiflex`. Поиск по
        `code` не нашёл бы номер из наклейки.
        """
        params = {}

        def fake(table, query, optional=False):
            params.update({"table": table, **query})
            return [{"id": 1}]

        with mock.patch("sendToDataBase.rest_get", side_effect=fake):
            self.assertTrue(v.lookup(v.KIND_QR_FLOOR, "GLP-C", "SS512-069045-0102"))

        self.assertEqual(params["table"], "map_shelf_places")
        self.assertEqual(params["base_code"], "eq.SS512-069045-0102")

    def test_partial_unknown_makes_the_whole_answer_unknown(self):
        """Одна непроверенная часть делает «неизвестно» весь ответ."""
        answers = iter([[{"id": 1}], None])

        with mock.patch("sendToDataBase.rest_get", side_effect=lambda *a, **k: next(answers)):
            self.assertIsNone(v.lookup(v.KIND_ROBOT, "GLP-C", "3561/9999"))


# ============================================================
# ДЕФЕКТ №3: ЗАРЯДКА — СНАЧАЛА СТАНЦИЯ, ПОТОМ РОБОТ
# ============================================================

class ChargingOrderChecks(unittest.TestCase):
    """Зарядная ветка **закрыта** владельцем, но её машинерия цела.

    **Что изменилось 12.10.2026.** Решение владельца: «Workstation и Charging
    Station ПОКА НЕ РАБОТАЮТ». Прежние проверки ходили по дереву
    (`charger → cs_k50h → ask_number → ask_charging_robot`) и требовали двух
    номеров. Идти этим путём больше нельзя — выбор категории останавливает
    диалог.

    **Почему проверки не удалены, а переписаны.** Порядок «станция, потом
    робот» — требование владельца и часть контракта записи: станция идёт в
    `device_number`, робот — в `robot_number`. Когда ветку вернут, регресс
    должен ловиться сразу, поэтому здесь сторожатся **обе** стороны:
    1) по дереву зарядка не проходит; 2) вид номера и запись двух колонок
    по-прежнему работают (`test_writer_files_both_numbers` ниже — без дерева).
    """

    def setUp(self):
        """Изоляция от загрязнения `sys.modules` чужими тестами."""
        restore_real_send_to_database()

    def walk(self):
        """Путь зарядки в дереве: доходит до узла «недоступно» и стоит."""
        session = Session(tree=DEFAULT_TREE)
        session.select("charger")
        return session

    def test_charging_branch_stops_at_the_unavailable_node(self):
        """Выбор зарядки показывает отказ и **не** спрашивает номер."""
        session = self.walk()

        self.assertEqual(session.current_id, "charging_unavailable")
        self.assertFalse(session.is_number_step())
        self.assertFalse(session.is_text_step())
        self.assertIn("not available yet", session.current_node().description)

    def test_charging_branch_cannot_be_continued(self):
        """Из закрытой ветки нельзя ни выбрать вариант, ни ввести текст."""
        session = self.walk()

        with self.assertRaises(EngineError):
            session.select("cs_k50h")

        with self.assertRaises(EngineError):
            session.submit_text("137")

    def test_station_kind_is_still_known_to_the_code(self):
        """Вид `station` не потерян: карта веток и проверка формата на месте.

        Это то, что вернётся вместе с веткой. Проверяем напрямую, потому что
        через дерево до номера станции теперь не дойти.
        """
        from equipment_intake.tree_config import number_kind_for

        # Вид задаёт **ветка**, а не конкретная подмарка станции: в карте стоит
        # `charger`, и он же перекрывает `cs_k50h`/`cs_a42t_c2` ниже по пути.
        step = mock.Mock(option_id="charger")

        self.assertEqual(number_kind_for([step], None), v.KIND_STATION)
        self.assertIsNone(v.validate_format(v.KIND_STATION, "137"))
        self.assertIsNotNone(v.validate_format(v.KIND_STATION, "3780 robot"))

    def test_writer_files_both_numbers(self):
        """Станция — в `device_number`, робот — в `robot_number`."""
        rows = self.rows_for(
            {
                "object": "Charging station",
                "device_type": "Charge for small robot",
                "device_number": "137",
                "error_robot": "3780",
                "description": "does not charge",
            }
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["device_number"], "137")
        self.assertEqual(rows[0]["robot_number"], 3780)

    def rows_for(self, answers):
        result = {
            "answers": answers,
            "warehouse": "GLP-C",
            "employee": "U",
            "employee_card_id": 1,
            "photo_url": None,
            "user_id": 1,
            "chat_id": -100,
            "message_id": 1,
        }

        with mock.patch.object(rw, "canonical_type", return_value=None), \
             mock.patch.object(rw, "match_template", return_value=None), \
             mock.patch.object(rw, "load_templates", return_value=[]), \
             mock.patch("sendToDataBase.warehouse_id", return_value=1):
            return rw.build_rows(result, "GLP-C")


# ============================================================
# ДЕФЕКТ №4: ДЕЛЕНИЕ НОМЕРА ПО `/` — НЕ ДЛЯ QR
# ============================================================

class DeviceSplitChecks(unittest.TestCase):
    def setUp(self):
        """Изоляция от загрязнения `sys.modules` чужими тестами."""
        restore_real_send_to_database()

    """Один девайс — одна запись; `3561/3528` — две.

    Владелец: «на один девайс одна запись; если коллизия 3453/3144 то записи
    две». Но для QR деление **запрещено**: код места/полки содержит дефисы, а
    слэш — ошибка оператора.
    """

    def rows_for(self, answers):
        result = {
            "answers": answers,
            "warehouse": "GLP-C",
            "employee": "U",
            "employee_card_id": 1,
            "photo_url": None,
            "user_id": 1,
            "chat_id": -100,
            "message_id": 1,
        }

        with mock.patch.object(rw, "canonical_type", return_value=None), \
             mock.patch.object(rw, "match_template", return_value=None), \
             mock.patch.object(rw, "load_templates", return_value=[]), \
             mock.patch("sendToDataBase.warehouse_id", return_value=1):
            return rw.build_rows(result, "GLP-C")

    def test_robot_numbers_split_into_two_rows(self):
        rows = self.rows_for(
            {"object": "Robot", "device_type": "A42T",
             "device_number": "3561/3528", "description": "x"}
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual([r["robot_number"] for r in rows], [3561, 3528])

    def test_charging_station_number_splits(self):
        """Зарядка — не исключение: `137/140` это две станции."""
        rows = self.rows_for(
            {"object": "Charging station", "device_type": "Charge for small robot",
             "device_number": "137/140", "error_robot": "3780", "description": "x"}
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual([r["device_number"] for r in rows], ["137", "140"])

    def test_qr_slash_is_one_row_never_two(self):
        """Слэш в QR-коде — ошибка оператора, а не второе устройство."""
        rows = self.rows_for(
            {"object": "QR Code", "device_type": "Floor",
             "device_number": "SS512-020118-0101/SS512-020118-0102",
             "description": "dirty"}
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]["device_number"], "SS512-020118-0101/SS512-020118-0102"
        )

    def test_qr_shelf_slash_is_one_row(self):
        rows = self.rows_for(
            {"object": "QR Code", "device_type": "Shelf",
             "device_number": "Shelf-138-039-1/Shelf-138-039-2",
             "description": "dirty"}
        )

        self.assertEqual(len(rows), 1)

    def test_format_refuses_a_slash_in_a_qr_code(self):
        for kind, value in (
            (v.KIND_QR_FLOOR, "SS512-020118-0101/SS512-020118-0102"),
            (v.KIND_QR_SHELF, "Shelf-138-039-1/Shelf-138-039-2"),
        ):
            with self.subTest(kind=kind):
                self.assertIsNotNone(v.validate_format(kind, value))


# ============================================================
# ПОМЕТКА «НА РАЗБОР»
# ============================================================

class ReviewMarkingChecks(unittest.TestCase):
    def setUp(self):
        """Изоляция от загрязнения `sys.modules` чужими тестами."""
        restore_real_send_to_database()

    """Запись с номером не из справочника помечается, а не теряется."""

    def row(self, review_reason, include_review=True):
        result = {
            "answers": {"object": "Robot", "device_type": "A42T",
                        "device_number": "9999", "description": "x"},
            "warehouse": "GLP-C",
            "employee": "U",
            "employee_card_id": 1,
            "photo_url": "https://x/p.jpg",
            "user_id": 1,
            "chat_id": -100,
            "message_id": 1,
            "review_reason": review_reason,
        }

        with mock.patch.object(rw, "canonical_type", return_value=None), \
             mock.patch.object(rw, "match_template", return_value=None), \
             mock.patch.object(rw, "load_templates", return_value=[]), \
             mock.patch("sendToDataBase.warehouse_id", return_value=1):
            return rw.build_row(result, "GLP-C", include_review=include_review)

    def test_missing_number_marks_the_row(self):
        row = self.row("number is not in the registry: Robot 9999")

        self.assertTrue(row["needs_review"])
        self.assertIn("Robot 9999", row["review_reason"])

    def test_found_number_is_not_marked(self):
        row = self.row("")

        self.assertFalse(row["needs_review"])
        self.assertIsNone(row["review_reason"])

    def test_review_columns_can_be_dropped(self):
        """В единой таблице этих колонок пока нет — вставка не должна падать."""
        row = self.row("number is not in the registry: Robot 9999", include_review=False)

        self.assertNotIn("needs_review", row)
        self.assertNotIn("review_reason", row)

    def test_reason_survives_in_unresolved_reason_without_the_columns(self):
        """Пометку нельзя потерять: без своих колонок она идёт в `unresolved_reason`."""
        with mock.patch.object(rw, "_review_columns_available", return_value=False):
            row = self.row("number is not in the registry: Robot 9999")

        self.assertIn("number_not_in_registry", row["unresolved_reason"])

    def test_flow_warns_before_writing(self):
        """`_confirm` сначала предупреждает, а пишет только после согласия."""
        from equipment_intake import flow as intake_flow

        session = Session(tree=DEFAULT_TREE)

        # Слой ошибок (12.10.2026): между модулем и номером выбирается ошибка.
        for option in ("robot", "k50h", "safety", "safety_front_button"):
            session.select(option)

        session.submit_text("3780")
        session.submit_text("Lift reports an error")
        session.select("obstacle")
        session.data.update({"warehouse": "GLP-C", "image": "/tmp/x.jpg", "message_id": 5})

        with mock.patch.object(intake_flow, "unregistered_numbers", return_value=["Robot 3780"]):
            prompt = intake_flow.soft_review_prompt(session)

        self.assertIsNotNone(prompt)
        text, keyboard = prompt
        self.assertIn("Not in the registry", text)
        self.assertIn("Robot 3780", text)
        # Мягко: есть кнопка подтверждения, запись не теряется.
        buttons = keyboard["inline_keyboard"][0]
        self.assertTrue(any(button["text"] == "Confirm" for button in buttons))

    def test_warning_does_not_lose_the_session_or_the_data(self):
        """После предупреждения сессия жива, и подтвердить можно снова.

        Мягкая валидация не должна превращаться в жёсткую: если бы `_confirm`
        удалял сессию, оператор потерял бы уже введённые ответы и фото.
        """
        from equipment_intake import flow as intake_flow

        session = Session(tree=DEFAULT_TREE)

        # Слой ошибок (12.10.2026): между модулем и номером выбирается ошибка.
        for option in ("robot", "k50h", "safety", "safety_front_button"):
            session.select(option)

        session.submit_text("9999")
        session.submit_text("Lift reports an error")
        session.select("obstacle")
        session.data.update({"warehouse": "GLP-C", "image": "/tmp/x.jpg", "message_id": 5})

        intake_flow.reset_state()
        intake_flow.put_session(-100, 1, session)

        with mock.patch.object(intake_flow, "_bot", return_value=mock.Mock(_send=lambda *a, **k: None)), \
             mock.patch.object(intake_flow, "edit_caption"), \
             mock.patch.object(intake_flow, "_employee_context", return_value=("U", None)), \
             mock.patch.object(intake_flow, "unregistered_numbers",
                               return_value=["Robot 9999"]), \
             mock.patch("equipment_intake.integrations.persist_and_send") as send:
            intake_flow._confirm(-100, {"id": 1}, session, 5)

        self.assertFalse(send.called, "строка записана до согласия оператора")
        self.assertIsNotNone(
            intake_flow.get_session(-100, 1), "сессия потеряна после предупреждения"
        )
        self.assertTrue(session.data.get("review_pending"))
        intake_flow.reset_state()

    def test_flow_writes_after_confirmation(self):
        """Согласие подтверждается повторным Confirm — тогда пишем с пометкой."""
        from equipment_intake import flow as intake_flow

        session = Session(tree=DEFAULT_TREE)

        # Слой ошибок (12.10.2026): между модулем и номером выбирается ошибка.
        for option in ("robot", "k50h", "safety", "safety_front_button"):
            session.select(option)

        session.submit_text("9999")
        session.submit_text("Lift reports an error")
        session.select("obstacle")
        session.data.update({
            "warehouse": "GLP-C", "image": "/tmp/x.jpg", "message_id": 5,
            "review_ack": True,
        })
        sent = {}

        def fake_persist(result, image):
            sent.update(result)
            return {
                "database_saved": True, "device_queued": False,
                "lark_delivered": True, "glpc_saved": True, "glpc_error": None,
            }

        with mock.patch.object(intake_flow, "unregistered_numbers",
                               return_value=["Robot 9999"]), \
             mock.patch("equipment_intake.integrations.persist_and_send",
                        side_effect=fake_persist), \
             mock.patch.object(intake_flow, "edit_caption"), \
             mock.patch.object(intake_flow, "_bot",
                               return_value=mock.Mock(_send=lambda *a, **k: None)), \
             mock.patch.dict(sys.modules, {"telegram_store": mock.Mock(
                 get_employee_context=lambda _: ("U", None))}):
            intake_flow._confirm(-100, {"id": 1}, session, 5)

        self.assertIn("Robot 9999", sent.get("review_reason", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
