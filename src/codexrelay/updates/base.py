from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class UpdateChannel(StrEnum):
    STABLE = "stable"
    BETA = "beta"


@dataclass(frozen=True, slots=True)
class UpdateState:
    enabled: bool
    channel: UpdateChannel
    checking: bool = False
    available_version: str | None = None
    message: str = ""


class UpdateProvider(Protocol):
    """Boundary for a future Sparkle-backed update provider.

    Development and ad-hoc builds use a disabled implementation. A signed public
    build can later supply a Sparkle provider without coupling update behavior to
    the settings window or runtime.
    """

    @property
    def state(self) -> UpdateState: ...

    def check_for_updates(self) -> None: ...
