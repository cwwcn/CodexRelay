from __future__ import annotations

from codexrelay.updates.base import UpdateState


class DisabledUpdateProvider:
    def __init__(self) -> None:
        self._state = UpdateState(
            enabled=False,
            message="自动更新将在正式 GitHub 发行版中启用",
        )

    @property
    def state(self) -> UpdateState:
        return self._state

    def check_for_updates(self) -> UpdateState:
        return self._state

    def download_update(self) -> UpdateState:
        return self._state
