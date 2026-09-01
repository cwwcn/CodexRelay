from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from codexrelay.secrets import SecretStore


def test_secret_store_round_trip_uses_private_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "telegram-tokens.json"
    store = SecretStore(path)

    asyncio.run(store.set_telegram_token("123456:token", "main-bot"))

    assert asyncio.run(store.get_telegram_token("main-bot")) == "123456:token"
    assert path.stat().st_mode & 0o777 == 0o600
    assert "123456:token" in path.read_text(encoding="utf-8")


def test_secret_store_supports_multiple_accounts_and_delete(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "telegram-tokens.json")

    asyncio.run(store.set_telegram_token("1:first", "first"))
    asyncio.run(store.set_telegram_token("2:second", "second"))
    asyncio.run(store.delete_telegram_token("first"))

    assert asyncio.run(store.get_telegram_token("first")) is None
    assert asyncio.run(store.get_telegram_token("second")) == "2:second"


def test_secret_store_rejects_invalid_token(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "telegram-tokens.json")

    with pytest.raises(ValueError):
        asyncio.run(store.set_telegram_token("invalid", "main-bot"))
