# macOS 适配验证

后续详细测试发现并修复了 8 处问题，自动化增至 161 项；最新原生截图、报告与修复后下载包见 [详细 QA 报告](../../MACOS_TEST_REPORT_20261103.md)。下方为首次适配提交的历史记录，不代表最新修复包。

日期：2026-11-03。源代码提交：`7277f4ae3fdbc7064492272d1b299c8ac989eb0a`。本目录的 JSON 和截图记录该提交的原生构建，不会因后续文档提交而改写；[证据清单](validation/evidence.json)包含下载 ZIP 和截图的摘要。

## 平台结果

| 环境 | 自动化 | 打包运行 |
|---|---|---|
| Linux / Python 3.12 | 129 通过，0 跳过 | 源码隔离冒烟；12 项合成 Qt 工作流通过 |
| macOS 15.8.2 / arm64 / Python 3.12.10 | 129 通过，0 跳过 | `.app` 离屏通过；ZIP 解压后 Cocoa 通过 |
| macOS 15.7.9 / x86_64 / Python 3.12.10 | 129 通过，0 跳过 | `.app` 离屏通过；ZIP 解压后 Cocoa 通过 |
| Windows x64 / Python 3.12 | 127 通过，2 项既有平台跳过 | EXE 实际启动及合成数据流程通过 |

[macOS 原生构建](https://github.com/Ren-ZH-007/DeepRecover/actions/runs/41589567579)；[Windows 回归构建](https://github.com/Ren-ZH-007/DeepRecover/actions/runs/41589567347)。Windows 跳过项仍仅为打开文件期间替换路径、符号链接权限，两种 macOS 均执行并通过这些测试。

新增 26 项自动化，覆盖 macOS 工作目录、Finder 只读发现、ADB 优先级和授权门禁、菜单与退出保护、应用元数据/架构/验证报告错误路径。原有数据源、扫描、预览、导出与历史回读测试继续执行。

## 实际应用验证

PySide6 6.8.3 / PyInstaller 6.12.0，分别在官方 `macos-15` 和 `macos-15-intel` runner 编译，不使用 Rosetta 代替另一种原生架构。

1. 验证 `DeepRecover.app` 的 Info.plist、版本、图标、Mach-O 和对应架构；应用声明最低 macOS 13.0。
2. 验证本地 ad-hoc 签名完整性；运行打包入口的 offscreen 验证。
3. 使用 `ditto` 压缩及重新解压，检查解压后签名，再运行实际 Cocoa 插件。两个 ZIP 均保留 94 个链接，下载后 SHA-256、ZIP CRC、执行权限、版本与 Mach-O CPU 类型复核通过。
4. 隔离临时工作数据，实际执行 7 页导航、SQLite v3、PNG/JPEG 解码、本地扫描和 2 项摘要对照导出，并确认源字节未改变。
5. Cocoa 额外检查非空 Qt 标准退出绑定（macOS Command+Q 对应的逻辑 Control+Q）、原生菜单、QuitRole 和实际退出动作。跨平台 UI 回归另通过合成快捷键事件验证活任务停止/等待及导出历史关闭，不把快捷键字形当作功能证据。

报告见 `validation/`：各架构的 `test-report.json`、`offscreen-smoke.json`、`cocoa-smoke.json`、`bundle-validation.json`。完整构建日志和签名诊断保留在对应 Actions 制品。

## 实际 Cocoa 窗口截图

- [Apple Silicon](validation/arm64-cocoa-overview.png)：1024×653，受 runner 屏幕尺寸约束。
- [Intel](validation/x86_64-cocoa-overview.png)：1280×820。

均由解压后的应用窗口自身抓取，不是 Linux 截图或设计稿，不包含真实用户备份。系统菜单栏不在窗口截图范围内。

## 初次适配制品（7277f4a 历史记录）

- [Apple Silicon ZIP + SHA256SUMS](https://github.com/Ren-ZH-007/DeepRecover/actions/runs/41589567579/artifacts/13209387916)
- [Intel ZIP + SHA256SUMS](https://github.com/Ren-ZH-007/DeepRecover/actions/runs/41589567579/artifacts/13209188457)

需要仓库访问权限；Actions 制品有保留期限。当前为分支构建，不代表 v0.3.0 已发布。合并并创建匹配版本标签后，跨平台发布工作流才会同时发布 Windows 与两个 macOS ZIP。

## 未验收范围

最低版本声明不等于已在 macOS 13/14 上实测；本次实际为 macOS 15 runner。未完成真实手机、真实 Finder 备份、物理快捷键/系统事件、Retina/多屏、带隔离标记的 Finder 下载启动、Developer ID 签名或 Apple 公证验收。应用包仅本地 ad-hoc 签名。Windows 仍是便携包，不是安装程序。第一阶段 M5 继续保留这些实机验收项。
