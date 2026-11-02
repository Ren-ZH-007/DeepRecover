# DeepRecover

个人自用、本地运行的手机数据恢复工作台。首期目标：Windows 10/11 x64，Python 3.12 + PySide6 + SQLite。

**当前版本：0.1.0，第一阶段 M2 授权采集与索引。尚不是完整恢复软件。**
依据《DeepRecover_当前对话归档与研发方案_20261102.md》创建。

## 已实现

- 分层工程、虚拟环境依赖清单、入口和深色主窗口，7 个导航页面。
- 工作目录初始化、轮转日志、SQLite v2 迁移、数据源和候选模型（兼容 v1 历史记录）。
- 后台任务执行、协作式取消、成功/失败/中断状态及任务历史。
- ADB 可用性检测、已授权/未授权/offline/no permissions 状态解析。
- 选择已授权 Android 设备、共享目录和普通文件，确认后只读 `adb pull` 到本地；记录设备、原始路径、本地副本、采集时间和本地 SHA-256。
- iPhone 常用备份目录发现、自选目录、备份元数据和加密状态识别。
- 未加密 Manifest.db 只读解析、候选入库，缺失文件显式标记。
- 候选按任务每页 100 条展示；历史任务结果可重开，失败/取消任务保留已提交的部分记录。
- 35 项自动化测试，包含离屏 Qt 采集流程、分页和历史回读。

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
可设置 `DEEPRECOVER_DATA_DIR` 指向独立工作目录；不要指向原始备份目录或待恢复介质。
目录包括 Devices、Backups、Acquisitions、ScanCache、Recovered、Reports、Logs 和 index.db。
设备序列号、备份来源目录、候选原始路径、本地路径、采集时间和副本 SHA-256 保存在本地数据库，可在结果页查看。日志不记录文件内容、完整设备序列号或异常明文。来源包含敏感信息，请保护工作目录。

备份入口自动检查 `%USERPROFILE%\Apple\MobileSync\Backup` 和 `%APPDATA%\Apple Computer\MobileSync\Backup`。
用户可选择 Backup 根目录或单份备份目录。加密备份只识别，不解密。
界面使用只读流式遍历全部普通文件记录（丢弃无效 fileID），每 100 项提交一次，不再受旧界面 10,000 条上限限制。结果页从本地数据库分页查询，不一次载入完整历史结果。底层兼容函数 `read_manifest(..., limit=10000)` 保留旧调用上限，新界面使用 `iter_manifest`。
文件名和路径只作为显示元数据，不用于导出写入。
候选 `integrity=not_checked` 表示结构未验证；`missing` 表示备份文件缺失。
Android SHA-256 仅是本地副本的摘要，不是源端哈希比对或文件结构验证；状态仍是 candidate，不标记 validated、exported 或成功恢复。

任务记录显示最近 200 个任务，选择后点击“回读选中任务的结果”或双击任务。结果页显示来源、任务状态、原始路径、本地文件位置和分页；重启后仍可回读，无需重新连接设备或读取源备份。旧 v1 记录没有独立数据源时会明确提示，不推测来源。历史路径可能已失效，不会自动重新采集。取消、失败、中断不等于完整完成，尚未提交的索引批次不显示。异常断电或本地数据库写入失败可能留下无索引副本，不自动删除已有采集文件；不支持断点续采。

## 测试与构建

从工程根目录运行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

无 Qt 环境可用标准 Python 运行核心测试；UI 测试会明确跳过。
可选界面检查：`python scripts/check_ui.py`，输出 `docs/interface-preview.png`。
Windows 构建脚本：`scripts/build-windows.ps1`，先运行测试，再用 PyInstaller 生成目录式 EXE。
这是构建入口，**本次未生成或验证 Windows EXE，也未生成安装包**。
运行脚本需要安装相应依赖并遵循组织的 PowerShell 策略。

## 工程结构

- `app/`：启动、配置、日志、主窗口和路由。
- `ui/`：页面拆分预留包，当前页面组装位于 app/main_window.py。
- `core/device/`：Android 与 iPhone 适配。
- `core/acquisition/android.py`：用户选择的普通文件采集与本地副本摘要。
- `core/parser/ios_backup/`：未加密备份索引读取。
- `core/tasks.py`：后台任务与取消。
- `core/scanner/base.py`：扫描引擎协议（暂无实现）。
- `storage/`：数据库迁移、任务存储、候选模型。
- `tests/`：临时生成的合成备份、ADB mock、Qt 冒烟测试。
- `docs/`：当前交付状态、测试记录和后续计划。

## 下一步

1. 连续文件签名扫描及结构验证：优先 JPG/PNG，再扩展视频和文档。
2. 图片/视频预览、批量导出、防覆盖和源/目标 SHA-256 校验。
3. Windows 实机、真实 Android/iPhone 备份测试与打包验收。

源文件默认只读。索引读取、普通文件采集不等同于已删除数据恢复。
文件系统分析、碎片重组、加密备份解析和 Rust 引擎均未实现。
