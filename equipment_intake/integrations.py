"""Production Supabase persistence and warehouse-routed Lark delivery."""
from __future__ import annotations
import logging
from typing import Any, Dict
logger = logging.getLogger(__name__)
REPORTS = "telegram_equipment_reports"
QUEUE = "telegram_devices_to_add"


def _category(value):
    """Single source of truth: the journal writer owns this mapping."""
    from .report_writer import intake_category

    return intake_category(value)


def _warehouse_error(result):
    """
    Validate the warehouse once, for every destination.

    The warehouse decides the journal column, the row's site and which Lark
    group receives the card. Checking it per-destination produced inconsistent
    outcomes: the journal refused an unknown warehouse while the detail table
    stored a row with `warehouse=""` and the card was posted to the default
    (GLP-C) group — a fault announced in the wrong chat.
    """
    from .report_writer import WarehouseError, validate_warehouse

    try:
        return validate_warehouse(result.get("warehouse")), None
    except WarehouseError as error:
        return None, str(error)


def _save(result, image_path):
    from sendToDataBase import rest_get, rest_post
    from supabase_storage import upload_photo_and_get_url
    answers = result.get("answers") or {}
    category = _category(answers.get("object"))
    photo_url = None
    try:
        photo_url = upload_photo_and_get_url(image_path)
    except Exception:
        logger.exception("Equipment intake photo upload failed")
    device_number = str(answers.get("device_number") or "").strip()
    payload = {
        "warehouse": result.get("warehouse") or "", "category": category,
        "device_type": str(answers.get("device_type") or "").strip(),
        "module": str(answers.get("module") or "").strip() or None,
        "device_number": device_number,
        "description": str(answers.get("description") or "").strip(),
        "photo_url": photo_url, "employee": result.get("employee") or "",
        "telegram_user_id": result.get("user_id"), "telegram_username": result.get("username") or "",
        "telegram_chat_id": result.get("chat_id"), "topic_id": result.get("thread_id"),
        "source_message_id": result.get("message_id"), "answers": answers,
        "path": result.get("path") or [],
    }
    # Idempotency on Telegram source coordinates.
    saved = rest_post(REPORTS, payload, ignore_conflict=True) is not None
    queued = False
    if category in {"robot", "workstation", "charging"} and device_number:
        # Only enqueue when the canonical inventory lookup succeeded and found no match.
        # The queue is auxiliary: a failure here must not erase the fact that
        # the report row itself was written.
        try:
            known = rest_get("equipment", {"select":"id", "category":f"eq.{category}", "equipment_code":f"eq.{device_number}", "warehouse":f"eq.{payload['warehouse']}", "limit":"1"})
            if known is not None and not known:
                q = {"category":category, "device_type":payload["device_type"], "device_number":device_number,
                     "warehouse":payload["warehouse"], "employee":payload["employee"],
                     "telegram_user_id":result.get("user_id"), "report_description":payload["description"]}
                queued = rest_post(QUEUE, q, ignore_conflict=True) is not None
            elif known is None:
                logger.error("Inventory lookup failed; device was not marked unknown")
        except Exception:
            logger.exception("Equipment intake device-queue write failed")
    return saved, queued, photo_url


def _identity_field(answers):
    """
    Label and value for the equipment identity, matching the journal's shape.

    The card used to show only `device_number`, so a QR Floor report (which
    carries X/Y/zone instead) displayed "Device number: —" and the actual
    coordinates never reached the group. The identity is built by the same
    helper the journal uses, so the two cannot drift apart.
    """
    from .report_writer import identifier

    value = identifier(answers) or "—"
    shelf = str(answers.get("shelf_number") or "").strip()

    if shelf:
        return "Shelf number (not its QR code)", value

    if str(answers.get("qr_x") or "").strip():
        return "QR code X / Y / zone", value

    return "Device number", value


