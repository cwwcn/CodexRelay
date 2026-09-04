"""Single source of truth for the application version and build metadata."""

import os
import plistlib
import sys
from pathlib import Path

__version__ = "0.1.2"


def _bundle_build_time() -> str | None:
    if not getattr(sys, "frozen", False) or sys.platform != "darwin":
        return None
    info_path = Path(sys.executable).resolve().parents[2] / "Info.plist"
    try:
        with info_path.open("rb") as source:
            value = plistlib.load(source).get("CodexRelayBuildTime")
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    return str(value) if value else None


__build_time__ = os.environ.get("CODEXRELAY_BUILD_TIME") or _bundle_build_time() or "Source build"
