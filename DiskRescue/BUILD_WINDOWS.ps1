# DiskRescue Windows build script (ASCII-only for Windows PowerShell 5.1)
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot

$running = Get-Process -Name 'DiskRescue' -ErrorAction SilentlyContinue
if ($running) {
    $processIds = ($running | ForEach-Object { $_.Id }) -join ', '
    throw "DiskRescue is still running (PID: $processIds). Close every DiskRescue window and run this script again."
}

$venvDir = [System.IO.Path]::Combine($projectRoot, '.venv')
$python = [System.IO.Path]::Combine($venvDir, 'Scripts', 'python.exe')
$pyinstaller = [System.IO.Path]::Combine($venvDir, 'Scripts', 'pyinstaller.exe')

if (-not (Test-Path -LiteralPath $python)) {
    & py -3 -m venv $venvDir
    if ($LASTEXITCODE -ne 0) {
        throw "Creating the virtual environment failed. Exit code: $LASTEXITCODE"
    }
}

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python executable was not created: $python"
}

& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "Upgrading pip failed. Exit code: $LASTEXITCODE"
}

& $python -m pip install -r requirements.txt pyinstaller
if ($LASTEXITCODE -ne 0) {
    throw "Installing requirements failed. Exit code: $LASTEXITCODE"
}

if (-not (Test-Path -LiteralPath $pyinstaller)) {
    throw "PyInstaller executable was not found: $pyinstaller"
}

& $pyinstaller --noconfirm --clean --windowed --name DiskRescue main.py
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed. Exit code: $LASTEXITCODE"
}

$exe = [System.IO.Path]::Combine($projectRoot, 'dist', 'DiskRescue', 'DiskRescue.exe')
if (-not (Test-Path -LiteralPath $exe)) {
    throw "Build returned success but the executable is missing: $exe"
}

Write-Host ''
Write-Host 'BUILD SUCCEEDED' -ForegroundColor Green
Write-Host "Executable: $exe"
Write-Host 'Keep the _internal directory next to DiskRescue.exe.'
