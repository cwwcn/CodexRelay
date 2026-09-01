from __future__ import annotations

import os
import tempfile
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

import tomli_w


@dataclass(frozen=True, slots=True)
class AppSection:
    auto_connect: bool = True
    launch_at_login: bool = False
    prevent_sleep_while_running: bool = True
    update_checks_automatically: bool = False
    update_channel: str = "stable"


@dataclass(frozen=True, slots=True)
class TelegramSection:
    account_id: str = "main-bot"
    private_chat_only: bool = True
    max_image_bytes: int = 20 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ProjectSection:
    scan_roots: tuple[str, ...] = field(default_factory=lambda: ("~/Documents",))
    scan_depth: int = 2


@dataclass(frozen=True, slots=True)
class Settings:
    app: AppSection = field(default_factory=AppSection)
    telegram: TelegramSection = field(default_factory=TelegramSection)
    projects: ProjectSection = field(default_factory=ProjectSection)


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        with self.path.open("rb") as source:
            raw = tomllib.load(source)
        self._reject_secrets(raw)
        app = raw.get("app", {})
        telegram = raw.get("telegram", {})
        projects = raw.get("projects", {})
        if not all(isinstance(section, dict) for section in (app, telegram, projects)):
            raise ValueError("settings sections must be TOML tables")
        return Settings(
            app=AppSection(
                auto_connect=bool(app.get("auto_connect", True)),
                launch_at_login=bool(app.get("launch_at_login", False)),
                prevent_sleep_while_running=bool(app.get("prevent_sleep_while_running", True)),
                update_checks_automatically=bool(app.get("update_checks_automatically", False)),
                update_channel=str(app.get("update_channel", "stable")),
            ),
            telegram=TelegramSection(
                account_id=str(telegram.get("account_id", "main-bot")),
                private_chat_only=bool(telegram.get("private_chat_only", True)),
                max_image_bytes=int(telegram.get("max_image_bytes", 20 * 1024 * 1024)),
            ),
            projects=ProjectSection(
                scan_roots=tuple(str(item) for item in projects.get("scan_roots", ["~/Documents"])),
                scan_depth=int(projects.get("scan_depth", 2)),
            ),
        )

    def save(self, settings: Settings) -> None:
        payload = asdict(settings)
        self._reject_secrets(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                tomli_w.dump(payload, output)
                output.flush()
                os.fsync(output.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _reject_secrets(value: object, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = str(key).casefold().replace("-", "_")
                if normalized in {"bot_token", "telegram_token", "api_key", "password"}:
                    location = ".".join((*path, str(key)))
                    raise ValueError(f"secret field is forbidden in settings: {location}")
                SettingsStore._reject_secrets(child, (*path, str(key)))
        elif isinstance(value, list | tuple):
            for child in value:
                SettingsStore._reject_secrets(child, path)
