# CodexRelay 技术方案草案

> 状态：个人版 MVP 已实现；真实 Telegram Bot 端到端验收已通过
> 更新日期：2026-09-01
> 目标项目目录：`/Users/cwwen/Documents/CodexRelay`

当前实现产物：

- 源码：`/Users/cwwen/Documents/CodexRelay`
- macOS 候选应用：`/Users/cwwen/Documents/CodexRelay/artifacts/current/CodexRelay.app`
- 自动化质量门：Ruff、Mypy strict、64 项 Pytest 全部通过。
- 打包验证：arm64、`LSUIElement=true`、ad-hoc 深度签名、Finder/LaunchServices 启动、
  单实例和正常退出均通过。
- 真实 Telegram E2E：Bot 配对、项目列表、稳定编号切换与切回、文本任务、图片任务、
  图片临时文件清理、`/status`、`/stop`、任务期间 `caffeinate`、项目外写入的允许一次与拒绝、
  Inbox/Outbox 投递和中断后保守恢复均通过。
- 联调中发现并修复：当前项目导致编号重排、并发 SQLite 事务冲突、Codex 空回复导致
  Telegram 重试空消息，以及审批结果文案无法区分允许/拒绝。
- 多项目模型已明确为“全局单任务、空闲时切换、每项目独立 Codex thread”：任务运行或等待
  审批期间拒绝切换；A → B → A 时恢复 A 原有上下文，不创建无关的新 thread。
- 模型与推理强度保存在每个项目的活动 Conversation 中，通过 App Server 的 turn 参数生效；
  不读取后回写、不覆盖 `~/.codex/config.toml`，因此不影响 Codex 的全局设置。
- 历史构建已于 2026-08-31 非破坏性归档到 `artifacts/archive/`；没有永久删除文件。

## 1. 一句话定义

CodexRelay 是一个常驻 macOS 的本地消息网关：它接收经过身份验证的 Telegram 消息，把消息转换为本机 Codex 任务，并将任务状态、审批请求和最终结果发回手机。

第一版只实现 Telegram，但核心、存储和身份模型不绑定 Telegram。未来需要企业微信、钉钉、飞书等渠道时，应通过新增 Connector 完成，而不是重写核心。

```text
Telegram 手机端
      │ HTTPS Long Polling
      ▼
TelegramConnector
      │ 标准化事件
      ▼
CodexRelay Core
      ├── 身份认证与配对
      ├── 命令路由
      ├── 对话与任务队列
      ├── 审批协调
      └── SQLite 持久化
      │ stdio JSONL / 双向 JSON-RPC
      ▼
codex app-server
      │
      ▼
本机项目、文件与命令
```

## 2. 第一性原理：真正需要解决什么

把“在 Telegram 里调用 Codex”拆到底层，产品必须同时满足以下条件：

1. **消息可靠到达**：网络波动、程序崩溃或重复 Update 不应导致任务静默丢失或重复执行。
2. **远程身份可信**：只有明确配对的 Telegram 数字用户 ID 可以驱动本机 Codex。
3. **对话连续**：每条手机消息不是孤立进程；每个项目独立保留活动 Codex thread，切换回
   项目时继续原上下文，也可以通过 `/new` 显式新建该项目对话。
4. **权限可控**：远程入口不能默认拥有整台 Mac 的无限权限；文件写入、网络和危险命令应有明确边界。
5. **长任务可观察**：用户需要知道任务处于排队、运行、等待审批、完成、失败还是中断状态。
6. **进程可恢复**：Telegram 网络、Codex App Server 或桌面进程异常退出后，系统能重连并恢复可恢复状态。
7. **macOS 使用自然**：应用以菜单栏程序存在，Token 进 Keychain，可登录启动，并能导出脱敏诊断信息。
8. **渠道可以替换**：Telegram 是首个接入端，而不是写死在核心和数据库中的产品边界。

因此，本项目的核心不是“机器人回复文本”，而是一个小型、持久化、有权限边界的远程任务协调器。

## 3. 范围与非目标

### 3.1 第一版必须实现

- Python 构建的 macOS 菜单栏应用和设置窗口。
- 一个 Telegram Bot、一个已配对 Telegram 用户、仅私聊。
- Telegram Long Polling、Update 去重和游标持久化。
- 多个经过 Mac 端授权的本地项目；Telegram 可以查看、按稳定编号或名称切换项目。
- 第一版全局只执行一个任务；任务运行或等待审批期间禁止切换项目，完成或 `/stop` 后方可切换。
- 每个项目独立保存活动 Conversation 与 Codex thread，项目切换不会清空其他项目上下文。
- 当前项目的活动 Conversation 独立保存模型与推理强度；Mac App 和 Telegram 均可修改。
- 新建、继续、中断和查看 Codex 对话。
- 统一 `CodexBackend` 抽象；首版通过官方 `openai-codex` Python SDK 调用本机已登录的
  Codex CLI/App Server，核心层不依赖 Telegram。
- 任务队列、状态通知、长文本安全分段。
- Telegram 按钮式单次审批。
- SQLite 状态存储、Keychain Token 存储、滚动脱敏日志。
- 崩溃恢复、网络重连、Codex 子进程监督。
- 单元测试、假 Telegram/假 Codex 集成测试和少量真机端到端测试。

### 3.2 第一版明确不做

- 不实现企业微信、钉钉、飞书或其他渠道。
- 不做可下载安装的动态 Connector 插件系统。
- 不支持群聊、多用户、多租户或远程用户管理。
- 不允许通过 Telegram 开启 `dangerFullAccess`、永久放行命令或修改项目根目录。
- 不做 Mac App Store 发布。
- 不复制或管理 Codex 的登录凭据。
- 不内嵌 Codex CLI；继续使用本机安装并已登录的 Codex。
- 不做跨渠道消息同步。

这条边界非常重要：**第一版只开发 Telegram，所谓“留口子”只体现在内部接口、统一模型和数据库命名上，不提前实现未来功能。**

## 4. 项目命名

项目名采用 `CodexRelay`。

- `Codex` 指明本地执行后端。
- `Relay` 表示在消息渠道和 Codex 之间中继事件。
- 名称不绑定 Telegram，后续增加其他渠道不需要改产品名。

建议标识：

