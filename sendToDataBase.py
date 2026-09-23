import os
import requests

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from rapidfuzz import fuzz, process

from lark_send import send_text_message
from shift import get_current_shift
from logging_config import setup_logging


logger = setup_logging(__name__)


# .env должен быть загружен до чтения SUPABASE_* ниже.
load_dotenv()


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
# USER NOTIFIER
# ============================================================
#
# Этот модуль должен уметь отвечать пользователю («шаблон не найден»,
# «сотрудник не найден», ...). Раньше ответ всегда уходил в Lark через
# API (расход квоты). Теперь вызывающий код может подменить транспорт:
# Telegram-бот ставит сюда функцию отправки в Telegram, и тогда ни один
# ответ пользователю не тратит квоту Lark Open Platform.

_notifier = None


def set_notifier(notify):
    """
    Регистрирует функцию notify(chat_id, text) для ответов пользователю.

    Если notifier не задан, используется старый путь (Lark API).
    """
    global _notifier
    _notifier = notify


def notify_user(chat_id, text):
    """Отправляет текст пользователю через notifier (или Lark API)."""
    if _notifier is None:
        # Так быть не должно: в Telegram-боте notifier ставится на старте,
        # а Lark API здесь получил бы Telegram chat_id и потратил квоту.
        logger.warning(
            "Notifier не задан — ответ уйдёт через Lark API (chat_id=%s)",
            chat_id,
        )
        send_text_message(chat_id, text)
        return

    try:
        _notifier(chat_id, text)
    except Exception:
        logger.exception("Notifier failed for chat %s", chat_id)


# ============================================================
# POSTGREST HELPERS
# ============================================================

def _headers() -> dict:
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def _json_or_none(response, table: str, action: str):
    """
    Разбирает JSON-ответ PostgREST.

    Не-JSON (HTML от прокси, пустое тело, обрезанный ответ) — это ошибка, а
    не «пустой результат»: возвращаем None, чтобы вызывающий код не принял
    сбой за «данных нет».
    """
    try:
        return response.json()
    except ValueError:
        logger.error(
            "%s %s: ответ не JSON (HTTP %s, %s байт)",
            action,
            table,
            response.status_code,
            len(response.content or b""),
        )
        return None


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

        return _json_or_none(response, table, "GET")

    except requests.exceptions.RequestException as e:
        logger.error("GET %s failed: %s", table, e)
        return None


