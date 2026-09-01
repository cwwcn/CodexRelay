"""Application update integration boundary."""

from codexrelay.updates.base import UpdateChannel, UpdateProvider, UpdateState
from codexrelay.updates.disabled import DisabledUpdateProvider
from codexrelay.updates.github import GitHubReleaseUpdateProvider

__all__ = [
    "DisabledUpdateProvider",
    "GitHubReleaseUpdateProvider",
    "UpdateChannel",
    "UpdateProvider",
    "UpdateState",
]
