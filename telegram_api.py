"""
Тонкий клиент Telegram Bot API (long polling).

Без внешних зависимостей кроме requests — чтобы не тянуть в проект
aiogram/python-telegram-bot ради нескольких методов.

Документация: https://core.telegram.org/bots/api
"""

from __future__ import annotations

import os
import time
import json

import requests
from dotenv import load_dotenv

from env_utils import env_int
from logging_config import setup_logging
from text_utils import TELEGRAM_TEXT_LIMIT, truncate


# .env должен быть загружен до чтения токена ниже.
load_dotenv()


logger = setup_logging(__name__)


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

# Подпись к фото у Telegram короче обычного текста (1024 символа).
TELEGRAM_CAPTION_LIMIT = 1024

# Telegram Bot API отдаёт боту файлы до 20 МБ; страхуемся от «тяжёлых» фото,
# чтобы не вычитывать гигабайты в память.
MAX_FILE_MB = env_int("TELEGRAM_MAX_FILE_MB", 25)

_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
_FILE_BASE = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}"

# Telegram отдаёт retry_after при 429; повторяем запрос один раз.
_MAX_FLOOD_RETRIES = 1


def token_configured() -> bool:
    """True, если токен бота задан в окружении."""
    return bool(TELEGRAM_BOT_TOKEN)


# Методы, которые в dry-run разрешены: без getUpdates бот не получит
# сообщения, а getMe нужен для проверки токена на старте.
_DRY_RUN_ALLOWED = {"getUpdates", "getMe", "getFile"}

# Методы, отправляющие текст в чат. В dry-run проходят ТОЛЬКО в топики из
# белого списка (TELEGRAM_LISTEN_TOPICS): так тестовый бот отвечает внутри
# своего топика, но не может ничего написать в боевые.
_DRY_RUN_TEXT_METHODS = {
    "sendMessage",
    "editMessageText",
    "editMessageCaption",
    "sendPhoto",
}


