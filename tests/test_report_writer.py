"""
Adversarial coverage for the guided-intake journal writer.

This file exists to attack `equipment_intake/report_writer.py` and the dispatch
guards around it, not to demonstrate that they work on the happy path. It covers
the ordering rules that decide the warehouse, the timestamp handling that decides
the shift, identifier shapes, and the regressions that were actually found while
writing the module:

* `str.isdecimal()` accepting Arabic-Indic digits and inventing a robot number;
* `flow` stamping `created_at` while the writer read `received_at`, so the
  report time silently became "server now".

Every test is offline: the database URL points at an unreachable host and the
Telegram client is stubbed.

Run:

    python3 tests/test_report_writer.py
"""

import json
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["SUPABASE_URL"] = "http://supabase.invalid"
os.environ["SUPABASE_SERVICE_KEY"] = "offline-test-key"
os.environ["TELEGRAM_DRY_RUN"] = "1"
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000:TEST-TOKEN")

from datetime import datetime, timedelta, timezone  # noqa: E402

from equipment_intake import report_writer as rw  # noqa: E402
from shift import WARSAW_TZ  # noqa: E402

WARSAW = WARSAW_TZ


def answers(**overrides):
    data = {
        "object": "Robot",
        "device_type": "A42T C2",
        "module": "Lifting",
        "device_number": "3490",
        "description": "Lift reports an error",
    }
    data.update(overrides)
    return data


def result(warehouse="GLP-C", **overrides):
    data = {
        "answers": answers(),
        "warehouse": warehouse,
        "employee": "Smoke User",
        "user_id": 12345,
        "username": "smoke-user",
        "chat_id": -100123,
        "message_id": 777,
        "image": "/tmp/photo-1.jpg",
    }
    data.update(overrides)
    return data


def row_for(warehouse="GLP-C", resolved=None, **overrides):
    """Build a row with the type lookup stubbed to a known answer."""
    with mock.patch.object(rw, "canonical_type", return_value=resolved):
        return rw.build_row(result(warehouse, **overrides), warehouse)


class WarehouseGuardChecks(unittest.TestCase):
    """The warehouse decides which site a fault is filed under."""

    def test_accepts_only_configured_titles(self):
        self.assertEqual(rw.validate_warehouse("GLP-C"), "GLP-C")
        self.assertEqual(rw.validate_warehouse("SMALL-P3"), "SMALL-P3")

    def test_trims_surrounding_whitespace(self):
        self.assertEqual(rw.validate_warehouse("  SMALL-P3\n"), "SMALL-P3")

    def test_rejects_empty_values(self):
        for value in ("", "   ", "\t", None):
            with self.subTest(value=value):
                with self.assertRaises(rw.WarehouseError):
                    rw.validate_warehouse(value)

    def test_rejects_unconfigured_warehouses(self):
        # These exist in the `warehouses` table but the bot does not serve them.
        for value in ("PNT-A", "P3-DC-1", "P3-DC-3"):
            with self.subTest(value=value):
                with self.assertRaises(rw.WarehouseError):
                    rw.validate_warehouse(value)

    def test_rejects_case_and_separator_variants(self):
        """A near-miss must not be coerced into a real warehouse."""
        for value in ("glp-c", "glpc", "GLP_C", "Small-P3", "SMALL P3"):
            with self.subTest(value=value):
                with self.assertRaises(rw.WarehouseError):
                    rw.validate_warehouse(value)

    def test_rejects_non_string_values(self):
        for value in (123, 0, [], {}):
            with self.subTest(value=value):
                with self.assertRaises(rw.WarehouseError):
                    rw.validate_warehouse(value)

    def test_write_refuses_before_touching_the_database(self):
        """A refusal must not reach PostgREST at all."""
        for warehouse in ("", None, "PNT-A"):
            with self.subTest(warehouse=warehouse):
                with mock.patch("sendToDataBase.rest_post") as post:
                    with self.assertRaises(rw.WarehouseError):
                        rw.write_exception(result(warehouse=warehouse), warehouse)
                post.assert_not_called()

    def test_write_uses_explicit_argument_over_payload(self):
        """The caller's warehouse wins, so a stale payload cannot redirect it."""
        with mock.patch.object(rw, "write_exception", wraps=rw.write_exception):
            with mock.patch("sendToDataBase.rest_post", return_value=[{"id": 1}]) as post, \
                 mock.patch.object(rw, "canonical_type", return_value=None):
                self.assertTrue(rw.write_exception(result("GLP-C"), "SMALL-P3"))

        self.assertEqual(post.call_args.args[1]["warehouse"], "SMALL-P3")

    def test_write_falls_back_to_payload_warehouse(self):
        with mock.patch("sendToDataBase.rest_post", return_value=[{"id": 1}]) as post, \
             mock.patch.object(rw, "canonical_type", return_value=None):
            self.assertTrue(rw.write_exception(result("SMALL-P3")))

        self.assertEqual(post.call_args.args[1]["warehouse"], "SMALL-P3")

    def test_write_reports_failure_when_insert_fails(self):
        with mock.patch("sendToDataBase.rest_post", return_value=None), \
             mock.patch.object(rw, "canonical_type", return_value=None):
            self.assertFalse(rw.write_exception(result()))

    def test_write_never_raises_on_conflict(self):
        """A repeated delivery is success, not an error."""
        with mock.patch("sendToDataBase.rest_post", return_value=[]), \
             mock.patch.object(rw, "canonical_type", return_value=None):
            self.assertTrue(rw.write_exception(result()))


class CanonicalTypeChecks(unittest.TestCase):
    """`device_type` must describe the fleet, not the tapped button."""

    def test_lookup_is_scoped_to_warehouse_and_category(self):
        """SMALL-P3 code 13 is both a robot and a charging station."""
        calls = []

        def fake_rest_get(table, params):
            calls.append((table, params))
            return [{"equipment_type_id": 13}] if table == "equipment" else [{"type": "RT_KUBOT"}]

        with mock.patch("sendToDataBase.rest_get", side_effect=fake_rest_get):
            self.assertEqual(rw.canonical_type("SMALL-P3", "robot", "13"), "RT_KUBOT")

        equipment = calls[0][1]
        self.assertEqual(equipment["warehouse"], "eq.SMALL-P3")
        self.assertEqual(equipment["category"], "eq.robot")
        self.assertEqual(equipment["equipment_code"], "eq.13")

    def test_qr_categories_skip_the_inventory(self):
        """QR codes are not inventory units; no lookup should happen."""
        with mock.patch("sendToDataBase.rest_get") as get:
            self.assertIsNone(rw.canonical_type("GLP-C", "qr", "1"))
        get.assert_not_called()

    def test_empty_code_skips_the_inventory(self):
        with mock.patch("sendToDataBase.rest_get") as get:
            self.assertIsNone(rw.canonical_type("GLP-C", "robot", ""))
        get.assert_not_called()

    def test_unknown_device_resolves_to_none(self):
        with mock.patch("sendToDataBase.rest_get", return_value=[]):
            self.assertIsNone(rw.canonical_type("GLP-C", "robot", "999999"))

    def test_failed_lookup_is_not_treated_as_known(self):
        """A read failure must not be mistaken for 'device absent'."""
        with mock.patch("sendToDataBase.rest_get", return_value=None):
            self.assertIsNone(rw.canonical_type("GLP-C", "robot", "3490"))

    def test_missing_type_row_resolves_to_none(self):
        def fake(table, params):
            return [{"equipment_type_id": 999}] if table == "equipment" else []

        with mock.patch("sendToDataBase.rest_get", side_effect=fake):
            self.assertIsNone(rw.canonical_type("GLP-C", "robot", "3490"))

    def test_null_type_id_resolves_to_none(self):
        with mock.patch("sendToDataBase.rest_get", return_value=[{"equipment_type_id": None}]):
            self.assertIsNone(rw.canonical_type("GLP-C", "robot", "3490"))

    def test_blank_type_name_resolves_to_none(self):
        def fake(table, params):
            return [{"equipment_type_id": 2}] if table == "equipment" else [{"type": "  "}]

        with mock.patch("sendToDataBase.rest_get", side_effect=fake):
            self.assertIsNone(rw.canonical_type("GLP-C", "robot", "3490"))


class IdentifierChecks(unittest.TestCase):
    def test_plain_device_number(self):
        self.assertEqual(rw.identifier({"device_number": "3490"}), "3490")

    def test_shelf_number_wins_over_device_number(self):
        """Shelf identity is the shelf number, not the printed QR code."""
        self.assertEqual(
            rw.identifier({"shelf_number": "A-12", "device_number": "should-not-win"}),
            "A-12",
        )

    def test_qr_floor_combines_x_y_zone(self):
        self.assertEqual(
            rw.identifier({"qr_x": "162", "qr_y": "382", "qr_zone": "30"}),
            "X=162; Y=382; Zone=30",
        )

    def test_missing_identity_is_empty(self):
        self.assertEqual(rw.identifier({}), "")
        self.assertEqual(rw.identifier({"device_number": "   "}), "")

    def test_whitespace_is_trimmed(self):
        self.assertEqual(rw.identifier({"device_number": "  3490  "}), "3490")


class RobotNumberChecks(unittest.TestCase):
    def test_ascii_digits_become_an_integer(self):
        self.assertEqual(rw._robot_number("3490"), 3490)
        self.assertEqual(rw._robot_number("0"), 0)

    def test_non_ascii_digits_are_refused(self):
        """
        Regression: `str.isdecimal()` accepts Arabic-Indic digits.

        Without the ASCII guard, '١٢٣' silently became robot 123 — a fault
        attributed to a robot that was never reported.
        """
        for value in ("١٢٣", "٣٤٩٠", "１２３"):
            with self.subTest(value=value):
                self.assertIsNone(rw._robot_number(value))

    def test_non_numeric_identifiers_are_refused(self):
        for value in ("Shelf-3938", "X=1; Y=2; Zone=3", "12.5", "12a", "-5", "", None):
            with self.subTest(value=value):
                self.assertIsNone(rw._robot_number(value))

    def test_bigint_bounds(self):
        self.assertEqual(rw._robot_number("9223372036854775807"), 9_223_372_036_854_775_807)
        self.assertIsNone(rw._robot_number("9223372036854775808"))


class AsciiDigitChecks(unittest.TestCase):
    """
    `str.isdigit()`/`int()` accept non-ASCII digits.

    '٣٧٨٠' passes an `isdigit()` guard and `int()` turns it into 3780 — a
    *different* robot. An operator typing digits in a local numeral system would
    have the fault filed against an unrelated robot. The shared helpers must
    reject those values instead.
    """

    def test_ascii_digits_accepted(self):
        from text_utils import ascii_digits

        for value in ("0", "3780", "9223372036854775807"):
            with self.subTest(value=value):
                self.assertTrue(ascii_digits(value))

    def test_non_ascii_digits_rejected(self):
        from text_utils import ascii_digits

        for value in ("٣", "٣٧٨٠", "１２３", "١٢٣"):
            with self.subTest(value=value):
                self.assertFalse(ascii_digits(value))

    def test_garbage_rejected(self):
        from text_utils import ascii_digits

        for value in ("", "   ", None, "12.5", "abc", "1 2", "１２"):
            with self.subTest(value=value):
                self.assertFalse(ascii_digits(value))

    def test_ascii_digits_trims_whitespace(self):
        from text_utils import ascii_digits

        self.assertTrue(ascii_digits("  3780  "))

    def test_to_int_parses_only_ascii(self):
        from text_utils import to_int

        self.assertEqual(to_int("3780"), 3780)
        self.assertEqual(to_int("  42 "), 42)
        self.assertEqual(to_int("-5"), -5)
        self.assertEqual(to_int("+7"), 7)

    def test_to_int_refuses_non_ascii_digits(self):
        """The value that used to become a different robot number."""
        from text_utils import to_int

        for value in ("٣٧٨٠", "١٢٣", "１２３"):
            with self.subTest(value=value):
                self.assertIsNone(to_int(value))

    def test_to_int_never_raises(self):
        from text_utils import to_int

        for value in ("", "   ", None, "abc", "12.5", "1e3", [], {}):
            with self.subTest(value=value):
                self.assertIsNone(to_int(value))

    def test_robot_parser_path_rejects_local_digits(self):
        """
        End-to-end: the parsed robot survives the ASCII gate, so the message is
        refused with a hint instead of being filed against robot 3780.
        """
        from error_parser import parse_error_message
        from text_utils import ascii_digits

        parsed = parse_error_message("Unable to drive: safety. ٣٧٨٠")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["robot"], "٣٧٨٠")
        self.assertFalse(ascii_digits(parsed["robot"]))

        normal = parse_error_message("Unable to drive: safety. 3780")
        self.assertTrue(ascii_digits(normal["robot"]))

    def test_number_node_rejects_local_digits(self):
        """
        `\\d` in a regex and `float()` both accept Arabic-Indic digits, so a
        NUMBER node would read '٣٧٨' as 378. No NUMBER node exists in the active
        tree today, so this path is dormant — the test keeps it correct for when
        one is added (the generated tree has them).
        """
        from equipment_intake.engine import EngineError, Session
        from equipment_intake.types import Node, NodeType

        node = Node(id="n", title="N", type=NodeType.NUMBER)

        for value in ("٣٧٨", "٣.٥", "１２３"):
            with self.subTest(value=value):
                with self.assertRaises(EngineError):
                    Session._parse_number(node, value)

    def test_number_node_still_accepts_normal_values(self):
        from equipment_intake.engine import Session
        from equipment_intake.types import Node, NodeType

        node = Node(id="n", title="N", type=NodeType.NUMBER)

        self.assertEqual(Session._parse_number(node, "378"), 378)
        self.assertEqual(Session._parse_number(node, "3.5"), 3.5)
        self.assertEqual(Session._parse_number(node, "3,5"), 3.5)
        self.assertEqual(Session._parse_number(node, "3 шт"), 3)
        self.assertEqual(Session._parse_number(node, "-12"), -12)

    def test_number_node_respects_bounds(self):
        from equipment_intake.engine import EngineError, Session
        from equipment_intake.types import Node, NodeType

        node = Node(id="n", title="N", type=NodeType.NUMBER,
                    min_value=1, max_value=10)

        self.assertEqual(Session._parse_number(node, "5"), 5)
        for value in ("0", "11"):
            with self.subTest(value=value):
                with self.assertRaises(EngineError):
                    Session._parse_number(node, value)


