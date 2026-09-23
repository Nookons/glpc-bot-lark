"""
Отчёт за смену.

В конце каждой смены (06:00 — конец ночной, 18:00 — конец дневной)
отправляет в целевую группу Lark сводку по исключениям склада.

Отчёт «скомбинированный»: ключевые цифры, динамика к прошлой смене,
роботы на обслуживание, топ-5 типов и топ-5 сотрудников — без разбора
по каждому роботу.

Основной формат — интерактивная карточка; если карточка не проходит,
уходит текстовый вариант того же отчёта.

Превью, не дожидаясь смены:

    python3 shift_report.py                                  # текущая смена, текст
    python3 shift_report.py --date 2026-09-23 --shift day
    python3 shift_report.py --send                           # отправить карточку в группу
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from lark_media import hook_ok, send_card_via_hook, send_text_via_hook
from pending_photos import TARGET_HOOK_URL
from sendToDataBase import WAREHOUSE, shift_report_data
from logging_config import setup_logging


logger = setup_logging(__name__)

WARSAW_TZ = ZoneInfo("Europe/Warsaw")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)

    if raw is None or not str(raw).strip():
        return default

    try:
        return int(str(raw).strip())
    except ValueError:
        logger.warning("Invalid %s=%r, using %s", name, raw, default)
        return default


# Сколько строк показывать в топах.
REPORT_TOP = _env_int("REPORT_TOP", 5)

# С какого числа исключений робота зовём обслуживание.
MAINTENANCE_THRESHOLD = _env_int("ERROR_THRESHOLD", 3)

# С какого числа исключений за смену карточка становится оранжевой.
REPORT_WARN_TOTAL = _env_int("REPORT_WARN_TOTAL", 5)

# Отчёт отправляется в течение этого окна после конца смены.
REPORT_WINDOW_MINUTES = _env_int("REPORT_WINDOW_MINUTES", 15)

CHECK_INTERVAL_SECONDS = _env_int("CHECK_INTERVAL_SECONDS", 30)


# ============================================================
# TIME
# ============================================================

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


def previous_shift(shift_date: str, shift_name: str):
    """(date, name) смены, которая была перед указанной."""
    date = datetime.strptime(shift_date, "%Y-%m-%d").date()

    if shift_name == "day":
        # День D идёт после ночи, начавшейся D-1.
        return (date - timedelta(days=1)).strftime("%Y-%m-%d"), "night"

    # Ночь, начавшаяся D, идёт после дня D.
    return shift_date, "day"


# ============================================================
# FORMATTING HELPERS
# ============================================================

def shift_label(shift_name: str) -> str:
    return "Day (06:00–18:00)" if shift_name == "day" else "Night (18:00–06:00)"


def _pretty_date(shift_date: str) -> str:
    try:
        return datetime.strptime(shift_date, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return shift_date


def format_duration(minutes) -> str:
    """95 -> '1h 35m', 40 -> '40m'."""
    minutes = int(minutes or 0)

    if minutes < 60:
        return f"{minutes}m"

    hours, rest = divmod(minutes, 60)

    return f"{hours}h {rest:02d}m"


def format_delta(delta: int) -> str:
    if delta > 0:
        return f"+{delta} ▲"

    if delta < 0:
        return f"{delta} ▼"

    return "±0"


def _sorted_counts(mapping: dict):
    """[(key, count)] по убыванию количества, затем по имени."""
    return sorted(mapping.items(), key=lambda item: (-item[1], str(item[0])))


def _top_line(items, limit: int = None, unit: str = "more") -> str:
    limit = REPORT_TOP if limit is None else limit
    head = items[:limit]

    if not head:
        return "—"

    text = " · ".join(f"{name} ({count})" for name, count in head)
    rest = len(items) - len(head)

    if rest > 0:
        text += f" (+{rest} {unit})"

    return text


def _issues_line(types: dict, total: int, limit: int = None) -> str:
    limit = REPORT_TOP if limit is None else limit
    items = _sorted_counts(types)[:limit]

    if not items:
        return "—"

    parts = []

    for name, count in items:
        share = round(count * 100 / total) if total else 0
        parts.append(f"{name} — {count} ({share}%)")

    text = " · ".join(parts)
    rest = len(types) - len(items)

    if rest > 0:
        text += f" (+{rest} more)"

    return text


def _maintenance_line(maintenance) -> str:
    if not maintenance:
        return "none"

    return " · ".join(f"{robot} ({count})" for robot, count in maintenance)


# ============================================================
# METRICS
# ============================================================

def shift_metrics(shift_date: str, shift_name: str) -> dict:
    """Метрики смены + сравнение с предыдущей сменой."""
    data = shift_report_data(
        shift_date,
        shift_name,
        maintenance_threshold=MAINTENANCE_THRESHOLD,
    )

    prev_date, prev_name = previous_shift(shift_date, shift_name)

    previous = shift_report_data(
        prev_date,
        prev_name,
        maintenance_threshold=MAINTENANCE_THRESHOLD,
    )

    data["previous"] = {
        "date": prev_date,
        "shift": prev_name,
        "total": previous["total"],
    }
    data["delta"] = data["total"] - previous["total"]

    return data


def report_color(metrics: dict) -> str:
    """Цвет заголовка карточки: зелёный / оранжевый / красный."""
    if metrics["total"] == 0:
        return "green"

    if metrics["maintenance"]:
        return "red"

    if metrics["total"] >= REPORT_WARN_TOTAL:
        return "orange"

    return "green"


def report_title(shift_date: str, shift_name: str) -> str:
    return (
        f"📊 Shift report · {WAREHOUSE} · "
        f"{_pretty_date(shift_date)} · {shift_label(shift_name)}"
    )


# ============================================================
# BUILDERS
# ============================================================

def build_shift_summary(shift_date: str, shift_name: str, metrics: dict = None) -> str:
    """Текстовый вариант отчёта."""
    metrics = metrics or shift_metrics(shift_date, shift_name)

    header = report_title(shift_date, shift_name)
    total = metrics["total"]

    if not total:
        return f"{header}\n\n🎉 No exceptions this shift."

    previous = metrics["previous"]

    lines = [
        header,
        "",
        f"Total {total} exceptions · {len(metrics['robots'])} robots · "
        f"{len(metrics['employees'])} employees",
        f"Downtime {format_duration(metrics['downtime_minutes'])} · "
        f"vs previous shift ({_pretty_date(previous['date'])} {previous['shift']}) "
        f"{format_delta(metrics['delta'])}",
        "",
        f"⚠️ Maintenance ({MAINTENANCE_THRESHOLD}+ per shift): "
        f"{_maintenance_line(metrics['maintenance'])}",
        "",
        f"🗂 Top issues: {_issues_line(metrics['types'], total)}",
        "",
        f"👥 Top reporters: {_top_line(_sorted_counts(metrics['employees']))}",
        "",
        f"🤖 Top robots: {_top_line(_sorted_counts(metrics['robots']))}",
    ]

    return "\n".join(lines)


def build_shift_card(shift_date: str, shift_name: str, metrics: dict = None) -> dict:
    """Интерактивная карточка Lark."""
    metrics = metrics or shift_metrics(shift_date, shift_name)

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": report_color(metrics),
            "title": {
                "tag": "plain_text",
                "content": report_title(shift_date, shift_name),
            },
        },
        "elements": [],
    }

    total = metrics["total"]

    if not total:
        card["elements"].append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "🎉 **No exceptions this shift.**"},
        })

        return card

    previous = metrics["previous"]

    card["elements"].append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": (
                f"**Total** {total} exceptions · "
                f"**robots** {len(metrics['robots'])} · "
                f"**employees** {len(metrics['employees'])}\n"
                f"**Downtime** {format_duration(metrics['downtime_minutes'])}\n"
                f"**vs previous shift** "
                f"({_pretty_date(previous['date'])} {previous['shift']}): "
                f"**{format_delta(metrics['delta'])}**"
            ),
        },
    })

    card["elements"].append({"tag": "hr"})

    card["elements"].append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": (
                f"⚠️ **Maintenance ({MAINTENANCE_THRESHOLD}+ per shift):** "
                f"{_maintenance_line(metrics['maintenance'])}"
            ),
        },
    })

    card["elements"].append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": f"🗂 **Top issues:** {_issues_line(metrics['types'], total)}",
        },
    })

    card["elements"].append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": (
                "👥 **Top reporters:** "
                f"{_top_line(_sorted_counts(metrics['employees']))}"
            ),
        },
    })

    card["elements"].append({
        "tag": "note",
        "elements": [{
            "tag": "plain_text",
            "content": (
                "🤖 Top robots: "
                f"{_top_line(_sorted_counts(metrics['robots']))}"
            ),
        }],
    })

    return card


# ============================================================
# SENDING
# ============================================================

def send_shift_report(shift_date: str, shift_name: str):
    """
    Отправляет отчёт за смену в целевую группу.

    Сначала карточкой, при отказе — текстом.
    """
    metrics = shift_metrics(shift_date, shift_name)

    logger.info(
        "Sending shift report: date=%s shift=%s total=%s downtime=%sm",
        shift_date,
        shift_name,
        metrics["total"],
        metrics["downtime_minutes"],
    )

    card = build_shift_card(shift_date, shift_name, metrics)
    result = send_card_via_hook(TARGET_HOOK_URL, card)

    if hook_ok(result):
        logger.info("Shift report sent as card: %s/%s", shift_date, shift_name)
        return result

    logger.warning("Card rejected (%s) — отправляю текстовый отчёт", result)

    text = build_shift_summary(shift_date, shift_name, metrics)
    result = send_text_via_hook(TARGET_HOOK_URL, text)

    logger.info(
        "Shift report sent as text: %s/%s result=%s",
        shift_date,
        shift_name,
        result,
    )

    return result


# ============================================================
# SCHEDULER
# ============================================================

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


# ============================================================
# CLI: ПРЕВЬЮ
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Превью отчёта за смену")
    parser.add_argument("--date", help="дата смены YYYY-MM-DD (по умолчанию — текущая)")
    parser.add_argument("--shift", choices=("day", "night"), help="смена")
    parser.add_argument(
        "--send",
        action="store_true",
        help="отправить карточку в целевой Lark-чат (превью в группе)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="показать JSON карточки вместо текста",
    )

    args = parser.parse_args()

    if args.date and args.shift:
        shift_date, shift_name = args.date, args.shift
    else:
        from shift import get_current_shift

        shift_date, shift_name = get_current_shift()

    metrics = shift_metrics(shift_date, shift_name)

    if args.json:
        print(json.dumps(build_shift_card(shift_date, shift_name, metrics),
                         ensure_ascii=False, indent=2))
    else:
        print(build_shift_summary(shift_date, shift_name, metrics))

    if args.send:
        result = send_shift_report(shift_date, shift_name)
        print("\n--- отправлено в Lark ---")
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
