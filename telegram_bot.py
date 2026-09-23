"""
Telegram-бот приёма сообщений об ошибках роботов (склад GLP-C).

Что изменилось по сравнению с Lark-версией (webhookApp.py):

  * сообщения приходят из Telegram (long polling), а не из событий Lark;
  * имя сотрудника берётся из привязки /reg (Supabase), а не из
    Contact API — это убирает вызовы Lark API на каждое сообщение;
  * все ответы пользователю уходят в Telegram — квота Lark не тратится;
  * вывод в Lark-группу идёт через custom-bot webhook (тоже без квоты);
  * из Lark API осталась только загрузка фото (im/v1/images),
    1 вызов на фотографию.

Логика парсинга, смен и записи в Supabase переиспользуется из
error_parser.py / shift.py / sendToDataBase.py без изменений.
"""

from __future__ import annotations

import difflib
import os
import threading
import time
from collections import OrderedDict
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

import telegram_api as tg
from error_parser import parse_error_message
from logging_config import setup_logging
from pending_photos import forward_error, handle_incoming_photo
from sendToDataBase import (
    count_robot_errors_in_shift,
    notify_user,
    send_to_data_base,
    set_notifier,
    shift_stats,
    table_exists,
)
from shift import get_current_shift
from shift_report import start_shift_scheduler
from telegram_store import (
    get_employee_name,
    link_user,
    resolve_employee_name,
    unlink_user,
)


logger = setup_logging(__name__)

console = Console()
app = Flask(__name__)


# ============================================================
# SETTINGS
# ============================================================

WARSAW_TZ = ZoneInfo("Europe/Warsaw")


def _env_int(name: str, default: int) -> int:
    """Читает целое из окружения; при мусоре возвращает default."""
    raw = os.environ.get(name)

    if raw is None or not str(raw).strip():
        return default

    try:
        return int(str(raw).strip())
    except ValueError:
        logger.warning("Invalid %s=%r, using %s", name, raw, default)
        return default


def _env_int_opt(name: str):
    """Читает целое из окружения; None, если не задано/мусор."""
    raw = os.environ.get(name)

    if raw is None or not str(raw).strip():
        return None

    try:
        return int(str(raw).strip())
    except ValueError:
        logger.warning("Invalid %s=%r, ignoring", name, raw)
        return None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)

    if raw is None or not str(raw).strip():
        return default

    return str(raw).strip().lower() in ("1", "true", "yes", "on")


# Сколько ошибок робота за смену считается поводом для алерта.
ERROR_THRESHOLD = _env_int("ERROR_THRESHOLD", 3)

# Сообщения старше этого возраста не обрабатываем (защита от
# «хвоста» апдейтов после простоя/деплоя).
MESSAGE_MAX_AGE_SECONDS = _env_int("MESSAGE_MAX_AGE_SECONDS", 300)

# Присылать ли в Telegram подтверждение об успешной записи.
SEND_CONFIRMATION = os.environ.get(
    "TELEGRAM_CONFIRM",
    "true",
).strip().lower() in ("1", "true", "yes", "on")

# Необязательный белый список чатов (id через запятую). Если задан,
# бот игнорирует сообщения из всех остальных чатов.
ALLOWED_CHAT_IDS = {
    int(part)
    for part in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").replace(" ", "").split(",")
    if part.strip().lstrip("-").isdigit()
}

# Топик (форум-группа), из которого бот берёт сообщения.
# TELEGRAM_TOPIC_ID — точный id топика (рекомендуется), узнать: /id в топике.
# TELEGRAM_TOPIC_NAME — имя топика; сработает, если бот уже видел сервисное
# сообщение о создании/переименовании топика.
TELEGRAM_TOPIC_ID = _env_int_opt("TELEGRAM_TOPIC_ID")
TELEGRAM_TOPIC_NAME = os.environ.get("TELEGRAM_TOPIC_NAME", "").strip()

# Отвечать ли в чужом топике подсказкой (один раз на топик).
WRONG_TOPIC_HINT = _env_bool("TELEGRAM_WRONG_TOPIC_HINT", True)

# Эти команды отвечают даже в чате не из белого списка: иначе после
# включения TELEGRAM_ALLOWED_CHAT_IDS нельзя было бы узнать chat_id через /id.
BOOTSTRAP_COMMANDS = ("id", "help", "start")

