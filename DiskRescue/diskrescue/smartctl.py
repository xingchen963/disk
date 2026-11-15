import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass
class Device:
    name: str
    device_type: str = ""
    protocol: str = ""


class SmartctlError(RuntimeError):
    pass


def smartctl_path() -> str | None:
    return shutil.which("smartctl")


def discover_devices() -> list[Device]:
    exe = smartctl_path()
    if not exe:
        raise SmartctlError(
            "找不到 smartctl.exe。请安装 smartmontools，并将其加入 PATH。"
        )

    proc = subprocess.run(
        [exe, "--scan-open", "-j"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise SmartctlError(
            f"设备扫描输出无法解析（退出码 {proc.returncode}）：{detail or exc}"
        ) from exc

    if proc.returncode not in (0, 1) and not payload.get("devices"):
        raise SmartctlError(
            f"设备扫描失败（退出码 {proc.returncode}）："
            f"{proc.stderr.strip() or 'smartctl 未返回设备列表'}"
        )

    devices = []
    for item in payload.get("devices", []):
        name = str(item.get("name", "")).strip()
        if name:
            devices.append(
                Device(
                    name=name,
                    device_type=str(item.get("type", "")).strip(),
                    protocol=str(item.get("protocol", "")).strip(),
                )
            )
    return devices


def read_smart(device: Device) -> dict[str, Any]:
    exe = smartctl_path()
    if not exe:
        raise SmartctlError("找不到 smartctl.exe。")

    # Use the device type discovered by smartctl. Keep the exact command
    # and process diagnostics so a failed read is visible in the UI.
    command = [exe, "-a", "-j"]
    if device.device_type:
        command += ["-d", device.device_type]
    command.append(device.name)

    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SmartctlError(
            f"读取 SMART 超时（60 秒）。设备：{device.name}"
        ) from exc
    except OSError as exc:
        raise SmartctlError(f"无法启动 smartctl：{exc}") from exc

    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise SmartctlError(
            f"SMART 输出无法解析（退出码 {proc.returncode}）。"
            f"命令：{' '.join(command)}\n{detail or exc}"
        ) from exc

    if not payload:
        raise SmartctlError(
            f"设备没有返回 SMART JSON（退出码 {proc.returncode}）。"
            f"命令：{' '.join(command)}\n{proc.stderr.strip()}"
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


def summarize(payload: dict[str, Any]) -> list[tuple[str, str]]:
    capacity = payload.get("user_capacity", {}).get("bytes")
    if not isinstance(capacity, int):
        capacity = payload.get("nvme_total_capacity")
    capacity_text = (
        f"{capacity:,} bytes" if isinstance(capacity, int) else "不可用"
    )

    passed = payload.get("smart_status", {}).get("passed")
    health = (
        "通过（不代表没有故障）"
        if passed is True
        else "未通过/异常"
        if passed is False
        else "未知或不支持"
    )

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
        ("容量", capacity_text),
        ("SMART 总体状态", health),
        ("温度", f"{temperature} °C" if temperature is not None else "不可用"),
        ("通电时间", f"{power_hours} 小时" if power_hours is not None else "不可用"),
    ]

    if nvme_log:
        nvme_fields = [
            ("NVMe Critical Warning", "critical_warning"),
            ("可用备用空间", "available_spare"),
            ("备用空间阈值", "available_spare_threshold"),
            ("寿命已使用", "percentage_used"),
            ("数据读取量（单位 512,000 bytes）", "data_units_read"),
            ("数据写入量（单位 512,000 bytes）", "data_units_written"),
            ("通电次数", "power_cycles"),
            ("异常断电次数", "unsafe_shutdowns"),
            ("介质与数据完整性错误", "media_errors"),
            ("错误日志条目数", "num_err_log_entries"),
        ]
        for label, key in nvme_fields:
            if key in nvme_log:
                value = nvme_log[key]
                suffix = "%" if key in {"available_spare", "available_spare_threshold", "percentage_used"} else ""
                rows.append((label, f"{_number(value)}{suffix}"))

        sensors = nvme_log.get("temperature_sensors")
        if isinstance(sensors, list):
            rows.append(("温度传感器", " / ".join(f"{v} °C" for v in sensors)))

    # ATA attributes apply to SATA/ATA devices; NVMe uses the health log above.
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
        attr_id = attr.get("id")
        if attr_id in interesting:
            raw = attr.get("raw", {}).get("value", "不可用")
            rows.append((interesting[attr_id], str(raw)))

    return rows
