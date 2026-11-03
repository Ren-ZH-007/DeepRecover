# macOS 适配详细测试与缺陷回归

日期：2026-11-03。基线 `99d5d1c`；修复及最终测试代码 `91f5f760747b9751fda64ba68c07d15fee9b6a46`。本轮在原有 129 项通过的基础上独立检查边界，新增 32 项自动化并修复 8 处问题。后续证据提交只增加文档和截图。

## 最终执行结果

| 环境 | 自动化结果 | 完整界面流程 | 打包后程序 |
|---|---|---|---|
| Linux / Qt offscreen | 161 通过，0 跳过 | 14 项通过 | 源码验证，不作为 macOS 编译证据 |
| macOS 15 / Apple Silicon | 161 通过，0 跳过 | 14 项 Cocoa 流程，20 张截图 | Mach-O / ICNS / 签名完整性 / ZIP 解压 / offscreen 与 Cocoa 均通过 |
| macOS 15 / Intel | 161 通过，0 跳过 | 14 项 Cocoa 流程，20 张截图 | 同上，使用原生 x86_64 |
| Windows x64 | 159 通过，2 项既有平台跳过 | 本轮未额外执行原生桌面完整流程 | EXE 编译和实际启动流程通过 |

所有环境最终无失败或错误。Windows 仅跳过原有的打开文件期间替换路径、创建符号链接权限两项；两种 macOS 均执行这些测试，无新增跳过。

