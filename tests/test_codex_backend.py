from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from openai_codex import AsyncCodex, CodexConfig
from openai_codex.generated.v2_all import ApprovalsReviewer, AskForApprovalValue

from codexrelay.codex.app_server import (
    AppServerBackend,
    approval_settings,
    codex_subprocess_environment,
    discover_codex_bin,
    user_approval_settings,
)
from codexrelay.models import ProjectApprovalMode


@dataclass
class FakeResult:
    id: str = "turn-1"
    final_response: str | None = "done"
    error: Any = None
    status: str = "completed"


class FakeHandle:
    id = "turn-1"

    async def run(self) -> FakeResult:
        return FakeResult()

    async def interrupt(self) -> None:
        return None


class FakeThread:
    id = "thread-1"

    def __init__(self) -> None:
        self.turn_kwargs: dict[str, object] = {}

    async def turn(self, _inputs: object, **kwargs: object) -> FakeHandle:
        self.turn_kwargs = kwargs
        return FakeHandle()


class FakeClient:
    def __init__(self, _config: CodexConfig) -> None:
        self.closed = False
        self.started_cwd: str | None = None
        self.started_kwargs: dict[str, object] = {}
        self.thread = FakeThread()

    async def account(self) -> object:
        return object()

    async def close(self) -> None:
        self.closed = True

    async def thread_start(self, **kwargs: object) -> FakeThread:
        self.started_cwd = cast(str, kwargs["cwd"])
        self.started_kwargs = kwargs
        return self.thread

    async def thread_resume(self, _thread_id: str, **kwargs: object) -> FakeThread:
        self.started_cwd = cast(str, kwargs["cwd"])
        self.started_kwargs = kwargs
        return self.thread


@pytest.mark.asyncio
async def test_sdk_backend_runs_text_and_image_turn(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    image = tmp_path / "screen.png"
    image.write_bytes(b"fake image")
    fake_client: FakeClient | None = None

    def factory(config: CodexConfig) -> AsyncCodex:
        nonlocal fake_client
        fake_client = FakeClient(config)
        return cast(AsyncCodex, fake_client)

    backend = AppServerBackend(codex_bin="/usr/bin/true", client_factory=factory)
    await backend.start()
    result = await backend.run_turn(
        project=project,
        text="inspect",
        image_paths=(image,),
        model="gpt-5.6-terra",
        reasoning_effort="high",
    )
    await backend.stop()

    assert result.thread_id == "thread-1"
    assert result.turn_id == "turn-1"
    assert result.final_text == "done"
    assert fake_client is not None
    assert fake_client.started_cwd == str(project)
    assert fake_client.started_kwargs["model"] == "gpt-5.6-terra"
    assert fake_client.thread.turn_kwargs["model"] == "gpt-5.6-terra"
    assert str(fake_client.thread.turn_kwargs["effort"]) == "ReasoningEffort.high"
    assert fake_client.closed


@pytest.mark.asyncio
async def test_preflight_releases_probe_writer_before_real_turn(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    clients: list[FakeClient] = []

    def factory(config: CodexConfig) -> AsyncCodex:
        client = FakeClient(config)
        clients.append(client)
        return cast(AsyncCodex, client)

    backend = AppServerBackend(codex_bin="/usr/bin/true", client_factory=factory)
    await backend.start()
    await backend.preflight_thread(
        project=project,
        thread_id="desktop-thread",
    )

    assert len(clients) == 2
    assert clients[0].closed
    assert not clients[1].closed
    await backend.stop()


def test_codex_discovery_includes_common_gui_launch_paths() -> None:
    environment = codex_subprocess_environment()
    entries = environment["PATH"].split(":")
    assert str(Path.home() / ".npm-global" / "bin") in entries
    assert "/usr/local/bin" in entries


def test_codex_discovery_finds_executable_without_system_install(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_codex = fake_bin / "codex"
    fake_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_codex.chmod(0o755)

    assert discover_codex_bin(str(fake_bin)) == str(fake_codex)


def test_human_approval_does_not_use_sdk_auto_reviewer() -> None:
    policy, reviewer = user_approval_settings()

    assert policy.root is AskForApprovalValue.on_request
    assert reviewer is ApprovalsReviewer.user


def test_project_auto_mode_keeps_server_requests_for_scope_checks() -> None:
    policy, reviewer = approval_settings(ProjectApprovalMode.PROJECT_AUTO)

    assert policy.root is AskForApprovalValue.on_request
    assert reviewer is ApprovalsReviewer.user


def test_custom_client_cannot_silently_bypass_approval_handler() -> None:
    def factory(config: CodexConfig) -> AsyncCodex:
        return AsyncCodex(config)

    with pytest.raises(ValueError, match="cannot be combined"):
        AppServerBackend(
            codex_bin="/usr/bin/true",
            client_factory=factory,
            approval_handler=lambda _method, _params: {},
        )
