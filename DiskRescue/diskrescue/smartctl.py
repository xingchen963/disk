import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass
class Device:
    name: str
    device_type: str = ""
    protocol: str = ""
    model: str = ""
    serial: str = ""
    capacity_bytes: int = 0
    cached_payload: dict[str, Any] | None = None


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


def discover_devices() -> list[Device]:
    exe = smartctl_path()
    if not exe:
        raise SmartctlError("找不到 smartctl.exe。请安装 smartmontools，并将其加入 PATH。")
    payload, proc = _run_json([exe, "--scan-open", "-j"], timeout=30)
    if proc.returncode not in (0, 1) and not payload.get("devices"):
        raise SmartctlError(f"设备扫描失败（退出码 {proc.returncode}）：{proc.stderr.strip()}")
    devices = []
    for item in payload.get("devices", []):
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        device = Device(
            name=name,
            device_type=str(item.get("type", "")).strip(),
            protocol=str(item.get("protocol", "")).strip(),
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
        except Exception:
            # Keep the device visible even when identity/SMART passthrough fails.
            pass
        devices.append(device)
    return devices


def read_smart(device: Device) -> dict[str, Any]:
    exe = smartctl_path()
    if not exe:
        raise SmartctlError("找不到 smartctl.exe。")
    command = [exe, "-a", "-j"]
    if device.device_type:
        command += ["-d", device.device_type]
    command.append(device.name)
    payload, proc = _run_json(command, timeout=60)
    if not payload:
        raise SmartctlError(
            f"设备没有返回 SMART JSON（退出码 {proc.returncode}）。"
            f"命令：{' '.join(command)}\\n{proc.stderr.strip()}"
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
        return f"{value:,}" if isinstance(value, int) else str(value)
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
    capacity = payload.get("user_capacity", {}).get("bytes") or payload.get("nvme_total_capacity")
    passed = payload.get("smart_status", {}).get("passed")
    health = "通过（不代表没有故障）" if passed is True else "未通过/异常" if passed is False else "未知或不支持"
    temperature = payload.get("temperature", {}).get("current")
    nvme_log = payload.get("nvme_smart_health_information_log", {})
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
        ("通电时间", f"{power_hours:,} 小时" if isinstance(power_hours, (int, float)) else "不可用"),
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
            if key in nvme_log:
                value = nvme_log[key]
                if key in {"data_units_read", "data_units_written"}:
                    value = f"{_number(value)} units（约 {_human_bytes(value * 512000)}）"
                    rows.append((label, value))
                else:
                    rows.append((label, f"{_number(value)}{suffix}"))
    ata_attributes = payload.get("ata_smart_attributes", {}).get("table", [])
    interesting = {
        5: "重新分配扇区数",
        187: "报告的不可校正错误",
        188: "命令超时",
        197: "待处理扇区数",
        198: "离线不可校正扇区数",
        199: "接口 CRC 错误",
    }
    for attr in ata_attributes:
        if attr.get("id") in interesting:
            rows.append((interesting[attr["id"]], str(attr.get("raw", {}).get("value", "不可用"))))
    return rows
