from pathlib import Path

import httpx
import pytest

from codexrelay.connectors.telegram.api import (
    TelegramAPIError,
    TelegramClient,
    parse_incoming_message,
    split_text,
)


def test_parse_private_text_and_largest_photo() -> None:
    message = parse_incoming_message(
        {
            "update_id": 99,
            "message": {
                "from": {"id": 123},
                "chat": {"id": 123, "type": "private"},
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
