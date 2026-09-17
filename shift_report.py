"""
Отчёт за смену.

В конце каждой смены (06:00 — конец ночной, 18:00 — конец дневной)
отправляет в целевую группу (ту же, куда пересылаются ошибки)
сообщение с общими метриками за смену.
"""

import threading
import time

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from lark_media import send_text_via_hook
from pending_photos import TARGET_HOOK_URL
from sendToDataBase import shift_stats
from logging_config import setup_logging


logger = setup_logging(__name__)

WARSAW_TZ = ZoneInfo("Europe/Warsaw")

# Отчёт отправляется в течение этого окна после конца смены.
REPORT_WINDOW_MINUTES = 15
CHECK_INTERVAL_SECONDS = 30


def _now() -> datetime:
    return datetime.now(WARSAW_TZ)


def _get_reportable_shift(now: datetime):
    """
    Возвращает (shift_date, shift_name) для смены, которая только
    что закончилась, если мы в окне отправки отчёта. Иначе None.

    Дневная смена 06:00–18:00 → отчёт в ~18:00 за (сегодня, "day").
    Ночная смена 18:00–06:00 → отчёт в ~06:00 за (вчера, "night").
    """
    hour = now.hour
    minute = now.minute

    # Конец ночной смены: 06:00. Отчитываемся за ночную смену,
    # которая началась вчера.
    if hour == 6 and minute < REPORT_WINDOW_MINUTES:
        yesterday = now - timedelta(days=1)
        return yesterday.strftime("%Y-%m-%d"), "night"

    # Конец дневной смены: 18:00.
    if hour == 18 and minute < REPORT_WINDOW_MINUTES:
        return now.strftime("%Y-%m-%d"), "day"

    return None


def build_shift_summary(shift_date: str, shift_name: str) -> str:
    """Формирует текст метрик за смену."""
    total, by_robot, by_type = shift_stats(shift_date, shift_name)

    shift_label = "Day (06:00–18:00)" if shift_name == "day" else "Night (18:00–06:00)"

    lines = [
        "📊 Shift report",
        "",
        f"📅 Date: {shift_date}",
        f"🕐 Shift: {shift_label}",
        "",
        f"Total exceptions: {total}",
    ]

    if by_robot:
        lines.append("")
        lines.append("🤖 By robot:")
        for robot, count in sorted(
            by_robot.items(),
            key=lambda item: (-item[1], item[0]),
        ):
            lines.append(f"  • Robot {robot}: {count}")

    if by_type:
        lines.append("")
        lines.append("⚠️ By issue type:")
        for issue_type, count in sorted(
            by_type.items(),
            key=lambda item: (-item[1], item[0]),
        ):
            lines.append(f"  • {issue_type}: {count}")

    return "\n".join(lines)


def send_shift_report(shift_date: str, shift_name: str):
    """Отправляет метрики за смену в целевую группу."""
    text = build_shift_summary(shift_date, shift_name)

    logger.info(
        "Sending shift report: date=%s shift=%s",
        shift_date,
        shift_name,
    )

    result = send_text_via_hook(TARGET_HOOK_URL, text)

    logger.info(
        "Shift report sent: date=%s shift=%s result=%s",
        shift_date,
        shift_name,
        result,
    )

    return result


def _scheduler_loop():
    """Фоновый цикл: проверяет время и шлёт отчёт раз за смену."""
    sent = set()

    while True:
        try:
            now = _now()
            reportable = _get_reportable_shift(now)

            if reportable and reportable not in sent:
                sent.add(reportable)
                send_shift_report(*reportable)
        except Exception:
            logger.exception("Shift report scheduler error")

        time.sleep(CHECK_INTERVAL_SECONDS)


def start_shift_scheduler() -> threading.Thread:
    """Запускает фоновый планировщик отчётов за смену."""
    thread = threading.Thread(
        target=_scheduler_loop,
        name="shift-report-scheduler",
        daemon=True,
    )
    thread.start()
    logger.info("Shift report scheduler started")
    return thread
