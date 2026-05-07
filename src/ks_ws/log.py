import logging
import logging.handlers
import sys
from pathlib import Path

_LOG_DIR = Path(__file__).resolve().parents[2] / "logs"


def setup(level: int | str = logging.INFO) -> logging.Logger:
    """Configure the 'ks_ws' logger with console + daily-rotating file handlers.

    Idempotent — safe to call multiple times.
    """
    logger = logging.getLogger("ks_ws")
    if logger.handlers:
        return logger

    logger.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.TimedRotatingFileHandler(
        _LOG_DIR / "ks_ws.log",
        when="midnight",
        backupCount=14,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logger.propagate = False
    return logger
