import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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


if __name__ == "__main__":
    unittest.main()
