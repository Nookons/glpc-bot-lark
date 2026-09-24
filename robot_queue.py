"""
Очередь роботов, которых нет в справочнике (`robots_to_add`).

Сотрудник присылает ошибку с номером робота, которого нет в
`robots_maintenance_list`. Записать исключение нельзя (нет robot_id), но и
терять его нельзя — ошибка уходит в Lark, а номер ставится в очередь
`robots_to_add`, чтобы администратор добавил робота.

Проблема: очередь никто не разбирает. Проверка 23.09.2026 показала 282
открытых строки, из которых 140 — роботы, которые УЖЕ есть в справочнике
(их добавили позже, а строку не закрыли). Такие строки этот модуль закрывает
автоматически: раз в `QUEUE_AUTOCLOSE_INTERVAL` секунд смотрим открытые
номера и проставляем `status = true` тем, что нашлись в справочнике.

Закрытие — только в сторону «робот найден». Если номер есть на другом складе
(P3-DC-1, PNT-A, SMALL-P3), строка тоже закрывается: робот в системе есть,
вопрос к складу, а не к отсутствию записи.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from env_utils import env_bool, env_int
from sendToDataBase import rest_count, rest_get, rest_patch
from logging_config import setup_logging


logger = setup_logging(__name__)


QUEUE_TABLE = "robots_to_add"
ROBOTS_TABLE = "robots_maintenance_list"

AUTOCLOSE_ENABLED = env_bool("QUEUE_AUTOCLOSE_ENABLED", True)

# Раз в полчаса достаточно: очередь растёт на ~17 строк в сутки.
AUTOCLOSE_INTERVAL_SECONDS = env_int("QUEUE_AUTOCLOSE_INTERVAL", 1800)

# Сколько открытых строк разбираем за один проход.
QUEUE_SCAN_LIMIT = env_int("QUEUE_SCAN_LIMIT", 2000)

# Размер пачки для `in.(...)`: длинный URL PostgREST может отвергнуть.
IN_CHUNK = 100

# Верхняя граница для `in.(...)` при поиске по справочнику.
KNOWN_CHUNK = 50


def _chunks(items, size: int):
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _digits(values):
    """Оставляет только числовые номера: в очереди встречается мусор."""
    result = []

    for value in values:
        text = str(value or "").strip()

        if text.isdigit():
            result.append(text)

    return result


def open_queue_rows(limit: int = None):
    """
    Открытые строки очереди или None при сбое чтения.

    `status = false` — заявка ещё не обработана.
    """
    return rest_get(
        QUEUE_TABLE,
        params={
            "select": "id,robot_number,created_at,employee_id",
            "status": "is.false",
            "order": "id.asc",
            "limit": str(limit or QUEUE_SCAN_LIMIT),
        },
    )


def known_robot_numbers(numbers):
    """
    Подмножество номеров, которые есть в справочнике роботов (любой склад).

    None — сбой чтения: закрывать очередь по неполным данным нельзя.
    """
    found = set()

    for chunk in _chunks(list(numbers), KNOWN_CHUNK):
        rows = rest_get(
            ROBOTS_TABLE,
            params={
                "select": "robot_number",
                "robot_number": f"in.({','.join(chunk)})",
            },
        )

        if rows is None:
            logger.error("Не удалось проверить номера в справочнике роботов")
            return None

        for row in rows:
            number = row.get("robot_number")

            if number is not None:
                found.add(str(number))

    return found


def close_queued_robots() -> dict:
    """
    Закрывает заявки, роботы по которым уже есть в справочнике.

    Возвращает {"checked": n, "closed": n, "error": bool}. Ошибка чтения —
    это НЕ «нечего закрывать»: вызывающий код (лог/дайджест) должен видеть
    разницу.
    """
    rows = open_queue_rows()

    if rows is None:
        return {"checked": 0, "closed": 0, "error": True}

    numbers = sorted(set(_digits(row.get("robot_number") for row in rows)))

    if not numbers:
        return {"checked": 0, "closed": 0, "error": False}

    known = known_robot_numbers(numbers)

    if known is None:
        return {"checked": len(numbers), "closed": 0, "error": True}

    to_close = [number for number in numbers if number in known]

    if not to_close:
        return {"checked": len(numbers), "closed": 0, "error": False}

    closed = 0

    for chunk in _chunks(to_close, IN_CHUNK):
        patched = rest_patch(
            QUEUE_TABLE,
            params={
                "status": "is.false",
                "robot_number": f"in.({','.join(chunk)})",
            },
            payload={"status": True},
        )

        if patched is None:
            logger.error(
                "Не удалось закрыть заявки очереди (%s номеров в пачке)",
                len(chunk),
            )
            return {
                "checked": len(numbers),
                "closed": closed,
                "error": True,
            }

        closed += len(patched)

    if closed:
        logger.info(
            "Очередь роботов: закрыто %s заявок (проверено %s)",
            closed,
            len(numbers),
        )

    return {"checked": len(numbers), "closed": closed, "error": False}


def queue_stats(days: int = 1) -> dict:
    """
    Сводка по очереди для дайджеста: открыто / новых за сутки / топ номеров.

    None-поля означают сбой чтения, а не ноль.
    """
    open_count = rest_count(QUEUE_TABLE, {"status": "is.false"})

    since = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat()

    recent_count = rest_count(
        QUEUE_TABLE,
        {"status": "is.false", "created_at": f"gte.{since}"},
    )

    # Последние открытые заявки (не только «за сутки»): дайджест показывает
    # именно то, что человеку нужно разобрать.
    recent = rest_get(
        QUEUE_TABLE,
        params={
            "select": "robot_number,created_at,employee_id",
            "status": "is.false",
            "order": "created_at.desc",
            "limit": "200",
        },
    )

    return {
        "open": open_count,
        "new": recent_count,
        "rows": recent,
    }


def autoclose_loop(interval_seconds: int = None):
    """Периодически закрывает заявки, потерявшие смысл."""
    interval = interval_seconds or AUTOCLOSE_INTERVAL_SECONDS

    while True:
        try:
            close_queued_robots()
        except Exception:
            logger.exception("Ошибка автозакрытия очереди роботов")

        time.sleep(interval)


def start_queue_autoclose() -> threading.Thread:
    """Запускает фоновое автозакрытие очереди (только у опрашивающего)."""
    thread = threading.Thread(
        target=autoclose_loop,
        name="robot-queue-autoclose",
        daemon=True,
    )
    thread.start()

    logger.info(
        "Robot queue autoclose started (каждые %s с)",
        AUTOCLOSE_INTERVAL_SECONDS,
    )

    return thread
