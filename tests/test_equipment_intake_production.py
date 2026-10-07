"""
Offline coverage for the production equipment intake.

No network, no production database: the Supabase URL is pointed at an
unreachable host *before* the bot modules are imported, so a forgotten stub
fails loudly instead of writing a real row. This matters — an earlier version
of this file called `rest_post` for real and created a row in the production
`exceptions_glpc` table.

Run:

    python3 tests/test_equipment_intake_production.py
"""

import os
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Isolation must happen before the modules under test read configuration.
os.environ["SUPABASE_URL"] = "http://supabase.invalid"
os.environ["SUPABASE_SERVICE_KEY"] = "offline-test-key"
os.environ["TELEGRAM_DRY_RUN"] = "1"
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000:TEST-TOKEN")

import telegram_api as tg  # noqa: E402
from equipment_intake import editor, flow, integrations, report_writer, storage  # noqa: E402
from equipment_intake.engine import Session  # noqa: E402
from equipment_intake.tree_config import DEFAULT_TREE  # noqa: E402

# The chat allow-list is read from the real `.env` when the bot is imported, so
# an end-to-end test must use a chat id the bot actually accepts — otherwise
# dispatch stops on the allow-list before reaching the code under test.
import telegram_bot as _bot  # noqa: E402

ALLOWED_CHAT_ID = next(iter(_bot.ALLOWED_CHAT_IDS)) if _bot.ALLOWED_CHAT_IDS else -100123


def _answers(**overrides):
    answers = {
        "object": "Robot",
        "device_type": "A42T C2",
        "module": "Lifting",
        "device_number": "3490",
        "description": "Lift reports an error",
    }
    answers.update(overrides)
    return answers


def _result(warehouse="GLP-C", **overrides):
    result = {
        "answers": _answers(),
        "warehouse": warehouse,
        "employee": "Smoke User",
        "user_id": 12345,
        "username": "smoke-user",
        "chat_id": -100123,
        "message_id": 777,
        "image": "/tmp/telegram-photo-bot-test.jpg",
    }
    result.update(overrides)
    return result


