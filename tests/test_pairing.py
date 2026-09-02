from datetime import timedelta
from pathlib import Path

import pytest

from codexrelay.database import Database
from codexrelay.pairing import PairingError, PairingService


@pytest.mark.asyncio
async def test_pairing_code_is_single_use_and_authorizes_identity(tmp_path: Path) -> None:
    async with Database(tmp_path / "state.db") as database:
        service = PairingService(database)
        assert not await database.has_enabled_identity(
            connector_type="telegram", account_id="main-bot"
        )
        challenge = await service.generate()
        user_id = await service.pair(
            code=challenge.code,
            external_user_id="123",
            external_conversation_id="123",
            display_name="Owner",
        )

        assert user_id
        assert await database.is_authorized_identity(
            connector_type="telegram", account_id="main-bot", external_user_id="123"
        )
        assert await database.has_enabled_identity(
            connector_type="telegram", account_id="main-bot"
        )
        with pytest.raises(PairingError, match="no active"):
            await service.pair(
                code=challenge.code,
                external_user_id="456",
                external_conversation_id="456",
                display_name="Attacker",
            )


@pytest.mark.asyncio
async def test_expired_pairing_code_is_rejected(tmp_path: Path) -> None:
    async with Database(tmp_path / "state.db") as database:
        service = PairingService(database)
        challenge = await service.generate(lifetime=timedelta(seconds=-1))

        with pytest.raises(PairingError, match="expired"):
            await service.pair(
                code=challenge.code,
                external_user_id="123",
                external_conversation_id="123",
                display_name="Owner",
            )

        cursor = await database.connection.execute(
            "SELECT consumed_at FROM pairing_codes ORDER BY created_at DESC LIMIT 1"
        )
        assert (await cursor.fetchone())["consumed_at"] is not None


@pytest.mark.asyncio
async def test_failed_pairing_attempt_is_committed(tmp_path: Path) -> None:
    async with Database(tmp_path / "state.db") as database:
        service = PairingService(database)
        await service.generate()

        with pytest.raises(PairingError, match="invalid"):
            await service.pair(
                code="000000",
                external_user_id="123",
                external_conversation_id="123",
                display_name="Owner",
            )

        cursor = await database.connection.execute(
            "SELECT attempt_count FROM pairing_codes ORDER BY created_at DESC LIMIT 1"
        )
        assert (await cursor.fetchone())["attempt_count"] == 1
