# DiskRescue Windows build script
# Run from the DiskRescue project directory in PowerShell.
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

# Do not let PyInstaller delete/replace Qt DLLs that are still loaded by the app.
$running = Get-Process -Name "DiskRescue" -ErrorAction SilentlyContinue
if ($running) {
    $ids = ($running | Select-Object -ExpandProperty Id) -join ", "
    throw "检测到 DiskRescue.exe 仍在运行（PID: $ids）。请先关闭所有 DiskRescue 窗口，再重新执行本脚本。"
}

$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$pyinstaller = Join-Path $projectRoot ".venv\Scripts\pyinstaller.exe"

if (-not (Test-Path $python)) {
    py -3 -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        throw "创建 Python 虚拟环境失败，退出码：$LASTEXITCODE"
    }
}

& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "升级 pip 失败，退出码：$LASTEXITCODE"
}

& $python -m pip install -r requirements.txt pyinstaller
if ($LASTEXITCODE -ne 0) {
    throw "安装项目依赖失败，退出码：$LASTEXITCODE"
}

& $pyinstaller --noconfirm --clean --windowed --name DiskRescue main.py
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 编译失败，退出码：$LASTEXITCODE。请查看上方错误日志。"
}

$exe = Join-Path $projectRoot "dist\DiskRescue\DiskRescue.exe"
if (-not (Test-Path $exe)) {
    throw "PyInstaller 返回成功，但没有找到预期文件：$exe"
}

Write-Host ""
Write-Host "Build succeeded." -ForegroundColor Green
Write-Host "Executable: $exe"
Write-Host "请保留同目录下的 _internal 文件夹，不要只复制 EXE。"
