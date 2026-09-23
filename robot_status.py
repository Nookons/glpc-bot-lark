"""
Перевод робота в офлайн/онлайн (из Telegram).

Пишет ровно туда же, куда и приложение склада:

  * robots_maintenance_list.status  — текущее состояние робота;
  * change_status_robots            — журнал переходов (кто, когда, почему).

Причины (type_problem) взяты из вашего же журнала, чтобы данные не
разъезжались: в офлайн — «Abnormal walking»/«Damaged car body parts»/…,
в онлайн — «Solved without changing»/«Replaced Spare Parts»/«Software Upgrade»/…
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sendToDataBase import WAREHOUSE, rest_get, rest_patch, rest_post
from logging_config import setup_logging


logger = setup_logging(__name__)

WARSAW_TZ = ZoneInfo("Europe/Warsaw")

ONLINE = "在线 | Online"
OFFLINE = "离线 | Offline"


DIRECTIONS = {
    "offline": {
        "new_status": OFFLINE,
        "expected_status": ONLINE,
        "label": "Offline",
        "emoji": "🔌",
        "color": "orange",
    },
    "online": {
        "new_status": ONLINE,
        "expected_status": OFFLINE,
        "label": "Online",
        "emoji": "✅",
        "color": "green",
    },
}


# Коды нужны для callback_data кнопок, подписи — то, что уходит в базу.
REASONS = {
    "offline": (
        ("abnormal_walking", "Abnormal walking"),
        ("damaged_body", "Damaged car body parts"),
        ("safety_controller", "Safety controller issues"),
        ("other", "Other"),
    ),
    "online": (
        ("solved_without_changes", "Solved without changing"),
        ("replaced_parts", "Replaced Spare Parts"),
        ("software_upgrade", "Software Upgrade"),
        ("software_fix", "Software fix"),
        ("other", "Other"),
    ),
}

STATUS_HISTORY_TABLE = "change_status_robots"
ROBOTS_TABLE = "robots_maintenance_list"


def reason_label(direction: str, code: str):
    """Подпись причины по её коду (или None)."""
    for item_code, label in REASONS.get(direction, ()):
        if item_code == code:
            return label

    return None


def find_robot(robot_number, warehouse: str = WAREHOUSE):
    """Строка робота из robots_maintenance_list (или None)."""
    try:
        number = int(str(robot_number).strip().lstrip("#"))
    except (TypeError, ValueError):
        return None

    rows = rest_get(
        ROBOTS_TABLE,
        params={
            "select": "*",
            "robot_number": f"eq.{number}",
            "warehouse": f"eq.{warehouse}",
            "order": "updated_at.desc",
            "limit": "1",
        },
    )

    return rows[0] if rows else None


def find_robot_by_id(robot_id):
    """Строка робота по первичному ключу (нужна для callback-кнопок)."""
    try:
        key = int(robot_id)
    except (TypeError, ValueError):
        return None

    rows = rest_get(
        ROBOTS_TABLE,
        params={"select": "*", "id": f"eq.{key}", "limit": "1"},
    )

    return rows[0] if rows else None


def is_in_status(robot: dict, direction: str) -> bool:
    """Робот уже в целевом статусе?"""
    return str(robot.get("status") or "") == DIRECTIONS[direction]["new_status"]


def change_robot_status(
    robot: dict,
    direction: str,
    type_problem: str,
    problem_note: str,
    employee: dict,
):
    """
    Меняет статус робота и добавляет запись в журнал.

    Возвращает dict с old/new статусом либо None, если статус не удалось
    обновить (журнал пишется best-effort и только предупреждает в лог).
    """
    spec = DIRECTIONS[direction]

    old_status = str(robot.get("status") or "")
    new_status = spec["new_status"]
    changed_at = datetime.now(timezone.utc).isoformat()
    card_id = (employee or {}).get("card_id")

    payload = {
        "status": new_status,
        "updated_at": changed_at,
        "updated_by": card_id,
    }

    if direction == "offline":
        # Дашборд показывает «CURRENT ISSUE» из самой карточки робота,
        # поэтому тип проблемы и заметку пишем и сюда, а не только в журнал.
        payload["type_problem"] = type_problem
        payload["problem_note"] = problem_note or ""
    else:
        # Робот вернулся в работу — текущая проблема больше не актуальна.
        payload["type_problem"] = None
        payload["problem_note"] = None

    updated = rest_patch(
        ROBOTS_TABLE,
        params={"id": f"eq.{robot['id']}"},
        payload=payload,
    )

    if updated is None:
        logger.error(
            "Не удалось изменить статус робота #%s",
            robot.get("robot_number"),
        )
        return None

    history = rest_post(
        STATUS_HISTORY_TABLE,
        {
            "robot_id": robot.get("id"),
            "robot_number": robot.get("robot_number"),
            "old_status": old_status,
            "new_status": new_status,
            "type_problem": type_problem,
            "problem_note": problem_note or "",
            "add_by": card_id,
            "warehouse": robot.get("warehouse") or WAREHOUSE,
        },
    )

    if history is None:
        logger.warning(
            "Статус робота #%s изменён, но запись в %s не добавилась",
            robot.get("robot_number"),
            STATUS_HISTORY_TABLE,
        )

    logger.info(
        "Статус робота #%s: %s -> %s (%s) by %s",
        robot.get("robot_number"),
        old_status,
        new_status,
        type_problem,
        (employee or {}).get("user_name"),
    )

    return {
        "robot": robot,
        "old_status": old_status,
        "new_status": new_status,
        "type_problem": type_problem,
        "problem_note": problem_note or "",
        "changed_at": changed_at,
        "history_saved": history is not None,
    }


# ============================================================
# ОФОРМЛЕНИЕ
# ============================================================

def _warsaw_time(changed_at: str) -> str:
    try:
        moment = datetime.fromisoformat(str(changed_at).replace("Z", "+00:00"))
        return moment.astimezone(WARSAW_TZ).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return ""


def status_title(direction: str, robot: dict) -> str:
    spec = DIRECTIONS[direction]

    return f"{spec['emoji']} Robot {robot.get('robot_number')} → {spec['label']}"


def build_status_card(
    direction: str,
    result: dict,
    employee_name: str,
) -> dict:
    """Карточка для Lark-группы."""
    spec = DIRECTIONS[direction]
    robot = result["robot"]
    note = result.get("problem_note") or "—"

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": spec["color"],
            "title": {
                "tag": "plain_text",
                "content": status_title(direction, robot),
            },
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**Robot** {robot.get('robot_number')} · "
                        f"{robot.get('robot_type') or '-'} · "
                        f"{robot.get('warehouse') or WAREHOUSE}\n"
                        f"**Status** {result['old_status']} → {result['new_status']}"
                    ),
                },
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**Reason:** {result['type_problem']}\n"
                        f"**Note:** {note}"
                    ),
                },
            },
            {
                "tag": "note",
                "elements": [{
                    "tag": "plain_text",
                    "content": (
                        f"👤 {employee_name} · "
                        f"{_warsaw_time(result.get('changed_at'))}"
                    ),
                }],
            },
        ],
    }


def build_status_text(direction: str, result: dict, employee_name: str) -> str:
    """Текстовый вариант (запасной, если карточка не прошла)."""
    robot = result["robot"]

    return "\n".join([
        status_title(direction, robot),
        f"Robot {robot.get('robot_number')} · {robot.get('robot_type') or '-'} · "
        f"{robot.get('warehouse') or WAREHOUSE}",
        f"Status: {result['old_status']} → {result['new_status']}",
        f"Reason: {result['type_problem']}",
        f"Note: {result.get('problem_note') or '—'}",
        f"By: {employee_name} · {_warsaw_time(result.get('changed_at'))}",
    ])
