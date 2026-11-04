# 电脑磁盘与镜像恢复测试报告

日期：2026-11-04。最终功能及原生测试代码 `11420f188d951e2954c2dd3c6be598ea3b3984a8`；Linux 自动化报告来自 `13b1162`，后续只改截图测试字体配置及文档，Linux 电脑流程已在 `11420f1` 重跑。之后的证据提交只增加文档、截图与 JSON 报告。本报告保存 0.4.0 发布前的 PR 验证快照，执行时最新发布版为 v0.3.0。

## 执行结果

| 环境 | 自动化结果 | 电脑界面流程 | 打包后程序 |
|---|---|---|---|
| Linux / Qt offscreen | 270 通过，0 跳过 | 9 项通过，12 张截图 | 源码验证 |
| Windows x64 / Qt offscreen | 268 通过，2 项平台跳过 | 9 项通过，12 张截图，中文字形检查通过 | PE EXE 编译、实际启动及校验导出通过 |
| macOS 15 / Apple Silicon | 270 通过，0 跳过 | 9 项 Cocoa 流程，12 张截图 | arm64 `.app`、签名完整性、ZIP 解压后运行通过 |
| macOS 15 / Intel | 270 通过，0 跳过 | 9 项 Cocoa 流程，12 张截图 | x86_64 `.app`、签名完整性、ZIP 解压后运行通过 |

所有环境无失败或错误。Windows 两项跳过仍为打开文件期间替换路径、创建符号链接权限；没有新增跳过。两种 macOS 另各执行原有 14 项手机/备份/扫描/导出回归、20 张截图，均通过。

