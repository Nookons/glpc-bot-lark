import os
import time
import json
import hmac
import hashlib
import base64

import requests

from getToken import get_tenant_access_token
from logging_config import setup_logging
from text_utils import LARK_TEXT_LIMIT, truncate


logger = setup_logging(__name__)

LARK_HOOK_SECRET = os.environ.get("LARK_HOOK_SECRET", "")

def upload_image(image_path: str) -> str:
    """
    Загружает картинку в Lark и возвращает image_key.

    Это единственный вызов Lark API, который расходует квоту, поэтому все
    ошибки заворачиваем в RuntimeError: вызывающий код поймает его и
    отправит фото ссылкой через Supabase Storage.
    """
    token = get_tenant_access_token()

    if not token:
        raise RuntimeError("Не удалось получить tenant_access_token")

    try:
        with open(image_path, "rb") as f:
            resp = requests.post(
                "https://open.larksuite.com/open-apis/im/v1/images",
                headers={"Authorization": f"Bearer {token}"},
                data={"image_type": "message"},
                files={"image": f},
                timeout=30,
            )
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Lark im/v1/images недоступен: {e}") from e
    except OSError as e:
        raise RuntimeError(f"Не удалось прочитать {image_path}: {e}") from e

    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(
            f"Lark im/v1/images вернул не-JSON (HTTP {resp.status_code})"
        )

    if data.get("code") != 0:
        raise RuntimeError(f"Failed to upload image: {data}")

    image_key = (data.get("data") or {}).get("image_key")

    if not image_key:
        raise RuntimeError(f"Lark im/v1/images: пустой image_key: {data}")

    return image_key


def _hook_payload_extra():
    if not LARK_HOOK_SECRET:
        return {}
    timestamp = str(int(time.time()))
    string_to_sign = f"{timestamp}\n{LARK_HOOK_SECRET}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    sign = base64.b64encode(hmac_code).decode("utf-8")
    return {"timestamp": timestamp, "sign": sign}


def _hook_post(url: str, payload: dict):
    """
    POST в webhook с защитой от таймаутов и не-JSON ответов.

    Возвращает dict: при сбое — {"code": -1, ...}, чтобы вызывающий код
    увидел неуспех (hook_ok) и сработал fallback, а не получил исключение.
    """
    try:
        response = requests.post(url, json=payload, timeout=15)
    except requests.exceptions.RequestException as e:
        logger.error("Lark webhook недоступен: %s", e)
        return {"code": -1, "msg": str(e)}

    try:
        return response.json()
    except ValueError:
        logger.error(
            "Lark webhook вернул не-JSON (HTTP %s, %s байт)",
            response.status_code,
            len(response.content or b""),
        )
        return {"code": -1, "msg": f"non-json HTTP {response.status_code}"}


def hook_ok(result) -> bool:
    """Успешен ли ответ webhook: старая схема StatusCode, новая — code."""
    if not isinstance(result, dict):
        return False

    return result.get("code") == 0 or result.get("StatusCode") == 0


def send_card_via_hook(hook_url: str, card: dict):
    """Отправляет интерактивную карточку в группу через webhook."""
    payload = {"msg_type": "interactive", "card": card}
    payload.update(_hook_payload_extra())
    return _hook_post(hook_url, payload)


def send_text_via_hook(hook_url: str, text: str):
    payload = {
        "msg_type": "text",
        "content": {"text": truncate(text, LARK_TEXT_LIMIT)},
    }
    payload.update(_hook_payload_extra())
    return _hook_post(hook_url, payload)


def send_image_via_hook(hook_url: str, image_key: str):
    payload = {"msg_type": "image", "content": {"image_key": image_key}}
    payload.update(_hook_payload_extra())
    return _hook_post(hook_url, payload)


def send_post_via_hook(hook_url: str, image_key: str, text: str):
    payload = {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": "",
                    "content": [
                        [{"tag": "img", "image_key": image_key}],
                        [{"tag": "text", "text": text}],
                    ],
                }
            }
        },
    }
    payload.update(_hook_payload_extra())
    return _hook_post(hook_url, payload)