class TruncateChecks(unittest.TestCase):
    """
    Telegram and Lark measure text in UTF-16 code units, not Python characters.

    A character outside the BMP (an emoji, some CJK) costs two units. The old
    implementation truncated by `len()`, so a description of 3000 emoji became
    "3000 characters" — 6000 UTF-16 units — and the API rejected the message
    instead of showing a shortened one.
    """

    def test_utf16_length_counts_astral_chars_twice(self):
        from text_utils import utf16_length

        self.assertEqual(utf16_length("abc"), 3)
        self.assertEqual(utf16_length("😀"), 2)
        self.assertEqual(utf16_length("a😀b"), 4)
        self.assertEqual(utf16_length(""), 0)
        self.assertEqual(utf16_length(None), 0)

    def test_astral_text_is_truncated_to_the_real_limit(self):
        from text_utils import TELEGRAM_TEXT_LIMIT, truncate, utf16_length

        kept = truncate("😀" * 3000, TELEGRAM_TEXT_LIMIT)

        self.assertLessEqual(utf16_length(kept), TELEGRAM_TEXT_LIMIT)
        # The old behaviour would have produced 6000 units.
        self.assertGreater(len(kept), 0)

    def test_every_limit_is_respected(self):
        from text_utils import truncate, utf16_length

        for text in ("😀" * 3000, "a" * 5000, "😀a" * 2000):
            for limit in (10, 100, 500, 4000):
                with self.subTest(limit=limit, text=text[:4]):
                    self.assertLessEqual(utf16_length(truncate(text, limit)), limit)

    def test_short_text_is_returned_unchanged(self):
        from text_utils import truncate

        self.assertEqual(truncate("short", 500), "short")
        self.assertEqual(truncate("", 500), "")
        self.assertEqual(truncate(None, 500), "")

    def test_suffix_is_appended_when_truncated(self):
        from text_utils import truncate

        self.assertTrue(truncate("x" * 100, 10).endswith("…"))

    def test_zero_or_negative_limit_is_a_no_op(self):
        from text_utils import truncate

        self.assertEqual(truncate("abc", 0), "abc")
        self.assertEqual(truncate("abc", -5), "abc")

    def test_ascii_behaviour_is_unchanged(self):
        from text_utils import truncate

        self.assertEqual(len(truncate("a" * 5000, 4000)), 4000)
        self.assertEqual(truncate("a" * 4000, 4000), "a" * 4000)


class RowMappingChecks(unittest.TestCase):
    def test_canonical_type_replaces_the_label(self):
        row = row_for(resolved="RT_KUBOT_MINI_HAIFLEX")

        self.assertEqual(row["device_type"], "RT_KUBOT_MINI_HAIFLEX")
        self.assertIn("Reported type: A42T C2", row["issue_description"])

    def test_label_is_kept_when_type_is_unknown(self):
        row = row_for(resolved=None)

        self.assertEqual(row["device_type"], "A42T C2")
        self.assertNotIn("Reported type", row["issue_description"])

    def test_label_is_not_repeated_when_it_equals_the_type(self):
        row = row_for(resolved="A42T C2")

        self.assertEqual(row["device_type"], "A42T C2")
        self.assertNotIn("Reported type", row["issue_description"])

    def test_numeric_identity_fills_error_robot(self):
        self.assertEqual(row_for()["error_robot"], 3490)

    def test_textual_identity_keeps_the_number_column_null(self):
        data = result()
        data["answers"]["device_number"] = "Shelf-3938-3002-20"

        with mock.patch.object(rw, "canonical_type", return_value=None):
            row = rw.build_row(data, "GLP-C")

        self.assertIsNone(row["error_robot"])
        self.assertIn("Shelf-3938-3002-20", row["issue_description"])

    def test_module_is_preserved_in_second_column_and_details(self):
        row = row_for(resolved=None)

        self.assertEqual(row["second_column"], "Lifting")
        self.assertIn("Module: Lifting", row["issue_description"])

    def test_missing_module_falls_back_to_device_type(self):
        data = result()
        data["answers"].pop("module")

        with mock.patch.object(rw, "canonical_type", return_value=None):
            row = rw.build_row(data, "GLP-C")

        self.assertEqual(row["second_column"], "A42T C2")
        self.assertNotIn("Module:", row["issue_description"])

    def test_end_time_follows_the_table_convention(self):
        """
        `error_end_time = error_start_time + solving_time` across the live table
        (verified on 23393 rows). With no issue template `solving_time` is 0, so
        the expected end equals the start — NULL would be the odd one out here.
        """
        row = row_for(resolved=None)

        self.assertEqual(row["solving_time"], 0)
        self.assertTrue(row["error_start_time"])
        self.assertEqual(row["error_end_time"], row["error_start_time"])

    def test_end_time_tracks_the_report_time_not_the_server_clock(self):
        """The derived value must follow the report's own timestamp."""
        row = row_for(resolved=None, received_at="2026-10-02T04:00:00+00:00")

        self.assertEqual(row["error_end_time"], row["error_start_time"])
        self.assertEqual(
            datetime.fromisoformat(row["error_end_time"]).astimezone(timezone.utc),
            datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc),
        )

    def test_warehouse_column_matches_the_argument(self):
        for warehouse in ("GLP-C", "SMALL-P3"):
            with self.subTest(warehouse=warehouse):
                self.assertEqual(row_for(warehouse=warehouse)["warehouse"], warehouse)

    def test_legacy_issue_warehouse_constant_is_unchanged(self):
        """Kept for parity with existing rows; changing it is a separate call."""
        self.assertEqual(row_for()["issue_warehouse"], "C2")

    def test_employee_prefers_the_linked_name(self):
        self.assertEqual(row_for()["employee"], "Smoke User")

    def test_employee_falls_back_to_username_then_id(self):
        without_employee = row_for(employee="")
        self.assertEqual(without_employee["employee"], "smoke-user")

        without_both = row_for(employee="", username="")
        self.assertEqual(without_both["employee"], "Telegram 12345")

    def test_missing_answers_do_not_raise(self):
        with mock.patch.object(rw, "canonical_type", return_value=None):
            row = rw.build_row({"warehouse": "GLP-C", "user_id": 1}, "GLP-C")

        self.assertEqual(row["device_type"], "")
        self.assertIsNone(row["error_robot"])


class EmployeeCardChecks(unittest.TestCase):
    """`add_by` must carry the employee's card, or stay honestly empty."""

    def test_card_id_from_the_result_reaches_add_by(self):
        row = row_for(resolved=None, employee_card_id="CARD-1")

        self.assertEqual(row["add_by"], "CARD-1")

    def test_numeric_card_id_is_preserved(self):
        row = row_for(resolved=None, employee_card_id=60072001)

        self.assertEqual(row["add_by"], 60072001)

    def test_unlinked_employee_leaves_add_by_empty(self):
        """A missing card must not stop the report — same rule as `employee`."""
        row = row_for(resolved=None, employee_card_id=None)

        self.assertIsNone(row["add_by"])

    def test_blank_card_id_is_not_written_as_an_empty_string(self):
        for value in ("", "   ", None):
            with self.subTest(value=value):
                self.assertIsNone(row_for(resolved=None, employee_card_id=value)["add_by"])

    def test_missing_key_does_not_raise(self):
        """Legacy callers that never pass the key still get a valid row."""
        data = result()
        data.pop("employee", None)

        with mock.patch.object(rw, "canonical_type", return_value=None):
            row = rw.build_row(data, "GLP-C")

        self.assertIsNone(row["add_by"])


class PhotoUrlChecks(unittest.TestCase):
    """The photo the operator sent must be attached to the journal row."""

    def test_uploaded_url_reaches_photo_url(self):
        row = row_for(resolved=None, photo_url="https://storage.invalid/p.jpg")

        self.assertEqual(row["photo_url"], "https://storage.invalid/p.jpg")

    def test_failed_upload_leaves_the_column_empty(self):
        """`_save` returns None when the upload fails; the row is still filed."""
        row = row_for(resolved=None, photo_url=None)

        self.assertIsNone(row["photo_url"])

    def test_blank_url_is_normalised_to_null(self):
        row = row_for(resolved=None, photo_url="")

        self.assertIsNone(row["photo_url"])


