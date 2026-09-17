import os
import requests

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from rapidfuzz import fuzz, process

from lark_send import send_text_message
from shift import get_current_shift


# ============================================================
# CONFIG
# ============================================================
#
# Бот больше не ходит через tk-assist-api: новый API убрал эндпоинты
# записи исключений. Поэтому читаем и пишем напрямую в Supabase
# (PostgREST) сервисным ключом.

SUPABASE_URL = os.environ.get(
    "SUPABASE_URL",
    "https://ljkugtpeboomboobodom.supabase.co",
)

SUPABASE_SERVICE_KEY = os.environ.get(
    "SUPABASE_SERVICE_KEY",
    "",
)

WAREHOUSE = "GLP-C"

WARSAW_TZ = ZoneInfo("Europe/Warsaw")


# ============================================================
# POSTGREST HELPERS
# ============================================================

def _headers() -> dict:
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def _rest_get(table: str, params: dict = None):
    """
    GET /rest/v1/<table>. Возвращает JSON (список или объект)
    либо None при ошибке.
    """
    url = f"{SUPABASE_URL}/rest/v1/{table}"

    try:
        response = requests.get(
            url,
            headers=_headers(),
            params=params,
            timeout=10,
        )

        response.raise_for_status()

        return response.json()

    except requests.exceptions.RequestException as e:
        print(f"Error fetching from {table}: {e}")
        return None


def _rest_post(table: str, payload: dict):
    """
    POST /rest/v1/<table>. Возвращает JSON (созданные строки)
    либо None при ошибке.
    """
    url = f"{SUPABASE_URL}/rest/v1/{table}"

    headers = _headers()
    # PostgREST по умолчанию отдаёт пустое тело на POST — просим
    # вернуть созданные строки, чтобы отличать успех от неудачи.
    headers["Prefer"] = "return=representation"

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=10,
        )

        print(f"POST {table} -> {response.status_code}")

        response.raise_for_status()

        return response.json()

    except requests.exceptions.RequestException as e:
        print(f"Error posting data to {table}: {e}")
        return None


# ============================================================
# SHIFT EXCEPTIONS (from Supabase)
# ============================================================

def get_shift_exceptions(
    shift_date: str,
    shift_name: str,
    warehouse: str = WAREHOUSE,
    limit: int = 1000,
):
    """
    Список исключений за смену из таблицы exceptions_glpc.

    Возвращает None, если запрос не удался.
    """
    return _rest_get(
        "exceptions_glpc",
        params={
            "issue_data": f"eq.{shift_date}",
            "shift_type": f"eq.{shift_name}",
            "warehouse": f"eq.{warehouse}",
            "order": "error_start_time.desc",
            "limit": str(limit),
        },
    )


def count_robot_errors_in_shift(
    robot,
    shift_date: str,
    shift_name: str,
):
    """
    Количество сохранённых исключений робота за смену.
    Считаем по таблице exceptions_glpc (источник для отчётов).
    """
    data = get_shift_exceptions(shift_date, shift_name)

    if not data:
        return 0

    robot_str = str(robot)

    return sum(
        1
        for exc in data
        if str(exc.get("error_robot")) == robot_str
    )


def shift_stats(shift_date: str, shift_name: str):
    """
    Возвращает (total, {robot: count}, {issue_type: count})
    за смену из exceptions_glpc.
    """
    data = get_shift_exceptions(shift_date, shift_name)

    if not data:
        return 0, {}, {}

    total = len(data)
    by_robot = {}
    by_type = {}

    for exc in data:
        robot = str(exc.get("error_robot"))
        by_robot[robot] = by_robot.get(robot, 0) + 1

        issue_type = (
            exc.get("issue_type")
            or exc.get("first_column")
            or "unknown"
        )
        by_type[issue_type] = by_type.get(issue_type, 0) + 1

    return total, by_robot, by_type


# ============================================================
# FIND BEST ERROR TEMPLATE
# ============================================================

def find_best_template(
    error_text: str,
    templates: list[dict],
    threshold: int = 60,
):
    titles = [
        template.get(
            "employee_title",
            "",
        )
        for template in templates
    ]

    match = process.extractOne(
        error_text,
        titles,
        scorer=fuzz.token_sort_ratio,
    )

    if not match:
        return None

    matched_title, score, index = match

    print(
        f"Template similarity: "
        f"{score:.1f}% | "
        f"'{matched_title}'"
    )

    if score < threshold:
        return None

    return templates[index]


# ============================================================
# SAVE EXCEPTION
# ============================================================