class WarehouseRoutingChecks(unittest.TestCase):
    """The warehouse must come from the topic, never from a default."""

    def test_configured_warehouses_are_accepted(self):
        self.assertEqual(report_writer.validate_warehouse("GLP-C"), "GLP-C")
        self.assertEqual(report_writer.validate_warehouse("SMALL-P3"), "SMALL-P3")
        self.assertEqual(report_writer.validate_warehouse("  SMALL-P3  "), "SMALL-P3")

    def test_missing_warehouse_is_refused(self):
        for value in ("", None, "   "):
            with self.assertRaises(report_writer.WarehouseError):
                report_writer.validate_warehouse(value)

    def test_unconfigured_warehouse_is_refused(self):
        for value in ("PNT-A", "P3-DC-1", "Nope"):
            with self.assertRaises(report_writer.WarehouseError):
                report_writer.validate_warehouse(value)

    def test_row_keeps_the_topic_warehouse(self):
        for warehouse in ("GLP-C", "SMALL-P3"):
            row = report_writer.build_row(_result(warehouse), warehouse)
            self.assertEqual(row["warehouse"], warehouse)

    def test_write_refuses_missing_warehouse(self):
        with patch("sendToDataBase.rest_post") as post:
            with self.assertRaises(report_writer.WarehouseError):
                report_writer.write_exception(_result(warehouse=""))
        post.assert_not_called()

    def test_write_never_falls_back_to_default(self):
        """An unconfigured warehouse must not be filed as GLP-C."""
        with patch("sendToDataBase.rest_post") as post:
            with self.assertRaises(report_writer.WarehouseError):
                report_writer.write_exception(_result(warehouse="PNT-A"))
        post.assert_not_called()

    def test_strict_resolver_rejects_non_error_topics(self):
        import telegram_bot as bot

        original = dict(bot.TOPIC_RAW)
        # Топик ошибок склада по умолчанию хранится отдельной константой
        # (`TELEGRAM_TOPIC_ID`), прочитанной из окружения при импорте. Без
        # `.env` (например, в CI) она пуста, и `error_topic("GLP-C")` не
        # находит топик — проверка падала бы на конфигурации оператора, а не
        # на поведении кода. Задаём значение явно и возвращаем как было.
        original_topic_id = bot.TELEGRAM_TOPIC_ID
        original_topic_name = bot.TELEGRAM_TOPIC_NAME

        try:
            bot.TOPIC_RAW.clear()
            bot.TOPIC_RAW.update({"error": "2", "error:SMALL-P3": "77"})
            bot.TELEGRAM_TOPIC_ID = 2
            bot.TELEGRAM_TOPIC_NAME = ""

            self.assertEqual(bot.error_warehouse_for_thread(-100, 2), "GLP-C")
            self.assertEqual(bot.error_warehouse_for_thread(-100, 77), "SMALL-P3")
            # Unknown and general topics are not error topics.
            self.assertIsNone(bot.error_warehouse_for_thread(-100, 999))
            self.assertIsNone(bot.error_warehouse_for_thread(-100, None))
        finally:
            bot.TOPIC_RAW.clear()
            bot.TOPIC_RAW.update(original)
            bot.TELEGRAM_TOPIC_ID = original_topic_id
            bot.TELEGRAM_TOPIC_NAME = original_topic_name

    def test_strict_resolver_keeps_legacy_mode_when_nothing_configured(self):
        """With no error topic configured the bot has no way to know better."""
        import telegram_bot as bot

        original = dict(bot.TOPIC_RAW)

        try:
            bot.TOPIC_RAW.clear()
            self.assertFalse(bot.error_routing_configured())
            self.assertEqual(
                bot.error_warehouse_for_thread(-100, 42), bot.DEFAULT_WAREHOUSE
            )
        finally:
            bot.TOPIC_RAW.clear()
            bot.TOPIC_RAW.update(original)

    def test_photo_from_general_topic_is_rejected(self):
        """
        A photo in a status/service topic must not become a GLP-C error.

        End-to-end guard for plan risk §6.2: the dispatch used to let photos from
        shared topics through, and `current_warehouse()` then reported GLP-C.
        """
        import telegram_bot as bot

        original = dict(bot.TOPIC_RAW)
        hinted = set(bot._hinted_threads)

        try:
            bot.TOPIC_RAW.clear()
            bot.TOPIC_RAW.update({"error": "2", "error:SMALL-P3": "77", "status": "50"})
            # The wrong-topic hint fires once per topic; clear it so the check
            # below observes this call instead of an earlier one.
            bot._hinted_threads.clear()

            with patch.object(bot, "handle_photo") as photo, \
                 patch.object(bot, "_handle_wrong_topic") as wrong:
                bot._handle_update_inner({
                    "update_id": 1,
                    "message": {
                        "message_id": 10,
                        "date": 1_800_000_000,
                        "chat": {"id": ALLOWED_CHAT_ID, "type": "supergroup"},
                        "from": {"id": 5, "username": "u"},
                        "message_thread_id": 50,
                        "photo": [{"file_id": "f", "file_unique_id": "u"}],
                    },
                })

            photo.assert_not_called()
            self.assertTrue(wrong.called, "the operator should be told it is the wrong topic")
        finally:
            bot.TOPIC_RAW.clear()
            bot.TOPIC_RAW.update(original)
            bot._hinted_threads.clear()
            bot._hinted_threads.update(hinted)


