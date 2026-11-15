import json
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen, QBrush
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget, QGridLayout,
)

from .smartctl import Device, discover_devices, read_smart, summarize
from .disk_layout import get_disk_layout


def human_bytes(value):
    if not isinstance(value, (int, float)) or value < 0:
        return "未知"
    raw = int(value)
    decimal = float(raw)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if decimal < 1000 or unit == "PB":
            decimal_text = f"{decimal:,.2f} {unit}" if unit != "B" else f"{decimal:,.0f} B"
            break
        decimal /= 1000
    if raw >= 1_000_000_000:
        binary = raw / (1024 ** 3)
        return f"{decimal_text}（{binary:,.1f} GiB）"
    return decimal_text


def percent(part, total):
    if total <= 0:
        return 0.0
    return max(0.0, min(100.0, part * 100.0 / total))


class PartitionMap(QWidget):
    """One disk-wide map: each partition is sized by capacity and split into used/free."""
    COLORS = ["#3678d4", "#229b73", "#d49b32", "#8a70d6", "#cf6b91", "#319da0"]

    def __init__(self):
        super().__init__()
        self.parts = []
        self.total = 0
        self.setMinimumHeight(76)

    def set_data(self, parts, total):
        self.parts = parts or []
        self.total = max(int(total or 0), 0)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        outer = self.rect().adjusted(2, 5, -2, -5)
        if self.total <= 0:
            painter.drawText(outer, Qt.AlignmentFlag.AlignCenter, "暂无可用的分区容量信息")
            return

        bar = outer.adjusted(0, 18, 0, -20)
        x = bar.left()
        width_total = bar.width()
        allocated = 0
        for p in self.parts:
            size = max(int(p.get("Size") or 0), 0)
            if not size:
                continue
            allocated += size
            seg_w = max(1, round(width_total * size / self.total))
            seg_w = min(seg_w, bar.right() - x + 1)
            used = p.get("SizeUsed")
            free = p.get("SizeRemaining")
            if isinstance(used, (int, float)) and isinstance(free, (int, float)) and used + free > 0:
                used_w = round(seg_w * max(0, used) / (used + free))
                used_w = min(max(used_w, 0), seg_w)
                painter.fillRect(x, bar.top(), used_w, bar.height(), QColor(self.COLORS[len([q for q in self.parts[:self.parts.index(p)]]) % len(self.COLORS)]))
                painter.fillRect(x + used_w, bar.top(), seg_w - used_w, bar.height(), QColor("#cbd5e1"))
            else:
                painter.fillRect(x, bar.top(), seg_w, bar.height(), QColor("#7b8794"))
            x += seg_w
            if x > bar.right():
                break

        unallocated = max(self.total - allocated, 0)
        if unallocated:
            remain_w = max(0, bar.right() - x + 1)
            if remain_w:
                painter.fillRect(x, bar.top(), remain_w, bar.height(), QColor("#454d5a"))

        painter.setPen(QPen(QColor("#9aa4b2"), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(bar, 5, 5)

        painter.setPen(QColor(self.palette().color(self.foregroundRole())))
        painter.drawText(outer.left(), outer.top() + 13, f"磁盘总容量 {human_bytes(self.total)}")
        painter.drawText(outer.left(), outer.bottom() - 2, "彩色 = 已用    浅灰 = 可用    深灰 = 未分配 / 未知")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DiskRescue — 硬盘检测与数据救援")
        self.resize(1220, 900)
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
        self.device_list.setMinimumWidth(240)
        self.device_list.setMaximumWidth(340)
        self.device_list.currentRowChanged.connect(self.selection_changed)
        split.addWidget(self.device_list)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        self.status = QLabel("请扫描磁盘。选择左侧硬盘查看健康状态和空间使用。")
        right_layout.addWidget(self.status)

        health_frame = QFrame()
        health_frame.setFrameShape(QFrame.Shape.StyledPanel)
        health_layout = QVBoxLayout(health_frame)
        health_layout.addWidget(QLabel("硬盘健康概览"))
        self.health_grid = QGridLayout()
        self.health_labels = {}
        names = ["寿命已使用", "可用备用空间", "温度", "通电时间", "异常断电", "介质错误", "数据读取量", "数据写入量"]
        for i, name in enumerate(names):
            card = QFrame()
            card.setStyleSheet("QFrame{background:#f3f5f8;border:1px solid #d9dee7;border-radius:8px;}")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(10, 7, 10, 7)
            title = QLabel(name)
            title.setStyleSheet("color:#667085;border:0;background:transparent;")
            value = QLabel("—")
            value.setStyleSheet("font-size:18px;font-weight:600;color:#172b4d;border:0;background:transparent;")
            card_layout.addWidget(title)
            card_layout.addWidget(value)
            self.health_labels[name] = value
            self.health_grid.addWidget(card, i // 4, i % 4)
        health_layout.addLayout(self.health_grid)
        right_layout.addWidget(health_frame)

        partition_frame = QFrame()
        partition_frame.setFrameShape(QFrame.Shape.StyledPanel)
        partition_layout = QVBoxLayout(partition_frame)
        self.partition_title = QLabel("分区容量与空间使用")
        self.partition_title.setStyleSheet("font-weight:600;font-size:16px;")
        partition_layout.addWidget(self.partition_title)
        self.partition_map = PartitionMap()
        partition_layout.addWidget(self.partition_map)
        self.partition_legend = QLabel("选择左侧硬盘后显示分区容量和已用 / 可用空间")
        self.partition_legend.setWordWrap(True)
        partition_layout.addWidget(self.partition_legend)
        self.partition_table = QTableWidget(0, 6)
        self.partition_table.setHorizontalHeaderLabels(["分区", "卷标 / 文件系统", "分区容量", "已用", "可用", "使用率"])
        self.partition_table.horizontalHeader().setStretchLastSection(True)
        self.partition_table.setMaximumHeight(220)
        partition_layout.addWidget(self.partition_table)
        right_layout.addWidget(partition_frame)

        detail_frame = QFrame()
        detail_frame.setFrameShape(QFrame.Shape.StyledPanel)
        detail_layout = QVBoxLayout(detail_frame)
        detail_layout.addWidget(QLabel("设备详细信息与诊断"))
        self.details = QTableWidget(0, 2)
        self.details.setHorizontalHeaderLabels(["项目", "读取结果"])
        self.details.horizontalHeader().setStretchLastSection(True)
        self.details.setMaximumHeight(150)
        detail_layout.addWidget(self.details)
        self.raw = QTextEdit()
        self.raw.setReadOnly(True)
        self.raw.setPlaceholderText("原始 SMART JSON（用于排查设备兼容性）")
        self.raw.setMaximumHeight(100)
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
                cap = human_bytes(device.capacity_bytes)
                item = QListWidgetItem(f"{title}\n{cap}  ·  {device.protocol or device.device_type}")
                item.setToolTip(f"{device.name}\n序列号：{device.serial or '不可用'}")
                self.device_list.addItem(item)
            self.status.setText(f"识别到 {len(self.devices)} 个设备。左侧显示设备报告容量。")
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
                capacity_bytes=int(payload.get("user_capacity", {}).get("bytes") or device.capacity_bytes or 0),
            )
            self.current_layout = layout
            self._show_partitions(layout, payload)
            self.status.setText(f"读取完成：{device.model or device.name}。SMART 与分区信息均为只读查询。")
        except Exception as exc:
            QMessageBox.warning(self, "读取失败", str(exc))
            self.status.setText(f"读取失败：{exc}")
        finally:
            self.smart_button.setEnabled(True)

    def _show_partitions(self, layout, payload):
        if layout.get("error"):
            self.partition_title.setText("分区容量与空间使用（无法可靠对应）")
            self.partition_legend.setText(layout["error"])
            self.partition_map.set_data([], 0)
            self.partition_table.setRowCount(0)
            return

        parts = layout.get("partitions", [])
        windows_total = int(layout.get("disk_size") or 0)
        smart_total = int(payload.get("user_capacity", {}).get("bytes") or 0)
        # Windows Get-Disk is authoritative for partition geometry; SMART capacity is shown separately.
        total = windows_total or smart_total
        self.partition_title.setText(
            f"物理磁盘容量：{human_bytes(total)}（Windows 磁盘 {layout.get('disk_number')}；匹配方式：{layout.get('match_method')}）"
        )
        self.partition_map.set_data(parts, total)

        rows = []
        allocated = 0
        known_used = 0
        known_free = 0
        for part in parts:
            size = int(part.get("Size") or 0)
            allocated += size
            used = part.get("SizeUsed")
            free = part.get("SizeRemaining")
            if isinstance(used, (int, float)) and isinstance(free, (int, float)):
                known_used += int(used)
                known_free += int(free)
                usage = f"{percent(used, used + free):.1f}%"
            else:
                usage = "未挂载 / 未知"
            drive = str(part.get("DriveLetter") or "").strip()
            letter = f"{drive}:" if len(drive) == 1 else (drive or "无盘符")
            label = part.get("FileSystemLabel") or part.get("Type") or "分区"
            fs = part.get("FileSystem") or "未知文件系统"
            rows.append((letter, f"{label} / {fs}", human_bytes(size), human_bytes(used), human_bytes(free), usage))

        self.partition_table.setRowCount(len(rows))
        for r, values in enumerate(rows):
            for c, value in enumerate(values):
                self.partition_table.setItem(r, c, QTableWidgetItem(str(value)))

        unallocated = max(total - allocated, 0)
        smart_text = f"SMART 命名空间容量：{human_bytes(smart_total)}" if smart_total else ""
        self.partition_legend.setText(
            f"分区 {len(parts)} 个　已分配 {human_bytes(allocated)}　未分配 {human_bytes(unallocated)}\n"
            f"已用 {human_bytes(known_used)}　可用 {human_bytes(known_free)}　{smart_text}"
        )
