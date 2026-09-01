# CodexRelay 技术架构

> 文档状态：Early Preview / Alpha
> 文档定位：说明 CodexRelay 的产品边界、当前实现、运行机制与扩展方向。
> 适用对象：用户、贡献者以及需要评估本项目的开发者。

## 1. 项目概述

CodexRelay 是一个运行在 macOS 菜单栏中的本地消息网关。它把 Telegram 私聊消息转发给本机已经安装并登录的 Codex，任务在用户明确授权的项目目录中执行，状态、审批请求和结果再返回 Telegram。

CodexRelay 不是云端代码执行服务，也不托管用户的项目文件或 Codex 凭据。核心数据和执行过程都留在用户自己的 Mac 上。

```text
Telegram
   │ HTTPS Long Polling
   ▼
Telegram Connector
   │ 标准化消息
   ▼
Relay Core
   ├─ 身份与配对
   ├─ 命令路由
   ├─ 项目/会话/任务协调
   ├─ 审批协调
   └─ SQLite 持久化
   │ Python SDK / 本机 Codex CLI
   ▼
Codex App Server
   │
   ▼
授权项目目录中的文件、命令和工具
```

当前版本的首要交互方式是 Telegram。核心层使用通用的消息、任务和后端边界，未来可以增加企业微信、钉钉或其他连接器，而不需要把这些平台的协议类型带入 Codex 执行层。

## 2. 设计目标与非目标

### 2.1 设计目标

- **本地执行**：Codex 进程和项目文件始终位于用户 Mac。
- **可靠投递**：Telegram Update、任务状态和待发送回复通过本地数据库保存，程序重启后可以继续处理可恢复状态。
- **上下文连续**：每个项目拥有独立的 Codex 会话。切换回项目后继续其原有 thread，不把不同项目的上下文混在一起。
- **明确授权**：只有完成配对的 Telegram 用户、在 Mac 中登记的项目目录，以及经过审批的高风险操作才能生效。
- **可观察**：菜单栏和 Telegram 都能反映连接、项目、任务、审批和错误状态。
- **macOS 原生体验**：常驻菜单栏、设置窗口、Keychain、单实例、登录启动和退出确认均围绕 macOS 使用习惯设计。
- **可演进**：Connector、CodexBackend、更新提供器和启动服务均有独立边界，便于替换实现。

### 2.2 当前非目标

- 不提供多租户云服务或远程执行平台。
- 不在应用内重新实现 Codex，也不管理 Codex 登录凭据。
- 当前不支持群聊、多用户协作和跨渠道会话同步。
- 当前只实现 Telegram，不提供动态安装第三方连接器的插件市场。
- 当前不通过应用自动下载并替换版本；更新检查只读取 GitHub Releases，并由用户确认下载。

## 3. 系统架构

项目采用 Python 3.12 和 `src` layout。运行时分为桌面层、连接器层、核心服务层、Codex 适配层和持久化层。

```text
┌──────────────────────────────────────────────┐
│ macOS UI                                     │
│ PySide6 · 菜单栏 · 设置 · About · 状态刷新    │
└──────────────────────┬───────────────────────┘
                       │ 线程安全命令/状态
┌──────────────────────▼───────────────────────┐
│ Runtime                                      │
│ 生命周期 · 后台 asyncio · 组件启动/停止       │
└───────────────┬──────────────────┬───────────┘
                │                  │
┌───────────────▼────────┐ ┌───────▼───────────┐
│ Connector Layer         │ │ Core Services      │
│ Telegram Poller/Router  │ │ 配对 · 路由 · Job  │
│ Telegram Outbox         │ │ 项目 · 审批协调    │
└───────────────┬────────┘ └───────┬───────────┘
                │                  │
                └──────────┬───────┘
                           ▼
┌──────────────────────────────────────────────┐
│ Persistence                                  │
│ SQLite · migrations · inbox/outbox · recovery │
└──────────────────────┬───────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────┐
│ Codex Backend                                │
│ openai-codex Python SDK · App Server adapter  │
└──────────────────────────────────────────────┘
```