# Все поддерживаемые команды (для подсказок «может, вы имели в виду…»).
KNOWN_COMMANDS = ("reg", "unreg", "whoami", "stats", "id", "help", "start")

# Русская раскладка: люди часто набирают /reg как /куп, а /req как /куй.
# Переводим символы ЙЦУКЕН в QWERTY, чтобы команда распозналась.
RU_LAYOUT = str.maketrans({
    "й": "q", "ц": "w", "у": "e", "к": "r", "е": "t", "н": "y",
    "г": "u", "ш": "i", "щ": "o", "з": "p", "х": "[", "ъ": "]",
    "ф": "a", "ы": "s", "в": "d", "а": "f", "п": "g", "р": "h",
    "о": "j", "л": "k", "д": "l", "ж": ";", "э": "'",
    "я": "z", "ч": "x", "с": "c", "м": "v", "и": "b", "т": "n",
    "ь": "m", "б": ",", "ю": ".",
})


# Псевдонимы для безопасных команд: люди регулярно пишут /req вместо /reg.
# Для удаляющей /unreg псевдонимов намеренно нет.
COMMAND_ALIASES = {
    "req": "reg",
    "regs": "reg",
    "register": "reg",
    "stat": "stats",
}


def normalize_command(command: str) -> str:
    """Латиница + нижний регистр + раскладка + псевдонимы."""
    if not command:
        return command

    normalized = command.strip().lower().translate(RU_LAYOUT)

    return COMMAND_ALIASES.get(normalized, normalized)


def suggest_command(command: str):
    """Ближайшая известная команда или None."""
    matches = difflib.get_close_matches(command, KNOWN_COMMANDS, n=1, cutoff=0.6)

    return matches[0] if matches else None

_SEEN_LIMIT = 2000
_seen_lock = threading.Lock()
_seen_message_ids = OrderedDict()

_IMAGES_DIR = os.environ.get("IMAGES_DIR", "images")


HELP_TEXT = (
    "🤖 Robot exception bot\n"
    "\n"
    "Send an exception in this format:\n"
    "<issue type>: <description>. <robot number>\n"
    "\n"
    "Example:\n"
    "Unable to drive: Security module failure. 3780\n"
    "\n"
    "Commands:\n"
    "/reg <Your Name> — link Telegram to your employee name\n"
    "/whoami — show current link\n"
    "/unreg — remove the link\n"
    "/stats — current shift statistics\n"
    "/id — show chat/user IDs\n"
    "/help — this message"
)

FORMAT_HINT = (
    "⚠️ Can't parse the message. Please use the format:\n"
    "<issue type>: <description>. <robot number>\n"
    "\n"
    "Example:\n"
    "Unable to drive: Security module failure. 3780"
)

NOT_REGISTERED_HINT = (
    "⚠️ You are not registered yet.\n"
    "Send /reg <Your Name> (the name as it is written in Lark), e.g.\n"
    "/reg Ivan Petrenko"
)


# ============================================================
# TIME HELPERS
# ============================================================

def now_warsaw() -> datetime:
    """Текущее время Europe/Warsaw (учитывает переход на летнее время)."""
    return datetime.now(WARSAW_TZ)


# ============================================================
# MESSAGE DEDUPLICATION / AGE
# ============================================================

def _already_processed(message_id) -> bool:
    with _seen_lock:
        if message_id in _seen_message_ids:
            return True

        _seen_message_ids[message_id] = True

        if len(_seen_message_ids) > _SEEN_LIMIT:
            _seen_message_ids.popitem(last=False)

        return False


def _is_message_too_old(unix_seconds) -> bool:
    """
    Telegram присылает дату сообщения в Unix-секундах
    (в отличие от Lark, где были миллисекунды).
    """
    if not unix_seconds:
        return False

    try:
        message_time = datetime.fromtimestamp(int(unix_seconds), tz=WARSAW_TZ)
    except (ValueError, TypeError, OSError):
        return False

    age_seconds = (now_warsaw() - message_time).total_seconds()

    return age_seconds > MESSAGE_MAX_AGE_SECONDS


