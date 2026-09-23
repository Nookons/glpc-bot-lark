"""
Безопасное чтение переменных окружения.

Опечатка в переменной Railway (например, `BOT_LEASE_TTL=90s`) не должна
ронять бота на старте — берём значение по умолчанию и пишем предупреждение.
"""

from __future__ import annotations

import os

from logging_config import setup_logging


logger = setup_logging(__name__)


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)

    if raw is None or not str(raw).strip():
        return default

    try:
        return int(str(raw).strip())
    except ValueError:
        logger.warning(
            "Некорректное значение %s=%r — беру %s",
            name,
            raw,
            default,
        )
        return default


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)

    if raw is None or not str(raw).strip():
        return default

    return str(raw).strip().lower() in ("1", "true", "yes", "on")