依赖方向保持单向：Telegram 只产生标准化消息；Core 决定是否授权、如何路由以及是否创建任务；Codex Backend 只负责执行；UI 通过运行时服务读取状态，不直接依赖 Telegram 原始 Update 或数据库表结构。

### 3.1 技术选型

| 领域 | 选型 | 主要职责 |
| --- | --- | --- |
| 语言 | Python 3.12 | 业务逻辑、异步 I/O、进程协调和桌面应用 |
| 依赖管理 | uv | 虚拟环境、锁文件和可复现开发环境 |
| 异步运行时 | `asyncio` | Telegram 轮询、后台任务、重试和 Codex 调用 |
| Telegram HTTP | `httpx` | Bot API 请求、长轮询和附件下载 |
| 桌面 UI | PySide6 | 菜单栏、设置窗口、原生菜单和状态展示 |
| 数据库 | SQLite + `aiosqlite` | 事件、任务、会话、审批、游标和迁移 |
| 密钥存储 | `keyring` / macOS Keychain | Telegram Bot Token |
| Codex 接入 | `openai-codex` Python SDK | 本机 Codex App Server 适配 |
| 打包 | PyInstaller | 独立 macOS `.app` |
| 质量保障 | Ruff、Mypy、Pytest | lint、类型检查和自动化测试 |

选型优先考虑本地部署、可测试性和协议边界，而不是追求额外的服务端基础设施。SQLite 不需要独立数据库进程，适合菜单栏应用的单机状态管理。

## 4. 启动与运行流程

应用启动后，`CodexRelayRuntime` 按以下顺序初始化：

1. 创建应用数据、日志和诊断目录。
2. 读取非敏感 TOML 配置，并从 macOS Keychain 读取 Telegram Bot Token。
3. 打开 SQLite，执行迁移、过期审批清理、传输数据维护和中断任务修复。
4. 定位本机 `codex` 可执行文件，启动 Codex Backend，并读取可用模型目录。
5. 调用 Telegram `getMe` 验证 Bot Token，删除旧 webhook（保留待处理 Update）。
6. 启动 Telegram Poller、消息 Router 和 Outbox Worker。
7. 菜单栏显示连接状态；设置窗口可以继续查看和修改配置。

运行期间，Poller 和 Outbox 在后台 asyncio 事件循环中工作，Qt 主线程只负责 UI。关闭设置窗口不会停止后台服务；退出应用时先停止活动任务，再关闭 Telegram、Codex 和数据库连接。

### 4.1 入站消息流程

```text
Telegram getUpdates
        ↓
解析并校验 Update
        ↓
SQLite 事务写入 inbound_events + 推进 cursor
        ↓
Router 处理已落库事件
        ↓
配对/命令/普通任务分流
        ↓
标记事件 processed 或 failed
```

Update 的外部 ID 在数据库中有唯一约束，因此网络重试或 Telegram 重复投递不会创建同一任务两次。

### 4.2 出站消息流程

任务完成、状态变化或审批请求不会直接依赖当前网络连接，而是先写入 `outbound_messages`。Outbox Worker 调用 Telegram API，成功后记录 Telegram `message_id`；失败则按重试策略再次投递，达到上限后保留失败状态供诊断。

Telegram API 没有适用于普通 `sendMessage` 的业务幂等键，因此“网络请求已经成功、应用尚未来得及记录结果”这一极小窗口可能产生重复消息。系统保证不重复执行 Codex turn，但不宣称外部消息具备绝对 exactly-once 语义。

## 5. 连接器架构

当前仓库实现了 Telegram 连接器，通用边界位于 `src/codexrelay/connectors/base.py`。标准化消息至少包含：

- 连接器类型和账号标识；
- 外部事件、用户和会话标识；
- 文本和图片附件；
- 回调数据（用于审批按钮）；
- 发送回复所需的目标信息。

Core 不读取 Telegram `Update` 字段。未来新增连接器时，适配工作应集中在新目录中，例如 `connectors/dingtalk/`，并实现同一组消息接收、文本发送和生命周期边界。当前不实现动态插件发现，也不在 UI 中展示尚未实现的渠道。

