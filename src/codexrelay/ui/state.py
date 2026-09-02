from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from codexrelay.models import JobStatus


class RuntimeState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    CONNECTED = "connected"
    RESTARTING = "restarting"
    ATTENTION = "attention"
    STOPPING = "stopping"


@dataclass(frozen=True, slots=True)
class AppStatusSnapshot:
    runtime_state: RuntimeState = RuntimeState.STARTING
    bot_username: str | None = None
    telegram_paired: bool = False
    current_project: str | None = None
    active_project: str | None = None
    active_job_count: int = 0
    active_job_status: JobStatus | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    last_error: str | None = None

    @property
    def connection_title(self) -> str:
        if self.runtime_state is RuntimeState.CONNECTED:
            return (
                "Telegram 已连接 · 已配对"
                if self.telegram_paired
                else "Telegram 已连接 · 待完成配对"
            )
        return {
            RuntimeState.STARTING: "正在连接",
            RuntimeState.RESTARTING: "正在重连",
            RuntimeState.ATTENTION: "需要处理",
            RuntimeState.STOPPING: "正在退出",
            RuntimeState.STOPPED: "未连接",
        }[self.runtime_state]

    @property
    def task_title(self) -> str:
        if self.active_job_count == 0:
            return "空闲"
        if self.active_job_status is JobStatus.WAITING_APPROVAL:
            return "等待安全审批"
        if self.active_job_status is JobStatus.STARTING:
            return "正在启动"
        return "正在运行"

    @property
    def model_title(self) -> str:
        model = self.model or "本机默认模型"
        effort = self.reasoning_effort or "默认推理强度"
        return f"{model} · {effort}"

    def persisted(
        self,
        *,
        telegram_paired: bool,
        current_project: str | None,
        active_project: str | None,
        active_job_count: int,
        active_job_status: JobStatus | None,
        model: str | None,
        reasoning_effort: str | None,
    ) -> AppStatusSnapshot:
        return replace(
            self,
            telegram_paired=telegram_paired,
            current_project=current_project,
            active_project=active_project,
            active_job_count=active_job_count,
            active_job_status=active_job_status,
            model=model,
            reasoning_effort=reasoning_effort,
        )
