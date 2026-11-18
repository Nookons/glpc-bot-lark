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
from text_utils import VALUE_LIMIT, truncate
from time_utils import parse_iso


logger = setup_logging(__name__)

WARSAW_TZ = ZoneInfo("Europe/Warsaw")

ONLINE = "在线 | Online"
OFFLINE = "离线 | Offline"

#: «В ремонте» — новое состояние починки.
#:
#: 🔴 **Почему `/offline` теперь ставит `maintenance`, а не `offline`**
#: (30.10.2026). База требует: **проблему можно записать только единице в
#: `maintenance`** (триггер `enforce_problem_only_in_repair`).
#:
#: ## Что было сломано
#:
#: Бот ставил `离线 | Offline` **и** `type_problem` в одном UPDATE — база
#: отвечала `23514: Only equipment in maintenance can carry a problem`, PATCH
#: падал, и **статус не менялся вообще**. Снаружи это выглядело как
#: «`/offline` не работает».
#:
#: ## Как правильно
#:
#: `device → go to repair` — это **maintenance**: единица выведена из работы, у
#: неё есть причина, и с этого момента идёт простой. `offline` в новой системе —
#: «связи нет», без причины.
#:
#: 🔴 Триггер `equipment_canonical_status` знает `维护`/`maintenance`, поэтому
#: легаси-зеркало и каноническая таблица согласованы.
MAINTENANCE = "维护 | Maintenance"