身份采用带命名空间的组合键：

```text
(connector_type, account_id, external_user_id)
```

这样可以避免不同平台的用户 ID 偶然相同而发生身份冲突。

## 6. Telegram 接入

### 6.1 Long Polling

应用使用 Telegram Bot API 的 `getUpdates` Long Polling，不要求公网域名、Webhook 服务器或路由器端口映射。Poller 将最高已处理 `update_id + 1` 保存为 cursor，并限制接收 `message` 与 `callback_query` 两类 Update。

连接错误、超时和服务端错误会进入受控重试；Telegram 返回 `retry_after` 时优先遵守服务端给出的等待时间。无效 Token 或账号权限错误会暂停连接，并在菜单栏和设置窗口中显示需要处理的状态。

### 6.2 文本、图片和长消息

Telegram 文本超过单条消息限制时由 API 层分段发送。分段器优先选择换行和段落边界，并避免破坏常见代码块。图片先下载到应用临时目录，再以本地图片输入传给 Codex；任务完成或失败后立即清理临时文件，超过配置大小限制的附件会被拒绝。

### 6.3 命令路由

当前支持的主要命令包括：

```text
/help                 查看帮助
/projects             查看已授权项目
/use <编号或名称>     切换当前项目
/new                  为当前项目创建新对话
/models               查看可用模型
/model <编号或名称>   修改当前会话模型
/reasoning <强度>     修改当前会话推理强度
/status               查看连接、项目和任务状态
/stop                 中断当前任务
```

命令解析在 Telegram Router 中完成，但真正的项目切换、会话创建和任务状态修改仍通过 Core/Database 服务执行。

应用连接 Telegram 时还会通过 `setMyCommands` 注册这组命令的中文说明，并限定在私聊范围内。这样用户在 Telegram 输入 `/` 时可以看到原生命令提示；命令菜单属于 Telegram Connector 的体验能力，不会成为其他连接器必须实现的共性接口。命令名称、菜单说明和 `/help` 文本由 Telegram Connector 内部的单一注册表生成，新增 Telegram 命令时不需要同步维护多份列表。

## 7. 身份、配对与授权

初次使用时，用户在 Mac 设置窗口保存 Bot Token，并生成一个短期一次性配对码。Telegram 私聊 Bot 发送配对命令后，系统使用 Telegram 提供的数字用户 ID 和私聊会话 ID 完成授权。

安全规则如下：

- 配对码使用安全随机数生成，数据库只保存带盐哈希；
- 配对码有有效期、尝试次数上限和一次性消费语义；
- 仅允许私聊，不接受群聊中的配对或任务；
- 同一 Bot 账号只保留一个启用的授权身份；
- 解绑在 Mac 本地完成，不开放给远程消息；
- 未授权用户只能得到通用拒绝信息，不会暴露项目路径、任务状态或系统信息；
- 审批回调会重新校验用户、Bot 账号、nonce 和待审批任务。

## 8. Codex 后端

### 8.1 当前实现

当前后端为 `AppServerBackend`，通过官方 `openai-codex` Python SDK 连接本机 Codex。应用不内嵌 Codex CLI，也不修改用户全局 Codex 配置。

后端负责：

- 定位 `codex` 可执行文件并建立受控子进程环境；
- 检查本机 Codex 账号状态；
- 获取可用模型和每个模型支持的推理强度；
- 创建或恢复 thread；
- 启动一次 turn，传递项目目录、模型、推理强度和图片输入；
- 接收最终回复；
- 在用户请求 `/stop` 或应用退出时中断活动 turn；
- 将 Codex 审批请求交给 `ApprovalCoordinator`。

SDK 内部承载 App Server 的协议握手与双向消息处理，Relay 业务层只依赖 `CodexBackend` 接口。这样可以把协议变化隔离在 `codex/` 目录内。

### 8.2 会话与执行映射

```text
Conversation  = 一个项目的可持续 Codex 对话
Job           = 一次入站消息对应的可靠执行记录
Codex thread  = Codex 侧的上下文恢复键
Codex turn    = thread 中的一次执行轮次
```

