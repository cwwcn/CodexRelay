from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from typing import Literal, Protocol

from codexrelay.codex.model_catalog import (
    CodexModelCatalog,
    CodexModelOption,
    reasoning_effort_label,
)
from codexrelay.connectors.base import IncomingMessage
from codexrelay.connectors.telegram.api import TelegramClient
from codexrelay.core import DeliveryTarget, RelayService
from codexrelay.database import Database
from codexrelay.models import Conversation, JobStatus, Project
from codexrelay.pairing import PairingError, PairingService
from codexrelay.projects import ProjectService

PAIR_PATTERN = re.compile(r"^/(?:pair\s+|start\s+pair_)(\d{6})$")


class ApprovalResolver(Protocol):
    async def resolve_callback(
        self, callback_data: str
    ) -> Literal["accept", "decline"] | None: ...


class TelegramRouter:
    def __init__(
        self,
        *,
        database: Database,
        client: TelegramClient,
        relay: RelayService,
        pairing: PairingService,
        project_service: ProjectService,
        temporary_directory: Path,
        max_image_bytes: int = 20 * 1024 * 1024,
        approval_resolver: ApprovalResolver | None = None,
        model_catalog: CodexModelCatalog | None = None,
    ) -> None:
        self.database = database
        self.client = client
        self.relay = relay
        self.pairing = pairing
        self.project_service = project_service
        self.temporary_directory = temporary_directory
        self.max_image_bytes = max_image_bytes
        self.approval_resolver = approval_resolver
        self.model_catalog = model_catalog
        self._job_lock = asyncio.Lock()

    async def handle(self, event_id: str, message: IncomingMessage) -> None:
        authorized = await self.database.is_authorized_identity(
            connector_type="telegram",
            account_id=message.account_id,
            external_user_id=message.external_user_id,
        )
        if message.callback_data is not None:
            decision: Literal["accept", "decline"] | None = None
            if authorized and self.approval_resolver is not None:
                decision = await self.approval_resolver.resolve_callback(message.callback_data)
            if decision == "accept":
                callback_text = "已允许，本次审批已记录。"
            elif decision == "decline":
                callback_text = "已拒绝，本次审批已记录。"
            else:
                callback_text = "审批已失效或无权操作"
            if message.callback_query_id is not None:
                await self.client.answer_callback_query(message.callback_query_id, callback_text)
            if decision is not None:
                await self._reply(message, callback_text)
            return
        pair_match = PAIR_PATTERN.fullmatch(message.text.strip())
        if not authorized:
            if pair_match is None:
                await self._reply(message, "此Telegram账号尚未配对。请在Mac端生成一次性配对码。")
                return
            try:
                await self.pairing.pair(
                    code=pair_match.group(1),
                    external_user_id=message.external_user_id,
                    external_conversation_id=message.external_conversation_id,
                    display_name=message.sender_display_name,
                    account_id=message.account_id,
                )
            except PairingError as error:
                await self._reply(message, f"配对失败：{error}")
                return
            await self._reply(message, "配对成功。发送 /projects 查看可用项目。")
            return

        command, _, argument = message.text.strip().partition(" ")
        if command == "/help" or command == "/start":
            await self._reply(
                message,
                "/projects 查看项目\n/use <编号或名称> 切换项目\n/new 新建对话\n"
                "/models 查看模型\n/model <编号或名称> 选择模型\n"
                "/reasoning <强度> 设置推理强度\n/status 查看状态\n/stop 终止当前任务",
            )
            return
        if command == "/projects":
            projects = await self.project_service.list_projects()
            if not projects:
                await self._reply(message, "Mac端尚未添加授权项目。")
                return
            lines = [
                f"{'●' if project.is_current else '○'} {index}. {project.name}\n   {project.path}"
                for index, project in enumerate(projects, start=1)
            ]
            await self._reply(message, "可用项目：\n" + "\n".join(lines))
            return
        if command == "/use":
            selector = argument.strip()
            if not selector:
                await self._reply(message, "用法：/use <项目编号或名称>")
                return
            if self._job_lock.locked():
                await self._reply(
                    message,
                    "切换失败：任务运行期间不能切换项目，请等待完成或先使用 /stop。",
                )
                return
            async with self._job_lock:
                projects = await self.project_service.list_projects()
                if selector.isdigit() and 1 <= int(selector) <= len(projects):
                    selector = projects[int(selector) - 1].id
                try:
                    selected = await self.project_service.switch(selector)
                except (ValueError, RuntimeError) as error:
                    await self._reply(message, f"切换失败：{error}")
                    return
            await self._reply(
                message,
                f"已切换到：{selected.name}\n{selected.path}",
            )
            return
        if command == "/new":
            if await self.database.active_job_count():
                await self._reply(message, "任务运行期间不能新建对话。")
                return
            project = await self.database.current_project()
            if project is None:
                await self._reply(message, "当前没有可用项目。")
                return
            await self.database.start_new_conversation(project.id, project.name)
            await self._reply(message, f"已为 {project.name} 新建Codex对话。")
            return
        if command == "/models":
            state = await self._current_model_state()
            if state is None:
                await self._reply(message, "当前没有可用项目，或Codex模型清单尚未就绪。")
                return
            assert self.model_catalog is not None
            project, _conversation, model_option, effort = state
            lines = [
                f"{'●' if option.model == model_option.model else '○'} {index}. "
                f"{option.display_name} ({option.model})"
                for index, option in enumerate(self.model_catalog.models, start=1)
            ]
            await self._reply(
                message,
                f"{project.name} 当前会话的可用模型：\n"
                + "\n".join(lines)
                + f"\n当前推理强度：{reasoning_effort_label(effort)} ({effort})",
            )
            return
        if command == "/model":
            state = await self._current_model_state()
            if state is None:
                await self._reply(message, "当前没有可用项目，或Codex模型清单尚未就绪。")
                return
            assert self.model_catalog is not None
            project, _conversation, model_option, effort = state
            selector = argument.strip()
            if not selector:
                await self._reply(
                    message,
                    f"当前模型：{model_option.display_name} ({model_option.model})\n"
                    f"推理强度：{reasoning_effort_label(effort)} ({effort})\n"
                    "发送 /models 查看编号，使用 /model <编号或名称> 修改。",
                )
                return
            if self._job_lock.locked():
                await self._reply(message, "修改失败：任务运行期间不能修改模型或推理强度。")
                return
            try:
                option = self.model_catalog.resolve(selector)
            except ValueError:
                await self._reply(message, "未找到该模型。发送 /models 查看可用编号和名称。")
                return
            next_effort = effort if option.supports(effort) else option.default_reasoning_effort
            async with self._job_lock:
                try:
                    await self.database.set_active_conversation_model(
                        project.id,
                        model=option.model,
                        reasoning_effort=next_effort,
                        title=project.name,
                    )
                except RuntimeError as error:
                    await self._reply(message, f"修改失败：{error}")
                    return
            await self._reply(
                message,
                f"已为 {project.name} 当前会话选择：{option.display_name}\n"
                f"推理强度：{reasoning_effort_label(next_effort)} ({next_effort})\n"
                "既有上下文保持不变，从下一条任务开始生效。",
            )
            return
        if command in {"/reasoning", "/effort"}:
            state = await self._current_model_state()
            if state is None:
                await self._reply(message, "当前没有可用项目，或Codex模型清单尚未就绪。")
                return
            project, _conversation, model_option, effort = state
            requested = argument.strip().casefold()
            choices = ", ".join(
                f"{reasoning_effort_label(value)} ({value})"
                for value in model_option.supported_reasoning_efforts
            )
            if not requested:
                await self._reply(
                    message,
                    f"当前推理强度：{reasoning_effort_label(effort)} ({effort})\n"
                    f"{model_option.display_name} 支持：{choices}\n"
                    "使用 /reasoning <英文强度> 修改。",
                )
                return
            if not model_option.supports(requested):
                await self._reply(message, f"该模型不支持此强度。可选：{choices}")
                return
            if self._job_lock.locked():
                await self._reply(message, "修改失败：任务运行期间不能修改模型或推理强度。")
                return
            async with self._job_lock:
                try:
                    await self.database.set_active_conversation_model(
                        project.id,
                        model=model_option.model,
                        reasoning_effort=requested,
                        title=project.name,
                    )
                except RuntimeError as error:
                    await self._reply(message, f"修改失败：{error}")
                    return
            await self._reply(
                message,
                f"已将 {project.name} 当前会话的推理强度设为："
                f"{reasoning_effort_label(requested)} ({requested})\n"
                "既有上下文保持不变，从下一条任务开始生效。",
            )
            return
        if command == "/status":
            project = await self.database.current_project()
            active_jobs = await self.database.active_job_count()
            name = project.name if project is not None else "未选择"
            running_project = await self.database.active_job_project()
            running_name = running_project.name if running_project is not None else "无"
            model_status = "模型：尚未就绪"
            state = await self._current_model_state()
            if state is not None:
                _project, _conversation, model_option, effort = state
                model_status = (
                    f"模型：{model_option.display_name} ({model_option.model})\n"
                    f"推理强度：{reasoning_effort_label(effort)} ({effort})"
                )
            await self._reply(
                message,
                f"当前项目：{name}\n{model_status}\n运行中任务：{active_jobs}\n"
                f"任务所属项目：{running_name}",
            )
            return
        if command == "/stop":
            running_project = await self.database.active_job_project()
            stopped = await self.relay.interrupt_active()
            if stopped and running_project is not None:
                reply = f"已终止 {running_project.name} 的当前任务。"
            elif stopped:
                reply = "已终止当前任务。"
            else:
                reply = "当前没有运行中的任务。"
            await self._reply(message, reply)
            return

        async with self._job_lock:
            previous_status = await self.database.job_status_for_inbound_event(event_id)
            if previous_status is not None:
                if previous_status is JobStatus.COMPLETED:
                    await self.database.rebuild_missing_outbox(
                        connector_type="telegram",
                        account_id=message.account_id,
                        external_conversation_id=message.external_conversation_id,
                    )
                else:
                    await self._reply(
                        message,
                        "检测到此消息对应的旧任务已中断或失败。为避免重复执行副作用，"
                        "系统没有自动重放；如需重试，请重新发送该任务。",
                    )
                return
            project = await self.database.current_project()
            if project is None:
                await self._reply(message, "当前没有可用项目。请先在Mac端添加项目。")
                return
            images = await self._download_images(message)
            try:
                await self.relay.run_project(
                    project_id=project.id,
                    text=message.text,
                    image_paths=images,
                    inbound_event_id=event_id,
                    delivery=DeliveryTarget(
                        connector_type="telegram",
                        account_id=message.account_id,
                        external_conversation_id=message.external_conversation_id,
                    ),
                )
            finally:
                for image in images:
                    image.unlink(missing_ok=True)
                if images:
                    try:
                        images[0].parent.rmdir()
                    except OSError:
                        pass

    async def _download_images(self, message: IncomingMessage) -> tuple[Path, ...]:
        if not message.images:
            return ()
        directory = self.temporary_directory / str(uuid.uuid4())
        downloaded: list[Path] = []
        try:
            for index, image in enumerate(message.images):
                file_path = await self.client.get_file_path(image.external_id)
                destination = directory / f"image-{index}.jpg"
                await self.client.download_file(
                    file_path=file_path,
                    destination=destination,
                    max_bytes=self.max_image_bytes,
                )
                downloaded.append(destination)
        except Exception:
            for path in downloaded:
                path.unlink(missing_ok=True)
            try:
                directory.rmdir()
            except OSError:
                pass
            raise
        return tuple(downloaded)

    async def _current_model_state(
        self,
    ) -> tuple[Project, Conversation, CodexModelOption, str] | None:
        if self.model_catalog is None:
            return None
        project = await self.database.current_project()
        if project is None:
            return None
        conversation = await self.database.get_or_create_active_conversation(
            project.id, title=project.name
        )
        option, effort = self.model_catalog.effective(
            conversation.model, conversation.reasoning_effort
        )
        return project, conversation, option, effort

    async def _reply(self, message: IncomingMessage, text: str) -> None:
        await self.database.queue_text(
            connector_type="telegram",
            account_id=message.account_id,
            external_conversation_id=message.external_conversation_id,
            text=text,
        )
