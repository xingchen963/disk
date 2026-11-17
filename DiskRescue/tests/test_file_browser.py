import tempfile
import unittest
from pathlib import Path

from diskrescue.file_browser import (
    catalog_directory_entries,
    format_creation_time,
    is_path_within_root,
    validate_file_open_target,
    list_windows_directory,
    read_text_preview,
    windows_root_for_partition,
)


class ReadOnlyFileBrowserTests(unittest.TestCase):
    def test_creation_time_uses_birth_time_and_windows_creation_fallback_only(self):
        from datetime import datetime
        from types import SimpleNamespace

        stamp = 1_700_000_000
        expected = datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(format_creation_time(SimpleNamespace(st_birthtime=stamp), windows=False), expected)
        self.assertEqual(format_creation_time(SimpleNamespace(st_ctime=stamp), windows=True), expected)
        self.assertEqual(format_creation_time(SimpleNamespace(st_ctime=stamp), windows=False), "未知")

    def test_validate_file_open_target_restricts_root_and_regular_files(self):
        with tempfile.TemporaryDirectory() as root:
            file_path = Path(root) / "open.txt"
            file_path.write_text("safe", encoding="utf-8")
            self.assertEqual(validate_file_open_target(str(file_path), root), (True, ""))
            outside = Path(root).parent / "outside-file.txt"
            outside.write_text("outside", encoding="utf-8")
            valid, message = validate_file_open_target(str(outside), root)
            self.assertFalse(valid)
            self.assertIn("超出当前分区范围", message)

    def test_lists_directories_before_files_and_returns_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "Docs"
            folder.mkdir()
            file_path = Path(root) / "readme.txt"
            file_path.write_text("hello", encoding="utf-8")

            result = list_windows_directory(root, root)
            self.assertEqual(result["error"], "")
            self.assertEqual([entry["name"] for entry in result["entries"]], ["Docs", "readme.txt"])
            self.assertTrue(result["entries"][0]["is_dir"])
            self.assertTrue(result["entries"][1]["is_file"])
            self.assertEqual(result["entries"][1]["size"], 5)
            self.assertEqual(result["entries"][1]["id"], file_path.stat().st_ino)
            self.assertEqual(result["entries"][1]["id_type"], "文件 ID")
            self.assertIn("created", result["entries"][1])
            self.assertNotEqual(result["entries"][1]["created"], "")

    def test_blocks_paths_outside_selected_partition_root(self):
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "volume"
            outside = Path(parent) / "outside"
            root.mkdir()
            outside.mkdir()
            result = list_windows_directory(str(outside), str(root))
            self.assertIn("超出当前分区范围", result["error"])
            self.assertFalse(is_path_within_root(str(outside), str(root)))

    def test_catalog_listing_returns_only_immediate_children(self):
        catalog = {
            "entries": [
                {"kind": "目录", "name": "Docs", "path": "/Docs", "id": 16, "child_count": 1},
                {"kind": "文件", "name": "README.txt", "path": "/README.txt", "id": 17, "size": 64},
                {"kind": "文件", "name": "note.txt", "path": "/Docs/note.txt", "id": 18, "size": 1234},
                {"kind": "文件", "name": "deep.txt", "path": "/Docs/More/deep.txt", "id": 19, "size": 9},
            ]
        }
        root_entries = catalog_directory_entries(catalog, "/")
        self.assertEqual([item["name"] for item in root_entries], ["Docs", "README.txt"])
        docs_entries = catalog_directory_entries(catalog, "/Docs")
        self.assertEqual([item["name"] for item in docs_entries], ["note.txt"])
        self.assertEqual(root_entries[0]["id_type"], "CNID")
        self.assertEqual(docs_entries[0]["id"], 18)
        self.assertEqual(root_entries[0]["created"], "文件系统未提供")

    def test_text_preview_is_bounded_and_decodes_utf8(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "large.txt"
            path.write_text("A" * 1000, encoding="utf-8")
            preview = read_text_preview(str(path), root, max_bytes=100)
            self.assertEqual(preview["error"], "")
            self.assertEqual(len(preview["text"]), 100)
            self.assertTrue(preview["truncated"])
            self.assertIn("已截断", preview["note"])

    def test_binary_preview_is_limited_to_hex_sample(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "sample.bin"
            path.write_bytes(b"\x00\x01\x02" * 300)
            preview = read_text_preview(str(path), root, max_bytes=128)
            self.assertTrue(preview["binary"])
            self.assertIn("二进制内容", preview["error"])
            self.assertLessEqual(len(preview["text"].split()), 512)

    def test_windows_drive_root_resolution(self):
        self.assertEqual(windows_root_for_partition({"DriveLetter": "D"}), "D:\\")
        self.assertEqual(windows_root_for_partition({"DriveLetter": ""}), "")

    def test_does_not_traverse_symlink_or_junction_inside_selected_root(self):
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "volume"
            outside = Path(parent) / "outside"
            root.mkdir()
            outside.mkdir()
            (outside / "secret.txt").write_text("not inside selected volume", encoding="utf-8")
            link = root / "portal"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("This Windows runner does not allow creating directory symlinks.")

            result = list_windows_directory(str(link), str(root))
            self.assertIn("重解析点", result["error"])
            preview = read_text_preview(str(link / "secret.txt"), str(root))
            self.assertIn("重解析点", preview["error"])



if __name__ == "__main__":
    unittest.main()