典型映射为：

```text
conversation.codex_thread_id  → Codex thread
job.codex_turn_id              → Codex turn
job.inbound_event_id           → Telegram 入站事件
```

模型和推理强度保存在 CodexRelay 自己的 `conversations` 表中，在下一次 turn 调用时作为参数传入。它们不会写回 `~/.codex/config.toml`，因此不会改变 Codex CLI 或其他 Codex 客户端的全局默认值。

### 8.3 任务状态机

```text
queued → starting → running
                     ├─ waiting_approval → running
                     ├─ completed
                     ├─ failed
                     └─ interrupted
```

状态转换由数据库方法统一校验。当前版本全局只允许一个活动任务；任务运行或等待审批期间，项目切换、新建对话以及模型设置修改都会被拒绝。应用重启时，无法确认执行结果的活动任务会保守标记为 `interrupted`，不会自动重放可能产生副作用的命令。

## 9. 审批与安全边界

Codex 可能请求命令执行、文件变更或额外权限。`ApprovalCoordinator` 把请求摘要写入数据库，并通过 Telegram Inline Keyboard 发送“允许一次”和“拒绝”按钮。

审批回调数据只包含动作和短 nonce，不携带命令、路径或完整审批对象。nonce 的哈希与任务、RPC 请求、授权身份和过期时间绑定；数据库使用条件更新保证第一次有效点击原子消费，重复点击、过期回调和跨用户回调都会失败。

默认执行边界是 Codex 的 workspace-write 模式，工作目录绑定到用户在 Mac 中登记的项目。Telegram 不提供开启全盘访问、扩大可写目录、永久放行或执行任意 shell 的命令。

需要特别区分两类权限：

- **Relay 权限**：项目是否登记、当前任务是否允许切换、谁可以发送指令；
- **Codex 权限**：具体命令、文件变更和额外系统权限是否需要本次审批。

两层边界同时成立时，远程消息才可能驱动本机执行。

## 10. 数据持久化与恢复

### 10.1 存储位置

默认使用 macOS 的应用支持目录和日志目录，实际路径由 `platformdirs` 计算，也支持开发和测试时通过环境变量覆盖：

```text
应用数据目录/
├── codexrelay.db
├── settings.toml
├── instance.lock
├── temporary/
└── diagnostics/

日志目录/
└── codexrelay.log
```

Telegram Bot Token 不进入 TOML、SQLite、环境变量、命令行参数或日志，而是通过 `keyring` 保存到 macOS Keychain。

### 10.2 数据模型

SQLite 通过迁移管理 schema。核心实体包括：

| 实体 | 作用 |
| --- | --- |
| `connector_accounts` | 记录连接器账号及启用状态 |
| `connector_cursors` | 保存 Telegram 等连接器的消费游标 |
| `inbound_events` | 保存入站事件、去重键和处理状态 |
| `projects` | Mac 端明确登记的项目目录 |
| `conversations` | 项目与 Codex thread 的持久映射，以及模型设置 |
| `conversation_messages` | 可审计的用户正文和 Codex 最终回复 |
| `jobs` | 一次任务的状态、turn ID 和错误信息 |
| `approval_requests` | 待审批操作、nonce 和审批结果 |
| `outbound_messages` | 待发送、重试中和已送达的 Telegram 回复 |
| `context_checkpoints` | 可选的上下文恢复检查点 |

传输记录与规范会话记录分离：Telegram 原始 payload 和 Outbox 正文可以按保留策略清理，而规范消息、thread ID 和任务关系继续用于审计和恢复。

### 10.3 写入顺序

一次普通任务遵循以下顺序：

1. Telegram Update 在事务中写入 Inbox，并推进 cursor。
2. 用户正文先写入 `conversation_messages`，再创建 `jobs` 记录。
3. 创建或恢复 Codex thread，并持久化 thread ID。
4. 启动 turn，记录 turn ID 和任务状态。
5. turn 成功后先保存 Codex 最终回复并完成 Job，再创建 Outbox 记录。
6. Outbox 成功发送后记录外部消息 ID。

