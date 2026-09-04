"""Telegram-specific command-menu metadata.

The command menu belongs to the Telegram connector. Core routing remains
connector-neutral so another channel can expose its own native affordances.
"""

from collections.abc import Mapping
from dataclasses import dataclass

TELEGRAM_PRIVATE_COMMAND_SCOPE: Mapping[str, str] = {"type": "all_private_chats"}

# Aliases stay Telegram-connector-specific.  They are accepted by the router
# for backwards compatibility, but are intentionally not duplicated in the
# Telegram command menu so the native picker remains compact.
TELEGRAM_COMMAND_ALIASES: Mapping[str, str] = {
    "approval": "security",
    "effort": "reasoning",
}


@dataclass(frozen=True, slots=True)
class TelegramCommand:
    """One Telegram-native command and its user-facing descriptions."""

    name: str
    description: str
    help_text: str

    def bot_api_payload(self) -> dict[str, str]:
        return {"command": self.name, "description": self.description}


# This is the single source of truth for Telegram's primary command menu and
# /help. Project management lives in the Mac app's System page; compatibility
# commands remain accepted by Router but are not advertised in the native menu.
TELEGRAM_COMMANDS: tuple[TelegramCommand, ...] = (
    TelegramCommand("start", "开始使用 CodexRelay", "开始使用 CodexRelay"),
    TelegramCommand("help", "查看帮助", "查看帮助"),
    TelegramCommand("pair", "使用 Mac 端配对码", "使用 Mac 端配对码"),
    TelegramCommand("new", "在当前目录新建会话", "在当前会话工作目录新建会话"),
    TelegramCommand(
        "sessions",
        "查看全部会话",
        "查看全部会话（项目归属会显示在列表中）",
    ),
    TelegramCommand("session", "切换当前会话", "切换会话：/session <编号>"),
    TelegramCommand("models", "查看可用模型", "查看可用模型"),
    TelegramCommand("model", "选择当前会话模型", "选择模型：/model <编号或名称>"),
    TelegramCommand(
        "reasoning",
        "设置推理强度",
        "设置推理强度：/reasoning <强度>（别名：/effort）",
    ),
    TelegramCommand("status", "查看当前状态", "查看当前状态"),
    TelegramCommand(
        "security",
        "设置当前会话安全模式",
        "设置当前会话安全模式（别名：/approval）",
    ),
    TelegramCommand("stop", "停止当前任务", "停止当前任务"),
    TelegramCommand("release", "清理异常状态", "清理异常遗留状态（通常无需使用）"),
    TelegramCommand(
        "takeover",
        "查看会话接力说明",
        "查看会话接力说明（兼容命令，无需手动接管）",
    ),
)

TELEGRAM_COMPATIBILITY_COMMANDS: tuple[TelegramCommand, ...] = (
    TelegramCommand(
        "projects",
        "查看会话的项目归属（兼容）",
        "查看会话的项目归属（兼容命令；切换会话请使用 /sessions）",
    ),
    TelegramCommand(
        "use",
        "按项目选择会话（兼容）",
        "按项目选择最近会话（兼容命令；推荐使用 /session）",
    ),
)


def bot_api_commands() -> tuple[dict[str, str], ...]:
    """Return fresh payloads suitable for Telegram's setMyCommands API."""
    return tuple(command.bot_api_payload() for command in TELEGRAM_COMMANDS)


def recognized_command_names() -> frozenset[str]:
    """Return canonical commands plus compatibility aliases accepted by Router."""
    return (
        frozenset(command.name for command in TELEGRAM_COMMANDS)
        | frozenset(command.name for command in TELEGRAM_COMPATIBILITY_COMMANDS)
        | frozenset(TELEGRAM_COMMAND_ALIASES)
    )


def help_text() -> str:
    """Render the same command registry as the Telegram /help response."""
    groups = (
        ("会话操作", {"sessions", "session", "new", "status"}),
        ("任务控制", {"stop", "release", "takeover"}),
        ("配置与安全", {"models", "model", "reasoning", "security"}),
        ("首次设置", {"pair"}),
        ("其他", {"start", "help"}),
    )
    sections: list[str] = []
    for title, names in groups:
        commands = [command for command in TELEGRAM_COMMANDS if command.name in names]
        if commands:
            sections.append(
                title
                + "：\n"
                + "\n".join(f"/{command.name} {command.help_text}" for command in commands)
            )
    sections.append(
        "兼容命令（项目管理请在 Mac 端‘系统’页完成）：\n"
        + "\n".join(
            f"/{command.name} {command.help_text}"
            for command in TELEGRAM_COMPATIBILITY_COMMANDS
        )
    )
    return "\n\n".join(sections)
