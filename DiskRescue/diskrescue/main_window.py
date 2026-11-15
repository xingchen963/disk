import json
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .smartctl import Device, SmartctlError, discover_devices, read_smart, summarize


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DiskRescue — 硬盘检测与数据救援")
        self.resize(1050, 720)
        self.devices: list[Device] = []

        central = QWidget()
        layout = QVBoxLayout(central)

        banner = QLabel(
            "安全模式：当前版本仅执行设备识别和 SMART 只读查询，不会修改磁盘数据。"
        )
        banner.setWordWrap(True)
        banner.setStyleSheet(
            "padding:10px;background:#e8f2ff;color:#17365d;border-radius:6px;"
        )
        layout.addWidget(banner)

        actions = QHBoxLayout()
        self.scan_button = QPushButton("扫描磁盘")
        self.scan_button.clicked.connect(self.scan)
        self.smart_button = QPushButton("读取 SMART")
        self.smart_button.clicked.connect(self.show_smart)
        self.smart_button.setEnabled(False)
        actions.addWidget(self.scan_button)
        actions.addWidget(self.smart_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.device_list = QListWidget()
        self.device_list.currentRowChanged.connect(self.selection_changed)
        splitter.addWidget(self.device_list)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.status = QLabel("请先扫描磁盘。")
        right_layout.addWidget(self.status)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["项目", "读取结果"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setAlternatingRowColors(True)
        right_layout.addWidget(self.table)

        self.raw = QTextEdit()
        self.raw.setReadOnly(True)
        self.raw.setPlaceholderText("SMART 原始 JSON 和 smartctl 诊断信息")
        right_layout.addWidget(self.raw)

        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)

        self.setCentralWidget(central)
        self.statusBar().showMessage("DiskRescue 开发版")

    def selection_changed(self, row: int):
        self.smart_button.setEnabled(0 <= row < len(self.devices))

    def scan(self):
        self.scan_button.setEnabled(False)
        self.status.setText("正在识别设备……")
        self.device_list.clear()
        self.table.setRowCount(0)
        self.raw.clear()

        try:
            self.devices = discover_devices()
            for device in self.devices:
                label = (
                    f"{device.name}    "
                    f"[{device.protocol or device.device_type or '设备'}]"
                )
                self.device_list.addItem(label)
            self.status.setText(f"识别到 {len(self.devices)} 个设备。")
            if not self.devices:
                self.status.setText(
                    "未发现支持 SMART 查询的设备。USB 转接盒可能不透传 SMART。"
                )
        except Exception as exc:
            QMessageBox.warning(self, "设备扫描", str(exc))
            self.status.setText("扫描未完成。")
        finally:
            self.scan_button.setEnabled(True)

    def show_smart(self):
        row = self.device_list.currentRow()
        if row < 0 or row >= len(self.devices):
            return

        self.smart_button.setEnabled(False)
        self.status.setText("正在读取 SMART 信息……")
        self.table.setRowCount(0)
        self.raw.clear()
        try:
            payload = read_smart(self.devices[row])
            rows = summarize(payload)
            self.table.setRowCount(len(rows))
            for index, (key, value) in enumerate(rows):
                self.table.setItem(index, 0, QTableWidgetItem(key))
                self.table.setItem(index, 1, QTableWidgetItem(value))

            diagnostics = payload.get("_diskrescue_diagnostics", {})
            self.raw.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))
            rc = diagnostics.get("return_code", "未知")
            self.status.setText(
                f"SMART 查询完成；smartctl 退出码：{rc}。"
                "如有字段不可用，请查看原始 JSON。"
            )
        except Exception as exc:
            self.raw.setPlainText(str(exc))
            QMessageBox.warning(self, "SMART 读取失败", str(exc))
            self.status.setText("读取失败；详细原因已显示在原始信息区域。")
        finally:
            self.smart_button.setEnabled(True)
