import time
import requests

from config import APP_ID, APP_SECRET
from logging_config import setup_logging

logger = setup_logging(__name__)

tenant_token = None
tenant_token_expire = 0


def get_tenant_access_token():
    global tenant_token, tenant_token_expire

    if tenant_token and time.time() < tenant_token_expire:
        return tenant_token

    url = (
        "https://open.larksuite.com/open-apis/auth/v3/"
        "tenant_access_token/internal"
    )

    try:
        r = requests.post(
            url,
            json={
                "app_id": APP_ID,
                "app_secret": APP_SECRET,
            },
            timeout=10,
        )
    except requests.exceptions.RequestException as e:
        logger.error("Failed to get tenant access token: %s", e)
        return None

    data = r.json()

    if data.get("code") != 0:
        logger.error(
            "Lark token error: code=%s msg=%s",
            data.get("code"),
            data.get("msg"),
        )
        return None

    tenant_token = data["tenant_access_token"]
    tenant_token_expire = time.time() + data["expire"] - 60

    return tenant_token
