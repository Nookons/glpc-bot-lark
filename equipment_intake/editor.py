"""
Telegram-редактор дерева решений для сотрудников.

Идея: сотрудник нажимает кнопки и может расширять
дерево сам, из Telegram, без кода и сервера. Объём прав намеренно узкий —
ДОБАВИТЬ новый вариант к существующему узлу. Переименование, удаление и
перестройка структуры запрещены: живую ветку слишком легко сломать, а
откатывать правку менеджеру из чата нечем.

Правки хранятся в Supabase через storage.py. Пока идёт наполнение, дерево уже работает: до правки
менеджер видит встроенные варианты, после — они плюс добавленные.

UI редактора живёт в отдельном пространстве callback_data («ed:»), чтобы
не пересекаться с рабочим деревом («dt:» в flow.py).
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

# Типы узлов, к которым разрешено добавлять варианты. У текстовых и
# финального узла кнопок нет — новый вариант там никто не увидит.
_OPTION_NODE_TYPES = (NodeType.CHOICE, NodeType.MULTI, NodeType.YESNO)


def is_editor(sender: Dict[str, Any]) -> bool:
    """
    Редактор доступен всем участникам Telegram-чата бота.

    Вход в обработчики ограничен шлюзом бота по чату/топику. Редактор позволяет только добавлять
    варианты, не менять и не удалять существующие узлы.
    """
    return isinstance(sender, dict) and sender.get("id") is not None


# ============================================================
# СОСТОЯНИЕ «ЖДЁМ НАЗВАНИЕ ВАРИАНТА»
# ============================================================

# Сколько ждём название, прежде чем забыть про начатое добавление.
PENDING_TTL_SECONDS = 600

# callback_data ограничена 64 байтами, поэтому в кнопку едет только код
# действия и короткий id узла.
CB_PREFIX = "ed"

_PENDING: Dict[Tuple[int, int], Dict[str, Any]] = {}
_LOCK = threading.Lock()


def _key(chat_id, user_id) -> Tuple[int, int]:
    return (int(chat_id), int(user_id))


def put_pending(chat_id, user_id, node_id: str, menu_message_id) -> None:
    """Запоминает, что от этого человека ждём название варианта."""
    # sweep вызываем ДО захвата замка: threading.Lock не реентерабельный,
    # и вызов внутри `with` привёл бы к зависанию.
    sweep()

    with _LOCK:
        _PENDING[_key(chat_id, user_id)] = {
            "node_id": str(node_id),
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


def sweep() -> int:
    """Убирает просроченные ожидания, чтобы словарь не рос."""
    now = time.time()

    with _LOCK:
        stale = [
            key for key, entry in _PENDING.items()
            if entry["expires"] < now
        ]

        for key in stale:
            _PENDING.pop(key, None)

    return len(stale)


def reset_state() -> None:
    """Сброс ожиданий — только для тестов."""
    with _LOCK:
        _PENDING.clear()


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
    Дерево, которое видит редактор: встроенное плюс накопленный оверлей.

    Берём DEFAULT_TREE, а не EQUIPMENT_INTAKE_TREE_SOURCE: storage проверяет
    новые варианты именно по встроенному дереву, поэтому редактор и
    хранилище смотрят на один и тот же набор узлов.
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


def _cb(action: str, arg: str = "") -> str:
    return f"{CB_PREFIX}:{action}" + (f":{arg}" if arg else "")


def _node_caption(tree: DecisionTree, node: Node, notice: str = "") -> str:
    """Текст экрана узла: варианты списком, встроенные и добавленные."""
    base = DEFAULT_TREE.node(node.id)
    builtin_ids = {option.id for option in base.options} if base else set()

    lines: List[str] = ["Редактор дерева", ""]

    # Notice живёт в самом экране узла, а не отдельным сообщением: экран
    # перерисовывается на том же message_id и затёр бы подтверждение.
    if notice:
        lines.append(notice)
        lines.append("")

    lines.append(f"❓ {node.title}")
    lines.append(f"{node.id} · {node.type.value}")

    if node.description:
        lines.append(f"_{node.description}_")

    lines.append("")
    lines.append(f"Варианты ({len(node.options)}):")

    if not node.options:
        lines.append("— пока пусто —")

    for option in node.options:
        if option.id in builtin_ids:
            mark = "встроенный"
        else:
            mark = "добавленный"

        target = option.next_node or "конец ветки"
        icon = f"{option.icon} " if option.icon else ""
        lines.append(f"{mark}: {icon}{option.label}  →  {target}")
        lines.append(f"    id: {option.id}")

    lines.append("")
    lines.append(
        "Доступно только добавление нового варианта. "
        "Переименование и удаление — через разработчика."
    )

    return "\n".join(lines)


def _node_keyboard(tree: DecisionTree, node: Node) -> Dict[str, list]:
    """Клавиатура экрана узла: крошки, «добавить», «вверх», «закрыть»."""
    rows: List[list] = []
    path = _path_to(tree, node.id)

    # Крошки: каждый предыдущий уровень ведёт на свой узел. Длинный путь
    # укорачиваем, чтобы клавиатура не расползлась.
    if len(path) > 1:
        crumbs = path if len(path) <= 5 else path[:1] + path[-4:]
        row: List[Dict[str, str]] = []

        for crumb_id in crumbs:
            crumb = tree.node(crumb_id)
            label = crumb.title if crumb else crumb_id

            if crumb_id == node.id:
                label = f"[{label}]"

            row.append({
                "text": label[:24],
                "callback_data": _cb("n", crumb_id),
            })

        rows.append(row)

    rows.append([{
        "text": "Add option",
        "callback_data": _cb("a", node.id),
    }])

    parent = _parent_of(tree, node.id)

    if parent:
        rows.append([{
            "text": "Up",
            "callback_data": _cb("n", parent),
        }])

    rows.append([{"text": "Close", "callback_data": _cb("x")}])

    return {"inline_keyboard": rows}


def _show_node(chat_id, sender, node_id: str, message_id, edit: bool = True,
               notice: str = "") -> bool:
    """
    Показывает узел: список вариантов и кнопки редактора.

    edit=True меняет уже открытое сообщение, edit=False отправляет новое
    (так открывается меню по /tree). notice — строка-подтверждение, она
    встраивается в тот же экран.
    """
    tree = _tree()
    node = tree.node(node_id) or tree.node(tree.root_id)

    if node is None:
        return False

    text = _node_caption(tree, node, notice=notice)
    keyboard = _node_keyboard(tree, node)

    if edit and message_id is not None:
        _edit(chat_id, message_id, text, reply_markup=keyboard)
    else:
        _send(chat_id, text, reply_markup=keyboard)

    return True


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

    if not is_editor(sender):
        logger.info(
            "Equipment intake editor: отказ в доступе user=%s",
            (sender or {}).get("id"),
        )
        _send(
            chat_id,
            "Это редактор дерева решений: доступен только сопровождающим.\n"
            "Обратитесь к ответственному за бота.",
            message_id=message_id,
        )
        return True

    _show_node(chat_id, sender, DEFAULT_TREE.root_id, message_id, edit=False)
    return True


def handle_callback(chat_id, sender, parts, message_id, callback_id) -> bool:
    """Кнопка редактора. False — это не наша кнопка или не редактор."""
    if not parts or parts[0] != CB_PREFIX:
        return False

    if not is_editor(sender):
        return False

    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""
    tree = _tree()

    if action == "n":
        _answer(callback_id)
        _show_node(chat_id, sender, arg or tree.root_id, message_id, edit=True)
        return True

    if action == "a":
        node = tree.node(arg)

        if node is None or node.type not in _OPTION_NODE_TYPES:
            _answer(callback_id, "Сюда нельзя добавить вариант")
            return True

        put_pending(chat_id, sender.get("id"), node.id, message_id)

        text = (
            f"Новый вариант для «{node.title}»\n\n"
            "Пришлите название ответа одним сообщением "
            f"(до {storage.MAX_LABEL_LENGTH} символов).\n"
            "Например: «Disconnected».\n\n"
            "Отмена — кнопкой ниже."
        )
        keyboard = {"inline_keyboard": [[{
            "text": "Cancel",
            "callback_data": _cb("x"),
        }]]}

        _answer(callback_id)
        _edit(chat_id, message_id, text, reply_markup=keyboard)
        return True

    if action == "x":
        drop_pending(chat_id, sender.get("id"))
        _answer(callback_id)
        _delete_quiet(chat_id, message_id)
        return True

    _answer(callback_id, "Неизвестное действие")
    return True


def handle_text(chat_id, sender, text: str, message_id) -> bool:
    """
    Текст от редактора во время добавления варианта.

    False — ожидания нет, значит это обычное сообщение (фото-флоу,
    разбор ошибки и т.п.), и перехватывать его нельзя.
    """
    pending = get_pending(chat_id, sender.get("id"))

    if not pending:
        return False

    node_id = pending.get("node_id") or DEFAULT_TREE.root_id
    menu_message_id = pending.get("menu_message_id")

    # Редактор уже ответил текстом — ожидание закрыто (иначе два быстрых
    # сообщения создали бы два варианта).
    drop_pending(chat_id, sender.get("id"))

    label = (text or "").strip()

    if not label:
        _show_node(
            chat_id, sender, node_id, menu_message_id, edit=True,
            notice="⚠️ Пустое название — вариант не добавлен.",
        )
        return True

    option_id = storage.add_option(node_id, label, created_by=(sender or {}).get("id"))

    if not option_id:
        _show_node(
            chat_id, sender, node_id, menu_message_id, edit=True,
            notice=(
                "⚠️ Не удалось добавить вариант: название пустое или "
                f"длиннее {storage.MAX_LABEL_LENGTH} символов, либо узел "
                "недоступен. Попробуйте другое название."
            ),
        )
        return True

    # Сообщение редактора убираем ТОЛЬКО после успеха: при ошибке текст
    # остаётся в чате, и его можно поправить.
    _delete_quiet(chat_id, message_id)
    _show_node(
        chat_id, sender, node_id, menu_message_id, edit=True,
        notice=(
            f"✅ Вариант добавлен: {label} (id: {option_id}).\n"
            "Он уже доступен сотрудникам на этом шаге."
        ),
    )

    logger.info(
        "Equipment intake editor: user=%s добавил вариант %s к узлу %s",
        sender.get("id"),
        option_id,
        node_id,
    )

    return True
