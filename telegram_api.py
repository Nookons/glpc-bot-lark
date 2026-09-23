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

from logging_config import setup_logging


# .env должен быть загружен до чтения токена ниже.
load_dotenv()


logger = setup_logging(__name__)


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

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
        "allowed_updates": ["message"],
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
):
    """
    Отправляет текстовое сообщение. Возвращает message или None.

    message_thread_id — топик форум-группы: без него сообщение уйдёт
    в «General», а не в тот топик, откуда пришла ошибка.
    """
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }

    if message_thread_id is not None:
        payload["message_thread_id"] = int(message_thread_id)

    if reply_to_message_id is not None:
        payload["reply_parameters"] = {
            "message_id": reply_to_message_id,
            "allow_sending_without_reply": True,
        }

    if disable_notification:
        payload["disable_notification"] = True

    return call("sendMessage", payload, timeout=15)


def send_chat_action(chat_id, action: str = "typing", message_thread_id: int = None):
    """Показывает «печатает…», пока идёт обработка."""
    payload = {"chat_id": chat_id, "action": action}

    if message_thread_id is not None:
        payload["message_thread_id"] = int(message_thread_id)

    return call("sendChatAction", payload, timeout=15)


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
        response = requests.get(url, timeout=60)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error("Telegram file download failed: %s", e)
        return None

    try:
        os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)

        with open(destination, "wb") as f:
            f.write(response.content)
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
