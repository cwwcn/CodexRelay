from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from codexrelay.codex.base import DesktopThread
from codexrelay.models import (
    CanonicalMessage,
    Conversation,
    GlobalSession,
    JobStatus,
    MessageRole,
    OutboundMessage,
    Project,
    ProjectApprovalMode,
)

SCHEMA_VERSION = 9

MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS app_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    current_project_id TEXT NULL
);
INSERT OR IGNORE INTO app_state(singleton, current_project_id) VALUES (1, NULL);

CREATE TABLE IF NOT EXISTS connector_cursors (
    connector_type TEXT NOT NULL,
    account_id TEXT NOT NULL,
    cursor_name TEXT NOT NULL,
    cursor_value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(connector_type, account_id, cursor_name)
);

CREATE TABLE IF NOT EXISTS inbound_events (
    id TEXT PRIMARY KEY,
    connector_type TEXT NOT NULL,
    account_id TEXT NOT NULL,
    external_event_id TEXT NOT NULL,
    payload_json TEXT NULL,
    status TEXT NOT NULL,
    received_at TEXT NOT NULL,
    processed_at TEXT NULL,
    error_message TEXT NULL,
    UNIQUE(connector_type, account_id, external_event_id)
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    codex_thread_id TEXT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    last_message_id TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    inbound_event_id TEXT NULL REFERENCES inbound_events(id) ON DELETE SET NULL,
    codex_turn_id TEXT NULL,
    input_message_id TEXT NULL,
    output_message_id TEXT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT NULL,
    finished_at TEXT NULL,
    error_message TEXT NULL
);

CREATE TABLE IF NOT EXISTS conversation_messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    job_id TEXT NULL REFERENCES jobs(id) ON DELETE SET NULL,
    role TEXT NOT NULL,
    content_text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    codex_item_id TEXT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(conversation_id, role, content_hash, created_at)
);