def _rest_post(table: str, payload: dict, ignore_conflict: bool = False):
    """
    POST /rest/v1/<table>. Возвращает JSON (созданные строки)
    либо None при ошибке.

    ignore_conflict — для вставок в очереди: 409 означает «строка уже есть»,
    это не ошибка и не должно выглядеть как сбой в логах.
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

        response.raise_for_status()

        logger.info("POST %s -> %s", table, response.status_code)

        return _json_or_none(response, table, "POST")

    except requests.exceptions.HTTPError as e:
        response = getattr(e, "response", None)

        if (
            ignore_conflict
            and response is not None
            and response.status_code == 409
        ):
            logger.info("POST %s: строка уже существует (409)", table)
            return []

        logger.error("POST %s failed: %s", table, e)
        return None

    except requests.exceptions.RequestException as e:
        logger.error("POST %s failed: %s", table, e)
        return None


def rest_get(table: str, params: dict = None):
    """Публичный доступ к GET /rest/v1/<table> (для других модулей)."""
    return _rest_get(table, params)


def rest_count(table: str, params: dict = None):
    """
    Точное число строк (PostgREST `Prefer: count=exact`).

    Выгрузка строк и подсчёт в Python врут: PostgREST ограничивает ответ
    (db-max-rows), и на больших сменах часть строк просто не доезжает.
    """
    url = f"{SUPABASE_URL}/rest/v1/{table}"

    headers = _headers()
    headers["Prefer"] = "count=exact"
    headers["Range-Unit"] = "items"

    query = dict(params or {})
    query["select"] = "id"
    query["limit"] = "0"

    try:
        response = requests.get(
            url,
            headers=headers,
            params=query,
            timeout=10,
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error("COUNT %s failed: %s", table, e)
        return None

    content_range = response.headers.get("Content-Range") or ""

    if "/" not in content_range:
        logger.error("COUNT %s: нет Content-Range (%s)", table, content_range)
        return None

    total = content_range.rsplit("/", 1)[1]

    try:
        return int(total)
    except ValueError:
        logger.error("COUNT %s: непонятный Content-Range %r", table, content_range)
        return None


def set_exception_photo(table: str, row_id, photo_url: str):
    """Прикрепляет ссылку на фото к уже созданной записи об ошибке."""
    if not row_id or not photo_url:
        return False

    updated = rest_patch(
        table,
        params={"id": f"eq.{row_id}"},
        payload={"photo_url": photo_url},
    )

    if not updated:
        logger.error(
            "Не удалось привязать фото к %s id=%s",
            table,
            row_id,
        )
        return False

    logger.info("Фото привязано к %s id=%s", table, row_id)

    return True


def rest_post(table: str, payload: dict, ignore_conflict: bool = False):
    """Публичный доступ к POST /rest/v1/<table> (для других модулей)."""
    return _rest_post(table, payload, ignore_conflict=ignore_conflict)


def rest_upsert(table: str, payload: dict, on_conflict: str):
    """
    POST с upsert-семантикой (insert ... on conflict do update).

    Возвращает список строк при успехе, иначе None.
    """
    url = f"{SUPABASE_URL}/rest/v1/{table}"

    headers = _headers()
    headers["Prefer"] = "resolution=merge-duplicates,return=representation"

    try:
        response = requests.post(
            url,
            headers=headers,
            params={"on_conflict": on_conflict},
            json=payload,
            timeout=10,
        )

        response.raise_for_status()

        logger.info("UPSERT %s -> %s", table, response.status_code)

        return _json_or_none(response, table, "UPSERT")

    except requests.exceptions.RequestException as e:
        logger.error("UPSERT %s failed: %s", table, e)
        return None


def supabase_headers() -> dict:
    """Публичный доступ к заголовкам PostgREST/Storage (для других модулей)."""
    return _headers()


def table_exists(table: str) -> bool:
    """
    True, если таблица доступна через PostgREST.

    Нужно, чтобы бот мог работать до применения SQL-миграции
    (в этом случае привязки Telegram хранятся в Storage).
    """
    url = f"{SUPABASE_URL}/rest/v1/{table}"

    try:
        response = requests.get(
            url,
            headers=_headers(),
            params={"select": "*", "limit": "0"},
            timeout=10,
        )
    except requests.exceptions.RequestException as e:
        logger.error("Probe %s failed: %s", table, e)
        return False

    if response.status_code == 200:
        return True

    logger.info(
        "Probe %s -> HTTP %s (%s)",
        table,
        response.status_code,
        response.text[:120],
    )

    return False


def rest_patch(table: str, params: dict, payload: dict):
    """
    PATCH /rest/v1/<table>?<params>. Возвращает обновлённые строки или None.
    """
    url = f"{SUPABASE_URL}/rest/v1/{table}"

    headers = _headers()
    headers["Prefer"] = "return=representation"

    try:
        response = requests.patch(
            url,
            headers=headers,
            params=params,
            json=payload,
            timeout=10,
        )

        response.raise_for_status()

        logger.info("PATCH %s -> %s", table, response.status_code)

        return _json_or_none(response, table, "PATCH")

    except requests.exceptions.RequestException as e:
        logger.error("PATCH %s failed: %s", table, e)
        return None


def rest_delete(table: str, params: dict = None) -> bool:
    """DELETE /rest/v1/<table>. True при успехе (204 No Content)."""
    url = f"{SUPABASE_URL}/rest/v1/{table}"

    try:
        response = requests.delete(
            url,
            headers=_headers(),
            params=params,
            timeout=10,
        )

        response.raise_for_status()

        logger.info("DELETE %s -> %s", table, response.status_code)

        return True

    except requests.exceptions.RequestException as e:
        logger.error("DELETE %s failed: %s", table, e)
        return False


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
    warehouse: str = WAREHOUSE,
):
    """
    Количество сохранённых исключений робота за смену.

    Считаем по таблице exceptions_glpc (источник для отчётов). Берём только
    номер робота, чтобы не тянуть текстовые поля на каждое сообщение.
    """
    robot_str = str(robot).strip()

    total = rest_count(
        "exceptions_glpc",
        params={
            "issue_data": f"eq.{shift_date}",
            "shift_type": f"eq.{shift_name}",
            "warehouse": f"eq.{warehouse}",
            "error_robot": f"eq.{robot_str}",
        },
    )

    if total is None:
        logger.warning(
            "Не удалось посчитать ошибки робота %s за %s/%s",
            robot_str,
            shift_date,
            shift_name,
        )
        return 0

    return total


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


def shift_report_data(
    shift_date: str,
    shift_name: str,
    warehouse: str = WAREHOUSE,
    maintenance_threshold: int = 3,
):
    """
    Все метрики смены одним запросом (для отчёта).

    Возвращает dict:
        total              — всего исключений
        robots             — {robot: count}
        types              — {issue_type: count}
        employees          — {employee: count}
        downtime_minutes   — сумма solving_time (минуты)
        maintenance        — [(robot, count), ...] у кого count >= порога
    """
    rows = get_shift_exceptions(
        shift_date,
        shift_name,
        warehouse=warehouse,
        limit=2000,
    )

    if rows is not None and len(rows) >= 2000:
        logger.warning(
            "За %s/%s вернулось %s строк (лимит) — метрики могут быть неполными",
            shift_date,
            shift_name,
            len(rows),
        )

    if rows is None:
        # Ошибка запроса: выдавать это за «смена без исключений» нельзя.
        logger.error(
            "Не удалось получить исключения за %s/%s — метрики недоступны",
            shift_date,
            shift_name,
        )
        return None

    by_robot: dict = {}
    by_type: dict = {}
    by_employee: dict = {}
    downtime = 0

    for exc in rows or []:
        robot = str(exc.get("error_robot"))
        by_robot[robot] = by_robot.get(robot, 0) + 1

        issue_type = (
            exc.get("issue_type")
            or exc.get("first_column")
            or "unknown"
        )
        by_type[issue_type] = by_type.get(issue_type, 0) + 1

        employee = exc.get("employee") or "unknown"
        by_employee[employee] = by_employee.get(employee, 0) + 1

        solving_time = exc.get("solving_time")

        if isinstance(solving_time, (int, float)):
            downtime += int(solving_time)

    maintenance = [
        (robot, count)
        for robot, count in by_robot.items()
        if count >= maintenance_threshold
    ]
    maintenance.sort(key=lambda item: (-item[1], _robot_sort_key(item[0])))

    return {
        "total": len(rows or []),
        "robots": by_robot,
        "types": by_type,
        "employees": by_employee,
        "downtime_minutes": downtime,
        "maintenance": maintenance,
    }


def _robot_sort_key(robot: str):
    """Сортировка роботов: числовые номера — по значению, остальные — по строке."""
    text = str(robot)

    return (0, int(text), "") if text.isdigit() else (1, 0, text)


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

    if not match or not match[0]:
        return None

    matched_title, score, index = match

    logger.info(
        "Template similarity: %.1f%% | '%s'",
        score,
        matched_title,
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
    """
    Записывает исключение в Supabase (exceptions + exceptions_glpc).

    Возвращает результат POST /exceptions (список созданных строк)
    при успехе, иначе None. Сообщения об ошибках отправляет в чат.
    """
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
        logger.error("Failed to fetch exception templates")

        notify_user(
            chat_id,
            "⚠️ Can't load issue templates right now. "
            "Please try again in a minute.",
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
        logger.warning(
            "Template not found for text: %r",
            parsed["error_text"],
        )

        notify_user(
            chat_id,
            "⚠️ Can't recognize the issue description. "
            "Please check the text and try again.",
        )

        return None

    logger.info(
        "Template found: %s (id=%s)",
        best_match.get("employee_title"),
        best_match.get("id"),
    )

    # ========================================================
    # FIND EMPLOYEE
    # ========================================================

    employee_data = _rest_get(
        "employees",
        params={
            "select": "*",
            "user_name": f"eq.{table_lines['employee']}",
            "order": "id.asc",
            "limit": "1",
        },
    )

    if employee_data is None:
        # Сбой чтения: нельзя говорить «сотрудник не найден».
        logger.error(
            "Не удалось проверить сотрудника %r — база недоступна",
            table_lines.get("employee"),
        )

        notify_user(
            chat_id,
            "⚠️ Can't check the employee right now (database error). "
            "Please try again in a minute.",
        )

        return None

    if not employee_data:

        logger.warning(
            "Employee not found: %r",
            table_lines["employee"],
        )

        notify_user(
            chat_id,
            "⚠️ Employee not found. "
            "The issue was not saved. "
            "Please check your name and try again.",
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
            minutes=int(best_match.get("solving_time") or 0)
        )
    )

    end_time_iso = end_time.isoformat()

    pretty_datetime = now.strftime(
        "%d.%m.%Y %H:%M:%S"
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

    if robot_data is None:
        # Сбой чтения: робота НЕ ставим в очередь (иначе на сетевом сбое
        # в очереди появятся фантомные роботы).
        logger.error(
            "Не удалось проверить робота #%s — база недоступна",
            table_lines.get("robot"),
        )

        notify_user(
            chat_id,
            "⚠️ Can't check the robot right now (database error). "
            "Please try again in a minute.",
        )

        return None

    if not robot_data:
        obj = {
            "robot_number": table_lines['robot'],
            "employee_id": employee.get("card_id"),
            "warehouse": WAREHOUSE,
        }

        queued = _rest_post(
            "robots_to_add",
            obj,
            ignore_conflict=True,
        )

        if not queued:
            # Строка уже могла быть в очереди (409) — это не ошибка,
            # но если не вышло вообще, стоит знать.
            logger.info(
                "Робот #%s не добавлен в robots_to_add (возможно, уже в очереди)",
                table_lines["robot"],
            )

        logger.warning(
            "Robot not found: #%s",
            table_lines["robot"],
        )

        notify_user(
            chat_id,
            f"⚠️ Robot #{table_lines['robot']} is not in the system.\n"
            "The issue was forwarded to Lark, "
            "the robot is queued to be added.",
        )

        # Возвращаем маркер: запись в базу невозможна (нет робота), но
        # вызывающий код всё равно перешлёт ошибку в Lark-группу, чтобы
        # смена её увидела, а не потеряла.
        return {
            "robot_missing": True,
            "robot": str(table_lines["robot"]),
        }

    robot = robot_data[0]

    # ========================================================
    # CURRENT SHIFT
    # ========================================================

    shift_date, shift_name = (
        get_current_shift(now)
    )

    logger.info(
        "Saving exception: robot=%s employee=%s shift=%s/%s",
        robot.get("robot_number"),
        employee.get("user_name"),
        shift_date,
        shift_name,
    )

    # ========================================================
    # NEW EXCEPTION OBJECT (таблица exceptions)
    # ========================================================

    obj = {
        "workstation_id": None,

        "robot_id": robot.get("id"),

        "handle_by": employee.get("card_id"),

        "start_time": now_iso,

        "end_time": end_time_iso,

        "exception_id": best_match.get("id"),

        "shift_type": shift_name,

        "warehouse": employee.get("home_warehouse"),
    }

    # ========================================================
    # OLD EXCEPTION OBJECT (таблица exceptions_glpc)
    # ========================================================

    old_obj = {
        "error_robot": robot.get("robot_number"),

        "add_by": employee.get("card_id"),

        "device_type": robot.get("robot_type"),

        "employee": employee.get("user_name"),

        "error_end_time": end_time_iso,

        "error_start_time": now_iso,

        "first_column": best_match.get("issue_sub_type"),

        "issue_description": best_match.get("issue_description"),

        "issue_type": best_match.get("issue_type"),

        "recovery_title": best_match.get("recovery_title"),

        "second_column": best_match.get("issue_sub_type"),

        "solving_time": int(best_match.get("solving_time") or 0),

        "uniq_key": (
            f"{employee.get('user_name')}."
            f"{robot.get('robot_number')}."
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

    # ignore_conflict: при уникальном индексе по uniq_key повторная доставка
    # апдейта даёт 409 — это «уже сохранено», а не ошибка.
    saved_old = _rest_post(
        "exceptions_glpc",
        old_obj,
        ignore_conflict=True,
    )

    # ========================================================
    # CHECK RESULT
    # ========================================================

    if not saved:

        logger.error("Failed to save exception")

        notify_user(
            chat_id,
            "⚠️ Failed to save the issue. "
            "Please try again.",
        )

        return None

    if not saved_old:
        logger.warning(
            "Запись в exceptions_glpc не добавлена "
            "(уже есть или сбой) — отчёты по этой смене могут недосчитать её"
        )

    logger.info(
        "Exception saved: robot=%s time=%s shift=%s",
        robot.get("robot_number"),
        pretty_datetime,
        shift_name,
    )

    def _row_id(rows):
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return rows[0].get("id")

        return None

    return {
        "status": "saved",
        "rows": saved,
        "glpc_id": _row_id(saved_old),
        "exception_id": _row_id(saved),
        "robot": str(table_lines.get("robot")),
    }
