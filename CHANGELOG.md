# Changelog / 更新日志

## Unreleased / 未发布

Changes for the next release will be recorded here. / 下一版本的变更将在此记录。

### Local lifecycle and offline recovery / 本地生命周期与离线恢复

- Added persisted online, recovering, and offline runtime states with heartbeat timestamps;
- Added best-effort Telegram notices for startup, sleep, wake, and explicit shutdown;
- Added macOS workspace sleep/wake/power-off observation with elapsed-time wake detection fallback;
- Treated Telegram transport interruptions longer than 30 seconds as offline periods and restored the online boundary before dispatching queued updates;
- Deferred ordinary Telegram tasks received while the Mac was offline until the user chooses “现在执行” or “忽略”;
- Kept status and help commands available without replaying stale task side effects;
- Added lifecycle notification settings and persistent recovery records for future diagnostics.

- 增加在线、恢复中和离线状态及心跳时间的持久化记录；
- 在启动、睡眠、唤醒和显式退出时尽力通过 Telegram 通知；
- 接入 macOS 睡眠、唤醒和关机事件，并用时间间隔检测作为兜底；
- 将超过 30 秒的 Telegram 传输中断纳入离线状态，并在分发积压消息前先恢复在线边界；
- Mac 离线期间收到的普通 Telegram 任务延迟到用户选择“现在执行”或“忽略”后处理；
- `/status` 等状态命令仍可用，不会静默重放旧任务的副作用；
- 增加生命周期通知设置和可供后续诊断中心使用的持久化恢复记录。

## 0.1.3 - 2026-09-05

### Global session view / 全局会话视图

- Added a global Codex session index with project, unassigned, unavailable-path, and recoverable archival states;
- Made `/sessions` the global conversation view; `/sessions all` remains accepted as a compatibility spelling;
- Added a dedicated Mac Sessions page with filters, on-demand synchronization, explicit assignment to an authorized project, and safe session activation;
- Unassigned sessions can be selected and executed directly in controlled safe mode; project association remains explicit and optional.
- Moved project management into the Mac System page and removed project selection from Telegram's primary command menu; `/projects` and `/use` remain compatibility commands.
- Preserved one canonical conversation row when an unassigned session is explicitly assigned to a project, so its history and per-session model settings remain continuous.
- Enforced the global single-task slot at the persistence boundary, including queued work, and made newly created Codex threads appear in the global session view immediately.
- Redesigned the System page as a compact macOS-style settings view, replacing the oversized project list with a readable project selector and independent separators.
- Changed long session titles from seamless marquee scrolling to round-based playback: scroll to the end, pause, then restart from the beginning.

- 增加全局 Codex 会话索引，区分项目、未归属、路径不可用和可恢复归档状态；
- Telegram 将 `/sessions` 统一为全局会话视图，同时兼容接受 `/sessions all`；
- Mac App 增加独立“会话”页面，支持筛选、即时同步、显式归属到已授权项目和安全切换会话；
- 无项目会话可以直接选择和执行，风险权限仍通过受控安全模式审批；项目归属保持显式且可选。
- 将项目管理收归 Mac 端“系统”页面，并从 Telegram 原生命令菜单移除项目选择；`/projects` 和 `/use` 作为兼容命令保留。
- 无项目会话明确归属项目时复用同一条会话记录，保留历史消息以及每会话模型设置，避免上下文分叉。
- 在持久化边界统一执行全局单任务限制（包括排队状态），新创建的 Codex thread 会立即出现在全局会话视图中。
- 将“系统”页面重构为紧凑的 macOS 设置风格，用清晰的项目选择器替代过大的项目列表，并将分隔线独立处理，避免与按钮重叠。
- 将超长会话标题从首尾无缝滚动改为分轮播放：滚动到末尾后暂停，再从开头重新播放。

## 0.1.2 - 2026-09-04

