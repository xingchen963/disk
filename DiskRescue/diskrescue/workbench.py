"""DiskRescue partition-centric workbench UI.

The layout follows a traditional disk utility workflow:
physical disk/partition tree on the left; disk map and tabbed inspection
workspace on the right. All disk operations remain read-only.
"""

import json
from typing import Any

from PySide6.QtCore import Qt, QTimer, QThread, Signal
from PySide6.QtGui import QAction, QColor, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .main_window import DeviceReadWorker, DiscoveryWorker, PartitionMap, human_bytes, percent
from .smartctl import Device
from .smart_report import build_smart_report
from .file_browser import (
    catalog_directory_entries,
    list_windows_directory,
    read_text_preview,
    windows_root_for_partition,
)


USER_ROLE = Qt.ItemDataRole.UserRole


class DirectoryReadWorker(QThread):
    completed = Signal(int, object)

    def __init__(self, token: int, path: str, root_path: str, parent=None):
        super().__init__(parent)
        self.token = token
        self.path = path
        self.root_path = root_path

    def run(self):
        result = list_windows_directory(self.path, self.root_path)
        self.completed.emit(self.token, result)


class MainWindow(QMainWindow):
    """Partition-oriented, read-only DiskRescue workbench."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("DiskRescue — 硬盘检测与数据救援 | 工作台 R11")
        self.resize(1440, 920)
        self.setMinimumSize(1100, 720)

        self.devices: list[Device] = []
        self.device_items: dict[int, QTreeWidgetItem] = {}
        self.current_device_index: int | None = None
        self.current_partition_index: int | None = None
        self.current_payload: dict[str, Any] | None = None
        self.current_layout: dict[str, Any] | None = None
        self.hfsplus_reports: list[dict[str, Any]] = []
        # Per-disk workers and result cache: switching disks never repeats a
        # completed SMART/partition/filesystem read.
        self._device_cache: dict[int, dict[str, Any]] = {}
        self._read_workers: dict[int, DeviceReadWorker] = {}
        self._read_errors: dict[int, str] = {}
        self._discovery_thread = None
        self._building_tree = False
        self._available_hfs_reports: list[dict[str, Any]] = []
        self._browser_kind = "none"
        self._browser_root = ""
        self._browser_path = ""
        self._browser_history: list[str] = []
        self._browser_report: dict[str, Any] | None = None
        self._browser_generation = 0
        self._browser_workers: dict[int, DirectoryReadWorker] = {}

        self._build_menus_and_toolbar()
        self._build_workbench()
        self._apply_workbench_style()
        self.statusBar().showMessage("安全模式 · 仅执行磁盘与文件系统只读检查")
        QTimer.singleShot(0, self.scan)

    def _build_menus_and_toolbar(self):
        file_menu = self.menuBar().addMenu("文件(&F)")
        file_menu.addAction("退出", self.close)

        disk_menu = self.menuBar().addMenu("磁盘(&D)")
        disk_menu.addAction("扫描磁盘", self.scan)

        view_menu = self.menuBar().addMenu("查看(&V)")
        view_menu.addAction("分区参数", lambda: self.detail_tabs.setCurrentIndex(0))
        view_menu.addAction("浏览文件", lambda: self.detail_tabs.setCurrentIndex(1))
        view_menu.addAction("只读扇区预览", lambda: self.detail_tabs.setCurrentIndex(2))
        view_menu.addAction("健康信息", lambda: self.detail_tabs.setCurrentIndex(3))

        help_menu = self.menuBar().addMenu("帮助(&H)")
        help_menu.addAction("安全模式说明", self._show_safety_help)

        self.toolbar = QToolBar("常用操作", self)
        self.toolbar.setMovable(False)
        self.addToolBar(self.toolbar)
        self.scan_action = QAction("扫描磁盘", self)
        self.scan_action.triggered.connect(self.scan)
        self.toolbar.addAction(self.scan_action)
        self.toolbar.addSeparator()
        self.toolbar.addWidget(QLabel("  只读模式：不会向源磁盘写入数据  "))

    def _build_workbench(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(6, 5, 6, 5)
        root.setSpacing(5)

        self.banner = QLabel(
            "安全模式：磁盘身份、分区表、文件系统和目录信息均以只读方式检查。"
            "扇区预览不提供写入、格式化或修复操作。"
        )
        self.banner.setWordWrap(True)
        self.banner.setObjectName("safetyBanner")
        root.addWidget(self.banner)

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(3)
        left_title = QLabel("物理磁盘 / 分区")
        left_title.setObjectName("panelTitle")
        left_layout.addWidget(left_title)

        self.device_list = QTreeWidget()
        self.device_list.setColumnCount(1)
        self.device_list.setHeaderLabels(["磁盘与分区"])
        self.device_list.setMinimumWidth(255)
        self.device_list.setUniformRowHeights(True)
        self.device_list.setIndentation(18)
        self.device_list.currentItemChanged.connect(self._tree_selection_changed)
        self.device_list.itemDoubleClicked.connect(self._tree_item_double_clicked)
        left_layout.addWidget(self.device_list)
        left_panel.setMinimumWidth(255)
        left_panel.setMaximumWidth(360)
        self.main_splitter.addWidget(left_panel)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(5, 0, 0, 0)
        right_layout.setSpacing(4)

        self.disk_summary = QLabel("正在自动识别磁盘……")
        self.disk_summary.setObjectName("diskSummary")
        self.disk_summary.setWordWrap(True)
        right_layout.addWidget(self.disk_summary)

        self.partition_capacity_summary = QLabel("磁盘总容量：等待读取")
        self.partition_capacity_summary.setObjectName("subtleText")
        self.partition_capacity_summary.setWordWrap(True)
        right_layout.addWidget(self.partition_capacity_summary)

        self.partition_map = PartitionMap()
        self.partition_map.setMinimumHeight(122)
        self.partition_map.setMaximumHeight(145)
        right_layout.addWidget(self.partition_map)

        self.partition_legend = QLabel("选择左侧磁盘查看分区；选择分区查看详细参数。")
        self.partition_legend.setWordWrap(True)
        self.partition_legend.setObjectName("subtleText")
        right_layout.addWidget(self.partition_legend)

        self.detail_tabs = QTabWidget()
        self.detail_tabs.setDocumentMode(True)
        self._build_partition_tab()
        self._build_file_tab()
        self._build_sector_tab()
        self._build_smart_tab()
        right_layout.addWidget(self.detail_tabs, 1)

        self.main_splitter.addWidget(right_panel)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([285, 1100])
        root.addWidget(self.main_splitter, 1)

        self.setCentralWidget(central)

    def _build_partition_tab(self):
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(5, 7, 5, 5)
        page_layout.setSpacing(0)

        # The blank area previously belonged to the partition table's viewport.
        # Put the table and property panel in a real vertical splitter so users
        # can choose how much height each workspace receives.
        self.partition_vertical_splitter = QSplitter(Qt.Orientation.Vertical, page)
        self.partition_vertical_splitter.setObjectName("partitionVerticalSplitter")
        self.partition_vertical_splitter.setChildrenCollapsible(False)
        self.partition_vertical_splitter.setHandleWidth(10)
        self.partition_vertical_splitter.setOpaqueResize(True)

        partition_panel = QWidget()
        partition_layout = QVBoxLayout(partition_panel)
        partition_layout.setContentsMargins(0, 0, 0, 4)
        partition_layout.setSpacing(3)

        partition_title = QLabel("分区列表")
        partition_title.setObjectName("panelTitle")
        partition_layout.addWidget(partition_title)

        self.partition_table = QTableWidget(0, 7)
        self.partition_table.setHorizontalHeaderLabels([
            "卷标 / 分区类型",
            "文件系统",
            "起始 LBA",
            "容量",
            "已用",
            "可用",
            "属性",
        ])
        self.partition_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.partition_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.partition_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.partition_table.setAlternatingRowColors(True)
        self.partition_table.horizontalHeader().setStretchLastSection(True)
        for col, width in enumerate((235, 145, 120, 130, 130, 130)):
            self.partition_table.setColumnWidth(col, width)
        self.partition_table.currentCellChanged.connect(self._partition_table_row_changed)
        partition_layout.addWidget(self.partition_table, 1)
        partition_panel.setMinimumHeight(125)

        property_panel = QWidget()
        property_layout = QVBoxLayout(property_panel)
        property_layout.setContentsMargins(0, 4, 0, 0)
        property_layout.setSpacing(3)

        property_header = QHBoxLayout()
        self.property_title = QLabel("分区参数")
        self.property_title.setObjectName("panelTitle")
        property_header.addWidget(self.property_title)
        property_header.addStretch(1)
        self.property_hint = QLabel("选择上方分区查看属性")
        self.property_hint.setObjectName("subtleText")
        property_header.addWidget(self.property_hint)
        property_layout.addLayout(property_header)

        self.details = QTableWidget(0, 2)
        self.details.setHorizontalHeaderLabels(["属性", "读取结果"])
        self.details.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.details.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.details.setAlternatingRowColors(True)
        self.details.horizontalHeader().setStretchLastSection(True)
        self.details.setColumnWidth(0, 210)
        property_layout.addWidget(self.details, 1)
        property_panel.setMinimumHeight(150)

        self.partition_vertical_splitter.addWidget(partition_panel)
        self.partition_vertical_splitter.addWidget(property_panel)
        self.partition_vertical_splitter.setStretchFactor(0, 3)
        self.partition_vertical_splitter.setStretchFactor(1, 2)
        self.partition_vertical_splitter.setSizes([290, 280])
        page_layout.addWidget(self.partition_vertical_splitter, 1)

        self.detail_tabs.addTab(page, "分区参数")

    def _build_file_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(5, 7, 5, 5)
        layout.setSpacing(5)

        self.file_summary = QLabel(
            "先在左侧选择一个分区。已挂载卷可逐层浏览目录；HFS+ / HFSX 显示只读目录元数据。"
        )
        self.file_summary.setWordWrap(True)
        self.file_summary.setObjectName("subtleText")
        layout.addWidget(self.file_summary)

        navigation = QHBoxLayout()
        navigation.setSpacing(5)
        self.file_back_button = QPushButton("返回")
        self.file_back_button.clicked.connect(self._browser_back)
        self.file_up_button = QPushButton("上级")
        self.file_up_button.clicked.connect(self._browser_up)
        self.file_refresh_button = QPushButton("刷新")
        self.file_refresh_button.clicked.connect(self._browser_refresh)
        self.file_path_edit = QLineEdit()
        self.file_path_edit.setPlaceholderText("目录路径")
        self.file_path_edit.returnPressed.connect(self._browser_go_to_path)
        self.file_go_button = QPushButton("转到")
        self.file_go_button.clicked.connect(self._browser_go_to_path)
        navigation.addWidget(self.file_back_button)
        navigation.addWidget(self.file_up_button)
        navigation.addWidget(self.file_refresh_button)
        navigation.addWidget(self.file_path_edit, 1)
        navigation.addWidget(self.file_go_button)
        layout.addLayout(navigation)

        self.file_table = QTableWidget(0, 6)
        self.file_table.setHorizontalHeaderLabels([
            "类型", "名称", "大小 / 子项", "修改时间", "属性", "ID / CNID"
        ])
        self.file_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.file_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.file_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.file_table.setAlternatingRowColors(True)
        self.file_table.horizontalHeader().setStretchLastSection(True)
        for col, width in enumerate((100, 260, 145, 165, 180)):
            self.file_table.setColumnWidth(col, width)
        self.file_table.cellDoubleClicked.connect(self._browser_item_activated)
        self.file_table.currentCellChanged.connect(self._browser_item_selected)

        self.file_vertical_splitter = QSplitter(Qt.Orientation.Vertical, page)
        self.file_vertical_splitter.setObjectName("fileBrowserSplitter")
        self.file_vertical_splitter.setChildrenCollapsible(False)
        self.file_vertical_splitter.setHandleWidth(8)
        self.file_vertical_splitter.addWidget(self.file_table)

        preview_panel = QWidget()
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(0, 4, 0, 0)
        preview_label = QLabel("文件信息 / 文本预览（只读）")
        preview_label.setObjectName("panelTitle")
        preview_layout.addWidget(preview_label)
        self.file_preview = QPlainTextEdit()
        self.file_preview.setReadOnly(True)
        self.file_preview.setPlaceholderText(
            "单击文件查看属性；双击文件夹进入目录；双击普通文件尝试读取有限长度的文本预览。"
        )
        preview_layout.addWidget(self.file_preview, 1)
        self.file_vertical_splitter.addWidget(preview_panel)
        self.file_vertical_splitter.setStretchFactor(0, 3)
        self.file_vertical_splitter.setStretchFactor(1, 2)
        self.file_vertical_splitter.setSizes([390, 180])
        layout.addWidget(self.file_vertical_splitter, 1)

        self.file_back_button.setEnabled(False)
        self.file_up_button.setEnabled(False)
        self.file_refresh_button.setEnabled(False)
        self.file_go_button.setEnabled(False)
        self.file_path_edit.setEnabled(False)
        self.detail_tabs.addTab(page, "浏览文件")

    def _build_sector_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        note = QLabel(
            "只读扇区预览工作区\n\n"
            "当前版本已读取分区表和文件系统元数据，但尚未开放通用 LBA 十六进制浏览。\n"
            "后续会提供按扇区 / 偏移读取、十六进制与 ASCII 对照显示。\n"
            "本工作区不会提供扇区写入、覆盖或编辑功能。"
        )
        note.setWordWrap(True)
        note.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        note.setObjectName("subtleText")
        layout.addWidget(note)
        self.sector_view = QPlainTextEdit()
        self.sector_view.setReadOnly(True)
        self.sector_view.setPlaceholderText("只读扇区预览尚未开放。")
        self.sector_view.setPlainText("安全策略：禁止直接向源磁盘写入扇区。")
        layout.addWidget(self.sector_view, 1)
        self.detail_tabs.addTab(page, "扇区预览（只读）")

    def _build_smart_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(9, 8, 9, 8)
        layout.setSpacing(7)

        self.smart_model = QLabel("尚未读取 SMART 设备")
        self.smart_model.setObjectName("smartModel")
        self.smart_model.setWordWrap(True)
        layout.addWidget(self.smart_model)

        identity = QHBoxLayout()
        self.smart_serial = QLabel("序列号：设备未提供")
        self.smart_firmware = QLabel("固件：设备未提供")
        self.smart_serial.setObjectName("smartIdentity")
        self.smart_firmware.setObjectName("smartIdentity")
        identity.addWidget(self.smart_serial)
        identity.addSpacing(35)
        identity.addWidget(self.smart_firmware)
        identity.addStretch(1)
        layout.addLayout(identity)

        top = QHBoxLayout()
        top.setSpacing(9)

        health_panel = QFrame()
        health_panel.setObjectName("smartHealthPanel")
        health_panel.setMinimumWidth(175)
        health_layout = QVBoxLayout(health_panel)
        health_layout.setContentsMargins(12, 10, 12, 10)
        health_label = QLabel("健康状态")
        health_label.setObjectName("smartSmallLabel")
        self.smart_health_value = QLabel("设备未提供")
        self.smart_health_value.setObjectName("smartHealthValue")
        self.smart_health_detail = QLabel("尚未读取")
        self.smart_health_detail.setWordWrap(True)
        self.smart_health_detail.setObjectName("smartSmallLabel")
        temp_label = QLabel("温度")
        temp_label.setObjectName("smartSmallLabel")
        self.smart_temperature_value = QLabel("设备未提供")
        self.smart_temperature_value.setObjectName("smartTemperatureValue")
        health_layout.addWidget(health_label)
        health_layout.addWidget(self.smart_health_value)
        health_layout.addWidget(self.smart_health_detail)
        health_layout.addSpacing(10)
        health_layout.addWidget(temp_label)
        health_layout.addWidget(self.smart_temperature_value)
        health_layout.addStretch(1)
        top.addWidget(health_panel, 1)

        metric_panel = QFrame()
        metric_panel.setObjectName("smartMetricPanel")
        metric_grid = QGridLayout(metric_panel)
        metric_grid.setContentsMargins(8, 6, 8, 6)
        metric_grid.setHorizontalSpacing(12)
        metric_grid.setVerticalSpacing(8)
        self.smart_metric_values = {}
        metric_names = [
            "预计剩余寿命", "可用备用空间", "主机读取量总计", "主机写入量总计",
            "通电时间", "通电次数", "传输模式", "标准", "支持的功能",
        ]
        for index, name in enumerate(metric_names):
            card = QFrame()
            card.setObjectName("smartMetricCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(8, 5, 8, 5)
            caption = QLabel(name)
            caption.setObjectName("smartSmallLabel")
            value = QLabel("设备未提供")
            value.setObjectName("smartMetricValue")
            value.setWordWrap(True)
            card_layout.addWidget(caption)
            card_layout.addWidget(value)
            self.smart_metric_values[name] = value
            metric_grid.addWidget(card, index // 3, index % 3)
        top.addWidget(metric_panel, 4)
        layout.addLayout(top, 2)

        self.smart_attribute_summary = QLabel("SMART 属性读取结果")
        self.smart_attribute_summary.setObjectName("panelTitle")
        layout.addWidget(self.smart_attribute_summary)

        self.smart_detail_tabs = QTabWidget()
        self.smart_detail_tabs.setDocumentMode(True)

        attribute_page = QWidget()
        attribute_layout = QVBoxLayout(attribute_page)
        attribute_layout.setContentsMargins(2, 2, 2, 2)
        self.smart_attributes_table = QTableWidget(0, 5)
        self.smart_attributes_table.setHorizontalHeaderLabels([
            "状态", "ID", "属性名称", "原始数据（十六进制）", "原始数据（十进制）"
        ])
        self.smart_attributes_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.smart_attributes_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.smart_attributes_table.setAlternatingRowColors(True)
        self.smart_attributes_table.horizontalHeader().setStretchLastSection(True)
        for col, width in enumerate((85, 55, 260, 230, 190)):
            self.smart_attributes_table.setColumnWidth(col, width)
        attribute_layout.addWidget(self.smart_attributes_table)
        self.smart_detail_tabs.addTab(attribute_page, "S.M.A.R.T. 属性")

        raw_page = QWidget()
        raw_layout = QVBoxLayout(raw_page)
        raw_layout.setContentsMargins(2, 2, 2, 2)
        self.raw = QPlainTextEdit()
        self.raw.setReadOnly(True)
        self.raw.setPlaceholderText("原始 SMART JSON")
        raw_layout.addWidget(self.raw)
        self.smart_detail_tabs.addTab(raw_page, "原始数据（JSON）")
        layout.addWidget(self.smart_detail_tabs, 5)

        self.smart_summary = QLabel("SMART 数据尚未读取。")
        self.smart_summary.setWordWrap(True)
        self.smart_summary.setObjectName("subtleText")
        layout.addWidget(self.smart_summary)

        self.detail_tabs.addTab(page, "健康信息")

    def _apply_workbench_style(self):
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#161b22"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#e6edf3"))
        palette.setColor(QPalette.ColorRole.Base, QColor("#0f141b"))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#1d2632"))
        palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#222c38"))
        palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#f0f4f8"))
        palette.setColor(QPalette.ColorRole.Text, QColor("#e6edf3"))
        palette.setColor(QPalette.ColorRole.Button, QColor("#263241"))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor("#e6edf3"))
        palette.setColor(QPalette.ColorRole.Highlight, QColor("#245b8e"))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
        self.setPalette(palette)

        self.setStyleSheet("""
            QWidget, QMainWindow { background: #161b22; color: #e6edf3; }
            QMenuBar, QToolBar { background: #202833; color: #e6edf3; border-bottom: 1px solid #394656; padding: 4px; }
            QMenuBar::item { padding: 6px 10px; }
            QMenuBar::item:selected, QMenu::item:selected { background: #245b8e; color: #ffffff; }
            QMenu { background: #202833; color: #e6edf3; border: 1px solid #465365; }
            QMenu::item { padding: 6px 22px; }
            QToolButton, QPushButton { background: #2a3543; color: #e6edf3; border: 1px solid #465365; border-radius: 4px; padding: 5px 10px; }
            QToolButton:hover, QPushButton:hover { background: #35465a; border-color: #79b8ff; }
            QToolButton:pressed, QPushButton:pressed { background: #1d4d78; }
            QToolButton:disabled, QPushButton:disabled { color: #748294; background: #202833; }
            #safetyBanner { background: #172d43; color: #c8e1ff; border: 1px solid #31577b; padding: 7px 10px; }
            #diskSummary { background: #202a36; color: #e6edf3; border: 1px solid #3b4858; padding: 8px 10px; font-weight: 600; }
            #panelTitle { color: #d8e5f3; font-weight: 600; padding: 4px 1px; }
            #smartModel { color: #79b8ff; font-size: 29px; font-weight: 500; padding: 1px 4px; }
            #smartIdentity { color: #cbd5e1; font-size: 16px; padding: 0 8px 3px 8px; }
            #smartHealthPanel, #smartMetricPanel { background: #202a36; border: 1px solid #3b4858; border-radius: 7px; }
            #smartMetricCard { background: #263241; border: 1px solid #405064; border-radius: 5px; }
            #smartSmallLabel, #subtleText { color: #9eafc2; }
            #smartHealthValue { color: #56d364; font-size: 27px; font-weight: 700; }
            #smartTemperatureValue { color: #7ee787; font-size: 25px; font-weight: 600; }
            #smartMetricValue { color: #e6edf3; font-size: 16px; font-weight: 600; }
            QTreeWidget { background: #1b232e; color: #e6edf3; border: 1px solid #394656; alternate-background-color: #202a36; outline: 0; }
            QTreeWidget::item { padding: 4px 3px; }
            QTreeWidget::item:hover { background: #2a3b4f; }
            QTreeWidget::item:selected { background: #245b8e; color: #ffffff; }
            QTableWidget, QTableView { background: #171e28; color: #e6edf3; alternate-background-color: #1d2632; gridline-color: #354252; border: 1px solid #3b4858; selection-background-color: #245b8e; selection-color: #ffffff; outline: 0; }
            QTableWidget::item, QTableView::item { padding: 3px; }
            QTableWidget::item:selected, QTableView::item:selected { background: #245b8e; color: #ffffff; }
            QHeaderView::section { background: #263241; color: #dce6f1; padding: 6px; border: 0; border-right: 1px solid #3a4859; border-bottom: 1px solid #455468; }
            QTabWidget::pane { background: #171e28; border: 1px solid #394656; }
            QTabBar::tab { background: #222c38; color: #b9c7d6; padding: 8px 16px; border: 1px solid #3b4858; }
            QTabBar::tab:hover { background: #2b3949; color: #ffffff; }
            QTabBar::tab:selected { background: #171e28; color: #79b8ff; font-weight: 600; }
            QPlainTextEdit, QTextEdit, QLineEdit { background: #0f141b; color: #dce6f1; border: 1px solid #3b4858; selection-background-color: #245b8e; }
            QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit { background: #202a36; color: #e6edf3; border: 1px solid #465365; padding: 5px 7px; }
            QCheckBox, QRadioButton { color: #dce6f1; }
            QProgressBar { background: #222d3a; color: #e6edf3; border: 1px solid #465365; border-radius: 4px; text-align: center; }
            QProgressBar::chunk { background: #3478c8; }
            QSplitter#partitionVerticalSplitter::handle:vertical { background: #394656; border-top: 1px solid #566579; border-bottom: 1px solid #566579; margin: 1px 0; }
            QSplitter#partitionVerticalSplitter::handle:vertical:hover { background: #3478c8; }
            QScrollBar:vertical { background: #171e28; width: 12px; margin: 0; }
            QScrollBar:horizontal { background: #171e28; height: 12px; margin: 0; }
            QScrollBar::handle:vertical, QScrollBar::handle:horizontal { background: #48586b; border-radius: 5px; min-height: 24px; min-width: 24px; }
            QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover { background: #627d9d; }
            QScrollBar::add-line, QScrollBar::sub-line { background: #202a36; border: 0; width: 0; height: 0; }
            QToolTip { background: #202a36; color: #f0f4f8; border: 1px solid #607086; padding: 4px; }
            QStatusBar { background: #202833; color: #aebdce; border-top: 1px solid #354151; }
        """)

    def _show_safety_help(self):
        self.statusBar().showMessage(
            "安全模式：只读枚举、分区解析和文件系统元数据检查；不执行格式化、修复或写盘操作。",
            8000,
        )

    def scan(self):
        if self._discovery_thread is not None and self._discovery_thread.isRunning():
            return
        running = [
            index for index, worker in self._read_workers.items()
            if worker is not None and worker.isRunning()
        ]
        if running:
            self.statusBar().showMessage(
                "仍有磁盘正在读取；请等待当前读取结束后再重新扫描磁盘清单。"
            )
            return

        self.scan_action.setEnabled(False)
        self.devices = []
        self.device_items.clear()
        self._device_cache.clear()
        self._read_errors.clear()
        self.current_device_index = None
        self.current_partition_index = None
        self.current_payload = None
        self.current_layout = None
        self.hfsplus_reports = []
        self.device_list.blockSignals(True)
        self.device_list.clear()
        self.device_list.blockSignals(False)
        self._clear_workbench()
        self.disk_summary.setText("正在扫描 Windows 物理磁盘……")
        self.statusBar().showMessage("正在识别磁盘，请稍候……")

        self._discovery_thread = DiscoveryWorker(self)
        self._discovery_thread.devices_ready.connect(self._on_devices_found)
        self._discovery_thread.failed.connect(self._on_discovery_failed)
        self._discovery_thread.finished.connect(self._on_discovery_thread_finished)
        self._discovery_thread.finished.connect(self._discovery_thread.deleteLater)
        self._discovery_thread.start()


    def _on_devices_found(self, devices):
        self.devices = devices or []
        self.scan_action.setEnabled(True)
        self.device_list.blockSignals(True)
        self.device_list.clear()
        self.device_items.clear()

        for index, device in enumerate(self.devices):
            disk_no = device.windows_disk_number
            disk_label = f"HD{disk_no}" if disk_no is not None else "设备"
            name = device.model or device.name or "未知型号"
            root = QTreeWidgetItem([
                f"{disk_label}: {name}  ({human_bytes(device.capacity_bytes)})"
            ])
            root.setData(0, USER_ROLE, ("disk", index))
            root.setToolTip(
                0,
                f"{device.name}\n型号：{name}\n容量：{human_bytes(device.capacity_bytes)}"
                f"\n总线：{device.bus_type or device.protocol or '未知'}"
                f"\n分区表：{device.partition_style or '未知'}"
                f"\nSMART：{'可用' if device.smart_supported else '不可用'}"
                "\n单击：切换 / 查看缓存；双击：重新读取该磁盘",
            )
            self.device_list.addTopLevelItem(root)
            self.device_items[index] = root

        if not self.devices:
            self.device_list.blockSignals(False)
            self.disk_summary.setText("没有发现 Windows 可枚举的物理磁盘。")
            self.statusBar().showMessage("未发现物理磁盘")
            return

        # Avoid currentItemChanged causing a duplicate first read.
        self.device_list.setCurrentItem(self.device_items[0])
        self.device_list.blockSignals(False)
        self.selection_changed(0)


    def _tree_selection_changed(self, current, previous):
        if self._building_tree or current is None:
            return
        data = current.data(0, USER_ROLE)
        if not data:
            return
        if data[0] == "disk":
            # A single click switches to this disk; existing results are reused.
            self.selection_changed(int(data[1]))
        elif data[0] == "partition":
            disk_index = int(data[1])
            part_index = int(data[2])
            if disk_index == self.current_device_index and self.current_layout is not None:
                self._select_partition(part_index, sync_tree=False)

    def _tree_item_double_clicked(self, item, column):
        data = item.data(0, USER_ROLE)
        if not data or data[0] != "disk":
            return
        # Only a double-click on the physical-disk root forces a fresh read.
        self.selection_changed(int(data[1]), force_refresh=True)

    def selection_changed(self, index: int, force_refresh: bool = False):
        if index < 0 or index >= len(self.devices):
            return

        previous_index = self.current_device_index
        self.current_device_index = index
        device = self.devices[index]

        active_worker = self._read_workers.get(index)
        if active_worker is not None and active_worker.isRunning():
            if previous_index != index and self.current_layout is not None:
                self.current_partition_index = None
                self.current_payload = None
                self.current_layout = None
                self.hfsplus_reports = []
                self._clear_workbench()
            self.disk_summary.setText(
                f"{device.model or device.name} · {human_bytes(device.capacity_bytes)} · 正在读取……"
            )
            self.statusBar().showMessage(
                f"{device.model or device.name} 正在读取；不会重复启动读取任务。"
            )
            return

        cached = self._device_cache.get(index)
        if cached is not None and not force_refresh:
            self._display_device_result(index, cached)
            self.statusBar().showMessage(
                f"已切换到缓存结果：{device.model or device.name}。单击不重新读取，双击可刷新。"
            )
            return

        if previous_index != index and not force_refresh:
            self.current_partition_index = None
            self.current_payload = None
            self.current_layout = None
            self.hfsplus_reports = []
            self._clear_workbench()

        if not force_refresh:
            root_item = self.device_items.get(index)
            if root_item is not None:
                self._building_tree = True
                root_item.takeChildren()
                self._building_tree = False
        else:
            # Keep the previous successful content visible until refresh finishes.
            self.statusBar().showMessage(
                f"正在重新读取 {device.model or device.name}……"
            )

        self.disk_summary.setText(
            f"{device.model or device.name} · {human_bytes(device.capacity_bytes)} · "
            f"{device.bus_type or device.protocol or '总线未知'} · "
            f"{'正在重新读取……' if force_refresh else '正在读取……'}"
        )
        self._start_device_read(index, force_refresh=force_refresh)

    def _start_device_read(self, index: int, force_refresh: bool = False):
        if index < 0 or index >= len(self.devices):
            return
        existing = self._read_workers.get(index)
        if existing is not None and existing.isRunning():
            return

        device = self.devices[index]
        cached_payload = None if force_refresh else getattr(device, "cached_payload", None)
        if cached_payload is not None:
            device.cached_payload = None

        worker = DeviceReadWorker(device, cached_payload, self)
        self._read_workers[index] = worker
        worker.data_ready.connect(
            lambda payload, layout, smart_error, reports, disk_index=index, worker_ref=worker:
                self._on_device_data_ready(
                    disk_index, worker_ref, payload, layout, smart_error, reports
                )
        )
        worker.failed.connect(
            lambda message, disk_index=index:
                self._on_device_read_failed(disk_index, message)
        )
        worker.finished.connect(
            lambda disk_index=index, worker_ref=worker:
                self._on_read_thread_finished(disk_index, worker_ref)
        )
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_device_data_ready(
        self, index, worker, payload, layout, smart_error, hfsplus_reports
    ):
        # Cache every completed disk result, even when the user has switched away.
        # Stale worker responses must never overwrite another disk's visible state.
        if self._read_workers.get(index) is not worker:
            return
        result = {
            "payload": payload or {},
            "layout": layout or {},
            "smart_error": smart_error or "",
            "hfsplus_reports": hfsplus_reports or [],
        }
        self._device_cache[index] = result
        self._read_errors.pop(index, None)

        if index != self.current_device_index:
            return
        self._display_device_result(index, result)

    def _display_device_result(self, index: int, result: dict[str, Any]):
        if index < 0 or index >= len(self.devices) or index != self.current_device_index:
            return

        device = self.devices[index]
        payload = result.get("payload") or {}
        layout = result.get("layout") or {}
        smart_error = result.get("smart_error") or ""
        hfsplus_reports = result.get("hfsplus_reports") or []

        self.current_payload = payload or None
        self.current_layout = layout
        self.hfsplus_reports = hfsplus_reports

        self._update_disk_summary(device, payload, layout, smart_error)
        self._populate_disk_map(layout, device)
        self._populate_smart(payload, smart_error, device)
        self._populate_partition_table(layout)
        self._populate_partition_tree(layout)
        self._populate_file_browser(self.hfsplus_reports)

        parts = layout.get("partitions", [])
        if parts:
            self._select_partition(0)
        else:
            self._show_disk_properties(device, payload, layout)

        message = (
            f"读取完成：{device.model or device.name}；"
            f"{len(parts)} 个分区；SMART {'可用' if payload else '不可用'}。"
        )
        self.statusBar().showMessage(message + " 单击切换缓存；双击重新读取。")
        self.disk_summary.setText(
            f"{device.model or device.name} · {human_bytes(device.capacity_bytes)} · "
            f"{device.bus_type or device.protocol or '总线未知'} · "
            f"{layout.get('partition_table_style') or device.partition_style or '未知分区表'} · 读取完成"
        )


    def _update_disk_summary(self, device, payload, layout, smart_error):
        disk_number = layout.get("disk_number", device.windows_disk_number)
        total = layout.get("disk_size") or device.capacity_bytes
        style = layout.get("partition_table_style") or device.partition_style or "未知分区表"
        bus = layout.get("bus_type") or device.bus_type or device.protocol or "未知总线"
        smart_text = "SMART 已读取" if payload else f"SMART 不可用：{smart_error or '设备不支持'}"
        self.disk_summary.setText(
            f"磁盘 {disk_number if disk_number is not None else '?'} · "
            f"{device.model or device.name} · {human_bytes(total)} · "
            f"{style} · {bus} · {smart_text}"
        )

    def _populate_disk_map(self, layout, device):
        layout = layout or {}
        parts = layout.get("partitions", []) or []
        total = int(layout.get("disk_size") or getattr(device, "capacity_bytes", 0) or 0)
        if total <= 0:
            total = sum(max(int(part.get("Size") or 0), 0) for part in parts)

        allocated = sum(max(int(part.get("Size") or 0), 0) for part in parts)
        unallocated = max(total - allocated, 0)
        self.partition_map.set_data(parts, total)

        summary = (
            f"磁盘总容量：{human_bytes(total)}　"
            f"已分配：{human_bytes(allocated)}　"
            f"未分配：{human_bytes(unallocated)}"
        )
        if allocated > total > 0:
            summary += "　警告：分区容量合计超过磁盘容量，需检查分区表是否重叠。"
        self.partition_capacity_summary.setText(summary)
        self.partition_legend.setText(
            f"分区 {len(parts)} 个 · 彩色为已用空间，白色为分区可用空间，"
            "深灰为未分配空间，浅灰表示空间使用情况未知。"
            "极小分区按最小可视宽度显示，实际容量以分区表为准。"
        )

    def _populate_partition_tree(self, layout):
        if self.current_device_index is None:
            return
        root = self.device_items.get(self.current_device_index)
        if root is None:
            return

        parts = layout.get("partitions", []) if layout else []
        self._building_tree = True
        root.takeChildren()
        for part_index, part in enumerate(parts):
            drive = str(part.get("DriveLetter") or "").strip().upper()
            partition_no = part.get("PartitionNumber", part_index + 1)
            role = PartitionMap.partition_role(part)
            fs = (
                part.get("DetectedFileSystem")
                or part.get("FileSystem")
                or part.get("PartitionTypeName")
                or "未识别格式"
            )
            label = part.get("FileSystemLabel") or part.get("PartitionName") or ""
            size = human_bytes(part.get("Size"))
            prefix = f"{drive}: " if drive else ""
            name = f"{prefix}{label or role}  ·  {size}  ·  {fs}"
            child = QTreeWidgetItem([name])
            child.setData(0, USER_ROLE, ("partition", self.current_device_index, part_index))
            child.setToolTip(
                0,
                f"分区编号：{partition_no}\n类型：{role}\n文件系统：{fs}"
                f"\n偏移：{part.get('Offset', 0)} 字节\n容量：{size}",
            )
            root.addChild(child)

        root.setExpanded(True)
        self._building_tree = False

    def _populate_partition_table(self, layout):
        parts = layout.get("partitions", []) if layout else []
        self.partition_table.setRowCount(len(parts))
        logical_sector = int(layout.get("logical_sector_size") or 512)

        for row, part in enumerate(parts):
            drive = str(part.get("DriveLetter") or "").strip().upper()
            name = part.get("FileSystemLabel") or part.get("PartitionName") or PartitionMap.partition_name(part)
            if drive:
                name = f"{drive}:  {name}"
            fs = part.get("DetectedFileSystem") or part.get("FileSystem") or "未知 / 未挂载"
            offset = int(part.get("Offset") or 0)
            start_lba = offset // max(logical_sector, 1)
            size = int(part.get("Size") or 0)
            used = part.get("SizeUsed")
            free = part.get("SizeRemaining")
            flags = []
            if part.get("IsHidden"):
                flags.append("隐藏")
            if part.get("NoDefaultDriveLetter"):
                flags.append("无默认盘符")
            if part.get("IsReadOnly"):
                flags.append("只读")
            if part.get("IsOffline"):
                flags.append("离线")
            type_name = str(part.get("PartitionTypeName") or "")
            if type_name:
                flags.append(type_name)

            values = [
                name,
                fs,
                f"{start_lba:,}",
                human_bytes(size),
                human_bytes(used),
                human_bytes(free),
                "；".join(flags) or "正常",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(USER_ROLE, row)
                self.partition_table.setItem(row, col, item)
            self.partition_table.setRowHeight(row, 27)

        self.partition_table.clearSelection()

    def _partition_table_row_changed(self, row, column, previous_row, previous_column):
        if row < 0 or self.current_layout is None:
            return
        self._select_partition(row)

    def _select_partition(self, part_index: int, sync_tree: bool = True):
        parts = (self.current_layout or {}).get("partitions", [])
        if not (0 <= part_index < len(parts)):
            return
        self.current_partition_index = part_index
        part = parts[part_index]
        device = self.devices[self.current_device_index] if self.current_device_index is not None else None
        layout = self.current_layout or {}

        self.partition_table.blockSignals(True)
        self.partition_table.selectRow(part_index)
        self.partition_table.blockSignals(False)

        if sync_tree and self.current_device_index in self.device_items:
            root = self.device_items[self.current_device_index]
            if part_index < root.childCount():
                self._building_tree = True
                self.device_list.setCurrentItem(root.child(part_index))
                self._building_tree = False

        logical_sector = int(layout.get("logical_sector_size") or 512)
        offset = int(part.get("Offset") or 0)
        size = int(part.get("Size") or 0)
        drive = str(part.get("DriveLetter") or "").strip().upper()
        detected_fs = part.get("DetectedFileSystem") or part.get("FileSystem") or "未识别 / 未挂载"
        rows = [
            ("所属磁盘", f"磁盘 {layout.get('disk_number', '?')} · {device.model if device else ''}"),
            ("分区编号", str(part.get("PartitionNumber", part_index + 1))),
            ("盘符", f"{drive}:" if drive else "无盘符"),
            ("卷标 / 分区名称", str(part.get("FileSystemLabel") or part.get("PartitionName") or "无")),
            ("文件系统 / 容器", str(detected_fs)),
            ("文件系统识别来源", str(part.get("FileSystemSource") or "未提供")),
            ("分区类型", str(part.get("PartitionTypeName") or "未知")),
            ("分区表", str(part.get("PartitionTableStyle") or layout.get("partition_table_style") or "未知")),
            ("分区类型 GUID", str(part.get("PartitionTypeGuid") or part.get("GptType") or "不可用")),
            ("MBR 类型代码", str(part.get("MbrType") or "不适用")),
            ("起始偏移", f"{offset:,} 字节"),
            ("起始 LBA", f"{offset // max(logical_sector, 1):,}"),
            ("分区容量", f"{human_bytes(size)} ({size:,} 字节)"),
            ("逻辑扇区大小", f"{logical_sector} 字节"),
            ("已用空间", human_bytes(part.get("SizeUsed"))),
            ("可用空间", human_bytes(part.get("SizeRemaining"))),
            ("隐藏属性", "是" if part.get("IsHidden") else "否"),
            ("无默认盘符属性", "是" if part.get("NoDefaultDriveLetter") else "否"),
            ("只读属性", "是" if part.get("IsReadOnly") else "否"),
            ("离线属性", "是" if part.get("IsOffline") else "否"),
        ]

        selected_report = None
        for report in self.hfsplus_reports:
            if int(report.get("partition_offset", -1)) == offset:
                selected_report = report
                break
        if selected_report:
            if selected_report.get("error"):
                rows.append(("HFS+ 深度解析", f"失败：{selected_report['error']}"))
            else:
                rows.extend([
                    ("HFS+ 卷名", str(selected_report.get("catalog", {}).get("volume_name", "未知"))),
                    ("HFS+ 卷版本", f"{selected_report.get('filesystem')} v{selected_report.get('version')}"),
                    ("分配块大小", f"{selected_report.get('block_size', 0):,} 字节"),
                    ("总分配块数", f"{selected_report.get('total_blocks', 0):,}"),
                    ("空闲分配块数", f"{selected_report.get('free_blocks', 0):,}"),
                    ("文件数量（卷头）", f"{selected_report.get('file_count', 0):,}"),
                    ("文件夹数量（卷头）", f"{selected_report.get('folder_count', 0):,}"),
                    ("日志状态", "已启用" if selected_report.get("journaled") else "未启用"),
                    ("正常卸载标记", "是" if selected_report.get("clean_unmount") else "否 / 未确认"),
                ])
        self._set_property_rows(rows)
        self.property_title.setText(f"分区 {part.get('PartitionNumber', part_index + 1)} 参数")
        self.property_hint.setText(f"{human_bytes(size)} · {detected_fs}")
        self._configure_file_browser(part, selected_report)
        self.statusBar().showMessage(
            f"已选中分区 {part.get('PartitionNumber', part_index + 1)}：{detected_fs}，"
            f"起始偏移 {offset:,} 字节。"
        )

    def _show_disk_properties(self, device, payload, layout):
        rows = [
            ("型号", device.model or device.name),
            ("序列号", device.serial or "不可用"),
            ("物理磁盘编号", str(layout.get("disk_number", device.windows_disk_number))),
            ("磁盘容量", human_bytes(layout.get("disk_size") or device.capacity_bytes)),
            ("接口 / 总线", device.bus_type or device.protocol or "未知"),
            ("分区表", layout.get("partition_table_style") or device.partition_style or "未知"),
            ("逻辑扇区大小", f"{layout.get('logical_sector_size', 512)} 字节"),
            ("物理扇区大小", f"{layout.get('physical_sector_size', 512)} 字节"),
            ("设备状态", device.operational_status or "未知"),
            ("SMART", "已读取" if payload else device.smart_error or "不可用"),
        ]
        self._set_property_rows(rows)

    def _set_property_rows(self, rows):
        self.details.setRowCount(len(rows))
        for row, (name, value) in enumerate(rows):
            self.details.setItem(row, 0, QTableWidgetItem(str(name)))
            self.details.setItem(row, 1, QTableWidgetItem(str(value)))
            self.details.setRowHeight(row, 25)

    def _populate_file_browser(self, reports):
        self._available_hfs_reports = reports or []
        self._reset_file_browser(
            "已读取分区信息。选择一个已挂载分区，或选择可读取的 HFS+ / HFSX 分区开始浏览。"
        )

    def _reset_file_browser(self, message):
        self._browser_generation += 1
        self._browser_kind = "none"
        self._browser_root = ""
        self._browser_path = ""
        self._browser_history = []
        self._browser_report = None
        self.file_table.setRowCount(0)
        self.file_summary.setText(message)
        self.file_preview.clear()
        self.file_path_edit.clear()
        self.file_path_edit.setEnabled(False)
        self.file_back_button.setEnabled(False)
        self.file_up_button.setEnabled(False)
        self.file_refresh_button.setEnabled(False)
        self.file_go_button.setEnabled(False)

    def _configure_file_browser(self, partition, hfs_report=None):
        self._reset_file_browser("正在准备所选分区的只读文件浏览器……")
        root = windows_root_for_partition(partition)
        if root:
            self._browser_kind = "windows"
            self._browser_root = root
            self._browser_path = root
            self.file_path_edit.setEnabled(True)
            self.file_refresh_button.setEnabled(True)
            self.file_go_button.setEnabled(True)
            self._navigate_windows_directory(root, add_history=False)
            return

        if hfs_report and not hfs_report.get("error"):
            self._browser_kind = "hfs"
            self._browser_report = hfs_report
            self._browser_path = "/"
            self.file_path_edit.setEnabled(True)
            self.file_go_button.setEnabled(True)
            self._navigate_hfs_directory("/", add_history=False)
            return

        if hfs_report and hfs_report.get("error"):
            self.file_summary.setText(
                f"HFS+ / HFSX 元数据读取失败：{hfs_report['error']}。"
                "当前不会挂载或修改该分区。"
            )
        else:
            self.file_summary.setText(
                "该分区没有可用盘符，也没有可用的 HFS+ / HFSX Catalog 元数据。"
                "当前版本不能直接浏览此文件系统；不会擅自挂载、解密或修复。"
            )
        self.file_path_edit.setEnabled(False)
        self.file_go_button.setEnabled(False)

    def _update_browser_navigation(self):
        self.file_path_edit.setText(self._browser_path)
        self.file_back_button.setEnabled(bool(self._browser_history))
        self.file_up_button.setEnabled(
            bool(self._browser_path)
            and self._browser_path.rstrip("\\/") not in {
                self._browser_root.rstrip("\\/"),
                "/",
            }
        )
        self.file_refresh_button.setEnabled(self._browser_kind == "windows")
        self.file_go_button.setEnabled(self._browser_kind in {"windows", "hfs"})

    def _navigate_windows_directory(self, path, add_history=True):
        target = os.path.abspath(os.path.normpath(path))
        if not self._browser_root or not self._path_is_within_root(target, self._browser_root):
            self.file_summary.setText("访问路径超出当前分区范围，已阻止。")
            return

        if add_history and self._browser_path and target != self._browser_path:
            self._browser_history.append(self._browser_path)
        self._browser_kind = "windows"
        self._browser_path = target
        self._update_browser_navigation()
        self.file_table.setRowCount(0)
        self.file_preview.clear()
        self.file_summary.setText(f"正在读取目录：{target}")

        self._browser_generation += 1
        token = self._browser_generation
        worker = DirectoryReadWorker(token, target, self._browser_root, self)
        self._browser_workers[token] = worker
        worker.completed.connect(self._on_windows_directory_ready)
        worker.finished.connect(
            lambda request_token=token, request_worker=worker:
                self._on_directory_worker_finished(request_token, request_worker)
        )
        worker.finished.connect(worker.deleteLater)
        worker.start()

    @staticmethod
    def _path_is_within_root(path, root):
        from .file_browser import is_path_within_root
        return is_path_within_root(path, root)

    def _on_windows_directory_ready(self, token, result):
        if token != self._browser_generation or self._browser_kind != "windows":
            return
        if result.get("error"):
            self.file_summary.setText(result["error"])
            self.file_table.setRowCount(0)
            return

        entries = result.get("entries", [])
        self._render_file_entries(entries)
        folder_count = sum(1 for item in entries if item.get("is_dir"))
        file_count = sum(1 for item in entries if item.get("is_file"))
        summary = (
            f"{self._browser_path} · {folder_count:,} 个文件夹 · "
            f"{file_count:,} 个文件 · 只读浏览"
        )
        if result.get("truncated"):
            summary += " · 目录项目达到显示上限，列表已截断"
        warnings = result.get("warnings") or []
        if warnings:
            summary += f" · {len(warnings)} 个项目无法读取"
        self.file_summary.setText(summary)

    def _on_directory_worker_finished(self, token, worker):
        if self._browser_workers.get(token) is worker:
            self._browser_workers.pop(token, None)

    def _navigate_hfs_directory(self, path, add_history=True):
        if not self._browser_report or self._browser_report.get("error"):
            return
        target = posixpath.normpath("/" + str(path or "/").lstrip("/"))
        if not target.startswith("/"):
            self.file_summary.setText("HFS+ 路径无效。")
            return

        catalog = self._browser_report.get("catalog", {})
        if add_history and self._browser_path and target != self._browser_path:
            self._browser_history.append(self._browser_path)
        self._browser_kind = "hfs"
        self._browser_path = target
        self._update_browser_navigation()
        entries = catalog_directory_entries(catalog, target)
        self._render_file_entries(entries)
        volume_name = catalog.get("volume_name") or "未命名卷"
        self.file_summary.setText(
            f"{volume_name}:{target} · {len(entries):,} 个直接子项目 · "
            f"{self._browser_report.get('filesystem')} 只读 Catalog 浏览。"
            "HFS+ 文件内容读取尚未开放。"
        )

    def _render_file_entries(self, entries):
        self.file_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            size = (
                f"{int(entry.get('child_count') or 0):,} 项"
                if entry.get("is_dir") and entry.get("child_count") is not None
                else "—" if entry.get("is_dir")
                else human_bytes(entry.get("size"))
            )
            values = [
                str(entry.get("kind") or ""),
                str(entry.get("name") or ""),
                size,
                str(entry.get("modified") or "—"),
                str(entry.get("attributes") or "—"),
                str(entry.get("id") or "—"),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 1:
                    item.setData(USER_ROLE, entry)
                self.file_table.setItem(row, col, item)
            self.file_table.setRowHeight(row, 25)
        if entries:
            self.file_table.selectRow(0)
        else:
            self.file_preview.setPlainText("当前目录为空，或 Catalog 中没有可显示的直接子项目。")

    def _browser_item_selected(self, row, column, previous_row, previous_column):
        if row < 0:
            return
        item = self.file_table.item(row, 1)
        if item is None:
            return
        entry = item.data(USER_ROLE)
        if not isinstance(entry, dict):
            return
        self._show_file_entry_details(entry)

    def _show_file_entry_details(self, entry):
        details = [
            f"名称：{entry.get('name', '')}",
            f"类型：{entry.get('kind', '')}",
            f"路径：{entry.get('path', '')}",
            f"大小：{human_bytes(entry.get('size')) if entry.get('size') is not None else '目录 / 未提供'}",
            f"修改时间：{entry.get('modified') or '未提供'}",
            f"属性：{entry.get('attributes') or '未提供'}",
        ]
        if entry.get("id") is not None:
            details.append(f"文件系统 ID / CNID：{entry['id']}")
        if entry.get("is_link"):
            details.append("安全限制：不会跟随符号链接或 Windows 重解析点。")
        self.file_preview.setPlainText("\n".join(details))

    def _browser_item_activated(self, row, column):
        item = self.file_table.item(row, 1)
        if item is None:
            return
        entry = item.data(USER_ROLE)
        if not isinstance(entry, dict):
            return

        if entry.get("is_dir"):
            if self._browser_kind == "windows":
                self._navigate_windows_directory(entry.get("path", ""), add_history=True)
            elif self._browser_kind == "hfs":
                self._navigate_hfs_directory(entry.get("path", ""), add_history=True)
            return

        if not entry.get("is_file"):
            self._show_file_entry_details(entry)
            return

        if self._browser_kind == "hfs":
            self.file_preview.setPlainText(
                self.file_preview.toPlainText()
                + "\n\nHFS+ / HFSX 当前已读取目录记录和文件逻辑长度，"
                "但尚未实现普通文件 Data Fork 的 extent 映射，因此不能预览文件正文。"
            )
            return

        preview = read_text_preview(entry.get("path", ""), self._browser_root)
        if preview.get("error") and not preview.get("text"):
            self.file_preview.setPlainText(preview["error"])
            return
        note = preview.get("note") or preview.get("error") or "二进制数据预览"
        self.file_preview.setPlainText(
            f"{entry.get('name', '')}\n{note}\n\n{preview.get('text', '')}"
        )

    def _browser_back(self):
        if not self._browser_history:
            return
        target = self._browser_history.pop()
        if self._browser_kind == "windows":
            self._navigate_windows_directory(target, add_history=False)
        elif self._browser_kind == "hfs":
            self._navigate_hfs_directory(target, add_history=False)

    def _browser_up(self):
        if self._browser_kind == "windows":
            parent = os.path.dirname(self._browser_path.rstrip("\\/"))
            if not parent:
                parent = self._browser_root
            self._navigate_windows_directory(parent, add_history=True)
        elif self._browser_kind == "hfs":
            parent = posixpath.dirname(self._browser_path.rstrip("/")) or "/"
            self._navigate_hfs_directory(parent, add_history=True)

    def _browser_refresh(self):
        if self._browser_kind == "windows":
            self._navigate_windows_directory(self._browser_path, add_history=False)
        elif self._browser_kind == "hfs":
            self._navigate_hfs_directory(self._browser_path, add_history=False)

    def _browser_go_to_path(self):
        raw = self.file_path_edit.text().strip()
        if not raw:
            return
        if self._browser_kind == "windows":
            target = raw if os.path.isabs(raw) else os.path.join(self._browser_path, raw)
            self._navigate_windows_directory(target, add_history=True)
        elif self._browser_kind == "hfs":
            self._navigate_hfs_directory(raw, add_history=True)


    def _populate_smart(self, payload, smart_error, device=None):
        fallback = {
            "model": getattr(device, "model", ""),
            "serial": getattr(device, "serial", ""),
            "protocol": getattr(device, "protocol", ""),
            "capacity_bytes": getattr(device, "capacity_bytes", None),
            "smart_error": smart_error,
        }
        report = build_smart_report(payload or {}, fallback)
        self.smart_model.setText(report["model"])
        self.smart_serial.setText(f"序列号：{report['serial']}")
        self.smart_firmware.setText(f"固件：{report['firmware']}")
        self.smart_health_value.setText(report["health"])
        self.smart_health_detail.setText(report["health_detail"])
        self.smart_temperature_value.setText(report["temperature"])

        health = report["health"]
        if health == "良好":
            self.smart_health_value.setStyleSheet("color:#28a745;font-size:27px;font-weight:700;")
        elif health in {"警告", "不可用"}:
            self.smart_health_value.setStyleSheet("color:#d04437;font-size:27px;font-weight:700;")
        else:
            self.smart_health_value.setStyleSheet("color:#9a6700;font-size:27px;font-weight:700;")

        for name, value in report["metrics"]:
            label = self.smart_metric_values.get(name)
            if label is not None:
                label.setText(value)

        attributes = report["attributes"]
        self.smart_attributes_table.setRowCount(len(attributes))
        for row, attribute in enumerate(attributes):
            values = [
                attribute["status"],
                attribute["id"],
                attribute["name"],
                attribute["raw_hex"],
                attribute["raw_decimal"],
            ]
            for col, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                self.smart_attributes_table.setItem(row, col, cell)
            self.smart_attributes_table.setRowHeight(row, 25)

        self.smart_attribute_summary.setText(
            f"{report['attribute_kind']} · {len(attributes)} 项 · 原始值同时显示十六进制和十进制"
            if report["available"] else "SMART 属性不可用"
        )
        if report["available"]:
            self.raw.setPlainText(json.dumps(report["raw_json"], ensure_ascii=False, indent=2))
            self.smart_summary.setText(
                f"协议：{report['protocol']} · 容量：{report['capacity']} · "
                f"未提供的字段会如实显示，不以 0 代替。"
            )
        else:
            self.raw.setPlainText(
                "该物理磁盘已由 Windows 识别，但没有取得 SMART 数据。\n"
                f"原因：{smart_error or '设备或 USB 转接桥未提供 SMART 直通'}"
            )
            self.smart_summary.setText(
                f"SMART 不可用：{smart_error or '设备未提供 SMART 直通'}。"
                "磁盘与分区信息仍可继续读取。"
            )

    def _on_device_read_failed(self, index, message):
        self._read_errors[index] = str(message)
        if index != self.current_device_index:
            return

        cached = self._device_cache.get(index)
        if cached is not None:
            self._display_device_result(index, cached)
            self.statusBar().showMessage(
                f"重新读取失败，继续显示上次成功结果：{message}"
            )
            return

        self.disk_summary.setText(f"读取失败：{message}")
        self.file_summary.setText(message)
        self.raw.setPlainText(message)
        self.statusBar().showMessage("磁盘信息读取失败")

    def _on_read_thread_finished(self, index, worker):
        if self._read_workers.get(index) is worker:
            self._read_workers.pop(index, None)
        if index == self.current_device_index and index in self._read_errors:
            self.statusBar().showMessage(
                f"读取失败：{self._read_errors[index]}；双击磁盘可重试。"
            )


    def _on_discovery_failed(self, message):
        self.scan_action.setEnabled(True)
        self.disk_summary.setText(f"磁盘识别失败：{message}")
        self.statusBar().showMessage("磁盘识别失败")

    def _on_discovery_thread_finished(self):
        self._discovery_thread = None

    def _clear_workbench(self):
        self.disk_summary.setText("正在读取所选磁盘……")
        self.partition_map.set_data([], 0)
        self.partition_capacity_summary.setText("磁盘总容量：正在读取……")
        self.partition_legend.setText("正在读取分区信息……")
        self.partition_table.setRowCount(0)
        self.details.setRowCount(0)
        self._reset_file_browser("正在读取文件系统信息……")
        self.smart_attributes_table.setRowCount(0)
        self.smart_model.setText("尚未读取 SMART 设备")
        self.smart_serial.setText("序列号：设备未提供")
        self.smart_firmware.setText("固件：设备未提供")
        self.smart_health_value.setText("设备未提供")
        self.smart_health_detail.setText("尚未读取")
        self.smart_temperature_value.setText("设备未提供")
        for value in self.smart_metric_values.values():
            value.setText("设备未提供")
        self.smart_summary.setText("SMART 数据尚未读取。")
        self.raw.clear()
        self.current_partition_index = None
        self.hfsplus_reports = []


    def closeEvent(self, event):
        workers = list(self._read_workers.values()) + list(self._browser_workers.values())
        if self._discovery_thread is not None:
            workers.append(self._discovery_thread)
        for worker in workers:
            if worker is not None and worker.isRunning():
                worker.wait(3000)
        super().closeEvent(event)
