import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from diskrescue.workbench import MainWindow

_APP = QApplication.instance() or QApplication([])


class MainWindowConstructionTests(unittest.TestCase):
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
            self.assertEqual(window.smart_detail_tabs.tabText(0), "S.M.A.R.T. 属性")
            self.assertEqual(window.smart_detail_tabs.tabText(1), "原始数据（JSON）")
        finally:
            window.close()

    def test_partition_and_property_panels_are_vertically_resizable(self):
        window = MainWindow()
        try:
            splitter = window.partition_vertical_splitter
            self.assertEqual(splitter.orientation(), Qt.Orientation.Vertical)
            self.assertEqual(splitter.count(), 2)
            self.assertGreaterEqual(splitter.handleWidth(), 8)

            window.show()
            _APP.processEvents()
            splitter.setSizes([240, 320])
            _APP.processEvents()

            sizes = splitter.sizes()
            self.assertEqual(len(sizes), 2)
            self.assertGreater(sizes[0], 0)
            self.assertGreater(sizes[1], 0)
        finally:
            window.close()

    def test_partition_capacity_summary_and_map_are_populated(self):
        from types import SimpleNamespace

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
            self.assertIn("分区 2 个", window.partition_legend.text())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
