from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class JobStatus(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    ABANDONED = "abandoned"


ACTIVE_JOB_STATUSES = frozenset(
    {JobStatus.QUEUED, JobStatus.STARTING, JobStatus.RUNNING, JobStatus.WAITING_APPROVAL}
)


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ProjectApprovalMode(StrEnum):
    SAFE = "safe"
    PROJECT_AUTO = "project_auto"


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    name: str
    path: Path
    enabled: bool
    is_current: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Conversation:
    id: str
    project_id: str | None
    codex_thread_id: str | None
    title: str
    status: str
    last_message_id: str | None
    model: str | None
    reasoning_effort: str | None
    scope: str
    source: str
    last_used_at: str
    is_pinned: bool
    archived_at: str | None
    lock_owner: str | None
    cwd: Path | None = None


@dataclass(frozen=True, slots=True)
class GlobalSession:
    thread_id: str
    title: str
    cwd: Path
    source: str
    codex_updated_at: int
    is_active: bool
    project_id: str | None
    project_name: str | None
    project_enabled: bool
    conversation_id: str | None
    is_current_project: bool
    is_current_conversation: bool
    path_available: bool
    archived_at: str | None

    @property
    def is_unassigned(self) -> bool:
        return self.project_id is None


@dataclass(frozen=True, slots=True)
class CanonicalMessage:
    id: str
    conversation_id: str
    job_id: str | None
    role: MessageRole
    content_text: str
    content_hash: str
    created_at: str


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    id: str
    connector_type: str
    account_id: str
    external_conversation_id: str
    payload_json: str
    attempt_count: int


@dataclass(frozen=True, slots=True)
class LifecycleState:
    state: str
    started_at: str | None
    last_seen_at: str | None
    offline_since: str | None
    last_reason: str | None
    updated_at: str
