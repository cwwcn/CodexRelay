import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from codexrelay.codex.base import DesktopThread
from codexrelay.database import (
    MIGRATION_1,
    MIGRATION_2,
    MIGRATION_3,
    MIGRATION_5,
    MIGRATION_6,
    MIGRATION_7,
    SCHEMA_VERSION,
    Database,
)
from codexrelay.models import JobStatus


@pytest.mark.asyncio
async def test_concurrent_write_transactions_are_serialized(tmp_path: Path) -> None:
    async with Database(tmp_path / "state.db") as database:
        first_entered = asyncio.Event()
        release_first = asyncio.Event()

        async def hold_transaction() -> None:
            async with database.transaction():
                first_entered.set()
                await release_first.wait()

        first = asyncio.create_task(hold_transaction())
        await first_entered.wait()
        second = asyncio.create_task(
            database.queue_text(
                connector_type="telegram",
                account_id="main",
                external_conversation_id="123",
                text="serialized",
            )
        )
        await asyncio.sleep(0)

        assert not second.done()

        release_first.set()
        await first
        outbound_id = await second

        cursor = await database.connection.execute(
            "SELECT status FROM outbound_messages WHERE id=?", (outbound_id,)
        )
        row = await cursor.fetchone()
        assert row is not None and row["status"] == "pending"


@pytest.mark.asyncio
async def test_inbox_deduplication_and_cursor_are_atomic(tmp_path: Path) -> None:
    async with Database(tmp_path / "state.db") as database:
        first_id, inserted = await database.ingest_event(
            connector_type="telegram",
            account_id="main",
            external_event_id="42",
            payload={"update_id": 42},
            cursor_name="update_offset",
            cursor_value="43",
        )
        duplicate_id, duplicate_inserted = await database.ingest_event(
            connector_type="telegram",
            account_id="main",
            external_event_id="42",
            payload={"update_id": 42},
            cursor_name="update_offset",
            cursor_value="43",
        )

        assert inserted
        assert not duplicate_inserted
        assert duplicate_id == first_id
        cursor = await database.connection.execute(
            "SELECT cursor_value FROM connector_cursors WHERE connector_type='telegram'"
        )
        assert (await cursor.fetchone())["cursor_value"] == "43"


