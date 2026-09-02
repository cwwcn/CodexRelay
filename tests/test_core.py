from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from codexrelay.codex.base import TurnResult
from codexrelay.core import DeliveryTarget, RelayService
from codexrelay.database import Database
from codexrelay.models import ProjectApprovalMode


class FakeBackend:
    def __init__(self, *, final_text: str = "world") -> None:
        self.interrupted_turn_id: str | None = None
        self.final_text = final_text
        self.model: str | None = None
        self.reasoning_effort: str | None = None
        self.approval_mode = ProjectApprovalMode.SAFE

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def run_turn(
        self,
        *,
        project: Path,
        text: str,
        image_paths: tuple[Path, ...] = (),
        thread_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        approval_mode: ProjectApprovalMode = ProjectApprovalMode.SAFE,
        on_turn_started: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> TurnResult:
        assert project.is_dir()
        assert text == "hello"
        assert image_paths == ()
        assert thread_id is None
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.approval_mode = approval_mode
        if on_turn_started is not None:
            await on_turn_started("thread-1", "turn-1")
        return TurnResult(
            thread_id="thread-1", turn_id="turn-1", final_text=self.final_text
        )

    async def interrupt(self, turn_id: str) -> None:
        self.interrupted_turn_id = turn_id


class ProjectContextBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def run_turn(
        self,
        *,
        project: Path,
        text: str,
        image_paths: tuple[Path, ...] = (),
        thread_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        on_turn_started: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> TurnResult:
        del text, image_paths, model, reasoning_effort
        self.calls.append((project.name, thread_id))
        resolved_thread_id = thread_id or f"thread-{project.name}"
        turn_id = f"turn-{len(self.calls)}"
        if on_turn_started is not None:
            await on_turn_started(resolved_thread_id, turn_id)
        return TurnResult(
            thread_id=resolved_thread_id,
            turn_id=turn_id,
            final_text=f"reply from {project.name}",
        )

    async def interrupt(self, _turn_id: str) -> None:
        return None


class FakeLease:
    async def __aenter__(self) -> FakeLease:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class FakeSleepInhibitor:
    def __init__(self) -> None:
        self.leased = False

    def lease(self) -> FakeLease:
        self.leased = True
        return FakeLease()


@pytest.mark.asyncio
async def test_relay_persists_turn_before_result_and_queues_delivery(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        await database.add_project(project_path)
        inhibitor = FakeSleepInhibitor()
        backend = FakeBackend()
        service = RelayService(
            database=database,
            backend=backend,
            sleep_inhibitor=inhibitor,  # type: ignore[arg-type]
        )
        result = await service.run_current_project(
            text="hello",
            delivery=DeliveryTarget(
                connector_type="telegram",
                account_id="main",
                external_conversation_id="123",
            ),
        )

        cursor = await database.connection.execute(
            "SELECT status, codex_turn_id, output_message_id FROM jobs WHERE id=?",
            (result.job_id,),
        )
        job = await cursor.fetchone()
        conversation = await database.active_conversation(
            (await database.current_project()).id  # type: ignore[union-attr]
        )

        assert inhibitor.leased
        assert job is not None
        assert job["status"] == "completed"
        assert job["codex_turn_id"] == "turn-1"
        assert job["output_message_id"] is not None
        assert conversation is not None
        assert conversation.codex_thread_id == "thread-1"
        assert result.outbound_id is not None


@pytest.mark.asyncio
async def test_relay_replaces_empty_codex_result_with_deliverable_text(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        await database.add_project(project_path)
        service = RelayService(
            database=database,
            backend=FakeBackend(final_text=" \n"),
            sleep_inhibitor=FakeSleepInhibitor(),  # type: ignore[arg-type]
        )

        result = await service.run_current_project(
            text="hello",
            delivery=DeliveryTarget(
                connector_type="telegram",
                account_id="main",
                external_conversation_id="123",
            ),
        )

        assert result.final_text == "任务已完成。"
        cursor = await database.connection.execute(
            "SELECT content_text FROM conversation_messages WHERE role='assistant'"
        )
        canonical = await cursor.fetchone()
        cursor = await database.connection.execute(
            "SELECT payload_json FROM outbound_messages WHERE id=?", (result.outbound_id,)
        )
        outbound = await cursor.fetchone()
        assert canonical is not None and canonical["content_text"] == "任务已完成。"
        assert outbound is not None and "任务已完成。" in outbound["payload_json"]


@pytest.mark.asyncio
async def test_relay_passes_private_conversation_model_settings_to_codex(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        project = await database.add_project(project_path)
        await database.set_active_conversation_model(
            project.id,
            model="gpt-5.6-terra",
            reasoning_effort="high",
            title=project.name,
        )
        backend = FakeBackend()
        service = RelayService(
            database=database,
            backend=backend,
            sleep_inhibitor=FakeSleepInhibitor(),  # type: ignore[arg-type]
        )

        await service.run_current_project(text="hello")

        assert backend.model == "gpt-5.6-terra"
        assert backend.reasoning_effort == "high"


@pytest.mark.asyncio
async def test_relay_passes_project_auto_approval_mode_to_codex(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        await database.add_project(project_path)
        await database.connection.execute(
            """
            INSERT INTO local_users(id, display_name, enabled, created_at)
            VALUES ('user-1', 'Owner', 1, 'now')
            """
        )
        await database.connection.execute(
            """
            INSERT INTO external_identities(
                id, local_user_id, connector_type, account_id,
                external_user_id, external_conversation_id, paired_at, enabled
            ) VALUES ('identity-1', 'user-1', 'telegram', 'main-bot', '123', '123', 'now', 1)
            """
        )
        await database.connection.commit()
        await database.set_current_project_approval_mode(
            ProjectApprovalMode.PROJECT_AUTO,
            connector_type="telegram",
            account_id="main-bot",
            external_user_id="123",
        )
        backend = FakeBackend()
        service = RelayService(
            database=database,
            backend=backend,
            sleep_inhibitor=FakeSleepInhibitor(),  # type: ignore[arg-type]
        )

        await service.run_current_project(text="hello")

        assert backend.approval_mode is ProjectApprovalMode.PROJECT_AUTO


@pytest.mark.asyncio
async def test_switching_projects_resumes_each_projects_own_codex_thread(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    database_path = tmp_path / "state.db"
    first_path.mkdir()
    second_path.mkdir()
    backend = ProjectContextBackend()
    async with Database(database_path) as database:
        first = await database.add_project(first_path, "First")
        second = await database.add_project(second_path, "Second")
        service = RelayService(
            database=database,
            backend=backend,  # type: ignore[arg-type]
            sleep_inhibitor=FakeSleepInhibitor(),  # type: ignore[arg-type]
        )

        await service.run_current_project(text="first turn")
        await database.switch_project(second.id)
        await service.run_current_project(text="second project turn")

    async with Database(database_path) as database:
        service = RelayService(
            database=database,
            backend=backend,  # type: ignore[arg-type]
            sleep_inhibitor=FakeSleepInhibitor(),  # type: ignore[arg-type]
        )
        await database.switch_project(first.id)
        await service.run_current_project(text="resume first project")

        assert backend.calls == [
            ("first", None),
            ("second", None),
            ("first", "thread-first"),
        ]
        first_conversation = await database.active_conversation(first.id)
        second_conversation = await database.active_conversation(second.id)
        assert first_conversation is not None
        assert first_conversation.codex_thread_id == "thread-first"
        assert second_conversation is not None
        assert second_conversation.codex_thread_id == "thread-second"


@pytest.mark.asyncio
async def test_interrupt_active_turn_updates_backend_and_persisted_job(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        project = await database.add_project(project_path)
        conversation = await database.get_or_create_active_conversation(project.id)
        job_id, _message = await database.create_queued_job_with_input(
            conversation_id=conversation.id,
            text="long task",
        )
        await database.mark_job_starting(job_id)
        await database.mark_turn_started(job_id, "thread-1", "turn-1")
        backend = FakeBackend()
        service = RelayService(
            database=database,
            backend=backend,
            sleep_inhibitor=FakeSleepInhibitor(),  # type: ignore[arg-type]
        )

        assert await service.interrupt_active()

        cursor = await database.connection.execute(
            "SELECT status FROM jobs WHERE id=?", (job_id,)
        )
        row = await cursor.fetchone()
        assert backend.interrupted_turn_id == "turn-1"
        assert row is not None and row["status"] == "interrupted"