def _chat_allowed(chat_id) -> bool:
    if not ALLOWED_CHAT_IDS:
        return True

    return int(chat_id) in ALLOWED_CHAT_IDS


# ============================================================
# COMMAND PARSING
# ============================================================

def parse_command(text: str, bot_username: str = None):
    """
    Разбирает команду Telegram.

    '/reg@MyBot Ivan' -> ('reg', 'Ivan')

    Возвращает (command, args). command=None, если это команда,
    адресованная другому боту (её нужно молча игнорировать).
    """
    if not text or not text.startswith("/"):
        return None, ""

    parts = text.split(maxsplit=1)
    command = parts[0][1:]
    args = parts[1].strip() if len(parts) > 1 else ""

    if "@" in command:
        command, _, target = command.partition("@")

        if bot_username and target.lower() != bot_username.lower():
            return None, ""

    return command.lower(), args


# ============================================================
# FORUM TOPICS
# ============================================================
#
# В Bot API нет метода «узнать имя топика по message_thread_id» (есть только
# createForumTopic / editForumTopic / getForumTopicIconStickers), поэтому:
#   * TELEGRAM_TOPIC_ID   — точный id топика, надёжный вариант;
#   * TELEGRAM_TOPIC_NAME — имя топика, матчится только если бот уже видел
#     сервисное сообщение о создании/переименовании этого топика.
# `message_thread_id` нужного топика показывает команда /id.

_topic_names = {}      # (chat_id, thread_id) -> name
_routes = {}           # chat_id -> thread_id последнего сообщения
_hinted_threads = set()


def _chat_key(chat_id) -> int:
    return int(chat_id)


def remember_topic(chat_id, thread_id, name):
    """Запоминает имя топика (из сервисных сообщений форума)."""
    if not name or thread_id is None:
        return

    key = (_chat_key(chat_id), int(thread_id))

    if _topic_names.get(key) != name:
        _topic_names[key] = name
        logger.info(
            "Learned topic name: chat=%s thread=%s name=%r",
            chat_id,
            thread_id,
            name,
        )


def topic_name(chat_id, thread_id):
    """Известное имя топика или None."""
    if thread_id is None:
        return None

    return _topic_names.get((_chat_key(chat_id), int(thread_id)))


def _learn_topic_from_message(chat_id, message):
    """Собирает имя топика из сервисных сообщений Message."""
    thread_id = message.get("message_thread_id")

    created = message.get("forum_topic_created")

    if created:
        remember_topic(chat_id, thread_id, created.get("name"))
        return

    edited = message.get("forum_topic_edited")

    if edited:
        remember_topic(chat_id, thread_id, edited.get("name"))
        return

    replied = message.get("reply_to_message") or {}
    created = replied.get("forum_topic_created")

    if created:
        remember_topic(
            chat_id,
            replied.get("message_thread_id", thread_id),
            created.get("name"),
        )


def monitored_topic_label() -> str:
    """Человекочитаемое описание отслеживаемого топика."""
    if TELEGRAM_TOPIC_ID is not None:
        return f"topic id {TELEGRAM_TOPIC_ID}"

    if TELEGRAM_TOPIC_NAME:
        return f"topic {TELEGRAM_TOPIC_NAME!r}"

    return "any topic"


def topic_allowed(chat_id, thread_id):
    """
    Можно ли брать сообщение из этого топика.

    Возвращает (allowed, reason).
    """
    if TELEGRAM_TOPIC_ID is None and not TELEGRAM_TOPIC_NAME:
        return True, "no-filter"

    if thread_id is None:
        # Сообщение вне топиков — это «General».
        return False, "general-topic"

    if TELEGRAM_TOPIC_ID is not None and int(thread_id) == TELEGRAM_TOPIC_ID:
        return True, "id-match"

    if TELEGRAM_TOPIC_NAME:
        name = topic_name(chat_id, thread_id)

        if name is None:
            return False, "name-unknown"

        if name.strip().casefold() == TELEGRAM_TOPIC_NAME.strip().casefold():
            return True, "name-match"

        return False, "name-mismatch"

    return False, "id-mismatch"


