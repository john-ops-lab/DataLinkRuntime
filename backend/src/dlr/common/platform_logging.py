"""Persistent platform-service logging with external-rotation support.

The application log remains on stdout for normal container operation.  This
module adds a ``WatchedFileHandler`` for the host-mounted platform log root;
when logrotate renames the file, the next record is written to a fresh file
without restarting the process.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path

from dlr.common.config import settings

_HANDLER_MARKER = "_dlr_platform_log_handler"
_SENSITIVE_VALUE = re.compile(
    r"(?i)(authorization|cookie|password|token|api[_-]?key|secret|master[_-]?key)"
    r"(\s*[:=]\s*|\s+)[^\s,;&]+"
)


class _RedactingFormatter(logging.Formatter):
    """Keep common credential values out of the persistent log copy."""

    def format(self, record: logging.LogRecord) -> str:
        return _SENSITIVE_VALUE.sub(r"\1=<redacted>", super().format(record))


def _service_directory(service: str) -> Path:
    if service not in {"control", "worker"}:
        raise ValueError(f"unsupported DLR platform log service: {service}")
    return Path(settings.platform_log_root).expanduser() / service


def configure_platform_logging(service: str) -> bool:
    """Attach one watched persistent handler for a Control or Worker process.

    A deployment may deliberately run without a writable platform log mount
    (for example a local unit test).  In that case stdout logging remains
    available and the function returns ``False`` instead of preventing the
    process from starting.  Production Compose mounts the directory and
    therefore expects this to return ``True``.
    """

    directory = _service_directory(service)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        log_path = directory / f"{service}.log"
        handler = logging.handlers.WatchedFileHandler(log_path, encoding="utf-8", delay=True)
    except OSError:
        logging.getLogger(f"dlr.{service}").warning(
            "platform log directory is not writable: service=%s path=%s",
            service,
            directory,
        )
        return False

    formatter = _RedactingFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    handler.setFormatter(formatter)
    setattr(handler, _HANDLER_MARKER, service)

    root_logger = logging.getLogger()
    for existing in root_logger.handlers:
        if getattr(existing, _HANDLER_MARKER, None) == service:
            existing.close()
            root_logger.removeHandler(existing)
    root_logger.addHandler(handler)

    # Uvicorn's access/error loggers may disable propagation.  Attach the
    # same handler directly and disable propagation to avoid duplicate lines.
    if service == "control":
        for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
            uvicorn_logger = logging.getLogger(logger_name)
            for existing in uvicorn_logger.handlers:
                if getattr(existing, _HANDLER_MARKER, None) == service:
                    uvicorn_logger.removeHandler(existing)
            uvicorn_logger.addHandler(handler)
            uvicorn_logger.propagate = False

    return True
