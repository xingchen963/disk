"""SMART report normalization for NVMe and ATA devices.

The normalizer deliberately preserves missing values as unavailable instead of
substituting zero. NVMe Data Units Read/Written are each 512,000 bytes.
"""

from __future__ import annotations

from typing import Any


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        try:
            return int(text, 0)
        except ValueError:
            try:
                return int(text, 10)
            except ValueError:
                return None
    return None


def _display_number(value: int | None) -> str:
    return "设备未提供" if value is None else f"{value:,}"


def _bytes_text(value: int | None) -> str:
    if value is None:
        return "设备未提供"
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if amount < 1000 or unit == "PB":
            return f"{amount:,.2f} {unit}" if unit != "B" else f"{amount:,.0f} B"
        amount /= 1000
    return f"{amount:,.2f} PB"


def _raw_pair(value: int | None, width: int = 12) -> tuple[str, str]:
    if value is None:
        return "设备未提供", "设备未提供"
    if value < 0:
        return f"-{abs(value):0{width}X}", f"{value:,}"
    return f"{value:0{width}X}", f"{value:,}"


def _nvme_version_text(value: Any) -> str:
    if value is None or value == "":
        return "设备未提供"
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return f"NVM Express {int(value[0])}.{int(value[1])}"
        except (TypeError, ValueError):
            return "NVM Express " + ".".join(str(part) for part in value)
    text = str(value).strip()
    if text.lower().startswith("nvm express"):
        return text
    return f"NVM Express {text}"


def _format_link(payload: dict[str, Any]) -> str:
    device = payload.get("device") or {}
    for key in ("pcie_link_mode", "link_mode", "transfer_mode"):
        value = device.get(key) or payload.get(key)
        if value:
            return str(value)

    speed = device.get("pcie_link_speed") or payload.get("pcie_link_speed")
    width = device.get("pcie_link_width") or payload.get("pcie_link_width")
    if speed or width:
        speed_text = f"PCIe {speed}" if speed else "PCIe"
        width_text = f"x{width}" if width else ""
        return f"{speed_text} {width_text}".strip()
    return "设备未提供"


def _supported_features(payload: dict[str, Any], protocol: str, nvme_log: dict[str, Any]) -> str:
    values: list[str] = []
    explicit = payload.get("supported_features") or payload.get("supported_capabilities")
    if isinstance(explicit, (list, tuple)):
        values.extend(str(item) for item in explicit if item)
    elif isinstance(explicit, str) and explicit.strip():
        values.append(explicit.strip())

    if nvme_log and "S.M.A.R.T." not in values:
        values.insert(0, "S.M.A.R.T. / NVMe Health Log")

    ata_support = payload.get("ata_smart_support") or {}
    if isinstance(ata_support, dict) and ata_support.get("enabled") is True:
        if "S.M.A.R.T." not in values:
            values.insert(0, "S.M.A.R.T.")

    trim = payload.get("trim") or payload.get("ata_trim")
    if isinstance(trim, dict):
        enabled = trim.get("supported") or trim.get("enabled")
        if enabled is True:
            values.append("TRIM")
    elif trim is True:
        values.append("TRIM")

    vwc = payload.get("volatile_write_cache")
    if vwc is True:
        values.append("VolatileWriteCache")

    deduped = list(dict.fromkeys(values))
    return "、".join(deduped) if deduped else "设备未提供"


def _nvme_status(key: str, value: int | None, log: dict[str, Any]) -> str:
    if value is None:
        return "设备未提供"
    if key == "critical_warning":
        return "良好" if value == 0 else "警告"
    if key == "available_spare":
        threshold = _as_int(log.get("available_spare_threshold"))
        if threshold is None:
            return "已读取"
        return "良好" if value >= threshold else "警告"
    if key == "percentage_used":
        return "良好" if value <= 100 else "超过寿命估计"
    if key == "media_errors":
        return "良好" if value == 0 else "需关注"
    if key in {"warning_temp_time", "critical_comp_time"}:
        return "未触发" if value == 0 else "曾触发"
    if key == "num_err_log_entries":
        return "无错误日志" if value == 0 else "有日志记录"
    return "已读取"


