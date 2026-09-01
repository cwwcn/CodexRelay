# CodexRelay

<p align="center">
  <img src="assets/CodexRelay.svg" alt="CodexRelay" width="96">
</p>

<p align="center">
  <a href="https://github.com/cwwcn/CodexRelay/actions/workflows/ci.yml"><img src="https://github.com/cwwcn/CodexRelay/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI status"></a>
  <a href="https://github.com/cwwcn/CodexRelay/releases/latest"><img src="https://img.shields.io/github/v/release/cwwcn/CodexRelay?display_name=tag&sort=semver" alt="Latest release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/cwwcn/CodexRelay" alt="MIT License"></a>
</p>

**Use Codex on your own Mac from Telegram.**

CodexRelay is a macOS menu bar app that connects Telegram with a local Codex runtime. Your Mac executes the work in an explicitly authorized project directory, while Telegram provides the remote conversation, approvals, and status updates. Project sessions, context, model settings, and approval state stay on your Mac so you can return to a project without losing the thread.

English | [简体中文](README.zh-CN.md) | [Documentation](docs/)

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
- Telegram Bot Token stored in CodexRelay's private app-data file with user-only permissions;
- Optional sleep prevention while a task is running;
- Rotating logs, single-instance protection, and crash-recovery safeguards.

## Security boundaries

- The Bot Token is never written to TOML, SQLite, environment variables, command-line arguments, or logs;
- Telegram access is unavailable until pairing is complete;
- Telegram cannot add arbitrary local directories;
- Approval buttons are single-use, and both allow and deny decisions are reported explicitly;
- CodexRelay never modifies your global `~/.codex/config.toml`;
- GitHub Releases update checking is available; when automatic checks find a release, the menu-bar panel shows a “New version available” action. After the user clicks it, CodexRelay selects the current Mac architecture's DMG, verifies its SHA-256 digest, and opens the installer for manual replacement.

## Requirements

For the packaged DMG:

- macOS on Apple Silicon or Intel;
- Codex CLI installed and authenticated on the Mac;
- A Telegram Bot Token.

CodexRelay currently supports one-to-one Telegram chats only; group chats and channels are not supported. Create the bot through [@BotFather](https://t.me/BotFather), and keep the Bot Token private.

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

### Where the app appears

CodexRelay is a menu bar app. It normally does not open a Dock window when launched. Click the CodexRelay icon in the macOS menu bar to open the overview panel, then choose **Settings** to complete setup. If the menu bar is crowded, look in the macOS Control Center or remove unused menu bar items temporarily.

## Quick start

### 1. Run from source

```bash
uv sync --extra dev --extra gui
uv run codexrelay init
uv run codexrelay-gui
```

### 2. Complete first-time setup

1. Install and authenticate Codex CLI on the Mac;
2. Create a bot with [@BotFather](https://t.me/BotFather) and copy its Bot Token;
3. Enter the token on CodexRelay's **Telegram** page;
4. Add and approve a project directory on the **Projects** page;
5. Generate a one-time pairing code;
6. Message your bot in a private Telegram chat with `/pair 123456`.

The token is stored only in CodexRelay's private app-data file, never in a project configuration file.

> Upgrading from an older development build: to avoid recurring macOS Keychain authorization prompts, current builds no longer read the legacy Keychain entry. Enter the Bot Token once again on the Telegram page after upgrading.

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
| Telegram Bot Token | CodexRelay private app-data file (`0600`) |

Logs rotate at 2 MB and keep three historical files (about 8 MB maximum). The database stores projects, sessions, tasks, messages, approvals, and delivery state; it does not store the Bot Token.

## Development and tests

```bash
uv sync --extra dev --extra gui
uv run ruff check .
uv run mypy --strict src
QT_QPA_PLATFORM=offscreen uv run pytest -q
```

The current quality gate includes Ruff, strict Mypy, and the full Pytest suite; all checks run in GitHub Actions.

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
- The current update flow downloads and opens the matching GitHub Release DMG after user confirmation; the final drag-to-Applications step remains manual because the package is not yet Apple-notarized;
- Planned work includes signed/notarized distribution, optional Sparkle-based in-place updates, and additional connectors.

## Documentation

- [Technical design](docs/CodexRelay-技术方案.md)
- [macOS product-shape research](docs/macos-product-redesign-research.md)
- [Menu bar design research](docs/menu-bar-design-research.md)
- [Contributing guide](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)

## License

CodexRelay is released under the [MIT License](LICENSE).