```text
应用名：CodexRelay
Python 包：codexrelay
Bundle ID：com.cwwen.codexrelay
数据目录：~/Library/Application Support/CodexRelay/
日志目录：~/Library/Logs/CodexRelay/
```

## 5. 技术路线

| 领域 | 选型 | 用途 |
|---|---|---|
| 语言 | Python 3.12 | 核心业务、异步 I/O、进程管理和桌面应用 |
| 依赖管理 | uv | 虚拟环境、锁文件和可复现构建 |
| 异步运行时 | `asyncio` | Telegram 轮询、任务队列、Codex JSONL 和重试 |
| Telegram HTTP | `httpx` | 直接调用 Bot API，完整控制 offset、超时和重试 |
| 桌面 UI | PySide6 | 菜单栏、设置窗口、通知和状态展示 |
| 数据库 | SQLite + `aiosqlite` | Inbox、Outbox、会话、任务、审批与迁移 |
| 数据模型 | Pydantic | 外部 JSON 校验和协议边界 |
| 密钥 | `keyring`（macOS Keychain 后端） | 保存 Telegram Bot Token |
| macOS 桥接 | 按需使用 PyObjC | `SMAppService` 等仅 macOS API |
| 日志 | 标准库 `logging` | 滚动文件、结构化字段和脱敏 |
| 测试 | pytest、pytest-asyncio、respx | 单元、异步和 HTTP 模拟测试 |
| 质量 | Ruff、mypy | 格式、lint 和类型检查 |
| 打包 | PyInstaller | 生成独立的 `CodexRelay.app` |

Python 标准库的异步子进程与管道用于启动并持续读写 App Server；stdout 与 stderr 各设独立读取任务，启动参数使用参数数组传递，不通过 shell 拼接。

SQLite 作为无需独立服务进程的本地状态库，承载任务状态、Telegram 游标和 Update 去重；相关更新通过事务完成，避免多个 JSON 文件“读—改—写”造成的并发覆盖。

## 6. 总体分层

```text
┌───────────────────────────────────────────────┐
│ macOS Shell                                  │
│ PySide6 Tray · Settings · Keychain · Startup │
└───────────────────────┬───────────────────────┘
                        │ 状态/命令
┌───────────────────────▼───────────────────────┐
│ Connector Layer                              │
│ TelegramConnector（第一版唯一实现）           │
└───────────────────────┬───────────────────────┘
                        │ IncomingMessage
┌───────────────────────▼───────────────────────┐
│ Core                                         │
│ Auth · Router · Conversation · Job · Approval│
└──────────────┬──────────────────────┬─────────┘
               │                      │
┌──────────────▼────────────┐ ┌───────▼─────────┐
│ Codex Backend             │ │ Persistence     │
│ AppServer / Exec fallback │ │ SQLite/Keychain │
└───────────────────────────┘ └─────────────────┘
```

依赖方向必须单向：

- Core 可以依赖通用 Connector Protocol，但不能 import Telegram 的 `Update` 类型。
- Telegram Connector 可以调用 Core 提交标准事件，但不能直接创建 Codex turn。
- Codex Backend 不知道请求来自 Telegram 还是未来其他渠道。
- UI 通过应用服务读取状态，不直接操作数据库表。

## 7. Connector：只实现 Telegram，但不写死 Telegram

### 7.1 最小 Connector 接口

第一版不做动态插件系统，只保留内部 Protocol：

```python
class InteractionConnector(Protocol):
    connector_type: str
    account_id: str

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def health_check(self) -> ConnectorHealth: ...
    async def send_text(
        self, target: ReplyTarget, text: str
    ) -> SentMessage: ...
    async def edit_text(
        self, message: SentMessage, text: str
    ) -> None: ...
    async def send_file(
        self, target: ReplyTarget, file: OutgoingFile
    ) -> SentMessage: ...
    async def send_action(
        self, target: ReplyTarget, action: str
    ) -> None: ...
    async def send_approval(
        self, target: ReplyTarget, request: ApprovalView
    ) -> SentMessage: ...
```

第一版仓库中只有：

```text
connectors/
├── base.py
├── models.py
├── registry.py
└── telegram/
```

将来需要钉钉时才添加 `connectors/dingtalk/`，核心无需改写。第一版不显示“尚未实现的渠道”，避免空功能和过度设计。

### 7.2 标准化入站模型

```python
@dataclass(frozen=True)
class IncomingMessage:
    event_id: str
    connector_type: str
    connector_account_id: str
    conversation_id: str
    sender_id: str
    sender_name: str | None
    conversation_type: ConversationType
    text: str | None
    attachments: tuple[Attachment, ...]
    reply_target: ReplyTarget
    received_at: datetime
```

Telegram Connector 把 `Update` 转成该对象；Core 从此不接触 Telegram 原始字段。

### 7.3 身份带命名空间

外部用户身份必须由三元组唯一确定：

```text
(connector_type, connector_account_id, external_user_id)
```

示例：

```text
telegram:main-bot:99887766
```

不能只保存 `99887766`，否则以后其他平台可能发生 ID 冲突。

## 8. Telegram 接入实现

### 8.1 选择 Long Polling

第一版使用 `getUpdates` 长轮询，不部署 webhook。原因是私人 Mac 应用无需公网域名、HTTPS 入口或路由器端口映射，所有网络流量都由 Mac 主动发起。

Telegram 官方规定：

