# DeepRecover v0.3.0 · macOS 适配预览版

本版新增 macOS 支持，保留 Windows 10/11 x64：

- Apple Silicon（arm64）与 Intel（x86_64）分别原生构建 `DeepRecover.app`，目标 macOS 13+，ZIP 保留应用框架链接并附 SHA-256 校验值。
- 工作数据放在 `~/Library/Application Support/DeepRecover`，默认发现 Finder/iTunes 的 MobileSync 备份。
- Finder 启动时补充 Homebrew ADB 发现；显式工具路径和环境配置优先，设备仍须授权。
- macOS 中文字体、多选提示和 Command+Q 原生菜单适配，退出遵守后台任务停止检查。
- 原生平台测试及打包后应用检查成为发布前置条件，macOS 另验证实际 Cocoa 插件并保存窗口截图。
- 详细 QA 修复损坏/无权限备份的发现、元数据链接越界、最小窗口/长路径裁剪，以及 PNG/ICNS 构建检查遗漏；双架构各 161 项自动化、14 项 Cocoa 流程通过。

下载与你的芯片一致的 `DeepRecover-v0.3.0-macos-arm64.zip` 或 `DeepRecover-v0.3.0-macos-x86_64.zip`，解压后把整个 `.app` 拖入“应用程序”。Windows 用户使用 `DeepRecover-v0.3.0-windows-x64.zip`，解压后保留整个目录运行 EXE。均无需另装 Python。具体操作与源码运行见 [macOS 文档](https://github.com/Ren-ZH-007/DeepRecover/blob/main/docs/MACOS.md)。

macOS 预览包未提供 Apple Developer ID 签名或公证；原生构建与 Cocoa 自动验证不等于真实机器的下载隔离/启动、Retina、多屏、手机或真实备份验收。Windows 包仍为便携目录，尚非安装程序。ADB 需自行安装官方工具并取得设备授权。

处理保持本地，原始来源只读。支持授权普通文件采集、未加密稳定备份索引、连续 JPG/PNG 候选扫描、图片预览和校验导出；普通复制不等于已删除数据恢复。视频、碎片重组、文件系统恢复与加密备份解密不在本版范围内。
