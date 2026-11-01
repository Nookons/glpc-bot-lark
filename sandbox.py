"""
═══════════════════════════════════════════════════════════════════════════════
ТЕСТОВЫЙ СТЕНД: ФОТО → МОДЕЛЬ И НОМЕР, БЕЗ ОТПРАВОК
═══════════════════════════════════════════════════════════════════════════════

🔴 **Требование владельца 01.11.2026:** «сделать топик новый в телеграмме, пока
тестовый, ошибки никуда не отправлять; бот при помощи нашей нейронки понимает,
что за модель робота на фото и какой у него номер».

## Что делает этот модуль

1. Ловит фото **только в тестовом топике**.
2. Отправляет снимок на сервер распознавания.
3. Отвечает: модель робота, уверенность, номер.
4. 🔴 **Никуда не пишет:** ни в журнал, ни в Lark, ни в базу, ни в отчёт.

## Почему отдельный модуль, а не ветка в основном потоке

Основной приём ошибок — это **запись факта поломки**: журнал, карточка в Lark,
статусы оборудования. Тестовому стенду ничего из этого не нужно, и любая ошибка
в общей ветке создала бы настоящую запись из тестового фото.

## Настройка

| Переменная | Что |
|---|---|
| `TELEGRAM_SANDBOX_TOPICS` | id топиков через запятую, например `12345` |
| `VISION_URL` | адрес сервера распознавания |
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

#: Топики-песочницы: `12345` или `12345,67890`.
TOPICS = {
    int(value)
    for value in os.environ.get("TELEGRAM_SANDBOX_TOPICS", "").replace(" ", "").split(",")
    if value.strip().isdigit()
}

#: Куда слать фото. Без адреса стенд молчит, а не притворяется работающим.
VISION_URL = os.environ.get("VISION_URL", "").rstrip("/")


def enabled() -> bool:
    """Включён ли стенд: нужны и топик, и адрес сервера."""
    return bool(TOPICS and VISION_URL)


def handles(thread_id) -> bool:
    """Это тестовый топик?"""
    return bool(TOPICS) and thread_id in TOPICS


def identify(photo_path: str) -> dict:
    """Что на фото: модель робота и номер.

    🔴 **Ошибка сети — не «ничего не найдено».** Возвращаем `error`, и бот
    говорит «сервис недоступен», а не «робот не распознан»: это разные вещи, и
    вторая читается как «модель не видит робота».
    """
    if not VISION_URL:
        return {"error": "VISION_URL не настроен"}

    try:
        with open(photo_path, "rb") as handle:
            payload = handle.read()
    except OSError as error:
        return {"error": f"файл не читается: {error}"}

    try:
        response = requests.post(
            f"{VISION_URL}/api/identify",
            data=payload,
            headers={"Content-Type": "image/jpeg"},
            timeout=180,
        )
        if response.status_code != 200:
            return {"error": f"сервис ответил {response.status_code}"}

        return response.json()
    except Exception as error:  # noqa: BLE001 — сеть не должна ронять бота
        logger.warning("sandbox: распознавание не удалось", exc_info=True)
        return {"error": str(error)}


def caption(result: dict) -> str:
    """Ответ для человека — короткий и без технического шума."""
    if result.get("error"):
        return f"⚠️ Сервис распознавания недоступен\n{result['error']}"

    model = result.get("model")
    number = result.get("number")
    exact = result.get("number_exact")

    if not model and not number:
        return "🤷 Не понял, что на фото. Попробуйте снять ближе и при хорошем свете."

    lines = []

    if model:
        percent = int(round((result.get("model_confidence") or 0) * 100))
        lines.append(f"🤖 Модель: {model.upper()} · {percent}%")

    if number:
        lines.append(f"🔢 Номер: {number}" + ("" if exact else " (проверьте)"))
    else:
        lines.append("🔢 Номер не читается")

    if result.get("objects", 0) > 1:
        lines.append(f"👀 Объектов в кадре: {result['objects']}")

    lines.append("\n_Тестовый режим: никуда не отправлено._")

    return "\n".join(lines)
