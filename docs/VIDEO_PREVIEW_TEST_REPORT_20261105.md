# 视频预览测试报告

日期：2026-11-05（北京时间）。功能代码 `507397877eaf08cfb3cdbbd76216d2f892772ded`，开发版本 0.7.0；当前已发布版本仍为 v0.6.0。使用方法及支持范围见[视频预览](VIDEO_PREVIEW.md)。

## 交付范围

结果页可从受支持的 Android 本地采集副本、iPhone 备份条目及电脑删除记录中预览自包含 H.264 MP4/MOV。核对来源后，在内存设备上静音显示首帧、播放/暂停和拖动进度。含旋转元数据的手机画面及直接编码的横/竖屏 1080p 均纳入本轮验证。

不创建预览副本，不向播放器传入来源路径或网络 URL，不改变候选状态、删除依据或导出记录。容器检查只是播放准入，不宣称完整结构/比特流校验。视频扫描、修复、HEVC、其他格式及 M5 实机验收不属于本轮已实现范围。

## 自动化回归

Linux Python 3.12 / PySide6 6.8.3 完整 **417 项通过**，0 失败、0 跳过；在 v0.6.0 的 392 项基础上新增 25 项（16 项来源/准入测试，9 项真实 Qt 界面回归）。[原始测试报告](qa/video-20261105/linux/test-report.json)。

覆盖来源身份和摘要、正在运行的任务/目录边界、无扩展名备份 blob、碎片逻辑顺序、输入大小/时长/尺寸/采样限制、播放列表/外部及隐藏引用、分段/加密/不支持编码、取消、无效解码、暂停/拖动、选择/分页/筛选/任务切换与关闭释放。预览准备任务不占普通历史列表，候选与源内容保持原值。

修复并回归两项实测问题：加载阶段直接通过 PySide 释放 QIODevice 可能在等待读取线程时阻塞，现改由 Qt 原生事件队列清除媒体来源后再释放缓冲；原宽高限制误拒绝竖屏 1080p，现按长/短边检查，并验证旋转后实际帧。真实无效媒体也会走失败清理，未把头部准入当作解码成功。

本地完整门禁与截图脚本分别为 `scripts/run_release_tests.py` 和 `scripts/video_qa_capture.py`。当前云端沙箱限制 Qt 本地 IPC，本轮真实多媒体测试在隔离测试目录、允许本地 IPC 的执行模式下运行；未连接用户设备或读取用户数据。测试媒体为自生成的两秒 H.264/AAC 图案与测试音，来源见 `app/assets/README.txt`，没有采集第三方或用户视频。

## 关键流程与截图

7 个 Qt 工作流通过，6 张关键截图；正常路径使用实际播放器与解码器，仅 VQA-05 注入解码错误以核对错误脱敏。其他负向媒体、来源和关闭验证独立由自动化测试覆盖。

| 流程 | 验证内容 | Linux 截图 |
|---|---|---|
| VQA-01 | 真实首帧、仅内存源，存在 AAC 音轨但没有音频输出 | [首帧](qa/video-20261105/linux/01-actual-video-first-frame.png) |
| VQA-02 | 播放推进、暂停、拖动后得到新解码帧 | [播放与拖动](qa/video-20261105/linux/02-playback-pause-and-seek.png) |
| VQA-03 | 改选后旧播放器停止、压缩缓冲及解码帧释放 | [释放](qa/video-20261105/linux/03-selection-change-releases-video.png) |
| VQA-04 | 外部引用在启动解码器之前被拒绝 | [拒绝](qa/video-20261105/linux/04-external-reference-rejected.png) |
| VQA-05 | 不显示后端私有路径，失败后不保留旧画面 | [错误清理](qa/video-20261105/linux/05-decoder-error-clears-frame.png) |
| VQA-06 | 只读历史不准备视频，候选/源字节不变、无预览/导出文件 | [流程指标](qa/video-20261105/linux/evidence.json) |
| VQA-07 | 轨道旋转矩阵实际生效，竖屏帧方向正确，源与候选不变 | [手机竖屏](qa/video-20261105/linux/06-phone-portrait-rotation.png) |

## 原生编译验证

[Windows 构建](https://github.com/Ren-ZH-007/DeepRecover/actions/runs/41951421978)与 [macOS 双架构构建](https://github.com/Ren-ZH-007/DeepRecover/actions/runs/41951427269)均已通过。macOS 每架构 417 项通过，Windows 415 项通过及 2 项既有预期跳过，无新增跳过。macOS 的 PR 合并检出提交为 `157308984174613337449112a0f21a69cbf5e217`，功能头为上方 `5073978`；[验证记录](qa/video-20261105/VALIDATION.json)区分两者。

三平台的编译程序均实际打开视频窗口、解码 96×64 测试帧、静音、播放推进/拖动、关闭清空压缩缓冲与解码帧，并确认来源和候选不变；输出 `video_preview_verified`、`video_muted`、`video_buffer_released` 为 true。原有图片扫描/预览/校验导出、8 页导航、SQLite v3、诊断报告及工作目录历史往返继续通过，Cocoa 原生退出也通过。

每个 macOS 架构通过 41 个源码界面流程、53 张流程截图；Windows 通过 27 个流程、33 张截图。下载后检查全部 **139 张流程截图**的证据摘要、PNG CRC、Qt 解码及非空采样，另 2 张打包应用截图通过完整性检查。仓库保留本轮视频的 24 张关键图（Linux 与三个原生平台各 6 张）；其余完整流程图保留在 Actions 验证制品。

| 平台 | 首帧 | 暂停/拖动 | 错误清理 | 手机竖屏 |
|---|---|---|---|---|
| macOS Apple Silicon | [截图](qa/video-20261105/arm64/01-actual-video-first-frame.png) | [截图](qa/video-20261105/arm64/02-playback-pause-and-seek.png) | [截图](qa/video-20261105/arm64/05-decoder-error-clears-frame.png) | [截图](qa/video-20261105/arm64/06-phone-portrait-rotation.png) |
| macOS Intel | [截图](qa/video-20261105/x86_64/01-actual-video-first-frame.png) | [截图](qa/video-20261105/x86_64/02-playback-pause-and-seek.png) | [截图](qa/video-20261105/x86_64/05-decoder-error-clears-frame.png) | [截图](qa/video-20261105/x86_64/06-phone-portrait-rotation.png) |
| Windows x64 | [截图](qa/video-20261105/windows/01-actual-video-first-frame.png) | [截图](qa/video-20261105/windows/02-playback-pause-and-seek.png) | [截图](qa/video-20261105/windows/05-decoder-error-clears-frame.png) | [截图](qa/video-20261105/windows/06-phone-portrait-rotation.png) |

三个编译包也已重新下载：SHA256SUMS、ZIP CRC、可执行架构、macOS bundle 版本/执行位/符号链接及 Windows Qt 插件检查均通过。它们是 PR 测试制品，尚未创建 v0.7.0 Release；准确包名、摘要、下载链接及原生测试报告见验证记录，受仓库权限和 Actions 制品保留期限限制。

## 边界

本轮只使用合成视频/镜像和受控来源，不扫描云端真实磁盘。原始盘预检的同盘或未知映射拒绝沿用现有机制，真实硬盘/U 盘及手机仍待 M5。原生 runner 的自动化不代表高 DPI/多屏、真实长视频与各种手机编码、安装程序、Developer ID 签名或 Apple 公证已验收。不能把普通副本播放、签名候选或摘要一致描述为已删除数据恢复成功。
