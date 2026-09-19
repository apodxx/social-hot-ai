"""Logging setup.

The task log in the project spec looks like ``[08:00:01] 开始执行热点任务``, so the
formatter leads with a wall-clock time and the logger name for attribution.
"""

from __future__ import annotations

import logging
import sys

_DATE_FORMAT = "%H:%M:%S"
_LOG_FORMAT = "[%(asctime)s] %(levelname)-5s %(name)s: %(message)s"

_configured = False


def setup_logging(level: str = "INFO") -> None:
    """Install one stdout handler on the root logger. Idempotent."""
    global _configured
    root = logging.getLogger()
    if not _configured:
        # The console encoding on a Chinese Windows install is not UTF-8, so keywords,
        # titles and interest terms came out as replacement characters in the server
        # log while the same text printed correctly from scripts (which reconfigure
        # their own stdout). A log nobody can read is not a log. stderr is included
        # because uvicorn keeps its own handler there.
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8", errors="replace")
                except (ValueError, OSError):  # pragma: no cover - stream already detached
                    pass
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
        root.addHandler(handler)
        _configured = True
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # httpx logs every request at INFO, which would double-log our own lines.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Named logger for one module."""
    return logging.getLogger(name)
