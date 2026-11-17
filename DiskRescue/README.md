# DiskRescue

DiskRescue is an early-stage Windows disk diagnostics and data-rescue project.

## Current milestone: physical-disk inventory + read-only partition identification

The application now:
- Enumerates physical disks through Windows Storage (Get-Disk), with Win32_DiskDrive as a fallback. Disk listing does not depend on SMART support.
- Starts an automatic scan when the application opens. The Scan Disks button remains available for a manual re-scan.
- Loads the selected disk when its row is clicked.
- Reads SMART data through smartmontools when the device/USB bridge supports passthrough. A SMART failure does not hide the physical disk or prevent partition inspection.
- Reads Windows partition and volume metadata, including partitions without drive letters.
- Reads a small number of metadata sectors from the physical disk in read-only mode. The packaged EXE requests Administrator elevation for this operation.
- Parses GPT, MBR (including extended/logical partition chains), and Apple Partition Map (APM) partition layouts.
- Identifies BSD disklabel and Solaris VTOC signatures, but does not yet parse their partition geometry.
- Detects common file-system/container signatures: NTFS, FAT12/16/32, exFAT, ReFS signature candidate, BitLocker, ext2/3/4, XFS, Btrfs, APFS, HFS+, HFSX, Apple CoreStorage, LUKS, Linux Swap, LVM PV, UDF, and ISO 9660.
- When Windows already lists physical disks, unmatched smartctl aliases (for example `/dev/sdc`) are not displayed as additional physical disks.
- HFS+ / HFSX volumes are inspected read-only: volume header, allocation bitmap, Catalog B-tree, folder/file names, CNIDs, paths, and data-fork logical sizes are reported when the required metadata extents are readable.
- HFS+ / HFSX catalog inspection does not read file contents, restore files, replay a journal, decrypt volumes, or repair metadata. Extended extents are resolved through the Extents Overflow B-tree where available.
- APFS detection currently remains signature-level only; APFS container/volume parsing is a later milestone.

## Detection limits

- The packaged EXE requests Administrator permission because raw physical-sector metadata reads require elevation on many Windows systems. Without elevation, Windows-reported GPT/MBR layout and mounted-volume file-system information are still shown, but an unmounted hidden partition may only be identified by its partition-type GUID or MBR type.
- A file-system signature match is identification only. It does not mean that DiskRescue can mount, browse, repair, or recover that file system.
- Linux LVM, RAID members, LUKS, BitLocker, and swap areas are identified as container/partition types where possible; their contents are not mounted or decrypted.
- Tiny partitions receive a 10-pixel minimum visual width in the disk map; those pixels are borrowed from larger partition segments. This is only a visual aid: exact capacities are shown in the partition table.
- Windows Storage natively reports GPT, MBR, or RAW. APM is parsed from raw sectors. BSD disklabel and Solaris VTOC are signature-only at this milestone.
- Unknown or damaged partition metadata is reported as unknown; the program must not guess a partition type or file system.

## Requirements
- Windows 10/11 (x64)
- Python 3.10+
- Install dependencies: `pip install -r requirements.txt`
- smartmontools is optional for disk inventory, but required for SMART data. Install it and make `smartctl.exe` available on PATH:
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
pyinstaller --noconfirm --clean --windowed --uac-admin --name DiskRescue main.py
```

## Safety rules
1. Disk inventory, partition-table parsing, and file-system signature detection are read-only. They do not format, repartition, repair, or write to disks.
2. Do not run destructive tests on a disk containing data you need.
3. If a disk clicks, disconnects, freezes the system, or becomes extremely slow, stop repeated scans.
4. For an unstable disk, image/clone it first with a recovery-oriented tool or consult a professional lab.
5. Always recover files to a different physical disk.
6. A SMART "PASSED" result does not guarantee a healthy disk.

## Development status
This is a source-code prototype, not a tested production recovery tool. Hardware-specific USB bridge behavior, raw-sector access permissions, damaged partition structures, and Windows PE compatibility require validation on real hardware.
