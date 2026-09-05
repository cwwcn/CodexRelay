from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from codexrelay.approval import ApprovalCoordinator
from codexrelay.codex.app_server import AppServerBackend
from codexrelay.codex.model_catalog import CodexModelCatalog
from codexrelay.connectors.telegram.api import TelegramClient, TelegramError
from codexrelay.connectors.telegram.commands import (
    TELEGRAM_PRIVATE_COMMAND_SCOPE,
    bot_api_commands,
)
from codexrelay.connectors.telegram.outbox import TelegramOutbox
from codexrelay.connectors.telegram.poller import TelegramPoller
from codexrelay.connectors.telegram.router import TelegramRouter
from codexrelay.core import RelayService
from codexrelay.database import Database
from codexrelay.models import LifecycleState
from codexrelay.pairing import PairingService
from codexrelay.paths import AppPaths
from codexrelay.projects import ProjectService
from codexrelay.secrets import SecretStore
from codexrelay.session_sync import SessionSynchronizer
from codexrelay.settings import Settings, SettingsStore
from codexrelay.sleep import SleepInhibitor

LOGGER = logging.getLogger("codexrelay.runtime")


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    bot_id: str
    bot_username: str


class CodexRelayRuntime:
    def __init__(self, paths: AppPaths | None = None) -> None:
        self.paths = paths or AppPaths.default()
        self.settings: Settings | None = None
        self.database: Database | None = None
        self.backend: AppServerBackend | None = None
        self.telegram: TelegramClient | None = None
        self.relay: RelayService | None = None
        self.poller: TelegramPoller | None = None
        self.outbox: TelegramOutbox | None = None
        self.router: TelegramRouter | None = None
        self.sleep_inhibitor: SleepInhibitor | None = None
        self.identity: RuntimeIdentity | None = None
        self.approvals: ApprovalCoordinator | None = None
        self.model_catalog: CodexModelCatalog | None = None
        self.online_since: datetime | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._last_wake_at: datetime | None = None

    async def start(self) -> RuntimeIdentity:
        if self.database is not None:
            if self.identity is None:
                raise RuntimeError("runtime is partially initialized")
            return self.identity
        self.paths.ensure()
        settings = SettingsStore(self.paths.settings).load()
        token = await SecretStore(self.paths.data_dir / "telegram-tokens.json").get_telegram_token(
            settings.telegram.account_id
        )
        if token is None:
            raise RuntimeError("Telegram Bot Token is not configured in CodexRelay")

        database = Database(self.paths.database)
        await database.open()
        telegram = TelegramClient(token)
        approvals = ApprovalCoordinator(
            database=database,
            loop=asyncio.get_running_loop(),
            account_id=settings.telegram.account_id,
        )
        backend = AppServerBackend(approval_handler=approvals.handle_sync)
        sleep_inhibitor = SleepInhibitor(enabled=settings.app.prevent_sleep_while_running)
        try:
            await database.expire_pending_approvals()
            await database.interrupt_stale_jobs()
            await database.clear_stale_conversation_locks()
            await database.housekeep()
            for project in await database.list_projects():
                try:
                    ProjectService.preflight_access(project.path)
                except PermissionError as error:
                    LOGGER.warning(
                        "project access preflight failed for %s: %s", project.path, error
                    )
            await backend.start()
            model_catalog = await backend.model_catalog()
            bot = await telegram.get_me()
            try:
                await telegram.set_my_commands(
                    bot_api_commands(),
                    scope=TELEGRAM_PRIVATE_COMMAND_SCOPE,
                )
            except TelegramError as error:
                # Native command suggestions are useful, but their absence must
                # not prevent an otherwise valid relay from starting.
                LOGGER.warning("could not register Telegram command menu: %s", error)
            await telegram.delete_webhook()
        except BaseException:
            await telegram.close()
            await backend.stop()
            await database.close()
            await sleep_inhibitor.close()
            raise

        relay = RelayService(
            database=database,
            backend=backend,
            sleep_inhibitor=sleep_inhibitor,
        )
        pairing = PairingService(database)
        router = TelegramRouter(
            database=database,
            client=telegram,
            relay=relay,
            pairing=pairing,
            project_service=ProjectService(database),
            temporary_directory=self.paths.data_dir / "temporary",
            max_image_bytes=settings.telegram.max_image_bytes,
            approval_resolver=approvals,
            model_catalog=model_catalog,
            codex_backend=backend,
            release_codex_connection=self.release_codex_connection,
        )
        poller = TelegramPoller(
            database=database,
            client=telegram,
            account_id=settings.telegram.account_id,
            on_connection_lost=self.notify_transport_lost,
            on_connection_restored=self.notify_transport_restored,
        )
        outbox = TelegramOutbox(
            database=database,
            client=telegram,
            account_id=settings.telegram.account_id,
        )
        identity = RuntimeIdentity(
            bot_id=str(bot.get("id", "")),
            bot_username=str(bot.get("username", "")),
        )
        self.settings = settings
        self.database = database
        self.backend = backend
        self.telegram = telegram
        self.sleep_inhibitor = sleep_inhibitor
        self.relay = relay
        self.router = router
        self.poller = poller
        self.outbox = outbox
        self.identity = identity
        self.approvals = approvals
        self.model_catalog = model_catalog
        previous_lifecycle = await database.lifecycle_state()
        online_since = datetime.now(UTC)
        self.online_since = online_since
        await database.record_lifecycle(
            "runtime_started",
            state="online",
            reason="application_started",
            occurred_at=online_since.isoformat(timespec="milliseconds"),
            started_at=online_since.isoformat(timespec="milliseconds"),
            offline_since=None,
        )
        router.set_online_since(online_since)
        await self._queue_startup_notification(previous_lifecycle)
        return identity

    async def run(self, stop: asyncio.Event) -> None:
        await self.start()
        if self.poller is None or self.router is None or self.outbox is None:
            raise RuntimeError("runtime components are missing")
        poller_task = asyncio.create_task(self.poller.run(self.router.handle, stop))
        outbox_task = asyncio.create_task(self._run_outbox(stop))
        session_sync_task = asyncio.create_task(self._run_session_sync(stop))
        heartbeat_task = asyncio.create_task(self._run_heartbeat(stop))
        try:
            await stop.wait()
            if self.relay is not None:
                await self.relay.interrupt_active()
            await asyncio.gather(poller_task, outbox_task, session_sync_task, heartbeat_task)
        finally:
            for task in (poller_task, outbox_task, session_sync_task, heartbeat_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                poller_task,
                outbox_task,
                session_sync_task,
                heartbeat_task,
                return_exceptions=True,
            )
            await self.stop()

    async def stop(self) -> None:
        telegram, self.telegram = self.telegram, None
        backend, self.backend = self.backend, None
        database, self.database = self.database, None
        inhibitor, self.sleep_inhibitor = self.sleep_inhibitor, None
        self.settings = None
        self.relay = None
        self.router = None
        self.poller = None
        self.outbox = None
        self.identity = None
        self.approvals = None
        self.model_catalog = None
        self.online_since = None
        if telegram is not None:
            await telegram.close()
        if backend is not None:
            await backend.stop()
        if inhibitor is not None:
            await inhibitor.close()
        if database is not None:
            await database.close()

    async def release_codex_connection(self) -> None:
        """Release Codex writer leases so the desktop client can take over."""
        backend = self.backend
        if backend is None:
            raise RuntimeError("Codex 服务尚未连接")
        await backend.stop()
        await backend.start()

    async def notify_sleep(self) -> None:
        """Record impending system sleep and notify the paired Telegram chat."""
        async with self._lifecycle_lock:
            database = self.database
            telegram = self.telegram
            settings = self.settings
            if database is None:
                return
            now = datetime.now(UTC)
            if self._last_wake_at is not None and (now - self._last_wake_at).total_seconds() < 15:
                return
            self._last_wake_at = now
            await database.record_lifecycle(
                "system_sleep",
                state="offline",
                reason="system_sleep",
                occurred_at=now.isoformat(timespec="milliseconds"),
                offline_since=now.isoformat(timespec="milliseconds"),
            )
            if settings is not None and settings.app.lifecycle_notifications:
                await self._send_lifecycle_text(
                    telegram,
                    "CodexRelay 已离线\nMac 即将进入睡眠，暂时无法接收任务。\n"
                    f"时间：{_local_time(now)}",
                )

    async def notify_wake(self) -> None:
        """Re-establish readiness after wake and announce the recovered state."""
        async with self._lifecycle_lock:
            database = self.database
            router = self.router
            if database is None:
                return
            now = datetime.now(UTC)
            previous = await database.lifecycle_state()
            offline_since = _parse_timestamp(None if previous is None else previous.offline_since)
            if router is not None:
                router.set_online_since(now)
            self.online_since = now
            await database.record_lifecycle(
                "system_wake",
                state="recovering",
                reason="system_wake",
                occurred_at=now.isoformat(timespec="milliseconds"),
                started_at=now.isoformat(timespec="milliseconds"),
                offline_since=(
                    offline_since.isoformat(timespec="milliseconds")
                    if offline_since is not None
                    else None
                ),
            )
            await self._recover_and_notify(offline_since)

    async def notify_shutdown(self) -> None:
        """Best-effort notice for an explicit user-confirmed application exit."""
        async with self._lifecycle_lock:
            database = self.database
            if database is None:
                return
            now = datetime.now(UTC)
            await database.record_lifecycle(
                "runtime_stopped",
                state="offline",
                reason="application_quit",
                occurred_at=now.isoformat(timespec="milliseconds"),
                offline_since=now.isoformat(timespec="milliseconds"),
            )
            if self.settings is not None and self.settings.app.lifecycle_notifications:
                await self._send_lifecycle_text(
                    self.telegram,
                    "CodexRelay 已离线\nMac 端应用已退出，暂时无法接收任务。\n"
                    f"时间：{_local_time(now)}",
                )

    async def notify_transport_lost(self, disconnected_at: datetime) -> None:
        database = self.database
        if database is None:
            return
        await database.record_lifecycle(
            "telegram_disconnected",
            state="offline",
            reason="telegram_disconnected",
            occurred_at=disconnected_at.isoformat(timespec="milliseconds"),
            offline_since=disconnected_at.isoformat(timespec="milliseconds"),
        )

    async def notify_transport_restored(self, disconnected_at: datetime) -> None:
        async with self._lifecycle_lock:
            database = self.database
            router = self.router
            if database is None:
                return
            now = datetime.now(UTC)
            if self._last_wake_at is not None and (now - self._last_wake_at).total_seconds() < 15:
                return
            self._last_wake_at = now
            self.online_since = now
            if router is not None:
                router.set_online_since(now)
            await database.record_lifecycle(
                "telegram_reconnected",
                state="recovering",
                reason="telegram_reconnected",
                occurred_at=now.isoformat(timespec="milliseconds"),
                started_at=now.isoformat(timespec="milliseconds"),
                offline_since=disconnected_at.isoformat(timespec="milliseconds"),
            )
            await self._recover_and_notify(
                disconnected_at,
                recovery_text=(
                    "CodexRelay 已重新连接\n"
                    "Telegram 与 Codex 均已就绪，现在可以继续发送任务。"
                ),
            )

    async def _recover_and_notify(
        self,
        offline_since: datetime | None,
        *,
        recovery_text: str = (
            "CodexRelay 已恢复\n"
            "Telegram 与 Codex 均已就绪，现在可以继续发送任务。"
        ),
    ) -> None:
        database = self.database
        telegram = self.telegram
        backend = self.backend
        settings = self.settings
        if database is None:
            return
        last_error: Exception | None = None
        for delay in (0, 3, 7, 15, 30):
            if delay:
                await asyncio.sleep(delay)
            try:
                if telegram is None or backend is None:
                    raise RuntimeError("runtime components are unavailable")
                await telegram.get_me()
                await backend.model_catalog()
                now = datetime.now(UTC)
                await database.record_lifecycle(
                    "runtime_ready",
                    state="online",
                    reason="connections_ready",
                    occurred_at=now.isoformat(timespec="milliseconds"),
                    started_at=(
                        self.online_since.isoformat(timespec="milliseconds")
                        if self.online_since is not None
                        else None
                    ),
                    offline_since=None,
                )
                if settings is not None and settings.app.lifecycle_notifications:
                    duration = _format_duration(now - offline_since) if offline_since else None
                    detail = f"\n离线约 {duration}。" if duration else ""
                    await self._send_lifecycle_text(
                        telegram,
                        recovery_text + detail,
                    )
                return
            except Exception as error:
                last_error = error
        LOGGER.warning("connections did not recover after wake: %s", last_error)
        await database.record_lifecycle(
            "runtime_recovery_failed",
            state="recovering",
            reason=type(last_error).__name__ if last_error is not None else "unknown",
            offline_since=(
                offline_since.isoformat(timespec="milliseconds")
                if offline_since is not None
                else None
            ),
        )

    async def _queue_startup_notification(self, previous: LifecycleState | None) -> None:
        database = self.database
        settings = self.settings
        if database is None or settings is None or not settings.app.lifecycle_notifications:
            return
        conversation_id = await database.authorized_conversation_id(
            connector_type="telegram", account_id=settings.telegram.account_id
        )
        if conversation_id is None:
            return
        now = datetime.now(UTC)
        previous_seen = _parse_timestamp(None if previous is None else previous.last_seen_at)
        previous_state = None if previous is None else previous.state
        if previous_state == "online" and previous_seen is not None:
            if (now - previous_seen).total_seconds() < 30:
                return
        offline_since = _parse_timestamp(None if previous is None else previous.offline_since)
        offline_started = offline_since or previous_seen
        duration = _format_duration(now - offline_started) if offline_started else None
        suffix = f"\n离线约 {duration}。" if duration else ""
        await database.queue_text(
            connector_type="telegram",
            account_id=settings.telegram.account_id,
            external_conversation_id=conversation_id,
            text="CodexRelay 已上线\nTelegram 与 Codex 均已就绪。" + suffix,
        )

    async def _send_lifecycle_text(self, telegram: TelegramClient | None, text: str) -> None:
        database = self.database
        settings = self.settings
        if database is None or settings is None or telegram is None:
            return
        chat_id = await database.authorized_conversation_id(
            connector_type="telegram", account_id=settings.telegram.account_id
        )
        if chat_id is None:
            return
        try:
            await asyncio.wait_for(telegram.send_text(chat_id, text), timeout=4)
        except Exception as error:
            LOGGER.warning("could not deliver lifecycle notice immediately: %s", error)

    async def _run_heartbeat(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if self.database is not None:
                await self.database.heartbeat()
            try:
                await asyncio.wait_for(stop.wait(), timeout=15)
            except TimeoutError:
                pass

    async def _run_outbox(self, stop: asyncio.Event) -> None:
        if self.outbox is None:
            raise RuntimeError("outbox is not initialized")
        while not stop.is_set():
            await self.outbox.dispatch_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.75)
            except TimeoutError:
                pass

    async def _run_session_sync(self, stop: asyncio.Event) -> None:
        """Periodically reconcile Codex threads without disturbing Telegram polling."""
        if self.database is not None and self.backend is not None:
            await self._sync_registered_projects(self.database, self.backend)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=60)
            except TimeoutError:
                if self.database is not None and self.backend is not None:
                    await self._sync_registered_projects(self.database, self.backend)

    @staticmethod
    async def _sync_registered_projects(database: Database, backend: AppServerBackend) -> None:
        try:
            await SessionSynchronizer(database, backend).sync_all()
        except Exception as error:
            # A failed complete listing must never hide the last known snapshot.
            LOGGER.warning("could not reconcile Codex conversations: %s", error)


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _local_time(value: datetime) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


def _format_duration(value: timedelta) -> str:
    seconds = max(int(value.total_seconds()), 0)
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours} 小时 {minutes} 分钟"
    if minutes:
        return f"{minutes} 分钟"
    return "不到 1 分钟"
