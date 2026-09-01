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


ACTIVE_JOB_STATUSES = frozenset({JobStatus.STARTING, JobStatus.RUNNING, JobStatus.WAITING_APPROVAL})


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


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
    project_id: str
    codex_thread_id: str | None
    title: str
    status: str
    last_message_id: str | None
    model: str | None
    reasoning_effort: str | None


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