class CanonicalTypeChecks(unittest.TestCase):
    """`device_type` must hold the canonical fleet type, not the button label."""

    def test_label_is_kept_when_type_is_known(self):
        with patch.object(report_writer, "canonical_type", return_value="RT_KUBOT_MINI_HAIFLEX"):
            row = report_writer.build_row(_result(), "GLP-C")

        self.assertEqual(row["device_type"], "RT_KUBOT_MINI_HAIFLEX")
        # The operator's label is preserved, not lost.
        self.assertIn("Reported type: A42T C2", row["issue_description"])

    def test_label_is_used_when_type_is_unknown(self):
        with patch.object(report_writer, "canonical_type", return_value=None):
            row = report_writer.build_row(_result(), "GLP-C")

        self.assertEqual(row["device_type"], "A42T C2")
        self.assertNotIn("Reported type", row["issue_description"])

    def test_type_lookup_is_category_aware(self):
        """SMALL-P3 code 13 is both a robot and a charging station."""
        calls = []

        def fake_rest_get(table, params):
            calls.append((table, params))
            return [{"equipment_type_id": 13}]

        with patch("sendToDataBase.rest_get", side_effect=fake_rest_get):
            with patch.object(report_writer, "canonical_type", wraps=report_writer.canonical_type):
                report_writer.canonical_type("SMALL-P3", "robot", "13")

        equipment_params = calls[0][1]
        self.assertEqual(equipment_params["warehouse"], "eq.SMALL-P3")
        self.assertEqual(equipment_params["category"], "eq.robot")
        self.assertEqual(equipment_params["equipment_code"], "eq.13")


class RowMappingChecks(unittest.TestCase):
    def test_numeric_number_goes_to_error_robot(self):
        with patch.object(report_writer, "canonical_type", return_value=None):
            row = report_writer.build_row(_result(), "GLP-C")
        self.assertEqual(row["error_robot"], 3490)

    def test_non_numeric_identifier_stays_in_description(self):
        result = _result()
        result["answers"]["device_number"] = "Shelf-3938-3002-20"

        with patch.object(report_writer, "canonical_type", return_value=None):
            row = report_writer.build_row(result, "GLP-C")

        self.assertIsNone(row["error_robot"])
        self.assertIn("Shelf-3938-3002-20", row["issue_description"])

    def test_qr_floor_shape_is_supported(self):
        result = _result()
        result["answers"].pop("device_number")
        result["answers"].update({"qr_x": "162", "qr_y": "382", "qr_zone": "30"})

        with patch.object(report_writer, "canonical_type", return_value=None):
            row = report_writer.build_row(result, "GLP-C")

        self.assertIn("X=162; Y=382; Zone=30", row["issue_description"])
        self.assertIsNone(row["error_robot"])

    def test_uniq_key_uses_telegram_coordinates(self):
        """
        One message is one report; the pair is stable across redelivery and
        distinct between two different reports.
        """
        with patch.object(report_writer, "canonical_type", return_value=None):
            row = report_writer.build_row(_result(), "GLP-C")

        self.assertEqual(row["uniq_key"], "telegram-photo-bot:-100123:777")
        # The key shape differs from the legacy `<user>.<robot>.<time>`, so the
        # two flows cannot collide on the unique index.
        self.assertTrue(row["uniq_key"].startswith("telegram-photo-bot:"))

    def test_shift_boundaries_use_warsaw_time(self):
        """Shift is decided in Europe/Warsaw, not on the host's local zone."""
        cases = [
            ("2026-10-02T03:59:00+00:00", "2026-10-01", "night"),  # 05:59 Warsaw
            ("2026-10-02T04:00:00+00:00", "2026-10-02", "day"),    # 06:00 Warsaw
            ("2026-10-02T15:59:00+00:00", "2026-10-02", "day"),    # 17:59 Warsaw
            ("2026-10-02T16:00:00+00:00", "2026-10-02", "night"),  # 18:00 Warsaw
        ]

        for received, expected_date, expected_shift in cases:
            result = _result(received_at=received)

            with patch.object(report_writer, "canonical_type", return_value=None):
                row = report_writer.build_row(result, "GLP-C")

            self.assertEqual(row["issue_data"], expected_date, received)
            self.assertEqual(row["shift_type"], expected_shift, received)

    def test_row_carries_both_warehouses_independently(self):
        with patch.object(report_writer, "canonical_type", return_value=None):
            glpc = report_writer.build_row(_result("GLP-C"), "GLP-C")
            sp3 = report_writer.build_row(_result("SMALL-P3"), "SMALL-P3")

        self.assertEqual(glpc["warehouse"], "GLP-C")
        self.assertEqual(sp3["warehouse"], "SMALL-P3")


