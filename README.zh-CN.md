# CodexRelay

<p align="center">
  <img src="assets/CodexRelay.svg" alt="CodexRelay" width="96">
</p>

<p align="center">
  <a href="https://github.com/cwwcn/CodexRelay/actions/workflows/ci.yml"><img src="https://github.com/cwwcn/CodexRelay/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI 状态"></a>
  <a href="https://github.com/cwwcn/CodexRelay/releases/latest"><img src="https://img.shields.io/github/v/release/cwwcn/CodexRelay?display_name=tag&sort=semver" alt="最新版本"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/cwwcn/CodexRelay" alt="MIT License"></a>
</p>

通过 Telegram 私聊，安全地使用你自己 Mac 上运行的 Codex。

CodexRelay 是一个 macOS 菜单栏应用：Telegram 负责远程交互，本机 Codex 在当前会话的工作目录中执行任务。会话、可选项目归属、上下文、模型设置和审批状态都保存在本机。

[English](README.md) | 简体中文 | [文档](docs/)

> **当前版本：** `v0.1.3`
>
> 当前版本同时面向 Apple Silicon 和 Intel Mac。发布包使用 ad-hoc 签名，尚未完成 Apple 公证；下方提供首次打开时 macOS 安全确认的操作说明。

## 它解决什么问题

你可以在手机 Telegram 中向机器人发送任务，让自己的 Mac 在当前会话的工作目录中调用 Codex，并把结果返回 Telegram。会话可以归属于已明确授权的项目，也可以保持未归属。它不是云端代码执行服务，任务实际运行在你自己的 Mac 上。

## 主要能力

- Telegram 文本和图片输入；
- 一次性配对码和单用户授权；
- 项目目录需在 Mac App 中明确授权，并可选择性地与会话建立归属；
- 每个会话独立保存 Codex thread，包括无项目会话；
- 每个会话独立选择 Codex 模型与推理强度；
- 全局同时只运行一个任务，运行期间禁止切换会话；
- Codex thread/turn、Telegram inbox/outbox 和任务状态持久化；
- 危险命令、文件修改和额外权限通过 Telegram 单次审批；
- 可选的“本项目内自动允许”审批模式，必须经过明确的二次确认；无项目会话仍可正常使用，风险操作使用受控安全模式；
- 菜单栏概览面板、设置窗口、任务停止和退出确认；
- Telegram Bot Token 保存在 CodexRelay 私有数据文件中，仅当前用户可读写；
- 任务运行期间可阻止 Mac 自动睡眠；
- 日志轮转、单实例运行和崩溃恢复保护。

## 安全边界

- Bot Token 不写入 TOML、SQLite、环境变量、命令行参数或日志；
- Telegram 只有在完成配对后才能操作；
- Telegram 不能直接添加任意本机目录；
- “扫描项目”会将活动列表同步为当前扫描结果：配置的扫描范围内，已移动、重命名或不再符合项目特征的旧项目会从活动列表隐藏；扫描范围外手动添加的项目不受影响。数据库记录保留且可恢复，不会删除任何项目文件；
- 审批按钮为单次消费，允许和拒绝都会明确反馈；
- 默认使用**安全模式**。本项目内自动允许仅绑定当前项目路径和当前配对的 Telegram 身份，切换项目或重新配对后自动失效；它不会放开项目外访问，也不会绕过 macOS 隐私权限；
- 不修改用户全局 `~/.codex/config.toml`；
- 已提供 GitHub Releases 更新检查；自动检查发现新版本后，会在菜单栏面板显示“发现新版本，下载更新”。用户点击后，程序选择匹配当前 Mac 架构的 DMG，校验 SHA-256 后打开安装包，由用户完成最后的替换。

## 使用要求

使用打包 DMG 需要：

- macOS Apple Silicon 或 Intel；
- 已安装并登录本机 Codex CLI；
- 一个 Telegram Bot Token。

如果从源码运行或参与开发，还需要 Python 3.12 和 `uv`。

