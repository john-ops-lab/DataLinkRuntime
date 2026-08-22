"""M5.11 Wave B platform log persistence and external rotation tests."""

import logging
import logging.handlers
from pathlib import Path

from dlr.common.config import settings
from dlr.common.platform_logging import configure_platform_logging


def _remove_handlers_under(root: Path) -> None:
    loggers = [logging.getLogger(), logging.getLogger("uvicorn")]
    for logger in loggers:
        for handler in list(logger.handlers):
            filename = getattr(handler, "baseFilename", "")
            if isinstance(handler, logging.handlers.WatchedFileHandler) and str(root) in filename:
                logger.removeHandler(handler)
                handler.close()


def test_watched_platform_log_reopens_after_external_rename(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "platform_log_root", str(tmp_path))
    try:
        assert configure_platform_logging("worker")
        logger = logging.getLogger("dlr.platform-test")
        logger.setLevel(logging.INFO)
        logger.info("before rotation")

        current = tmp_path / "worker" / "worker.log"
        rotated = tmp_path / "worker" / "worker.log.1"
        assert current.read_text(encoding="utf-8").endswith("before rotation\n")
        current.rename(rotated)
        current.touch()

        logger.info("after rotation")
        assert "before rotation" in rotated.read_text(encoding="utf-8")
        assert "after rotation" in current.read_text(encoding="utf-8")
    finally:
        _remove_handlers_under(tmp_path)


def test_persistent_platform_copy_redacts_common_credential_values(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "platform_log_root", str(tmp_path))
    try:
        assert configure_platform_logging("worker")
        logger = logging.getLogger("dlr.platform-secret-test")
        logger.setLevel(logging.INFO)
        logger.info("token=%s password=%s", "EXAMPLE_TOKEN", "EXAMPLE_PASSWORD")
        content = (tmp_path / "worker" / "worker.log").read_text(encoding="utf-8")
        assert "EXAMPLE_TOKEN" not in content
        assert "EXAMPLE_PASSWORD" not in content
        assert "token=<redacted>" in content
    finally:
        _remove_handlers_under(tmp_path)
