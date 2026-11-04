# DeepRecover v0.4.1 · macOS APFS 预检修复

修复部分 macOS 选择 Macintosh HD 后提示“缺少物理磁盘标识”、无法完成只读预检的问题。

- 兼容 `diskutil info` 的 `APFSPhysicalStore` 字段和 APFS 列表的 `DeviceIdentifier` 字段。
- 按明确的 APFS 卷字段解析系统快照，未知或冲突映射继续拒绝；Fusion 的全部物理成员仍参与同盘检查。
- 原始盘预检失败时显示当前工作目录与操作说明，提供“查看工作目录设置”入口。
- 增加 macOS 双架构原生磁盘元数据检查，只查询系统元数据，不打开或扫描原始设备。

扫描 Macintosh HD 时，工作目录和导出目标仍须位于另一块物理磁盘。同一磁盘的另一个分区不满足要求。启动前设置 `DEEPRECOVER_DATA_DIR`，操作示例见[电脑恢复说明](https://github.com/Ren-ZH-007/DeepRecover/blob/main/docs/COMPUTER_RECOVERY.md)。

APFS/HFS+ 仍仅支持连续 JPG/PNG 签名候选，不解析其删除记录，不解密、不声称恢复 TRIM 或已覆盖内容。原始设备权限和真实硬盘/U 盘仍需实机验收；macOS 应用未提供 Developer ID 签名或 Apple 公证。
