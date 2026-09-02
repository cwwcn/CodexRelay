from __future__ import annotations

import logging
from time import monotonic
from typing import Protocol

from codexrelay.connectors.telegram.api import TelegramError

LOGGER = logging.getLogger("codexrelay.telegram.progress")


class ProgressMessageClient(Protocol):
    async def send_text(
        self, chat_id: str, text: str, *, reply_markup: dict[str, object] | None = None
    ) -> list[str]: ...

    async def edit_text(self, chat_id: str, message_id: str, text: str) -> None: ...

    async def delete_message(self, chat_id: str, message_id: str) -> None: ...


class TelegramProgress:
    """Best-effort, ephemeral task status shown while Codex is running."""

    _MIN_EDIT_INTERVAL = 1.5

    def __init__(self, client: object, chat_id: str) -> None:
        self._client = client
        self._chat_id = chat_id
        self._message_id: str | None = None
        self._last_stage: str | None = None
        self._last_edit_at = 0.0

    @property
    def active(self) -> bool:
        return self._message_id is not None

    async def start(self) -> bool:
        sender = getattr(self._client, "send_text", None)
        if not callable(sender):
            return False
        try:
            message_ids = await sender(self._chat_id, self._render("正在分析请求…"))
        except (TelegramError, TypeError, ValueError) as error:
            LOGGER.debug("could not send Telegram progress message: %s", error)
            return False
        if isinstance(message_ids, list) and message_ids:
            self._message_id = str(message_ids[0])
            self._last_stage = "正在分析请求…"
            self._last_edit_at = monotonic()
            return True
        return False

    async def update(self, stage: str) -> None:
        if self._message_id is None or not stage or stage == self._last_stage:
            return
        now = monotonic()
        if now - self._last_edit_at < self._MIN_EDIT_INTERVAL:
            return
        editor = getattr(self._client, "edit_text", None)
        if not callable(editor):
            return
        try:
            await editor(self._chat_id, self._message_id, self._render(stage))
        except (TelegramError, TypeError, ValueError) as error:
            LOGGER.debug("could not update Telegram progress message: %s", error)
            return
        self._last_stage = stage
        self._last_edit_at = now

    async def finish(self) -> None:
        message_id = self._message_id
        self._message_id = None
        if message_id is None:
            return
        deleter = getattr(self._client, "delete_message", None)
        if not callable(deleter):
            return
        try:
            await deleter(self._chat_id, message_id)
        except (TelegramError, TypeError, ValueError) as error:
            LOGGER.debug("could not remove Telegram progress message: %s", error)

    @staticmethod
    def _render(stage: str) -> str:
        return f"⏳ 正在处理你的请求…\n\n阶段：{stage}"