def _send(chat_id, text, reply_to_message_id=None, thread_id=None, disable_notification=False):
    """
    Отправка с автоопределением топика: ответ уходит в тот же топик,
    откуда пришло сообщение (для форум-групп).

    Если в этот топик отправить нельзя (например, General закрыт —
    Telegram отвечает TOPIC_CLOSED), повторяем в отслеживаемый топик,
    чтобы пользователь всё-таки увидел ответ.
    """
    if thread_id is None:
        thread_id = _routes.get(_chat_key(chat_id))

    result = tg.send_message(
        chat_id,
        text,
        reply_to_message_id=reply_to_message_id,
        disable_notification=disable_notification,
        message_thread_id=thread_id,
    )

    if result is not None:
        return result

    fallback = TELEGRAM_TOPIC_ID

    if (
        fallback is not None
        and _chat_key(chat_id) in ALLOWED_CHAT_IDS
        and (thread_id is None or int(thread_id) != int(fallback))
    ):
        logger.warning(
            "Ответ в топик %s не ушёл — повторяю в отслеживаемый топик %s",
            thread_id,
            fallback,
        )

        return tg.send_message(
            chat_id,
            text,
            disable_notification=disable_notification,
            message_thread_id=fallback,
        )

    return None


def _send_action(chat_id, action="typing"):
    return tg.send_chat_action(
        chat_id,
        action,
        message_thread_id=_routes.get(_chat_key(chat_id)),
    )


def _handle_wrong_topic(chat_id, thread_id, reason):
    """Сообщение пришло не из отслеживаемого топика."""
    logger.info(
        "Ignored: chat=%s thread=%s reason=%s (monitored: %s)",
        chat_id,
        thread_id,
        reason,
        monitored_topic_label(),
    )

    if not WRONG_TOPIC_HINT:
        return

    key = (_chat_key(chat_id), thread_id)

    if key in _hinted_threads:
        return

    _hinted_threads.add(key)

    if reason == "name-unknown":
        text = (
            "⚠️ I can't identify this topic yet, so messages from it are "
            "ignored.\n"
            "Send /id here and set TELEGRAM_TOPIC_ID=<message_thread_id> "
            "in .env — after that the topic is matched reliably."
        )
    else:
        text = (
            "⚠️ This topic is not monitored. Please send robot exceptions "
            f"to the {monitored_topic_label()}."
        )

    _send(chat_id, text, thread_id=thread_id)


# ============================================================
# TELEGRAM FLOWS
# ============================================================

def _sender_title(sender: dict) -> str:
    username = sender.get("username")

    if username:
        return f"@{username}"

    return sender.get("first_name") or str(sender.get("id"))


def _show_console_message(chat, sender, message_type: str, extra_rows=None):
    table = Table(show_header=False)

    table.add_row("👤 Отправитель", _sender_title(sender))
    table.add_row("🆔 ID", str(sender.get("id")))
    table.add_row("💬 Чат", str(chat.get("id")))
    table.add_row("📝 Тип", message_type)
    table.add_row(
        "🧵 Топик",
        str(_routes.get(_chat_key(chat.get("id")))) if chat.get("id") is not None else "-",
    )

    for label, value in extra_rows or []:
        table.add_row(label, str(value))

    console.print(
        Panel(
            table,
            title="[bold cyan]📩 Telegram Message[/bold cyan]",
            border_style="blue",
        )
    )


def _handle_reg(chat_id, sender, args, reply_to):
    telegram_id = sender.get("id")

    if not args:
        _send(
            chat_id,
            "Usage: /reg <Your Name>\n"
            "Example: /reg Ivan Petrenko",
            reply_to_message_id=reply_to,
        )
        return

    employee_name, suggestions = resolve_employee_name(args)

    if not employee_name:
        text = f"⚠️ Employee {args!r} not found in the employees list."

        if suggestions:
            text += "\n\nDid you mean:\n" + "\n".join(
                f"  • {name}" for name in suggestions
            )

        text += "\n\nCheck the spelling and send /reg again."

        _send(chat_id, text, reply_to_message_id=reply_to)
        return

    username = sender.get("username")

    if not link_user(telegram_id, username, employee_name):
        _send(
            chat_id,
            "⚠️ Can't save the link right now (database error). "
            "Please tell the administrator.",
            reply_to_message_id=reply_to,
        )
        return

    _send(
        chat_id,
        f"✅ Linked to employee: {employee_name}\n"
        f"Telegram: {_sender_title(sender)} (id {telegram_id})\n"
        "\nNow just send exceptions in the usual format.",
        reply_to_message_id=reply_to,
    )


