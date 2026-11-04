# macOS APFS 只读预检修复

日期：2026-11-04。本报告保存 0.4.1 发布前的 PR 验证快照；该修复已随 [v0.4.1 预览版](https://github.com/Ren-ZH-007/DeepRecover/releases/tag/v0.4.1)发布，v0.4.0 不包含此修复。[PR #10](https://github.com/Ren-ZH-007/DeepRecover/pull/10)。

## 问题和处理

选择 Macintosh HD 后提示“缺少物理磁盘标识”。原因是 `diskutil info -plist` 的 `APFSPhysicalStores` 项使用 `APFSPhysicalStore`，旧代码只识别 `DeviceIdentifier`；后者通常来自 `diskutil apfs list -plist`。同时补齐系统快照的字符串卷引用处理，未知快照不再回退到合成磁盘号。

两种明确字段均可解析；缺失、损坏或冲突字段继续拒绝。Fusion 保留全部成员。容器和卷引用必须一致；明确标为物理分区的来源仅在其确属已解析 stores 成员时允许引用不同编号的合成容器。

界面在原始盘预检失败时显示当前工作目录、物理盘隔离要求和“查看工作目录设置”入口。更换来源会清除旧文件系统说明，避免预检失败后沿用上一来源的能力提示。失败预检不创建扫描任务，也不打开原始磁盘内容。

## 验证

- 本地最终 292 项测试通过，0 跳过；电脑流程 9 项通过、13 张截图。原先缺字段与冲突引用的失败复现保留在[证据目录](qa/apfs-preflight-20261104/)。
- macOS Apple Silicon / Intel 各 292 项通过；Windows 290 通过、2 项既有平台跳过。三个 0.4.1 编译包的摘要、ZIP CRC、程序头均已核对。功能提交 `027b2be`；后续证据提交仅修改文档。
- 原生执行与编译结果、来源提交、制品和关键截图见[验证清单](qa/apfs-preflight-20261104/evidence.json)。macOS 的原生元数据检查实际查询 runner 的 `diskutil/df`，不读取块设备内容。
- 新增跨平台模拟测试对 macOS 设备路径使用明确的边界替身，避免 Windows 将 `/dev/disk…` 当作本机文件路径；生产路径校验保持不变。两种 macOS 的原生检查直接使用生产校验。

原生元数据通过仅证明对应 runner 的字段兼容及同盘拒绝逻辑，不等于实际硬盘/U 盘数据恢复、权限或文件系统兼容性验收。APFS/HFS+ 仍仅连续 JPG/PNG 签名候选，不解析其删除记录。

两种 macOS runner 均实际返回了 `APFSPhysicalStore`，完成来源/工作卷映射和同盘拒绝，报告：[Apple Silicon](qa/apfs-preflight-20261104/arm64/disk-metadata-validation.json)、[Intel](qa/apfs-preflight-20261104/x86_64/disk-metadata-validation.json)。新增模拟测试不会替代这项原生检查。

发布更新：v0.4.1 从合并提交 `2c0b3243c386189959d29aceb6808678bbb8ae1a` 重新构建。[正式发布流水线](https://github.com/Ren-ZH-007/DeepRecover/actions/runs/41780403227)、[正式附件下载后校验记录](releases/0.4.1/PUBLISHED.json)与[仓库发布状态核查](releases/0.4.1/REPOSITORY_STATUS.md)分别记录该标签结果。下方及证据目录中的 PR 制品继续保留原来源和摘要；下载以正式 Release 附件及 SHA256SUMS.txt 为准。

关键界面截图来自 Qt 与实际服务、合成镜像和受控 APFS 元数据替身；没有打开真实 raw 来源：[Apple Silicon 工作目录说明](qa/apfs-preflight-20261104/arm64/10a-work-directory-guidance.png)、[Intel](qa/apfs-preflight-20261104/x86_64/10a-work-directory-guidance.png)、[Windows](qa/apfs-preflight-20261104/windows/10a-work-directory-guidance.png)。原生检查与模拟界面证据分开记录。

## 扫描 Macintosh HD 前

即使修复映射，若工作目录仍位于内置盘，同盘保护仍会阻止预检。将工作目录放到另一块物理磁盘；同盘的其他分区不满足要求。退出应用，在终端按实际外置盘名称启动，例如应用已放在“应用程序”时：

```sh
DEEPRECOVER_DATA_DIR='/Volumes/外置盘名/DeepRecover_Work' \
  /Applications/DeepRecover.app/Contents/MacOS/DeepRecover
```

替换外置盘名称；应用若放在其他目录，也相应调整启动路径。新工作目录不会自动迁移原历史记录。导出目标同样必须位于其他物理盘。权限或映射无法确认时继续拒绝，不自动提权、卸载、挂载、修复或回写源盘。
