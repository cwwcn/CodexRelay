from __future__ import annotations

import os
import plistlib
import sys
import tempfile
from pathlib import Path


class StartupService:
    label = "com.cwwen.codexrelay"

    def __init__(self, launch_agents: Path | None = None) -> None:
        self.launch_agents = launch_agents or Path.home() / "Library" / "LaunchAgents"

    @property
    def plist_path(self) -> Path:
        return self.launch_agents / f"{self.label}.plist"

    @property
    def available(self) -> bool:
        return bool(getattr(sys, "frozen", False))

    @property
    def enabled(self) -> bool:
        return self.plist_path.exists()

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            self.install()
        else:
            self.uninstall()

    def install(self) -> None:
        if not self.available:
            raise RuntimeError("login launch can only be enabled from the packaged app")
        self.launch_agents.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "Label": self.label,
            "ProgramArguments": [sys.executable],
            "RunAtLoad": True,
            "KeepAlive": False,
            "ProcessType": "Interactive",
            "LimitLoadToSessionType": "Aqua",
            "StandardOutPath": str(Path.home() / "Library" / "Logs" / "CodexRelay.launch.log"),
            "StandardErrorPath": str(
                Path.home() / "Library" / "Logs" / "CodexRelay.launch.error.log"
            ),
        }
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.plist_path.name}.", dir=self.launch_agents
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                plistlib.dump(payload, output, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, self.plist_path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def uninstall(self) -> None:
        self.plist_path.unlink(missing_ok=True)
