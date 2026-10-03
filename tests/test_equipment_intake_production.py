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

        try:
            bot.TOPIC_RAW.clear()
            bot.TOPIC_RAW.update({"error": "2", "error:SMALL-P3": "77"})

            self.assertEqual(bot.error_warehouse_for_thread(-100, 2), "GLP-C")
            self.assertEqual(bot.error_warehouse_for_thread(-100, 77), "SMALL-P3")
            # Unknown and general topics are not error topics.
            self.assertIsNone(bot.error_warehouse_for_thread(-100, 999))
            self.assertIsNone(bot.error_warehouse_for_thread(-100, None))
        finally:
            bot.TOPIC_RAW.clear()
            bot.TOPIC_RAW.update(original)

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

            for option in ("robot", "a42t_c2", "lifting"):
                self.assertTrue(flow.handle_callback(-100123, sender, ["dt", "s", option], 900, "cb"))

            self.assertTrue(flow.handle_text(-100123, sender, "ROBOT-SMOKE-9182", 901))
            self.assertTrue(flow.handle_text(-100123, sender, "Lift reports an error", 902))

            session = flow.get_session(-100123, sender["id"])
            self.assertTrue(session.is_completed)
            self.assertEqual(session.result()["answers"], {
                "object": "Robot", "device_type": "A42T C2", "module": "Lifting",
                "device_number": "ROBOT-SMOKE-9182", "description": "Lift reports an error"})

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
            option_id = storage.add_option("robot_module", "Auxiliary sensor", created_by=12345)

        self.assertTrue(option_id)
        self.assertEqual(inserted[0]["label"], "Auxiliary sensor")
        self.assertTrue(editor.is_editor({"id": 12345}))

        with patch("sendToDataBase.rest_get", return_value=inserted):
            merged = storage.apply_overlay(DEFAULT_TREE)

        self.assertIn(option_id, {o.id for o in merged.node("robot_module").options})

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
