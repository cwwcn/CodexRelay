"""Application update integration boundary."""

from codexrelay.updates.base import UpdateChannel, UpdateProvider, UpdateState
from codexrelay.updates.disabled import DisabledUpdateProvider

__all__ = [
    "DisabledUpdateProvider",
    "UpdateChannel",
    "UpdateProvider",
    "UpdateState",
]
