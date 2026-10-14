"""
Карточка робота: статус, простой, история переходов и последние ошибки.

Нужна для команды `/robot <номер>` и для подсказки «может, вы имели в виду
#882?», когда сотрудник прислал номер, которого нет в справочнике.

Все запросы идут через те же REST-хелперы, что и остальной бот, и отличают
«данных нет» от «база недоступна»: наружу это разные ответы.
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from rapidfuzz import fuzz, process

import journal
from env_utils import env_int
from sendToDataBase import WAREHOUSE, rest_count, rest_get
from time_utils import parse_iso
from logging_config import setup_logging


logger = setup_logging(__name__)


WARSAW_TZ = ZoneInfo("Europe/Warsaw")

ROBOTS_TABLE = "robots_maintenance_list"
HISTORY_TABLE = "change_status_robots"
#: Таблица инцидентов — **единый журнал** `exceptions`.
#:
#: История вопроса: 06.10.2026 здесь стояло `"exceptions"`, но переход тогда не
#: был сделан — писателя в таблице не было (7 строк против 24 185 в
#: `exceptions_glpc`), и карточка робота не показывала ни одной ошибки. Источник
#: вернули на legacy.
#:
#: **12.10.2026 переход завершён:** история перенесена миграцией `0069`,
#: писатели переведены, поэтому источник снова `exceptions` — и теперь это
#: верно. Имя таблицы берётся из общего `journal`, чтобы карточка и остальные
#: читатели не разошлись.
EXCEPTIONS_TABLE = journal.EXCEPTIONS_TABLE
TEMPLATES_TABLE = "issue_templates"
EMPLOYEES_TABLE = "employees"

OFFLINE_STATUS = "离线 | Offline"

HISTORY_LIMIT = env_int("ROBOT_CARD_HISTORY", 5)
ERROR_WINDOW_DAYS = env_int("ROBOT_CARD_WINDOW_DAYS", 7)
LAST_ISSUES_LIMIT = 3

# Кэши, чтобы /robot не дёргал базу на каждый чих.
_CACHE_SECONDS = env_int("ROBOT_CARD_CACHE_SECONDS", 60)
_NUMBERS_CACHE_SECONDS = env_int("ROBOT_NUMBERS_CACHE_SECONDS", 600)
_TEMPLATES_CACHE_SECONDS = 600

SUGGEST_LIMIT = env_int("ROBOT_FIX_LIMIT", 3)
SUGGEST_CUTOFF = env_int("ROBOT_FIX_CUTOFF_PERCENT", 85)

_numbers_cache = {"at": 0.0, "numbers": []}
_templates_cache = {"at": 0.0, "map": {}}
_card_cache = {}


def robot_numbers(warehouse: str = WAREHOUSE):
    """
    Все номера роботов склада (для подсказок). None — сбой чтения.
    """
    now = time.time()

    if (
        _numbers_cache["numbers"]
        and now - _numbers_cache["at"] < _NUMBERS_CACHE_SECONDS
    ):
        return _numbers_cache["numbers"]

    rows = rest_get(
        ROBOTS_TABLE,
        params={
            "select": "robot_number",
            "warehouse": f"eq.{warehouse}",
            "order": "robot_number.asc",
            "limit": "5000",
        },
    )

    if rows is None:
        logger.error("Не удалось прочитать список номеров роботов")
        return None

    numbers = sorted({
        str(row.get("robot_number"))
        for row in rows
        if row.get("robot_number") is not None
    })

    _numbers_cache["numbers"] = numbers
    _numbers_cache["at"] = now

    return numbers


def suggest_robot_numbers(number, limit: int = None, cutoff: int = None):
    """
    Похожие существующие номера: «3882» -> «882».

    Пустой список — либо номера нет смысла исправлять, либо база недоступна
    (в логе будет ошибка). Ничего не меняем сами: решение за сотрудником.
    """
    limit = limit or SUGGEST_LIMIT
    cutoff = SUGGEST_CUTOFF if cutoff is None else cutoff

    raw = str(number or "").strip().lstrip("#")

    if not raw.isdigit():
        return []

    numbers = robot_numbers()

    if not numbers:
        return []

    matches = process.extract(
        raw,
        numbers,
        scorer=fuzz.ratio,
        limit=limit + 2,
        score_cutoff=cutoff,
    )

    # При близких score предпочитаем номер, который ближе по длине:
    # «3882» -> «882» осмысленнее, чем «3881».
    matches.sort(key=lambda item: (-item[1], abs(len(item[0]) - len(raw))))

    return [match[0] for match in matches[:limit]]


def _templates_map() -> dict:
    """id шаблона -> тип проблемы (кэш на 10 минут)."""
    now = time.time()

    if (
        _templates_cache["map"]
        and now - _templates_cache["at"] < _TEMPLATES_CACHE_SECONDS
    ):
        return _templates_cache["map"]

    rows = rest_get(
        TEMPLATES_TABLE,
        params={"select": "id,issue_type", "limit": "500"},
    )

    if rows is None:
        return {}

    mapping = {
        row.get("id"): row.get("issue_type")
        for row in rows
        if row.get("id") is not None
    }

    _templates_cache["map"] = mapping
    _templates_cache["at"] = now

    return mapping


def _employee_names(card_ids):
    """card_id -> user_name (одним запросом)."""
    ids = sorted({int(card) for card in card_ids if card is not None})

    if not ids:
        return {}

    rows = rest_get(
        EMPLOYEES_TABLE,
        params={
            "select": "card_id,user_name",
            "card_id": f"in.({','.join(str(card) for card in ids)})",
        },
    )

    if rows is None:
        return {}

    return {
        row.get("card_id"): row.get("user_name")
        for row in rows
        if row.get("card_id") is not None
    }


def format_duration(seconds) -> str:
    """«3 дн 4 ч», «5 ч 12 мин», «12 мин» — для строки про простоой."""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "?"

    if seconds < 0:
        seconds = 0

    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60

    if days:
        return f"{days}d {hours}h"

    if hours:
        return f"{hours}h {minutes}m"

    return f"{minutes}m"


def _pretty(moment) -> str:
    """ISO-время -> «23.09 18:47» (Варшава)."""
    if not moment:
        return "-"

    parsed = parse_iso(moment)

    if parsed is None:
        return str(moment)[:16]

    return parsed.astimezone(WARSAW_TZ).strftime("%d.%m %H:%M")


def robot_card(number, warehouse: str = WAREHOUSE):
    """
    Данные карточки робота.

    None — база недоступна (это не «робот не найден»).
    {"found": False, ...} — робота нет в справочнике склада.
    """
    raw = str(number or "").strip().lstrip("#")

    if not raw.isdigit():
        return {"found": False, "number": raw, "suggestions": []}

    cache_key = (raw, warehouse)
    now = time.time()
    cached = _card_cache.get(cache_key)

    if cached and now - cached[0] < _CACHE_SECONDS:
        return cached[1]

    rows = rest_get(
        ROBOTS_TABLE,
        params={
            "select": "*",
            "robot_number": f"eq.{raw}",
            "warehouse": f"eq.{warehouse}",
            "order": "updated_at.desc",
            "limit": "1",
        },
    )

    if rows is None:
        logger.error("Не удалось прочитать робота #%s — база недоступна", raw)
        return None

    if not rows:
        return {
            "found": False,
            "number": raw,
            "suggestions": suggest_robot_numbers(raw),
        }

    robot = rows[0]

    history = rest_get(
        HISTORY_TABLE,
        params={
            "select": "created_at,old_status,new_status,type_problem,problem_note,add_by",
            "robot_number": f"eq.{raw}",
            "order": "created_at.desc",
            "limit": str(HISTORY_LIMIT),
        },
    )

    since = (
        datetime.now(timezone.utc) - timedelta(days=ERROR_WINDOW_DAYS)
    ).isoformat()

    # **Колонки единой таблицы.** Робот совпадает по `robot_number` (число,
    # равное номеру в `robots_maintenance_list`), начало ошибки — `start_time`,
    # склад — **числовой id**.
    #
    # **Склад обязателен.** Номера роботов повторяются между складами (`121`,
    # `122`, `123` есть и в GLP-C, и в SMALL-P3; `201`, `2000` — в GLP-C и
    # P3-DC-1), поэтому без склада карточка показала бы чужие ошибки. У
    # `exceptions.warehouse` тип bigint: сравнение с названием вернуло бы ноль
    # строк — «карточка без ошибок», хотя они есть.
    warehouse_key = journal.warehouse_id(warehouse)

    if warehouse_key is None:
        logger.warning("robot_card: неизвестный склад %r", warehouse)

        return None

    # 🔴 **Фильтр по НОМЕРУ робота, а не по `id` строки справочника.**
    #
    # Дефект найден аудитом 14.10.2026. Здесь стояло `robot.get('id')` — это
    # **первичный ключ** `robots_maintenance_list`, а `exceptions.robot_number`
    # хранит номер, который назвал оператор. Поля разные, и связь молча
    # разошлась: у робота **#3646** (`id` = 3208) карточка показывала
    # **0 ошибок за 7 дней**, хотя их **32** — он был лидером по числу ошибок.
    #
    # **Почему это не поймали ни тесты, ни лог.** Запрос оставался валидным:
    # `robot_number = 3208` синтаксически верен и возвращает пусто. То есть
    # карточка не падала, а **уверенно сообщала «Issues (7 days): 0»** — худший
    # вид дефекта: человек видит спокойного робота там, где робот проблемный.
    #
    # Выше в этой же функции уже есть правильный фильтр по `raw` (для поиска
    # самого робота) — то есть номер доступен и был под рукой.
    errors_filter = {
        "robot_number": f"eq.{raw}",
        "start_time": f"gte.{since}",
        "warehouse": f"eq.{warehouse_key}",
    }

    total = rest_count(EXCEPTIONS_TABLE, errors_filter)

    recent = journal.legacy_rows(
        rest_get(
            EXCEPTIONS_TABLE,
            params={
                "select": journal.JOURNAL_COLUMNS,
                **errors_filter,
                "order": "start_time.desc",
                "limit": "200",
            },
        )
    )

    # Автор ошибки лежит в `employee` (имя строкой), а не в `handle_by`:
    # в legacy-таблице нет ссылки на сотрудника, только записанное имя.
    names = _employee_names([row.get("add_by") for row in (history or [])])

    templates = _templates_map()

    card = {
        "found": True,
        "number": raw,
        "robot": robot,
        "offline_for": None,
        "history_available": history is not None,
        "history": [],
        "errors": total,
        "errors_available": recent is not None,
        "reporters": [],
        "last_issues": [],
    }

    if str(robot.get("status") or "") == OFFLINE_STATUS:
        started = robot.get("updated_at")

        if started:
            moment = parse_iso(started)

            if moment is None:
                card["offline_for"] = "?"
            else:
                card["offline_for"] = format_duration(
                    (datetime.now(timezone.utc) - moment).total_seconds()
                )

    for row in history or []:
        card["history"].append({
            "at": _pretty(row.get("created_at")),
            "old": row.get("old_status") or "-",
            "new": row.get("new_status") or "-",
            "type_problem": row.get("type_problem"),
            "note": row.get("problem_note"),
            "by": names.get(row.get("add_by")) or "-",
        })

    if recent:
        # В legacy-таблице автор записан **именем** (`employee`), а не ссылкой:
        # переводить его через `_employee_names` нечего. Раньше здесь стоял
        # `handle_by`, которого в таблице нет, — счётчик всегда был пуст.
        counter = Counter(
            row.get("employee") for row in recent if row.get("employee")
        )

        card["reporters"] = [
            (employee, count) for employee, count in counter.most_common(3)
        ]

        for row in recent[:LAST_ISSUES_LIMIT]:
            # 🔴 **Тип ошибки берётся общим правилом, а не `issue_type`.**
            #
            # Дефект найден 14.10.2026 (третья копия одного и того же): в
            # `issue_type` лежит `equipment_category` — **категория
            # оборудования**. В списке последних ошибок робота это выглядело как
            # «14.10 19:08 · 设备Equipment · Yassine Si-Afif» или «· robot», то
            # есть вместо неисправности карточка называла класс устройства.
            #
            # Правило — то же, что у `/top`, `/stats` и рапорта смены
            # (`journal.meaningful_issue`): снимок `raw_*` важнее справочника,
            # категории и модули отбрасываются.
            card["last_issues"].append({
                "at": _pretty(row.get("start_time")),
                "issue_type": journal.meaningful_issue(row),
                "recovery": row.get("recovery_title") or None,
                "by": row.get("employee") or "-",
            })

    _card_cache[cache_key] = (now, card)

    if len(_card_cache) > 200:
        _card_cache.pop(next(iter(_card_cache)))

    return card


def format_robot_card(card) -> str:
    """Текст карточки для Telegram (без Markdown — его нельзя ломать)."""
    if card is None:
        return "⚠️ Can't read the robot right now (database error)."

    number = card.get("number")

    if not card.get("found"):
        lines = [f"⚠️ Robot #{number} is not in the {WAREHOUSE} list."]

        suggestions = card.get("suggestions") or []

        if suggestions:
            lines.append("")
            lines.append("Did you mean:")
            lines.extend(f"  • /robot {item}" for item in suggestions)

        return "\n".join(lines)

    robot = card["robot"]

    lines = [
        f"🔧 Robot {robot.get('robot_number')} · "
        f"{robot.get('robot_type') or '-'} · "
        f"{robot.get('warehouse') or WAREHOUSE}",
        f"Status: {robot.get('status') or '-'}",
    ]

    if card.get("offline_for"):
        lines.append(f"Offline for: {card['offline_for']}")

    if robot.get("type_problem"):
        lines.append(f"Current issue: {robot['type_problem']}")

    if (robot.get("problem_note") or "").strip() not in ("", "?"):
        lines.append(f"Note: {robot['problem_note']}")

    if card.get("errors_available"):
        lines.append(
            f"Issues ({ERROR_WINDOW_DAYS} days): {card.get('errors')}"
        )

    if card.get("reporters"):
        lines.append(
            "Reported by: "
            + ", ".join(
                f"{name} ({count})" for name, count in card["reporters"]
            )
        )

    if card.get("last_issues"):
        lines.append("")
        lines.append("Last issues:")

        for item in card["last_issues"]:
            lines.append(
                f"  {item['at']} · {item['issue_type']} · {item['by']}"
            )

    if card.get("history"):
        lines.append("")
        lines.append("History:")

        for item in card["history"]:
            note = f" · {item['note']}" if item.get("note") else ""
            lines.append(
                f"  {item['at']} · {item['old']} → {item['new']} · "
                f"{item.get('type_problem') or '-'} · {item['by']}{note}"
            )

    return "\n".join(lines)
