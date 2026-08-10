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

API_BASE_URL = os.environ.get(
    "API_BASE_URL",
    "https://tk-assistant-api-production.up.railway.app",
)

WARSAW_TZ = ZoneInfo("Europe/Warsaw")


# ============================================================
# GET REQUEST
# ============================================================

def get_data(
    url: str,
    params: dict = None,
    headers: dict = None,
):
    try:
        response = requests.get(
            url,
            params=params,
            headers=headers,
            timeout=5,
        )

        response.raise_for_status()

        return response.json()

    except requests.exceptions.RequestException as e:
        print(
            f"Error fetching data from {url}: {e}"
        )

        return None


# ============================================================
# POST REQUEST
# ============================================================

def post_data(
    url: str,
    payload: dict,
):
    try:
        response = requests.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json"
            },
            timeout=5,
        )

        print(
            f"POST {url} -> {response.status_code}"
        )

        response.raise_for_status()

        return response.json()

    except requests.exceptions.RequestException as e:
        print(
            f"Error posting data to {url}: {e}"
        )

        return None


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

    error_templates = get_data(
        f"{API_BASE_URL}/exceptionsTemplates/get_templates"
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

    employee_data = get_data(
        f"{API_BASE_URL}/employees/get_employee_by_name",
        params={
            "name": table_lines["employee"]
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

    pretty_date = now.strftime(
        "%Y-%m-%d"
    )

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

    robot_data = get_data(
        f"{API_BASE_URL}/robots/get_robots_by_number",
        params={
            "robot_number": int(
                table_lines["robot"]
            ),
            "warehouse": "GLP-C",
            "limit": 1,
        },
    )

    if not robot_data:

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
    # NEW EXCEPTION OBJECT
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
    # OLD EXCEPTION OBJECT
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

        "warehouse": "GLP-C",

        "issue_data": shift_date,

        "issue_warehouse": "C2",
    }

    # ========================================================
    # SAVE NEW EXCEPTION
    # ========================================================

    saved = post_data(
        f"{API_BASE_URL}/exceptions/add_exceptions",
        obj,
    )

    # ========================================================
    # SAVE OLD EXCEPTION
    # ========================================================

    saved_old = post_data(
        f"{API_BASE_URL}/exceptions/add_old_exceptions",
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