def _handle_unreg(chat_id, sender, reply_to):
    telegram_id = sender.get("id")

    if not unlink_user(telegram_id):
        _send(
            chat_id,
            "⚠️ Can't remove the link right now (database error).",
            reply_to_message_id=reply_to,
        )
        return

    _send(
        chat_id,
        "✅ Link removed. Use /reg <Your Name> to link again.",
        reply_to_message_id=reply_to,
    )


def _handle_whoami(chat_id, sender, reply_to):
    employee_name = get_employee_name(sender.get("id"))

    if not employee_name:
        _send(chat_id, NOT_REGISTERED_HINT, reply_to_message_id=reply_to)
        return

    _send(
        chat_id,
        f"👤 {_sender_title(sender)} → employee: {employee_name}",
        reply_to_message_id=reply_to,
    )


def _stats_text(shift_date: str, shift_name: str) -> str:
    total, by_robot, by_type = shift_stats(shift_date, shift_name)

    lines = [
        "📊 Shift statistics",
        "",
        f"📅 Date: {shift_date}",
        f"🕐 Shift: {shift_name}",
        "",
        f"Total exceptions: {total}",
    ]

    if by_robot:
        lines.append("")
        lines.append("🤖 By robot:")
        for robot, count in sorted(
            by_robot.items(),
            key=lambda item: (-item[1], item[0]),
        )[:15]:
            lines.append(f"  • Robot {robot}: {count}")

    if by_type:
        lines.append("")
        lines.append("⚠️ By issue type:")
        for issue_type, count in sorted(
            by_type.items(),
            key=lambda item: (-item[1], item[0]),
        )[:15]:
            lines.append(f"  • {issue_type}: {count}")

    return "\n".join(lines)


def _handle_stats(chat_id, args, reply_to):
    parts = args.split()

    shift_date = parts[0] if len(parts) > 0 else None
    shift_name = parts[1] if len(parts) > 1 else None

    if not shift_date and not shift_name:
        shift_date, shift_name = get_current_shift()
    elif not shift_date or not shift_name or shift_name not in ("day", "night"):
        _send(
            chat_id,
            "Usage: /stats [YYYY-MM-DD] [day|night]\n"
            "Example: /stats 2026-03-08 night",
            reply_to_message_id=reply_to,
        )
        return

    _send(
        chat_id,
        _stats_text(shift_date, shift_name),
        reply_to_message_id=reply_to,
    )


def _handle_command(chat_id, sender, command, args, reply_to, chat=None) -> bool:
    """Обрабатывает команду. True, если команда распознана."""
    if command in ("start", "help"):
        _send(
            chat_id,
            HELP_TEXT + f"\n\n📌 Monitored: {monitored_topic_label()}",
            reply_to_message_id=reply_to,
        )
        return True

    if command == "reg":
        _handle_reg(chat_id, sender, args, reply_to)
        return True

    if command == "unreg":
        _handle_unreg(chat_id, sender, reply_to)
        return True

    if command == "whoami":
        _handle_whoami(chat_id, sender, reply_to)
        return True

    if command == "stats":
        _handle_stats(chat_id, args, reply_to)
        return True

    if command == "id":
        thread_id = _routes.get(_chat_key(chat_id))
        name = topic_name(chat_id, thread_id)
        allowed, reason = topic_allowed(chat_id, thread_id)

        _send(
            chat_id,
            f"💬 chat_id: {chat_id}\n"
            f"👤 user_id: {sender.get('id')}\n"
            f"🏷 chat_type: {(chat or {}).get('type')}\n"
            f"🧵 message_thread_id: {thread_id}\n"
            f"📌 topic name: {name or 'unknown'}\n"
            f"⚙️ monitored: {monitored_topic_label()}\n"
            f"{'✅' if allowed else '⛔'} this topic is "
            f"{'monitored' if allowed else 'ignored'} ({reason})",
            reply_to_message_id=reply_to,
        )
        return True

    return False


