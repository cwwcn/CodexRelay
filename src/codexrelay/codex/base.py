from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class TurnResult:
    thread_id: str
    turn_id: str | None
    final_text: str


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
        on_turn_started: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> TurnResult: ...

    async def interrupt(self, turn_id: str) -> None: ...
