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

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from lark_media import hook_ok, send_card_via_hook, send_text_via_hook
import robot_status
from shift import DAY_END, DAY_START
from supabase_storage import download_json, upload_json
from pending_photos import TARGET_HOOK_URL
from sendToDataBase import WAREHOUSE, rest_get, shift_report_data
from logging_config import setup_logging
from time_utils import parse_iso


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

# Журнал смен статуса небольшой (сотни строк), поэтому читаем его целиком:
# простой считается склейкой Offline→Online, и обрезанный список дал бы
# неверные интервалы.
HISTORY_TABLE = "change_status_robots"
HISTORY_SCAN_LIMIT = _env_int("DOWNTIME_HISTORY_LIMIT", 4000)

# С какого числа исключений робота зовём обслуживание.
MAINTENANCE_THRESHOLD = _env_int("ERROR_THRESHOLD", 3)

# С какого числа исключений за смену карточка становится оранжевой.
REPORT_WARN_TOTAL = _env_int("REPORT_WARN_TOTAL", 5)

# Bucket для маркеров «отчёт за смену отправлен».
REPORT_BUCKET = os.environ.get("SUPABASE_REPORT_BUCKET", "bot-reports")

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

    # Конец ночной смены. Отчитываемся за ночную смену, начавшуюся вчера.
    if hour == DAY_START and minute < REPORT_WINDOW_MINUTES:
        yesterday = now - timedelta(days=1)
        return yesterday.strftime("%Y-%m-%d"), "night"

    # Конец дневной смены.
    if hour == DAY_END and minute < REPORT_WINDOW_MINUTES:
        return now.strftime("%Y-%m-%d"), "day"

    return None


def previous_shift(shift_date: str, shift_name: str):
    """(date, name) смены перед указанной. None — дата некорректна."""
    try:
        date = datetime.strptime(str(shift_date), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        logger.warning(
            "Некорректная дата смены %r — сравнение с прошлой сменой пропускаю",
            shift_date,
        )
        return None

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


def format_delta(delta) -> str:
    if delta is None:
        return "—"

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

def shift_window(shift_date: str, shift_name: str):
    """Границы смены в UTC: [начало, конец)."""
    try:
        day = datetime.strptime(shift_date, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None

    if shift_name == "day":
        start = day.replace(hour=DAY_START, minute=0, second=0, microsecond=0)
        end = day.replace(hour=DAY_END, minute=0, second=0, microsecond=0)
    else:
        start = day.replace(hour=DAY_END, minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=24 - DAY_END + DAY_START)

    return (
        start.replace(tzinfo=WARSAW_TZ).astimezone(timezone.utc),
        end.replace(tzinfo=WARSAW_TZ).astimezone(timezone.utc),
    )


def downtime_intervals():
    """
    Интервалы простоя Offline→Online из журнала смен статуса.

    None — сбой чтения (это не «простоев не было»). Незакрытые интервалы
    (робот до сих пор в офлайне) не возвращаются: их простой ещё идёт.
    """
    rows = rest_get(
        HISTORY_TABLE,
        params={
            "select": "created_at,robot_number,new_status,type_problem",
            "order": "created_at.asc",
            "limit": str(HISTORY_SCAN_LIMIT),
        },
    )

    if rows is None:
        logger.error("Не удалось прочитать журнал смен статуса для MTTR")
        return None

    opened = {}
    intervals = []

    for row in rows:
        number = row.get("robot_number")
        moment = parse_iso(row.get("created_at"))

        if number is None or moment is None:
            continue

        status = row.get("new_status")

        if status == robot_status.OFFLINE:
            opened[number] = (moment, row.get("type_problem"))
        elif status == robot_status.ONLINE and number in opened:
            started, type_problem = opened.pop(number)

            if moment >= started:
                intervals.append({
                    "robot": number,
                    "start": started,
                    "end": moment,
                    "seconds": (moment - started).total_seconds(),
                    "type_problem": type_problem,
                })

    return intervals


def downtime_metrics(shift_date: str, shift_name: str) -> dict:
    """
    Простой роботов за смену: MTTR и самый долгий простой.

    Считаем по интервалам Offline→Online, которые закрылись внутри смены
    (робот вернулся в работу на этой смене). Источник — тот же журнал
    `change_status_robots`, из которого строит отчёт веб-приложение, поэтому
    цифры не разъезжаются. Роботы, которые всё ещё в офлайне, в MTTR смены
    не попадают: их простой ещё не закончился.
    """
    window = shift_window(shift_date, shift_name)

    if window is None:
        return {"available": False}

    start, end = window

    intervals = downtime_intervals()

    if intervals is None:
        return {"available": False}

    # MTTR считаем по ремонтам, которые начались И закончились в этой смене:
    # иначе закрытие старой заявки (робот стоял месяцами) раздувает среднее
    # и делает цифру несравнимой между сменами.
    recovered = [
        item for item in intervals
        if start <= item["start"] and item["end"] < end
    ]

    # Отдельно — заявки, которые закрыли в эту смену, но открыли раньше.
    legacy = [
        item for item in intervals
        if item["start"] < start and start <= item["end"] < end
    ]

    if not recovered:
        return {
            "available": True,
            "count": 0,
            "legacy": len(legacy),
            "mttr_seconds": None,
            "longest": None,
        }

    durations = [item["seconds"] for item in recovered]
    longest = max(recovered, key=lambda item: item["seconds"])

    return {
        "available": True,
        "count": len(recovered),
        "legacy": len(legacy),
        "mttr_seconds": int(sum(durations) / len(durations)),
        "longest": {
            "robot": longest.get("robot"),
            "seconds": int(longest["seconds"]),
            "type_problem": longest.get("type_problem"),
        },
    }


def downtime_line(metrics: dict) -> str:
    """Строка простоя для отчёта (или None, если данных нет)."""
    downtime = (metrics or {}).get("downtime") or {}

    if not downtime.get("available"):
        return None

    if not downtime.get("count"):
        if downtime.get("legacy"):
            return (
                f"🛠 {downtime['legacy']} older repair(s) closed this shift "
                "(started earlier)"
            )

        return "🛠 MTTR: no robot came back online this shift"

    line = (
        f"🛠 MTTR {format_duration(downtime['mttr_seconds'] // 60)} "
        f"over {downtime['count']} repairs"
    )

    longest = downtime.get("longest") or {}

    if longest.get("robot") is not None:
        line += (
            f" · longest #{longest['robot']} "
            f"{format_duration(longest['seconds'] // 60)}"
        )

    return line


def shift_metrics(shift_date: str, shift_name: str) -> dict:
    """Метрики смены + сравнение с предыдущей сменой."""
    data = shift_report_data(
        shift_date,
        shift_name,
        maintenance_threshold=MAINTENANCE_THRESHOLD,
    )

    if data is None:
        return None

    previous_key = previous_shift(shift_date, shift_name)
    previous = None

    if previous_key:
        previous = shift_report_data(
            previous_key[0],
            previous_key[1],
            maintenance_threshold=MAINTENANCE_THRESHOLD,
        )

    data["previous"] = {
        "date": previous_key[0] if previous_key else None,
        "shift": previous_key[1] if previous_key else None,
        "total": previous["total"] if previous else None,
    }
    data["delta"] = data["total"] - previous["total"] if previous else None

    # Простой — отдельный источник; его сбой не должен ломать отчёт.
    try:
        data["downtime"] = downtime_metrics(shift_date, shift_name)
    except Exception:
        logger.exception(
            "Не удалось посчитать простой за %s/%s", shift_date, shift_name
        )
        data["downtime"] = {"available": False}

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

    if metrics is None:
        return (
            "⚠️ Can't read the shift data right now (database error). "
            "Please try again in a minute."
        )

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
        f"vs previous shift "
        f"({_pretty_date(previous['date']) if previous['date'] else 'n/a'} "
        f"{previous['shift'] or ''}) {format_delta(metrics['delta'])}",
        *([downtime_line(metrics)] if downtime_line(metrics) else []),
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


def build_shift_card(shift_date: str, shift_name: str, metrics: dict = None):
    """Интерактивная карточка Lark. None — данных нет (ошибка БД)."""
    metrics = metrics or shift_metrics(shift_date, shift_name)

    if metrics is None:
        return None

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
                f"({_pretty_date(previous['date']) if previous['date'] else 'n/a'} "
                f"{previous['shift'] or ''}): "
                f"**{format_delta(metrics['delta'])}**"
            ),
        },
    })

    repair_line = downtime_line(metrics)

    if repair_line:
        card["elements"].append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": repair_line},
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

