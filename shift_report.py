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


# Сколько строк показывать в топах **автоматического отчёта**.
#
# Отчёт приходит в группу по расписанию (06:00 и 18:00), поэтому он короткий:
# его читают на ходу, и длинный список роботов мешал бы видеть главное.
REPORT_TOP = _env_int("REPORT_TOP", 5)

# Сколько строк показывать в топе роботов **по команде `/stats`**.
#
# Владелец 07.10.2026: «на команду /stats сделай больше роботов в топе 10-20».
# Разница по смыслу: автоматический отчёт — сводка на бегу, а `/stats` человек
# запрашивает **специально**, чтобы разобраться, и короткий список его не
# устраивает. Поэтому лимиты разные, а не один на оба случая.
#
# Почему роботы, а не все топы: у типов ошибок и у сотрудников список обычно
# короче, и растягивать их нет нужды. Роботов же на складе сотни, и «+N more»
# в отчёте ничего не говорит о том, кто ломается чаще всех.
STATS_ROBOT_TOP = _env_int("STATS_ROBOT_TOP", 15)

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


def _gaps_line(metrics: dict) -> str:
    """
    Сколько записей смены осталось без «что сделано» и без времени решения.

    🔴 **Зачем это в отчёте.** Аудит 14.10.2026: за смену 14.10 day по GLP-C
    **56 из 121** записи (46 %) были без `raw_recovery`, и **10** — без времени
    решения. Отчёт об этом молчал, поэтому незакрытая половина смены выглядела
    как обычные строки, а «Booked repair time» считал по ним минуты.

    Владельцу эта цифра нужна не для контроля ради контроля: она показывает,
    **где заготовки не заполнены**, и после их заполнения в отчётах появится
    «что сделано».

    Пустая строка, когда терять нечего — не засоряем карточку.
    """
    total = metrics.get("total") or 0

    if not total:
        return ""

    no_recovery = metrics.get("no_recovery") or 0
    no_time = metrics.get("no_solving_time") or 0

    if not no_recovery and not no_time:
        return ""

    parts = []

    if no_recovery:
        parts.append(
            f"{no_recovery} without «what was done» "
            f"({100 * no_recovery // total}%)"
        )

    if no_time:
        parts.append(f"{no_time} without solving time")

    return "📝 Not filled in: " + " · ".join(parts)


def _zone_line(metrics: dict) -> str:
    """
    Разбивка смены по зонам — из `zone` (legacy-имя `issue_warehouse`).

    🔴 **Зачем.** У SMALL-P3 зона выводится **из устройства** (`D`/`E`) и
    используется во всех остальных отчётах, а в сменном её не было вовсе: по
    GLP-C 121 из 121 строки — `C2`, по SMALL-P3 `E` 24 и `D` 7. Без разбивки
    отчёт по складу не отвечает на вопрос «в какой части склада было хуже».
    """
    zones = metrics.get("zones") or {}

    if not zones:
        return ""

    parts = [
        f"{zone} {count}"
        for zone, count in sorted(zones.items(), key=lambda kv: -kv[1])
    ]

    return "📍 Zones: " + " · ".join(parts)


def _unassigned_line(metrics: dict) -> str:
    """
    Записи без номера робота — отдельной строкой, а не «роботом None».

    🔴 **Что было.** `str(None)` давал строку `"None"`, и она попадала в топ
    роботов **как робот**. На смене 04.10.2026 ночь таких записей было
    **74 из 198 (37 %)**: карточка печатала «Top robots: None (74)» и звала
    «робота None» на обслуживание, а читатель не видел главного — у 37 % смены
    робот не указан.

    Пустая строка, когда таких записей нет.
    """
    unassigned = metrics.get("unassigned") or 0

    if not unassigned:
        return ""

    total = metrics.get("total") or 0
    share = f" ({100 * unassigned // total}%)" if total else ""

    return f"❔ No robot in the record: {unassigned}{share}"


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


def downtime_intervals(warehouse: str = None):
    """
    Интервалы простоя Offline→Online из журнала смен статуса.

    None — сбой чтения (это не «простоев не было»). Незакрытые интервалы
    (робот до сих пор в офлайне) не возвращаются: их простой ещё идёт.
    """
    params = {
        "select": "created_at,robot_number,new_status,type_problem",
        "order": "created_at.asc",
        "limit": str(HISTORY_SCAN_LIMIT),
    }

    if warehouse:
        params["warehouse"] = f"eq.{warehouse}"

    rows = rest_get(HISTORY_TABLE, params=params)

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


