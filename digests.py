"""
Ежедневный дайджест обслуживания (в личку ответственным).

Два сюжета, которые иначе никто не замечает:

1. **Роботы, залипшие в офлайне.** По проверке 23.09.2026 робот #135 стоял
   в офлайне 117 дней, #123 — 25 дней: записи есть, а разбора нет.
2. **Очередь `robots_to_add`.** Сотрудники присылают номера, которых нет в
   справочнике (~17 за сутки). Автозакрытие убирает «потерявшие смысл»
   заявки, но остальные должен разобрать человек.

Дайджест уходит только тем, у кого есть Telegram-привязка:
`ROBOT_ADMIN_TELEGRAM_IDS` (явный список id) или, если он пуст, —
зарегистрированным сотрудникам с `employees.is_leader = true`.

Отправка помечается в Storage (`bot-reports/<дата>-digest.json`), поэтому
перезапуск контейнера не приводит ко второму дайджесту за день.
"""

from __future__ import annotations

import os
import threading
import time

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from env_utils import env_bool, env_int
from robot_status import OFFLINE
from sendToDataBase import WAREHOUSE, rest_get
from supabase_storage import download_json, upload_json
from time_utils import parse_iso
from logging_config import setup_logging


logger = setup_logging(__name__)


WARSAW_TZ = ZoneInfo("Europe/Warsaw")

ROBOTS_TABLE = "robots_maintenance_list"
USERS_TABLE = "telegram_users"
EMPLOYEES_TABLE = "employees"
MARKER_BUCKET = "bot-reports"

DIGEST_ENABLED = env_bool("DIGEST_ENABLED", True)

# Час (по Варшаве), после которого дайджест уходит один раз в сутки.
DIGEST_HOUR = env_int("DIGEST_HOUR", 9)

# Как часто проверять время отправки.
CHECK_INTERVAL_SECONDS = env_int("DIGEST_CHECK_INTERVAL", 60)

# Порог «залип в офлайне» и сколько строк показывать.
STALE_OFFLINE_HOURS = env_int("STALE_OFFLINE_HOURS", 24)
STALE_OFFLINE_LIMIT = env_int("STALE_OFFLINE_LIMIT", 10)

QUEUE_TOP = env_int("QUEUE_DIGEST_TOP", 5)

_sender = None
_last_sent_date = None
_recipients_count = {"count": 0}


def set_digest_sender(sender):
    """Регистрирует транспорт для личных сообщений (ставит telegram_bot)."""
    global _sender
    _sender = sender


def parse_recipients(raw: str):
    """Список user id из строки «1,2,3» (мусор молча пропускаем)."""
    ids = []

    for chunk in str(raw or "").replace(";", ",").split(","):
        chunk = chunk.strip()

        if not chunk:
            continue

        try:
            ids.append(int(chunk))
        except ValueError:
            logger.warning("ROBOT_ADMIN_TELEGRAM_IDS: %r не похоже на id", chunk)

    return sorted(set(ids))


def registered_leaders():
    """Telegram id зарегистрированных лидеров (если явный список пуст)."""
    users = rest_get(
        USERS_TABLE,
        params={"select": "telegram_id,employee_name", "limit": "500"},
    )

    if users is None:
        logger.error("Дайджест: не удалось прочитать %s", USERS_TABLE)
        return []

    leaders = rest_get(
        EMPLOYEES_TABLE,
        params={"select": "user_name", "is_leader": "is.true", "limit": "500"},
    )

    if leaders is None:
        logger.error("Дайджест: не удалось прочитать %s", EMPLOYEES_TABLE)
        return []

    names = {row.get("user_name") for row in leaders if row.get("user_name")}
    ids = []

    for row in users:
        telegram_id = row.get("telegram_id")

        if telegram_id is None:
            continue

        if row.get("employee_name") in names:
            ids.append(int(telegram_id))

    return sorted(set(ids))


def recipients():
    """Кому слать дайджест: явный список или зарегистрированные лидеры."""
    explicit = parse_recipients(os.environ.get("ROBOT_ADMIN_TELEGRAM_IDS", ""))

    if explicit:
        return explicit

    return registered_leaders()


def stale_offline_robots(hours: int = None, warehouse: str = WAREHOUSE):
    """
    Роботы склада, которые в офлайне дольше hours.

    None — сбой чтения. Список отсортирован по времени простоя (дольше всех
    сверху) и содержит готовые к печати строки.
    """
    hours = STALE_OFFLINE_HOURS if hours is None else hours
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(hours=hours)

    rows = rest_get(
        ROBOTS_TABLE,
        params={
            "select": "robot_number,status,type_problem,problem_note,updated_at,warehouse",
            "status": f"eq.{OFFLINE}",
            "warehouse": f"eq.{warehouse}",
            "order": "updated_at.asc",
            "limit": "500",
        },
    )

    if rows is None:
        logger.error("Дайджест: не удалось прочитать офлайн-роботов")
        return None

    stale = []

    for row in rows:
        moment = parse_iso(row.get("updated_at"))

        if moment is None or moment > cutoff:
            continue

        note = (row.get("problem_note") or "").strip()
        problem = (row.get("type_problem") or "").strip()

        if problem in ("", "?"):
            problem = note if note not in ("", "?") else "no reason recorded"

        offline_for = now_utc - moment

        stale.append({
            "robot": row.get("robot_number"),
            "hours": offline_for.total_seconds() / 3600,
            "days": offline_for.days,
            "problem": problem,
            "updated_at": moment,
        })

    stale.sort(key=lambda item: -item["hours"])

    return stale


