# ==========================
# USER INFO
# ==========================
import requests
import json
from getToken import get_tenant_access_token

def get_user_name(user_id, console):
    token = get_tenant_access_token()

    headers = {
        "Authorization": f"Bearer {token}"
    }

    url = (
        f"https://open.larksuite.com/open-apis/"
        f"contact/v3/users/{user_id}"
    )

    r = requests.get(
        url,
        headers=headers,
        params={
            "user_id_type": "user_id",
            "fields": "name,avatar"
        }
    )

    data = r.json()

    console.print(
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False
        )
    )

    if data.get("code") != 0:
        return user_id


    user = (
        data
        .get("data", {})
        .get("user", {})
    )

    return (
        user.get("name")
        or user.get("en_name")
        or user_id
    )