def still_offline(warehouse: str = None):
    """
    Роботы, которые **прямо сейчас** в офлайне, с началом простоя.

    🔴 **Почему отдельной функцией, а не внутри `downtime_intervals`.**
    `downtime_intervals` возвращает только **закрытые** пары Offline→Online:
    интервал без закрытия не становится записью списка, потому что у него нет
    длительности. Менять её формат значило бы сломать три вызывающих места
    (`analytics.py` — два, `shift_report.py` — один), которым длительность
    нужна именно посчитанной.

    Поэтому незакрытые читаются **своей** функцией. Это не дублирование: запрос
    тот же, смысл другой — «стоит сейчас» против «стоял тогда».

    **Зачем это в отчёте.** Аудит 14.10.2026: в офлайне висели **7 роботов**,
    самый старый — **с 18.04.2026 (179 суток)**, ещё три — с 07–09.10. Сменный
    рапорт об этом молчал полностью, а `MTTR` в тех же сменах печатал «no robot
    came back online». Читатель видел «всё спокойно», хотя часть парка стоит
    неделями.

    `None` — сбой чтения; пустой список — «все на линии».
    """
    # 🔴 **Источник — живая таблица `equipment`, а НЕ журнал переходов.**
    #
    # Дефект найден при сверке 15.10.2026: журнал `change_status_robots` и живая
    # таблица давали **разные наборы роботов, без единого общего**:
    #
    # | Источник | Роботы в офлайне |
    # |---|---|
    # | журнал (последняя запись 09.10) | 115, 3742, 3694, 3587, 3773, 3628 |
    # | `equipment` (живой статус) | **2627, 5138, 7449, 3511** |
    #
    # Разгадка — в привязке к складу: `#115` журнал показывал как GLP-C, а на
    # самом деле он **SMALL-P3** (`warehouse='SMALL-P3'`, зона `D`). То есть
    # журнал не только устарел, но и **путал склады**: он писался, когда склад
    # ещё не проставлялся.
    #
    # **Почему живая таблица верна.** Её наполняет приложение склада и синк с
    # ESS (`ess_synced_at`), статус там **канонический** (`online`/`offline`),
    # а `status_raw` хранит исходную строку. Проверено: те же 4 робота, что и в
    # `robots_maintenance_list`, — независимое подтверждение.
    #
    # **Время простоя — по `updated_at`.** Момента перехода в этом источнике нет
    # (он не журнал), поэтому возраст считается от последнего изменения строки.
    # Это **оценка снизу**: статус обновлялся не позже, чем робот встал, значит
    # «стоит не меньше N дней». В строке отчёта так и написано — «oldest», без
    # утверждения точной даты.
    key = _warehouse_key(warehouse) if warehouse else None
    params = {
        "select": "equipment_code,status,status_raw,problem_note,updated_at,warehouse",
        "category": "eq.robot",
        "status": "eq.offline",
        "limit": "500",
    }

    title = None

    if warehouse:
        # `equipment.warehouse` хранит **название** склада, а не id: сравнение с
        # числом вернуло бы пусто и «все на линии» — ложное спокойствие.
        title = str(warehouse).strip()
        params["warehouse"] = f"eq.{title}"

    rows = rest_get("equipment", params=params)

    if rows is None:
        logger.error("Не удалось прочитать офлайн-роботов из equipment")
        return None

    now = _now()
    still = []

    for row in rows:
        number = row.get("equipment_code")
        moment = parse_iso(row.get("updated_at"))

        if number in (None, ""):
            continue

        problem = str(row.get("problem_note") or "").strip() or str(
            row.get("status_raw") or ""
        ).strip()

        hours = (
            max((now - moment).total_seconds() / 3600, 0.0) if moment else 0.0
        )

        still.append(
            {
                "robot": number,
                "start": moment,
                "hours": hours,
                "type_problem": problem,
            }
        )

    still.sort(key=lambda item: -item["hours"])

    return still


