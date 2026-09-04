# CodexRelay macOS 产品形态整改调研

日期：2026-09-01

本文只记录与下一轮 macOS 重构直接相关的官方事实和工程建议，不代表功能已经实现。

## 1. 官方事实

### 1.1 菜单栏程序与弹层

- Apple 将 `NSStatusItem` 定义为系统菜单栏中的独立项目，并允许通过按钮或菜单响应点击。
- `NSPopover` 用于显示与现有界面元素相关联的附加内容；`transient` 行为会在用户点击外部时自动关闭，适合菜单栏概览弹层。
- 菜单应优先放置高频项目，并按职责分组，避免无边界地堆积命令。

来源：

- Apple `NSStatusItem`：https://developer.apple.com/documentation/appkit/nsstatusitem
- Apple `NSPopover`：https://developer.apple.com/documentation/appkit/nspopover
- Apple HIG Menus：https://developer.apple.com/design/human-interface-guidelines/menus

### 1.2 设置窗口与键盘命令

- Apple 建议 macOS 应用在 App 菜单中提供“设置”，并使用独立设置窗口；标准快捷键是 `Command-,`。
- 设置窗口通常按 pane 分类，且不需要最大化。关闭设置窗口不等于退出后台应用。
- Qt 提供平台标准快捷键 `Close`、`Preferences` 和 `Quit`，在 Apple 平台分别映射为 `Command-W`、`Command-,` 和 `Command-Q`。
- Qt 的原生菜单栏会按 `QAction.menuRole` 把 About、Settings 和 Quit 合并到标准 macOS App 菜单位置。

来源：

- Apple HIG Settings：https://developer.apple.com/design/human-interface-guidelines/settings
- Qt `QKeySequence`：https://doc.qt.io/qtforpython-6/PySide6/QtGui/QKeySequence.html
- Qt `QMenuBar`：https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QMenuBar.html

### 1.3 登录启动

- macOS 13 及更高版本提供 `SMAppService.mainApp` 注册主应用为登录项，并可获得系统授权状态。
- 手工写入 `~/Library/LaunchAgents` 不是现代普通应用的首选登录启动接口。

来源：

- Apple `SMAppService.mainApp`：https://developer.apple.com/documentation/servicemanagement/smappservice/mainapp
- Apple `SMAppService.register()`：https://developer.apple.com/documentation/servicemanagement/smappservice/register()

### 1.4 开源发布与自动更新

- GitHub Releases 可以按版本标签发布说明和二进制附件，适合作为公开发行入口。
- Sparkle 2 使用 appcast 描述版本，并要求递增的 `CFBundleVersion`。正式更新应通过 HTTPS、Developer ID 签名、公证和 Sparkle EdDSA 签名保护。
- Sparkle 推荐新应用使用 `SPUStandardUpdaterController`；在完成 Developer ID 签名、公证和密钥管理后，可作为后续的原地更新实现。
- Apple 对 Mac App Store 之外的正式分发建议使用 Developer ID、Hardened Runtime 和公证；当前项目使用的 ad-hoc 签名只适合本机开发验证。

来源：

- GitHub Releases：https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository
- Sparkle 文档：https://sparkle-project.org/documentation/
- Sparkle 发布更新：https://sparkle-project.org/documentation/publishing/
- Apple 公证：https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution
- Apple Developer ID：https://developer.apple.com/support/developer-id/

## 2. 当前实现审计（2026-09-04）

此前的基线问题已完成一轮整改；以下记录当前仍然存在的边界，避免研究文档与实际行为不一致：

1. 菜单栏点击显示轻量概览面板，复杂配置在独立设置窗口中完成。
2. 设置窗口已接入原生 App 菜单、`Command-W`、`Command-,` 和 `Command-Q` 退出确认。
3. 退出时先隐藏菜单栏图标，再由后台 Runtime 完成有序停止；超时会恢复运行状态并提示用户。
4. Runtime、菜单栏概览和设置窗口共享不可变状态快照；会话是主对象，项目只是可选归属。
5. `src/codexrelay/ui/app.py` 仍包含较多 UI 与生命周期代码，后续可以按模块边界渐进拆分，但不影响当前功能。
6. 版本号已经由 `src/codexrelay/version.py` 作为构建来源，PyInstaller 只读取该来源并写入 `Info.plist`。
7. 当前登录启动仍由独立 `StartupService` 封装的用户级 LaunchAgent 提供，后续可迁移到 `SMAppService`。
8. 当前主题以 macOS 浅色界面为主，深色模式和完整辅助功能覆盖仍需在后续 UI 专项中继续增强。
9. 当前发布包为 arm64 / Intel 架构的 ad-hoc 签名包；Developer ID、公证和正式更新签名仍属于发行增强项。

## 3. 建议的正式产品形态

### 3.1 两层界面

第一层是菜单栏概览弹层，只承担高频状态与动作：

- 总体连接状态和最近更新时间；
- 当前项目；
- 当前任务状态、耗时和简短描述；
- 当前模型与推理强度；
- 等待安全审批时的醒目提示；
- 重新连接、停止当前任务、打开设置、退出。

第二层是独立设置窗口，只承担低频配置：

- Telegram；
- Codex 会话设置；
- 全局会话视图；
- 系统运行策略与项目归属；
- 关于与更新。

