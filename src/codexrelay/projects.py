from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from codexrelay.database import Database
from codexrelay.models import Project

PROJECT_MARKERS = frozenset(
    {
        ".git",
        "pyproject.toml",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "Package.swift",
        "*.xcodeproj",
    }
)

IGNORED_DIRECTORIES = frozenset(
    {".git", ".venv", "node_modules", "build", "dist", "Library", ".Trash"}
)


class ProjectService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def register(self, path: Path, name: str | None = None) -> Project:
        return await self.database.add_project(path, name)

    async def list_projects(self) -> list[Project]:
        return await self.database.list_projects()

    async def switch(self, selector: str) -> Project:
        projects = await self.list_projects()
        exact_id = next((project for project in projects if project.id == selector), None)
        if exact_id is not None:
            return await self.database.switch_project(exact_id.id)
        normalized = selector.casefold()
        by_name = [project for project in projects if project.name.casefold() == normalized]
        if len(by_name) == 1:
            return await self.database.switch_project(by_name[0].id)
        if len(by_name) > 1:
            raise ValueError("more than one project has that name; use the project id")
        raise ValueError("unknown project; list registered projects first")

    def discover(self, roots: Iterable[Path], *, max_depth: int = 2) -> list[Path]:
        if max_depth < 0:
            raise ValueError("max_depth cannot be negative")
        candidates: set[Path] = set()
        for root in roots:
            resolved = root.expanduser().resolve(strict=True)
            if not resolved.is_dir():
                continue
            self._walk(resolved, depth=0, max_depth=max_depth, candidates=candidates)
        return sorted(candidates, key=lambda item: str(item).casefold())

    def _walk(self, path: Path, *, depth: int, max_depth: int, candidates: set[Path]) -> None:
        try:
            entries = tuple(path.iterdir())
        except (OSError, PermissionError):
            return
        if self._looks_like_project(entries):
            candidates.add(path)
            return
        if depth >= max_depth:
            return
        for entry in entries:
            if (
                not entry.is_dir()
                or entry.name in IGNORED_DIRECTORIES
                or entry.name.startswith(".")
            ):
                continue
            self._walk(entry, depth=depth + 1, max_depth=max_depth, candidates=candidates)

    @staticmethod
    def _looks_like_project(entries: tuple[Path, ...]) -> bool:
        names = {entry.name for entry in entries}
        if any(marker in names for marker in PROJECT_MARKERS if "*" not in marker):
            return True
        return any(entry.suffix == ".xcodeproj" for entry in entries)