class AnalyticsColumnChecks(unittest.TestCase):
    """
    The analytics columns arrive with a migration that may lag the deploy.

    PostgREST rejects the whole insert with 400 when a referenced column does not
    exist, so the report must be written without those columns until they land —
    and the expected state must not be logged as an error on every message.
    """

    def setUp(self):
        rw.reset_schema_cache()

    def tearDown(self):
        rw.reset_schema_cache()

    def row(self):
        with mock.patch.object(rw, "canonical_type", return_value=None):
            return rw.build_row(result(), "GLP-C")

    def test_unresolved_reason_is_filled_for_new_rows(self):
        """Признак неполноты заполняется, а не остаётся пустым.

        Колонка `unresolved_reason` заведена миграцией 0067, но её никто не
        заполнял: в живой базе значение было только у строк, которым его
        проставил разовый backfill, а новые записи приходили пустыми. То есть
        по полю нельзя было понять, полная запись или нет, хотя оно ровно для
        этого и нужно.
        """
        # Полная запись: карточка работника и загруженное фото на месте.
        # Это тот случай, который в живом флоу даёт `_save` перед записью.
        complete_result = result(employee_card_id=60130607, photo_url="https://x/p.jpg")

        with mock.patch.object(rw, "canonical_type", return_value=None):
            complete = rw.build_row(complete_result, "GLP-C")

        self.assertIsNone(
            complete["unresolved_reason"], "полная запись помечена как неполная"
        )

        broken = result()
        broken["employee_card_id"] = None
        broken["photo_url"] = None

        with mock.patch.object(rw, "canonical_type", return_value=None):
            row = rw.build_row(broken, "GLP-C")

        self.assertIn("no_employee", row["unresolved_reason"])
        self.assertIn("no_photo", row["unresolved_reason"])

    def test_unresolved_reason_uses_the_same_separator_as_the_migration(self):
        """Разделитель обязан совпадать с backfill'ом в 0067: `", "`, не `","`.

        Одна колонка с двумя форматами — тихая ловушка: потребитель, который
        разберёт строку по `", "` (а именно так пишет миграция
        `concat_ws(', ', …)`), не увидит причин в записях бота. В живой базе уже
        есть строки обоих видов.
        """
        rows = {
            "both": result(employee_card_id=None, photo_url=None),
            "card_only": result(employee_card_id=1, photo_url=None),
        }

        with mock.patch.object(rw, "canonical_type", return_value=None):
            pair = rw.build_row(rows["both"], "GLP-C")
            single = rw.build_row(rows["card_only"], "GLP-C")

        self.assertEqual(pair["unresolved_reason"], "no_employee, no_photo")
        self.assertNotIn(
            ",",
            single["unresolved_reason"].replace(", ", ""),
            "одиночная причина не должна содержать разделитель",
        )

    def test_unresolved_reason_names_only_what_is_missing(self):
        """Признак называет именно то, чего не хватает, а не всё подряд."""
        no_card = result(employee_card_id=None, photo_url="https://x/p.jpg")

        with mock.patch.object(rw, "canonical_type", return_value=None):
            row = rw.build_row(no_card, "GLP-C")

        self.assertEqual(row["unresolved_reason"], "no_employee")

        no_photo = result(employee_card_id=60130607, photo_url=None)

        with mock.patch.object(rw, "canonical_type", return_value=None):
            row = rw.build_row(no_photo, "GLP-C")

        self.assertEqual(row["unresolved_reason"], "no_photo")

    def test_full_row_fills_every_analytics_column(self):
        row = self.row()

        self.assertEqual(row["module"], "Lifting")
        self.assertEqual(row["device_number"], "3490")
        self.assertEqual(row["object_type"], "robot")
        self.assertEqual(row["report_id"], "photo-1.jpg")

    def test_optional_columns_can_be_dropped(self):
        with mock.patch.object(rw, "canonical_type", return_value=None):
            row = rw.build_row(result(), "GLP-C", include_optional=False)

        for column in rw.OPTIONAL_COLUMNS:
            self.assertNotIn(column, row)

        # Everything else is identical, so no report data is lost.
        self.assertEqual(row["error_robot"], 3490)
        self.assertEqual(row["warehouse"], "GLP-C")
        self.assertEqual(row["uniq_key"], "telegram-photo-bot:-100123:777")

    def test_write_works_before_the_migration(self):
        """The only successful insert must be the minimal row."""
        attempts = []

        def fake_get(table, params, optional=False):
            # The analytics probe fails (unknown column), the plain read works:
            # exactly the pre-migration state of a healthy database.
            return None if "module" in str(params.get("select")) else [{"id": 1}]

        def fake_post(table, payload, ignore_conflict=False):
            attempts.append(payload)
            return [{"id": 1}]

        with mock.patch("sendToDataBase.rest_get", side_effect=fake_get), \
             mock.patch("sendToDataBase.rest_post", side_effect=fake_post), \
             mock.patch.object(rw, "canonical_type", return_value=None):
            self.assertTrue(rw.write_exception(result(), "GLP-C"))

        self.assertEqual(len(attempts), 1)
        self.assertNotIn("module", attempts[0])
        self.assertEqual(attempts[0]["uniq_key"], "telegram-photo-bot:-100123:777")

    def test_write_uses_the_analytics_columns_once_they_exist(self):
        attempts = []

        def fake_post(table, payload, ignore_conflict=False):
            attempts.append(payload)
            return [{"id": 1}]

        with mock.patch("sendToDataBase.rest_get", return_value=[{"id": 1}]), \
             mock.patch("sendToDataBase.rest_post", side_effect=fake_post), \
             mock.patch.object(rw, "canonical_type", return_value=None):
            self.assertTrue(rw.write_exception(result(), "GLP-C"))

        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["module"], "Lifting")

    def test_missing_columns_are_remembered_not_probed_per_report(self):
        """Otherwise every report pays for two requests and logs an error."""
        def fake_get(table, params, optional=False):
            return None if "module" in str(params.get("select")) else [{"id": 1}]

        with mock.patch("sendToDataBase.rest_get", side_effect=fake_get) as get, \
             mock.patch("sendToDataBase.rest_post", return_value=[{"id": 1}]), \
             mock.patch.object(rw, "canonical_type", return_value=None):
            rw.write_exception(result(), "GLP-C")
            rw.write_exception(result(chat_id=-100124, message_id=778), "GLP-C")

        # First call: the column probe + the reachability probe. The second call
        # must not probe again.
        self.assertEqual(get.call_count, 2)

    def test_unreachable_database_is_not_mistaken_for_missing_columns(self):
        """
        A network failure must not strip the analytics fields — that would drop
        data silently for the whole cache TTL.
        """
        attempts = []

        def fake_post(table, payload, ignore_conflict=False):
            attempts.append(payload)
            return [{"id": 1}]

        # `None` for both the column probe and the reachability probe.
        with mock.patch("sendToDataBase.rest_get", return_value=None), \
             mock.patch("sendToDataBase.rest_post", side_effect=fake_post), \
             mock.patch.object(rw, "canonical_type", return_value=None):
            self.assertTrue(rw.write_exception(result(), "GLP-C"))

        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["object_type"], "robot")

    def test_unreachable_database_is_not_cached_as_missing_columns(self):
        """The next report must probe again instead of silently dropping data."""
        probe_selects = []

        def fake_get(table, params, optional=False):
            probe_selects.append(str(params.get("select")))
            return None

        with mock.patch("sendToDataBase.rest_get", side_effect=fake_get), \
             mock.patch("sendToDataBase.rest_post", return_value=[{"id": 1}]), \
             mock.patch.object(rw, "canonical_type", return_value=None):
            rw.write_exception(result(), "GLP-C")
            probe_selects.clear()
            rw.write_exception(result(chat_id=-100124, message_id=778), "GLP-C")

        # The second call probed again instead of trusting a stale "no columns".
        self.assertTrue(any("module" in select for select in probe_selects))

    def test_write_retries_without_columns_when_an_insert_is_rejected(self):
        """A rejection over a column still files the report, just leaner."""
        attempts = []
        probes = []

        def fake_get(table, params, optional=False):
            probes.append(params)
            return [{"id": 1}]  # the probe says the columns exist

        def fake_post(table, payload, ignore_conflict=False):
            attempts.append(payload)
            if "module" in payload:
                return None  # rejected over an unknown column
            return [{"id": 1}]

        with mock.patch("sendToDataBase.rest_get", side_effect=fake_get), \
             mock.patch("sendToDataBase.rest_post", side_effect=fake_post), \
             mock.patch.object(rw, "canonical_type", return_value=None):
            self.assertTrue(rw.write_exception(result(), "GLP-C"))

        self.assertEqual(len(attempts), 2)
        self.assertIn("module", attempts[0])
        self.assertNotIn("module", attempts[1])

    def test_a_real_duplicate_is_still_success_without_the_columns(self):
        """`ignore_conflict` semantics must survive the fallback path."""
        responses = iter([None, []])

        with mock.patch("sendToDataBase.rest_get", return_value=[{"id": 1}]), \
             mock.patch("sendToDataBase.rest_post", side_effect=lambda *a, **k: next(responses)), \
             mock.patch.object(rw, "canonical_type", return_value=None):
            self.assertTrue(rw.write_exception(result(), "GLP-C"))


class UniqKeyChecks(unittest.TestCase):
    """The unique index must not merge two genuinely different reports."""

    def test_key_uses_telegram_coordinates(self):
        """One message is exactly one report, and the pair survives redelivery."""
        self.assertEqual(row_for()["uniq_key"], "telegram-photo-bot:-100123:777")

    def test_key_is_stable_for_the_same_input(self):
        """Idempotency depends on this being deterministic."""
        self.assertEqual(row_for()["uniq_key"], row_for()["uniq_key"])

    def test_redelivered_update_keeps_one_key(self):
        self.assertEqual(
            row_for(message_id=777)["uniq_key"],
            row_for(message_id=777)["uniq_key"],
        )

    def test_different_messages_get_different_keys(self):
        self.assertNotEqual(
            row_for(message_id=777)["uniq_key"],
            row_for(message_id=778)["uniq_key"],
        )

    def test_two_reports_without_a_photo_do_not_collide(self):
        """
        Regression: the key fell back to the device number, so two faults on the
        same robot by the same sender collapsed into one row and the second was
        dropped by the unique index as a duplicate.
        """
        first = row_for(image="", message_id=1)
        second = row_for(image="", message_id=2)

        self.assertNotEqual(first["uniq_key"], second["uniq_key"])

    def test_key_differs_per_chat(self):
        self.assertNotEqual(
            row_for(chat_id=-100)["uniq_key"],
            row_for(chat_id=-200)["uniq_key"],
        )

    def test_key_falls_back_to_sender_and_photo(self):
        """Without Telegram coordinates the older shape is still usable."""
        row = row_for(chat_id=None, message_id=None)
        self.assertEqual(row["uniq_key"], "telegram-photo-bot:12345:photo-1.jpg")

    def test_key_uses_report_id_when_the_photo_path_is_blank(self):
        """A blank photo path falls back to the device number."""
        row = row_for(chat_id=None, message_id=None, image="   ")
        self.assertEqual(row["uniq_key"], "telegram-photo-bot:12345:3490")

    def test_key_shape_cannot_collide_with_the_legacy_format(self):
        """Legacy keys look like `<user>.<robot>.<timestamp>`."""
        key = row_for()["uniq_key"]
        self.assertTrue(key.startswith("telegram-photo-bot:"))
        self.assertNotRegex(key.split(":", 2)[2], r"^\d{4}-\d{2}-\d{2}T")


