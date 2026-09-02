import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from codexrelay.database import MIGRATION_1, MIGRATION_2, MIGRATION_3, Database
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
        assert replacement.reasoning_effort == "high"

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
        assert version is not None and version["version"] == 5
        assert conversation is not None
        assert conversation.codex_thread_id == "thread-1"
        assert conversation.model is None
        assert conversation.reasoning_effort is None
