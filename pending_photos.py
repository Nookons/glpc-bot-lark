"""
Пересылка в целевую Lark-группу через custom-bot webhook.

Важно: webhook группы (bot/v2/hook) НЕ расходует квоту Open Platform —
в отличие от отправки сообщений через im/v1/messages с tenant-токеном.
Поэтому весь вывод бота идёт сюда.

Единственное исключение — фото: чтобы отправить картинку в webhook,
её сначала нужно загрузить через im/v1/images (1 вызов API на фото).
"""

import os

from lark_media import (
    hook_ok,
    send_image_via_hook,
    send_post_via_hook,
    send_text_via_hook,
    upload_image,
)
from supabase_storage import upload_photo_and_get_url
from logging_config import setup_logging


logger = setup_logging(__name__)


# Webhook бота целевой группы Lark. Можно переопределить через .env.
DEFAULT_TARGET_HOOK_URL = (
    "https://open.larksuite.com/open-apis/bot/v2/hook/"
    "dc2c430d-ce07-4ff2-b5ca-0b92feb4f62a"
)

TARGET_HOOK_URL = os.environ.get(
    "LARK_TARGET_HOOK_URL",
    DEFAULT_TARGET_HOOK_URL,
)


# Оставлено для обратной совместимости: реализация живёт в lark_media.
_hook_ok = hook_ok


def handle_incoming_photo(image_path: str, console=None, caption: str = None) -> str:
    """
    Отправляет фото в целевую группу Lark.

    Штатный путь: загрузка картинки в Lark (im/v1/images) + отправка вебхуком.

    Если Lark API недоступен — например, исчерпана квота (code 99991403) —
    фото уходит в Supabase Storage, а в группу отправляется сообщение со
    ссылкой. В этом режиме квота Lark не расходуется вообще.

    caption — необязательная подпись (например, «Фото от Ивана»).

    Возвращает: "lark" — картинкой, "link" — ссылкой, "none" — не удалось.
    """
    image_key = None

    try:
        image_key = upload_image(image_path)
    except Exception as e:
        logger.warning(
            "Lark im/v1/images недоступен (%s) — пересылаю фото ссылкой "
            "через Supabase Storage",
            e,
        )

        if console:
            console.print(
                f"[yellow]Lark upload failed, fallback to Storage link: {e}[/yellow]"
            )

    if image_key:
        if caption:
            result = send_post_via_hook(TARGET_HOOK_URL, image_key, caption)
        else:
            result = send_image_via_hook(TARGET_HOOK_URL, image_key)

        if _hook_ok(result):
            logger.info("Photo forwarded to Lark group: %s", image_path)
            return "lark"

        logger.error("Failed to send photo to Lark hook: %s", result)

    # ------------------------------------------------------------
    # Резервный путь: Supabase Storage + ссылка (квота Lark не тратится)
    # ------------------------------------------------------------

    link = upload_photo_and_get_url(image_path)

    if not link:
        logger.error("Photo fallback failed: ссылку получить не удалось")
        return "none"

    text = f"{caption}\n🔗 {link}" if caption else f"📷 Photo\n🔗 {link}"

    result = send_text_via_hook(TARGET_HOOK_URL, text)

    if not _hook_ok(result):
        logger.error("Failed to send photo link to Lark hook: %s", result)
        return "none"

    logger.info("Photo sent to Lark group as link: %s", image_path)

    return "link"


def forward_error(parsed: dict, table_lines=None) -> bool:
    """Отправляет карточку ошибки в целевую группу Lark."""
    plain_line = f"{parsed['error_type']}: {parsed['error_text']}. {parsed['robot']}"

    if table_lines:
        text_block = "\n".join(f"{label}: {value}" for label, value in table_lines)
    else:
        text_block = plain_line

    result = send_text_via_hook(TARGET_HOOK_URL, text_block)

    if not _hook_ok(result):
        logger.error("Failed to forward error to Lark hook: %s", result)
        return False

    logger.info(
        "Error forwarded to Lark group: robot=%s",
        parsed.get("robot"),
    )

    return True
