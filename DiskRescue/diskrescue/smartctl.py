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
    if proc.returncode not in (0, 1):
        raise SmartctlError(proc.stderr.strip() or "设备扫描失败")

    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise SmartctlError(f"无法解析 smartctl 输出：{exc}") from exc

    return [
        Device(
            name=str(item.get("name", "")),
            device_type=str(item.get("type", "")),
            protocol=str(item.get("protocol", "")),
        )
        for item in payload.get("devices", [])
        if item.get("name")
    ]


def read_smart(device: Device) -> dict[str, Any]:
    exe = smartctl_path()
    if not exe:
        raise SmartctlError("找不到 smartctl.exe。")

    command = [exe, "-a", "-j"]
    if device.device_type:
        command += ["-d", device.device_type]
    command.append(device.name)

    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    # smartctl uses non-zero exit bits for health warnings/status.
    # Parse JSON first because useful attributes can still be present.
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise SmartctlError(
            proc.stderr.strip() or f"SMART 信息读取失败：{exc}"
        ) from exc

    if not payload:
        raise SmartctlError(proc.stderr.strip() or "设备没有返回 SMART 数据")
    return payload


def summarize(payload: dict[str, Any]) -> list[tuple[str, str]]:
    identity = payload.get("model_name") or payload.get("product") or "未知型号"
    serial = payload.get("serial_number") or "不可用"
    capacity = payload.get("user_capacity", {}).get("bytes")
    capacity_text = f"{capacity:,} bytes" if isinstance(capacity, int) else "不可用"

    passed = payload.get("smart_status", {}).get("passed")
    health = (
        "通过（不代表没有故障）"
        if passed is True
        else "未通过/异常"
        if passed is False
        else "未知或不支持"
    )

    temp = payload.get("temperature", {}).get("current")
    temp_text = f"{temp} °C" if isinstance(temp, (int, float)) else "不可用"
    power_on = payload.get("power_on_time", {}).get("hours")
    power_text = (
        f"{power_on} 小时"
        if isinstance(power_on, (int, float))
        else "不可用"
    )

    rows = [
        ("型号", str(identity)),
        ("序列号", str(serial)),
        ("容量", capacity_text),
        ("SMART 总体状态", health),
        ("温度", temp_text),
        ("通电时间", power_text),
    ]

    attributes = payload.get("ata_smart_attributes", {}).get("table", [])
    interesting = {
        5: "重新分配扇区数",
        9: "通电时间",
        187: "不可校正错误",
        188: "命令超时",
        197: "待处理扇区数",
        198: "离线不可校正扇区数",
        199: "接口 CRC 错误",
    }
    for attr in attributes:
        attr_id = attr.get("id")
        if attr_id in interesting:
            raw = attr.get("raw", {}).get("value", "不可用")
            rows.append((interesting[attr_id], str(raw)))

    return rows
