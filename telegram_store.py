"""
Привязка Telegram-аккаунтов к сотрудникам.

Раньше имя сотрудника бот получал из Lark Contact API по user_id
(это тратило квоту Open Platform). В Telegram соответствие задаётся
один раз командой /reg и хранится в Supabase в таблице telegram_users.

SQL для создания таблицы: sql/telegram_users.sql
"""

from __future__ import annotations

from datetime import datetime, timezone

from rapidfuzz import fuzz, process

from sendToDataBase import rest_delete, rest_get, rest_upsert
from logging_config import setup_logging


logger = setup_logging(__name__)


TABLE = "telegram_users"

# Порог схожести для автоподбора имени сотрудника (rapidfuzz).
NAME_MATCH_THRESHOLD = 78

# Сколько подсказок показывать, если имя не найдено.
SUGGESTION_LIMIT = 5


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_link(telegram_id: int):
    """Строка привязки из telegram_users или None."""
    rows = rest_get(
        TABLE,
        params={
            "select": "*",
            "telegram_id": f"eq.{int(telegram_id)}",
            "limit": "1",
        },
    )

    if not rows:
        return None

    return rows[0]


def get_employee_name(telegram_id: int):
    """Имя сотрудника для Telegram-аккаунта или None."""
    link = get_link(telegram_id)

    if not link:
        return None

    return link.get("employee_name")


def link_user(telegram_id: int, username, employee_name: str) -> bool:
    """
    Создаёт/обновляет привязку Telegram-аккаунта к сотруднику.

    Повторный /reg перезаписывает привязку (upsert по telegram_id).
    """
    payload = {
        "telegram_id": int(telegram_id),
        "telegram_username": username,
        "employee_name": employee_name,
        "updated_at": _now_iso(),
    }

    result = rest_upsert(TABLE, payload, on_conflict="telegram_id")

    if result is None:
        logger.error(
            "Failed to link telegram_id=%s to employee=%r "
            "(is table %s created?)",
            telegram_id,
            employee_name,
            TABLE,
        )
        return False

    logger.info(
        "Linked telegram_id=%s (%s) -> %s",
        telegram_id,
        username,
        employee_name,
    )

    return True


def unlink_user(telegram_id: int) -> bool:
    """Удаляет привязку Telegram-аккаунта."""
    return rest_delete(
        TABLE,
        params={"telegram_id": f"eq.{int(telegram_id)}"},
    )


# ============================================================
# EMPLOYEE NAME RESOLUTION
# ============================================================

def load_employee_names() -> list:
    """Список user_name из таблицы employees."""
    rows = rest_get(
        "employees",
        params={
            "select": "user_name",
            "order": "user_name.asc",
            "limit": "5000",
        },
    )

    if not rows:
        return []

    return sorted({
        row["user_name"]
        for row in rows
        if row.get("user_name")
    })


def suggest_employee_names(raw_name: str, names: list, limit: int = SUGGESTION_LIMIT) -> list:
    """Похожие имена для подсказки пользователю."""
    if not names:
        return []

    matches = process.extract(
        raw_name,
        names,
        scorer=fuzz.WRatio,
        limit=limit,
    )

    return [name for name, _score, _index in matches]


def resolve_employee_name(raw_name: str):
    """
    Ищет сотрудника по введённому имени.

    Возвращает (employee_name | None, suggestions).

    Сначала точное совпадение без учёта регистра, затем нечёткое
    сравнение через rapidfuzz (люди пишут имена с опечатками).
    """
    names = load_employee_names()

    if not names:
        logger.error("No employees loaded (table employees?)")
        return None, []

    target = (raw_name or "").strip()

    if not target:
        return None, names[:SUGGESTION_LIMIT]

    for name in names:
        if name.casefold() == target.casefold():
            return name, []

    match = process.extractOne(
        target,
        names,
        scorer=fuzz.token_sort_ratio,
    )

    if match and match[1] >= NAME_MATCH_THRESHOLD:
        logger.info(
            "Employee name matched: %r -> %r (%.1f%%)",
            target,
            match[0],
            match[1],
        )
        return match[0], []

    return None, suggest_employee_names(target, names)