def _card(result, photo_url, db_saved, queued, glpc_saved=None):
    a = result.get("answers") or {}; category = _category(a.get("object"))
    labels = {"robot":"Robot", "workstation":"Workstation", "charging":"Charging station", "qr":"QR code"}
    identity_label, identity_value = _identity_field(a)
    fields = [("Warehouse",result.get("warehouse") or "Unknown"),("Equipment",f"{labels.get(category,category)} · {a.get('device_type') or '—'}"),(identity_label,identity_value)]
    if a.get("module"): fields.append(("Module",a["module"]))
    fields.append(("Reported by",result.get("employee") or result.get("username") or "Unknown"))

    # Two different stores with two different consequences: the detail table
    # feeds the intake history, while `exceptions_glpc` feeds shift reports and
    # /top. Reporting only the first as "Saved" hid a failed journal write from
    # everyone reading the group.
    fields.append(("Intake details","Saved" if db_saved else "Save failed"))

    if glpc_saved is not None:
        fields.append(("Shift journal","Saved" if glpc_saved else "Save failed"))

    if queued: fields.append(("Device registry","Added to review queue"))
    elements=[{"tag":"div","fields":[{"is_short":True,"text":{"tag":"lark_md","content":f"**{k}**\n{str(v)[:500]}"}} for k,v in fields]}]
    elements.append({"tag":"div","text":{"tag":"plain_text","content":f"Problem: {str(a.get('description') or '')[:1500]}"}})
    if photo_url: elements.append({"tag":"action","actions":[{"tag":"button","text":{"tag":"plain_text","content":"Open photo"},"type":"primary","url":photo_url}]})
    return {"header":{"template":"orange","title":{"tag":"plain_text","content":"Equipment error report"}},"elements":elements}


def _send(result, photo_url, db_saved, queued, image_path, glpc_saved=None, warehouse=None):
    """
    Post the card to the group of the report's warehouse.

    `warehouse` should be the value already validated by `persist_and_send`.
    Passing the raw `result["warehouse"]` is unsafe: `lark_hooks` matches the
    title exactly, so a padded or differently-cased value silently falls back to
    the shared hook and the card lands in the wrong group.
    """
    import lark_hooks, lark_media

    title = warehouse or result.get("warehouse") or ""
    hook = lark_hooks.error_hook(title)
    card = _card(result, photo_url, db_saved, queued, glpc_saved)
    try:
        image_key = lark_media.upload_image(image_path)
        card["elements"].insert(0,{"tag":"img","img_key":image_key,"alt":{"tag":"plain_text","content":"Reported photo"}})
    except Exception as e:
        logger.warning("Lark image upload unavailable (%s); card will include photo link if available",type(e).__name__)
    return lark_media.hook_ok(lark_media.send_card_via_hook(hook,card))


def persist_and_send(result: Dict[str, Any], image_path: str) -> dict:
    """
    File one report and announce it, keeping destinations independent.

    Independence applies to *failures*: a Lark outage must not discard a saved
    row, and a database outage must not silence the card. It does not apply to
    the warehouse, which is a precondition — an unknown warehouse makes every
    destination unsafe (a detail row with `warehouse=""`, a card posted to the
    default GLP-C group). Such a report is refused wholesale.
    """
    warehouse, warehouse_error = _warehouse_error(result)

    if warehouse_error:
        logger.error("Equipment intake refused: %s", warehouse_error)
        return {
            "database_saved": False,
            "device_queued": False,
            "lark_delivered": False,
            "glpc_saved": False,
            "glpc_error": warehouse_error,
        }

    db_saved = queued = False
    photo_url = None
    try:
        db_saved, queued, photo_url = _save(result, image_path)
    except Exception:
        logger.exception("Equipment intake Supabase persistence failed")

    # The journal is written independently from the detail table and from Lark:
    # one destination failing must not stop the others.
    glpc_saved = False
    glpc_error = None
    try:
        from .report_writer import write_exception

        # The photo was already uploaded for the Lark card; the same URL is the
        # journal's `photo_url`. `_save` returns None when the upload failed, and
        # then the column simply stays empty — the report is still filed.
        if photo_url:
            result["photo_url"] = photo_url

        glpc_saved = write_exception(result, warehouse)
    except Exception:
        logger.exception("Equipment intake journal write failed")

    try:
        delivered = _send(result, photo_url, db_saved, queued, image_path, glpc_saved, warehouse)
    except Exception:
        logger.exception("Equipment intake Lark delivery failed")
        delivered = False

    return {
        "database_saved": db_saved,
        "device_queued": queued,
        "lark_delivered": delivered,
        "glpc_saved": glpc_saved,
        "glpc_error": glpc_error,
    }
