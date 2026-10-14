"""
Очередь устройств, которых нет в справочнике (`telegram_devices_to_add`).

Сотрудник присылает ошибку с номером робота, которого нет в
`robots_maintenance_list`. Записать исключение нельзя (нет robot_id), но и
терять его нельзя — ошибка уходит в Lark, а номер ставится в очередь
очереди, чтобы администратор добавил устройство через `/dashboard/devices-admin`.

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


# **Новая очередь.** `telegram_devices_to_add` — все категории устройств, а не
# только роботы. Пишет в неё приём оборудования (`equipment_intake`), поэтому
# бот и админская страница `/dashboard/devices-admin` смотрят в одно место.
#
# **Почему перешли 11.10.2026.** Старая `robots_to_add` **перестала
# наполняться** 04.10.2026 (последняя запись — того дня), а в новой с тех пор
# **308**. Дайджест в Lark продолжал показывать счётчик старой: сообщал «195
# заявок», хотя реально их **297**. Люди видели неверное число.
QUEUE_TABLE = "telegram_devices_to_add"
ROBOTS_TABLE = "robots_maintenance_list"
#: Поле с номером устройства: в новой таблице имя другое.
NUMBER_FIELD = "device_number"
#: Открытая заявка: в новой таблице статус текстовый, а не boolean.
OPEN_FILTER = "eq.pending"
#: Что писать при закрытии заявки.
CLOSED_VALUE = {"status": "accepted"}

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
            # **Имена колонок новой таблицы.** В `robots_to_add` было
            # `robot_number` и `employee_id`; здесь — `device_number`, а автора
            # заявки несёт `employee` (текстом) и `telegram_user_id`.
            "select": f"id,{NUMBER_FIELD},created_at,warehouse,category",
            # Статус текстовый: открытая заявка — `pending`.
            "status": OPEN_FILTER,
            "order": "id.asc",
            "limit": str(limit or QUEUE_SCAN_LIMIT),
        },
    )


def known_robot_numbers(numbers, warehouse: str = None):
    """
    Подмножество номеров, которые есть в справочнике ЭТОГО склада.

    Номера между складами повторяются, поэтому искать «где-нибудь» нельзя:
    заявка склада GLP-C не должна закрываться из-за робота с тем же номером
    на SMALL-P3.

    None — сбой чтения: закрывать очередь по неполным данным нельзя.
    """
    found = set()

    for chunk in _chunks(list(numbers), KNOWN_CHUNK):
        params = {
            "select": "robot_number",
            "robot_number": f"in.({','.join(chunk)})",
        }

        if warehouse:
            params["warehouse"] = f"eq.{warehouse}"

        rows = rest_get(ROBOTS_TABLE, params=params)

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

    # Группируем по складу заявки: робот ищется только на своём складе.
    by_warehouse = {}

    for row in rows:
        numbers = _digits([row.get(NUMBER_FIELD)])

        if not numbers:
            continue

        title = str(row.get("warehouse") or "").strip()
        by_warehouse.setdefault(title, set()).add(numbers[0])

    checked = 0
    closed = 0

    for title, group in by_warehouse.items():
        numbers = sorted(group)
        checked += len(numbers)

        known = known_robot_numbers(numbers, title or None)

        if known is None:
            return {"checked": checked, "closed": closed, "error": True}

        to_close = [number for number in numbers if number in known]

        for chunk in _chunks(to_close, IN_CHUNK):
            params = {
                "status": OPEN_FILTER,
                NUMBER_FIELD: f"in.({','.join(chunk)})",
            }

            if title:
                params["warehouse"] = f"eq.{title}"

            # **`accepted`, а не `true`.** В новой таблице статус текстовый;
            # `true` PostgREST отверг бы, и очередь не закрывалась бы вовсе.
            patched = rest_patch(QUEUE_TABLE, params=params, payload=CLOSED_VALUE)

            if patched is None:
                logger.error(
                    "Не удалось закрыть заявки очереди (%s номеров в пачке)",
                    len(chunk),
                )
                return {
                    "checked": checked,
                    "closed": closed,
                    "error": True,
                }

            closed += len(patched)

    if closed:
        logger.info(
            "Очередь роботов: закрыто %s заявок (проверено %s)",
            closed,
            checked,
        )

    return {"checked": checked, "closed": closed, "error": False}


def queue_stats(days: int = 1, warehouse: str = None) -> dict:
    """
    Сводка по очереди для дайджеста: открыто / новых за сутки / топ номеров.

    None-поля означают сбой чтения, а не ноль.
    """
    # 🔴 **Фильтр по текстовому статусу `pending`, а не по булеву `is.false`.**
    #
    # Дефект найден аудитом 14.10.2026. `status` в этой таблице — **текст**
    # (`pending`), поэтому запрос `status=is.false` PostgREST отвергал с
    # `400 Bad Request`. `rest_count` в этом случае возвращает `None`, а не
    # ноль, и `queue_stats` отдавал «сбой чтения».
    #
    # **Последствие было двойным.** Дайджест считает `open is None` признаком
    # сбоя и **отбрасывает секцию целиком** — вместе с ней исчезала и вторая
    # часть: роботы, залипшие в офлайне. Команда `/digest` отвечала
    # «✅ Nothing to report», хотя в очереди стояло **138 заявок по GLP-C** и
    # **6 по SMALL-P3**, а в офлайне — роботы с простоем 54–61 день.
    #
    # Правильная константа объявлена в этом же модуле (`OPEN_FILTER`) и уже
    # используется двумя другими запросами — то есть значение было известно, а
    # здесь стояло выдуманное.
    base = {"status": OPEN_FILTER}

    if warehouse:
        base["warehouse"] = f"eq.{warehouse}"

    open_count = rest_count(QUEUE_TABLE, base)

    since = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat()

    recent_count = rest_count(
        QUEUE_TABLE,
        {**base, "created_at": f"gte.{since}"},
    )

    # Последние открытые заявки (не только «за сутки»): дайджест показывает
    # именно то, что человеку нужно разобрать.
    recent = rest_get(
        QUEUE_TABLE,
        params={
            "select": f"{NUMBER_FIELD},created_at,category",
            **base,
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
