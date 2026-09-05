import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from codexrelay.connectors.telegram.api import (
    TelegramAPIError,
    TelegramClient,
    parse_incoming_message,
    split_text,
)
from codexrelay.connectors.telegram.commands import (
    TELEGRAM_PRIVATE_COMMAND_SCOPE,
    bot_api_commands,
    help_text,
    recognized_command_names,
)


def test_parse_private_text_and_largest_photo() -> None:
    message = parse_incoming_message(
        {
            "update_id": 99,
            "message": {
                "from": {"id": 123},
                "chat": {"id": 123, "type": "private"},
                "date": 1788570000,
                "caption": "Please inspect this screenshot",
                "photo": [
                    {"file_id": "small", "file_unique_id": "a", "width": 90, "height": 90},
                    {
                        "file_id": "large",
                        "file_unique_id": "b",
                        "width": 1280,
                        "height": 720,
                    },
                ],
            },
        }
    )

    assert message is not None
    assert message.external_event_id == "99"
    assert message.external_user_id == "123"
    assert message.text == "Please inspect this screenshot"
    assert message.images[0].external_id == "large"
    assert message.sent_at == datetime.fromtimestamp(1788570000, UTC)


def test_ignore_non_private_messages() -> None:
    assert (
        parse_incoming_message(
            {
                "update_id": 1,
                "message": {
                    "from": {"id": 1},
                    "chat": {"id": -1, "type": "group"},
                    "text": "ignored",
                },
            }
        )
        is None
    )


def test_parse_callback_query() -> None:
    message = parse_incoming_message(
        {
            "update_id": 101,
            "callback_query": {
                "id": "callback-1",
                "from": {"id": 123, "first_name": "Owner"},
                "message": {"chat": {"id": 123, "type": "private"}},
                "data": "approve:nonce",
            },
        }
    )

    assert message is not None
    assert message.callback_data == "approve:nonce"
    assert message.callback_query_id == "callback-1"
    assert message.external_user_id == "123"


def test_split_text_respects_limit() -> None:
    chunks = split_text("alpha " * 100, limit=40)
    assert len(chunks) > 1
    assert all(len(chunk) <= 40 for chunk in chunks)


@pytest.mark.asyncio
async def test_client_handles_retry_after_without_exposing_token() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 3},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TelegramClient("123:secret-token", client=http_client)
        with pytest.raises(TelegramAPIError) as raised:
            await client.get_updates(offset=None)

    assert raised.value.retry_after == 3
    assert "secret-token" not in str(raised.value)


@pytest.mark.asyncio
async def test_client_distinguishes_invalid_token_from_transport_failure() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, request=request, json={"ok": False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TelegramClient("123:secret-token", client=http_client)
        with pytest.raises(TelegramAPIError, match="Token 无效或已失效") as raised:
            await client.get_me()

    assert raised.value.error_code == 401


@pytest.mark.asyncio
async def test_set_my_commands_publishes_private_chat_menu() -> None:
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert isinstance(body, dict)
        requests.append(body)
        return httpx.Response(200, request=request, json={"ok": True, "result": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TelegramClient("123:secret-token", client=http_client)
        await client.set_my_commands(bot_api_commands(), scope=TELEGRAM_PRIVATE_COMMAND_SCOPE)

    assert requests == [
        {
            "commands": list(bot_api_commands()),
            "scope": dict(TELEGRAM_PRIVATE_COMMAND_SCOPE),
        }
    ]


@pytest.mark.asyncio
async def test_client_edits_and_deletes_progress_message() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert isinstance(body, dict)
        requests.append((request.url.path.rsplit("/", 1)[-1], body))
        if request.url.path.endswith("deleteMessage"):
            return httpx.Response(200, request=request, json={"ok": True, "result": True})
        if request.url.path.endswith("editMessageText"):
            return httpx.Response(
                200,
                request=request,
                json={"ok": True, "result": {"message_id": 9}},
            )
        raise AssertionError(f"unexpected method: {request.url.path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TelegramClient("123:secret-token", client=http_client)
        await client.edit_text("42", "9", "⏳ 正在处理你的请求…")
        await client.delete_message("42", "9")

    assert requests == [
        ("editMessageText", {"chat_id": "42", "message_id": 9, "text": "⏳ 正在处理你的请求…"}),
        ("deleteMessage", {"chat_id": "42", "message_id": 9}),
    ]


def test_telegram_command_registry_drives_help_text() -> None:
    commands = bot_api_commands()

    assert [item["command"] for item in commands] == [
        "start",
        "help",
        "pair",
        "new",
        "sessions",
        "session",
        "models",
        "model",
        "reasoning",
        "status",
        "security",
        "stop",
        "release",
        "takeover",
    ]
    assert "/use 按项目选择最近会话（兼容命令；推荐使用 /session）" in help_text()
    assert "别名：/approval" in help_text()
    assert "别名：/effort" in help_text()
    assert recognized_command_names() >= {"security", "approval", "reasoning", "effort"}


@pytest.mark.asyncio
async def test_download_enforces_size_limit(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=b"123456")

    destination = tmp_path / "image.jpg"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TelegramClient("123:secret-token", client=http_client)
        with pytest.raises(RuntimeError, match="size limit"):
            await client.download_file(
                file_path="photos/image.jpg", destination=destination, max_bytes=4
            )

    assert not destination.exists()
