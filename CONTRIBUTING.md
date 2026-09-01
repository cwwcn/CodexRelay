# Contributing to CodexRelay

CodexRelay is a Public Alpha project. Issues, design feedback, documentation improvements, and focused pull requests are welcome. Please read the [security policy](SECURITY.md) before reporting a security concern.

CodexRelay 目前是公开 Alpha 项目，欢迎通过 Issue 反馈问题、提出设计建议、改进文档或提交聚焦的 Pull Request。提交安全问题前，请先阅读[安全策略](SECURITY.md)。

## Development setup / 开发环境

```bash
uv sync --extra dev --extra gui
```

## Checks before submitting / 提交前检查

```bash
uv run ruff check .
uv run mypy --strict src
QT_QPA_PLATFORM=offscreen uv run pytest -q
```

For UI changes, include the macOS version, machine architecture, screenshots when useful, and reproduction steps. Runtime and persistence changes should include corresponding tests.

涉及 UI 的修改，请说明 macOS 版本、机器架构、必要时附截图和复现步骤；涉及运行时或持久化的修改，请补充相应测试。

## Pull request guidelines / Pull Request 建议

- Keep one pull request focused on one topic;
- never commit tokens, logs, databases, `.app` bundles, or personal machine paths;
- describe behavior changes, compatibility impact, and verification performed;
- never modify a user's global `~/.codex/config.toml`;
- discuss breaking changes in an Issue before implementing them.

- 一个 PR 尽量只解决一个主题；
- 不要提交 Token、日志、数据库、`.app` 或本机路径；
- 描述行为变化、兼容性影响和验证方式；
- 不要修改用户全局 `~/.codex/config.toml`；
- 破坏性变更请先通过 Issue 讨论。