class ShiftTimestampChecks(unittest.TestCase):
    """Shift assignment is decided in Europe/Warsaw, from the report time."""

    def shift_of(self, **overrides):
        return row_for(resolved=None, **overrides)

    def test_day_shift_starts_at_0600_warsaw(self):
        # 04:00 UTC == 06:00 Warsaw (summer).
        row = self.shift_of(received_at="2026-10-02T04:00:00+00:00")
        self.assertEqual((row["issue_data"], row["shift_type"]), ("2026-10-02", "day"))

    def test_just_before_0600_is_night_of_the_previous_day(self):
        row = self.shift_of(received_at="2026-10-02T03:59:00+00:00")
        self.assertEqual((row["issue_data"], row["shift_type"]), ("2026-10-01", "night"))

    def test_night_shift_starts_at_1800_warsaw(self):
        row = self.shift_of(received_at="2026-10-02T16:00:00+00:00")
        self.assertEqual((row["issue_data"], row["shift_type"]), ("2026-10-02", "night"))

    def test_just_before_1800_is_still_day(self):
        row = self.shift_of(received_at="2026-10-02T15:59:00+00:00")
        self.assertEqual((row["issue_data"], row["shift_type"]), ("2026-10-02", "day"))

    def test_after_midnight_utc_belongs_to_the_previous_shift_date(self):
        # 00:30 UTC on the 2nd == 02:30 Warsaw on the 2nd, night of the 1st.
        row = self.shift_of(received_at="2026-10-02T00:30:00+00:00")
        self.assertEqual((row["issue_data"], row["shift_type"]), ("2026-10-01", "night"))

    def test_z_suffix_is_parsed(self):
        row = self.shift_of(received_at="2026-10-02T04:00:00Z")
        self.assertEqual(row["shift_type"], "day")

    def test_result_timestamp_is_preserved_exactly(self):
        row = self.shift_of(received_at="2026-10-02T04:00:00+00:00")
        self.assertEqual(
            datetime.fromisoformat(row["error_start_time"]).astimezone(timezone.utc),
            datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc),
        )

    def test_naive_timestamp_is_read_as_host_local_time(self):
        """
        `flow` stamps `created_at` with `time.strftime`, which is host-local.

        Reading that as UTC would move the shift on a non-UTC host, so it is
        interpreted as local time instead.
        """
        naive = "2026-10-02T08:00:00"
        expected = datetime.fromisoformat(naive).astimezone()

        row = self.shift_of(created_at=naive)
        self.assertEqual(
            datetime.fromisoformat(row["error_start_time"]).astimezone(timezone.utc),
            expected.astimezone(timezone.utc),
        )

    def test_created_at_is_used_when_received_at_is_absent(self):
        """
        Regression: `flow` writes `created_at`, the writer used to read only
        `received_at`, and the report time silently became 'server now'.
        """
        row = self.shift_of(created_at="2026-10-02T04:00:00+00:00")
        self.assertEqual(
            datetime.fromisoformat(row["error_start_time"]).astimezone(timezone.utc),
            datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc),
        )

    def test_received_at_wins_over_created_at(self):
        row = self.shift_of(
            received_at="2026-10-02T04:00:00+00:00",
            created_at="2020-01-01T00:00:00+00:00",
        )
        self.assertEqual(row["issue_data"], "2026-10-02")

    def test_unparseable_timestamp_falls_back_to_now(self):
        before = datetime.now(timezone.utc) - timedelta(seconds=5)
        row = self.shift_of(received_at="not-a-timestamp")
        after = datetime.now(timezone.utc) + timedelta(seconds=5)

        stamp = datetime.fromisoformat(row["error_start_time"]).astimezone(timezone.utc)
        self.assertLessEqual(before, stamp)
        self.assertLessEqual(stamp, after)

    def test_aware_datetime_object_is_accepted(self):
        row = self.shift_of(
            received_at=datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(row["shift_type"], "day")

    def test_shift_boundary_uses_warsaw_not_utc(self):
        """A UTC-only implementation would call 17:00 UTC 'night' wrongly."""
        # 16:30 UTC == 18:30 Warsaw (summer) -> night. 15:30 UTC == 17:30 -> day.
        self.assertEqual(
            self.shift_of(received_at="2026-07-01T16:30:00+00:00")["shift_type"], "night"
        )
        self.assertEqual(
            self.shift_of(received_at="2026-07-01T15:30:00+00:00")["shift_type"], "day"
        )


class CategoryChecks(unittest.TestCase):
    def test_operator_labels_map_to_canonical_categories(self):
        cases = {
            "Robot": "robot",
            "Workstation": "workstation",
            "Charging station": "charging",
            "Charger": "charging",
            # `qr_code`, а не `qr`: так называется категория в колонке
            # `object_type` (комментарий в схеме) и так её канонизирует API.
            # Прежнее ожидание `qr` фиксировало расхождение, а не правило.
            "QR Code": "qr_code",
            "QR": "qr_code",
        }

        for label, expected in cases.items():
            with self.subTest(label=label):
                self.assertEqual(rw.intake_category(label), expected)

    def test_category_matching_is_case_insensitive(self):
        self.assertEqual(rw.intake_category("  robot "), "robot")
        self.assertEqual(rw.intake_category("CHARGING STATION"), "charging")

    def test_unknown_category_passes_through(self):
        self.assertEqual(rw.intake_category("Conveyor"), "conveyor")

    def test_category_reaches_first_and_second_column(self):
        for label, expected in (("Robot", "robot"), ("QR Code", "qr_code")):
            with self.subTest(label=label):
                data = result()
                data["answers"]["object"] = label
                with mock.patch.object(rw, "canonical_type", return_value=None):
                    row = rw.build_row(data, "GLP-C")
                self.assertEqual(row["first_column"], expected)
                self.assertEqual(row["issue_type"], expected)


class SessionHygieneChecks(unittest.TestCase):
    """
    Abandoned intake sessions must not accumulate in memory.

    Sessions live in a process-local dict. `get_session` only evicts the key it
    is asked about, so without a periodic sweep every abandoned report stayed
    until the next restart — the bot handles months of uptime.
    """

    def setUp(self):
        from equipment_intake import flow

        self.flow = flow
        flow.reset_state()

    def tearDown(self):
        self.flow.reset_state()

    def _seed(self, count=5):
        from equipment_intake.engine import Session
        from equipment_intake.tree_config import DEFAULT_TREE

        for uid in range(count):
            self.flow.put_session(-100, uid, Session(tree=DEFAULT_TREE))

    def test_sweep_removes_expired_sessions(self):
        import time

        self._seed(5)

        with self.flow._LOCK:
            for entry in self.flow._SESSIONS.values():
                entry["expires"] = time.time() - 1

        self.assertEqual(self.flow.sweep_sessions(), 5)
        self.assertEqual(len(self.flow._SESSIONS), 0)

    def test_sweep_keeps_live_sessions(self):
        self._seed(3)

        self.assertEqual(self.flow.sweep_sessions(), 0)
        self.assertEqual(len(self.flow._SESSIONS), 3)

    def test_janitor_wires_the_sweep(self):
        """
        Regression: `sweep_sessions` existed but nothing called it, so the dict
        grew for the whole process lifetime.
        """
        import threading
        import time

        import telegram_bot

        self._seed(2)

        with self.flow._LOCK:
            for entry in self.flow._SESSIONS.values():
                entry["expires"] = time.time() - 1

        original_sleep = time.sleep
        time.sleep = lambda _seconds: (_ for _ in ()).throw(SystemExit)

        try:
            with self.assertRaises(SystemExit):
                telegram_bot.images_janitor_loop(interval_seconds=1)
        finally:
            time.sleep = original_sleep

        self.assertEqual(len(self.flow._SESSIONS), 0)

    def test_janitor_sweeps_custom_text_waits(self):
        """
        Regression: `sweep_custom_texts` was defined but never called.

        A report abandoned at the "describe it yourself" step left its entry in
        `_pending_custom_text` for the whole process lifetime.
        """
        import time

        import telegram_bot

        with telegram_bot._choice_lock:
            telegram_bot._pending_custom_text[(-100, 1)] = {
                "x": 1, "expires": time.time() - 1,
            }

        original_sleep = time.sleep
        time.sleep = lambda _seconds: (_ for _ in ()).throw(SystemExit)

        try:
            with self.assertRaises(SystemExit):
                telegram_bot.images_janitor_loop(interval_seconds=1)
        finally:
            time.sleep = original_sleep
            with telegram_bot._choice_lock:
                telegram_bot._pending_custom_text.pop((-100, 1), None)

        self.assertEqual(len(telegram_bot._pending_custom_text), 0)


class TopicDiagnosticsChecks(unittest.TestCase):
    """
    Misconfiguration must be reported, not discovered as silent data loss.

    A topic configured by *name* does not resolve until the bot sees the forum
    service message that announces it. Until then every error photo is refused,
    which looks like the bot ignoring the group.
    """

    def setUp(self):
        import telegram_bot

        self.bot = telegram_bot
        self.saved = (dict(telegram_bot.TOPIC_RAW),
                      telegram_bot.TELEGRAM_TOPIC_ID,
                      telegram_bot.TELEGRAM_TOPIC_NAME,
                      dict(telegram_bot._topic_names))
        telegram_bot.TOPIC_RAW.clear()

    def tearDown(self):
        topics, topic_id, topic_name, names = self.saved
        self.bot.TOPIC_RAW.clear()
        self.bot.TOPIC_RAW.update(topics)
        self.bot.TELEGRAM_TOPIC_ID = topic_id
        self.bot.TELEGRAM_TOPIC_NAME = topic_name
        self.bot._topic_names.clear()
        self.bot._topic_names.update(names)

    def problems(self, **config):
        import telegram_bot

        for key, value in config.items():
            if key == "topic_id":
                telegram_bot.TELEGRAM_TOPIC_ID = value
            elif key == "topic_name":
                telegram_bot.TELEGRAM_TOPIC_NAME = value
            else:
                telegram_bot.TOPIC_RAW[key] = value

        return telegram_bot.topic_configuration_problems(-100)

    def test_two_warehouses_sharing_a_topic_is_reported(self):
        problems = self.problems(**{"error:GLP-C": "2", "error:SMALL-P3": "2"})

        self.assertTrue(problems)
        self.assertIn("are the same", " ".join(problems))

    def test_error_topic_colliding_with_a_shared_topic_is_reported(self):
        """
        The default warehouse's error topic comes from TELEGRAM_TOPIC_ID, so a
        collision with a shared topic is expressed through that variable.
        """
        problems = self.problems(topic_id=319, **{"status": "319"})

        self.assertTrue(problems)
        self.assertIn("shared topic", " ".join(problems))

    def test_unlearned_topic_name_is_reported(self):
        problems = self.problems(topic_id=None, topic_name="Ex GLPC")

        self.assertTrue(problems)
        self.assertIn("has not seen it yet", " ".join(problems))

    def test_per_warehouse_unlearned_name_is_reported(self):
        problems = self.problems(**{"error": "2", "error:SMALL-P3": "Ex SP3"})

        self.assertTrue(problems)
        self.assertIn("SMALL-P3", " ".join(problems))

    def test_learned_topic_name_is_not_reported(self):
        self.bot.remember_topic(-100, 42, "Ex GLPC")

        problems = self.problems(topic_id=None, topic_name="Ex GLPC")

        self.assertEqual(problems, [])

    def test_numeric_ids_produce_no_warnings(self):
        """The production shape: ids for both warehouses and shared topics."""
        problems = self.problems(**{
            "error": "2", "error:SMALL-P3": "318",
            "status": "319", "stats": "320", "service": "321",
        })

        self.assertEqual(problems, [])


class DispatchGuardChecks(unittest.TestCase):
    """Photos must only be accepted where the warehouse is knowable."""

    def setUp(self):
        import telegram_bot

        self.bot = telegram_bot
        self.original_topics = dict(telegram_bot.TOPIC_RAW)
        # Топик ошибок склада по умолчанию живёт не в `TOPIC_RAW`, а в двух
        # отдельных константах, прочитанных из окружения при импорте:
        # `TELEGRAM_TOPIC_ID` и `TELEGRAM_TOPIC_NAME`. Их тоже надо сохранить и
        # вернуть — иначе `set_topics` управляет только половиной состояния, и
        # результат начинает зависеть от `.env` оператора и от порядка запуска
        # наборов.
        self.original_topic_id = telegram_bot.TELEGRAM_TOPIC_ID
        self.original_topic_name = telegram_bot.TELEGRAM_TOPIC_NAME
        self.original_hints = set(telegram_bot._hinted_threads)
        self.allowed_chat = (
            next(iter(telegram_bot.ALLOWED_CHAT_IDS))
            if telegram_bot.ALLOWED_CHAT_IDS
            else -100123
        )

    def tearDown(self):
        self.bot.TOPIC_RAW.clear()
        self.bot.TOPIC_RAW.update(self.original_topics)
        self.bot.TELEGRAM_TOPIC_ID = self.original_topic_id
        self.bot.TELEGRAM_TOPIC_NAME = self.original_topic_name
        self.bot._hinted_threads.clear()
        self.bot._hinted_threads.update(self.original_hints)

    def set_topics(self, **topics):
        self.bot.TOPIC_RAW.clear()
        self.bot.TOPIC_RAW.update(topics)

        # Приводим «топик по умолчанию» в соответствие с заданными топиками.
        # `set_topics()` (пустой вызов) обязан означать «маршрутизация не
        # настроена вовсе» — тогда срабатывает исторический режим без фильтра.
        # Без сброса имени проверка `test_legacy_mode_still_accepts_photos`
        # проходила лишь потому, что в `.env` оператора стоял
        # `TELEGRAM_TOPIC_NAME=Exceptions` и `TELEGRAM_TOPIC_ID=2`.
        raw_error = str(topics.get("error", "")).strip()
        self.bot.TELEGRAM_TOPIC_ID = int(raw_error) if raw_error.isdigit() else None
        self.bot.TELEGRAM_TOPIC_NAME = ""

    def dispatch_photo(self, thread_id):
        self.bot._hinted_threads.clear()
        update = {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "date": 1_800_000_000,
                "chat": {"id": self.allowed_chat, "type": "supergroup"},
                "from": {"id": 5, "username": "u"},
                "photo": [{"file_id": "f", "file_unique_id": "u"}],
            },
        }
        if thread_id is not None:
            update["message"]["message_thread_id"] = thread_id

        with mock.patch.object(self.bot, "handle_photo") as photo, \
             mock.patch.object(self.bot, "_handle_wrong_topic") as wrong:
            self.bot._handle_update_inner(update)

        return photo, wrong

    def test_error_topic_is_accepted_and_mapped(self):
        self.set_topics(error="2", **{"error:SMALL-P3": "318"})

        photo, _ = self.dispatch_photo(318)
        photo.assert_called_once()

    def test_status_topic_photo_is_rejected(self):
        self.set_topics(error="2", **{"error:SMALL-P3": "318", "status": "319"})

        photo, wrong = self.dispatch_photo(319)
        photo.assert_not_called()
        wrong.assert_called_once()

    def test_stats_topic_photo_is_rejected(self):
        self.set_topics(error="2", **{"error:SMALL-P3": "318", "stats": "320"})

        photo, _ = self.dispatch_photo(320)
        photo.assert_not_called()

    def test_unknown_topic_photo_is_rejected(self):
        self.set_topics(error="2", **{"error:SMALL-P3": "318"})

        photo, _ = self.dispatch_photo(9999)
        photo.assert_not_called()

    def test_private_chat_photo_is_rejected_when_routing_is_configured(self):
        self.set_topics(error="2")

        photo, _ = self.dispatch_photo(None)
        photo.assert_not_called()

    def test_legacy_mode_still_accepts_photos(self):
        """
        With nothing configured the bot cannot know better, so it keeps working.

        A topic id is used because a message without `message_thread_id` counts
        as a general-topic message and is rejected by the pre-existing filter,
        before any warehouse logic runs.
        """
        self.set_topics()

        photo, _ = self.dispatch_photo(2)
        photo.assert_called_once()

    def test_glpc_and_sp3_map_to_different_warehouses(self):
        chat = self.allowed_chat
        self.set_topics(error="2", **{"error:SMALL-P3": "318"})

        self.assertEqual(self.bot.error_warehouse_for_thread(chat, 2), "GLP-C")
        self.assertEqual(self.bot.error_warehouse_for_thread(chat, 318), "SMALL-P3")

    def test_dispatch_never_files_a_status_topic_photo_as_glpc(self):
        """
        The regression this guard exists for: a photo in a shared topic used to
        reach the flow, and `current_warehouse()` then reported GLP-C.
        """
        self.set_topics(error="2", **{"error:SMALL-P3": "318", "status": "319"})

        with mock.patch.object(self.bot, "handle_photo") as photo, \
             mock.patch.object(self.bot, "current_warehouse", return_value="GLP-C"), \
             mock.patch.object(self.bot, "_handle_wrong_topic"):
            self.bot._handle_update_inner({
                "update_id": 1,
                "message": {
                    "message_id": 11,
                    "date": 1_800_000_000,
                    "chat": {"id": self.allowed_chat, "type": "supergroup"},
                    "from": {"id": 5, "username": "u"},
                    "message_thread_id": 319,
                    "photo": [{"file_id": "f", "file_unique_id": "u"}],
                },
            })

        photo.assert_not_called()


