# macOS 适配

[v0.6.0 预览版已发布](https://github.com/Ren-ZH-007/DeepRecover/releases/tag/v0.6.0)，目标 macOS 13 或更高版本，分别提供 Apple Silicon（arm64）和 Intel（x86_64）原生应用。请从 Release 下载与你的芯片一致的 ZIP；实际原生验证环境为 macOS 15。

v0.6.0 新增本机环境检查与脱敏本地报告，双架构各 392 项自动化、34 项 Cocoa 流程及打包后诊断保存检查通过；M5 实机项目仍未执行，见[准备工具测试报告](M5_PREPARATION_TEST_REPORT_20261105.md)。

v0.5.0 新增来源树、图形化工作目录与中文结果依据，原生双架构各 364 项自动化通过；每架构另有 30 项 Cocoa 流程、43 张工作流截图，打包后另验证工作目录选择与历史往返，见[升级测试报告](GUIDED_RECOVERY_TEST_REPORT_20261104.md)。v0.4.1 修复 APFS 物理盘映射与预检提示，原生双架构各 292 项自动化通过，见[修复报告](APFS_PREFLIGHT_FIX_20261104.md)。v0.4.0 原生双架构各 270 项自动化通过，每架构另有 9 项电脑流程及原有 14 项 Cocoa 回归，见[电脑恢复测试报告与截图](COMPUTER_TEST_REPORT_20261104.md)。[v0.3.0 适配报告](MACOS_TEST_REPORT_20261103.md)记录当时的 161 项自动化、14 项 Cocoa 工作流和缺陷修复；[初次适配验证](releases/0.3.0/VALIDATION.md)保留历史证据。

正式附件的下载后校验结果及发布构建见 [v0.6.0 发布校验记录](releases/0.6.0/PUBLISHED.json)。

## 使用应用包

在“关于本机”确认芯片类型，下载对应的 `DeepRecover-v0.6.0-macos-arm64.zip` 或 `DeepRecover-v0.6.0-macos-x86_64.zip`。解压后把整个 `DeepRecover.app` 拖到“应用程序”，再从 Finder 打开；不需要另装 Python。不要单独复制 `Contents/MacOS` 中的可执行文件。

预览构建未提供 Apple Developer ID 签名和 Apple 公证；本地 ad-hoc 签名不等于 Developer ID 签名。下载带隔离标记的应用可能被 Gatekeeper 拦截。只在确认来源可信后按 Apple 的“系统设置 → 隐私与安全性 → 仍要打开”流程处理，或按下方从源码启动；不要关闭系统安全策略。应用不要求管理员权限。

SHA-256 校验（在 ZIP 所在目录执行）：

```sh
shasum -a 256 DeepRecover-v0.6.0-macos-arm64.zip
```

将结果与 `SHA256SUMS.txt` 中同名文件的摘要比较。Intel 用户将命令中的 `arm64` 改为 `x86_64`。独立 CI 制品中的校验文件仅对应当前架构；正式 Release 的校验文件对应全部平台。

## Android 与 Finder 备份

Android 仍需 Google 官方 [Android Platform Tools](https://developer.android.com/tools/releases/platform-tools) 和手机主动授权 USB 调试。可自行安装官方工具，或在已有 Homebrew 的电脑上执行 `brew install --cask android-platform-tools`。应用按显式配置、`DEEPRECOVER_ADB`、PATH 的优先级寻找 ADB；Finder 启动没有 shell PATH 时，回退检测 `/opt/homebrew/bin/adb` 与 `/usr/local/bin/adb`。不会下载、运行 root 或写入设备文件。

通过终端启动时可以配置其他官方工具位置：

```sh
DEEPRECOVER_ADB="$HOME/Library/Android/sdk/platform-tools/adb" \
  /Applications/DeepRecover.app/Contents/MacOS/DeepRecover
```

只读扫描和备份索引不需要连接 Android。iPhone 在 Finder 创建并完成备份后，应用优先发现 `~/Library/Application Support/MobileSync/Backup`；也可以选择其他备份目录。仅解析稳定且未加密的备份，不访问 iPhone 实时文件系统。macOS 如果拒绝目录访问，请使用系统允许的目录或按系统提示授予该应用必要权限；应用不会自动改变隐私权限或备份内容。

## 本地工作目录与操作

默认工作目录为 `~/Library/Application Support/DeepRecover`，包含数据库、日志、采集副本和导出记录。`DEEPRECOVER_DATA_DIR` 可覆盖，需在启动前设置；不要指向源备份或待扫描介质。移除 `.app` 不会主动删除工作数据，旧开发版本留在 `~/DeepRecover_Data` 的数据也不会自动迁移。需要继续使用旧记录时可显式指定旧目录。

macOS 多选使用 Command/Shift；应用菜单提供 Command+Q。若后台任务仍在执行，退出会先请求停止并保留已提交记录，待任务完成后再次退出。中文界面使用系统可用的 Helvetica Neue/PingFang SC 字体。处理保持本地，普通采集和范围复制不等于已删除数据恢复。

## 从源码运行与构建

安装 Python 3.12，进入工程目录：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py
```

也可运行 `DEEPRECOVER_BUILD_PYTHON=python3.12 bash scripts/bootstrap.sh` 配置环境并启动。

本机架构应与 Python 一致，不使用 Rosetta 将另一架构当作原生构建。运行 `DEEPRECOVER_BUILD_PYTHON=python3.12 bash scripts/build-macos.sh` 生成 `.app`、ZIP 与校验值；Apple Silicon 与 Intel 包需各自原生构建。脚本在测试失败、意外跳过、Mach-O/应用元数据错误或实际应用启动验证失败时停止，不会把 Linux 输出当作 macOS 应用。

`.github/workflows/macos-build.yml` 分别执行原生构建、离屏 Qt 验证和 Cocoa Qt 验证，保留 JSON 报告及应用自身抓取的 PNG 截图。Cocoa 验证仍使用合成数据，不代表真人交互、真实 USB/手机、真实备份、Retina/多屏、Gatekeeper 下载启动、Developer ID 签名或公证已经验收。这些项目继续纳入 M5 实机验收。
