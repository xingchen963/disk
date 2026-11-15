# DiskRescue

DiskRescue is an early-stage Windows disk diagnostics and data-rescue project.

## Current milestone: safe device inventory + SMART report

This starter milestone is intentionally **read-only**:
- Discovers devices through `smartctl --scan-open -j`
- Reads device/SMART information through `smartctl -a -j`
- Does not write to disks, repair filesystems, remap sectors, or recover files yet

## Requirements
- Windows 10/11 (x64)
- Python 3.10+
- Install dependencies: `pip install -r requirements.txt`
- Install smartmontools and make `smartctl.exe` available on PATH:
  https://www.smartmontools.org/

## Run from source
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

## Build an EXE
```powershell
pip install pyinstaller
pyinstaller --noconfirm --clean --windowed --name DiskRescue main.py
```

## Safety rules
1. Do not run destructive tests on a disk containing data you need.
2. If a disk clicks, disconnects, freezes the system, or becomes extremely slow, stop repeated scans.
3. For an unstable disk, image/clone it first with a recovery-oriented tool or consult a professional lab.
4. Always recover files to a different physical disk.
5. A SMART "PASSED" result does not guarantee a healthy disk.

## Important limitation
Software cannot physically repair damaged platters, heads, NAND cells, or controller electronics. Sector remapping is generally handled by drive firmware and is not a guaranteed operation exposed to third-party software.

## Development status
This is a source-code prototype, not a tested production executable. Windows device access, USB bridge SMART passthrough, and PE compatibility require validation on real hardware.