Maintenance release focused on reliable session continuity, Codex reconciliation, and a more predictable Telegram command experience. / 聚焦会话连续性、Codex 会话校准和 Telegram 命令体验的维护版本。

- Reconciled all locally bound conversations against Codex `thread/list`, including pagination and recoverable archiving when a thread is deleted or archived in Codex;
- Automatically reselected the first valid conversation when the current conversation disappears, while preserving local history for recovery;
- Added startup and periodic (60-second) session reconciliation, with safeguards that skip reconciliation while a task is active;
- Made Telegram command parsing case-insensitive, tolerant of `@bot` suffixes and extra whitespace, and explicit about unknown slash commands;
- Added `/approval` and `/effort` compatibility aliases, clarified `/release` and `/takeover`, and prevented repeated `/pair` commands from becoming Codex tasks;
- Removed persistent manual hand-off semantics: Telegram task leases are short-lived and released after completion, failure, or interruption so the Mac client can continue naturally;
- Expanded regression coverage for runtime synchronization, deleted/restored conversations, active-task safeguards, and Telegram command routing.

- 按 Codex `thread/list` 全量校准本地绑定会话，支持分页；Codex 中删除或归档的会话会可恢复地归档隐藏；
- 当前会话失效时自动切换到第一条有效会话，同时保留本地历史以便恢复；
- 增加启动时和每 60 秒一次的后台会话校准，任务运行期间自动跳过，避免误处理；
- Telegram 命令支持大小写、`@bot` 后缀和多余空白，未识别的斜杠命令会明确提示；
- 增加 `/approval`、`/effort` 兼容别名，明确 `/release`、`/takeover` 的含义，并避免重复 `/pair` 被误当成 Codex 任务；
- 移除持久化手动交接语义：Telegram 任务在完成、失败或中断后释放短期租约，电脑端可以自然继续；
- 增加运行时同步、会话删除/恢复、活动任务保护和 Telegram 命令路由的回归测试。

## 0.1.1 - 2026-09-02

Maintenance release focused on stability, project discovery, approvals, and Telegram task feedback. / 聚焦稳定性、项目扫描、审批和 Telegram 任务反馈的维护版本。

- Added Telegram task progress updates and clearer approval outcomes;
- Reconciled scanned projects with the current filesystem while preserving recoverable database records;
- Added project-access preflight checks before startup and task execution;
- Hardened Codex thread/task persistence and recovery behavior;
- Improved pairing, project status, and menu-bar error feedback;
- Expanded regression coverage to 91 tests.

- 增加 Telegram 任务进度更新，并明确反馈审批结果；
- 项目扫描与当前文件系统同步，同时保留可恢复的数据库记录；
- 在应用启动和任务执行前增加项目访问预检；
- 加强 Codex thread/任务持久化与恢复行为；
- 改进配对、项目状态和菜单栏错误提示；
- 回归测试扩展至 91 项。

## 0.1.0 - 2026-09-01

First public Alpha release. / 首个公开 Alpha 版本。

- Redesigned the macOS menu bar overview panel.
- Added hierarchy for project, session configuration, and task state.
- Added distinct connection colors and task-state indicators.
- Added Telegram's native command menu and `/help` output.
- Added the GitHub Release update-checking flow; automatic checks are opt-in.
- Refined macOS keyboard shortcuts, quit confirmation, and task interruption semantics.
- Added architecture-specific DMG packages for Apple Silicon and Intel Macs.

- 重构 macOS 菜单栏概览面板；
- 增加项目、会话配置和任务状态的分层展示；
- 增加连接状态颜色和独立任务状态圆点；
- 支持 Telegram 原生命令菜单和 `/help` 命令；
- 增加 GitHub Release 更新检查流程，自动检查默认关闭；
- 完善 macOS 快捷键、退出确认和任务中断语义；
- 提供 Apple Silicon 和 Intel 架构对应的 DMG 安装包。
