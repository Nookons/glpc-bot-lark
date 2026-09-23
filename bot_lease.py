"""
Лиз «единственного опрашивающего Telegram».

Long polling допускает только один активный getUpdates, но если запущены два
инстанса (локально и на сервере), Telegram может отдать один и тот же апдейт
обоим: апдейт считается подтверждённым лишь при следующем getUpdates с
бо́льшим offset. Итог — сообщения обрабатываются дважды.

Поэтому перед стартом polling инстанс занимает строку в public.bot_leases:

  * строки нет            -> создаём, лиз наш;
  * лиз наш               -> продлеваем;
  * лиз просрочен         -> забираем (сравнение-и-замена по holder);
  * лиз у живого соседа   -> этот процесс встаёт в standby.

Лиз продлевается на каждой итерации polling. Если продлить не удалось
(лиз увели), polling останавливается — так дубли невозможны.
"""

from __future__ import annotations

import os
import socket
import time

from datetime import datetime, timedelta, timezone

from env_utils import env_int
from sendToDataBase import (
    rest_delete,
    rest_get,
    rest_patch,
    rest_post,
    table_exists,
)
from logging_config import setup_logging


logger = setup_logging(__name__)


TABLE = "bot_leases"

# Имя лиза: разные боты (GLP-C, P3, ...) не мешают друг другу.
LEASE_NAME = os.environ.get("BOT_LEASE_NAME", "glpc-bot-telegram")

# Через сколько секунд без heartbeat лиз считается брошенным.
# Небольшой TTL нужен, чтобы после деплоя новый контейнер быстро подхватил
# работу, если старый не успел отпустить лиз сам.
LEASE_TTL_SECONDS = env_int("BOT_LEASE_TTL", 90)

_table_ok = None
_table_checked_at = 0.0
_warned_no_table = False

# Если таблицы нет, проверяем её снова раз в 5 минут: миграцию могли
# применить без перезапуска бота.
_TABLE_RECHECK_SECONDS = 300


def holder_id() -> str:
    """Идентификатор этого процесса (хост + pid)."""
    try:
        host = socket.gethostname()
    except OSError:
        host = "unknown-host"

    return f"{host}-{os.getpid()}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_stale(heartbeat_at, ttl: int) -> bool:
    """Просрочен ли heartbeat соседа (нечитаемое время считаем живым)."""
    if not heartbeat_at:
        # Пустой/битый heartbeat — лиз считаем брошенным, иначе он
        # «залипнет» навсегда и опрос не переедет на живой инстанс.
        return True

    try:
        moment = datetime.fromisoformat(str(heartbeat_at).replace("Z", "+00:00"))
    except ValueError:
        return False

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    return moment < datetime.now(timezone.utc) - timedelta(seconds=ttl)


def _has_table() -> bool:
    global _table_ok, _table_checked_at

    now = time.time()

    if _table_ok is True:
        return True

    if _table_ok is False and now - _table_checked_at < _TABLE_RECHECK_SECONDS:
        return False

    _table_ok = table_exists(TABLE)
    _table_checked_at = now

    if _table_ok:
        logger.info("Лиз единственного инстанса: таблица %s найдена", TABLE)
    else:
        logger.warning(
            "Таблицы %s нет — защита от двойного запуска выключена. "
            "Выполните sql/bot_leases.sql (проверю снова через %s с).",
            TABLE,
            _TABLE_RECHECK_SECONDS,
        )

    return _table_ok


def _read_row():
    rows = rest_get(
        TABLE,
        params={"select": "*", "name": f"eq.{LEASE_NAME}", "limit": "1"},
    )

    if rows is None:
        return None

    return rows[0] if rows else {}


