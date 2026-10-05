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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from equipment_intake.template_match import match_template
from shift import WARSAW_TZ, get_current_shift
from warehouses import WAREHOUSES

logger = logging.getLogger(__name__)

GLPC_TABLE = "exceptions_glpc"

# Columns that arrive with the intake-analytics migration. They may be missing
# from the live table while the bot is already deployed, so the write must work
# without them (see `write_exception`). A missing column is an expected state
# during rollout, not a failure.
OPTIONAL_COLUMNS = ("module", "device_number", "object_type", "report_id", "unresolved_reason")

# How long the "columns are missing" answer is remembered. Short TTL, same
# reasoning as the intake editor's schema cache: after the migration is applied
# the bot picks the columns up without a restart.
_OPTIONAL_TTL_SECONDS = 120
_optional_columns_state: Optional[bool] = None
_optional_columns_checked_at: float = 0.0

# Legacy constant kept for parity with every existing row: the API intake also
# writes "C2" for both warehouses, and nothing in the bot reads this column.
# Changing it is a separate decision (integration plan, question 8.2).
ISSUE_WAREHOUSE = "C2"

# Categories that can be resolved against the canonical equipment inventory.
INVENTORY_CATEGORIES = ("robot", "workstation", "charging")

# Справочник готовых описаний ошибок: по нему отдел ведёт журнал. Пока приём
# писал сюда сырой текст, строки нельзя было ни группировать по типу ошибки, ни
# считать время решения.
TEMPLATES_TABLE = "issue_templates"

#: Поля шаблона, нужные для подбора и заполнения колонок.
TEMPLATE_COLUMNS = (
    "employee_title",
    "issue_sub_type",
    "issue_type",
    "issue_description",
    "recovery_title",
    "solving_time",
)

