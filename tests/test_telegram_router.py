from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from codexrelay.codex.model_catalog import CodexModelCatalog, CodexModelOption
from codexrelay.connectors.base import ImageAttachment, IncomingMessage
from codexrelay.connectors.telegram.api import TelegramClient
from codexrelay.connectors.telegram.router import TelegramRouter
from codexrelay.core import RelayService
from codexrelay.database import Database
from codexrelay.pairing import PairingService
from codexrelay.projects import ProjectService


def model_catalog() -> CodexModelCatalog:
    return CodexModelCatalog(
        (
            CodexModelOption(
                model="gpt-fast",
                display_name="GPT Fast",
                description="Fast model",
                default_reasoning_effort="medium",
                supported_reasoning_efforts=("low", "medium", "high"),
                is_default=True,
            ),
            CodexModelOption(
                model="gpt-deep",
                display_name="GPT Deep",
                description="Deep model",
                default_reasoning_effort="high",
                supported_reasoning_efforts=("medium", "high", "xhigh"),
            ),
        )
    )


class UnusedTelegramClient:
    async def get_file_path(self, _file_id: str) -> str:
        raise AssertionError("no image expected")


class UnusedRelay:
    async def run_project(self, **_kwargs: Any) -> None:
        raise AssertionError("commands should not run Codex")


class ImageTelegramClient:
    async def get_file_path(self, file_id: str) -> str:
        assert file_id == "photo-1"
        return "photos/photo-1.jpg"

    async def download_file(
        self, *, file_path: str, destination: Path, max_bytes: int
    ) -> int:
        assert file_path == "photos/photo-1.jpg"
        assert max_bytes == 1024
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"telegram-image")
        return len(b"telegram-image")


class ImageRelay:
    def __init__(self) -> None:
        self.image_path: Path | None = None
        self.image_bytes: bytes | None = None

    async def run_project(self, **kwargs: Any) -> None:
        image_paths = cast(tuple[Path, ...], kwargs["image_paths"])
        assert len(image_paths) == 1
        self.image_path = image_paths[0]
        self.image_bytes = image_paths[0].read_bytes()

    async def interrupt_active(self) -> bool:
        return False


class BlockingRelay:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.calls: list[str] = []
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.active_count = 0
        self.maximum_active_count = 0

    async def run_project(self, **kwargs: Any) -> None:
        project_id = cast(str, kwargs["project_id"])
        conversation = await self.database.get_or_create_active_conversation(project_id)
        job_id, _message = await self.database.create_queued_job_with_input(
            conversation_id=conversation.id,
            text="blocking test task",
        )
        await self.database.mark_job_starting(job_id)
        await self.database.mark_turn_started(
            job_id,
            f"thread-{project_id}",
            f"turn-{len(self.calls) + 1}",
        )
        self.calls.append(project_id)
        self.active_count += 1
        self.maximum_active_count = max(self.maximum_active_count, self.active_count)
        try:
            if len(self.calls) == 1:
                self.first_started.set()
                await self.release_first.wait()
        finally:
            self.active_count -= 1
            await self.database.mark_job_interrupted(job_id)

    async def interrupt_active(self) -> bool:
        return False


class CallbackTelegramClient:
    def __init__(self) -> None:
        self.answers: list[tuple[str, str]] = []

    async def answer_callback_query(self, callback_query_id: str, text: str) -> None:
        self.answers.append((callback_query_id, text))


class CallbackResolver:
    def __init__(self, decision: Literal["accept", "decline"]) -> None:
        self.decision = decision

    async def resolve_callback(
        self, _callback_data: str
    ) -> Literal["accept", "decline"]:
        return self.decision


def message(text: str) -> IncomingMessage:
    return IncomingMessage(
        connector_type="telegram",
        account_id="main-bot",
        external_event_id="1",
        external_user_id="123",
        external_conversation_id="123",
        sender_display_name="Owner",
        text=text,
    )


