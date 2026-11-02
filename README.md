# DeepRecover

个人自用、本地运行的手机数据恢复工作台。首期目标：Windows 10/11 x64，Python 3.12 + PySide6 + SQLite。

**当前版本：0.1.0，第一阶段 M1 工程初始化。尚不是完整恢复软件。**
依据《DeepRecover_当前对话归档与研发方案_20261102.md》创建。

## 已实现

- 分层工程、虚拟环境依赖清单、入口和深色主窗口，7 个导航页面。
- 工作目录初始化、轮转日志、SQLite v1 迁移和候选模型。
- 后台任务执行、协作式取消、成功/失败/中断状态及任务历史。
- ADB 可用性检测、已授权/未授权/offline/no permissions 状态解析。
- iPhone 常用备份目录发现、自选目录、备份元数据和加密状态识别。
- 未加密 Manifest.db 只读解析、候选入库，缺失文件显式标记。
- 15 项自动化测试，包含离屏 Qt 窗口和导航验证。

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

通过 USB 连接设备并在手机上主动允许 USB 调试。本版只调用 `adb devices -l`；不采集文件。
ADB 检测可能启动本机 ADB 服务。第三方工具未随源码包分发。

## 工作数据

默认 `%LOCALAPPDATA%\DeepRecover_Data`，未设置 LOCALAPPDATA 时使用用户主目录。
可设置 `DEEPRECOVER_DATA_DIR` 指向独立工作目录；不要指向原始备份目录或待恢复介质。
目录包括 Devices、Backups、ScanCache、Recovered、Reports、Logs 和 index.db。
备份索引中的原始路径保存在本地数据库，日志不记录文件内容、完整设备序列号或异常明文。

备份入口自动检查 `%USERPROFILE%\Apple\MobileSync\Backup` 和 `%APPDATA%\Apple Computer\MobileSync\Backup`。
用户可选择 Backup 根目录或单份备份目录。加密备份只识别，不解密。
索引界面最多读取 10,000 条普通文件记录（包括后续丢弃的无效 fileID）；不是完整大备份统计。
文件名和路径只作为显示元数据，不用于导出写入。
候选 `integrity=not_checked` 表示结构未验证；`missing` 表示备份文件缺失。

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
- `core/parser/ios_backup/`：未加密备份索引读取。
- `core/tasks.py`：后台任务与取消。
- `core/scanner/base.py`：扫描引擎协议（暂无实现）。
- `storage/`：数据库迁移、任务存储、候选模型。
- `tests/`：临时生成的合成备份、ADB mock、Qt 冒烟测试。
- `docs/`：当前交付状态、测试记录和后续计划。

## 下一步

1. 授权文件采集（ADB 列表、用户选择、只读 pull、来源记录）。
2. 连续文件签名扫描及结构验证：优先 JPG/PNG，再扩展视频和文档。
3. 结果分页、来源定位、图片/视频预览、批量导出和 SHA-256 校验。
4. Windows 实机、真实 Android/iPhone 备份测试与打包验收。

源文件默认只读。索引读取、普通文件采集不等同于已删除数据恢复。
文件系统分析、碎片重组、加密备份解析和 Rust 引擎均未实现。
