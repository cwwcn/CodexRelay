# CodexRelay

通过 Telegram 私聊，安全地使用你自己 Mac 上运行的 Codex。

CodexRelay 是一个 macOS 菜单栏应用：Telegram 负责远程交互，本机 Codex 负责执行任务。项目、会话上下文、模型设置和审批状态都保存在本机，切换回项目后可以继续原来的对话。

[English](README.md) | 简体中文 | [Wiki](../../wiki)

> 当前状态：Early Preview / Alpha
>
> 目前主要面向个人使用和 Apple Silicon Mac。界面、打包、公证和在线更新仍在持续完善。

## 它解决什么问题

你可以在手机 Telegram 中向机器人发送任务，让自己的 Mac 在指定项目目录中调用 Codex，并把结果返回 Telegram。它不是云端代码执行服务，任务实际运行在你自己的 Mac 上。

## 主要能力

- Telegram 文本和图片输入；
- 一次性配对码和单用户授权；
- 只允许操作在 Mac App 中明确授权的项目目录；
- 每个项目独立保存 Codex thread，切换回来后继续原上下文；
- 每个项目可独立选择 Codex 模型与推理强度；
- 首版全局只运行一个任务，运行期间禁止切换项目；
- Codex thread/turn、Telegram inbox/outbox 和任务状态持久化；
- 危险命令、文件修改和额外权限通过 Telegram 单次审批；
- 菜单栏概览面板、设置窗口、任务停止和退出确认；
- Telegram Bot Token 保存在 macOS Keychain；
- 任务运行期间可阻止 Mac 自动睡眠；
- 日志轮转、单实例运行和崩溃恢复保护。

## 安全边界

- Bot Token 不写入 TOML、SQLite、环境变量、命令行参数或日志；
- Telegram 只有在完成配对后才能操作；
- Telegram 不能直接添加任意本机目录；
- 审批按钮为单次消费，允许和拒绝都会明确反馈；
- 不修改用户全局 `~/.codex/config.toml`；
- 已提供 GitHub Releases 更新检查；只读取发行版元数据，不会自动替换应用。

## 使用要求

- macOS Apple Silicon；
- Python 3.12；
- 已安装并登录本机 Codex CLI；
- 一个 Telegram Bot Token；
- `uv`（源码运行和开发构建时需要）。

## 快速开始

### 1. 源码运行

```bash
uv sync --extra dev --extra gui
uv run codexrelay init
uv run codexrelay-gui
```

### 2. 完成首次配置

1. 在 Telegram 的 BotFather 创建机器人并取得 Bot Token；
2. 在 CodexRelay 的“Telegram”页面输入 Token；
3. 在“项目”页面添加并选择允许 Codex 操作的目录；
4. 生成一次性配对码；
5. 在 Telegram 私聊机器人发送 `/pair 123456`。

Token 只会写入 macOS 钥匙串，不会写入项目配置文件。

## Telegram 命令

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

项目切换只允许在当前任务结束后进行。需要立刻切换时，先发送 `/stop`，再发送 `/use`。切换项目不会清空原项目对话。

## 模型和推理强度

模型设置可以在 Mac App 的“Codex”页面修改，也可以在 Telegram 中执行：

```text
/models
/model 2
/reasoning high
```

设置保存在 CodexRelay 自己的 SQLite 数据库中，只作用于当前项目的当前会话。它不会改变 Codex CLI、Codex 桌面端或其他项目的全局默认值。

## 本地数据

| 内容 | 默认位置 |
| --- | --- |
| 数据库和设置 | `~/Library/Application Support/CodexRelay/` |
| 运行日志 | `~/Library/Logs/CodexRelay/` |
| Telegram Bot Token | macOS Keychain |

日志按 2 MB 轮转并保留 3 份历史文件，最大约 8 MB。数据库保存项目、会话、任务、消息、审批和投递状态，不保存 Bot Token。

## 开发与测试

```bash
uv sync --extra dev --extra gui
uv run ruff check .
uv run mypy --strict src
QT_QPA_PLATFORM=offscreen uv run pytest -q
```

当前质量门：Ruff、Mypy strict 和 65 项 Pytest 通过。

## 构建 macOS App

```bash
uv sync --extra gui --extra packaging
./scripts/build_app.sh
```

构建脚本默认把中间文件和应用输出放到 `artifacts/`，并拒绝覆盖已有 `.app`。个人构建使用 ad-hoc 签名；面向其他用户分发前还需要 Apple Developer ID 签名和 notarization。

## 当前限制与路线图

- 当前全局只执行一个任务；
- 当前 Telegram 是首个连接器，核心层已为未来其他连接器留出边界；
- 当前主要支持 Apple Silicon；
- 当前更新流程会打开官方 GitHub Release 页面，由用户确认下载；正式签名和公证完成后再接入 Sparkle 安装；
- 后续计划包括正式发行包、公证、自动更新和更多连接器。

## 文档

- [技术方案](docs/CodexRelay-技术方案.md)
- [macOS 产品形态调研](docs/macos-product-redesign-research.md)
- [菜单栏设计调研](docs/menu-bar-design-research.md)
- [贡献指南](CONTRIBUTING.md)
- [安全策略](SECURITY.md)
- [更新日志](CHANGELOG.md)

## 许可证

CodexRelay 采用 [MIT License](LICENSE) 开源。
