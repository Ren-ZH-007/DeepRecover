param([string]$Version = '')
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
if ($env:OS -ne 'Windows_NT') { throw '请在 Windows x64 上构建' }
$python = '.\.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw '请先建立 Python 3.12 虚拟环境' }
& $python -c "import struct,sys; assert struct.calcsize('P')==8 and sys.version_info[:2]==(3,12), 'Python 3.12 x64 required'"
if ($LASTEXITCODE -ne 0) { throw '需要 Python 3.12 x64' }
if (-not $Version) { $Version = (& $python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])").Trim() }
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw '版本必须为三段数字' }
& $python -m pip install --disable-pip-version-check -r requirements.txt pyinstaller==6.12.0
if ($LASTEXITCODE -ne 0) { throw '构建依赖安装失败' }
$env:QT_QPA_PLATFORM = 'offscreen'
& $python scripts/run_release_tests.py --report build/test-report.json
if ($LASTEXITCODE -ne 0) { throw '测试失败，停止构建' }
& $python -m PyInstaller --noconfirm --clean --windowed --onedir --collect-data app --specpath build/spec --workpath build/pyinstaller --name DeepRecover main.py
if ($LASTEXITCODE -ne 0) { throw '打包失败' }
$report = 'build/executable-smoke.json'
if (Test-Path $report) { Remove-Item $report }
& $python scripts/verify_windows_build.py --executable dist/DeepRecover/DeepRecover.exe --report $report --version $Version
if ($LASTEXITCODE -ne 0) { throw 'Windows EXE 冒烟测试失败，停止发布' }
$archive = "dist/DeepRecover-v$Version-windows-x64.zip"
if (Test-Path $archive) { Remove-Item $archive }
Compress-Archive -Path dist/DeepRecover -DestinationPath $archive -CompressionLevel Optimal
$hash = (Get-FileHash -Algorithm SHA256 $archive).Hash.ToLowerInvariant()
$line = "$hash  $([IO.Path]::GetFileName($archive))`n"
[IO.File]::WriteAllText((Join-Path (Get-Location) 'dist/SHA256SUMS.txt'), $line, [Text.UTF8Encoding]::new($false))
Write-Host "已构建并启动验证：$archive；请分发完整 ZIP。真实手机和 Windows 实机体验仍需另行验收。"