def _dry_run() -> bool:
    """Тестовый режим: наружу ничего не отправляем (читается на каждом вызове)."""
    return os.environ.get("TELEGRAM_DRY_RUN", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _test_prefix() -> str:
    """Необязательная пометка тестового бота в ответах."""
    return os.environ.get("TELEGRAM_TEST_REPLY_PREFIX", "").strip()[:40]


def _listen_topic_ids() -> set:
    """Id топиков из белого списка (TELEGRAM_LISTEN_TOPICS)."""
    raw = os.environ.get("TELEGRAM_LISTEN_TOPICS", "")

    return {
        int(part.strip())
        for part in raw.replace(" ", "").split(",")
        if part.strip().lstrip("-").isdigit()
    }


def _dry_run_reply_allowed(method: str, payload: dict) -> bool:
    """
    Можно ли в dry-run всё-таки отправить это сообщение.

    Разрешено ровно одно: ответ в топик из белого списка. Проверка идёт по
    message_thread_id — то есть по адресу доставки, а не по тексту. Поэтому
    никакой ответ не может уехать в боевой топик, даже если в боте
    где-то забудут проверку.
    """
    if method not in _DRY_RUN_TEXT_METHODS:
        return False

    allowed = _listen_topic_ids()

    if not allowed:
        # Белого списка нет — значит непонятно, какой топик тестовый.
        # Безопасное поведение: не отправляем ничего.
        return False

    thread_id = (payload or {}).get("message_thread_id")

    if thread_id is None:
        return False

    try:
        return int(thread_id) in allowed
    except (TypeError, ValueError):
        return False


def call(method: str, payload: dict = None, timeout: int = 40):
    """
    Вызывает метод Bot API. Возвращает поле `result` при успехе,
    иначе None (ошибка сети, не-JSON ответ, ok=false).
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set")
        return None

    if _dry_run() and method not in _DRY_RUN_ALLOWED:
        prefix = _test_prefix()
        payload = payload or {}

        # Тестовый бот отвечает только внутри своего топика; пометка, если
        # задана, добавляется к тексту (необязательное удобство).
        if _dry_run_reply_allowed(method, payload):
            text = payload.get("text")

            if prefix and isinstance(text, str) and not text.startswith(prefix):
                payload = dict(payload, text=f"{prefix} {text}")

            logger.info(
                "[DRY-RUN] ответ внутри тестового топика: %s thread=%s",
                method,
                payload.get("message_thread_id"),
            )

            return _post_call(method, payload, timeout)

        # Тестовый бот не должен ничего писать в чат/менять состояние.
        logger.info(
            "[DRY-RUN] Telegram %s не вызван: payload=%r",
            method,
            payload,
        )
        # Форма ответа зависит от метода: send_message ждёт dict с
        # message_id, delete_message — True. Возвращаем правдоподобную
        # заглушку, чтобы вызывающий код не ушёл в обработку ошибки.
        if method == "delete_message":
            return True

        return {"message_id": 0, "dry_run": True}

    return _post_call(method, payload or {}, timeout)


def _post_call(method: str, payload: dict, timeout: int):
    """
    Реальный вызов Bot API с обработкой flood-limit.

    Вынесено отдельно, чтобы dry-run мог отправить разрешённый ответ
    (внутри тестового топика) тем же путём, что и обычный вызов.
    """
    url = f"{_API_BASE}/{method}"

    for attempt in range(_MAX_FLOOD_RETRIES + 1):
        try:
            response = requests.post(
                url,
                json=payload,
                timeout=timeout,
            )
        except requests.exceptions.RequestException as e:
            logger.error("Telegram %s failed: %s", method, e)
            return None

        try:
            data = response.json()
        except ValueError:
            logger.error(
                "Telegram %s returned non-JSON (HTTP %s)",
                method,
                response.status_code,
            )
            return None

        if data.get("ok"):
            return data.get("result")

        retry_after = (data.get("parameters") or {}).get("retry_after")

        description = str(data.get("description") or "")

        if data.get("error_code") == 409 and method == "getUpdates":
            # Второй опрашивающий (например, во время выкатки) — это
            # ожидаемая ситуация, а не сбой.
            logger.warning(
                "Telegram %s: %s",
                method,
                description,
            )
        elif "message is not modified" in description:
            # Telegram отвечает так, когда содержимое и кнопки совпадают с
            # текущими. Для многоуровневых меню это норма: экран перерисовывается
            # на каждом нажатии, и повторное нажатие той же кнопки даёт
            # идентичный экран. Раньше это писалось как ERROR и засоряло логи
            # (на живом проде дало 3 «ошибки» за 2 минуты, хотя сбоя не было);
            # настоящие сбои в них терялись. Уровень — DEBUG: перерисовка
            # ожидаема, а видеть её при разборе всё равно полезно.
            logger.debug(
                "Telegram %s: %s",
                method,
                description,
            )
        else:
            logger.error(
                "Telegram %s error: code=%s description=%s",
                method,
                data.get("error_code"),
                description,
            )

        if retry_after and attempt < _MAX_FLOOD_RETRIES:
            wait = min(int(retry_after), 30) + 1
            logger.warning("Telegram flood limit, sleeping %ss", wait)
            time.sleep(wait)
            continue

        return None

    return None


# ============================================================
# UPDATES (long polling)
# ============================================================

def get_updates(offset: int = None, timeout: int = 30):
    """
    Long polling getUpdates.

    Возвращает список апдейтов (возможно пустой) или None при ошибке.
    """
    payload = {
        "timeout": timeout,
        # callback_query нужен для кнопок выбора причины смены статуса.
        "allowed_updates": ["message", "callback_query"],
    }

    if offset is not None:
        payload["offset"] = offset

    # HTTP-таймаут должен быть больше long-poll таймаута.
    return call("getUpdates", payload, timeout=timeout + 15)


# ============================================================
# MESSAGES
# ============================================================

def send_message(
    chat_id,
    text: str,
    reply_to_message_id: int = None,
    disable_notification: bool = False,
    message_thread_id: int = None,
    reply_markup: dict = None,
):
    """
    Отправляет текстовое сообщение. Возвращает message или None.

    message_thread_id — топик форум-группы: без него сообщение уйдёт
    в «General», а не в тот топик, откуда пришла ошибка.
    """
    text = truncate(text, TELEGRAM_TEXT_LIMIT)

    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }

    if message_thread_id is not None:
        payload["message_thread_id"] = int(message_thread_id)

    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    if reply_to_message_id is not None:
        payload["reply_parameters"] = {
            "message_id": reply_to_message_id,
            "allow_sending_without_reply": True,
        }

    if disable_notification:
        payload["disable_notification"] = True

    return call("sendMessage", payload, timeout=15)


def edit_message_text(
    chat_id,
    message_id,
    text: str,
    reply_markup: dict = None,
    message_thread_id: int = None,
):
    """
    Меняет текст сообщения бота (и убирает кнопки, если markup не передан).

    message_thread_id нужен и на правке: Telegram адресует сообщение по
    (chat_id, message_id), но без thread_id не понимает, что правка идёт
    внутри форум-топика, и отклоняет запрос. Поэтому топик передаётся явно —
    бот всегда знает, откуда пришло сообщение.
    """
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": truncate(text, TELEGRAM_TEXT_LIMIT),
        "disable_web_page_preview": True,
    }

    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    if message_thread_id is not None:
        payload["message_thread_id"] = message_thread_id

    return call("editMessageText", payload, timeout=15)


def edit_message_caption(
    chat_id,
    message_id,
    caption: str,
    reply_markup: dict = None,
    message_thread_id: int = None,
):
    """
    Меняет подпись к фото и его кнопки.

    Нужно для многоуровневого меню: сообщение с фото остаётся тем же,
    меняются только подпись и кнопки (иначе чат засоряется копиями фото).
    message_thread_id обязателен по той же причине, что и в edit_message_text.
    """
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "caption": truncate(caption, TELEGRAM_CAPTION_LIMIT),
    }

    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    if message_thread_id is not None:
        payload["message_thread_id"] = message_thread_id

    return call("editMessageCaption", payload, timeout=15)


def send_photo(
    chat_id,
    image_path: str,
    caption: str = None,
    message_thread_id: int = None,
    reply_markup: dict = None,
    reply_to_message_id: int = None,
    timeout: int = 60,
):
    """
    Отправляет фото из файла. Возвращает message или None.

    Отличие от send_message: файл уходит multipart/form-data, поэтому
    вызов идёт мимо call() (он шлёт JSON) — с той же проверкой dry-run.
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set")
        return None

    if _dry_run() and not _dry_run_reply_allowed(
        "sendPhoto", {"message_thread_id": message_thread_id}
    ):
        logger.info(
            "[DRY-RUN] sendPhoto не вызван: chat=%s thread=%s file=%s caption=%r",
            chat_id,
            message_thread_id,
            image_path,
            caption,
        )
        return {"message_id": 0, "dry_run": True}

    data = {"chat_id": str(chat_id)}

    if caption:
        data["caption"] = truncate(caption, TELEGRAM_CAPTION_LIMIT)

    if message_thread_id is not None:
        data["message_thread_id"] = str(int(message_thread_id))

    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

    if reply_to_message_id is not None:
        data["reply_parameters"] = json.dumps({
            "message_id": reply_to_message_id,
            "allow_sending_without_reply": True,
        })

    try:
        with open(image_path, "rb") as handle:
            response = requests.post(
                f"{_API_BASE}/sendPhoto",
                data=data,
                files={"photo": handle},
                timeout=timeout,
            )
    except requests.exceptions.RequestException as e:
        logger.error("Telegram sendPhoto failed: %s", e)
        return None
    except OSError as e:
        logger.error("Не удалось прочитать фото %s: %s", image_path, e)
        return None

    try:
        payload = response.json()
    except ValueError:
        logger.error(
            "Telegram sendPhoto вернул не-JSON (HTTP %s)",
            response.status_code,
        )
        return None

    if not payload.get("ok"):
        logger.error(
            "Telegram sendPhoto error: code=%s description=%s",
            payload.get("error_code"),
            payload.get("description"),
        )
        return None

    return payload.get("result")


def answer_callback_query(callback_query_id: str, text: str = None):
    """Гасит «часики» на кнопке; text показывается всплывашкой."""
    payload = {"callback_query_id": callback_query_id}

    if text:
        payload["text"] = text[:200]

    return call("answerCallbackQuery", payload, timeout=15)


def send_chat_action(chat_id, action: str = "typing", message_thread_id: int = None):
    """Показывает «печатает…», пока идёт обработка."""
    payload = {"chat_id": chat_id, "action": action}

    if message_thread_id is not None:
        payload["message_thread_id"] = int(message_thread_id)

    return call("sendChatAction", payload, timeout=15)


def delete_message(chat_id, message_id) -> bool:
    """
    Удаляет сообщение бота.

    «Сообщение уже удалено» — обычная ситуация (например, кто-то удалил
    его вручную), поэтому такие ответы не засоряют лог ошибками.
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set")
        return False

    if _dry_run():
        # Тестовый бот не должен трогать чужие сообщения в боевой группе.
        logger.info(
            "[DRY-RUN] deleteMessage не вызван: chat=%s message=%s",
            chat_id,
            message_id,
        )
        return True

    try:
        response = requests.post(
            f"{_API_BASE}/deleteMessage",
            json={"chat_id": chat_id, "message_id": message_id},
            timeout=15,
        )
        data = response.json()
    except requests.exceptions.RequestException as e:
        logger.warning("Telegram deleteMessage failed: %s", e)
        return False
    except ValueError:
        logger.warning("Telegram deleteMessage returned non-JSON")
        return False

    if not data.get("ok"):
        logger.info(
            "Telegram deleteMessage: %s (chat=%s message=%s)",
            data.get("description"),
            chat_id,
            message_id,
        )
        return False

    return True


# ============================================================
# FILES
# ============================================================

def get_file(file_id: str):
    """Возвращает file_path для скачивания или None."""
    result = call("getFile", {"file_id": file_id}, timeout=20)

    if not result:
        return None

    return result.get("file_path")


def download_file(file_path: str, destination: str):
    """
    Скачивает файл по file_path из getFile и сохраняет в destination.
    Возвращает destination или None.
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set")
        return None

    url = f"{_FILE_BASE}/{file_path}"

    try:
        response = requests.get(url, timeout=60, stream=True)
        response.raise_for_status()

        declared_size = int(response.headers.get("Content-Length") or 0)

        if declared_size and declared_size > MAX_FILE_MB * 1024 * 1024:
            logger.error(
                "Файл %s слишком большой: %s байт (лимит %s МБ)",
                file_path,
                declared_size,
                MAX_FILE_MB,
            )
            response.close()
            return None

        content = response.content
    except requests.exceptions.RequestException as e:
        logger.error("Telegram file download failed: %s", e)
        return None

    if len(content) > MAX_FILE_MB * 1024 * 1024:
        logger.error(
            "Файл %s превысил лимит %s МБ после скачивания",
            file_path,
            MAX_FILE_MB,
        )
        return None

    try:
        os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)

        with open(destination, "wb") as f:
            f.write(content)
    except OSError as e:
        logger.error("Failed to write %s: %s", destination, e)
        return None

    return destination


# ============================================================
# BOT META
# ============================================================

def get_me():
    """Информация о боте (нужна для username и проверки токена)."""
    return call("getMe", {}, timeout=15)


def set_my_commands(commands: list):
    """Публикует список команд для меню Telegram."""
    return call(
        "setMyCommands",
        {"commands": commands},
        timeout=15,
    )
