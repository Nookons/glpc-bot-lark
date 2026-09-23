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
import time

import requests

from env_utils import env_int
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
SIGNED_URL_TTL = env_int("SUPABASE_SIGNED_URL_TTL", 31536000)

# Кэш созданных bucket'ов: {имя: bool}
_buckets_ok = {}

# Короткие ссылки: https://<наш домен>/p/<файл> вместо длинного signed URL.
PHOTO_LINK_BASE = os.environ.get(
    "PHOTO_LINK_BASE",
    "https://glpc-bot-lark-production.up.railway.app",
).rstrip("/")

# true — отдавать публичные ссылки Supabase (bucket становится публичным),
# false — короткая ссылка на наш /p/<файл> (bucket остаётся приватным).
PUBLIC_PHOTO_URLS = os.environ.get(
    "PUBLIC_PHOTO_URLS",
    "false",
).strip().lower() in ("1", "true", "yes", "on")

# Кэш подписанных ссылок: файл -> (url, годен до).
_signed_cache = {}

# Негативный кэш: файл -> момент, раньше которого не пробуем подписывать снова.
# Без него любой запрос к /p/<несуществующий файл> бил бы в Supabase.
_signed_missing = {}

# Ограничение размера кэшей: имена файлов приходят из интернета, поэтому
# неограниченный словарь — это утечка памяти.
_SIGNED_CACHE_MAX = env_int("SIGNED_URL_CACHE_MAX", 512)

_SIGNED_MISS_TTL_SECONDS = env_int("SIGNED_URL_MISS_TTL", 300)

_SIGNED_CACHE_SECONDS = 3600


class StorageUnavailable(Exception):
    """Supabase Storage недоступен (сеть/5xx) — это не «файла нет»."""


def _cache_put(cache: dict, key, value) -> None:
    """Кладёт значение в кэш с вытеснением самых старых записей."""
    cache.pop(key, None)
    cache[key] = value

    while len(cache) > _SIGNED_CACHE_MAX:
        cache.pop(next(iter(cache)))


def _safe_object_name(name: str) -> str:
    """Оставляет в имени только безопасные символы."""
    base = os.path.basename(name or "")
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)

    return base or "photo.jpg"


def _set_bucket_public(bucket: str) -> bool:
    """
    Делает существующий bucket публичным.

    POST /storage/v1/bucket на существующий bucket отвечает 409 и видимость
    НЕ меняет, поэтому при PUBLIC_PHOTO_URLS=true нужен явный PUT — иначе
    ссылки public/... отдают 403, а бот считает, что фото доставлено.
    """
    url = f"{SUPABASE_URL}/storage/v1/bucket/{bucket}"

    try:
        response = requests.put(
            url,
            headers=supabase_headers(),
            json={"public": True},
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        logger.error(
            "Storage: не удалось сделать bucket %s публичным: %s", bucket, e
        )
        return False

    if response.status_code in (200, 201):
        logger.info("Storage: bucket %s переведён в public", bucket)
        return True

    logger.error(
        "Storage: bucket %s public -> HTTP %s %s",
        bucket,
        response.status_code,
        response.text[:200],
    )
    return False


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

    # 200/201 — создали.
    if response.status_code in (200, 201):
        _buckets_ok[bucket] = True
        return True

    # 409 — bucket уже существует. POST видимость не меняет: если нужен
    # публичный доступ, переводим bucket в public явно.
    if response.status_code == 409:
        if public and not _set_bucket_public(bucket):
            return False

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
    return ensure_bucket_named(BUCKET, BUCKET_PUBLIC or PUBLIC_PHOTO_URLS)


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


def create_signed_url(object_name: str, expires_in: int = None, strict: bool = False):
    """
    Подписанная ссылка на приватный объект или None.

    Supabase отдаёт относительный путь вида /object/sign/<bucket>/<path>?token=...

    strict=True — сбой сервиса (сеть/5xx/пустой ответ) бросает
    StorageUnavailable вместо None: «файла нет» и «Storage лежит» — это
    разные ответы для короткой ссылки /p/<файл> (404 против 503).
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

        if strict:
            raise StorageUnavailable(str(e)) from e

        return None

    if response.status_code not in (200, 201):
        # Отсутствующий объект (старая или битая ссылка) — не ошибка сервиса.
        if response.status_code in (400, 404) and "not_found" in response.text:
            logger.info("Storage: файла %s нет (ссылка устарела?)", object_name)
            return None

        logger.error(
            "Storage: sign %s -> HTTP %s %s",
            object_name,
            response.status_code,
            response.text[:200],
        )

        if strict:
            raise StorageUnavailable(
                f"HTTP {response.status_code}: {response.text[:200]}"
            )

        return None

    try:
        payload = response.json()
    except ValueError:
        payload = {}

    signed = payload.get("signedURL") or payload.get("signedUrl")

    if not signed:
        logger.error("Storage: пустой signedURL в ответе")

        if strict:
            raise StorageUnavailable("пустой signedURL в ответе")

        return None

    if signed.startswith("http"):
        return signed

    return f"{SUPABASE_URL}/storage/v1{signed}"


def short_photo_url(object_name: str) -> str:
    """Короткая ссылка на фото: <наш домен>/p/<файл>."""
    return f"{PHOTO_LINK_BASE}/p/{object_name}"


def resolve_photo_url(object_name: str, strict: bool = False):
    """
    Ссылка, на которую ведёт короткий адрес: подписанный URL Supabase.

    Подписанные ссылки кэшируются на час, чтобы клик по короткой ссылке
    не дёргал Supabase каждый раз. Неудачи кэшируются ненадолго (негативный
    кэш): иначе перебор имён файлов — это бесплатная нагрузка на Storage.

    strict=True — сбой Storage бросает StorageUnavailable (для /p/ это 503);
    None в этом режиме означает именно «файла нет».
    """
    now = time.time()

    cached = _signed_cache.get(object_name)

    if cached:
        if cached[1] > now:
            # Освежаем позицию в кэше (LRU), а не только факт попадания.
            _cache_put(_signed_cache, object_name, cached)
            return cached[0]

        _signed_cache.pop(object_name, None)

    retry_after = _signed_missing.get(object_name)

    if retry_after and retry_after > now:
        return None

    try:
        url = create_signed_url(object_name, strict=strict)
    except StorageUnavailable:
        # Транзиентный сбой: запоминаем коротко, чтобы не долбить Storage,
        # но и не выдавать 404 вместо 503.
        _cache_put(_signed_missing, object_name, now + _SIGNED_MISS_TTL_SECONDS)
        raise

    if url:
        _cache_put(
            _signed_cache,
            object_name,
            (url, now + _SIGNED_CACHE_SECONDS),
        )
        _signed_missing.pop(object_name, None)
    else:
        _cache_put(_signed_missing, object_name, now + _SIGNED_MISS_TTL_SECONDS)

    return url


def photo_url_for_object(object_name: str):
    """Ссылка для показа пользователю: публичная или короткая."""
    if PUBLIC_PHOTO_URLS:
        return public_url(object_name)

    return short_photo_url(object_name)


def upload_photo_and_get_url(local_path: str, object_name: str = None):
    """
    Загружает фото и возвращает короткую ссылку для Lark-группы.

    При включённом PUBLIC_PHOTO_URLS — публичную ссылку Supabase.
    """
    uploaded = upload_file(local_path, object_name)

    if not uploaded:
        return None

    return photo_url_for_object(uploaded)