_NVME_FIELDS = [
    (0x01, "严重警告标志", "critical_warning", ""),
    (0x02, "温度", "temperature", "K"),
    (0x03, "可用备用空间", "available_spare", "%"),
    (0x04, "可用备用空间阈值", "available_spare_threshold", "%"),
    (0x05, "已用寿命百分比", "percentage_used", "%"),
    (0x06, "主机总计读取", "data_units_read", "512,000 字节/单位"),
    (0x07, "主机总计写入", "data_units_written", "512,000 字节/单位"),
    (0x08, "主机读取命令数", "host_reads", "次"),
    (0x09, "主机写入命令数", "host_writes", "次"),
    (0x0A, "控制器忙碌时间", "controller_busy_time", "分钟"),
    (0x0B, "通电次数", "power_cycles", "次"),
    (0x0C, "通电时间（小时）", "power_on_hours", "小时"),
    (0x0D, "不安全关机计数", "unsafe_shutdowns", "次"),
    (0x0E, "介质与数据完整性错误计数", "media_errors", "次"),
    (0x0F, "错误日志项数", "num_err_log_entries", "项"),
    (0x10, "警告综合温度时间", "warning_temp_time", "分钟"),
    (0x11, "严重综合温度时间", "critical_comp_time", "分钟"),
    (0x1A, "热管理温度 1 转换次数", "thm_temp1_trans_count", "次"),
    (0x1B, "热管理温度 2 转换次数", "thm_temp2_trans_count", "次"),
    (0x1C, "热管理温度 1 总时间", "thm_temp1_total_time", "秒"),
    (0x1D, "热管理温度 2 总时间", "thm_temp2_total_time", "秒"),
]


