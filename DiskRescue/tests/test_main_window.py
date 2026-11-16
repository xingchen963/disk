import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from diskrescue.main_window import MainWindow


_APP = QApplication.instance() or QApplication([])


class MainWindowConstructionTests(unittest.TestCase):
    def test_main_window_constructs_hfs_catalog_tab(self):
        window = MainWindow()
        try:
            self.assertEqual(window.detail_tabs.count(), 2)
            self.assertEqual(window.detail_tabs.tabText(0), "SMART 原始数据")
            self.assertEqual(window.detail_tabs.tabText(1), "HFS+ / HFSX 目录预览")
            self.assertEqual(window.hfs_table.columnCount(), 5)
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
