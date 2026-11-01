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


#: Имена топиков, которые тоже считаются стендом (без учёта регистра).
#:
#: 🔴 **Зачем имя, а не только id.** 01.11.2026 фото уходили в топик `2`
#: (General), потому что человек писал в общем потоке, — и стенд молчал, хотя
#: всё настроено. Имя переживает пересоздание топика и понятно в настройке.
NAME_MARKERS = ("sandbox", "тест", "test", "ai ")


def handles(thread_id, name: str | None = None) -> bool:
    """Это тестовый топик — по id **или** по имени?"""
    if thread_id is not None and thread_id in TOPICS:
        return True

    text = (name or "").strip().lower()

    return bool(text) and any(marker in text for marker in NAME_MARKERS)


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
            # 🔴 **`ngrok-skip-browser-warning`.** Туннель отдаёт HTML-заглушку
            # вместо JSON, если этого заголовка нет, — и бот получал бы «не
            # распознано» вместо ответа модели.
            headers={
                "Content-Type": "image/jpeg",
                "ngrok-skip-browser-warning": "1",
            },
            timeout=180,
        )
        if response.status_code != 200:
            return {"error": f"сервис ответил {response.status_code}"}

        return response.json()
    except Exception as error:  # noqa: BLE001 — сеть не должна ронять бота
        logger.warning("sandbox: распознавание не удалось", exc_info=True)
        return {"error": str(error)}


def save_annotated(result: dict, source_path: str) -> str | None:
    """Сохраняет размеченное фото рядом с исходным и возвращает путь.

    🔴 **Картинку рисует сервер распознавания**, а не бот: PIL-пайплайн и палитра
    классов живут там, и второй рисовальщик разошёлся бы с первым при первой
    правке цветов.
    """
    import base64
    import os

    payload = result.get("annotated")

    if not payload:
        return None

    try:
        destination = os.path.splitext(source_path)[0] + "_annotated.jpg"
        with open(destination, "wb") as handle:
            handle.write(base64.b64decode(payload))

        return destination
    except Exception:  # noqa: BLE001 — без картинки ответ всё равно уходит
        logger.warning("sandbox: не удалось сохранить обводку", exc_info=True)
        return None


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
