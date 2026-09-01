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


class UpdateProvider(Protocol):
    """Boundary for a future Sparkle-backed update provider.

    Development and ad-hoc builds use a disabled implementation. A signed public
    build can later supply a Sparkle provider without coupling update behavior to
    the settings window or runtime.
    """

    @property
    def state(self) -> UpdateState: ...

    def check_for_updates(self) -> UpdateState: ...
