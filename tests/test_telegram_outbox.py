from pathlib import Path

import pytest

from codexrelay.connectors.telegram.outbox import TelegramOutbox
from codexrelay.database import Database


class FakeMessageClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(
        self, chat_id: str, text: str, *, reply_markup: dict[str, object] | None = None
    ) -> list[str]:
        assert reply_markup is None
        self.sent.append((chat_id, text))
        return ["77"]


@pytest.mark.asyncio
async def test_outbox_marks_message_delivered(tmp_path: Path) -> None:
    client = FakeMessageClient()
    async with Database(tmp_path / "state.db") as database:
        outbound_id = await database.queue_text(
            connector_type="telegram",
            account_id="main-bot",
            external_conversation_id="123",
            text="hello",
        )

        assert await TelegramOutbox(database=database, client=client).dispatch_once() == 1

        cursor = await database.connection.execute(
            "SELECT status, external_message_id FROM outbound_messages WHERE id=?",
            (outbound_id,),
        )
        row = await cursor.fetchone()
        assert client.sent == [("123", "hello")]
        assert row is not None
        assert row["status"] == "delivered"
        assert row["external_message_id"] == "77"
