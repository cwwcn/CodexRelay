# Changelog / 更新日志

## Unreleased / 未发布

Changes will be recorded here. / 后续变更将在这里记录。

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