CREATE TABLE IF NOT EXISTS context_checkpoints (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    codex_thread_id TEXT NULL,
    through_message_id TEXT NOT NULL REFERENCES conversation_messages(id) ON DELETE RESTRICT,
    summary_text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbound_messages (
    id TEXT PRIMARY KEY,
    connector_type TEXT NOT NULL,
    account_id TEXT NOT NULL,
    external_conversation_id TEXT NOT NULL,
    canonical_message_id TEXT NULL REFERENCES conversation_messages(id) ON DELETE SET NULL,
    message_type TEXT NOT NULL,
    payload_json TEXT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    external_message_id TEXT NULL,
    next_retry_at TEXT NULL,
    delivered_at TEXT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_messages_conversation
ON conversation_messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_outbound_status ON outbound_messages(status, next_retry_at);
"""

MIGRATION_2 = """
CREATE TABLE IF NOT EXISTS connector_accounts (
    connector_type TEXT NOT NULL,
    account_id TEXT NOT NULL,
    display_name TEXT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(connector_type, account_id)
);

CREATE TABLE IF NOT EXISTS local_users (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS external_identities (
    id TEXT PRIMARY KEY,
    local_user_id TEXT NOT NULL REFERENCES local_users(id) ON DELETE CASCADE,
    connector_type TEXT NOT NULL,
    account_id TEXT NOT NULL,
    external_user_id TEXT NOT NULL,
    external_conversation_id TEXT NOT NULL,
    paired_at TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    UNIQUE(connector_type, account_id, external_user_id)
);

CREATE TABLE IF NOT EXISTS pairing_codes (
    id TEXT PRIMARY KEY,
    connector_type TEXT NOT NULL,
    account_id TEXT NOT NULL,
    code_salt TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    expires_at TEXT NOT NULL,
    consumed_at TEXT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approval_requests (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    rpc_request_id TEXT NOT NULL,
    nonce_hash TEXT NOT NULL UNIQUE,
    approval_type TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    status TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    resolved_at TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_identity_lookup
ON external_identities(connector_type, account_id, external_user_id, enabled);
CREATE UNIQUE INDEX IF NOT EXISTS idx_single_enabled_identity
ON external_identities(connector_type, account_id) WHERE enabled=1;
CREATE INDEX IF NOT EXISTS idx_pairing_active
ON pairing_codes(connector_type, account_id, consumed_at, expires_at);
"""

MIGRATION_3 = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_inbound_event_unique
ON jobs(inbound_event_id) WHERE inbound_event_id IS NOT NULL;
"""

MIGRATION_4 = """
ALTER TABLE conversations ADD COLUMN model TEXT NULL;
ALTER TABLE conversations ADD COLUMN reasoning_effort TEXT NULL;
"""

MIGRATION_5 = """
CREATE TABLE IF NOT EXISTS project_approval_policies (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    mode TEXT NOT NULL DEFAULT 'safe' CHECK(mode IN ('safe', 'project_auto')),
    scope_path TEXT NULL,
    identity_id TEXT NULL REFERENCES external_identities(id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL
);
"""

MIGRATION_6 = """
ALTER TABLE conversations ADD COLUMN scope TEXT NOT NULL DEFAULT 'project';
ALTER TABLE conversations ADD COLUMN source TEXT NOT NULL DEFAULT 'telegram';
ALTER TABLE conversations ADD COLUMN last_used_at TEXT NOT NULL DEFAULT '';
ALTER TABLE conversations ADD COLUMN is_pinned INTEGER NOT NULL DEFAULT 0;
ALTER TABLE conversations ADD COLUMN archived_at TEXT NULL;
ALTER TABLE conversations ADD COLUMN lock_owner TEXT NULL;
"""

MIGRATION_7 = """
ALTER TABLE app_state ADD COLUMN current_conversation_id TEXT NULL;
"""

MIGRATION_8 = """
CREATE TABLE IF NOT EXISTS discovered_threads (
    codex_thread_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    cwd TEXT NOT NULL,
    source TEXT NOT NULL,
    codex_updated_at INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 0 CHECK(is_active IN (0, 1)),
    project_id TEXT NULL REFERENCES projects(id) ON DELETE SET NULL,
    conversation_id TEXT NULL REFERENCES conversations(id) ON DELETE SET NULL,
    path_available INTEGER NOT NULL DEFAULT 0 CHECK(path_available IN (0, 1)),
    archived_at TEXT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_discovered_threads_project
ON discovered_threads(project_id, archived_at, codex_updated_at);
CREATE INDEX IF NOT EXISTS idx_discovered_threads_conversation
ON discovered_threads(conversation_id);
"""

MIGRATION_9 = """
CREATE TABLE conversations_new (
    id TEXT PRIMARY KEY,
    project_id TEXT NULL REFERENCES projects(id) ON DELETE SET NULL,
    codex_thread_id TEXT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    last_message_id TEXT NULL,
    model TEXT NULL,
    reasoning_effort TEXT NULL,
    scope TEXT NOT NULL DEFAULT 'project',
    source TEXT NOT NULL DEFAULT 'telegram',
    last_used_at TEXT NOT NULL DEFAULT '',
    is_pinned INTEGER NOT NULL DEFAULT 0,
    archived_at TEXT NULL,
    lock_owner TEXT NULL,
    cwd TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO conversations_new(
    id, project_id, codex_thread_id, title, status, last_message_id,
    model, reasoning_effort, scope, source, last_used_at, is_pinned,
    archived_at, lock_owner, created_at, updated_at
)
SELECT id, project_id, codex_thread_id, title, status, last_message_id,
       model, reasoning_effort, scope, source, last_used_at, is_pinned,
       archived_at, lock_owner, created_at, updated_at
FROM conversations;
DROP TABLE conversations;
ALTER TABLE conversations_new RENAME TO conversations;
UPDATE conversations
SET cwd=(SELECT path FROM projects WHERE projects.id=conversations.project_id)
WHERE cwd IS NULL AND project_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_conversations_project
ON conversations(project_id, archived_at, last_used_at);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _is_within(path: Path, root: Path) -> bool:
    """Return whether *path* is equal to or below *root*."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: aiosqlite.Connection | None = None
        self._transaction_lock = asyncio.Lock()

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        async with self._migration_guard():
            try:
                self._connection = await aiosqlite.connect(self.path)
                self._connection.row_factory = aiosqlite.Row
                await self._connection.execute("PRAGMA foreign_keys=ON")
                await self._connection.execute("PRAGMA busy_timeout=5000")
                await self._connection.execute("PRAGMA journal_mode=WAL")
                await self._connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
                await self._migrate()
            except BaseException:
                await self.close()
                raise
        self.path.chmod(0o600)

    @asynccontextmanager
    async def _migration_guard(self) -> AsyncIterator[None]:
        """Serialize schema setup across runtime and short-lived UI connections."""
        lock_path = self.path.with_name(f"{self.path.name}.migration.lock")
        lock_file = lock_path.open("a+", encoding="utf-8")
        try:
            await asyncio.to_thread(fcntl.flock, lock_file.fileno(), fcntl.LOCK_EX)
            lock_path.chmod(0o600)
            yield
        finally:
            await asyncio.to_thread(fcntl.flock, lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def __aenter__(self) -> Database:
        await self.open()
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("database is not open")
        return self._connection

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._transaction_lock:
            connection = self.connection
            # The runtime and short-lived UI workers can open separate SQLite
            # connections during startup. WAL helps readers, but writers still
            # serialize; retry briefly instead of surfacing a transient lock
            # error to the user.
            for attempt in range(6):
                try:
                    await connection.execute("BEGIN IMMEDIATE")
                    break
                except aiosqlite.OperationalError as error:
                    if "locked" not in str(error).casefold() or attempt == 5:
                        raise
                    await asyncio.sleep(0.05 * (attempt + 1))
            try:
                yield connection
            except BaseException:
                await connection.rollback()
                raise
            else:
                await connection.commit()

    async def _migrate(self) -> None:
        connection = self.connection
        await connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
        )
        cursor = await connection.execute("SELECT MAX(version) AS version FROM schema_version")
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("failed to read database schema version")
        version = int(row["version"] or 0)
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema {version} is newer than supported {SCHEMA_VERSION}"
            )
        if version < 1:
            await connection.executescript(MIGRATION_1)
            await connection.execute("DELETE FROM schema_version")
            await connection.execute("INSERT INTO schema_version(version) VALUES (?)", (1,))
            await connection.commit()
            version = 1
        if version < 2:
            await connection.executescript(MIGRATION_2)
            await connection.execute("INSERT INTO schema_version(version) VALUES (?)", (2,))
            await connection.commit()
            version = 2
        if version < 3:
            await connection.executescript(MIGRATION_3)
            await connection.execute("INSERT INTO schema_version(version) VALUES (?)", (3,))
            await connection.commit()
            version = 3
        if version < 4:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    "SELECT MAX(version) AS version FROM schema_version"
                )
                current = await cursor.fetchone()
                current_version = 0 if current is None else int(current["version"] or 0)
                if current_version < 4:
                    cursor = await connection.execute("PRAGMA table_info(conversations)")
                    columns = {str(column["name"]) for column in await cursor.fetchall()}
                    if "model" not in columns:
                        await connection.execute(
                            "ALTER TABLE conversations ADD COLUMN model TEXT NULL"
                        )
                    if "reasoning_effort" not in columns:
                        await connection.execute(
                            "ALTER TABLE conversations ADD COLUMN reasoning_effort TEXT NULL"
                        )
                    await connection.execute(
                        "INSERT INTO schema_version(version) VALUES (?)", (4,)
                    )
            except BaseException:
                await connection.rollback()
                raise
            else:
                await connection.commit()
            version = 4
        if version < 5:
            await connection.executescript(MIGRATION_5)
            await connection.execute("INSERT INTO schema_version(version) VALUES (?)", (5,))
            await connection.commit()
            version = 5
        if version < 6:
            # A partially-applied migration can leave some columns present
            # while the schema version is still 5. Resume it safely.
            for column, definition in (
                ("scope", "TEXT NOT NULL DEFAULT 'project'"),
                ("source", "TEXT NOT NULL DEFAULT 'telegram'"),
                ("last_used_at", "TEXT NOT NULL DEFAULT ''"),
                ("is_pinned", "INTEGER NOT NULL DEFAULT 0"),
                ("archived_at", "TEXT NULL"),
                ("lock_owner", "TEXT NULL"),
            ):
                cursor = await connection.execute("PRAGMA table_info(conversations)")
                columns = {str(item[1]) for item in await cursor.fetchall()}
                if column not in columns:
                    await connection.execute(
                        f"ALTER TABLE conversations ADD COLUMN {column} {definition}"
                    )
            await connection.execute(
                "UPDATE conversations SET last_used_at=updated_at WHERE last_used_at=''"
            )
            await connection.execute("INSERT INTO schema_version(version) VALUES (?)", (6,))
            await connection.commit()
            version = 6
        if version < 7:
            await connection.executescript(MIGRATION_7)
            await connection.execute(
                """UPDATE app_state SET current_conversation_id=(
                       SELECT c.id FROM conversations c
                       WHERE c.project_id=app_state.current_project_id
                         AND c.archived_at IS NULL
                       ORDER BY c.last_used_at DESC, c.updated_at DESC
                       LIMIT 1
                   ) WHERE singleton=1 AND current_conversation_id IS NULL"""
            )
            await connection.execute("INSERT INTO schema_version(version) VALUES (?)", (7,))
            await connection.commit()
            version = 7
        if version < 8:
            await connection.executescript(MIGRATION_8)
            await connection.execute("INSERT INTO schema_version(version) VALUES (?)", (8,))
            await connection.commit()
            version = 8
        if version < 9:
            # SQLite cannot make the legacy project_id column nullable in
            # place. Keep all existing rows and rebuild only this table.
            await connection.execute("PRAGMA foreign_keys=OFF")
            await connection.execute("PRAGMA legacy_alter_table=ON")
            try:
                await connection.executescript(MIGRATION_9)
                await connection.execute("INSERT INTO schema_version(version) VALUES (?)", (9,))
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
            finally:
                await connection.execute("PRAGMA legacy_alter_table=OFF")
                await connection.execute("PRAGMA foreign_keys=ON")

    async def add_project(self, path: Path, name: str | None = None) -> Project:
        resolved = path.expanduser().resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError(f"project path is not a directory: {resolved}")
        project_id = str(uuid.uuid4())
        project_name = (name or resolved.name).strip()
        if not project_name:
            raise ValueError("project name cannot be empty")
        now = utc_now()
        async with self.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO projects(id, name, path, enabled, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    name=excluded.name,
                    enabled=1,
                    updated_at=excluded.updated_at
                """,
                (project_id, project_name, str(resolved), now, now),
            )
            cursor = await connection.execute(
                "SELECT current_project_id, current_conversation_id "
                "FROM app_state WHERE singleton=1"
            )
            state = await cursor.fetchone()
            cursor = await connection.execute(
                "SELECT id FROM projects WHERE path=?", (str(resolved),)
            )
            stored = await cursor.fetchone()
            if state is None or stored is None:
                raise RuntimeError("failed to read project state after insert")
            stored_id = str(stored["id"])
            if (
                state["current_project_id"] is None
                and state["current_conversation_id"] is None
            ):
                await connection.execute(
                    "UPDATE app_state SET current_project_id=? WHERE singleton=1", (stored_id,)
                )
        project = await self.get_project(stored_id)
        if project is None:
            raise RuntimeError("project disappeared after insert")
        return project

    async def list_projects(self) -> list[Project]:
        cursor = await self.connection.execute(
            """
            SELECT p.*, CASE WHEN s.current_project_id=p.id THEN 1 ELSE 0 END AS is_current
            FROM projects p CROSS JOIN app_state s
            WHERE p.enabled=1
            ORDER BY lower(p.name), p.path
            """
        )
        return [self._project_from_row(row) for row in await cursor.fetchall()]

    async def list_all_projects(self) -> list[Project]:
        """Return enabled and disabled projects for global session classification."""
        cursor = await self.connection.execute(
            """
            SELECT p.*, CASE WHEN s.current_project_id=p.id THEN 1 ELSE 0 END AS is_current
            FROM projects p CROSS JOIN app_state s
            ORDER BY p.enabled DESC, lower(p.name), p.path
            """
        )
        return [self._project_from_row(row) for row in await cursor.fetchall()]

    async def disable_missing_projects(self) -> int:
        """Hide registered projects whose paths no longer exist.

        This explicit maintenance action keeps the database row for recovery;
        it never removes project files.
        """
        projects = await self.list_projects()
        missing = [project.id for project in projects if not project.path.is_dir()]
        if not missing:
            return 0
        async with self.transaction() as connection:
            for project_id in missing:
                await connection.execute(
                    "UPDATE projects SET enabled=0, updated_at=? WHERE id=?",
                    (utc_now(), project_id),
                )
        return len(missing)

    async def reconcile_projects(self, paths: set[Path], roots: tuple[Path, ...]) -> int:
        """Make active projects under scan roots match the latest scan result."""
        projects = await self.list_projects()
        normalized_paths = {path.expanduser().resolve() for path in paths}
        normalized_roots = tuple(root.expanduser().resolve() for root in roots)
        stale: list[str] = []
        for project in projects:
            project_path = project.path.resolve()
            if any(_is_within(project_path, root) for root in normalized_roots):
                if project_path not in normalized_paths:
                    stale.append(project.id)
        if not stale:
            return 0
        async with self.transaction() as connection:
            for project_id in stale:
                await connection.execute(
                    "UPDATE projects SET enabled=0, updated_at=? WHERE id=?",
                    (utc_now(), project_id),
                )
        return len(stale)

    async def get_project(self, project_id: str) -> Project | None:
        cursor = await self.connection.execute(
            """
            SELECT p.*, CASE WHEN s.current_project_id=p.id THEN 1 ELSE 0 END AS is_current
            FROM projects p CROSS JOIN app_state s
            WHERE p.id=?
            """,
            (project_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else self._project_from_row(row)

    async def current_project(self) -> Project | None:
        cursor = await self.connection.execute(
            """
            SELECT p.*, 1 AS is_current
            FROM projects p JOIN app_state s ON s.current_project_id=p.id
            WHERE p.enabled=1
            """
        )
        row = await cursor.fetchone()
        return None if row is None else self._project_from_row(row)

    async def switch_project(self, project_id: str) -> Project:
        project = await self.get_project(project_id)
        if project is None or not project.enabled:
            raise ValueError("project is not registered or is disabled")
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE status IN (?, ?, ?, ?)",
                (
                    JobStatus.QUEUED,
                    JobStatus.STARTING,
                    JobStatus.RUNNING,
                    JobStatus.WAITING_APPROVAL,
                ),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("无法确认当前任务状态")
            if int(row["count"]):
                raise RuntimeError("任务运行期间不能切换项目，请等待完成或先使用 /stop。")
            cursor = await connection.execute(
                "SELECT current_project_id FROM app_state WHERE singleton=1"
            )
            state = await cursor.fetchone()
            if state is None:
                raise RuntimeError("无法读取当前项目状态")
            if state["current_project_id"] != project_id:
                await self.reset_project_approval_policies(connection)
            await connection.execute(
                """UPDATE app_state
                   SET current_project_id=?, current_conversation_id=NULL
                   WHERE singleton=1""",
                (project_id,),
            )
        updated = await self.get_project(project_id)
        if updated is None:
            raise RuntimeError("project disappeared after switch")
        return updated

    async def project_approval_mode(
        self,
        project_id: str,
        *,
        connector_type: str = "telegram",
        account_id: str = "main-bot",
    ) -> ProjectApprovalMode:
        """Return the effective project policy, failing closed on stale bindings."""
        cursor = await self.connection.execute(
            """
            SELECT p.path, policy.mode, policy.scope_path, policy.identity_id,
                   enabled_identity.id AS enabled_identity_id
            FROM projects p
            LEFT JOIN project_approval_policies policy ON policy.project_id=p.id
            LEFT JOIN external_identities enabled_identity
              ON enabled_identity.connector_type=?
             AND enabled_identity.account_id=?
             AND enabled_identity.enabled=1
            WHERE p.id=? AND p.enabled=1
            """,
            (connector_type, account_id, project_id),
        )
        row = await cursor.fetchone()
        if row is None or row["mode"] != ProjectApprovalMode.PROJECT_AUTO:
            return ProjectApprovalMode.SAFE
        if (
            row["scope_path"] != row["path"]
            or row["identity_id"] is None
            or row["identity_id"] != row["enabled_identity_id"]
        ):
            return ProjectApprovalMode.SAFE
        return ProjectApprovalMode.PROJECT_AUTO

    async def project_for_turn(self, turn_id: str) -> Project | None:
        cursor = await self.connection.execute(
            """
            SELECT p.*, CASE WHEN s.current_project_id=p.id THEN 1 ELSE 0 END AS is_current
            FROM jobs j
            JOIN conversations c ON c.id=j.conversation_id
            JOIN projects p ON p.id=c.project_id
            CROSS JOIN app_state s
            WHERE j.codex_turn_id=?
            LIMIT 1
            """,
            (turn_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else self._project_from_row(row)

    async def set_current_project_approval_mode(
        self,
        mode: ProjectApprovalMode,
        *,
        connector_type: str,
        account_id: str,
        external_user_id: str,
    ) -> Project:
        """Set a policy only for the current project and currently paired identity."""
        now = utc_now()
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE status IN (?, ?, ?, ?)",
                (
                    JobStatus.QUEUED,
                    JobStatus.STARTING,
                    JobStatus.RUNNING,
                    JobStatus.WAITING_APPROVAL,
                ),
            )
            active = await cursor.fetchone()
            if active is None:
                raise RuntimeError("无法确认当前任务状态")
            if int(active["count"]):
                raise RuntimeError("任务运行期间不能修改审批模式。")
            cursor = await connection.execute(
                """
                SELECT p.id, p.path
                FROM projects p JOIN app_state s ON s.current_project_id=p.id
                WHERE p.enabled=1
                """
            )
            project = await cursor.fetchone()
            if project is None:
                raise RuntimeError("当前没有可用项目。")
            identity_id: str | None = None
            scope_path: str | None = None
            if mode is ProjectApprovalMode.PROJECT_AUTO:
                cursor = await connection.execute(
                    """
                    SELECT e.id
                    FROM external_identities e
                    JOIN local_users u ON u.id=e.local_user_id
                    WHERE e.connector_type=? AND e.account_id=? AND e.external_user_id=?
                      AND e.enabled=1 AND u.enabled=1
                    """,
                    (connector_type, account_id, external_user_id),
                )
                identity = await cursor.fetchone()
                if identity is None:
                    raise RuntimeError("当前 Telegram 账号尚未配对。")
                identity_id = str(identity["id"])
                scope_path = str(project["path"])
            await connection.execute(
                """
                INSERT INTO project_approval_policies(
                    project_id, mode, scope_path, identity_id, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    mode=excluded.mode,
                    scope_path=excluded.scope_path,
                    identity_id=excluded.identity_id,
                    updated_at=excluded.updated_at
                """,
                (str(project["id"]), mode, scope_path, identity_id, now),
            )
        selected = await self.get_project(str(project["id"]))
        if selected is None:
            raise RuntimeError("项目在更新审批模式后不可用")
        return selected

    async def reset_project_approval_policies(self, connection: aiosqlite.Connection) -> None:
        await connection.execute(
            """
            UPDATE project_approval_policies
            SET mode='safe', scope_path=NULL, identity_id=NULL, updated_at=?
            WHERE mode!='safe' OR scope_path IS NOT NULL OR identity_id IS NOT NULL
            """,
            (utc_now(),),
        )

    async def ingest_event(
        self,
        *,
        connector_type: str,
        account_id: str,
        external_event_id: str,
        payload: dict[str, Any],
        cursor_name: str,
        cursor_value: str,
    ) -> tuple[str, bool]:
        event_id = str(uuid.uuid4())
        now = utc_now()
        async with self.transaction() as connection:
            result = await connection.execute(
                """
                INSERT OR IGNORE INTO inbound_events(
                    id, connector_type, account_id, external_event_id,
                    payload_json, status, received_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    event_id,
                    connector_type,
                    account_id,
                    external_event_id,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    now,
                ),
            )
            inserted = result.rowcount == 1
            if not inserted:
                cursor = await connection.execute(
                    """
                    SELECT id FROM inbound_events
                    WHERE connector_type=? AND account_id=? AND external_event_id=?
                    """,
                    (connector_type, account_id, external_event_id),
                )
                existing = await cursor.fetchone()
                if existing is None:
                    raise RuntimeError("duplicate inbound event could not be loaded")
                event_id = str(existing["id"])
            await connection.execute(
                """
                INSERT INTO connector_cursors(
                    connector_type, account_id, cursor_name, cursor_value, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(connector_type, account_id, cursor_name) DO UPDATE SET
                    cursor_value=excluded.cursor_value,
                    updated_at=excluded.updated_at
                """,
                (connector_type, account_id, cursor_name, cursor_value, now),
            )
        return event_id, inserted

    async def connector_cursor(
        self, *, connector_type: str, account_id: str, cursor_name: str
    ) -> str | None:
        cursor = await self.connection.execute(
            """
            SELECT cursor_value FROM connector_cursors
            WHERE connector_type=? AND account_id=? AND cursor_name=?
            """,
            (connector_type, account_id, cursor_name),
        )
        row = await cursor.fetchone()
        return None if row is None else str(row["cursor_value"])

    async def pending_inbound_events(
        self, *, connector_type: str, account_id: str
    ) -> list[tuple[str, dict[str, Any]]]:
        cursor = await self.connection.execute(
            """
            SELECT id, payload_json FROM inbound_events
            WHERE connector_type=? AND account_id=? AND status IN ('pending', 'failed')
              AND payload_json IS NOT NULL
            ORDER BY received_at, id
            """,
            (connector_type, account_id),
        )
        events: list[tuple[str, dict[str, Any]]] = []
        for row in await cursor.fetchall():
            payload = json.loads(str(row["payload_json"]))
            if isinstance(payload, dict):
                events.append((str(row["id"]), payload))
        return events

    async def mark_inbound_processed(self, event_id: str) -> None:
        async with self.transaction() as connection:
            await connection.execute(
                """
                UPDATE inbound_events
                SET status='processed', processed_at=?, error_message=NULL
                WHERE id=?
                """,
                (utc_now(), event_id),
            )

    async def mark_inbound_failed(self, event_id: str, error_type: str) -> None:
        async with self.transaction() as connection:
            await connection.execute(
                """
                UPDATE inbound_events
                SET status='failed', error_message=?
                WHERE id=?
                """,
                (error_type[:200], event_id),
            )

    async def is_authorized_identity(
        self, *, connector_type: str, account_id: str, external_user_id: str
    ) -> bool:
        cursor = await self.connection.execute(
            """
            SELECT 1
            FROM external_identities e
            JOIN local_users u ON u.id=e.local_user_id
            WHERE e.connector_type=? AND e.account_id=? AND e.external_user_id=?
              AND e.enabled=1 AND u.enabled=1
            """,
            (connector_type, account_id, external_user_id),
        )
        return await cursor.fetchone() is not None

    async def has_enabled_identity(self, *, connector_type: str, account_id: str) -> bool:
        """Return whether an account has a currently enabled paired identity."""
        cursor = await self.connection.execute(
            """
            SELECT 1
            FROM external_identities e
            JOIN local_users u ON u.id=e.local_user_id
            WHERE e.connector_type=? AND e.account_id=?
              AND e.enabled=1 AND u.enabled=1
            LIMIT 1
            """,
            (connector_type, account_id),
        )
        return await cursor.fetchone() is not None

    async def create_conversation(
        self,
        project_id: str | None,
        title: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        scope: str = "project",
        source: str = "telegram",
        cwd: Path | None = None,
    ) -> str:
        conversation_id = str(uuid.uuid4())
        now = utc_now()
        async with self.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO conversations(
                    id, project_id, title, status, model, reasoning_effort,
                    scope, source, last_used_at, created_at, updated_at, cwd
                ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    project_id,
                    title.strip() or "New conversation",
                    model,
                    reasoning_effort,
                    scope,
                    source,
                    now,
                    now,
                    now,
                    None if cwd is None else str(cwd.expanduser().resolve()),
                ),
            )
        return conversation_id

    async def active_conversation(self, project_id: str) -> Conversation | None:
        cursor = await self.connection.execute(
            """
            SELECT id, project_id, codex_thread_id, title, status, last_message_id,
                   model, reasoning_effort
            FROM conversations
            WHERE project_id=? AND status='active'
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (project_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else self._conversation_from_row(row)

    async def current_conversation(self, project_id: str) -> Conversation | None:
        cursor = await self.connection.execute(
            """SELECT c.id, c.project_id, c.codex_thread_id, c.title, c.status,
                      c.last_message_id, c.model, c.reasoning_effort, c.scope,
                      c.source, c.last_used_at, c.is_pinned, c.archived_at, c.lock_owner
               FROM conversations c JOIN app_state s
                 ON s.current_conversation_id=c.id
               WHERE c.project_id=? AND c.archived_at IS NULL""",
            (project_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else self._conversation_from_row(row)

    async def current_global_conversation(self) -> Conversation | None:
        """Return the selected conversation regardless of project ownership."""
        cursor = await self.connection.execute(
            """SELECT c.id, c.project_id, c.codex_thread_id, c.title, c.status,
                      c.last_message_id, c.model, c.reasoning_effort, c.scope,
                      c.source, c.last_used_at, c.is_pinned, c.archived_at,
                      c.lock_owner, c.cwd
               FROM conversations c JOIN app_state s
                 ON s.current_conversation_id=c.id
               WHERE c.archived_at IS NULL"""
        )
        row = await cursor.fetchone()
        return None if row is None else self._conversation_from_row(row)

    async def select_conversation(
        self, conversation_id: str, project_id: str | None = None
    ) -> Conversation:
        now = utc_now()
        async with self.transaction() as connection:
            if project_id is None:
                cursor = await connection.execute(
                    "SELECT id, project_id FROM conversations "
                    "WHERE id=? AND archived_at IS NULL",
                    (conversation_id,),
                )
            else:
                cursor = await connection.execute(
                    "SELECT id, project_id FROM conversations "
                    "WHERE id=? AND project_id=? AND archived_at IS NULL",
                    (conversation_id, project_id),
                )
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("会话不存在或已归档")
            await connection.execute(
                "UPDATE app_state SET current_project_id=?, current_conversation_id=? "
                "WHERE singleton=1",
                (row["project_id"], conversation_id),
            )
            await connection.execute(
                "UPDATE conversations SET last_used_at=?, updated_at=? WHERE id=?",
                (now, now, conversation_id),
            )
        selected = await self.conversation(conversation_id)
        if selected is None:
            raise RuntimeError("会话切换后无法读取会话")
        return selected

    async def acquire_conversation_lock(self, conversation_id: str, owner: str) -> None:
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "SELECT lock_owner FROM conversations WHERE id=? AND archived_at IS NULL",
                (conversation_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("会话不存在或已归档")
            current = row["lock_owner"]
            if current is not None and str(current) != owner:
                raise RuntimeError(f"会话正在被 {current} 使用")
            await connection.execute(
                "UPDATE conversations SET lock_owner=?, last_used_at=?, updated_at=? WHERE id=?",
                (owner, utc_now(), utc_now(), conversation_id),
            )

    async def release_conversation_lock(
        self, conversation_id: str, owner: str | None = None
    ) -> bool:
        async with self.transaction() as connection:
            if owner is None:
                result = await connection.execute(
                    "UPDATE conversations SET lock_owner=NULL, updated_at=? WHERE id=?",
                    (utc_now(), conversation_id),
                )
            else:
                result = await connection.execute(
                    "UPDATE conversations SET lock_owner=NULL, updated_at=? "
                    "WHERE id=? AND lock_owner=?",
                    (utc_now(), conversation_id, owner),
                )
            return result.rowcount == 1

    async def get_or_create_active_conversation(
        self, project_id: str, title: str = "CodexRelay"
    ) -> Conversation:
        existing = await self.current_conversation(project_id)
        if existing is None:
            existing = await self.active_conversation(project_id)
        if existing is not None:
            async with self.transaction() as connection:
                await connection.execute(
                    "UPDATE app_state SET current_project_id=?, current_conversation_id=? "
                    "WHERE singleton=1",
                    (project_id, existing.id),
                )
                if existing.cwd is None:
                    project = await self.get_project(project_id)
                    if project is not None:
                        await connection.execute(
                            "UPDATE conversations SET cwd=? WHERE id=?",
                            (str(project.path), existing.id),
                        )
            return existing
        conversation_id = await self.create_conversation(project_id, title)
        async with self.transaction() as connection:
            await connection.execute(
                "UPDATE app_state SET current_conversation_id=? WHERE singleton=1",
                (conversation_id,),
            )
        created = await self.conversation(conversation_id)
        if created is None or created.id != conversation_id:
            raise RuntimeError("failed to create active conversation")
        return created

    async def list_all_conversations(self) -> list[Conversation]:
        cursor = await self.connection.execute(
            """SELECT id, project_id, codex_thread_id, title, status, last_message_id,
                      model, reasoning_effort, scope, source, last_used_at,
                      is_pinned, archived_at, lock_owner, cwd
               FROM conversations
               WHERE archived_at IS NULL
               ORDER BY is_pinned DESC, last_used_at DESC, created_at DESC"""
        )
        return [self._conversation_from_row(row) for row in await cursor.fetchall()]

    async def create_standalone_conversation(
        self, cwd: Path, title: str = "临时会话", source: str = "telegram"
    ) -> Conversation:
        resolved = cwd.expanduser().resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("会话工作目录不是有效目录")
        conversation_id = await self.create_conversation(
            None,
            title,
            scope="standalone",
            source=source,
            cwd=resolved,
        )
        await self.select_conversation(conversation_id)
        selected = await self.conversation(conversation_id)
        if selected is None:
            raise RuntimeError("独立会话创建后无法读取")
        return selected

    async def start_new_conversation(self, project_id: str, title: str) -> Conversation:
        conversation_id = str(uuid.uuid4())
        now = utc_now()
        project = await self.get_project(project_id)
        if project is None or not project.enabled:
            raise RuntimeError("项目不存在或已停用。")
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE status IN (?, ?, ?, ?)",
                (
                    JobStatus.QUEUED,
                    JobStatus.STARTING,
                    JobStatus.RUNNING,
                    JobStatus.WAITING_APPROVAL,
                ),
            )
            active = await cursor.fetchone()
            if active is None:
                raise RuntimeError("无法确认当前任务状态")
            if int(active["count"]):
                raise RuntimeError("任务运行期间不能新建对话。")
            cursor = await connection.execute(
                """
                SELECT model, reasoning_effort FROM conversations
                WHERE project_id=? AND status='active'
                ORDER BY updated_at DESC LIMIT 1
                """,
                (project_id,),
            )
            previous = await cursor.fetchone()
            await connection.execute(
                """
                INSERT INTO conversations(
                    id, project_id, title, status, model, reasoning_effort,
                    scope, source, last_used_at, created_at, updated_at, cwd
                ) VALUES (?, ?, ?, 'active', ?, ?, 'project', 'telegram', ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    project_id,
                    title.strip() or "New conversation",
                    None if previous is None else previous["model"],
                    None if previous is None else previous["reasoning_effort"],
                    now,
                    now,
                    now,
                    str(project.path),
                ),
            )
            await connection.execute(
                "UPDATE app_state SET current_conversation_id=? WHERE singleton=1",
                (conversation_id,),
            )
        created = await self.conversation(conversation_id)
        if created is None or created.id != conversation_id:
            raise RuntimeError("failed to start a new conversation")
        return created

    async def conversation(self, conversation_id: str) -> Conversation | None:
        cursor = await self.connection.execute(
            """
            SELECT id, project_id, codex_thread_id, title, status, last_message_id,
                   model, reasoning_effort, scope, source, last_used_at,
                   is_pinned, archived_at, lock_owner, cwd
            FROM conversations WHERE id=?
            """,
            (conversation_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else self._conversation_from_row(row)

    async def list_conversations(self, project_id: str) -> list[Conversation]:
        cursor = await self.connection.execute(
            """SELECT id, project_id, codex_thread_id, title, status, last_message_id,
                      model, reasoning_effort, scope, source, last_used_at,
                      is_pinned, archived_at, lock_owner, cwd
               FROM conversations
               WHERE project_id=? AND archived_at IS NULL
               ORDER BY is_pinned DESC,
                        COALESCE(
                            (SELECT d.codex_updated_at FROM discovered_threads d
                             WHERE d.conversation_id=conversations.id), 0
                        ) DESC,
                        last_used_at DESC, created_at DESC""",
            (project_id,),
        )
        return [self._conversation_from_row(row) for row in await cursor.fetchall()]

    async def reconcile_global_threads(self, threads: list[DesktopThread]) -> None:
        """Refresh the global Codex thread index after a successful complete listing.

        Exact cwd matches are assigned to registered projects.  Everything else
        remains visible but unassigned, so discovery never grants project access.
        Missing threads are recoverably archived rather than deleted.
        """
        if await self.active_job_count():
            return
        projects = await self.list_all_projects()
        project_by_id = {project.id: project for project in projects}
        project_roots = sorted(
            (
                (project.path.expanduser().resolve(), project)
                for project in projects
            ),
            key=lambda item: len(item[0].parts),
            reverse=True,
        )
        seen: set[str] = set()
        now = utc_now()
        async with self.transaction() as connection:
            # Re-check after BEGIN IMMEDIATE. A task may have started while
            # the complete Codex listing was in flight; preserve the current
            # snapshot and let the next scheduled sync retry in that case.
            cursor = await connection.execute(
                """SELECT COUNT(*) AS count FROM jobs
                   WHERE status IN (?, ?, ?, ?)""",
                (
                    JobStatus.QUEUED,
                    JobStatus.STARTING,
                    JobStatus.RUNNING,
                    JobStatus.WAITING_APPROVAL,
                ),
            )
            active = await cursor.fetchone()
            if active is None:
                raise RuntimeError("无法确认当前任务状态")
            if int(active["count"]):
                return
            for value in threads:
                thread_id = value.thread_id
                title = value.title.strip() or "未命名会话"
                cwd = value.cwd.expanduser()
                resolved_cwd = cwd.resolve()
                project = next(
                    (
                        candidate
                        for root, candidate in project_roots
                        if _is_within(resolved_cwd, root)
                    ),
                    None,
                )
                cursor = await connection.execute(
                    """SELECT project_id, id, scope FROM conversations
                       WHERE codex_thread_id=?
                       ORDER BY CASE WHEN project_id IS NOT NULL THEN 1 ELSE 0 END DESC,
                                archived_at IS NULL DESC,
                                last_used_at DESC, updated_at DESC LIMIT 1""",
                    (thread_id,),
                )
                binding = await cursor.fetchone()
                if binding is not None:
                    bound_project = project_by_id.get(str(binding["project_id"]))
                    if bound_project is not None:
                        project = bound_project
                    elif binding["project_id"] is None and binding["scope"] == "standalone":
                        # A standalone conversation is an explicit user choice.
                        # Keep it unassigned even when its cwd happens to sit
                        # under a registered project root; only an explicit
                        # UI assignment may change that association.
                        project = None
                # A project association is an authorization boundary, not a
                # label. If a previously assigned thread reports a cwd outside
                # that boundary (for example after a folder move), retain the
                # thread in the index for diagnosis but execute from the
                # authorized project root until the user explicitly changes
                # the association.
                conversation_cwd = resolved_cwd
                if project is not None:
                    project_root = project.path.expanduser().resolve()
                    if not _is_within(resolved_cwd, project_root):
                        conversation_cwd = project_root
                source = value.source
                updated_at = value.updated_at
                is_active = value.is_active
                path_available = cwd.is_dir()
                conversation_id = str(binding["id"]) if binding is not None else None
                if project is not None and conversation_id is None:
                    cursor = await connection.execute(
                        """SELECT id FROM conversations
                           WHERE project_id=? AND codex_thread_id=?
                           LIMIT 1""",
                        (project.id, thread_id),
                    )
                    row = await cursor.fetchone()
                    if row is not None:
                        conversation_id = str(row["id"])
                if project is None and conversation_id is None:
                    conversation_id = str(uuid.uuid4())
                    await connection.execute(
                        """INSERT INTO conversations(
                               id, project_id, codex_thread_id, title, status, source,
                               scope, last_used_at, created_at, updated_at, cwd
                           ) VALUES (?, NULL, ?, ?, 'active', ?, 'standalone', ?, ?, ?, ?)""",
                        (
                            conversation_id,
                            thread_id,
                            title[:120],
                            source,
                            now,
                            now,
                            now,
                            str(conversation_cwd),
                        ),
                    )
                await connection.execute(
                    """
                    INSERT INTO discovered_threads(
                        codex_thread_id, title, cwd, source, codex_updated_at,
                        is_active, project_id, conversation_id, path_available,
                        archived_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    ON CONFLICT(codex_thread_id) DO UPDATE SET
                        title=excluded.title,
                        cwd=excluded.cwd,
                        source=excluded.source,
                        codex_updated_at=excluded.codex_updated_at,
                        is_active=excluded.is_active,
                        project_id=excluded.project_id,
                        conversation_id=excluded.conversation_id,
                        path_available=excluded.path_available,
                        archived_at=NULL,
                        last_seen_at=excluded.last_seen_at
                    """,
                    (
                        thread_id,
                        title[:120],
                        str(cwd),
                        source,
                        updated_at,
                        int(is_active),
                        None if project is None else project.id,
                        conversation_id,
                        int(path_available),
                        now,
                    ),
                )
                # A thread may have an old unassigned discovery row and a
                # later explicit project binding. Keep the canonical
                # conversation row in sync with the currently observed
                # thread, including restoring a previously archived binding.
                if conversation_id is not None:
                    await connection.execute(
                        """UPDATE conversations
                           SET project_id=?, title=?, source=?, cwd=?,
                               archived_at=NULL, status='active', updated_at=?
                           WHERE id=?""",
                        (
                            None if project is None else project.id,
                            title[:120],
                            source,
                            str(conversation_cwd),
                            now,
                            conversation_id,
                        ),
                    )
                seen.add(thread_id)
            query = (
                "UPDATE discovered_threads SET archived_at=?, is_active=0 "
                "WHERE archived_at IS NULL"
            )
            parameters: list[str] = [now]
            if seen:
                placeholders = ",".join("?" for _ in seen)
                query += f" AND codex_thread_id NOT IN ({placeholders})"
                parameters.extend(sorted(seen))
            await connection.execute(query, parameters)
            # Reconcile conversation rows as well as the discovery index. This
            # is especially important for standalone (unassigned) sessions:
            # when a thread is deleted in Codex, the selected local
            # conversation must not remain a phantom current session.
            archive_query = (
                "UPDATE conversations SET archived_at=?, lock_owner=NULL "
                "WHERE archived_at IS NULL AND codex_thread_id IS NOT NULL "
                "AND NOT EXISTS ("
                "SELECT 1 FROM jobs WHERE jobs.conversation_id=conversations.id "
                "AND jobs.status IN (?, ?, ?, ?)"
                ")"
            )
            archive_parameters: list[str] = [
                now,
                JobStatus.QUEUED,
                JobStatus.STARTING,
                JobStatus.RUNNING,
                JobStatus.WAITING_APPROVAL,
            ]
            if seen:
                placeholders = ",".join("?" for _ in seen)
                archive_query += f" AND codex_thread_id NOT IN ({placeholders})"
                archive_parameters.extend(sorted(seen))
            await connection.execute(archive_query, archive_parameters)
            await connection.execute(
                """UPDATE app_state SET current_conversation_id=NULL
                   WHERE singleton=1 AND current_conversation_id IN (
                       SELECT id FROM conversations WHERE archived_at=?
                   )""",
                (now,),
            )
            await connection.execute(
                """UPDATE app_state
                   SET current_conversation_id=COALESCE(
                       current_conversation_id,
                       (SELECT d.conversation_id FROM discovered_threads d
                        WHERE d.archived_at IS NULL
                          AND d.project_id=app_state.current_project_id
                        ORDER BY d.codex_updated_at DESC LIMIT 1),
                       (SELECT d.conversation_id FROM discovered_threads d
                        WHERE d.archived_at IS NULL
                        ORDER BY d.codex_updated_at DESC LIMIT 1)
                   )
                   WHERE singleton=1 AND current_conversation_id IS NULL"""
            )

    async def list_global_sessions(
        self, *, include_archived: bool = False
    ) -> list[GlobalSession]:
        archived_clause = "" if include_archived else "WHERE d.archived_at IS NULL"
        cursor = await self.connection.execute(
            f"""
            SELECT d.*, p.name AS project_name, p.enabled AS project_enabled,
                   CASE WHEN s.current_project_id=d.project_id THEN 1 ELSE 0 END
                       AS is_current_project,
                   CASE WHEN s.current_conversation_id=d.conversation_id THEN 1 ELSE 0 END
                       AS is_current_conversation
            FROM discovered_threads d
            LEFT JOIN projects p ON p.id=d.project_id
            CROSS JOIN app_state s
            {archived_clause}
            ORDER BY d.archived_at IS NOT NULL,
                     is_current_project DESC,
                     d.project_id IS NULL,
                     lower(COALESCE(p.name, '')),
                     d.codex_updated_at DESC,
                     lower(d.title)
            """
        )
        return [self._global_session_from_row(row) for row in await cursor.fetchall()]

    async def assign_global_session(self, thread_id: str, project_id: str) -> Conversation:
        """Explicitly bind a discovered thread to an already authorized project."""
        if await self.active_job_count():
            raise RuntimeError("任务运行期间不能修改会话归属。")
        project = await self.get_project(project_id)
        if project is None or not project.enabled:
            raise RuntimeError("目标项目未授权或已停用。")
        cursor = await self.connection.execute(
            """SELECT title, source, archived_at FROM discovered_threads
               WHERE codex_thread_id=?""",
            (thread_id,),
        )
        row = await cursor.fetchone()
        if row is None or row["archived_at"] is not None:
            raise RuntimeError("该 Codex 会话已经不存在或已归档。")
        cursor = await self.connection.execute(
            "SELECT project_id FROM discovered_threads WHERE codex_thread_id=?",
            (thread_id,),
        )
        assignment = await cursor.fetchone()
        if assignment is not None and assignment["project_id"] not in (None, project_id):
            raise RuntimeError("该会话已经归属于其他项目。")
        conversation = await self.register_external_conversation(
            project.id,
            codex_thread_id=thread_id,
            title=str(row["title"]),
            source=str(row["source"]),
            allow_rebind=True,
        )
        async with self.transaction() as connection:
            await connection.execute(
                """UPDATE discovered_threads
                   SET project_id=?, conversation_id=?
                   WHERE codex_thread_id=?""",
                (project.id, conversation.id, thread_id),
            )
        return conversation

    async def archive_missing_codex_conversations(
        self, project_id: str, present_thread_ids: set[str]
    ) -> int:
        """Hide Codex conversations no longer returned by Codex thread/list.

        This reconciliation is only called after a successful discovery request,
        so a transient App Server failure cannot erase the local view.  Archived
        rows remain in SQLite for context recovery but no longer appear in
        Telegram's selectable session list.
        """
        now = utc_now()
        async with self.transaction() as connection:
            query = (
                "UPDATE conversations SET archived_at=?, lock_owner=NULL "
                "WHERE project_id=? AND archived_at IS NULL "
                "AND codex_thread_id IS NOT NULL "
                "AND NOT EXISTS ("
                "SELECT 1 FROM jobs WHERE jobs.conversation_id=conversations.id "
                "AND jobs.status IN (?, ?, ?, ?)"
                ")"
            )
            parameters = [
                now,
                project_id,
                JobStatus.QUEUED,
                JobStatus.STARTING,
                JobStatus.RUNNING,
                JobStatus.WAITING_APPROVAL,
            ]
            if present_thread_ids:
                placeholders = ",".join("?" for _ in present_thread_ids)
                query += f" AND codex_thread_id NOT IN ({placeholders})"
                parameters.extend(sorted(present_thread_ids))
            result = await connection.execute(query, parameters)
            await connection.execute(
                """UPDATE app_state SET current_conversation_id=NULL
                   WHERE singleton=1 AND current_conversation_id IN (
                       SELECT id FROM conversations
                       WHERE project_id=? AND archived_at=?
                   )""",
                (project_id, now),
            )
            return result.rowcount

    async def select_first_available_conversation(self, project_id: str) -> Conversation | None:
        """Select the first real Codex conversation when the current one vanished."""
        current = await self.current_conversation(project_id)
        if current is not None:
            return current
        cursor = await self.connection.execute(
            """SELECT id FROM conversations
               WHERE project_id=? AND archived_at IS NULL
                 AND codex_thread_id IS NOT NULL
               ORDER BY is_pinned DESC, last_used_at DESC, created_at DESC
               LIMIT 1""",
            (project_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        conversation_id = str(row["id"])
        async with self.transaction() as connection:
            await connection.execute(
                "UPDATE app_state SET current_conversation_id=? WHERE singleton=1",
                (conversation_id,),
            )
        return await self.conversation(conversation_id)

    async def register_external_conversation(
        self,
        project_id: str | None,
        *,
        codex_thread_id: str,
        title: str,
        source: str = "desktop",
        cwd: Path | None = None,
        allow_rebind: bool = False,
    ) -> Conversation:
        """Register a locally discovered Codex thread idempotently."""
        now = utc_now()
        clean_title = title.strip() or "未命名会话"
        if cwd is None and project_id is not None:
            project = await self.get_project(project_id)
            cwd = None if project is None else project.path
        async with self.transaction() as connection:
            # A Codex thread is the canonical conversation identity. Reuse one
            # existing row, including an archived row, so rebinding cannot
            # split its message history or model settings into duplicates.
            cursor = await connection.execute(
                """SELECT id, project_id, source, archived_at FROM conversations
                   WHERE codex_thread_id=?
                   ORDER BY CASE WHEN project_id=? THEN 0
                                 WHEN project_id IS NULL THEN 1
                                 ELSE 2 END,
                            archived_at IS NULL DESC,
                            last_used_at DESC, updated_at DESC
                   LIMIT 1""",
                (codex_thread_id, project_id),
            )
            row = await cursor.fetchone()
            effective_project_id = project_id
            effective_cwd = cwd
            if row is not None:
                existing_project_id = row["project_id"]
                if (
                    not allow_rebind
                    and existing_project_id != project_id
                    and not (
                        existing_project_id is None
                        and project_id is not None
                        and str(row["source"]) in {"desktop", "desktop_migrated"}
                    )
                ):
                    # Automatic discovery never moves a thread away from an
                    # explicit association (including an explicit standalone
                    # choice). Only an explicit UI assignment may rebind it.
                    effective_project_id = (
                        str(existing_project_id) if existing_project_id is not None else None
                    )
                if cwd is None and effective_project_id is not None:
                    project = await self.get_project(str(effective_project_id))
                    cwd = None if project is None else project.path
                effective_cwd = cwd
                if effective_project_id is not None:
                    project = await self.get_project(str(effective_project_id))
                    if project is not None:
                        project_root = project.path.expanduser().resolve()
                        candidate = (
                            project_root
                            if cwd is None
                            else cwd.expanduser().resolve()
                        )
                        effective_cwd = (
                            candidate if _is_within(candidate, project_root) else project_root
                        )
                conversation_id = str(row["id"])
                await connection.execute(
                    """UPDATE conversations
                       SET project_id=?, title=?, source=?, scope=?, cwd=?,
                           archived_at=NULL, status='active', updated_at=?
                       WHERE id=?""",
                    (
                        effective_project_id,
                        clean_title,
                        source,
                        "standalone" if effective_project_id is None else "project",
                        (
                            None
                            if effective_cwd is None
                            else str(effective_cwd.expanduser().resolve())
                        ),
                        now,
                        conversation_id,
                    ),
                )
            else:
                conversation_id = str(uuid.uuid4())
                effective_cwd = cwd
                if effective_project_id is not None:
                    project = await self.get_project(str(effective_project_id))
                    if project is not None:
                        project_root = project.path.expanduser().resolve()
                        candidate = (
                            project_root
                            if cwd is None
                            else cwd.expanduser().resolve()
                        )
                        effective_cwd = (
                            candidate if _is_within(candidate, project_root) else project_root
                        )
                await connection.execute(
                    """INSERT INTO conversations(
                           id, project_id, codex_thread_id, title, status, source,
                           scope, last_used_at, created_at, updated_at, cwd
                       ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)""",
                    (
                        conversation_id,
                        effective_project_id,
                        codex_thread_id,
                        clean_title,
                        source,
                        "standalone" if effective_project_id is None else "project",
                        now,
                        now,
                        now,
                        (
                            None
                            if effective_cwd is None
                            else str(effective_cwd.expanduser().resolve())
                        ),
                    ),
                )
            await connection.execute(
                """UPDATE discovered_threads
                   SET project_id=?, conversation_id=?
                   WHERE codex_thread_id=?""",
                (effective_project_id, conversation_id, codex_thread_id),
            )
            if effective_cwd is not None:
                await connection.execute(
                    "UPDATE conversations SET cwd=? WHERE id=?",
                    (str(effective_cwd.expanduser().resolve()), conversation_id),
                )
        conversation = await self.conversation(conversation_id)
        if conversation is None:
            raise RuntimeError("external conversation disappeared after registration")
        return conversation

    async def activate_global_session(self, thread_id: str) -> Conversation:
        """Switch the app to any available project or standalone session."""
        if await self.active_job_count():
            raise RuntimeError("任务运行期间不能切换会话。")
        cursor = await self.connection.execute(
            """
            SELECT d.project_id, d.conversation_id, d.cwd, d.title, d.source, p.enabled
            FROM discovered_threads d
            LEFT JOIN projects p ON p.id=d.project_id
            WHERE d.codex_thread_id=? AND d.archived_at IS NULL
            """,
            (thread_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("该 Codex 会话已经不存在或已归档。")
        if row["conversation_id"] is None:
            registered = await self.register_external_conversation(
                None,
                codex_thread_id=thread_id,
                title=str(row["title"]),
                source=str(row["source"]),
                cwd=Path(str(row["cwd"])),
            )
            conversation_id = registered.id
        else:
            conversation_id = str(row["conversation_id"])
        if row["project_id"] is not None and not bool(row["enabled"]):
            raise RuntimeError("会话所属项目已停用，请先重新授权项目。")
        async with self.transaction() as connection:
            if row["project_id"] is not None:
                await self.reset_project_approval_policies(connection)
            await connection.execute(
                """UPDATE app_state
                   SET current_project_id=?, current_conversation_id=?
                   WHERE singleton=1""",
                (row["project_id"], conversation_id),
            )
            await connection.execute(
                "UPDATE conversations SET last_used_at=?, updated_at=? WHERE id=?",
                (utc_now(), utc_now(), conversation_id),
            )
        conversation = await self.conversation(conversation_id)
        if conversation is None:
            raise RuntimeError("会话切换后无法读取。")
        return conversation

    async def set_active_conversation_model(
        self,
        project_id: str,
        *,
        model: str,
        reasoning_effort: str,
        title: str = "CodexRelay",
    ) -> Conversation:
        """Backward-compatible project-scoped model setter."""
        current = await self.current_conversation(project_id)
        if current is not None:
            return await self.set_conversation_model(
                current.id,
                model=model,
                reasoning_effort=reasoning_effort,
            )
        if not model.strip() or not reasoning_effort.strip():
            raise ValueError("model and reasoning effort are required")
        conversation_id = str(uuid.uuid4())
        now = utc_now()
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE status IN (?, ?, ?, ?)",
                (
                    JobStatus.QUEUED,
                    JobStatus.STARTING,
                    JobStatus.RUNNING,
                    JobStatus.WAITING_APPROVAL,
                ),
            )
            active = await cursor.fetchone()
            if active is None:
                raise RuntimeError("无法确认当前任务状态")
            if int(active["count"]):
                raise RuntimeError("任务运行期间不能修改模型或推理强度。")
            cursor = await connection.execute(
                """SELECT c.id FROM conversations c
                   JOIN app_state s ON s.current_conversation_id=c.id
                   WHERE c.project_id=? AND c.archived_at IS NULL""",
                (project_id,),
            )
            existing = await cursor.fetchone()
            if existing is None:
                cursor = await connection.execute(
                    """SELECT id FROM conversations
                       WHERE project_id=? AND archived_at IS NULL
                       ORDER BY last_used_at DESC, created_at DESC LIMIT 1""",
                    (project_id,),
                )
                existing = await cursor.fetchone()
            if existing is None:
                await connection.execute(
                    """
                    INSERT INTO conversations(
                        id, project_id, title, status, model, reasoning_effort,
                        scope, source, last_used_at, created_at, updated_at
                    ) VALUES (?, ?, ?, 'active', ?, ?, 'project', 'telegram', ?, ?, ?)
                    """,
                    (
                        conversation_id,
                        project_id,
                        title.strip() or "New conversation",
                        model,
                        reasoning_effort,
                        now,
                        now,
                        now,
                    ),
                )
            else:
                conversation_id = str(existing["id"])
                await connection.execute(
                    """
                    UPDATE conversations
                    SET model=?, reasoning_effort=?, last_used_at=?, updated_at=?
                    WHERE id=?
                    """,
                    (model, reasoning_effort, now, now, conversation_id),
                )
            await connection.execute(
                "UPDATE app_state SET current_conversation_id=? WHERE singleton=1",
                (conversation_id,),
            )
        configured = await self.conversation(conversation_id)
        if configured is None:
            raise RuntimeError("conversation disappeared after model update")
        return configured

    async def set_conversation_model(
        self,
        conversation_id: str,
        *,
        model: str,
        reasoning_effort: str,
    ) -> Conversation:
        """Set model settings on the selected conversation, with no project requirement."""
        if not model.strip() or not reasoning_effort.strip():
            raise ValueError("model and reasoning effort are required")
        now = utc_now()
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE status IN (?, ?, ?, ?)",
                (
                    JobStatus.QUEUED,
                    JobStatus.STARTING,
                    JobStatus.RUNNING,
                    JobStatus.WAITING_APPROVAL,
                ),
            )
            active = await cursor.fetchone()
            if active is None:
                raise RuntimeError("无法确认当前任务状态")
            if int(active["count"]):
                raise RuntimeError("任务运行期间不能修改模型或推理强度。")
            result = await connection.execute(
                """UPDATE conversations
                   SET model=?, reasoning_effort=?, last_used_at=?, updated_at=?
                   WHERE id=? AND archived_at IS NULL""",
                (model, reasoning_effort, now, now, conversation_id),
            )
            if result.rowcount != 1:
                raise RuntimeError("当前会话不存在或已归档。")
            await connection.execute(
                "UPDATE app_state SET current_conversation_id=? WHERE singleton=1",
                (conversation_id,),
            )
        configured = await self.conversation(conversation_id)
        if configured is None:
            raise RuntimeError("conversation disappeared after model update")
        return configured

    async def mark_job_starting(self, job_id: str) -> None:
        async with self.transaction() as connection:
            result = await connection.execute(
                """
                UPDATE jobs SET status=?, started_at=?
                WHERE id=? AND status=?
                """,
                (JobStatus.STARTING, utc_now(), job_id, JobStatus.QUEUED),
            )
            if result.rowcount != 1:
                raise RuntimeError("job is not queued")

    async def mark_turn_started(self, job_id: str, thread_id: str, turn_id: str) -> None:
        now = utc_now()
        async with self.transaction() as connection:
            cursor = await connection.execute(
                """SELECT j.conversation_id, c.project_id, c.title, c.source,
                          COALESCE(c.cwd, p.path) AS cwd
                   FROM jobs j
                   JOIN conversations c ON c.id=j.conversation_id
                   LEFT JOIN projects p ON p.id=c.project_id
                   WHERE j.id=? AND j.status=?""",
                (job_id, JobStatus.STARTING),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("job is not starting")
            await connection.execute(
                """
                UPDATE jobs SET codex_turn_id=?, status=? WHERE id=?
                """,
                (turn_id, JobStatus.RUNNING, job_id),
            )
            await connection.execute(
                """
                UPDATE conversations SET codex_thread_id=?, updated_at=? WHERE id=?
                """,
                (thread_id, now, row["conversation_id"]),
            )
            cwd = row["cwd"]
            if cwd is None:
                raise RuntimeError("conversation has no working directory")
            path_available = Path(str(cwd)).expanduser().is_dir()
            codex_updated_at = int(datetime.now(UTC).timestamp())
            await connection.execute(
                """INSERT INTO discovered_threads(
                       codex_thread_id, title, cwd, source, codex_updated_at,
                       is_active, project_id, conversation_id, path_available,
                       archived_at, last_seen_at
                   ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, NULL, ?)
                   ON CONFLICT(codex_thread_id) DO UPDATE SET
                       title=excluded.title,
                       cwd=excluded.cwd,
                       source=excluded.source,
                       codex_updated_at=excluded.codex_updated_at,
                       is_active=1,
                       project_id=excluded.project_id,
                       conversation_id=excluded.conversation_id,
                       path_available=excluded.path_available,
                       archived_at=NULL,
                       last_seen_at=excluded.last_seen_at""",
                (
                    thread_id,
                    str(row["title"]).strip() or "未命名会话",
                    str(cwd),
                    str(row["source"]),
                    codex_updated_at,
                    row["project_id"],
                    row["conversation_id"],
                    int(path_available),
                    now,
                ),
            )

    async def fail_job(self, job_id: str, error_message: str) -> None:
        async with self.transaction() as connection:
            await connection.execute(
                """
                UPDATE jobs
                SET status=?, finished_at=?, error_message=?
                WHERE id=? AND status IN (?, ?, ?, ?)
                """,
                (
                    JobStatus.FAILED,
                    utc_now(),
                    error_message[:1000],
                    job_id,
                    JobStatus.QUEUED,
                    JobStatus.STARTING,
                    JobStatus.RUNNING,
                    JobStatus.WAITING_APPROVAL,
                ),
            )

    async def active_job(self) -> tuple[str, str | None] | None:
        cursor = await self.connection.execute(
            """
            SELECT id, codex_turn_id FROM jobs
            WHERE status IN (?, ?, ?, ?)
            ORDER BY started_at, rowid LIMIT 1
            """,
            (
                JobStatus.QUEUED,
                JobStatus.STARTING,
                JobStatus.RUNNING,
                JobStatus.WAITING_APPROVAL,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return str(row["id"]), (
            str(row["codex_turn_id"]) if row["codex_turn_id"] is not None else None
        )

    async def mark_job_interrupted(self, job_id: str) -> None:
        async with self.transaction() as connection:
            await connection.execute(
                """
                UPDATE jobs SET status=?, finished_at=?
                WHERE id=? AND status IN (?, ?, ?, ?)
                """,
                (
                    JobStatus.INTERRUPTED,
                    utc_now(),
                    job_id,
                    JobStatus.QUEUED,
                    JobStatus.STARTING,
                    JobStatus.RUNNING,
                    JobStatus.WAITING_APPROVAL,
                ),
            )

    async def complete_job(self, job_id: str, text: str) -> str:
        message_id = str(uuid.uuid4())
        now = utc_now()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "SELECT conversation_id FROM jobs WHERE id=? AND status=?",
                (job_id, JobStatus.RUNNING),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("job is not running")
            conversation_id = str(row["conversation_id"])
            await connection.execute(
                """
                INSERT INTO conversation_messages(
                    id, conversation_id, job_id, role, content_text, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    conversation_id,
                    job_id,
                    MessageRole.ASSISTANT,
                    text,
                    digest,
                    now,
                ),
            )
            await connection.execute(
                """
                UPDATE jobs SET output_message_id=?, status=?, finished_at=? WHERE id=?
                """,
                (message_id, JobStatus.COMPLETED, now, job_id),
            )
            await connection.execute(
                "UPDATE conversations SET last_message_id=?, updated_at=? WHERE id=?",
                (message_id, now, conversation_id),
            )
        return message_id

    async def queue_canonical_reply(
        self,
        *,
        canonical_message_id: str,
        connector_type: str,
        account_id: str,
        external_conversation_id: str,
    ) -> str:
        cursor = await self.connection.execute(
            "SELECT content_text FROM conversation_messages WHERE id=?",
            (canonical_message_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ValueError("canonical message does not exist")
        outbound_id = str(uuid.uuid4())
        async with self.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO outbound_messages(
                    id, connector_type, account_id, external_conversation_id,
                    canonical_message_id, message_type, payload_json,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'text', ?, 'pending', ?)
                """,
                (
                    outbound_id,
                    connector_type,
                    account_id,
                    external_conversation_id,
                    canonical_message_id,
                    json.dumps(
                        {"text": row["content_text"]},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    utc_now(),
                ),
            )
        return outbound_id

    async def queue_text(
        self,
        *,
        connector_type: str,
        account_id: str,
        external_conversation_id: str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> str:
        outbound_id = str(uuid.uuid4())
        async with self.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO outbound_messages(
                    id, connector_type, account_id, external_conversation_id,
                    message_type, payload_json, status, created_at
                ) VALUES (?, ?, ?, ?, 'text', ?, 'pending', ?)
                """,
                (
                    outbound_id,
                    connector_type,
                    account_id,
                    external_conversation_id,
                    json.dumps(
                        {"text": text, "reply_markup": reply_markup},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    utc_now(),
                ),
            )
        return outbound_id

    async def pending_outbound_messages(
        self, *, connector_type: str, account_id: str, limit: int = 20
    ) -> list[OutboundMessage]:
        cursor = await self.connection.execute(
            """
            SELECT id, connector_type, account_id, external_conversation_id,
                   payload_json, attempt_count
            FROM outbound_messages
            WHERE connector_type=? AND account_id=? AND status IN ('pending', 'retry')
              AND payload_json IS NOT NULL
              AND (next_retry_at IS NULL OR next_retry_at<=?)
            ORDER BY created_at, rowid
            LIMIT ?
            """,
            (connector_type, account_id, utc_now(), limit),
        )
        return [
            OutboundMessage(
                id=str(row["id"]),
                connector_type=str(row["connector_type"]),
                account_id=str(row["account_id"]),
                external_conversation_id=str(row["external_conversation_id"]),
                payload_json=str(row["payload_json"]),
                attempt_count=int(row["attempt_count"]),
            )
            for row in await cursor.fetchall()
        ]

    async def mark_outbound_delivered(self, outbound_id: str, external_message_id: str) -> None:
        async with self.transaction() as connection:
            await connection.execute(
                """
                UPDATE outbound_messages
                SET status='delivered', external_message_id=?, delivered_at=?, next_retry_at=NULL
                WHERE id=?
                """,
                (external_message_id, utc_now(), outbound_id),
            )

    async def mark_outbound_retry(
        self, outbound_id: str, *, next_retry_at: str, terminal: bool = False
    ) -> None:
        async with self.transaction() as connection:
            await connection.execute(
                """
                UPDATE outbound_messages
                SET status=?, attempt_count=attempt_count+1, next_retry_at=?
                WHERE id=?
                """,
                ("failed" if terminal else "retry", next_retry_at, outbound_id),
            )

    async def active_job_count(self) -> int:
        cursor = await self.connection.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE status IN (?, ?, ?, ?)",
            (
                JobStatus.QUEUED,
                JobStatus.STARTING,
                JobStatus.RUNNING,
                JobStatus.WAITING_APPROVAL,
            ),
        )
        row = await cursor.fetchone()
        return 0 if row is None else int(row["count"])

    async def active_job_status(self) -> JobStatus | None:
        cursor = await self.connection.execute(
            """
            SELECT status FROM jobs
            WHERE status IN (?, ?, ?, ?)
            ORDER BY started_at, rowid
            LIMIT 1
            """,
            (
                JobStatus.QUEUED,
                JobStatus.STARTING,
                JobStatus.RUNNING,
                JobStatus.WAITING_APPROVAL,
            ),
        )
        row = await cursor.fetchone()
        return None if row is None else JobStatus(str(row["status"]))

    async def active_job_project(self) -> Project | None:
        cursor = await self.connection.execute(
            """
            SELECT p.*, CASE WHEN s.current_project_id=p.id THEN 1 ELSE 0 END AS is_current
            FROM jobs j
            JOIN conversations c ON c.id=j.conversation_id
            JOIN projects p ON p.id=c.project_id
            CROSS JOIN app_state s
            WHERE j.status IN (?, ?, ?, ?)
            ORDER BY j.started_at, j.rowid
            LIMIT 1
            """,
            (
                JobStatus.QUEUED,
                JobStatus.STARTING,
                JobStatus.RUNNING,
                JobStatus.WAITING_APPROVAL,
            ),
        )
        row = await cursor.fetchone()
        return None if row is None else self._project_from_row(row)

    async def job_status_for_inbound_event(self, event_id: str) -> JobStatus | None:
        cursor = await self.connection.execute(
            "SELECT status FROM jobs WHERE inbound_event_id=? LIMIT 1", (event_id,)
        )
        row = await cursor.fetchone()
        return None if row is None else JobStatus(str(row["status"]))

    async def interrupt_stale_jobs(self) -> int:
        """Make crash recovery conservative: never replay an uncertain prior turn."""
        async with self.transaction() as connection:
            result = await connection.execute(
                """
                UPDATE jobs
                SET status=?, finished_at=?, error_message='runtime_restarted'
                WHERE status IN (?, ?, ?, ?)
                """,
                (
                    JobStatus.INTERRUPTED,
                    utc_now(),
                    JobStatus.QUEUED,
                    JobStatus.STARTING,
                    JobStatus.RUNNING,
                    JobStatus.WAITING_APPROVAL,
                ),
            )
        return result.rowcount

    async def clear_stale_conversation_locks(self) -> int:
        """Clear leases left by a crashed runtime after stale jobs are reconciled."""
        async with self.transaction() as connection:
            result = await connection.execute(
                """UPDATE conversations SET lock_owner=NULL, updated_at=?
                   WHERE lock_owner IS NOT NULL
                     AND id NOT IN (
                       SELECT conversation_id FROM jobs
                       WHERE status IN (?, ?, ?, ?)
                     )""",
                (
                    utc_now(),
                    JobStatus.QUEUED,
                    JobStatus.STARTING,
                    JobStatus.RUNNING,
                    JobStatus.WAITING_APPROVAL,
                ),
            )
        return result.rowcount

    async def housekeep(self, *, now: datetime | None = None) -> dict[str, int]:
        """Trim transport details while preserving canonical conversation history."""
        current = now or datetime.now(UTC)
        transport_cutoff = (current - timedelta(days=7)).isoformat(timespec="milliseconds")
        diagnostic_cutoff = (current - timedelta(days=30)).isoformat(timespec="milliseconds")
        counts: dict[str, int] = {}
        async with self.transaction() as connection:
            processed = await connection.execute(
                """
                UPDATE inbound_events SET payload_json=NULL, error_message=NULL
                WHERE status='processed' AND processed_at IS NOT NULL AND processed_at<?
                  AND payload_json IS NOT NULL
                """,
                (transport_cutoff,),
            )
            delivered = await connection.execute(
                """
                UPDATE outbound_messages SET payload_json=NULL
                WHERE status='delivered' AND delivered_at IS NOT NULL AND delivered_at<?
                  AND payload_json IS NOT NULL
                """,
                (transport_cutoff,),
            )
            failed_inbound = await connection.execute(
                """
                UPDATE inbound_events SET payload_json=NULL, error_message=NULL
                WHERE status='failed' AND received_at<?
                  AND (payload_json IS NOT NULL OR error_message IS NOT NULL)
                """,
                (diagnostic_cutoff,),
            )
            failed_outbound = await connection.execute(
                """
                UPDATE outbound_messages SET payload_json=NULL
                WHERE status='failed' AND created_at<? AND payload_json IS NOT NULL
                """,
                (diagnostic_cutoff,),
            )
            approvals = await connection.execute(
                """
                UPDATE approval_requests SET summary_json='{}'
                WHERE status!='pending' AND resolved_at IS NOT NULL AND resolved_at<?
                  AND summary_json!='{}'
                """,
                (diagnostic_cutoff,),
            )
            counts = {
                "processed_inbound": processed.rowcount,
                "delivered_outbound": delivered.rowcount,
                "failed_inbound": failed_inbound.rowcount,
                "failed_outbound": failed_outbound.rowcount,
                "resolved_approvals": approvals.rowcount,
            }
        await self.connection.execute("PRAGMA optimize")
        await self.connection.execute("PRAGMA incremental_vacuum(256)")
        await self.connection.commit()
        return counts

    async def authorized_conversation_id(
        self, *, connector_type: str, account_id: str
    ) -> str | None:
        cursor = await self.connection.execute(
            """
            SELECT e.external_conversation_id
            FROM external_identities e
            JOIN local_users u ON u.id=e.local_user_id
            WHERE e.connector_type=? AND e.account_id=? AND e.enabled=1 AND u.enabled=1
            LIMIT 1
            """,
            (connector_type, account_id),
        )
        row = await cursor.fetchone()
        return None if row is None else str(row["external_conversation_id"])

    async def job_id_for_turn(self, turn_id: str) -> str | None:
        cursor = await self.connection.execute(
            "SELECT id FROM jobs WHERE codex_turn_id=? LIMIT 1", (turn_id,)
        )
        row = await cursor.fetchone()
        return None if row is None else str(row["id"])

    async def create_approval_request(
        self,
        *,
        approval_id: str,
        job_id: str,
        rpc_request_id: str,
        nonce_hash: str,
        approval_type: str,
        summary: dict[str, Any],
        expires_at: str,
    ) -> None:
        async with self.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO approval_requests(
                    id, job_id, rpc_request_id, nonce_hash, approval_type,
                    summary_json, status, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    approval_id,
                    job_id,
                    rpc_request_id,
                    nonce_hash,
                    approval_type,
                    json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
                    expires_at,
                ),
            )
            await connection.execute(
                "UPDATE jobs SET status=? WHERE id=? AND status=?",
                (JobStatus.WAITING_APPROVAL, job_id, JobStatus.RUNNING),
            )

    async def resolve_approval(self, nonce_hash: str, decision: str) -> tuple[bool, str | None]:
        now = utc_now()
        async with self.transaction() as connection:
            cursor = await connection.execute(
                """
                SELECT id, job_id FROM approval_requests
                WHERE nonce_hash=? AND status='pending' AND expires_at>?
                """,
                (nonce_hash, now),
            )
            row = await cursor.fetchone()
            if row is None:
                return False, None
            result = await connection.execute(
                """
                UPDATE approval_requests SET status=?, resolved_at=?
                WHERE id=? AND status='pending'
                """,
                (decision, now, row["id"]),
            )
            if result.rowcount != 1:
                return False, None
            await connection.execute(
                "UPDATE jobs SET status=? WHERE id=? AND status=?",
                (JobStatus.RUNNING, row["job_id"], JobStatus.WAITING_APPROVAL),
            )
            return True, str(row["job_id"])

    async def expire_pending_approvals(self) -> int:
        now = utc_now()
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "SELECT job_id FROM approval_requests WHERE status='pending'"
            )
            job_ids = [str(row["job_id"]) for row in await cursor.fetchall()]
            result = await connection.execute(
                """
                UPDATE approval_requests SET status='expired', resolved_at=?
                WHERE status='pending'
                """,
                (now,),
            )
            for job_id in job_ids:
                await connection.execute(
                    "UPDATE jobs SET status=? WHERE id=? AND status=?",
                    (JobStatus.RUNNING, job_id, JobStatus.WAITING_APPROVAL),
                )
        return result.rowcount

    async def expire_approval(self, nonce_hash: str) -> bool:
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "SELECT job_id FROM approval_requests WHERE nonce_hash=? AND status='pending'",
                (nonce_hash,),
            )
            row = await cursor.fetchone()
            if row is None:
                return False
            await connection.execute(
                """
                UPDATE approval_requests SET status='expired', resolved_at=?
                WHERE nonce_hash=? AND status='pending'
                """,
                (utc_now(), nonce_hash),
            )
            await connection.execute(
                "UPDATE jobs SET status=? WHERE id=? AND status=?",
                (JobStatus.RUNNING, row["job_id"], JobStatus.WAITING_APPROVAL),
            )
        return True

    async def create_queued_job_with_input(
        self,
        *,
        conversation_id: str,
        text: str,
        inbound_event_id: str | None = None,
    ) -> tuple[str, CanonicalMessage]:
        if not text.strip():
            raise ValueError("message cannot be empty")
        job_id = str(uuid.uuid4())
        message_id = str(uuid.uuid4())
        now = utc_now()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        async with self.transaction() as connection:
            # The first version intentionally has one global execution slot.
            # Enforce that invariant in the same write transaction that queues
            # the job, so future connectors cannot bypass the Telegram router's
            # in-memory lock or race each other between a check and an insert.
            cursor = await connection.execute(
                """SELECT COUNT(*) AS count FROM jobs
                   WHERE status IN (?, ?, ?, ?)""",
                (
                    JobStatus.QUEUED,
                    JobStatus.STARTING,
                    JobStatus.RUNNING,
                    JobStatus.WAITING_APPROVAL,
                ),
            )
            active = await cursor.fetchone()
            if active is None:
                raise RuntimeError("无法确认当前任务状态")
            if int(active["count"]):
                raise RuntimeError("全局已有任务运行，请等待完成后再开始新任务。")
            await connection.execute(
                """
                INSERT INTO jobs(
                    id, conversation_id, inbound_event_id, input_message_id,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    conversation_id,
                    inbound_event_id,
                    message_id,
                    JobStatus.QUEUED,
                    now,
                ),
            )
            await connection.execute(
                """
                INSERT INTO conversation_messages(
                    id, conversation_id, job_id, role, content_text, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (message_id, conversation_id, job_id, MessageRole.USER, text, digest, now),
            )
            await connection.execute(
                """
                UPDATE conversations SET last_message_id=?, updated_at=? WHERE id=?
                """,
                (message_id, now, conversation_id),
            )
        return job_id, CanonicalMessage(
            id=message_id,
            conversation_id=conversation_id,
            job_id=job_id,
            role=MessageRole.USER,
            content_text=text,
            content_hash=digest,
            created_at=now,
        )

    async def complete_job_and_queue_reply(
        self,
        *,
        job_id: str,
        text: str,
        connector_type: str,
        account_id: str,
        external_conversation_id: str,
    ) -> tuple[str, str]:
        message_id = str(uuid.uuid4())
        outbound_id = str(uuid.uuid4())
        now = utc_now()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        async with self.transaction() as connection:
            cursor = await connection.execute(
                "SELECT conversation_id FROM jobs WHERE id=?", (job_id,)
            )
            row = await cursor.fetchone()
            if row is None:
                raise ValueError("job does not exist")
            conversation_id = str(row["conversation_id"])
            await connection.execute(
                """
                INSERT INTO conversation_messages(
                    id, conversation_id, job_id, role, content_text, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    conversation_id,
                    job_id,
                    MessageRole.ASSISTANT,
                    text,
                    digest,
                    now,
                ),
            )
            await connection.execute(
                """
                UPDATE jobs
                SET output_message_id=?, status=?, finished_at=?
                WHERE id=?
                """,
                (message_id, JobStatus.COMPLETED, now, job_id),
            )
            await connection.execute(
                "UPDATE conversations SET last_message_id=?, updated_at=? WHERE id=?",
                (message_id, now, conversation_id),
            )
            await connection.execute(
                """
                INSERT INTO outbound_messages(
                    id, connector_type, account_id, external_conversation_id,
                    canonical_message_id, message_type, payload_json,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'text', ?, 'pending', ?)
                """,
                (
                    outbound_id,
                    connector_type,
                    account_id,
                    external_conversation_id,
                    message_id,
                    json.dumps({"text": text}, ensure_ascii=False, separators=(",", ":")),
                    now,
                ),
            )
        return message_id, outbound_id

    async def rebuild_missing_outbox(
        self,
        *,
        connector_type: str,
        account_id: str,
        external_conversation_id: str,
    ) -> int:
        now = utc_now()
        cursor = await self.connection.execute(
            """
            SELECT m.id, m.content_text
            FROM jobs j
            JOIN conversation_messages m ON m.id=j.output_message_id
            LEFT JOIN outbound_messages o ON o.canonical_message_id=m.id
            WHERE j.status=? AND o.id IS NULL
            """,
            (JobStatus.COMPLETED,),
        )
        rows = list(await cursor.fetchall())
        if not rows:
            return 0
        async with self.transaction() as connection:
            for row in rows:
                await connection.execute(
                    """
                    INSERT INTO outbound_messages(
                        id, connector_type, account_id, external_conversation_id,
                        canonical_message_id, message_type, payload_json,
                        status, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'text', ?, 'pending', ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        connector_type,
                        account_id,
                        external_conversation_id,
                        row["id"],
                        json.dumps(
                            {"text": row["content_text"]},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        now,
                    ),
                )
        return len(rows)

    @staticmethod
    def _project_from_row(row: aiosqlite.Row) -> Project:
        return Project(
            id=str(row["id"]),
            name=str(row["name"]),
            path=Path(str(row["path"])),
            enabled=bool(row["enabled"]),
            is_current=bool(row["is_current"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _conversation_from_row(row: aiosqlite.Row) -> Conversation:
        raw_cwd = row["cwd"] if "cwd" in row.keys() else None
        return Conversation(
            id=str(row["id"]),
            project_id=str(row["project_id"]) if row["project_id"] is not None else None,
            codex_thread_id=(
                str(row["codex_thread_id"]) if row["codex_thread_id"] is not None else None
            ),
            title=str(row["title"]),
            status=str(row["status"]),
            last_message_id=(
                str(row["last_message_id"]) if row["last_message_id"] is not None else None
            ),
            model=str(row["model"]) if row["model"] is not None else None,
            reasoning_effort=(
                str(row["reasoning_effort"])
                if row["reasoning_effort"] is not None
                else None
            ),
            scope=str(row["scope"]) if "scope" in row.keys() else "project",
            source=str(row["source"]) if "source" in row.keys() else "telegram",
            last_used_at=(
                str(row["last_used_at"])
                if "last_used_at" in row.keys()
                else ""
            ),
            is_pinned=bool(row["is_pinned"]) if "is_pinned" in row.keys() else False,
            archived_at=(
                str(row["archived_at"])
                if "archived_at" in row.keys() and row["archived_at"] is not None
                else None
            ),
            lock_owner=(
                str(row["lock_owner"])
                if "lock_owner" in row.keys() and row["lock_owner"] is not None
                else None
            ),
            cwd=Path(str(raw_cwd)) if raw_cwd is not None else None,
        )

    @staticmethod
    def _global_session_from_row(row: aiosqlite.Row) -> GlobalSession:
        return GlobalSession(
            thread_id=str(row["codex_thread_id"]),
            title=str(row["title"]),
            cwd=Path(str(row["cwd"])),
            source=str(row["source"]),
            codex_updated_at=int(row["codex_updated_at"]),
            is_active=bool(row["is_active"]),
            project_id=str(row["project_id"]) if row["project_id"] is not None else None,
            project_name=(
                str(row["project_name"]) if row["project_name"] is not None else None
            ),
            project_enabled=bool(row["project_enabled"]),
            conversation_id=(
                str(row["conversation_id"])
                if row["conversation_id"] is not None
                else None
            ),
            is_current_project=bool(row["is_current_project"]),
            is_current_conversation=bool(row["is_current_conversation"]),
            path_available=bool(row["path_available"]),
            archived_at=(
                str(row["archived_at"]) if row["archived_at"] is not None else None
            ),
        )
