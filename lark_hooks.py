"""
Куда бот пишет в Lark: у ошибок и у смены статусов разные вебхуки, и у
каждого склада — свой.

    ошибки GLP-C   -> LARK_HOOK_ERROR_GLPC
    ошибки SP3     -> LARK_HOOK_ERROR_SP3
    статусы GLP-C  -> LARK_HOOK_STATUS_GLPC
    статусы SP3    -> LARK_HOOK_STATUS_SP3

Если переменная не задана, берётся общий `LARK_TARGET_HOOK_URL` (историческая
настройка): бот не должен молчать из-за незаполненного склада. Значения
читаются при каждом вызове, чтобы правка переменных не требовала перезапуска
(в тестах это ещё и позволяет подменять их на месте).
"""

from __future__ import annotations

import os


DEFAULT_TARGET_HOOK_URL = (
    "https://open.larksuite.com/open-apis/bot/v2/hook/"
    "dc2c430d-ce07-4ff2-b5ca-0b92feb4f62a"
)

# Общий вебхук: используется отчётами и как запасной для всех остальных.
TARGET_HOOK_URL = os.environ.get(
    "LARK_TARGET_HOOK_URL",
    DEFAULT_TARGET_HOOK_URL,
)

KINDS = ("error", "status")


def _warehouse_key(warehouse: str):
    """Ключ склада (glpc/sp3) для имени переменной."""
    from warehouses import WAREHOUSES

    if not warehouse:
        return None

    for key, title in WAREHOUSES.items():
        if title == warehouse:
            return key

    return None


def hook(kind: str, warehouse: str = None) -> str:
    """Вебхук для вида сообщений и склада (или общий)."""
    from warehouses import DEFAULT_WAREHOUSE

    title = warehouse or DEFAULT_WAREHOUSE
    key = _warehouse_key(title)

    if key:
        raw = os.environ.get(
            f"LARK_HOOK_{str(kind).upper()}_{key.upper()}",
            "",
        ).strip()

        if raw:
            return raw

    return TARGET_HOOK_URL


def error_hook(warehouse: str = None) -> str:
    return hook("error", warehouse)


def status_hook(warehouse: str = None) -> str:
    return hook("status", warehouse)


def hook_map() -> dict:
    """Карта вебхуков для диагностики (/topics и стартовый лог)."""
    from warehouses import WAREHOUSES

    return {
        kind: {title: hook(kind, title) for title in WAREHOUSES.values()}
        for kind in KINDS
    }


def short(url: str) -> str:
    """Хвост вебхука для логов: целиком его печатать незачем."""
    return str(url or "").rstrip("/").split("/")[-1][:8]
