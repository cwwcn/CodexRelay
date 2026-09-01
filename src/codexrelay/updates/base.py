from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class UpdateState:
    enabled: bool
    checking: bool = False
    available_version: str | None = None
    message: str = ""
    release_url: str | None = None
    published_at: str | None = None
    release_notes: str | None = None
    architecture: str | None = None
    asset_name: str | None = None
    asset_url: str | None = None
    asset_digest: str | None = None
    downloaded_path: str | None = None
    downloading: bool = False
    downloaded_bytes: int = 0
    total_bytes: int | None = None


class UpdateProvider(Protocol):
    """Update boundary independent of the eventual installation mechanism.

    The current GitHub implementation supports user-confirmed DMG downloads for
    unsigned releases. A signed build can later replace it with Sparkle without
    coupling update behavior to the settings window or runtime.
    """

    @property
    def state(self) -> UpdateState: ...

    def check_for_updates(self) -> UpdateState: ...

    def download_update(self) -> UpdateState: ...