class IntegrationChecks(unittest.TestCase):
    def tearDown(self):
        flow.reset_state()
        editor.reset_state()

    def test_photo_to_confirmed_report(self):
        sender = {"id": 12345, "username": "smoke-user"}
        fake_bot = types.SimpleNamespace(
            _reply_thread=lambda chat_id: 88,
            _delete_user_message=lambda *a: True,
            _delete_quiet=lambda *a: True,
        )

        with patch.object(flow, "_bot", return_value=fake_bot), \
             patch.object(flow, "_active_tree", return_value=DEFAULT_TREE), \
             patch.object(tg, "send_photo", return_value={"message_id": 900}), \
             patch.object(tg, "answer_callback_query"), \
             patch.object(flow, "edit_caption", return_value={"message_id": 900}), \
             patch.object(integrations, "persist_and_send", return_value={
                 "database_saved": True, "device_queued": False,
                 "lark_delivered": True, "glpc_saved": True, "glpc_error": None,
             }) as deliver, \
             patch.dict(sys.modules, {"telegram_store": types.SimpleNamespace(
                 get_employee_name=lambda _: "Smoke User")}):
            self.assertTrue(flow.start(-100123, sender, "/tmp/photo.jpg", 777, warehouse="GLP-C"))

            # Путь: робот → модель → модуль → **номер → описание → причина**.
            # Вопрос о причине добавлен 07.10.2026 («добавить причину ошибки как
            # ещё один вопрос»), поэтому шагов стало на один больше.
            #
            # **Порядок важен:** причина спрашивается **после** описания. Первая
            # версия этого теста отвечала причину перед номером, и сессия не
            # завершалась — шаг причины оставался неотвеченным.
            for option in ("robot", "a42t_c2", "lifting"):
                self.assertTrue(flow.handle_callback(-100123, sender, ["dt", "s", option], 900, "cb"))

            self.assertTrue(flow.handle_text(-100123, sender, "ROBOT-SMOKE-9182", 901))
            self.assertTrue(flow.handle_text(-100123, sender, "Lift reports an error", 902))
            # Причина — отдельный вопрос, поэтому ответ на него идёт последним.
            self.assertTrue(flow.handle_callback(-100123, sender, ["dt", "s", "obstacle"], 903, "cb"))

            session = flow.get_session(-100123, sender["id"])
            self.assertTrue(session.is_completed)
            self.assertEqual(session.result()["answers"], {
                "object": "Robot", "device_type": "A42T C2", "module": "Lifting",
                "device_number": "ROBOT-SMOKE-9182", "description": "Lift reports an error",
                "cause": "Obstacle on the path"})

            flow._confirm(-100123, sender, session, 900)
            deliver.assert_called_once()
            report = deliver.call_args.args[0]
            self.assertEqual(report["warehouse"], "GLP-C")
            self.assertEqual(report["chat_id"], -100123)
            self.assertEqual(report["thread_id"], 88)

    def test_menu_addition_shared_through_supabase_storage(self):
        inserted = []

        with patch("sendToDataBase.rest_get", return_value=[]), \
             patch("sendToDataBase.rest_post",
                   side_effect=lambda table, row: inserted.append(row) or [row]):
            # Узел модулей **отдельный на модель** (07.10.2026): у K50H три
            # модуля, у A42T — четыре, и общий узел показывал всем всё.
            option_id = storage.add_option(
                "robot_module_a42t", "Auxiliary sensor", created_by=12345
            )

        self.assertTrue(option_id)
        self.assertEqual(inserted[0]["label"], "Auxiliary sensor")
        self.assertTrue(editor.is_editor({"id": 12345}))

        with patch("sendToDataBase.rest_get", return_value=inserted):
            merged = storage.apply_overlay(DEFAULT_TREE)

        self.assertIn(
            option_id, {o.id for o in merged.node("robot_module_a42t").options}
        )

    def test_lark_is_attempted_when_supabase_fails(self):
        sent = []

        with patch.object(integrations, "_save", side_effect=RuntimeError("offline")), \
             patch.object(integrations, "_send",
                          side_effect=lambda *args: sent.append(args) or True), \
             patch.object(report_writer, "write_exception", return_value=True):
            outcome = integrations.persist_and_send(_result(), "/tmp/photo.jpg")

        self.assertFalse(outcome["database_saved"])
        self.assertTrue(outcome["lark_delivered"])
        self.assertTrue(outcome["glpc_saved"])
        self.assertEqual(len(sent), 1)

    def test_supabase_success_keeps_independent_lark_outcome(self):
        with patch.object(integrations, "_save", return_value=(True, True, "https://photo.invalid")), \
             patch.object(integrations, "_send", return_value=False), \
             patch.object(report_writer, "write_exception", return_value=True):
            outcome = integrations.persist_and_send(_result("SMALL-P3"), "/tmp/photo.jpg")

        self.assertEqual(outcome, {
            "database_saved": True, "device_queued": True, "lark_delivered": False,
            "glpc_saved": True, "glpc_error": None,
        })

    def test_journal_is_written_for_both_warehouses(self):
        written = []

        with patch.object(integrations, "_save", return_value=(True, False, None)), \
             patch.object(integrations, "_send", return_value=True), \
             patch.object(report_writer, "write_exception",
                          side_effect=lambda result, warehouse=None: written.append(
                              warehouse if warehouse is not None else result.get("warehouse")) or True):
            integrations.persist_and_send(_result("GLP-C"), "/tmp/photo.jpg")
            integrations.persist_and_send(_result("SMALL-P3"), "/tmp/photo.jpg")

        self.assertEqual(written, ["GLP-C", "SMALL-P3"])

    def test_unknown_warehouse_stops_every_destination(self):
        """
        The warehouse is a precondition for all three destinations.

        An unknown warehouse must not produce a detail row with `warehouse=""`
        or a card posted to the default GLP-C group — the fault would be
        announced in the wrong chat while the journal refused it.
        """
        with patch.object(integrations, "_save") as save, \
             patch.object(integrations, "_send") as send:
            outcome = integrations.persist_and_send(_result(warehouse=""), "/tmp/photo.jpg")

        save.assert_not_called()
        send.assert_not_called()
        self.assertFalse(outcome["database_saved"])
        self.assertFalse(outcome["lark_delivered"])
        self.assertFalse(outcome["glpc_saved"])
        self.assertIsNotNone(outcome["glpc_error"])

    def test_journal_write_error_does_not_stop_other_destinations(self):
        """A genuine write failure stays independent, unlike a warehouse refusal."""
        with patch.object(integrations, "_save", return_value=(True, False, None)), \
             patch.object(integrations, "_send", return_value=True), \
             patch.object(report_writer, "write_exception", return_value=False):
            outcome = integrations.persist_and_send(_result(), "/tmp/photo.jpg")

        self.assertFalse(outcome["glpc_saved"])
        self.assertIsNone(outcome["glpc_error"])
        self.assertTrue(outcome["database_saved"])
        self.assertTrue(outcome["lark_delivered"])

    def test_confirm_message_reports_refusal_to_operator(self):
        """The operator must learn the report was not filed, and why."""
        sender = {"id": 1, "username": "u"}
        session = Session(tree=DEFAULT_TREE)
        session.data.update({"warehouse": "", "image": "/tmp/x.jpg", "message_id": 5})
        captions = []

        with patch.object(integrations, "persist_and_send", return_value={
                "database_saved": False, "device_queued": False,
                "lark_delivered": False, "glpc_saved": False,
                "glpc_error": "Warehouse is not set for this report."}), \
             patch.object(flow, "edit_caption",
                          side_effect=lambda *a, **k: captions.append(a[2])), \
             patch.dict(sys.modules, {"telegram_store": types.SimpleNamespace(
                 get_employee_name=lambda _: "U")}):
            flow._confirm(-100, sender, session, 5)

        self.assertTrue(captions)
        self.assertIn("Not filed in the journal", captions[0])
        self.assertIn("Warehouse is not set", captions[0])

    def test_confirm_message_reports_success(self):
        sender = {"id": 1, "username": "u"}
        session = Session(tree=DEFAULT_TREE)
        session.data.update({"warehouse": "SMALL-P3", "image": "/tmp/x.jpg", "message_id": 5})
        captions = []

        with patch.object(integrations, "persist_and_send", return_value={
                "database_saved": True, "device_queued": False,
                "lark_delivered": True, "glpc_saved": True, "glpc_error": None}), \
             patch.object(flow, "edit_caption",
                          side_effect=lambda *a, **k: captions.append(a[2])), \
             patch.dict(sys.modules, {"telegram_store": types.SimpleNamespace(
                 get_employee_name=lambda _: "U")}):
            flow._confirm(-100, sender, session, 5)

        self.assertIn("Journal entry saved", captions[0])
        self.assertIn("Lark card sent", captions[0])


