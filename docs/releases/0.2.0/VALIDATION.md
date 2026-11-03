# v0.2.0 界面、编译与发布验证

日期：2026-11-03。基线：已合并的 M4/QA 修复；本次更新 UI、Windows 兼容性和构建发布流程。

## 界面

深蓝导航 + 浅色工作区，统一图标、主/次/停止操作、卡片分区、表格选中/隔行色、预览区与导出历史。最小窗口 880×620 保留关键操作；不引入远程资源、联网预览或新的用户数据上传。

12 项合成 Qt 工作流通过，15 张截图及哈希见 [evidence.json](screenshots/evidence.json)。关键截图：[总览](screenshots/02-overview.png)、[Android](screenshots/04-android-selected-files.png)、[扫描](screenshots/05b-scan-source-selected.png)、[预览](screenshots/07-preview-and-filter.png)、[导出历史](screenshots/08-verified-export-page-2.png)、[最小窗口](screenshots/14-minimum-window.png)。截图为真实控件渲染，不代表真实手机连接。

## 自动化与原生 Windows 编译

- Linux：103 项通过，0 失败/错误/跳过。[报告](linux-test-report.json)。
- 原生 Windows x64 / Python 3.12.10 / PySide6 6.8.3：[首次完整成功构建](https://github.com/Ren-ZH-007/DeepRecover/actions/runs/41560446067)，103 项中 101 通过、2 项已声明平台跳过，无失败/错误。[报告](windows-test-report.json)。
- 两项跳过为“打开文件期间替换路径”及“创建符号链接”的平台权限语义，未跳过来源绑定、摘要、防覆盖、扫描、预览或导出主流程。
- PyInstaller 6.12.0 onedir 输出，实际 EXE 启动并退出 0。[EXE 报告](windows-executable-smoke.json)验证 v0.2.0、SQLite v3、7 页导航、PNG/JPEG 解码、两项真实校验导出和源字节不变。
- ZIP 已在下载后重新校验 SHA-256、ZIP CRC、216 个文件、x64 PE machine=0x8664、qwindows/qjpeg 插件。发布流水线会对最终标签提交重新构建并核验，再上传 ZIP 与 SHA256SUMS.txt；最终下载的校验值以该次发布附件为准。

## 实际 Windows 差异修复

1. CPython 3.12 的 Windows `stat(path).ctime` 为兼容创建时间，`fstat(fd).ctime` 为变更时间；共享快照统一使用 birthtime，保留卷/完整文件 ID、大小、mtime。POSIX 继续使用 ctime。新增路径/句柄一致性与大文件 ID 保留回归，未删除身份校验。
2. Windows 不能删除仍被日志句柄占用的临时文件；隔离冒烟检查结束先关闭日志，再清理临时目录，不访问真实用户工作目录。
3. SQLite connection 上下文只提交事务，不负责关闭；测试夹具改为显式 closing，保留 WAL 测试需要的活连接生命周期，不放宽业务断言。
4. Windows 8.3 短路径与 resolve 后的完整路径通过 canonical 路径比较，不误判未执行 rb 读取。
5. 10,005 条索引为正确性夹具，不是 20 秒 SLA；仅将 Windows 测试等待上限调为 90 秒，数量/内容/只读断言不变。

## 发布门禁

`scripts/build-windows.ps1`：固定依赖 → 测试 → PyInstaller → 实际 EXE 检查 → ZIP + SHA256SUMS。任何错误、意外跳过、expectedFailure 或错误 EXE 报告阻止发布。工作流使用官方 Actions 的已核验提交，标签必须与 pyproject.toml 版本一致。

发布工作流只允许在 Windows 构建成功后执行，发布为预发布版本，并拒绝覆盖已有同标签版本/附件。新样式的最终提交仍须经过此门禁；报告文件记录的是上述首次成功编译，最终标签构建的最新证据由对应 Actions run 保存。

## 尚未验收

Windows runner 的离屏自动启动不等于 Windows 10/11 真机交互、高 DPI、多屏、真实 USB/手机、真实备份、安装程序、签名或取证写阻断验收。产物是完整目录便携包，不是安装包。普通复制、候选定位、预览和摘要一致不构成已删除数据恢复成功的结论。
