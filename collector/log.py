"""Logging setup shared by every entrypoint (receiver, backfill worker, mock bridge).

Each process writes to its own rotating file in LOG_DIR plus stdout. Separate files per
process because two processes rotating the same file corrupt each other's rotation.
"""
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_MARKER = "_collector_handler"


def setup_logging(process_name: str, log_dir: str = "logs", level: str = "INFO") -> logging.Logger:
    """Attach a rotating file handler and a stdout handler to the root logger.

    Idempotent: calling it again (tests, reloads) does not add duplicate handlers.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())

    if not any(getattr(h, _MARKER, False) for h in root.handlers):
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        formatter = logging.Formatter(FORMAT)

        file_handler = RotatingFileHandler(
            Path(log_dir) / f"{process_name}.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        stream_handler = logging.StreamHandler(sys.stdout)
        for handler in (file_handler, stream_handler):
            handler.setFormatter(formatter)
            setattr(handler, _MARKER, True)
            root.addHandler(handler)

    # httpx logs every request at INFO; we log requests ourselves with status + latency.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return logging.getLogger(process_name)
