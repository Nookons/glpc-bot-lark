"""
═══════════════════════════════════════════════════════════════════════════════
РАСПОЗНАВАНИЕ ОБОРУДОВАНИЯ НА ФОТО
═══════════════════════════════════════════════════════════════════════════════

🔴 **Требование владельца 31.10.2026:** «сделать AI который будет искать
маленького робота на фото (K50H = MINI_HAIFLEX), и если получилось найти, то сразу
помечать, что это робот, и спрашивать номер».

## Зачем

Сейчас каждый разбор ошибки начинается с вопроса «Which equipment?». Оператор
снимал робота и всё равно выбирает «Robot» — ответ есть на самом фото.

## Как это работает

Фото уходит в **модель со зрением** вместе со строгим заданием вернуть JSON:
что на снимке и насколько уверенно. Ответ разбирается здесь, а не в диалоге:
диалог должен получить либо готовый вид оборудования, либо «не знаю».

## 🔴 Чего этот модуль НЕ делает

- **Не угадывает.** Пустой ответ, ошибка сети, нет ключа, уверенность ниже
  порога — возвращается `None`, и бот спрашивает как раньше.
- **Не пишет в отчёт.** Результат только **предзаполняет** первый шаг; всё
  остальное оператор подтверждает сам.
- **Не работает без ключа.** Ключа в проекте нет — пока его не добавят,
  модуль молча отключается (`available()` → `False`), и поведение бота не
  меняется ни на йоту.

## Настройка

| Переменная | Что |
|---|---|
| `VISION_API_KEY` | ключ; без него распознавание выключено |
| `VISION_BASE_URL` | по умолчанию `https://api.openai.com/v1` (любой OpenAI-совместимый) |
| `VISION_MODEL` | по умолчанию `gpt-4o-mini` |
| `VISION_MIN_CONFIDENCE` | порог уверенности, по умолчанию `0.75` |

**Стоимость:** один снимок ≈ 0.001–0.003 $, то есть сотни разборов в месяц —
порядка доллара.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Подпись вида оборудования в дереве приёма ошибок.
KIND_BY_CATEGORY = {
    "robot": "robot",
    "charging": "charger",
    "workstation": "workstation",
}

#: Модели, которые бот умеет назвать. Остальное — «не знаю»: подставить чужую
#: модель хуже, чем промолчать, потому что по ней выберется чек-лист и модули.
MODEL_NAMES = {
    "K50H": "RT_KUBOT_MINI_HAIFLEX",
    "MINI_HAIFLEX": "RT_KUBOT_MINI_HAIFLEX",
    "A42T": "RT_KUBOT_E2",
    "A42T C2": "RT_KUBOT_E2",
    "E2": "RT_KUBOT_E2",
}

#: Задание модели. Короткое и с жёсткой схемой ответа: длинная инструкция
#: повышает шанс рассуждения вместо JSON.
PROMPT = """You are looking at a photo from a warehouse with HAIPICK robots.
Classify the main piece of equipment in the photo.

Answer with JSON only, no prose:
{"kind": "robot|charger|workstation|qr|other", "model": "K50H|A42T|A42T C2|unknown", "confidence": 0.0-1.0}

Rules:
- "robot": the small blue autonomous mobile robot (K50H / MINI_HAIFLEX) or the
  taller A42T carrier. "model" names which one.
- "charger": a charging station (a dock, usually with a green light).
- "workstation": a picking/conveyor station, blue metal frame, no wheels.
- "qr": a QR code sticker filling the frame.
- "other": anything else, including photos where the equipment is not visible.
- If you cannot tell the model, use "unknown" — do not guess."""


def _config() -> dict[str, str]:
    """Настройки на момент вызова: их меняют без перезапуска."""
    return {
        "key": os.environ.get("VISION_API_KEY", "").strip(),
        "base": os.environ.get("VISION_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        "model": os.environ.get("VISION_MODEL", "gpt-4o-mini").strip(),
        "min_confidence": os.environ.get("VISION_MIN_CONFIDENCE", "0.75").strip(),
    }


def available() -> bool:
    """Включено ли распознавание. Без ключа — нет, и это нормальный режим."""
    return bool(_config()["key"])


def _threshold() -> float:
    try:
        value = float(_config()["min_confidence"])
    except ValueError:
        return 0.75

    return min(max(value, 0.0), 1.0)


def _encode(image_path: str) -> Optional[str]:
    """Фото в data-URL. `None` — файла нет или он не читается."""
    try:
        with open(image_path, "rb") as handle:
            payload = handle.read()
    except OSError:
        logger.warning("vision: не удалось прочитать фото %r", image_path)

        return None

    if not payload:
        return None

    return "data:image/jpeg;base64," + base64.b64encode(payload).decode("ascii")


def identify(image_path: str) -> Optional[dict[str, Any]]:
    """Что на фото. `None` — не знаем (нет ключа, ошибка, низкая уверенность).

    Возвращает `{"kind": "robot", "model": "RT_KUBOT_MINI_HAIFLEX",
    "confidence": 0.93}` — то, чем можно предзаполнить первый шаг диалога.
    """
    config = _config()

    if not config["key"]:
        return None

    data_url = _encode(image_path)

    if data_url is None:
        return None

    # 🔴 **`requests`, а не `httpx`:** весь бот уже ходит через `requests`
    # (`requirements.txt`), а `httpx` в зависимостях не объявлен. Второй HTTP-клиент
    # ради одного вызова дал бы `ImportError` в проде — и распознавание молча
    # выключилось бы там, где ключ есть.
    import requests

    body = {
        "model": config["model"],
        "temperature": 0,
        "max_tokens": 120,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }

    try:
        response = requests.post(
            f"{config['base']}/chat/completions",
            headers={
                "Authorization": f"Bearer {config['key']}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=25.0,
        )
        response.raise_for_status()
        answer = response.json()["choices"][0]["message"]["content"]
    except Exception:  # noqa: BLE001 — распознавание не должно ломать приём фото
        logger.warning("vision: запрос не удался", exc_info=True)

        return None

    return _parse(answer)


def _parse(answer: str) -> Optional[dict[str, Any]]:
    """Разбор ответа модели. Любая неожиданность — `None`, а не исключение."""
    text = str(answer or "").strip()

    # Модель иногда оборачивает JSON в ```-блок, несмотря на просьбу.
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1] if "\n" in text else text

    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        logger.warning("vision: ответ не JSON: %r", text[:200])

        return None

    if not isinstance(payload, dict):
        return None

    kind = str(payload.get("kind") or "").strip().lower()

    if kind not in KIND_BY_CATEGORY:
        return None

    try:
        confidence = float(payload.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0

    if confidence < _threshold():
        logger.info(
            "vision: %s с уверенностью %.2f — ниже порога, спрашиваем как раньше",
            kind,
            confidence,
        )

        return None

    result: dict[str, Any] = {
        "kind": KIND_BY_CATEGORY[kind],
        "confidence": confidence,
    }

    if kind == "robot":
        raw_model = str(payload.get("model") or "").strip()
        model = MODEL_NAMES.get(raw_model.upper())
        # 🔴 Незнакомая модель — только вид, без модели: подставить чужую
        # значит выбрать не тот чек-лист.
        if model:
            result["model"] = model

    return result