def _still_offline_line(warehouse: str = None) -> str:
    """
    Строка «🔌 Still offline: N · oldest #X (Nd)» для отчёта.

    Пустая строка, когда все на линии или чтение не удалось: молчание здесь
    лучше выдуманного нуля.
    """
    robots = still_offline(warehouse)

    if not robots:
        return ""

    oldest = robots[0]
    days = int(oldest["hours"] // 24)

    age = f"{days}d" if days else f"{int(oldest['hours'])}h"

    return (
        f"🔌 Still offline: {len(robots)} · "
        f"oldest #{oldest['robot']} ({age})"
    )


def downtime_metrics(shift_date: str, shift_name: str, warehouse: str = None) -> dict:
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

    intervals = downtime_intervals(warehouse)

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


def _journal_last_entry() -> str:
    """
    Дата последней записи в журнале смен статуса — `ДД.ММ.ГГГГ` или пустая строка.

    🔴 **Зачем это в отчёте.** Строка про MTTR должна называть **причину**, по
    которой цифры нет. Без даты читатель не может отличить «журнал сломан» от
    «смена была спокойной», а это разные вещи и лечатся по-разному.

    `None`/`""` при сбое чтения — тогда отчёт скажет просто «журнал пуст», не
    выдумывая дату.
    """
    rows = rest_get(
        HISTORY_TABLE,
        params={"select": "created_at", "order": "created_at.desc", "limit": "1"},
    )

    if not rows:
        return ""

    moment = parse_iso(rows[0].get("created_at"))

    return _pretty_date_from(moment) if moment else ""


def _pretty_date_from(moment) -> str:
    """`ДД.ММ.ГГГГ` из даты — тем же видом, что и в заголовке отчёта."""
    try:
        return moment.astimezone(WARSAW_TZ).strftime("%d.%m.%Y")
    except Exception:
        return ""


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

        # 🔴 **«Никто не вернулся» заменено на честную причину.**
        #
        # Аудит 15.10.2026: строка «MTTR: no robot came back online this shift»
        # **вводила в заблуждение**. Она читается как «роботы не ломались», а на
        # самом деле означает «измерить нечем»: журнал смен статуса
        # (`change_status_robots`) не наполняется — последняя запись 09.10.2026.
        #
        # Проверено, что другого **измеренного** источника нет:
        #
        # * `equipment` хранит только текущий статус и время его изменения —
        #   момента перехода в офлайн там нет, поэтому начало ремонта неизвестно;
        # * `exceptions.end_time` — это `start_time + справочное время заготовки`
        #   (сходится в 99.99 % строк), то есть **оценка**, а не факт.
        #
        # Поэтому вместо ложного спокойствия печатается факт с датой последней
        # записи и подсказка, чем это лечится (команды `/offline` и `/online`
        # работают и пишут в журнал — ими просто не пользуются).
        since = _journal_last_entry()

        if since:
            return (
                f"🛠 Repair time not measurable: the status journal has no "
                f"entries since {since} (use /offline and /online)"
            )

        return "🛠 Repair time not measurable: the status journal is empty"

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


def shift_metrics(shift_date: str, shift_name: str, warehouse: str = None) -> dict:
    """Метрики смены + сравнение с предыдущей сменой."""
    warehouse = warehouse or WAREHOUSE

    data = shift_report_data(
        shift_date,
        shift_name,
        warehouse=warehouse,
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
            warehouse=warehouse,
            maintenance_threshold=MAINTENANCE_THRESHOLD,
        )

    data["previous"] = {
        "date": previous_key[0] if previous_key else None,
        "shift": previous_key[1] if previous_key else None,
        "total": previous["total"] if previous else None,
    }
    data["delta"] = data["total"] - previous["total"] if previous else None
    data["warehouse"] = warehouse

    # Простой — отдельный источник; его сбой не должен ломать отчёт.
    try:
        data["downtime"] = downtime_metrics(shift_date, shift_name, warehouse)
    except Exception:
        logger.exception(
            "Не удалось посчитать простой за %s/%s", shift_date, shift_name
        )
        data["downtime"] = {"available": False}

    return data


def report_color(metrics: dict) -> str:
    """
    Цвет заголовка карточки: зелёный / оранжевый / красный.

    🔴 **Почему переписано.** Дефект найден аудитом 14.10.2026: карточка была
    **всегда красной** — проверено 4 смены подряд, все `red`. Причины было две:

    1. `maintenance` (порог «3+ записей на робота») срабатывал почти всегда —
       на смене 14.10 ночь это **15 роботов**, у которых 87 % ошибок решались за
       минуты. Теперь порог считает **незакрытые** ошибки (см. `shift_report_data`),
       и «обслуживание» означает именно «нужен ремонт»;
    2. порог `REPORT_WARN_TOTAL` был равен **5** — при 100–200 ошибках за смену
       он не различал ничего: всё, что выше пяти, одинаково «оранжевое».

    **Новая шкала — по доле незакрытых, а не по числу записей.** Смена с 200
    мелкими ошибками, закрытыми за минуты, — это нормальная работа, а смена с 20
    ошибками, из которых 15 висят, — тревога. Абсолютное число об этом не
    говорит: оно зависит от размера парка и загрузки склада.

    | Цвет | Когда |
    |---|---|
    | 🔴 red | есть робот с незакрытыми ошибками (порог обслуживания) |
    | 🟠 orange | незакрыто больше четверти смены либо ≥ `REPORT_WARN_TOTAL` |
    | 🟢 green | остал��ное |

    Порог `REPORT_WARN_TOTAL` остался и теперь работает как «абсолютный пол»:
    маленькая смена с несколькими незакрытыми тоже заслуживает внимания.
    """
    total = metrics.get("total") or 0

    if total == 0:
        return "green"

    # Есть робот, у которого копится незакрытое, — это уже ремонт, а не шум.
    if metrics.get("maintenance"):
        return "red"

    # Доля незакрытых: сколько записей без «что сделано».
    no_recovery = metrics.get("no_recovery") or 0
    share = no_recovery / total if total else 0.0

    if no_recovery >= REPORT_WARN_TOTAL or share >= 0.25:
        return "orange"

    return "green"


def report_title(shift_date: str, shift_name: str, warehouse: str = None) -> str:
    return (
        f"📊 Shift report · {warehouse or WAREHOUSE} · "
        f"{_pretty_date(shift_date)} · {shift_label(shift_name)}"
    )


# ============================================================
# BUILDERS
# ============================================================

def build_shift_summary(
    shift_date: str,
    shift_name: str,
    metrics: dict = None,
    warehouse: str = None,
    robot_top: int = None,
) -> str:
    """Текстовый вариант отчёта.

    `robot_top` задаёт длину топа роботов. `None` — лимит автоматического отчёта
    (`REPORT_TOP`); команда `/stats` передаёт свой (`STATS_ROBOT_TOP`), потому
    что её запрашивают специально и короткий список не устраивает.
    """
    metrics = metrics or shift_metrics(shift_date, shift_name, warehouse)

    if metrics is None:
        return (
            "⚠️ Can't read the shift data right now (database error). "
            "Please try again in a minute."
        )

    header = report_title(
        shift_date, shift_name, metrics.get("warehouse") or warehouse
    )
    total = metrics["total"]

    if not total:
        return f"{header}\n\n🎉 No exceptions this shift."

    previous = metrics["previous"]

    lines = [
        header,
        "",
        f"Total {total} exceptions · {len(metrics['robots'])} robots · "
        f"{len(metrics['employees'])} employees",
        # 🔴 **«Booked repair time», а не «Downtime».**
        #
        # Аудит 14.10.2026: это **сумма справочного `solving_time`** (по смене
        # 14.10 day — 340 минут = 50×4 + 14×6 + 7×2), а не время, когда роботы
        # стояли. Подпись «Downtime» утверждала обратное, и в той же карточке
        # ниже стояло «MTTR: no robot came back online». Настоящий простой
        # считает `downtime_line` (пары Offline→Online) — он ниже.
        f"Booked repair time {format_duration(metrics['downtime_minutes'])} · "
        f"vs previous shift "
        f"({_pretty_date(previous['date']) if previous['date'] else 'n/a'} "
        f"{previous['shift'] or ''}) {format_delta(metrics['delta'])}",
        *([downtime_line(metrics)] if downtime_line(metrics) else []),
        # Незакрытые простои: их не показывал ни один отчёт (см. `still_offline`).
        *([_still_offline_line(metrics.get("warehouse"))] if _still_offline_line(metrics.get("warehouse")) else []),
        # Новые строки — только когда есть что сказать.
        *([_unassigned_line(metrics)] if _unassigned_line(metrics) else []),
        *([_zone_line(metrics)] if _zone_line(metrics) else []),
        *([_gaps_line(metrics)] if _gaps_line(metrics) else []),
        "",
        f"⚠️ Maintenance ({MAINTENANCE_THRESHOLD}+ per shift): "
        f"{_maintenance_line(metrics['maintenance'])}",
        "",
        f"🗂 Top issues: {_issues_line(metrics['types'], total)}",
        "",
        f"👥 Top reporters: {_top_line(_sorted_counts(metrics['employees']))}",
        "",
        f"🤖 Top robots: {_top_line(_sorted_counts(metrics['robots']), limit=robot_top)}",
    ]

    return "\n".join(lines)


def build_shift_card(
    shift_date: str,
    shift_name: str,
    metrics: dict = None,
    warehouse: str = None,
):
    """Интерактивная карточка Lark. None — данных нет (ошибка БД)."""
    metrics = metrics or shift_metrics(shift_date, shift_name, warehouse)

    if metrics is None:
        return None

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": report_color(metrics),
            "title": {
                "tag": "plain_text",
                "content": report_title(
                    shift_date,
                    shift_name,
                    metrics.get("warehouse") or warehouse,
                ),
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
                # См. разбор в текстовой версии: это справочное время, а не
                # простой. Настоящий простой — строкой ниже.
                f"**Booked repair time** "
                f"{format_duration(metrics['downtime_minutes'])}\n"
                f"**vs previous shift** "
                f"({_pretty_date(previous['date']) if previous['date'] else 'n/a'} "
                f"{previous['shift'] or ''}): "
                f"**{format_delta(metrics['delta'])}**"
                + (f"\n{_unassigned_line(metrics)}" if _unassigned_line(metrics) else "")
                + (f"\n{_zone_line(metrics)}" if _zone_line(metrics) else "")
                + (f"\n{_gaps_line(metrics)}" if _gaps_line(metrics) else "")
            ),
        },
    })

    repair_line = downtime_line(metrics)

    if repair_line:
        card["elements"].append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": repair_line},
        })

    # 🔴 **Роботы, которые стоят ПРЯМО СЕЙЧАС.**
    #
    # Аудит 14.10.2026: в офлайне 7 роботов, самый старый — с 18.04.2026
    # (**179 суток**). Ни сменный отчёт, ни карточка об этом не сообщали: в тех
    # же сменах MTTR печатал «no robot came back online», и читатель видел
    # «всё спокойно», хотя часть парка стоит неделями.
    still_line = _still_offline_line(metrics.get("warehouse"))

    if still_line:
        card["elements"].append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": still_line},
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

