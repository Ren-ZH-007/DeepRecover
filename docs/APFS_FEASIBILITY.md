# APFS 删除文件恢复可行性研究（2026-11-07）

路线图“macOS 恢复能力”一项要求先验证 APFS 删除恢复是否可行，再决定是否实现。本文是这次研究的结论、实测结果和实现建议。目前 DeepRecover 对 APFS 只提供连续签名候选，这不能代表删除恢复。

## 结论

1. **在未加密、未被 TRIM 清除的 APFS 上，删除文件可以恢复，而且能拿回原文件名、目录、大小和修改时间。** 在一个真实的 macOS APFS 测试镜像上，原型从孤立的文件树节点里完整还原了被删文件夹里的全部 5 个文件。PDF 和 Office 结构校验对字节级损坏非常敏感，这几个文件不仅通过了校验，长度也和 inode 记录的完全一致。
2. **Apple Silicon 和 T2 芯片 Mac 的内置 SSD 基本无法做原始块恢复。** 内置存储始终由芯片中的硬件密钥加密：公开的取证资料指出，拆下存储无法得到可用的镜像；也有开发者报告，在内置 SSD 上读取原始设备只得到全零。再加上内置 SSD 会执行 TRIM，已释放的块通常会被控制器擦除。这不是 DeepRecover 能绕过的限制。
3. **可行的对象**：未加密的外接硬盘、U 盘和外接 SSD（macOS 对外接 SSD 默认通常不执行 TRIM），APFS 磁盘镜像，以及没有 T2 芯片（大多是 2018 年以前的机型）、也没有开启 FileVault 的 Intel Mac。
4. 建议分阶段实现：先做“APFS 删除记录”候选（节点扫描 + 分配检查），再做快照读取。历史检查点的价值很低，不单独做。

## 不同设备上的可行性

| 场景 | 原始块恢复 | 原因 |
|---|---|---|
| Apple Silicon / T2 Mac 的内置 SSD | 不可行 | 硬件加密，无法得到可用的原始数据；内置 SSD 执行 TRIM |
| 开启 FileVault 或加密的 APFS 卷 | 当前不可行 | 文件树和数据都已加密；没有密钥就无法解析。以后可以研究“输入密码后解密”，但范围很大 |
| 未加密的外接 HDD | 可行 | 没有 TRIM，已释放的块在被新数据覆盖前一直存在 |
| 未加密的外接 SSD / U 盘 | 多数可行 | macOS 对外接 SSD 默认不开 TRIM，USB 通常也不转发 UNMAP；用户手动执行过 `trimforce enable` 的除外 |
| APFS 磁盘镜像（raw / dmg） | 可行 | 和外接盘相同；稀疏镜像可能会回收已释放的空间，需要实测 |
| 没有 T2 芯片、未开启 FileVault 的 Intel Mac | 部分可行 | 没有硬件加密；但如果内置的是 SSD，会执行 TRIM |

对内置磁盘，可行的替代方案是**本地 Time Machine 快照**：开启 Time Machine 后，macOS 会在内置数据卷上保留最近的本地快照，快照里有删除之前的完整文件。通过系统接口以只读方式挂载快照时，数据由系统读取，已解锁的卷不需要另外解密。这条路线只能在 macOS 上运行，需要管理员权限，属于逻辑恢复而不是原始块恢复。

## 原理

APFS 是写时复制的文件系统：修改元数据时不会原地覆盖，而是把新版本的 B 树节点写到空闲位置，旧节点所在的块随后被释放。所以删除文件之后：

- **当前文件树**里已经没有它的目录项、inode 和区段记录。
- **旧的文件树叶节点**往往还留在已释放的块里，直到这些块被重新分配。节点有 Fletcher-64 校验和，可以可靠地分辨出真正的节点和随机数据。
- **文件内容**所在的块同样已被释放，只要还没被重新写入就还在原处。
- **历史检查点**：检查点描述区保留了少数几份旧的容器超级块，从中可以找到旧的对象映射和旧的文件树。但这只能往回看几个事务。
- **快照**：快照会冻结某一时刻的整棵文件树，被快照引用的块不会被释放。只要快照早于删除操作，就能完全恢复。

区段记录给出了文件占用的物理块，但 APFS 不保存文件内容的校验和。所以内容是否完整，要靠分配检查（这些块是否已被重新占用），再加上已有的格式结构校验（PDF、Office、图片、音视频）来补充证明。

## 实测

原型是只读的研究脚本（Python，没有第三方依赖），测试样本是开源工具 AFRO 自带的测试镜像 `wsdf.dmg`：100 MB，GPT 分区表，由 macOS 创建，删除过一个名为 `document` 的文件夹（先移进废纸篓再清空）。