- [macOS 原生执行与制品](https://github.com/Ren-ZH-007/DeepRecover/actions/runs/41593713229)：PR 合并候选提交 `d983f3c7ed0413608ad8132093ea17bb99bfb16c`。
- [Windows 同代码回归](https://github.com/Ren-ZH-007/DeepRecover/actions/runs/41593708890)。
- [报告与制品摘要清单](qa/macos-20261103/evidence.json)、[Linux 报告](qa/macos-20261103/linux-test-report.json)、[Windows 报告](qa/macos-20261103/windows-test-report.json)。

完整 Cocoa 流程运行源码 Qt 窗口及实际服务，打包后的 `.app` 另行执行隔离冒烟检查；两类证据分别保存，不混称为完整的打包后手工验收。

## 已修复缺陷

| 编号 | 复现与影响 | 修复及回归依据 |
|---|---|---|
| MAC-QA-01 | 截断的 Manifest/Info XML 抛出未处理的 ExpatError，导致发现任务失败 | 损坏备份保留错误状态，其他备份继续展示；`test_truncated_xml_metadata_stays_visible_without_hiding_other_backups` |
| MAC-QA-02 | 直接选择仅有 Manifest.db 的备份，返回空列表而不提示缺失元数据 | 同时识别数据库入口并显示损坏原因；`test_direct_selection_with_only_manifest_database_reports_missing_metadata` |
| MAC-QA-03 | 一个目录访问被拒绝，会丢失其他可读目录结果 | 逐根/逐子目录处理权限错误，保留可读兄弟结果并提示权限；对应 unreadable / permission 三项回归 |
| MAC-QA-04 | Manifest.plist 或 Info.plist 链接可越过所选备份边界 | 元数据读取前检查规范路径范围，允许根内别名；对应 canonical_path / alias 回归 |
| MAC-QA-05 | 880×620 概览按钮被压缩到不足自然高度，文字不可见 | 概览内容可滚动，按钮保留自然高度；最小窗口回归及 Cocoa QA-12 |
| MAC-QA-06 | 长中文扫描路径被裁剪，缺少完整路径提示 | 扫描内容可滚动，路径可选择并有完整 tooltip；长路径回归及 Cocoa QA-13 |
| MAC-QA-07 | PNG 头加垃圾、截断或 CRC 错误可能被截图门禁接受 | 检查完整块边界、CRC 和真实解码，拒绝损坏截图；新增构建负向回归 |
| MAC-QA-08 | 损坏 ICNS 可通过仅检查图标存在性的门禁 | 原生应用检查验证 ICNS 容器并实际解码；新增图标回归 |

代码回归分别位于 `tests/test_macos_qa_core.py`（12 项）、`tests/test_macos_qa_ui.py`（8 项）、`tests/test_macos_qa_build.py`（12 项）。ADB 的中文空格工具路径、多设备授权隔离和六种非 device 状态也通过，未发现需要修改授权门禁的问题。

测试基础设施另修正了规范路径比较、合成快捷键修饰状态清理，以及 QScrollArea 的异步布局等待。长路径截图检查仍保留自然高度断言，最长等待 2 秒；没有通过跳过或降低可读性要求获得通过。新增截图断言的首轮失败及修正过程保留在 Actions 历史中。

## Cocoa 工作流与关键截图

每种架构独立执行以下 14 项，共保存 40 张流程截图，另有 2 张打包后启动截图。Android 传输和用户确认由受控替身注入，Qt 控件、后台任务、采集、索引、扫描、预览、导出和 SQLite 实际执行。

| 工作流 | 关键断言 |
|---|---|
| QA-01 / QA-02 | 未授权时不请求 shell/pull；授权后仅采集选中 2 项，源与本地摘要检查 |
| QA-03 | 稳定的合成 Finder 备份索引 2 项，未创建源 sidecar，源哈希不变 |
| QA-04 / QA-05 | 103 条跨块/截断候选分页，102 条有效筛选，实际图片预览 |
| QA-06 | 跨页导出 102 项，逐项源/目标摘要与清单对照 |
| QA-07 / QA-08 | 工作副本被修改时拒绝预览；拒绝向其他已登记备份写入 |
| QA-09 / QA-10 | 活跃 WAL 备份拒绝索引且源不变；取消后保留 1 项、101 项未尝试 |
| QA-11 | 重开窗口/数据库后回读 102 条导出，未完成任务标记 interrupted |
| QA-12 | 严格保持 880×620，概览入口自然高度可读，结果导出控件可见 |
| QA-13 | 中文、空格、长文件名路径可读，预览及 2 项校验导出成功，源字节不变 |
| QA-14 | 退出先取消并等待任务，保留两个窗口；结束后再次退出关闭历史 |

| 节点 | Apple Silicon | Intel |
|---|---|---|
| 最小概览修复 | [截图](qa/macos-20261103/arm64/14a-minimum-overview.png) | [截图](qa/macos-20261103/x86_64/14a-minimum-overview.png) |
| 最小结果窗口 | [截图](qa/macos-20261103/arm64/14-minimum-window.png) | [截图](qa/macos-20261103/x86_64/14-minimum-window.png) |
| 长中文扫描路径 | [截图](qa/macos-20261103/arm64/17a-unicode-source-layout.png) | [截图](qa/macos-20261103/x86_64/17a-unicode-source-layout.png) |
| 图片预览与筛选 | [截图](qa/macos-20261103/arm64/07-preview-and-filter.png) | [截图](qa/macos-20261103/x86_64/07-preview-and-filter.png) |
| 校验导出分页 | [截图](qa/macos-20261103/arm64/08-verified-export-page-2.png) | [截图](qa/macos-20261103/x86_64/08-verified-export-page-2.png) |
| 退出等待 | [截图](qa/macos-20261103/arm64/19-exit-waits-for-task.png) | [截图](qa/macos-20261103/x86_64/19-exit-waits-for-task.png) |

[ARM 完整截图摘要](qa/macos-20261103/arm64/evidence.json)与[Intel 完整截图摘要](qa/macos-20261103/x86_64/evidence.json)均已逐文件重新校验；PNG 块校验与解码也通过。修复前的[概览裁剪](qa/macos-20261103/before-linux/overview-buttons-clipped.png)、[长路径裁剪](qa/macos-20261103/before-linux/long-source-path-clipped.png)来自 Linux 离屏复现，明确不作为修复前 macOS 截图。

## 最终构建包

- [Apple Silicon ZIP + SHA256SUMS](https://github.com/Ren-ZH-007/DeepRecover/actions/runs/41593713229/artifacts/13210249610)
- [Intel ZIP + SHA256SUMS](https://github.com/Ren-ZH-007/DeepRecover/actions/runs/41593713229/artifacts/13210619403)

两个下载包均重新校验 SHA-256、ZIP CRC、Mach-O CPU 类型、执行权限、版本、ICNS 容器及框架链接保留情况。原生 runner 已对 ZIP 解压后的 `.app` 再做签名完整性和 Cocoa 启动检查。当前为 PR 分支制品，不代表 v0.3.0 Release 已发布；制品访问需仓库权限并受 Actions 保留期限限制。

## 未验收范围

实际原生环境为 macOS 15；macOS 13/14、真实 USB 设备、真实 Finder 备份、物理键盘/系统快捷键事件、Retina/多屏及带隔离标记的 Finder 下载启动未验收。权限拒绝使用受控故障注入，不能视为已完成 TCC 实机验收。应用仅本地 ad-hoc 签名，未提供 Developer ID 签名或 Apple 公证。普通文件采集及范围复制不等于已删除数据恢复。
