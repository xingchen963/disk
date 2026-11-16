import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

from .disk_layout import list_windows_disks


@dataclass
class Device:
    name: str
    device_type: str = ""
    protocol: str = ""
    model: str = ""
    serial: str = ""
    capacity_bytes: int = 0
    cached_payload: dict[str, Any] | None = None
    windows_disk_number: int | None = None
    partition_style: str = "Unknown"
    bus_type: str = ""
    operational_status: str = ""
    is_offline: bool = False
    is_read_only: bool = False
    smart_supported: bool = False
    smart_error: str = ""


class SmartctlError(RuntimeError):
    pass


def _hidden_subprocess_options() -> dict[str, Any]:
    """Prevent smartctl console windows from flashing in the windowed app."""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }


def smartctl_path() -> str | None:
    return shutil.which("smartctl")


def _run_json(command: list[str], timeout: int = 60) -> tuple[dict[str, Any], subprocess.CompletedProcess]:
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            **_hidden_subprocess_options(),
        )
    except subprocess.TimeoutExpired as exc:
        raise SmartctlError(f"命令执行超时：{' '.join(command)}") from exc
    except OSError as exc:
        raise SmartctlError(f"无法启动命令：{exc}") from exc
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise SmartctlError(f"JSON 解析失败（退出码 {proc.returncode}）：{detail or exc}") from exc
    return payload, proc


