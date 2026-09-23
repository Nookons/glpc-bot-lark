"""
Тонкий клиент Telegram Bot API (long polling).

Без внешних зависимостей кроме requests — чтобы не тянуть в проект
aiogram/python-telegram-bot ради нескольких методов.

Документация: https://core.telegram.org/bots/api
"""

from __future__ import annotations

import os
import time

import requests
from dotenv import load_dotenv

from env_utils import env_int
from logging_config import setup_logging
from text_utils import TELEGRAM_TEXT_LIMIT, truncate


# .env должен быть загружен до чтения токена ниже.
load_dotenv()


logger = setup_logging(__name__)


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

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


def call(method: str, payload: dict = None, timeout: int = 40):
    """
    Вызывает метод Bot API. Возвращает поле `result` при успехе,
    иначе None (ошибка сети, не-JSON ответ, ok=false).
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set")
        return None

    url = f"{_API_BASE}/{method}"

    for attempt in range(_MAX_FLOOD_RETRIES + 1):
        try:
            response = requests.post(
                url,
                json=payload or {},
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

        if data.get("error_code") == 409 and method == "getUpdates":
            # Второй опрашивающий (например, во время выкатки) — это
            # ожидаемая ситуация, а не сбой.
            logger.warning(
                "Telegram %s: %s",
                method,
                data.get("description"),
            )
        else:
            logger.error(
                "Telegram %s error: code=%s description=%s",
                method,
                data.get("error_code"),
                data.get("description"),
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


def edit_message_text(chat_id, message_id, text: str, reply_markup: dict = None):
    """Меняет текст сообщения бота (и убирает кнопки, если markup не передан)."""
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": truncate(text, TELEGRAM_TEXT_LIMIT),
        "disable_web_page_preview": True,
    }

    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    return call("editMessageText", payload, timeout=15)


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
