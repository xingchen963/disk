"""DiskRescue partition-centric workbench UI.

The layout follows a traditional disk utility workflow:
physical disk/partition tree on the left; disk map and tabbed inspection
workspace on the right. All disk operations remain read-only.
"""

import json
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
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


USER_ROLE = Qt.ItemDataRole.UserRole


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
        self._read_thread = None
        self._discovery_thread = None
        self._reading_device = None
        self._building_tree = False

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
        layout = QVBoxLayout(page)
        layout.setContentsMargins(5, 7, 5, 5)
        layout.setSpacing(5)

        partition_title = QLabel("分区列表")
        partition_title.setObjectName("panelTitle")
        layout.addWidget(partition_title)

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
        layout.addWidget(self.partition_table, 3)

        property_header = QHBoxLayout()
        self.property_title = QLabel("分区参数")
        self.property_title.setObjectName("panelTitle")
        property_header.addWidget(self.property_title)
        property_header.addStretch(1)
        self.property_hint = QLabel("选择上方分区查看属性")
        self.property_hint.setObjectName("subtleText")
        property_header.addWidget(self.property_hint)
        layout.addLayout(property_header)

        self.details = QTableWidget(0, 2)
        self.details.setHorizontalHeaderLabels(["属性", "读取结果"])
        self.details.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.details.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.details.setAlternatingRowColors(True)
        self.details.horizontalHeader().setStretchLastSection(True)
        self.details.setColumnWidth(0, 210)
        layout.addWidget(self.details, 2)

        self.detail_tabs.addTab(page, "分区参数")

    def _build_file_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(5, 7, 5, 5)
        self.file_summary = QLabel(
            "选择包含受支持文件系统的分区后，这里显示只读目录结构。"
        )
        self.file_summary.setWordWrap(True)
        self.file_summary.setObjectName("subtleText")
        layout.addWidget(self.file_summary)

        self.file_table = QTableWidget(0, 5)
        self.file_table.setHorizontalHeaderLabels(["类型", "名称", "CNID", "大小 / 子项", "完整路径"])
        self.file_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.file_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.file_table.setAlternatingRowColors(True)
        self.file_table.horizontalHeader().setStretchLastSection(True)
        self.file_table.setColumnWidth(0, 75)
        self.file_table.setColumnWidth(1, 210)
        self.file_table.setColumnWidth(2, 90)
        self.file_table.setColumnWidth(3, 135)
        layout.addWidget(self.file_table, 1)
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
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #f3f4f6; color: #20252b; }
            QMenuBar { background: #f7f7f7; border-bottom: 1px solid #d4d7dc; }
            QMenuBar::item { padding: 5px 10px; }
            QToolBar { background: #f7f7f7; border: 0; border-bottom: 1px solid #d7dbe0; spacing: 8px; padding: 4px; }
            QToolButton { padding: 5px 10px; }
            #safetyBanner { background: #e8f1ff; color: #17365d; border: 1px solid #cadcf5; padding: 6px 9px; }
            #diskSummary { background: #ffffff; border: 1px solid #d4d8de; padding: 7px 9px; font-weight: 600; }
            #panelTitle { font-weight: 600; color: #26394f; padding: 3px 1px; }
            #smartModel { color: #153fc0; font-size: 29px; font-weight: 500; padding: 1px 4px; }
            #smartIdentity { color: #18212d; font-size: 16px; padding: 0 8px 3px 8px; }
            #smartHealthPanel { background: #f0f3f7; border: 1px solid #d5dbe3; border-radius: 7px; }
            #smartMetricPanel { background: #f0f3f7; border: 1px solid #d5dbe3; border-radius: 7px; }
            #smartMetricCard { background: #ffffff; border: 1px solid #dce1e8; border-radius: 5px; }
            #smartSmallLabel { color: #5c6d83; font-size: 12px; }
            #smartHealthValue { color: #27a844; font-size: 27px; font-weight: 700; }
            #smartTemperatureValue { color: #36b94a; font-size: 25px; font-weight: 600; }
            #smartMetricValue { color: #172d4d; font-size: 16px; font-weight: 600; }
            #subtleText { color: #5c6775; }
            QTreeWidget { background: #fffef4; border: 1px solid #d3d6db; alternate-background-color: #fafafa; }
            QTreeWidget::item { padding: 3px 2px; }
            QTreeWidget::item:selected { background: #dceaff; color: #102f5d; }
            QTableWidget { background: #ffffff; gridline-color: #e0e3e7; border: 1px solid #d3d7dd; }
            QHeaderView::section { background: #e9ebee; color: #263342; padding: 5px; border: 0; border-right: 1px solid #d7dbe0; border-bottom: 1px solid #cdd2d9; }
            QTabWidget::pane { background: #ffffff; border: 1px solid #cfd4db; }
            QTabBar::tab { background: #e9ebef; padding: 7px 15px; border: 1px solid #d0d5dc; }
            QTabBar::tab:selected { background: #ffffff; color: #173b69; font-weight: 600; }
            QStatusBar { background: #f4f5f6; border-top: 1px solid #d4d7dc; }
        """)

    def _show_safety_help(self):
        self.statusBar().showMessage(
            "安全模式：只读枚举、分区解析和文件系统元数据检查；不执行格式化、修复或写盘操作。",
            8000,
        )

    def scan(self):
        if self._discovery_thread is not None and self._discovery_thread.isRunning():
            return
        if self._read_thread is not None and self._read_thread.isRunning():
            self.statusBar().showMessage("当前磁盘正在读取，请完成后再扫描。")
            return

        self.scan_action.setEnabled(False)
        self.devices = []
        self.device_items.clear()
        self.current_device_index = None
        self.current_partition_index = None
        self.current_layout = None
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
                f"\nSMART：{'可用' if device.smart_supported else '不可用'}",
            )
            self.device_list.addTopLevelItem(root)
            self.device_items[index] = root

        self.device_list.blockSignals(False)
        if not self.devices:
            self.disk_summary.setText("没有发现 Windows 可枚举的物理磁盘。")
            self.statusBar().showMessage("未发现物理磁盘")
            return

        self.device_list.setCurrentItem(self.device_items[0])
        self.selection_changed(0)

    def _tree_selection_changed(self, current, previous):
        if self._building_tree or current is None:
            return
        data = current.data(0, USER_ROLE)
        if not data:
            return
        if data[0] == "disk":
            self.selection_changed(int(data[1]))
        elif data[0] == "partition":
            disk_index = int(data[1])
            part_index = int(data[2])
            if disk_index == self.current_device_index and self.current_layout is not None:
                self._select_partition(part_index, sync_tree=False)

    def selection_changed(self, index: int):
        if index < 0 or index >= len(self.devices):
            return
        if self._read_thread is not None and self._read_thread.isRunning():
            return
        if index == self.current_device_index and self.current_layout is not None:
            return

        device = self.devices[index]
        self.current_device_index = index
        self.current_partition_index = None
        self.current_layout = None
        self._reading_device = device
        self.device_list.setEnabled(False)
        self._clear_workbench()
        self.disk_summary.setText(
            f"{device.model or device.name} · {human_bytes(device.capacity_bytes)} · "
            f"{device.bus_type or device.protocol or '总线未知'} · 正在读取……"
        )
        self.statusBar().showMessage(f"正在读取：{device.model or device.name}")

        root_item = self.device_items.get(index)
        if root_item is not None:
            self._building_tree = True
            root_item.takeChildren()
            self._building_tree = False

        cached_payload = getattr(device, "cached_payload", None)
        if cached_payload is not None:
            device.cached_payload = None
        self._read_thread = DeviceReadWorker(device, cached_payload, self)
        self._read_thread.data_ready.connect(self._on_device_data_ready)
        self._read_thread.failed.connect(self._on_device_read_failed)
        self._read_thread.finished.connect(self._on_read_thread_finished)
        self._read_thread.finished.connect(self._read_thread.deleteLater)
        self._read_thread.start()

    def _on_device_data_ready(self, payload, layout, smart_error, hfsplus_reports):
        device = self._reading_device
        if device is None:
            return

        payload = payload or {}
        self.current_payload = payload or None
        self.current_layout = layout or {}
        self.hfsplus_reports = hfsplus_reports or []

        self._update_disk_summary(device, payload, layout, smart_error)
        self._populate_smart(payload, smart_error, device)
        self._populate_partition_table(layout)
        self._populate_partition_tree(layout)
        self._populate_file_browser(self.hfsplus_reports)

        parts = (layout or {}).get("partitions", [])
        if parts:
            self._select_partition(0)
        else:
            self._show_disk_properties(device, payload, layout)

        message = (
            f"读取完成：{device.model or device.name}；"
            f"{len(parts)} 个分区；SMART {'可用' if payload else '不可用'}。"
        )
        self.statusBar().showMessage(message)
        self.disk_summary.setText(
            self.disk_summary.text().replace("正在读取……", "读取完成")
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
        self.file_table.setRowCount(0)
        summaries = []
        all_rows = []
        for report_index, report in enumerate(reports or [], start=1):
            if report.get("error"):
                summaries.append(f"Apple 分区 {report_index} 读取失败：{report['error']}")
                continue

            catalog = report.get("catalog", {})
            volume_name = catalog.get("volume_name") or "未命名卷"
            summaries.append(
                f"{volume_name} · {report.get('filesystem')} v{report.get('version')} · "
                f"文件 {report.get('file_count', 0):,} · 文件夹 {report.get('folder_count', 0):,} · "
                f"已列出 {catalog.get('entries_returned', 0):,} 项"
            )
            for warning in report.get("warnings", []):
                summaries.append(f"提示：{warning}")

            for entry in catalog.get("entries", []):
                if len(all_rows) >= 20000:
                    break
                if entry.get("kind") == "文件":
                    size = human_bytes(entry.get("size"))
                else:
                    size = f"{int(entry.get('child_count') or 0):,} 项"
                all_rows.append((
                    str(entry.get("kind") or ""),
                    str(entry.get("name") or ""),
                    str(entry.get("id") or ""),
                    size,
                    str(entry.get("path") or ""),
                ))

        if not reports:
            summaries.append("尚未发现可读取的 HFS+ / HFSX 分区。其他文件系统的浏览模块将在后续版本逐步加入。")
        self.file_summary.setText("\n".join(summaries))
        self.file_table.setRowCount(len(all_rows))
        for row, values in enumerate(all_rows):
            for col, value in enumerate(values):
                self.file_table.setItem(row, col, QTableWidgetItem(value))
            self.file_table.setRowHeight(row, 24)

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

    def _on_device_read_failed(self, message):
        self.disk_summary.setText(f"读取失败：{message}")
        self.file_summary.setText(message)
        self.raw.setPlainText(message)
        self.statusBar().showMessage("磁盘信息读取失败")

    def _on_read_thread_finished(self):
        self.device_list.setEnabled(True)
        self._read_thread = None
        self._reading_device = None

    def _on_discovery_failed(self, message):
        self.scan_action.setEnabled(True)
        self.disk_summary.setText(f"磁盘识别失败：{message}")
        self.statusBar().showMessage("磁盘识别失败")

    def _on_discovery_thread_finished(self):
        self._discovery_thread = None

    def _clear_workbench(self):
        self.disk_summary.setText("正在读取所选磁盘……")
        self.partition_map.set_data([], 0)
        self.partition_legend.setText("正在读取分区信息……")
        self.partition_table.setRowCount(0)
        self.details.setRowCount(0)
        self.file_table.setRowCount(0)
        self.file_summary.setText("正在读取文件系统信息……")
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
        for worker in (self._read_thread, self._discovery_thread):
            if worker is not None and worker.isRunning():
                worker.wait(3000)
        super().closeEvent(event)