def report_marker_name(shift_date: str, shift_name: str) -> str:
    return f"{shift_date}-{shift_name}.json"


def report_already_sent(shift_date: str, shift_name: str) -> bool:
    """True — отчёт за эту смену уже уходил (маркер в Supabase Storage)."""
    marker = download_json(
        REPORT_BUCKET,
        report_marker_name(shift_date, shift_name),
    )

    return bool(marker)


def mark_report_sent(shift_date: str, shift_name: str) -> bool:
    """Ставит маркер, что отчёт за смену отправлен."""
    return upload_json(
        REPORT_BUCKET,
        report_marker_name(shift_date, shift_name),
        {
            "shift_date": shift_date,
            "shift_name": shift_name,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def send_shift_report(shift_date: str, shift_name: str, force: bool = False):
    """
    Отправляет отчёт за смену в целевую группу.

    Сначала карточкой, при отказе — текстом. Повторная отправка за ту же
    смену блокируется маркером: перезапуск бота внутри окна отчёта не должен
    прислать дубль.
    """
    if not force and report_already_sent(shift_date, shift_name):
        logger.warning(
            "Отчёт за %s/%s уже отправлялся — пропускаю, чтобы не дублировать",
            shift_date,
            shift_name,
        )
        return {"skipped": True, "reason": "already-sent"}

    metrics = shift_metrics(shift_date, shift_name)

    if metrics is None:
        logger.error(
            "Отчёт за %s/%s не отправляю: не удалось прочитать данные смены",
            shift_date,
            shift_name,
        )
        return {"skipped": True, "reason": "db-error"}

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
        mark_report_sent(shift_date, shift_name)
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

    if hook_ok(result):
        mark_report_sent(shift_date, shift_name)

    return result


# ============================================================
# SCHEDULER
# ============================================================

def _report_sent_ok(result) -> bool:
    """Считать ли отчёт доставленным (для отметки «уже отправлен»)."""
    if isinstance(result, dict) and result.get("skipped"):
        # Маркер «уже отправляли» — повторять не нужно;
        # ошибка чтения данных — нужно (попробуем в следующем цикле).
        return result.get("reason") == "already-sent"

    return hook_ok(result)


def _scheduler_loop():
    """Фоновый цикл: проверяет время и шлёт отчёт раз за смену."""
    sent = set()

    while True:
        try:
            now = _now()
            reportable = _get_reportable_shift(now)

            if reportable and reportable not in sent:
                result = send_shift_report(*reportable)

                # Отмечаем смену отправленной только при успехе: иначе
                # неудачная отправка больше никогда не повторится.
                if _report_sent_ok(result):
                    sent.add(reportable)
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
        result = send_shift_report(shift_date, shift_name, force=True)
        print("\n--- отправлено в Lark ---")
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