async def authorize(database: Database) -> None:
    pairing = PairingService(database)
    challenge = await pairing.generate()
    await pairing.pair(
        code=challenge.code,
        external_user_id="123",
        external_conversation_id="123",
        display_name="Owner",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        ("accept", "已允许，本次审批已记录。"),
        ("decline", "已拒绝，本次审批已记录。"),
    ],
)
async def test_router_reports_the_recorded_approval_decision(
    tmp_path: Path,
    decision: Literal["accept", "decline"],
    expected: str,
) -> None:
    async with Database(tmp_path / "state.db") as database:
        await authorize(database)
        client = CallbackTelegramClient()
        router = TelegramRouter(
            database=database,
            client=cast(TelegramClient, client),
            relay=cast(RelayService, UnusedRelay()),
            pairing=PairingService(database),
            project_service=ProjectService(database),
            temporary_directory=tmp_path / "temp",
            approval_resolver=CallbackResolver(decision),
        )
        incoming = IncomingMessage(
            connector_type="telegram",
            account_id="main-bot",
            external_event_id="callback-event",
            external_user_id="123",
            external_conversation_id="123",
            sender_display_name="Owner",
            text="",
            callback_data=f"{decision}:nonce",
            callback_query_id="callback-1",
        )

        await router.handle("event-callback", incoming)

        assert client.answers == [("callback-1", expected)]
        pending = await database.pending_outbound_messages(
            connector_type="telegram", account_id="main-bot"
        )
        assert len(pending) == 1
        assert expected in pending[0].payload_json


@pytest.mark.asyncio
async def test_router_lists_and_switches_registered_projects(tmp_path: Path) -> None:
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        projects = ProjectService(database)
        await projects.register(first_path, "First")
        second = await projects.register(second_path, "Second")
        await authorize(database)
        router = TelegramRouter(
            database=database,
            client=cast(TelegramClient, UnusedTelegramClient()),
            relay=cast(RelayService, UnusedRelay()),
            pairing=PairingService(database),
            project_service=projects,
            temporary_directory=tmp_path / "temp",
        )

        await router.handle("event-1", message("/projects"))
        await router.handle("event-2", message("/use 2"))

        current = await database.current_project()
        assert current is not None
        assert current.id == second.id

        await router.handle("event-3", message("/use 1"))

        current = await database.current_project()
        assert current is not None
        assert current.id != second.id
        assert current.name == "First"
        cursor = await database.connection.execute(
            "SELECT payload_json FROM outbound_messages ORDER BY created_at, rowid"
        )
        replies = [str(row["payload_json"]) for row in await cursor.fetchall()]
        assert "First" in replies[0]
        assert "Second" in replies[0]
        assert "Second" in replies[1]
        assert "First" in replies[2]


