from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

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
from codexrelay.pairing import PairingService
from codexrelay.paths import AppPaths
from codexrelay.projects import ProjectService
from codexrelay.secrets import SecretStore
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
        return identity

    async def run(self, stop: asyncio.Event) -> None:
        await self.start()
        if self.poller is None or self.router is None or self.outbox is None:
            raise RuntimeError("runtime components are missing")
        poller_task = asyncio.create_task(self.poller.run(self.router.handle, stop))
        outbox_task = asyncio.create_task(self._run_outbox(stop))
        try:
            await stop.wait()
            if self.relay is not None:
                await self.relay.interrupt_active()
            await asyncio.gather(poller_task, outbox_task)
        finally:
            for task in (poller_task, outbox_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(poller_task, outbox_task, return_exceptions=True)
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

    async def _run_outbox(self, stop: asyncio.Event) -> None:
        if self.outbox is None:
            raise RuntimeError("outbox is not initialized")
        while not stop.is_set():
            await self.outbox.dispatch_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.75)
            except TimeoutError:
                pass
