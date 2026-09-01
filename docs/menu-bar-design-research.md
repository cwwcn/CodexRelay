# CodexRelay 菜单栏面板设计研究

日期：2026-09-01

目标：从优秀 macOS 菜单栏 / 弹出面板应用里，提炼出适合 CodexRelay 的信息层级、动作分组与透明材质方案。

## 1. 一手参考

- Apple Materials / NSVisualEffectView：macOS 提供标准材质与 vibrancy，适合做轻量半透明面板。
- Raycast Menu Bar Commands：把“可扫一眼的信息”放进菜单栏，并持续后台刷新。
- Dato：菜单栏入口可以很小，但点击后给出更完整的分层信息；它也支持多个菜单栏入口。
- CleanShot X Quick Access Overlay：小型浮层，信息少但动作明确，支持复制、保存、标注、拖拽。
- Dropover：浮动 shelf + 动作菜单，把“暂存”和“操作”拆开。
- Bartender / Ice：适合高密度菜单栏管理，但本质是“管理器”，不是“状态面板”。
- MonitorControl：典型的“菜单栏入口 + 简洁控制面板 + 设置分离”。

## 2. 观察到的模式

- 好的菜单栏面板都先回答“我现在是什么状态”，再给“我能做什么”。
- 主信息通常只有一个：连接状态、当前时间、当前任务、当前文件。
- 次要信息要么拆成独立行，要么变成小标签，不要和主信息混在同一段文字里。
- 动作区通常固定在底部，和状态区拉开间距。
- 透明材质只做氛围，不抢内容对比度。
- 更复杂的功能会迁到独立设置窗口，不让弹层承担全部职责。

## 3. 对 CodexRelay 的结论

CodexRelay 不是 Bartender，也不是 Dropover。它更像“状态桥接器 + 安全控制台”。所以最适合的不是高密度工具箱，而是“清晰的状态卡片 + 少量高频动作”。

## 4. 三套可选方向

### 方向 A：状态卡片式

参考：Dato + CleanShot X + Raycast

结构：

- 顶部：应用名 + 连接状态
- 中部：当前项目、当前会话配置、当前任务
- 底部：重新连接、停止任务、打开设置、退出

优点：

- 最清楚，最不乱
- 适合当前单任务、单项目的产品状态
- 以后加审批、上下文、运行时信息也容易扩展

缺点：

- 不如工具型产品“热闹”

### 方向 B：Shelf 式

参考：Dropover

结构：

- 顶部：状态摘要
- 中部：像 shelf 一样展示当前任务 / 审批 / 上下文
- 底部：一排即时动作

优点：

- 很适合“临时承载内容、再批量处理”
- 对多上下文内容很友好

缺点：

- 容易往“复杂工作台”方向滑
- 不太像 CodexRelay 现在的单任务安全语义

### 方向 C：管理器式

参考：Bartender + Ice

结构：

- 多分区、多规则、多预设
- 侧重管理、分组、显示/隐藏、布局控制

优点：

- 后期如果要做多项目、多连接器、多模式切换，会很强

缺点：

- 现在会显得过重
- 容易把 CodexRelay 做成“另一个管理器”，失去轻量感

## 5. 推荐方向

推荐选方向 A，并吸收一点 B 的动作组织方式。

原因很简单：

- 现在最重要的是“看一眼就懂”
- 当前产品是单任务、单项目、Telegram 入口
- 用户最敏感的是层级混乱，不是功能不够多
- 半透明应该服务秩序感，而不是制造视觉噪音

## 6. 具体落地建议

- 菜单栏点击后只出 1 个轻量面板，不直接打开设置。
- 面板宽度控制在 340–380 pt。
- 每个信息块只回答一个问题。
- 项目、会话配置、任务状态分开成独立行或独立卡片。
- 操作区固定在底部，最多 3–4 个高频动作。
- 背景用轻微透明材质，建议偏“雾面玻璃”，不要做强玻璃特效。
- 文本保持高对比，透明度只加在容器层，不要压低正文可读性。

## 7. 来源

- Apple Materials: https://developer.apple.com/design/human-interface-guidelines/materials
- Apple NSVisualEffectView: https://developer.apple.com/documentation/appkit/nsvisualeffectview
- Raycast API Blog: https://www.raycast.com/blog/making-our-api-more-powerful
- Raycast Store / Menu Bar Commands: https://developers.raycast.com/api-reference/menu-bar-commands
- Dato: https://sindresorhus.com/dato
- CleanShot X Features: https://cleanshot.com/features
- Dropover Home: https://dropoverapp.com/
- Bartender 5: https://www.macbartender.com/Bartender5/
- Ice README: https://github.com/jordanbaird/Ice
- MonitorControl README: https://github.com/MonitorControl/MonitorControl
