# CodexRelay 技术架构

> 文档状态：Public Alpha / 公开 Alpha
> 文档定位：说明 CodexRelay 的产品边界、当前实现、运行机制与扩展方向。
> 适用对象：用户、贡献者以及需要评估本项目的开发者。

## 1. 项目概述

CodexRelay 是一个运行在 macOS 菜单栏中的本地消息网关。它把 Telegram 私聊消息转发给本机已经安装并登录的 Codex，任务在当前选中会话的工作目录中执行，状态、审批请求和结果再返回 Telegram。项目是会话的可选归属：项目会话使用已登记的目录边界，无项目会话仍可使用自身工作目录，并在风险操作上请求审批。

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
当前会话工作目录中的文件、命令和工具
```

当前版本的首要交互方式是 Telegram。核心层使用通用的消息、任务和后端边界，未来可以增加企业微信、钉钉或其他连接器，而不需要把这些平台的协议类型带入 Codex 执行层。

## 2. 设计目标与非目标

### 2.1 设计目标

- **本地执行**：Codex 进程和项目文件始终位于用户 Mac。
- **可靠投递**：Telegram Update、任务状态和待发送回复通过本地数据库保存，程序重启后可以继续处理可恢复状态。
- **上下文连续**：每个会话拥有独立的 Codex thread；项目是可选归属。切换会话后继续原有上下文，不把不同会话混在一起。
- **明确授权**：只有完成配对的 Telegram 用户才能远程操作；项目会话受 Mac 中登记的目录边界约束，无项目会话使用受控安全模式，高风险操作必须经过审批。
- **可观察**：菜单栏和 Telegram 都能反映连接、项目、任务、审批和错误状态。
- **macOS 原生体验**：常驻菜单栏、设置窗口、私有凭据文件、单实例、登录启动和退出确认均围绕 macOS 使用习惯设计。
- **可演进**：Connector、CodexBackend、更新提供器和启动服务均有独立边界，便于替换实现。

### 2.2 当前非目标

- 不提供多租户云服务或远程执行平台。
- 不在应用内重新实现 Codex，也不管理 Codex 登录凭据。
- 当前不支持群聊、多用户协作；Telegram 与电脑端 Codex 的同会话发现和接续属于单用户本地协同能力，不扩展为云端跨用户同步。
- 当前只实现 Telegram，不提供动态安装第三方连接器的插件市场。
- 更新由 macOS 应用发起：检查 GitHub Releases，按当前 Mac 架构选择 DMG，校验 SHA-256 后打开安装包。应用不后台覆盖正在运行的自身，用户完成最后的拖入“应用程序”步骤。

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
| 密钥存储 | CodexRelay 私有数据文件（`0600`） | Telegram Bot Token |
| Codex 接入 | `openai-codex` Python SDK | 本机 Codex App Server 适配 |
| 打包 | PyInstaller | 独立 macOS `.app` |
| 质量保障 | Ruff、Mypy、Pytest | lint、类型检查和自动化测试 |

选型优先考虑本地部署、可测试性和协议边界，而不是追求额外的服务端基础设施。SQLite 不需要独立数据库进程，适合菜单栏应用的单机状态管理。

## 4. 启动与运行流程

应用启动后，`CodexRelayRuntime` 按以下顺序初始化：

1. 创建应用数据、日志和诊断目录。
2. 读取非敏感 TOML 配置，并从 CodexRelay 私有数据文件读取 Telegram Bot Token。
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
/pair <六位配对码>     首次配对 Telegram 账号
/projects             查看会话的项目归属（兼容命令）
/use <编号或名称>     按项目选择会话（兼容命令）
/new                  在当前会话工作目录创建新对话
/sessions             查看全部 Codex 会话（项目、未归属、不可用）
/session <编号>       切换当前会话
/models               查看可用模型
/model <编号或名称>   修改当前会话模型
/reasoning <强度>     修改当前会话推理强度（别名：/effort）
/status               查看连接、会话和任务状态
/security             查看当前会话安全模式（别名：/approval）
/stop                 中断当前任务
/release              清理异常遗留状态（通常无需使用）
/takeover             查看会话接力说明（兼容命令，无需手动接管）
```

命令解析在 Telegram Router 中完成，但真正的项目归属、会话创建和任务状态修改仍通过 Core/Database 服务执行。解析会统一处理大小写、连续空白和 Telegram 可能附加的 `@bot_username`；未注册的斜杠命令会明确提示，不会误当成普通 Codex 任务。Telegram 原生命令菜单和 `/help` 由连接器内的注册表驱动；项目管理主要在 Mac App 的“系统”页完成，`/projects` 与 `/use` 仅作为兼容命令保留，不再作为原生菜单中的主入口。