def _format_age(item) -> str:
    if item["days"] >= 1:
        return f"{item['days']}d"

    return f"{int(item['hours'])}h"


def build_digest(now: datetime = None):
    """
    Текст дайджеста или None, если сообщать нечего (или база недоступна).

    Пустой дайджест не отправляем: смысл в сигнале, а не в ежедневном шуме.
    """
    now = now or datetime.now(WARSAW_TZ)

    stale = stale_offline_robots()

    if stale is None:
        return None

    # Импорт внутри функции: robot_queue→sendToDataBase, а digests уже
    # зависит от sendToDataBase — так меньше шансов на цикл импортов.
    import robot_queue

    queue = robot_queue.queue_stats()

    if queue.get("open") is None:
        return None

    lines = [f"🗂 Maintenance digest · {now.strftime('%d.%m.%Y')}"]

    if stale:
        lines.append("")
        lines.append(f"🔌 Offline longer than {STALE_OFFLINE_HOURS}h: {len(stale)}")

        for item in stale[:STALE_OFFLINE_LIMIT]:
            lines.append(
                f"  #{item['robot']} · {_format_age(item)} · {item['problem']}"
            )

        if len(stale) > STALE_OFFLINE_LIMIT:
            lines.append(f"  … and {len(stale) - STALE_OFFLINE_LIMIT} more")

    open_rows = queue.get("rows") or []

    if queue["open"]:
        lines.append("")
        lines.append(
            f"🧾 Open robot-add requests: {queue['open']} "
            f"(new in 24h: {queue.get('new')})"
        )

        for row in open_rows[:QUEUE_TOP]:
            moment = parse_iso(row.get("created_at"))
            stamp = moment.astimezone(WARSAW_TZ).strftime("%d.%m %H:%M") if moment else "?"

            lines.append(f"  #{row.get('robot_number')} · {stamp}")

        if queue["open"] > QUEUE_TOP:
            lines.append(f"  … and {queue['open'] - QUEUE_TOP} more")

    if not stale and not queue["open"]:
        return None

    return "\n".join(lines)


def _marker_name(now: datetime) -> str:
    return f"{now.strftime('%Y-%m-%d')}-digest.json"


def was_sent(now: datetime) -> bool:
    marker = download_json(MARKER_BUCKET, _marker_name(now))

    return isinstance(marker, dict) and bool(marker.get("sent_at"))


def mark_sent(now: datetime) -> bool:
    return upload_json(
        MARKER_BUCKET,
        _marker_name(now),
        {
            "sent_at": now.strftime("%d.%m.%Y %H:%M:%S"),
            "recipients": _recipients_count.get("count", 0),
        },
    )


def send_digest(now: datetime = None, force: bool = False) -> dict:
    """Собирает и рассылает дайджест. Возвращает отчёт о попытке."""
    now = now or datetime.now(WARSAW_TZ)

    if not force and was_sent(now):
        return {"sent": 0, "reason": "already sent"}

    text = build_digest(now)

    if not text:
        return {"sent": 0, "reason": "nothing to report"}

    if _sender is None:
        logger.error("Дайджест: транспорт не зарегистрирован")
        return {"sent": 0, "reason": "no sender"}

    targets = recipients()

    if not targets:
        logger.warning(
            "Дайджест: нет получателей — задайте ROBOT_ADMIN_TELEGRAM_IDS "
            "или зарегистрируйте лидеров через /reg"
        )
        return {"sent": 0, "reason": "no recipients"}

    _recipients_count["count"] = len(targets)

    sent = 0
    failed = 0

    for chat_id in targets:
        try:
            if _sender(chat_id, text):
                sent += 1
            else:
                failed += 1
        except Exception:
            failed += 1
            logger.exception("Дайджест: не удалось отправить в %s", chat_id)

    if sent:
        mark_sent(now)

    logger.info(
        "Дайджест отправлен: %s из %s (ошибок: %s)",
        sent,
        len(targets),
        failed,
    )

    return {"sent": sent, "failed": failed, "reason": "ok"}


def digest_due(now: datetime) -> bool:
    """Пора ли отправлять: после DIGEST_HOUR и ещё не сегодня."""
    global _last_sent_date

    if now.hour < DIGEST_HOUR:
        return False

    today = now.strftime("%Y-%m-%d")

    if _last_sent_date == today:
        return False

    if was_sent(now):
        # Дайджест уже ушёл (например, до перезапуска контейнера).
        _last_sent_date = today
        return False

    return True


# Причины, после которых в этот день пробовать снова не нужно: либо уже
# отправлено, либо отправлять нечего/некому.
FINAL_REASONS = ("ok", "already sent", "nothing to report", "no recipients")


def digest_loop():
    while True:
        try:
            now = datetime.now(WARSAW_TZ)

            if digest_due(now):
                result = send_digest(now)

                if result.get("reason") in FINAL_REASONS:
                    _set_last_sent(now)
        except Exception:
            logger.exception("Ошибка дайджеста обслуживания")

        time.sleep(CHECK_INTERVAL_SECONDS)


def _set_last_sent(now: datetime):
    global _last_sent_date
    _last_sent_date = now.strftime("%Y-%m-%d")


def start_digest_scheduler() -> threading.Thread:
    """Запускает ежедневный дайджест (только у опрашивающего инстанса)."""
    thread = threading.Thread(
        target=digest_loop,
        name="maintenance-digest",
        daemon=True,
    )
    thread.start()

    logger.info(
        "Maintenance digest started (после %s:00 по Варшаве, раз в сутки)",
        DIGEST_HOUR,
    )

    return thread
