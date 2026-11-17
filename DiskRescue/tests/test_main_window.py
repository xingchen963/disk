import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QTreeWidgetItem

from diskrescue.workbench import MainWindow, USER_ROLE

_APP = QApplication.instance() or QApplication([])


class MainWindowConstructionTests(unittest.TestCase):
    def make_window(self):
        window = MainWindow()
        window.devices = [
            SimpleNamespace(
                model="Disk A", name="PhysicalDrive0", capacity_bytes=1000,
                windows_disk_number=0, protocol="NVMe", bus_type="NVMe",
                smart_supported=False, smart_error="", cached_payload=None
            ),
            SimpleNamespace(
                model="Disk B", name="PhysicalDrive1", capacity_bytes=2000,
                windows_disk_number=1, protocol="NVMe", bus_type="NVMe",
                smart_supported=False, smart_error="", cached_payload=None
            ),
        ]
        window.device_list.blockSignals(True)
        window.device_list.clear()
        window.device_items.clear()
        for index, device in enumerate(window.devices):
            item = QTreeWidgetItem([device.model])
            item.setData(0, USER_ROLE, ("disk", index))
            window.device_list.addTopLevelItem(item)
            window.device_items[index] = item
        window.device_list.blockSignals(False)
        return window

    def test_partition_workbench_constructs(self):
        window = MainWindow()
        try:
            self.assertEqual(window.detail_tabs.count(), 4)
            self.assertEqual(
                [window.detail_tabs.tabText(i) for i in range(window.detail_tabs.count())],
                ["分区参数", "浏览文件", "扇区预览（只读）", "健康信息"],
            )
            self.assertEqual(window.partition_table.columnCount(), 7)
            self.assertEqual(window.details.columnCount(), 2)
            self.assertEqual(window.device_list.columnCount(), 1)
            self.assertEqual(window.smart_attributes_table.columnCount(), 5)
            self.assertEqual(window.smart_detail_tabs.count(), 2)
        finally:
            window.close()

    def test_partition_and_property_panels_are_vertically_resizable(self):
        window = MainWindow()
        try:
            splitter = window.partition_vertical_splitter
            self.assertEqual(splitter.orientation(), Qt.Orientation.Vertical)
            self.assertEqual(splitter.count(), 2)
            self.assertGreaterEqual(splitter.handleWidth(), 8)
        finally:
            window.close()

    def test_partition_capacity_summary_and_map_are_populated(self):
        window = MainWindow()
        try:
            device = SimpleNamespace(capacity_bytes=1_000_000)
            layout = {
                "disk_size": 1_000_000,
                "partitions": [
                    {"Size": 600_000, "SizeUsed": 200_000, "SizeRemaining": 400_000},
                    {"Size": 300_000, "SizeUsed": None, "SizeRemaining": None},
                ],
            }
            window._populate_disk_map(layout, device)
            self.assertEqual(window.partition_map.total, 1_000_000)
            self.assertEqual(len(window.partition_map.parts), 2)
            summary = window.partition_capacity_summary.text()
            self.assertIn("磁盘总容量：1.00 MB", summary)
            self.assertIn("已分配：900.00 KB", summary)
            self.assertIn("未分配：100.00 KB", summary)
        finally:
            window.close()

    def test_single_click_same_disk_uses_cache_without_reading(self):
        window = self.make_window()
        cached = {
            "payload": {"model_name": "Disk B"},
            "layout": {"disk_number": 1, "disk_size": 2000, "partitions": []},
            "smart_error": "",
            "hfsplus_reports": [],
        }
        try:
            window.current_device_index = 1
            window._device_cache[1] = cached
            with patch.object(window, "_display_device_result") as display:
                with patch.object(window, "_start_device_read") as start:
                    window.selection_changed(1)
            display.assert_called_once_with(1, cached)
            start.assert_not_called()
        finally:
            window.close()

    def test_single_click_other_cached_disk_does_not_read_again(self):
        window = self.make_window()
        cached = {
            "payload": {},
            "layout": {"disk_number": 0, "disk_size": 1000, "partitions": []},
            "smart_error": "",
            "hfsplus_reports": [],
        }
        try:
            window.current_device_index = 1
            window._device_cache[0] = cached
            with patch.object(window, "_display_device_result") as display:
                with patch.object(window, "_start_device_read") as start:
                    window.selection_changed(0)
            self.assertEqual(window.current_device_index, 0)
            display.assert_called_once_with(0, cached)
            start.assert_not_called()
        finally:
            window.close()

    def test_double_click_requests_explicit_refresh(self):
        window = self.make_window()
        try:
            with patch.object(window, "_start_device_read") as start:
                window.selection_changed(1, force_refresh=True)
            start.assert_called_once_with(1, force_refresh=True)
        finally:
            window.close()

    def test_switching_disks_keeps_tree_enabled_and_starts_new_disk_read(self):
        window = self.make_window()
        try:
            with patch.object(window, "_start_device_read") as start:
                window.selection_changed(0)
                window.selection_changed(1)
            self.assertTrue(window.device_list.isEnabled())
            self.assertEqual(start.call_count, 2)
            self.assertEqual(start.call_args_list[0].args, (0,))
            self.assertEqual(start.call_args_list[1].args, (1,))
        finally:
            window.close()

    def test_stale_worker_result_is_cached_but_not_displayed(self):
        window = self.make_window()
        worker = Mock()
        worker.isRunning.return_value = False
        layout = {"disk_number": 0, "disk_size": 1000, "partitions": []}
        try:
            window.current_device_index = 1
            window._read_workers[0] = worker
            with patch.object(window, "_display_device_result") as display:
                window._on_device_data_ready(0, worker, {}, layout, "", [])
            self.assertIn(0, window._device_cache)
            display.assert_not_called()
        finally:
            window.close()

    def test_dark_theme_palette_and_major_widgets(self):
        window = MainWindow()
        try:
            self.assertEqual(window.palette().color(QPalette.ColorRole.Window).name(), "#161b22")
            style = window.styleSheet()
            for selector in ("QTreeWidget", "QTableWidget", "QTabBar::tab", "QMenuBar", "QScrollBar", "#smartMetricCard"):
                self.assertIn(selector, style)
            self.assertIn("#171e28", style)
            self.assertIn("#245b8e", style)
        finally:
            window.close()


    def test_file_browser_has_navigation_controls_and_preview(self):
        window = MainWindow()
        try:
            self.assertEqual(window.file_table.columnCount(), 6)
            self.assertTrue(hasattr(window, "file_back_button"))
            self.assertTrue(hasattr(window, "file_up_button"))
            self.assertTrue(hasattr(window, "file_refresh_button"))
            self.assertTrue(hasattr(window, "file_path_edit"))
            self.assertTrue(window.file_preview.isReadOnly())
            self.assertEqual(window.file_vertical_splitter.count(), 2)
        finally:
            window.close()

    def test_hfs_catalog_browser_navigates_into_subfolders(self):
        window = MainWindow()
        report = {
            "filesystem": "HFS+",
            "catalog": {
                "volume_name": "Kingston",
                "entries_returned": 3,
                "entries": [
                    {"kind": "目录", "name": "Docs", "id": 16, "parent_id": 2,
                     "path": "/Docs", "size": None, "child_count": 1},
                    {"kind": "文件", "name": "README.txt", "id": 17, "parent_id": 2,
                     "path": "/README.txt", "size": 64, "child_count": None},
                    {"kind": "文件", "name": "note.txt", "id": 18, "parent_id": 16,
                     "path": "/Docs/note.txt", "size": 1234, "child_count": None},
                ],
            },
        }
        partition = {"DriveLetter": "", "AccessPaths": [], "Offset": 2048}
        try:
            window._configure_file_browser(partition, report)
            self.assertEqual(window._browser_kind, "hfs")
            self.assertEqual(window._browser_path, "/")
            self.assertEqual(window.file_table.rowCount(), 2)
            first = window.file_table.item(0, 1)
            self.assertEqual(first.text(), "Docs")

            window._browser_item_activated(0, 1)
            self.assertEqual(window._browser_path, "/Docs")
            self.assertEqual(window.file_table.rowCount(), 1)
            self.assertEqual(window.file_table.item(0, 1).text(), "note.txt")

            window._browser_item_activated(0, 1)
            self.assertIn("尚未实现普通文件 Data Fork", window.file_preview.toPlainText())
        finally:
            window.close()



if __name__ == "__main__":
    unittest.main()