@pytest.mark.asyncio
async def test_canonical_messages_survive_outbox_rebuild(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        project = await database.add_project(project_path)
        conversation_id = await database.create_conversation(project.id, "Test")
        job_id, user_message = await database.create_queued_job_with_input(
            conversation_id=conversation_id,
            text="Keep this context",
        )
        assistant_message_id, outbound_id = await database.complete_job_and_queue_reply(
            job_id=job_id,
            text="Canonical reply",
            connector_type="telegram",
            account_id="main",
            external_conversation_id="123",
        )
        await database.connection.execute(
            "DELETE FROM outbound_messages WHERE id=?", (outbound_id,)
        )
        await database.connection.commit()

        rebuilt = await database.rebuild_missing_outbox(
            connector_type="telegram",
            account_id="main",
            external_conversation_id="123",
        )

        assert user_message.content_text == "Keep this context"
        assert rebuilt == 1
        cursor = await database.connection.execute(
            "SELECT canonical_message_id, payload_json FROM outbound_messages"
        )
        row = await cursor.fetchone()
        assert row["canonical_message_id"] == assistant_message_id
        assert "Canonical reply" in row["payload_json"]


@pytest.mark.asyncio
async def test_restart_interrupts_uncertain_active_job_without_losing_context(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        project = await database.add_project(project_path)
        conversation = await database.get_or_create_active_conversation(project.id)
        event_id, _inserted = await database.ingest_event(
            connector_type="telegram",
            account_id="main-bot",
            external_event_id="77",
            payload={"update_id": 77},
            cursor_name="update_offset",
            cursor_value="78",
        )
        job_id, message = await database.create_queued_job_with_input(
            conversation_id=conversation.id,
            text="keep this input",
            inbound_event_id=event_id,
        )
        await database.mark_job_starting(job_id)

        assert await database.interrupt_stale_jobs() == 1

        status = await database.job_status_for_inbound_event(event_id)
        cursor = await database.connection.execute(
            "SELECT error_message FROM jobs WHERE id=?", (job_id,)
        )
        row = await cursor.fetchone()
        assert status is JobStatus.INTERRUPTED
        assert row is not None and row["error_message"] == "runtime_restarted"
        assert message.content_text == "keep this input"


@pytest.mark.asyncio
async def test_reconciliation_never_archives_a_conversation_with_an_active_job(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        project = await database.add_project(project_path)
        conversation = await database.register_external_conversation(
            project.id,
            codex_thread_id="active-thread",
            title="Active session",
        )
        await database.select_conversation(conversation.id, project.id)
        job_id, _message = await database.create_queued_job_with_input(
            conversation_id=conversation.id,
            text="keep the session visible",
        )
        await database.mark_job_starting(job_id)

        assert await database.archive_missing_codex_conversations(project.id, set()) == 0
        still_current = await database.current_conversation(project.id)
        assert still_current is not None and still_current.id == conversation.id
        assert still_current.archived_at is None

        await database.mark_job_interrupted(job_id)
        assert await database.archive_missing_codex_conversations(project.id, set()) == 1
        archived = await database.conversation(conversation.id)
        assert archived is not None and archived.archived_at is not None
        assert await database.current_conversation(project.id) is None


@pytest.mark.asyncio
async def test_global_execution_slot_rejects_a_second_queued_job(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        project = await database.add_project(project_path)
        conversation = await database.get_or_create_active_conversation(project.id)
        first_job, _message = await database.create_queued_job_with_input(
            conversation_id=conversation.id,
            text="first",
        )
        await database.mark_job_starting(first_job)

        with pytest.raises(RuntimeError, match="全局已有任务运行"):
            await database.create_queued_job_with_input(
                conversation_id=conversation.id,
                text="second",
            )


@pytest.mark.asyncio
async def test_global_session_index_classifies_unassigned_and_supports_explicit_assignment(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "project"
    other_path = tmp_path / "other"
    project_path.mkdir()
    other_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        project = await database.add_project(project_path, "Project")
        threads = [
            DesktopThread("project-thread", "Project session", project_path, updated_at=2),
            DesktopThread("other-thread", "Other session", other_path, updated_at=1),
        ]

        await database.reconcile_global_threads(threads)
        sessions = await database.list_global_sessions()
        assert [session.thread_id for session in sessions] == [
            "project-thread",
            "other-thread",
        ]
        project_session = sessions[0]
        unassigned = sessions[1]
        assert project_session.project_id == project.id
        assert project_session.is_unassigned is False
        assert unassigned.project_id is None
        assert unassigned.is_unassigned

        assigned = await database.assign_global_session("other-thread", project.id)
        assert assigned.codex_thread_id == "other-thread"
        sessions = await database.list_global_sessions()
        assigned_view = next(item for item in sessions if item.thread_id == "other-thread")
        assert assigned_view.project_id == project.id
        assert assigned_view.conversation_id == assigned.id

        activated = await database.activate_global_session("other-thread")
        assert activated.id == assigned.id
        current_project = await database.current_project()
        assert current_project is not None and current_project.id == project.id
        current = await database.current_conversation(project.id)
        assert current is not None and current.id == assigned.id


@pytest.mark.asyncio
async def test_assigning_unassigned_session_rebinds_same_conversation_history(
    tmp_path: Path,
) -> None:
    session_path = tmp_path / "loose"
    project_path = tmp_path / "project"
    session_path.mkdir()
    project_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        project = await database.add_project(project_path, "Project")
        await database.reconcile_global_threads(
            [DesktopThread("thread-1", "Loose work", session_path, updated_at=1)]
        )
        before = await database.current_global_conversation()
        assert before is not None and before.project_id is None
        await database.set_conversation_model(before.id, model="gpt-test", reasoning_effort="high")
        job_id, _message = await database.create_queued_job_with_input(
            conversation_id=before.id,
            text="preserve this context",
        )
        await database.mark_job_starting(job_id)
        await database.mark_job_interrupted(job_id)

        assigned = await database.assign_global_session("thread-1", project.id)
        after = await database.current_global_conversation()

        assert assigned.id == before.id
        assert after is not None and after.id == before.id
        assert after.project_id == project.id
        assert after.model == "gpt-test"
        assert after.reasoning_effort == "high"
        cursor = await database.connection.execute(
            "SELECT content_text FROM conversation_messages "
            "WHERE conversation_id=? ORDER BY created_at",
            (before.id,),
        )
        messages = await cursor.fetchall()
        assert [str(item["content_text"]) for item in messages] == ["preserve this context"]
        assert len(await database.list_all_conversations()) == 1


@pytest.mark.asyncio
async def test_global_session_index_keeps_disabled_project_classification(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "disabled-project"
    project_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        project = await database.add_project(project_path, "Disabled")
        await database.connection.execute("UPDATE projects SET enabled=0 WHERE id=?", (project.id,))
        await database.connection.commit()

        await database.reconcile_global_threads(
            [DesktopThread("disabled-thread", "Saved task", project_path, updated_at=1)]
        )
        session = (await database.list_global_sessions())[0]

        assert session.project_id == project.id
        assert session.project_name == "Disabled"
        assert not session.project_enabled
        assert not session.is_unassigned


@pytest.mark.asyncio
async def test_adding_project_does_not_override_selected_unassigned_session(
    tmp_path: Path,
) -> None:
    session_path = tmp_path / "notes"
    project_path = tmp_path / "project"
    session_path.mkdir()
    project_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        await database.reconcile_global_threads(
            [DesktopThread("loose-thread", "Loose", session_path, updated_at=1)]
        )
        current = await database.current_global_conversation()
        assert current is not None and current.project_id is None

        await database.add_project(project_path, "Project")

        selected = await database.current_global_conversation()
        assert selected is not None and selected.id == current.id
        assert selected.project_id is None


@pytest.mark.asyncio
async def test_explicit_standalone_session_is_not_auto_assigned_by_cwd(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        project = await database.add_project(project_path, "Project")
        standalone = await database.create_standalone_conversation(
            project_path, title="Temporary work"
        )
        await database.connection.execute(
            "UPDATE conversations SET codex_thread_id=? WHERE id=?",
            ("standalone-thread", standalone.id),
        )
        await database.connection.commit()

        await database.reconcile_global_threads(
            [DesktopThread("standalone-thread", "Temporary work", project_path, updated_at=1)]
        )
        current = await database.current_global_conversation()
        session = (await database.list_global_sessions())[0]

        assert current is not None and current.id == standalone.id
        assert current.project_id is None
        assert session.project_id is None
        assert session.is_unassigned
        assert project.id != session.project_id


@pytest.mark.asyncio
async def test_project_binding_keeps_execution_cwd_inside_authorized_root(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "project"
    external_path = tmp_path / "external"
    project_path.mkdir()
    external_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        project = await database.add_project(project_path, "Project")
        thread = DesktopThread(
            "external-thread",
            "External session",
            external_path,
            updated_at=1,
        )

        await database.reconcile_global_threads([thread])
        assigned = await database.assign_global_session(thread.thread_id, project.id)
        assert assigned.cwd == project_path.resolve()

        await database.reconcile_global_threads([thread])
        refreshed = await database.conversation(assigned.id)
        discovered = (await database.list_global_sessions())[0]

        assert refreshed is not None
        assert refreshed.project_id == project.id
        assert refreshed.cwd == project_path.resolve()
        assert discovered.project_id == project.id
        assert discovered.cwd == external_path.resolve()


@pytest.mark.asyncio
async def test_housekeeping_trims_transport_payloads_but_keeps_canonical_context(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    database_path = tmp_path / "state.db"
    async with Database(database_path) as database:
        project = await database.add_project(project_path)
        conversation = await database.get_or_create_active_conversation(project.id)
        event_id, _inserted = await database.ingest_event(
            connector_type="telegram",
            account_id="main-bot",
            external_event_id="99",
            payload={"update_id": 99, "message": {"text": "canonical input"}},
            cursor_name="update_offset",
            cursor_value="100",
        )
        await database.mark_inbound_processed(event_id)
        job_id, _message = await database.create_queued_job_with_input(
            conversation_id=conversation.id,
            text="canonical input",
            inbound_event_id=event_id,
        )
        _assistant_id, outbound_id = await database.complete_job_and_queue_reply(
            job_id=job_id,
            text="canonical output",
            connector_type="telegram",
            account_id="main-bot",
            external_conversation_id="123",
        )
        await database.mark_outbound_delivered(outbound_id, "telegram-1")
        old = "2026-08-01T00:00:00.000+00:00"
        await database.connection.execute(
            "UPDATE inbound_events SET processed_at=? WHERE id=?", (old, event_id)
        )
        await database.connection.execute(
            "UPDATE outbound_messages SET delivered_at=? WHERE id=?", (old, outbound_id)
        )
        await database.connection.commit()

        counts = await database.housekeep(now=datetime(2026, 8, 31, tzinfo=UTC))

        inbound = await database.connection.execute(
            "SELECT payload_json FROM inbound_events WHERE id=?", (event_id,)
        )
        outbound = await database.connection.execute(
            "SELECT payload_json FROM outbound_messages WHERE id=?", (outbound_id,)
        )
        messages = await database.connection.execute(
            "SELECT content_text FROM conversation_messages ORDER BY created_at, rowid"
        )
        assert counts["processed_inbound"] == 1
        assert counts["delivered_outbound"] == 1
        assert (await inbound.fetchone())["payload_json"] is None
        assert (await outbound.fetchone())["payload_json"] is None
        assert [row["content_text"] for row in await messages.fetchall()] == [
            "canonical input",
            "canonical output",
        ]

    assert database_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_conversation_model_settings_persist_per_project_and_new_conversation(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()
    database_path = tmp_path / "state.db"

    async with Database(database_path) as database:
        first = await database.add_project(first_path, "First")
        second = await database.add_project(second_path, "Second")
        await database.set_active_conversation_model(
            first.id,
            model="gpt-first",
            reasoning_effort="high",
            title=first.name,
        )
        await database.set_active_conversation_model(
            second.id,
            model="gpt-second",
            reasoning_effort="low",
            title=second.name,
        )
        replacement = await database.start_new_conversation(first.id, first.name)

    assert replacement.model == "gpt-first"

    async with Database(database_path) as database:
        first_conversation = await database.active_conversation(first.id)
        second_conversation = await database.active_conversation(second.id)

        assert first_conversation is not None
        assert first_conversation.model == "gpt-first"
        assert first_conversation.reasoning_effort == "high"
        assert second_conversation is not None
        assert second_conversation.model == "gpt-second"
        assert second_conversation.reasoning_effort == "low"


@pytest.mark.asyncio
async def test_conversation_selection_and_lease_are_isolated(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        project = await database.add_project(project_path, "Project")
        first = await database.get_or_create_active_conversation(project.id, "First")
        second = await database.start_new_conversation(project.id, "Second")

        assert (await database.current_conversation(project.id)).id == second.id
        await database.select_conversation(first.id, project.id)
        assert (await database.current_conversation(project.id)).id == first.id

        await database.acquire_conversation_lock(first.id, "telegram")
        with pytest.raises(RuntimeError, match="正在被 telegram 使用"):
            await database.acquire_conversation_lock(first.id, "desktop")
        assert not await database.release_conversation_lock(first.id, "desktop")
        assert await database.release_conversation_lock(first.id, "telegram")
        await database.acquire_conversation_lock(first.id, "desktop")


@pytest.mark.asyncio
async def test_model_settings_follow_selected_conversation(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        project = await database.add_project(project_path)
        first = await database.get_or_create_active_conversation(project.id, "First")
        second = await database.start_new_conversation(project.id, "Second")
        await database.select_conversation(first.id, project.id)
        configured = await database.set_active_conversation_model(
            project.id, model="gpt-test", reasoning_effort="high"
        )
        assert configured.id == first.id
        assert (await database.conversation(second.id)).model is None


@pytest.mark.asyncio
async def test_stale_conversation_leases_are_released_on_recovery(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        project = await database.add_project(project_path)
        conversation = await database.get_or_create_active_conversation(project.id)
        await database.acquire_conversation_lock(conversation.id, "telegram")

        assert await database.clear_stale_conversation_locks() == 1
        recovered = await database.conversation(conversation.id)
        assert recovered is not None and recovered.lock_owner is None


@pytest.mark.asyncio
async def test_running_job_blocks_conversation_model_changes(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        project = await database.add_project(project_path)
        conversation = await database.get_or_create_active_conversation(project.id)
        job_id, _message = await database.create_queued_job_with_input(
            conversation_id=conversation.id,
            text="running",
        )
        await database.mark_job_starting(job_id)

        with pytest.raises(RuntimeError, match="任务运行期间"):
            await database.set_active_conversation_model(
                project.id,
                model="gpt-deep",
                reasoning_effort="high",
                title=project.name,
            )


@pytest.mark.asyncio
async def test_schema_v3_migrates_model_settings_without_losing_conversations(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.db"
    raw = sqlite3.connect(database_path)
    try:
        raw.executescript(MIGRATION_1)
        raw.executescript(MIGRATION_2)
        raw.executescript(MIGRATION_3)
        raw.execute(
            """
            INSERT INTO projects(id, name, path, enabled, created_at, updated_at)
            VALUES ('project-1', 'Legacy', '/tmp/legacy', 1, 'then', 'then')
            """
        )
        raw.execute(
            """
            INSERT INTO conversations(
                id, project_id, codex_thread_id, title, status, created_at, updated_at
            ) VALUES ('conversation-1', 'project-1', 'thread-1', 'Legacy', 'active', 'then', 'then')
            """
        )
        raw.execute("DELETE FROM schema_version")
        raw.execute("INSERT INTO schema_version(version) VALUES (3)")
        raw.commit()
    finally:
        raw.close()

    async with Database(database_path) as database:
        cursor = await database.connection.execute("PRAGMA table_info(conversations)")
        columns = {str(row["name"]) for row in await cursor.fetchall()}
        cursor = await database.connection.execute(
            "SELECT MAX(version) AS version FROM schema_version"
        )
        version = await cursor.fetchone()
        conversation = await database.conversation("conversation-1")

        assert {"model", "reasoning_effort"} <= columns
        assert version is not None and version["version"] == 10
        assert conversation is not None
        assert conversation.codex_thread_id == "thread-1"
        assert conversation.model is None
        assert conversation.reasoning_effort is None


@pytest.mark.asyncio
async def test_schema_v6_migration_backfills_current_conversation(tmp_path: Path) -> None:
    database_path = tmp_path / "state.db"
    raw = sqlite3.connect(database_path)
    try:
        raw.executescript(MIGRATION_1)
        raw.executescript(MIGRATION_2)
        raw.executescript(MIGRATION_3)
        raw.execute("ALTER TABLE conversations ADD COLUMN model TEXT NULL")
        raw.execute("ALTER TABLE conversations ADD COLUMN reasoning_effort TEXT NULL")
        raw.executescript(MIGRATION_5)
        raw.executescript(MIGRATION_6)
        raw.execute("DELETE FROM schema_version")
        raw.execute("INSERT INTO schema_version(version) VALUES (6)")
        raw.execute(
            "INSERT INTO projects(id,name,path,enabled,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            ("p", "Project", str(tmp_path), 1, "now", "now"),
        )
        raw.execute("UPDATE app_state SET current_project_id='p' WHERE singleton=1")
        raw.execute(
            "INSERT INTO conversations("
            "id,project_id,title,status,last_used_at,created_at,updated_at) "
            "VALUES ('c','p','Conversation','active','later','now','now')"
        )
        raw.commit()
    finally:
        raw.close()
    async with Database(database_path) as database:
        current = await database.current_conversation("p")
        assert current is not None and current.id == "c"


@pytest.mark.asyncio
async def test_schema_v7_adds_global_discovered_thread_index(tmp_path: Path) -> None:
    database_path = tmp_path / "state.db"
    raw = sqlite3.connect(database_path)
    try:
        raw.executescript(MIGRATION_1)
        raw.executescript(MIGRATION_2)
        raw.executescript(MIGRATION_3)
        raw.execute("ALTER TABLE conversations ADD COLUMN model TEXT NULL")
        raw.execute("ALTER TABLE conversations ADD COLUMN reasoning_effort TEXT NULL")
        raw.executescript(MIGRATION_5)
        raw.executescript(MIGRATION_6)
        raw.executescript(MIGRATION_7)
        raw.execute("DELETE FROM schema_version")
        raw.execute("INSERT INTO schema_version(version) VALUES (7)")
        raw.commit()
    finally:
        raw.close()
    async with Database(database_path) as database:
        cursor = await database.connection.execute("PRAGMA table_info(discovered_threads)")
        columns = {str(row["name"]) for row in await cursor.fetchall()}
        version_cursor = await database.connection.execute(
            "SELECT MAX(version) AS version FROM schema_version"
        )
        version = await version_cursor.fetchone()

    assert {
        "codex_thread_id",
        "cwd",
        "project_id",
        "conversation_id",
        "archived_at",
    } <= columns
    assert version is not None and version["version"] == 10


@pytest.mark.asyncio
async def test_lifecycle_state_and_deferred_inbound_are_persistent(tmp_path: Path) -> None:
    database_path = tmp_path / "state.db"
    async with Database(database_path) as database:
        state = await database.record_lifecycle(
            "system_sleep",
            state="offline",
            reason="system_sleep",
            occurred_at="2026-09-05T01:00:00+00:00",
            offline_since="2026-09-05T01:00:00+00:00",
        )
        assert state.state == "offline"
        event_id, inserted = await database.ingest_event(
            connector_type="telegram",
            account_id="main-bot",
            external_event_id="offline-1",
            payload={"update_id": 1, "message": {"text": "later"}},
            cursor_name="update_offset",
            cursor_value="2",
        )
        assert inserted
        assert await database.defer_inbound_event(event_id, "token")

    async with Database(database_path) as database:
        state = await database.lifecycle_state()
        assert state is not None and state.offline_since == "2026-09-05T01:00:00+00:00"
        claimed = await database.claim_deferred_inbound("token", decision="ignored")
        assert claimed is not None and claimed[0] == event_id
        assert await database.claim_deferred_inbound("token", decision="ignored") is None


@pytest.mark.asyncio
async def test_partial_schema_v6_migration_is_resumed_without_duplicate_column_error(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "partial.db"
    raw = sqlite3.connect(database_path)
    try:
        raw.executescript(MIGRATION_1)
        raw.executescript(MIGRATION_2)
        raw.executescript(MIGRATION_3)
        raw.execute("ALTER TABLE conversations ADD COLUMN model TEXT NULL")
        raw.execute("ALTER TABLE conversations ADD COLUMN reasoning_effort TEXT NULL")
        raw.executescript(MIGRATION_5)
        # Simulate an app that added the first v6 column but crashed before
        # recording schema version 6.
        raw.execute("ALTER TABLE conversations ADD COLUMN scope TEXT NOT NULL DEFAULT 'project'")
        raw.execute("DELETE FROM schema_version")
        raw.execute("INSERT INTO schema_version(version) VALUES (5)")
        raw.commit()
    finally:
        raw.close()

    async with Database(database_path) as database:
        cursor = await database.connection.execute("PRAGMA table_info(conversations)")
        columns = {str(row["name"]) for row in await cursor.fetchall()}
        assert {
            "scope",
            "source",
            "last_used_at",
            "is_pinned",
            "archived_at",
            "lock_owner",
        } <= columns


@pytest.mark.asyncio
async def test_concurrent_database_open_serializes_schema_migration(tmp_path: Path) -> None:
    database_path = tmp_path / "concurrent.db"
    databases = [Database(database_path) for _ in range(4)]

    await asyncio.gather(*(database.open() for database in databases))
    try:
        versions = []
        for database in databases:
            cursor = await database.connection.execute(
                "SELECT MAX(version) AS version FROM schema_version"
            )
            row = await cursor.fetchone()
            versions.append(None if row is None else row["version"])
        assert versions == [SCHEMA_VERSION] * len(databases)
    finally:
        await asyncio.gather(*(database.close() for database in databases))
