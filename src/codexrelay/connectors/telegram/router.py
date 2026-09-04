from __future__ import annotations

import asyncio
import re
import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

from codexrelay.codex.base import CodexBackend
from codexrelay.codex.model_catalog import (
    CodexModelCatalog,
    CodexModelOption,
    reasoning_effort_label,
)
from codexrelay.connectors.base import IncomingMessage
from codexrelay.connectors.telegram.api import TelegramClient
from codexrelay.connectors.telegram.commands import (
    TELEGRAM_COMMAND_ALIASES,
    help_text,
    recognized_command_names,
)
from codexrelay.connectors.telegram.progress import TelegramProgress
from codexrelay.core import DeliveryTarget, RelayService
from codexrelay.database import Database
from codexrelay.models import Conversation, JobStatus, Project, ProjectApprovalMode
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
        codex_backend: CodexBackend | None = None,
        release_codex_connection: Callable[[], Awaitable[None]] | None = None,
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
        self.codex_backend = codex_backend
        self.release_codex_connection = release_codex_connection
        self._job_lock = asyncio.Lock()
        self._security_confirmations: dict[str, tuple[str, str, str, datetime]] = {}

    async def handle(self, event_id: str, message: IncomingMessage) -> None:
        authorized = await self.database.is_authorized_identity(
            connector_type="telegram",
            account_id=message.account_id,
            external_user_id=message.external_user_id,
        )
        if message.callback_data is not None:
            if authorized and message.callback_data.startswith("security:"):
                if message.callback_query_id is not None:
                    await self.client.answer_callback_query(message.callback_query_id, "已收到")
                await self._handle_security_callback(message)
                return
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
            self._security_confirmations.clear()
            await self._reply(message, "配对成功。发送 /projects 查看可用项目。")
            return

        command, argument = self._parse_command(message.text)
        if command.startswith("/") and command.removeprefix("/") not in recognized_command_names():
            await self._reply(
                message,
                f"未识别的命令：{command}\n发送 /help 查看可用命令；"
                "如需把它作为任务，请去掉开头的 /。",
            )
            return
        if command == "/help" or command == "/start":
            await self._reply(message, help_text())
            return
        if command == "/pair":
            await self._reply(
                message,
                "当前 Telegram 账号已经完成配对。"
                "如需更换账号，请在 Mac 端重新生成一次性配对码。",
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
                previous_project = await self.database.current_project()
                if previous_project is not None:
                    previous_session = await self.database.current_conversation(previous_project.id)
                    if previous_session is not None:
                        await self.database.release_conversation_lock(
                            previous_session.id, "telegram"
                        )
                try:
                    selected = await self.project_service.switch(selector)
                except (ValueError, RuntimeError) as error:
                    await self._reply(message, f"切换失败：{error}")
                    return
            await self._reply(
                message,
                f"已切换到：{selected.name}\n{selected.path}",
            )
            self._security_confirmations.clear()
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
        if command == "/sessions":
            project = await self.database.current_project()
            if project is None:
                await self._reply(message, "当前没有可用项目。")
                return
            discovery_note = ""
            if self.codex_backend is not None and not await self.database.active_job_count():
                try:
                    desktop_threads = await self.codex_backend.list_project_threads(project.path)
                    await self.database.archive_missing_codex_conversations(
                        project.id,
                        {thread.thread_id for thread in desktop_threads},
                    )
                    for thread in desktop_threads:
                        await self.database.register_external_conversation(
                            project.id,
                            codex_thread_id=thread.thread_id,
                            title=thread.title,
                            source=thread.source,
                        )
                    await self.database.select_first_available_conversation(project.id)
                except Exception:
                    # Discovery is best-effort; a temporary SDK/store failure
                    # must not hide persisted Telegram conversations.
                    discovery_note = "\n（电脑端会话暂时无法同步，请稍后重试。）"
            # A conversation without a Codex thread is only the local Telegram
            # placeholder created before its first task runs. It is not a
            # selectable Codex session and must not inflate the list shown to
            # the user. Keep the numbering identical between /sessions and
            # /session <number>.
            sessions = self._selectable_sessions(
                await self.database.list_conversations(project.id)
            )
            current = await self.database.current_conversation(project.id)
            if not sessions:
                await self._reply(message, "当前项目还没有会话。")
                return
            lines = []
            for index, session in enumerate(sessions, start=1):
                marker = "●" if current is not None and session.id == current.id else "○"
                source_label = {
                    "telegram": "Telegram",
                    "desktop": "电脑端会话",
                    "desktop_migrated": "电脑端相关会话",
                    "other": "其他连接器创建",
                }.get(session.source, "未知来源")
                current_label = " · 当前" if marker == "●" else ""
                lock_label = (
                    " · Telegram占用"
                    if session.lock_owner == "telegram"
                    else (f" · {session.lock_owner}占用" if session.lock_owner else "")
                )
                lines.append(
                    f"{marker} {index}. {session.title} · {source_label}"
                    f"{current_label}{lock_label}"
                )
            await self._reply(
                message,
                f"当前项目会话（{len(sessions)}）：\n" + "\n".join(lines)
                + "\n使用 /session <会话编号> 切换。"
                + "\n电脑端会话可直接继续；也可使用 /new 创建 Telegram 会话。"
                + discovery_note,
            )
            return
        if command == "/session":
            selector = argument.strip()
            project = await self.database.current_project()
            if project is None:
                await self._reply(message, "当前没有可用项目。")
                return
            if not selector.isdigit():
                await self._reply(message, "用法：/session <会话编号>，发送 /sessions 查看列表。")
                return
            if self._job_lock.locked() or await self.database.active_job_count():
                await self._reply(message, "任务运行期间不能切换会话，请等待完成或先使用 /stop。")
                return
            sessions = self._selectable_sessions(
                await self.database.list_conversations(project.id)
            )
            index = int(selector) - 1
            if index < 0 or index >= len(sessions):
                await self._reply(message, "未找到该会话编号，请发送 /sessions 查看列表。")
                return
            selected_id = sessions[index].id
            current_session = await self.database.current_conversation(project.id)
            if current_session is not None and current_session.id != selected_id:
                await self.database.release_conversation_lock(current_session.id, "telegram")
            selected_session = await self.database.select_conversation(selected_id, project.id)
            self._security_confirmations.clear()
            await self._reply(
                message,
                f"已切换会话：{selected_session.title}\n"
                f"来源：{self._conversation_source_label(selected_session.source)}",
            )
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
        if command == "/reasoning":
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
            conversation_status = "会话：未选择"
            if project is not None:
                conversation = await self.database.current_conversation(project.id)
                if conversation is not None:
                    lock = {
                        None: "空闲",
                        "telegram": "Telegram占用",
                        "desktop": "电脑端占用",
                    }.get(conversation.lock_owner, f"{conversation.lock_owner}占用")
                    source_label = self._conversation_source_label(conversation.source)
                    conversation_status = (
                        f"会话：{conversation.title}\n"
                        f"会话来源：{source_label}\n"
                        f"会话状态：{lock}"
                    )
            running_project = await self.database.active_job_project()
            running_name = running_project.name if running_project is not None else None
            approval_status = "审批模式：未选择项目"
            if project is not None:
                approval_mode = await self.database.project_approval_mode(
                    project.id,
                    connector_type=message.connector_type,
                    account_id=message.account_id,
                )
                approval_status = (
                    "审批模式：本项目内自动允许"
                    if approval_mode is ProjectApprovalMode.PROJECT_AUTO
                    else "审批模式：安全模式"
                )
            model_status = "模型：尚未就绪"
            state = await self._current_model_state()
            if state is not None:
                _project, _conversation, model_option, effort = state
                model_status = (
                    f"模型：{model_option.display_name} ({model_option.model})\n"
                    f"推理强度：{reasoning_effort_label(effort)} ({effort})"
                )
            status_lines = [
                f"当前项目：{name}",
                conversation_status,
                model_status,
                approval_status,
                f"运行中任务：{active_jobs}",
            ]
            if running_name is not None:
                status_lines.append(f"任务所属项目：{running_name}")
            await self._reply(message, "\n".join(status_lines))
            return
        if command == "/security":
            await self._handle_security_command(message)
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
        if command == "/release":
            project = await self.database.current_project()
            if project is None:
                await self._reply(message, "当前没有可用项目。")
                return
            conversation = await self.database.current_conversation(project.id)
            if conversation is None:
                await self._reply(message, "当前没有选中的会话。")
                return
            if await self.database.active_job_count():
                await self._reply(message, "当前会话仍有任务运行，请先使用 /stop。")
                return
            if self.release_codex_connection is not None:
                try:
                    # Closing and reopening the relay's App Server connection
                    # releases Codex's external writer lease. Releasing only
                    # our SQLite lease is not enough for desktop hand-off.
                    await self.release_codex_connection()
                except Exception as error:
                    await self._reply(message, f"清理异常状态失败：{error}")
                    return
            released = await self.database.release_conversation_lock(
                conversation.id, "telegram"
            )
            await self._reply(
                message,
                "已清理异常遗留状态。现在可以在电脑端继续使用。"
                if released
                else "当前没有 Telegram 遗留占用。电脑端可以直接继续使用。",
            )
            return
        if command == "/takeover":
            project = await self.database.current_project()
            if project is None:
                await self._reply(message, "当前没有可用项目。")
                return
            conversation = await self.database.current_conversation(project.id)
            if conversation is None:
                await self._reply(message, "当前没有选中的会话。")
                return
            await self._reply(
                message,
                f"Telegram 会话已就绪：{conversation.title}\n"
                "现在不需要手动接管；任务完成后，电脑端可直接继续。",
            )
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
            progress = TelegramProgress(self.client, message.external_conversation_id)
            await progress.start()
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
                    on_progress=progress.update if progress.active else None,
                )
            except PermissionError:
                await self._reply(
                    message,
                    "当前项目访问权限不可用。请在 Mac 端的‘系统设置 → 隐私与安全性’中允许 "
                    "CodexRelay 访问该目录后再试。",
                )
            except Exception as error:
                await self._reply(message, self._task_error_message(error))
            finally:
                await progress.finish()
                for image in images:
                    image.unlink(missing_ok=True)
                if images:
                    try:
                        images[0].parent.rmdir()
                    except OSError:
                        pass

    @staticmethod
    def _parse_command(text: str) -> tuple[str, str]:
        """Normalize a Telegram command while preserving its argument text."""
        parts = text.strip().split(maxsplit=1)
        if not parts:
            return "", ""
        command = parts[0].casefold()
        # Telegram may append @bot_username when a command is copied from a
        # group or another chat. Private-chat commands should behave identically.
        if command.startswith("/") and "@" in command:
            command = command.split("@", 1)[0]
        if command.startswith("/"):
            name = command.removeprefix("/")
            command = "/" + TELEGRAM_COMMAND_ALIASES.get(name, name)
        argument = parts[1].strip() if len(parts) == 2 else ""
        return command, argument

    @staticmethod
    def _selectable_sessions(sessions: list[Conversation]) -> list[Conversation]:
        """Return only real Codex threads, preserving visible selection order."""
        return [session for session in sessions if session.codex_thread_id is not None]

    @staticmethod
    def _conversation_source_label(source: str) -> str:
        return {
            "telegram": "Telegram",
            "desktop": "电脑上创建",
            "desktop_migrated": "电脑端相关会话",
            "other": "其他连接器创建",
        }.get(source, "未知来源")

    @staticmethod
    def _task_error_message(error: Exception) -> str:
        detail = str(error)
        lowered = detail.casefold()
        if "already has an active writer" in lowered:
            return (
                "任务未执行：这个会话当前由电脑端 Codex 持有。即使没有运行任务，"
                "桌面端打开该会话时也不能从 Telegram 写入。请完全退出电脑端 Codex 后重试，"
                "或使用 /new 创建 Telegram 独立会话。"
            )
        if "is archived" in lowered or "已归档" in lowered:
            return "任务未执行：这个会话已在 Codex 中归档，请先在电脑端恢复后再试。"
        if detail:
            return f"任务未执行：{detail[:500]}"
        return "任务未执行：Codex 返回了未知错误，请稍后重试。"

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

    async def _reply(
        self,
        message: IncomingMessage,
        text: str,
        *,
        reply_markup: dict[str, object] | None = None,
    ) -> None:
        await self.database.queue_text(
            connector_type="telegram",
            account_id=message.account_id,
            external_conversation_id=message.external_conversation_id,
            text=text,
            reply_markup=reply_markup,
        )

    async def _handle_security_command(self, message: IncomingMessage) -> None:
        project = await self.database.current_project()
        if project is None:
            await self._reply(message, "当前没有可用项目。请先在 Mac 端添加项目。")
            return
        mode = await self.database.project_approval_mode(
            project.id,
            connector_type=message.connector_type,
            account_id=message.account_id,
        )
        mode_label = (
            "本项目内自动允许"
            if mode is ProjectApprovalMode.PROJECT_AUTO
            else "安全模式"
        )
        await self._reply(
            message,
            f"当前项目：{project.name}\n当前审批模式：{mode_label}\n\n"
            "安全模式会在需要时通过 Telegram 逐项请求确认。\n"
            "本项目内自动允许只对当前项目目录生效，但会降低安全性。",
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "安全模式", "callback_data": "security:safe"},
                        {"text": "本项目内自动允许", "callback_data": "security:auto"},
                    ]
                ]
            },
        )

    async def _handle_security_callback(self, message: IncomingMessage) -> None:
        data = message.callback_data or ""
        if data == "security:safe":
            if self._job_lock.locked():
                await self._reply(message, "修改失败：任务运行期间不能修改审批模式。")
                return
            try:
                project = await self.database.set_current_project_approval_mode(
                    ProjectApprovalMode.SAFE,
                    connector_type="telegram",
                    account_id=message.account_id,
                    external_user_id=message.external_user_id,
                )
            except RuntimeError as error:
                await self._reply(message, f"修改失败：{error}")
                return
            await self._reply(message, f"已切换为“安全模式”。当前项目：{project.name}")
            return
        if data == "security:auto":
            token = secrets.token_urlsafe(12)
            self._security_confirmations[token] = (
                message.connector_type,
                message.account_id,
                message.external_user_id,
                datetime.now(UTC) + timedelta(minutes=5),
            )
            await self._reply(
                message,
                "开启后，Codex 将自动批准当前项目目录内的文件修改和命令执行，\n"
                "不再逐项请求确认。\n\n这会降低安全性，确定开启吗？",
                reply_markup={
                    "inline_keyboard": [[
                        {"text": "确认开启", "callback_data": f"security:confirm:{token}"},
                        {"text": "取消", "callback_data": "security:cancel"},
                    ]]
                },
            )
            return
        if data == "security:cancel":
            await self._reply(message, "已取消，审批模式保持不变。")
            return
        if data.startswith("security:confirm:"):
            token = data.removeprefix("security:confirm:")
            pending = self._security_confirmations.pop(token, None)
            if (
                pending is None
                or pending[0] != message.connector_type
                or pending[1] != message.account_id
                or pending[2] != message.external_user_id
                or pending[3] <= datetime.now(UTC)
            ):
                await self._reply(message, "确认已失效，请重新发送 /security。")
                return
            if self._job_lock.locked():
                await self._reply(message, "修改失败：任务运行期间不能修改审批模式。")
                return
            try:
                project = await self.database.set_current_project_approval_mode(
                    ProjectApprovalMode.PROJECT_AUTO,
                    connector_type="telegram",
                    account_id=message.account_id,
                    external_user_id=message.external_user_id,
                )
            except RuntimeError as error:
                await self._reply(message, f"修改失败：{error}")
                return
            await self._reply(
                message,
                f"已开启“本项目内自动允许”。\n仅对当前项目 {project.name} 生效。",
            )
