# v0.9.0 PDF 扫描与静态预览测试

日期：2026-11-05（北京时间）。[v0.9.0 预览版已发布](https://github.com/Ren-ZH-007/DeepRecover/releases/tag/v0.9.0)，正式附件与三平台门禁核验完成。M5 实机按要求跳过并保留未执行标记。

新增传统交叉引用 PDF 的范围检查、文档筛选和分页/缩放预览，并接通既有校验导出与历史。静态支持边界、候选 EOF 范围和资源限制见[PDF 说明](PDF_PREVIEW.md)。

## 验证结果

Linux x86_64 / Python 3.12 / PySide6 6.8.3：全量 **493/493 通过、0 跳过**，其中本轮新增 46 项核心与界面测试。新 PDF Qt 工作流 **8/8 通过、9 张截图**，使用两页原创合成 PDF；另验证了 Qt 自己生成的含嵌入字体 PDF。[完整测试门禁](qa/pdf-20261105/linux/test-report.json)、[界面证据](qa/pdf-20261105/linux/evidence.json)。

正式发布源提交 `16de471db47ef96f3b67c1035f97a94655408dab`，为 PR #22 的合并提交；应用代码与最终受测功能提交 `96f8909` 一致。北京时间 2026-11-05 18:52:53 发布，运行：[三平台标签构建](https://github.com/Ren-ZH-007/DeepRecover/actions/runs/42013701376)。发布前原生运行：[Windows](https://github.com/Ren-ZH-007/DeepRecover/actions/runs/42012242581)、[macOS 双架构](https://github.com/Ren-ZH-007/DeepRecover/actions/runs/42012252845)。

| 平台 | 全量自动化 | 全部界面工作流 / 截图 | 打包验证 |
|---|---|---|---|
| Windows x64 | 491 通过 / 2 项既有跳过 | 43 / 50 | 实际 EXE、windowed PDF 管道与导出通过 |
| macOS Apple Silicon | 493 通过 / 0 跳过 | 57 / 70 | 原生 .app、offscreen / Cocoa 与 PDF 子进程通过 |
| macOS Intel | 493 通过 / 0 跳过 | 57 / 70 | 原生 .app、offscreen / Cocoa 与 PDF 子进程通过 |

新 PDF 流程在各平台各 8 项、9 张截图。正式构建 190 张工作流截图和 2 张打包 Cocoa 截图均已核验 PNG、SHA-256、解码尺寸与非空白像素；全部工作流、只读来源、打包 PDF 门禁通过。

已重新下载 Release 的 3 个 ZIP 和摘要清单，核对 SHA-256、CRC、版本、PE/Mach-O 架构及 macOS 可执行权限/框架符号链接。见[正式发布记录](releases/0.9.0/PUBLISHED.json)及[摘要清单](releases/0.9.0/SHA256SUMS.txt)，原始测试/冒烟 JSON 和 PDF 新流程截图位于其 validation 子目录。macOS 仍为 ad-hoc 签名，未进行 Developer ID 签名或公证。

macOS 原生门禁曾在系统默认 UI 字体生成的嵌入字体 PDF 上触发 8 秒超时；该回归样本现显式使用 Arial（系统缺失时由 Qt 本地替代），三平台通过。未放宽资源保护，不据此保证所有系统字体可渲染。手动关闭 PDF 后主窗口状态反馈也已修正并验证。

## 核心覆盖

- 头部跨块、绝对偏移、精确 EOF、相邻 PDF、流内伪 startxref/EOF、间接 Length、xref 错位/对象身份、未索引/重叠对象、页树计数/循环/父引用及缺失对象。
- 加密、转义名称下的活动内容、表单、附件、外部文件/URL、对象流/交叉引用流和增量修订分别受限；普通字符串中的 `/JavaScript` 等文字不误报活动内容。
- 文件/对象/语法/页数/字符串/嵌套/解压预算、无最终换行的合法 EOF、截断、分区边界与取消；400 次可重复元数据突变不出现未处理异常。
- 普通镜像与电脑分区扫描、授权 Android 副本、iPhone 扩展名缺失 blob、元数据已知的碎片范围顺序，来源只读、摘要校验、源变化拒绝及历史源缺失。
- 实际子进程渲染两页不同像素与缩放；取消、超时、内存阈值和异常协议会结束子进程；UI 停止/重试、改选/导航/新任务/退出释放数据并清除旧画面。
- 原始盘预检拒绝发生在数据库预览任务和渲染器启动之前；精确范围导出及源/目标摘要、候选状态保留、文档过滤和历史回读。

打包冒烟增加 PDF 专用门禁：在冻结程序中实际启动 PDF 工作进程（含 Windows windowed 管道）、扫描→两页解码→缩放→释放→校验导出→工作目录切换后的历史回读。门禁报告须提供 pdf_scan_verified、pdf_preview_verified、pdf_worker_verified、pdf_buffer_released、2 页和 1 项导出。

开发测试发现并修正：PDFium 虚拟地址预留不能直接视为驻留内存；伪 EOF（包括伪交叉引用流）的错误偏移不能提前屏蔽后续有效 EOF；字体资源名 A 不应被误认为动作；EOF 无最终换行属于合法布局。相关测试已覆盖。运行器内部内存采样与超时不替代所有原生库分配的硬配额。

## 关键截图

下图来自正式标签的 Apple Silicon 构建，为未编辑的合成数据截图；不是从用户数据提取的文件。

![PDF 对象与边界依据](releases/0.9.0/validation/arm64/pdf-qa/02-pdf-structure-evidence.png)

![实际渲染第二页](releases/0.9.0/validation/arm64/pdf-qa/04-second-page-actual-render.png)

![缩放后的页面](releases/0.9.0/validation/arm64/pdf-qa/05-document-zoom.png)

M5 真实手机、备份、介质及安装体验未执行，不据此计算删除恢复成功率。Windows 原有两项路径替换/符号链接测试沿用明确跳过，不新增 PDF 跳过。
