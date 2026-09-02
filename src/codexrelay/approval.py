from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from openai_codex.models import JsonObject

from codexrelay.database import Database
from codexrelay.models import Project, ProjectApprovalMode

COMMAND_APPROVAL = "item/commandExecution/requestApproval"
FILE_APPROVAL = "item/fileChange/requestApproval"
PERMISSIONS_APPROVAL = "item/permissions/requestApproval"
SUPPORTED_APPROVALS = frozenset({COMMAND_APPROVAL, FILE_APPROVAL, PERMISSIONS_APPROVAL})


class ApprovalCoordinator:
    def __init__(
        self,
        *,
        database: Database,
        loop: asyncio.AbstractEventLoop,
        account_id: str = "main-bot",
        timeout_seconds: int = 300,
    ) -> None:
        self.database = database
        self.loop = loop
        self.account_id = account_id
        self.timeout_seconds = timeout_seconds
        self._pending: dict[str, asyncio.Future[str]] = {}

    def handle_sync(self, method: str, params: JsonObject | None) -> JsonObject:
        if method not in SUPPORTED_APPROVALS:
            return {}
        request = asyncio.run_coroutine_threadsafe(self.request(method, params or {}), self.loop)
        try:
            decision = request.result(timeout=self.timeout_seconds + 15)
        except Exception:
            request.cancel()
            decision = "decline"
        return self._response(method, decision, params or {})

    async def request(self, method: str, params: JsonObject) -> str:
        turn_id = params.get("turnId")
        if not isinstance(turn_id, str):
            return "decline"
        job_id = await self.database.job_id_for_turn(turn_id)
        chat_id = await self.database.authorized_conversation_id(
            connector_type="telegram", account_id=self.account_id
        )
        if job_id is None or chat_id is None:
            return "decline"
        project = await self.database.project_for_turn(turn_id)
        if (
            project is not None
            and await self.database.project_approval_mode(project.id, account_id=self.account_id)
            is ProjectApprovalMode.PROJECT_AUTO
        ):
            if self._auto_allows(method, params, project):
                return "accept"

        nonce = secrets.token_urlsafe(18)
        nonce_hash = self._hash_nonce(nonce)
        expires_at = (datetime.now(UTC) + timedelta(seconds=self.timeout_seconds)).isoformat(
            timespec="milliseconds"
        )
        approval_id = str(uuid.uuid4())
        summary = self._summary(method, params)
        await self.database.create_approval_request(
            approval_id=approval_id,
            job_id=job_id,
            rpc_request_id=str(params.get("itemId", approval_id)),
            nonce_hash=nonce_hash,
            approval_type=method,
            summary=summary,
            expires_at=expires_at,
        )
        future = self.loop.create_future()
        self._pending[nonce_hash] = future
        await self.database.queue_text(
            connector_type="telegram",
            account_id=self.account_id,
            external_conversation_id=chat_id,
            text=self._format_message(summary),
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "允许一次", "callback_data": f"approve:{nonce}"},
                        {"text": "拒绝", "callback_data": f"deny:{nonce}"},
                    ]
                ]
            },
        )
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=self.timeout_seconds)
        except TimeoutError:
            await self.database.expire_approval(nonce_hash)
            return "decline"
        finally:
            self._pending.pop(nonce_hash, None)

    @staticmethod
    def _auto_allows(method: str, params: JsonObject, project: Project) -> bool:
        """Allow only project-scoped operations; fail closed for extra permissions."""
        root = project.path.resolve()

        def within(value: str) -> bool:
            try:
                Path(value).expanduser().resolve().relative_to(root)
            except (OSError, ValueError):
                return False
            return True

        if method == COMMAND_APPROVAL:
            cwd = params.get("cwd")
            if not isinstance(cwd, str) or not within(cwd):
                return False
            if params.get("networkApprovalContext") is not None:
                return False
            if params.get("proposedNetworkPolicyAmendments") is not None:
                return False
            actions = params.get("commandActions")
            if isinstance(actions, list):
                for action in actions:
                    if not isinstance(action, dict):
                        return False
                    path = action.get("path")
                    if path is not None and (not isinstance(path, str) or not within(path)):
                        return False
            return True
        if method == FILE_APPROVAL:
            grant_root = params.get("grantRoot")
            if grant_root is None:
                return True
            if not isinstance(grant_root, str):
                return False
            return within(grant_root)
        if method == PERMISSIONS_APPROVAL:
            permissions = params.get("permissions")
            if not isinstance(permissions, dict):
                return False
            network = permissions.get("network")
            if isinstance(network, dict) and network.get("enabled") is True:
                return False
            filesystem = permissions.get("fileSystem")
            if not isinstance(filesystem, dict):
                return True
            for key in ("read", "write"):
                paths = filesystem.get(key)
                if not isinstance(paths, list):
                    continue
                for value in paths:
                    if not isinstance(value, str) or not within(value):
                        return False
            entries = filesystem.get("entries")
            if entries is not None:
                if not isinstance(entries, list):
                    return False
                for entry in entries:
                    if not isinstance(entry, dict):
                        return False
                    path = entry.get("path")
                    if not isinstance(path, dict) or path.get("type") != "path":
                        return False
                    value = path.get("path")
                    if not isinstance(value, str) or not within(value):
                        return False
            return True
        return False

    async def resolve_callback(
        self, callback_data: str
    ) -> Literal["accept", "decline"] | None:
        action, separator, nonce = callback_data.partition(":")
        if separator != ":" or action not in {"approve", "deny"} or not nonce:
            return None
        nonce_hash = self._hash_nonce(nonce)
        decision: Literal["accept", "decline"] = (
            "accept" if action == "approve" else "decline"
        )
        resolved, _job_id = await self.database.resolve_approval(nonce_hash, decision)
        if not resolved:
            return None
        future = self._pending.get(nonce_hash)
        if future is not None and not future.done():
            future.set_result(decision)
        return decision

    @staticmethod
    def _response(method: str, decision: str, params: JsonObject) -> JsonObject:
        if method in {COMMAND_APPROVAL, FILE_APPROVAL}:
            return {"decision": decision}
        if method == PERMISSIONS_APPROVAL and decision == "accept":
            permissions = params.get("permissions")
            return {
                "permissions": permissions if isinstance(permissions, dict) else {},
                "scope": "turn",
            }
        if method == PERMISSIONS_APPROVAL:
            return {"permissions": {}, "scope": "turn"}
        return {}

    @staticmethod
    def _summary(method: str, params: JsonObject) -> dict[str, object]:
        kind = {
            COMMAND_APPROVAL: "命令执行",
            FILE_APPROVAL: "文件修改",
            PERMISSIONS_APPROVAL: "额外权限",
        }.get(method, "未知操作")
        summary: dict[str, object] = {"kind": kind}
        for key in ("command", "cwd", "reason", "grantRoot", "permissions"):
            value = params.get(key)
            if value is not None:
                summary[key] = value
        serialized = json.dumps(summary, ensure_ascii=False)
        if len(serialized) > 3500:
            summary = {"kind": kind, "detail": serialized[:3500] + "…"}
        return summary

    @staticmethod
    def _format_message(summary: dict[str, object]) -> str:
        lines = [f"Codex请求：{summary.get('kind', '操作')}"]
        for key, label in (
            ("command", "命令"),
            ("cwd", "目录"),
            ("reason", "原因"),
            ("grantRoot", "额外目录"),
            ("permissions", "权限"),
            ("detail", "详情"),
        ):
            if key in summary:
                value = summary[key]
                rendered = (
                    json.dumps(value, ensure_ascii=False)
                    if isinstance(value, (dict, list))
                    else str(value)
                )
                lines.append(f"{label}：{rendered}")
        lines.append("请选择一次性决定；5分钟后自动拒绝。")
        return "\n".join(lines)

    @staticmethod
    def _hash_nonce(nonce: str) -> str:
        return hashlib.sha256(nonce.encode()).hexdigest()
