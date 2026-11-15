import json
import subprocess
from typing import Any


POWERSHELL = r"""
$ErrorActionPreference = 'Stop'
$allDisks = @(Get-Disk | ForEach-Object {
    $d = $_
    $parts = @(Get-Partition -DiskNumber $d.Number -ErrorAction SilentlyContinue | ForEach-Object {
        $p = $_
        $v = $null
        if ($p.DriveLetter) {
            $v = Get-Volume -DriveLetter $p.DriveLetter -ErrorAction SilentlyContinue
        }
        [pscustomobject]@{
            PartitionNumber = [int]$p.PartitionNumber
            DriveLetter = if ($p.DriveLetter) { [string]$p.DriveLetter } else { '' }
            Type = [string]$p.Type
            Size = [int64]$p.Size
            FileSystem = if ($v) { [string]$v.FileSystem } else { '' }
            FileSystemLabel = if ($v) { [string]$v.FileSystemLabel } else { '' }
            SizeRemaining = if ($v) { [int64]$v.SizeRemaining } else { $null }
            SizeUsed = if ($v) { [int64]($v.Size - $v.SizeRemaining) } else { $null }
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
    """Read Windows disk/partition/volume metadata. This function is read-only."""
    try:
        proc = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-Command", POWERSHELL,
            ],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except Exception as exc:
        return {"error": f"无法读取 Windows 分区信息：{exc}", "partitions": []}

    if proc.returncode != 0:
        return {
            "error": proc.stderr.strip() or f"PowerShell 退出码 {proc.returncode}",
            "partitions": [],
        }
    try:
        disks = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        return {"error": f"Windows 磁盘信息 JSON 解析失败：{exc}", "partitions": []}
    if isinstance(disks, dict):
        disks = [disks]

    wanted_serial = _normal(serial)
    wanted_model = _normal(model)
    selected = None

    if wanted_serial:
        for disk in disks:
            if _normal(disk.get("SerialNumber")) == wanted_serial:
                selected = disk
                break

    if selected is None and wanted_model and capacity_bytes:
        candidates = [
            d for d in disks
            if _normal(d.get("FriendlyName")) == wanted_model
            and abs(int(d.get("Size") or 0) - capacity_bytes) <= max(16 * 1024 * 1024, capacity_bytes // 100)
        ]
        if len(candidates) == 1:
            selected = candidates[0]

    if selected is None:
        return {
            "error": "未能将 smartctl 设备与 Windows 物理磁盘可靠对应；为避免显示错盘，暂不展示分区。",
            "partitions": [],
        }

    parts = selected.get("Partitions") or []
    if isinstance(parts, dict):
        parts = [parts]
    return {
        "disk_number": selected.get("Number"),
        "friendly_name": selected.get("FriendlyName"),
        "partition_style": selected.get("PartitionStyle"),
        "is_boot": selected.get("IsBoot"),
        "is_system": selected.get("IsSystem"),
        "disk_size": selected.get("Size"),
        "partitions": parts,
    }
