import logging
from logging.handlers import RotatingFileHandler

from .config import ensure_dirs, LOG_DIR

def setup_logging() -> None:
    ensure_dirs()

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        LOG_DIR / "app.log",
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)

    # 중복 등록 방지
    if not logger.handlers:
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