def report_marker_name(
    shift_date: str,
    shift_name: str,
    warehouse: str = None,
) -> str:
    """
    Имя маркера «отчёт отправлен».

    🔴 **Склад входит в имя** — иначе второй склад молча считался бы отправленным.

    Дефект, найденный аудитом 14.10.2026: у каждого склада свой Lark-хук для
    ошибок (`LARK_HOOK_ERROR_SP3`), а **сменный рапорт всегда уходил по GLP-C**.
    Причина была в вызове (`shift_metrics(date, shift, warehouse=None)` →
    `WAREHOUSE`), но при добавлении второго склада маркер стал бы **второй**
    ловушкой: имя `2026-10-14-night.json` одно на оба склада, и отчёт по SMALL-P3
    был бы отброшен как «уже отправленный» после GLP-C.

    Склад в имени делает маркеры независимыми: у каждой смены каждого склада он
    свой. Имя без склада сохранено (`warehouse=None`) — так читаются маркеры,
    записанные до этой правки, и первый запуск не пришлёт дубль за прошедшие смены.
    """
    if warehouse:
        # `cfold` и пробелы → дефис: имя файла в Storage не должно зависеть от
        # регистра и не должно содержать пробелов.
        key = str(warehouse).strip().casefold().replace(" ", "-")

        return f"{shift_date}-{shift_name}-{key}.json"

    return f"{shift_date}-{shift_name}.json"