当前大窗口中的“概览”和左侧 Signal Path 应迁入菜单栏弹层，设置窗口不再承担仪表盘职责。

### 3.2 生命周期

- 点击菜单栏图标：切换概览弹层，不打开设置窗口。
- 点击“设置…”或按 `Command-,`：打开并聚焦设置窗口。
- 点击红色关闭按钮或按 `Command-W`：关闭设置窗口，Runtime 与 Telegram 保持运行。
- 按 `Command-Q` 或选择“退出”：统一进入退出确认流程。
- 空闲退出：提示退出后手机端将无法连接 CodexRelay。
- 任务运行或等待审批时退出：明确提示会中断当前任务，并默认选中“取消”。
- 用户确认后异步停止 Runtime；UI 不得同步冻结。停止超时后执行受控兜底，并将任务持久化为 interrupted。
- macOS 注销或关机时优先快速、可恢复地停止，不弹出可能阻塞系统退出的普通确认框。

建议采用明确的确认对话框，而不是“长按 Command-Q”。长按方式更接近手势延迟，并非真正的二次确认；对键盘、辅助功能和菜单点击也不统一。

### 3.3 视觉方向

- 菜单栏弹层宽约 340–380 pt，空闲态不滚动；只展示当前状态，不复制 CodexBar 的配额密度。
- 设置窗口约 720–780 pt 宽，使用五个标准 pane，去掉大号品牌标题和装饰性侧栏。
- 使用系统字体、语义色和系统外观；完整支持浅色、深色、高对比度、键盘焦点与 VoiceOver。
- 项目路径默认只在设置窗口显示；菜单栏概览只显示项目名，降低隐私暴露。
- 状态颜色只用于 ready、running、attention 和 offline，不以颜色作为唯一信息。

## 4. 建议的代码边界

```text
codexrelay/
├── ui/
│   ├── application.py       # 应用生命周期、原生菜单、退出协调
│   ├── status_popover.py    # 菜单栏概览弹层
│   ├── settings_window.py   # 设置窗口壳
│   ├── pages/               # Telegram / Codex / Sessions / System / About
│   ├── state.py             # AppStatusSnapshot
│   └── commands.py          # 共享 QAction 与快捷键
├── platform/
│   └── macos.py             # 登录项、系统外观及后续原生桥接
├── updates/
│   ├── base.py              # UpdateProvider 协议
│   └── disabled.py          # 当前版本的空实现
└── version.py               # 唯一版本来源
```

Runtime 应发布不可变 `AppStatusSnapshot`，菜单弹层和设置窗口只订阅状态，不各自查询和拼装状态。建议状态至少包含：

- runtime state；
- Telegram 身份与连接状态；
- 当前项目；
- 活跃任务状态、开始时间和安全审批状态；
- 当前模型与推理强度；
- 最近错误；
- 可执行命令的 enabled/disabled 状态。

## 5. 自动更新与发行

当前版本已接入 GitHub Releases 检查和用户确认后的 DMG 下载，不会后台替换正在运行的应用；下载完成后自动打开 DMG，最后拖入“应用程序”由用户完成。自动检查开关只负责定期检查和提示，不会代替用户点击下载。正式签名、公证和 Sparkle 发布链路可在后续增强。实现与规划如下：

1. 版本号收敛为单一来源，并在构建时写入 Info.plist。
2. 增加 `UpdateProvider`、`UpdateState` 和 release channel 数据模型；当前使用 GitHub Releases 元数据 provider。
3. Info.plist 构建器预留 Sparkle feed、公钥和自动检查键，但未配置时不写入正式包。
4. 设置窗口的“关于”页展示版本、构建时间、自动检查开关、手动检查按钮和官方链接。
5. 发布流水线预留 tag → 测试 → arm64 构建 → Developer ID 签名 → 公证 → 打包 → Sparkle 签名/appcast → GitHub Release。
6. 项目只维护单一正式发布序列；Draft 和 Pre-release 不会提示给普通用户，开发版和 ad-hoc 包不执行静默安装。

## 6. 长期 UI 设计约束

CodexRelay 后续界面统一遵循 macOS 简洁、克制、清晰的产品语言：

- 优先使用符合 macOS 语义的控件；二元状态使用开关，不使用复选框冒充开关。
- 信息层级依靠字号、间距和对齐建立，避免网页仪表盘式的卡片堆叠和装饰。
- 正常窗口尺寸下不得出现内容重叠、控件遮挡或无必要的滚动条。
- 一个控件只表达一个动作；设置名称、按钮反馈和结果文案保持一致。
- 所有新页面都必须在目标窗口尺寸下进行截图验收，并覆盖键盘焦点和辅助功能名称。
- 颜色仅用于状态和操作重点，默认界面保持安静，避免过量强调色。
- 优先减少内容和控件数量；只有用户确实需要选择时才增加选项。

## 7. 后续演进顺序

1. 继续按模块边界拆分 UI 与生命周期代码，同时保持状态快照和命令注册表的单一来源。
2. 增强深色模式、键盘焦点、VoiceOver 和动态字体支持。
3. 迁移登录启动到 `SMAppService`，保留旧 LaunchAgent 的一次性兼容清理方案。
4. 完成 Developer ID 签名、公证和正式更新签名后，再评估 Sparkle 原地更新。
5. 在不改变会话优先模型的前提下，评估更多连接器和更细粒度的审批权限。
