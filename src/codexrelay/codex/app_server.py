from __future__ import annotations

import os
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

from openai_codex import (
    ApprovalMode,
    AsyncCodex,
    AsyncTurnHandle,
    CodexConfig,
    InputItem,
    LocalImageInput,
    Sandbox,
    TextInput,
)
from openai_codex.api import AsyncThread
from openai_codex.async_client import AsyncCodexClient
from openai_codex.client import ApprovalHandler, CodexClient
from openai_codex.generated.v2_all import (
    ApprovalsReviewer,
    AskForApproval,
    AskForApprovalValue,
    ReasoningEffort,
    SandboxMode,
    ThreadResumeParams,
    ThreadStartParams,
)

from codexrelay.codex.base import TurnResult
from codexrelay.codex.model_catalog import CodexModelCatalog, CodexModelOption


class CodexBackendError(RuntimeError):
    pass


class _ApprovalAsyncCodexClient(AsyncCodexClient):
    def __init__(self, config: CodexConfig, approval_handler: ApprovalHandler) -> None:
        self._sync = CodexClient(config=config, approval_handler=approval_handler)


class _ApprovalAsyncCodex(AsyncCodex):
    """SDK client that routes escalated requests to the caller's approval handler.

    The SDK's public ``ApprovalMode.auto_review`` selects the App Server's automatic
    reviewer, so it must not be used for human approval flows.
    """

    def __init__(self, config: CodexConfig, approval_handler: ApprovalHandler) -> None:
        super().__init__(config)
        self._client = _ApprovalAsyncCodexClient(config, approval_handler)

    async def thread_start_with_user_approval(
        self, *, cwd: str, model: str | None
    ) -> AsyncThread:
        await self._ensure_initialized()
        approval_policy, approvals_reviewer = user_approval_settings()
        params = ThreadStartParams(
            approval_policy=approval_policy,
            approvals_reviewer=approvals_reviewer,
            cwd=cwd,
            model=model,
            sandbox=SandboxMode.workspace_write,
        )
        started = await self._client.thread_start(params)
        return AsyncThread(self, started.thread.id)

    async def thread_resume_with_user_approval(
        self, thread_id: str, *, cwd: str, model: str | None
    ) -> AsyncThread:
        await self._ensure_initialized()
        approval_policy, approvals_reviewer = user_approval_settings()
        params = ThreadResumeParams(
            thread_id=thread_id,
            approval_policy=approval_policy,
            approvals_reviewer=approvals_reviewer,
            cwd=cwd,
            model=model,
            sandbox=SandboxMode.workspace_write,
        )
        resumed = await self._client.thread_resume(thread_id, params)
        return AsyncThread(self, resumed.thread.id)


