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
import signal
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
import bot_lease
from lark_media import hook_ok, send_card_via_hook, send_text_via_hook
from pending_photos import TARGET_HOOK_URL, forward_error, handle_incoming_photo
from supabase_storage import download_json, upload_json
import robot_status
from sendToDataBase import (
    WAREHOUSE,
    count_robot_errors_in_shift,
    notify_user,
    send_to_data_base,
    set_notifier,
    shift_stats,
    table_exists,
)
from shift import get_current_shift
from shift_report import build_shift_summary, shift_metrics, start_shift_scheduler
from text_utils import truncate
from telegram_store import (
    get_employee,
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

# Через сколько секунд удалять свои служебные подтверждения
# («сохранено», «фото переслано»). 0 — не удалять.
CONFIRM_TTL_SECONDS = _env_int("TELEGRAM_CONFIRM_TTL", 10)

# Сколько ждём описание причины при смене статуса робота.
STATUS_FLOW_TTL_SECONDS = _env_int("TELEGRAM_STATUS_FLOW_TTL", 300)

# Как часто standby-инстанс пробует забрать освободившийся лиз.
STANDBY_RETRY_SECONDS = _env_int("BOT_STANDBY_RETRY", 10)

# Как часто отдельный поток продлевает лиз. Обработка фото может занять
# минуты, поэтому heartbeat нельзя делать только в цикле опроса.
LEASE_HEARTBEAT_SECONDS = _env_int("BOT_LEASE_HEARTBEAT", 15)

# Если опрашивающий инстанс не делал getUpdates дольше этого времени —
# /health отдаёт 503, чтобы Railway перезапустил контейнер.
POLL_STALL_SECONDS = _env_int("POLL_STALL_SECONDS", 180)

# Где храним подтверждённый offset Telegram (чтобы после перезапуска
# Telegram не переотдал уже обработанные апдейты = дубли).
OFFSET_BUCKET = os.environ.get("SUPABASE_STATE_BUCKET", "bot-state")
OFFSET_OBJECT = "telegram-offset.json"

# Сколько дней хранить скачанные из Telegram фото (0 — не удалять).
IMAGES_RETENTION_DAYS = _env_int("IMAGES_RETENTION_DAYS", 7)

# Необязательный токен для HTTP-эндпоинта /shift_stats.
STATS_TOKEN = os.environ.get("STATS_TOKEN", "").strip()

# Сколько секунд дать текущему апдейту дописаться при остановке контейнера.
SHUTDOWN_GRACE_SECONDS = _env_int("SHUTDOWN_GRACE_SECONDS", 3)

# Эти команды отвечают даже в чате не из белого списка: иначе после
# включения TELEGRAM_ALLOWED_CHAT_IDS нельзя было бы узнать chat_id через /id.
BOOTSTRAP_COMMANDS = ("id", "help", "start")

# Все поддерживаемые команды (для подсказок «может, вы имели в виду…»).
KNOWN_COMMANDS = (
    "reg", "unreg", "whoami", "stats", "id", "help", "start",
    "offline", "online", "cancel",
)

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
    "/stats [date] [day|night] — shift statistics\n"
    "/offline <robot> — take a robot out of service\n"
    "/online <robot> — return a robot to service\n"
    "/cancel — cancel the current action\n"
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

def _is_processed(key) -> bool:
    """Уже обрабатывали этот апдейт в этом процессе?"""
    with _seen_lock:
        return key in _seen_message_ids


def _mark_processed(key) -> None:
    """
    Помечает апдейт обработанным.

    Вызывается ТОЛЬКО после успешной обработки: иначе разовый сбой БД
    «съел» бы сообщение навсегда, потому что повторная доставка была бы
    отброшена как дубль.
    """
    if not key:
        return

    with _seen_lock:
        _seen_message_ids[key] = True
        _seen_message_ids.move_to_end(key)

        if len(_seen_message_ids) > _SEEN_LIMIT:
            _seen_message_ids.popitem(last=False)


def _update_key(update: dict) -> str:
    """Ключ апдейта для дедупа: (chat_id, message_id) или id нажатия кнопки."""
    callback = update.get("callback_query")

    if callback:
        callback_id = callback.get("id")

        return f"cb:{callback_id}" if callback_id else ""

    message = update.get("message") or update.get("edited_message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")

    if chat_id is None or message_id is None:
        return ""

    # Ключ именно (чат, сообщение): в разных чатах id пересекаются.
    return f"{chat_id}:{message_id}"


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
_chat_types = {}       # chat_id -> тип чата (supergroup/private/...)

# Общие словари пишутся из потока опроса и читаются из Flask/нотификатора.
_routes_lock = threading.Lock()


def _chat_key(chat_id) -> int:
    return int(chat_id)


def _set_route(chat_id, thread_id, chat_type: str = None):
    """Запоминаем, в какой топик отвечать и что это за чат."""
    key = _chat_key(chat_id)

    with _routes_lock:
        _routes[key] = thread_id

        if chat_type:
            _chat_types[key] = chat_type


def _route_thread(chat_id):
    with _routes_lock:
        return _routes.get(_chat_key(chat_id))


def _route_chat_type(chat_id):
    with _routes_lock:
        return _chat_types.get(_chat_key(chat_id))


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


def _send(
    chat_id,
    text,
    reply_to_message_id=None,
    thread_id=None,
    disable_notification=False,
    delete_after: int = None,
    reply_markup: dict = None,
):
    """
    Отправка с автоопределением топика: ответ уходит в тот же топик,
    откуда пришло сообщение (для форум-групп).

    Если в этот топик отправить нельзя (например, General закрыт —
    Telegram отвечает TOPIC_CLOSED), повторяем в отслеживаемый топик,
    чтобы пользователь всё-таки увидел ответ.

    delete_after — через сколько секунд удалить это сообщение (0/None — не удалять).
    """
    if thread_id is None:
        thread_id = _route_thread(chat_id)

    result = tg.send_message(
        chat_id,
        text,
        reply_to_message_id=reply_to_message_id,
        disable_notification=disable_notification,
        message_thread_id=thread_id,
        reply_markup=reply_markup,
    )

    if result is None:
        fallback = TELEGRAM_TOPIC_ID
        # Откат нужен только для групп: в личке ответ в группу отправлять
        # нельзя. Раньше условие требовало непустой белый список чатов,
        # из-за чего в конфиге «все чаты» откат не срабатывал вообще.
        is_group = _route_chat_type(chat_id) in ("group", "supergroup")

        if (
            fallback is not None
            and (is_group or _chat_key(chat_id) in ALLOWED_CHAT_IDS)
            and (thread_id is None or int(thread_id) != int(fallback))
        ):
            logger.warning(
                "Ответ в топик %s не ушёл — повторяю в отслеживаемый топик %s",
                thread_id,
                fallback,
            )

            result = tg.send_message(
                chat_id,
                text,
                disable_notification=disable_notification,
                message_thread_id=fallback,
            )

    if result is not None and delete_after:
        schedule_deletion(
            chat_id,
            result.get("message_id"),
            delay=delete_after,
        )

    return result


def _send_action(chat_id, action="typing"):
    return tg.send_chat_action(
        chat_id,
        action,
        message_thread_id=_route_thread(chat_id),
    )


# ------------------------------------------------------------
# Самоудаление служебных сообщений
# ------------------------------------------------------------
#
# Подтверждения («✅ Saved», «✅ Photo forwarded») нужны только как
# мгновенная обратная связь, поэтому через CONFIRM_TTL_SECONDS бот
# удаляет их за собой, чтобы группа не зарастала «хвостами».

_pending_deletions = []
_pending_lock = threading.Lock()


def schedule_deletion(chat_id, message_id, delay: int = None) -> bool:
    """Ставит сообщение бота в очередь на удаление через delay секунд."""
    delay = CONFIRM_TTL_SECONDS if delay is None else int(delay)

    if delay <= 0 or message_id is None:
        return False

    with _pending_lock:
        _pending_deletions.append((time.time() + delay, int(chat_id), int(message_id)))

    logger.info(
        "Сообщение %s в чате %s будет удалено через %ss",
        message_id,
        chat_id,
        delay,
    )

    return True


def _drain_pending_deletions(now: float = None) -> int:
    """Удаляет все сообщения, срок которых наступил. Возвращает их число."""
    now = time.time() if now is None else now

    with _pending_lock:
        due = [item for item in _pending_deletions if item[0] <= now]
        for item in due:
            _pending_deletions.remove(item)

    for _due_at, chat_id, message_id in due:
        tg.delete_message(chat_id, message_id)

    return len(due)


def _deletion_loop():
    while True:
        try:
            _drain_pending_deletions()
        except Exception:
            logger.exception("Ошибка при удалении служебных сообщений")

        time.sleep(1)


def cleanup_old_images() -> int:
    """Удаляет фото из IMAGES_DIR старше IMAGES_RETENTION_DAYS."""
    if IMAGES_RETENTION_DAYS <= 0:
        return 0

    cutoff = time.time() - IMAGES_RETENTION_DAYS * 86400
    removed = 0

    try:
        names = os.listdir(_IMAGES_DIR)
    except OSError:
        return 0

    for name in names:
        path = os.path.join(_IMAGES_DIR, name)

        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            logger.warning("Не удалось удалить старый файл %s", path)

    if removed:
        logger.info(
            "Очистка %s: удалено файлов старше %s дней — %s",
            _IMAGES_DIR,
            IMAGES_RETENTION_DAYS,
            removed,
        )

    return removed


def images_janitor_loop(interval_seconds: int = 3600):
    """Раз в час убираем старые фото, чтобы диск не разрастался."""
    while True:
        try:
            cleanup_old_images()
            sweep_pending_status()
        except Exception:
            logger.exception("Ошибка очистки старых фото")

        time.sleep(interval_seconds)


def start_images_janitor() -> threading.Thread:
    thread = threading.Thread(
        target=images_janitor_loop,
        name="images-janitor",
        daemon=True,
    )
    thread.start()

    return thread


def start_deletion_worker() -> threading.Thread:
    thread = threading.Thread(
        target=_deletion_loop,
        name="telegram-deletion",
        daemon=True,
    )
    thread.start()

    return thread


def load_saved_offset():
    """Последний подтверждённый offset из Storage (None — нет/ошибка)."""
    data = download_json(OFFSET_BUCKET, OFFSET_OBJECT)

    if not data:
        return None

    try:
        return int(data.get("offset"))
    except (TypeError, ValueError):
        logger.warning("Некорректный сохранённый offset: %r", data)
        return None


def save_offset(offset) -> bool:
    """Сохраняет offset, чтобы Telegram не переотдал обработанные апдейты."""
    if offset is None:
        return False

    return upload_json(
        OFFSET_BUCKET,
        OFFSET_OBJECT,
        {
            "offset": int(offset),
            "saved_at": datetime.now(WARSAW_TZ).strftime("%d.%m.%Y %H:%M:%S"),
        },
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
# СМЕНА СТАТУСА РОБОТА (офлайн / онлайн)
# ============================================================
#
# Флоу: /offline 3680 → бот показывает робота и кнопки причин → тап по
# причине → бот просит описать причину сообщением → сотрудник пишет →
# бот убирает свои сообщения, меняет статус в базе и отправляет карточку
# в целевую Lark-группу.

STATUS_DIRECTIONS = ("offline", "online")

_pending_status = {}
_pending_status_lock = threading.Lock()


def _pending_key(chat_id, user_id):
    return (int(chat_id), int(user_id))


def set_pending_status(chat_id, user_id, data: dict):
    with _pending_status_lock:
        _pending_status[_pending_key(chat_id, user_id)] = dict(
            data,
            expires=time.time() + STATUS_FLOW_TTL_SECONDS,
        )


def take_pending_status(chat_id, user_id):
    """
    Атомарно забирает незавершённый флоу (single-flight).

    Именно забирает, а не читает: иначе две копии одного сообщения (повторная
    доставка апдейта, двойной тап) успевают обе пройти проверку и статус
    меняется дважды с двумя записями в журнале и двумя карточками в Lark.
    """
    key = _pending_key(chat_id, user_id)

    with _pending_status_lock:
        data = _pending_status.pop(key, None)

    if not data:
        return None

    if data.get("expires", 0) < time.time():
        return None

    return data


def peek_pending_status(chat_id, user_id):
    """Незавершённый флоу без изъятия (для проверок и логов)."""
    key = _pending_key(chat_id, user_id)

    with _pending_status_lock:
        data = _pending_status.get(key)

    if not data or data.get("expires", 0) < time.time():
        return None

    return dict(data)


def sweep_pending_status() -> int:
    """Удаляет просроченные незавершённые флоу, чтобы словарь не рос."""
    now = time.time()
    removed = 0

    with _pending_status_lock:
        for key, data in list(_pending_status.items()):
            if data.get("expires", 0) < now:
                _pending_status.pop(key, None)
                removed += 1

    return removed


def clear_pending_status(chat_id, user_id):
    with _pending_status_lock:
        return _pending_status.pop(_pending_key(chat_id, user_id), None)


def reasons_keyboard(direction: str, robot_id) -> dict:
    """Кнопки с причинами: callback_data несёт всё нужное, состояние не нужно."""
    rows = [
        [{"text": label, "callback_data": f"st:{direction}:{robot_id}:{code}"}]
        for code, label in robot_status.REASONS.get(direction, ())
    ]

    rows.append([{
        "text": "✖️ Cancel",
        "callback_data": f"st:{direction}:{robot_id}:cancel",
    }])

    return {"inline_keyboard": rows}


def _delete_quiet(chat_id, message_id) -> bool:
    """Удаляет сообщение бота, если у него есть id."""
    if message_id is None:
        return False

    return tg.delete_message(chat_id, message_id)


def status_usage(direction: str) -> str:
    return (
        f"Usage: /{direction} <robot number>\n"
        f"Example: /{direction} 3680"
    )


def _handle_status_command(chat_id, sender, direction, args, message_id):
    """/offline или /online: показываем робота и кнопки причин."""
    employee = get_employee(sender.get("id"))

    if not employee:
        _send(chat_id, NOT_REGISTERED_HINT, reply_to_message_id=message_id)
        return

    if not args:
        _send(chat_id, status_usage(direction), reply_to_message_id=message_id)
        return

    robot_number = args.split()[0]
    robot = robot_status.find_robot(robot_number)
    spec = robot_status.DIRECTIONS[direction]

    if not robot:
        _send(
            chat_id,
            f"⚠️ Robot {robot_number} not found in {WAREHOUSE}.",
            reply_to_message_id=message_id,
        )
        return

    if robot_status.is_in_status(robot, direction):
        other = "online" if direction == "offline" else "offline"

        _send(
            chat_id,
            f"ℹ️ Robot {robot.get('robot_number')} is already "
            f"{robot.get('status')}.\n"
            f"Use /{other} {robot.get('robot_number')} if that is wrong.",
            reply_to_message_id=message_id,
        )
        return

    _send(
        chat_id,
        f"{spec['emoji']} Robot {robot.get('robot_number')} · "
        f"{robot.get('robot_type') or '-'} · {robot.get('warehouse') or WAREHOUSE}\n"
        f"Status: {robot.get('status')} → {spec['new_status']}\n"
        "\n"
        "Choose the reason:",
        reply_to_message_id=message_id,
        reply_markup=reasons_keyboard(direction, robot.get("id")),
    )


def handle_status_callback(callback: dict):
    """Нажатие кнопки с причиной."""
    sender = callback.get("from") or {}
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    callback_id = callback.get("id")

    if chat_id is None:
        tg.answer_callback_query(callback_id)
        return

    if callback_id and _is_processed(f"cb:{callback_id}"):
        tg.answer_callback_query(callback_id, "Already handled")
        return

    thread_id = message.get("message_thread_id")
    _set_route(chat_id, thread_id, chat.get("type"))

    if not _chat_allowed(chat_id):
        tg.answer_callback_query(callback_id, "This chat is not allowed")
        return

    allowed, _reason = topic_allowed(chat_id, thread_id)

    if not allowed:
        tg.answer_callback_query(callback_id, "This topic is not monitored")
        return

    parts = (callback.get("data") or "").split(":")

    if len(parts) != 4 or parts[0] != "st":
        tg.answer_callback_query(callback_id)
        return

    _, direction, robot_id, code = parts

    if direction not in STATUS_DIRECTIONS:
        tg.answer_callback_query(callback_id, "Unknown action")
        return

    if code == "cancel":
        tg.answer_callback_query(callback_id, "Cancelled")
        clear_pending_status(chat_id, sender.get("id"))
        _delete_quiet(chat_id, message_id)
        return

    employee = get_employee(sender.get("id"))

    if not employee:
        tg.answer_callback_query(callback_id, "Register first: /reg <Your Name>")
        return

    label = robot_status.reason_label(direction, code)

    if not label:
        tg.answer_callback_query(callback_id, "Unknown reason")
        return

    robot = robot_status.find_robot_by_id(robot_id)

    if not robot:
        tg.answer_callback_query(callback_id, "Robot not found")
        return

    spec = robot_status.DIRECTIONS[direction]

    if robot_status.is_in_status(robot, direction):
        tg.answer_callback_query(callback_id, f"Already {spec['label']}")
        _delete_quiet(chat_id, message_id)
        return

    set_pending_status(chat_id, sender.get("id"), {
        "direction": direction,
        "robot_number": robot.get("robot_number"),
        "type_problem": label,
        "prompt_message_id": message_id,
    })

    tg.answer_callback_query(callback_id, f"Reason: {label}")

    tg.edit_message_text(
        chat_id,
        message_id,
        f"{spec['emoji']} Robot {robot.get('robot_number')} · "
        f"{robot.get('status')} → {spec['new_status']}\n"
        f"Reason: {label}\n"
        "\n"
        "✍️ Describe the reason in one message (or /cancel).",
    )


def _notify_lark_status(direction, result, employee_name) -> bool:
    """Карточка о смене статуса в целевой Lark-группе (с откатом на текст)."""
    card = robot_status.build_status_card(direction, result, employee_name)
    hook_result = send_card_via_hook(TARGET_HOOK_URL, card)

    if hook_ok(hook_result):
        logger.info(
            "Статус робота #%s отправлен в Lark карточкой",
            result["robot"].get("robot_number"),
        )
        return True

    logger.warning("Карточка статуса не прошла (%s) — отправляю текстом", hook_result)

    send_text_via_hook(
        TARGET_HOOK_URL,
        robot_status.build_status_text(direction, result, employee_name),
    )

    return False


def finish_status_change(chat_id, sender, note, message_id, pending: dict):
    """Сотрудник описал причину: меняем статус и убираем свои сообщения."""
    direction = pending["direction"]
    note = (note or "").strip()

    if not note:
        # Флоу уже изъят из состояния (single-flight) — возвращаем его,
        # чтобы сотрудник мог дописать причину следующим сообщением.
        set_pending_status(chat_id, sender.get("id"), pending)

        _send(
            chat_id,
            "✍️ Please describe the reason in one message (or /cancel).",
            reply_to_message_id=message_id,
            delete_after=CONFIRM_TTL_SECONDS,
        )
        return

    employee = get_employee(sender.get("id"))

    if not employee:
        clear_pending_status(chat_id, sender.get("id"))
        _send(chat_id, NOT_REGISTERED_HINT, reply_to_message_id=message_id)
        return

    robot = robot_status.find_robot(pending["robot_number"])

    if not robot:
        clear_pending_status(chat_id, sender.get("id"))
        _send(
            chat_id,
            f"⚠️ Robot {pending['robot_number']} not found in {WAREHOUSE}.",
            reply_to_message_id=message_id,
        )
        return

    if robot_status.is_in_status(robot, direction):
        clear_pending_status(chat_id, sender.get("id"))
        _delete_quiet(chat_id, pending.get("prompt_message_id"))
        _send(
            chat_id,
            f"ℹ️ Robot {robot.get('robot_number')} is already "
            f"{robot.get('status')}.",
            reply_to_message_id=message_id,
            delete_after=CONFIRM_TTL_SECONDS,
        )
        return

    result = robot_status.change_robot_status(
        robot,
        direction,
        pending["type_problem"],
        note,
        employee,
    )

    clear_pending_status(chat_id, sender.get("id"))

    if not result:
        _send(
            chat_id,
            "⚠️ Can't change the robot status right now (database error).",
            reply_to_message_id=message_id,
            delete_after=CONFIRM_TTL_SECONDS,
        )
        return

    # Убираем свои сообщения: в чате остаётся только сообщение сотрудника.
    _delete_quiet(chat_id, pending.get("prompt_message_id"))

    _send(
        chat_id,
        "\n".join([
            robot_status.status_title(direction, robot),
            f"Reason: {pending['type_problem']}",
            f"Note: {note}",
        ]),
        delete_after=CONFIRM_TTL_SECONDS,
    )

    _notify_lark_status(direction, result, employee.get("user_name"))


def _handle_cancel(chat_id, sender, reply_to):
    pending = clear_pending_status(chat_id, sender.get("id"))

    if pending:
        _delete_quiet(chat_id, pending.get("prompt_message_id"))

    _send(
        chat_id,
        "✖️ Cancelled." if pending else "Nothing to cancel.",
        reply_to_message_id=reply_to,
        delete_after=CONFIRM_TTL_SECONDS,
    )


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
        str(_route_thread(chat.get("id"))) if chat.get("id") is not None else "-",
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
    """
    Статистика смены в Telegram.

    Используется тот же билдер, что и для отчёта в Lark: итоги, простой,
    динамика к прошлой смене, роботы на обслуживание и топы — чтобы вид
    не разъезжался между командой и автоматическим отчётом.
    """
    metrics = shift_metrics(shift_date, shift_name)

    return build_shift_summary(shift_date, shift_name, metrics)


def _handle_stats(chat_id, args, reply_to):
    parts = args.split()

    shift_date = parts[0] if len(parts) > 0 else None
    shift_name = parts[1] if len(parts) > 1 else None

    usage = (
        "Usage: /stats [YYYY-MM-DD] [day|night]\n"
        "Example: /stats 2026-03-08 night"
    )

    if not shift_date and not shift_name:
        shift_date, shift_name = get_current_shift()
    elif not shift_date or not shift_name or shift_name not in ("day", "night"):
        _send(chat_id, usage, reply_to_message_id=reply_to)
        return

    try:
        datetime.strptime(shift_date, "%Y-%m-%d")
    except (TypeError, ValueError):
        _send(
            chat_id,
            f"⚠️ Bad date: {shift_date!r}\n\n{usage}",
            reply_to_message_id=reply_to,
        )
        return

    try:
        text = _stats_text(shift_date, shift_name)
    except Exception:
        logger.exception(
            "Не удалось собрать статистику смены %s/%s",
            shift_date,
            shift_name,
        )
        _send(
            chat_id,
            "⚠️ Can't build the shift stats right now (see logs).",
            reply_to_message_id=reply_to,
            delete_after=CONFIRM_TTL_SECONDS,
        )
        return

    _send(chat_id, text, reply_to_message_id=reply_to)


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

    if command in STATUS_DIRECTIONS:
        _handle_status_command(chat_id, sender, command, args, reply_to)
        return True

    if command == "cancel":
        _handle_cancel(chat_id, sender, reply_to)
        return True

    if command == "id":
        thread_id = _route_thread(chat_id)
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

    if isinstance(saved, dict) and saved.get("robot_missing"):
        # Робота нет в списке склада: в базу писать нечего, но саму ошибку
        # отправляем в Lark как обычную карточку. Факт «робота нет в системе»
        # в Lark не пишем — об этом сообщаем только сотруднику в Telegram.
        pretty = now_warsaw().strftime("%d.%m.%Y %H:%M:%S")

        forward_error(parsed, [
            ("👤 Employee", employee_name),
            ("🤖 Robot", parsed["robot"]),
            ("⚠️ Time", pretty),
            ("📝 Details", parsed["error_text"]),
        ])

        logger.warning(
            "Робот %s не найден в системе — ошибка переслана в Lark "
            "без записи в базу (сотрудник уведомлён в Telegram)",
            parsed["robot"],
        )
        return

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

        _send(
            chat_id,
            f"✅ Saved: robot {parsed['robot']} — "
            f"{truncate(parsed['error_text'], 300)}\n"
            f"📊 Shift issues: {count}{suffix}",
            delete_after=CONFIRM_TTL_SECONDS,
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
            delete_after=CONFIRM_TTL_SECONDS,
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
            delete_after=CONFIRM_TTL_SECONDS,
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
                delete_after=CONFIRM_TTL_SECONDS,
            )
    elif mode == "link":
        _send(
            chat_id,
            "✅ Photo sent to Lark as a link\n"
            "(Lark API quota exceeded — uploaded to Supabase Storage)",
            reply_to_message_id=message_id,
            delete_after=CONFIRM_TTL_SECONDS,
        )
    else:
        _send(
            chat_id,
            "⚠️ Can't forward the photo to Lark right now (see logs).",
            reply_to_message_id=message_id,
            delete_after=CONFIRM_TTL_SECONDS,
        )


def handle_update(update: dict, bot_username: str = None):
    """
    Обрабатывает один апдейт Telegram.

    Метка «обработано» ставится только при успешном завершении: если
    обработка упала, повторная доставка апдейта не будет отброшена как дубль.
    """
    key = _update_key(update)

    _handle_update_inner(update, bot_username)

    _mark_processed(key)


def _handle_update_inner(update: dict, bot_username: str = None):
    """Разбор апдейта без учёта дедупликации."""
    callback = update.get("callback_query")

    if callback:
        try:
            handle_status_callback(callback)
        except Exception:
            logger.exception("Не удалось обработать нажатие кнопки")

        return

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
    key = _update_key(update)

    if key and _is_processed(key):
        logger.info("Апдейт %s уже обработан, пропускаю", key)
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
    _set_route(chat_id, thread_id, chat.get("type"))

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

        if normalized != "cancel":
            interrupted = clear_pending_status(chat_id, sender.get("id"))

            if interrupted:
                # Новая команда прерывает незавершённую смену статуса.
                _delete_quiet(chat_id, interrupted.get("prompt_message_id"))
                logger.info(
                    "Флоу смены статуса прерван командой /%s",
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

    pending = take_pending_status(chat_id, sender.get("id"))

    if pending:
        # Это описание причины для смены статуса, а не сообщение об ошибке.
        logger.info(
            "Описание причины для робота #%s: %r",
            pending.get("robot_number"),
            text[:80],
        )

        finish_status_change(chat_id, sender, text, message_id, pending)
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

# Состояние лиза «единственного опрашивающего»: кто опрашивает Telegram
# и в каком режиме находится этот процесс.
LEASE_HOLDER = None
LEASE_STATUS = "starting"
LEASE_MODE = "unknown"   # acquired / taken-over / no-table / held-by-other

# Когда последний раз успешно вызывали getUpdates (для /health).
_LAST_POLL_AT = 0.0

# Статус таблицы привязок (проверяется один раз на старте, чтобы
# /health не дёргал Supabase на каждый запрос).
USERS_TABLE_OK = None

USERS_TABLE = "telegram_users"


def polling_loop(stop_event: threading.Event = None, lease_holder: str = None):
    """
    Бесконечный long polling Telegram.

    lease_holder — лиз, который нужно продлевать. Потеряли лиз — прекращаем
    опрос: значит его забрал другой инстанс, и продолжать нельзя (будут дубли).
    """
    global LEASE_STATUS

    offset = load_saved_offset()

    if offset is not None:
        logger.info("Продолжаю с сохранённого offset=%s", offset)

    logger.info(
        "Telegram polling started (bot=%s, holder=%s)",
        BOT_USERNAME,
        lease_holder,
    )

    global _LAST_POLL_AT

    while stop_event is None or not stop_event.is_set():
        if lease_holder:
            try:
                state = bot_lease.check(lease_holder)
            except Exception:
                # Проверка лиза не должна убивать поток опроса.
                logger.exception("Ошибка проверки лиза — продолжаю опрос")
                state = "error"

            if state == "lost":
                LEASE_STATUS = "lease-lost"
                logger.error(
                    "Лиз опроса потерян — останавливаю polling, "
                    "чтобы не дублировать сообщения"
                )
                return

            if state == "error":
                # Транзиентный сбой базы: молча замолчать хуже, чем
                # продолжить опрос — лиз при этом не продлевается.
                logger.warning(
                    "Не удалось продлить лиз (база недоступна) — "
                    "продолжаю опрос"
                )

        try:
            # Таймаут меньше TTL лиза: цикл успевает продлить лиз с запасом.
            updates = tg.get_updates(offset=offset, timeout=20)
        except Exception:
            logger.exception("Ошибка getUpdates — пауза и повтор")
            time.sleep(3)
            continue

        _LAST_POLL_AT = time.time()

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
                # Метка не ставится: при повторной доставке обработаем снова.
                logger.exception("Failed to handle update %s", update_id)

        if updates:
            # Фиксируем offset, чтобы после перезапуска Telegram не прислал
            # эти же апдейты повторно.
            save_offset(offset)

        if not updates:
            time.sleep(0.2)


def lease_heartbeat_loop(holder: str):
    """
    Продлевает лиз отдельным потоком.

    В цикле опроса check() вызывается раз за итерацию, а один апдейт с фото
    может занять больше минуты — без отдельного heartbeat лиз успел бы
    протухнуть и его забрал бы standby-сосед (два опрашивающих = дубли).
    """
    while True:
        time.sleep(LEASE_HEARTBEAT_SECONDS)

        try:
            state = bot_lease.check(holder)
        except Exception:
            logger.exception("Ошибка heartbeat лиза — повторю позже")
            continue

        if state == "lost":
            logger.error(
                "Лиз опроса потерян (heartbeat) — цикл опроса остановится сам"
            )
            return


def start_polling(lease_holder: str = None) -> threading.Thread:
    thread = threading.Thread(
        target=polling_loop,
        args=(None, lease_holder),
        name="telegram-polling",
        daemon=True,
    )
    thread.start()

    return thread


def install_shutdown_handler(holder: str):
    """
    На SIGTERM/SIGINT отпускаем лиз, чтобы после деплоя новый контейнер
    начал опрашивать Telegram сразу, а не ждал истечения лиза.
    """
    def handler(signum, _frame):
        logger.info("Сигнал %s — отпускаю лиз опроса и завершаюсь", signum)

        try:
            # Даём текущему апдейту дописаться: резкий выход рвёт запись
            # посередине (например, exceptions записан, exceptions_glpc — нет).
            time.sleep(SHUTDOWN_GRACE_SECONDS)
        except Exception:
            pass

        try:
            bot_lease.release(holder)
        except Exception:
            logger.exception("Не удалось отпустить лиз при завершении")

        os._exit(0)

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)

        if sig is not None:
            signal.signal(sig, handler)


def standby_loop(holder: str, interval: int = None) -> bool:
    """
    Ждём, когда освободится лиз. True — получили его.

    Опрос запускает не эта функция, а poller_supervisor: жизненный цикл
    polling живёт в одном месте и его нельзя случайно запустить дважды.
    """
    global LEASE_STATUS, LEASE_MODE

    interval = STANDBY_RETRY_SECONDS if interval is None else interval

    logger.info("Standby: жду освобождения лиза опроса (%s)", holder)

    while True:
        time.sleep(interval)

        try:
            result = bot_lease.acquire(holder)
        except Exception:
            logger.exception("Ошибка получения лиза — повторю позже")
            LEASE_STATUS = "standby (error)"
            continue

        if result["acquired"]:
            LEASE_MODE = result["status"]
            return True

        LEASE_STATUS = f"standby ({result.get('holder')})"


def poller_supervisor(holder: str, acquired: bool = False):
    """
    Единственный владелец жизненного цикла опроса.

    Держим лиз — опрашиваем Telegram и шлём отчёты; потеряли лиз — уходим в
    standby и ждём его снова. Так инстанс не может «зависнуть» без опроса.
    """
    global LEASE_HOLDER, LEASE_STATUS

    LEASE_HOLDER = holder
    scheduler_started = False

    while True:
        if acquired:
            LEASE_STATUS = "poller"

            if not scheduler_started:
                # Отчёт за смену шлёт только опрашивающий инстанс.
                start_shift_scheduler()
                scheduler_started = True

            threading.Thread(
                target=lease_heartbeat_loop,
                args=(holder,),
                name="lease-heartbeat",
                daemon=True,
            ).start()

            try:
                start_polling(holder).join()
            except Exception:
                logger.exception("Поток опроса упал")

            acquired = False
            LEASE_STATUS = "standby (lease lost)"
            logger.warning("Опрос остановлен (лиз потерян) — перехожу в standby")
            continue

        try:
            got_lease = standby_loop(holder)
        except Exception:
            logger.exception("Ошибка ожидания лиза — повторю через паузу")
            LEASE_STATUS = "standby (error)"
            time.sleep(STANDBY_RETRY_SECONDS)
            continue

        if got_lease:
            acquired = True
            console.print(
                "[bold green]Лиз получен — начинаю опрашивать Telegram[/bold green]"
            )


# ============================================================
# HTTP (Railway healthcheck + статистика)
# ============================================================

@app.route("/health", methods=["GET"])
def health():
    """
    Liveness-сигнал для Railway.

    503 — опрашивающий инстанс завис или потерял лиз: контейнер надо
    перезапустить. Standby (лиз у соседа) — это нормально, отдаём 200.
    """
    now = time.time()
    healthy = True
    reason = "ok"

    if LEASE_STATUS == "poller":
        if _LAST_POLL_AT and (now - _LAST_POLL_AT) > POLL_STALL_SECONDS:
            healthy, reason = False, "poller stalled"
    elif LEASE_STATUS == "lease-lost" or "error" in str(LEASE_STATUS):
        healthy, reason = False, str(LEASE_STATUS)

    payload = {
        "status": "ok" if healthy else "degraded",
        "reason": reason,
        "bot": BOT_USERNAME,
        "users_table": USERS_TABLE_OK,
        "poller": LEASE_STATUS,
        "poller_holder": LEASE_HOLDER,
        "lease": LEASE_MODE,
        "last_poll_seconds_ago": (
            round(now - _LAST_POLL_AT) if _LAST_POLL_AT else None
        ),
        "time": now_warsaw().strftime("%d.%m.%Y %H:%M:%S"),
    }

    return jsonify(payload), (200 if healthy else 503)


@app.route("/shift_stats", methods=["GET"])
def shift_stats_endpoint():
    if STATS_TOKEN and request.args.get("token") != STATS_TOKEN:
        return jsonify({"error": "forbidden"}), 403

    shift_date = request.args.get("date")
    shift_name = request.args.get("shift")

    if not shift_date or not shift_name:
        shift_date, shift_name = get_current_shift()

    try:
        total, by_robot, by_type = shift_stats(shift_date, shift_name)
    except Exception:
        logger.exception("Ошибка /shift_stats для %s/%s", shift_date, shift_name)
        return jsonify({"error": "database error"}), 503

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
        {"command": "offline", "description": "Take a robot out of service"},
        {"command": "online", "description": "Return a robot to service"},
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

    # Старые фото из Telegram не копим на диске.
    start_images_janitor()

    # Удаление служебных подтверждений («хвостов») через CONFIRM_TTL_SECONDS.
    if CONFIRM_TTL_SECONDS > 0:
        start_deletion_worker()
        console.print(
            f"[cyan]Подтверждения удаляются через "
            f"{CONFIRM_TTL_SECONDS}s[/cyan]"
        )

    # Один Telegram — один опрашивающий. Иначе Telegram отдаёт одни и те же
    # сообщения обоим инстансам и они обрабатываются дважды (дубли).
    global LEASE_STATUS, LEASE_MODE
    holder = bot_lease.holder_id()
    lease = bot_lease.acquire(holder)

    LEASE_MODE = lease["status"]
    install_shutdown_handler(holder)

    if lease["acquired"]:
        console.print(
            f"[green]Опрашиваю Telegram (лиз: {lease['status']})[/green]"
        )
    else:
        LEASE_STATUS = f"standby ({lease.get('holder')})"
        console.print(
            f"[bold yellow]Telegram уже опрашивает другой инстанс "
            f"({lease.get('holder')}) — этот процесс в режиме standby, "
            f"сообщения он не обрабатывает.[/bold yellow]"
        )

    # Жизненный цикл опроса: держим лиз — опрашиваем, потеряли — снова ждём.
    threading.Thread(
        target=poller_supervisor,
        args=(holder, lease["acquired"]),
        name="poller-supervisor",
        daemon=True,
    ).start()

    port = int(os.environ.get("PORT", 7777))

    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
