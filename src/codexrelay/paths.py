from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path, user_log_path


@dataclass(frozen=True, slots=True)
class AppPaths:
    data_dir: Path
    log_dir: Path

    @classmethod
    def default(cls) -> AppPaths:
        data_override = os.environ.get("CODEXRELAY_DATA_DIR")
        log_override = os.environ.get("CODEXRELAY_LOG_DIR")
        return cls(
            data_dir=(
                Path(data_override).expanduser()
                if data_override
                else Path(user_data_path("CodexRelay", appauthor=False))
            ),
            log_dir=(
                Path(log_override).expanduser()
                if log_override
                else Path(user_log_path("CodexRelay", appauthor=False))
            ),
        )

    @property
    def database(self) -> Path:
        return self.data_dir / "codexrelay.db"

    @property
    def settings(self) -> Path:
        return self.data_dir / "settings.toml"

    @property
    def diagnostics(self) -> Path:
        return self.data_dir / "diagnostics"

    def ensure(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.diagnostics.mkdir(parents=True, exist_ok=True, mode=0o700)