def check(holder: str = None) -> str:
    """
    Состояние лиза: "ok" | "lost" | "error".

    * "ok"    — лиз наш, heartbeat продлён;
    * "lost"  — лиз забрал другой инстанс или строку удалили: опрос надо
                немедленно прекратить, иначе будут дубли;
    * "error" — база недоступна (транзиентно): опрос прекращать нельзя,
                иначе бот замолчит из-за сетевого сбоя.

    Без таблицы лиза всегда "ok" (защита выключена, но работать надо).
    """
    holder = holder or holder_id()

    if not _has_table():
        return "ok"

    row = _read_row()

    if row is None:
        return "error"

    if not row:
        return "lost"

    if str(row.get("holder") or "") != holder:
        return "lost"

    renewed = rest_patch(
        TABLE,
        params={"name": f"eq.{LEASE_NAME}", "holder": f"eq.{holder}"},
        payload={"heartbeat_at": _now_iso()},
    )

    if renewed is None:
        return "error"

    # Пустой ответ = строку лиза удалили или она уже не наша.
    return "ok" if renewed else "lost"


def refresh(holder: str = None, ttl: int = None) -> bool:
    """
    Продлевает лиз. False — лиз потерян (нужно прекратить опрос).

    Транзиентная ошибка базы возвращает True: продолжать опрос безопаснее,
    чем молча остановиться.
    """
    return check(holder) != "lost"



def release(holder: str = None) -> bool:
    """
    Отпускает лиз — вызывается при аккуратном завершении (деплой, Ctrl+C).

    Тогда новый контейнер подхватывает работу сразу, а не ждёт, пока лиз
    протухнет.
    """
    holder = holder or holder_id()

    if not _has_table():
        return False

    released = rest_delete(
        TABLE,
        params={"name": f"eq.{LEASE_NAME}", "holder": f"eq.{holder}"},
    )

    if released:
        logger.info("Лиз опроса отпущен: %s", holder)
    else:
        logger.warning("Лиз опроса отпустить не удалось (holder=%s)", holder)

    return released


def acquire(holder: str = None, ttl: int = None) -> dict:
    """
    Пытается занять лиз.

    Возвращает {"acquired": bool, "status": str, "holder": str|None}.
    """
    global _warned_no_table

    holder = holder or holder_id()
    ttl = int(ttl or LEASE_TTL_SECONDS)

    if not _has_table():
        if not _warned_no_table:
            _warned_no_table = True
            logger.warning(
                "Работаю без лиза: возможны дубли, если запущен второй бот."
            )

        return {"acquired": True, "status": "no-table", "holder": holder}

    row = _read_row()

    if row is None:
        logger.error("Не удалось прочитать лиз из %s", TABLE)
        return {"acquired": False, "status": "error", "holder": None}

    if not row:
        created = rest_post(
            TABLE,
            {
                "name": LEASE_NAME,
                "holder": holder,
                "heartbeat_at": _now_iso(),
            },
        )

        if created:
            logger.info("Лиз опроса занят (создан): %s", holder)
            return {"acquired": True, "status": "acquired", "holder": holder}

        # Кто-то успел вставить строку первым — перечитываем.
        row = _read_row()

        if not row:
            return {"acquired": False, "status": "error", "holder": None}

    current_holder = str(row.get("holder") or "")

    if current_holder == holder:
        if refresh(holder, ttl):
            return {"acquired": True, "status": "acquired", "holder": holder}

        return {"acquired": False, "status": "lost", "holder": current_holder}

    if _is_stale(row.get("heartbeat_at"), ttl):
        taken = rest_patch(
            TABLE,
            params={"name": f"eq.{LEASE_NAME}", "holder": f"eq.{current_holder}"},
            payload={"holder": holder, "heartbeat_at": _now_iso()},
        )

        if taken:
            logger.warning(
                "Лиз опроса был просрочен (%s) — забираю его себе",
                current_holder,
            )
            return {"acquired": True, "status": "taken-over", "holder": holder}

        return {"acquired": False, "status": "raced", "holder": current_holder}

    return {
        "acquired": False,
        "status": "held-by-other",
        "holder": current_holder,
    }