因此，程序在完成 Codex 执行后崩溃时，可以重建待发送回复，而不需要重新执行原任务。

### 10.4 上下文连续性

当前项目只是“下一次任务默认使用哪个项目”的指针；每个项目自己的活动 Conversation 和 Codex thread 独立保存。空闲时可以在已授权项目之间切换，切回原项目后使用其 thread resume，模型和推理强度也随该项目会话恢复。

`/new` 只创建该项目的新 Conversation，不删除旧消息；旧 thread 不会被其他项目复用。若 Codex thread 已不可用，Relay 可以保留规范消息并生成恢复交接，但不会声称能够绕过 Codex 本身的上下文窗口限制。

### 10.5 保留与空间

默认只自动清理可重建或敏感性较高的传输数据：已处理 Inbox/已送达 Outbox 的正文在一段时间后置空，失败记录和已解决审批保留更久用于排障，日志采用轮转，任务临时附件在结束后清理。规范会话不会被静默删除；需要释放空间时由用户显式删除或导出旧会话。

数据库大小主要取决于长期保留的用户消息和 Codex 最终回复，通常远小于项目文件、构建产物或 Codex 自身缓存。应用可以在设置窗口展示本地数据占用，超过阈值时提醒用户处理，而不是擅自删除上下文。

## 11. macOS 应用结构

### 11.1 线程模型

```text
Qt 主线程       → 菜单栏、设置窗口、原生应用菜单、对话框
后台 asyncio    → Telegram、SQLite、Codex、Outbox、重试
线程安全信号    → UI 与后台运行时之间的状态更新
```

Qt 控件只在主线程访问。设置窗口关闭时仅隐藏窗口；菜单栏中的退出操作才会触发完整停机和二次确认。

### 11.2 菜单栏与设置

菜单栏面板以概览为主，展示连接状态、Codex 状态、当前项目、当前任务、模型和推理强度，并提供重新连接、停止任务、打开设置、关于和退出入口。复杂配置放在独立设置窗口中，包括：

- Telegram Bot Token 验证、配对码和授权状态；
- Codex CLI 检测、模型和推理强度；
- 项目登记、扫描、选择和权限说明；
- 登录启动、自动连接、阻止睡眠、数据目录和日志目录；
- 版本信息、仓库、许可证和 GitHub Releases 更新检查。

应用使用 macOS 原生的 `Command+W` 关闭窗口、`Command+,` 打开设置和 `Command+Q` 退出，并在退出会中断活动任务时显示明确确认。

### 11.3 单实例与登录启动

应用通过文件锁保证同一用户会话只运行一个实例，避免同一 Bot 被多个 Poller 并发消费。登录启动由独立的 `StartupService` 封装；当前打包应用使用用户级 LaunchAgent 机制，后续可在不改变 Core 的前提下切换到更原生的系统 Login Item 实现。

## 12. 配置与敏感信息

非敏感设置写入 TOML，例如自动连接、登录启动、阻止睡眠、项目扫描范围和 Telegram 账号标识。设置加载和保存会递归拒绝 `bot_token`、`api_key`、`password` 等敏感字段，避免凭据被误写入配置。

示例：

```toml
[app]
auto_connect = true
launch_at_login = false
prevent_sleep_while_running = true
update_checks_automatically = false

[telegram]
account_id = "main-bot"
private_chat_only = true

[projects]
scan_roots = ["~/Documents"]
scan_depth = 2
```

CodexRelay 只保存自己的项目、会话和模型设置，不读取后回写或覆盖用户的全局 Codex 配置文件。

## 13. 更新、构建与发布

### 13.1 当前更新机制

更新提供器从 GitHub Releases API 读取最新正式发行版，忽略草稿和预发布版本，比较应用版本号并展示发行说明、发布时间和官方 Releases 地址。当前流程是“检查 → 提示 → 用户打开官方页面下载”，不会静默替换应用。

更新逻辑通过 `UpdateProvider` 接口隔离。未来完成签名、公证和发行渠道建设后，可以接入 Sparkle 或其他安装器，而不需要重写 About 页面和设置持久化。

