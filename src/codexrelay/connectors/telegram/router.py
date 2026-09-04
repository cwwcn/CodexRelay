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
from codexrelay.session_sync import SessionSynchronizer

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
                await self._reply(message, "Mac 端‘系统’页尚未添加授权项目。")
                return
            lines = [
                f"{'●' if project.is_current else '○'} {index}. {project.name}\n   {project.path}"
                for index, project in enumerate(projects, start=1)
            ]
            await self._reply(
                message,
                "已登记项目（项目是会话的可选归属）：\n"
                + "\n".join(lines)
                + "\n\n项目管理请在 Mac 端‘系统’页完成；切换会话请使用 /sessions 和 /session。",
            )
            return
        if command == "/use":
            selector = argument.strip()
            if not selector:
                await self._reply(
                    message,
                    "用法：/use <项目编号或名称>（兼容命令；推荐使用 /session <会话编号>）",
                )
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
                previous_session = await self.database.current_global_conversation()
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
                f"已切换兼容项目上下文：{selected.name}\n{selected.path}\n"
                "项目只是会话的可选归属；如需精确选择上下文，请发送 /sessions。",
            )
            self._security_confirmations.clear()
            return
        if command == "/new":
            if await self.database.active_job_count():
                await self._reply(message, "任务运行期间不能新建对话。")
                return
            current = await self.database.current_global_conversation()
            if current is None:
                project = await self.database.current_project()
                if project is None:
                    await self._reply(
                        message,
                        "当前没有会话工作目录，请先在 Mac 端选择或创建一个会话。",
                    )
                    return
                current = await self.database.get_or_create_active_conversation(
                    project.id, title=project.name
                )
            if current.cwd is None:
                await self._reply(message, "当前会话没有可用工作目录，无法新建会话。")
                return
            if current.project_id is not None:
                created = await self.database.start_new_conversation(
                    current.project_id, current.title
                )
            else:
                created = await self.database.create_standalone_conversation(
                    current.cwd, title="临时会话"
                )
            await self._reply(message, f"已新建会话：{created.title}\n工作目录：{created.cwd}")
            return
        if command == "/sessions":
            if argument and argument.casefold() != "all":
                await self._reply(message, "用法：/sessions（查看全部会话）")
                return
            await self._handle_all_sessions(message)
            return
        if command == "/session":
            selector = argument.strip()
            if not selector.isdigit():
                await self._reply(message, "用法：/session <会话编号>，发送 /sessions 查看列表。")
                return
            if self._job_lock.locked() or await self.database.active_job_count():
                await self._reply(message, "任务运行期间不能切换会话，请等待完成或先使用 /stop。")
                return
            if self.codex_backend is not None:
                try:
                    await SessionSynchronizer(self.database, self.codex_backend).sync_all()
                except Exception:
                    pass
            sessions = await self.database.list_global_sessions()
            index = int(selector) - 1
            if index < 0 or index >= len(sessions):
                await self._reply(message, "未找到该会话编号，请发送 /sessions 查看最新列表。")
                return
            selected_global = sessions[index]
            try:
                selected_session = await self.database.activate_global_session(
                    selected_global.thread_id
                )
            except RuntimeError as error:
                await self._reply(message, f"切换失败：{error}")
                return
            self._security_confirmations.clear()
            await self._reply(
                message,
                f"已切换会话：{selected_session.title}\n"
                f"归属：{selected_global.project_name or '无项目'}\n"
                f"工作目录：{selected_session.cwd or '不可用'}",
            )
            return
        if command == "/models":
            state = await self._current_model_state()
            if state is None:
                await self._reply(message, "当前没有可用会话，或Codex模型清单尚未就绪。")
                return
            assert self.model_catalog is not None
            _project, conversation, model_option, effort = state
            lines = [
                f"{'●' if option.model == model_option.model else '○'} {index}. "
                f"{option.display_name} ({option.model})"
                for index, option in enumerate(self.model_catalog.models, start=1)
            ]
            await self._reply(
                message,
                f"当前会话：{conversation.title}\n"
                f"可用模型：\n"
                + "\n".join(lines)
                + f"\n当前推理强度：{reasoning_effort_label(effort)} ({effort})",
            )
            return
        if command == "/model":
            state = await self._current_model_state()
            if state is None:
                await self._reply(message, "当前没有可用会话，或Codex模型清单尚未就绪。")
                return
            assert self.model_catalog is not None
            _project, conversation, model_option, effort = state
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
                    await self.database.set_conversation_model(
                        conversation.id,
                        model=option.model,
                        reasoning_effort=next_effort,
                    )
                except RuntimeError as error:
                    await self._reply(message, f"修改失败：{error}")
                    return
            await self._reply(
                message,
                f"已为当前会话选择：{option.display_name}\n"
                f"推理强度：{reasoning_effort_label(next_effort)} ({next_effort})\n"
                "既有上下文保持不变，从下一条任务开始生效。",
            )
            return
        if command == "/reasoning":
            state = await self._current_model_state()
            if state is None:
                await self._reply(message, "当前没有可用会话，或Codex模型清单尚未就绪。")
                return
            _project, conversation, model_option, effort = state
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
                    await self.database.set_conversation_model(
                        conversation.id,
                        model=model_option.model,
                        reasoning_effort=requested,
                    )
                except RuntimeError as error:
                    await self._reply(message, f"修改失败：{error}")
                    return
            await self._reply(
                message,
                "已将当前会话的推理强度设为："
                f"{reasoning_effort_label(requested)} ({requested})\n"
                "既有上下文保持不变，从下一条任务开始生效。",
            )
            return
        if command == "/status":
            active_jobs = await self.database.active_job_count()
            status_conversation = await self.database.current_global_conversation()
            project = (
                await self.database.get_project(status_conversation.project_id)
                if status_conversation is not None and status_conversation.project_id is not None
                else None
            )
            name = project.name if project is not None else "无项目会话"
            conversation_status = "会话：未选择"
            if status_conversation is not None:
                lock = {
                    None: "空闲",
                    "telegram": "Telegram占用",
                    "desktop": "电脑端占用",
                }.get(
                    status_conversation.lock_owner,
                    f"{status_conversation.lock_owner}占用",
                )
                source_label = self._conversation_source_label(status_conversation.source)
                conversation_status = (
                    f"会话：{status_conversation.title}\n"
                    f"会话来源：{source_label}\n"
                    f"会话状态：{lock}"
                )
            running_project = await self.database.active_job_project()
            running_name = running_project.name if running_project is not None else None
            approval_status = "审批模式：受控模式（无项目会话）"
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
                f"会话归属：{name}"
                + (f"\n当前项目：{name}" if project is not None else ""),
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
            release_conversation = await self.database.current_global_conversation()
            if release_conversation is None:
                await self._reply(message, "当前没有选中的会话。请先发送 /sessions。")
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
                release_conversation.id, "telegram"
            )
            await self._reply(
                message,
                "已清理异常遗留状态。现在可以在电脑端继续使用。"
                if released
                else "当前没有 Telegram 遗留占用。电脑端可以直接继续使用。",
            )
            return
        if command == "/takeover":
            takeover_conversation = await self.database.current_global_conversation()
            if takeover_conversation is None:
                await self._reply(message, "当前没有选中的会话。请先发送 /sessions。")
                return
            await self._reply(
                message,
                f"Telegram 会话已就绪：{takeover_conversation.title}\n"
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
            active_conversation = await self.database.current_global_conversation()
            if active_conversation is None:
                fallback_project = await self.database.current_project()
                if fallback_project is None:
                    await self._reply(
                        message,
                        "当前没有选中的会话。请先发送 /sessions 选择会话。",
                    )
                    return
                active_conversation = await self.database.get_or_create_active_conversation(
                    fallback_project.id, title=fallback_project.name
                )
            images = await self._download_images(message)
            progress = TelegramProgress(self.client, message.external_conversation_id)
            await progress.start()
            try:
                delivery = DeliveryTarget(
                    connector_type="telegram",
                    account_id=message.account_id,
                    external_conversation_id=message.external_conversation_id,
                )
                run_current = getattr(self.relay, "run_current_conversation", None)
                if callable(run_current):
                    await run_current(
                        text=message.text,
                        image_paths=images,
                        inbound_event_id=event_id,
                        delivery=delivery,
                        on_progress=progress.update if progress.active else None,
                    )
                elif active_conversation.project_id is not None:
                    # Compatibility for lightweight test/integration relays
                    # that still expose only the original project entrypoint.
                    await self.relay.run_project(
                        project_id=active_conversation.project_id,
                        text=message.text,
                        image_paths=images,
                        inbound_event_id=event_id,
                        delivery=delivery,
                        on_progress=progress.update if progress.active else None,
                    )
                else:
                    raise RuntimeError("当前会话执行器尚未支持无项目会话")
            except PermissionError:
                await self._reply(
                    message,
                    "当前会话工作目录访问权限不可用。请在 Mac 端的‘系统设置 → 隐私与安全性’中允许 "
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

    async def _handle_all_sessions(self, message: IncomingMessage) -> None:
        discovery_note = ""
        if self.codex_backend is not None and not await self.database.active_job_count():
            try:
                await SessionSynchronizer(self.database, self.codex_backend).sync_all()
            except Exception:
                discovery_note = "\n（Codex 会话暂时无法同步，以下为上次成功结果。）"
        sessions = await self.database.list_global_sessions()
        if not sessions:
            await self._reply(
                message,
                "暂未发现可用的 Codex 会话。" + discovery_note,
            )
            return
        project_count = len({item.project_id for item in sessions if item.project_id})
        unassigned_count = sum(item.is_unassigned for item in sessions)
        lines = [
            f"全部会话（{len(sessions)}） · {project_count} 个项目 · "
            f"{unassigned_count} 个未归属"
        ]
        current_group: str | None = None
        for index, session in enumerate(sessions, start=1):
            if session.project_name is None:
                group = "未归属"
            elif session.project_enabled:
                group = session.project_name
            else:
                group = f"{session.project_name}（项目已停用）"
            if group != current_group:
                lines.append(f"\n{group}")
                current_group = group
            marker = "●" if session.is_current_conversation else "○"
            path_note = "" if session.path_available else " · 路径不可用"
            source_note = f" · {self._conversation_source_label(session.source)}"
            lines.append(f"{marker} {index}. {session.title}{source_note}{path_note}")
        lines.append("\n使用 /session <编号> 切换任意会话；项目只是会话的可选归属。")
        if unassigned_count:
            lines.append("未归属会话不会自动获得项目权限，可在 Mac 端明确归属。")
        await self._reply(message, "\n".join(lines) + discovery_note)

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
    ) -> tuple[Project | None, Conversation, CodexModelOption, str] | None:
        if self.model_catalog is None:
            return None
        conversation = await self.database.current_global_conversation()
        if conversation is None:
            project = await self.database.current_project()
            if project is None:
                return None
            conversation = await self.database.get_or_create_active_conversation(
                project.id, title=project.name
            )
        project = (
            await self.database.get_project(conversation.project_id)
            if conversation.project_id is not None
            else None
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
        conversation = await self.database.current_global_conversation()
        if conversation is None:
            fallback_project = await self.database.current_project()
            if fallback_project is None:
                await self._reply(message, "当前没有选中的会话。请先发送 /sessions。")
                return
            conversation = await self.database.get_or_create_active_conversation(
                fallback_project.id, title=fallback_project.name
            )
        project = (
            await self.database.get_project(conversation.project_id)
            if conversation.project_id is not None
            else None
        )
        if project is None:
            await self._reply(
                message,
                f"当前会话：{conversation.title}\n"
                "安全模式：受控模式（无项目会话）\n\n"
                "普通工作可以直接执行；涉及工作目录外文件、网络、提权或敏感数据时，"
                "会通过 Telegram 单独请求审批。无项目会话不支持“项目内自动允许”。",
            )
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
