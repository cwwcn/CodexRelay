from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path


class SecretStoreError(RuntimeError):
    pass


class SecretStore:
    """Store connector secrets in CodexRelay's private app-data directory.

    Development builds are ad-hoc signed, so macOS Keychain treats every rebuilt
    app as a new client and repeatedly asks for permission. A mode-0600 file keeps
    the token outside project/config/log files while avoiding that recurring prompt.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (
            Path.home()
            / "Library"
            / "Application Support"
            / "CodexRelay"
            / "telegram-tokens.json"
        )

    async def get_telegram_token(self, account_id: str = "main-bot") -> str | None:
        try:
            return await asyncio.to_thread(self._read_token, account_id)
        except OSError as error:
            raise SecretStoreError("could not read the Telegram token file") from error

    async def set_telegram_token(self, token: str, account_id: str = "main-bot") -> None:
        normalized = token.strip()
        if not normalized or ":" not in normalized:
            raise ValueError("Telegram bot token has an invalid shape")
        try:
            await asyncio.to_thread(self._write_token, account_id, normalized)
        except OSError as error:
            raise SecretStoreError("could not save the Telegram token file") from error

    async def delete_telegram_token(self, account_id: str = "main-bot") -> None:
        try:
            await asyncio.to_thread(self._delete_token, account_id)
        except OSError as error:
            raise SecretStoreError("could not delete the Telegram token file") from error

    def _read_token(self, account_id: str) -> str | None:
        try:
            with self.path.open("r", encoding="utf-8") as source:
                payload = json.load(source)
        except FileNotFoundError:
            return None
        if not isinstance(payload, dict):
            raise SecretStoreError("Telegram token file has an invalid format")
        token = payload.get(account_id)
        return token if isinstance(token, str) and token else None

    def _write_token(self, account_id: str, token: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload: dict[str, str] = {}
        try:
            with self.path.open("r", encoding="utf-8") as source:
                existing = json.load(source)
            if isinstance(existing, dict):
                payload = {
                    str(key): str(value)
                    for key, value in existing.items()
                    if isinstance(key, str) and isinstance(value, str)
                }
        except FileNotFoundError:
            pass
        payload[account_id] = token
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)

    def _delete_token(self, account_id: str) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as source:
                payload = json.load(source)
        except FileNotFoundError:
            return
        if not isinstance(payload, dict):
            return
        payload.pop(account_id, None)
        if payload:
            temporary = self.path.with_name(f".{self.path.name}.tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        else:
            self.path.unlink(missing_ok=True)
