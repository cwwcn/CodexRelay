from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from codexrelay.codex.base import DesktopThread
from codexrelay.database import Database
from codexrelay.runtime import CodexRelayRuntime


class ProjectThreadBackend:
    def __init__(self, threads: dict[Path, list[DesktopThread]]) -> None:
        self.threads = threads
        self.calls: list[Path] = []

    async def list_project_threads(self, project: Path) -> list[DesktopThread]:
        self.calls.append(project)
        return list(self.threads[project])


@pytest.mark.asyncio
async def test_session_sync_only_repairs_current_project_selection(tmp_path: Path) -> None:
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        first = await database.add_project(first_path, "First")
        await database.add_project(second_path, "Second")
        backend = ProjectThreadBackend(
            {
                first_path: [
                    DesktopThread("first-thread", "First session", first_path, updated_at=1)
                ],
                second_path: [
                    DesktopThread("second-thread", "Second session", second_path, updated_at=2)
                ],
            }
        )

        await CodexRelayRuntime._sync_registered_projects(
            database, cast(Any, backend)
        )

        current_project = await database.current_project()
        current_conversation = await database.current_conversation(first.id)
        assert current_project is not None and current_project.id == first.id
        assert current_conversation is not None
        assert current_conversation.codex_thread_id == "first-thread"


@pytest.mark.asyncio
async def test_session_sync_skips_while_a_codex_job_is_active(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        project = await database.add_project(project_path, "Project")
        conversation_id = await database.create_conversation(project.id, "Active session")
        await database.connection.execute(
            "UPDATE conversations SET codex_thread_id=? WHERE id=?",
            ("missing-while-running", conversation_id),
        )
        await database.connection.commit()
        job_id, _message = await database.create_queued_job_with_input(
            conversation_id=conversation_id,
            text="still running",
        )
        await database.mark_job_starting(job_id)

        backend = ProjectThreadBackend({project_path: []})
        await CodexRelayRuntime._sync_registered_projects(
            database, cast(Any, backend)
        )

        current = await database.conversation(conversation_id)
        assert current is not None
        assert current.archived_at is None
        assert backend.calls == []

        await database.mark_job_interrupted(job_id)
        await CodexRelayRuntime._sync_registered_projects(
            database, cast(Any, backend)
        )
        archived = await database.conversation(conversation_id)
        assert archived is not None and archived.archived_at is not None