当前版本只支持 Telegram 一对一私聊，不支持群组和频道。请通过 [@BotFather](https://t.me/BotFather) 创建机器人，并妥善保管 Bot Token。

## 从 GitHub Releases 安装

从 [GitHub Releases](https://github.com/cwwcn/CodexRelay/releases) 下载与你的 Mac 架构匹配的 DMG：Apple Silicon（M 系列）选择 `arm64`，Intel 选择 `x86_64`。打开后将 CodexRelay 拖入“应用程序”文件夹。

### macOS 首次打开

当前发布包尚未完成 Apple 公证，macOS 首次打开时可能会拦截：

1. 双击打开 CodexRelay，看到安全提示后点击“取消”；
2. 打开“系统设置 → 隐私与安全性”；
3. 向下找到关于 CodexRelay 的安全提示；
4. 点击“仍要打开”，再确认一次；
5. 重新打开 CodexRelay。

也可以在 Finder 中右键点击应用并选择“打开”。不要关闭 macOS 的整体安全保护。如果系统提示“应用会损害你的电脑”或将应用移到废纸篓，请不要强行绕过，重新下载安装包并反馈提示内容。

### App 启动后在哪里

CodexRelay 是菜单栏应用。启动后通常不会像普通 App 一样在 Dock 中打开窗口，请点击 macOS 菜单栏中的 CodexRelay 图标打开概览面板，再选择“设置”完成配置。如果菜单栏空间不足，可以先在 macOS 控制中心查找，或暂时移除不常用的菜单栏图标。

## 快速开始

### 1. 源码运行

```bash
uv sync --extra dev --extra gui
uv run codexrelay init
uv run codexrelay-gui
```

### 2. 完成首次配置

1. 在 Mac 上安装并登录 Codex CLI；
2. 在 Telegram 中通过 [@BotFather](https://t.me/BotFather) 创建机器人并取得 Bot Token；
3. 在 CodexRelay 的“Telegram”页面输入 Token；
4. （可选）在“系统”页面的项目区域中添加并选择允许 Codex 操作的目录；会话也可以保持未归属；
5. 生成一次性配对码；
6. 在 Telegram 私聊机器人发送 `/pair 123456`。

Token 只会写入 CodexRelay 私有数据文件，不会写入项目配置文件。

添加项目时，CodexRelay 会立即做一次最小访问预检。若项目位于“文稿”等受保护目录，macOS 会在这一步请求授权；请在配置阶段完成允许操作，任务执行过程中不会故意等待这类授权。

> 如果你从旧版开发构建升级：为了避免反复出现 macOS 钥匙串授权弹窗，新版本不会读取旧版钥匙串中的 Token。升级后请在“Telegram”页面重新输入一次 Bot Token。

## Telegram 命令

日常使用主要记住以下命令：

```text
/sessions       查看全部会话
/session 1      选择会话
```

选好会话后，直接发送普通文字即可执行任务。会话可以有项目归属，也可以保持未归属。需要新开上下文时使用 `/new`；任务运行时需要停止则使用 `/stop`。

完整命令参考：

```text
/help
/pair 123456
 首次配对 Telegram 账号
/projects
 查看会话的项目归属（兼容命令）
/use 1
/use CodexRelay
 按项目选择上下文（兼容命令；推荐使用 `/session`）
/new
 在当前会话工作目录中新建会话
/sessions
 查看全部 Codex 会话，包括未归属会话
/session <编号>
 切换当前会话（项目归属会显示在列表中）
/models
/model 2
/reasoning high
/status
/security
/stop
 停止当前任务
/release
 清理异常遗留状态（通常无需使用）
/takeover
 查看会话接力说明（兼容命令，无需手动接管）
```

`/help` 会按“会话操作、任务控制、配置与安全、首次设置”分组显示命令，并在末尾列出项目管理兼容命令。项目管理主要在 Mac 端“系统”页完成；选择会话请使用 `/sessions` 和 `/session`。未配对时仅接受 `/pair <六位配对码>`（也兼容 BotFather 深链格式 `/start pair_<六位配对码>`）；已配对账号再次发送 `/pair` 只会收到状态提示，不会被当作任务执行。`/security` 用于查看当前会话的安全模式；项目会话经过二次确认后可以启用“本项目内自动允许”，无项目会话仍可正常使用，风险操作使用受控安全模式；`/reasoning` 用于修改推理强度，兼容别名为 `/effort`。设置保存在 CodexRelay 自己的本地数据库中，不会修改 `~/.codex/config.toml`。

项目切换只允许在当前任务结束后进行。`/use` 是为旧流程保留的兼容命令；新的主流程是在 `/sessions` 中查看全部会话，再使用 `/session` 精确选择。切换项目归属不会删除会话历史。

会话是主要对象，项目只是可选归属。`/sessions` 会同步并列出按项目和未归属状态分组的全部 Codex 会话，`/session <编号>` 选择会话，`/new` 在当前工作目录中新建会话。Mac App 也提供独立的“会话”页面展示同一份全局视图。应用启动、后台定期同步和会话列表请求都会校准会话；只要会话在 Codex 中被删除或归档，下一次成功同步时就会从活动视图隐藏，但本地历史仍保留；当前会话失效时会自动选择第一条仍有效的会话。无项目会话可以直接执行，但使用受控安全模式，涉及风险权限时通过 Telegram 请求审批。Telegram 任务完成后会自动释放短期占用，回到电脑端无需手动交接。会话上下文彼此隔离；模型和推理强度跟随会话保存。`/use` 保留为项目切换兼容命令，`/release` 用于清理异常遗留状态，`/takeover` 为兼容保留。未识别的斜杠命令会直接提示，不会被误当成 Codex 任务。

## 模型和推理强度

模型设置可以在 Mac App 的“Codex”页面修改，也可以在 Telegram 中执行：

```text
/models
/model 2
/reasoning high
```

设置保存在 CodexRelay 自己的 SQLite 数据库中，只作用于当前选中的会话。它不会改变 Codex CLI、Codex 桌面端或其他会话的全局默认值。

## 本地数据

| 内容 | 默认位置 |
| --- | --- |
| 数据库和设置 | `~/Library/Application Support/CodexRelay/` |
| 运行日志 | `~/Library/Logs/CodexRelay/` |
| Telegram Bot Token | CodexRelay 私有数据文件（`0600`） |

日志按 2 MB 轮转并保留 3 份历史文件，最大约 8 MB。数据库保存项目、会话、任务、消息、审批和投递状态，不保存 Bot Token。

## 开发与测试

```bash
uv sync --extra dev --extra gui
uv run ruff check .
uv run mypy --strict src
QT_QPA_PLATFORM=offscreen uv run pytest -q
```

当前质量门：Ruff、Mypy strict 和完整 Pytest 测试套件；所有检查都会在 GitHub Actions 中运行。

## 构建 macOS App

```bash
uv sync --extra gui --extra packaging
./scripts/build_app.sh
```

构建脚本默认把中间文件和应用输出放到 `artifacts/`，并拒绝覆盖已有 `.app`。个人构建使用 ad-hoc 签名；面向其他用户分发前还需要 Apple Developer ID 签名和 notarization。

## 当前限制与路线图

- 当前全局只执行一个任务；
- 当前 Telegram 是首个连接器，核心层已为未来其他连接器留出边界；
- 当前同时支持 Apple Silicon 和 Intel，发布包按架构分别提供；
- 当前更新流程会在用户确认后下载并打开匹配架构的 GitHub Release DMG；由于尚未完成 Apple 公证，最后拖入“应用程序”的步骤仍由用户手动完成；
- 后续计划包括签名与公证发行、可选的 Sparkle 原地更新，以及更多连接器。

## 文档

- [技术方案](docs/CodexRelay-技术方案.md)
- [macOS 产品形态调研](docs/macos-product-redesign-research.md)
- [菜单栏设计调研](docs/menu-bar-design-research.md)
- [更新日志](CHANGELOG.md)

## 许可证

CodexRelay 采用 [MIT License](LICENSE) 开源。
