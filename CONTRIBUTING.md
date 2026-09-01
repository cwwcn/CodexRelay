# Contributing to CodexRelay

感谢关注 CodexRelay。项目目前处于 Early Preview 阶段，欢迎通过 Issue 反馈问题、提出设计建议或提交代码改进。

## 开始开发

```bash
uv sync --extra dev --extra gui
```

## 提交前检查

```bash
uv run ruff check .
uv run mypy --strict src
QT_QPA_PLATFORM=offscreen uv run pytest -q
```

涉及 UI 的修改，请同时说明 macOS 版本、机器架构和复现步骤；涉及运行时或持久化的修改，请补充相应测试。

## Pull Request 建议

- 一个 PR 尽量只解决一个主题；
- 不要提交 Token、日志、数据库、`.app` 或本机路径；
- 描述行为变化、兼容性影响和验证方式；
- 不要修改用户全局 `~/.codex/config.toml`；
- 破坏性变更请先通过 Issue 讨论。