@pytest.mark.asyncio
async def test_router_selects_model_and_reasoning_for_current_project_conversation(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        project = await database.add_project(project_path, "Relay")
        await authorize(database)
        router = TelegramRouter(
            database=database,
            client=cast(TelegramClient, UnusedTelegramClient()),
            relay=cast(RelayService, UnusedRelay()),
            pairing=PairingService(database),
            project_service=ProjectService(database),
            temporary_directory=tmp_path / "temp",
            model_catalog=model_catalog(),
        )

        await router.handle("event-models", message("/models"))
        await router.handle("event-model", message("/model 2"))
        await router.handle("event-reasoning", message("/reasoning xhigh"))
        await router.handle("event-status", message("/status"))

        conversation = await database.active_conversation(project.id)
        assert conversation is not None
        assert conversation.model == "gpt-deep"
        assert conversation.reasoning_effort == "xhigh"
        cursor = await database.connection.execute(
            "SELECT payload_json FROM outbound_messages ORDER BY created_at, rowid"
        )
        replies = [str(row["payload_json"]) for row in await cursor.fetchall()]
        assert "GPT Deep" in replies[0]
        assert "既有上下文保持不变" in replies[1]
        assert "xhigh" in replies[2]
        assert "模型：GPT Deep (gpt-deep)" in replies[3]


@pytest.mark.asyncio
async def test_router_requires_the_single_task_to_finish_before_switching_projects(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        projects = ProjectService(database)
        first = await projects.register(first_path, "First")
        second = await projects.register(second_path, "Second")
        await authorize(database)
        relay = BlockingRelay(database)
        router = TelegramRouter(
            database=database,
            client=cast(TelegramClient, UnusedTelegramClient()),
            relay=cast(RelayService, relay),
            pairing=PairingService(database),
            project_service=projects,
            temporary_directory=tmp_path / "temp",
        )

        first_task = asyncio.create_task(router.handle("event-first", message("first task")))
        await relay.first_started.wait()
        await router.handle("event-use", message("/use 2"))

        assert relay.calls == [first.id]
        assert relay.maximum_active_count == 1
        current = await database.current_project()
        assert current is not None and current.id == first.id

        relay.release_first.set()
        await first_task
        await router.handle("event-use-retry", message("/use 2"))
        await router.handle("event-second", message("second task"))

        assert relay.calls == [first.id, second.id]
        assert relay.maximum_active_count == 1


@pytest.mark.asyncio
async def test_switch_reply_explains_that_running_tasks_block_project_changes(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        projects = ProjectService(database)
        first = await projects.register(first_path, "First")
        await projects.register(second_path, "Second")
        conversation = await database.get_or_create_active_conversation(first.id)
        job_id, _message = await database.create_queued_job_with_input(
            conversation_id=conversation.id,
            text="running",
        )
        await database.mark_job_starting(job_id)
        await database.mark_turn_started(job_id, "thread-first", "turn-first")
        await authorize(database)
        router = TelegramRouter(
            database=database,
            client=cast(TelegramClient, UnusedTelegramClient()),
            relay=cast(RelayService, UnusedRelay()),
            pairing=PairingService(database),
            project_service=projects,
            temporary_directory=tmp_path / "temp",
        )

        await router.handle("event-use", message("/use 2"))
        await router.handle("event-status", message("/status"))

        pending = await database.pending_outbound_messages(
            connector_type="telegram", account_id="main-bot"
        )
        assert len(pending) == 2
        assert "切换失败：任务运行期间不能切换项目" in pending[0].payload_json
        assert "当前项目：First" in pending[1].payload_json
        assert "任务所属项目：First" in pending[1].payload_json


@pytest.mark.asyncio
async def test_unpaired_user_can_pair_with_one_time_code(tmp_path: Path) -> None:
    async with Database(tmp_path / "state.db") as database:
        pairing = PairingService(database)
        challenge = await pairing.generate()
        router = TelegramRouter(
            database=database,
            client=cast(TelegramClient, UnusedTelegramClient()),
            relay=cast(RelayService, UnusedRelay()),
            pairing=pairing,
            project_service=ProjectService(database),
            temporary_directory=tmp_path / "temp",
        )

        await router.handle("event-1", message(f"/pair {challenge.code}"))

        assert await database.is_authorized_identity(
            connector_type="telegram", account_id="main-bot", external_user_id="123"
        )


@pytest.mark.asyncio
async def test_router_downloads_image_for_codex_then_removes_temporary_copy(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        await database.add_project(project_path)
        await authorize(database)
        relay = ImageRelay()
        router = TelegramRouter(
            database=database,
            client=cast(TelegramClient, ImageTelegramClient()),
            relay=cast(RelayService, relay),
            pairing=PairingService(database),
            project_service=ProjectService(database),
            temporary_directory=tmp_path / "temp",
            max_image_bytes=1024,
        )
        incoming = IncomingMessage(
            connector_type="telegram",
            account_id="main-bot",
            external_event_id="image-event",
            external_user_id="123",
            external_conversation_id="123",
            sender_display_name="Owner",
            text="inspect",
            images=(ImageAttachment("photo-1", "image/jpeg", "photo.jpg"),),
        )

        await router.handle("event-image", incoming)

        assert relay.image_bytes == b"telegram-image"
        assert relay.image_path is not None
        assert not relay.image_path.exists()


@pytest.mark.asyncio
async def test_recovered_inbox_does_not_replay_an_uncertain_prior_job(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        project = await database.add_project(project_path)
        await authorize(database)
        event_id, _inserted = await database.ingest_event(
            connector_type="telegram",
            account_id="main-bot",
            external_event_id="88",
            payload={"update_id": 88},
            cursor_name="update_offset",
            cursor_value="89",
        )
        conversation = await database.get_or_create_active_conversation(project.id)
        job_id, _message = await database.create_queued_job_with_input(
            conversation_id=conversation.id,
            text="do not replay",
            inbound_event_id=event_id,
        )
        await database.mark_job_starting(job_id)
        await database.interrupt_stale_jobs()
        router = TelegramRouter(
            database=database,
            client=cast(TelegramClient, UnusedTelegramClient()),
            relay=cast(RelayService, UnusedRelay()),
            pairing=PairingService(database),
            project_service=ProjectService(database),
            temporary_directory=tmp_path / "temp",
        )

        await router.handle(event_id, message("do the work"))

        pending = await database.pending_outbound_messages(
            connector_type="telegram", account_id="main-bot"
        )
        assert len(pending) == 1
        assert "没有自动重放" in pending[0].payload_json
