import ctypes
import json
import os
import struct
import subprocess
import uuid
from ctypes import wintypes
from typing import Any


def _hidden_subprocess_options() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }


def _run_powershell_json(command: str, timeout: int = 25) -> list[dict[str, Any]]:
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


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def list_windows_disks() -> list[dict[str, Any]]:
    """Enumerate physical disks through Windows Storage, independent of SMART support."""
    command = (
        "Get-Disk | Select-Object Number,FriendlyName,SerialNumber,Size,"
        "PartitionStyle,IsBoot,IsSystem,BusType,OperationalStatus,IsOffline,"
        "IsReadOnly,LogicalSectorSize,PhysicalSectorSize | ConvertTo-Json -Depth 4 -Compress"
    )
    try:
        rows = _run_powershell_json(command)
        return sorted(rows, key=lambda item: _as_int(item.get("Number"), 999999))
    except Exception as storage_error:
        # CIM is a fallback for older Windows/WinPE builds without the Storage cmdlets.
        cim_command = (
            "Get-CimInstance Win32_DiskDrive | Select-Object Index,DeviceID,Model,"
            "SerialNumber,Size,InterfaceType,Status | ConvertTo-Json -Depth 3 -Compress"
        )
        try:
            rows = _run_powershell_json(cim_command)
        except Exception as cim_error:
            raise RuntimeError(
                f"Windows 磁盘枚举失败。Get-Disk：{storage_error}；Win32_DiskDrive：{cim_error}"
            ) from cim_error
        normalized = []
        for row in rows:
            number = _as_int(row.get("Index"), -1)
            if number < 0:
                continue
            normalized.append({
                "Number": number,
                "FriendlyName": row.get("Model") or "",
                "SerialNumber": row.get("SerialNumber") or "",
                "Size": _as_int(row.get("Size")),
                "PartitionStyle": "Unknown",
                "BusType": row.get("InterfaceType") or "Unknown",
                "OperationalStatus": row.get("Status") or "Unknown",
                "IsOffline": False,
                "IsReadOnly": False,
                "LogicalSectorSize": 512,
                "PhysicalSectorSize": 512,
                "DeviceID": row.get("DeviceID") or f"\\\\.\\PhysicalDrive{number}",
                "_inventory_source": "Win32_DiskDrive",
            })
        return sorted(normalized, key=lambda item: item["Number"])