def handle_error_text(chat_id, sender, text, message_id):
    """Сообщение с описанием ошибки: сохранить и переслать в Lark."""
    employee_name = get_employee_name(sender.get("id"))

    if not employee_name:
        _send(chat_id, NOT_REGISTERED_HINT, reply_to_message_id=message_id)
        return

    parsed = parse_error_message(text)

    if not parsed:
        _send(chat_id, FORMAT_HINT, reply_to_message_id=message_id)
        return

    if not parsed["robot"].isdigit():
        _send(
            chat_id,
            f"⚠️ Robot number must be digits, got {parsed['robot']!r}.\n"
            "Example: Unable to drive: Security module failure. 3780",
            reply_to_message_id=message_id,
        )
        return

    shift_date, shift_name = get_current_shift()

    data_obj = {
        "employee": employee_name,
        "robot": parsed["robot"],
        "error_text": parsed["error_text"],
    }

    logger.info(
        "Exception from %s: robot=%s type=%s shift=%s/%s",
        employee_name,
        parsed["robot"],
        parsed["error_type"],
        shift_date,
        shift_name,
    )

    # send_to_data_base сам сообщит в чат, если шаблон/сотрудник/робот
    # не найдены (через notifier, т.е. в Telegram).
    saved = send_to_data_base(parsed, data_obj, chat_id)

    if not saved:
        logger.warning(
            "Exception not saved, skip forwarding: robot=%s",
            parsed["robot"],
        )
        return

    count = count_robot_errors_in_shift(
        parsed["robot"],
        shift_date,
        shift_name,
    )

    pretty = now_warsaw().strftime("%d.%m.%Y %H:%M:%S")

    table_lines = [
        ("👤 Employee", employee_name),
        ("🤖 Robot", parsed["robot"]),
        ("⚠️ Time", pretty),
        ("📝 Details", parsed["error_text"]),
        ("📊 Shift issues", str(count)),
    ]

    forwarded = forward_error(parsed, table_lines)

    if count >= ERROR_THRESHOLD:
        notify_user(
            chat_id,
            f"⚠️ Robot {parsed['robot']} has {count} exceptions "
            f"this shift. It should be sent to maintenance!",
        )
        console.print(
            f"[bold red]Robot {parsed['robot']}: {count} exceptions "
            f"this shift[/bold red]"
        )
    elif SEND_CONFIRMATION:
        suffix = "" if forwarded else " (Lark forward failed, see logs)"

        notify_user(
            chat_id,
            f"✅ Saved: robot {parsed['robot']} — {parsed['error_text']}\n"
            f"📊 Shift issues: {count}{suffix}",
        )


def handle_photo(chat_id, sender, message, message_id):
    """Фото: скачать из Telegram и переслать в Lark-группу."""
    sizes = message.get("photo") or []

    if not sizes:
        return

    best = sizes[-1]

    file_path = tg.get_file(best.get("file_id"))

    if not file_path:
        _send(
            chat_id,
            "⚠️ Can't download the photo from Telegram. Please try again.",
            reply_to_message_id=message_id,
        )
        return

    extension = os.path.splitext(file_path)[1] or ".jpg"
    file_unique_id = best.get("file_unique_id") or best.get("file_id")

    destination = os.path.join(
        _IMAGES_DIR,
        f"tg_{file_unique_id}{extension}",
    )

    if not tg.download_file(file_path, destination):
        _send(
            chat_id,
            "⚠️ Can't save the photo on the server. Please try again.",
            reply_to_message_id=message_id,
        )
        return

    employee_name = get_employee_name(sender.get("id"))

    if employee_name:
        caption = f"📷 Photo from {employee_name}"
    else:
        caption = f"📷 Photo from {_sender_title(sender)} (Telegram)"

    console.print(f"[cyan]📷 Фото сохранено: {destination}[/cyan]")

    mode = handle_incoming_photo(destination, console, caption=caption)

    if mode == "lark":
        if SEND_CONFIRMATION:
            _send(
                chat_id,
                "✅ Photo forwarded to Lark",
                reply_to_message_id=message_id,
            )
    elif mode == "link":
        _send(
            chat_id,
            "✅ Photo sent to Lark as a link\n"
            "(Lark API quota exceeded — uploaded to Supabase Storage)",
            reply_to_message_id=message_id,
        )
    else:
        _send(
            chat_id,
            "⚠️ Can't forward the photo to Lark right now (see logs).",
            reply_to_message_id=message_id,
        )


