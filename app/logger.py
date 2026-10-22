import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import settings


_CONFIGURED = False


def setup_logging() -> None:
    global _CONFIGURED

    if _CONFIGURED:
        return

    log_level = getattr(logging, settings.LOG_LEVEL.upper(), None)

    if not isinstance(log_level, int):
        raise ValueError(f"Invalid LOG_LEVEL: {settings.LOG_LEVEL}")

    log_format = (
        "%(asctime)s | %(levelname)s | "
        "%(name)s | %(message)s"
    )
    date_format = "%Y-%m-%d %H:%M:%S"

    formatter = logging.Formatter(
        fmt=log_format,
        datefmt=date_format,
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    if settings.LOG_TO_FILE:
        log_path = Path(settings.LOG_FILE)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        file_handler = RotatingFileHandler(
            filename=log_path,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