class LarkCardChecks(unittest.TestCase):
    """The card must carry the same identity the journal stores."""

    def fields(self, **answers_over):
        from equipment_intake import integrations

        answers = {"object": "Robot", "device_type": "K50H",
                   "device_number": "3490", "description": "broken"}
        answers.update(answers_over)

        card = integrations._card(
            {"answers": answers, "warehouse": "GLP-C", "employee": "Ivan"},
            None, True, False,
        )

        rows = {}
        for field in card["elements"][0]["fields"]:
            label, _, value = field["text"]["content"].partition("\n")
            rows[label.strip("* ")] = value
        return rows

    def test_plain_robot_keeps_the_device_number_label(self):
        rows = self.fields()

        self.assertEqual(rows["Device number"], "3490")
        self.assertNotIn("QR code", " ".join(rows))

    def test_qr_floor_card_carries_the_coordinates(self):
        """
        Regression: the card showed "Device number: —" and the X/Y/zone values
        never reached the Lark group, although the journal stored them.
        """
        rows = self.fields(object="QR Code", device_type="Floor",
                           device_number="", qr_x="162", qr_y="382", qr_zone="30")

        self.assertIn("QR code X / Y / zone", rows)
        self.assertEqual(rows["QR code X / Y / zone"], "X=162; Y=382; Zone=30")
        self.assertNotIn("—", rows["QR code X / Y / zone"])

    def test_qr_shelf_card_names_the_shelf_number(self):
        rows = self.fields(object="QR Code", device_type="Shelf",
                           device_number="", shelf_number="A-12")

        self.assertIn("Shelf number (not its QR code)", rows)
        self.assertEqual(rows["Shelf number (not its QR code)"], "A-12")

    def test_shelf_number_wins_over_device_number_on_the_card(self):
        rows = self.fields(object="QR Code", device_type="Shelf",
                           device_number="999", shelf_number="A-12")

        self.assertEqual(rows["Shelf number (not its QR code)"], "A-12")

    def test_module_is_only_shown_when_present(self):
        self.assertIn("Module", self.fields(module="Lifting"))
        self.assertNotIn("Module", self.fields())

    def test_missing_identity_shows_a_dash(self):
        rows = self.fields(device_number="")

        self.assertEqual(rows["Device number"], "—")

    def test_supabase_field_reflects_the_save_result(self):
        from equipment_intake import integrations

        card = integrations._card(
            {"answers": {"object": "Robot", "device_number": "1"}, "warehouse": "GLP-C"},
            None, False, False,
        )
        text = json.dumps(card, ensure_ascii=False)

        self.assertIn("Save failed", text)

    def test_card_reports_the_shift_journal_separately(self):
        """Сбой журнала смен виден в карточке.

        Regression: карточка говорила «Saved», когда запись в `exceptions_glpc`
        **падала**. Из этого журнала считаются отчёты смен и `/top`, то есть
        группа узнавала об ошибке, которой в журнале нет.

        **07.10.2026 владелец попросил убрать строку из карточки** («убери
        информацию о Shift Journal») — она была внутренней подробностью и
        мешала читать карточку. Убрана **только как признак успеха**: при сбое
        о ней по-прежнему сообщаем, иначе потеря данных стала бы молчаливой.
        """
        rows = self.fields_glpc(db_saved=True, glpc_saved=False)

        self.assertEqual(rows["Intake details"], "Saved")
        self.assertEqual(rows["Shift journal"], "Save failed")

    def test_card_does_not_show_the_journal_when_it_succeeded(self):
        """Успех журнала не показываем — это и просил убрать владелец.

        Строка дублировала «Intake details: Saved» и описывала внутреннюю
        подробность записи, а не то, что произошло на складе.
        """
        rows = self.fields_glpc(db_saved=True, glpc_saved=True)

        self.assertEqual(rows["Intake details"], "Saved")
        self.assertNotIn(
            "Shift journal", rows, "успешная запись в журнал не должна выводиться"
        )

    def test_card_omits_the_journal_field_when_the_caller_does_not_know(self):
        """Legacy callers that pass no journal outcome still get a valid card."""
        from equipment_intake import integrations

        card = integrations._card(
            {"answers": {"object": "Robot", "device_number": "1"}, "warehouse": "GLP-C"},
            None, True, False,
        )
        text = json.dumps(card, ensure_ascii=False)

        self.assertNotIn("Shift journal", text)

    def fields_glpc(self, db_saved, glpc_saved):
        from equipment_intake import integrations

        card = integrations._card(
            {"answers": {"object": "Robot", "device_number": "1"}, "warehouse": "GLP-C"},
            None, db_saved, False, glpc_saved,
        )

        rows = {}
        for field in card["elements"][0]["fields"]:
            label, _, value = field["text"]["content"].partition("\n")
            rows[label.strip("* ")] = value
        return rows


class IntegrationOutcomeChecks(unittest.TestCase):
    """The three destinations stay independent, including a warehouse refusal."""

    def persist(self, warehouse="GLP-C", save=None, send=None, write=None):
        from equipment_intake import integrations

        default_write = lambda *a, **k: True  # noqa: E731

        with mock.patch.object(integrations, "_save", side_effect=save or (lambda *a: (True, False, None))), \
             mock.patch.object(integrations, "_send", side_effect=send or (lambda *a: True)), \
             mock.patch.object(rw, "write_exception", side_effect=write or default_write):
            return integrations.persist_and_send(result(warehouse), "/tmp/photo-1.jpg")

    def test_all_three_succeed(self):
        outcome = self.persist()

        self.assertTrue(outcome["database_saved"])
        self.assertTrue(outcome["glpc_saved"])
        self.assertTrue(outcome["lark_delivered"])
        self.assertIsNone(outcome["glpc_error"])

    def test_invalid_warehouse_refuses_every_destination(self):
        """
        An unknown warehouse is a precondition, not an independent failure.

        Filing a detail row with `warehouse=""` and posting the card to the
        default GLP-C group would announce a fault in the wrong chat, so nothing
        is written and nothing is sent.
        """
        outcome = self.persist(warehouse="PNT-A")

        self.assertFalse(outcome["glpc_saved"])
        self.assertFalse(outcome["database_saved"])
        self.assertFalse(outcome["lark_delivered"])
        self.assertIn("PNT-A", outcome["glpc_error"])

    def test_missing_warehouse_refuses_before_any_write(self):
        from equipment_intake import integrations

        with mock.patch.object(integrations, "_save") as save, \
             mock.patch.object(integrations, "_send") as send:
            outcome = integrations.persist_and_send(result(warehouse=""), "/tmp/p.jpg")

        save.assert_not_called()
        send.assert_not_called()
        self.assertFalse(outcome["glpc_saved"])
        self.assertIsNotNone(outcome["glpc_error"])

    def test_journal_failure_does_not_block_the_others(self):
        """A genuine write error is independent, unlike a warehouse refusal."""
        outcome = self.persist(write=lambda *a, **k: False)

        self.assertFalse(outcome["glpc_saved"])
        self.assertIsNone(outcome["glpc_error"])
        self.assertTrue(outcome["database_saved"])
        self.assertTrue(outcome["lark_delivered"])

    def test_detail_save_failure_does_not_block_the_journal(self):
        outcome = self.persist(save=mock.Mock(side_effect=RuntimeError("db down")))

        self.assertFalse(outcome["database_saved"])
        self.assertTrue(outcome["glpc_saved"])
        self.assertTrue(outcome["lark_delivered"])

    def test_lark_failure_does_not_block_the_journal(self):
        outcome = self.persist(send=mock.Mock(side_effect=RuntimeError("hook down")))

        self.assertTrue(outcome["glpc_saved"])
        self.assertFalse(outcome["lark_delivered"])

    def test_journal_is_written_once_per_report(self):
        from equipment_intake import integrations

        with mock.patch.object(integrations, "_save", return_value=(True, False, None)), \
             mock.patch.object(integrations, "_send", return_value=True), \
             mock.patch.object(rw, "write_exception", return_value=True) as write:
            integrations.persist_and_send(result(), "/tmp/photo-1.jpg")

        write.assert_called_once()

    def test_warehouse_reaches_the_writer_unchanged(self):
        from equipment_intake import integrations

        seen = []

        with mock.patch.object(integrations, "_save", return_value=(True, False, None)), \
             mock.patch.object(integrations, "_send", return_value=True), \
             mock.patch.object(rw, "write_exception",
                               side_effect=lambda res, wh=None: seen.append(wh) or True):
            integrations.persist_and_send(result("GLP-C"), "/tmp/p.jpg")
            integrations.persist_and_send(result("SMALL-P3"), "/tmp/p.jpg")

        self.assertEqual(seen, ["GLP-C", "SMALL-P3"])

    def test_unexpected_writer_error_is_swallowed(self):
        """A writer crash must not take down the intake."""
        outcome = self.persist(write=mock.Mock(side_effect=ValueError("boom")))

        self.assertFalse(outcome["glpc_saved"])
        self.assertIsNone(outcome["glpc_error"])
        self.assertTrue(outcome["lark_delivered"])

    def test_uploaded_photo_reaches_the_journal(self):
        """
        The photo was already uploaded for the Lark card; the same URL is what
        the journal must store, and it must not be uploaded twice.
        """
        from equipment_intake import integrations

        seen = []

        with mock.patch.object(
                 integrations, "_save",
                 return_value=(True, False, "https://storage.invalid/p.jpg")), \
             mock.patch.object(integrations, "_send", return_value=True), \
             mock.patch.object(rw, "write_exception",
                               side_effect=lambda res, wh=None: seen.append(res) or True):
            integrations.persist_and_send(result(), "/tmp/photo-1.jpg")

        self.assertEqual(seen[0]["photo_url"], "https://storage.invalid/p.jpg")

    def test_failed_upload_leaves_the_journal_photo_empty(self):
        """`_save` returns None when the upload fails; the report is still filed."""
        from equipment_intake import integrations

        seen = []

        with mock.patch.object(integrations, "_save", return_value=(True, False, None)), \
             mock.patch.object(integrations, "_send", return_value=True), \
             mock.patch.object(rw, "write_exception",
                               side_effect=lambda res, wh=None: seen.append(res) or True):
            integrations.persist_and_send(result(), "/tmp/photo-1.jpg")

        self.assertNotIn("photo_url", seen[0])

    def test_photo_url_is_not_invented_when_the_detail_save_fails(self):
        """A failed `_save` must not fabricate a link."""
        from equipment_intake import integrations

        captured = {}

        def fake_write(res, wh=None):
            captured["photo_url"] = res.get("photo_url")
            return True

        with mock.patch.object(integrations, "_save",
                               side_effect=RuntimeError("db down")), \
             mock.patch.object(integrations, "_send", return_value=True), \
             mock.patch.object(rw, "write_exception", side_effect=fake_write):
            integrations.persist_and_send(result(), "/tmp/photo-1.jpg")

        self.assertIsNone(captured["photo_url"])


