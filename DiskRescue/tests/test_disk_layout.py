import struct
import unittest
import uuid

from diskrescue.disk_layout import _detect_filesystem, _parse_apm, _parse_gpt, _parse_mbr


class MemoryDisk:
    def __init__(self, size):
        self.data = bytearray(size)
        self.error = ""

    def read(self, offset, length):
        if offset < 0 or offset >= len(self.data):
            return b""
        return bytes(self.data[offset:offset + length])


class PartitionTableTests(unittest.TestCase):
    def test_gpt_linux_partition_entry(self):
        disk = MemoryDisk(512 * 4096)
        header = memoryview(disk.data)[512:1024]
        header[:8] = b"EFI PART"
        struct.pack_into("<Q", header, 72, 2)
        struct.pack_into("<I", header, 80, 128)
        struct.pack_into("<I", header, 84, 128)

        entry_offset = 2 * 512
        entry = memoryview(disk.data)[entry_offset:entry_offset + 128]
        entry[:16] = uuid.UUID("0FC63DAF-8483-4772-8E79-3D69D8477DE4").bytes_le
        entry[16:32] = uuid.uuid4().bytes_le
        struct.pack_into("<Q", entry, 32, 2048)
        struct.pack_into("<Q", entry, 40, 4095)
        entry[56:56 + len("Linux root".encode("utf-16le"))] = "Linux root".encode("utf-16le")

        parsed = _parse_gpt(disk, 512)
        self.assertIsNotNone(parsed)
        partitions, style = parsed
        self.assertEqual(style, "GPT")
        self.assertEqual(len(partitions), 1)
        self.assertEqual(partitions[0]["PartitionTypeName"], "Linux 文件系统分区")
        self.assertEqual(partitions[0]["Offset"], 2048 * 512)
        self.assertEqual(partitions[0]["PartitionName"], "Linux root")

    def test_mbr_partition_entry(self):
        disk = MemoryDisk(512 * 8192)
        first = memoryview(disk.data)[:512]
        first[510:512] = b"\x55\xAA"
        entry = first[446:462]
        entry[4] = 0x07
        struct.pack_into("<II", entry, 8, 2048, 4096)

        parsed = _parse_mbr(disk, 512, bytes(first))
        self.assertIsNotNone(parsed)
        partitions, style = parsed
        self.assertEqual(style, "MBR")
        self.assertEqual(len(partitions), 1)
        self.assertIn("NTFS", partitions[0]["PartitionTypeName"])
        self.assertEqual(partitions[0]["Offset"], 2048 * 512)

    def test_apple_partition_map(self):
        disk = MemoryDisk(512 * 10000)
        first = memoryview(disk.data)[:512]
        first[:2] = b"ER"
        struct.pack_into(">H", first, 2, 512)
        struct.pack_into(">I", first, 4, 10000)

        entry = memoryview(disk.data)[512:1024]
        entry[:2] = b"PM"
        struct.pack_into(">I", entry, 4, 1)
        struct.pack_into(">II", entry, 8, 64, 512)
        name = b"Macintosh"
        part_type = b"Apple_HFS"
        entry[16:16 + len(name)] = name
        entry[48:48 + len(part_type)] = part_type

        parsed = _parse_apm(disk, bytes(first))
        self.assertIsNotNone(parsed)
        partitions, style = parsed
        self.assertEqual(style, "Apple Partition Map（APM）")
        self.assertEqual(len(partitions), 1)
        self.assertEqual(partitions[0]["PartitionName"], "Macintosh")
        self.assertEqual(partitions[0]["Offset"], 64 * 512)


class FileSystemSignatureTests(unittest.TestCase):
    def test_ntfs_signature(self):
        data = bytearray(4096)
        data[3:11] = b"NTFS    "
        self.assertEqual(_detect_filesystem(bytes(data))[0], "NTFS")

    def test_ext4_signature(self):
        data = bytearray(4096)
        struct.pack_into("<H", data, 1080, 0xEF53)
        struct.pack_into("<I", data, 1100, 1)
        struct.pack_into("<I", data, 1120, 0x40)
        self.assertEqual(_detect_filesystem(bytes(data))[0], "ext4")

    def test_xfs_signature(self):
        data = bytearray(4096)
        data[:4] = b"XFSB"
        self.assertEqual(_detect_filesystem(bytes(data))[0], "XFS")

    def test_btrfs_signature(self):
        data = bytearray(131072)
        data[0x10040:0x10048] = b"_BHRfS_M"
        self.assertEqual(_detect_filesystem(bytes(data))[0], "Btrfs")

    def test_luks_signature(self):
        data = bytearray(4096)
        data[:6] = b"LUKS\xBA\xBE"
        self.assertEqual(_detect_filesystem(bytes(data))[0], "LUKS 加密容器")

    def test_apfs_container_signature(self):
        data = bytearray(4096)
        data[32:36] = b"NXSB"
        self.assertEqual(_detect_filesystem(bytes(data))[0], "Apple APFS")

    def test_hfs_plus_volume_signature(self):
        data = bytearray(4096)
        data[1024:1026] = b"H+"
        self.assertEqual(_detect_filesystem(bytes(data))[0], "Apple HFS+")


if __name__ == "__main__":
    unittest.main()
