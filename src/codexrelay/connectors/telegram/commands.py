"""Telegram-specific command-menu metadata.

The command menu belongs to the Telegram connector. Core routing remains
connector-neutral so another channel can expose its own native affordances.
"""

from collections.abc import Mapping
from dataclasses import dataclass

TELEGRAM_PRIVATE_COMMAND_SCOPE: Mapping[str, str] = {"type": "all_private_chats"}


@dataclass(frozen=True, slots=True)
class TelegramCommand:
    """One Telegram-native command and its user-facing descriptions."""

    name: str
    description: str
    help_text: str

    def bot_api_payload(self) -> dict[str, str]:
        return {"command": self.name, "description": self.description}


# This is the single source of truth for Telegram's command menu and /help.
# Adding a command here updates both surfaces without touching Core.
TELEGRAM_COMMANDS: tuple[TelegramCommand, ...] = (
    TelegramCommand("start", "开始使用 CodexRelay", "开始使用 CodexRelay"),
    TelegramCommand("help", "查看帮助", "查看帮助"),
    TelegramCommand("pair", "使用 Mac 端配对码", "使用 Mac 端配对码"),
    TelegramCommand("projects", "查看已授权项目", "查看已授权项目"),
    TelegramCommand("use", "切换当前项目", "切换当前项目：/use <编号或名称>"),
    TelegramCommand("new", "新建当前项目对话", "新建当前项目对话"),
    TelegramCommand("models", "查看可用模型", "查看可用模型"),
    TelegramCommand("model", "选择当前会话模型", "选择模型：/model <编号或名称>"),
    TelegramCommand("reasoning", "设置推理强度", "设置推理强度：/reasoning <强度>"),
    TelegramCommand("status", "查看当前状态", "查看当前状态"),
    TelegramCommand("stop", "停止当前任务", "停止当前任务"),
)


def bot_api_commands() -> tuple[dict[str, str], ...]:
    """Return fresh payloads suitable for Telegram's setMyCommands API."""
    return tuple(command.bot_api_payload() for command in TELEGRAM_COMMANDS)


def help_text() -> str:
    """Render the same command registry as the Telegram /help response."""
    return "\n".join(f"/{command.name} {command.help_text}" for command in TELEGRAM_COMMANDS)
