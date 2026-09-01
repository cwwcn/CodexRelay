from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Protocol

from codexrelay.connectors.telegram.api import TelegramAPIError, TelegramError
from codexrelay.database import Database


class MessageClient(Protocol):
    async def send_text(
        self, chat_id: str, text: str, *, reply_markup: dict[str, object] | None = None
    ) -> list[str]: ...


class TelegramOutbox:
    def __init__(
        self,
        *,
        database: Database,
        client: MessageClient,
        account_id: str = "main-bot",
        max_attempts: int = 8,
    ) -> None:
        self.database = database
        self.client = client
        self.account_id = account_id
        self.max_attempts = max_attempts

    async def dispatch_once(self) -> int:
        messages = await self.database.pending_outbound_messages(
            connector_type="telegram", account_id=self.account_id
        )
        for message in messages:
            try:
                payload = json.loads(message.payload_json)
                text = payload["text"]
                if not isinstance(text, str):
                    raise ValueError("outbox text is not a string")
                reply_markup = payload.get("reply_markup")
                if reply_markup is not None and not isinstance(reply_markup, dict):
                    raise ValueError("outbox reply markup is not an object")
                external_ids = await self.client.send_text(
                    message.external_conversation_id,
                    text,
                    reply_markup=reply_markup,
                )
                await self.database.mark_outbound_delivered(message.id, ",".join(external_ids))
            except (TelegramError, ValueError, KeyError, json.JSONDecodeError) as error:
                attempts = message.attempt_count + 1
                retry_after = error.retry_after if isinstance(error, TelegramAPIError) else None
                delay = retry_after or min(2**attempts, 300)
                next_retry = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat(
                    timespec="milliseconds"
                )
                await self.database.mark_outbound_retry(
                    message.id,
                    next_retry_at=next_retry,
                    terminal=attempts >= self.max_attempts,
                )
        return len(messages)
