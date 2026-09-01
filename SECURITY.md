# Security Policy / 安全策略

## Supported versions / 支持范围

The latest `main` branch and the latest published release are supported. CodexRelay is a Public Alpha project; do not use an unaudited build for high-risk or production workloads.

当前支持最新的 `main` 分支和最新公开 Release。CodexRelay 目前是公开 Alpha 项目，不建议将未经审计的构建用于高风险或生产环境。

## Reporting a vulnerability / 报告安全问题

Do not publish Bot Tokens, Codex credentials, private code, exploitable details, or complete private logs in a public Issue. Use [GitHub private vulnerability reporting](https://github.com/cwwcn/CodexRelay/security/advisories/new) when available. If it is unavailable, open an Issue containing only the minimum non-sensitive summary and ask for a private contact channel.

请不要在公开 Issue 中发布 Bot Token、Codex 凭据、私人代码、可利用细节或完整私有日志。优先使用 [GitHub 私密漏洞报告](https://github.com/cwwcn/CodexRelay/security/advisories/new)；如果暂时不可用，请只提交不包含敏感细节的最小摘要，并请求私下联系。

Include the affected version, macOS version and architecture, reproduction steps, impact, and any suggested mitigation.

报告时请尽量提供：受影响版本、macOS 版本与架构、复现步骤、影响范围和修复建议。

## Handling principles / 处理原则

- We will not ask you to disclose a Bot Token or Codex credential.
- Unfixed exploitable details will not be published before a fix is available.
- Fixes and their impact will be recorded in `CHANGELOG.md`.

- 不会要求你公开 Bot Token 或 Codex 凭据；
- 修复前不会公开未修复的利用细节；
- 修复及其影响范围会记录在 `CHANGELOG.md` 中。