当前版本将 Conversation 作为主要对象，项目是可选归属。`app_state.current_conversation_id` 记录当前选中的会话；切换会话时同步更新可选的 `current_project_id`。会话的 `source` 表示它最初在哪一端创建（Telegram 创建、电脑端创建或其他连接器创建），`lock_owner` 则独立表示当前正在运行的这一轮任务由哪一端发起，不能混为同一个概念。`/sessions` 在请求时通过 Codex App Server 的 `thread/list` 同步电脑端会话：当前 `cwd` 精确匹配已登记项目的 thread 显示项目归属；其他 thread 作为无项目会话显示。项目目录迁移后，标题明确匹配当前项目名但 cwd 不同的桌面 thread 以路径不可用提示列出，供用户显式选择。同步按 Codex thread ID 幂等注册，不复制上下文，也不按模糊项目名授予项目权限；应用启动、定期后台同步和 `/sessions` 请求都会执行校准，每次成功同步还会把 Codex 已不再返回的会话归档并从可选列表隐藏，保留本地历史，不做永久删除；如果当前选中的会话失效，则自动选择第一条仍有效的会话，没有有效会话时保持未选择；如果会话之后在 Codex 中恢复，则按原 thread ID 解除归档，避免重复条目；临时同步失败则不触发清理。无项目会话可以直接选择和执行，并固定使用受控安全模式，风险权限通过 Telegram 单次审批；项目会话可在明确确认后启用项目内自动允许。会话拥有独立的 Codex thread、模型配置和上下文。使用 `/session` 只改变当前选择，不创建永久占用；Telegram 任务运行期间短暂持有会话租约，任务完成、失败或停止后自动释放，让用户回到电脑端即可继续。`/release` 用于清理异常遗留状态，`/takeover` 为兼容保留，不创建永久锁定。启动恢复阶段会清理没有活动任务支撑的遗留租约，避免异常退出造成永久占用。

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
- 启动一次 turn，传递会话工作目录、模型、推理强度和图片输入；
- 接收最终回复；
- 在用户请求 `/stop` 或应用退出时中断活动 turn；
- 将 Codex 审批请求交给 `ApprovalCoordinator`。

SDK 内部承载 App Server 的协议握手与双向消息处理，Relay 业务层只依赖 `CodexBackend` 接口。这样可以把协议变化隔离在 `codex/` 目录内。

### 8.2 会话与执行映射