### 13.2 本地构建

项目使用 `uv` 管理依赖，Ruff 和 strict Mypy 负责静态质量，Pytest 负责单元与集成测试。macOS 应用使用 PyInstaller `onedir + windowed` 构建，Codex CLI 不打包进应用，启动时仍使用用户本机已安装的 CLI。

面向公开分发时，还需要 Apple Developer ID 签名、公证、票据 stapling、干净用户环境验证以及 GitHub Release 资产发布。当前主要验证 Apple Silicon 架构。

## 14. 测试与质量保障

测试覆盖以下边界：

- Telegram Update 去重、cursor、断网、超时、429 和长消息分段；
- 配对码过期、重用、错误尝试、未授权身份和私聊限制；
- 项目登记、稳定选择器、活动任务期间禁止切换；
- Codex thread 创建/恢复、模型目录、推理强度、最终回复为空和中断；
- 命令/文件/权限审批、nonce 重放和审批结果反馈；
- Inbox、Outbox、Job 状态转换、迁移和重启恢复；
- 图片下载、大小限制和临时文件清理；
- Keychain、单实例、登录启动和 Qt 菜单栏行为；
- GitHub Releases 版本比较和网络错误。

核心流程使用假连接器和可控的后端替身测试，确保业务逻辑不依赖 Telegram 原始类型。真实 Telegram 验收则用于验证 Bot API、配对、项目切换、审批和消息投递的端到端行为。

## 15. 当前限制

- 全局同时只能运行一个 Codex 任务；这是为了降低远程误操作、项目竞态和资源争用风险。
- 当前只允许一个 Telegram Bot 账号对应一个已授权用户，未提供多用户权限模型。
- Telegram 使用 Long Polling，应用必须在本地持续运行并能访问 Telegram API。
- 主要支持 Apple Silicon macOS；其他架构和发行方式需要独立构建验证。
- 外部 Telegram 消息无法做到绝对 exactly-once，但 Codex 任务不会因普通重试而自动重复执行。
- 会话连续性受 Codex 自身上下文窗口、thread 可用性和本地磁盘可靠性约束。
- 更新检查已经预留正式安装器边界，但当前仍需用户打开 Releases 页面完成下载和安装。

## 16. 扩展方向

后续可以在不改变核心任务模型的情况下逐步增加：

1. 更多连接器，例如企业微信、钉钉和飞书。
2. 更细粒度的用户、项目和审批权限。
3. 多任务队列或按项目并行执行（需要重新设计资源锁与风险模型）。
4. 签名发行包、公证和 Sparkle 自动更新。
5. 可导出、可恢复的会话归档与更完善的诊断报告。
6. 对 Codex App Server 协议版本的能力协商和兼容矩阵。

这些能力属于演进方向，不代表当前版本已经提供对应的用户界面或安全保证。

## 17. 代码导航

| 目录/文件 | 职责 |
| --- | --- |
| `src/codexrelay/runtime.py` | 运行时组件装配、启动、停止与后台循环 |
| `src/codexrelay/core.py` | 项目任务执行与 Codex 结果协调 |
| `src/codexrelay/database.py` | SQLite schema、迁移、仓储和恢复操作 |
| `src/codexrelay/connectors/telegram/` | Telegram API、Poller、Router 和 Outbox |
| `src/codexrelay/codex/` | Codex SDK 后端、模型目录和执行适配 |
| `src/codexrelay/approval.py` | 审批请求、nonce 和 Telegram 回调协调 |
| `src/codexrelay/pairing.py` | 一次性配对码和授权身份 |
| `src/codexrelay/ui/` | 菜单栏、设置窗口和状态展示 |
| `src/codexrelay/updates/` | GitHub Releases 检查和未来更新提供器边界 |
| `tests/` | 单元、集成、UI 和更新流程测试 |

## 18. 许可证

CodexRelay 采用 MIT License。贡献者在提交代码、文档或设计时，应同时遵守仓库中的 `CONTRIBUTING.md` 和 `SECURITY.md`。