class DeviceNumberShapeChecks(unittest.TestCase):
    """В очередь «неизвестных устройств» должно попадать только то, что может
    быть номером устройства.

    Найдено по живой базе: из 93 заявок **71** содержали свободный текст
    (`EX: 驱动组件异常 … 3545`, `3685 tray problem`). Причина: очередь
    заполнялась по условию «лукап по справочнику пуст», а для текста он пуст
    **всегда** — не потому, что устройства нет, а потому что строка не может
    совпасть с кодом. Из 12 текстовых заявок, начинавшихся с числа, **7**
    указывали на устройства, которые в справочнике **есть**.
    """

    def test_plain_numbers_pass(self):
        """Настоящие номера обязаны проходить: все коды справочника числовые."""
        for value in ("3490", "3747", "13", "2520"):
            self.assertTrue(
                integrations.looks_like_device_number(value), value
            )

    def test_composite_numbers_pass(self):
        """Составной номер — документированный формат ввода, а не мусор.

        В `tree_config.py` он приведён как пример («H108/1834»), и в журнале
        такие записи есть. Отсекать его значило бы ломать поддержанный ввод.
        """
        for value in ("H108/1834", "3647/3636", "3829/3570"):
            self.assertTrue(
                integrations.looks_like_device_number(value), value
            )

    def test_free_text_is_refused(self):
        """Текст описания в поле номера не должен создавать заявку.

        Значения — реальные из очереди на 04.10.2026.
        """
        for value in (
            "EX: 驱动组件异常 Driver component exception. 3545",
            "EX: 取放箱位置错误 Wrong pick and place box position. 48",
            "3685 tray problem",
            "3683 unable to drive",
            "3699 ,rotation",
        ):
            self.assertFalse(
                integrations.looks_like_device_number(value), value
            )

    def test_ambiguous_value_is_left_to_a_human(self):
        """Неоднозначное значение НЕ отсекаем — границу проводим по доказанному.

        `3452/3707/H156` — реальная заявка из очереди. Пробела нет, все символы
        ASCII, вид составного номера. Отличить её от настоящего составного
        номера (`H108/1834`) по форме **нельзя**, а придумывать правило «три
        части через слэш — это текст» значило бы гадать. Такая заявка остаётся
        человеку: показать её в очереди дешевле, чем молча потерять настоящий
        номер.
        """
        self.assertTrue(integrations.looks_like_device_number("3452/3707/H156"))

    def test_documented_composite_and_ascii_codes_still_pass(self):
        """Проверка не должна сужать задокументированные форматы.

        Первая версия перечисляла разрешённые символы (`0-9 / - _`) и отсекала
        `H108/1834` — составной номер, который сам проект приводит как штатный
        пример ввода (`tree_config.py`), и `ROBOT-SMOKE-9182` из собственных
        тестов. Тест это поймал.
        """
        for value in ("H108/1834", "ROBOT-SMOKE-9182", "Shelf-3938-3002-20"):
            self.assertTrue(
                integrations.looks_like_device_number(value), value
            )

    def test_empty_values_are_refused(self):
        for value in (None, "", "   ", "\t"):
            self.assertFalse(integrations.looks_like_device_number(value), repr(value))

    def test_queue_is_not_written_for_free_text(self):
        """Сквозная проверка: текст в номере не создаёт строку в очереди.

        Проверяется именно отсутствие вызова записи в очередь, а не только
        форма помощника: иначе можно поправить помощник и забыть подключить его.
        """
        posted = []

        def fake_rest_post(table, payload, ignore_conflict=False):
            posted.append(table)
            return [{"id": 1}]

        def fake_rest_get(table, params=None, optional=False):
            if table == "equipment":
                return []  # устройство «не найдено»
            return []

        import sendToDataBase

        with patch.object(sendToDataBase, "rest_post", fake_rest_post), patch.object(
            sendToDataBase, "rest_get", fake_rest_get
        ):
            integrations._save(
                {
                    "answers": {
                        "object": "Robot",
                        "device_type": "K50H",
                        "device_number": "3685 tray problem",
                        "description": "3685 tray problem",
                    },
                    "warehouse": "GLP-C",
                    "employee": "Operator",
                    "user_id": 1,
                    "chat_id": -100,
                    "message_id": 5,
                    "path": [],
                },
                "",
            )

        self.assertNotIn(
            integrations.QUEUE,
            posted,
            "текст в поле номера создал заявку в очереди",
        )

    def test_queue_is_written_for_a_real_unknown_number(self):
        """А настоящий неизвестный номер заявку создаёт — смысл очереди цел."""
        posted = []

        def fake_rest_post(table, payload, ignore_conflict=False):
            posted.append(table)
            return [{"id": 1}]

        def fake_rest_get(table, params=None, optional=False):
            if table == "equipment":
                return []  # устройства нет — это и есть повод для заявки
            return []

        import sendToDataBase

        with patch.object(sendToDataBase, "rest_post", fake_rest_post), patch.object(
            sendToDataBase, "rest_get", fake_rest_get
        ):
            integrations._save(
                {
                    "answers": {
                        "object": "Robot",
                        "device_type": "K50H",
                        "device_number": "99999",
                        "description": "lift failure",
                    },
                    "warehouse": "GLP-C",
                    "employee": "Operator",
                    "user_id": 1,
                    "chat_id": -100,
                    "message_id": 6,
                    "path": [],
                },
                "",
            )

        self.assertIn(integrations.QUEUE, posted, "заявка не создана")


