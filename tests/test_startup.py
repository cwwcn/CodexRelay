import plistlib
import sys
from pathlib import Path

from pytest import MonkeyPatch

from codexrelay.startup import StartupService


def test_uninstall_is_idempotent(tmp_path: Path) -> None:
    service = StartupService(tmp_path)
    service.uninstall()
    assert not service.enabled


def test_packaged_app_installs_user_launch_agent(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    executable = tmp_path / "CodexRelay.app" / "Contents" / "MacOS" / "CodexRelay"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    service = StartupService(tmp_path / "LaunchAgents")

    service.install()

    with service.plist_path.open("rb") as source:
        payload = plistlib.load(source)
    assert payload["ProgramArguments"] == [str(executable)]
    assert payload["RunAtLoad"] is True
    assert payload["LimitLoadToSessionType"] == "Aqua"
    assert service.plist_path.stat().st_mode & 0o777 == 0o600
