# 仓库构建与发布状态核查

核查日期：2026-11-04。范围为全部远程分支、开放 PR、版本标签、Release 附件和对应构建记录。

## 本次发布

所有功能与修复分支均已合并到 `main`，APFS 修复 PR #10 的合并提交为 `2c0b3243c386189959d29aceb6808678bbb8ae1a`。v0.4.1 标签从该提交构建 Windows x64、macOS arm64、macOS x86_64 并发布；正式附件与原生报告见 [PUBLISHED.json](PUBLISHED.json)。发布文档通过 PR #11 同步，未修改程序或构建输入。

远程功能分支 `feature/m2-authorized-acquisition`、`feature/m3-image-signature-scanner`、`feature/m4-preview-and-verified-export`、`feature/ui-refresh-release`、`feature/macos-support`、`feature/computer-recovery`，以及 `fix/detailed-qa-20261103`、`fix/macos-apfs-preflight` 的提交均已纳入 main；这些变更统一由当前版本构建发布。旧文档分支也已合并。

## 历史标签核对

| 标签 | 当时发布范围 | 核对结果 |
|---|---|---|
| v0.1.0 | 源码预览；发布说明明确不含 Windows EXE/安装包 | 已发布源码，零二进制附件符合该版本说明 |
| v0.2.0 | Windows x64 便携预览版 | Windows ZIP 与 SHA256SUMS 齐全，标签构建 41586264504 成功；当时尚无 macOS 适配 |
| v0.3.0 | Windows x64、macOS 双架构 | 三个 ZIP 与 SHA256SUMS 齐全 |
| v0.4.0 | Windows x64、macOS 双架构，新增电脑入口 | 三个 ZIP 与 SHA256SUMS 齐全 |
| v0.4.1 | 三平台 APFS 预检修复预览版 | 已从准确标签重新构建，附件下载后独立校验 |

历史失败或取消的中间运行由后续修复及最终成功构建覆盖，不属于未发布的新代码。历史版本保留原始源码、平台范围和附件，不将当前程序挂到旧标签。

原生测试和 APFS 元数据查询不等于真实硬盘/U 盘恢复验收；APFS/HFS+ 仍只有连续 JPG/PNG 签名候选。工作目录和导出目标仍须与原始来源位于不同物理盘。
