"""
Пересылка в целевую Lark-группу через custom-bot webhook.

Важно: webhook группы (bot/v2/hook) НЕ расходует квоту Open Platform —
в отличие от отправки сообщений через im/v1/messages с tenant-токеном.
Поэтому весь вывод бота идёт сюда.

Единственное исключение — фото: чтобы отправить картинку в webhook,
её сначала нужно загрузить через im/v1/images (1 вызов API на фото).
"""

from lark_hooks import TARGET_HOOK_URL, error_hook
from lark_media import (
    hook_ok,
    send_image_via_hook,
    send_post_via_hook,
    send_text_via_hook,
    upload_image,
)
from supabase_storage import upload_photo_and_get_url
from text_utils import VALUE_LIMIT, truncate
from logging_config import setup_logging


logger = setup_logging(__name__)


# TARGET_HOOK_URL — общий вебхук (отчёты и запасной вариант). Ошибки уходят
# в вебхук своего склада: lark_hooks.error_hook(warehouse).
DEFAULT_TARGET_HOOK_URL = TARGET_HOOK_URL


# Оставлено для обратной совместимости: реализация живёт в lark_media.
_hook_ok = hook_ok


def send_photo(
    image_path: str,
    caption: str = None,
    console=None,
    warehouse: str = None,
) -> dict:
    """
    Отправляет фото в целевую группу Lark.

    Штатный путь: загрузка картинки в Lark (im/v1/images) + отправка вебхуком.

    Если Lark API недоступен — например, исчерпана квота (code 99991403) —
    фото уходит в Supabase Storage, а в группу отправляется сообщение со
    ссылкой. В этом режиме квота Lark не расходуется вообще.

    Возвращает {"mode": "lark"|"link"|"none", "url": <ссылка или None>}:
    url нужен, чтобы привязать фото к записи об ошибке.
    """
    target = error_hook(warehouse)
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
            result = send_post_via_hook(target, image_key, caption)
        else:
            result = send_image_via_hook(target, image_key)

        if _hook_ok(result):
            logger.info("Photo forwarded to Lark group: %s", image_path)
            return {"mode": "lark", "url": None}

        logger.error("Failed to send photo to Lark hook: %s", result)

    # ------------------------------------------------------------
    # Резервный путь: Supabase Storage + ссылка (квота Lark не тратится)
    # ------------------------------------------------------------

    link = upload_photo_and_get_url(image_path)

    if not link:
        logger.error("Photo fallback failed: ссылку получить не удалось")
        return {"mode": "none", "url": None}

    text = f"{caption}\n🔗 {link}" if caption else f"📷 Photo\n🔗 {link}"

    result = send_text_via_hook(target, text)

    if not _hook_ok(result):
        logger.error("Failed to send photo link to Lark hook: %s", result)
        return {"mode": "none", "url": link}

    logger.info("Photo sent to Lark group as link: %s", image_path)

    return {"mode": "link", "url": link}


def handle_incoming_photo(
    image_path: str,
    console=None,
    caption: str = None,
    warehouse: str = None,
) -> str:
    """Совместимая обёртка: возвращает только режим доставки."""
    return send_photo(image_path, caption, console, warehouse=warehouse)["mode"]


def send_error_with_photo(
    parsed: dict,
    table_lines=None,
    photo_path: str = None,
    photo_url: str = None,
    warehouse: str = None,
) -> str:
    """
    Отправляет карточку ошибки вместе с фото.

    Сначала пробует картинкой (нужна квота Lark), иначе текстом со ссылкой.
    Возвращает "lark" | "link" | "text" | "none".
    """
    target = error_hook(warehouse)
    plain_line = f"{parsed['error_type']}: {parsed['error_text']}. {parsed['robot']}"

    if table_lines:
        text_block = "\n".join(
            f"{label}: {truncate(value, VALUE_LIMIT)}"
            for label, value in table_lines
        )
    else:
        text_block = truncate(plain_line, VALUE_LIMIT)

    if photo_path:
        try:
            image_key = upload_image(photo_path)
        except Exception as e:
            logger.warning(
                "Lark im/v1/images недоступен (%s) — фото уйдёт ссылкой",
                e,
            )
            image_key = None

        if image_key:
            if _hook_ok(send_post_via_hook(target, image_key, text_block)):
                logger.info(
                    "Ошибка с фото отправлена в Lark картинкой: robot=%s",
                    parsed.get("robot"),
                )
                return "lark"

    if photo_url:
        result = send_text_via_hook(
            target,
            f"{text_block}\n🔗 {photo_url}",
        )

        if _hook_ok(result):
            logger.info(
                "Ошибка с фото отправлена в Lark ссылкой: robot=%s",
                parsed.get("robot"),
            )
            return "link"

        logger.error("Failed to send error with photo to Lark: %s", result)
        return "none"

    return "text" if forward_error(parsed, table_lines, warehouse) else "none"


def forward_error(parsed: dict, table_lines=None, warehouse: str = None) -> bool:
    """Отправляет карточку ошибки в группу Lark своего склада."""
    plain_line = f"{parsed['error_type']}: {parsed['error_text']}. {parsed['robot']}"

    if table_lines:
        text_block = "\n".join(
            f"{label}: {truncate(value, VALUE_LIMIT)}"
            for label, value in table_lines
        )
    else:
        text_block = truncate(plain_line, VALUE_LIMIT)

    result = send_text_via_hook(error_hook(warehouse), text_block)

    if not _hook_ok(result):
        logger.error("Failed to forward error to Lark hook: %s", result)
        return False

    logger.info(
        "Error forwarded to Lark group: robot=%s",
        parsed.get("robot"),
    )

    return True
