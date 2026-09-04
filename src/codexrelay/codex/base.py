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


def select_project_threads(
    threads: list[DesktopThread],
    project: Path,
    project_name: str,
    assigned_thread_ids: set[str] | None = None,
) -> list[DesktopThread]:
    """Select cwd-contained, explicitly assigned, or safely migrated threads."""
    resolved_project = project.expanduser().resolve()
    selected: list[DesktopThread] = []
    for thread in threads:
        resolved_cwd = thread.cwd.expanduser().resolve()
        try:
            resolved_cwd.relative_to(resolved_project)
            cwd_matches = True
        except ValueError:
            cwd_matches = False
        migrated_match = (
            not thread.cwd.expanduser().is_dir()
            and project_name.casefold() in thread.title.casefold()
        )
        explicitly_assigned = (
            assigned_thread_ids is not None and thread.thread_id in assigned_thread_ids
        )
        if not cwd_matches and not migrated_match and not explicitly_assigned:
            continue
        source = thread.source
        if source == "desktop" and not cwd_matches:
            source = "desktop_migrated"
        selected.append(
            DesktopThread(
                thread_id=thread.thread_id,
                title=thread.title,
                cwd=thread.cwd,
                updated_at=thread.updated_at,
                is_active=thread.is_active,
                source=source,
                cwd_matches_project=cwd_matches,
            )
        )
    return selected


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

    async def list_all_threads(self) -> list[DesktopThread]: ...