def report_already_sent(
    shift_date: str,
    shift_name: str,
    warehouse: str = None,
) -> bool:
    """True — отчёт за эту смену **этого склада** уже уходил."""
    marker = download_json(
        REPORT_BUCKET,
        report_marker_name(shift_date, shift_name, warehouse),
    )

    if marker:
        return True

    # 🔴 **Старый маркер без склада тоже учитывается — но только для склада по
    # умолчанию.**
    #
    # До этой правки маркер был один на смену и относился к GLP-C. Если его не
    # проверять, первый запуск после деплоя пришлёт дубль за уже отправленную
    # смену. Для второго склада старый маркер, наоборот, **не** признак: его
    # отчёта там не было вовсе, и промолчать значило бы потерять смену.
    if warehouse and _warehouse_key(warehouse) == _warehouse_key(WAREHOUSE):
        return bool(
            download_json(
                REPORT_BUCKET,
                report_marker_name(shift_date, shift_name),
            )
        )

    return False


def mark_report_sent(
    shift_date: str,
    shift_name: str,
    warehouse: str = None,
) -> bool:
    """Ставит маркер, что отчёт за смену этого склада отправлен."""
    return upload_json(
        REPORT_BUCKET,
        report_marker_name(shift_date, shift_name, warehouse),
        {
            "shift_date": shift_date,
            "shift_name": shift_name,
            "warehouse": warehouse or WAREHOUSE,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _warehouse_key(title) -> str:
    """Склад в сравнимом виде: без регистра и лишних пробелов."""
    return str(title or "").strip().casefold()


def send_shift_report(
    shift_date: str,
    shift_name: str,
    force: bool = False,
    warehouse: str = None,
):
    """
    Отправляет отчёт за смену **одного склада** в его группу.

    Сначала карточкой, при отказе — текстом. Повторная отправка за ту же
    смену блокируется маркером: перезапуск бота внутри окна отчёта не должен
    прислать дубль.

    🔴 **Склад теперь обязателен по смыслу** (аудит 14.10.2026). Раньше отчёт
    всегда считался по `WAREHOUSE` (GLP-C): вызов `shift_metrics(date, shift)` без
    склада подставлял его по умолчанию. Второй склад — **SMALL-P3** — не
    отчитывался в Lark вовсе, хотя ошибки туда уходят своим хуком
    (`LARK_HOOK_ERROR_SP3`). За смену 14.10 ночь это **7 записей**, которых
    группа не видела.

    `warehouse=None` оставлен для совместимости: он означает склад по умолчанию
    и общий вебхук — то есть прежнее поведение.
    """
    title = warehouse or WAREHOUSE

    if not force and report_already_sent(shift_date, shift_name, title):
        logger.warning(
            "Отчёт за %s/%s (%s) уже отправлялся — пропускаю, чтобы не дублировать",
            shift_date,
            shift_name,
            title,
        )
        return {"skipped": True, "reason": "already-sent"}

    metrics = shift_metrics(shift_date, shift_name, title)

    if metrics is None:
        logger.error(
            "Отчёт за %s/%s (%s) не отправляю: не удалось прочитать данные смены",
            shift_date,
            shift_name,
            title,
        )
        return {"skipped": True, "reason": "db-error"}

    logger.info(
        "Sending shift report: date=%s shift=%s warehouse=%s total=%s downtime=%sm",
        shift_date,
        shift_name,
        title,
        metrics["total"],
        metrics["downtime_minutes"],
    )

    # 🔴 **Хук берётся по складу, как у карточек ошибок.**
    #
    # Общий `TARGET_HOOK_URL` остаётся запасным: если для склада свой вебхук не
    # задан, отчёт уйдёт в общий — молчание было бы хуже.
    import lark_hooks

    target = lark_hooks.hook("status", title)

    card = build_shift_card(shift_date, shift_name, metrics, title)
    result = send_card_via_hook(target, card)

    if hook_ok(result):
        logger.info(
            "Shift report sent as card: %s/%s (%s)", shift_date, shift_name, title
        )
        mark_report_sent(shift_date, shift_name, title)
        return result

    logger.warning("Card rejected (%s) — отправляю текстовый отчёт", result)

    text = build_shift_summary(shift_date, shift_name, metrics)
    result = send_text_via_hook(target, text)

    logger.info(
        "Shift report sent as text: %s/%s (%s) result=%s",
        shift_date,
        shift_name,
        title,
        result,
    )

    if hook_ok(result):
        mark_report_sent(shift_date, shift_name, title)

    return result


def send_shift_reports(shift_date: str, shift_name: str, force: bool = False):
    """
    Отчёт за смену **по каждому складу** — своему хуку и своей группе.

    🔴 **Зачем отдельная функция, а не цикл в планировщике.** Сбор и отправка —
    разные решения: планировщик должен знать только «пора», а **какие склады
    есть** и как не задвоить отправку — дело отчёта. Плюс сбой одного склада не
    должен мешать второму: здесь они независимы так же, как записи в базе и
    карточки в Lark.

    Возвращается карта `{склад: результат}` — по ней планировщик решает,
    отметить ли смену отправленной (см. `_report_sent_ok`).
    """
    from warehouses import WAREHOUSES

    titles = list(WAREHOUSES.values()) or [WAREHOUSE]
    results = {}

    for title in titles:
        try:
            results[title] = send_shift_report(
                shift_date, shift_name, force=force, warehouse=title
            )
        except Exception:
            # Сбой одного склада не отменяет отчёт по другому.
            logger.exception(
                "Отчёт за %s/%s (%s) не отправлен", shift_date, shift_name, title
            )
            results[title] = {"skipped": True, "reason": "error"}

    return results


# ============================================================
# SCHEDULER
# ============================================================

def _report_sent_ok(result) -> bool:
    """
    Считать ли отчёт доставленным (для отметки «уже отправлен»).

    🔴 **Понимает и карту складов.** `send_shift_reports` возвращает
    `{склад: результат}`, и смена считается отправленной, только если **по
    каждому** складу отчёт либо доставлен, либо уже был отправлен раньше.
    Иначе сбой одного склада навсегда закрыл бы смену для второго.
    """
    if not isinstance(result, dict):
        return False

    if result.get("skipped"):
        # Маркер «уже отправляли» — повторять не нужно;
        # ошибка чтения данных — нужно (попробуем в следующем цикле).
        return result.get("reason") == "already-sent"

    # 🔴 **Карта складов отличается от ответа вебхука по ФОРМЕ, а не по
    # отсутствию `skipped`.**
    #
    # Здесь я ошибся с первого раза: признаком карты было `"skipped" not in
    # result`, и **успешный ответ вебхука** (`{"code": 0}`) попадал в ветку карты.
    # Дальше он перебирал свои же ключи (`code`, `msg`), `hook_ok` на них падал, и
    # функция возвращала `False` — то есть **успешная отправка считалась
    # неуспешной**. Планировщик не отметил бы смену отправленной и присылал бы
    # отчёт заново каждый цикл.
    #
    # Признак карты — значения-словари: у вебхука их не бывает.
    values = list(result.values())

    if values and all(isinstance(item, dict) for item in values):
        return all(_report_sent_ok(item) for item in values)

    return hook_ok(result)


def _scheduler_loop():
    """Фоновый цикл: проверяет время и шлёт отчёт раз за смену по всем складам."""
    sent = set()

    while True:
        try:
            now = _now()
            reportable = _get_reportable_shift(now)

            if reportable and reportable not in sent:
                # 🔴 **Отчёт по каждому складу** (аудит 14.10.2026): раньше
                # планировщик звал `send_shift_report` без склада, и SMALL-P3 не
                # отчитывался вовсе.
                result = send_shift_reports(*reportable)

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
    parser = argparse.ArgumentParser(description="Shift report preview")
    parser.add_argument("--date", help="shift date YYYY-MM-DD (default: current)")
    parser.add_argument("--shift", choices=("day", "night"), help="shift")
    parser.add_argument(
        "--send",
        action="store_true",
        help="send the card to the target Lark chat (preview in the group)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the card JSON instead of the text",
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
        # Отправляем **по всем складам** — как это делает планировщик. Так
        # превью проверяет ровно тот путь, что работает в проде; иначе легко
        # проверить GLP-C и не заметить, что второй склад молчит.
        result = send_shift_reports(shift_date, shift_name, force=True)
        print("\n--- sent to Lark ---")
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