class ConfirmationTextChecks(unittest.TestCase):
    """The operator is told exactly what happened to the journal entry."""

    @classmethod
    def setUpClass(cls):
        """
        Stub the employee lookup once, not per test.

        `mock.patch.dict(sys.modules, ...)` inside a `with` block leaves the
        patch machinery in a state where the *next* `mock.patch` in the same
        process does not take effect — the second call here silently reached the
        real integrations module. Installing the stub once avoids that entirely.
        """
        cls._saved_telegram_store = sys.modules.get("telegram_store")
        sys.modules["telegram_store"] = types.SimpleNamespace(
            get_employee_name=lambda _: "U"
        )

    @classmethod
    def tearDownClass(cls):
        if cls._saved_telegram_store is not None:
            sys.modules["telegram_store"] = cls._saved_telegram_store
        else:
            sys.modules.pop("telegram_store", None)

    def confirm(self, delivery, warehouse="GLP-C"):
        from equipment_intake import flow
        from equipment_intake.engine import Session
        from equipment_intake.tree_config import DEFAULT_TREE

        session = Session(tree=DEFAULT_TREE)
        # **Сессию доводим до конца.** 07.10.2026 `_confirm` получил защиту от
        # незавершённой сессии: раньше он писал что есть, и в базу уходили
        # строки с пустым описанием и без устройства — таких нашли 11 с 04.10.
        # Тест подтверждает **завершённый** путь, поэтому его и проходим.
        session.select("robot")
        session.select("k50h")
        session.select("safety")
        session.submit_text("3780")
        session.submit_text("Lift reports an error")
        session.select("obstacle")
        session.data.update(
            {"warehouse": warehouse, "image": "/tmp/p.jpg", "message_id": 5}
        )
        captions = []
        sender = {"id": 1, "username": "u"}
        delivery_mock = mock.Mock(return_value=delivery)

        with mock.patch.object(flow, "edit_caption",
                               side_effect=lambda *a, **k: captions.append(a[2])), \
             mock.patch("equipment_intake.integrations.persist_and_send", delivery_mock):
            flow._confirm(-100, sender, session, 5)

        # **Последняя подпись, а не первая.** С 07.10.2026 `_confirm` сначала
        # показывает состояние «Saving…» (чтобы нажатие не выглядело
        # зависанием), и только потом — итоговую карточку. Первая подпись
        # теперь служебная, а пользователь видит **последнюю**.
        return captions[-1] if captions else ""

    def test_success_is_reported(self):
        text = self.confirm({
            "database_saved": True, "device_queued": False,
            "lark_delivered": True, "glpc_saved": True, "glpc_error": None,
        })

        self.assertIn("Journal entry saved", text)
        self.assertIn("Lark card sent", text)

    def test_warehouse_refusal_is_reported_with_reason(self):
        text = self.confirm({
            "database_saved": False, "device_queued": False,
            "lark_delivered": False, "glpc_saved": False,
            "glpc_error": "Warehouse is not set for this report.",
        })

        self.assertIn("Not filed in the journal", text)
        self.assertIn("Warehouse is not set", text)

    def test_journal_failure_without_a_reason_is_reported(self):
        text = self.confirm({
            "database_saved": False, "device_queued": False,
            "lark_delivered": False, "glpc_saved": False, "glpc_error": None,
        })

        self.assertIn("Journal entry not saved", text)

    def test_queued_device_is_mentioned(self):
        """Текст не только сообщает о заявке, но и **говорит, где разбирать**.

        Владелец 07.10.2026: «текст бота правдив, но не говорит, где разбирать».
        Прежний текст («Device added to the add queue.») называл факт, но не
        путь; кнопки разбора появились, и путь можно назвать точно.
        """
        text = self.confirm({
            "database_saved": True, "device_queued": True,
            "lark_delivered": True, "glpc_saved": True, "glpc_error": None,
        })

        self.assertIn("Needs review", text, "не сказано, где разбирать заявку")
        self.assertIn("Equipment", text)

    def test_missing_keys_do_not_raise(self):
        text = self.confirm({})

        self.assertIn("Journal entry not saved", text)


class TelegramConfirmationCardChecks(unittest.TestCase):
    """
    A brief card must stay in the Telegram topic.

    The employee's own photo message is deleted when the flow starts, so the
    card left behind is the only trace of the report in the chat. It stays
    short: the full path goes to the Lark group and to the journal.
    """

    def confirm(self, delivery, context=None, warehouse="GLP-C"):
        from equipment_intake import flow
        from equipment_intake.engine import Session
        from equipment_intake.tree_config import DEFAULT_TREE

        session = Session(tree=DEFAULT_TREE)
        # Сессию доводит до конца сам тест ниже: у него свои шаги, и добавленные
        # здесь дублировали бы их (`summary` не принимает выбор варианта).
        # Первый помощник сессию завершает сам — `_confirm` теперь не пишет
        # незавершённый путь.
        session.data.update(
            {
                "warehouse": warehouse,
                "image": "/tmp/p.jpg",
                "message_id": 5,
                "thread_id": 318,
            }
        )
        for step in ("robot", "a42t_c2", "lifting"):
            session.select(step)

        session.submit_text("3490")
        session.submit_text("Lift reports an error")
        # Причина — последний шаг: без него сессия не завершена, и `_confirm`
        # справедливо откажет (защита от записи неполного пути).
        session.select("obstacle")

        calls = []
        lookup = context or (lambda _: ("Smoke User", {"card_id": 60072001}))

        # `_employee_context` is the module seam for the linked employee: it
        # avoids `mock.patch.dict(sys.modules, ...)`, which is known to break the
        # *next* `mock.patch` in the same process (see ConfirmationTextChecks).
        with mock.patch.object(flow, "_employee_context", side_effect=lookup), \
             mock.patch.object(
                 flow, "edit_caption",
                 side_effect=lambda *a, **k: calls.append((a, k)),
             ), \
             mock.patch("equipment_intake.integrations.persist_and_send",
                        return_value=delivery) as send:
            flow._confirm(-100, {"id": 1, "username": "u"}, session, 5)

        return calls, send

    def saved(self):
        return {
            "database_saved": True, "device_queued": False,
            "lark_delivered": True, "glpc_saved": True, "glpc_error": None,
        }

    def test_card_says_the_error_was_saved(self):
        calls, _ = self.confirm(self.saved())
        caption = calls[-1][0][2]

        self.assertIn("Error report saved", caption)

    def test_card_is_brief_and_does_not_repeat_the_lark_card(self):
        """
        The whole selected path must not be duplicated in Telegram: only the
        status and one compact identity line are kept.
        """
        calls, _ = self.confirm(self.saved())
        caption = calls[-1][0][2]

        self.assertIn("robot · A42T C2 · 3490", caption)
        # The verbose path/description still lives in the Lark card and journal.
        self.assertNotIn("Lift reports an error", caption)
        self.assertNotIn("Check and confirm", caption)
        # Compact: a short status plus one identity line.
        self.assertLessEqual(len(caption.splitlines()), 4)

    def test_card_is_sent_into_the_same_topic(self):
        calls, _ = self.confirm(self.saved())

        # `edit_caption` receives the session, and it is the session that carries
        # `thread_id` — the card cannot drift into another topic.
        self.assertIs(calls[-1][0][3].data["thread_id"], 318)

    def test_card_reports_a_failed_save_plainly(self):
        calls, _ = self.confirm({
            "database_saved": False, "device_queued": False,
            "lark_delivered": False, "glpc_saved": False, "glpc_error": None,
        })
        caption = calls[-1][0][2]

        self.assertIn("Error report not saved", caption)
        self.assertIn("Journal entry not saved", caption)

    def test_confirmation_does_not_keep_the_whole_path_summary(self):
        calls, _ = self.confirm(self.saved())
        caption = calls[-1][0][2]

        for label in ("Equipment type", "Component", "Device number"):
            self.assertNotIn(label, caption)

    def test_card_id_is_handed_to_the_journal_writer(self):
        """`add_by` needs the card, and the lookup must not run twice."""
        calls, send = self.confirm(self.saved())
        report = send.call_args.args[0]

        self.assertEqual(report["employee_card_id"], 60072001)
        self.assertEqual(report["employee"], "Smoke User")

    def test_unlinked_employee_does_not_block_the_report(self):
        calls, send = self.confirm(
            self.saved(), context=lambda _: (None, None)
        )
        report = send.call_args.args[0]

        self.assertNotIn("employee_card_id", report)
        self.assertIn("Error report saved", calls[-1][0][2])

    def test_unavailable_database_still_files_the_report(self):
        def boom(_):
            raise RuntimeError("db down")

        calls, send = self.confirm(self.saved(), context=boom)

        send.assert_called_once()
        self.assertNotIn("employee_card_id", send.call_args.args[0])
        self.assertIn("Error report saved", calls[-1][0][2])

    def test_a_card_without_an_identity_still_renders(self):
        from equipment_intake import flow
        from equipment_intake.engine import Session
        from equipment_intake.tree_config import DEFAULT_TREE

        caption = flow.confirmation_card(Session(tree=DEFAULT_TREE), self.saved())

        self.assertIn("Error report saved", caption)


class TemplateMatchingChecks(unittest.TestCase):
    """Приём подбирает готовое описание ошибки из `issue_templates`.

    До этой правки сюда шёл сырой текст оператора: `first_column` и `issue_type`
    дублировали категорию оборудования («robot» = «robot»), `recovery_title` был
    пуст, `solving_time` равен нулю. Из-за этого строки приёма нельзя было
    группировать по типу ошибки и считать время решения. Проверено на живой
    базе 05.10.2026: так выглядели все 246 строк бота.
    """

    TEMPLATE = {
        "employee_title": "Driver component exception",
        "issue_sub_type": "Driver component exception",
        "issue_type": "Unable to drive",
        "issue_description": "In drive process robot got problem driver component exception",
        "recovery_title": "Move on QR Code robot then recovery robot",
        "solving_time": 6,
    }

    def setUp(self):
        rw.reset_templates_cache()
        self.addCleanup(rw.reset_templates_cache)

    def _with_templates(self, templates):
        """Подсовывает справочник, минуя сеть."""
        rw._templates_cache = templates
        rw._templates_checked_at = 9e9

    def _row(self, description):
        """Строка журнала для описания оператора.

        Описание обязано лежать в `answers` (там его читает `build_row`), а не
        на верхнем уровне `result` — `row_for(description=…)` положил бы его не
        туда, и тест «проходил» бы, ничего не проверяя.
        """
        return row_for(answers=answers(description=description))

    def test_columns_are_filled_from_the_matched_template(self):
        self._with_templates([self.TEMPLATE])

        row = self._row("driver component exception")

        self.assertEqual(row["first_column"], "Driver component exception")
        self.assertEqual(row["issue_type"], "Unable to drive")
        self.assertEqual(row["second_column"], "Driver component exception")
        self.assertEqual(row["recovery_title"], "Move on QR Code robot then recovery robot")
        self.assertEqual(row["solving_time"], 6)

    def test_end_time_is_start_plus_solving_time(self):
        """Конвенция журнала: `error_end_time = error_start_time + solving_time`.

        Проверена на живой базе: сходится у 23 384 из 23 400 строк, где
        `solving_time > 0`. Без шаблона `solving_time = 0`, и конец равен началу.
        """
        from datetime import datetime, timedelta

        self._with_templates([self.TEMPLATE])

        row = self._row("driver component exception")

        start = datetime.fromisoformat(row["error_start_time"])
        end = datetime.fromisoformat(row["error_end_time"])

        self.assertEqual(end - start, timedelta(minutes=6))

    def test_no_match_keeps_the_previous_behaviour(self):
        """Отказ — правильный ответ, а не сбой.

        `'unable to rotate'` — реальный текст оператора. Уверенного совпадения
        для него нет, и колонки обязаны остаться прежними: категория и модуль.
        Выдуманный шаблон отнёс бы ошибку не к тому типу.
        """
        self._with_templates([self.TEMPLATE])

        row = self._row("totally unrelated wording xyz")

        self.assertEqual(row["first_column"], "robot")
        self.assertEqual(row["issue_type"], "robot")
        self.assertEqual(row["second_column"], "Lifting")   # модуль из answers()
        self.assertIsNone(row["recovery_title"])
        self.assertEqual(row["solving_time"], 0)

    def test_no_match_keeps_end_equal_to_start(self):
        from datetime import datetime

        self._with_templates([self.TEMPLATE])

        row = self._row("totally unrelated wording xyz")

        self.assertEqual(row["error_end_time"], row["error_start_time"])

    def test_database_failure_still_files_the_report(self):
        """Сбой справочника не теряет отчёт.

        `rest_get` вернёт `None` — это нормальное состояние (база недоступна),
        а не повод не записать ошибку. Колонки остаются прежними.
        """
        with mock.patch.object(rw, "load_templates", return_value=[]):
            row = self._row("driver component exception")

        self.assertEqual(row["issue_type"], "robot")
        self.assertEqual(row["solving_time"], 0)
        # Самое важное: отчёт состоялся и сырой текст на месте.
        self.assertIn("driver component exception", row["issue_description"])

    def test_raw_operator_text_is_never_lost(self):
        """Подбор заполняет колонки, но сырой текст остаётся в журнале.

        Это единственное место, где видно, что именно написал человек; то же
        описание дублируется в `telegram_equipment_reports`.
        """
        self._with_templates([self.TEMPLATE])

        row = self._row("driver component exception")

        self.assertIn("Reported error: driver component exception", row["issue_description"])

    def test_solving_time_is_coerced_to_int(self):
        """Справочник может вернуть строку или мусор — запись не должна падать."""
        self._with_templates([dict(self.TEMPLATE, solving_time="8")])
        self.assertEqual(self._row("driver component exception")["solving_time"], 8)

        self._with_templates([dict(self.TEMPLATE, solving_time=None)])
        self.assertEqual(self._row("driver component exception")["solving_time"], 0)

        self._with_templates([dict(self.TEMPLATE, solving_time="abc")])
        self.assertEqual(self._row("driver component exception")["solving_time"], 0)

    def test_template_without_sub_type_does_not_leave_first_column_empty(self):
        """У шаблона id 122 `issue_sub_type` пуст — колонка не должна опустеть."""
        self._with_templates([{
            "employee_title": "Security module failure",
            "issue_sub_type": "",
            "issue_type": "Unable to drive",
            "solving_time": 6,
        }])

        row = self._row("security module failure")

        self.assertEqual(row["first_column"], "Security module failure")
        self.assertEqual(row["issue_type"], "Unable to drive")


