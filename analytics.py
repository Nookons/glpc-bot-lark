"""
Аналитика по журналу ошибок и простоям (Telegram-команды и недельный отчёт).

Источники те же, что у отчёта смены:

  * `exceptions_glpc` — ошибки (тип проблемы, робот, время);
  * `change_status_robots` — переходы Offline→Online (простой).

Что считаем:

  * `/top [day|week|month]` — топ типов проблем и топ роботов;
  * `/downtime [дни]` — топ роботов по суммарному простою и MTTR;
  * `/week` — текст недельного отчёта (кандидаты на вывод из эксплуатации
    — роботы, которые часто уходят в офлайн).
"""

from __future__ import annotations

import threading
import time

from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from env_utils import env_bool, env_int
from lark_media import send_text_via_hook
from pending_photos import TARGET_HOOK_URL
from sendToDataBase import WAREHOUSE, rest_get_all
from supabase_storage import download_json, upload_json
from shift_report import downtime_intervals, format_duration
from time_utils import parse_iso
from logging_config import setup_logging


logger = setup_logging(__name__)


WARSAW_TZ = ZoneInfo("Europe/Warsaw")

EXCEPTIONS_TABLE = "exceptions_glpc"
MARKER_BUCKET = "bot-reports"

PERIODS = {"day": 1, "week": 7, "month": 30}
DEFAULT_PERIOD = "week"

TOP_LIMIT = env_int("ANALYTICS_TOP", 5)

# Недельный отчёт в Lark-группу. По умолчанию выключен: включается осознанно.
WEEKLY_REPORT_ENABLED = env_bool("WEEKLY_REPORT_ENABLED", False)
WEEKLY_REPORT_HOUR = env_int("WEEKLY_REPORT_HOUR", 8)
WEEKLY_CHECK_INTERVAL = env_int("WEEKLY_CHECK_INTERVAL", 300)

# Сколько офлайнов за период делают робота кандидатом на вывод.
RETIREMENT_MIN_OFFLINES = env_int("RETIREMENT_MIN_OFFLINES", 3)

_last_sent_monday = None


def period_days(period: str) -> int:
    """Длительность периода в днях (None — неизвестный период)."""
    return PERIODS.get(str(period or "").strip().lower())


def period_start(days: int, now: datetime = None) -> datetime:
    now = now or datetime.now(timezone.utc)

    return now - timedelta(days=days)


def fetch_exceptions(since: datetime, warehouse: str = None):
    """
    Ошибки склада за период (постранично, чтобы не упереться в лимит).

    None — сбой чтения.
    """
    return rest_get_all(
        EXCEPTIONS_TABLE,
        params={
            "select": "error_robot,issue_type,error_start_time",
            "warehouse": f"eq.{warehouse or WAREHOUSE}",
            "error_start_time": f"gte.{since.isoformat()}",
            "order": "error_start_time.desc",
        },
    )


def top_report(period: str = DEFAULT_PERIOD, now: datetime = None, warehouse: str = None):
    """Топ типов проблем и роботов за период."""
    days = period_days(period)

    if not days:
        return None

    since = period_start(days, now)
    rows = fetch_exceptions(since, warehouse)

    if rows is None:
        return None

    issues = Counter()
    robots = Counter()

    for row in rows:
        issue = (row.get("issue_type") or "unknown").strip() or "unknown"
        issues[issue] += 1

        robot = row.get("error_robot")

        if robot is not None:
            robots[str(robot)] += 1

    return {
        "period": period,
        "days": days,
        "since": since,
        "warehouse": warehouse or WAREHOUSE,
        "total": len(rows),
        "issues": issues.most_common(TOP_LIMIT),
        "robots": robots.most_common(TOP_LIMIT),
    }


def format_top_report(report) -> str:
    """Текст ответа на /top."""
    if report is None:
        return (
            "Usage: /top [day|week|month]\n"
            "Example: /top week"
        )

    lines = [
        f"📈 Top for the last {report['days']} day(s) · "
        f"{report.get('warehouse') or WAREHOUSE}",
        f"Total: {report['total']} exceptions",
    ]

    if not report["total"]:
        lines.append("")
        lines.append("No exceptions in this period.")
        return "\n".join(lines)

    lines.append("")
    lines.append("🗂 Issues:")

    for issue, count in report["issues"]:
        lines.append(f"  {count} · {issue}")

    lines.append("")
    lines.append("🤖 Robots:")

    for robot, count in report["robots"]:
        lines.append(f"  {count} · #{robot}")

    return "\n".join(lines)


