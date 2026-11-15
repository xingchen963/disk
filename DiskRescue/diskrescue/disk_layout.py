import json
import subprocess
from typing import Any


POWERSHELL = r"""
$ErrorActionPreference = 'Stop'
$allDisks = @(Get-Disk | ForEach-Object {
    $d = $_
    $parts = @(Get-Partition -DiskNumber $d.Number -ErrorAction SilentlyContinue | ForEach-Object {
        $p = $_
        $v = Get-Volume -Partition $p -ErrorAction SilentlyContinue | Select-Object -First 1
        [pscustomobject]@{
            PartitionNumber = [int]$p.PartitionNumber
            DriveLetter = if ($p.DriveLetter) { [string]$p.DriveLetter } elseif ($p.AccessPaths) { [string]$p.AccessPaths[0] } else { '' }
            Type = [string]$p.Type
            Size = [int64]$p.Size
            FileSystem = if ($v) { [string]$v.FileSystem } else { '' }
            FileSystemLabel = if ($v) { [string]$v.FileSystemLabel } else { '' }
            SizeRemaining = if ($v -and $null -ne $v.SizeRemaining) { [int64]$v.SizeRemaining } else { $null }
            SizeUsed = if ($v -and $null -ne $v.SizeRemaining) { [int64]($v.Size - $v.SizeRemaining) } else { $null }
            VolumeSize = if ($v) { [int64]$v.Size } else { $null }
            HealthStatus = if ($v) { [string]$v.HealthStatus } else { '' }
        }
    })
    [pscustomobject]@{
        Number = [int]$d.Number
        FriendlyName = [string]$d.FriendlyName
        SerialNumber = ([string]$d.SerialNumber).Trim()
        Size = [int64]$d.Size
        PartitionStyle = [string]$d.PartitionStyle
        IsBoot = [bool]$d.IsBoot
        IsSystem = [bool]$d.IsSystem
        Partitions = $parts
    }
})
ConvertTo-Json -InputObject $allDisks -Depth 6 -Compress
"""


def _normal(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def get_disk_layout(serial: str, model: str = "", capacity_bytes: int = 0) -> dict[str, Any]:
    """Read Windows physical disk, partition and volume metadata; never writes to disks."""
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", POWERSHELL],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except Exception as exc:
        return {"error": f"无法读取 Windows 分区信息：{exc}", "partitions": []}

    if proc.returncode != 0:
        return {"error": proc.stderr.strip() or f"PowerShell 退出码 {proc.returncode}", "partitions": []}
    try:
        disks = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        return {"error": f"Windows 磁盘信息 JSON 解析失败：{exc}", "partitions": []}
    if isinstance(disks, dict):
        disks = [disks]

    wanted_serial = _normal(serial)
    wanted_model = _normal(model)
    selected = None
    match_method = ""

    # Serial match is strongest, but Windows and smartctl sometimes format serials differently.
    if wanted_serial:
        matches = [d for d in disks if _normal(d.get("SerialNumber")) == wanted_serial]
        if len(matches) == 1:
            selected, match_method = matches[0], "序列号"

    # Model + capacity is accepted only when it identifies exactly one physical disk.
    if selected is None and wanted_model and capacity_bytes:
        candidates = [
            d for d in disks
            if _normal(d.get("FriendlyName")) == wanted_model
            and abs(int(d.get("Size") or 0) - int(capacity_bytes)) <= max(64 * 1024 * 1024, int(capacity_bytes) // 100)
        ]
        if len(candidates) == 1:
            selected, match_method = candidates[0], "型号和容量"

    if selected is None:
        return {
            "error": "无法将 SMART 设备与 Windows 物理磁盘唯一对应；为防止显示错盘，未展示分区。",
            "partitions": [],
        }

    parts = selected.get("Partitions") or []
    if isinstance(parts, dict):
        parts = [parts]
    return {
        "disk_number": selected.get("Number"),
        "friendly_name": selected.get("FriendlyName"),
        "serial_number": selected.get("SerialNumber"),
        "match_method": match_method,
        "partition_style": selected.get("PartitionStyle"),
        "is_boot": selected.get("IsBoot"),
        "is_system": selected.get("IsSystem"),
        "disk_size": int(selected.get("Size") or 0),
        "partitions": parts,
    }
