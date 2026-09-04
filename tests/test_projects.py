from pathlib import Path

import pytest

from codexrelay.database import Database
from codexrelay.projects import ProjectService


@pytest.mark.asyncio
async def test_register_list_and_switch_projects(tmp_path: Path) -> None:
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()

    async with Database(tmp_path / "state.db") as database:
        service = ProjectService(database)
        first = await service.register(first_path)
        second = await service.register(second_path)

        assert first.is_current
        assert not second.is_current

        selected = await service.switch("second")
        assert selected.id == second.id
        assert selected.is_current

        projects = await service.list_projects()
        assert [project.id for project in projects] == [first.id, second.id]
        assert [project.is_current for project in projects] == [False, True]


@pytest.mark.asyncio
async def test_cannot_switch_project_while_a_job_is_running(tmp_path: Path) -> None:
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()

    async with Database(tmp_path / "state.db") as database:
        service = ProjectService(database)
        first = await service.register(first_path)
        second = await service.register(second_path)
        conversation = await database.get_or_create_active_conversation(first.id)
        job_id, _message = await database.create_queued_job_with_input(
            conversation_id=conversation.id,
            text="long-running task",
        )
        await database.mark_job_starting(job_id)
        await database.mark_turn_started(job_id, "thread-first", "turn-first")

        with pytest.raises(RuntimeError, match="任务运行期间不能切换项目"):
            await service.switch(second.id)

        current = await database.current_project()
        assert current is not None and current.id == first.id
        active = await database.active_job()
        assert active == (job_id, "turn-first")


@pytest.mark.asyncio
async def test_switch_project_does_not_reuse_previous_project_conversation(tmp_path: Path) -> None:
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        first = await database.add_project(first_path)
        second = await database.add_project(second_path)
        first_conversation = await database.get_or_create_active_conversation(first.id, "First")
        await database.switch_project(second.id)
        second_conversation = await database.get_or_create_active_conversation(second.id, "Second")

        assert first_conversation.id != second_conversation.id
        assert second_conversation.project_id == second.id


@pytest.mark.asyncio
async def test_scan_maintenance_hides_missing_registered_projects(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    missing = tmp_path / "missing"
    existing.mkdir()
    missing.mkdir()
    async with Database(tmp_path / "state.db") as database:
        await database.add_project(existing, "Existing")
        await database.add_project(missing, "Missing")
        missing.rmdir()

        assert await database.disable_missing_projects() == 1
        projects = await database.list_projects()
        assert [project.name for project in projects] == ["Existing"]

        cursor = await database.connection.execute(
            "SELECT enabled FROM projects WHERE name='Missing'"
        )
        row = await cursor.fetchone()
        assert row is not None and row["enabled"] == 0


@pytest.mark.asyncio
async def test_reconcile_scan_removes_stale_projects_inside_roots(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    current = root / "current"
    stale = root / "stale"
    outside = tmp_path / "outside"
    for project in (current, stale, outside):
        project.mkdir()
        (project / "pyproject.toml").touch()

    async with Database(tmp_path / "state.db") as database:
        await database.add_project(stale, "Stale")
        await database.add_project(outside, "Outside")
        removed = await database.reconcile_projects({current.resolve()}, (root,))

        assert removed == 1
        active = await database.list_projects()
        assert [project.name for project in active] == ["Outside"]
        cursor = await database.connection.execute(
            "SELECT enabled FROM projects WHERE name='Stale'"
        )
        row = await cursor.fetchone()
        assert row is not None and row["enabled"] == 0


@pytest.mark.asyncio
async def test_discover_projects_is_bounded(tmp_path: Path) -> None:
    python_project = tmp_path / "python-project"
    nested_project = tmp_path / "group" / "nested-project"
    ignored_project = tmp_path / "node_modules" / "ignored"
    python_project.mkdir()
    nested_project.mkdir(parents=True)
    ignored_project.mkdir(parents=True)
    (python_project / "pyproject.toml").touch()
    (nested_project / ".git").mkdir()
    (ignored_project / "package.json").touch()

    async with Database(tmp_path / "state.db") as database:
        discovered = ProjectService(database).discover([tmp_path], max_depth=2)

    assert discovered == [nested_project.resolve(), python_project.resolve()]
