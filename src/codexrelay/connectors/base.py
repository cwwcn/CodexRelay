from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ImageAttachment:
    external_id: str
    mime_type: str
    file_name: str | None = None


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    connector_type: str
    account_id: str
    external_event_id: str
    external_user_id: str
    external_conversation_id: str
    sender_display_name: str
    text: str
    images: tuple[ImageAttachment, ...] = ()
    callback_data: str | None = None
    callback_query_id: str | None = None
    sent_at: datetime | None = None


class Connector(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def messages(self) -> AsyncIterator[IncomingMessage]: ...

    async def send_text(self, conversation_id: str, text: str) -> str: ...
