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

import contextlib
import difflib
import os
import re
import signal
import threading
import time
from collections import OrderedDict
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, redirect, request
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

import telegram_api as tg
from error_parser import parse_error_message
from logging_config import setup_logging
import analytics
import bot_lease
import digests
import robot_card
from env_utils import env_bool, env_int
from robot_queue import start_queue_autoclose
import lark_hooks
from analytics import start_weekly_scheduler
from warehouses import (
    DEFAULT_WAREHOUSE,
    WAREHOUSES,
    warehouse_from_args,
    warehouse_key,
)
from digests import start_digest_scheduler
from lark_media import hook_ok, send_card_via_hook, send_text_via_hook
from pending_photos import (
    TARGET_HOOK_URL,
    forward_error,
    handle_incoming_photo,
    send_error_with_photo,
    send_photo,
)
from supabase_storage import (
    StorageUnavailable,
    download_json,
    resolve_photo_url,
    upload_json,
    upload_photo_and_get_url,
)
import robot_status
from sendToDataBase import (
    WAREHOUSE,
    count_robot_errors_in_shift,
    queue_missing_robot,
    set_exception_photo,
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
    StoreUnavailable,
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
    return env_int(name, default)


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
    """Читает булево из окружения."""
    return env_bool(name, default)


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

# ============================================================
# ТОПИКИ ФОРУМА ПО НАЗНАЧЕНИЮ
# ============================================================
#
# Один топик — один смысл. В топике ошибок («Ex GLPC») не должно быть ничего,
# кроме ошибок: статусы роботов, статистика и служебные ответы уходят в свои
# топики.
#
# Значение переменной — id топика (надёжно; узнать: /id в этом топике) или
# его имя (сработает, если бот видел сервисное сообщение о создании топика).
TOPIC_RAW = {
    "error": os.environ.get("TELEGRAM_TOPIC_ID", "").strip(),
    "status": os.environ.get("TELEGRAM_TOPIC_STATUS", "").strip(),
    "stats": os.environ.get("TELEGRAM_TOPIC_STATS", "").strip(),
    "service": os.environ.get("TELEGRAM_TOPIC_SERVICE", "").strip(),
}

# Топик ошибок для каждого склада: TELEGRAM_TOPIC_ERROR_GLPC / _SP3.
# Для склада по умолчанию работает и историческая TELEGRAM_TOPIC_ID.
for _key, _title in WAREHOUSES.items():
    _raw = os.environ.get(f"TELEGRAM_TOPIC_ERROR_{_key.upper()}", "").strip()

    if _raw:
        TOPIC_RAW[f"error:{_title}"] = _raw

TOPIC_KINDS = ("error", "status", "stats", "service")

TOPIC_TITLES = {
    "error": "Ex GLPC (errors)",
    "status": "Robot status",
    "stats": "Stats",
    "service": "Service",
}

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

# Сколько ждать первого опроса, прежде чем считать /health нездоровым.
STARTUP_GRACE_SECONDS = _env_int("STARTUP_GRACE_SECONDS", 60)

# Момент импорта модуля: нужен, чтобы отличить «ещё стартую» от «завис».
_STARTED_AT = time.time()

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

# Сколько раз пытаться обработать один апдейт, прежде чем пропустить его
# (защита от «отравленного» сообщения, которое всегда падает).
MAX_UPDATE_ATTEMPTS = _env_int("MAX_UPDATE_ATTEMPTS", 3)

# К ошибке какой давности можно прикрепить присланное фото (секунды).
PHOTO_ATTACH_WINDOW = _env_int("PHOTO_ATTACH_WINDOW", 600)

# Сколько держим фото, ожидая текст ошибки, прежде чем переслать отдельно.
PHOTO_HOLD_SECONDS = _env_int("PHOTO_HOLD_SECONDS", 90)

# Объединять ли фото с записью об ошибке (привязка, ожидание текста,
# photo_url). По умолчанию выключено: фото просто уходит в группу.
PHOTO_ATTACH_ENABLED = _env_bool("PHOTO_ATTACH_ENABLED", False)

# Удалять ли сообщения сотрудников: команды боту и описание причины
# при смене статуса (бот — админ группы, права позволяют).
DELETE_USER_MESSAGES = _env_bool("DELETE_USER_MESSAGES", True)

# Подсказывать ли исправление номера, когда робота нет в справочнике.
# Опечатки («3882» вместо «882») — частая причина «робота нет в системе»:
# сначала предлагаем похожие номера, а очередь/Lark — после ответа.
ROBOT_FIX_SUGGEST = _env_bool("ROBOT_FIX_SUGGEST", True)

# Сколько ждать ответа сотрудника, прежде чем дослать номер как есть.
ROBOT_FIX_TTL_SECONDS = _env_int("ROBOT_FIX_TTL", 180)

# Эти команды отвечают даже в чате не из белого списка: иначе после
# включения TELEGRAM_ALLOWED_CHAT_IDS нельзя было бы узнать chat_id через /id.
BOOTSTRAP_COMMANDS = ("id", "help", "start")

# Все поддерживаемые команды (для подсказок «может, вы имели в виду…»).
KNOWN_COMMANDS = (
    "reg", "unreg", "whoami", "stats", "robot", "digest", "top",
    "downtime", "week", "topics", "id", "help", "start", "offline",
    "online", "cancel",
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

# Общий предел для словарей-кэшей, которые наполняются из интернета
# (имена топиков, последние ошибки, «уже подсказали»): без него память
# процесса растёт бесконечно.
_CACHE_LIMIT = 500


def _remember_bounded(store: dict, key, value, limit: int = _CACHE_LIMIT) -> None:
    """Пишет в словарь-кэш, вытесняя самые старые записи."""
    store.pop(key, None)
    store[key] = value

    while len(store) > limit:
        store.pop(next(iter(store)))


def _remember_flag(store: set, item, limit: int = _CACHE_LIMIT) -> None:
    """Добавляет элемент в set-кэш, вытесняя произвольные старые."""
    store.add(item)

    while len(store) > limit:
        store.discard(next(iter(store)))


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
    "/robot <number> — robot card: status, downtime, history\n"
    "/digest — maintenance digest (stale offline, add-robot queue)\n"
    "/top [day|week|month] — top issues and robots\n"
    "/downtime [days] — longest total downtime and MTTR\n"
    "/week — weekly report preview\n"
    "/topics — which topic is used for what\n"
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

DB_UNAVAILABLE_HINT = (
    "⚠️ Can't read the database right now. Please try again in a minute."
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


def _update_text(update: dict) -> str:
    """Текст апдейта — для логов, когда обработка падает."""
    message = update.get("message") or update.get("edited_message") or {}

    return message.get("text") or message.get("caption") or ""


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
_topic_seen = {}       # (chat_id, thread_id) -> name или None (бот видел тут сообщения)
_routes = {}           # chat_id -> thread_id последнего сообщения
_hinted_threads = set()
_chat_types = {}       # chat_id -> тип чата (supergroup/private/...)

# Общие словари пишутся из потока опроса и читаются из Flask/нотификатора.
_routes_lock = threading.Lock()


_warehouse_ctx = threading.local()


def current_warehouse() -> str:
    """
    Склад текущего сообщения.

    Ставится из топика: топик ошибок GLP-C -> GLP-C, топик ошибок SP3 ->
    SMALL-P3. В общих топиках (статусы/статистика/служебный) — склад по
    умолчанию; команды могут указать склад аргументом (/stats sp3).
    """
    return getattr(_warehouse_ctx, "value", None) or DEFAULT_WAREHOUSE


def set_current_warehouse(title: str):
    _warehouse_ctx.value = title or DEFAULT_WAREHOUSE


def _chat_key(chat_id) -> int:
    return int(chat_id)


def _set_route(chat_id, thread_id, chat_type: str = None):
    """Запоминаем, в какой топик отвечать и что это за чат."""
    key = _chat_key(chat_id)

    with _routes_lock:
        _routes[key] = thread_id

        if chat_type:
            _chat_types[key] = chat_type

        if thread_id is not None and len(_topic_seen) < _CACHE_LIMIT:
            observed = (key, int(thread_id))

            if observed not in _topic_seen:
                # Топик, в котором бот видел хоть одно сообщение: из этого
                # списка видно, какие топики уже существуют и какие id у них.
                _topic_seen[observed] = _topic_names.get(observed)


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
        _remember_bounded(_topic_names, key, name)

        if key in _topic_seen:
            _topic_seen[key] = name
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


def _topic_by_name(name, chat_id=None):
    """thread_id топика по имени (из сервисных сообщений форума)."""
    wanted = str(name or "").strip().casefold()

    if not wanted:
        return None

    for (key_chat, thread_id), known in list(_topic_names.items()):
        if chat_id is not None and int(key_chat) != int(chat_id):
            continue

        if (known or "").strip().casefold() == wanted:
            return thread_id

    return None


def error_topic(warehouse: str = None, chat_id=None):
    """thread_id топика ошибок указанного склада."""
    title = warehouse or current_warehouse()

    raw = TOPIC_RAW.get(f"error:{title}", "")

    if not raw and title == DEFAULT_WAREHOUSE:
        if TELEGRAM_TOPIC_ID is not None:
            return TELEGRAM_TOPIC_ID

        return _topic_by_name(TELEGRAM_TOPIC_NAME, chat_id)

    if not raw:
        return None

    digits = raw.lstrip("-")

    if digits.isdigit():
        return int(raw)

    return _topic_by_name(raw, chat_id)


def error_topic_map(chat_id=None) -> dict:
    """Склад -> thread_id его топика ошибок."""
    return {
        title: error_topic(title, chat_id)
        for title in WAREHOUSES.values()
    }


def warehouse_for_thread(chat_id, thread_id) -> str:
    """Склад по топику: в топике ошибок SP3 пишут ошибки SP3."""
    if thread_id is None:
        return DEFAULT_WAREHOUSE

    for title in WAREHOUSES.values():
        configured = error_topic(title, chat_id)

        if configured is not None and int(configured) == int(thread_id):
            return title

    return DEFAULT_WAREHOUSE


def topic_thread(kind: str, chat_id=None):
    """
    Настроенный thread_id топика для вида сообщений.

    None — топик не настроен (или имя ещё не выучено). Это не ошибка:
    вызывающий код отвечает в топик-источник, как раньше.
    """
    kind = str(kind or "").strip().lower()

    if kind == "error":
        return error_topic(current_warehouse(), chat_id)

    if kind not in TOPIC_KINDS:
        return None

    raw = TOPIC_RAW.get(kind, "")

    if not raw:
        return None

    digits = raw.lstrip("-")

    if digits.isdigit():
        return int(raw)

    return _topic_by_name(raw, chat_id)


def topic_map(chat_id=None) -> dict:
    return {kind: topic_thread(kind, chat_id) for kind in TOPIC_KINDS}


def topic_title(kind: str, chat_id=None) -> str:
    """«Ex GLPC (errors)» или «Ex GLPC (errors) #2» — для подсказок и логов."""
    thread_id = topic_thread(kind, chat_id)
    name = topic_name(chat_id, thread_id) if thread_id else None

    if name:
        return f"{name!r}"

    return TOPIC_TITLES.get(kind, kind)


_origin_thread = threading.local()


def set_origin_thread(chat_id, thread_id):
    """
    Запоминает топик, из которого пришло текущее сообщение.

    Ответы уходят ТОЛЬКО туда: бот не отвечает в других топиках и ничего по
    ним не раскидывает.
    """
    _origin_thread.value = (int(chat_id), thread_id)


_reply_override = threading.local()


@contextlib.contextmanager
def reply_thread(thread_id):
    """
    Контекст: сообщения блока уходят в указанный топик.

    Нужен фоновым досылам (фото, подсказка по номеру): они работают вне
    апдейта, но обязаны ответить в тот топик, откуда пришло сообщение.
    """
    previous = getattr(_reply_override, "value", None)
    _reply_override.value = thread_id

    try:
        yield thread_id
    finally:
        _reply_override.value = previous


def _reply_thread(chat_id, thread_id=None):
    """Топик для ответа: явно заданный, топик текущего сообщения или последний."""
    if thread_id is not None:
        return thread_id

    override = getattr(_reply_override, "value", None)

    if override is not None:
        return override

    origin = getattr(_origin_thread, "value", None)

    if origin and origin[0] == int(chat_id) and origin[1] is not None:
        return origin[1]

    return _route_thread(chat_id)


def is_shared_topic(chat_id, thread_id) -> bool:
    """Общий топик (статусы/статистика/служебный), а не топик ошибок."""
    if thread_id is None:
        return False

    for kind in ("status", "stats", "service"):
        configured = topic_thread(kind, chat_id)

        if configured is not None and int(configured) == int(thread_id):
            return True

    return False


def monitored_topic_label() -> str:
    """Человекочитаемое описание топиков ошибок (по складам)."""
    parts = []

    for title, thread_id in error_topic_map().items():
        if thread_id is not None:
            parts.append(f"{title}: topic id {thread_id}")

    if parts:
        return " · ".join(parts)

    if TELEGRAM_TOPIC_NAME:
        return f"topic {TELEGRAM_TOPIC_NAME!r}"

    return "any topic"


def topic_allowed(chat_id, thread_id):
    """
    Можно ли брать сообщение из этого топика.

    Возвращает (allowed, reason).
    """
    configured_errors = error_topic_map(chat_id)

    if (
        TELEGRAM_TOPIC_ID is None
        and not TELEGRAM_TOPIC_NAME
        and not any(configured_errors.values())
    ):
        return True, "no-filter"

    if thread_id is None:
        # Сообщение вне топиков — это «General».
        return False, "general-topic"

    for title, configured in configured_errors.items():
        if configured is not None and int(thread_id) == int(configured):
            return True, f"errors:{title}"

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
    Отправка с автоопределением топика: ответ уходит только в тот топик,
    откуда пришло сообщение (для форум-групп).

    Если в этот топик отправить нельзя (например, General закрыт —
    Telegram отвечает TOPIC_CLOSED), повторяем в отслеживаемый топик,
    чтобы пользователь всё-таки увидел ответ.

    delete_after — через сколько секунд удалить это сообщение (0/None — не удалять).
    """
    if thread_id is None:
        thread_id = _reply_thread(chat_id)

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
                reply_to_message_id=reply_to_message_id,
                disable_notification=disable_notification,
                message_thread_id=fallback,
                # Без этого кнопки выбора причины терялись и флоу
                # /offline и /online становился непроходимым.
                reply_markup=reply_markup,
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
            flush_expired_photos()
            flush_expired_robot_fixes()
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

    if not isinstance(data, dict):
        if data is not None:
            logger.warning("Сохранённый offset не объект: %r", data)

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


def _handle_wrong_topic(chat_id, thread_id, reason, error_attempt: bool = False):
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

    _remember_flag(_hinted_threads, key)

    if reason == "name-unknown":
        text = (
            "⚠️ I can't identify this topic yet, so messages from it are "
            "ignored.\n"
            "Send /id here and set TELEGRAM_TOPIC_ID=<message_thread_id> "
            "in .env — after that the topic is matched reliably."
        )
    elif error_attempt:
        text = (
            "⚠️ Robot errors are accepted only in "
            f"{monitored_topic_label()}.\n"
            "Send the error there — I won't save it from this topic, "
            "otherwise it would go to the wrong warehouse."
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


def _delete_user_message(chat_id, message_id) -> bool:
    """
    Убирает сообщение сотрудника (команду или описание причины).

    Сообщения об ошибках не трогаем: их видит смена, и они источник записи.
    """
    if not DELETE_USER_MESSAGES or message_id is None:
        return False

    chat_type = _route_chat_type(chat_id)

    if chat_type and chat_type not in ("group", "supergroup"):
        # В личной переписке чужие сообщения не удаляем: команда deleteMessage
        # там работает иначе, а смысла «чистить» личку нет.
        return False

    deleted = tg.delete_message(chat_id, message_id)

    if deleted:
        logger.info("Сообщение сотрудника %s удалено", message_id)

    return deleted


def status_usage(direction: str) -> str:
    keys = "|".join(WAREHOUSES)

    return (
        f"Usage: /{direction} [{keys}] <robot number>\n"
        f"Example: /{direction} 3680"
    )


def find_robot_in_warehouse(number, warehouse: str = None, strict: bool = True):
    """
    Робот ТОЛЬКО своего склада: чужой склад не подставляем.

    У каждого робота в `robots_maintenance_list` есть `warehouse`, и номера
    между складами повторяются (например #123 есть и в GLP-C, и в P3-DC-1).
    Поэтому поиск «по всем складам» мог взять чужого робота и переключить
    статус не той машине. Склад берётся из топика или из аргумента команды.

    Возвращает (robot, warehouse_title). (None, title) — робота нет на этом
    складе. StatusUnavailable — база недоступна (это не «робота нет»).
    """
    title = warehouse or current_warehouse()
    robot = robot_status.find_robot(number, warehouse=title, strict=strict)

    return robot, title


def find_robot_matches(number, strict: bool = True):
    """
    Все склады, где есть робот с таким номером.

    Каждый склад проверяется отдельно — чужой робот не подставляется, просто
    возвращаются все совпадения: [(склад, робот), ...]. Если номер есть на
    двух складах, вызывающий код спрашивает сотрудника кнопками.

    StatusUnavailable — база недоступна (это не «робота нет»).
    """
    matches = []

    for title in WAREHOUSES.values():
        robot = robot_status.find_robot(number, warehouse=title, strict=strict)

        if robot:
            matches.append((title, robot))

    return matches


def warehouse_keyboard(action: str, number, titles):
    """Кнопки выбора склада: номер есть и там, и там."""
    rows = [
        [{
            "text": f"🏭 {title}",
            "callback_data": f"w:{action}:{warehouse_key(title)}:{number}",
        }]
        for title in titles
    ]

    rows.append([{"text": "✖️ Cancel", "callback_data": "w:cancel:-:0"}])

    return {"inline_keyboard": rows}


def _ask_warehouse(chat_id, action: str, number, titles, reply_to=None):
    """Спрашивает склад, если номер есть на нескольких складах."""
    _send(
        chat_id,
        f"🤖 Robot {number} is in {len(titles)} warehouses: "
        f"{', '.join(titles)}.\nWhich one?",
        reply_to_message_id=reply_to,
        reply_markup=warehouse_keyboard(action, number, titles),
    )


def _show_robot_card(chat_id, number, warehouse, reply_to=None) -> bool:
    """Карточка робота конкретного склада. False — база недоступна."""
    card = robot_card.robot_card(number, warehouse=warehouse)

    if card is None:
        _send(
            chat_id,
            DB_UNAVAILABLE_HINT,
            reply_to_message_id=reply_to,
            delete_after=CONFIRM_TTL_SECONDS,
        )
        return False

    text = robot_card.format_robot_card(card)

    if not card.get("found"):
        text += _warehouse_hint("robot", warehouse)

    _send(chat_id, text, reply_to_message_id=reply_to)

    return True


def _warehouse_hint(command: str, title: str) -> str:
    """Подсказка, как повторить команду для другого склада."""
    others = [key for key, name in WAREHOUSES.items() if name != title]

    if not others:
        return ""

    return (
        f"\nIf the robot belongs to another warehouse, send "
        f"/{command} {others[0]} <number>."
    )


def _handle_status_command(chat_id, sender, direction, args, message_id):
    """/offline или /online: показываем робота и кнопки причин."""
    explicit_warehouse, args = warehouse_from_args(args)
    warehouse = explicit_warehouse or current_warehouse()

    employee, db_error = _lookup_employee(chat_id, sender, message_id)

    if db_error:
        return

    if not employee:
        _send(chat_id, NOT_REGISTERED_HINT, reply_to_message_id=message_id)
        return

    if not args:
        _send(chat_id, status_usage(direction))
        return

    robot_number = args.split()[0]

    try:
        if explicit_warehouse:
            robot, robot_warehouse = find_robot_in_warehouse(
                robot_number, warehouse
            )
            matches = [(robot_warehouse, robot)] if robot else []
        else:
            matches = find_robot_matches(robot_number)
    except robot_status.StatusUnavailable:
        _send(
            chat_id,
            DB_UNAVAILABLE_HINT,
            delete_after=CONFIRM_TTL_SECONDS,
        )
        return

    if not matches:
        _send(
            chat_id,
            f"⚠️ Robot {robot_number} not found in "
            f"{' or '.join(WAREHOUSES.values())}."
            + _warehouse_hint(direction, warehouse),
        )
        return

    if len(matches) > 1:
        # Номер есть на нескольких складах: выбирает сотрудник, а не бот.
        _ask_warehouse(
            chat_id, direction, robot_number, [title for title, _ in matches]
        )
        return

    robot_warehouse, robot = matches[0]
    spec = robot_status.DIRECTIONS[direction]

    if robot_status.is_in_status(robot, direction):
        other = "online" if direction == "offline" else "offline"

        _send(
            chat_id,
            f"ℹ️ Robot {robot.get('robot_number')} is already "
            f"{robot.get('status')}.\n"
            f"Use /{other} {robot.get('robot_number')} if that is wrong.",
        )
        return

    start_status_flow(chat_id, direction, robot, robot_warehouse)


def start_status_flow(chat_id, direction: str, robot: dict, warehouse: str = None):
    """Показывает робота и кнопки причин (команду сотрудника бот удалит)."""
    spec = robot_status.DIRECTIONS[direction]

    _send(
        chat_id,
        f"{spec['emoji']} Robot {robot.get('robot_number')} · "
        f"{robot.get('robot_type') or '-'} · "
        f"{robot.get('warehouse') or warehouse or WAREHOUSE}\n"
        f"Status: {robot.get('status')} → {spec['new_status']}\n"
        "\n"
        "Choose the reason:",
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
        if callback_id:
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

    if not allowed and not is_shared_topic(chat_id, thread_id):
        tg.answer_callback_query(callback_id, "This topic is not monitored")
        return

    parts = (callback.get("data") or "").split(":")

    if parts and parts[0] == "rf":
        _handle_robot_fix_callback(chat_id, sender, parts, message_id, callback_id)
        return

    if parts and parts[0] == "w":
        _handle_warehouse_choice(chat_id, sender, parts, message_id, callback_id)
        return

    if len(parts) != 4 or parts[0] != "st":
        if callback_id:
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

    try:
        employee = get_employee(sender.get("id"), strict=True)
    except StoreUnavailable:
        tg.answer_callback_query(
            callback_id,
            "Database unavailable — try again in a minute",
        )
        return

    if not employee:
        tg.answer_callback_query(callback_id, "Register first: /reg <Your Name>")
        return

    label = robot_status.reason_label(direction, code)

    if not label:
        tg.answer_callback_query(callback_id, "Unknown reason")
        return

    try:
        robot = robot_status.find_robot_by_id(robot_id, strict=True)
    except robot_status.StatusUnavailable:
        tg.answer_callback_query(
            callback_id,
            "Database unavailable — try again in a minute",
        )
        return

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
        # Склад робота: описание причины может прийти уже в общем топике,
        # где склад по умолчанию другой.
        "warehouse": robot.get("warehouse") or current_warehouse(),
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
    """Карточка о смене статуса в Lark-группе склада (с откатом на текст)."""
    robot = result.get("robot") or {}
    target = lark_hooks.status_hook(
        robot.get("warehouse") or current_warehouse()
    )

    card = robot_status.build_status_card(direction, result, employee_name)
    hook_result = send_card_via_hook(target, card)

    if hook_ok(hook_result):
        logger.info(
            "Статус робота #%s отправлен в Lark карточкой",
            result["robot"].get("robot_number"),
        )
        return True

    logger.warning("Карточка статуса не прошла (%s) — отправляю текстом", hook_result)

    send_text_via_hook(
        target,
        robot_status.build_status_text(direction, result, employee_name),
    )

    return False


def finish_status_change(chat_id, sender, note, message_id, pending: dict) -> bool:
    """
    Сотрудник описал причину: меняем статус и убираем свои сообщения.

    True — статус изменён, сообщение сотрудника можно удалять. False — ничего
    не изменилось (база недоступна / ошибка записи / робот не найден):
    сообщение с причиной остаётся в чате, чтобы сотрудник не потерял текст.
    """
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
        return False

    employee = get_employee(sender.get("id"))

    if not employee:
        clear_pending_status(chat_id, sender.get("id"))
        _send(chat_id, NOT_REGISTERED_HINT, reply_to_message_id=message_id)
        return False

    try:
        robot, _robot_warehouse = find_robot_in_warehouse(
            pending["robot_number"],
            pending.get("warehouse"),
        )
    except robot_status.StatusUnavailable:
        # База недоступна: возвращаем флоу, чтобы сотрудник просто повторил
        # сообщение с причиной, и не теряем его текст.
        set_pending_status(chat_id, sender.get("id"), pending)

        _send(
            chat_id,
            DB_UNAVAILABLE_HINT,
            reply_to_message_id=message_id,
            delete_after=CONFIRM_TTL_SECONDS,
        )
        return False

    if not robot:
        clear_pending_status(chat_id, sender.get("id"))
        _send(
            chat_id,
            f"⚠️ Robot {pending['robot_number']} not found in "
            f"{pending.get('warehouse') or current_warehouse()}."
            + _warehouse_hint(
                pending["direction"],
                pending.get("warehouse") or current_warehouse(),
            ),
            reply_to_message_id=message_id,
        )
        return False

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
        return False

    result = robot_status.change_robot_status(
        robot,
        direction,
        pending["type_problem"],
        note,
        employee,
    )

    if not result:
        # Ничего не изменилось: возвращаем флоу, чтобы сотрудник просто
        # повторил сообщение с причиной (его текст остаётся в чате).
        set_pending_status(chat_id, sender.get("id"), pending)

        _send(
            chat_id,
            "⚠️ Can't change the robot status right now (database error).\n"
            "Send the reason again in a moment.",
            reply_to_message_id=message_id,
            delete_after=CONFIRM_TTL_SECONDS,
        )
        return False

    clear_pending_status(chat_id, sender.get("id"))

    # Убираем свои сообщения: в чате остаётся только сообщение сотрудника.
    _delete_quiet(chat_id, pending.get("prompt_message_id"))

    # Информация о смене статуса остаётся в чате — её видят все.
    _send(
        chat_id,
        "\n".join([
            robot_status.status_title(direction, robot),
            f"Reason: {pending['type_problem']}",
            f"Note: {note}",
        ]),
    )

    _notify_lark_status(direction, result, employee.get("user_name"))

    return True


def _handle_cancel(chat_id, sender, reply_to):
    pending = clear_pending_status(chat_id, sender.get("id"))

    if pending:
        _delete_quiet(chat_id, pending.get("prompt_message_id"))

    waiting_photo = (
        take_pending_photo(chat_id, sender.get("id"))
        if PHOTO_ATTACH_ENABLED
        else None
    )

    if waiting_photo:
        # Фото ждало текст ошибки, но его отменили — пересылаем как есть.
        flush_pending_photo(chat_id, sender.get("id"), waiting_photo)
        return

    _send(
        chat_id,
        "✖️ Cancelled." if pending else "Nothing to cancel.",
        reply_to_message_id=reply_to,
        delete_after=CONFIRM_TTL_SECONDS,
    )


# ============================================================
# ФОТО И ЗАПИСЬ ОБ ОШИБКЕ
# ============================================================
#
# Сотрудник присылает текст ошибки и фото — в любом порядке. Бот собирает
# это в одну запись: фото уходит в Supabase Storage, ссылка пишется в запись,
# а в Lark уходит одна карточка (картинкой или ссылкой).

_pending_photo = {}   # (chat_id, user_id) -> данные фото, ждущего текст
_last_error = {}      # (chat_id, user_id) -> последняя запись сотрудника
_photo_lock = threading.Lock()


def _photo_key(chat_id, user_id):
    return (int(chat_id), int(user_id))


def put_pending_photo(
    chat_id,
    user_id,
    path,
    message_id,
    caption=None,
    warehouse=None,
    thread_id=None,
):
    """Запоминаем фото, к которому ещё может прийти текст ошибки."""
    with _photo_lock:
        _pending_photo[_photo_key(chat_id, user_id)] = {
            "path": path,
            "message_id": message_id,
            "caption": caption or "",
            # Склад и топик нужны уборщику: он пересылает фото вне апдейта,
            # когда контекста уже нет.
            "warehouse": warehouse or current_warehouse(),
            "thread_id": _reply_thread(chat_id, thread_id),
            "expires": time.time() + PHOTO_HOLD_SECONDS,
        }


def take_pending_photo(chat_id, user_id):
    """Забираем ожидающее фото (если оно ещё актуально)."""
    key = _photo_key(chat_id, user_id)

    with _photo_lock:
        data = _pending_photo.pop(key, None)

    if not data:
        return None

    if time.time() - (data["expires"] - PHOTO_HOLD_SECONDS) > PHOTO_HOLD_SECONDS * 4:
        return None

    return data


def expired_pending_photos():
    """Фото, для которых текст ошибки так и не пришёл."""
    now = time.time()
    due = []

    with _photo_lock:
        for key, data in list(_pending_photo.items()):
            if data.get("expires", 0) <= now:
                due.append((key, _pending_photo.pop(key)))

    return due


def remember_last_error(chat_id, user_id, saved, parsed, table_lines):
    """Запоминаем последнюю запись: к ней прикрепится фото, если придёт позже."""
    if not isinstance(saved, dict):
        return

    glpc_id = saved.get("glpc_id")

    if not glpc_id:
        return

    with _photo_lock:
        _remember_bounded(
            _last_error,
            _photo_key(chat_id, user_id),
            {
                "glpc_id": glpc_id,
                "parsed": parsed,
                "table_lines": table_lines,
                "at": time.time(),
            },
        )


def recent_last_error(chat_id, user_id):
    """Последняя запись сотрудника, если она ещё не «остыла»."""
    key = _photo_key(chat_id, user_id)

    with _photo_lock:
        data = _last_error.get(key)

    if not data:
        return None

    if time.time() - data.get("at", 0) > PHOTO_ATTACH_WINDOW:
        return None

    return data


def store_photo_for_record(saved, photo_path):
    """Кладём фото в Storage и прописываем ссылку в запись. Возвращает URL."""
    url = upload_photo_and_get_url(
        photo_path,
        object_name=os.path.basename(photo_path),
    )

    if not url:
        logger.error("Не удалось сохранить фото для записи: %s", photo_path)
        return None

    if saved.get("glpc_id"):
        set_exception_photo("exceptions_glpc", saved["glpc_id"], url)

    if saved.get("exception_id"):
        set_exception_photo("exceptions", saved["exception_id"], url)

    return url


def flush_pending_photo(chat_id, user_id, data):
    """Фото осталось без текста ошибки — пересылаем его отдельно."""
    return _flush_pending_photo_inner(chat_id, user_id, data)


def _flush_pending_photo_inner(chat_id, user_id, data):
    with reply_thread(data.get("thread_id")):
        return _flush_pending_photo_locked(chat_id, user_id, data)


def _flush_pending_photo_locked(chat_id, user_id, data):
    employee_name = get_employee_name(user_id)

    if employee_name:
        caption = f"📷 Photo from {employee_name}"
    else:
        caption = "📷 Photo from Telegram"

    if data.get("caption"):
        caption = f"{caption}\n{data['caption']}"

    mode = send_photo(
        data["path"],
        caption,
        console,
        warehouse=data.get("warehouse"),
    )

    logger.info(
        "Фото переслано отдельно (не нашлось текста ошибки): chat=%s user=%s mode=%s",
        chat_id,
        user_id,
        mode,
    )

    if mode["mode"] == "none":
        _send(
            chat_id,
            "⚠️ Can't forward the photo to Lark right now (see logs).",
            delete_after=CONFIRM_TTL_SECONDS,
        )
    elif not SEND_CONFIRMATION:
        pass
    elif mode["mode"] == "lark":
        _send(
            chat_id,
            "✅ Photo forwarded to Lark",
            delete_after=CONFIRM_TTL_SECONDS,
        )
    else:
        _send(
            chat_id,
            "✅ Photo sent to Lark as a link\n"
            "(Lark API quota exceeded — uploaded to Supabase Storage)",
            delete_after=CONFIRM_TTL_SECONDS,
        )


def flush_expired_photos():
    """Периодическая задача: отдаём фото, которые так и не дождались текста."""
    if not PHOTO_ATTACH_ENABLED:
        return

    for (chat_id, user_id), data in expired_pending_photos():
        try:
            flush_pending_photo(chat_id, user_id, data)
        except Exception:
            logger.exception("Не удалось переслать отложенное фото")


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


def _lookup_employee_name(chat_id, sender, message_id):
    """
    Имя сотрудника для Telegram-аккаунта.

    Возвращает (имя|None, ошибка_базы). При ошибке ответ уже отправлен:
    «вы не зарегистрированы» на сетевом сбое — неверный и путающий ответ.
    """
    try:
        return get_employee_name(sender.get("id"), strict=True), False
    except StoreUnavailable:
        _send(
            chat_id,
            DB_UNAVAILABLE_HINT,
            reply_to_message_id=message_id,
            delete_after=CONFIRM_TTL_SECONDS,
        )
        return None, True


def _lookup_employee(chat_id, sender, message_id):
    """Строка сотрудника (с card_id) или (None, True) при сбое базы."""
    try:
        return get_employee(sender.get("id"), strict=True), False
    except StoreUnavailable:
        _send(
            chat_id,
            DB_UNAVAILABLE_HINT,
            reply_to_message_id=message_id,
            delete_after=CONFIRM_TTL_SECONDS,
        )
        return None, True


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

    try:
        employee_name, suggestions = resolve_employee_name(args, strict=True)
    except StoreUnavailable:
        _send(
            chat_id,
            DB_UNAVAILABLE_HINT,
            reply_to_message_id=reply_to,
            delete_after=CONFIRM_TTL_SECONDS,
        )
        return

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


def _stats_text(shift_date: str, shift_name: str, warehouse: str = None) -> str:
    """
    Статистика смены в Telegram.

    Используется тот же билдер, что и для отчёта в Lark: итоги, простой,
    динамика к прошлой смене, роботы на обслуживание и топы — чтобы вид
    не разъезжался между командой и автоматическим отчётом.
    """
    metrics = shift_metrics(shift_date, shift_name, warehouse)

    return build_shift_summary(shift_date, shift_name, metrics, warehouse)


def _handle_stats(chat_id, args, reply_to):
    warehouse, args = warehouse_from_args(args)
    warehouse = warehouse or current_warehouse()

    parts = args.split()

    shift_date = parts[0] if len(parts) > 0 else None
    shift_name = parts[1] if len(parts) > 1 else None

    keys = "|".join(WAREHOUSES)

    usage = (
        f"Usage: /stats [{keys}] [YYYY-MM-DD] [day|night]\n"
        f"Example: /stats {keys.split('|')[0]} 2026-03-08 night"
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
        text = _stats_text(shift_date, shift_name, warehouse)
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
    return _handle_command_inner(chat_id, sender, command, args, reply_to, chat)


def _handle_command_inner(chat_id, sender, command, args, reply_to, chat=None) -> bool:
    """Тело обработки команд (внутри выбранного вида топика)."""
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

    if command == "robot":
        _handle_robot(chat_id, args, reply_to)
        return True

    if command == "digest":
        _handle_digest(chat_id, reply_to)
        return True

    if command == "topics":
        _handle_topics(chat_id, reply_to)
        return True

    if command == "top":
        _handle_top(chat_id, args, reply_to)
        return True

    if command == "downtime":
        _handle_downtime(chat_id, args, reply_to)
        return True

    if command == "week":
        _handle_week(chat_id, reply_to)
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


# ============================================================
# КАРТОЧКА РОБОТА И ИСПРАВЛЕНИЕ НОМЕРА
# ============================================================
#
# Сотрудники регулярно присылают номер с опечаткой («3882» вместо «882»).
# Раньше ошибка уходила в Lark, номер падал в robots_to_add, и запись в базу
# не появлялась. Теперь бот сначала показывает похожие номера и ждёт ответа
# (ROBOT_FIX_TTL_SECONDS): исправили — сохраняем как обычно; не ответили или
# нажали «номер верный» — ведём себя как раньше (очередь + Lark).

_pending_robot_fix = {}
_pending_robot_fix_lock = threading.Lock()


def _robot_fix_key(chat_id, user_id):
    return (int(chat_id), int(user_id))


def set_pending_robot_fix(chat_id, user_id, data: dict):
    with _pending_robot_fix_lock:
        _pending_robot_fix[_robot_fix_key(chat_id, user_id)] = dict(
            data,
            expires=time.time() + ROBOT_FIX_TTL_SECONDS,
        )


def take_pending_robot_fix(chat_id, user_id):
    """Забирает ожидающую подсказку по номеру (single-flight)."""
    key = _robot_fix_key(chat_id, user_id)

    with _pending_robot_fix_lock:
        data = _pending_robot_fix.pop(key, None)

    if not data or data.get("expires", 0) < time.time():
        return None

    return data


def peek_pending_robot_fix(chat_id, user_id):
    """Ожидающая подсказка без изъятия (для тестов и логов)."""
    key = _robot_fix_key(chat_id, user_id)

    with _pending_robot_fix_lock:
        data = _pending_robot_fix.get(key)

    if not data or data.get("expires", 0) < time.time():
        return None

    return dict(data)


def expired_robot_fixes():
    """Просроченные подсказки: ждать больше нельзя, номер уходит как есть."""
    now = time.time()
    due = []

    with _pending_robot_fix_lock:
        for key, data in list(_pending_robot_fix.items()):
            if data.get("expires", 0) <= now:
                due.append((key, _pending_robot_fix.pop(key)))

    return due


def flush_expired_robot_fixes() -> int:
    """
    Досылает ошибки, по которым сотрудник не ответил.

    Терять сообщение нельзя: если подсказка истекла, работаем как раньше
    (очередь «неизвестных роботов» + карточка в Lark).
    """
    due = expired_robot_fixes()

    for _key, data in due:
        try:
            with reply_thread(data.get("thread_id")):
                _complete_missing_robot(
                    data["chat_id"],
                    data["sender"],
                    data["employee_name"],
                    data["parsed"],
                    data.get("photo_path"),
                    data.get("employee_card_id"),
                    prompt_message_id=data.get("prompt_message_id"),
                    warehouse=data.get("warehouse"),
                )
        except Exception:
            logger.exception(
                "Не удалось дослать ошибку по неизвестному роботу #%s",
                (data.get("parsed") or {}).get("robot"),
            )

    return len(due)


def robot_fix_keyboard(candidates):
    """Кнопки: исправить на похожий номер или подтвердить текущий."""
    rows = [
        [{"text": f"✅ Robot #{number}", "callback_data": f"rf:{number}"}]
        for number in candidates
    ]

    rows.append([{
        "text": "➡️ No, the number is correct",
        "callback_data": "rf:no",
    }])

    return {"inline_keyboard": rows}


def _ask_robot_fix(
    chat_id,
    sender,
    employee_name,
    parsed,
    photo_path,
    candidates,
    employee_card_id=None,
):
    """Спрашивает, не опечатка ли номер, и запоминает контекст ошибки."""
    sent = _send(
        chat_id,
        f"⚠️ Robot #{parsed['robot']} is not in the system.\n"
        "Is the number correct?",
        reply_markup=robot_fix_keyboard(candidates),
    )

    prompt_message_id = (sent or {}).get("message_id")

    set_pending_robot_fix(chat_id, sender.get("id"), {
        "chat_id": chat_id,
        "sender": sender,
        "employee_name": employee_name,
        "parsed": parsed,
        "photo_path": photo_path,
        "employee_card_id": employee_card_id,
        "prompt_message_id": prompt_message_id,
        # Склад и топик: досыл по TTL идёт из фонового потока, где контекста
        # уже нет, а запись обязана уйти на свой склад и в свой топик.
        "warehouse": current_warehouse(),
        "thread_id": _reply_thread(chat_id),
    })

    logger.info(
        "Робот #%s не найден: предложены варианты %s",
        parsed["robot"],
        candidates,
    )


def _complete_missing_robot(
    chat_id,
    sender,
    employee_name,
    parsed,
    photo_path=None,
    employee_card_id=None,
    prompt_message_id=None,
    warehouse=None,
):
    """
    Робота нет в справочнике: ставим номер в очередь и шлём ошибку в Lark.

    Факт «робота нет в системе» в Lark не пишем — об этом бот сообщает
    сотруднику в Telegram.
    """
    _delete_quiet(chat_id, prompt_message_id)

    warehouse = warehouse or current_warehouse()

    queue_missing_robot(
        parsed["robot"],
        employee_card_id=employee_card_id,
        chat_id=chat_id,
        warehouse=warehouse,
    )

    pretty = now_warsaw().strftime("%d.%m.%Y %H:%M:%S")

    forward_error(parsed, [
        ("👤 Employee", employee_name),
        ("🤖 Robot", parsed["robot"]),
        ("⚠️ Time", pretty),
        ("📝 Details", parsed["error_text"]),
    ], warehouse)

    if photo_path:
        send_photo(
            photo_path,
            f"📷 {employee_name}: {parsed['error_text']}",
            console,
            warehouse=warehouse,
        )

    logger.warning(
        "Робот %s не найден в системе — ошибка переслана в Lark "
        "без записи в базу (сотрудник уведомлён в Telegram)",
        parsed["robot"],
    )


def _handle_robot_fix_callback(chat_id, sender, parts, message_id, callback_id):
    """Нажатие кнопки в подсказке по номеру робота."""
    pending = take_pending_robot_fix(chat_id, sender.get("id"))

    if not pending:
        if callback_id:
            tg.answer_callback_query(
                callback_id,
                "This suggestion is outdated — send the message again",
            )

        _delete_quiet(chat_id, message_id)
        return

    action = parts[1] if len(parts) > 1 else "no"

    if action.isdigit():
        parsed = dict(pending["parsed"])
        parsed["robot"] = action

        if callback_id:
            tg.answer_callback_query(callback_id, f"Robot #{action}")

        # Сохраняем как обычную ошибку; повторно номер не предлагаем.
        save_and_forward_error(
            chat_id,
            pending["sender"],
            parsed,
            pending.get("prompt_message_id") or message_id,
            pending["employee_name"],
            pending.get("photo_path"),
            allow_fix=False,
        )
    else:
        if callback_id:
            tg.answer_callback_query(callback_id, "Keeping the number as is")

        _complete_missing_robot(
            chat_id,
            pending["sender"],
            pending["employee_name"],
            pending["parsed"],
            pending.get("photo_path"),
            pending.get("employee_card_id"),
            prompt_message_id=pending.get("prompt_message_id"),
            warehouse=pending.get("warehouse"),
        )

    _delete_quiet(chat_id, message_id)


def _handle_topics(chat_id, reply_to):
    """Показывает, какой топик за что отвечает (настройка и проверка)."""
    lines = ["🗂 Topic routing", ""]

    roles = {
        "error": "errors are read and answered here",
        "status": "/offline, /online, /robot, status changes",
        "stats": "/stats, /top, /downtime, /week, /digest",
        "service": "/help, /id, /reg, warnings",
    }

    lines.append("Errors are read from these topics:")
    lines.append("")

    for title, thread_id in error_topic_map(chat_id).items():
        name = topic_name(chat_id, thread_id) if thread_id else None

        if thread_id is None:
            shown = "NOT configured"
        else:
            shown = f"id {thread_id}" + (f" · {name!r}" if name else "")

        lines.append(f"  {title}: {shown}")

    lines.append("")
    lines.append("Shared topics:")
    lines.append("")

    for kind in ("status", "stats", "service"):
        thread_id = topic_thread(kind, chat_id)
        name = topic_name(chat_id, thread_id) if thread_id else None

        if thread_id is None:
            shown = "NOT configured"
        else:
            shown = f"id {thread_id}" + (f" · {name!r}" if name else "")

        lines.append(f"{TOPIC_TITLES[kind]}: {shown}")
        lines.append(f"    {roles[kind]}")

    seen = sorted(
        (thread_id, known)
        for (key_chat, thread_id), known in list(_topic_seen.items())
        if int(key_chat) == _chat_key(chat_id)
    )

    lines.append("")

    if seen:
        lines.append("Topics the bot has seen messages in:")
        lines.extend(
            f"  id {thread_id}" + (f" · {known!r}" if known else "")
            for thread_id, known in seen
        )
    else:
        lines.append("The bot hasn't seen any topic here yet.")

    lines.append("")
    lines.append(
        "If your topic is not in the list, send /id inside it."
    )
    lines.append("")
    lines.append("Lark hooks:")

    for kind, mapping in lark_hooks.hook_map().items():
        for title, url in mapping.items():
            lines.append(f"  {kind} {title}: …{lark_hooks.short(url)}")

    lines.append("")
    lines.append(
        "Answers always go to the topic the message came from."
    )
    lines.append(
        "Send /id in a topic to learn its id, then set "
        "TELEGRAM_TOPIC_STATUS / _STATS / _SERVICE in the service variables."
    )

    _send(chat_id, "\n".join(lines), reply_to_message_id=reply_to)


def _handle_top(chat_id, args, reply_to):
    """Топ типов проблем и роботов за период (/top [склад] day|week|month)."""
    warehouse, rest = warehouse_from_args(args)
    warehouse = warehouse or current_warehouse()

    raw = rest.strip().split()
    period = raw[0].lower() if raw else analytics.DEFAULT_PERIOD

    if not analytics.period_days(period):
        _send(
            chat_id,
            "Usage: /top [склад] [day|week|month]\n"
            f"Example: /top {list(WAREHOUSES)[0]} week",
            reply_to_message_id=reply_to,
        )
        return

    report = analytics.top_report(period, warehouse=warehouse)

    if report is None:
        _send(
            chat_id,
            DB_UNAVAILABLE_HINT,
            reply_to_message_id=reply_to,
            delete_after=CONFIRM_TTL_SECONDS,
        )
        return

    _send(
        chat_id,
        analytics.format_top_report(report),
        reply_to_message_id=reply_to,
    )


def _handle_downtime(chat_id, args, reply_to):
    """Топ роботов по суммарному простою (/downtime [склад] [дни])."""
    warehouse, rest = warehouse_from_args(args)
    warehouse = warehouse or current_warehouse()

    raw = rest.strip().split()
    days = raw[0] if raw else "7"

    if not days.isdigit() or not (1 <= int(days) <= 90):
        _send(
            chat_id,
            "Usage: /downtime [склад] [days 1..90]\n"
            f"Example: /downtime {list(WAREHOUSES)[0]} 7",
            reply_to_message_id=reply_to,
        )
        return

    report = analytics.downtime_report(int(days), warehouse=warehouse)

    if report is None:
        _send(
            chat_id,
            DB_UNAVAILABLE_HINT,
            reply_to_message_id=reply_to,
            delete_after=CONFIRM_TTL_SECONDS,
        )
        return

    _send(
        chat_id,
        analytics.format_downtime_report(report, int(days)),
        reply_to_message_id=reply_to,
    )


def _handle_week(chat_id, reply_to):
    """Предпросмотр недельного отчёта (в Lark он уходит по расписанию)."""
    text = analytics.weekly_text(warehouse=current_warehouse())

    _send(
        chat_id,
        text or "No data for the weekly report.",
        reply_to_message_id=reply_to,
    )


def _handle_digest(chat_id, reply_to):
    """Предпросмотр дайджеста обслуживания (та же сводка, что уходит в личку)."""
    text = digests.build_digest(warehouse=current_warehouse())

    _send(
        chat_id,
        text or "✅ Nothing to report: no stale offline robots, no open requests.",
        reply_to_message_id=reply_to,
    )


def _handle_warehouse_choice(chat_id, sender, parts, message_id, callback_id):
    """Нажатие кнопки выбора склада: номер есть на нескольких складах."""
    if len(parts) != 4:
        if callback_id:
            tg.answer_callback_query(callback_id, "Unknown action")

        return

    _, action, key, number = parts

    if action == "cancel":
        if callback_id:
            tg.answer_callback_query(callback_id, "Cancelled")

        _delete_quiet(chat_id, message_id)
        return

    title = WAREHOUSES.get(key)

    if not title:
        if callback_id:
            tg.answer_callback_query(callback_id, "Unknown warehouse")

        return

    if action == "robot":
        if callback_id:
            tg.answer_callback_query(callback_id, title)

        _show_robot_card(chat_id, number, title)
        _delete_quiet(chat_id, message_id)
        return

    if action not in STATUS_DIRECTIONS:
        if callback_id:
            tg.answer_callback_query(callback_id, "Unknown action")

        return

    employee, db_error = _lookup_employee(chat_id, sender, message_id)

    if db_error or not employee:
        if callback_id:
            tg.answer_callback_query(
                callback_id,
                "Database unavailable" if db_error else "Register first: /reg",
            )

        _delete_quiet(chat_id, message_id)
        return

    try:
        robot = robot_status.find_robot(number, warehouse=title, strict=True)
    except robot_status.StatusUnavailable:
        if callback_id:
            tg.answer_callback_query(
                callback_id, "Database unavailable — try again in a minute"
            )

        return

    if not robot:
        if callback_id:
            tg.answer_callback_query(callback_id, f"Not found in {title}")

        _delete_quiet(chat_id, message_id)
        return

    if callback_id:
        tg.answer_callback_query(callback_id, title)

    start_status_flow(chat_id, action, robot, title)
    _delete_quiet(chat_id, message_id)


def _handle_robot(chat_id, args, reply_to):
    """Карточка робота: /robot [склад] <номер>."""
    explicit, rest = warehouse_from_args(args)
    warehouse = explicit or current_warehouse()

    rest = (rest or "").strip()
    keys = "|".join(WAREHOUSES)

    if not rest:
        _send(
            chat_id,
            f"Usage: /robot [{keys}] <number>\n"
            f"Example: /robot {list(WAREHOUSES)[0]} 3680",
            reply_to_message_id=reply_to,
        )
        return

    number = rest.split()[0]

    if not number.lstrip("#").isdigit():
        _send(
            chat_id,
            f"⚠️ Robot number must be digits, got {number!r}.\n"
            "Example: /robot 3680",
            reply_to_message_id=reply_to,
        )
        return

    if explicit:
        _show_robot_card(chat_id, number, warehouse, reply_to)
        return

    try:
        matches = find_robot_matches(number)
    except robot_status.StatusUnavailable:
        _send(
            chat_id,
            DB_UNAVAILABLE_HINT,
            reply_to_message_id=reply_to,
            delete_after=CONFIRM_TTL_SECONDS,
        )
        return

    if len(matches) > 1:
        # Номер есть на нескольких складах: пусть выберет сотрудник.
        _ask_warehouse(
            chat_id, "robot", number, [title for title, _ in matches], reply_to
        )
        return

    _show_robot_card(
        chat_id,
        number,
        matches[0][0] if matches else warehouse,
        reply_to,
    )


def handle_error_text(chat_id, sender, text, message_id):
    """Сообщение с описанием ошибки: сохранить и переслать в Lark."""
    employee_name, db_error = _lookup_employee_name(chat_id, sender, message_id)

    if db_error:
        return

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

    save_and_forward_error(chat_id, sender, parsed, message_id, employee_name)


def save_and_forward_error(
    chat_id,
    sender,
    parsed,
    message_id,
    employee_name,
    photo_path: str = None,
    allow_fix: bool = True,
):
    """
    Сохраняет ошибку и пересылает её в Lark.

    Если для сотрудника ждёт фото (прислал раньше текста) или фото передано
    аргументом (текст был в подписи) — оно прикрепляется к этой же записи,
    и в Lark уходит одно сообщение: карточка ошибки + фото.
    """
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
    # не найдены (через notifier, т.е. в Telegram). При подсказке номера
    # решение об очереди и ответе принимает бот (defer_missing).
    defer = bool(allow_fix and ROBOT_FIX_SUGGEST)

    saved = send_to_data_base(
        parsed,
        data_obj,
        chat_id,
        defer_missing=defer,
        warehouse=current_warehouse(),
    )

    if isinstance(saved, dict) and saved.get("robot_missing"):
        if defer:
            candidates = robot_card.suggest_robot_numbers(parsed["robot"])
        else:
            candidates = []

        if candidates:
            if PHOTO_ATTACH_ENABLED:
                waited = take_pending_photo(chat_id, sender.get("id"))

                if waited and not photo_path:
                    photo_path = waited.get("path")

            _ask_robot_fix(
                chat_id,
                sender,
                employee_name,
                parsed,
                photo_path,
                candidates,
                saved.get("employee_card_id"),
            )
            return

        _complete_missing_robot(
            chat_id,
            sender,
            employee_name,
            parsed,
            photo_path,
            saved.get("employee_card_id"),
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
        warehouse=current_warehouse(),
    )

    pretty = now_warsaw().strftime("%d.%m.%Y %H:%M:%S")

    table_lines = [
        ("👤 Employee", employee_name),
        ("🤖 Robot", parsed["robot"]),
        ("⚠️ Time", pretty),
        ("📝 Details", parsed["error_text"]),
        ("📊 Shift issues", str(count)),
    ]

    # Фото, которое ждало текст ошибки (сотрудник прислал фото раньше).
    waited_photo = (
        take_pending_photo(chat_id, sender.get("id"))
        if PHOTO_ATTACH_ENABLED
        else None
    )

    if not PHOTO_ATTACH_ENABLED:
        photo_path = None

    if waited_photo and not photo_path:
        photo_path = waited_photo.get("path")

        logger.info(
            "К ошибке робота %s прикреплено ожидавшее фото",
            parsed["robot"],
        )

    # Запись в базу уже сделана — это точка фиксации. Всё, что ниже
    # (Storage, Lark, счётчики), best-effort: исключение здесь не должно
    # приводить к повторной обработке апдейта и второй записи.
    try:
        photo_url = (
            store_photo_for_record(saved, photo_path) if photo_path else None
        )

        if photo_path or photo_url:
            mode = send_error_with_photo(
                parsed,
                table_lines,
                photo_path=photo_path,
                photo_url=photo_url,
                warehouse=current_warehouse(),
            )
            forwarded = mode != "none"
        else:
            forwarded = forward_error(
                parsed, table_lines, current_warehouse()
            )

        remember_last_error(
            chat_id,
            sender.get("id"),
            saved,
            parsed,
            table_lines,
        )
    except Exception:
        logger.exception(
            "Ошибка пересылки после сохранения (робот %s) — запись уже в базе",
            parsed.get("robot"),
        )
        forwarded = False

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
        with_photo = " + photo" if (photo_path or photo_url) else ""

        _send(
            chat_id,
            f"✅ Saved: robot {parsed['robot']}{with_photo} — "
            f"{truncate(parsed['error_text'], 300)}\n"
            f"📊 Shift issues: {count}{suffix}",
            delete_after=CONFIRM_TTL_SECONDS,
        )


def attach_photo_to_last_error(chat_id, sender, recent, photo_path, message_id):
    """Фото пришло после текста — прикрепляем его к последней записи."""
    employee_name = get_employee_name(sender.get("id")) or _sender_title(sender)
    robot = recent.get("parsed", {}).get("robot", "?")

    result = send_photo(
        photo_path,
        f"📷 Robot {robot} · {employee_name}",
        console,
        warehouse=current_warehouse(),
    )

    if result.get("url") and recent.get("glpc_id"):
        set_exception_photo("exceptions_glpc", recent["glpc_id"], result["url"])

    logger.info(
        "Фото прикреплено к последней ошибке робота %s (режим %s)",
        robot,
        result.get("mode"),
    )

    if result.get("mode") == "none":
        _send(
            chat_id,
            "⚠️ Can't forward the photo to Lark right now (see logs).",
            reply_to_message_id=message_id,
            delete_after=CONFIRM_TTL_SECONDS,
        )
        return

    if SEND_CONFIRMATION:
        _send(
            chat_id,
            f"✅ Photo attached to robot {robot}",
            reply_to_message_id=message_id,
            delete_after=CONFIRM_TTL_SECONDS,
        )


def handle_photo(chat_id, sender, message, message_id):
    """
    Фото: ищем текст ошибки рядом с ним и создаём одну запись.

    Порядок: подпись к фото → недавняя ошибка этого же сотрудника
    (текст был раньше) → ожидание текста (текст придёт следующим).
    """
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

    console.print(f"[cyan]📷 Фото сохранено: {destination}[/cyan]")

    caption = (message.get("caption") or "").strip()
    employee_name = get_employee_name(sender.get("id"))

    if not PHOTO_ATTACH_ENABLED:
        # Привязка выключена: фото просто уходит в группу (как раньше).
        photo_caption = (
            f"📷 Photo from {employee_name}"
            if employee_name
            else f"📷 Photo from {_sender_title(sender)} (Telegram)"
        )

        if caption:
            photo_caption = f"{photo_caption}\n{caption}"

        mode = send_photo(
            destination,
            photo_caption,
            console,
            warehouse=current_warehouse(),
        )

        if mode["mode"] == "none":
            _send(
                chat_id,
                "⚠️ Can't forward the photo to Lark right now (see logs).",
                reply_to_message_id=message_id,
                delete_after=CONFIRM_TTL_SECONDS,
            )
        elif SEND_CONFIRMATION and mode["mode"] == "lark":
            _send(
                chat_id,
                "✅ Photo forwarded to Lark",
                reply_to_message_id=message_id,
                delete_after=CONFIRM_TTL_SECONDS,
            )
        elif mode["mode"] == "link":
            _send(
                chat_id,
                "✅ Photo sent to Lark as a link",
                reply_to_message_id=message_id,
                delete_after=CONFIRM_TTL_SECONDS,
            )

        return

    # 1) В подписи целиком ошибка — создаём запись вместе с фото.
    parsed = parse_error_message(caption) if caption else None

    if parsed and parsed["robot"].isdigit() and employee_name:
        logger.info("Ошибка из подписи к фото: robot=%s", parsed["robot"])
        save_and_forward_error(
            chat_id,
            sender,
            parsed,
            message_id,
            employee_name,
            photo_path=destination if PHOTO_ATTACH_ENABLED else None,
        )
        return

    # 2) Есть недавняя ошибка этого сотрудника — прикрепляем фото к ней.
    recent = recent_last_error(chat_id, sender.get("id"))

    if recent:
        attach_photo_to_last_error(
            chat_id,
            sender,
            recent,
            destination,
            message_id,
        )
        return

    # 3) Держим фото: возможно, текст ошибки придёт следующим сообщением.
    put_pending_photo(
        chat_id,
        sender.get("id"),
        destination,
        message_id,
        caption,
        current_warehouse(),
        _reply_thread(chat_id),
    )

    _send(
        chat_id,
        "📷 Photo received.\n"
        f"Send the error text in one message within {PHOTO_HOLD_SECONDS}s "
        "and I'll combine it with the photo into one record "
        "(or /cancel to forward the photo as is).",
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


# Сервисные сообщения форума: создание/переименование топика, закрепление,
# вход/выход участника и т.п. Это не «непонятный тип», а служебная лента —
# отвечать на неё нечем.
SERVICE_MESSAGE_FIELDS = (
    "forum_topic_created",
    "forum_topic_edited",
    "forum_topic_closed",
    "forum_topic_reopened",
    "general_forum_topic_hidden",
    "general_forum_topic_unhidden",
    "pinned_message",
    "new_chat_members",
    "left_chat_member",
    "new_chat_title",
    "new_chat_photo",
    "delete_chat_photo",
    "video_chat_started",
    "video_chat_ended",
    "video_chat_participants_invited",
    "message_auto_delete_timer_changed",
    "proximity_alert_triggered",
    "write_access_allowed",
    "successful_payment",
)


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

    chat = message.get("chat") or {}
    chat_id = chat.get("id")

    if chat_id is None:
        return

    # Имена топиков бот узнаёт только из сервисных сообщений форума, поэтому
    # учим их ДО проверки «от бота ли сообщение»: топики может создать и сам
    # бот (createForumTopic), и другой бот.
    if message.get("forum_topic_created") or message.get("forum_topic_edited"):
        _learn_topic_from_message(chat_id, message)

    if any(message.get(field) for field in SERVICE_MESSAGE_FIELDS):
        logger.info(
            "Ignored: сервисное сообщение (chat=%s thread=%s)",
            chat_id,
            message.get("message_thread_id"),
        )
        return

    sender = message.get("from") or {}

    if sender.get("is_bot"):
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

    # Склад сообщения: топик ошибок GLP-C -> GLP-C, топик ошибок SP3 -> SMALL-P3.
    # В общих топиках — склад по умолчанию (его можно переопределить в команде).
    set_current_warehouse(warehouse_for_thread(chat_id, thread_id))

    # Ответ уходит только в этот топик — никуда больше.
    set_origin_thread(chat_id, thread_id)

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
    shared = is_shared_topic(chat_id, thread_id)

    if not allowed and not is_command and not shared:
        _handle_wrong_topic(chat_id, thread_id, reason)
        return

    if text:
        if not allowed and not is_command:
            # Общий топик: описание причины для смены статуса принимаем (флоу
            # живёт здесь), а ошибку — нет: её склад определяется топиком
            # ошибок, иначе запись уйдёт не на тот склад.
            if peek_pending_status(chat_id, sender.get("id")):
                _handle_text_message(chat_id, sender, text, message_id, chat)
            elif parse_error_message(text):
                _handle_wrong_topic(
                    chat_id, thread_id, reason, error_attempt=True
                )
            else:
                logger.info(
                    "Ignored: текст в общем топике без ошибки (chat=%s thread=%s)",
                    chat_id,
                    thread_id,
                )

            return

        _handle_text_message(chat_id, sender, text, message_id, chat)
        return

    if message.get("photo"):
        _show_console_message(chat, sender, "photo", extra_rows=[("🖼 Caption", caption or "-")])
        _send_action(chat_id, "upload_photo")
        handle_photo(chat_id, sender, message, message_id)
        return

    if caption:
        # Вложение с подписью (документ, видео, гифка). Если в подписи есть
        # ошибка — сохраняем её как обычный текст; иначе молчим: подсказка на
        # каждое вложение превращается в спам.
        if parse_error_message(caption):
            logger.info(
                "Ошибка в подписи к вложению (без фото): %r", caption[:80]
            )
            handle_error_text(chat_id, sender, caption, message_id)
        else:
            logger.info(
                "Ignored: вложение с подписью без ошибки (chat=%s message=%s)",
                chat_id,
                message_id,
            )

        return

    logger.info(
        "Ignored: неподдерживаемый тип сообщения (chat=%s message=%s: %s)",
        chat_id,
        message_id,
        ", ".join(
            key for key in (
                "sticker", "voice", "video", "video_note", "audio",
                "document", "animation", "contact", "location", "poll",
                "dice", "venue", "game",
            )
            if message.get(key)
        ) or "unknown",
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

        # reply_to не передаём: команду бот сразу удалит, и ответ «в ответ
        # на удалённое сообщение» выглядел бы сломанным.
        if _handle_command(chat_id, sender, normalized, args, None, chat):
            # Команда отработана — затираем её, чтобы чат не зарастал.
            _delete_user_message(chat_id, message_id)
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

        _send(chat_id, hint)

        _delete_user_message(chat_id, message_id)
        return

    pending = take_pending_status(chat_id, sender.get("id"))

    if pending:
        # Это описание причины для смены статуса, а не сообщение об ошибке.
        logger.info(
            "Описание причины для робота #%s: %r",
            pending.get("robot_number"),
            text[:80],
        )

        changed = finish_status_change(
            chat_id, sender, text, message_id, pending
        )

        if changed:
            # Описание причины бот забирает себе — но только если статус
            # действительно изменён: иначе сотрудник потерял бы текст.
            _delete_user_message(chat_id, message_id)

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

# Последний подтверждённый offset: сохраняем его при остановке, чтобы
# после перезапуска Telegram не прислал уже обработанные апдейты.
_CURRENT_OFFSET = None

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

    global _LAST_POLL_AT, _CURRENT_OFFSET

    # update_id -> сколько раз обработка уже падала
    failed_attempts = {}

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

            try:
                handle_update(update, BOT_USERNAME)
            except Exception:
                attempts = failed_attempts.get(update_id, 0) + 1
                failed_attempts[update_id] = attempts

                logger.exception(
                    "Не удалось обработать апдейт %s (попытка %s из %s)",
                    update_id,
                    attempts,
                    MAX_UPDATE_ATTEMPTS,
                )

                if attempts >= MAX_UPDATE_ATTEMPTS:
                    logger.error(
                        "Апдейт %s пропускаю после %s попыток (текст: %r)",
                        update_id,
                        attempts,
                        str(_update_text(update))[:120],
                    )
                    failed_attempts.pop(update_id, None)
                else:
                    # Апдейт НЕ подтверждаем: Telegram пришлёт его снова.
                    break
            else:
                failed_attempts.pop(update_id, None)

            if update_id is not None:
                offset = update_id + 1
                _CURRENT_OFFSET = offset

                # Фиксируем offset после КАЖДОГО подтверждённого апдейта.
                # Если сохранять его только в конце батча, падение процесса
                # посреди батча вернёт Telegram уже обработанные апдейты —
                # а это дубли строк в Supabase и дубли карточек в Lark.
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
            # Фиксируем offset: иначе после перезапуска Telegram пришлёт
            # последний батч повторно и записи продублируются.
            if _CURRENT_OFFSET:
                save_offset(_CURRENT_OFFSET)
        except Exception:
            logger.exception("Не удалось сохранить offset при завершении")

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

        if result["status"] == "error":
            LEASE_STATUS = "standby (error)"
            continue

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
                # Отчёты и обслуживание очереди — только у опрашивающего.
                start_shift_scheduler()
                start_queue_autoclose()

                if digests.DIGEST_ENABLED:
                    start_digest_scheduler()

                if analytics.WEEKLY_REPORT_ENABLED:
                    start_weekly_scheduler()

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

    if LEASE_STATUS == "starting":
        if (now - _STARTED_AT) > STARTUP_GRACE_SECONDS:
            healthy, reason = False, "not polling yet"
    elif LEASE_STATUS == "poller":
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


def is_safe_object_name(name: str) -> bool:
    """Имя файла из короткой ссылки: без .., только безопасные символы."""
    return bool(name) and ".." not in name and bool(
        re.fullmatch(r"[A-Za-z0-9._/-]{1,120}", name)
    )


@app.route("/p/<path:object_name>", methods=["GET"])
def photo_redirect(object_name):
    """
    Короткая ссылка на фото: /p/<файл> -> подписанный URL Supabase.

    Имя файла приходит от Telegram (случайный id), но проверяем его на
    всякий случай: без .. и посторонних символов.
    """
    if not is_safe_object_name(object_name):
        return jsonify({"error": "bad object name"}), 400

    try:
        url = resolve_photo_url(object_name, strict=True)
    except StorageUnavailable:
        # Storage лежит — это не «файла нет»: отдаём 503, чтобы битая
        # ссылка не закэшировалась у клиента и в Telegram.
        logger.warning(
            "Storage недоступен при открытии короткой ссылки /p/%s",
            object_name,
        )
        return jsonify({"error": "storage unavailable"}), 503

    if not url:
        return jsonify({"error": "not found"}), 404

    return redirect(url, code=302)


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
        {"command": "robot", "description": "Robot card by number"},
        {"command": "offline", "description": "Take a robot out of service"},
        {"command": "online", "description": "Return a robot to service"},
        {"command": "id", "description": "Show chat/user IDs"},
        {"command": "help", "description": "Help"},
    ])

    # Ответы пользователю идут в Telegram, а не в Lark API.
    set_notifier(lambda chat_id, text: _send(chat_id, text))

    # Дайджест обслуживания уходит в личку, а не в топик.
    digests.set_digest_sender(
        lambda chat_id, text: tg.send_message(chat_id, text)
    )

    console.print("[bold green]Telegram-бот запущен[/bold green]")
    console.print(f"[cyan]Bot: @{BOT_USERNAME}[/cyan]")
    console.print(f"[cyan]Timezone: {WARSAW_TZ}[/cyan]")
    console.print(
        f"[cyan]Current time: {now_warsaw().strftime('%d.%m.%Y %H:%M:%S')}[/cyan]"
    )

    if ALLOWED_CHAT_IDS:
        console.print(f"[cyan]Allowed chats: {sorted(ALLOWED_CHAT_IDS)}[/cyan]")

    console.print("[cyan]Lark hooks:[/cyan]")

    for hook_kind, mapping in lark_hooks.hook_map().items():
        for warehouse_title, url in mapping.items():
            console.print(
                f"  {hook_kind} {warehouse_title}: …{lark_hooks.short(url)}"
            )

    console.print(f"[cyan]Monitored topic: {monitored_topic_label()}[/cyan]")

    console.print("[cyan]Topic routing:[/cyan]")

    for warehouse_title, thread_id in error_topic_map().items():
        if thread_id is None:
            console.print(
                f"  [red]Ошибки {warehouse_title}: топик не задан[/red]"
            )
        else:
            console.print(
                f"  [green]Ошибки {warehouse_title}: id {thread_id}[/green]"
            )

    for topic_kind in ("status", "stats", "service"):
        thread_id = topic_thread(topic_kind)

        if thread_id is None:
            console.print(
                f"  [yellow]{TOPIC_TITLES[topic_kind]}: не задан — "
                f"ответы остаются в топике-источнике[/yellow]"
            )
        else:
            console.print(
                f"  [green]{TOPIC_TITLES[topic_kind]}: id {thread_id}[/green]"
            )

    if TELEGRAM_TOPIC_ID is None and not TELEGRAM_TOPIC_NAME:
        console.print(
            "[bold red]⚠️ Фильтр топика не задан: бот принимает сообщения "
            "из ЛЮБОГО топика и любого чата. Задайте TELEGRAM_TOPIC_ID "
            "(узнать: /id в нужном топике).[/bold red]"
        )

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

    # Уборщик: удаляет подтверждения (если TTL > 0) и пересылает фото,
    # которые так и не дождались текста ошибки.
    start_deletion_worker()

    if CONFIRM_TTL_SECONDS > 0:
        console.print(
            f"[cyan]Подтверждения удаляются через "
            f"{CONFIRM_TTL_SECONDS}s[/cyan]"
        )

    # Один Telegram — один опрашивающий. Иначе Telegram отдаёт одни и те же
    # сообщения обоим инстансам и они обрабатываются дважды (дубли).
    global LEASE_STATUS, LEASE_MODE
    holder = bot_lease.holder_id()

    # Обработчик ставим ДО получения лиза: сигнал в это окно иначе оставил бы
    # лиз висеть до истечения TTL.
    install_shutdown_handler(holder)

    lease = bot_lease.acquire(holder)
    LEASE_MODE = lease["status"]

    if lease["acquired"]:
        console.print(
            f"[green]Опрашиваю Telegram (лиз: {lease['status']})[/green]"
        )
    elif lease["status"] == "error":
        # Не смогли даже прочитать лиз: это не «standby у соседа»,
        # а нездоровое состояние — /health должен отдавать 503.
        LEASE_STATUS = "standby (error)"
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