# Справочник меняется редко (29 строк, правится вручную), поэтому держим его в
# памяти. TTL, а не вечное кэширование: добавленный шаблон должен заработать без
# перезапуска бота.
_TEMPLATES_TTL_SECONDS = 600
_templates_cache: Optional[list] = None
_templates_checked_at: float = 0.0


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
    """
    Ответ оператора → канонический ключ категории.

    **`qr` → `qr_code`.** Дерево отдаёт `qr`, а колонка `object_type` и
    справочник `equipment.category` используют `qr_code`. Раньше здесь
    возвращалось `qr`, и одно понятие получало **два написания** в зависимости от
    того, каким путём пришёл отчёт: через API (`telegram_reports.py`
    канонизирует `qr` → `qr_code`) или напрямую из бота. Собственный комментарий
    API называет это недопустимым, и он прав: группировка по категориям
    разделила бы «qr» и «qr_code» на две строки.

    Проверено по живой базе: словарь `equipment.category` —
    `charging` / `qr_code` / `robot` / `workstation`, значения `qr` там нет.
    """
    return {
        "robot": "robot",
        "workstation": "workstation",
        "charging station": "charging",
        "charger": "charging",
        "qr code": "qr_code",
        "qr": "qr_code",
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


def _card_id(value):
    """
    `add_by` value for the journal, or None.

    A whitespace-only card must not become a value that points nowhere, so the
    text is trimmed like every other text field here. A numeric card passes
    through unchanged — `employees.card_id` is not always a string.
    """
    if value is None:
        return None

    if isinstance(value, str):
        return value.strip() or None

    return value


def _optional_fields(
    answers: Dict[str, Any],
    category: str,
    module: str,
    report_id: str,
    unresolved_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Columns added by the intake-analytics migration.

    Kept separate so `write_exception` can retry without them: the bot is
    deployed before the migration is applied, and PostgREST answers 400 for an
    unknown column. Those four columns must never cost the report itself.
    """
    device_number = str(answers.get("device_number") or "").strip()

    return {
        "module": module or None,
        "device_number": device_number or None,
        "object_type": category or None,
        "report_id": report_id or None,
        "unresolved_reason": unresolved_reason,
    }


def _unresolved_reason(card_id: Any, result: Dict[str, Any]) -> Optional[str]:
    """
    Чего не хватило в записи — одной строкой через запятую.

    Поле `unresolved_reason` заведено миграцией 0067, но до этой правки его
    никто не заполнял: по нему нельзя было понять, полная запись или нет,
    хотя именно для этого он и нужен. Считается здесь, а не в
    `_optional_fields`, потому что фото и карточка работника живут в `result`,
    а не в ответах дерева.

    Значения и **разделитель** совпадают с backfill'ом в 0067 (`concat_ws(', ', …)`)
    — иначе одна колонка имела бы два формата: `no_employee,no_photo` от бота и
    `no_employee, no_end_time, no_photo` от миграции. Потребитель, разбирающий
    строку по `", "`, молча не увидел бы причин в записях бота. Проверено на
    живой базе: в ней уже есть строки обоих видов.

    `no_end_time` здесь намеренно **нет**: приём всегда пишет `error_end_time`
    (он равен времени отчёта, `solving_time = 0`), поэтому эта причина у новых
    записей невозможна. Ставить её «на всякий случай» значило бы помечать
    полные записи неполными.
    """
    reasons = []

    if not card_id:
        # Ошибку не с кем связать: в метрики по работнику она не попадёт.
        reasons.append("no_employee")

    if not str(result.get("photo_url") or "").strip():
        # Фото к ошибке не привязано (загрузка не удалась).
        reasons.append("no_photo")

    return ", ".join(reasons) or None


def load_templates() -> list:
    """
    Справочник `issue_templates` (кэш на 10 минут).

    Пустой список — не ошибка: он означает «шаблонов нет». `None` от `rest_get`
    тоже приводится к пустому списку, потому что сбой базы **не должен** ломать
    приём: отчёт запишется с прежними значениями, как до этой правки. Разница
    между «база недоступна» и «шаблонов нет» для подбора не важна — в обоих
    случаях шаблон не найден, и подставлять наугад нельзя.
    """
    global _templates_cache, _templates_checked_at

    now = time.time()

    if _templates_cache is not None and now - _templates_checked_at < _TEMPLATES_TTL_SECONDS:
        return _templates_cache

    from sendToDataBase import rest_get

    # `optional=True`: сбой справочника — ожидаемая ситуация, не ERROR в логе.
    rows = rest_get(
        TEMPLATES_TABLE,
        {"select": ",".join(TEMPLATE_COLUMNS), "limit": "500"},
        optional=True,
    )

    _templates_cache = list(rows or [])
    _templates_checked_at = now

    return _templates_cache


def reset_templates_cache() -> None:
    """Сбросить кэш справочника (тесты и ручная проверка)."""
    global _templates_cache, _templates_checked_at

    _templates_cache = None
    _templates_checked_at = 0.0


def _template_values(template: Optional[Dict[str, Any]], category: str, module: str, device_type: str) -> Dict[str, Any]:
    """
    Значения колонок журнала из подобранного шаблона.

    Маппинг восстановлен по живой базе, а не придуман: на 12 474 классических
    строках, где человек проставил шаблон точно, `second_column` =
    `employee_title` совпадает в 79 % случаев, `first_column` =
    `issue_sub_type` — в 53 %.

    При `template = None` возвращаются прежние значения приёма: категория
    оборудования и модуль. Это честный откат, а не догадка — пустая колонка
    лучше ошибки, отнесённой не к тому типу.
    """
    if not template:
        return {
            "first_column": category,
            "issue_type": category,
            "second_column": module or device_type,
            "recovery_title": None,
            "solving_time": 0,
        }

    return {
        # `issue_sub_type` — «подтип» ошибки, именно он лежит в `first_column` у
        # классических строк. Если у шаблона он пуст (такие есть: id 122),
        # берём название, чтобы колонка не осталась пустой.
        "first_column": template.get("issue_sub_type") or template.get("employee_title"),
        "issue_type": template.get("issue_type"),
        "second_column": template.get("employee_title"),
        "recovery_title": template.get("recovery_title") or None,
        "solving_time": _solving_time(template.get("solving_time")),
    }


def _solving_time(value: Any) -> int:
    """
    Время решения в минутах — целое.

    В базе колонка целочисленная, а `error_end_time` считается как
    `error_start_time + solving_time`. Нечисловое значение приводим к нулю:
    испорченный справочник не должен ронять запись отчёта.
    """
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def build_row(
    result: Dict[str, Any], warehouse: str, include_optional: bool = True
) -> Dict[str, Any]:
    """
    Map an intake result onto the legacy journal columns.

    `include_optional=False` drops the analytics columns for a table that does
    not have them yet; every other value stays identical, so a pending
    migration can never lose a report.

    `add_by`, `photo_url` and the analytics fields are read from `result`,
    which `flow._confirm` fills while it already has the employee row at hand —
    so the writer never issues a second lookup for data the caller knows.
    """
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

    # `add_by` is the employee's `card_id`, which the caller already resolved
    # while looking the name up. It stays None when the account is not linked or
    # the database is unavailable — the report must still be filed, exactly like
    # `employee` does. A missing card is acceptable; an invented one is not.
    card_id = _card_id(result.get("employee_card_id"))

    # Подбор готового описания ошибки из справочника. Раньше сюда шёл сырой текст
    # оператора: `first_column` и `issue_type` дублировали категорию оборудования
    # («robot» = «robot»), `recovery_title` был пуст, `solving_time` равен нулю.
    # Из-за этого строки приёма нельзя было ни группировать по типу ошибки, ни
    # считать время решения. Проверено на живой базе 05.10.2026: так выглядели
    # все 246 строк бота.
    #
    # `match_template` возвращает `None`, когда уверенного совпадения нет, — и
    # тогда остаются прежние значения. Выдумывать шаблон нельзя: пустая колонка
    # честнее ошибки, отнесённой не к тому типу.
    template = match_template(answers.get("description"), load_templates())
    template_columns = _template_values(template, category, module, device_type)

    # Время решения берём из шаблона; конец ошибки считается по конвенции
    # журнала (`error_end_time = error_start_time + solving_time`), проверенной
    # на 23 384 из 23 400 строк. Без шаблона `solving_time = 0`, и конец равен
    # началу — как было раньше.
    solving_time = template_columns["solving_time"]
    error_end = received + timedelta(minutes=solving_time)

    row = {
        "error_robot": _robot_number(code),
        "add_by": card_id,
        "device_type": device_type,
        "employee": result.get("employee") or result.get("username") or f"Telegram {user_id}",
        "error_end_time": error_end.isoformat(),
        "error_start_time": received.isoformat(),
        "first_column": template_columns["first_column"],
        # Сырой текст оператора остаётся здесь целиком и **не** подменяется
        # шаблоном: это единственное место, где видно, что именно написал
        # человек. То же описание дублируется в `telegram_equipment_reports`
        # (`description`), поэтому подбор ничего не теряет.
        "issue_description": "\n".join(details),
        "issue_type": template_columns["issue_type"],
        "photo_url": result.get("photo_url") or None,
        "recovery_title": template_columns["recovery_title"],
        "second_column": template_columns["second_column"],
        "solving_time": solving_time,
        "uniq_key": _uniq_key(result, report_id, user_id),
        "shift_type": shift_name,
        "warehouse": warehouse,
        "issue_data": shift_date,
        "issue_warehouse": ISSUE_WAREHOUSE,
    }

    if include_optional:
        row.update(
            _optional_fields(answers, category, module, report_id, _unresolved_reason(card_id, result))
        )

    return row


def _analytics_columns_available() -> bool:
    """
    Are the intake-analytics columns present yet?

    Asked as a cheap, expected-to-fail single-row read, exactly like the intake
    editor probes its v2 schema: the bot is deployed before the migration is
    applied, so a missing column is a normal state and must not reach the logs as
    ERROR on every report.

    A `None` answer is ambiguous — the column may be missing, or the database may
    be unreachable. The second case is told apart by one extra plain read, and it
    is deliberately **not** cached: a network blip must not strip the analytics
    columns from the next 120 seconds of reports. The happy path costs a single
    request, and the answer is cached with a short TTL so a migration applied
    while the bot runs is picked up without a restart.
    """
    global _optional_columns_state, _optional_columns_checked_at

    if (
        _optional_columns_state is not None
        and time.time() - _optional_columns_checked_at < _OPTIONAL_TTL_SECONDS
    ):
        return _optional_columns_state

    from sendToDataBase import rest_get

    # `optional=True`: отсутствие колонок — ожидаемое состояние до миграции.
    rows = rest_get(
        GLPC_TABLE,
        {"select": ",".join(OPTIONAL_COLUMNS), "limit": "1"},
        optional=True,
    )

    if rows is None:
        reachable = rest_get(GLPC_TABLE, {"select": "id", "limit": "1"}) is not None

        if not reachable:
            # База недоступна: это не «колонок нет», и запоминать это нельзя.
            logger.warning(
                "intake journal: could not check analytics columns in %s; "
                "filing with the full row",
                GLPC_TABLE,
            )
            return True

        logger.warning(
            "intake journal: %s has no analytics columns yet (%s) — filing "
            "reports without them",
            GLPC_TABLE,
            ", ".join(OPTIONAL_COLUMNS),
        )
        _optional_columns_state = False
        _optional_columns_checked_at = time.time()

        return False

    _optional_columns_state = True
    _optional_columns_checked_at = time.time()

    return True


def reset_schema_cache() -> None:
    """Сброс памяти о колонках — только для тестов."""
    global _optional_columns_state, _optional_columns_checked_at

    _optional_columns_state = None
    _optional_columns_checked_at = 0.0


def write_exception(result: Dict[str, Any], warehouse: Optional[str] = None) -> bool:
    """
    File one completed report in `exceptions_glpc`.

    Returns True when the row exists after the call (a repeated delivery counts
    as success — `uniq_key` makes the insert idempotent).

    Raises `WarehouseError` when the warehouse is unknown, so a mis-routed topic
    can never be filed under the default warehouse.

    Rollout safety: the analytics columns (`module`, `device_number`,
    `object_type`, `report_id`) arrive with a migration that may lag the deploy,
    and PostgREST rejects a whole insert when one of them is unknown. Their
    presence is checked up front (see `_analytics_columns_available`), so the
    report is written once, with whatever columns actually exist, and the
    expected pre-migration state never reaches the logs as an error.
    """
    title = validate_warehouse(warehouse if warehouse is not None else result.get("warehouse"))
    include_optional = _analytics_columns_available()

    from sendToDataBase import rest_post

    saved = rest_post(
        GLPC_TABLE,
        build_row(result, title, include_optional=include_optional),
        ignore_conflict=True,
    )

    if saved is None and include_optional:
        # The probe just succeeded, so the database is reachable and the insert
        # was most likely rejected over a column. Retrying without the analytics
        # fields is cheap insurance against losing the report; the negative
        # answer is remembered so the next reports skip the probe.
        logger.warning(
            "Write to %s failed with the analytics columns; retrying without them",
            GLPC_TABLE,
        )
        _optional_columns_state = False
        _optional_columns_checked_at = time.time()

        saved = rest_post(
            GLPC_TABLE,
            build_row(result, title, include_optional=False),
            ignore_conflict=True,
        )

    if saved is None:
        logger.error("Could not write report to %s: warehouse=%s", GLPC_TABLE, title)
        return False

    return True
