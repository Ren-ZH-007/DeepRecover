# M5 本地诊断与验收准备测试报告

日期：2026-11-05（北京时间）。功能提交 `31e8c6dff69dbe871bba204ee510aacc39537005`，开发版本 0.6.0；当前已发布版本仍为 v0.5.0。本轮交付验收准备工具，不宣称 M5 实机验收完成。

## 功能与只读边界

设置页可查看本机系统与架构、应用启动形式、Qt 显示环境、工作目录容量及 ADB 工具是否可定位。后台检查只读元数据，不运行 ADB、不枚举设备、扫描备份或打开源内容，不建立任务或改变数据库。容量可读不等于写入已测试，工具存在不等于手机已授权。

用户明确保存时，先检查工作目录与当前原始磁盘隔离，包含尚未检测分区的高亮磁盘。来源、工作目录身份改变或窗口关闭会阻止保存。报告只写入当前工作目录已有 Reports 下的新独占文件；不覆盖已有文件，拒绝链接重定向。固定字段清理路径、序列号、卷名、屏幕名称、额外字段和异常原文。报告的 9 个实机大类始终保持 `not_run`，手工细项按 [M5 清单](M5_ACCEPTANCE.md)另行记录。

## 自动化与缺陷回归

Linux Python 3.12 / PySide6 6.8.3 完整回归 **392 项通过**，0 失败、0 跳过，在 v0.5.0 的 364 项基础上新增 28 项。覆盖隐私字段闭合、零源内容读取/零设备命令、元数据失败、取消检查、报告防覆盖/重定向/共享文件、忙任务门禁、同盘及未知映射、来源/目录变更、异步关闭及窗口清理。

本轮发现并修复两处集成/测试问题：诊断窗口关闭处理最初插入了结果页清理位置，导致旧依据窗引用未清除；现已恢复独立清理并验证主窗口关闭时诊断窗同步关闭。另修正 macOS 系统版本元数据 provider 的测试隔离，避免把系统版本 plist 与用户源文件混为一类，并增加 darwin 分支验证。

本地执行命令：

```sh
QT_QPA_PLATFORM=offscreen XDG_CACHE_HOME=/workspace/.cache .venv/bin/python scripts/run_release_tests.py --report /workspace/.deeprecover-setup/m5-tests-20261105.json
QT_QPA_PLATFORM=offscreen QT_QPA_FONTDIR=/workspace/.deeprecover-setup .venv/bin/python scripts/diagnostics_qa_capture.py --output /workspace/.deeprecover-setup/m5-qa-20261105
```

该字体目录为本轮环境中已校验的 CJK 测试字体位置，不随应用分发。[测试报告](qa/m5-20261105/linux/test-report.json)与[流程指标](qa/m5-20261105/linux/evidence.json)保存原始结果。

## 关键流程与截图

4 个真实 Qt 诊断流程全部通过；仅负向用例注入错误，正常检查和保存调用实际服务。合成来源摘要、工作数据库、任务及来源记录均保持不变。

| 流程 | 验证点 | Linux 截图 |
|---|---|---|
| DQA-01 | 读取环境，未主动保存前没有报告，实机项目全部未执行 | [环境检查](qa/m5-20261105/linux/01-local-environment-check.png) |
| DQA-02 | 新建一份脱敏本地报告，数据库/任务不变 | [保存成功](qa/m5-20261105/linux/02-sanitized-report-saved.png) |
| DQA-03 | 保存异常不泄露原文，不覆盖已有报告 | [保存失败](qa/m5-20261105/linux/03-report-error-redacted.png) |
| DQA-04 | 重查失败清除旧报告，禁用保存，异常原文不进入页面 | [检查失败](qa/m5-20261105/linux/04-check-error-redacted.png) |

## 原生构建

[Windows 原生构建](https://github.com/Ren-ZH-007/DeepRecover/actions/runs/41942714396)与 [macOS 双架构构建](https://github.com/Ren-ZH-007/DeepRecover/actions/runs/41942744276)均已通过。macOS 两种架构各 392 项通过，Windows 390 项通过、2 项既有平台测试按预期跳过；无新增跳过。macOS 实际测试 PR 合并检出提交为 `c354dafe7b67a97c8badd18aa84ca72ba49bb873`，功能头为上方 `31e8c6d`；准确来源、制品链接和下载后校验见[验证记录](qa/m5-20261105/VALIDATION.json)。

每个平台的打包程序均实际打开诊断窗口、保存一份新脱敏报告，并确认任务/数据库不变，输出 `environment_diagnostics_verified=true`。原有 8 页导航、SQLite v3、PNG/JPEG、扫描→预览→校验导出、工作目录历史往返仍通过；macOS 另验证 Cocoa 原生退出。源码界面流程与打包检查分别记录，不能当作真人实机交互。

每个 macOS 架构通过 34 项界面流程、47 张流程截图；Windows 通过 20 项流程、27 张截图。下载后核验全部 **121 张流程截图**的 SHA-256、PNG CRC、Qt 解码和非空采样，另 2 张打包应用截图也通过完整性检查。入库保留 16 张本轮诊断关键图；其余流程原图保存在 Actions 验证制品，受仓库权限与保留期限限制。

| 平台 | 环境检查 | 保存成功 | 保存异常 | 重查异常 |
|---|---|---|---|---|
| macOS Apple Silicon | [截图](qa/m5-20261105/arm64/01-local-environment-check.png) | [截图](qa/m5-20261105/arm64/02-sanitized-report-saved.png) | [截图](qa/m5-20261105/arm64/03-report-error-redacted.png) | [截图](qa/m5-20261105/arm64/04-check-error-redacted.png) |
| macOS Intel | [截图](qa/m5-20261105/x86_64/01-local-environment-check.png) | [截图](qa/m5-20261105/x86_64/02-sanitized-report-saved.png) | [截图](qa/m5-20261105/x86_64/03-report-error-redacted.png) | [截图](qa/m5-20261105/x86_64/04-check-error-redacted.png) |
| Windows x64 | [截图](qa/m5-20261105/windows/01-local-environment-check.png) | [截图](qa/m5-20261105/windows/02-sanitized-report-saved.png) | [截图](qa/m5-20261105/windows/03-report-error-redacted.png) | [截图](qa/m5-20261105/windows/04-check-error-redacted.png) |

三个 PR 编译包已下载并核对 SHA256SUMS、ZIP CRC、可执行架构、macOS bundle 版本/执行位/符号链接，以及 Windows Qt 插件。它们是测试制品，不是新的 Release；下载及各包准确摘要见验证记录，避免与 v0.5.0 正式附件混用。

## 尚未验收

真实手机/备份、硬盘与 U 盘、Windows 10/11 交互、高 DPI/多屏、macOS 13/14、Gatekeeper 下载启动、安装程序及签名公证仍待实机验收。云端测试使用合成数据，不访问真实原始设备；macOS 原生元数据检查仅查询系统清单。普通文件采集、图片签名候选及摘要一致都不等于已删除数据恢复成功；APFS/HFS+ 仍不解析删除记录。