def send_to_data_base(
    parsed: dict,
    table_lines: dict,
    chat_id: str,
):
    # ========================================================
    # GET ERROR TEMPLATES
    # ========================================================

    error_templates = _rest_get(
        "issue_templates",
        params={
            "select": "*",
            "order": "created_at.desc",
        },
    )

    if not error_templates:
        print(
            "Failed to fetch exception templates"
        )

        return None

    # ========================================================
    # FIND BEST TEMPLATE
    # ========================================================

    best_match = find_best_template(
        parsed["error_text"],
        error_templates,
    )

    if not best_match:
        print(
            "Template not found"
        )

        return None

    print(
        f"Template found: "
        f"{best_match['employee_title']} "
        f"(id={best_match['id']})"
    )

    # ========================================================
    # FIND EMPLOYEE
    # ========================================================

    employee_data = _rest_get(
        "employees",
        params={
            "select": "*",
            "user_name": f"eq.{table_lines['employee']}",
        },
    )

    if not employee_data:

        alert = (
            "⚠️ Can't find employee, "
            "issue don't save to database, "
            "please check your name and try again."
        )

        send_text_message(
            chat_id,
            alert,
        )

        print(
            "Employee not found"
        )

        return None

    employee = employee_data[0]

    # ========================================================
    # WARSAW TIME
    # ========================================================

    now = datetime.now(WARSAW_TZ)

    now_iso = now.isoformat()

    end_time = (
        now
        + timedelta(
            minutes=best_match["solving_time"]
        )
    )

    end_time_iso = end_time.isoformat()

    pretty_datetime = now.strftime(
        "%d.%m.%Y %H:%M:%S"
    )

    print(
        f"Exception time: {pretty_datetime} "
        f"(Europe/Warsaw)"
    )

    # ========================================================
    # FIND ROBOT
    # ========================================================

    robot_data = _rest_get(
        "robots_maintenance_list",
        params={
            "select": "*",
            "robot_number": f"eq.{int(table_lines['robot'])}",
            "warehouse": f"eq.{WAREHOUSE}",
            "order": "updated_at.desc",
            "limit": "1",
        },
    )

    if not robot_data:
        obj = {
            "robot_number": table_lines['robot'],
            "employee_id": employee["card_id"],
            "warehouse": WAREHOUSE,
        }

        _rest_post(
            "robots_to_add",
            obj,
        )

        alert = (
            f"⚠️ Can't find robot "
            f"#{table_lines['robot']}, "
            "issue don't save to database, "
            "please check the robot number."
        )

        send_text_message(
            chat_id,
            alert,
        )

        print(
            "Robot not found"
        )

        return None

    robot = robot_data[0]

    # ========================================================
    # CURRENT SHIFT
    # ========================================================

    shift_date, shift_name = (
        get_current_shift(now)
    )

    print(
        f"Shift: {shift_date} / {shift_name}"
    )

    # ========================================================
    # NEW EXCEPTION OBJECT (таблица exceptions)
    # ========================================================

    obj = {
        "workstation_id": None,

        "robot_id": robot["id"],

        "handle_by": employee["card_id"],

        "start_time": now_iso,

        "end_time": end_time_iso,

        "exception_id": best_match["id"],

        "shift_type": shift_name,

        "warehouse": employee["home_warehouse"],
    }

    # ========================================================
    # OLD EXCEPTION OBJECT (таблица exceptions_glpc)
    # ========================================================

    old_obj = {
        "error_robot": robot["robot_number"],

        "add_by": employee["card_id"],

        "device_type": robot["robot_type"],

        "employee": employee["user_name"],

        "error_end_time": end_time_iso,

        "error_start_time": now_iso,

        "first_column": best_match[
            "issue_sub_type"
        ],

        "issue_description": best_match[
            "issue_description"
        ],

        "issue_type": best_match[
            "issue_type"
        ],

        "recovery_title": best_match[
            "recovery_title"
        ],

        "second_column": best_match[
            "issue_sub_type"
        ],

        "solving_time": best_match[
            "solving_time"
        ],

        "uniq_key": (
            f"{employee['user_name']}."
            f"{robot['robot_number']}."
            f"{now_iso}"
        ),

        "shift_type": shift_name,

        "warehouse": WAREHOUSE,

        "issue_data": shift_date,

        "issue_warehouse": "C2",
    }

    # ========================================================
    # SAVE NEW EXCEPTION
    # ========================================================

    saved = _rest_post(
        "exceptions",
        obj,
    )

    # ========================================================
    # SAVE OLD EXCEPTION
    # ========================================================

    saved_old = _rest_post(
        "exceptions_glpc",
        old_obj,
    )

    # ========================================================
    # CHECK RESULT
    # ========================================================

    if not saved:

        alert = (
            "⚠️ Failed to save exception "
            "to database."
        )

        send_text_message(
            chat_id,
            alert,
        )

        print(
            "Failed to save exception"
        )

        return None

    print(
        f"✅ Exception saved successfully "
        f"(robot={robot['robot_number']}, "
        f"time={pretty_datetime}, "
        f"shift={shift_name})"
    )

    return saved