def _normal(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _same_model_capacity(disk: dict[str, Any], model: str, capacity_bytes: int) -> bool:
    disk_model = _normal(disk.get("FriendlyName"))
    wanted_model = _normal(model)
    disk_size = _as_int(disk.get("Size"))
    return bool(
        wanted_model and disk_model == wanted_model and capacity_bytes > 0
        and abs(disk_size - int(capacity_bytes)) <= max(64 * 1024 * 1024, int(capacity_bytes) // 100)
    )


GPT_TYPE_NAMES = {
    "C12A7328F81F11D2BA4B00A0C93EC93B": "EFI 系统分区",
    "E3C9E3160B5C4DB8817DF92DF00215AE": "Microsoft 保留分区（MSR）",
    "EBD0A0A2B9E5443387C068B6B72699C7": "Microsoft 基本数据分区",
    "DE94BBA406D14D40A16ABFD50179D6AC": "Windows 恢复分区",
    "21686148-6449-6E6F-744E-656564454649": "BIOS 启动分区",
    "0FC63DAF848347728E793D69D8477DE4": "Linux 文件系统分区",
    "0657FD6DA4AB43C484E50933C84B4F4F": "Linux Swap 分区",
    "E6D6D379F50744C2A23C238F2A3DF928": "Linux LVM 分区",
    "A19D880F05FC4D3BA006743F0F84911E": "Linux RAID 分区",
    "7C3457EF000011AAAA1100306543ECAC": "Apple APFS 分区",
    "48465300000011AAAA1100306543ECAC": "Apple HFS/HFS+ 分区",
    "426F6F74000011AAAA1100306543ECAC": "Apple 启动分区",
    "516E7CB46ECF11D68FF800022D09712B": "FreeBSD 数据分区",
    "516E7CB56ECF11D68FF800022D09712B": "FreeBSD Swap 分区",
    "9E1A2D38C6124316AA268B49521E5A8B": "ChromeOS 内核分区",
}


MBR_TYPE_NAMES = {
    0x01: "FAT12",
    0x04: "FAT16（小于 32MB）",
    0x06: "FAT16",
    0x07: "NTFS / exFAT / HPFS 类型",
    0x0B: "FAT32",
    0x0C: "FAT32 LBA",
    0x0E: "FAT16 LBA",
    0x11: "隐藏 FAT12",
    0x14: "隐藏 FAT16",
    0x16: "隐藏 FAT16",
    0x17: "隐藏 NTFS / HPFS",
    0x1B: "隐藏 FAT32",
    0x1C: "隐藏 FAT32 LBA",
    0x27: "Windows 恢复 / 隐藏分区",
    0x42: "Windows 动态磁盘",
    0x82: "Linux Swap / Solaris",
    0x83: "Linux 文件系统",
    0x85: "Linux 扩展分区",
    0x8E: "Linux LVM",
    0xA5: "FreeBSD",
    0xA6: "OpenBSD",
    0xA8: "Apple UFS",
    0xA9: "NetBSD",
    0xAB: "Apple 启动分区",
    0xAF: "Apple HFS/HFS+",
    0xEE: "GPT 保护分区",
    0xEF: "EFI 系统分区",
    0xFD: "Linux RAID",
}


def _guid_name(raw_guid: bytes) -> tuple[str, str]:
    try:
        guid = str(uuid.UUID(bytes_le=raw_guid)).upper()
    except (ValueError, AttributeError):
        return "", "未知 GPT 分区类型"
    compact = guid.replace("-", "")
    return guid, GPT_TYPE_NAMES.get(compact, GPT_TYPE_NAMES.get(guid, f"未知 GPT 类型 {guid}"))


def _parse_gpt(raw_disk, sector_size: int) -> tuple[list[dict[str, Any]], str] | None:
    for header_offset in sorted({512, max(512, sector_size)}):
        header = raw_disk.read(header_offset, max(512, sector_size))
        if len(header) < 92 or header[:8] != b"EFI PART":
            continue
        entries_lba = struct.unpack_from("<Q", header, 72)[0]
        entry_count = struct.unpack_from("<I", header, 80)[0]
        entry_size = struct.unpack_from("<I", header, 84)[0]
        if not (0 < entry_count <= 4096 and 128 <= entry_size <= 4096):
            return [], "GPT（分区表参数异常）"
        table_bytes = min(entry_count * entry_size, 8 * 1024 * 1024)
        table = raw_disk.read(entries_lba * sector_size, table_bytes)
        partitions = []
        actual_count = min(entry_count, len(table) // entry_size)
        for index in range(actual_count):
            entry = table[index * entry_size:(index + 1) * entry_size]
            type_raw = entry[:16]
            if type_raw == b"\x00" * 16:
                continue
            type_guid, type_name = _guid_name(type_raw)
            first_lba = struct.unpack_from("<Q", entry, 32)[0]
            last_lba = struct.unpack_from("<Q", entry, 40)[0]
            attributes = struct.unpack_from("<Q", entry, 48)[0]
            if last_lba < first_lba:
                continue
            name_bytes = entry[56:min(entry_size, 128)]
            name = name_bytes.decode("utf-16le", errors="ignore").rstrip("\x00").strip()
            partitions.append({
                "PartitionNumber": index + 1,
                "Offset": first_lba * sector_size,
                "Size": (last_lba - first_lba + 1) * sector_size,
                "PartitionTypeGuid": type_guid,
                "PartitionTypeName": type_name,
                "PartitionName": name,
                "GptAttributes": attributes,
                "DriveLetter": "",
                "Type": "GPT",
                "IsHidden": bool(attributes & (1 << 62)),
                "IsSystem": False,
                "IsBoot": False,
                "NoDefaultDriveLetter": False,
                "IsReadOnly": False,
                "IsOffline": False,
                "_geometry_source": "GPT 原始分区表",
            })
        return partitions, "GPT"
    return None


def _mbr_partition(index: int, part_type: int, start_lba: int, sectors: int, sector_size: int, boot: int = 0) -> dict[str, Any]:
    return {
        "PartitionNumber": index,
        "Offset": start_lba * sector_size,
        "Size": sectors * sector_size,
        "PartitionTypeGuid": "",
        "PartitionTypeName": MBR_TYPE_NAMES.get(part_type, f"未知 MBR 类型 0x{part_type:02X}"),
        "MbrType": f"0x{part_type:02X}",
        "DriveLetter": "",
        "Type": "MBR",
        "IsHidden": part_type in {0x11, 0x14, 0x16, 0x17, 0x1B, 0x1C},
        "IsSystem": False,
        "IsBoot": boot == 0x80,
        "NoDefaultDriveLetter": False,
        "IsReadOnly": False,
        "IsOffline": False,
        "_geometry_source": "MBR 原始分区表",
    }


def _parse_mbr(raw_disk, sector_size: int, first_sector: bytes) -> tuple[list[dict[str, Any]], str] | None:
    if len(first_sector) < 512 or first_sector[510:512] != b"\x55\xAA":
        return None
    partitions = []
    extended_bases = []
    for slot in range(4):
        entry = first_sector[446 + slot * 16:462 + slot * 16]
        boot, part_type = entry[0], entry[4]
        start_lba, sectors = struct.unpack_from("<II", entry, 8)
        if part_type == 0 or sectors == 0:
            continue
        if part_type in {0x05, 0x0F, 0x85}:
            extended_bases.append(start_lba)
            continue
        if part_type == 0xEE:
            continue
        partitions.append(_mbr_partition(slot + 1, part_type, start_lba, sectors, sector_size, boot))

    # Follow the Extended Boot Record chain so logical partitions are not omitted.
    logical_number = 5
    for base_lba in extended_bases:
        current_lba = base_lba
        visited = set()
        for _ in range(128):
            if current_lba in visited:
                break
            visited.add(current_lba)
            ebr = raw_disk.read(current_lba * sector_size, 512)
            if len(ebr) < 512 or ebr[510:512] != b"\x55\xAA":
                break
            first = ebr[446:462]
            ptype = first[4]
            relative, sectors = struct.unpack_from("<II", first, 8)
            if ptype and sectors:
                partitions.append(_mbr_partition(
                    logical_number, ptype, current_lba + relative, sectors, sector_size, first[0]
                ))
                logical_number += 1
            link = ebr[462:478]
            link_type = link[4]
            next_relative, next_sectors = struct.unpack_from("<II", link, 8)
            if link_type not in {0x05, 0x0F, 0x85} or next_sectors == 0:
                break
            current_lba = base_lba + next_relative

    if any(first_sector[446 + slot * 16 + 4] == 0xEE for slot in range(4)):
        return partitions, "GPT（保护 MBR 存在，但 GPT 头未读取成功）"
    return partitions, "MBR"


def _parse_apm(raw_disk, first_sector: bytes) -> tuple[list[dict[str, Any]], str] | None:
    if len(first_sector) < 8 or first_sector[:2] != b"ER":
        return None
    block_size = struct.unpack_from(">H", first_sector, 2)[0]
    total_blocks = struct.unpack_from(">I", first_sector, 4)[0]
    if not (512 <= block_size <= 65536 and total_blocks > 0):
        return [], "Apple Partition Map（APM，参数异常）"
    first_map = raw_disk.read(block_size, block_size)
    if len(first_map) < 16 or first_map[:2] != b"PM":
        return [], "Apple Partition Map（APM，分区映射不可读）"
    map_count = struct.unpack_from(">I", first_map, 4)[0]
    if not (0 < map_count <= 4096):
        return [], "Apple Partition Map（APM，分区数量异常）"
    partitions = []
    for index in range(map_count):
        entry = raw_disk.read((index + 1) * block_size, block_size)
        if len(entry) < 80 or entry[:2] != b"PM":
            continue
        start_block, block_count = struct.unpack_from(">II", entry, 8)
        name = entry[16:48].split(b"\x00", 1)[0].decode("mac_roman", errors="replace").strip()
        part_type = entry[48:80].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
        if block_count <= 0:
            continue
        partitions.append({
            "PartitionNumber": index + 1,
            "Offset": start_block * block_size,
            "Size": block_count * block_size,
            "PartitionTypeGuid": "",
            "PartitionTypeName": part_type or "Apple 分区",
            "PartitionName": name,
            "DriveLetter": "",
            "Type": "APM",
            "IsHidden": False,
            "IsSystem": False,
            "IsBoot": False,
            "NoDefaultDriveLetter": False,
            "IsReadOnly": False,
            "IsOffline": False,
            "_geometry_source": "Apple Partition Map",
        })
    return partitions, "Apple Partition Map（APM）"


def _detect_partition_table(raw_disk, disk_row: dict[str, Any]) -> tuple[str, list[dict[str, Any]], str]:
    sector_size = _as_int(disk_row.get("LogicalSectorSize"), 512)
    if not (512 <= sector_size <= 65536):
        sector_size = 512
    first = raw_disk.read(0, max(4096, sector_size))
    if not first:
        return str(disk_row.get("PartitionStyle") or "未知"), [], raw_disk.error or "无法读取原始磁盘扇区"

    gpt = _parse_gpt(raw_disk, sector_size)
    if gpt is not None:
        return gpt[1], gpt[0], "原始磁盘 GPT 表头与分区项"
    apm = _parse_apm(raw_disk, first)
    if apm is not None:
        return apm[1], apm[0], "Apple Partition Map 原始扇区"
    mbr = _parse_mbr(raw_disk, sector_size, first)
    if mbr is not None:
        return mbr[1], mbr[0], "MBR 原始分区表"

    # Identify common legacy labels without pretending to parse their partition geometry.
    for probe in (first[0x1C0:0x1C4], first[0x40:0x44], first[0x80:0x84]):
        if probe in {b"\x57\x45\x56\x82", b"\x82\x56\x45\x57"}:
            return "BSD Disklabel（已识别，分区解析待实现）", [], "BSD disklabel 签名"
    if len(first) >= 510 and first[508:510] in {b"\xDA\xBE", b"\xBE\xDA"}:
        return "Solaris VTOC（已识别，分区解析待实现）", [], "Solaris VTOC 签名"

    windows_style = str(disk_row.get("PartitionStyle") or "RAW")
    if windows_style.upper() == "RAW":
        return "RAW / 未识别", [], "Windows 与原始扇区均未识别常见分区表"
    return windows_style, [], "Windows Storage API"


def _detect_filesystem(data: bytes, partition_type_name: str = "") -> tuple[str, str] | None:
    if not data:
        return None
    if len(data) >= 11 and data[3:9] == b"FVE-FS":
        return "BitLocker 加密卷", "分区引导扇区签名"
    if len(data) >= 11 and data[3:11] == b"NTFS    ":
        return "NTFS", "分区引导扇区签名"
    if len(data) >= 11 and data[3:11] == b"EXFAT   ":
        return "exFAT", "分区引导扇区签名"
    if len(data) >= 90 and data[82:90] == b"FAT32   ":
        return "FAT32", "FAT 引导扇区签名"
    if len(data) >= 62 and data[54:62] == b"FAT16   ":
        return "FAT16", "FAT 引导扇区签名"
    if len(data) >= 62 and data[54:62] == b"FAT12   ":
        return "FAT12", "FAT 引导扇区签名"
    if len(data) >= 7 and data[3:7] == b"ReFS":
        return "ReFS（签名候选）", "分区引导扇区签名"
    if data[:6] == b"LUKS\xBA\xBE":
        return "LUKS 加密容器", "LUKS 魔数"
    if len(data) >= 1024 + 58 and struct.unpack_from("<H", data, 1024 + 56)[0] == 0xEF53:
        rev = struct.unpack_from("<I", data, 1024 + 76)[0]
        compat = struct.unpack_from("<I", data, 1024 + 92)[0]
        incompat = struct.unpack_from("<I", data, 1024 + 96)[0]
        if rev == 0:
            fs = "ext2"
        elif incompat & 0x40:
            fs = "ext4"
        elif compat & 0x04:
            fs = "ext3"
        else:
            fs = "ext 系列（版本待确认）"
        return fs, "ext 超级块魔数"
    if data[:4] == b"XFSB":
        return "XFS", "XFS 超级块魔数"
    if len(data) > 0x10048 and data[0x10040:0x10048] == b"_BHRfS_M":
        return "Btrfs", "Btrfs 超级块魔数"
    if len(data) >= 36 and data[32:36] == b"NXSB":
        return "Apple APFS", "APFS 容器超级块魔数"
    if len(data) >= 1026 and data[1024:1026] in {b"H+", b"HX"}:
        return "Apple HFS+", "HFS+ 卷头签名"
    if len(data) >= 16 * 2048 + 6 and data[16 * 2048 + 1:16 * 2048 + 6] == b"CD001":
        return "ISO 9660", "光盘卷描述符签名"
    if len(data) >= 32768 + 6 and data[32768 + 1:32768 + 6] in {b"BEA01", b"NSR02", b"NSR03"}:
        return "UDF", "UDF 卷识别序列"
    if len(data) >= 520 and data[512:520] == b"LABELONE":
        return "Linux LVM PV", "LVM2 标签签名"
    for page_size in (4096, 8192, 16384, 32768, 65536):
        signature_at = page_size - 10
        if len(data) >= signature_at + 10 and data[signature_at:signature_at + 10] in {b"SWAPSPACE2", b"SWAP-SPACE"}:
            return "Linux Swap", "Swap 分区尾部签名"
    type_text = partition_type_name.casefold()
    if "swap" in type_text:
        return "Linux Swap（按分区类型推断）", "GPT/MBR 分区类型"
    if "lvm" in type_text:
        return "Linux LVM（按分区类型推断）", "GPT/MBR 分区类型"
    if "raid" in type_text:
        return "RAID 成员分区（按分区类型推断）", "GPT/MBR 分区类型"
    return None


class _RawDisk:
    """Minimal read-only handle to a physical disk; reads only requested metadata ranges."""
    def __init__(self, disk_number: int):
        self.handle = None
        self.error = ""
        self.kernel32 = None
        if os.name != "nt":
            self.error = "原始磁盘扇区读取仅在 Windows 上实现"
            return
        try:
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel32.CreateFileW.argtypes = [
                wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE
            ]
            self.kernel32.CreateFileW.restype = ctypes.c_void_p
            self.kernel32.SetFilePointerEx.argtypes = [
                ctypes.c_void_p, ctypes.c_longlong,
                ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD
            ]
            self.kernel32.SetFilePointerEx.restype = wintypes.BOOL
            self.kernel32.ReadFile.argtypes = [
                ctypes.c_void_p, wintypes.LPVOID, wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID
            ]
            self.kernel32.ReadFile.restype = wintypes.BOOL
            self.kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            self.kernel32.CloseHandle.restype = wintypes.BOOL

            path = f"\\\\.\\PhysicalDrive{disk_number}"
            handle = self.kernel32.CreateFileW(
                path, 0x80000000, 0x00000001 | 0x00000002,
                None, 3, 0x00000080, None
            )
            invalid = ctypes.c_void_p(-1).value
            if handle == invalid:
                code = ctypes.get_last_error()
                self.error = f"无法只读打开物理磁盘（Windows 错误 {code}；可能需要管理员权限）"
            else:
                self.handle = handle
        except Exception as exc:
            self.error = f"原始磁盘读取初始化失败：{exc}"

    def read(self, offset: int, length: int) -> bytes:
        if self.handle is None or length <= 0:
            return b""
        length = min(int(length), 8 * 1024 * 1024)
        new_position = ctypes.c_longlong()
        if not self.kernel32.SetFilePointerEx(
            self.handle, ctypes.c_longlong(max(0, int(offset))),
            ctypes.byref(new_position), 0
        ):
            self.error = f"定位磁盘扇区失败（Windows 错误 {ctypes.get_last_error()}）"
            return b""
        buffer = ctypes.create_string_buffer(length)
        received = wintypes.DWORD(0)
        if not self.kernel32.ReadFile(
            self.handle, buffer, length, ctypes.byref(received), None
        ):
            self.error = f"只读扇区读取失败（Windows 错误 {ctypes.get_last_error()}）"
            return b""
        return buffer.raw[:received.value]

    def close(self):
        if self.handle is not None and self.kernel32 is not None:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None


def _merge_windows_partition(raw_part: dict[str, Any], windows_parts: list[dict[str, Any]]) -> dict[str, Any]:
    offset = _as_int(raw_part.get("Offset"), -1)
    size = _as_int(raw_part.get("Size"))
    match = None
    for candidate in windows_parts:
        candidate_offset = _as_int(candidate.get("Offset"), -2)
        if candidate_offset == offset and abs(_as_int(candidate.get("Size")) - size) <= 1024 * 1024:
            match = candidate
            break
    merged = dict(raw_part)
    if match:
        merged.update({key: value for key, value in match.items() if value not in (None, "")})
        merged["Offset"] = offset
        merged["Size"] = size
        windows_parts.remove(match)
    return merged


def get_disk_layout(
    serial: str,
    model: str = "",
    capacity_bytes: int = 0,
    disk_number: int | None = None,
) -> dict[str, Any]:
    """Identify partition tables and file-system signatures without writing to disk."""
    try:
        disks = list_windows_disks()
    except Exception as exc:
        return {"error": f"读取 Windows 物理磁盘失败：{exc}", "partitions": []}

    if disk_number is not None:
        matches = [disk for disk in disks if _as_int(disk.get("Number"), -1) == int(disk_number)]
    else:
        matches = [disk for disk in disks if _same_model_capacity(disk, model, capacity_bytes)]
    if len(matches) != 1:
        reason = (
            "没有找到指定的 Windows 物理磁盘"
            if not matches else "多块 Windows 物理磁盘匹配，无法安全确定目标"
        )
        return {"error": reason + "；为防止显示错盘，未展示分区。", "partitions": []}

    selected = matches[0]
    number = _as_int(selected.get("Number"), -1)
    if number < 0:
        return {"error": "Windows 未提供有效的物理磁盘编号。", "partitions": []}

    try:
        windows_rows = _run_powershell_json(
            "Get-Partition | Select-Object DiskNumber,PartitionNumber,DriveLetter,AccessPaths,"
            "Type,Size,Offset,GptType,MbrType,IsHidden,IsSystem,IsBoot,NoDefaultDriveLetter,"
            "IsReadOnly,IsOffline | ConvertTo-Json -Depth 4 -Compress"
        )
        windows_parts = [row for row in windows_rows if _as_int(row.get("DiskNumber"), -1) == number]
    except Exception as exc:
        windows_parts = []
        partition_error = str(exc)
    else:
        partition_error = ""

    try:
        volumes = _run_powershell_json(
            "Get-Volume | Select-Object DriveLetter,Path,UniqueId,FileSystemLabel,FileSystem,"
            "Size,SizeRemaining,HealthStatus | ConvertTo-Json -Depth 3 -Compress"
        )
    except Exception as exc:
        volumes = []
        volume_error = str(exc)
    else:
        volume_error = ""

    volumes_by_key = {}
    for volume in volumes:
        for key in (volume.get("DriveLetter"), volume.get("Path"), volume.get("UniqueId")):
            normalized = _normal(key)
            if normalized:
                volumes_by_key[normalized] = volume

    raw_disk = _RawDisk(number)
    try:
        table_style, raw_parts, table_source = _detect_partition_table(raw_disk, selected)
        if not raw_parts:
            parts = [dict(part) for part in windows_parts]
        else:
            remaining_windows = [dict(part) for part in windows_parts]
            parts = [_merge_windows_partition(part, remaining_windows) for part in raw_parts]
            parts.extend(remaining_windows)

        disk_size = _as_int(selected.get("Size"))
        for part in parts:
            drive = str(part.get("DriveLetter") or "").strip().upper()
            access_paths = part.get("AccessPaths") or []
            if isinstance(access_paths, str):
                access_paths = [access_paths]
            volume = {}
            for key in [drive] + list(access_paths):
                normalized = _normal(key)
                if normalized and normalized in volumes_by_key:
                    volume = volumes_by_key[normalized]
                    break

            volume_size = volume.get("Size")
            remaining = volume.get("SizeRemaining")
            used = None
            if isinstance(volume_size, (int, float)) and isinstance(remaining, (int, float)):
                used = max(int(volume_size) - int(remaining), 0)

            partition_type_name = str(
                part.get("PartitionTypeName")
                or _partition_type_from_windows(part)
                or ""
            )
            offset = _as_int(part.get("Offset"))
            size = _as_int(part.get("Size"))
            detected = None
            if size > 0:
                probe = raw_disk.read(offset, min(size, 131072))
                detected = _detect_filesystem(probe, partition_type_name)

            windows_fs = str(volume.get("FileSystem") or "").strip()
            fs_name = windows_fs or (detected[0] if detected else "未识别 / 未挂载")
            fs_source = "Windows Get-Volume" if windows_fs else (detected[1] if detected else "未检测到已知签名")
            if not windows_fs and not detected and partition_type_name:
                inferred = _filesystem_from_partition_type(partition_type_name)
                if inferred:
                    fs_name = inferred
                    fs_source = "GPT/MBR 分区类型推断"

            part.update({
                "DiskNumber": number,
                "DriveLetter": drive,
                "AccessPaths": access_paths,
                "Offset": offset,
                "Size": size,
                "FileSystem": windows_fs,
                "DetectedFileSystem": fs_name,
                "FileSystemSource": fs_source,
                "FileSystemLabel": str(volume.get("FileSystemLabel") or part.get("PartitionName") or ""),
                "SizeRemaining": int(remaining) if isinstance(remaining, (int, float)) else None,
                "SizeUsed": used,
                "VolumeSize": int(volume_size) if isinstance(volume_size, (int, float)) else None,
                "HealthStatus": str(volume.get("HealthStatus") or ""),
                "PartitionTableStyle": table_style,
                "PartitionTypeName": partition_type_name or "未知分区类型",
                "IsHidden": bool(part.get("IsHidden") or part.get("NoDefaultDriveLetter")) or not bool(drive),
                "IsReadOnly": bool(part.get("IsReadOnly")),
                "IsOffline": bool(part.get("IsOffline")) or bool(selected.get("IsOffline")),
            })

        parts.sort(key=lambda item: (_as_int(item.get("Offset")), _as_int(item.get("PartitionNumber"))))
        return {
            "disk_number": number,
            "friendly_name": selected.get("FriendlyName"),
            "serial_number": selected.get("SerialNumber"),
            "match_method": "Windows 物理磁盘编号" if disk_number is not None else "型号和容量（唯一匹配）",
            "partition_style": str(selected.get("PartitionStyle") or ""),
            "partition_table_style": table_style,
            "partition_table_source": table_source,
            "disk_size": disk_size,
            "logical_sector_size": _as_int(selected.get("LogicalSectorSize"), 512),
            "physical_sector_size": _as_int(selected.get("PhysicalSectorSize"), 512),
            "bus_type": str(selected.get("BusType") or ""),
            "operational_status": str(selected.get("OperationalStatus") or ""),
            "is_offline": bool(selected.get("IsOffline")),
            "is_read_only": bool(selected.get("IsReadOnly")),
            "partitions": parts,
            "partition_warning": partition_error,
            "volume_warning": volume_error,
            "raw_read_warning": raw_disk.error,
            "raw_read_available": raw_disk.handle is not None,
        }
    finally:
        raw_disk.close()


def _partition_type_from_windows(part: dict[str, Any]) -> str:
    gpt = _normal(part.get("GptType"))
    if gpt:
        return GPT_TYPE_NAMES.get(gpt, f"未知 GPT 类型 {part.get('GptType')}")
    mbr = str(part.get("MbrType") or "").strip().lower()
    try:
        code = int(mbr, 0) if mbr else 0
    except ValueError:
        try:
            code = int(mbr, 16)
        except ValueError:
            code = 0
    if code:
        return MBR_TYPE_NAMES.get(code, f"未知 MBR 类型 0x{code:02X}")
    type_name = str(part.get("Type") or "").strip()
    return type_name or "未知分区类型"


def _filesystem_from_partition_type(type_name: str) -> str:
    lowered = type_name.casefold()
    if "swap" in lowered:
        return "Linux Swap（类型推断，未验证卷内容）"
    if "lvm" in lowered:
        return "Linux LVM（类型推断，未验证卷内容）"
    if "raid" in lowered:
        return "RAID 成员分区（类型推断，未验证卷内容）"
    return ""