class ZoneChecks(unittest.TestCase):
    """Зона склада: у SMALL-P3 выводится из устройства, у остальных — C2.

    До этой правки `issue_warehouse` была **константой `C2` для всех складов**,
    и у SMALL-P3 стояла зона GLP-C (проверено: 1 253 строки). Метрика «ошибки по
    зонам» складывала два склада в одну корзину.

    Зона берётся из `sub_warehouse` робота — проверено на живой базе, что это
    поле заполнено **только** у SMALL-P3 (`D`: 40 роботов, `E`: 80).
    """

    def setUp(self):
        rw.reset_templates_cache()
        self.addCleanup(rw.reset_templates_cache)
        rw._templates_cache = []
        rw._templates_checked_at = 9e9

    def _zone(self, warehouse, number):
        with mock.patch.object(rw, "canonical_type", return_value=None):
            row = row_for(
                warehouse,
                answers=answers(device_number=str(number)),
            )
        return row["issue_warehouse"]

    def test_glp_c_keeps_the_default_zone(self):
        """GLP-C: разбиения на зоны нет, значение по умолчанию."""
        self.assertEqual(self._zone("GLP-C", "3452"), "C2")

    def test_warehouse_without_zones_keeps_the_default(self):
        """PNT-A и прочие склады без зон — тоже значение по умолчанию."""
        for warehouse in ("PNT-A", "P3-DC-1", "P3-DC-3"):
            with self.subTest(warehouse=warehouse):
                self.assertEqual(self._zone(warehouse, "100"), "C2")

    def test_small_p3_zone_comes_from_the_robot(self):
        """SMALL-P3: зона из `sub_warehouse` устройства, а не константа."""
        with mock.patch.object(rw, "device_zone", return_value="E"):
            self.assertEqual(self._zone("SMALL-P3", "98"), "E")

        with mock.patch.object(rw, "device_zone", return_value="D"):
            self.assertEqual(self._zone("SMALL-P3", "6"), "D")

    def test_unknown_device_falls_back_to_the_default(self):
        """Устройство не найдено — зона неизвестна, но отчёт не теряется.

        Подставлять чужую зону нельзя, поэтому остаётся значение по умолчанию,
        а не догадка.
        """
        with mock.patch.object(rw, "device_zone", return_value=None):
            self.assertEqual(self._zone("SMALL-P3", "999999"), "C2")

    def test_non_numeric_code_gives_no_zone(self):
        """Составной номер (`H108/1834`) в справочнике роботов не лежит."""
        self.assertIsNone(rw.device_zone("SMALL-P3", "H108/1834"))
        self.assertIsNone(rw.device_zone("SMALL-P3", ""))

    def test_warehouse_without_zones_does_not_query(self):
        """Для GLP-C запрос к справочнику не делается вовсе."""
        with mock.patch.object(rw, "rest_get", create=True) as get:
            # Импорт внутри функции — подменяем через sys.modules
            import sendToDataBase

            with mock.patch.object(sendToDataBase, "rest_get") as rest_get:
                self.assertIsNone(rw.device_zone("GLP-C", "3452"))
                rest_get.assert_not_called()

    def test_database_failure_does_not_lose_the_report(self):
        """Сбой справочника роботов — отчёт всё равно записывается."""
        import sendToDataBase

        with mock.patch.object(sendToDataBase, "rest_get", return_value=None):
            self.assertIsNone(rw.device_zone("SMALL-P3", "6"))

        with mock.patch.object(rw, "device_zone", return_value=None):
            row = self._zone("SMALL-P3", "6")
        self.assertEqual(row, "C2")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class IncompleteSessionIsNotWritten(unittest.TestCase):
    """Незавершённую сессию не записываем в базу.

    **Дефект, найденный 07.10.2026.** `_confirm` брал `session.result()` и сразу
    писал строку, **не проверяя полноту**. Если подтверждение приходило до конца
    пути (кнопка Confirm висит на финальном экране, но callback можно отправить
    и раньше — повторным нажатием из старого сообщения), в базу уходила запись с
    **пустым описанием и без устройства**.

    Проверено на живой базе: **11 таких строк** с 04.10, у всех
    `issue_description = 'Reported error: '` и `solving_time = 0`. В отчёте они
    выглядели как ошибки без типа и устройства и портили топ устройств и среднее
    время.
    """

    def test_incomplete_session_is_refused(self):
        from equipment_intake import flow
        from equipment_intake.engine import Session
        from equipment_intake.tree_config import DEFAULT_TREE

        session = Session(tree=DEFAULT_TREE)
        # Путь не начат: ответов нет вовсе.
        session.data.update({"warehouse": "GLP-C", "image": "/tmp/x.jpg", "message_id": 5})
        sent = []

        with mock.patch.object(flow, "_bot") as bot, \
             mock.patch.object(flow, "edit_caption") as edit, \
             mock.patch("equipment_intake.integrations.persist_and_send") as save:
            bot.return_value._send.side_effect = lambda *a, **k: sent.append(a)
            flow._confirm(-100, {"id": 1, "username": "u"}, session, 5)

        self.assertFalse(save.called, "незавершённая сессия не должна писаться")
        self.assertFalse(edit.called, "карточк�� не должна рисоваться")
        self.assertTrue(sent, "человеку нужно сказать, чего не хватает")

    def test_refusal_names_the_unfinished_step(self):
        """Отказ называет шаг, который не закончен, — иначе непонятно, что делать."""
        from equipment_intake import flow
        from equipment_intake.engine import Session
        from equipment_intake.tree_config import DEFAULT_TREE

        session = Session(tree=DEFAULT_TREE)
        session.select("robot")
        session.select("k50h")
        session.data.update({"warehouse": "GLP-C", "image": "/tmp/x.jpg", "message_id": 5})
        sent = []

        with mock.patch.object(flow, "_bot") as bot, \
             mock.patch.object(flow, "edit_caption"), \
             mock.patch("equipment_intake.integrations.persist_and_send"):
            bot.return_value._send.side_effect = lambda *a, **k: sent.append(a)
            flow._confirm(-100, {"id": 1, "username": "u"}, session, 5)

        self.assertTrue(sent)
        self.assertIn("Which robot module?", sent[0][1])

    def test_completed_session_is_written(self):
        """Завершённый путь пишется — защита не ломает обычную работу."""
        from equipment_intake import flow
        from equipment_intake.engine import Session
        from equipment_intake.tree_config import DEFAULT_TREE

        session = Session(tree=DEFAULT_TREE)
        for step in ("robot", "k50h", "safety"):
            session.select(step)
        session.submit_text("3780")
        session.submit_text("Lift reports an error")
        session.select("obstacle")
        session.data.update({"warehouse": "GLP-C", "image": "/tmp/x.jpg", "message_id": 5})

        with mock.patch.object(flow, "_bot"), \
             mock.patch.object(flow, "edit_caption"), \
             mock.patch.object(flow, "_employee_context", return_value=("U", None)), \
             mock.patch("equipment_intake.integrations.persist_and_send",
                        return_value={"database_saved": True, "device_queued": False,
                                      "lark_delivered": True, "glpc_saved": True,
                                      "glpc_error": None}) as save:
            flow._confirm(-100, {"id": 1, "username": "u"}, session, 5)

        self.assertTrue(save.called, "завершённый путь должен записываться")
        report = save.call_args.args[0]
        self.assertEqual(report["answers"]["device_number"], "3780")
        self.assertEqual(report["answers"]["cause"], "Obstacle on the path")


class StatsRobotTopIsLonger(unittest.TestCase):
    """`/stats` показывает больше роботов, чем автоматический отчёт.

    **Владелец 07.10.2026:** «на команду /stats сделай больше роботов в топе
    10-20».

    **Почему лимитов два, а не один.** Автоматический отчёт приходит в группу по
    расписанию (06:00 и 18:00) и читается на ходу — длинный список роботов
    мешал бы видеть главное. `/stats` человек запрашивает **специально**, чтобы
    разобраться, и «+N more» там бесполезно: роботов на складе сотни.
    """

    def _metrics(self, robots: int) -> dict:
        return {
            "total": robots,
            "types": {"Structural damage": robots},
            "employees": {"Тест": robots},
            # Ключи — номера роботов, значения — число ошибок (по убыванию).
            "robots": {str(1000 + i): robots - i for i in range(robots)},
            "maintenance": {},
            "downtime_minutes": 0,
            # `previous` и `delta` повторяют форму из `shift_report.shift_metrics`:
            # `delta` — **число** (разница итогов), а не словарь, иначе
            # `format_delta` падает на сравнении.
            "previous": {"total": 0, "shift": None, "date": None},
            "delta": 0,
            "warehouse": "GLP-C",
        }

    def test_stats_top_holds_more_than_the_default(self) -> None:
        import shift_report

        text = shift_report.build_shift_summary(
            "2026-10-06", "day", self._metrics(40), "GLP-C",
            robot_top=shift_report.STATS_ROBOT_TOP,
        )
        robots_line = next(
            line for line in text.splitlines() if line.startswith("🤖 Top robots:")
        )

        # Каждый показанный робот — это «номер (число)».
        shown = robots_line.count("(") - robots_line.count("more")
        self.assertGreaterEqual(shown, 10, f"в /stats слишком мало роботов: {shown}")
        self.assertLessEqual(shown, 20, f"в /stats слишком много роботов: {shown}")

    def test_default_report_stays_short(self) -> None:
        """Автоматический отчёт остаётся коротким — его читают на ходу."""
        import shift_report

        text = shift_report.build_shift_summary(
            "2026-10-06", "day", self._metrics(40), "GLP-C"
        )
        robots_line = next(
            line for line in text.splitlines() if line.startswith("🤖 Top robots:")
        )
        shown = robots_line.count("(") - robots_line.count("more")

        self.assertEqual(shown, shift_report.REPORT_TOP)

    def test_stats_command_actually_passes_its_limit(self) -> None:
        """Команда `/stats` **реально передаёт** свой лимит.

        **Почему этот тест отдельный.** Первые три проверяли
        `build_shift_summary` напрямую — и **не падали**, когда я сломал
        проводку в `_stats_text` (подменил лимит на короткий). Тест, который
        сам зовёт функцию и сам передаёт параметр, **не проверяет**, что
        приложение передаёт его так же.

        Здесь проверяется настоящий путь `_stats_text` → `build_shift_summary`.
        """
        import telegram_bot
        import shift_report

        captured = {}

        def fake_summary(shift_date, shift_name, metrics=None, warehouse=None, robot_top=None):
            captured["robot_top"] = robot_top
            return "ok"

        with mock.patch.object(shift_report, "shift_metrics", return_value={}), \
             mock.patch.object(telegram_bot, "build_shift_summary", fake_summary):
            telegram_bot._stats_text("2026-10-06", "day", "GLP-C")

        self.assertEqual(
            captured.get("robot_top"),
            shift_report.STATS_ROBOT_TOP,
            "команда /stats обязана передавать СВОЙ лимит, а не общий",
        )

    def test_longer_top_keeps_the_remaining_count(self) -> None:
        """Остаток всё равно называется: иначе непонятно, что список неполон."""
        import shift_report

        text = shift_report.build_shift_summary(
            "2026-10-06", "day", self._metrics(40), "GLP-C",
            robot_top=shift_report.STATS_ROBOT_TOP,
        )
        robots_line = next(
            line for line in text.splitlines() if line.startswith("🤖 Top robots:")
        )

        self.assertIn("more", robots_line)


