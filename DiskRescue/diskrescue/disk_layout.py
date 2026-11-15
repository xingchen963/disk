import json
import os
import subprocess
from typing import Any


def _hidden_subprocess_options() -> dict[str, Any]:
    """Prevent console windows from flashing when the GUI launches PowerShell."""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }


def _run_powershell_json(command: str, timeout: int = 20) -> list[dict[str, Any]]:
    """Run one small read-only Storage cmdlet query and normalize JSON to a list."""
    proc = subprocess.run(
        [
            "powershell.exe", "-NoLogo", "-NoProfile",
            "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-Command", command,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        **_hidden_subprocess_options(),
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"退出码 {proc.returncode}"
        raise RuntimeError(detail)
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"PowerShell JSON 解析失败：{exc}") from exc
    if data is None:
        return []
    return data if isinstance(data, list) else [data]


def _normal(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _same_model_capacity(disk: dict[str, Any], model: str, capacity_bytes: int) -> bool:
    disk_model = _normal(disk.get("FriendlyName"))
    wanted_model = _normal(model)
    disk_size = int(disk.get("Size") or 0)
    # Require exact normalized model name and a tight 1%/64MiB capacity tolerance.
    return bool(
        wanted_model
        and disk_model == wanted_model
        and capacity_bytes > 0
        and abs(disk_size - int(capacity_bytes)) <= max(64 * 1024 * 1024, int(capacity_bytes) // 100)
    )


def get_disk_layout(serial: str, model: str = "", capacity_bytes: int = 0) -> dict[str, Any]:
    """Read Windows physical disks, partitions and volumes using separate read-only queries.

    Serial strings from smartctl and Windows Storage frequently use different encodings,
    so serial equality is deliberately not used as the primary cross-provider key.
    """
    try:
        disks = _run_powershell_json(
            "Get-Disk | Select-Object Number,FriendlyName,SerialNumber,Size,PartitionStyle,IsBoot,IsSystem | ConvertTo-Json -Depth 3 -Compress"
        )
    except Exception as exc:
        return {"error": f"读取 Windows 物理磁盘失败：{exc}", "partitions": []}

    matches = [d for d in disks if _same_model_capacity(d, model, capacity_bytes)]
    if len(matches) != 1:
        if not matches:
            reason = "没有找到型号和容量均匹配的唯一 Windows 物理磁盘"
        else:
            reason = "型号和容量匹配到多块 Windows 物理磁盘，无法安全确定目标"
        return {"error": reason + "；为防止显示错盘，未展示分区。", "partitions": []}

    selected = matches[0]
    disk_number = int(selected.get("Number"))

    try:
        all_partitions = _run_powershell_json(
            "Get-Partition | Select-Object DiskNumber,PartitionNumber,DriveLetter,AccessPaths,Type,Size,GptType,MbrType,IsHidden,IsSystem,IsBoot,NoDefaultDriveLetter,IsReadOnly,IsOffline | ConvertTo-Json -Depth 3 -Compress"
        )
    except Exception as exc:
        return {
            "error": f"已识别 Windows 磁盘 {disk_number}，但读取分区列表失败：{exc}",
            "disk_number": disk_number,
            "disk_size": int(selected.get("Size") or 0),
            "partitions": [],
        }

    try:
        all_volumes = _run_powershell_json(
            "Get-Volume | Select-Object DriveLetter,FileSystemLabel,FileSystem,Size,SizeRemaining,HealthStatus | ConvertTo-Json -Depth 3 -Compress"
        )
    except Exception as exc:
        # Keep partition geometry even if volume enumeration fails.
        all_volumes = []
        volume_error = str(exc)
    else:
        volume_error = ""

    volumes_by_letter = {}
    for volume in all_volumes:
        letter = str(volume.get("DriveLetter") or "").strip().upper()
        if letter:
            volumes_by_letter[letter] = volume

    partitions = []
    for part in all_partitions:
        if int(part.get("DiskNumber", -1)) != disk_number:
            continue
        drive_letter = str(part.get("DriveLetter") or "").strip().upper()
        volume = volumes_by_letter.get(drive_letter, {}) if drive_letter else {}
        volume_size = volume.get("Size")
        size_remaining = volume.get("SizeRemaining")
        size_used = None
        if isinstance(volume_size, (int, float)) and isinstance(size_remaining, (int, float)):
            size_used = max(int(volume_size) - int(size_remaining), 0)

        access_paths = part.get("AccessPaths") or []
        if isinstance(access_paths, str):
            access_paths = [access_paths]

        partitions.append({
            "PartitionNumber": int(part.get("PartitionNumber") or 0),
            "DriveLetter": drive_letter,
            "AccessPaths": access_paths,
            "Type": str(part.get("Type") or ""),
            "GptType": str(part.get("GptType") or ""),
            "MbrType": str(part.get("MbrType") or ""),
            "IsHidden": bool(part.get("IsHidden")),
            "IsSystem": bool(part.get("IsSystem")),
            "IsBoot": bool(part.get("IsBoot")),
            "NoDefaultDriveLetter": bool(part.get("NoDefaultDriveLetter")),
            "IsReadOnly": bool(part.get("IsReadOnly")),
            "IsOffline": bool(part.get("IsOffline")),
            "Size": int(part.get("Size") or 0),
            "FileSystem": str(volume.get("FileSystem") or ""),
            "FileSystemLabel": str(volume.get("FileSystemLabel") or ""),
            "SizeRemaining": int(size_remaining) if isinstance(size_remaining, (int, float)) else None,
            "SizeUsed": size_used,
            "VolumeSize": int(volume_size) if isinstance(volume_size, (int, float)) else None,
            "HealthStatus": str(volume.get("HealthStatus") or ""),
        })

    partitions.sort(key=lambda p: p["PartitionNumber"])
    return {
        "disk_number": disk_number,
        "friendly_name": selected.get("FriendlyName"),
        "serial_number": selected.get("SerialNumber"),
        "match_method": "型号和容量（唯一匹配）",
        "partition_style": selected.get("PartitionStyle"),
        "is_boot": selected.get("IsBoot"),
        "is_system": selected.get("IsSystem"),
        "disk_size": int(selected.get("Size") or 0),
        "partitions": partitions,
        "volume_warning": volume_error,
    }
