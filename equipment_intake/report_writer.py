"""
Warehouse-aware write of a completed intake report to `exceptions_glpc`.

Why this module exists
----------------------
The legacy text flow (`sendToDataBase.send_to_data_base`) writes the journal.
The guided photo flow used to stop at `telegram_equipment_reports`. The
integration plan unifies both, so this writer feeds `exceptions_glpc` from the
guided flow.

Two rules are enforced here on purpose:

1. **The warehouse comes from the Telegram topic, never from a default.**
   `DEFAULT_WAREHOUSE` is GLP-C, so any code path that falls back to it would
   silently file SMALL-P3 faults under GLP-C. When the warehouse is missing or
   is not a configured one, `write_exception` raises `WarehouseError` and the
   caller tells the operator instead of guessing.

2. **`device_type` holds the canonical fleet type**, not the operator's button
   label. The column already migrated to canonical names (`RT_KUBOT`,
   `RT_KUBOT_E2`, `RT_KUBOT_MINI_HAIFLEX`), so the label the operator tapped is
   preserved in the description and the `answers` table instead.

Category matters when resolving the type: `warehouse + equipment_code` is not
unique. On SMALL-P3 the code `13` is both the robot `RT_KUBOT` and a charging
station, and 29 such collisions exist. The lookup therefore always filters by
category as well.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from shift import WARSAW_TZ, get_current_shift
from warehouses import WAREHOUSES

logger = logging.getLogger(__name__)

GLPC_TABLE = "exceptions_glpc"

# Legacy constant kept for parity with every existing row: the API intake also
# writes "C2" for both warehouses, and nothing in the bot reads this column.
# Changing it is a separate decision (integration plan, question 8.2).
ISSUE_WAREHOUSE = "C2"

# Categories that can be resolved against the canonical equipment inventory.
INVENTORY_CATEGORIES = ("robot", "workstation", "charging")


class WarehouseError(ValueError):
    """The report cannot be filed because its warehouse is unknown."""


def configured_warehouses() -> Tuple[str, ...]:
    return tuple(WAREHOUSES.values())


def validate_warehouse(warehouse: Optional[str]) -> str:
    """
    Return the warehouse title or raise `WarehouseError`.

    Empty input is rejected: falling back to `DEFAULT_WAREHOUSE` here is exactly
    the bug this guard exists to prevent.
    """
    title = str(warehouse or "").strip()

    if not title:
        raise WarehouseError(
            "Warehouse is not set for this report. Send the photo in the "
            "warehouse's own error topic."
        )

    if title not in configured_warehouses():
        raise WarehouseError(
            f"Warehouse '{title}' is not configured. Configured: "
            f"{', '.join(configured_warehouses())}."
        )

    return title


def intake_category(value: str) -> str:
    """Operator's answer -> canonical category key."""
    return {
        "robot": "robot",
        "workstation": "workstation",
        "charging station": "charging",
        "charger": "charging",
        "qr code": "qr",
        "qr": "qr",
    }.get(str(value or "").strip().casefold(), str(value or "").strip().casefold())


def identifier(answers: Dict[str, Any]) -> str:
    """
    Equipment identity for this report.

    The guided flow may carry the richer QR shape (shelf number, or X/Y/zone)
    once the test-bot flow lands; the current tree only collects a device
    number. Both shapes are accepted so the writer does not need revisiting.
    """
    shelf = str(answers.get("shelf_number") or "").strip()

    if shelf:
        return shelf

    x = str(answers.get("qr_x") or "").strip()

    if x:
        y = str(answers.get("qr_y") or "").strip()
        zone = str(answers.get("qr_zone") or "").strip()

        return f"X={x}; Y={y}; Zone={zone}"

    return str(answers.get("device_number") or "").strip()


def canonical_type(warehouse: str, category: str, code: str) -> Optional[str]:
    """
    Canonical fleet type for a device code, or None when it cannot be resolved.

    None is not an error: an unknown or brand-new device still has to be filed,
    with the operator's label kept in the description.
    """
    if not code or category not in INVENTORY_CATEGORIES:
        return None

    from sendToDataBase import rest_get

    rows = rest_get(
        "equipment",
        {
            "select": "equipment_type_id",
            "warehouse": f"eq.{warehouse}",
            "category": f"eq.{category}",
            "equipment_code": f"eq.{code}",
            "limit": "1",
        },
    )

    if not rows:
        if rows is None:
            logger.warning(
                "Canonical type lookup failed for %s/%s/%s", warehouse, category, code
            )

        return None

    type_id = rows[0].get("equipment_type_id")

    if type_id is None:
        return None

    types = rest_get("equipment_types", {"select": "type", "id": f"eq.{type_id}", "limit": "1"})

    if not types:
        return None

    return str(types[0].get("type") or "").strip() or None