class CategoryVocabularyChecks(unittest.TestCase):
    """Категория из дерева должна совпадать со словарём справочника.

    Дерево кладёт в `answers.object` **подпись** варианта, а не id: для зарядной
    станции это `Charging station` (`tree_config.py`). Бот превращает её в
    значение колонки `object_type`, и оно обязано быть из того же словаря, что
    `equipment.category`, иначе группировка по категориям разделит одно понятие
    на две строки.

    Найдено на живой базе: словарь там
    `charging` / `qr_code` / `robot` / `workstation` — значения `qr` и
    `charging station` отсутствуют, а бот их писал.
    """

    #: Словарь из данных (`select distinct category from equipment`).
    DATABASE_VOCABULARY = {"robot", "workstation", "charging", "qr_code"}

    #: Подписи, которые реально отдаёт дерево.
    TREE_LABELS = ("Robot", "Workstation", "Charging station", "QR Code")

    def test_every_tree_label_maps_into_the_database_vocabulary(self):
        for label in self.TREE_LABELS:
            value = report_writer.intake_category(label)

            self.assertIn(
                value,
                self.DATABASE_VOCABULARY,
                f"{label!r} → {value!r}, а в справочнике только "
                f"{sorted(self.DATABASE_VOCABULARY)}",
            )

    def test_qr_is_written_as_qr_code(self):
        """`qr` и `qr_code` — одно понятие; в колонке должно быть одно написание.

        API канонизирует `qr` → `qr_code`, и его комментарий называет два
        написания одной сущности недопустимыми. Бот писал `qr` напрямую, минуя
        API, — то есть расхождение было реальным.
        """
        for label in ("QR Code", "qr", "qr code", "qr_code"):
            self.assertEqual(report_writer.intake_category(label), "qr_code")

    def test_charging_station_label_maps_to_charging(self):
        self.assertEqual(
            report_writer.intake_category("Charging station"), "charging"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
