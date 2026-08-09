import os
import time
import json
import hmac
import hashlib
import base64

import requests

from getToken import get_tenant_access_token

LARK_HOOK_SECRET = os.environ.get("LARK_HOOK_SECRET", "")

def upload_image(image_path: str) -> str:
    token = get_tenant_access_token()
    with open(image_path, "rb") as f:
        resp = requests.post(
            "https://open.larksuite.com/open-apis/im/v1/images",
            headers={"Authorization": f"Bearer {token}"},
            data={"image_type": "message"},
            files={"image": f},
            timeout=30,
        )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to upload image: {data}")
    return data["data"]["image_key"]


def send_image_message(chat_id: str, image_key: str):
    token = get_tenant_access_token()
    resp = requests.post(
        "https://open.larksuite.com/open-apis/im/v1/messages?receive_id_type=chat_id",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "receive_id": chat_id,
            "msg_type": "image",
            "content": json.dumps({"image_key": image_key}),
        },
        timeout=10,
    )
    return resp.json()


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


def send_text_via_hook(hook_url: str, text: str):
    payload = {"msg_type": "text", "content": {"text": text}}
    payload.update(_hook_payload_extra())
    resp = requests.post(hook_url, json=payload, timeout=10)
    return resp.json()


def send_image_via_hook(hook_url: str, image_key: str):
    payload = {"msg_type": "image", "content": {"image_key": image_key}}
    payload.update(_hook_payload_extra())
    resp = requests.post(hook_url, json=payload, timeout=10)
    return resp.json()


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
    resp = requests.post(hook_url, json=payload, timeout=10)
    return resp.json()


def send_post_with_image_and_text(chat_id: str, image_key: str, text: str):
    token = get_tenant_access_token()
    content = {
        "zh_cn": {
            "title": "",
            "content": [
                [{"tag": "img", "image_key": image_key}],
                [{"tag": "text", "text": text}],
            ],
        }
    }
    resp = requests.post(
        "https://open.larksuite.com/open-apis/im/v1/messages?receive_id_type=chat_id",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "receive_id": chat_id,
            "msg_type": "post",
            "content": json.dumps(content),
        },
        timeout=10,
    )
    return resp.json()