| 验证项 | 结果 |
|---|---|
| 分区识别 | DeepRecover 现有的分区识别正确找到偏移 0x5000 处的 APFS 分区 |
| 容器和卷 | 容器超级块、检查点描述区、对象映射、卷超级块（`wsdf`，未加密，无快照）全部通过校验和检查 |
| 当前文件树 | 正确列出 18 个现存文件和它们的目录 |
| 历史检查点 | 检查点描述区只保留了 4 份超级块（事务号 11 到 14），而删除发生在这之前，**这条路线在此镜像上找不回任何文件** |
| 节点扫描 | 扫描全部 24,410 个块，找到 15 个不在当前树里、但校验和有效的文件树叶节点，从中还原出 `/.Trashes/501/document` 和里面的 5 个文件 |
| 内容 | 5 个文件都只有 1 个区段，覆盖完整大小，没有一个块被现存文件占用。pdf（438,947 字节）、xlsx、docx、pptx 通过 PDF / Office 结构校验，长度精确；txt 内容可读 |
| 空间管理器 | 位图标记的已用块数加上空闲块数正好等于总块数；所有现存文件的块都标记为已用 |
| 分配状态 | 被删的 5 个文件，块在位图里**仍标记为已用**，但**全部在空闲队列中**（已释放、待回收） |

最后一项决定了分配检查的写法：块在位图里是空闲的，**或者**在空闲队列里，才说明还没被重新占用。只看位图会把刚删除的文件全部误判为“已被覆盖”。

这个镜像的局限：样本小（100 MB），来自 2018 年前后的 macOS，删除后几乎没有新的写入。真实磁盘上，已释放的块会被陆续重新分配，能恢复的比例会随使用时间下降。

## 实现建议

### 第一阶段：APFS 删除记录候选

适用于未加密的外接盘和镜像，接入现有的“电脑恢复 → 解析删除记录”流程。现有的分区识别和解析器选择（`core/computer/service.py`）可以直接接上。

- 解析容器超级块、检查点描述区和映射、对象映射、卷超级块；遇到加密卷、Fusion Drive、非连续的检查点区等情况，明确提示不支持。
- 解析当前文件树，用来排除现存文件。
- 扫描分区里的每个块，找出校验和有效、子类型为文件树、却不在当前树里的叶节点，取出目录项、inode 和区段记录，按事务号保留每个文件最新的版本，并还原路径。
- 分配检查：所有区段块都要在位图中空闲或位于空闲队列中，并且不被当前树引用。和 NTFS 一样，通过的报告为 `metadata_checked`，候选的删除状态记为 `deleted_record`。
- 能做格式结构校验的文件类型（PDF、Office、图片、音视频）再额外做一次内容校验，作为辅助依据。
- 第一阶段不支持：透明压缩（decmpfs）、稀疏文件、克隆共享区段、扩展属性中的数据，遇到时报告为受限项。
- 测试：需要用新版 macOS 创建的真实镜像覆盖多种情况（大量文件、多次删除、碎片文件、块被重新占用、压缩文件），不能只靠这一个样本。

### 第二阶段：快照

- 读取快照元数据树，列出每个快照的时间和名称；按快照时刻遍历文件树，找出“快照里有、当前没有”的文件。快照中的文件是完整的，可靠性比节点扫描高得多。
- 内置磁盘的本地 Time Machine 快照需要走 macOS 的挂载接口，涉及管理员权限，要单独评估。

### 不建议单独实现

- 历史检查点：实测只能回看几个事务，节点扫描已经覆盖它能找到的内容。
- 加密卷解密：需要解析密钥包和实现 AES-XTS，还涉及密码处理，范围和风险都很大。

## 需要进一步验证

- 新版 macOS（14 及以后）创建的 APFS：检查点保留数量、节点被重新使用的速度。
- 稀疏磁盘镜像和外接 SSD 删除文件后，已释放的块是否会被系统回收或擦除。
- 包含本地快照的卷。

这些需要在 Mac 上用 `hdiutil` 等工具创建测试镜像，云端测试环境无法完成。

## 资料来源

- [APFS checkpoint management behaviour in macOS（OS3 研究报告，2020）](https://rp.os3.nl/2019-2020/p08/report.pdf)
- [APFS Internals for Forensic Analysis（ERNW 白皮书）](https://static.ernw.de/whitepaper/ERNW_Whitepaper65_APFS-forensics_signed.pdf)
- [AFRO 与 BlackLight、Sleuth Kit 的 APFS 恢复对比（insinuator.net）](https://insinuator.net/2018/10/comparison-of-our-tool-afro-apfs-file-recovery-with-blackbag-blacklight-and-sleuthkit/)
- [AFRO 源码与测试镜像（GitHub：cugu/afro）](https://github.com/cugu/afro)
- [From FileVault To T2: How To Deal With Native Apple Encryption（Forensic Focus）](https://www.forensicfocus.com/articles/from-filevault-to-t2-how-to-deal-with-native-apple-encryption/)
- [SWGDE Best Practices for Apple macOS Forensic Acquisition](https://www.swgde.org/documents/published-complete-listing/23-f-005-swgde-best-practices-apple-macos-forensic-acquisition/)
- [Caring for SSDs: TRIM, wear levelling and APFS（The Eclectic Light Company）](https://eclecticlight.co/2019/08/26/caring-for-ssds-trim-wear-levelling-and-apfs/)
- [Should you trim external SSDs?（The Eclectic Light Company）](https://eclecticlight.co/2023/03/25/should-you-trim-external-ssds/)
- [Unable to read Internal APFS SSD drive（Apple Developer Forums）](https://developer.apple.com/forums/thread/745817)