def _normal_identity(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _capacity_matches(left: int, right: int) -> bool:
    return bool(left and right and abs(left - right) <= max(64 * 1024 * 1024, min(left, right) // 100))


def _smart_physicaldrive_number(name: str) -> int | None:
    import re
    match = re.search(r"physicaldrive(\d+)", name, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def discover_devices() -> list[Device]:
    """List all Windows physical disks; attach SMART data only when a safe match exists."""
    windows_disks = []
    windows_error = ""
    try:
        windows_disks = list_windows_disks()
    except Exception as exc:
        windows_error = str(exc)

    smart_devices: list[Device] = []
    exe = smartctl_path()
    smart_scan_error = ""
    if exe:
        try:
            scan_payload, scan_proc = _run_json([exe, "--scan-open", "-j"], timeout=35)
            if scan_proc.returncode not in (0, 1) and not scan_payload.get("devices"):
                smart_scan_error = scan_proc.stderr.strip() or f"smartctl 扫描退出码 {scan_proc.returncode}"
            for item in scan_payload.get("devices", []):
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                device = Device(
                    name=name,
                    device_type=str(item.get("type", "")).strip(),
                    protocol=str(item.get("protocol", "")).strip(),
                    smart_supported=True,
                )
                try:
                    info = read_smart(device)
                    device.model = str(info.get("model_name") or info.get("product") or "")
                    device.serial = str(info.get("serial_number") or "")
                    device.capacity_bytes = int(
                        info.get("user_capacity", {}).get("bytes")
                        or info.get("nvme_total_capacity")
                        or 0
                    )
                    device.cached_payload = info
                except Exception as exc:
                    device.smart_error = str(exc)
                smart_devices.append(device)
        except Exception as exc:
            smart_scan_error = str(exc)
    else:
        smart_scan_error = "找不到 smartctl.exe；仅显示 Windows 能枚举的物理磁盘，SMART 数据不可用。"

    if not windows_disks:
        if smart_devices:
            return smart_devices
        if windows_error:
            if smart_scan_error:
                raise SmartctlError(f"Windows 磁盘枚举失败：{windows_error}\nSMART 扫描失败：{smart_scan_error}")
            raise SmartctlError(f"Windows 磁盘枚举失败：{windows_error}")
        # Windows enumeration succeeded and returned an empty list: report no disks,
        # even when smartmontools is not installed (SMART is optional now).
        return []

    devices: list[Device] = []
    used_smart_indices: set[int] = set()
    for disk in windows_disks:
        number = int(disk.get("Number", -1))
        model_win = str(disk.get("FriendlyName") or "")
        serial_win = str(disk.get("SerialNumber") or "")
        capacity_win = int(disk.get("Size") or 0)

        candidates = []
        # Highest-confidence match: a smartctl path explicitly names this PhysicalDrive number.
        for index, smart in enumerate(smart_devices):
            if index in used_smart_indices:
                continue
            if _smart_physicaldrive_number(smart.name) == number:
                candidates.append((0, index))
        if not candidates and serial_win:
            serial_key = _normal_identity(serial_win)
            for index, smart in enumerate(smart_devices):
                if index in used_smart_indices:
                    continue
                if serial_key and _normal_identity(smart.serial) == serial_key and _capacity_matches(capacity_win, smart.capacity_bytes):
                    candidates.append((1, index))
        if not candidates and model_win:
            model_key = _normal_identity(model_win)
            for index, smart in enumerate(smart_devices):
                if index in used_smart_indices:
                    continue
                if (
                    model_key
                    and _normal_identity(smart.model) == model_key
                    and _capacity_matches(capacity_win, smart.capacity_bytes)
                ):
                    candidates.append((2, index))

        match = None
        if candidates:
            candidates.sort()
            best_rank = candidates[0][0]
            best = [index for rank, index in candidates if rank == best_rank]
            if len(best) == 1:
                match = smart_devices[best[0]]
                used_smart_indices.add(best[0])

        model = (match.model if match and match.model else model_win) or f"Physical Disk {number}"
        capacity = capacity_win or (match.capacity_bytes if match else 0)
        device_name = (
            match.name if match else
            str(disk.get("DeviceID") or f"\\\\.\\PhysicalDrive{number}")
        )
        device = Device(
            name=device_name,
            device_type=match.device_type if match else "",
            protocol=(match.protocol if match and match.protocol else str(disk.get("BusType") or "")),
            model=model,
            serial=(match.serial if match and match.serial else serial_win),
            capacity_bytes=capacity,
            cached_payload=match.cached_payload if match else None,
            windows_disk_number=number,
            partition_style=str(disk.get("PartitionStyle") or "Unknown"),
            bus_type=str(disk.get("BusType") or ""),
            operational_status=str(disk.get("OperationalStatus") or ""),
            is_offline=bool(disk.get("IsOffline")),
            is_read_only=bool(disk.get("IsReadOnly")),
            smart_supported=bool(match and match.smart_supported),
            smart_error=(
                match.smart_error if match else
                (smart_scan_error or "该物理磁盘未出现在 smartctl 可访问设备列表中；分区信息仍可读取。")
            ),
        )
        devices.append(device)

    # Windows is the authoritative physical-disk inventory on this platform.
    # smartctl may expose the same storage device under an alias such as /dev/sdc.
    # If the alias cannot be mapped safely, do not add it as a second physical disk.
    return devices



def read_smart(device: Device) -> dict[str, Any]:
    """Read SMART data from one device. Non-zero smartctl status may still include valid JSON."""
    exe = smartctl_path()
    if not exe:
        raise SmartctlError("找不到 smartctl.exe。")

    command = [exe, "-a", "-j"]
    if device.device_type:
        command += ["-d", device.device_type]
    command.append(device.name)

    payload, proc = _run_json(command, timeout=60)
    if not isinstance(payload, dict) or not payload:
        detail = proc.stderr.strip() or proc.stdout.strip() or "设备没有返回 SMART JSON。"
        raise SmartctlError(
            f"SMART 数据读取失败（退出码 {proc.returncode}）：{detail}"
        )

    payload["_diskrescue_diagnostics"] = {
        "command": command,
        "return_code": proc.returncode,
        "stderr": proc.stderr.strip(),
    }
    return payload


def _number(value: Any) -> str:
    if value is None:
        return "不可用"
    if isinstance(value, (int, float)):
        return f"{value:,}" if isinstance(value, int) else f"{value}"
    return str(value)


def _human_bytes(value: Any) -> str:
    if not isinstance(value, (int, float)) or value < 0:
        return "不可用"
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if amount < 1000 or unit == "PB":
            return f"{amount:,.2f} {unit}" if unit != "B" else f"{amount:,.0f} B"
        amount /= 1000
    return f"{amount:,.2f} PB"


def summarize(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """Convert smartctl JSON into human-readable device and health rows."""
    payload = payload or {}
    capacity = (
        payload.get("user_capacity", {}).get("bytes")
        or payload.get("nvme_total_capacity")
    )
    passed = payload.get("smart_status", {}).get("passed")
    health = (
        "通过（不代表没有故障）" if passed is True
        else "未通过/异常" if passed is False
        else "未知或不支持"
    )

    temperature = payload.get("temperature", {}).get("current")
    nvme_log = payload.get("nvme_smart_health_information_log", {}) or {}
    if temperature is None:
        temperature = nvme_log.get("temperature")

    power_hours = payload.get("power_on_time", {}).get("hours")
    if power_hours is None:
        power_hours = nvme_log.get("power_on_hours")

    rows = [
        ("型号", str(payload.get("model_name") or payload.get("product") or "不可用")),
        ("序列号", str(payload.get("serial_number") or "不可用")),
        ("固件版本", str(payload.get("firmware_version") or "不可用")),
        ("协议", str(payload.get("device", {}).get("protocol") or "不可用")),
        ("容量", _human_bytes(capacity)),
        ("SMART 总体状态", health),
        ("温度", f"{temperature} °C" if temperature is not None else "不可用"),
        (
            "通电时间",
            f"{power_hours:,} 小时"
            if isinstance(power_hours, (int, float)) else "不可用",
        ),
    ]

    if nvme_log:
        fields = [
            ("寿命已使用", "percentage_used", "%"),
            ("可用备用空间", "available_spare", "%"),
            ("备用空间阈值", "available_spare_threshold", "%"),
            ("数据读取量", "data_units_read", " units"),
            ("数据写入量", "data_units_written", " units"),
            ("通电次数", "power_cycles", " 次"),
            ("异常断电次数", "unsafe_shutdowns", " 次"),
            ("介质与数据完整性错误", "media_errors", " 次"),
            ("错误日志条目数", "num_err_log_entries", " 条"),
        ]
        for label, key, suffix in fields:
            if key not in nvme_log:
                continue
            value = nvme_log[key]
            if key in {"data_units_read", "data_units_written"}:
                if isinstance(value, (int, float)):
                    value = f"{_number(value)} units（约 {_human_bytes(value * 512000)}）"
                else:
                    value = _number(value)
                rows.append((label, value))
            else:
                rows.append((label, f"{_number(value)}{suffix}"))

    ata_attributes = payload.get("ata_smart_attributes", {}).get("table", []) or []
    interesting = {
        5: "重新分配扇区数",
        187: "报告的不可校正错误",
        188: "命令超时",
        197: "待处理扇区数",
        198: "离线不可校正扇区数",
        199: "接口 CRC 错误",
    }
    for attr in ata_attributes:
        attribute_id = attr.get("id")
        if attribute_id in interesting:
            raw_value = attr.get("raw", {}).get("value", "不可用")
            rows.append((interesting[attribute_id], str(raw_value)))

    return rows