def _received_at(result: Dict[str, Any]) -> datetime:
    """
    When the report arrived, as an aware datetime.

    `flow._confirm` stamps `created_at` with `time.strftime` — a *naive* local
    timestamp of the host. Reading it as UTC would shift shift boundaries on a
    non-UTC host, so the value is interpreted through the host's local zone
    (which is what `time.strftime` encodes) and then made aware. An unusable or
    missing value falls back to "now", never to a silent wrong shift.
    """
    raw = result.get("received_at") or result.get("created_at")

    if isinstance(raw, datetime):
        received = raw
    elif raw:
        try:
            received = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)
    else:
        return datetime.now(timezone.utc)

    if received.tzinfo is None:
        # `time.strftime` produced local wall-clock time: attach the local zone.
        received = received.astimezone()

    return received


def _robot_number(code: str) -> Optional[int]:
    """
    `error_robot` is bigint; non-numeric identities live in the description.

    Only ASCII digits count. `str.isdecimal()` also accepts non-ASCII digits
    (e.g. Arabic-Indic '١٢٣'), which would silently turn a mis-typed identifier
    into a different robot number — so the value is validated explicitly.
    """
    text = str(code or "")

    if not text or not text.isascii() or not text.isdecimal():
        return None

    value = int(text)

    return value if value <= 9_223_372_036_854_775_807 else None


def _uniq_key(result: Dict[str, Any], report_id: str, user_id) -> str:
    """
    Stable identifier used by the unique index to make writes idempotent.

    Telegram message coordinates are preferred: one message is exactly one
    report, and the pair is stable across a redelivered update. Falling back to
    the photo filename is unsafe on its own — a report without a saved photo used
    the device number instead, so two different faults on the same robot by the
    same sender collapsed into one key and the second was dropped as a duplicate.
    """
    chat_id = result.get("chat_id")
    message_id = result.get("message_id")

    if chat_id is not None and message_id is not None:
        return f"telegram-photo-bot:{chat_id}:{message_id}"

    if chat_id is not None:
        return f"telegram-photo-bot:{chat_id}:{report_id}"

    return f"telegram-photo-bot:{user_id}:{report_id}"


def _report_id(result: Dict[str, Any], answers: Dict[str, Any]) -> str:
    """Name used for logging and as a weak fallback identity."""
    image = str(result.get("image") or "").strip()

    if image:
        name = Path(image).name.strip()

        if name:
            return name

    return identifier(answers) or "report"


def build_row(result: Dict[str, Any], warehouse: str) -> Dict[str, Any]:
    """Map an intake result onto the legacy journal columns."""
    answers = result.get("answers") or {}
    category = intake_category(answers.get("object"))
    code = identifier(answers)
    label = str(answers.get("device_type") or "").strip()
    module = str(answers.get("module") or "").strip()
    resolved = canonical_type(warehouse, category, code)

    # The operator's label is never lost: it goes to the description whenever
    # the column carries the canonical type instead.
    device_type = resolved or label

    received = _received_at(result)

    # Shift boundaries are defined in Europe/Warsaw, so the timestamp is
    # converted to that zone explicitly. `astimezone()` without an argument
    # would use the host's local time (UTC on Railway) and push night reports
    # into the wrong shift.
    shift_date, shift_name = get_current_shift(received.astimezone(WARSAW_TZ))
    report_id = _report_id(result, answers)
    user_id = result.get("user_id")

    details = [f"Reported error: {str(answers.get('description') or '').strip()}"]

    if code:
        details.append(f"Identifier: {code}")

    if label and label != device_type:
        details.append(f"Reported type: {label}")

    if module:
        details.append(f"Module: {module}")

    return {
        "error_robot": _robot_number(code),
        "add_by": None,
        "device_type": device_type,
        "employee": result.get("employee") or result.get("username") or f"Telegram {user_id}",
        "error_end_time": None,
        "error_start_time": received.isoformat(),
        "first_column": category,
        "issue_description": "\n".join(details),
        "issue_type": category,
        "recovery_title": None,
        "second_column": module or device_type,
        "solving_time": 0,
        "uniq_key": _uniq_key(result, report_id, user_id),
        "shift_type": shift_name,
        "warehouse": warehouse,
        "issue_data": shift_date,
        "issue_warehouse": ISSUE_WAREHOUSE,
    }


def write_exception(result: Dict[str, Any], warehouse: Optional[str] = None) -> bool:
    """
    File one completed report in `exceptions_glpc`.

    Returns True when the row exists after the call (a repeated delivery counts
    as success — `uniq_key` makes the insert idempotent).

    Raises `WarehouseError` when the warehouse is unknown, so a mis-routed topic
    can never be filed under the default warehouse.
    """
    title = validate_warehouse(warehouse if warehouse is not None else result.get("warehouse"))
    row = build_row(result, title)

    from sendToDataBase import rest_post

    saved = rest_post(GLPC_TABLE, row, ignore_conflict=True)

    if saved is None:
        logger.error("Could not write report to %s: warehouse=%s", GLPC_TABLE, title)
        return False

    return True
