# DeepRecover

个人自用、本地运行的手机与电脑数据恢复工作台。目标平台：Windows 10/11 x64、macOS 13+（Apple Silicon / Intel），Python 3.12 + PySide6 + SQLite。

**当前预览版本：[v0.10.1](https://github.com/Ren-ZH-007/DeepRecover/releases/tag/v0.10.1)，已发布 Windows 和 macOS 双架构包：签名扫描跳过现存文件、达到上限后可继续扫描、更清楚的磁盘权限提示、结果默认隐藏系统文件；v0.10.0 新增断点续扫、Office 与音频签名扫描、APFS 删除记录与快照。** 电脑功能的范围与使用方法见 [电脑恢复](docs/COMPUTER_RECOVERY.md)，macOS 启动说明见 [macOS 适配](docs/MACOS.md)。

v0.8.0 的支持范围与边界见[视频签名扫描](docs/VIDEO_SIGNATURE_SCAN.md)及[测试与截图](docs/VIDEO_SIGNATURE_TEST_REPORT_20261105.md)，正式附件下载核验见[发布记录](docs/releases/0.8.0/PUBLISHED.json)。签名候选的删除状态未知，不重组碎片或推断原文件尾随数据。

v0.10.0 新增的 Office 文档签名扫描（docx / xlsx / pptx、doc / xls / ppt）及碎片文件的限制见[说明](docs/OFFICE_SIGNATURE_SCAN.md)，音频签名扫描（WAV / MP3 / M4A）见[说明](docs/AUDIO_SIGNATURE_SCAN.md)，APFS 删除记录与快照见[说明](docs/APFS_DELETED_RECORDS.md)；正式附件、摘要和三平台门禁核验见 [v0.10.0](docs/releases/0.10.0/PUBLISHED.json) 和 [v0.10.1](docs/releases/0.10.1/PUBLISHED.json) 的发布记录。

v0.9.0 的 PDF 支持范围见[说明](docs/PDF_PREVIEW.md)及[测试与截图](docs/PDF_TEST_REPORT_20261105.md)，正式附件、摘要和三平台门禁核验见[发布记录](docs/releases/0.9.0/PUBLISHED.json)。候选删除状态未知，M5 实机按要求保留未执行状态。

依据《DeepRecover_当前对话归档与研发方案_20261102.md》创建。

0.5.0 的图形化工作目录、物理磁盘来源树、三步预检及中文结果依据见[使用与验证说明](docs/GUIDED_RECOVERY.md)，该版本附件校验见[历史发布记录](docs/releases/0.5.0/PUBLISHED.json)。

0.6.0 已发布：设置页新增本机环境检查和脱敏本地报告，为 M5 实机验收提供准备工具。该版本附件核验见[历史发布记录](docs/releases/0.6.0/PUBLISHED.json)。诊断不运行 ADB、不枚举设备或读取源内容，也不自动完成实机验收，操作及记录模板见 [M5 验收清单](docs/M5_ACCEPTANCE.md)，验证与截图见[测试报告](docs/M5_PREPARATION_TEST_REPORT_20261105.md)。

## 已实现

0.7.0 已发布：新增 MP4/MOV 静音视频预览、播放/暂停和进度拖动；来源只读，内容在内存中播放，不生成预览副本。首版支持 H.264、64 MiB/10 分钟/横竖屏 1080p，详见[视频预览](docs/VIDEO_PREVIEW.md)和[测试报告与截图](docs/VIDEO_PREVIEW_TEST_REPORT_20261105.md)。正式附件下载后核验见[发布记录](docs/releases/0.7.0/PUBLISHED.json)。

- 分层工程、虚拟环境依赖清单、入口和 8 个导航页面。
- 电脑恢复入口：Windows 本地磁盘/U 盘、macOS 磁盘、原始镜像；MBR/扩展分区及 GPT 识别，源只读，原始盘工作目录/导出目标必须位于另一物理盘。
- NTFS 删除记录与碎片布局、FAT32 空闲单簇、exFAT 明确连续布局候选；分配已重用或布局不明的记录保留为受限项。APFS 删除记录与快照（v0.10.0）只支持未加密、单卷的外接盘和镜像：快照里还在、当前已删除的文件按快照版本列出；其余删除记录中数据块被再使用或曾被覆盖的保留为受限项，见[说明](docs/APFS_DELETED_RECORDS.md)和[可行性研究](docs/APFS_FEASIBILITY.md)。HFS+ 提供 JPG/PNG/MP4/MOV/PDF/Office 文档/音频连续签名候选，不解析删除记录。
- 深蓝导航、浅色工作区、三步总览入口、卡片分区与统一图标；结果预览、分页工具栏和导出历史使用同一视觉主题。
- 工作目录初始化、轮转日志、SQLite v3 迁移、数据源/候选/导出记录（兼容 v1/v2 历史记录）。
- 后台任务执行、协作式取消、成功/失败/中断状态及任务历史。
- ADB 可用性检测、已授权/未授权/offline/no permissions 状态解析。
- 选择已授权 Android 设备、共享目录和普通文件，确认后只读 `adb pull` 到本地；记录设备、原始路径、本地副本、采集时间和本地 SHA-256。
- iPhone 常用备份目录发现、自选目录、备份元数据和加密状态识别。
- 未加密 Manifest.db 只读解析、候选入库，缺失文件显式标记。
- 候选按任务每页 100 条展示；历史任务结果可重开，失败/取消任务保留已提交的部分记录。
- 本地文件/镜像的 JPG、PNG、MP4、MOV、PDF、Office 文档（docx / xlsx / pptx、doc / xls / ppt）以及音频（WAV、带 ID3v2 标签的 MP3、M4A）连续签名扫描，记录源内偏移、长度和结构检查结果；支持后台进度与取消。签名扫描只能恢复连续存放的文件，不拼接碎片，见[说明](docs/OFFICE_SIGNATURE_SCAN.md#碎片文件)。
- 类型与状态筛选、JPG/PNG 图片、H.264 MP4/MOV 静音视频及受支持 PDF 静态分页预览；本页选中项或跨页全部筛选结果的批量导出。
- 防覆盖导出、源范围与目标 SHA-256 对照、可回读的导出记录及本地 JSON 清单。
- macOS 独立工作目录、Finder 备份发现、Homebrew ADB 发现与原生退出菜单；Apple Silicon / Intel 分别原生构建 `.app`。
- 完整自动化回归与原生平台构建门禁；打包后的程序需实际启动并完成扫描→预览→校验导出的合成数据流程。

## 下载预览包

- [Windows x64](https://github.com/Ren-ZH-007/DeepRecover/releases/download/v0.10.1/DeepRecover-v0.10.1-windows-x64.zip)
- [macOS Apple Silicon](https://github.com/Ren-ZH-007/DeepRecover/releases/download/v0.10.1/DeepRecover-v0.10.1-macos-arm64.zip)
- [macOS Intel](https://github.com/Ren-ZH-007/DeepRecover/releases/download/v0.10.1/DeepRecover-v0.10.1-macos-x86_64.zip)
- [SHA256SUMS.txt](https://github.com/Ren-ZH-007/DeepRecover/releases/download/v0.10.1/SHA256SUMS.txt)

Windows 解压后运行目录中的 `DeepRecover.exe`，保留整个 `DeepRecover` 文件夹及 `_internal` 文件，不能只复制 EXE。macOS 按芯片类型下载，解压后将整个 `DeepRecover.app` 拖入“应用程序”。预览包无需另外安装 Python，Windows 包不是安装程序；源码归档则按下方步骤配置 Python。macOS 包未提供 Developer ID 签名或 Apple 公证，详见 [macOS 说明](docs/MACOS.md)。

v0.5.0 界面截图：[选择工作目录](docs/qa/guided-20261104/arm64/01-workspace-before-initialization.png)、[物理磁盘分组](docs/qa/guided-20261104/arm64/14-grouped-physical-sources.png)、[结果依据](docs/qa/guided-20261104/arm64/02a-deleted-record-evidence.png)。

v0.6.0 诊断截图：[环境检查](docs/qa/m5-20261105/arm64/01-local-environment-check.png)、[保存本地报告](docs/qa/m5-20261105/arm64/02-sanitized-report-saved.png)。

v0.7.0 视频截图：[播放与拖动](docs/qa/video-20261105/arm64/02-playback-pause-and-seek.png)、[手机竖屏](docs/qa/video-20261105/arm64/06-phone-portrait-rotation.png)。

v0.8.0 扫描截图：[采样范围依据](docs/releases/0.8.0/validation/arm64/video-signature-qa/03-container-and-sample-evidence.png)、[扫描出的 MOV 预览](docs/releases/0.8.0/validation/arm64/video-signature-qa/05-scanned-mov-decoded-frame.png)、[精确范围导出](docs/releases/0.8.0/validation/arm64/video-signature-qa/06-verified-range-exports.png)。

v0.9.0 PDF 截图：[结构依据](docs/releases/0.9.0/validation/arm64/pdf-qa/02-pdf-structure-evidence.png)、[实际第二页](docs/releases/0.9.0/validation/arm64/pdf-qa/04-second-page-actual-render.png)、[缩放](docs/releases/0.9.0/validation/arm64/pdf-qa/05-document-zoom.png)。

## Windows 启动

先安装 Python 3.12 x64，进入解压后的 DeepRecover 文件夹：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

也可运行 `scripts/bootstrap.ps1` 完成上述步骤（不修改系统执行策略）。
需要联网安装依赖。依赖安装完成后，应用无需云服务。

Android 检测需要单独安装 Google Android Platform Tools；将其目录加入 PATH，或设置：

```powershell
$env:DEEPRECOVER_ADB = 'C:\Android\platform-tools\adb.exe'
```

通过 USB 连接设备并在手机上主动允许 USB 调试。在 Android 页检测设备、选择设备、列出共享目录、选中文件（Ctrl/Shift 多选），点击采集并确认。没有选中项或取消确认时不会采集。采集与列表始终绑定到所选序列号，每次 pull 前重新检查授权。

本版只允许 `/sdcard` 或 `/storage/emulated/0` 下的普通文件，使用 `find -type f -print0` 列表、`readlink -f` 检查解析后路径、`test` 检查文件类型和 `adb pull` 读取；不调用 root、push、删除或修改源文件的命令。需要设备提供相应 shell 工具，目录列表超过 10,000 项会明确失败，需选择更小目录，不会静默截断。控制字符或非 UTF-8 文件名不支持；符号链接文件不采集。设备仍可能在校验和复制之间改变路径或内容，采集不提供源端快照或取证写保护保证。

副本存入工作目录 `Acquisitions`，采用自动生成的独占文件名，原始名称仅作元数据，不作为本地写入路径。失败/超时/取消的当前临时 pull 会清理；此前成功且已入库的文件保留。ADB pull 超时 60 秒，其他调用超时 15 秒；取消在调用结束及本地哈希读取期间检查。ADB 检测可能启动本机 ADB 服务。第三方工具未随源码包分发。

## 工作数据

默认 `%LOCALAPPDATA%\DeepRecover_Data`，未设置 LOCALAPPDATA 时使用用户主目录。
macOS 默认使用 `~/Library/Application Support/DeepRecover`，不会在 `.app` 或原始备份中创建工作数据库。
可设置 `DEEPRECOVER_DATA_DIR` 指向独立工作目录；不要指向原始备份目录或待恢复介质。
目录包括 Devices、Backups、Acquisitions、ScanCache、Recovered、Reports、Logs 和 index.db。
设备序列号、备份/镜像来源、候选原始路径、本地路径、采集时间、文件身份及来源范围 SHA-256 保存在本地数据库，可在结果页查看。日志不记录文件内容、完整设备序列号或异常明文。导出清单也包含来源路径等本地敏感元数据，请保护工作目录及导出目录。

备份入口自动检查 `%USERPROFILE%\Apple\MobileSync\Backup` 和 `%APPDATA%\Apple Computer\MobileSync\Backup`。
macOS 优先发现 Finder/iTunes 的 `~/Library/Application Support/MobileSync/Backup`。
用户可选择 Backup 根目录或单份备份目录。加密备份只识别，不解密。
索引只读取稳定的 Manifest.db：不会创建或更新源目录的 WAL/SHM 伴随文件，遇到非空 WAL/事务日志会拒绝索引，提示先在备份工具中完成备份；应用不会替用户 checkpoint、删除或合并源日志。已正常 checkpoint、关闭且无待合并日志的 WAL 模式库可只读访问。Manifest 链接不得逃出所选备份根目录，读取期间索引文件身份/大小/时间变化会使任务失败，不冒称完整索引。
界面使用只读流式遍历全部普通文件记录（丢弃无效 fileID），每 100 项提交一次，不再受旧界面 10,000 条上限限制。结果页从本地数据库分页查询，不一次载入完整历史结果。底层兼容函数 `read_manifest(..., limit=10000)` 保留旧调用上限，新界面使用 `iter_manifest`。
文件名和路径只作为显示元数据，不用于导出写入。
候选 `integrity=not_checked` 表示结构未验证；`missing` 表示备份文件缺失。
Android SHA-256 仅是本地副本的摘要，不是源端哈希比对或文件结构验证；状态仍是 candidate，不标记 validated、exported 或成功恢复。

任务记录显示最近 200 个非预览任务，选择后点击“回读选中任务的结果”或双击任务。索引任务打开分页候选，导出任务打开每页 100 条的导出对照记录。历史回读无需重新连接设备或读取源备份，不代表目标文件已重新校验。旧 v1 记录没有独立数据源时会明确提示，不推测来源。历史路径可能已失效，不会自动重新采集。取消、失败、中断不等于完整完成，尚未提交的索引批次不显示。异常断电或本地数据库写入失败可能留下无索引副本，不自动删除已有采集文件；不支持断点续采。

## 本地 JPG/PNG 签名扫描

新增“电脑恢复”页面可读磁盘或镜像中的删除记录，并复用预览、分页、校验导出与历史。任意扩展名的文件可来自受支持的删除元数据；图片预览仍限 JPG/PNG。参见 [支持矩阵和源盘保护](docs/COMPUTER_RECOVERY.md)。

在“文件扫描”页选择稳定的本地普通文件或镜像（例如 `.img`、`.dd`、`.bin`），然后开始扫描。输入始终以 `rb` 打开，不支持直接扫描 USB 设备、原始块设备、目录或本程序工作数据库/日志。来源模型保存镜像路径，结果页显示十进制源内偏移和长度；不会创建提取副本，也不把签名记录当成成功恢复。可在本地采集副本上扫描，普通复制仍不等同于已删除数据恢复。

- 按 1 MiB 块搜索并保留 7 字节重叠，支持跨块 PNG/JPEG 签名；结构检查通过的完整容器整体跳过，内部缩略图/嵌套签名不单独报告。无效或截断容器不隐藏后续签名。
- PNG 检查 IHDR、基本关键块顺序/调色板、每块 CRC、IDAT 的 zlib 完结、解压扫描行长度和滤波类型，支持标准位深与 Adam7 布局计数。不进行像素反滤波、色彩解释或完整 PNG 语义校验。
- JPEG 支持 8 位基线与渐进式 Huffman 编码，检查 SOI/SOF/SOS/EOI、段长、尺寸、组件、量化/Huffman 表引用及基本表布局、字节填充和重启标记；不熵解码图像。算术、无损等其他编码模式不在本里程碑支持范围。
- 默认单个候选最多检查 64 MiB，PNG 解压检查上限 256 MiB，总候选最多 10,000 项；读取/解压均分块并检查取消。达到候选数上限后遇到更多签名会使任务失败并明确提示，保留此前已入库结果，不静默宣称完成。
- `structure_checked`：支持的结构检查通过，状态仍为 `candidate`；`truncated`：源文件结束前未完成容器，状态为 `partial`；`invalid_structure`：结构错误，状态为 `failed`；`validation_limit`：大小/解压资源上限或不支持的 JPEG 模式，状态为 `partial`。后两种不推断文件可恢复性，不给出恢复概率。
- 完整容器的长度到 IEND/EOI 为止。截断长度是从签名到源文件结尾的受限范围；错误/受限长度是检查停止的位置，并非已确认文件长度。扫描阶段不提取副本，也不标记 validated/exported；M4 允许用户明确选择结构通过且来源校验通过的候选进行预览或导出。
- 扫描进度是运行期的搜索范围与候选数快照；只有遍历和源身份检查完成后才达到 100%，不代表候选全部有效。每个候选独立入库；取消/失败保留已提交记录，任务状态可在历史中回读。签名扫描约每 2 秒记录一次断点：中断、停止或读取失败后，可在“任务记录”中选择“从断点继续”，先只读核对来源身份并重新计算最近已校验候选的 SHA-256，一致才继续，新候选并入原任务，结果与一次扫完相同。删除记录解析不支持续扫，需重新扫描；不会自动续扫。
- 扫描结束检查文件身份、大小和修改/变更时间；检测到扫描期间变化就使任务失败。该检查不是源文件快照或全文件哈希证明；历史路径和字节可能随后变化。不能用当前结果推断文件曾被删除，也不提供文件系统分析、碎片重组或取证写保护。

## 图片预览、筛选与批量导出

在结果页按类型（图片/视频/音频/文档/其他）和候选状态筛选，筛选条件同时用于总数、分页和“导出全部筛选结果”。类型分类来自文件类型元数据，不证明真实格式。Ctrl/Shift 可多选本页记录；没有选择时不导出。“导出全部筛选结果”包含其他页，单批最多 10,000 项，超限需要进一步筛选或分批处理。

选择单张 JPG/PNG 后点击预览。程序从本地来源读出有界数据、检查来源后，在后台用 Qt 根据内容格式解码，只在内存显示，不生成预览文件、不更改原候选状态。图片预览输入上限 32 MiB、1,600 万像素、Qt 解码分配上限 64 MiB，显示最多 1024×768。取消在读取期间及原生解码前后检查，不能中断正在执行的 Qt 原生解码。其他图片格式尚未支持。0.7.0 起另支持选中 MP4/MOV 后打开独立静音视频窗口，限制与使用方法见[视频预览](docs/VIDEO_PREVIEW.md)。解码失败不显示旧图，也不标记恢复成功。

macOS 使用 Command/Shift 多选；应用菜单的 Command+Q 经过任务停止检查，正在执行的任务需先结束再退出。

预览和导出共同检查来源绑定、读取边界、文件身份/大小/时间及已知摘要：

- Android：只访问工作目录 Acquisitions 中的副本，核对采集时 SHA-256；有文件身份信息时也核对身份。保留有效摘要的 M2 旧副本可使用，不会重新读取手机。
- iPhone：只访问所选备份根目录以内的普通文件，核对索引时保存的文件身份和大小。索引阶段不计算整个备份的内容哈希；导出时计算实际读取内容的 SHA-256。
- 镜像：只访问绑定镜像内记录的范围，仅允许 `structure_checked / candidate`。M4 新扫描记录整个源文件身份和该范围 SHA-256，访问时重新核对；部分/错误/受限扫描候选拒绝导出。
- v1/v2 历史仍可查看；缺少必要来源绑定或身份/范围摘要的旧索引和旧扫描会提示重新索引/扫描，不猜测新路径，也不把当前文件冒认为原始来源。源文件移动、替换或内容变化可能需要重新创建记录。

选择本地导出目录并确认后，每批创建新的 `export-...` 子目录。文件使用生成的 UUID 名称与受限扩展名，以独占创建方式写入，不用原名称/路径拼接输出，不覆盖已有文件。禁止将导出目录放在任何已登记的 iPhone 备份或采集副本目录内，即使该备份不是当前批次的数据源。先流式读取源范围并计算 SHA-256，检查已知摘要及源身份，再重新读取目标核对长度和 SHA-256；相同才将独立导出记录标记 `exported`。这只证明本次源范围与目标字节一致，不证明源端手机数据未变化、文件结构完全有效或数据曾被删除。普通文件复制不等于已删除数据恢复。

每批 `manifest.json` 包含选择/尝试/成功/失败/未尝试数量、来源元数据、偏移、字节数、输出位置、源/目标摘要和逐项结果。SQLite v3 同时保存导出记录，原始候选状态和摘要不被改写。正常失败或取消会清理当前未校验输出，保留此前已校验文件；有失败项时批次任务标记失败，成功项仍可使用。取消可以留下未尝试项，不会把它们记为成功。导出进度按已处理文件数显示，100% 不代表每项成功，应查看逐项状态。

断电、介质/权限故障或数据库故障可能留下未完成文件，不能把目录内存在文件视为导出成功。重启将未完成任务及 running 导出记录标记 interrupted；不自动删除/续导/重新校验旧目标，清单也可能未完整写出。导出不提供源快照或取证写保护，源文件仍应保持稳定。扫描、预览和导出均在本地运行，无上传请求。

## 测试与构建

从工程根目录运行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

无 Qt 环境可用标准 Python 运行核心测试；UI 测试会明确跳过。
2026-11-03 详细 QA：100 项自动化及 12 项独立 Qt 流程通过，修复源保护、任务完成结果留存和终态文案等 6 个缺陷。修复前复现、18 张节点截图、执行指标及未验收边界见 [详细 QA 报告](docs/DETAILED_QA_20261103.md)。截图复现：`QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/qa_capture.py`（Linux）。
macOS 适配后追加详细测试：161 项自动化，两种架构各 14 项 Cocoa 流程与 20 张流程截图，修复 Finder 发现、最小窗口、长路径及构建验证等 8 处问题；[v0.3.0 报告与下载包](docs/MACOS_TEST_REPORT_20261103.md)。v0.4.0 电脑恢复扩展增加至 270 项，Windows 为 268 通过、2 项既有平台跳过，macOS 双架构各 270 通过；每个平台另有 9 项电脑流程、12 张截图，见[电脑恢复测试报告](docs/COMPUTER_TEST_REPORT_20261104.md)。真实原始盘与 USB 仍待验收。v0.4.1 APFS 预检修复增加至 292 项，macOS 双架构各 292 通过，Windows 290 通过、2 项既有平台跳过，见[修复报告](docs/APFS_PREFLIGHT_FIX_20261104.md)。
可选界面检查：`python scripts/check_ui.py`，输出 `docs/interface-preview.png`。
Windows 构建脚本：`scripts/build-windows.ps1`，必须在原生 Windows + Python 3.12 x64 上运行。脚本先运行发布测试门禁，再用 PyInstaller 6.12.0 生成目录式 EXE，实际启动 EXE 的 `--smoke-test --smoke-report` 隔离验证流程，通过后才生成 Windows ZIP 和 SHA256SUMS.txt。Linux 不能直接交叉生成 Windows EXE。

macOS 构建脚本：`scripts/build-macos.sh`，必须在原生 macOS + 对应架构的 Python 3.12 上运行；先测试再构建，并验证 `.app` 内 Mach-O 架构、元数据及实际运行。CI 分别保存 Apple Silicon / Intel ZIP、校验值、离屏与 Cocoa 验证报告及真实窗口截图，见 [macOS 文档](docs/MACOS.md)。

`.github/workflows/windows-release.yml` 在功能分支构建验证；版本标签 `v*` 触发时同时要求 Windows 与 macOS 双架构构建和打包后检查通过，才会发布预发布版本。验证覆盖 8 页导航、SQLite v3、PNG/JPEG 插件、扫描、预览、原有两项及电脑两项校验导出。测试报告与应用报告由 CI 保存；仅允许 Windows 上两个已声明的文件替换/符号链接权限测试跳过，其他失败或跳过均阻止发布。原生 runner 的自动检查不代表真实手机、高 DPI/桌面体验、签名公证或安装包验收已经完成。

## 工程结构

- `app/`：启动、配置、日志、主窗口和路由。
- `app/theme.py`、`app/version.py`：统一配色/矢量图标与应用版本。
- `ui/`：页面拆分预留包，当前页面组装位于 app/main_window.py。
- `core/device/`：Android 与 iPhone 适配。
- `core/acquisition/android.py`：用户选择的普通文件采集与本地副本摘要。
- `core/parser/ios_backup/`：未加密备份索引读取。
- `core/tasks.py`：后台任务与取消。
- `core/scanner/base.py`：只读扫描引擎协议。
- `core/scanner/signature/scanner.py`：有界只读签名扫描、偏移与候选流。
- `core/scanner/image/validation.py`：PNG/JPEG 容器结构检查，不写入提取文件。
- `core/candidate_io.py`：有来源绑定与边界检查的只读候选数据访问。
- `core/preview/images.py`：有界 JPG/PNG 内存预览。
- `core/export/service.py`：独占输出、范围复制、摘要对照和本地导出清单。
- `ui/recovery/export_history.py`：导出记录分页回读。
- `storage/`：数据库迁移、任务存储、候选模型。
- `tests/`：临时生成的合成备份、ADB mock、Qt 冒烟测试。
- `docs/`：当前交付状态、测试记录和后续计划。

## 下一步

1. M5：Windows 10/11 x64 与 macOS 实机、真实 Android/iPhone 备份、打包与安装包验收。macOS 双架构适配是本次新增的平台范围，原生构建与自动化证据单独保存。
   本机环境检查和本地诊断报告用于准备及定位问题，真实设备与桌面操作的结论仍须按 [M5 清单](docs/M5_ACCEPTANCE.md)记录。
2. 视频预览已随 v0.7.0 发布；后续扩展视频/文档扫描及结构校验。

源文件默认只读。索引读取、普通文件采集不等同于已删除数据恢复。
电脑恢复新增受限的 NTFS/FAT32/exFAT/APFS 元数据解析和 NTFS 运行列表、APFS 区段读取，不是通用文件系统修复或任意碎片重组。内置磁盘的 Time Machine 本地快照、HFS+ 删除记录、加密来源解密、损坏分区修复及 Rust 引擎未实现。签名扫描仅定位连续图片候选，不能判断候选是否曾被删除。