- `getUpdates` 用于 Long Polling；`timeout` 应使用正数，短轮询只建议测试。
- `offset` 应为此前最高 `update_id + 1`，以确认旧 Update 并减少重复。
- 设置 webhook 后 `getUpdates` 不可用；切回轮询可调用 `deleteWebhook`。
- `allowed_updates` 可限制只接收 `message` 和 `callback_query`。[Telegram Bot API：getUpdates](https://core.telegram.org/bots/api#getupdates)

建议请求：

```python
await api.get_updates(
    offset=cursor,
    timeout=45,
    allowed_updates=["message", "callback_query"],
)
```

启动时：

1. `getMe` 验证 Token 和读取 Bot 标识。
2. `getWebhookInfo` 检查旧 webhook。
3. 如存在旧 webhook，由用户明确确认后或首次向导中调用 `deleteWebhook`；不能静默丢弃待处理更新。
4. 开始 45 秒 Long Polling；HTTP read timeout 应略大于 45 秒。

### 8.2 Inbox 与 offset 原子落库

接到 Update 后不应直接启动 Codex。正确顺序是：

```text
getUpdates 返回
    ↓
SQLite 事务：INSERT OR IGNORE inbound_event
    ↓
同一事务更新 connector cursor
    ↓
提交事务
    ↓
Inbox Worker 异步处理 pending 事件
```

这样可把“Telegram 接收确认”和“长时间运行 Codex”解耦。应用崩溃后，已落库但未完成的事件仍可恢复。

建议唯一约束：

```sql
UNIQUE(connector_type, connector_account_id, external_event_id)
```

网络策略：

- 连接失败、超时和 5xx：指数退避并加入随机抖动，设置最大间隔。
- 429：严格遵守 Telegram 返回的 `retry_after`。
- 401/403：视为配置或权限错误，暂停轮询并在 Mac UI 明确提示。
- JSON 校验失败：保存最小诊断信息，不让整个 Poller 崩溃。

### 8.3 回调按钮

审批按钮使用 Inline Keyboard。Telegram 的 `callback_data` 上限为 1–64 字节，因此只能放短动作名和随机 nonce，不能放命令、路径或完整审批对象。[Telegram Bot API：InlineKeyboardButton](https://core.telegram.org/bots/api#inlinekeyboardbutton)

收到 CallbackQuery 后必须尽快调用 `answerCallbackQuery`，否则 Telegram 客户端会持续显示进度条。[Telegram Bot API：CallbackQuery](https://core.telegram.org/bots/api#callbackquery)

### 8.4 回复长度与进度

`sendMessage` 文本在实体解析后限制为 1–4096 字符，因此必须实现 Unicode 安全、代码块感知的分段器。[Telegram Bot API：sendMessage](https://core.telegram.org/bots/api#sendmessage)

第一版策略：

- 默认发纯文本，避免 Codex 输出触发 MarkdownV2 转义错误。
- 优先在段落、换行和句子边界切分。
- 尽量保持代码围栏完整；必要时在分段两侧补围栏。
- 超长报告可以再用 `.md` 文件发送。
- 运行中定期发送 `sendChatAction(typing)`；官方说明该状态持续至多约 5 秒，故它只作为活动提示，不能作为任务状态的唯一载体。[Telegram Bot API：sendChatAction](https://core.telegram.org/bots/api#sendchataction)

### 8.5 Outbox 的边界

出站消息应先写入 `outbound_messages`，再由 Worker 发送并记录 Telegram `message_id`。但 Telegram 普通 `sendMessage` 没有业务幂等键，因此存在一个无法完全消除的小窗口：消息已经送达，而应用在记录成功前崩溃，恢复后可能重复发送。

这是外部 API 的客观边界。方案通过短重试窗口、记录 `message_id`、任务状态前缀和恢复提示降低影响，不宣称“绝对恰好一次”。

## 9. 配对与授权

第一版仅允许一个已配对用户的私人聊天。

配对过程：

1. 用户在 Mac 设置窗口保存 Bot Token。
2. Token 写入 Keychain，应用调用 `getMe` 验证。
3. Mac 本地生成 6 位一次性配对码，10 分钟有效。
4. 用户在 Telegram 私聊 Bot 发送 `/pair 482719`。
5. Connector 读取 Telegram 提供的数字 `from.id` 和 `chat.id`。
6. Core 校验配对码、私聊类型和“尚无已授权用户”。
7. 事务保存外部身份，立即消费配对码。

安全规则：

- 认证依据是 Telegram 数字用户 ID，不是可修改的 username。
- 配对码使用安全随机数生成，服务端只保存哈希。
- 每个码只能使用一次；连续错误尝试做速率限制。
- 已配对后，不再接受第二个用户的 `/pair`。
- 解绑只能在 Mac 本地设置界面完成。
- 未授权用户只能收到不含系统信息的通用拒绝消息。
- CallbackQuery 也必须重新校验用户、Bot 账号和会话目标，不能只校验 nonce。

## 10. Codex 后端

### 10.1 后端演进顺序

`CodexBackend` 是 Core 唯一依赖的执行接口。实现顺序与最终默认值分开：

1. **先实现 `ExecBackend`**：用最小代码跑通 Telegram → 队列 → `codex exec` → 回复的完整链路，优先验证身份、持久化、取消和错误反馈。
2. **再实现 `AppServerBackend`**：接入双向 JSON-RPC、流式事件、thread/turn、interrupt、steer 和审批。
3. **完成验收后将 `AppServerBackend` 设为默认**；`ExecBackend` 保留为明确可见的 fallback 和诊断通道。

OpenAI 官方将 `codex exec` 定位为脚本和 CI 的非交互模式，适合作为最小闭环和降级执行方式；它会将进度写到 stderr、最终 Agent 消息写到 stdout。[OpenAI Codex 非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)

### 10.2 App Server 作为完整主后端

Codex App Server 面向需要认证、对话历史、审批和流式 Agent 事件的深度客户端集成，符合本项目需求。[OpenAI Codex App Server 官方文档](https://learn.chatgpt.com/docs/app-server)

默认启动：

```text
codex app-server
```

官方文档说明，默认传输是 stdio；每一行是一个 JSON（JSONL）消息，WebSocket 目前属于实验性且不受支持的传输。因此第一版只使用 stdio，不开放 TCP 监听端口。[App Server 传输说明](https://learn.chatgpt.com/docs/app-server#transport)

### 10.3 JSON-RPC 生命周期

```text
启动 codex app-server
    ↓
initialize request
    ↓
initialized notification
    ↓
thread/start 或 thread/resume
    ↓
turn/start
    ↓
持续读取 item/*、turn/* 通知和 server request
    ↓
turn/completed / turn/interrupted / error
```

官方要求每条连接先发送 `initialize`，再发送 `initialized`；握手前的其他请求会被拒绝。新对话使用 `thread/start`，持久化恢复使用 `thread/resume`；`turn/start` 开始一轮，`turn/steer` 可向正在进行的 turn 追加输入。[App Server 生命周期](https://learn.chatgpt.com/docs/app-server#lifecycle-overview)

Python 客户端内部维护：

```python
next_request_id: int
pending_requests: dict[int, asyncio.Future[Any]]
notification_handlers: dict[str, list[Handler]]
server_request_handlers: dict[str, ServerRequestHandler]
write_lock: asyncio.Lock
```

必须区分：

- 带 `id` 和 `result`/`error`：请求响应。
- 不带 `id`：通知，例如 `item/agentMessage/delta`。
- App Server 发来带 `id` 和 `method` 的消息：服务器请求，例如审批；客户端必须回复该 ID。

### 10.4 Thread、Turn 与 Job 的映射

```text
Codex thread  = 可持续的对话
Codex turn    = 一次用户输入到结束的执行轮次
Relay job     = 本地可靠队列中的一次任务记录
```

关系：

```text
conversation.codex_thread_id → Codex thread
job.codex_turn_id             → Codex turn
job.inbound_event_id          → 原始渠道事件
```

第一版全局同时只运行一个 Job，同一 thread 自然也同时只有一个 turn。任务运行期间普通消息
按顺序等待；`/use` 明确拒绝切换并提示等待完成或先 `/stop`。不同项目各自保留独立 thread，
空闲时切换项目后，下一轮恢复目标项目原 thread。

### 10.5 Sandbox 与审批策略

`turn/start` 支持显式 `cwd`、审批策略和 `workspaceWrite` sandbox；`writableRoots` 可限制可写根目录，`networkAccess` 可单独关闭。官方示例同时展示了 `unlessTrusted` 与 `workspaceWrite` 组合。[App Server：启动 Turn 与 Sandbox](https://learn.chatgpt.com/docs/app-server#start-a-turn)

第一版默认：

```json
{
  "cwd": "/selected/project",
  "approvalPolicy": "unlessTrusted",
  "sandboxPolicy": {
    "type": "workspaceWrite",
    "writableRoots": ["/selected/project"],
    "networkAccess": false
  }
}
```

同时要注意：`workspaceWrite` 的只读访问默认可能是更广的 `fullAccess`。如果当前 Codex 版本支持显式受限读取，应配置 `readOnlyAccess`；否则 UI 中必须如实说明“写权限受项目目录限制，但读权限边界由当前 Codex sandbox 规则决定”，不能误导用户。[App Server：Sandbox Read Access](https://learn.chatgpt.com/docs/app-server#sandbox-read-access-readonlyaccess)

第一版禁止：

- Telegram 远程切换 `dangerFullAccess`。
- Telegram 远程扩大 `writableRoots`。
- Telegram 永久修改审批策略。
- 把用户输入拼接为 shell 命令启动 Codex。

### 10.6 审批请求

App Server 会通过服务器请求发起命令执行、文件变更和额外权限审批，例如：

- `item/commandExecution/requestApproval`
- `item/fileChange/requestApproval`
- `item/permissions/requestApproval`

客户端回复决定后，服务端会通过 `serverRequest/resolved` 表示该待处理请求已被回答或清理。[App Server：Approvals](https://learn.chatgpt.com/docs/app-server#approvals)

Telegram 展示：

```text
Codex 请求执行命令

命令：npm test
目录：/Users/.../project
原因：验证修改结果

[允许一次] [拒绝] [终止任务]
```

审批记录：

- nonce 使用安全随机值，只在 Telegram callback_data 放 `动作 + nonce`。
- nonce 绑定 job、App Server request ID、用户身份、conversation 和过期时间。
- 第一次有效点击使用 SQLite 条件更新原子消费。
- 过期、重复、其他用户或其他会话的回调一律拒绝。
- App Server 重启后，所有未决审批标记失效；不能把旧 RPC ID 发给新进程。
- 第一版只允许“本次允许”，不暴露会话级或永久允许。

### 10.7 协议版本和 Schema

App Server 可以生成与当前 Codex 版本精确匹配的 TypeScript 或 JSON Schema：

```bash
codex app-server generate-json-schema --out ./schemas/codex
```

因此构建和兼容性测试应记录 Codex 版本并生成 schema；运行时模型对未知字段采用向前兼容策略，不认识的通知应记录摘要后忽略，而不是导致整个客户端退出。[App Server Schema 生成](https://learn.chatgpt.com/docs/app-server#generate-typescript-and-json-schema)

第一版不要启用 `experimentalApi`，除非某项不可替代能力经过单独验证。稳定字段也不应散落在业务层，应集中在 `codex/` 目录。

### 10.8 后端适配与降级

保留统一后端接口：

```python
class CodexBackend(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def new_thread(self, project: Path) -> ThreadRef: ...
    async def resume_thread(self, thread_id: str) -> ThreadRef: ...
    async def start_turn(self, thread_id: str, text: str) -> TurnRef: ...
    async def steer_turn(self, turn_id: str, text: str) -> None: ...
    async def interrupt_turn(self, turn_id: str) -> None: ...
```

实现优先级：

- `AppServerBackend`：默认核心后端，支持双向 server request、Telegram 审批、流式事件、steer 和 interrupt。
- `ExecBackend`：最小降级与诊断后端，基于 `codex exec` / `codex exec resume`。

降级不是静默切换：App Server 失败时 UI 和 Telegram 都应明确说明已进入兼容模式；不支持的审批、steer 等能力应关闭或返回清晰提示。

## 11. Core 运行机制

### 11.1 应用启动

```text
获取单实例锁
  → 加载非敏感配置
  → 从 Keychain 读取 Bot Token
  → 打开 SQLite 并执行迁移
  → 恢复 pending inbox/jobs/outbox
  → 定位并检查 Codex CLI
  → 启动 App Server 与初始化握手
  → Telegram getMe / webhook 检查
  → 开始 Long Polling 和 Worker
  → 菜单栏显示已就绪
```

任何可恢复组件失败都不应让 UI 直接退出。状态应区分：

```text
disabled / starting / ready / degraded / reconnecting / failed
```

### 11.2 路由

Connector 只负责消息标准化，命令解析位于 Core：

```text
IncomingMessage
  → AuthorizationService
  → CommandRouter
      ├── /start
      ├── /help
      ├── /new
      ├── /models
      ├── /model <编号或名称>
      ├── /reasoning <强度>
      ├── /status
      ├── /stop
      ├── /recent
      ├── /take <n>
      └── /steer <text>
  → 普通文本则创建 Job
```

第一版不提供 `/shell`、`/danger`、`/allowall` 或远程修改项目目录命令。

### 11.3 Job 状态机

```text
queued
  ↓
starting
  ↓
running
  ├── waiting_approval ──→ running
  ├── completed
  ├── failed
  ├── interrupted
  └── abandoned
```

所有状态转换通过仓储方法完成，并校验允许的前置状态。例如只有 `queued` 才能变成 `starting`，只有 `running/waiting_approval` 才能变成 `interrupted`。

### 11.4 进程监督

App Server 使用：

```python
await asyncio.create_subprocess_exec(
    codex_path,
    "app-server",
    stdin=asyncio.subprocess.PIPE,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    start_new_session=True,
)
```

要求：

- 永不使用 `shell=True`。
- stdout 逐行解析 JSONL；stderr 单独持续排空并做脱敏。
- 单一 writer task 或 write lock 保证一条 JSON 消息不会交错。
- 请求有超时；进程退出后，所有 pending Future 以明确异常结束。
- 正常关闭先尝试协议/EOF 和等待，随后向进程组发送 SIGTERM，宽限期后才 SIGKILL。
- 异常退出使用有上限的指数退避；连续快速崩溃进入 `failed`，避免无限重启风暴。
- 崩溃时正在运行的 turn 标记 `interrupted`，不能未经用户确认就自动重复执行潜在副作用任务。

## 12. 数据持久化

### 12.1 数据目录

```text
~/Library/Application Support/CodexRelay/
├── codexrelay.db
├── settings.toml
├── instance.lock
└── diagnostics/

~/Library/Logs/CodexRelay/
└── codexrelay.log
```

Bot Token 不进入以上任何文件。

### 12.2 核心表

```sql
connector_accounts(
  id, connector_type, account_id, enabled, settings_json
)

connector_cursors(
  connector_type, account_id, cursor_name, cursor_value, updated_at
)

inbound_events(
  id, connector_type, account_id, external_event_id,
  payload_json, status, received_at, processed_at, error_message
)

local_users(
  id, display_name, enabled, created_at
)

external_identities(
  id, local_user_id, connector_type, account_id,
  external_user_id, external_conversation_id, paired_at, enabled
)

projects(
  id, name, path, enabled, created_at
)

conversations(
  id, local_user_id, project_id, codex_thread_id,
  title, status, last_message_id, model, reasoning_effort,
  created_at, updated_at
)

conversation_bindings(
  conversation_id, connector_type, account_id,
  external_conversation_id
)

conversation_messages(
  id, conversation_id, job_id, role, content_text, content_hash,
  codex_item_id, created_at
)

context_checkpoints(
  id, conversation_id, codex_thread_id, through_message_id,
  summary_text, created_at
)

jobs(
  id, conversation_id, inbound_event_id, codex_turn_id,
  input_message_id, output_message_id, status,
  created_at, started_at, finished_at, error_message
)

approval_requests(
  id, job_id, rpc_request_id, nonce_hash, approval_type,
  summary_json, status, expires_at, resolved_at
)

outbound_messages(
  id, connector_type, account_id, external_conversation_id,
  message_type, payload_json, status, attempt_count,
  external_message_id, next_retry_at, delivered_at
)
```

Telegram 专属 offset 放进通用 `connector_cursors`；`conversations` 中不出现 `telegram_chat_id`。这就是“只做 Telegram，但为其他渠道留口子”的具体体现。

`conversation_messages` 是 CodexRelay 的**规范会话记录**，只保存上下文恢复真正需要的用户正文和 Codex 最终回复，不保存每个流式 delta、命令 stdout 或重复的 Telegram 封装。`jobs` 通过 ID 引用规范消息，避免 prompt 和结果在多张表中重复占用空间。

### 12.3 隐私与日志

默认不写入日志、诊断包或通用错误字段：

- Bot Token 或含 Token 的 Telegram API URL。
- Codex 登录凭据。
- 用户消息全文、Codex 回复全文、文件内容和命令完整输出。

默认记录：

- 事件 ID、任务 ID、状态转换和耗时。
- HTTP 状态码、重试次数和脱敏异常类型。
- Codex 版本、进程退出码和协议方法名。

为了保证会话恢复，用户正文和 Codex 最终回复会作为规范消息保存在本机 SQLite，但它们不得进入运行日志。程序 UI 应明确说明这一点；用户显式删除会话时，应连同规范消息和检查点一起删除。数据库继承当前 macOS 账户的文件权限；如果用户需要静态数据加密，应启用 FileVault，而不宣称普通 SQLite 本身已加密。

诊断导出必须先经过同一套 redaction pipeline，并在 UI 中展示将导出的文件清单。

### 12.4 上下文连续性与崩溃恢复

上下文不能依赖 Telegram 的 Update 保留或 Outbox 是否还在。正确的数据分层是：

```text
Telegram 原始包 / Outbox  = 可清理的传输数据
conversation_messages     = 本地可审计的规范会话
codex_thread_id           = Codex 会话的恢复键
context_checkpoints       = Codex thread 不可用时的恢复辅助
项目工作区                   = 文件修改的最终事实来源
```

每轮任务使用以下持久化顺序：

1. 接收 Update 时，在同一事务内写入 `inbound_events` 并推进 cursor。
2. 调用 Codex 之前，先把标准化后的用户正文写入 `conversation_messages`，再创建 Job。
3. 创建新 Codex thread 后，先持久化 `codex_thread_id`，再启动 turn。
4. turn 完成时，先持久化 Codex 最终回复并将 Job 标记为 `completed`，再写入 Outbox。
5. Outbox 发送后即使程序崩溃，也能从规范回复重建发送任务，不需要重新执行 Codex turn。

项目连续性规则：

- `app_state.current_project_id` 只表示下一轮任务所选项目。
- 每个项目最多保留一个活动 Conversation；其 `codex_thread_id` 独立持久化。
- `/use` 只切换当前项目指针，不归档、不覆盖其他项目的活动 Conversation。
- 切回某项目后，下一轮使用该项目原 `codex_thread_id` 调用 resume，从而保持上下文不断节。
- `model` 与 `reasoning_effort` 属于活动 Conversation；切回项目时与 thread 一起恢复。
- 修改模型只更新 CodexRelay SQLite，并在下一次 thread/turn 调用中作为参数传入；绝不写入
  `~/.codex/config.toml`，不会改变 Codex 桌面端或 CLI 的全局默认设置。
- 若新模型不支持原推理强度，自动回落到该模型由 App Server 报告的默认强度。
- `/new` 创建新 Conversation 时继承该项目上一活动 Conversation 的模型设置，但不继承 thread。
- 为避免项目归属竞态，任务执行与 Telegram 项目切换共用全局执行锁，数据库事务再校验不存在
  `starting`、`running` 或 `waiting_approval` Job。
- 模型与推理强度修改也受同一执行锁和数据库事务约束，任务运行期间拒绝修改。

程序重启后按如下规则对账：

- `queued`：可安全启动，因为尚未调用 Codex。
- `starting/running/waiting_approval`：先根据 `codex_thread_id` / `codex_turn_id` 查询或恢复；状态无法确认时标记 `interrupted`，禁止自动重放。
- `completed` 但未送达：从 `conversation_messages` 重建 Outbox，不重新运行任务。
- Codex thread 仍存在：使用 `codex_thread_id` 恢复，这是正常路径。
- Codex thread 已不可用：新建 thread，使用最新 `context_checkpoint` 加其后的规范消息生成明确的“恢复交接”；原始规范消息仍保留，便于审计和重建。

`context_checkpoints` 只能在 turn 已成功完成后生成，并且不能用来删除规范消息。检查点是加速恢复的索引，不是唯一副本。

保证边界必须如实表达：

- 可保证在 Telegram 断网、CodexRelay 崩溃、Mac 重启和清理传输缓存后，已提交的规范消息和对话映射不丢失。
- 不得承诺在磁盘损坏、用户手动删除数据、Codex 历史与 Relay 数据库同时丢失后仍可恢复。这类需要系统备份，例如 Time Machine。
- 当会话长度超过模型的有限上下文窗口时，无法保证模型字节级记住所有历史；这与 Codex 自身的压缩/摘要边界一致。Relay 能保证原始规范会话可查、可重建，但不宣称能超越模型上下文上限。

### 12.5 数据保留、容量与空间回收

默认保留规则：

| 数据 | 默认策略 | 原因 |
|---|---|---|
| 规范用户消息与 Codex 最终回复 | 跟随会话生命周期，只在用户显式删除会话时清理 | 上下文审计和灾难恢复副本 |
| `codex_thread_id`、Job 状态和消息哈希 | 跟随会话生命周期 | 恢复键和去重依据 |
| 已处理的 Telegram 原始 `payload_json` | 7 天后置空正文，保留事件 ID 和状态 | 去重不依赖原始包 |
| 已送达 Outbox `payload_json` | 7 天后置空正文，保留送达元数据 | 规范回复已在会话表中 |
| 失败的 Inbox/Outbox 详情 | 30 天 | 留出排障窗口 |
| 已解决审批详情 | 30 天，后仅保留结果元数据 | 降低敏感命令长期暴露 |
| 运行日志 | 单文件 10 MB，最多 5 个 | 上限约 50 MB |
| 诊断包 | 7 天后清理 | 防止手动导出无限累积 |
| 临时附件与中间文件 | Job 结束后清理，失败时最多保留 24 小时 | 附件是最容易失控的空间来源 |

文本为主的粗略容量预算：

| 使用方式 | 每轮规范会话估算 | 每天 100 轮时的年增长 |
|---|---:|---:|
| 短问答 | 5–20 KB | 约 0.2–0.7 GB |
| 普通编程交互 | 20–50 KB | 约 0.7–1.8 GB |
| 经常包含长代码/长输出 | 50–200 KB | 约 1.8–7 GB |

这个预算是长期保留规范会话的上下文安全优先方案，不将 Codex 自身的历史目录、项目文件或 Git 仓库体积计入 CodexRelay。普通个人用量通常显著低于每天 100 轮；空间不足时应由用户显式导出/删除旧会话，不得静默删除规范会话。

SQLite 维护策略：

- 建库时启用 `WAL`、`foreign_keys=ON` 和 `auto_vacuum=INCREMENTAL`。
- 定期执行 WAL checkpoint、`PRAGMA optimize` 和有上限的 `incremental_vacuum`，避免一次维护长时间卡顿。
- 在 UI 显示数据库、日志、诊断包和临时文件的分项体积。
- 数据库超过 500 MB 时只提醒用户检查旧会话，不自动删除；后续可增加可恢复的压缩归档，但不列入第一版。

## 13. macOS 应用形态

### 13.1 线程与事件循环

推荐：

```text
主线程：QApplication / 菜单栏 / 设置窗口
后台服务线程：独立 asyncio event loop
通信：Qt Signal + 线程安全命令队列
```

Qt UI 只能在主线程操作；Telegram、SQLite、Codex 和重试逻辑都在后台 asyncio loop 中。关闭设置窗口只隐藏窗口，菜单栏“退出”才执行完整停机。

### 13.2 菜单栏

```text
Telegram：已连接 / 重连中 / 配置错误
Codex：就绪 / 运行中 / 等待审批 / 兼容模式
当前项目：...
打开设置
重启连接
打开日志
导出诊断
退出
```

### 13.3 设置窗口

第一版界面只展示真实存在的 Telegram 功能：

- **Telegram**：Token、验证、Bot 用户名、配对码、授权用户、解绑。
- **Codex**：CLI 路径、版本、登录/后端状态、重启后端。
- **项目**：项目名称、目录、当前项目、权限说明，以及各项目独立的当前 thread。
- **系统**：登录启动、自动连接、日志级别、数据目录和诊断导出。

不展示企业微信、钉钉等“未来支持”占位项。

### 13.4 Keychain

Telegram Bot Token 属于小型秘密，应保存在 macOS Keychain；Apple 将 Keychain 描述为存储密码、密钥等小型敏感数据的加密数据库。[Apple Keychain Services](https://developer.apple.com/documentation/security/keychain-services)

建议键：

```text
service = com.cwwen.codexrelay.connector.telegram
account = main-bot
```

Python 层使用 `keyring` 访问 Keychain。Token 不应放入 TOML、SQLite、环境变量、命令行参数或日志。

### 13.5 单实例与登录启动

单实例使用 `fcntl.flock()` 锁定明确的 `instance.lock`。第二个实例不得启动另一个 Poller，因为同一个 Bot 并发 `getUpdates` 会破坏游标和消费顺序。

最低系统建议 macOS 13。Apple 官方在 macOS 13 及以后使用 `SMAppService` 注册和控制 Login Items、LaunchAgents 与 LaunchDaemons。[Apple SMAppService](https://developer.apple.com/documentation/servicemanagement/smappservice)

Python 实现需先做一个 PyObjC + PyInstaller 的兼容性验证：

- 通过 PyObjC 调用 `SMAppService` 能稳定注册时，采用系统 Login Item。
- 若打包或签名验证不通过，MVP 可暂用用户可见、可卸载的 LaunchAgent，但要把它封装在 `StartupService` 后面，不让 Core 依赖具体机制。

## 14. 配置示例

非敏感配置可以使用 TOML：

```toml
[app]
auto_connect = true
launch_at_login = false

[codex]
backend = "app-server"
fallback_backend = "exec"
cli_path = "auto"

[[projects]]
id = "main"
name = "Main Project"
path = "/Users/cwwen/Documents/example"
network_access = false

[[connectors]]
type = "telegram"
account_id = "main-bot"
enabled = true
private_chat_only = true
```

不要在配置文件设计 `bot_token` 字段。

## 15. 项目目录建议

```text
CodexRelay/
├── pyproject.toml
├── uv.lock
├── README.md
├── src/
│   └── codexrelay/
│       ├── __main__.py
│       ├── app.py
│       ├── config.py
│       ├── lifecycle.py
│       ├── connectors/
│       │   ├── base.py
│       │   ├── models.py
│       │   ├── registry.py
│       │   └── telegram/
│       │       ├── api.py
│       │       ├── connector.py
│       │       ├── models.py
│       │       ├── poller.py
│       │       ├── presenter.py
│       │       ├── callbacks.py
│       │       └── chunker.py
│       ├── core/
│       │   ├── authorization.py
│       │   ├── pairing.py
│       │   ├── message_router.py
│       │   ├── command_router.py
│       │   ├── conversations.py
│       │   ├── jobs.py
│       │   ├── approvals.py
│       │   └── interaction_service.py
│       ├── codex/
│       │   ├── backend.py
│       │   ├── app_server.py
│       │   ├── exec_backend.py
│       │   ├── jsonrpc.py
│       │   ├── locator.py
│       │   ├── models.py
│       │   └── supervisor.py
│       ├── storage/
│       │   ├── database.py
│       │   ├── migrations.py
│       │   └── repositories.py
│       ├── security/
│       │   ├── keychain.py
│       │   ├── redaction.py
│       │   └── tokens.py
│       ├── ui/
│       │   ├── tray.py
│       │   ├── settings_window.py
│       │   ├── status_model.py
│       │   └── signals.py
│       └── macos/
│           ├── paths.py
│           ├── single_instance.py
│           └── startup.py
├── schemas/
│   └── codex/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── fixtures/
│   ├── fake_connector.py
│   └── fake_codex_server.py
├── packaging/
│   ├── CodexRelay.spec
│   ├── Info.plist
│   └── entitlements.plist
└── scripts/
    ├── build.sh
    ├── test.sh
    ├── sign.sh
    └── notarize.sh
```

`FakeConnector` 是架构测试的关键：如果 Core 测试完全不用 Telegram 类型仍能跑通消息、任务和审批流程，就证明预留的渠道边界真实有效。

## 16. 测试计划

### 16.1 Telegram

- offset 递增和事务提交。
- 重复 Update 去重。
- 轮询超时、断网、5xx、429 `retry_after`。
- Token 无效与 webhook 冲突。
- 未授权用户、非私聊、第二用户配对。
- 配对码过期、重用和暴力尝试限制。
- Callback nonce 重放、过期、跨用户和跨会话。
- 4096 字符边界、Unicode、长代码块和超长结果文件。

### 16.2 Codex

`fake_codex_server.py` 模拟：

- initialize / initialized。
- thread/start / resume。
- turn/start、delta、completed、failed、interrupt。
- 命令、文件和权限审批。
- 非法 JSONL、未知通知和请求乱序。
- App Server 启动失败、崩溃、卡死和 stderr 洪泛。
- pending RPC 在进程退出后正确失败。
- schema 兼容性测试。

### 16.3 Core 与数据库

- Job 合法/非法状态转换。
- Inbox 与 cursor 原子性。
- Outbox 重试与已送达恢复窗口。
- 同一 conversation 串行、不同 conversation 可并行的基础设计。
- 审批原子消费。
- migration 升级与失败回滚。
- 应用重启后 pending/processing 状态修复。
- 规范用户消息在调用 Codex 之前已持久化。
- 已完成但未送达的回复可从 `conversation_messages` 重建 Outbox，且不重放 turn。
- 清理过期 Inbox/Outbox 正文后，去重、会话恢复和审计仍然有效。
- Codex thread 缺失时，可使用检查点和其后的规范消息生成恢复交接。
- 保留期任务、WAL checkpoint 和 incremental vacuum 在大数据 fixture 下不阻塞正常消息处理。
- FakeConnector 端到端流程。

### 16.4 macOS

- 单实例锁。
- Keychain 保存、读取、替换和删除。
- 设置窗口关闭后后台继续运行。
- 正常退出关闭 Poller 和 Codex 进程组。
- Login Item 注册/取消。
- PyInstaller `.app` 在干净用户账户启动。
- 签名、公证和系统重启后的自动启动。

## 17. 打包与发布

推荐 PyInstaller `onedir + windowed`，不使用 `onefile`：

- Qt Framework 打包和签名更稳定。
- 无需每次启动解压运行时。
- 启动更快，诊断更直接。

Codex CLI 不打入 `.app`。应用启动时检测候选路径、执行版本检查和 App Server 能力探测；用户也可以在设置中手动选择路径。

发布步骤：

```text
uv sync --frozen
  → ruff / mypy / pytest
  → 生成并验证 Codex schema
  → PyInstaller 构建 CodexRelay.app
  → Developer ID 签名
  → notarytool 公证
  → stapler 附加票据
  → ZIP 分发与干净环境验证
```

第一版优先 Apple Silicon arm64。Intel 包应在真实 x86_64 构建环境单独产出，不在没有验证前承诺 universal2。

## 18. 实施阶段与验收标准

### 阶段 0：风险验证

- 验证本机 `codex exec`、`codex exec resume`，以及 App Server 的 schema、握手、审批和 interrupt。
- 验证 PySide6 + asyncio 后台线程模型。
- 验证 Keychain 和 `SMAppService` 的 PyObjC/PyInstaller 兼容性。

验收：三个最小实验均有自动化或可重复脚本；风险结论写入 ADR。

### 阶段 1：基础工程

- 创建同级 `CodexRelay` 项目。
- uv、src layout、Ruff、mypy、pytest。
- 配置、路径、日志、SQLite migration、Keychain、单实例。
- 定义 Connector、CodexBackend 和 Core 模型。

验收：FakeConnector 与 FakeCodexBackend 可完成一次持久化 Job。

### 阶段 2：Telegram 最小闭环

- Token 验证、Long Polling、Inbox/cursor。
- 一次性配对与单用户私聊白名单。
- Outbox、文本分段、`/start`、`/help`、`/status`。

验收：断网和进程重启后 Update 不丢失、不重复创建 Job。

### 阶段 3：ExecBackend 最小闭环

- CLI 定位和版本检查。
- 实现 `codex exec`、`codex exec resume`、取消和最终回复最小路径。
- 将 Exec 能力封装在 `CodexBackend` 接口后，Core 不直接调用命令行。
- `/new`、`/stop`。

验收：手机可驱动测试项目完成一次修改并收到最终结果；新会话、继续会话、取消和错误反馈均可重复验证。

### 阶段 4：App Server 主后端

- JSON-RPC、握手、thread/turn、流式事件。
- resume、interrupt、steer、schema 兼容检查。
- 子进程监督与重启恢复。

验收：连续对话、流式状态、中断和 App Server 崩溃均有可重复测试。

### 阶段 5：审批与安全

- 命令、文件和权限审批。
- Telegram Inline Keyboard、nonce、超时和重放防护。
- `workspaceWrite`、项目根目录和网络默认关闭。

验收：未授权用户、过期回调、重复点击和跨会话点击无法批准；危险能力不能从 Telegram 开启。

### 阶段 6：macOS 应用体验

- 菜单栏、设置窗口、状态展示。
- 登录启动、打开日志、导出诊断。
- 正常停机和 UI 错误提示。

验收：非开发环境中可完成配置、配对、运行、审批和退出。

### 阶段 7：打包发布

- PyInstaller、图标、签名、公证和 ZIP。
- 干净账户安装测试和升级测试。

验收：用户不安装 Python 也能启动，但需要已安装且已登录的 Codex CLI。

## 19. 关键风险与处理

| 风险 | 后果 | 处理 |
|---|---|---|
| App Server 协议随 Codex 版本变化 | 客户端解析失败 | 版本检测、生成 schema、集中协议层、未知通知容忍、Exec 降级 |
| Telegram 重复 Update | 重复执行副作用任务 | Inbox 唯一约束和 cursor 同事务提交 |
| Outbox 发送成功后崩溃 | 手机收到重复回复 | 记录 message_id、有限重试、恢复提示；承认无法完全 exactly-once |
| Bot Token 泄露 | 未授权访问消息入口 | Keychain、日志脱敏、Token 不进配置/URL 日志 |
| 远程审批被重放 | 危险命令获批 | nonce 哈希、短过期、身份/会话绑定、SQLite 原子消费 |
| Codex 子进程挂死 | 队列永久阻塞 | 请求超时、心跳/活动时间、SIGTERM→SIGKILL、熔断 |
| 自动重放中断任务 | 重复文件/命令副作用 | 崩溃中的 Job 标为 interrupted，由用户显式继续 |
| 清理传输数据导致上下文断节 | 重启后无法继续对话 | 传输表与规范会话分层；保留 thread ID、规范消息和检查点 |
| 规范会话长期增长 | 数据库占用持续上升 | 不存 delta/重复 payload，轮转日志，显示分项体积，超阈值提醒但不静默删除 |
| Python 打包与 macOS 系统 API | Login Item 或签名失败 | 阶段 0 独立验证；StartupService 封装备用实现 |
| PySide6 主线程被阻塞 | 菜单栏无响应 | Qt 主线程只做 UI，asyncio 独立服务线程 |

## 20. 需要在落地前确认的默认决策

建议按以下默认值进入实现：

1. 名称：`CodexRelay`。
2. 平台：macOS 13+，第一版 Apple Silicon。
3. 语言：Python 3.12，uv 管理依赖。
4. UI：PySide6 菜单栏应用和设置窗口。
5. 渠道：只实现一个 Telegram Bot、一个配对用户、仅私聊。
6. 接收方式：Long Polling。
7. 项目：第一版支持多个 Mac 端授权项目；空闲时可按稳定编号或名称切换，每个项目独立
   保存 Codex thread；全局保持单任务，运行或等待审批期间禁止切换。
8. Codex：先用 ExecBackend 跑通闭环；App Server 双向 JSON-RPC 验收后成为完整主后端；Exec 保留为明确可见的 fallback。
9. 权限：`workspaceWrite`，可写根仅当前项目，网络默认关闭。
10. 审批：Telegram 按钮逐次批准，不提供永久允许。
11. Token：只存 macOS Keychain。
12. 状态：SQLite 持久化 Inbox/Outbox/Job，并独立保存规范会话、Codex thread ID 和恢复检查点。
13. 保留：规范会话跟随会话生命周期；传输正文 7 天、失败详情和审批详情 30 天、日志上限约 50 MB。
14. 清理：不静默删除规范会话；仅自动清理可重建的传输数据、日志、诊断包和临时文件。
15. 其他渠道：只预留 Connector 边界，不实现、不展示。

## 21. 结论

Python 可以完整承担 CodexRelay 的主体功能，并且在异步网络、JSON 协议、任务协调和可测试性方面很合适。相较 Swift，代价是应用体积更大、部分 macOS 系统能力需要 PyObjC 或包装层；换来的好处是实现速度、可读性和后续维护成本更符合当前需求。

方案的核心取舍是：

- **产品只做 Telegram，架构不绑定 Telegram。**
- **先用 ExecBackend 降低首个闭环风险，再用 App Server 双向 JSON-RPC 提供完整审批与流式能力。**
- **先保证可靠性和安全边界，再增加交互花样。**
- **未来新增渠道时新增 Connector，不改 Codex Core。**

在上述默认决策确认后，即可进入阶段 0 风险验证和项目脚手架实现。