def build_smart_report(
    payload: dict[str, Any] | None,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build top-level SMART summary and a vendor-neutral attribute table."""
    payload = payload or {}
    fallback = fallback or {}
    device = payload.get("device") or {}
    nvme_log = payload.get("nvme_smart_health_information_log") or {}
    is_nvme = bool(nvme_log) or str(device.get("protocol") or "").upper() == "NVME"

    model = str(payload.get("model_name") or payload.get("product") or fallback.get("model") or "设备未提供")
    serial = str(payload.get("serial_number") or fallback.get("serial") or "设备未提供")
    firmware = str(payload.get("firmware_version") or "设备未提供")
    protocol = str(device.get("protocol") or fallback.get("protocol") or ("NVMe" if is_nvme else "设备未提供"))

    capacity = _as_int((payload.get("user_capacity") or {}).get("bytes"))
    if capacity is None:
        capacity = _as_int(payload.get("nvme_total_capacity"))
    if capacity is None:
        capacity = _as_int(fallback.get("capacity_bytes"))

    temperature_c = _as_int((payload.get("temperature") or {}).get("current"))
    if temperature_c is None:
        temperature_c = _as_int(nvme_log.get("temperature"))

    power_hours = _as_int((payload.get("power_on_time") or {}).get("hours"))
    if power_hours is None:
        power_hours = _as_int(nvme_log.get("power_on_hours"))

    power_cycles = _as_int(payload.get("power_cycle_count"))
    if power_cycles is None:
        power_cycles = _as_int(nvme_log.get("power_cycles"))

    used_percent = _as_int((payload.get("endurance_used") or {}).get("current_percent"))
    if used_percent is None:
        used_percent = _as_int(nvme_log.get("percentage_used"))
    remaining_life = max(0, 100 - used_percent) if used_percent is not None else None

    read_units = _as_int(nvme_log.get("data_units_read"))
    write_units = _as_int(nvme_log.get("data_units_written"))
    read_bytes = read_units * 512000 if read_units is not None else None
    write_bytes = write_units * 512000 if write_units is not None else None

    spare = _as_int(nvme_log.get("available_spare"))
    spare_threshold = _as_int(nvme_log.get("available_spare_threshold"))
    spare_summary = (
        f"{spare}%（阈值 {spare_threshold}%）"
        if spare is not None and spare_threshold is not None
        else f"{spare}%" if spare is not None else "设备未提供"
    )

    critical_warning = _as_int(nvme_log.get("critical_warning"))
    smart_passed = (payload.get("smart_status") or {}).get("passed")
    if is_nvme and critical_warning is not None:
        health = "良好" if critical_warning == 0 else "警告"
        health_detail = "严重警告标志为 0" if critical_warning == 0 else f"严重警告位掩码：0x{critical_warning:02X}"
    elif smart_passed is True:
        health, health_detail = "良好", "SMART 总体自检通过（不代表绝无故障）"
    elif smart_passed is False:
        health, health_detail = "警告", "设备报告 SMART 总体状态未通过"
    elif payload:
        health, health_detail = "未知", "设备未提供 SMART 总体健康结论"
    else:
        health, health_detail = "不可用", str(fallback.get("smart_error") or "设备未提供 SMART 数据")

    attributes: list[dict[str, str]] = []

    def add_attribute(attr_id: int, name: str, value: int | None, status: str, unit: str = ""):
        raw_hex, raw_decimal = _raw_pair(value)
        display = _display_number(value)
        if unit == "512,000 字节/单位" and value is not None:
            display = _bytes_text(value * 512000)
        elif unit and value is not None:
            display = f"{display} {unit}"
        attributes.append({
            "status": status,
            "id": f"{attr_id:02X}",
            "name": name,
            "raw_hex": raw_hex,
            "raw_decimal": raw_decimal,
            "display": display,
        })

    if is_nvme and nvme_log:
        for attr_id, name, key, unit in _NVME_FIELDS:
            value = _as_int(nvme_log.get(key))
            if key == "temperature" and value is not None:
                # smartctl JSON reports composite temperature in Celsius;
                # the raw NVMe SMART log encodes Kelvin.
                value = int(round(value + 273.15))
            add_attribute(attr_id, name, value, _nvme_status(key, value, nvme_log), unit)

        sensors = nvme_log.get("temperature_sensors")
        if isinstance(sensors, list):
            for index, sensor in enumerate(sensors[:8], start=0x12):
                value = _as_int(sensor)
                if value in (None, 0):
                    continue
                raw_kelvin = int(round(value + 273.15))
                add_attribute(index, f"温度传感器 {index - 0x11}", raw_kelvin, "已读取", "K")

    else:
        ata_attrs = (payload.get("ata_smart_attributes") or {}).get("table") or []
        for attr in ata_attrs:
            attr_id = _as_int(attr.get("id"))
            if attr_id is None:
                continue
            raw = attr.get("raw") or {}
            raw_value = _as_int(raw.get("value"))
            normalized = _as_int(attr.get("value"))
            threshold = _as_int(attr.get("thresh"))
            when_failed = str(attr.get("when_failed") or "").strip().lower()
            if when_failed == "now":
                status = "失败"
            elif when_failed == "past":
                status = "历史异常"
            elif normalized is not None and threshold not in (None, 0):
                status = "良好" if normalized > threshold else "警告"
            elif raw_value is not None:
                status = "已读取"
            else:
                status = "未知"

            name = str(attr.get("name") or f"ATA 属性 {attr_id:02X}")
            add_attribute(attr_id, name, raw_value, status)
            attributes[-1]["display"] = str(raw.get("string") or attributes[-1]["display"])

    rows = [
        ("预计剩余寿命", f"{remaining_life}%" if remaining_life is not None else "设备未提供"),
        ("可用备用空间", spare_summary),
        ("主机读取量总计", _bytes_text(read_bytes)),
        ("主机写入量总计", _bytes_text(write_bytes)),
        ("通电时间", f"{power_hours:,} 小时" if power_hours is not None else "设备未提供"),
        ("通电次数", _display_number(power_cycles)),
        ("传输模式", _format_link(payload)),
        ("标准", _nvme_version_text(payload.get("nvme_version")) if is_nvme else str(payload.get("ata_version", "设备未提供"))),
        ("支持的功能", _supported_features(payload, protocol, nvme_log)),
    ]

    return {
        "available": bool(payload),
        "is_nvme": is_nvme,
        "model": model,
        "serial": serial,
        "firmware": firmware,
        "protocol": protocol,
        "capacity": _bytes_text(capacity),
        "health": health,
        "health_detail": health_detail,
        "temperature": f"{temperature_c} °C" if temperature_c is not None else "设备未提供",
        "temperature_c": temperature_c,
        "metrics": rows,
        "attributes": attributes,
        "raw_json": payload,
        "attribute_kind": "NVMe SMART / Health Information Log" if is_nvme else "ATA S.M.A.R.T. Attributes",
    }
