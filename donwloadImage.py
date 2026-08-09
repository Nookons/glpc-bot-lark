# ==========================
# DOWNLOAD IMAGE
# ==========================
import os
import requests

from getToken import get_tenant_access_token


def download_image(image_key, message_id, console):
    token = get_tenant_access_token()

    headers = {
        "Authorization": f"Bearer {token}"
    }

    url = (
        "https://open.larksuite.com/"
        f"open-apis/im/v1/messages/{message_id}/resources/{image_key}"
    )

    response = requests.get(
        url,
        headers=headers,
        params={"type": "image"},
    )

    if response.status_code != 200:
        console.print(
            "[red]Ошибка скачивания изображения[/red]"
        )
        console.print(response.text)
        return None

    os.makedirs(
        "images",
        exist_ok=True
    )

    filename = (
        f"images/{image_key}.png"
    )

    with open(
        filename,
        "wb"
    ) as f:
        f.write(response.content)

    return filename