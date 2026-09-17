import logging
import os
from logging.handlers import RotatingFileHandler


def setup_logging(name: str = "glpc_bot_lark") -> logging.Logger:
    """
    Единая настройка логирования для всего проекта.

    Использование в любом модуле:
        from logging_config import setup_logging
        logger = setup_logging(__name__)
    """
    logger = logging.getLogger(name)

    if logger.handlers:
        # Логгер уже настроен (например, при повторном импорте).
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ------------------------------------------------------------
    # Console handler (Railway собирает stdout/stderr)
    # ------------------------------------------------------------
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # ------------------------------------------------------------
    # File handler с ротацией, чтобы лог-файлы не росли бесконечно.
    # Не критичен на Railway (ephemeral-диск), но удобен локально.
    # ------------------------------------------------------------
    log_dir = os.environ.get("LOG_DIR", "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
        file_handler = RotatingFileHandler(
            filename=os.path.join(log_dir, "app.log"),
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        # Если файл логирования недоступен (например, read-only FS) —
        # продолжаем работать с консольным обработчиком.
        pass

    return logger
