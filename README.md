# CodexRelay

**Use Codex on your own Mac from Telegram.**

CodexRelay is a macOS menu bar app that connects Telegram with a local Codex runtime. Your Mac executes the work in an explicitly authorized project directory, while Telegram provides the remote conversation, approvals, and status updates. Project sessions, context, model settings, and approval state stay on your Mac so you can return to a project without losing the thread.

English | [简体中文](README.zh-CN.md) | [Wiki](../../wiki)

> **Current release:** `v0.1.0`
>
> This release targets Apple Silicon and Intel Macs. The distributed app is ad-hoc signed and is not yet Apple-notarized; the first-launch instructions below explain the one-time macOS security confirmation.

## Why CodexRelay

Send a task to your Telegram bot and let your own Mac run Codex in a project you have explicitly approved. Results and progress are sent back to Telegram. CodexRelay is not a hosted code execution service: the task runs locally on your Mac.

## Features

- Telegram text and image input;
- One-time pairing code and single-user authorization;
- Project directories must be explicitly approved in the Mac app;
- An independent Codex thread for each project, restored when you switch back;
- Per-project Codex model and reasoning-effort settings;
- One active task globally in the first release; project switching is blocked while a task is running;
- Durable persistence for Codex threads/turns, Telegram inbox/outbox, and task state;
- One-time Telegram approval for dangerous commands, file changes, and extra permissions;
- Menu bar overview panel, settings window, task stop action, and quit confirmation;
- Telegram Bot Token stored in the macOS Keychain;
- Optional sleep prevention while a task is running;
- Rotating logs, single-instance protection, and crash-recovery safeguards.

## Security boundaries

- The Bot Token is never written to TOML, SQLite, environment variables, command-line arguments, or logs;
- Telegram access is unavailable until pairing is complete;
- Telegram cannot add arbitrary local directories;
- Approval buttons are single-use, and both allow and deny decisions are reported explicitly;
- CodexRelay never modifies your global `~/.codex/config.toml`;
- A GitHub Releases update checker is available; it only reads release metadata and never replaces the app automatically.

## Requirements

For the packaged DMG:

- macOS on Apple Silicon or Intel;
- Codex CLI installed and authenticated on the Mac;
- A Telegram Bot Token.

For source development, Python 3.12 and `uv` are also required.

## Install from GitHub Releases

Download the DMG matching your Mac from [GitHub Releases](https://github.com/cwwcn/CodexRelay/releases): `arm64` for Apple Silicon (M-series) or `x86_64` for Intel. Open it and drag CodexRelay to Applications.

### First launch on macOS

The current distribution is not Apple-notarized, so macOS may block the first launch:

1. Double-click CodexRelay and dismiss the security alert;
2. Open **System Settings → Privacy & Security**;
3. Scroll to the security message for CodexRelay;
4. Click **Open Anyway**, then confirm;
5. Launch CodexRelay again.

You can also right-click the app in Finder and choose **Open**. Do not disable Gatekeeper globally. If macOS says the app will damage your computer or moves it to the Trash, do not bypass the warning; download the package again and report the message.

## Quick start

### 1. Run from source

```bash
uv sync --extra dev --extra gui
uv run codexrelay init
uv run codexrelay-gui
```

### 2. Complete first-time setup

1. Create a bot with BotFather and copy its Bot Token;
2. Enter the token on CodexRelay's **Telegram** page;
3. Add and approve a project directory on the **Projects** page;
4. Generate a one-time pairing code;
5. Message your bot in Telegram with `/pair 123456`.

The token is stored only in the macOS Keychain, never in a project configuration file.

## Telegram commands

```text
/help
/projects
/use 1
/use CodexRelay
/new
/models
/model 2
/reasoning high
/status
/stop
```

Project switching is allowed only after the current task finishes. To switch immediately, send `/stop` first, then `/use`. Switching projects does not clear the previous project's conversation.

## Models and reasoning effort

Choose the model and reasoning effort on the Mac app's **Codex** page or from Telegram:

```text
/models
/model 2
/reasoning high
```

Settings are stored in CodexRelay's own SQLite database and apply only to the current project's session. They do not change the global defaults used by Codex CLI, the Codex desktop app, or other projects.

## Local data

| Data | Default location |
| --- | --- |
| Database and settings | `~/Library/Application Support/CodexRelay/` |
| Runtime logs | `~/Library/Logs/CodexRelay/` |
| Telegram Bot Token | macOS Keychain |

Logs rotate at 2 MB and keep three historical files (about 8 MB maximum). The database stores projects, sessions, tasks, messages, approvals, and delivery state; it does not store the Bot Token.

## Development and tests

```bash
uv sync --extra dev --extra gui
uv run ruff check .
uv run mypy --strict src
QT_QPA_PLATFORM=offscreen uv run pytest -q
```

The current quality gate includes Ruff, strict Mypy, and 73 passing Pytest tests.

## Build the macOS app

```bash
uv sync --extra gui --extra packaging
./scripts/build_app.sh
```

The build script places intermediate files and the app bundle in `artifacts/` and refuses to overwrite an existing `.app`. Personal builds use ad-hoc signing; distribution requires Apple Developer ID signing and notarization.

## Current limitations and roadmap

- One task runs globally at a time;
- Telegram is the first connector; the core layer leaves room for future connectors;
- Apple Silicon and Intel are supported through architecture-specific DMG packages;
- The current update flow opens the official GitHub Release page for user-confirmed downloads; Sparkle installation can be added after signed releases and notarization are in place;
- Planned work includes signed distribution builds, notarization, automatic updates, and additional connectors.

## Documentation

- [Technical design](docs/CodexRelay-技术方案.md)
- [macOS product-shape research](docs/macos-product-redesign-research.md)
- [Menu bar design research](docs/menu-bar-design-research.md)
- [Contributing guide](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)

## License

CodexRelay is released under the [MIT License](LICENSE).
