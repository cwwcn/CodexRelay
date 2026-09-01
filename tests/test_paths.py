from __future__ import annotations

import logging
from pathlib import Path

from pytest import MonkeyPatch

from codexrelay.logging_setup import configure_logging
from codexrelay.paths import AppPaths


def test_paths_support_isolated_runtime_overrides(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("CODEXRELAY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("CODEXRELAY_LOG_DIR", str(log_dir))

    paths = AppPaths.default()

    assert paths.data_dir == data_dir
    assert paths.log_dir == log_dir


def test_logging_is_local_rotating_and_idempotent(tmp_path: Path) -> None:
    logger = logging.getLogger("codexrelay")
    previous_handlers = list(logger.handlers)
    try:
        logger.handlers.clear()
        first = configure_logging(tmp_path)
        second = configure_logging(tmp_path)
        logger.info("runtime started")

        assert first == second == tmp_path / "codexrelay.log"
        assert len(logger.handlers) == 1
        assert "runtime started" in first.read_text(encoding="utf-8")
        assert first.stat().st_mode & 0o777 == 0o600
    finally:
        for handler in logger.handlers:
            handler.close()
        logger.handlers[:] = previous_handlers
