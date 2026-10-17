"""
Клиент API осмотров (`tk-assist-api`) для бота.

**Почему отдельный модуль, а не запросы прямо в диалоге.**
Диалог осмотра (`inspection.py`) занят шагами и текстами, а здесь живёт ровно
одна вещь: как поговорить с API. Так подмена «API недоступен» в тестах не
требует трогать логику шагов, а все сетевые ошибки переводятся в один
понятный человеку текст в одном месте.

─── 🔴 Почему бот ходит в API, а не в PostgREST напрямую ────────────────────

Приём ошибок пишет в Supabase напрямую сервисным ключом. Для осмотра этот путь
**не годится**, и вот почему.

Осмотр — это не одна строка, а сущность с правилами: пункты копируются из
шаблона, `failed` требует комментария, закрыть осмотр без вердиктов по всем
пунктам нельзя, вердикт считается по пунктам, публичную ссылку выдаёт только
завершённый осмотр. Все эти правила живут в `InspectionService`. Вторая их
реализация в боте разошлась бы с первой — в проекте это уже дважды случалось на
«общих» функциях (см. сторож `test_cross_repo_parity.py`).

Поэтому бот **не дублирует правила**, а пользуется ручками API. Побочный
выигрыш: авторство (`assigned_by`/`done_by`) и проверка склада проставляются
сервером, а не приходят из тела запроса.

─── Как бот подтверждает право ──────────────────────────────────────────────

Требованием владельца «в инспекции нужно помнить кто и когда её делал» право
должно быть **личным**, а не общим на весь бот: иначе все осмотры запишутся на
одного служебного сотрудника, и «кто делал» перестанет быть правдой.

API (ADR-0005) принимает **Supabase JWT**, а в `employees` у каждого работника
уже есть `auth_id`. Поэтому бот выпускает короткоживущий токен **от имени
работника**: `sub` = его `auth_id`. Это тот же способ, которым ходит веб, и он
не требует ни новых таблиц, ни нового секрета в API:

  * JWT подписывается `SUPABASE_JWT_SECRET` — тем же, которым API проверяет
    подпись (в проде проверено: токен, выпущенный этим секретом, принимается);
  * склад, роль и `card_id` API берёт **из базы** по `auth_id`, а не из токена,
    поэтому подделать автора или осмотреть чужой склад нельзя;
  * срок жизни — минуты, и токен выпускается на одну операцию.

Пустой `SUPABASE_JWT_SECRET` (или работник без `auth_id`) — это честный отказ
«осмотр недоступен», а не тихая запись без автора.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from logging_config import setup_logging


# **`.env` читаем сами, не полагаясь на порядок импортов.**
#
# Тот же приём (`override=False`), что в `sendToDataBase.py`: уже заданная
# переменная окружения (Railway, тест) обязана побеждать файл. Без этого
# вызова `inspection_api`, импортированный первым, прочитал бы пустые значения
# и навсегда залип бы с ними — «осмотр не настроен» без причины.
load_dotenv(override=False)

logger = setup_logging(__name__)

#: Тот же секрет, которым API проверяет подпись токена Supabase.
JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET", "").strip()

#: Адрес API. Пусто — осмотр выключен (см. `api_configured`), а не «ушки в
#: пустоту»: молчаливый отказ лучше, чем запрос на localhost в проде.
API_BASE = os.environ.get("TK_ASSIST_API_URL", "").strip().rstrip("/")

#: Сколько живёт выпущенный токен. Минуты, а не часы: токен выпускается на
#: одну операцию, и украденный из лога он почти сразу негоден.
TOKEN_TTL_SECONDS = int(os.environ.get("TK_ASSIST_TOKEN_TTL", "300") or "300")

#: Таймауты. Загрузка фото дольше: файл до 5 МБ уходит через интернет.
DEFAULT_TIMEOUT = 20
PHOTO_TIMEOUT = 60


class InspectionApiError(RuntimeError):
    """API отказал. `message` — то, что можно показать человеку."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status


class InspectionApiUnavailable(InspectionApiError):
    """API недоступен или не настроен — это НЕ «осмотр не найден»."""


def api_configured() -> bool:
    """Настроен ли путь до API и есть ли чем подписать токен."""
    return bool(API_BASE and JWT_SECRET)


def _import_jwt():
    """
    PyJWT импортируется лениво.

    Тот же приём, что у остальных необязательных зависимостей бота: если
    библиотеки нет, падать на импорте `telegram_bot` нельзя — от этого
    зависит ВСЯ работа бота, а не только осмотр.
    """
    try:
        import jwt  # type: ignore
    except ImportError:  # pragma: no cover - зависит от окружения
        return None

    return jwt


