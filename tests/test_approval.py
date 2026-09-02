from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from codexrelay.approval import (
    COMMAND_APPROVAL,
    PERMISSIONS_APPROVAL,
    ApprovalCoordinator,
)
from codexrelay.database import Database
from codexrelay.models import ProjectApprovalMode
from codexrelay.pairing import PairingService


async def prepare_running_job(database: Database, project_path: Path) -> str:
    project = await database.add_project(project_path)
    conversation = await database.get_or_create_active_conversation(project.id)
    job_id, _message = await database.create_queued_job_with_input(
        conversation_id=conversation.id, text="run tests"
    )
    await database.mark_job_starting(job_id)
    await database.mark_turn_started(job_id, "thread-1", "turn-1")
    pairing = PairingService(database)
    challenge = await pairing.generate()
    await pairing.pair(
        code=challenge.code,
        external_user_id="123",
        external_conversation_id="123",
        display_name="Owner",
    )
    return job_id


@pytest.mark.asyncio
async def test_approval_nonce_is_atomic_and_single_use(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        job_id = await prepare_running_job(database, project_path)
        coordinator = ApprovalCoordinator(
            database=database,
            loop=asyncio.get_running_loop(),
            timeout_seconds=5,
        )
        request = asyncio.create_task(
            coordinator.request(
                COMMAND_APPROVAL,
                {
                    "turnId": "turn-1",
                    "itemId": "item-1",
                    "command": "pytest -q",
                    "cwd": str(project_path),
                },
            )
        )

        for _ in range(20):
            pending = await database.pending_outbound_messages(
                connector_type="telegram", account_id="main-bot"
            )
            if pending:
                break
            await asyncio.sleep(0.01)
        assert pending
        payload = json.loads(pending[0].payload_json)
        callback_data = payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"]

        assert await coordinator.resolve_callback(callback_data) == "accept"
        assert await coordinator.resolve_callback(callback_data) is None
        assert await request == "accept"

        cursor = await database.connection.execute(
            "SELECT status FROM approval_requests WHERE job_id=?", (job_id,)
        )
        approval = await cursor.fetchone()
        cursor = await database.connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,))
        job = await cursor.fetchone()
        assert approval is not None and approval["status"] == "accept"
        assert job is not None and job["status"] == "running"


def test_permission_response_is_limited_to_requested_turn_permissions() -> None:
    requested = {"network": {"enabled": True}}
    accepted = ApprovalCoordinator._response(
        PERMISSIONS_APPROVAL, "accept", {"permissions": requested}
    )
    declined = ApprovalCoordinator._response(
        PERMISSIONS_APPROVAL, "decline", {"permissions": requested}
    )

    assert accepted == {"permissions": requested, "scope": "turn"}
    assert declined == {"permissions": {}, "scope": "turn"}


@pytest.mark.asyncio
async def test_project_auto_approval_is_bound_to_project_and_pairing_identity(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        project = await database.add_project(project_path)
        pairing = PairingService(database)
        challenge = await pairing.generate()
        await pairing.pair(
            code=challenge.code,
            external_user_id="123",
            external_conversation_id="123",
            display_name="Owner",
        )
        await database.set_current_project_approval_mode(
            ProjectApprovalMode.PROJECT_AUTO,
            connector_type="telegram",
            account_id="main-bot",
            external_user_id="123",
        )
        assert (
            await database.project_approval_mode(project.id)
            is ProjectApprovalMode.PROJECT_AUTO
        )

        outside = tmp_path / "outside"
        outside.mkdir()
        assert not ApprovalCoordinator._auto_allows(
            COMMAND_APPROVAL,
            {"cwd": str(outside), "command": "echo unsafe"},
            project,
        )

        challenge = await pairing.generate()
        await pairing.pair(
            code=challenge.code,
            external_user_id="456",
            external_conversation_id="456",
            display_name="New owner",
        )
        assert await database.project_approval_mode(project.id) is ProjectApprovalMode.SAFE


@pytest.mark.asyncio
async def test_project_auto_approval_accepts_in_scope_request_without_telegram_prompt(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    async with Database(tmp_path / "state.db") as database:
        project = await database.add_project(project_path)
        pairing = PairingService(database)
        challenge = await pairing.generate()
        await pairing.pair(
            code=challenge.code,
            external_user_id="123",
            external_conversation_id="123",
            display_name="Owner",
        )
        await database.set_current_project_approval_mode(
            ProjectApprovalMode.PROJECT_AUTO,
            connector_type="telegram",
            account_id="main-bot",
            external_user_id="123",
        )
        job_id, _message = await database.create_queued_job_with_input(
            conversation_id=(await database.get_or_create_active_conversation(project.id)).id,
            text="run tests",
        )
        await database.mark_job_starting(job_id)
        await database.mark_turn_started(job_id, "thread-1", "turn-1")
        coordinator = ApprovalCoordinator(
            database=database,
            loop=asyncio.get_running_loop(),
            timeout_seconds=1,
        )

        decision = await coordinator.request(
            COMMAND_APPROVAL,
            {
                "turnId": "turn-1",
                "itemId": "item-1",
                "command": "pytest -q",
                "cwd": str(project_path),
            },
        )

        assert decision == "accept"
        assert not await database.pending_outbound_messages(
            connector_type="telegram", account_id="main-bot"
        )
