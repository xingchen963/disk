"""Read-only HFS+ / HFSX volume and catalog inspection.

This module does not mount or modify a volume. It reads only the volume header,
special-file allocation metadata, allocation bitmap, and Catalog/Extents B-trees.
"""

from __future__ import annotations

import math
import struct
import unicodedata
from typing import Any, Callable


ReadAt = Callable[[int, int], bytes]

MAX_BTREE_NODES = 100_000
MAX_CATALOG_ENTRIES = 20_000
MAX_BITMAP_BYTES = 64 * 1024 * 1024
MAX_EXTENT_RECORDS = 20_000


class HFSPlusError(RuntimeError):
    pass


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from(">H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def _u64(data: bytes, offset: int) -> int:
    return struct.unpack_from(">Q", data, offset)[0]


def _read_exact(read_at: ReadAt, offset: int, length: int, label: str) -> bytes:
    if offset < 0 or length < 0:
        raise HFSPlusError(f"{label} 的读取偏移或长度无效")
    data = read_at(offset, length)
    if len(data) != length:
        raise HFSPlusError(
            f"{label} 读取不完整：需要 {length} 字节，实际读取 {len(data)} 字节"
        )
    return data


def _parse_fork(data: bytes, offset: int, block_size: int, total_blocks: int, name: str) -> dict[str, Any]:
    if len(data) < offset + 80:
        raise HFSPlusError(f"{name} ForkData 结构长度不足")
    logical_size = _u64(data, offset)
    clump_size = _u32(data, offset + 8)
    declared_blocks = _u32(data, offset + 12)
    extents = []
    for index in range(8):
        start = _u32(data, offset + 16 + index * 8)
        count = _u32(data, offset + 20 + index * 8)
        if start == 0 and count == 0:
            continue
        if count == 0 or start + count > total_blocks:
            raise HFSPlusError(f"{name} 的第 {index + 1} 个 extent 超出卷边界")
        extents.append((start, count))

    if logical_size and not extents:
        raise HFSPlusError(f"{name} 有逻辑长度，但没有有效 extent")
    if logical_size > max(total_blocks * block_size, 0):
        raise HFSPlusError(f"{name} 的逻辑长度超过卷容量")
    return {
        "name": name,
        "logical_size": logical_size,
        "clump_size": clump_size,
        "declared_blocks": declared_blocks,
        "extents": extents,
    }


class _ForkReader:
    def __init__(
        self,
        read_at: ReadAt,
        block_size: int,
        total_blocks: int,
        fork: dict[str, Any],
    ):
        self.read_at = read_at
        self.block_size = block_size
        self.total_blocks = total_blocks
        self.fork = fork
        self.logical_size = int(fork["logical_size"])
        self.extents = list(fork["extents"])
        self._coverage = sum(count for _, count in self.extents) * block_size

    def append_extents(self, extents: list[tuple[int, int]]) -> None:
        for start, count in extents:
            if count <= 0:
                continue
            if start < 0 or start + count > self.total_blocks:
                raise HFSPlusError(f"{self.fork['name']} 的扩展 extent 超出卷边界")
            self.extents.append((start, count))
            self._coverage += count * self.block_size

    def read(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset + length > self.logical_size:
            raise HFSPlusError(
                f"{self.fork['name']} 读取范围越界：offset={offset}, length={length}"
            )
        if length == 0:
            return b""
        if offset + length > self._coverage:
            raise HFSPlusError(
                f"{self.fork['name']} 的前八个 extent 不足，且扩展 extent 未完整解析"
            )

        output = bytearray()
        logical_base = 0
        remaining = length
        cursor = offset

        for start_block, block_count in self.extents:
            extent_bytes = block_count * self.block_size
            extent_end = logical_base + extent_bytes
            if cursor >= extent_end:
                logical_base = extent_end
                continue
            if cursor < logical_base:
                raise HFSPlusError(f"{self.fork['name']} 的 extent 映射出现间隙")

            within_extent = cursor - logical_base
            physical_offset = start_block * self.block_size + within_extent
            take = min(remaining, extent_bytes - within_extent)
            chunk = _read_exact(
                self.read_at,
                physical_offset,
                take,
                f"{self.fork['name']} extent 数据",
            )
            output.extend(chunk)
            cursor += take
            remaining -= take
            logical_base = extent_end
            if remaining == 0:
                break

        if remaining:
            raise HFSPlusError(f"{self.fork['name']} 的 extent 列表未覆盖请求数据")
        return bytes(output)


def _iter_btree_leaf_records(fork_reader: _ForkReader):
    """Yield raw keyed records by following the B-tree leaf-node forward links."""
    if fork_reader.logical_size < 512:
        raise HFSPlusError("B-tree 文件太小，无法包含头节点")

    header_node = fork_reader.read(0, min(512, fork_reader.logical_size))
    if header_node[8] != 1:
        raise HFSPlusError("B-tree 节点 0 不是 Header Node")

    header_record = header_node[14:]
    if len(header_record) < 42:
        raise HFSPlusError("B-tree Header Record 长度不足")

    first_leaf = _u32(header_record, 10)
    last_leaf = _u32(header_record, 14)
    node_size = _u16(header_record, 18)
    total_nodes = _u32(header_record, 22)
    leaf_record_count = _u32(header_record, 6)
    attributes = _u32(header_record, 38)

    if node_size < 512 or node_size > 32768 or node_size & (node_size - 1):
        raise HFSPlusError(f"B-tree 节点大小无效：{node_size}")
    if total_nodes <= 0 or total_nodes > max(1, fork_reader.logical_size // node_size):
        raise HFSPlusError("B-tree totalNodes 与文件长度不一致")
    if not (attributes & 0x02):
        raise HFSPlusError("B-tree 未设置 HFS Plus 必需的 BigKeys 标志")

    if first_leaf == 0:
        if leaf_record_count:
            raise HFSPlusError("B-tree 声明有叶记录，但 firstLeafNode 为 0")
        return {
            "node_size": node_size,
            "total_nodes": total_nodes,
            "leaf_records_declared": leaf_record_count,
            "leaf_records_read": 0,
            "truncated": False,
        }, iter(())

    if first_leaf >= total_nodes:
        raise HFSPlusError("B-tree firstLeafNode 超出 totalNodes")

    def records():
        node_number = first_leaf
        visited = set()
        nodes_read = 0
        records_read = 0

        while node_number:
            if node_number >= total_nodes:
                raise HFSPlusError(f"B-tree 叶节点编号越界：{node_number}")
            if node_number in visited:
                raise HFSPlusError("B-tree 叶节点链出现循环")
            if nodes_read >= MAX_BTREE_NODES:
                raise HFSPlusError(
                    f"B-tree 叶节点数量超过安全上限 {MAX_BTREE_NODES}，目录扫描已截断"
                )

            visited.add(node_number)
            node = fork_reader.read(node_number * node_size, node_size)
            if node[8] != 0xFF:
                raise HFSPlusError(f"B-tree 节点 {node_number} 不是 Leaf Node")

            forward_link = _u32(node, 0)
            record_count = _u16(node, 10)
            offsets_bytes = 2 * (record_count + 1)
            offsets_start = node_size - offsets_bytes
            if record_count > (node_size - 14) // 2 - 1 or offsets_start < 14:
                raise HFSPlusError(f"B-tree 节点 {node_number} 的 record offset 表损坏")

            offsets = [
                _u16(node, node_size - 2 * (index + 1))
                for index in range(record_count + 1)
            ]
            if not offsets or offsets[0] < 14:
                raise HFSPlusError(f"B-tree 节点 {node_number} 的首条记录偏移无效")
            if any(a >= b for a, b in zip(offsets, offsets[1:])):
                if record_count:
                    raise HFSPlusError(f"B-tree 节点 {node_number} 的记录偏移顺序无效")
            if offsets[-1] > offsets_start:
                raise HFSPlusError(f"B-tree 节点 {node_number} 的记录与 offset 表重叠")

            for index in range(record_count):
                start = offsets[index]
                end = offsets[index + 1]
                if end > start:
                    yield node[start:end]
                    records_read += 1

            nodes_read += 1
            node_number = forward_link
            if nodes_read >= MAX_BTREE_NODES and node_number:
                raise HFSPlusError(
                    f"B-tree 叶节点数量超过安全上限 {MAX_BTREE_NODES}，目录扫描已截断"
                )

        # Attach scan counters to a small mutable object owned by the outer scope.
        state["nodes_read"] = nodes_read
        state["records_read"] = records_read

    state = {"nodes_read": 0, "records_read": 0}
    iterator = records()
    return {
        "node_size": node_size,
        "total_nodes": total_nodes,
        "leaf_records_declared": leaf_record_count,
        "state": state,
        "truncated": False,
    }, iterator


def _collect_extents_overflow(
    extents_reader: _ForkReader | None,
) -> dict[tuple[int, int, int], list[tuple[int, int]]]:
    mapping: dict[tuple[int, int, int], list[tuple[int, int]]] = {}
    if extents_reader is None or extents_reader.logical_size == 0:
        return mapping

    header, iterator = _iter_btree_leaf_records(extents_reader)
    count = 0
    for record in iterator:
        if len(record) < 2:
            continue
        key_length = _u16(record, 0)
        key_start = 2
        key_end = key_start + key_length
        if key_length < 10 or key_end > len(record):
            continue
        key = record[key_start:key_end]
        data_start = key_end + (key_end & 1)
        if data_start + 64 > len(record):
            continue

        fork_type = key[0]
        file_id = _u32(key, 2)
        start_block = _u32(key, 6)
        extents = []
        for index in range(8):
            start = _u32(record, data_start + index * 8)
            blocks = _u32(record, data_start + index * 8 + 4)
            if start == 0 and blocks == 0:
                continue
            if blocks:
                extents.append((start, blocks))
        mapping[(file_id, fork_type, start_block)] = extents
        count += 1
        if count >= MAX_EXTENT_RECORDS:
            break

    header["overflow_records_read"] = count
    return mapping


def _complete_special_fork(
    fork: dict[str, Any],
    file_id: int,
    extents_map: dict[tuple[int, int, int], list[tuple[int, int]]],
    block_size: int,
    total_blocks: int,
) -> _ForkReader:
    reader = _ForkReader(lambda _offset, _length: b"", block_size, total_blocks, fork)
    required_blocks = math.ceil(reader.logical_size / block_size) if reader.logical_size else 0
    mapped_blocks = sum(count for _, count in reader.extents)

    loops = 0
    while mapped_blocks < required_blocks:
        key = (file_id, 0x00, mapped_blocks)
        additional = extents_map.get(key)
        if not additional:
            raise HFSPlusError(
                f"{fork['name']} 需要 Extents Overflow 记录，但找不到 fileID={file_id}, startBlock={mapped_blocks}"
            )
        reader.append_extents(additional)
        new_mapped = sum(count for _, count in reader.extents)
        if new_mapped <= mapped_blocks:
            raise HFSPlusError(f"{fork['name']} 的扩展 extent 没有增加映射长度")
        mapped_blocks = new_mapped
        loops += 1
        if loops > MAX_EXTENT_RECORDS:
            raise HFSPlusError(f"{fork['name']} 的扩展 extent 数量超过安全上限")
    reader.read_at = reader.read_at  # The actual reader is assigned by the caller.
    return reader


def _parse_catalog_key(record: bytes) -> tuple[int, str, int] | None:
    if len(record) < 10:
        return None
    key_length = _u16(record, 0)
    if key_length < 6 or key_length > 516 or 2 + key_length > len(record):
        return None
    parent_id = _u32(record, 2)
    name_length = _u16(record, 6)
    name_end = 8 + name_length * 2
    if name_length > 255 or name_end > 2 + key_length:
        return None
    raw_name = record[8:name_end]
    name = unicodedata.normalize("NFC", raw_name.decode("utf-16-be", errors="replace"))
    data_start = 2 + key_length
    data_start += data_start & 1
    return parent_id, name, data_start


def _build_entry_path(
    parent_id: int,
    name: str,
    folder_map: dict[int, tuple[int, str]],
) -> str:
    pieces = [name]
    current = parent_id
    visited = set()
    for _ in range(256):
        if current in (1, 2):
            break
        if current in visited:
            pieces.insert(0, "[循环目录]")
            break
        visited.add(current)
        parent = folder_map.get(current)
        if parent is None:
            pieces.insert(0, f"[未知目录 {current}]")
            break
        current, parent_name = parent
        pieces.append(parent_name)
    else:
        pieces.insert(0, "[目录深度超限]")
    return "/" + "/".join(piece for piece in reversed(pieces) if piece)


def _parse_catalog(
    catalog_reader: _ForkReader,
    header_file_count: int,
    header_folder_count: int,
    max_entries: int,
) -> dict[str, Any]:
    tree_header, iterator = _iter_btree_leaf_records(catalog_reader)
    folder_map: dict[int, tuple[int, str]] = {}
    raw_entries: list[dict[str, Any]] = []
    volume_name = ""

    for record in iterator:
        parsed_key = _parse_catalog_key(record)
        if parsed_key is None:
            continue
        parent_id, name, data_start = parsed_key
        if data_start + 2 > len(record):
            continue
        record_type = _u16(record, data_start)

        if record_type == 1:
            if data_start + 12 > len(record):
                continue
            child_count = _u32(record, data_start + 4)
            folder_id = _u32(record, data_start + 8)
            folder_map[folder_id] = (parent_id, name)
            if folder_id == 2 and parent_id == 1:
                volume_name = name
            if folder_id != 2:
                raw_entries.append({
                    "kind": "目录",
                    "name": name,
                    "id": folder_id,
                    "parent_id": parent_id,
                    "size": None,
                    "child_count": child_count,
                })

        elif record_type == 2:
            if data_start + 96 > len(record):
                continue
            file_id = _u32(record, data_start + 8)
            data_fork_size = _u64(record, data_start + 88)
            raw_entries.append({
                "kind": "文件",
                "name": name,
                "id": file_id,
                "parent_id": parent_id,
                "size": data_fork_size,
                "child_count": None,
            })

        if len(raw_entries) >= max_entries:
            break

    entries = []
    for entry in raw_entries:
        item = dict(entry)
        item["path"] = _build_entry_path(
            int(item["parent_id"]),
            str(item["name"]),
            folder_map,
        )
        entries.append(item)

    folder_items = sum(1 for entry in entries if entry["kind"] == "目录")
    file_items = sum(1 for entry in entries if entry["kind"] == "文件")
    iterator_state = tree_header.get("state", {})
    records_read = iterator_state.get("records_read", 0)

    return {
        "volume_name": volume_name or "未命名卷",
        "entries": entries,
        "entries_returned": len(entries),
        "folders_returned": folder_items,
        "files_returned": file_items,
        "header_file_count": header_file_count,
        "header_folder_count": header_folder_count,
        "leaf_records_declared": tree_header["leaf_records_declared"],
        "leaf_records_read": records_read,
        "btree_node_size": tree_header["node_size"],
        "btree_total_nodes": tree_header["total_nodes"],
        "truncated": len(raw_entries) >= max_entries,
    }


def inspect_hfsplus(
    read_at: ReadAt,
    volume_size_bytes: int,
    max_entries: int = 5000,
) -> dict[str, Any]:
    """Inspect an HFS+ or HFSX volume using a bounded, read-only byte reader."""
    if volume_size_bytes < 2048:
        return {"error": "分区容量太小，不可能包含有效的 HFS+ 卷头"}

    primary = _read_exact(read_at, 1024, 512, "HFS+ 主卷头")
    header = primary
    header_location = "主卷头"
    signature = header[:2]

    if signature not in (b"H+", b"HX"):
        alternate_offset = volume_size_bytes - 1024
        if alternate_offset >= 0:
            alternate = _read_exact(read_at, alternate_offset, 512, "HFS+ 备用卷头")
            if alternate[:2] in (b"H+", b"HX"):
                header = alternate
                signature = header[:2]
                header_location = "备用卷头"
            else:
                return {"error": "未找到有效的 HFS+ / HFSX 主卷头或备用卷头"}
        else:
            return {"error": "未找到有效的 HFS+ / HFSX 卷头"}

    version = _u16(header, 2)
    expected_version = 4 if signature == b"H+" else 5
    if version != expected_version:
        raise HFSPlusError(
            f"卷头签名与版本不匹配：{signature.decode('ascii')}, version={version}"
        )

    attributes = _u32(header, 4)
    file_count = _u32(header, 32)
    folder_count = _u32(header, 36)
    block_size = _u32(header, 40)
    total_blocks = _u32(header, 44)
    free_blocks = _u32(header, 48)
    journal_info_block = _u32(header, 12)

    if block_size < 512 or block_size > 1_048_576 or block_size & (block_size - 1):
        raise HFSPlusError(f"卷头中的 allocation block 大小无效：{block_size}")
    if total_blocks <= 0 or free_blocks > total_blocks:
        raise HFSPlusError("卷头中的总 allocation block / 空闲 block 数值无效")
    if total_blocks * block_size > volume_size_bytes + block_size:
        raise HFSPlusError("卷头声明的卷容量超过分区边界")

    allocation_fork = _parse_fork(header, 112, block_size, total_blocks, "Allocation File")
    extents_fork = _parse_fork(header, 192, block_size, total_blocks, "Extents Overflow File")
    catalog_fork = _parse_fork(header, 272, block_size, total_blocks, "Catalog File")
    attributes_fork = _parse_fork(header, 352, block_size, total_blocks, "Attributes File")

    empty_reader = lambda _offset, _length: b""
    extents_reader = _ForkReader(
        read_at, block_size, total_blocks, extents_fork
    )
    extents_map = _collect_extents_overflow(
        extents_reader if extents_fork["logical_size"] else None
    )

    allocation_reader = _complete_special_fork(
        allocation_fork, 6, extents_map, block_size, total_blocks
    )
    allocation_reader.read_at = read_at
    catalog_reader = _complete_special_fork(
        catalog_fork, 4, extents_map, block_size, total_blocks
    )
    catalog_reader.read_at = read_at

    bitmap_required = math.ceil(total_blocks / 8)
    allocation_info: dict[str, Any] = {
        "bitmap_size": allocation_fork["logical_size"],
        "bitmap_read": False,
        "free_blocks_header": free_blocks,
        "free_bytes_header": free_blocks * block_size,
    }
    if bitmap_required <= allocation_fork["logical_size"] and bitmap_required <= MAX_BITMAP_BYTES:
        bitmap = allocation_reader.read(0, bitmap_required)
        allocated_blocks = sum(byte.bit_count() for byte in bitmap)
        extra_bits = bitmap_required * 8 - total_blocks
        if extra_bits and bitmap:
            mask = (1 << extra_bits) - 1
            allocated_blocks -= (bitmap[-1] & mask).bit_count()
        bitmap_free = total_blocks - allocated_blocks
        allocation_info.update({
            "bitmap_read": True,
            "allocated_blocks_bitmap": allocated_blocks,
            "free_blocks_bitmap": bitmap_free,
            "free_bytes_bitmap": bitmap_free * block_size,
            "matches_header": bitmap_free == free_blocks,
        })
    else:
        allocation_info["warning"] = (
            "Allocation bitmap 超过本次只读扫描上限，空间统计采用卷头 freeBlocks。"
        )

    catalog = _parse_catalog(catalog_reader, file_count, folder_count, max_entries)
    warnings = []
    if attributes & (1 << 13):
        warnings.append("该卷标记为 Journaled；本工具不会回放日志，目录结果可能未包含日志中的未提交变更。")
    if not (attributes & (1 << 8)):
        warnings.append("卷头未设置正常卸载标记；建议先制作镜像，不要对原盘执行修复。")
    if attributes & (1 << 11):
        warnings.append("卷头设置了 Boot Volume Inconsistent 标记；元数据可能不一致。")
    if catalog["truncated"]:
        warnings.append(f"目录项目达到本次显示上限 {max_entries}，目录列表已截断。")
    if allocation_info.get("matches_header") is False:
        warnings.append("Allocation bitmap 计算的空闲块数与卷头 freeBlocks 不一致。")

    return {
        "filesystem": "HFS+" if signature == b"H+" else "HFSX",
        "signature": signature.decode("ascii"),
        "version": version,
        "header_location": header_location,
        "attributes": attributes,
        "journaled": bool(attributes & (1 << 13)),
        "clean_unmount": bool(attributes & (1 << 8)) and not bool(attributes & (1 << 11)),
        "journal_info_block": journal_info_block,
        "block_size": block_size,
        "total_blocks": total_blocks,
        "free_blocks": free_blocks,
        "total_bytes": total_blocks * block_size,
        "free_bytes": free_blocks * block_size,
        "file_count": file_count,
        "folder_count": folder_count,
        "allocation": allocation_info,
        "catalog": catalog,
        "attributes_file_size": attributes_fork["logical_size"],
        "warnings": warnings,
        "read_only": True,
    }



def is_hfsplus_partition_candidate(partition: dict[str, Any]) -> bool:
    """Return True for partitions that should be probed for an HFS+ / HFSX volume header.

    GPT/APM/MBR partition type is only a hint. The caller must still verify the
    HFS+ / HFSX volume header before treating the partition as that filesystem.
    """
    detected = str(
        partition.get("DetectedFileSystem")
        or partition.get("FileSystem")
        or ""
    ).strip().casefold()
    if detected.startswith(("apple hfs+", "apple hfsx")):
        return True

    type_name = str(partition.get("PartitionTypeName") or "").strip().casefold()
    if "apple hfs" in type_name or "apple_hfs" in type_name:
        return True

    raw_guid = str(partition.get("PartitionTypeGuid") or "").strip().upper()
    normalized_guid = raw_guid.replace("{", "").replace("}", "")
    if normalized_guid == "48465300-0000-11AA-AA11-00306543ECAC":
        return True

    mbr_type = str(partition.get("MbrType") or "").strip().casefold()
    return mbr_type in {"0xaf", "af"}

def inspect_hfsplus_partition(
    disk_number: int,
    partition_offset: int,
    partition_size: int,
    max_entries: int = 5000,
) -> dict[str, Any]:
    """Open one Windows physical disk read-only and inspect one HFS+ partition."""
    from .disk_layout import _RawDisk

    raw_disk = _RawDisk(int(disk_number))
    if raw_disk.handle is None:
        return {"error": raw_disk.error or "无法打开物理磁盘读取 HFS+ 元数据"}

    try:
        def read_partition(relative_offset: int, length: int) -> bytes:
            if relative_offset < 0 or relative_offset + length > partition_size:
                return b""
            return raw_disk.read(partition_offset + relative_offset, length)

        try:
            report = inspect_hfsplus(read_partition, int(partition_size), max_entries=max_entries)
            report["partition_offset"] = int(partition_offset)
            report["partition_size"] = int(partition_size)
            report["disk_number"] = int(disk_number)
            if raw_disk.error:
                report.setdefault("warnings", []).append(raw_disk.error)
            return report
        except Exception as exc:
            detail = str(exc)
            if raw_disk.error:
                detail += f"；原始磁盘读取提示：{raw_disk.error}"
            return {
                "error": detail,
                "partition_offset": int(partition_offset),
                "partition_size": int(partition_size),
                "disk_number": int(disk_number),
            }
    finally:
        raw_disk.close()