def handle_update(update: dict, bot_username: str = None):
    """Обрабатывает один апдейт Telegram."""
    message = update.get("message") or update.get("edited_message")

    if not message:
        return

    sender = message.get("from") or {}

    if sender.get("is_bot"):
        return

    chat = message.get("chat") or {}
    chat_id = chat.get("id")

    if chat_id is None:
        return

    message_id = message.get("message_id")

    if message_id is not None and _already_processed(message_id):
        logger.info("Duplicate message %s, skipping", message_id)
        return

    if _is_message_too_old(message.get("date")):
        logger.warning(
            "Message %s is older than %ss, skipping",
            message_id,
            MESSAGE_MAX_AGE_SECONDS,
        )
        return

    thread_id = message.get("message_thread_id")

    _learn_topic_from_message(chat_id, message)

    # Все ответы бота уходят в тот же топик.
    _routes[_chat_key(chat_id)] = thread_id

    text = message.get("text")
    caption = message.get("caption")

    # Команды (/id, /reg, /help) работают в любом топике, чтобы можно было
    # настроиться; содержимое (ошибки, фото) — только в нужном топике.
    is_command = bool(text) and text.startswith("/")

    command = None

    if is_command:
        command, _args = parse_command(text, BOT_USERNAME)

    if not _chat_allowed(chat_id) and command not in BOOTSTRAP_COMMANDS:
        logger.warning(
            "Message from chat %s ignored (not in allow-list)", chat_id
        )
        return

    allowed, reason = topic_allowed(chat_id, thread_id)

    if not allowed and not is_command:
        _handle_wrong_topic(chat_id, thread_id, reason)
        return

    if text:
        _handle_text_message(chat_id, sender, text, message_id, chat)
        return

    if message.get("photo"):
        _show_console_message(chat, sender, "photo", extra_rows=[("🖼 Caption", caption or "-")])
        _send_action(chat_id, "upload_photo")
        handle_photo(chat_id, sender, message, message_id)
        return

    if caption:
        # Фото/документ с подписью без самого фото — просто подсказка.
        _send(chat_id, FORMAT_HINT, reply_to_message_id=message_id)
        return

    _send(
        chat_id,
        "⚠️ Unsupported message type. Send text or photo.",
        reply_to_message_id=message_id,
    )


def _handle_text_message(chat_id, sender, text, message_id, chat):
    if text.startswith("/"):
        command, args = parse_command(text, BOT_USERNAME)

        if command is None:
            # Команда адресована другому боту в группе.
            return

        normalized = normalize_command(command)

        if normalized != command:
            logger.info(
                "Команда /%s распознана как /%s (раскладка клавиатуры)",
                command,
                normalized,
            )

        _show_console_message(
            chat,
            sender,
            "command",
            extra_rows=[
                ("⚙️ Команда", text),
                ("🔤 Распознано", f"/{normalized}"),
            ],
        )

        if _handle_command(chat_id, sender, normalized, args, message_id, chat):
            return

        suggestion = suggest_command(normalized)

        if suggestion:
            hint = (
                f"🤔 Unknown command: /{command}\n"
                f"Did you mean /{suggestion}?"
            )
        else:
            hint = (
                f"🤔 Unknown command: /{command}\n"
                "Send /help to see the available commands."
            )

        _send(chat_id, hint, reply_to_message_id=message_id)
        return

    parsed = parse_error_message(text)

    _show_console_message(
        chat,
        sender,
        "text",
        extra_rows=[
            ("💭 Текст", text),
            ("🤖 Robot", parsed["robot"] if parsed else "-"),
            ("⚠️ Issue Type", parsed["error_type"] if parsed else "-"),
        ],
    )

    _send_action(chat_id, "typing")

    handle_error_text(chat_id, sender, text, message_id)


# ============================================================
# LONG POLLING
# ============================================================

BOT_USERNAME = None

# Статус таблицы привязок (проверяется один раз на старте, чтобы
# /health не дёргал Supabase на каждый запрос).
USERS_TABLE_OK = None

USERS_TABLE = "telegram_users"


