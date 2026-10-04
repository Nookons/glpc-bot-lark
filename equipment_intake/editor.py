"""
Telegram-редактор дерева решений приёма ошибок склада.

Задача редактора — полностью настраивать дерево из Telegram, без правки кода и
деплоя. Встроенное дерево (`tree_config.DEFAULT_TREE`) остаётся авторитетной
логикой в коде, а все изменения живут в Supabase отдельным слоем
переопределений (см. `storage.py` и `sql/intake_editor_v2.sql`):

  * правка подписи, описания, иконки ЛЮБОГО варианта, включая встроенные;
  * скрытие/показ встроенных вариантов (вариант остаётся в коде);
  * добавление и удаление добавленных вариантов;
  * порядок вариантов — выше/ниже;
  * правка узла: заголовок, описание, подсказка, переход;
  * скрытие узла целиком (вместо удаления — см. пояснение ниже);
  * навигация по дереву с понятным путём (breadcrumb) и обзором структуры.

Почему нет создания и удаления узлов. В дереве ссылки только вперёд
(`next_node`), обратных связей нет. Удалить узел в такой модели нельзя: у
родителей останутся висячие ссылки, а восстановить, кто на узел ссылался,
неоткуда. Создание узла без правки чужих `next_node` тоже бессмысленно —
новый узел никто не вызовет. Поэтому редактор умеет безопасное подмножество:
правку существующих узлов и их скрытие (ссылки на скрытый узел схлопываются
на его следующий узел, см. `storage.hidden_node_target`).

Доступ. Полное редактирование — административное действие; список админов
берётся из `TELEGRAM_ADMIN_USER_IDS` и/или из `employees.is_leader` (см.
`editor_role`). Если не задано ни то, ни другое, редактор остаётся доступен
всем сотрудникам в прежнем узком режиме «только добавить вариант» — так
обновление не отнимает у людей возможность, которой они пользуются.

UI редактора живёт в отдельном пространстве callback_data («ed:»), чтобы не
пересекаться с рабочим деревом («dt:» в flow.py). В кнопки едут только
короткие коды и числа-позиции: id узлов в дереве из реальных данных длинные
и в лимит callback_data (64 байта) не влезают.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import telegram_api as tg

from . import storage
from .tree_config import DEFAULT_TREE
from .types import DecisionTree, Node, NodeType


logger = logging.getLogger(__name__)


# ============================================================
# ДОСТУП
# ============================================================

# Типы узлов, к которым применимы варианты. У INPUT/NUMBER/FINAL кнопок нет.
_OPTION_NODE_TYPES = (NodeType.CHOICE, NodeType.MULTI, NodeType.YESNO)

# Роли редактора:
#   ADMIN       — полное редактирование (включая скрытие, удаление, порядок,
#                 правку узлов);
#   CONTRIBUTOR — прежний режим: добавить вариант и посмотреть дерево;
#   NONE        — доступа нет.
ROLE_ADMIN = "admin"
ROLE_CONTRIBUTOR = "contributor"
ROLE_NONE = "none"

# Как долго помним результат проверки «этот человек лидер?». Ходить в базу
# на каждое нажатие кнопки нельзя, но и вечно кэшировать нельзя: лидера
# могут снять.
LEADER_CACHE_TTL_SECONDS = 300

_LEADER_CACHE: Dict[int, Tuple[bool, float]] = {}
_LEADER_LOCK = threading.Lock()


def _admin_user_ids() -> set:
    """
    Разбирает TELEGRAM_ADMIN_USER_IDS: id через запятую, пробел или точку с запятой.

    Значение — явный белый список администраторов редактора. Если переменная
    не задана, список пуст: это не «никому нельзя», а «решают лидеры» (см.
    `editor_role`).
    """
    raw = os.environ.get("TELEGRAM_ADMIN_USER_IDS") or ""
    result = set()

    for chunk in raw.replace(";", ",").replace(" ", ",").split(","):
        chunk = chunk.strip()

        if not chunk:
            continue

        try:
            result.add(int(chunk))
        except ValueError:
            logger.warning(
                "TELEGRAM_ADMIN_USER_IDS: %r не является id — пропускаю", chunk
            )

    return result


def _leaders_enabled() -> bool:
    """
    Учитывать ли `employees.is_leader`.

    По умолчанию да: в проекте уже есть понятие лидера (дайджесты ходят
    именно им), и это самый дешёвый способ навести порядок без переменных.
    Отключается `TELEGRAM_ADMIN_LEADERS=false`.
    """
    raw = (os.environ.get("TELEGRAM_ADMIN_LEADERS") or "").strip().lower()

    if not raw:
        return True

    return raw in ("1", "true", "yes", "on")


def _is_leader(user_id: int) -> bool:
    """
    Сотрудник с этим Telegram-id помечен лидером в `employees`.

    Любая ошибка чтения — False: доступ лучше не выдать, чем выдать по
    случайности. Значение кэшируется на LEADER_CACHE_TTL_SECONDS.
    """
    now = time.time()

    with _LEADER_LOCK:
        cached = _LEADER_CACHE.get(int(user_id))

        if cached and now - cached[1] < LEADER_CACHE_TTL_SECONDS:
            return cached[0]

    leader = False

    try:
        import telegram_store

        employee = telegram_store.get_employee(user_id)

        if isinstance(employee, dict):
            leader = bool(employee.get("is_leader"))
    except Exception:  # noqa: BLE001
        logger.exception(
            "Equipment intake editor: не удалось проверить лидера user=%s", user_id
        )
        leader = False

    with _LEADER_LOCK:
        _LEADER_CACHE[int(user_id)] = (leader, now)

    return leader


def editor_role(sender: Dict[str, Any]) -> str:
    """
    Роль отправителя в редакторе.

    Порядок решений:
      1. id в TELEGRAM_ADMIN_USER_IDS — ADMIN;
      2. задан явный белый список, а человека в нём нет — NONE
         (осознанное ограничение: если владелец перечислил админов, чужие
         правки не нужны);
      3. `employees.is_leader = true` — ADMIN;
      4. белый список пуст и человек не лидер — CONTRIBUTOR.

    Шаг 4 — обратная совместимость: до этой версии редактор был открыт всем и
    умел только добавлять варианты. Такое поведение остаётся доступным, пока
    владелец не задал ни переменную, ни лидеров. Сломать структуру дерева
    CONTRIBUTOR не может: ему доступно только добавление.
    """
    if not isinstance(sender, dict) or sender.get("id") is None:
        return ROLE_NONE

    user_id = int(sender["id"])
    admins = _admin_user_ids()

    if user_id in admins:
        return ROLE_ADMIN

    if admins:
        return ROLE_NONE

    if _leaders_enabled() and _is_leader(user_id):
        return ROLE_ADMIN

    return ROLE_CONTRIBUTOR


def is_editor(sender: Dict[str, Any]) -> bool:
    """Есть ли у человека доступ к редактору (любая роль, кроме NONE)."""
    return editor_role(sender) != ROLE_NONE


def reset_access_cache() -> None:
    """Сброс кэша лидеров — только для тестов."""
    with _LEADER_LOCK:
        _LEADER_CACHE.clear()


#: Что сказать, когда правка не удалась из-за неприменённой миграции.
#:
#: Раньше в этом случае показывалось «⚠️ Option not changed.» — админ видел, что
#: ничего не произошло, но **не понимал, почему**: выглядело как сбой бота, а не
#: как неприменённая миграция. Теперь причина названа и указано, что делать.
SCHEMA_NOT_READY_MESSAGE = (
    "⚠️ Not saved: the database is not ready for full editing "
    "(missing column is_builtin). Apply sql/intake_editor_v2.sql. "
    "Adding options works even now."
)


def _failure_notice(done: bool, success: str) -> str:
    """
    Текст о результате правки: успех — как есть, отказ — с настоящей причиной.

    Если база не готова (нет колонок v2), отказ почти всегда именно из-за этого,
    поэтому причину назвать честнее, чем показать «не изменилось».
    """
    if done:
        return success

    if not storage.overlay_supported():
        return SCHEMA_NOT_READY_MESSAGE

    return "⚠️ Not saved. The change was rejected."


# ============================================================
# СОСТОЯНИЕ РЕДАКТОРА
# ============================================================
#
# Два независимых словаря на пару (чат, пользователь):
#   * VIEW — где человек сейчас и какое подтверждение/позиция у него открыты;
#   * PENDING — что бот ждёт от него текстом (название, описание, иконка…).
#
# Разделение важное: текстовое ожидание перехватывает сообщение
# (`handle_text`), а вид — нет. Пока человек не нажал «Rename», его обычные
# сообщения должны идти в рабочий флоу приёма ошибок.

# Сколько ждём текст, прежде чем забыть про начатую правку.
PENDING_TTL_SECONDS = 600

# Сколько живёт открытое меню редактора без действий.
VIEW_TTL_SECONDS = 1800

# callback_data ограничена 64 байтами, поэтому в кнопку едет только код
# действия и короткий аргумент (позиция, страница, флаг).
CB_PREFIX = "ed"

_PENDING: Dict[Tuple[int, int], Dict[str, Any]] = {}
_VIEWS: Dict[Tuple[int, int], Dict[str, Any]] = {}
_LOCK = threading.Lock()

# Виды текстовых ожиданий.
KIND_ADD_LABEL = "add_label"
KIND_OPTION_LABEL = "option_label"
KIND_OPTION_DESCRIPTION = "option_description"
KIND_OPTION_ICON = "option_icon"
KIND_NODE_TITLE = "node_title"
KIND_NODE_DESCRIPTION = "node_description"
KIND_NODE_PLACEHOLDER = "node_placeholder"


def _key(chat_id, user_id) -> Tuple[int, int]:
    return (int(chat_id), int(user_id))


def _sweep_locked(now: float) -> None:
    for key in [k for k, v in _PENDING.items() if v["expires"] < now]:
        _PENDING.pop(key, None)

    for key in [k for k, v in _VIEWS.items() if v["expires"] < now]:
        _VIEWS.pop(key, None)


def sweep() -> int:
    """Убирает просроченные ожидания и виды, чтобы словари не росли."""
    now = time.time()

    with _LOCK:
        before = len(_PENDING) + len(_VIEWS)
        _sweep_locked(now)

        return before - (len(_PENDING) + len(_VIEWS))


def put_pending(chat_id, user_id, node_id: str, menu_message_id, kind: str = KIND_ADD_LABEL,
                option_id: Optional[str] = None) -> None:
    """Запоминает, что от этого человека ждём текст (какое поле и для чего)."""
    # sweep вызываем ДО захвата замка: threading.Lock не реентерабельный.
    sweep()

    with _LOCK:
        _PENDING[_key(chat_id, user_id)] = {
            "node_id": str(node_id),
            "option_id": option_id,
            "kind": kind,
            "menu_message_id": menu_message_id,
            "expires": time.time() + PENDING_TTL_SECONDS,
        }


def get_pending(chat_id, user_id) -> Optional[Dict[str, Any]]:
    key = _key(chat_id, user_id)

    with _LOCK:
        entry = _PENDING.get(key)

        if entry and entry["expires"] < time.time():
            _PENDING.pop(key, None)
            entry = None

        return dict(entry) if entry else None


def drop_pending(chat_id, user_id) -> None:
    with _LOCK:
        _PENDING.pop(_key(chat_id, user_id), None)


def get_view(chat_id, user_id) -> Optional[Dict[str, Any]]:
    """Текущий экран редактора у этого человека (или None)."""
    key = _key(chat_id, user_id)

    with _LOCK:
        entry = _VIEWS.get(key)

        if entry and entry["expires"] < time.time():
            _VIEWS.pop(key, None)
            entry = None

        return dict(entry) if entry else None


def put_view(chat_id, user_id, **fields) -> Dict[str, Any]:
    """
    Обновляет текущий экран редактора.

    Возвращает новое состояние целиком: вызывающий код обычно сразу
    перерисовывает экран и хочет видеть те же данные.
    """
    key = _key(chat_id, user_id)

    with _LOCK:
        entry = dict(_VIEWS.get(key) or {})
        entry.update(fields)
        entry["expires"] = time.time() + VIEW_TTL_SECONDS
        _VIEWS[key] = entry

        return dict(entry)


def drop_view(chat_id, user_id) -> None:
    with _LOCK:
        _VIEWS.pop(_key(chat_id, user_id), None)


def reset_state() -> None:
    """Сброс ожиданий и видов — только для тестов."""
    with _LOCK:
        _PENDING.clear()
        _VIEWS.clear()


# ============================================================
# TELEGRAM
# ============================================================
#
# Импортируем бота лениво: telegram_bot импортирует этот пакет, поэтому
# обратная ссылка на уровне модуля дала бы цикл.
#
# ВАЖНО: бот запускается как `python3 telegram_bot.py`, то есть его
# рабочая копия живёт в sys.modules под именем «__main__», а не
# «telegram_bot». Если сделать просто `import telegram_bot`, Python
# загрузит ВТОРУЮ копию модуля: у неё пустое состояние маршрутизации
# топиков (_routes), поэтому _reply_thread() вернёт None, и в dry-run
# сообщение будет заблокировано. Копируем логику flow._bot().
def _bot():
    import sys

    main = sys.modules.get("__main__")

    if main is not None and getattr(main, "__file__", None):
        same_file = os.path.abspath(main.__file__) == os.path.abspath(
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "telegram_bot.py")
        )

        if same_file and hasattr(main, "_reply_thread"):
            return main

    import telegram_bot

    return telegram_bot


def _thread_id(chat_id):
    """Топик, в который отвечать. Любая ошибка — не повод упасть."""
    try:
        return _bot()._reply_thread(chat_id)
    except Exception:  # noqa: BLE001
        return None


def _send(chat_id, text, message_id=None, reply_markup=None):
    """
    Отправляет сообщение редактору.

    Через _bot()._send, как flow.py: так ответ уходит в тот же топик и,
    что важнее, в dry-run не блокируется проверкой топика.
    """
    try:
        return _bot()._send(
            chat_id,
            text,
            reply_to_message_id=message_id,
            reply_markup=reply_markup,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Equipment intake editor: не удалось отправить сообщение")
        return None


def _edit(chat_id, message_id, text, reply_markup=None):
    """
    Меняет текст сообщения редактора.

    Не tg.edit_message_text: в тестовом боте включён DRY_RUN, и
    telegram_api пропускает ответ только если в payload есть
    message_thread_id. Общая функция его не передаёт, поэтому правки
    меню молча терялись бы. Вне dry-run поведение обычное.
    """
    if message_id is None:
        return None

    from text_utils import truncate

    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": truncate(text, 4096),
        "disable_web_page_preview": True,
    }

    thread_id = _thread_id(chat_id)

    if thread_id is not None:
        payload["message_thread_id"] = thread_id

    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    try:
        return tg.call("editMessageText", payload, timeout=15)
    except Exception:  # noqa: BLE001
        logger.exception("Equipment intake editor: не удалось изменить сообщение")
        return None


def _answer(callback_id, text: str = "") -> None:
    if callback_id:
        try:
            tg.answer_callback_query(callback_id, text or None)
        except Exception:  # noqa: BLE001
            logger.exception("Equipment intake editor: answerCallbackQuery упал")


def _delete_quiet(chat_id, message_id) -> None:
    if message_id is None:
        return

    try:
        _bot()._delete_quiet(chat_id, message_id)
    except Exception:  # noqa: BLE001
        logger.exception("Equipment intake editor: не удалось удалить сообщение")


# ============================================================
# ДЕРЕВО ДЛЯ РЕДАКТОРА
# ============================================================

def _tree() -> DecisionTree:
    """
    Дерево, которое видит редактор: встроенное плюс накопленные правки.

    Берём DEFAULT_TREE, а не EQUIPMENT_INTAKE_TREE_SOURCE: storage проверяет
    варианты именно по встроенному дереву, поэтому редактор и хранилище
    смотрят на один и тот же набор узлов.
    """
    return storage.apply_overlay(DEFAULT_TREE)


def _path_to(tree: DecisionTree, node_id: str) -> List[str]:
    """
    Путь от корня до узла — для кнопок-хлебных крошек.

    В дереве нет обратных ссылок (только next_node вперёд), поэтому ищем
    путь обходом в ширину. Если путь не нашёлся, возвращаем сам узел.
    """
    if node_id == tree.root_id:
        return [tree.root_id]

    queue: List[List[str]] = [[tree.root_id]]
    seen = {tree.root_id}

    while queue:
        path = queue.pop(0)
        node = tree.node(path[-1])

        if node is None:
            continue

        targets: List[str] = []

        if node.next_node:
            targets.append(node.next_node)

        for option in node.options:
            if option.next_node:
                targets.append(option.next_node)

        for target in targets:
            if target == node_id:
                return path + [target]

            if target not in seen:
                seen.add(target)
                queue.append(path + [target])

    return [node_id]


def _parent_of(tree: DecisionTree, node_id: str) -> Optional[str]:
    path = _path_to(tree, node_id)

    if len(path) >= 2:
        return path[-2]

    return None


def _node_name(tree: DecisionTree, node_id: Optional[str]) -> str:
    """Читаемое имя узла для подписи варианта (вместо голого id)."""
    if not node_id:
        return "end of branch"

    node = tree.node(node_id)

    if node is None:
        return f"missing node ({node_id})"

    return f"{node.title} [{node.id}]"


def _cb(action: str, arg: str = "") -> str:
    return f"{CB_PREFIX}:{action}" + (f":{arg}" if arg else "")


def _role_note(role: str) -> str:
    if role == ROLE_ADMIN:
        return "Role: maintainer — full editing."

    return (
        "Role: contributor — you can add options and browse the tree. "
        "Renaming, hiding and reordering are done by a maintainer."
    )


# ============================================================
# ЭКРАН УЗЛА
# ============================================================

def _node_caption(tree: DecisionTree, node: Node, role: str, notice: str = "") -> str:
    """Текст экрана узла: путь, вопрос, нумерованный список вариантов."""
    lines: List[str] = ["Decision tree editor", ""]

    # Notice живёт в самом экране узла, а не отдельным сообщением: экран
    # перерисовывается на том же message_id и затёр бы подтверждение.
    if notice:
        lines.append(notice)
        lines.append("")

    path = _path_to(tree, node.id)
    trail = " › ".join(
        (tree.node(item).title if tree.node(item) else item)[:24] for item in path
    )
    lines.append(f"📍 {trail}")
    lines.append("")

    if node.type in _OPTION_NODE_TYPES:
        lines.append(f"❓ {node.title}")
    else:
        lines.append(f"📄 {node.title}")

    lines.append(f"{node.id} · {node.type.value}")

    if node.description:
        lines.append(f"_{node.description}_")

    if node.placeholder:
        lines.append(f"placeholder: {node.placeholder}")

    if node.next_node:
        lines.append(f"next: {_node_name(tree, node.next_node)}")

    if node.is_stub:
        lines.append("(draft branch — no choices yet; employees describe it in words)")

    lines.append("")

    items = storage.effective_options(DEFAULT_TREE, node.id, storage.load_option_rows() or [])

    if not node.options:
        lines.append("Options: — none —")
    else:
        lines.append(f"Options ({len([i for i in items if not i['hidden']])} shown):")

    for index, item in enumerate(items):
        mark = "built-in" if item["kind"] == "builtin" else item["kind"]
        state = " 🙈 hidden" if item["hidden"] else ""
        icon = f"{item['icon']} " if item["icon"] else ""
        lines.append(f"{index + 1}. {icon}{item['label']}{state}")
        lines.append(f"     {mark} · → {_node_name(tree, item['next_node'])}")

    lines.append("")
    lines.append(_role_note(role))

    return "\n".join(lines)


def _node_keyboard(tree: DecisionTree, node: Node, role: str, items: List[dict]) -> Dict[str, list]:
    """
    Клавиатура экрана узла.

    Каждый вариант — отдельная кнопка, открывающая его карточку: так список
    остаётся читаемым, а все разрушительные действия живут внутри карточки,
    за подтверждением. Ниже — навигация и обзор структуры.
    """
    rows: List[list] = []

    for index, item in enumerate(items):
        state = "🙈 " if item["hidden"] else ""
        rows.append([{
            "text": f"{index + 1}. {state}{item['label']}"[:60],
            "callback_data": _cb("s", str(index)),
        }])

    if node.type in _OPTION_NODE_TYPES:
        if role == ROLE_ADMIN:
            rows.append([{
                "text": "➕ Add option",
                "callback_data": _cb("A"),
            }, {
                "text": "✏️ Edit node",
                "callback_data": _cb("e"),
            }])
        else:
            rows.append([{
                "text": "➕ Add option",
                "callback_data": _cb("A"),
            }])

    navigation: List[Dict[str, str]] = []

    parent = _parent_of(tree, node.id)

    if parent:
        navigation.append({"text": "⬆️ Up", "callback_data": _cb("u")})

    navigation.append({"text": "🏠 Home", "callback_data": _cb("h")})
    navigation.append({"text": "🗂 Structure", "callback_data": _cb("t", "0")})

    if navigation:
        rows.append(navigation)

    rows.append([{"text": "Close", "callback_data": _cb("x")}])

    return {"inline_keyboard": rows}


def _show_node(chat_id, sender, role: str, node_id: str, message_id, notice: str = "", edit: bool = True) -> bool:
    """Рисует экран узла в существующем сообщении меню."""
    tree = _tree()
    node = tree.node(node_id) or tree.node(tree.root_id)

    if node is None:
        return False


    items = storage.effective_options(DEFAULT_TREE, node.id, storage.load_option_rows() or [])

    put_view(
        chat_id,
        sender.get("id"),
        screen="node",
        node_id=node.id,
        option_index=None,
        confirm=None,
        page=0,
        options=[item["id"] for item in items],
    )

    if not storage.overlay_supported() and role == ROLE_ADMIN:
        notice = (
            "⚠️ The database is not ready for full editing: column is_builtin "
            "is missing. Apply sql/intake_editor_v2.sql.\n"
            "Until then only adding options works.\n\n" + notice
        )

    text = _node_caption(tree, node, role, notice=notice)
    keyboard = _node_keyboard(tree, node, role, items)

    if edit and message_id is not None:
        _edit(chat_id, message_id, text, reply_markup=keyboard)
    else:
        # /tree открывает редактор: сообщения ещё нет, править нечего.
        _send(chat_id, text, reply_markup=keyboard)

    return True


# ============================================================
# ЭКРАН ВАРИАНТА
# ============================================================

def _option_caption(item: dict, role: str, notice: str = "") -> str:
    lines: List[str] = ["Option", ""]

    if notice:
        lines.append(notice)
        lines.append("")

    icon = f"{item['icon']} " if item["icon"] else ""
    lines.append(f"{icon}{item['label']}")
    lines.append(f"id: {item['id']} · {item['kind']}")

    if item["description"]:
        lines.append(f"_{item['description']}_")

    if item["only_for"]:
        lines.append(f"only for: {', '.join(str(v) for v in item['only_for'])}")

    lines.append(f"next: {item['next_node'] or 'end of branch'}")
    lines.append(f"visible: {'no (hidden)' if item['hidden'] else 'yes'}")

    if item["kind"] == "builtin":
        lines.append("")
        lines.append("This option comes from the bot code. You can rename, retarget, reorder or hide it.")

    lines.append("")
    lines.append(_role_note(role))

    return "\n".join(lines)


def _option_keyboard(item: dict, role: str) -> Dict[str, list]:
    rows: List[list] = []

    if role == ROLE_ADMIN:
        rows.append([
            {"text": "✏️ Rename", "callback_data": _cb("r")},
            {"text": "📝 Description", "callback_data": _cb("d")},
        ])
        rows.append([
            {"text": "🎨 Icon", "callback_data": _cb("i")},
            {"text": "🔗 Target", "callback_data": _cb("g", "0")},
        ])

        move: List[Dict[str, str]] = []

        if item["index"] > 0:
            move.append({"text": "⬆️ Move up", "callback_data": _cb("U")})

        if not item.get("last", False):
            move.append({"text": "⬇️ Move down", "callback_data": _cb("W")})

        if move:
            rows.append(move)

        rows.append([{
            "text": "👁 Show" if item["hidden"] else "🙈 Hide",
            "callback_data": _cb("v"),
        }])

        if item["kind"] == "added":
            rows.append([{
                "text": "🗑 Delete",
                "callback_data": _cb("L"),
            }])

    rows.append([{
        "text": "⬅️ Back to node",
        "callback_data": _cb("n"),
    }, {
        "text": "Close",
        "callback_data": _cb("x"),
    }])

    return {"inline_keyboard": rows}


def _show_option(chat_id, sender, role: str, message_id, notice: str = "") -> bool:
    """Рисует карточку выбранного варианта. Контекст берётся из вида."""
    view = get_view(chat_id, sender.get("id")) or {}
    node_id = view.get("node_id") or DEFAULT_TREE.root_id
    index = view.get("option_index")

    items = storage.effective_options(
        DEFAULT_TREE, node_id, storage.load_option_rows() or []
    )

    if index is None or not (0 <= index < len(items)):
        return _show_node(chat_id, sender, role, node_id, message_id)

    item = dict(items[index])
    item["last"] = index == len(items) - 1

    put_view(
        chat_id,
        sender.get("id"),
        screen="option",
        node_id=node_id,
        option_index=index,
        confirm=None,
        options=[entry["id"] for entry in items],
    )

    _edit(
        chat_id,
        message_id,
        _option_caption(item, role, notice=notice),
        reply_markup=_option_keyboard(item, role),
    )

    return True


# ============================================================
# ОБЗОР СТРУКТУРЫ И ВЫБОР ЦЕЛИ
# ============================================================

PAGE_SIZE = 8


def _structure_screen(tree: DecisionTree, page: int) -> Tuple[str, Dict[str, list]]:
    """
    Обзор всех узлов дерева: видно структуру и скрытые узлы.

    Плоский список, но с постраничной навигацией и переходом в любой узел:
    это карта дерева, а не способ правки.
    """
    node_ids = list(tree.nodes)
    pages = max(1, (len(node_ids) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = node_ids[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    overrides = storage.node_overrides()

    lines = [
        "Tree structure",
        "",
        f"Nodes: {len(node_ids)} · page {page + 1}/{pages}",
        "",
    ]

    for offset, node_id in enumerate(chunk):
        node = tree.node(node_id)

        if node is None:
            continue

        position = page * PAGE_SIZE + offset + 1
        options = len(node.options)
        lines.append(f"{position}. {node.title}  ({node.id})")
        lines.append(f"     {node.type.value} · options: {options}")

    hidden = [
        node_id for node_id, row in overrides.items()
        if row.get("hidden") and node_id not in tree.nodes
    ]

    if hidden:
        lines.append("")
        lines.append("Hidden nodes (their links point past them): " + ", ".join(hidden[:5]))

    rows: List[list] = []

    for offset, node_id in enumerate(chunk):
        node = tree.node(node_id)
        rows.append([{
            "text": f"{page * PAGE_SIZE + offset + 1}. {(node.title if node else node_id)}"[:60],
            "callback_data": _cb("j", f"{page}:{offset}"),
        }])

    navigation: List[Dict[str, str]] = []

    if page > 0:
        navigation.append({"text": "⬅️ Prev", "callback_data": _cb("t", str(page - 1))})

    if page + 1 < pages:
        navigation.append({"text": "Next ➡️", "callback_data": _cb("t", str(page + 1))})

    if navigation:
        rows.append(navigation)

    rows.append([
        {"text": "⬅️ Back", "callback_data": _cb("n")},
        {"text": "Close", "callback_data": _cb("x")},
    ])

    return "\n".join(lines), {"inline_keyboard": rows}


def _target_picker(tree: DecisionTree, page: int, current: Optional[str]) -> Tuple[str, Dict[str, list]]:
    """
    Выбор узла-цели для варианта.

    Показываем все узлы (кроме самого себя и финала — финал виден как
    «end of branch»). Ссылку на несуществующий узел выбрать нельзя: висячие
    `next_node` ломают приём ошибок.
    """
    node_ids = list(tree.nodes)
    pages = max(1, (len(node_ids) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = node_ids[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    lines = [
        "Where should this option lead?",
        "",
        f"page {page + 1}/{pages}",
        f"current: {_node_name(tree, current)}",
        "",
        "Choose a node below. Final nodes end the branch.",
    ]

    rows: List[list] = []

    for offset, node_id in enumerate(chunk):
        node = tree.node(node_id)
        mark = "• " if node_id == current else ""

        rows.append([{
            "text": f"{mark}{(node.title if node else node_id)} ({node.type.value})"[:60],
            "callback_data": _cb("G", f"{page}:{offset}"),
        }])

    navigation: List[Dict[str, str]] = []

    if page > 0:
        navigation.append({"text": "⬅️ Prev", "callback_data": _cb("g", str(page - 1))})

    if page + 1 < pages:
        navigation.append({"text": "Next ➡️", "callback_data": _cb("g", str(page + 1))})

    if navigation:
        rows.append(navigation)

    rows.append([{"text": "⛔ End of branch", "callback_data": _cb("G", "e")}])
    rows.append([
        {"text": "⬅️ Back", "callback_data": _cb("b")},
        {"text": "Close", "callback_data": _cb("x")},
    ])

    return "\n".join(lines), {"inline_keyboard": rows}


# ============================================================
# ЭКРАН УЗЛА: ПРАВКА
# ============================================================

def _node_edit_caption(tree: DecisionTree, node: Node, role: str, notice: str = "") -> str:
    lines = ["Edit node", ""]

    if notice:
        lines.append(notice)
        lines.append("")

    lines.append(f"{node.title}")
    lines.append(f"id: {node.id} · {node.type.value}")

    if node.description:
        lines.append(f"description: {node.description}")

    if node.placeholder:
        lines.append(f"placeholder: {node.placeholder}")

    if node.next_node:
        lines.append(f"next: {_node_name(tree, node.next_node)}")

    rows = storage.node_overrides()
    row = rows.get(node.id) or {}
    lines.append(f"hidden: {'yes' if row.get('hidden') else 'no'}")
    lines.append("")
    lines.append(
        "Nodes cannot be created or deleted: the tree has no back-references, so a "
        "deleted node would leave broken links. Hiding is the safe alternative."
    )

    return "\n".join(lines)


def _node_edit_keyboard(tree: DecisionTree, node: Node) -> Dict[str, list]:
    rows: List[list] = [
        [{"text": "✏️ Title", "callback_data": _cb("T")},
         {"text": "📝 Description", "callback_data": _cb("E")}],
    ]

    if node.type in (NodeType.INPUT, NodeType.NUMBER):
        rows.append([
            {"text": "❔ Placeholder", "callback_data": _cb("P")},
            {"text": "🔗 Next node", "callback_data": _cb("N", "0")},
        ])

    can_hide = node.id != tree.root_id and node.type != NodeType.FINAL
    current = (storage.node_overrides().get(node.id) or {}).get("hidden")
    hidden = bool(current) and node.id not in tree.nodes

    if can_hide and not hidden:
        rows.append([{"text": "🙈 Hide node", "callback_data": _cb("H")}])
    elif hidden:
        rows.append([{"text": "👁 Show node", "callback_data": _cb("H")}])

    rows.append([
        {"text": "⬅️ Back to node", "callback_data": _cb("n")},
        {"text": "Close", "callback_data": _cb("x")},
    ])

    return {"inline_keyboard": rows}


def _show_node_edit(chat_id, sender, role: str, message_id, notice: str = "") -> bool:
    tree = _tree()
    view = get_view(chat_id, sender.get("id")) or {}
    node = tree.node(view.get("node_id")) or tree.node(tree.root_id)

    if node is None:
        return False

    put_view(chat_id, sender.get("id"), screen="node_edit", confirm=None)

    _edit(
        chat_id,
        message_id,
        _node_edit_caption(tree, node, role, notice=notice),
        reply_markup=_node_edit_keyboard(tree, node),
    )

    return True


# ============================================================
# ПОДТВЕРЖДЕНИЯ
# ============================================================

_CONFIRM_PROMPTS = {
    "delete_option": (
        "🗑 Delete this option?",
        "It will disappear for employees immediately. The action cannot be undone "
        "from Telegram, but the option can be added again.",
    ),
    "hide_option": (
        "🙈 Hide this option?",
        "Employees will stop seeing it. The option stays in the tree and can be "
        "shown again at any time.",
    ),
    "hide_node": (
        "🙈 Hide this whole step?",
        "Employees will skip this question. Links that pointed here will lead to "
        "the step this node pointed to. You can show it again later.",
    ),
}


def _show_confirm(chat_id, sender, action: str, message_id) -> bool:
    title, explanation = _CONFIRM_PROMPTS[action]

    put_view(chat_id, sender.get("id"), screen="confirm", confirm=action)

    _edit(
        chat_id,
        message_id,
        f"{title}\n\n{explanation}",
        reply_markup={"inline_keyboard": [[
            {"text": "✅ Yes, do it", "callback_data": _cb("y")},
            {"text": "Cancel", "callback_data": _cb("b")},
        ]]},
    )

    return True


# ============================================================
# ТЕКСТОВЫЕ ПОДСКАЗКИ (ожидание ввода)
# ============================================================

_PROMPTS = {
    KIND_ADD_LABEL: (
        "New option for \"{title}\"\n\n"
        "Send the answer name in one message (up to {label} characters).\n"
        "Example: \"Conveyor jammed\".\n\n"
        "After that you can change its target, description and icon from the option card.\n\n"
        "Cancel with the button below."
    ),
    KIND_OPTION_LABEL: (
        "Rename \"{current}\"\n\n"
        "Send the new answer name (up to {label} characters).\n\n"
        "Cancel with the button below."
    ),
    KIND_OPTION_DESCRIPTION: (
        "Description for \"{current}\"\n\n"
        "Send a short hint shown next to the option (up to {description} characters).\n"
        "Send a single dash \"-\" to clear it.\n\n"
        "Cancel with the button below."
    ),
    KIND_OPTION_ICON: (
        "Icon for \"{current}\"\n\n"
        "Send one emoji (for example: 🔧). Send a single dash \"-\" to clear it.\n\n"
        "Cancel with the button below."
    ),
    KIND_NODE_TITLE: (
        "Rename the question\n\n"
        "Current: \"{current}\"\n"
        "Send the new question text (up to {title} characters).\n\n"
        "Cancel with the button below."
    ),
    KIND_NODE_DESCRIPTION: (
        "Description for this question\n\n"
        "Current: \"{current}\"\n"
        "Send a short hint shown under the question (up to {description} characters).\n"
        "Send a single dash \"-\" to clear it.\n\n"
        "Cancel with the button below."
    ),
    KIND_NODE_PLACEHOLDER: (
        "Placeholder for the text step\n\n"
        "Current: \"{current}\"\n"
        "Send the example text shown to employees (up to {description} characters).\n"
        "Send a single dash \"-\" to clear it.\n\n"
        "Cancel with the button below."
    ),
}


def _prompt_text(kind: str, current: str, title: str) -> str:
    return _PROMPTS[kind].format(
        title=title[:60],
        current=(current or "—")[:60],
        label=storage.MAX_LABEL_LENGTH,
        description=storage.MAX_DESCRIPTION_LENGTH,
        title_max=storage.MAX_TITLE_LENGTH,
    )


def _start_prompt(chat_id, sender, kind: str, message_id, node: Node,
                  option_id: Optional[str] = None) -> None:
    """Открывает ожидание текста и показывает подсказку в меню."""
    put_pending(chat_id, sender.get("id"), node.id, message_id, kind=kind, option_id=option_id)

    current = ""
    title = node.title

    if option_id:
        items = storage.effective_options(
            DEFAULT_TREE, node.id, storage.load_option_rows() or []
        )
        item = next((entry for entry in items if entry["id"] == option_id), None)

        if item:
            current = item["label"]
            title = item["title"] if "title" in item else node.title

            if kind == KIND_OPTION_DESCRIPTION:
                current = item["description"]
            elif kind == KIND_OPTION_ICON:
                current = item["icon"]
    else:
        if kind == KIND_NODE_DESCRIPTION:
            current = node.description
        elif kind == KIND_NODE_PLACEHOLDER:
            current = node.placeholder
        else:
            current = node.title

    _edit(
        chat_id,
        message_id,
        _prompt_text(kind, current, title),
        reply_markup={"inline_keyboard": [[
            {"text": "Cancel", "callback_data": _cb("n")},
        ]]},
    )


# ============================================================
# ТОЧКИ ВХОДА (вызывает telegram_bot.py)
# ============================================================

def _normalize(value) -> str:
    """
    Приводит аргумент команды к простой строке для сравнения.

    Нужна потому, что `handle_command` вызывается по-разному: из бота
    приходит готовая строка `"tree"`, а из тестов — ещё и список, кортеж
    или словарь. Всё, что не строка, сводим к пустой строке: вызывающий
    сам решит, считать ли это совпадением.
    """
    if value is None:
        return ""

    if isinstance(value, str):
        return value.strip().casefold()

    return ""


def _is_tree_command(args) -> bool:
    """
    Похоже ли это на команду /tree.

    Сигнатура handle_command не содержит имени команды, поэтому args
    разбирается гибко: «/tree», «/tree 3», «tree», ["tree"], ("/tree",),
    {"command": "tree"} и пустое значение (вызов уже отобран под /tree).
    Всё остальное — чужая команда, и мы возвращаем False.
    """
    if args is None:
        return True

    if isinstance(args, dict):
        return _normalize(args.get("command")) == "tree"

    if isinstance(args, (list, tuple)):
        for item in args:
            value = _normalize(item)

            if value:
                return value.lstrip("/") == "tree"

        return True

    value = _normalize(args)

    if not value:
        return True

    return value.lstrip("/") == "tree" or value.startswith("/tree ") or value.startswith("tree ")


def handle_command(chat_id, sender, args, message_id) -> bool:
    """
    Обработка /tree.

    False — это не /tree (или не та команда), пусть бот обработает её сам.
    Не редактору вежливо отказываем, но True: команда наша, дальше искать
    нечего.
    """
    if not _is_tree_command(args):
        return False

    role = editor_role(sender)

    if role == ROLE_NONE:
        logger.info(
            "Equipment intake editor: отказ в доступе user=%s",
            (sender or {}).get("id"),
        )
        _send(
            chat_id,
            "This is the decision tree editor: available to maintainers only.\n"
            "Please contact the person responsible for the bot.",
            message_id=message_id,
        )
        return True

    _show_node(
        chat_id, sender, role, DEFAULT_TREE.root_id, message_id, notice="", edit=False
    )
    return True


def _int(value, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _current_node(tree: DecisionTree, view: Dict[str, Any]) -> Node:
    return tree.node(view.get("node_id")) or tree.node(tree.root_id)


def handle_callback(chat_id, sender, parts, message_id, callback_id) -> bool:
    """Кнопка редактора. False — это не наша кнопка или нет доступа."""
    if not parts or parts[0] != CB_PREFIX:
        return False

    role = editor_role(sender)

    if role == ROLE_NONE:
        return False

    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""
    view = get_view(chat_id, sender.get("id")) or {}

    tree = _tree()
    node = _current_node(tree, view)

    # ------------------------------------------------------------
    # Навигация
    # ------------------------------------------------------------

    if action == "x":
        drop_pending(chat_id, sender.get("id"))
        drop_view(chat_id, sender.get("id"))
        _answer(callback_id)
        _delete_quiet(chat_id, message_id)
        return True

    if action == "h":
        _answer(callback_id)
        _show_node(chat_id, sender, role, tree.root_id, message_id)
        return True

    if action == "n":
        drop_pending(chat_id, sender.get("id"))
        _answer(callback_id)
        _show_node(chat_id, sender, role, node.id, message_id)
        return True

    if action == "b":
        drop_pending(chat_id, sender.get("id"))
        _answer(callback_id)

        if view.get("screen") == "option":
            _show_option(chat_id, sender, role, message_id)
        elif view.get("screen") == "target_picker":
            _show_option(chat_id, sender, role, message_id)
        elif view.get("screen") in ("node_edit", "node_target_picker"):
            _show_node_edit(chat_id, sender, role, message_id)
        else:
            _show_node(chat_id, sender, role, node.id, message_id)

        return True

    if action == "u":
        parent = _parent_of(tree, node.id)
        _answer(callback_id)

        if parent:
            _show_node(chat_id, sender, role, parent, message_id)
        else:
            _show_node(chat_id, sender, role, node.id, message_id)

        return True

    if action == "t":
        _answer(callback_id)
        page = _int(arg, 0)
        text, keyboard = _structure_screen(tree, page)
        put_view(chat_id, sender.get("id"), screen="structure", page=page)
        _edit(chat_id, message_id, text, reply_markup=keyboard)
        return True

    if action == "j":
        page = _int((parts[2] if len(parts) > 2 else "0"), 0)
        offset = _int((parts[3] if len(parts) > 3 else "0"), 0)
        node_ids = list(tree.nodes)
        position = page * PAGE_SIZE + offset
        _answer(callback_id)

        if 0 <= position < len(node_ids):
            _show_node(chat_id, sender, role, node_ids[position], message_id)
        else:
            _show_node(chat_id, sender, role, node.id, message_id)

        return True

    # ------------------------------------------------------------
    # Экран варианта
    # ------------------------------------------------------------

    if action == "s":
        index = _int(arg, -1)
        snapshot = view.get("options") or []

        if not (0 <= index < len(snapshot)):
            _answer(callback_id, "Menu expired — open /tree again")
            return True

        _answer(callback_id)
        put_view(chat_id, sender.get("id"), option_index=index)
        _show_option(chat_id, sender, role, message_id)
        return True

    if action == "A":
        if node.type not in _OPTION_NODE_TYPES:
            _answer(callback_id, "Options cannot be added here")
            return True

        _answer(callback_id)
        _start_prompt(chat_id, sender, KIND_ADD_LABEL, message_id, node)
        return True

    # Дальше — действия над вариантом; contributor их не получает.
    if action in ("r", "d", "i", "g", "G", "U", "W", "v", "L", "y", "e", "T", "E", "P", "N", "H"):
        if role != ROLE_ADMIN:
            _answer(callback_id, "Maintainers only")
            return True

    if action in ("r", "d", "i"):
        item = _selected_option(view)

        if item is None:
            _answer(callback_id, "Menu expired — open /tree again")
            return True

        kind = {
            "r": KIND_OPTION_LABEL,
            "d": KIND_OPTION_DESCRIPTION,
            "i": KIND_OPTION_ICON,
        }[action]

        _answer(callback_id)
        _start_prompt(chat_id, sender, kind, message_id, node, option_id=item["id"])
        return True

    if action == "g":
        _answer(callback_id)
        item = _selected_option(view)
        current = item["next_node"] if item else None
        text, keyboard = _target_picker(tree, _int(arg, 0), current)
        put_view(chat_id, sender.get("id"), screen="target_picker")
        _edit(chat_id, message_id, text, reply_markup=keyboard)
        return True

    if action == "G":
        item = _selected_option(view)

        if item is None:
            _answer(callback_id, "Menu expired — open /tree again")
            return True

        if arg == "e":
            target: Optional[str] = None
        else:
            page = _int((parts[2] if len(parts) > 2 else "0"), 0)
            offset = _int((parts[3] if len(parts) > 3 else "0"), 0)
            node_ids = list(tree.nodes)
            position = page * PAGE_SIZE + offset

            if not (0 <= position < len(node_ids)):
                _answer(callback_id, "Node not found")
                return True

            target = node_ids[position]

        ok = storage.update_option(
            node.id, item["id"], next_node=target,
            is_builtin=item["is_builtin"], updated_by=sender.get("id"),
        )
        _answer(callback_id, "Saved" if ok else "Could not save")

        if ok:
            _show_option(
                chat_id, sender, role, message_id,
                notice=f"✅ Target updated to {_node_name(_tree(), target)}.",
            )
        else:
            _show_option(
                chat_id, sender, role, message_id, notice=_failure_notice(False, "")
            )

        return True

    if action in ("U", "W"):
        item = _selected_option(view)

        if item is None:
            _answer(callback_id, "Menu expired — open /tree again")
            return True

        moved = storage.move_option(
            node.id, item["id"], "up" if action == "U" else "down",
            updated_by=sender.get("id"),
        )
        _answer(callback_id, "Moved" if moved else "Could not move")

        if moved:
            items = storage.effective_options(
                DEFAULT_TREE, node.id, storage.load_option_rows() or []
            )
            new_index = next(
                (i for i, entry in enumerate(items) if entry["id"] == item["id"]),
                view.get("option_index") or 0,
            )
            put_view(chat_id, sender.get("id"), option_index=new_index)

        _show_option(
            chat_id, sender, role, message_id,
            notice=_failure_notice(moved, "✅ Order updated."),
        )
        return True

    if action == "v":
        item = _selected_option(view)

        if item is None:
            _answer(callback_id, "Menu expired — open /tree again")
            return True

        if not item["hidden"]:
            _answer(callback_id)
            _show_confirm(chat_id, sender, "hide_option", message_id)
            return True

        ok = storage.set_option_hidden(
            node.id, item["id"], False, is_builtin=item["is_builtin"],
            updated_by=sender.get("id"),
        )
        _answer(callback_id, "Shown" if ok else "Could not show")
        _show_option(
            chat_id, sender, role, message_id,
            notice=_failure_notice(ok, "✅ Option is visible again."),
        )
        return True

    if action == "L":
        item = _selected_option(view)

        if item is None:
            _answer(callback_id, "Menu expired — open /tree again")
            return True

        if item["kind"] != "added":
            _answer(callback_id, "Built-in options are hidden, not deleted")
            return True

        _answer(callback_id)
        _show_confirm(chat_id, sender, "delete_option", message_id)
        return True

    if action == "y":
        _answer(callback_id)
        _run_confirm(chat_id, sender, role, view, message_id)
        return True

    # ------------------------------------------------------------
    # Экран правки узла
    # ------------------------------------------------------------

    if action == "e":
        _answer(callback_id)
        _show_node_edit(chat_id, sender, role, message_id)
        return True

    if action in ("T", "E", "P"):
        kind = {
            "T": KIND_NODE_TITLE,
            "E": KIND_NODE_DESCRIPTION,
            "P": KIND_NODE_PLACEHOLDER,
        }[action]
        _answer(callback_id)
        _start_prompt(chat_id, sender, kind, message_id, node)
        return True

    if action == "N":
        _answer(callback_id)
        text, keyboard = _target_picker(tree, _int(arg, 0), node.next_node)
        put_view(chat_id, sender.get("id"), screen="node_target_picker")
        _edit(chat_id, message_id, text, reply_markup=keyboard)
        return True

    if action == "H":
        overrides = storage.node_overrides()
        hidden = bool((overrides.get(node.id) or {}).get("hidden")) and node.id not in tree.nodes
        _answer(callback_id)

        if hidden:
            ok = storage.update_node(node.id, hidden=False, updated_by=sender.get("id"))
            _show_node_edit(
                chat_id, sender, role, message_id,
                notice=_failure_notice(ok, "✅ The step is visible again."),
            )
            return True

        _show_confirm(chat_id, sender, "hide_node", message_id)
        return True

    _answer(callback_id, "Unknown action")
    return True


def _selected_option(view: Dict[str, Any]) -> Optional[dict]:
    """Вариант, открытый в карточке (по id из вида, а не по позиции)."""
    node_id = view.get("node_id")
    index = view.get("option_index")

    if not node_id or index is None:
        return None

    items = storage.effective_options(
        DEFAULT_TREE, node_id, storage.load_option_rows() or []
    )

    if not (0 <= index < len(items)):
        return None

    return items[index]


def _run_confirm(chat_id, sender, role: str, view: Dict[str, Any], message_id) -> None:
    """Выполняет подтверждённое действие и возвращает человека на нужный экран."""
    confirm = view.get("confirm")
    node_id = view.get("node_id") or DEFAULT_TREE.root_id
    item = _selected_option(view)

    if confirm == "delete_option" and item is not None:
        ok = storage.delete_option(node_id, item["id"])
        _show_node(
            chat_id, sender, role, node_id, message_id,
            notice=(
                f"✅ Option deleted: {item['label']}." if ok
                else "⚠️ Could not delete the option."
            ),
        )
        return

    if confirm == "hide_option" and item is not None:
        ok = storage.set_option_hidden(
            node_id, item["id"], True, is_builtin=item["is_builtin"],
            updated_by=sender.get("id"),
        )
        _show_option(
            chat_id, sender, role, message_id,
            notice=(
                _failure_notice(
                    ok,
                    f"✅ Option hidden: {item['label']}. It can be shown again.",
                )
            ),
        )
        return

    if confirm == "hide_node":
        ok = storage.update_node(node_id, hidden=True, updated_by=sender.get("id"))
        _show_node(
            chat_id, sender, role, node_id, message_id,
            notice=(
                _failure_notice(ok, "✅ Step hidden. Links now lead past it.")
            ),
        )
        return

    _show_node(chat_id, sender, role, node_id, message_id, notice="⚠️ Nothing to confirm.")


def handle_text(chat_id, sender, text: str, message_id) -> bool:
    """
    Текст от редактора во время правки.

    False — ожидания нет, значит это обычное сообщение (фото-флоу, разбор
    ошибки и т.п.), и перехватывать его нельзя.
    """
    pending = get_pending(chat_id, sender.get("id"))

    if not pending:
        return False

    role = editor_role(sender)

    if role == ROLE_NONE:
        drop_pending(chat_id, sender.get("id"))
        return False

    node_id = pending.get("node_id") or DEFAULT_TREE.root_id
    option_id = pending.get("option_id")
    kind = pending.get("kind") or KIND_ADD_LABEL
    menu_message_id = pending.get("menu_message_id")

    # Редактор уже ответил текстом — ожидание закрыто (иначе два быстрых
    # сообщения создали бы две правки).
    drop_pending(chat_id, sender.get("id"))

    value = (text or "").strip()

    if not value:
        _back_to_screen(
            chat_id, sender, role, kind, node_id, option_id, menu_message_id,
            notice="⚠️ Empty text — nothing changed.",
        )
        return True

    ok, notice, follow_id = _apply_edit(kind, node_id, option_id, value, sender)

    # Сообщение редактора убираем ТОЛЬКО после успеха: при ошибке текст
    # остаётся в чате, и его можно поправить.
    if ok:
        _delete_quiet(chat_id, message_id)

    logger.info(
        "Equipment intake editor: user=%s правка %s узла %s (%s)",
        sender.get("id"), kind, node_id, "ок" if ok else "отклонена",
    )

    if follow_id:
        put_view(chat_id, sender.get("id"), node_id=node_id, option_index=None)

        items = storage.effective_options(
            DEFAULT_TREE, node_id, storage.load_option_rows() or []
        )
        index = next((i for i, entry in enumerate(items) if entry["id"] == follow_id), None)

        if index is not None:
            put_view(chat_id, sender.get("id"), option_index=index)
            _show_option(chat_id, sender, role, menu_message_id, notice=notice)
            return True

    _back_to_screen(
        chat_id, sender, role, kind, node_id, option_id, menu_message_id, notice=notice,
    )

    return True


def _apply_edit(kind: str, node_id: str, option_id: Optional[str], value: str,
                sender) -> Tuple[bool, str, Optional[str]]:
    """
    Применяет введённый текст.

    Возвращает (успех, сообщение, option_id_для_показа). Пустое поле
    очищается одним дефисом: иначе стереть описание из Telegram нельзя.
    """
    tree = _tree()
    node = tree.node(node_id)

    if node is None:
        return False, "⚠️ Node not found.", None

    cleared = value == "-"

    if kind == KIND_ADD_LABEL:
        label = value[:storage.MAX_LABEL_LENGTH]

        if not label:
            return False, "⚠️ Empty name — the option was not added.", None

        option_id_new = storage.add_option(
            node_id, label, created_by=(sender or {}).get("id")
        )

        if not option_id_new:
            return False, (
                "⚠️ Could not add the option: the name is empty or "
                f"longer than {storage.MAX_LABEL_LENGTH} characters, or the node "
                "is unavailable. Try a different name."
            ), None

        return True, (
            f"✅ Option added: {label} (id: {option_id_new}). "
            "It is already available to employees at this step. "
            "Open its card to change the target, description or icon."
        ), option_id_new

    if option_id is None:
        return False, "⚠️ Option not found.", None

    items = storage.effective_options(DEFAULT_TREE, node_id, storage.load_option_rows() or [])
    item = next((entry for entry in items if entry["id"] == option_id), None)

    if item is None:
        return False, "⚠️ Option not found.", None

    if kind == KIND_OPTION_LABEL:
        if not value:
            return False, "⚠️ Empty name — nothing changed.", option_id

        ok = storage.update_option(
            node_id, option_id, label=value, is_builtin=item["is_builtin"],
            updated_by=(sender or {}).get("id"),
        )

        return ok, (
            f"✅ Renamed to: {value}." if ok
            else "⚠️ Could not save the new name (is the database available?)."
        ), option_id

    if kind == KIND_OPTION_DESCRIPTION:
        ok = storage.update_option(
            node_id, option_id, description="" if cleared else value,
            is_builtin=item["is_builtin"], updated_by=(sender or {}).get("id"),
        )

        return ok, (
            "✅ Description cleared." if ok and cleared else
            "✅ Description saved." if ok else
            "⚠️ Could not save the description."
        ), option_id

    if kind == KIND_OPTION_ICON:
        ok = storage.update_option(
            node_id, option_id, icon="" if cleared else value[:storage.MAX_ICON_LENGTH],
            is_builtin=item["is_builtin"], updated_by=(sender or {}).get("id"),
        )

        return ok, (
            "✅ Icon cleared." if ok and cleared else
            "✅ Icon saved." if ok else
            "⚠️ Could not save the icon."
        ), option_id

    # ------------------------------------------------------------
    # Поля узла
    # ------------------------------------------------------------

    if kind == KIND_NODE_TITLE:
        if not value:
            return False, "⚠️ Empty title — nothing changed.", None

        ok = storage.update_node(node_id, title=value, updated_by=(sender or {}).get("id"))

        return ok, (
            f"✅ Question renamed to: {value}." if ok
            else "⚠️ Could not save the question text."
        ), None

    if kind == KIND_NODE_DESCRIPTION:
        ok = storage.update_node(
            node_id, description="" if cleared else value,
            updated_by=(sender or {}).get("id"),
        )

        return ok, (
            "✅ Description cleared." if ok and cleared else
            "✅ Description saved." if ok else
            "⚠️ Could not save the description."
        ), None

    if kind == KIND_NODE_PLACEHOLDER:
        ok = storage.update_node(
            node_id, placeholder="" if cleared else value,
            updated_by=(sender or {}).get("id"),
        )

        return ok, (
            "✅ Placeholder cleared." if ok and cleared else
            "✅ Placeholder saved." if ok else
            "⚠️ Could not save the placeholder."
        ), None

    return False, "⚠️ Unknown action.", None


def _back_to_screen(chat_id, sender, role: str, kind: str, node_id: str,
                    option_id: Optional[str], menu_message_id, notice: str) -> None:
    """Возвращает человека на экран, с которого он начал правку."""
    if kind in (KIND_OPTION_LABEL, KIND_OPTION_DESCRIPTION, KIND_OPTION_ICON):
        items = storage.effective_options(
            DEFAULT_TREE, node_id, storage.load_option_rows() or []
        )
        index = next((i for i, entry in enumerate(items) if entry["id"] == option_id), None)
        put_view(chat_id, sender.get("id"), node_id=node_id, option_index=index)

        if index is not None:
            _show_option(chat_id, sender, role, menu_message_id, notice=notice)
            return

    if kind in (KIND_NODE_TITLE, KIND_NODE_DESCRIPTION, KIND_NODE_PLACEHOLDER):
        put_view(chat_id, sender.get("id"), node_id=node_id, screen="node_edit")
        _show_node_edit(chat_id, sender, role, menu_message_id, notice=notice)
        return

    _show_node(chat_id, sender, role, node_id, menu_message_id, notice=notice)
