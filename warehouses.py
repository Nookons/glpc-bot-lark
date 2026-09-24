"""
Склады, которые ведёт бот, и разбор аргументов команд.

Один бот обслуживает несколько складов (GLP-C и SMALL-P3). Склад приходит
либо из топика (топик ошибок SP3 -> SMALL-P3), либо аргументом команды
(`/stats sp3 day`). Названия должны совпадать с `warehouse` в базе:
`robots_maintenance_list.warehouse`, `exceptions_glpc.warehouse`.
"""

from __future__ import annotations

import os

from sendToDataBase import WAREHOUSE


def parse_warehouses(raw: str) -> dict:
    """«glpc=GLP-C,sp3=SMALL-P3» -> {"glpc": "GLP-C", "sp3": "SMALL-P3"}."""
    parsed = {}

    for chunk in str(raw or "").split(","):
        chunk = chunk.strip()

        if not chunk:
            continue

        if "=" in chunk:
            key, title = chunk.split("=", 1)
        else:
            key = title = chunk

        key = key.strip().lower().replace("-", "").replace("_", "")
        title = title.strip()

        if key and title:
            parsed[key] = title

    return parsed


WAREHOUSES = parse_warehouses(
    os.environ.get("TELEGRAM_WAREHOUSES", "glpc=GLP-C,sp3=SMALL-P3")
)

if not WAREHOUSES:
    WAREHOUSES = {"glpc": WAREHOUSE}

DEFAULT_WAREHOUSE = (
    WAREHOUSE if WAREHOUSE in WAREHOUSES.values() else next(iter(WAREHOUSES.values()))
)


def warehouse_key(title: str):
    """Ключ склада (glpc/sp3) по названию — для команд и сообщений."""
    for key, name in WAREHOUSES.items():
        if name == title:
            return key

    return None


def warehouse_from_args(args: str):
    """
    Ищет склад в аргументах команды: «/stats sp3 day» -> ("SMALL-P3", "day").

    None — склад не указан (значит, берём склад текущего топика).
    """
    tokens = str(args or "").split()

    for index, token in enumerate(tokens):
        cleaned = token.strip().lower().replace("-", "").replace("_", "")

        for key, title in WAREHOUSES.items():
            if cleaned == key or token.strip().casefold() == title.casefold():
                rest = tokens[:index] + tokens[index + 1:]

                return title, " ".join(rest)

    return None, args