DIRECTIONS = {
    "offline": {
        "new_status": MAINTENANCE,
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

#: Канонический реестр оборудования — **основной источник** для команд.
#:
#: 🔴 **Почему** (владелец 18.11.2026: «почему /offline 9999 пишет, что робота
#: нет»). Команды искали робота в легаси-зеркале `robots_maintenance_list`, а там
#: у GLP-C всего 52 строки из 2 191: 3 231 единица заведена вручную
#: (`legacy_table = 'manual'`). Робот 9999 есть в `equipment` и `online`, но в
#: легаси его нет — и бот отвечал «not found».
#:
#: Легаси остаётся запасным путём для строк, которых в реестре почему-то нет.
EQUIPMENT_TABLE = "equipment"
EQUIPMENT_TYPES_TABLE = "equipment_types"

#: Канонический статус → подпись, которой живёт бот и старые экраны.
CANONICAL_TO_LEGACY = {
    "online": ONLINE,
    "offline": OFFLINE,
    "maintenance": MAINTENANCE,
    "not_in_work": "Not in work",
    "unknown": "Unknown",
}

#: Обратное преобразование — только для записи в `equipment`.
LEGACY_TO_CANONICAL = {
    ONLINE: "online",
    OFFLINE: "offline",
    MAINTENANCE: "maintenance",
    "在线 | online": "online",
    "离线 | offline": "offline",
    "维护 | maintenance": "maintenance",
    "online": "online",
    "offline": "offline",
    "maintenance": "maintenance",
}

_TYPE_NAMES: dict = {}


def display_status(value) -> str:
    """Статус для человека и старых читателей: `online` → `在线 | Online`."""
    text = str(value or "").strip()

    if not text:
        return ""

    return CANONICAL_TO_LEGACY.get(text.casefold(), text)


def canonical_status(value) -> str:
    """Статус для записи в `equipment`. Неизвестное значение не выдумываем."""
    text = str(value or "").strip()

    return LEGACY_TO_CANONICAL.get(text, LEGACY_TO_CANONICAL.get(text.casefold(), text))


def _type_name(type_id):
    """Название модели по id — маленький кэш, чтобы не дёргать базу на каждый чих."""
    if not type_id:
        return None

    if type_id in _TYPE_NAMES:
        return _TYPE_NAMES[type_id]

    rows = rest_get(
        EQUIPMENT_TYPES_TABLE,
        params={"select": "type", "id": f"eq.{type_id}", "limit": "1"},
        optional=True,
    )
    name = (rows[0].get("type") if rows else None)
    _TYPE_NAMES[type_id] = name

    return name


def _equipment_robot(row: dict) -> dict:
    """Строка `equipment` в форме, которую ждут команды и карточка."""
    return {
        "id": row.get("id"),
        "robot_number": row.get("equipment_code"),
        "status": display_status(row.get("status")),
        "warehouse": row.get("warehouse"),
        "robot_type": _type_name(row.get("equipment_type_id")),
        "type_problem": row.get("problem_type"),
        "problem_note": row.get("problem_note"),
        "updated_at": row.get("updated_at"),
        "legacy_id": row.get("legacy_id"),
        "legacy_table": row.get("legacy_table"),
        # Служебные поля: по ним `change_robot_status` выбирает таблицу записи.
        "_source": "equipment",
        "_canonical_status": row.get("status"),
    }

#: Поручения на ремонт — **каноническая** таблица новой системы.
#:
#: 🔴 **Почему бот пишет сюда, а не в легаси** (30.10.2026).
#:
#: ## Что было не так
#:
#: `/offline` менял только `robots_maintenance_list.status`. Триггер
#: `equipment_sync_legacy` переносил статус в `equipment`, поэтому робот
#: «пропадал» из работы — но **поручения на ремонт не появлялось**.
#:
#: Следствие: панель простоя и отчёт ремонта считают время по
#: `repair_assignments.offline_at → closed_at`. Раз записи нет — нет ни часов
#: ремонта, ни строки в списке ремонтов, хотя робот стоит.
#:
#: ## Новая система починки
#:
#: `device → go to repair` = запись с `offline_at`; `return to service` =
#: `closed_at`. Бот обязан делать то же самое, иначе он живёт в отдельном мире
#: от веб-интерфейса.
REPAIRS_TABLE = "repair_assignments"


class StatusUnavailable(RuntimeError):
    """Не удалось прочитать данные робота: база недоступна."""


def reason_label(direction: str, code: str):
    """Подпись причины по её коду (или None)."""
    for item_code, label in REASONS.get(direction, ()):
        if item_code == code:
            return label

    return None


def find_robot(robot_number, warehouse: str = WAREHOUSE, strict: bool = False):
    """
    Робот склада: строка канонического реестра `equipment` (или легаси).

    strict=True — при сбое чтения бросает StatusUnavailable: «робот не
    найден» и «база недоступна» — разные вещи.

    🔴 **Сначала `equipment`, потом легаси.** Команды раньше смотрели только в
    `robots_maintenance_list`, где у GLP-C 52 строки из 2 191; роботы, заведённые
    вручную, для бота не существовали. `equipment` — тот же реестр, что у веба и
    приёма ошибок.
    """
    try:
        number = int(str(robot_number).strip().lstrip("#"))
    except (TypeError, ValueError):
        return None

    rows = rest_get(
        EQUIPMENT_TABLE,
        params={
            "select": "*",
            "warehouse": f"eq.{warehouse}",
            "category": "eq.robot",
            "equipment_code": f"eq.{number}",
            "order": "updated_at.desc",
            "limit": "1",
        },
    )

    if rows is None:
        logger.error("Не удалось найти робота #%s — база недоступна", robot_number)

        if strict:
            raise StatusUnavailable("robot lookup failed")

        return None

    if rows:
        return _equipment_robot(rows[0])

    # Запасной путь: строки, которых нет в каноническом реестре.
    legacy = rest_get(
        ROBOTS_TABLE,
        params={
            "select": "*",
            "robot_number": f"eq.{number}",
            "warehouse": f"eq.{warehouse}",
            "order": "updated_at.desc",
            "limit": "1",
        },
    )

    if legacy is None:
        if strict:
            raise StatusUnavailable("robot lookup failed")

        return None

    if not legacy:
        return None

    row = dict(legacy[0])
    row["_source"] = "legacy"

    return row


def find_robot_by_id(robot_id, strict: bool = False):
    """Робот по идентификатору из callback-кнопки.

    Новые кнопки несут `equipment.id`; старые (до 18.11.2026) — id легаси-строки.
    Поэтому сначала ищем по `equipment.id`, затем по `legacy_id`, и только потом
    в легаси-таблице: иначе кнопка, нажатая до выката, отвечала бы «not found».
    """
    try:
        key = int(robot_id)
    except (TypeError, ValueError):
        return None

    rows = rest_get(
        EQUIPMENT_TABLE,
        params={"select": "*", "id": f"eq.{key}", "category": "eq.robot", "limit": "1"},
    )

    if rows is None:
        logger.error("Не удалось прочитать робота id=%s — база недоступна", robot_id)

        if strict:
            raise StatusUnavailable("robot lookup failed")

        return None

    if rows:
        return _equipment_robot(rows[0])

    by_legacy = rest_get(
        EQUIPMENT_TABLE,
        params={
            "select": "*",
            "legacy_id": f"eq.{key}",
            "legacy_table": "eq.robots_maintenance_list",
            "limit": "1",
        },
    )

    if by_legacy:
        return _equipment_robot(by_legacy[0])

    legacy = rest_get(
        ROBOTS_TABLE,
        params={"select": "*", "id": f"eq.{key}", "limit": "1"},
    )

    if legacy is None:
        if strict:
            raise StatusUnavailable("robot lookup failed")

        return None

    if not legacy:
        return None

    row = dict(legacy[0])
    row["_source"] = "legacy"

    return row


def find_equipment(robot: dict):
    """Единица оборудования новой системы по строке легаси-зеркала.

    🔴 **Ищем по `legacy_id`**, а не по номеру: номера повторяются между
    складами (`121` есть и в GLP-C1, и в SMALL-P3), и поиск по номеру без склада
    вернул бы чужую единицу. `legacy_id` — первичный ключ строки зеркала, он
    однозначен.
    """
    if robot.get("_source") == "equipment":
        # Строка уже из канонического реестра: возвращаем её же, не делая второй
        # запрос. `id` здесь — `equipment.id`, `equipment_code` — номер.
        return {
            "id": robot.get("id"),
            "equipment_code": robot.get("robot_number"),
            "warehouse": robot.get("warehouse"),
            "status": robot.get("_canonical_status") or canonical_status(robot.get("status")),
            "category": "robot",
        }

    legacy_id = robot.get("id")

    if legacy_id is None:
        return None

    rows = rest_get(
        "equipment",
        params={
            "select": "id, equipment_code, warehouse, status, category",
            "legacy_table": "eq.robots_maintenance_list",
            "legacy_id": f"eq.{legacy_id}",
            "limit": "1",
        },
    )

    return rows[0] if rows else None


def open_repair(robot: dict, employee: dict, reason: str, note: str):
    """Открывает поручение на ремонт: `device → go to repair`.

    🔴 **Пишем в каноническую `repair_assignments`.** Именно её читают панель
    простоя, отчёт смены и веб-список ремонтов; запись в легаси-таблицу они не
    видят.

    Возвращает id записи или `None` (тогда бот скажет об этом честно).
    """
    equipment = find_equipment(robot)

    if not equipment:
        # 🔴 **Без единицы оборудование ремонт не заводим.** `warehouse`
        # обязателен, а `equipment_id` без записи в `equipment` дал бы висячую
        # строку, которую не открыть в интерфейсе.
        logger.warning(
            "Не завёл ремонт для робота #%s: нет единицы в equipment",
            robot.get("robot_number"),
        )

        return None

    card_id = (employee or {}).get("card_id")
    moment = datetime.now(timezone.utc).isoformat()

    created = rest_post(
        REPAIRS_TABLE,
        {
            "equipment_id": equipment.get("id"),
            "equipment_code": equipment.get("equipment_code"),
            "warehouse": equipment.get("warehouse"),
            "card_id": card_id,
            "assigned_by": card_id,
            "assigned_at": moment,
            # 🔴 `offline_at` — начало простоя. С него считается время ремонта;
            # без него запись не даёт ни одного часа.
            "offline_at": moment,
            "status": "open",
            "reason": reason,
            "note": note or "",
        },
    )

    if not created:
        logger.warning(
            "Статус робота #%s изменён, но поручение на ремонт не создалось",
            robot.get("robot_number"),
        )

        return None

    row = created[0] if isinstance(created, list) else created

    return row.get("id") if isinstance(row, dict) else None


def close_repair(robot: dict, employee: dict, solution: str, note: str):
    """Закрывает открытое поручение: `return to service`.

    🔴 **Закрываем последнее открытое**, а не все: у единицы могло быть несколько
    ремонтов за историю, и трогать закрытые нельзя — они уже посчитаны в
    простое.

    Возвращает число закрытых записей.
    """
    equipment = find_equipment(robot)

    if not equipment:
        return 0

    open_rows = rest_get(
        REPAIRS_TABLE,
        params={
            "select": "id, offline_at",
            "equipment_id": f"eq.{equipment.get('id')}",
            "status": "eq.open",
            "order": "assigned_at.desc",
            "limit": "1",
        },
    )

    if not open_rows:
        # Ремонта не было — это нормально: робота вернули, а поручения не
        # заводили (например, простой по ошибке). Молча выходим.
        return 0

    row = open_rows[0]
    moment = datetime.now(timezone.utc)
    card_id = (employee or {}).get("card_id")

    payload = {
        "status": "done",
        "closed_at": moment.isoformat(),
        "closed_by": card_id,
        "solution": solution,
        "note": note or "",
    }

    started = row.get("offline_at")

    if started:
        try:
            begin = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
            payload["duration_seconds"] = int((moment - begin).total_seconds())
        except ValueError:
            pass

    updated = rest_patch(
        REPAIRS_TABLE,
        params={"id": f"eq.{row.get('id')}"},
        payload=payload,
    )

    return 1 if updated else 0


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

    # 🔴 **Порядок шагов задаёт база, а не удобство** (30.10.2026).
    #
    # Два правила, найденные на живой базе:
    #
    # | Правило | Триггер | Что значит для порядка |
    # |---|---|---|
    # | проблему можно ставить **только** в `maintenance` | `enforce_problem_only_in_repair` | `/offline` пишет `maintenance`, а не `offline` |
    # | в `online` нельзя, **пока ремонт открыт** | `enforce_repair_opens_on_offline_unit` | `/online` сначала закрывает ремонт |
    #
    # 🔴 **Оба правила сначала выглядели как «команда не работает»:** PATCH
    # отвечал `400`, статус не менялся, и снаружи не было видно причины.
    #
    # Поэтому для возврата в работу ремонт закрывается **до** смены статуса, а
    # для вывода из работы — **после**: сначала единица становится
    # `maintenance`, иначе база не разрешит открыть ремонт.
    if direction == "online":
        close_repair(robot, employee, type_problem, problem_note)

    source = robot.get("_source") or "legacy"
    old_status = str(robot.get("status") or "")
    new_status = spec["new_status"]
    changed_at = datetime.now(timezone.utc).isoformat()
    card_id = (employee or {}).get("card_id")

    # В `equipment` статусы канонические (`maintenance`), в легаси — подписи
    # (`维护 | Maintenance`). Пишем в ту таблицу, откуда пришла строка.
    payload = {
        "status": canonical_status(new_status) if source == "equipment" else new_status,
        "updated_at": changed_at,
        "updated_by": card_id,
    }

    problem_field = "problem_type" if source == "equipment" else "type_problem"

    if direction == "offline":
        # Дашборд показывает «CURRENT ISSUE» из самой карточки робота,
        # поэтому тип проблемы и заметку пишем и сюда, а не только в журнал.
        payload[problem_field] = type_problem
        payload["problem_note"] = problem_note or ""
    else:
        # Робот вернулся в работу — текущая проблема больше не актуальна.
        payload[problem_field] = None
        payload["problem_note"] = None

    table = EQUIPMENT_TABLE if source == "equipment" else ROBOTS_TABLE

    updated = rest_patch(
        table,
        params={"id": f"eq.{robot['id']}"},
        payload=payload,
    )

    if not updated:
        logger.error(
            "Не удалось изменить статус робота #%s",
            robot.get("robot_number"),
        )
        return None

    # 🔴 **`robot_id` — идентификатор КАНОНИЧЕСКОЙ единицы** (30.10.2026).
    #
    # ## Что было не так
    #
    # Писался `robot.get("id")` — первичный ключ **легаси-зеркала**
    # `robots_maintenance_list`. У `change_status_robots.robot_id` есть внешний
    # ключ на `equipment.id`, а легаси-ключи туда не входят: у робота 544 это
    # `20267`, и такой единицы в реестре нет.
    #
    # Следствие: **вставка падала с нарушением внешнего ключа**, а код писал
    # только предупреждение в лог. Журнал статусов не пополнялся — и история
    # ремонта на вебе выглядела странно: старый процесс от 22 октября висел как
    # «in repair now», потому что перехода 29–30 октября в журнале не было.
    #
    # 🔴 **`legacy_id` остаётся в строке отдельным полем** — по нему историю
    # находят старые экраны, и терять его нельзя.
    equipment_row = find_equipment(robot)

    history = rest_post(
        STATUS_HISTORY_TABLE,
        {
            "robot_id": (equipment_row or {}).get("id") or robot.get("id"),
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

    # 🔴 **Новая система починки: устройство → в ремонт / из ремонта**
    # (30.10.2026).
    #
    # ## Чего не хватало
    #
    # Статус менялся, триггер переносил его в `equipment`, и робот пропадал из
    # работы. Но **поручения на ремонт не появлялось**, а панель простоя и отчёт
    # ремонта считают часы именно по `repair_assignments.offline_at → closed_at`.
    # Робот стоит, часов нет.
    #
    # ## Что теперь
    #
    # * `/offline` → открытая запись с `offline_at` (время пошло);
    # * `/online` → `closed_at` и `duration_seconds` (время посчитано).
    #
    # 🔴 **Статус и ремонт — одно действие, а не два.** Если запись не создалась,
    # это не повод откатывать статус (робот уже снят), но и молчать нельзя:
    # предупреждение в лог, чтобы расхождение было видно.
    # 🔴 Открываем ремонт **после** смены статуса: триггер
    # `enforce_repair_opens_on_offline_unit` не даст открыть его единице в
    # `online`, а статус меняется как раз выше.
    repair_id = None

    if direction == "offline":
        repair_id = open_repair(robot, employee, type_problem, problem_note)

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
        # 🔴 Идентификатор открытого ремонта: по нему бот показывает, что время
        # простоя уже идёт, и веб-интерфейс найдёт ту же запись.
        "repair_id": repair_id,
        "problem_note": problem_note or "",
        "changed_at": changed_at,
        "history_saved": history is not None,
    }


# ============================================================
# ОФОРМЛЕНИЕ
# ============================================================

def _warsaw_time(changed_at: str) -> str:
    moment = parse_iso(changed_at)

    if moment is None:
        return ""

    return moment.astimezone(WARSAW_TZ).strftime("%d.%m.%Y %H:%M")


def status_title(direction: str, robot: dict) -> str:
    spec = DIRECTIONS[direction]

    return f"{spec['emoji']} Robot {robot.get('robot_number')} → {spec['label']}"


def build_status_card(
    direction: str,
    result: dict,
    employee_name: str,
    media_url: str = None,
    media_kind: str = "photo",
) -> dict:
    """Карточка для Lark-группы.

    `media_url` — фото или видео, которое оператор прислал вместе со сменой
    статуса. Ссылка идёт кнопкой: Lark принимает в карточке только свой
    `img_key`, а файл лежит в нашем Storage, поэтому кнопка честнее
    неработающей картинки.
    """
    spec = DIRECTIONS[direction]
    robot = result["robot"]
    note = truncate(result.get("problem_note") or "—", VALUE_LIMIT)

    card = {
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

    if media_url:
        label = "🎬 Open video" if media_kind == "video" else "📷 Open photo"
        card["elements"].insert(
            -1,
            {
                "tag": "action",
                "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": label},
                    "type": "primary",
                    "url": media_url,
                }],
            },
        )

    return card


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
