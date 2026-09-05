from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from codexrelay.connectors.base import IncomingMessage
from codexrelay.connectors.telegram.api import (
    TelegramAPIError,
    TelegramTransportError,
    parse_incoming_message,
)
from codexrelay.database import Database


class UpdateClient(Protocol):
    async def get_updates(
        self, *, offset: int | None, poll_timeout: int = 30, limit: int = 100
    ) -> list[dict[str, Any]]: ...


MessageHandler = Callable[[str, IncomingMessage], Awaitable[None]]
ConnectionHandler = Callable[[datetime], Awaitable[None]]


class TelegramPoller:
    def __init__(
        self,
        *,
        database: Database,
        client: UpdateClient,
        account_id: str = "main-bot",
        on_connection_lost: ConnectionHandler | None = None,
        on_connection_restored: ConnectionHandler | None = None,
        disconnect_threshold: float = 30,
    ) -> None:
        self.database = database
        self.client = client
        self.account_id = account_id
        self._handler_tasks: set[asyncio.Task[None]] = set()
        self.on_connection_lost = on_connection_lost
        self.on_connection_restored = on_connection_restored
        self.disconnect_threshold = disconnect_threshold
        self._disconnected_at: datetime | None = None
        self._disconnect_announced = False

    async def recover_pending(self, handler: MessageHandler) -> int:
        pending = await self.database.pending_inbound_events(
            connector_type="telegram", account_id=self.account_id
        )
        for event_id, update in pending:
            try:
                await self._dispatch(event_id, update, handler)
            except Exception:
                continue
        return len(pending)

    async def poll_once(
        self,
        handler: MessageHandler,
        *,
        poll_timeout: int = 30,
        background_handlers: bool = False,
    ) -> int:
        cursor = await self.database.connector_cursor(
            connector_type="telegram",
            account_id=self.account_id,
            cursor_name="update_offset",
        )
        offset = int(cursor) if cursor is not None else None
        updates = await self.client.get_updates(offset=offset, poll_timeout=poll_timeout)
        await self._restore_connection_before_dispatch()
        processed = 0
        for update in updates:
            update_id = update.get("update_id")
            if not isinstance(update_id, int):
                continue
            event_id, inserted = await self.database.ingest_event(
                connector_type="telegram",
                account_id=self.account_id,
                external_event_id=str(update_id),
                payload=update,
                cursor_name="update_offset",
                cursor_value=str(update_id + 1),
            )
            if not inserted:
                continue
            processed += 1
            if background_handlers:
                self._spawn_dispatch(event_id, update, handler)
            else:
                try:
                    await self._dispatch(event_id, update, handler)
                except Exception:
                    continue
        return processed

    async def run(self, handler: MessageHandler, stop: asyncio.Event) -> None:
        await self.recover_pending(handler)
        delay = 1.0
        while not stop.is_set():
            poll_task = asyncio.create_task(self.poll_once(handler, background_handlers=True))
            stop_task = asyncio.create_task(stop.wait())
            done, _pending = await asyncio.wait(
                (poll_task, stop_task), return_when=asyncio.FIRST_COMPLETED
            )
            if stop_task in done:
                poll_task.cancel()
                await asyncio.gather(poll_task, return_exceptions=True)
                stop_task.cancel()
                await asyncio.gather(stop_task, return_exceptions=True)
                break
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
            try:
                await poll_task
                delay = 1.0
            except TelegramAPIError as error:
                delay = float(error.retry_after or min(delay * 2, 30))
            except TelegramTransportError:
                await self._mark_connection_lost()
                delay = min(delay * 2, 30)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
        if self._handler_tasks:
            await asyncio.gather(*tuple(self._handler_tasks), return_exceptions=True)

    async def wait_for_handlers(self) -> None:
        if self._handler_tasks:
            await asyncio.gather(*tuple(self._handler_tasks), return_exceptions=True)

    async def _mark_connection_lost(self) -> None:
        now = datetime.now(UTC)
        if self._disconnected_at is None:
            self._disconnected_at = now
        duration = (now - self._disconnected_at).total_seconds()
        if (
            duration >= self.disconnect_threshold
            and not self._disconnect_announced
            and self.on_connection_lost is not None
        ):
            self._disconnect_announced = True
            await self.on_connection_lost(self._disconnected_at)

    async def _restore_connection_before_dispatch(self) -> None:
        disconnected_at = self._disconnected_at
        if disconnected_at is None:
            return
        self._disconnected_at = None
        announced = self._disconnect_announced
        self._disconnect_announced = False
        if announced and self.on_connection_restored is not None:
            await self.on_connection_restored(disconnected_at)

    def _spawn_dispatch(
        self, event_id: str, update: dict[str, Any], handler: MessageHandler
    ) -> None:
        task = asyncio.create_task(self._dispatch(event_id, update, handler))
        self._handler_tasks.add(task)

        def finished(completed: asyncio.Task[None]) -> None:
            self._handler_tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finished)

    async def _dispatch(
        self, event_id: str, update: dict[str, Any], handler: MessageHandler
    ) -> None:
        message = parse_incoming_message(update, account_id=self.account_id)
        if message is None:
            await self.database.mark_inbound_processed(event_id)
            return
        try:
            await handler(event_id, message)
        except Exception as error:
            await self.database.mark_inbound_failed(event_id, type(error).__name__)
            raise
        else:
            await self.database.mark_inbound_processed(event_id)