```text
Conversation  = 一个可选归属项目的可持续 Codex 对话
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

默认执行边界是 Codex 的 workspace-write 模式。项目会话使用已登记项目目录；无项目会话使用自身 cwd，并在风险权限上保持受控审批。Telegram 不提供开启全盘访问、扩大可写目录、永久放行或执行任意 shell 的命令。

### 9.1 项目级自动允许（可选）

默认审批模式是安全模式：命令执行、文件变更和额外权限请求由 `ApprovalCoordinator` 转成 Telegram 一次性审批。为适应手机远程操作场景，Telegram 对项目会话提供可选的“本项目内自动允许”模式，但它不是全局放权；无项目会话不提供该模式：

- 策略保存在 CodexRelay 自己的 SQLite 数据库中，不修改用户的 `~/.codex/config.toml`；
- 策略绑定当前项目的规范化路径和当前已配对 Telegram 身份；
- 开启必须通过 `/security` 进入，并经过二次确认；运行任务期间不能修改；
- 命令工作目录、文件变更根目录和额外文件系统路径必须位于当前项目目录内；网络权限、项目外路径、未知路径格式和无法确认范围的请求继续走人工审批或拒绝。无项目会话不提供自动允许模式，始终按受控安全模式处理风险请求；
- Codex App Server 在两种模式下都保持 `on-request`，由 Relay 审批协调器执行范围判断；不会通过关闭服务端审批请求来模拟“自动允许”；
- 切换项目、项目路径发生变化或重新配对后，策略自动恢复为安全模式；
- 该策略不能绕过 macOS TCC/文件夹访问权限。系统权限失效时，应通过 Telegram 明确提示用户在 Mac 端重新授权。
- 项目登记后立即执行最小访问预检，并在应用启动时对已登记项目再次检查；预检只读取目录元数据和一层目录项，不递归、不修改文件。这样受保护目录的 macOS 授权尽量发生在首次配置阶段，而不是任务执行中途。

这项模式会降低安全边界，适合明确理解风险且希望减少逐项确认的个人用户；默认关闭，由用户自行决定是否开启。

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

Telegram Bot Token 不进入 TOML、SQLite、环境变量、命令行参数或日志，而是保存到应用数据目录下权限为 `0600` 的私有凭据文件。这样可以避免 ad-hoc 开发构建触发反复的 macOS 钥匙串授权弹窗。凭据文件不进入 Git，也不会被 CodexRelay 上传。为避免升级旧版本时再次触发系统授权，新版本不自动读取旧钥匙串记录；旧版本用户升级后需在 Telegram 设置页重新输入一次 Token。

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
| `project_approval_policies` | 项目审批模式、路径范围和配对身份绑定 |
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

当前会话是下一次任务的唯一执行对象；项目只是会话的可选归属。切换会话后使用其 Codex thread resume，模型和推理强度也随会话恢复；无项目会话在自身 cwd 下执行。

`/new` 在当前会话的工作目录中创建新的 Conversation，不删除旧消息；旧 thread 不会被其他会话复用。若 Codex thread 已不可用，Relay 可以保留规范消息并生成恢复交接，但不会声称能够绕过 Codex 本身的上下文窗口限制。

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

菜单栏面板以概览为主，展示连接状态、Codex 状态、当前会话及其可选项目归属、当前任务、模型和推理强度，并提供重新连接、停止任务、打开设置、关于和退出入口。复杂配置放在独立设置窗口中，包括：

- Telegram Bot Token 验证、配对码和授权状态；
- Codex CLI 检测、模型和推理强度；
- 会话全局视图、筛选、切换以及项目归属说明；
- 项目登记、扫描、选择和权限说明（项目管理位于“系统”页面，不作为一级主导航）；
- 用户点击“扫描项目”时，会把配置扫描范围内的活动项目与本次发现结果同步：新发现的项目登记并展示，已移动、重命名、删除或不再符合项目特征的旧项目标记为停用并从活动列表隐藏。扫描范围外手动登记的项目不受影响。数据库记录保留且不删除任何项目文件；
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

更新提供器从 GitHub Releases API 读取最新正式发行版，忽略草稿和预发布版本，比较应用版本号并展示发行说明、发布时间、匹配当前架构的 DMG 资源和校验信息。开启自动检查后，发现更新会在菜单栏面板显示“发现新版本，下载更新”；用户点击后进入“下载 → SHA-256 校验 → 自动打开 DMG → 手动拖入‘应用程序’”流程，不会后台覆盖正在运行的应用。

更新逻辑通过 `UpdateProvider` 接口隔离。当前 GitHub provider 负责正式 Release 发现、架构匹配、DMG 下载和 SHA-256 校验；未来完成 Developer ID 签名、公证和 Sparkle 发行链路后，可以替换安装器实现，而不需要重写 About 页面和设置持久化。

### 13.2 本地构建

项目使用 `uv` 管理依赖，Ruff 和 strict Mypy 负责静态质量，Pytest 负责单元与集成测试。macOS 应用使用 PyInstaller `onedir + windowed` 构建，Codex CLI 不打包进应用，启动时仍使用用户本机已安装的 CLI。

面向公开分发时，若希望消除 Gatekeeper 的首次拦截，还需要 Apple Developer ID 签名、公证、票据 stapling 和干净用户环境验证。当前 Release 流程分别构建并验证 Apple Silicon 与 Intel DMG；在未公证阶段，用户按项目文档通过“隐私与安全性 → 仍要打开”完成首次授权。

## 14. 测试与质量保障

测试覆盖以下边界：

- Telegram Update 去重、cursor、断网、超时、429 和长消息分段；
- 配对码过期、重用、错误尝试、未授权身份和私聊限制；
- 项目登记、项目选择器、活动任务期间禁止切换；
- Codex thread 创建/恢复、模型目录、推理强度、最终回复为空和中断；
- 命令/文件/权限审批、nonce 重放和审批结果反馈；
- Inbox、Outbox、Job 状态转换、迁移和重启恢复；
- 图片下载、大小限制和临时文件清理；
- 私有凭据文件、单实例、登录启动和 Qt 菜单栏行为；
- GitHub Releases 版本比较和网络错误。

核心流程使用假连接器和可控的后端替身测试，确保业务逻辑不依赖 Telegram 原始类型。真实 Telegram 验收则用于验证 Bot API、配对、项目切换、审批和消息投递的端到端行为。

## 15. 当前限制

- 全局同时只能运行一个 Codex 任务；这是为了降低远程误操作、项目竞态和资源争用风险。
- 当前只允许一个 Telegram Bot 账号对应一个已授权用户，未提供多用户权限模型。
- Telegram 使用 Long Polling，应用必须在本地持续运行并能访问 Telegram API。
- 当前发布流程同时提供 Apple Silicon（arm64）和 Intel（x86_64）macOS 构建；其他架构和发行方式需要独立构建验证。
- 外部 Telegram 消息无法做到绝对 exactly-once，但 Codex 任务不会因普通重试而自动重复执行。
- 会话连续性受 Codex 自身上下文窗口、thread 可用性和本地磁盘可靠性约束。
- 更新检查、架构匹配、DMG 下载和校验已经完成；当前仍需用户把打开的 App 拖入“应用程序”完成安装。

## 16. 扩展方向

后续可以在不改变核心任务模型的情况下逐步增加：

1. 更多连接器，例如企业微信、钉钉和飞书。
2. 更细粒度的用户、项目和审批权限。
3. 多任务队列或按项目并行执行（需要重新设计资源锁与风险模型）。
4. 签名发行包、公证和可选的 Sparkle 原地更新。
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

CodexRelay 采用 MIT License。提交代码、文档或设计时，请先阅读 README 中的项目说明，并通过 GitHub Issues 或 Security advisories 提交反馈。
