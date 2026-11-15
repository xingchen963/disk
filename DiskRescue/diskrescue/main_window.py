import json
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen, QBrush
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QPushButton, QProgressBar, QSplitter, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget, QGridLayout,
)

from .smartctl import Device, SmartctlError, discover_devices, read_smart, summarize
from .disk_layout import get_disk_layout


def human_bytes(value):
    if not isinstance(value, (int, float)):
        return "未知"
    amount = float(value)
    for unit in ("B", "KB", "GB", "TB", "PB"):
        if amount < 1000 or unit == "PB":
            return f"{amount:,.1f} {unit}" if unit != "B" else f"{amount:,.0f} B"
        amount /= 1000
    return f"{amount:,.1f} PB"


class PartitionMap(QWidget):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.total = 0
        self.setMinimumHeight(54)

    def set_data(self, parts, total):
        self.parts = parts or []
        self.total = max(int(total or 0), 0)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(2, 8, -2, -8)
        painter.setPen(QPen(QColor("#667085"), 1))
        painter.setBrush(QBrush(QColor("#252a34")))
        painter.drawRoundedRect(rect, 7, 7)
        if self.total <= 0:
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "暂无分区布局信息")
            return
        x = rect.left()
        usable = rect.width()
        palette = ["#4f8cff", "#22a06b", "#e5a000", "#8b70d6", "#d16b8f", "#4ba6a6"]
        for index, part in enumerate(self.parts):
            size = max(int(part.get("Size") or 0), 0)
            if size <= 0:
                continue
            width = max(1, round(usable * size / self.total))
            color = QColor(palette[index % len(palette)])
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(color))
            painter.drawRect(x, rect.top() + 1, min(width, rect.right() - x + 1), rect.height() - 2)
            x += width
        painter.setPen(QPen(QColor("#aab4c3"), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, 7, 7)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DiskRescue — 硬盘检测与数据救援")
        self.resize(1180, 820)
        self.devices: list[Device] = []
        self.current_payload = None
        self.current_layout = None

        central = QWidget()
        root = QVBoxLayout(central)
        banner = QLabel("安全模式：当前版本仅读取磁盘身份、SMART 和分区信息，不会修改磁盘数据。")
        banner.setWordWrap(True)
        banner.setStyleSheet("padding:9px 12px;background:#e8f2ff;color:#17365d;border-radius:6px;")
        root.addWidget(banner)

        actions = QHBoxLayout()
        self.scan_button = QPushButton("扫描磁盘")
        self.scan_button.clicked.connect(self.scan)
        self.smart_button = QPushButton("刷新健康信息")
        self.smart_button.clicked.connect(self.show_smart)
        self.smart_button.setEnabled(False)
        actions.addWidget(self.scan_button)
        actions.addWidget(self.smart_button)
        actions.addStretch(1)
        root.addLayout(actions)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        self.device_list = QListWidget()
        self.device_list.setMinimumWidth(230)
        self.device_list.setMaximumWidth(330)
        self.device_list.currentRowChanged.connect(self.selection_changed)
        split.addWidget(self.device_list)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        self.status = QLabel("请扫描磁盘。选择左侧硬盘查看健康状态和分区使用情况。")
        right_layout.addWidget(self.status)

        health_frame = QFrame()
        health_frame.setFrameShape(QFrame.Shape.StyledPanel)
        health_layout = QVBoxLayout(health_frame)
        health_title = QLabel("硬盘健康概览")
        health_title.setStyleSheet("font-weight:600;font-size:16px;")
        health_layout.addWidget(health_title)
        self.health_grid = QGridLayout()
        self.health_grid.setHorizontalSpacing(16)
        self.health_grid.setVerticalSpacing(8)
        self.health_labels = {}
        metric_names = [
            ("寿命已使用", "—"), ("可用备用空间", "—"),
            ("温度", "—"), ("通电时间", "—"),
            ("异常断电", "—"), ("介质错误", "—"),
            ("数据读取量", "—"), ("数据写入量", "—"),
        ]
        for index, (name, initial) in enumerate(metric_names):
            card = QFrame()
            card.setStyleSheet("QFrame{background:#f3f5f8;border:1px solid #d9dee7;border-radius:8px;}")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(10, 7, 10, 7)
            name_label = QLabel(name)
            name_label.setStyleSheet("color:#667085;border:0;background:transparent;")
            value_label = QLabel(initial)
            value_label.setStyleSheet("font-size:18px;font-weight:600;color:#172b4d;border:0;background:transparent;")
            card_layout.addWidget(name_label)
            card_layout.addWidget(value_label)
            self.health_labels[name] = value_label
            self.health_grid.addWidget(card, index // 4, index % 4)
        health_layout.addLayout(self.health_grid)
        right_layout.addWidget(health_frame)

        partition_frame = QFrame()
        partition_frame.setFrameShape(QFrame.Shape.StyledPanel)
        partition_layout = QVBoxLayout(partition_frame)
        self.partition_title = QLabel("分区与空间使用")
        self.partition_title.setStyleSheet("font-weight:600;font-size:16px;")
        partition_layout.addWidget(self.partition_title)
        self.partition_map = PartitionMap()
        partition_layout.addWidget(self.partition_map)
        self.partition_legend = QLabel("选择左侧硬盘后显示分区")
        self.partition_legend.setWordWrap(True)
        partition_layout.addWidget(self.partition_legend)
        self.partition_table = QTableWidget(0, 5)
        self.partition_table.setHorizontalHeaderLabels(["分区", "卷标 / 文件系统", "容量", "已用", "可用"])
        self.partition_table.horizontalHeader().setStretchLastSection(True)
        self.partition_table.setMaximumHeight(190)
        partition_layout.addWidget(self.partition_table)
        right_layout.addWidget(partition_frame)

        detail_frame = QFrame()
        detail_frame.setFrameShape(QFrame.Shape.StyledPanel)
        detail_layout = QVBoxLayout(detail_frame)
        detail_title = QLabel("设备详细信息与诊断")
        detail_title.setStyleSheet("font-weight:600;font-size:15px;")
        detail_layout.addWidget(detail_title)
        self.details = QTableWidget(0, 2)
        self.details.setHorizontalHeaderLabels(["项目", "读取结果"])
        self.details.horizontalHeader().setStretchLastSection(True)
        self.details.setMaximumHeight(180)
        detail_layout.addWidget(self.details)
        self.raw = QTextEdit()
        self.raw.setReadOnly(True)
        self.raw.setPlaceholderText("原始 SMART JSON（用于排查设备兼容性）")
        self.raw.setMaximumHeight(140)
        detail_layout.addWidget(self.raw)
        right_layout.addWidget(detail_frame)

        split.addWidget(right)
        split.setStretchFactor(1, 1)
        root.addWidget(split)
        self.setCentralWidget(central)
        self.statusBar().showMessage("DiskRescue 开发版")

    def selection_changed(self, row):
        valid = 0 <= row < len(self.devices)
        self.smart_button.setEnabled(valid)
        if valid:
            self.show_smart()

    def scan(self):
        self.scan_button.setEnabled(False)
        self.status.setText("正在识别硬盘并读取设备型号……")
        self.device_list.clear()
        self._clear_views()
        try:
            self.devices = discover_devices()
            for device in self.devices:
                title = device.model or "未知型号"
                capacity = human_bytes(device.capacity_bytes)
                item = QListWidgetItem(f"{title}\n{capacity}  ·  {device.protocol or device.device_type}")
                item.setToolTip(f"{device.name}\n序列号：{device.serial or '不可用'}")
                self.device_list.addItem(item)
            self.status.setText(f"识别到 {len(self.devices)} 个设备。左侧显示型号和容量。")
            if self.devices:
                self.device_list.setCurrentRow(0)
            else:
                self.status.setText("未发现支持 SMART 查询的设备。")
        except Exception as exc:
            QMessageBox.warning(self, "设备扫描", str(exc))
            self.status.setText("扫描未完成。")
        finally:
            self.scan_button.setEnabled(True)

    def _clear_views(self):
        self.current_payload = None
        self.current_layout = None
        self.details.setRowCount(0)
        self.partition_table.setRowCount(0)
        self.partition_map.set_data([], 0)
        self.partition_legend.setText("选择左侧硬盘后显示分区")
        self.raw.clear()
        for label in self.health_labels.values():
            label.setText("—")

    def show_smart(self):
        row = self.device_list.currentRow()
        if row < 0 or row >= len(self.devices):
            return
        device = self.devices[row]
        self.smart_button.setEnabled(False)
        self.status.setText(f"正在读取 {device.model or device.name} 的健康和分区信息……")
        try:
            payload = read_smart(device)
            self.current_payload = payload
            rows = summarize(payload)
            self.details.setRowCount(len(rows))
            for index, (key, value) in enumerate(rows):
                self.details.setItem(index, 0, QTableWidgetItem(key))
                self.details.setItem(index, 1, QTableWidgetItem(value))

            log = payload.get("nvme_smart_health_information_log", {})
            temp = payload.get("temperature", {}).get("current", log.get("temperature"))
            power = payload.get("power_on_time", {}).get("hours", log.get("power_on_hours"))
            read_units = log.get("data_units_read")
            write_units = log.get("data_units_written")
            metrics = {
                "寿命已使用": f"{log['percentage_used']}%" if "percentage_used" in log else "设备未提供",
                "可用备用空间": f"{log['available_spare']}%" if "available_spare" in log else "设备未提供",
                "温度": f"{temp} °C" if temp is not None else "设备未提供",
                "通电时间": f"{power:,} 小时" if isinstance(power, (int, float)) else "设备未提供",
                "异常断电": f"{log['unsafe_shutdowns']:,} 次" if "unsafe_shutdowns" in log else "设备未提供",
                "介质错误": f"{log['media_errors']:,} 次" if "media_errors" in log else "设备未提供",
                "数据读取量": human_bytes(read_units * 512000) if isinstance(read_units, (int, float)) else "设备未提供",
                "数据写入量": human_bytes(write_units * 512000) if isinstance(write_units, (int, float)) else "设备未提供",
            }
            for name, value in metrics.items():
                self.health_labels[name].setText(value)

            self.raw.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))
            layout = get_disk_layout(
                serial=payload.get("serial_number", device.serial),
                model=payload.get("model_name", device.model),
                capacity_bytes=payload.get("user_capacity", {}).get("bytes", device.capacity_bytes),
            )
            self.current_layout = layout
            self._show_partitions(layout)
            self.status.setText(
                f"读取完成：{device.model or device.name}。SMART 与分区信息均为只读查询。"
            )
        except Exception as exc:
            QMessageBox.warning(self, "读取失败", str(exc))
            self.status.setText(f"读取失败：{exc}")
        finally:
            self.smart_button.setEnabled(True)

    def _show_partitions(self, layout):
        if layout.get("error"):
            self.partition_title.setText("分区与空间使用（无法可靠对应）")
            self.partition_legend.setText(layout["error"])
            self.partition_map.set_data([], 0)
            self.partition_table.setRowCount(0)
            return

        parts = layout.get("partitions", [])
        total = int(layout.get("disk_size") or 0)
        self.partition_title.setText(
            f"分区与空间使用 · Windows 磁盘 {layout.get('disk_number')} · {layout.get('partition_style') or '未知分区表'}"
        )
        self.partition_map.set_data(parts, total)
        rows = []
        used_sum = 0
        for part in parts:
            size = int(part.get("Size") or 0)
            used = part.get("SizeUsed")
            free = part.get("SizeRemaining")
            if isinstance(used, (int, float)):
                used_sum += int(used)
            letter = f"{part.get('DriveLetter')}:\\" if part.get("DriveLetter") else "无盘符"
            label = part.get("FileSystemLabel") or part.get("Type") or "分区"
            fs = part.get("FileSystem") or "未知文件系统"
            rows.append((letter, f"{label} / {fs}", human_bytes(size), human_bytes(used), human_bytes(free)))
        self.partition_table.setRowCount(len(rows))
        for r, values in enumerate(rows):
            for c, value in enumerate(values):
                self.partition_table.setItem(r, c, QTableWidgetItem(str(value)))
        free_disk = max(total - sum(int(p.get("Size") or 0) for p in parts), 0)
        self.partition_legend.setText(
            f"磁盘总容量：{human_bytes(total)}　分区：{len(parts)} 个　"
            f"已分配：{human_bytes(sum(int(p.get('Size') or 0) for p in parts))}　"
            f"未分配：{human_bytes(free_disk)}"
        )