def polling_loop(stop_event: threading.Event = None):
    """Бесконечный long polling Telegram."""
    offset = None

    logger.info("Telegram polling started (bot=%s)", BOT_USERNAME)

    while stop_event is None or not stop_event.is_set():
        updates = tg.get_updates(offset=offset, timeout=30)

        if updates is None:
            # Ошибка сети/токена — пауза и повтор.
            time.sleep(5)
            continue

        for update in updates:
            update_id = update.get("update_id")

            if update_id is not None:
                offset = update_id + 1

            try:
                handle_update(update, BOT_USERNAME)
            except Exception:
                logger.exception("Failed to handle update %s", update_id)

        if not updates:
            time.sleep(0.2)


def start_polling() -> threading.Thread:
    thread = threading.Thread(
        target=polling_loop,
        name="telegram-polling",
        daemon=True,
    )
    thread.start()

    return thread


# ============================================================
# HTTP (Railway healthcheck + статистика)
# ============================================================

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "bot": BOT_USERNAME,
        "users_table": USERS_TABLE_OK,
        "time": now_warsaw().strftime("%d.%m.%Y %H:%M:%S"),
    })


@app.route("/shift_stats", methods=["GET"])
def shift_stats_endpoint():
    shift_date = request.args.get("date")
    shift_name = request.args.get("shift")

    if not shift_date or not shift_name:
        shift_date, shift_name = get_current_shift()

    total, by_robot, by_type = shift_stats(shift_date, shift_name)

    return jsonify({
        "shift_date": shift_date,
        "shift_name": shift_name,
        "total_errors": total,
        "by_robot": by_robot,
        "by_error_type": by_type,
    })


# ============================================================
# APPLICATION START
# ============================================================

def main():
    global BOT_USERNAME

    if not tg.token_configured():
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set. "
            "Add it to .env (see README_TELEGRAM.md)."
        )

    me = tg.get_me()

    if not me:
        raise SystemExit(
            "Can't reach Telegram with the configured token. "
            "Check TELEGRAM_BOT_TOKEN."
        )

    BOT_USERNAME = me.get("username")

    tg.set_my_commands([
        {"command": "reg", "description": "Link Telegram to your employee name"},
        {"command": "whoami", "description": "Show current link"},
        {"command": "unreg", "description": "Remove the link"},
        {"command": "stats", "description": "Shift statistics"},
        {"command": "id", "description": "Show chat/user IDs"},
        {"command": "help", "description": "Help"},
    ])

    # Ответы пользователю идут в Telegram, а не в Lark API.
    set_notifier(lambda chat_id, text: _send(chat_id, text))

    console.print("[bold green]Telegram-бот запущен[/bold green]")
    console.print(f"[cyan]Bot: @{BOT_USERNAME}[/cyan]")
    console.print(f"[cyan]Timezone: {WARSAW_TZ}[/cyan]")
    console.print(
        f"[cyan]Current time: {now_warsaw().strftime('%d.%m.%Y %H:%M:%S')}[/cyan]"
    )

    if ALLOWED_CHAT_IDS:
        console.print(f"[cyan]Allowed chats: {sorted(ALLOWED_CHAT_IDS)}[/cyan]")

    console.print(f"[cyan]Monitored topic: {monitored_topic_label()}[/cyan]")

    global USERS_TABLE_OK
    USERS_TABLE_OK = table_exists(USERS_TABLE)

    if USERS_TABLE_OK:
        console.print(
            f"[green]Привязки сотрудников: таблица {USERS_TABLE} на месте[/green]"
        )
    else:
        console.print(
            f"[bold yellow]⚠️ Таблицы {USERS_TABLE} нет — /reg не сможет "
            f"сохранить привязку. Выполните sql/{USERS_TABLE}.sql в Supabase."
            f"[/bold yellow]"
        )

    if TELEGRAM_TOPIC_ID is None and TELEGRAM_TOPIC_NAME:
        console.print(
            "[yellow]Совет: отправьте /id в нужном топике и задайте "
            "TELEGRAM_TOPIC_ID=<message_thread_id> — имя топика бот знает "
            "только если видел сообщение о его создании/переименовании."
            "[/yellow]"
        )

    # Отчёт за смену (в конце каждой смены шлёт метрики в целевую группу).
    start_shift_scheduler()

    start_polling()

    port = int(os.environ.get("PORT", 7777))

    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
