from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from codexrelay.codex.base import DesktopThread, select_project_threads
from codexrelay.database import Database
from codexrelay.runtime import CodexRelayRuntime


class ProjectThreadBackend:
    def __init__(self, threads: dict[Path, list[DesktopThread]]) -> None:
        self.threads = threads
        self.calls: list[Path] = []

    async def list_project_threads(self, project: Path) -> list[DesktopThread]:
        self.calls.append(project)
        return list(self.threads[project])

    async def list_all_threads(self) -> list[DesktopThread]:
        self.calls.extend(self.threads)
        return [thread for threads in self.threads.values() for thread in threads]


def test_project_thread_selection_uses_one_safe_classification_rule(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "Relay"
    nested_path = project_path / "feature"
    other_path = tmp_path / "other"
    nested_path.mkdir(parents=True)
    other_path.mkdir()
    missing_path = tmp_path / "old-relay"
    threads = [
        DesktopThread("nested", "Nested task", nested_path, updated_at=4),
        DesktopThread("false-title", "Relay notes", other_path, updated_at=3),
        DesktopThread("migrated", "Relay main", missing_path, updated_at=2),
        DesktopThread("assigned", "External task", other_path, updated_at=1),
    ]

    selected = select_project_threads(
        threads,
        project_path,
        "Relay",
        assigned_thread_ids={"assigned"},
    )

    assert [thread.thread_id for thread in selected] == ["nested", "migrated", "assigned"]
    assert selected[0].cwd_matches_project
    assert selected[1].source == "desktop_migrated"
    assert selected[2].source == "desktop_migrated"


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


@pytest.mark.asyncio
async def test_restored_external_session_keeps_explicit_project_assignment(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "project"
    external_path = tmp_path / "external"
    project_path.mkdir()
    external_path.mkdir()
    thread = DesktopThread("external-thread", "External", external_path, updated_at=1)
    backend = ProjectThreadBackend({project_path: [thread]})

    async with Database(tmp_path / "state.db") as database:
        project = await database.add_project(project_path, "Project")
        await CodexRelayRuntime._sync_registered_projects(database, cast(Any, backend))
        assert (await database.list_global_sessions())[0].is_unassigned

        assigned = await database.assign_global_session(thread.thread_id, project.id)
        backend.threads[project_path] = []
        await CodexRelayRuntime._sync_registered_projects(database, cast(Any, backend))
        archived = await database.conversation(assigned.id)
        assert archived is not None and archived.archived_at is not None

        backend.threads[project_path] = [thread]
        await CodexRelayRuntime._sync_registered_projects(database, cast(Any, backend))
        restored = await database.conversation(assigned.id)
        global_session = (await database.list_global_sessions())[0]

        assert restored is not None and restored.archived_at is None
        assert global_session.project_id == project.id
        assert global_session.conversation_id == assigned.id


@pytest.mark.asyncio
async def test_missing_unassigned_session_clears_current_selection(tmp_path: Path) -> None:
    notes_path = tmp_path / "notes"
    notes_path.mkdir()
    thread = DesktopThread("loose-thread", "临时会话", notes_path, updated_at=1)
    backend = ProjectThreadBackend({tmp_path: [thread]})

    async with Database(tmp_path / "state.db") as database:
        await CodexRelayRuntime._sync_registered_projects(database, cast(Any, backend))
        current = await database.current_global_conversation()
        assert current is not None and current.codex_thread_id == thread.thread_id

        backend.threads[tmp_path] = []
        await CodexRelayRuntime._sync_registered_projects(database, cast(Any, backend))

        assert await database.current_global_conversation() is None
        archived = await database.conversation(current.id)
        assert archived is not None and archived.archived_at is not None
