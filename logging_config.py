"""
Единая настройка логирования.

Хендлеры ставятся ОДИН раз на корневой логгер. Раньше каждый модуль вешал
свой RotatingFileHandler на один и тот же logs/app.log: получалось
одиннадцать файловых дескрипторов, независимые ротации (5 МБ каждая) и
потерянные записи после первой ротации.
"""

import logging
import os
from logging.handlers import RotatingFileHandler


LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


def _level() -> int:
    """Уровень логирования: LOG_LEVEL (по умолчанию INFO)."""
    raw = os.environ.get("LOG_LEVEL", "INFO")

    if not raw or not str(raw).strip():
        return logging.INFO

    return getattr(logging, str(raw).strip().upper(), logging.INFO)


def _configure_root() -> None:
    global _configured

    if _configured:
        return

    _configured = True

    formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT)
    level = _level()

    root = logging.getLogger()
    root.setLevel(level)

    # ------------------------------------------------------------
    # Консоль (Railway собирает stdout/stderr)
    # ------------------------------------------------------------
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    # ------------------------------------------------------------
    # Файл с ротацией: удобно локально, на Railway диск эфемерный.
    # ------------------------------------------------------------
    log_dir = os.environ.get("LOG_DIR", "logs")

    try:
        os.makedirs(log_dir, exist_ok=True)

        file_handler = RotatingFileHandler(
            filename=os.path.join(log_dir, "app.log"),
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as e:
        # Логирование в файл недоступно (read-only FS) — работаем с консолью.
        print(f"Логирование в файл недоступно: {e}")


def setup_logging(name: str = "glpc_bot_lark") -> logging.Logger:
    """
    Возвращает логгер модуля.

    Хендлеры настраиваются один раз при первом вызове; сообщения идут в них
    через propagation, поэтому дублирования нет.
    """
    _configure_root()

    logger = logging.getLogger(name)
    logger.setLevel(_level())

    return logger
