from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from codexrelay.connectors.base import IncomingMessage
from codexrelay.connectors.telegram.poller import TelegramPoller
from codexrelay.database import Database


class FakeUpdateClient:
    def __init__(self, updates: list[dict[str, Any]]) -> None:
        self.updates = updates
        self.offsets: list[int | None] = []

    async def get_updates(
        self, *, offset: int | None, poll_timeout: int = 30, limit: int = 100
    ) -> list[dict[str, Any]]:
        self.offsets.append(offset)
        return self.updates


class BlockingUpdateClient:
    def __init__(self) -> None:
        self.cancelled = False
        self.started = asyncio.Event()
        self._never = asyncio.Event()

    async def get_updates(
        self, *, offset: int | None, poll_timeout: int = 30, limit: int = 100
    ) -> list[dict[str, Any]]:
        del offset, poll_timeout, limit
        try:
            self.started.set()
            await self._never.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return []


class EmptyUpdateClient:
    async def get_updates(
        self, *, offset: int | None, poll_timeout: int = 30, limit: int = 100
    ) -> list[dict[str, Any]]:
        del offset, poll_timeout, limit
        return []


@pytest.mark.asyncio
async def test_poller_persists_cursor_and_deduplicates(tmp_path: Path) -> None:
    update = {
        "update_id": 10,
        "message": {
            "from": {"id": 123, "first_name": "Owner"},
            "chat": {"id": 123, "type": "private"},
            "text": "hello",
        },
    }
    client = FakeUpdateClient([update])
    received: list[tuple[str, IncomingMessage]] = []

    async def handler(event_id: str, message: IncomingMessage) -> None:
        received.append((event_id, message))

    async with Database(tmp_path / "state.db") as database:
        poller = TelegramPoller(database=database, client=client)
        assert await poller.poll_once(handler, poll_timeout=0) == 1
        assert await poller.poll_once(handler, poll_timeout=0) == 0

        assert len(received) == 1
        assert received[0][1].sender_display_name == "Owner"
        assert client.offsets == [None, 11]
        cursor = await database.connector_cursor(
            connector_type="telegram", account_id="main-bot", cursor_name="update_offset"
        )
        assert cursor == "11"


@pytest.mark.asyncio
async def test_failed_handler_is_recoverable_without_stopping_batch(tmp_path: Path) -> None:
    updates = [
        {
            "update_id": update_id,
            "message": {
                "from": {"id": 123, "first_name": "Owner"},
                "chat": {"id": 123, "type": "private"},
                "text": f"message {update_id}",
            },
        }
        for update_id in (1, 2)
    ]
    handled: list[str] = []

    async def handler(_event_id: str, message: IncomingMessage) -> None:
        if message.external_event_id == "1":
            raise RuntimeError("temporary failure")
        handled.append(message.external_event_id)

    async with Database(tmp_path / "state.db") as database:
        poller = TelegramPoller(database=database, client=FakeUpdateClient(updates))
        assert await poller.poll_once(handler, poll_timeout=0) == 2
        assert handled == ["2"]
        pending = await database.pending_inbound_events(
            connector_type="telegram", account_id="main-bot"
        )
        assert len(pending) == 1
        assert pending[0][1]["update_id"] == 1


@pytest.mark.asyncio
async def test_poller_cancels_long_poll_when_stop_is_requested(tmp_path: Path) -> None:
    client = BlockingUpdateClient()

    async def handler(_event_id: str, _message: IncomingMessage) -> None:
        raise AssertionError("no update should be dispatched")

    async with Database(tmp_path / "state.db") as database:
        poller = TelegramPoller(database=database, client=client)
        stop = asyncio.Event()
        task = asyncio.create_task(poller.run(handler, stop))
        await asyncio.wait_for(client.started.wait(), timeout=1)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    assert client.cancelled


@pytest.mark.asyncio
async def test_connection_restores_before_any_backlogged_dispatch(tmp_path: Path) -> None:
    order: list[str] = []

    async def lost(_started_at: object) -> None:
        order.append("lost")

    async def restored(_started_at: object) -> None:
        order.append("restored")

    async def handler(_event_id: str, _message: IncomingMessage) -> None:
        order.append("message")

    async with Database(tmp_path / "state.db") as database:
        poller = TelegramPoller(
            database=database,
            client=FakeUpdateClient(
                [
                    {
                        "update_id": 7,
                        "message": {
                            "from": {"id": 123, "first_name": "Owner"},
                            "chat": {"id": 123, "type": "private"},
                            "text": "queued",
                        },
                    }
                ]
            ),
            on_connection_lost=lost,
            on_connection_restored=restored,
            disconnect_threshold=0,
        )
        await poller._mark_connection_lost()
        await poller.poll_once(handler, poll_timeout=0)

    assert order == ["lost", "restored", "message"]