def downtime_report(days: int = 7, now: datetime = None, warehouse: str = None):
    """Топ роботов по суммарному простою за последние days дней."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)

    intervals = downtime_intervals(warehouse)

    if intervals is None:
        return None

    # Ремонты, начавшиеся в периоде: только они дают сравнимый MTTR.
    inside = [
        item for item in intervals
        if item["start"] >= since and item["end"] >= since
    ]

    # Закрытые в периоде заявки, которые открыли раньше (робот стоял долго).
    legacy = [
        item for item in intervals
        if item["start"] < since and item["end"] >= since
    ]

    per_robot = {}

    for item in inside:
        entry = per_robot.setdefault(
            item["robot"],
            {"seconds": 0.0, "count": 0, "last": item["end"]},
        )
        entry["seconds"] += item["seconds"]
        entry["count"] += 1

        if item["end"] > entry["last"]:
            entry["last"] = item["end"]

    top = sorted(
        per_robot.items(),
        key=lambda pair: -pair[1]["seconds"],
    )[:TOP_LIMIT]

    mttr = (
        int(sum(item["seconds"] for item in inside) / len(inside))
        if inside
        else None
    )

    return {
        "days": days,
        "warehouse": warehouse or WAREHOUSE,
        "repairs": len(inside),
        "mttr_seconds": mttr,
        "top": top,
        "legacy": len(legacy),
        "legacy_longest": (
            max(legacy, key=lambda item: item["seconds"]) if legacy else None
        ),
    }


def format_downtime_report(report, days: int = 7) -> str:
    """Текст ответа на /downtime."""
    if report is None:
        return (
            "Usage: /downtime [days]\n"
            "Example: /downtime 7"
        )

    lines = [
        f"🛠 Downtime · last {report['days']} day(s) · "
        f"{report.get('warehouse') or WAREHOUSE}"
    ]

    if report["mttr_seconds"] is None:
        lines.append("No robot came back online in this period.")
        return "\n".join(lines)

    lines.append(
        f"Repairs started: {report['repairs']} · "
        f"MTTR {format_duration(report['mttr_seconds'] // 60)}"
    )

    if report.get("legacy"):
        longest = report.get("legacy_longest") or {}
        lines.append(
            f"Also closed: {report['legacy']} older repair(s)"
            + (
                f" · longest #{longest.get('robot')} "
                f"{format_duration(int(longest['seconds']) // 60)}"
                if longest
                else ""
            )
        )

    lines.append("")
    lines.append("Longest total downtime:")

    for robot, entry in report["top"]:
        lines.append(
            f"  #{robot} · {format_duration(int(entry['seconds']) // 60)} "
            f"over {entry['count']} repair(s)"
        )

    return "\n".join(lines)


def retirement_candidates(
    days: int = 30,
    min_offlines: int = None,
    now: datetime = None,
    warehouse: str = None,
):
    """Роботы, которые чаще всех уходили в офлайн (кандидаты на вывод)."""
    min_offlines = RETIREMENT_MIN_OFFLINES if min_offlines is None else min_offlines
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)

    intervals = downtime_intervals(warehouse)

    if intervals is None:
        return None

    counter = Counter()
    totals = {}

    for item in intervals:
        if item["start"] < since:
            continue

        counter[item["robot"]] += 1
        totals[item["robot"]] = totals.get(item["robot"], 0.0) + item["seconds"]

    return [
        (robot, count, totals[robot])
        for robot, count in counter.most_common()
        if count >= min_offlines
    ]


def weekly_text(days: int = 7, now: datetime = None, warehouse: str = None) -> str:
    """Текст недельного отчёта (пустая строка — данных нет)."""
    now = now or datetime.now(WARSAW_TZ)

    top = top_report("week", now, warehouse)
    downtime = downtime_report(days, now, warehouse)
    candidates = retirement_candidates(days, now=now, warehouse=warehouse)

    if top is None and downtime is None:
        return ""

    lines = [
        f"📊 Weekly report · {warehouse or WAREHOUSE} · "
        f"{now.strftime('%d.%m.%Y')}",
    ]

    if top:
        lines.append("")
        lines.append(
            f"Exceptions: {top['total']} over {days} day(s)"
        )

        for issue, count in top["issues"][:3]:
            lines.append(f"  {count} · {issue}")

        for robot, count in top["robots"][:3]:
            lines.append(f"  {count} · #{robot}")

    if downtime and downtime["mttr_seconds"] is not None:
        lines.append("")
        lines.append(
            f"🛠 Repairs started: {downtime['repairs']} · "
            f"MTTR {format_duration(downtime['mttr_seconds'] // 60)}"
        )

        if downtime.get("legacy"):
            lines.append(f"  also closed: {downtime['legacy']} older repair(s)")

        for robot, entry in downtime["top"][:3]:
            lines.append(
                f"  #{robot} · {format_duration(int(entry['seconds']) // 60)}"
            )

    if candidates:
        lines.append("")
        lines.append("🔎 Most offline events (consider repair/replacement):")

        for robot, count, seconds in candidates[:3]:
            lines.append(
                f"  #{robot} · {count} offline(s) · "
                f"{format_duration(int(seconds) // 60)}"
            )

    return "\n".join(lines)


def _marker_name(now: datetime) -> str:
    monday = now - timedelta(days=now.weekday())

    return f"{monday.strftime('%Y-%m-%d')}-weekly.json"


def was_sent(now: datetime) -> bool:
    marker = download_json(MARKER_BUCKET, _marker_name(now))

    return isinstance(marker, dict) and bool(marker.get("sent_at"))


def mark_sent(now: datetime) -> bool:
    return upload_json(
        MARKER_BUCKET,
        _marker_name(now),
        {"sent_at": now.strftime("%d.%m.%Y %H:%M:%S")},
    )


def send_weekly_report(now: datetime = None, force: bool = False) -> dict:
    """Отправляет недельный отчёт в целевую Lark-группу."""
    now = now or datetime.now(WARSAW_TZ)

    if not force and was_sent(now):
        return {"sent": False, "reason": "already sent"}

    text = weekly_text(now=now)

    if not text:
        return {"sent": False, "reason": "nothing to report"}

    result = send_text_via_hook(TARGET_HOOK_URL, text)

    if result is None:
        logger.error("Недельный отчёт не ушёл в Lark")
        return {"sent": False, "reason": "hook failed"}

    mark_sent(now)

    logger.info("Недельный отчёт отправлен в Lark")
    return {"sent": True, "reason": "ok"}


def weekly_due(now: datetime) -> bool:
    """Пора ли: понедельник, после WEEKLY_REPORT_HOUR, и ещё не отправляли."""
    global _last_sent_monday

    if not WEEKLY_REPORT_ENABLED:
        return False

    if now.weekday() != 0 or now.hour < WEEKLY_REPORT_HOUR:
        return False

    key = now.strftime("%Y-%m-%d")

    if _last_sent_monday == key:
        return False

    if was_sent(now):
        _last_sent_monday = key
        return False

    return True


def weekly_loop():
    while True:
        try:
            now = datetime.now(WARSAW_TZ)

            if weekly_due(now):
                result = send_weekly_report(now)

                if result.get("reason") in ("ok", "already sent", "nothing to report"):
                    _set_last_monday(now)
        except Exception:
            logger.exception("Ошибка недельного отчёта")

        time.sleep(WEEKLY_CHECK_INTERVAL)


def _set_last_monday(now: datetime):
    global _last_sent_monday
    _last_sent_monday = now.strftime("%Y-%m-%d")


def start_weekly_scheduler() -> threading.Thread:
    """Запускает недельный отчёт (только у опрашивающего инстанса)."""
    thread = threading.Thread(
        target=weekly_loop,
        name="weekly-report",
        daemon=True,
    )
    thread.start()

    logger.info(
        "Weekly report scheduler started (понедельник после %s:00)",
        WEEKLY_REPORT_HOUR,
    )

    return thread
