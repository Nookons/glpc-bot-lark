import os
import requests
from rapidfuzz import fuzz, process
from lark_send import send_text_message
from datetime import datetime, timedelta
from shift import get_current_shift

API_BASE_URL = os.environ.get(
    "API_BASE_URL", "https://tk-assistant-api-production.up.railway.app"
)


def get_data(url: str, params: dict = None, headers: dict = None):
    try:
        response = requests.get(url, params=params, headers=headers, timeout=5)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching data from {url}: {e}")
        return None


def post_data(url: str, payload: dict):
    response = requests.post(
        url,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=5,
    )
    print("Status:", response.status_code)
    response.raise_for_status()
    return response.json()


def find_best_template(error_text: str, templates: list[dict], threshold: int = 60):
    titles = [t.get("employee_title", "") for t in templates]

    match = process.extractOne(
        error_text,
        titles,
        scorer=fuzz.token_sort_ratio,
    )

    if not match:
        return None

    matched_title, score, index = match

    if score < threshold:
        return None

    return templates[index]


def send_to_data_base(parsed: dict, table_lines: dict, chat_id: str):
    error_templates = get_data(
        f"{API_BASE_URL}/exceptionsTemplates/get_templates"
    )

    if not error_templates:
        print("Failed to fetch exception templates")
        return None

    best_match = find_best_template(parsed["error_text"], error_templates)

    if not best_match:
        print("Template not found")
        return None

    print(f"Template found: {best_match['employee_title']} (id={best_match['id']})")

    employee_data = get_data(
        f"{API_BASE_URL}/employees/get_employee_by_name?name={table_lines['employee']}"
    )

    if not employee_data:
        alert = (
            "⚠️ Can't find employee, issue don't save to database, "
            "please check your name and try again."
        )
        send_text_message(chat_id, alert)
        print("Employee not found")
        return None

    employee = employee_data[0]

    now = datetime.now()
    now_iso = now.isoformat()
    end_time_iso = (now + timedelta(minutes=best_match["solving_time"])).isoformat()

    pretty_date = now.strftime("%Y-%m-%d")

    robot_data = get_data(
        f"{API_BASE_URL}/robots/get_robots_by_number?robot_number={int(table_lines['robot'])}&warehouse=GLP-C&limit=1"
    )

    if not robot_data:
        alert = (
            f"⚠️ Can't find robot #{table_lines['robot']}, "
            "issue don't save to database, please check the robot number."
        )
        send_text_message(chat_id, alert)
        print("Robot not found")
        return None

    robot = robot_data[0]
    shift_date, shift_name = get_current_shift()

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

    old_obj = {
        "error_robot": robot["robot_number"],
        "add_by": employee["card_id"],
        "device_type": robot["robot_type"],
        "employee": employee["user_name"],
        "error_end_time": end_time_iso,
        "error_start_time": now_iso,
        "first_column": best_match["issue_sub_type"],
        "issue_description": best_match["issue_description"],
        "issue_type": best_match["issue_type"],
        "recovery_title": best_match["recovery_title"],
        "second_column": best_match["issue_sub_type"],
        "solving_time": best_match["solving_time"],
        "uniq_key": f"{employee['user_name']}.{robot['robot_number']}.{now_iso}",
        "shift_type": shift_name,
        "warehouse": "GLP-C",
        "issue_data": pretty_date,
        "issue_warehouse": "C2",
    }

    saved = post_data(f"{API_BASE_URL}/exceptions/add_exceptions", obj)
    saved_old = post_data(f"{API_BASE_URL}/exceptions/add_old_exceptions", old_obj)

    if not saved:
        alert = "⚠️ Failed to save exception to database."
        send_text_message(chat_id, alert)
        print("Failed to save exception")
        return None

    return saved