class LarkCardTextIsCorrect(unittest.TestCase):
    """Карточка в Lark: ярлык категории и экранирование разметки.

    **Дефект 1 (07.10.2026):** карточка показывала сырое `qr_code`.
    `intake_category` канонизирует `qr` → `qr_code` (так записано в базе), а
    словарь ярлыков в `_card` знал только ключ `qr` — он **не совпадал**.

    **Дефект 2:** значения карточки — введённый человеком текст, а в разметке
    Lark символы `*`, `_`, `~`, `` ` `` управляют оформлением. Номер устр��йства
    вида `**важное**` ломал структуру полей.
    """

    def _fields(self, answers: dict) -> dict:
        from equipment_intake import integrations

        card = integrations._card(
            {"answers": answers, "warehouse": "GLP-C", "employee": "Тест"},
            None, True, False, True,
        )
        rows = {}
        for field in card["elements"][0]["fields"]:
            label, _, value = field["text"]["content"].partition("\n")
            rows[label.strip("* ")] = value
        return rows

    def test_qr_report_shows_a_readable_label(self) -> None:
        """QR-отчёт показывает «QR code», а не сырое `qr_code`."""
        rows = self._fields({"object": "qr_code", "device_number": "30"})

        self.assertIn("QR code", rows["Equipment"])
        self.assertNotIn("qr_code", rows["Equipment"])

    def test_other_categories_are_readable_too(self) -> None:
        for key, expected in (
            ("robot", "Robot"),
            ("workstation", "Workstation"),
            ("charging", "Charging station"),
        ):
            rows = self._fields({"object": key, "device_number": "1"})
            self.assertIn(expected, rows["Equipment"], f"категория {key}")

    def test_markdown_characters_are_escaped(self) -> None:
        """Спецсимволы в значении не ломают разметку карточки."""
        rows = self._fields({"object": "robot", "device_number": "**жирный**"})

        self.assertEqual(rows["Device number"], "\\*\\*жирный\\*\\*")
        # Ключевое: без экранирования Lark сделал бы текст жирным и съел
        # звёздочки — структура полей разъехалась бы.
        self.assertNotEqual(rows["Device number"], "**жирный**")

    def test_ordinary_numbers_are_untouched(self) -> None:
        """Экранирование не портит обычные значения.

        Номера бывают составными (`H108/1834`) и с координатами
        (`X=123.45; Y=453.35`) — они обязаны отображаться **как есть**.
        """
        for value in ("3780", "H108/1834", "X=123.45; Y=453.35; Zone=30"):
            rows = self._fields({"object": "qr_code", "device_number": value})
            self.assertEqual(rows["Device number"], value)

    def test_qr_code_without_a_number_still_renders(self) -> None:
        """У QR-отчёта может не быть номера — карточка обязана собраться."""
        rows = self._fields({"object": "qr_code"})

        self.assertIn("QR code", rows["Equipment"])
        self.assertEqual(rows["Intake details"], "Saved")


class ConfirmShowsProgress(unittest.TestCase):
    """Подтверждение показывает, что работа идёт, а не «зависает».

    **Владелец 07.10.2026:** «сделай когда нажимаешь Submit на бота чтобы было
    видно что грузится, а то оно как будто зависает».

    **Причина была в порядке обратной связи.** `handle_callback` гасил «часики»
    на входе и молчал дальше: для мгновенного выбора этого хватало, но
    подтверждение делает три сетевые операции (запись — 10 с, фото — 30 с,
    карточка в Lark — 15 с), и всё это время экран не менялся.
    """

    def _session(self):
        from equipment_intake.engine import Session
        from equipment_intake.tree_config import DEFAULT_TREE

        session = Session(tree=DEFAULT_TREE)
        for step in ("robot", "k50h", "safety"):
            session.select(step)
        session.submit_text("3780")
        session.submit_text("Lift reports an error")
        session.select("obstacle")
        session.data.update(
            {
                "warehouse": "GLP-C",
                "image": "/tmp/x.jpg",
                "message_id": 5,
                "menu_message_id": 5,
                "thread_id": 318,
            }
        )
        return session

    def test_progress_is_shown_before_the_work(self):
        """Состояние загрузки показывается **до** сетевой работы.

        Порядок проверяется по подписям: сначала «Saving…», потом итоговая
        карточка. Если поменять местами, первые секунды ожидания снова будут
        выглядеть зависанием.
        """
        from equipment_intake import flow

        captions = []

        with mock.patch.object(flow, "edit_caption", side_effect=lambda *a, **k: captions.append(a[2])), \
             mock.patch.object(flow, "_employee_context", return_value=("Тест", None)), \
             mock.patch.object(flow, "_start_typing_keepalive"), \
             mock.patch("equipment_intake.integrations.persist_and_send",
                        return_value={"database_saved": True, "device_queued": False,
                                      "lark_delivered": True, "glpc_saved": True,
                                      "glpc_error": None}):
            flow._confirm(-100, {"id": 1, "username": "u"}, self._session(), 5)

        self.assertGreaterEqual(len(captions), 2, "ожидались прогресс и итог")
        self.assertIn("Saving the report", captions[0], "прогресс должен идти первым")
        self.assertIn("Journal entry saved", captions[-1], "итог — последним")

    def test_progress_removes_the_buttons(self):
        """На время сохранения кнопки убираются.

        Иначе нетерпеливый человек жмёт «Confirm» повторно, и отчёт уходит
        **дважды** — а это уже дубликаты в журнале.
        """
        from equipment_intake import flow

        seen = []

        def capture(chat_id, message_id, caption, session=None, reply_markup=None):
            seen.append(reply_markup)

        with mock.patch.object(flow, "edit_caption", side_effect=capture), \
             mock.patch.object(flow, "_employee_context", return_value=("Тест", None)), \
             mock.patch.object(flow, "_start_typing_keepalive"), \
             mock.patch("equipment_intake.integrations.persist_and_send",
                        return_value={"database_saved": True, "device_queued": False,
                                      "lark_delivered": True, "glpc_saved": True,
                                      "glpc_error": None}):
            flow._confirm(-100, {"id": 1, "username": "u"}, self._session(), 5)

        self.assertEqual(
            seen[0], {"inline_keyboard": []}, "на время работы кнопок быть не должно"
        )

    def test_refusal_does_not_show_progress(self):
        """Отказ (незавершённый путь) — **без** «Saving…».

        Показывать прогресс, а следом ошибку значило бы давать два
        противоречащих сигнала подряд.
        """
        from equipment_intake import flow
        from equipment_intake.engine import Session
        from equipment_intake.tree_config import DEFAULT_TREE

        session = Session(tree=DEFAULT_TREE)  # путь не начат
        captions = []

        with mock.patch.object(flow, "edit_caption", side_effect=lambda *a, **k: captions.append(a[2])), \
             mock.patch.object(flow, "_start_typing_keepalive") as typing, \
             mock.patch.object(flow, "_bot"):
            flow._confirm(-100, {"id": 1, "username": "u"}, session, 5)

        self.assertFalse(typing.called, "при отказе индикатор не нужен")
        for caption in captions:
            self.assertNotIn("Saving the report", caption)

    def test_typing_is_sent_to_the_report_topic(self):
        """«Печатает…» уходит в топик отчёта, а не в общий чат.

        Индикатор в чужом топике — это ответ там, где бот молчать обязан.

        **Проверяется без гонки с потоком.** Первая версия теста ждала 0.2 с и
        падала через раз: поток демон, и «успел ли он» зависело от загрузки
        машины. Здесь поток **не запускается** — вместо него вызывается тело
        цикла напрямую, поэтому проверка детерминирована.
        """
        from equipment_intake import flow

        session = self._session()
        calls = []

        # Останавливаю цикл после первой итерации: `sleep` бросает — так тело
        # выполняется ровно один раз и результат не зависит от планировщика.
        class _Stop(Exception):
            pass

        def stop(_seconds):
            raise _Stop

        with mock.patch.object(flow.tg, "send_chat_action",
                               side_effect=lambda *a, **k: calls.append(k)), \
             mock.patch.object(flow.time, "sleep", side_effect=stop), \
             mock.patch.object(flow.threading, "Thread") as thread:
            # Сессия должна быть **живой**: индикатор останавливается, когда
            # она исчезает (это признак конца записи).
            flow.put_session(-100, 1, session)
            flow._start_typing_keepalive(-100, session, user_id=1)
            target = thread.call_args.kwargs.get("target") or thread.call_args.args[0]
            try:
                target()
            except _Stop:
                pass
            flow.drop_session(-100, 1)

        self.assertTrue(calls, "индикатор должен отправляться")
        self.assertEqual(calls[0].get("message_thread_id"), 318)

    def test_real_indicator_sends_when_session_is_alive(self):
        """Настоящий индикатор отправляет «печатает…», пока сессия жива.

        **Проверяется без мока самой функции.** Первая версия цикла проверяла
        `session.is_completed`, а в `_confirm` сессия **уже завершена** —
        поэтому индикатор не отправлялся **ни разу**. Здесь вызывается
        настоящий `_start_typing_keepalive`: сессия лежит в памяти, `sleep`
        подменён так, чтобы цикл сделал ровно один оборот и вышел.
        """
        from equipment_intake import flow

        session = self._session()
        sent = []

        class _Stop(Exception):
            pass

        def stop(_seconds):
            # Первый оборот уже сделан — выходим из цикла.
            raise _Stop

        flow.put_session(-100, 1, session)

        try:
            with mock.patch.object(flow.tg, "send_chat_action",
                                   side_effect=lambda *a, **k: sent.append(k)), \
                 mock.patch.object(flow.time, "sleep", side_effect=stop), \
                 mock.patch.object(flow.threading, "Thread") as thread:
                flow._start_typing_keepalive(-100, session, user_id=1)
                target = thread.call_args.kwargs.get("target") or thread.call_args.args[0]
                try:
                    target()
                except _Stop:
                    pass
        finally:
            flow.drop_session(-100, 1)

        self.assertTrue(sent, "индикатор не отправился при живой сессии")
        self.assertEqual(sent[0].get("message_thread_id"), 318)

    def test_indicator_stops_when_the_session_is_gone(self):
        """Сессия исчезла (запись закончена) — индикатор молчит.

        `_confirm` в конце вызывает `drop_session`, поэтому исчезновение сессии
        и есть признак конца работы. Если проверять `is_completed`, условие
        сработает **сразу** и индикатор не покажется вообще — этот тест ловит
        такую подмену.
        """
        from equipment_intake import flow

        session = self._session()
        sent = []
        # Сессию **не** кладу в память: работа как будто уже закончена.
        flow.drop_session(-100, 1)

        with mock.patch.object(flow.tg, "send_chat_action",
                               side_effect=lambda *a, **k: sent.append(k)), \
             mock.patch.object(flow.threading, "Thread") as thread:
            flow._start_typing_keepalive(-100, session, user_id=1)
            target = thread.call_args.kwargs.get("target") or thread.call_args.args[0]
            target()

        self.assertEqual(sent, [], "при завершённой работе индикатор не нужен")
