from __future__ import annotations

import asyncio

import keyring
from keyring.errors import KeyringError


class SecretStoreError(RuntimeError):
    pass


class SecretStore:
    TELEGRAM_SERVICE = "com.cwwen.codexrelay.connector.telegram"

    async def get_telegram_token(self, account_id: str = "main-bot") -> str | None:
        try:
            return await asyncio.to_thread(keyring.get_password, self.TELEGRAM_SERVICE, account_id)
        except KeyringError as error:
            raise SecretStoreError("could not read the Telegram token from Keychain") from error

    async def set_telegram_token(self, token: str, account_id: str = "main-bot") -> None:
        normalized = token.strip()
        if not normalized or ":" not in normalized:
            raise ValueError("Telegram bot token has an invalid shape")
        try:
            await asyncio.to_thread(
                keyring.set_password, self.TELEGRAM_SERVICE, account_id, normalized
            )
        except KeyringError as error:
            raise SecretStoreError("could not save the Telegram token to Keychain") from error

    async def delete_telegram_token(self, account_id: str = "main-bot") -> None:
        try:
            await asyncio.to_thread(keyring.delete_password, self.TELEGRAM_SERVICE, account_id)
        except keyring.errors.PasswordDeleteError:
            return
        except KeyringError as error:
            raise SecretStoreError("could not delete the Telegram token from Keychain") from error
