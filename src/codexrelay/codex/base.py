from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from codexrelay.models import ProjectApprovalMode

ProgressCallback = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class TurnResult:
    thread_id: str
    turn_id: str | None
    final_text: str


@dataclass(frozen=True, slots=True)
class DesktopThread:
    """A Codex thread discovered from the local desktop Codex store."""

    thread_id: str
    title: str
    cwd: Path
    updated_at: int
    is_active: bool = False
    source: str = "desktop"
    cwd_matches_project: bool = True


class CodexBackend(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

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
        on_progress: ProgressCallback | None = None,
    ) -> TurnResult: ...

    async def preflight_thread(
        self,
        *,
        project: Path,
        thread_id: str,
        model: str | None = None,
        approval_mode: ProjectApprovalMode = ProjectApprovalMode.SAFE,
    ) -> None: ...

    async def interrupt(self, turn_id: str) -> None: ...

    async def list_project_threads(self, project: Path) -> list[DesktopThread]: ...
