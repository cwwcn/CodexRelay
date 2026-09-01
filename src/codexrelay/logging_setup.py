from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(log_dir: Path) -> Path:
    """Configure a small, local-only rotating application log."""
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = log_dir / "codexrelay.log"
    logger = logging.getLogger("codexrelay")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    resolved = log_path.resolve()
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == resolved:
            return log_path
    handler = RotatingFileHandler(
        log_path,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(handler)
    log_path.chmod(0o600)
    return log_path