class AppServerBackend:
    """Codex Python SDK adapter using the locally installed Codex binary."""

    def __init__(
        self,
        *,
        codex_bin: str | None = None,
        client_factory: Callable[[CodexConfig], AsyncCodex] | None = None,
        approval_handler: ApprovalHandler | None = None,
    ) -> None:
        if client_factory is not None and approval_handler is not None:
            raise ValueError("client_factory and approval_handler cannot be combined")
        environment = codex_subprocess_environment()
        resolved = codex_bin or discover_codex_bin(environment["PATH"])
        if resolved is None:
            raise FileNotFoundError("Codex CLI was not found in PATH")
        self._config = CodexConfig(
            codex_bin=resolved,
            env=environment,
            client_name="codexrelay",
            client_title="CodexRelay",
        )
        if client_factory is not None:
            self._client_factory = client_factory
        elif approval_handler is not None:
            self._client_factory = lambda config: _ApprovalAsyncCodex(config, approval_handler)
        else:
            self._client_factory = AsyncCodex
        self._uses_user_approval = approval_handler is not None
        self._client: AsyncCodex | None = None
        self._active_turns: dict[str, AsyncTurnHandle] = {}

    @property
    def started(self) -> bool:
        return self._client is not None

    async def start(self) -> None:
        if self._client is not None:
            return
        client = self._client_factory(self._config)
        try:
            await client.account()
        except BaseException:
            await client.close()
            raise
        self._client = client

    async def stop(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        for handle in tuple(self._active_turns.values()):
            try:
                await handle.interrupt()
            except BaseException:
                pass
        self._active_turns.clear()
        await client.close()

    async def model_catalog(self) -> CodexModelCatalog:
        response = await self._require_client().models()
        return CodexModelCatalog(
            tuple(
                CodexModelOption(
                    model=item.model,
                    display_name=item.display_name,
                    description=item.description,
                    default_reasoning_effort=item.default_reasoning_effort.value,
                    supported_reasoning_efforts=tuple(
                        option.reasoning_effort.value
                        for option in item.supported_reasoning_efforts
                    ),
                    is_default=item.is_default,
                )
                for item in response.data
                if not item.hidden
            )
        )

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
        client = self._require_client()
        resolved_project = project.expanduser().resolve(strict=True)
        if not resolved_project.is_dir():
            raise ValueError("Codex project path is not a directory")
        if not text.strip() and not image_paths:
            raise ValueError("a turn requires text or at least one image")
        for image_path in image_paths:
            if not image_path.expanduser().resolve(strict=True).is_file():
                raise ValueError(f"image does not exist: {image_path}")
        effort = ReasoningEffort(reasoning_effort) if reasoning_effort is not None else None

        if self._uses_user_approval:
            if not isinstance(client, _ApprovalAsyncCodex):
                raise RuntimeError("user approval client is not configured")
            if thread_id is None:
                thread = await client.thread_start_with_user_approval(
                    cwd=str(resolved_project), model=model
                )
            else:
                thread = await client.thread_resume_with_user_approval(
                    thread_id, cwd=str(resolved_project), model=model
                )
            turn_approval_mode = None
        else:
            if thread_id is None:
                thread = await client.thread_start(
                    approval_mode=ApprovalMode.deny_all,
                    cwd=str(resolved_project),
                    model=model,
                    sandbox=Sandbox.workspace_write,
                )
            else:
                thread = await client.thread_resume(
                    thread_id,
                    approval_mode=ApprovalMode.deny_all,
                    cwd=str(resolved_project),
                    model=model,
                    sandbox=Sandbox.workspace_write,
                )
            turn_approval_mode = ApprovalMode.deny_all

        inputs: list[InputItem] = []
        if text.strip():
            inputs.append(TextInput(text=text))
        inputs.extend(
            LocalImageInput(path=str(path.expanduser().resolve(strict=True)))
            for path in image_paths
        )
        handle = await thread.turn(
            inputs,
            approval_mode=turn_approval_mode,
            cwd=str(resolved_project),
            effort=effort,
            model=model,
            sandbox=Sandbox.workspace_write,
        )
        if on_turn_started is not None:
            await on_turn_started(thread.id, handle.id)
        self._active_turns[handle.id] = handle
        try:
            result = await handle.run()
        finally:
            self._active_turns.pop(handle.id, None)
        if result.final_response is None:
            detail = result.error.message if result.error is not None else str(result.status)
            raise CodexBackendError(f"Codex turn did not produce a final response: {detail}")
        return TurnResult(
            thread_id=thread.id,
            turn_id=result.id,
            final_text=result.final_response,
        )

    async def interrupt(self, turn_id: str) -> None:
        handle = self._active_turns.get(turn_id)
        if handle is None:
            raise ValueError("turn is not active")
        await handle.interrupt()

    def _require_client(self) -> AsyncCodex:
        if self._client is None:
            raise RuntimeError("Codex backend is not started")
        return self._client


def codex_subprocess_environment() -> dict[str, str]:
    environment = dict(os.environ)
    existing = environment.get("PATH", "")
    candidates = (
        str(Path.home() / ".npm-global" / "bin"),
        str(Path.home() / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    )
    entries: list[str] = []
    for entry in (*candidates, *existing.split(os.pathsep)):
        if entry and entry not in entries:
            entries.append(entry)
    environment["PATH"] = os.pathsep.join(entries)
    return environment


def discover_codex_bin(search_path: str) -> str | None:
    return shutil.which("codex", path=search_path)


def user_approval_settings() -> tuple[AskForApproval, ApprovalsReviewer]:
    return (
        AskForApproval(root=AskForApprovalValue.on_request),
        ApprovalsReviewer.user,
    )
