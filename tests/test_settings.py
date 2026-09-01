from pathlib import Path

import pytest

from codexrelay.settings import ProjectSection, Settings, SettingsStore


def test_settings_round_trip_without_secrets(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.toml")
    expected = Settings(projects=ProjectSection(scan_roots=("~/Code", "~/Documents"), scan_depth=3))

    store.save(expected)

    assert store.load() == expected
    assert "token" not in store.path.read_text().casefold()
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_settings_reject_token_field(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    path.write_text('[telegram]\nbot_token = "do-not-store"\n')

    with pytest.raises(ValueError, match="secret field"):
        SettingsStore(path).load()