def issue_token(auth_id: str) -> str | None:
    """
    Токен работника для API. None — нет секрета/библиотеки/`auth_id`.

    `role: authenticated` — обязательное поле для Supabase: API проверяет
    аудиторию (`aud`), а не это поле, но без него токен не похож на настоящий.
    """
    if not auth_id or not JWT_SECRET:
        return None

    jwt = _import_jwt()

    if jwt is None:
        logger.error("PyJWT не установлен — осмотр из бота недоступен")
        return None

    now = datetime.now(timezone.utc)

    payload = {
        "sub": str(auth_id),
        "aud": "authenticated",
        "role": "authenticated",
        "iat": now,
        "exp": now + timedelta(seconds=TOKEN_TTL_SECONDS),
    }

    try:
        return jwt.encode(payload, JWT_SECRET, algorithm="HS256")
    except Exception:
        # Секрет может не быть строкой HS256 (например, JWKS-only окружение) —
        # это «осмотр не настроен», а не падение диалога.
        logger.exception("Не удалось выпустить токен для осмотра")
        return None


def token_failure_reason(auth_id: str) -> str | None:
    """
    Почему токен не выпускается — **человеческим текстом**. None — выпускается.

    🔴 **Зачем отдельная функция.** Раньше единственное сообщение обвиняло
    `auth_id`:

    > Your Telegram account is not linked to a system user (no auth_id)

    А `issue_token` возвращает `None` по **четырём** разным причинам: нет
    `auth_id`, нет секрета `SUPABASE_JWT_SECRET`, нет библиотеки PyJWT или
    секрет не подходит для HS256.

    Владелец 17.10.2026 получил это сообщение, хотя **и привязка, и `auth_id`
    были на месте** — не хватало библиотеки PyJWT в контейнере (её не было в
    `requirements.txt`, а локально она стояла). По такому тексту человек пошёл бы
    чинить карточку сотрудника, то есть **не туда**.

    Диагноз обязан указывать на настоящую причину — иначе он вреднее молчания.
    """
    if not auth_id:
        return (
            "Your employee card has no `auth_id`, so an inspection can't be "
            "attributed to you. Ask a leader to link your card to a system user."
        )

    if not JWT_SECRET:
        return (
            "The bot can't sign an inspection token: `SUPABASE_JWT_SECRET` is "
            "not set on the bot. Ask an administrator to add it."
        )

    if _import_jwt() is None:
        return (
            "The bot can't sign an inspection token: the PyJWT library is not "
            "installed in the bot's environment. Ask an administrator to "
            "install it (it is listed in requirements.txt)."
        )

    return None


def _error_text(response) -> str:
    """
    Текст ошибки API, пригодный для человека.

    Формат ошибок API — `{"error": {"code": ..., "message": ...}}`. Показываем
    `message`: он написан для клиента. Код и HTTP оставляем в логе.
    """
    try:
        body = response.json()
    except ValueError:
        return ""

    error = body.get("error") if isinstance(body, dict) else None

    if isinstance(error, dict):
        return str(error.get("message") or "")

    if isinstance(body, dict):
        return str(body.get("detail") or "")

    return ""


def _request(
    method: str,
    path: str,
    auth_id: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    files=None,
    data=None,
    timeout: int = DEFAULT_TIMEOUT,
):
    """Один запрос к API. Ошибки — исключениями с понятным текстом."""
    if not API_BASE:
        raise InspectionApiUnavailable(
            "Inspections are not configured on the bot (TK_ASSIST_API_URL is empty)"
        )

    token = issue_token(auth_id)

    if not token:
        # 🔴 **Причину называем ту, что есть на самом деле.**
        #
        # Раньше здесь стоял текст только про `auth_id` — и он вводил в
        # заблуждение, когда `auth_id` был, а не хватало библиотеки (реальный
        # случай владельца 17.10.2026). `token_failure_reason` различает причины.
        raise InspectionApiUnavailable(
            token_failure_reason(auth_id)
            or "Inspections are unavailable: the bot could not sign a token"
        )

    url = f"{API_BASE}{path}"

    try:
        response = requests.request(
            method,
            url,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            json=json_body,
            files=files,
            data=data,
            timeout=timeout,
        )
    except requests.exceptions.RequestException as exc:
        logger.error("API осмотров недоступен: %s %s — %s", method, path, exc)
        raise InspectionApiUnavailable(
            "Can't reach the inspection service right now. Try again in a minute."
        ) from exc

    if response.status_code >= 400:
        message = _error_text(response) or "The inspection service rejected the request"
        logger.warning(
            "API осмотров отказал: %s %s -> %s %s",
            method,
            path,
            response.status_code,
            message,
        )
        raise InspectionApiError(message, status=response.status_code)

    if response.status_code == 204 or not response.content:
        return None

    try:
        body = response.json()
    except ValueError as exc:
        logger.error("API осмотров вернул не-JSON: %s %s", method, path)
        raise InspectionApiUnavailable(
            "The inspection service returned an unexpected response"
        ) from exc

    # Все ручки отдают конверт `{"data": ..., "meta": ...}`.
    if isinstance(body, dict) and "data" in body:
        return body["data"]

    return body


