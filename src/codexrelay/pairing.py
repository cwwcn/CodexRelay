from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from codexrelay.database import Database, utc_now


class PairingError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PairingCode:
    code: str
    expires_at: str


class PairingService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def generate(
        self,
        *,
        connector_type: str = "telegram",
        account_id: str = "main-bot",
        lifetime: timedelta = timedelta(minutes=10),
    ) -> PairingCode:
        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_hex(16)
        digest = self._digest(salt, code)
        now = datetime.now(UTC)
        expires_at = (now + lifetime).isoformat(timespec="milliseconds")
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                UPDATE pairing_codes SET consumed_at=?
                WHERE connector_type=? AND account_id=? AND consumed_at IS NULL
                """,
                (utc_now(), connector_type, account_id),
            )
            await connection.execute(
                """
                INSERT INTO pairing_codes(
                    id, connector_type, account_id, code_salt, code_hash,
                    expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    connector_type,
                    account_id,
                    salt,
                    digest,
                    expires_at,
                    now.isoformat(timespec="milliseconds"),
                ),
            )
        return PairingCode(code=code, expires_at=expires_at)

    async def pair(
        self,
        *,
        code: str,
        external_user_id: str,
        external_conversation_id: str,
        display_name: str,
        connector_type: str = "telegram",
        account_id: str = "main-bot",
    ) -> str:
        normalized = code.strip()
        if len(normalized) != 6 or not normalized.isdigit():
            raise PairingError("invalid pairing code")
        now = datetime.now(UTC)
        failure: str | None = None
        local_user_id: str | None = None
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """
                SELECT id, code_salt, code_hash, attempt_count, max_attempts, expires_at
                FROM pairing_codes
                WHERE connector_type=? AND account_id=? AND consumed_at IS NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (connector_type, account_id),
            )
            challenge = await cursor.fetchone()
            if challenge is None:
                raise PairingError("no active pairing code")
            expires_at = datetime.fromisoformat(str(challenge["expires_at"]))
            if expires_at <= now:
                await connection.execute(
                    "UPDATE pairing_codes SET consumed_at=? WHERE id=?",
                    (now.isoformat(timespec="milliseconds"), challenge["id"]),
                )
                failure = "pairing code expired"
            else:
                attempts = int(challenge["attempt_count"])
                maximum = int(challenge["max_attempts"])
                if attempts >= maximum:
                    failure = "pairing code locked"
                else:
                    supplied = self._digest(str(challenge["code_salt"]), normalized)
                    if not hmac.compare_digest(supplied, str(challenge["code_hash"])):
                        await connection.execute(
                            "UPDATE pairing_codes SET attempt_count=attempt_count+1 WHERE id=?",
                            (challenge["id"],),
                        )
                        failure = "invalid pairing code"

            if failure is None:
                local_user_id = str(uuid.uuid4())
                identity_id = str(uuid.uuid4())
                paired_at = now.isoformat(timespec="milliseconds")
                await connection.execute(
                    """
                    INSERT INTO local_users(id, display_name, enabled, created_at)
                    VALUES (?, ?, 1, ?)
                    """,
                    (local_user_id, display_name.strip() or "Telegram user", paired_at),
                )
                await connection.execute(
                    """
                    UPDATE external_identities SET enabled=0
                    WHERE connector_type=? AND account_id=? AND enabled=1
                    """,
                    (connector_type, account_id),
                )
                await connection.execute(
                    """
                    INSERT INTO external_identities(
                        id, local_user_id, connector_type, account_id,
                        external_user_id, external_conversation_id, paired_at, enabled
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(connector_type, account_id, external_user_id) DO UPDATE SET
                        local_user_id=excluded.local_user_id,
                        external_conversation_id=excluded.external_conversation_id,
                        paired_at=excluded.paired_at,
                        enabled=1
                    """,
                    (
                        identity_id,
                        local_user_id,
                        connector_type,
                        account_id,
                        external_user_id,
                        external_conversation_id,
                        paired_at,
                    ),
                )
                await connection.execute(
                    "UPDATE pairing_codes SET consumed_at=? WHERE id=?",
                    (paired_at, challenge["id"]),
                )
                await self.database.reset_project_approval_policies(connection)
        if failure is not None:
            raise PairingError(failure)
        if local_user_id is None:
            raise RuntimeError("pairing completed without a local user")
        return local_user_id

    @staticmethod
    def _digest(salt: str, code: str) -> str:
        return hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()
