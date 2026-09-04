from __future__ import annotations

from codexrelay.codex.base import CodexBackend, select_project_threads
from codexrelay.database import Database


class SessionSynchronizer:
    """Keep project conversations and the global Codex session index aligned."""

    def __init__(self, database: Database, backend: CodexBackend) -> None:
        self.database = database
        self.backend = backend

    async def sync_all(self) -> bool:
        """Synchronize after one complete Codex listing.

        Returns ``False`` when an active task makes reconciliation unsafe.
        A listing failure is intentionally allowed to propagate so callers can
        preserve the last successful local snapshot and present useful status.
        """
        if await self.database.active_job_count():
            return False
        threads = await self.backend.list_all_threads()
        await self.database.reconcile_global_threads(threads)
        global_sessions = await self.database.list_global_sessions()
        current_project = await self.database.current_project()
        for project in await self.database.list_projects():
            assigned = {
                session.thread_id
                for session in global_sessions
                if session.project_id == project.id
            }
            project_threads = select_project_threads(
                threads, project.path, project.name, assigned
            )
            await self.database.archive_missing_codex_conversations(
                project.id,
                {thread.thread_id for thread in project_threads},
            )
            for thread in project_threads:
                await self.database.register_external_conversation(
                    project.id,
                    codex_thread_id=thread.thread_id,
                    title=thread.title,
                    source=thread.source,
                )
        if current_project is not None:
            await self.database.select_first_available_conversation(current_project.id)
        return True
