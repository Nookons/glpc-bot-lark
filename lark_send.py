import requests
import json

from getToken import get_tenant_access_token

def send_text_message(chat_id: str, text: str):
    token = get_tenant_access_token()
    if not token:
        print("❌ Не удалось получить access token")
        return None

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}),
    }
    resp = requests.post(
        "https://open.larksuite.com/open-apis/im/v1/messages?receive_id_type=chat_id",
        headers=headers, json=payload
    )

    result = resp.json()
    print("Status:", resp.status_code)
    print("Response:", result)

    if result.get("code") != 0:
        print(f"❌ Ошибка отправки: {result.get('msg')}")

    return result