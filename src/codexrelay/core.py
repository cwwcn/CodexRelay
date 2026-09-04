from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from codexrelay.codex.base import CodexBackend, ProgressCallback
from codexrelay.database import Database
from codexrelay.projects import ProjectService
from codexrelay.sleep import SleepInhibitor


@dataclass(frozen=True, slots=True)
class DeliveryTarget:
    connector_type: str
    account_id: str
    external_conversation_id: str


@dataclass(frozen=True, slots=True)
class RelayResult:
    job_id: str
    conversation_id: str
    thread_id: str
    turn_id: str | None
    final_text: str
    outbound_id: str | None


class RelayService:
    def __init__(
        self,
        *,
        database: Database,
        backend: CodexBackend,
        sleep_inhibitor: SleepInhibitor,
    ) -> None:
        self.database = database
        self.backend = backend
        self.sleep_inhibitor = sleep_inhibitor

    async def run_current_project(
        self,
        *,
        text: str,
        image_paths: tuple[Path, ...] = (),
        inbound_event_id: str | None = None,
        delivery: DeliveryTarget | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> RelayResult:
        project = await self.database.current_project()
        if project is None:
            raise RuntimeError("no current project is configured")
        return await self.run_project(
            project_id=project.id,
            text=text,
            image_paths=image_paths,
            inbound_event_id=inbound_event_id,
            delivery=delivery,
            on_progress=on_progress,
        )

    async def run_project(
        self,
        *,
        project_id: str,
        text: str,
        image_paths: tuple[Path, ...] = (),
        inbound_event_id: str | None = None,
        delivery: DeliveryTarget | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> RelayResult:
        """Run against the explicitly selected project.

        The explicit ``project_id`` keeps the turn and its persisted conversation
        bound to one project. Telegram rejects /use while a turn is active.
        """
        project = await self.database.get_project(project_id)
        if project is None or not project.enabled:
            raise RuntimeError("the selected project is no longer available")
        # Fail before creating a job or starting Codex if macOS TCC access has
        # disappeared. This prevents a permission prompt from interrupting a
        # remotely initiated task halfway through execution.
        ProjectService.preflight_access(project.path)
        conversation = await self.database.get_or_create_active_conversation(
            project.id, title=project.name
        )
        preflight_thread = getattr(self.backend, "preflight_thread", None)
        if conversation.codex_thread_id is not None and callable(preflight_thread):
            await preflight_thread(
                project=project.path,
                thread_id=conversation.codex_thread_id,
                model=conversation.model,
                approval_mode=await self.database.project_approval_mode(project.id),
            )
        job_id, _message = await self.database.create_queued_job_with_input(
            conversation_id=conversation.id,
            text=text if text.strip() else "[Image input]",
            inbound_event_id=inbound_event_id,
        )
        await self.database.mark_job_starting(job_id)
        execution_conversation = await self.database.conversation(conversation.id)
        if execution_conversation is None:
            await self.database.fail_job(job_id, "conversation_disappeared")
            raise RuntimeError("conversation disappeared before Codex execution")
        owner = delivery.connector_type if delivery is not None else "local"
        # A conversation selected from Telegram may already have a persistent
        # lease. Keep that lease after the turn so `/release` remains the
        # explicit hand-off point to the desktop client. Newly acquired leases
        # are transient and are released when this turn finishes.
        persistent_lock = execution_conversation.lock_owner == owner
        try:
            await self.database.acquire_conversation_lock(conversation.id, owner)
        except BaseException as error:
            await self.database.fail_job(job_id, type(error).__name__)
            raise
        project_approval_mode = await self.database.project_approval_mode(project.id)

        async def on_turn_started(thread_id: str, turn_id: str) -> None:
            await self.database.mark_turn_started(job_id, thread_id, turn_id)

        try:
            async with self.sleep_inhibitor.lease():
                if on_progress is None:
                    if project_approval_mode.value == "safe":
                        result = await self.backend.run_turn(
                            project=project.path,
                            text=text,
                            image_paths=image_paths,
                            thread_id=execution_conversation.codex_thread_id,
                            model=execution_conversation.model,
                            reasoning_effort=execution_conversation.reasoning_effort,
                            on_turn_started=on_turn_started,
                        )
                    else:
                        result = await self.backend.run_turn(
                            project=project.path,
                            text=text,
                            image_paths=image_paths,
                            thread_id=execution_conversation.codex_thread_id,
                            model=execution_conversation.model,
                            reasoning_effort=execution_conversation.reasoning_effort,
                            approval_mode=project_approval_mode,
                            on_turn_started=on_turn_started,
                        )
                else:
                    if project_approval_mode.value == "safe":
                        result = await self.backend.run_turn(
                            project=project.path,
                            text=text,
                            image_paths=image_paths,
                            thread_id=execution_conversation.codex_thread_id,
                            model=execution_conversation.model,
                            reasoning_effort=execution_conversation.reasoning_effort,
                            on_turn_started=on_turn_started,
                            on_progress=on_progress,
                        )
                    else:
                        result = await self.backend.run_turn(
                            project=project.path,
                            text=text,
                            image_paths=image_paths,
                            thread_id=execution_conversation.codex_thread_id,
                            model=execution_conversation.model,
                            reasoning_effort=execution_conversation.reasoning_effort,
                            approval_mode=project_approval_mode,
                            on_turn_started=on_turn_started,
                            on_progress=on_progress,
                        )
            final_text = result.final_text if result.final_text.strip() else "任务已完成。"
            canonical_message_id = await self.database.complete_job(job_id, final_text)
            outbound_id = None
            if delivery is not None:
                outbound_id = await self.database.queue_canonical_reply(
                    canonical_message_id=canonical_message_id,
                    connector_type=delivery.connector_type,
                    account_id=delivery.account_id,
                    external_conversation_id=delivery.external_conversation_id,
                )
        except BaseException as error:
            await self.database.fail_job(job_id, type(error).__name__)
            raise
        finally:
            if not persistent_lock:
                await self.database.release_conversation_lock(conversation.id, owner)
        return RelayResult(
            job_id=job_id,
            conversation_id=conversation.id,
            thread_id=result.thread_id,
            turn_id=result.turn_id,
            final_text=final_text,
            outbound_id=outbound_id,
        )

    async def interrupt_active(self) -> bool:
        active = await self.database.active_job()
        if active is None:
            return False
        job_id, turn_id = active
        if turn_id is not None:
            await self.backend.interrupt(turn_id)
        await self.database.mark_job_interrupted(job_id)
        return True
