from __future__ import annotations

from codexrelay.updates.base import UpdateChannel, UpdateState


class DisabledUpdateProvider:
    def __init__(self, channel: UpdateChannel = UpdateChannel.STABLE) -> None:
        self._state = UpdateState(
            enabled=False,
            channel=channel,
            message="自动更新将在正式 GitHub 发行版中启用",
        )

    @property
    def state(self) -> UpdateState:
        return self._state

    def check_for_updates(self) -> None:
        return