- [Windows 原生构建与证据](https://github.com/Ren-ZH-007/DeepRecover/actions/runs/41751765063)。
- [macOS 双架构原生构建与证据](https://github.com/Ren-ZH-007/DeepRecover/actions/runs/41751771153)。
- [永久证据清单](qa/computer-20261104/evidence.json)：测试报告、截图摘要、源哈希检查、原生制品链接及版本关系。

界面流程使用源码 Qt 窗口和实际扫描、预览、导出、SQLite 服务，文件选择与确认由受控替身注入。打包后的 EXE/`.app` 另行实际执行隔离冒烟：8 页导航、SQLite v3、PNG/JPEG 解码、原有 2 项校验导出，以及电脑签名扫描 2 项候选、预览与 2 项校验导出。两类证据分别记录，不混称为完整的打包后人工验收。

## 自动化覆盖和修复

合成镜像测试覆盖 NTFS resident DATA 的 USA 修复、碎片 nonresident 数据与碎片 MFT、分配重用拒绝、空文件、FAT32 删除长名称与未知多簇链、exFAT entryset/boot 校验及分配位图。分区覆盖 MBR/扩展分区/GPT、512/4096 字节扇区、CRC、重叠、循环与越界。端到端覆盖范围摘要、逻辑碎片顺序、源变化拒绝、预览/导出限制、取消、历史回读和原始盘任务前预检。

本轮补充回归时修复了以下问题：

| 问题 | 修复及依据 |
|---|---|
| Windows 虚拟盘或存储池可能用不同逻辑盘号掩盖同一底层物理盘 | 查询磁盘总线类型；无法证明隔离的 Storage Spaces、文件支持虚拟盘、RAID/网络/未知存储拒绝原始盘操作；两项负向回归 |
| 分区镜像预检后默认选中“整个源”，使已有受支持分区走签名模式 | 优先选中 NTFS/FAT32/exFAT 数据分区及删除记录模式；新增 UI 回归 |
| Windows 离屏截图中英文字体均为方框 | 离屏字体目录单独提供固定版本、SHA-256 核对的 Noto Sans CJK SC；截图前检查实际字形，不再仅用控件尺寸推断可读性 |

Windows 字体只用于 CI 截图，不装入应用分发包；原生 Windows 桌面仍使用系统字体及回退。字体缺失的[修复前截图](qa/computer-20261104/before-windows/11-minimum-computer-source-controls.png)保留作缺陷证据，不作为有效的界面验收截图。

## 电脑流程与关键截图

| 流程 | 核对内容 |
|---|---|
| CQA-01 | NTFS 只读预检，不创建数据库任务，默认删除记录模式 |
| CQA-02 | 找到 3 条删除记录：resident PNG、两段碎片 TXT 可校验；加密项保持受限 |
| CQA-03 | resident PNG 经实际 Qt 解码预览 |
| CQA-04 | 2 项实际导出，PNG 与原始载荷一致，碎片 TXT 按逻辑顺序重组，源/目标摘要相同 |
| CQA-05 | 受限项预览和导出均被拒绝；保留失败导出审计 |
| CQA-06 | 重开窗口/数据库回读 3 条候选、2 条导出；历史回读不访问来源 |
| CQA-07 | APFS 标识 stub 加 PNG 验证签名回退路由，删除状态为 unknown；不是真实 APFS 文件系统验收 |
| CQA-08 | 注入同物理盘隔离失败，任务数不变，不打开真实设备 |
| CQA-09 | 880×620 下来源选择与扫描控件有可见、可滚动的操作空间；截图前中字/Latin 字形检查通过 |

以下三个原生平台各保存全部 12 张电脑流程截图；Linux 生成的截图未重复入库：

| 节点 | Windows | Apple Silicon | Intel |
|---|---|---|---|
| 只读预检 | [截图](qa/computer-20261104/windows/01-ntfs-readonly-preflight.png) | [截图](qa/computer-20261104/arm64/01-ntfs-readonly-preflight.png) | [截图](qa/computer-20261104/x86_64/01-ntfs-readonly-preflight.png) |
| 删除记录及受限项 | [截图](qa/computer-20261104/windows/02-ntfs-deleted-records.png) | [截图](qa/computer-20261104/arm64/02-ntfs-deleted-records.png) | [截图](qa/computer-20261104/x86_64/02-ntfs-deleted-records.png) |
| 图片预览 | [截图](qa/computer-20261104/windows/03-resident-png-preview.png) | [截图](qa/computer-20261104/arm64/03-resident-png-preview.png) | [截图](qa/computer-20261104/x86_64/03-resident-png-preview.png) |
| 校验导出 | [截图](qa/computer-20261104/windows/04-verified-deleted-exports.png) | [截图](qa/computer-20261104/arm64/04-verified-deleted-exports.png) | [截图](qa/computer-20261104/x86_64/04-verified-deleted-exports.png) |
| APFS 签名状态 | [截图](qa/computer-20261104/windows/09-apfs-unknown-deletion-state.png) | [截图](qa/computer-20261104/arm64/09-apfs-unknown-deletion-state.png) | [截图](qa/computer-20261104/x86_64/09-apfs-unknown-deletion-state.png) |
| 原始盘隔离拒绝 | [截图](qa/computer-20261104/windows/10-raw-workspace-isolation-refused.png) | [截图](qa/computer-20261104/arm64/10-raw-workspace-isolation-refused.png) | [截图](qa/computer-20261104/x86_64/10-raw-workspace-isolation-refused.png) |
| 最小窗口来源控件 | [截图](qa/computer-20261104/windows/11-minimum-computer-source-controls.png) | [截图](qa/computer-20261104/arm64/11-minimum-computer-source-controls.png) | [截图](qa/computer-20261104/x86_64/11-minimum-computer-source-controls.png) |

原生证据中的每张 PNG 摘要均重新核对，合成来源扫描前后的 SHA-256 均不变。[复现脚本](../scripts/computer_qa_capture.py)：macOS 使用 `QT_QPA_PLATFORM=cocoa`；其他环境使用 offscreen，并确保 Qt 可访问中字/Latin 字体。

## 能力边界与未验收项

- NTFS 支持本版已实现的删除 MFT 布局；压缩、加密、稀疏、属性扩展列表等受限项不能作为完整文件导出。删除元数据和内容摘要均不证明从未覆盖。
- FAT32 仅空文件/空闲单簇布局可校验导出，多簇删除后失效的簇链不猜测；exFAT 仅完整有效删除 entryset、明确 NoFatChain 连续布局且位图空闲的内容可校验导出。
- APFS/HFS+ 只有连续 JPG/PNG 签名候选，不解析删除记录，不解密 FileVault，不声称可以恢复已 TRIM 或覆盖的数据。普通复制与签名候选不能表述为已删除文件恢复成功。
- 原始设备 API、权限、枚举、跨盘卷及 APFS/Fusion 映射使用受控替身测试；云端没有枚举或扫描真实硬盘、USB 或用户数据。真实 Windows/macOS 硬盘/U 盘、操作系统权限和真实文件系统兼容性仍待实机验收。
- 正在使用的磁盘不是不可变快照；工作目录和导出目标的物理盘隔离不等于硬件写保护。应用不自动提权、卸载、挂载、修复或回写源盘。
- macOS 原生 runner 为 15，未完成 13/14、Retina/多屏和带隔离标记下载启动验收；`.app` 为本地 ad-hoc 签名，没有 Developer ID 签名和 Apple 公证。Windows 包为便携目录，不是安装程序。

上述三个 0.4.0 编译包是本轮 Actions 保存的发布前 PR 制品，需仓库权限并受保留期限限制；其摘要与截图保留为历史证据。使用方法和资源上限见[电脑恢复说明](COMPUTER_RECOVERY.md)。

发布更新：[v0.4.0 预览版](https://github.com/Ren-ZH-007/DeepRecover/releases/tag/v0.4.0)已从 PR #8 合并提交 `5080c1fc98fa9790eecc40b419b03e36bb114942` 重新构建并发布。[发布流水线](https://github.com/Ren-ZH-007/DeepRecover/actions/runs/41757734624)与[正式附件下载后校验记录](releases/0.4.0/PUBLISHED.json)单独记录该标签的结果；下载请以正式 Release 附件及 SHA256SUMS.txt 为准。
