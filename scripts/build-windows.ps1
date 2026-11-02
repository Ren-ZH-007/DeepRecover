$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
if ($env:OS -ne 'Windows_NT') { throw '请在 Windows x64 上构建' }
$python = '.\.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw '请先建立 Python 3.12 虚拟环境' }
& $python -m pip install -r requirements.txt pyinstaller==6.12.0
if ($LASTEXITCODE -ne 0) { throw '构建依赖安装失败' }
& $python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw '测试失败，停止构建' }
& $python -m PyInstaller --noconfirm --clean --windowed --onedir --name DeepRecover main.py
if ($LASTEXITCODE -ne 0) { throw '打包失败' }
Write-Host '构建完成：dist\DeepRecover\DeepRecover.exe；请分发整个目录，并执行实机验收。'
