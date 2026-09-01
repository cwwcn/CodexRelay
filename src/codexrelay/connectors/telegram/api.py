from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from codexrelay.connectors.base import ImageAttachment, IncomingMessage


class TelegramError(RuntimeError):
    pass


class TelegramTransportError(TelegramError):
    pass


class TelegramAPIError(TelegramError):
    def __init__(
        self, description: str, *, error_code: int | None, retry_after: int | None
    ) -> None:
        super().__init__(description)
        self.description = description
        self.error_code = error_code
        self.retry_after = retry_after


class TelegramClient:
    def __init__(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        request_timeout: float = 20,
    ) -> None:
        normalized = token.strip()
        if not normalized or ":" not in normalized:
            raise ValueError("Telegram bot token has an invalid shape")
        self._token = normalized
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(request_timeout, read=request_timeout + 40)
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> TelegramClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def get_me(self) -> dict[str, Any]:
        result = await self._call("getMe")
        if not isinstance(result, dict):
            raise TelegramAPIError(
                "getMe returned an invalid object", error_code=None, retry_after=None
            )
        return result

    async def delete_webhook(self) -> None:
        await self._call("deleteWebhook", {"drop_pending_updates": False})

    async def get_updates(
        self,
        *,
        offset: int | None,
        poll_timeout: int = 30,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": poll_timeout,
            "limit": limit,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = await self._call("getUpdates", payload)
        if not isinstance(result, list):
            raise TelegramAPIError(
                "getUpdates returned an invalid list", error_code=None, retry_after=None
            )
        return [item for item in result if isinstance(item, dict)]

    async def send_text(
        self,
        chat_id: str,
        text: str,
        *,
        reply_markup: Mapping[str, Any] | None = None,
    ) -> list[str]:
        message_ids: list[str] = []
        for chunk in split_text(text):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            if reply_markup is not None and len(message_ids) == 0:
                payload["reply_markup"] = dict(reply_markup)
            result = await self._call(
                "sendMessage",
                payload,
            )
            if not isinstance(result, dict) or "message_id" not in result:
                raise TelegramAPIError(
                    "sendMessage returned an invalid object", error_code=None, retry_after=None
                )
            message_ids.append(str(result["message_id"]))
        return message_ids

    async def answer_callback_query(self, callback_query_id: str, text: str) -> None:
        await self._call(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text},
        )

    async def get_file_path(self, file_id: str) -> str:
        result = await self._call("getFile", {"file_id": file_id})
        if not isinstance(result, dict) or not isinstance(result.get("file_path"), str):
            raise TelegramAPIError(
                "getFile returned no file path", error_code=None, retry_after=None
            )
        return str(result["file_path"])

    async def download_file(
        self,
        *,
        file_path: str,
        destination: Path,
        max_bytes: int = 20 * 1024 * 1024,
    ) -> int:
        url = f"https://api.telegram.org/file/bot{self._token}/{file_path.lstrip('/')}"
        total = 0
        try:
            async with self._client.stream("GET", url) as response:
                response.raise_for_status()
                declared = response.headers.get("content-length")
                if declared is not None and int(declared) > max_bytes:
                    raise TelegramError("Telegram image exceeds the configured size limit")
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with destination.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise TelegramError("Telegram image exceeds the configured size limit")
                        output.write(chunk)
        except TelegramError:
            destination.unlink(missing_ok=True)
            raise
        except (httpx.HTTPError, OSError, ValueError) as error:
            destination.unlink(missing_ok=True)
            raise TelegramTransportError("Telegram file download failed") from error
        return total

    async def _call(
        self, method: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any] | list[Any] | bool:
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        try:
            response = await self._client.post(url, json=dict(payload or {}))
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise TelegramTransportError(f"Telegram {method} request failed") from error
        if not isinstance(body, dict):
            raise TelegramAPIError(
                f"Telegram {method} returned invalid JSON", error_code=None, retry_after=None
            )
        if not body.get("ok"):
            parameters = body.get("parameters")
            retry_after = (
                int(parameters["retry_after"])
                if isinstance(parameters, dict) and "retry_after" in parameters
                else None
            )
            raise TelegramAPIError(
                str(body.get("description", "Telegram API error")),
                error_code=int(body["error_code"]) if "error_code" in body else None,
                retry_after=retry_after,
            )
        result = body.get("result")
        if not isinstance(result, (dict, list, bool)):
            raise TelegramAPIError(
                f"Telegram {method} returned an invalid result", error_code=None, retry_after=None
            )
        return result


def parse_incoming_message(
    update: Mapping[str, Any], *, account_id: str = "main-bot"
) -> IncomingMessage | None:
    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        return None
    callback = update.get("callback_query")
    callback_data: str | None = None
    callback_query_id: str | None = None
    if isinstance(callback, Mapping):
        message = callback.get("message")
        sender = callback.get("from")
        data = callback.get("data")
        callback_id = callback.get("id")
        callback_data = data if isinstance(data, str) else None
        callback_query_id = str(callback_id) if callback_id is not None else None
    else:
        message = update.get("message")
        sender = message.get("from") if isinstance(message, Mapping) else None
    if not isinstance(message, Mapping):
        return None
    chat = message.get("chat")
    if not isinstance(sender, Mapping) or not isinstance(chat, Mapping):
        return None
    if chat.get("type") != "private" or "id" not in sender or "id" not in chat:
        return None
    text = message.get("text")
    if not isinstance(text, str):
        caption = message.get("caption")
        text = caption if isinstance(caption, str) else ""
    images = _parse_photos(message.get("photo"))
    if not text.strip() and not images and callback_data is None:
        return None
    first_name = sender.get("first_name")
    last_name = sender.get("last_name")
    username = sender.get("username")
    display_parts = [
        str(value).strip()
        for value in (first_name, last_name)
        if isinstance(value, str) and value.strip()
    ]
    display_name = " ".join(display_parts)
    if not display_name and isinstance(username, str):
        display_name = f"@{username}"
    return IncomingMessage(
        connector_type="telegram",
        account_id=account_id,
        external_event_id=str(update_id),
        external_user_id=str(sender["id"]),
        external_conversation_id=str(chat["id"]),
        sender_display_name=display_name or f"Telegram {sender['id']}",
        text=text,
        images=images,
        callback_data=callback_data,
        callback_query_id=callback_query_id,
    )


def _parse_photos(value: object) -> tuple[ImageAttachment, ...]:
    if not isinstance(value, list):
        return ()
    photos = [item for item in value if isinstance(item, Mapping) and "file_id" in item]
    if not photos:
        return ()
    largest = max(
        photos,
        key=lambda item: (
            int(item.get("file_size", 0)) or int(item.get("width", 0)) * int(item.get("height", 0))
        ),
    )
    return (
        ImageAttachment(
            external_id=str(largest["file_id"]),
            mime_type="image/jpeg",
            file_name=f"telegram-{largest.get('file_unique_id', 'image')}.jpg",
        ),
    )


def split_text(text: str, *, limit: int = 4000) -> tuple[str, ...]:
    if limit < 1:
        raise ValueError("limit must be positive")
    if not text:
        return ("",)
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        boundary = remaining.rfind("\n", 0, limit + 1)
        if boundary < limit // 2:
            boundary = remaining.rfind(" ", 0, limit + 1)
        if boundary < limit // 2:
            boundary = limit
        chunks.append(remaining[:boundary].rstrip())
        remaining = remaining[boundary:].lstrip()
    chunks.append(remaining)
    return tuple(chunks)