# ── Шаблоны ──────────────────────────────────────────────────────────────────


def list_templates(auth_id: str, category: str) -> list[dict]:
    """
    Шаблоны осмотра строго одной категории оборудования.

    Категория передаётся в API, а не фильтруется в боте: у робота не должно
    быть даже возможности выбрать «Charging station inspection». Фильтр на
    стороне сервера — это ещё и единственный список, который API считает
    актуальным (`is_active`).
    """
    data = _request(
        "GET",
        "/v1/inspection-templates",
        auth_id,
        params={"category": category},
    )

    return data or []


# ── Оборудование ─────────────────────────────────────────────────────────────
#
# 🔴 **Поиск единицы живёт НЕ здесь, а в `equipment_intake.validation`.**
#
# Первая версия этого модуля искала устройство через `GET /v1/equipment`. Это
# было ошибкой, и вот почему — проверено на живой ручке:
#
#   * точного фильтра по номеру в API **нет**: `equipment_code` молча
#     игнорируется (запрос `?equipment_code=13` вернул станции `5`, `6`, `14`…),
#     а `search` ищет **по подстроке** (`search=13` вернул `113` и `213`, но не
#     `13`);
#   * значит по номеру «13» нужная станция не находилась бы вовсе, а «1»
#     вернула бы полсотни чужих единиц.
#
# `validation.find_equipment` делает **точное** сравнение по
# `equipment_code = eq.<номер>` вместе с категорией и складом — то же правило,
# по которому уже проверяется номер при приёме ошибки. Вторая реализация
# поиска в этом модуле разошлась бы с первой при первой же правке (в проекте
# это уже случалось на «общих» функциях), поэтому здесь её нет.


# ── Осмотры ──────────────────────────────────────────────────────────────────


def create_inspection(
    auth_id: str,
    equipment_id: int,
    template_id: int,
    comment: str | None = None,
) -> dict:
    """Создаёт осмотр и возвращает его вместе с пунктами шаблона."""
    return _request(
        "POST",
        "/v1/inspections",
        auth_id,
        json_body={
            "equipment_id": equipment_id,
            "template_id": template_id,
            "comment": comment,
        },
    )


def set_item_verdict(
    auth_id: str,
    inspection_id: int,
    item_id: int,
    verdict: str,
    comment: str | None = None,
) -> dict:
    """
    Вердикт пункта. `failed` без комментария API отклонит — и это правильно:
    «не прошло» без объяснения бесполезно тому, кто будет чинить.
    """
    return _request(
        "PATCH",
        f"/v1/inspections/{inspection_id}/items/{item_id}",
        auth_id,
        json_body={"verdict": verdict, "comment": comment},
    )


def upload_item_photo(
    auth_id: str,
    inspection_id: int,
    item_id: int,
    content: bytes,
    filename: str = "photo.jpg",
    content_type: str = "image/jpeg",
) -> dict:
    """
    Загружает фото пункта.

    Тип файла API определяет по содержимому (`validate_image`), а заголовок
    использует лишь как подсказку — поэтому подделанное расширение не пройдёт.
    """
    return _request(
        "POST",
        f"/v1/inspections/{inspection_id}/items/{item_id}/photo",
        auth_id,
        files={"file": (filename, content, content_type)},
        timeout=PHOTO_TIMEOUT,
    )


def complete_inspection(auth_id: str, inspection_id: int, comment: str | None = None) -> dict:
    """Закрывает осмотр; вердикт считает сервер по пунктам."""
    return _request(
        "POST",
        f"/v1/inspections/{inspection_id}/complete",
        auth_id,
        json_body={"comment": comment},
    )


def get_inspection(auth_id: str, inspection_id: int) -> dict:
    """Осмотр с пунктами — нужен, чтобы показать итог."""
    return _request("GET", f"/v1/inspections/{inspection_id}", auth_id)


def create_share(auth_id: str, inspection_id: int) -> dict:
    """
    Публичная ссылка «для всех на веб-версию».

    Выдаётся **только на закрытый осмотр** — черновик ещё меняется, и
    опубликованный отчёт разошёлся бы с делом. Повторный вызов возвращает ту
    же ссылку, а не плодит новые.
    """
    return _request(
        "POST",
        f"/v1/inspections/{inspection_id}/share",
        auth_id,
    )


#: Базовый адрес публичной страницы осмотра (`tk-new`). Ссылка без домена
#: бесполезна тому, кто откроет её в Telegram, поэтому домен подставляем сами.
PUBLIC_SITE_BASE = os.environ.get(
    "INSPECTION_PUBLIC_URL",
    "https://tk-server-assist.vercel.app",
).strip().rstrip("/")


def public_url(token: str) -> str:
    """Адрес публичной страницы осмотра по токену."""
    return f"{PUBLIC_SITE_BASE}/inspection/share/{token}"
