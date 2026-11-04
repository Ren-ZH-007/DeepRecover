# 引导式恢复界面升级测试报告

日期：2026-11-04。功能代码 `3e23dd584012ea7f252a6566412572fe4be92a6a`，版本 0.5.0；后续证据提交仅修改文档。本报告保留发布前 PR 验证快照，执行时最新发布版为 v0.4.1。

本轮完成物理磁盘来源树、工作目录图形化选择与空闲切换、三步预检，以及中文结果和独立依据窗。使用方法见[升级说明](GUIDED_RECOVERY.md)。

发布更新：[v0.5.0 预览版](https://github.com/Ren-ZH-007/DeepRecover/releases/tag/v0.5.0)已从 PR #12 合并提交重新构建并发布。准确构建提交、正式原生报告及附件下载后校验见[发布记录](releases/0.5.0/PUBLISHED.json)。以下 PR 制品和截图保留原始来源及摘要；下载以正式 Release 附件和 SHA256SUMS.txt 为准。

2026-11-04 19:09（北京时间）发布，正式构建提交 `fab92ed58b7b206b2261f78c3142da183db82972`，[发布流水线](https://github.com/Ren-ZH-007/DeepRecover/actions/runs/41818558830)全部通过。发布后重新下载三个平台 ZIP，逐包核验 SHA-256、ZIP CRC 与可执行架构；核对正式原生测试及工作流报告，并验证 109 张流程截图的摘要、PNG CRC、解码和非空采样，另有 2 张打包应用截图通过完整性检查。正式报告计数与下方 PR 验证一致；本记录不代替真实介质验收。

## 执行结果

| 环境 | 自动化 | 工作目录流程 | 电脑流程 | 原有回归 | 编译后程序 |
|---|---|---|---|---|---|
| Linux / offscreen | 364 通过，0 跳过 | 5 项 / 5 图 | 11 项 / 18 图 | 14 项 / 20 图 | 源码验证 |
| Windows x64 / offscreen | 362 通过，2 项既有跳过 | 5 项 / 5 图 | 11 项 / 18 图 | 本轮未额外执行原有 14 项桌面脚本 | EXE 实际启动、选择工作目录与历史往返通过 |
| macOS 15 / Apple Silicon | 364 通过，0 跳过 | 5 项 Cocoa / 5 图 | 11 项 Cocoa / 18 图 | 14 项 Cocoa / 20 图 | 原生 arm64 app、ZIP 解压运行、Cocoa 与退出验证通过 |
| macOS 15 / Intel | 364 通过，0 跳过 | 5 项 Cocoa / 5 图 | 11 项 Cocoa / 18 图 | 14 项 Cocoa / 20 图 | 原生 x86_64 app，其余同上 |

没有失败、错误或新增跳过。Windows 的两项跳过仍为打开文件期间替换路径、创建符号链接权限。新增链接保护测试使用边界替身，未通过新增跳过规避平台差异；Linux 另实际复现了 symlink/hardlink 日志源保护和双会话占用拒绝。

- [Windows 构建](https://github.com/Ren-ZH-007/DeepRecover/actions/runs/41816563725)。
- [macOS 双架构构建](https://github.com/Ren-ZH-007/DeepRecover/actions/runs/41816704020)。
- [完整证据清单](qa/guided-20261104/evidence.json)：109 张工作流截图重新核对 SHA-256、PNG CRC 和实际像素解码，其中 33 张新功能关键图永久入库。三个 ZIP 均重新核对 SHA-256、ZIP CRC 和 PE/Mach-O 架构。

界面工作流使用源码 Qt 窗口、真实本地扫描/预览/导出和 SQLite；设备枚举与模态选择使用受控替身。打包后的 EXE/app 另实际打开工作目录选择窗，确认选择前不创建目录，再完成工作区 A → 空白 B → 原 A 的历史往返；保留原有 PNG/JPEG 解码、两项普通扫描导出、两项电脑扫描导出和 8 页导航检查。两类证据分别保存。

## 关键验证

- 来源树：真实物理父项、普通卷、明确辅助角色和已知快照分组；未知来源保留，跨盘容器保留所有成员。过滤、刷新或更换选择会清除旧来源和范围，不能用旧索引行号启动扫描。
- 工作目录：启动取消无数据库/日志，选择后才初始化；切换前物理盘隔离，源备份和采集副本边界，目录/数据库/日志/锁文件链接与共享文件拒绝，源绑定和卷变化复查，目录占用及失败释放。
- 数据保留：切入新目录不复制历史；重开原目录能读回原候选，旧数据库与旧日志在切走时不被改写，后续日志只写新目录。
- 结果依据：普通采集、备份、删除记录和签名候选分开说明；布局/结构检查不证明内容完整。未知状态保守展示，技术值保留在提示中；独立导出记录只有有效且一致的双摘要才显示校验通过。分页和筛选不会沿用旧选择或依据窗。

复核发现并修复两类问题：第二个会话可误标第一个仍运行的任务为中断，现由数据库迁移/恢复前的独占锁阻止；日志文件链接可写出工作目录，现由预检、激活和日志入口的独立文件检查阻止。锁正常退出及初始化失败时释放，不因锁文件年龄自动接管活跃会话。使用新版打开旧工作目录前仍需关闭旧版窗口，旧版不识别新锁协议。

macOS 原生检查实际查询系统元数据：Apple Silicon 返回 20 个来源、2 个已知物理组；Intel 返回 11 个来源、1 个物理组。均通过根卷/工作卷映射与同盘拒绝。报告只保存类别与数量，不保存原始 UUID、序列号或真实路径：[ARM](qa/guided-20261104/arm64/disk-metadata-validation.json)、[Intel](qa/guided-20261104/x86_64/disk-metadata-validation.json)。

## 测试包与关键截图

以下保留发布前功能提交的 Actions 测试包，受仓库权限和制品保留期限限制。下载后先解压制品 ZIP，再使用其中的平台 ZIP 及 SHA256SUMS；当前下载请使用上方正式 v0.5.0 Release，避免混用 PR 与发布包的摘要。

- [Windows x64](https://github.com/Ren-ZH-007/DeepRecover/actions/runs/41816563725/artifacts/13314882423)
- [macOS Apple Silicon](https://github.com/Ren-ZH-007/DeepRecover/actions/runs/41816704020/artifacts/13314991511)
- [macOS Intel](https://github.com/Ren-ZH-007/DeepRecover/actions/runs/41816704020/artifacts/13315611019)

| 节点 | Windows | Apple Silicon | Intel |
|---|---|---|---|
| 启动选择工作目录 | [图](qa/guided-20261104/windows/01-workspace-before-initialization.png) | [图](qa/guided-20261104/arm64/01-workspace-before-initialization.png) | [图](qa/guided-20261104/x86_64/01-workspace-before-initialization.png) |
| 物理磁盘分组 | [图](qa/guided-20261104/windows/14-grouped-physical-sources.png) | [图](qa/guided-20261104/arm64/14-grouped-physical-sources.png) | [图](qa/guided-20261104/x86_64/14-grouped-physical-sources.png) |
| 显示辅助卷 | [图](qa/guided-20261104/windows/15-explicit-auxiliary-sources.png) | [图](qa/guided-20261104/arm64/15-explicit-auxiliary-sources.png) | [图](qa/guided-20261104/x86_64/15-explicit-auxiliary-sources.png) |
| 删除记录依据 | [图](qa/guided-20261104/windows/02a-deleted-record-evidence.png) | [图](qa/guided-20261104/arm64/02a-deleted-record-evidence.png) | [图](qa/guided-20261104/x86_64/02a-deleted-record-evidence.png) |
| 签名删除状态未知 | [图](qa/guided-20261104/windows/09a-signature-unknown-evidence.png) | [图](qa/guided-20261104/arm64/09a-signature-unknown-evidence.png) | [图](qa/guided-20261104/x86_64/09a-signature-unknown-evidence.png) |
| 新目录无历史迁移 | [图](qa/guided-20261104/windows/03-fresh-workspace-no-history-copy.png) | [图](qa/guided-20261104/arm64/03-fresh-workspace-no-history-copy.png) | [图](qa/guided-20261104/x86_64/03-fresh-workspace-no-history-copy.png) |
| 重开原历史 | [图](qa/guided-20261104/windows/04-existing-workspace-history-reopened.png) | [图](qa/guided-20261104/arm64/04-existing-workspace-history-reopened.png) | [图](qa/guided-20261104/x86_64/04-existing-workspace-history-reopened.png) |

## 尚未验收

本轮没有扩展文件系统删除解析、签名格式、断点续扫或安装程序。APFS/HFS+ 仍仅连续 JPG/PNG 签名候选，普通采集和摘要一致都不能当作已删除数据恢复成功。原生元数据查询与合成数据流程不等于真实硬盘、USB、手机、备份、权限或取证只读验收；这些继续纳入 M5。

macOS 原生环境为 15；13/14、Retina/多屏和 Gatekeeper 下载启动未验收。app 仍为本地 ad-hoc 签名，未提供 Developer ID 签名或 Apple 公证。
