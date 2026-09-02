from __future__ import annotations

import pytest

from codexrelay.connectors.telegram.progress import TelegramProgress


class FakeProgressClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.edited: list[tuple[str, str, str]] = []
        self.deleted: list[tuple[str, str]] = []

    async def send_text(
        self, chat_id: str, text: str, *, reply_markup: dict[str, object] | None = None
    ) -> list[str]:
        assert reply_markup is None
        self.sent.append((chat_id, text))
        return ["17"]

    async def edit_text(self, chat_id: str, message_id: str, text: str) -> None:
        self.edited.append((chat_id, message_id, text))

    async def delete_message(self, chat_id: str, message_id: str) -> None:
        self.deleted.append((chat_id, message_id))


@pytest.mark.asyncio
async def test_progress_message_is_updated_and_removed() -> None:
    client = FakeProgressClient()
    progress = TelegramProgress(client, "42")

    assert await progress.start()
    progress._last_edit_at = 0.0
    await progress.update("正在执行本地操作…")
    await progress.finish()

    assert client.sent == [("42", "⏳ 正在处理你的请求…\n\n阶段：正在分析请求…")]
    assert client.edited == [("42", "17", "⏳ 正在处理你的请求…\n\n阶段：正在执行本地操作…")]
    assert client.deleted == [("42", "17")]
    assert not progress.active


@pytest.mark.asyncio
async def test_progress_is_optional_for_test_or_non_message_clients() -> None:
    progress = TelegramProgress(object(), "42")

    assert not await progress.start()
    await progress.update("正在处理…")
    await progress.finish()
