# Run from the DiskRescue directory in Windows PowerShell.
$ErrorActionPreference = "Stop"
py -3 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller
& .\.venv\Scripts\pyinstaller.exe --noconfirm --clean --windowed --name DiskRescue main.py
Write-Host "Build finished. Output: dist\DiskRescue\"
