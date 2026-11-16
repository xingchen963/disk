import struct
import unittest

from diskrescue.hfsplus import inspect_hfsplus


class MemoryVolume:
    def __init__(self, size):
        self.data = bytearray(size)

    def read(self, offset, length):
        if offset < 0 or offset >= len(self.data):
            return b""
        return bytes(self.data[offset:offset + length])

    def write(self, offset, data):
        self.data[offset:offset + len(data)] = data


def make_catalog_record(parent_id, name, record_type, cnid, size=0, child_count=0):
    name_bytes = name.encode("utf-16-be")
    key_data = struct.pack(">IH", parent_id, len(name)) + name_bytes
    record = bytearray(struct.pack(">H", len(key_data)) + key_data)
    if len(record) & 1:
        record.append(0)

    if record_type == 1:
        data = struct.pack(">HHII", 1, 0, child_count, cnid) + bytes(76)
    elif record_type == 2:
        data = struct.pack(">HHII", 2, 0, 0, cnid) + bytes(76) + struct.pack(">Q", size)
    else:
        raise ValueError("unsupported fixture catalog record type")

    record.extend(data)
    if len(data) & 1:
        record.append(0)
    return bytes(record)


def build_hfsplus_volume():
    block_size = 512
    total_blocks = 80
    volume = MemoryVolume(block_size * total_blocks)

    header = bytearray(512)
    header[:2] = b"H+"
    struct.pack_into(">H", header, 2, 4)
    struct.pack_into(">I", header, 4, 1 << 8)  # cleanly unmounted
    struct.pack_into(">I", header, 32, 1)      # fileCount
    struct.pack_into(">I", header, 36, 1)      # folderCount, excluding root
    struct.pack_into(">I", header, 40, block_size)
    struct.pack_into(">I", header, 44, total_blocks)
    struct.pack_into(">I", header, 48, 40)     # freeBlocks

    # Allocation File: one extent at allocation block 2, 10-byte bitmap.
    struct.pack_into(">QII", header, 112, 10, 0, 1)
    struct.pack_into(">II", header, 128, 4, 1)

    # Catalog File: two 4 KB B-tree nodes at allocation block 10.
    struct.pack_into(">QII", header, 272, 8192, 0, 16)
    struct.pack_into(">II", header, 288, 10, 16)
    volume.write(1024, header)

    # Allocation bitmap: first 40 blocks allocated, remaining 40 free.
    volume.write(4 * block_size, b"\xFF" * 5 + b"\x00" * 5)

    # Catalog B-tree header node.
    node_size = 4096
    header_node = bytearray(node_size)
    header_node[8] = 1  # Header Node kind
    struct.pack_into(">H", header_node, 10, 3)
    struct.pack_into(">H", header_node, 14, 1)       # treeDepth
    struct.pack_into(">I", header_node, 16, 1)       # rootNode
    struct.pack_into(">I", header_node, 20, 3)       # leafRecords
    struct.pack_into(">I", header_node, 24, 1)       # firstLeafNode
    struct.pack_into(">I", header_node, 28, 1)       # lastLeafNode
    struct.pack_into(">H", header_node, 32, node_size)
    struct.pack_into(">H", header_node, 34, 516)     # maxKeyLength
    struct.pack_into(">I", header_node, 36, 2)       # totalNodes
    struct.pack_into(">I", header_node, 40, 0)       # freeNodes
    header_node[50] = 0                              # btreeType
    header_node[51] = 0xCF                           # case-folding compare
    struct.pack_into(">I", header_node, 52, 0x06)    # BigKeys + variable index keys

    records = [
        make_catalog_record(1, "Kingston", 1, 2, child_count=1),
        make_catalog_record(2, "Docs", 1, 16, child_count=1),
        make_catalog_record(16, "note.txt", 2, 17, size=1234),
    ]

    # Catalog B-tree first leaf node.
    leaf = bytearray(node_size)
    leaf[8] = 0xFF  # Leaf Node kind
    leaf[9] = 1
    struct.pack_into(">H", leaf, 10, len(records))

    offsets = []
    cursor = 14
    for record in records:
        offsets.append(cursor)
        leaf[cursor:cursor + len(record)] = record
        cursor += len(record)
    offsets.append(cursor)

    # Offset table is stored in reverse order: first record offset is last.
    for index, value in enumerate(offsets):
        struct.pack_into(">H", leaf, node_size - 2 * (index + 1), value)

    catalog_start = 10 * block_size
    volume.write(catalog_start, header_node)
    volume.write(catalog_start + node_size, leaf)
    return volume


class HFSPlusInspectionTests(unittest.TestCase):
    def test_reads_volume_header_allocation_bitmap_and_catalog(self):
        volume = build_hfsplus_volume()
        report = inspect_hfsplus(volume.read, len(volume.data))
        self.assertNotIn("error", report, msg=report.get("error", "unknown HFS+ parser error"))

        self.assertEqual(report["filesystem"], "HFS+")
        self.assertEqual(report["version"], 4)
        self.assertEqual(report["block_size"], 512)
        self.assertEqual(report["total_blocks"], 80)
        self.assertEqual(report["free_blocks"], 40)
        self.assertTrue(report["clean_unmount"])
        self.assertTrue(report["allocation"]["bitmap_read"])
        self.assertEqual(report["allocation"]["free_blocks_bitmap"], 40)
        self.assertTrue(report["allocation"]["matches_header"])

        catalog = report["catalog"]
        self.assertEqual(catalog["volume_name"], "Kingston")
        self.assertEqual(catalog["files_returned"], 1)
        self.assertEqual(catalog["folders_returned"], 1)

        by_name = {entry["name"]: entry for entry in catalog["entries"]}
        self.assertEqual(by_name["Docs"]["path"], "/Docs")
        self.assertEqual(by_name["note.txt"]["path"], "/Docs/note.txt")
        self.assertEqual(by_name["note.txt"]["size"], 1234)

    def test_rejects_non_hfs_volume(self):
        volume = MemoryVolume(4096)
        report = inspect_hfsplus(volume.read, len(volume.data))
        self.assertIn("error", report)


if __name__ == "__main__":
    unittest.main()
