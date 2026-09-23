"""
Загрузка файлов в Supabase Storage.

Нужен как резервный путь для фото: штатная отправка картинки в Lark-группу
требует вызова `im/v1/images`, который расходует квоту Open Platform и
падает, когда квота исчерпана (`code 99991403`). Storage работает по
service-ключу и квоту Lark не трогает — в группу уходит сообщение со ссылкой.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re

import requests

from sendToDataBase import SUPABASE_URL, supabase_headers
from logging_config import setup_logging


logger = setup_logging(__name__)


BUCKET = os.environ.get("SUPABASE_PHOTO_BUCKET", "bot-photos")

# Публичный bucket (проще, ссылка не истекает) или приватный + signed URL.
BUCKET_PUBLIC = os.environ.get(
    "SUPABASE_PHOTO_BUCKET_PUBLIC",
    "false",
).strip().lower() in ("1", "true", "yes", "on")

# Время жизни signed URL (по умолчанию 1 год).
SIGNED_URL_TTL = int(os.environ.get("SUPABASE_SIGNED_URL_TTL", "31536000"))

# Кэш созданных bucket'ов: {имя: bool}
_buckets_ok = {}


def _safe_object_name(name: str) -> str:
    """Оставляет в имени только безопасные символы."""
    base = os.path.basename(name or "")
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)

    return base or "photo.jpg"


def ensure_bucket_named(bucket: str, public: bool = False) -> bool:
    """Создаёт bucket с указанным именем, если его ещё нет."""
    if _buckets_ok.get(bucket):
        return True

    url = f"{SUPABASE_URL}/storage/v1/bucket"

    try:
        response = requests.post(
            url,
            headers=supabase_headers(),
            json={"id": bucket, "name": bucket, "public": public},
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        logger.error("Storage: не удалось создать bucket %s: %s", bucket, e)
        return False

    # 400/409 — bucket уже существует.
    if response.status_code in (200, 201, 400, 409):
        _buckets_ok[bucket] = True
        return True

    logger.error(
        "Storage: bucket %s -> HTTP %s %s",
        bucket,
        response.status_code,
        response.text[:200],
    )
    return False


def ensure_bucket() -> bool:
    """Bucket для фото."""
    return ensure_bucket_named(BUCKET, BUCKET_PUBLIC)


def upload_json(bucket: str, name: str, payload: dict) -> bool:
    """Кладёт JSON-объект в bucket (перезаписывая)."""
    if not ensure_bucket_named(bucket, public=False):
        return False

    url = f"{SUPABASE_URL}/storage/v1/object/{bucket}/{name}"

    headers = supabase_headers()
    headers["Content-Type"] = "application/json"
    headers["x-upsert"] = "true"

    try:
        response = requests.post(
            url,
            headers=headers,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=20,
        )
    except requests.exceptions.RequestException as e:
        logger.error("Storage: не удалось записать %s/%s: %s", bucket, name, e)
        return False

    if response.status_code not in (200, 201):
        logger.error(
            "Storage: запись %s/%s -> HTTP %s %s",
            bucket,
            name,
            response.status_code,
            response.text[:200],
        )
        return False

    return True


def download_json(bucket: str, name: str):
    """Читает JSON-объект из bucket. None — нет объекта или ошибка."""
    if not ensure_bucket_named(bucket, public=False):
        return None

    for url in (
        f"{SUPABASE_URL}/storage/v1/object/{bucket}/{name}",
        f"{SUPABASE_URL}/storage/v1/object/authenticated/{bucket}/{name}",
    ):
        try:
            response = requests.get(url, headers=supabase_headers(), timeout=15)
        except requests.exceptions.RequestException as e:
            logger.error("Storage: не удалось прочитать %s/%s: %s", bucket, name, e)
            return None

        if response.status_code == 200:
            try:
                return json.loads(response.content.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                logger.error("Storage: повреждён JSON %s/%s", bucket, name)
                return None

        if response.status_code == 404:
            return None

    return None


def upload_file(local_path: str, object_name: str = None) -> str:
    """
    Загружает файл в bucket. Возвращает имя объекта или None.
    """
    if not ensure_bucket():
        return None

    object_name = _safe_object_name(object_name or local_path)

    try:
        with open(local_path, "rb") as f:
            content = f.read()
    except OSError as e:
        logger.error("Storage: не удалось прочитать %s: %s", local_path, e)
        return None

    content_type = mimetypes.guess_type(object_name)[0] or "application/octet-stream"

    headers = supabase_headers()
    headers["Content-Type"] = content_type
    headers["x-upsert"] = "true"
    headers["cache-control"] = "max-age=31536000"

    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{object_name}"

    try:
        response = requests.post(url, headers=headers, data=content, timeout=60)
    except requests.exceptions.RequestException as e:
        logger.error("Storage: ошибка загрузки %s: %s", object_name, e)
        return None

    if response.status_code not in (200, 201):
        logger.error(
            "Storage: загрузка %s -> HTTP %s %s",
            object_name,
            response.status_code,
            response.text[:200],
        )
        return None

    logger.info("Storage: загружено %s (%s байт)", object_name, len(content))

    return object_name


def public_url(object_name: str) -> str:
    return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{object_name}"


def create_signed_url(object_name: str, expires_in: int = None):
    """
    Подписанная ссылка на приватный объект или None.

    Supabase отдаёт относительный путь вида /object/sign/<bucket>/<path>?token=...
    """
    ttl = int(expires_in or SIGNED_URL_TTL)

    url = f"{SUPABASE_URL}/storage/v1/object/sign/{BUCKET}/{object_name}"

    try:
        response = requests.post(
            url,
            headers=supabase_headers(),
            json={"expiresIn": ttl},
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        logger.error("Storage: не удалось подписать ссылку: %s", e)
        return None

    if response.status_code not in (200, 201):
        logger.error(
            "Storage: sign %s -> HTTP %s %s",
            object_name,
            response.status_code,
            response.text[:200],
        )
        return None

    try:
        payload = response.json()
    except ValueError:
        payload = {}

    signed = payload.get("signedURL") or payload.get("signedUrl")

    if not signed:
        logger.error("Storage: пустой signedURL в ответе")
        return None

    if signed.startswith("http"):
        return signed

    return f"{SUPABASE_URL}/storage/v1{signed}"


def upload_photo_and_get_url(local_path: str, object_name: str = None):
    """
    Загружает фото и возвращает ссылку для Lark-группы.

    Публичный bucket -> постоянная ссылка, приватный -> signed URL.
    """
    uploaded = upload_file(local_path, object_name)

    if not uploaded:
        return None

    if BUCKET_PUBLIC:
        return public_url(uploaded)

    return create_signed_url(uploaded)
