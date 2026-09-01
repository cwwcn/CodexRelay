from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from openai_codex.models import JsonObject

from codexrelay.database import Database

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
