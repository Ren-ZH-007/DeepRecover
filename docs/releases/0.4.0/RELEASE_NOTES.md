# DeepRecover v0.4.0 · 电脑恢复开发预览

新增 Windows/macOS 本地磁盘、U 盘及原始镜像入口，读取 MBR/GPT 分区并区分删除元数据与图片签名候选。

- NTFS：已删除 MFT 记录、resident USA 修复、nonresident 碎片布局、数据簇分配检查。
- FAT32：空内容/空闲单簇删除候选；多簇链未知时保留 partial。
- exFAT：删除 entryset、连续布局与分配位图检查。
- 原始盘只读，工作目录与导出目标须在其他物理盘；检查失败或无法确认时拒绝任务。
- 候选保存在本地，支持分页、图片预览、范围校验导出和历史回读。

APFS/HFS+ 当前仅连续 JPG/PNG 签名候选，不解析删除记录或解密。NTFS 压缩、加密、稀疏与扩展属性布局未支持；不修复源文件系统，不猜失效簇链。候选不等于恢复成功，SHA-256 一致只证明本次输出与读取范围相同。

详见 [电脑恢复说明](https://github.com/Ren-ZH-007/DeepRecover/blob/main/docs/COMPUTER_RECOVERY.md)。Windows/macOS 原始设备读取依赖本机权限，实际盘、U 盘和 live 系统盘仍需实机验收；本地 ad-hoc macOS 包未提供 Developer ID 签名或 Apple 公证。
