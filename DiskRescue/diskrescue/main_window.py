import json
from PySide6.QtCore import Qt, QRect, QThread, Signal, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QFont
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
    QPushButton, QProgressBar, QSplitter, QTableWidget, QTableWidgetItem,
    QTabWidget, QTextEdit, QVBoxLayout, QWidget, QGridLayout,
)

from .smartctl import Device, discover_devices, read_smart, summarize
from .disk_layout import get_disk_layout
from .hfsplus import inspect_hfsplus_partition


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


class DiscoveryWorker(QThread):
    devices_ready = Signal(object)
    failed = Signal(str)

    def run(self):
        try:
            self.devices_ready.emit(discover_devices())
        except Exception as exc:
            self.failed.emit(str(exc))


class DeviceReadWorker(QThread):
    data_ready = Signal(object, object, str, object)
    failed = Signal(str)

    def __init__(self, device, cached_payload=None, parent=None):
        super().__init__(parent)
        self.device = device
        self.cached_payload = cached_payload

    def run(self):
        try:
            payload = self.cached_payload
            smart_error = self.device.smart_error or ""
            if payload is None and self.device.smart_supported:
                try:
                    payload = read_smart(self.device)
                except Exception as exc:
                    smart_error = str(exc)
            elif payload is None:
                smart_error = smart_error or "该磁盘未提供可用的 SMART 直通接口。"

            layout = get_disk_layout(
                serial=(payload or {}).get("serial_number", self.device.serial),
                model=(payload or {}).get("model_name", self.device.model),
                capacity_bytes=int(
                    (payload or {}).get("user_capacity", {}).get("bytes")
                    or self.device.capacity_bytes
                    or 0
                ),
                disk_number=self.device.windows_disk_number,
            )
            hfsplus_reports = []
            if self.device.windows_disk_number is not None and not layout.get("error"):
                for partition in layout.get("partitions", []):
                    detected_fs = str(
                        partition.get("DetectedFileSystem")
                        or partition.get("FileSystem")
                        or ""
                    )
                    if detected_fs.startswith("Apple HFS+") or detected_fs.startswith("Apple HFSX"):
                        hfsplus_reports.append(
                            inspect_hfsplus_partition(
                                self.device.windows_disk_number,
                                int(partition.get("Offset") or 0),
                                int(partition.get("Size") or 0),
                            )
                        )
            self.data_ready.emit(payload, layout, smart_error, hfsplus_reports)
        except Exception as exc:
            self.failed.emit(str(exc))


class PartitionMap(QWidget):
    """Large disk map with partition letters/roles and used/free proportions."""
    COLORS = ["#3678d4", "#229b73", "#d49b32", "#8a70d6", "#cf6b91", "#319da0"]
    EFI_GUID = "C12A7328F81F11D2BA4B00A0C93EC93B"
    MSR_GUID = "E3C9E3160B5C4DB8817DF92DF00215AE"
    BASIC_GUID = "EBD0A0A2B9E5443387C068B6B72699C7"
    RECOVERY_GUID = "DE94BBA406D14D40A16ABFD50179D6AC"

    def __init__(self):
        super().__init__()
        self.parts = []
        self.total = 0
        self.setMinimumHeight(190)
        self.setSizePolicy(self.sizePolicy().horizontalPolicy(), self.sizePolicy().verticalPolicy())

    def set_data(self, parts, total):
        self.parts = parts or []
        self.total = max(int(total or 0), 0)
        self.update()

    @classmethod
    def partition_role(cls, part):
        drive = str(part.get("DriveLetter") or "").strip().upper()
        gpt = "".join(ch for ch in str(part.get("GptType") or "").upper() if ch.isalnum())
        mbr = str(part.get("MbrType") or "").strip().lower()
        # Get-Partition.Type is present in the user's verified Windows output:
        # "System", "Reserved" and "Basic". Use it as a reliable fallback when
        # the Storage provider omits optional GPT/boolean properties.
        part_type = str(part.get("Type") or "").strip().casefold()
        detected_type = str(part.get("PartitionTypeName") or "").strip()
        hidden = bool(part.get("IsHidden"))
        if detected_type and detected_type != "未知分区类型":
            return f"{detected_type}（隐藏）" if hidden else detected_type
        if gpt == cls.EFI_GUID or part_type == "system" or (part.get("IsSystem") and not drive):
            return "EFI 引导分区"
        if gpt == cls.MSR_GUID or part_type == "reserved":
            return "MSR 保留分区"
        if gpt == cls.RECOVERY_GUID or mbr in {"0x27", "27", "39"} or part_type == "recovery":
            return "Windows 恢复分区"
        if drive and (part.get("IsBoot") or part.get("IsSystem")):
            return "Windows 系统分区"
        if hidden or not drive:
            return "隐藏 / 无盘符分区"
        if drive:
            return "数据分区"
        return "未知分区类型"

    @classmethod
    def partition_name(cls, part):
        drive = str(part.get("DriveLetter") or "").strip().upper()
        role = cls.partition_role(part)
        if drive:
            return f"{drive}盘 · {role}"
        return f"无盘符 · {role}"

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        outer = self.rect().adjusted(2, 4, -2, -4)
        if self.total <= 0:
            painter.drawText(outer, Qt.AlignmentFlag.AlignCenter, "暂无可用的分区容量信息")
            return

        painter.setPen(QColor(self.palette().color(self.foregroundRole())))
        painter.drawText(outer.left(), outer.top() + 17, f"磁盘总容量：{human_bytes(self.total)}")

        # Full width; reduce the current R5 bar height by another 10% (76.5% * 0.90 = 68.85%).
        base_bar = outer.adjusted(0, 28, 0, -43)
        bar_height = max(int(base_bar.height() * 0.6885), 42)
        bar_height = min(bar_height, base_bar.height())
        bar_y = base_bar.top() + (base_bar.height() - bar_height) // 2
        bar = QRect(base_bar.left(), bar_y, base_bar.width(), bar_height)

        # Dark-gray base is the unallocated area; partition segments are painted over it.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#454d5a"))
        painter.drawRect(bar)

        x = bar.left()
        width_total = bar.width()
        for index, part in enumerate(self.parts):
            size = max(int(part.get("Size") or 0), 0)
            if not size:
                continue
            seg_w = max(1, round(width_total * size / self.total))
            seg_w = min(seg_w, max(0, bar.right() - x + 1))
            if seg_w <= 0:
                break

            used = part.get("SizeUsed")
            free = part.get("SizeRemaining")
            known = (
                isinstance(used, (int, float))
                and isinstance(free, (int, float))
                and used >= 0
                and free >= 0
                and used + free > 0
            )
            if known:
                used_w = min(max(round(seg_w * used / (used + free)), 0), seg_w)
                # Color = used/allocated portion; white = free/usable portion.
                painter.fillRect(
                    x, bar.top(), used_w, bar.height(),
                    QColor(self.COLORS[index % len(self.COLORS)])
                )
                painter.fillRect(
                    x + used_w, bar.top(), seg_w - used_w, bar.height(),
                    QColor("#FFFFFF")
                )
                if 0 < used_w < seg_w:
                    painter.setPen(QPen(QColor("#606873"), 1))
                    painter.drawLine(x + used_w, bar.top(), x + used_w, bar.bottom())
            else:
                # Light gray = partition found, but used/free split unavailable.
                painter.fillRect(x, bar.top(), seg_w, bar.height(), QColor("#B8BEC7"))

            # A visible separator distinguishes adjacent partitions and white free space.
            painter.setPen(QPen(QColor("#505866"), 1))
            painter.drawLine(x, bar.top(), x, bar.bottom())

            drive = str(part.get("DriveLetter") or "").strip().upper()
            if drive and seg_w >= 72:
                label = f"{drive}:"
                label_font = QFont(painter.font())
                label_font.setPointSize(17)
                label_font.setBold(True)
                painter.setFont(label_font)
                painter.setPen(QColor("#000000"))
                painter.drawText(
                    QRect(x + 2, bar.top(), max(seg_w - 4, 1), bar.height()),
                    Qt.AlignmentFlag.AlignCenter,
                    label,
                )

            x += seg_w
            if x > bar.right():
                break

        # Draw a dark outline around the full-width map.
        painter.setPen(QPen(QColor("#39414c"), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(bar)

        # Use visible color swatches instead of a low-contrast plain-text legend.
        legend_font = QFont(painter.font())
        legend_font.setPointSize(9)
        legend_font.setBold(False)
        painter.setFont(legend_font)
        fm = painter.fontMetrics()
        legend_y = bar.bottom() + 8
        legend_items = [
            (self.COLORS[0], "彩色：已分配 / 已用", False),
            ("#FFFFFF", "白色：可使用", True),
            ("#454d5a", "深灰：未分配", False),
            ("#B8BEC7", "浅灰：未知", False),
        ]
        legend_x = outer.left()
        for color, label, needs_outline in legend_items:
            swatch = QRect(legend_x, legend_y, 12, 12)
            painter.fillRect(swatch, QColor(color))
            painter.setPen(QPen(QColor("#596273" if needs_outline or color == "#454d5a" else color), 1))
            painter.drawRect(swatch)
            painter.setPen(QColor(self.palette().color(self.foregroundRole())))
            painter.drawText(legend_x + 17, legend_y + fm.ascent(), label)
            legend_x += 17 + fm.horizontalAdvance(label) + 20

        painter.setPen(QColor(self.palette().color(self.foregroundRole())))
        painter.drawText(
            outer.left(), outer.bottom() - 2,
            "分区标签见下方列表；无盘符分区会标注 EFI、恢复、MSR 保留或隐藏属性"
        )


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DiskRescue — 硬盘检测与数据救援 | HFS+ 目录读取 R10")
        self.resize(1220, 900)
        self.devices: list[Device] = []
        self.current_payload = None
        self.current_layout = None
        self._discovery_thread = None
        self._read_thread = None
        self._reading_device = None

        central = QWidget()
        root = QVBoxLayout(central)
        banner = QLabel("安全模式：只读磁盘身份、分区表及文件系统签名，不会修改磁盘数据；读取隐藏分区扇区时需要管理员权限。")
        banner.setWordWrap(True)
        banner.setStyleSheet("padding:9px 12px;background:#e8f2ff;color:#17365d;border-radius:6px;")
        root.addWidget(banner)

        actions = QHBoxLayout()
        self.scan_button = QPushButton("扫描磁盘")
        self.scan_button.clicked.connect(self.scan)
        actions.addWidget(self.scan_button)
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
        self.status = QLabel("正在自动识别磁盘……")
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
        self.partition_table.setHorizontalHeaderLabels(["盘符 / 分区类型", "卷标 / 文件系统识别", "分区容量", "已用", "可用", "使用率"])
        self.partition_table.horizontalHeader().setStretchLastSection(True)
        self.partition_table.setColumnWidth(0, 230)
        self.partition_table.setColumnWidth(1, 170)
        self.partition_table.setColumnWidth(2, 125)
        self.partition_table.setColumnWidth(3, 125)
        self.partition_table.setColumnWidth(4, 125)
        self.partition_table.setMaximumHeight(300)
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
        self.detail_tabs = QTabWidget()

        raw_tab = QWidget()
        raw_tab_layout = QVBoxLayout(raw_tab)
        raw_tab_layout.setContentsMargins(4, 4, 4, 4)
        self.raw = QTextEdit()
        self.raw.setReadOnly(True)
        self.raw.setPlaceholderText("原始 SMART JSON（用于排查设备兼容性）")
        raw_tab_layout.addWidget(self.raw)
        self.detail_tabs.addTab(raw_tab, "SMART 原始数据")

        hfs_tab = QWidget()
        hfs_tab_layout = QVBoxLayout(hfs_tab)
        hfs_tab_layout.setContentsMargins(4, 4, 4, 4)
        self.hfs_summary = QLabel("当前选中磁盘未发现 HFS+ 分区。")
        self.hfs_summary.setWordWrap(True)
        hfs_tab_layout.addWidget(self.hfs_summary)
        self.hfs_table = QTableWidget(0, 5)
        self.hfs_table.setHorizontalHeaderLabels(["类型", "名称", "CNID", "大小 / 子项", "完整路径"])
        self.hfs_table.horizontalHeader().setStretchLastSection(True)
        self.hfs_table.setColumnWidth(0, 70)
        self.hfs_table.setColumnWidth(1, 180)
        self.hfs_table.setColumnWidth(2, 90)
        self.hfs_table.setColumnWidth(3, 120)
        self.hfs_table.setMaximumHeight(165)
        hfs_tab_layout.addWidget(self.hfs_table)
        self.detail_tabs.addTab(hfs_tab, "HFS+ / HFSX 目录预览")
        self.detail_tabs.setMaximumHeight(215)
        detail_layout.addWidget(self.detail_tabs)

        right_layout.addWidget(detail_frame)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        root.addWidget(split)
        self.setCentralWidget(central)
        self.statusBar().showMessage("DiskRescue 开发版 · HFS+ 卷头 / 分配位图 / Catalog B-tree 只读读取 R10")
        QTimer.singleShot(0, self.scan)

    def scan(self):
        """Discover disks automatically in a worker thread; the UI remains responsive."""
        if self._discovery_thread is not None and self._discovery_thread.isRunning():
            return
        if self._read_thread is not None and self._read_thread.isRunning():
            self.status.setText("当前磁盘正在读取，请等待完成后再重新扫描。")
            return

        self.scan_button.setEnabled(False)
        self.devices = []
        self.device_list.blockSignals(True)
        self.device_list.clear()
        self.device_list.blockSignals(False)
        self.device_list.setEnabled(False)
        self._clear_views()
        self.status.setText("正在扫描 Windows 物理磁盘并检测 SMART……")
        self.statusBar().showMessage("正在识别磁盘，请稍候……")

        self._discovery_thread = DiscoveryWorker(self)
        self._discovery_thread.devices_ready.connect(self._on_devices_found)
        self._discovery_thread.failed.connect(self._on_discovery_failed)
        self._discovery_thread.finished.connect(self._on_discovery_thread_finished)
        self._discovery_thread.finished.connect(self._discovery_thread.deleteLater)
        self._discovery_thread.start()

    def _on_devices_found(self, devices):
        self.devices = devices or []
        self.scan_button.setEnabled(True)
        self.device_list.blockSignals(True)
        self.device_list.clear()
        for device in self.devices:
            title = device.model or device.name or "未知型号"
            cap = human_bytes(device.capacity_bytes)
            disk_number = (
                f"磁盘 {device.windows_disk_number}"
                if device.windows_disk_number is not None else "SMART 独立设备"
            )
            table_style = device.partition_style or "分区表未知"
            bus = device.bus_type or device.protocol or device.device_type or "总线未知"
            smart_label = "SMART 可用" if device.smart_supported else "SMART 不可用"
            item = QListWidgetItem(
                f"{disk_number} · {title}\n{cap}  ·  {table_style}  ·  {bus}  ·  {smart_label}"
            )
            item.setToolTip(
                f"{device.name}\n序列号：{device.serial or '不可用'}"
                f"\n设备状态：{device.operational_status or '未知'}"
                f"\n离线：{device.is_offline}；只读：{device.is_read_only}"
                f"\nSMART：{device.smart_error or '可用'}"
            )
            self.device_list.addItem(item)
        self.device_list.blockSignals(False)
        self.device_list.setEnabled(True)

        if not self.devices:
            self.status.setText("没有发现 Windows 可枚举的物理磁盘。请检查硬盘连接和存储控制器驱动。")
            self.statusBar().showMessage("未发现物理磁盘")
            return

        self.status.setText(f"已识别 {len(self.devices)} 个磁盘。正在自动读取默认磁盘。")
        self.device_list.blockSignals(True)
        self.device_list.setCurrentRow(0)
        self.device_list.blockSignals(False)
        self.selection_changed(0)

    def _on_discovery_failed(self, message):
        self.scan_button.setEnabled(True)
        self.device_list.setEnabled(True)
        self.status.setText(f"磁盘识别失败：{message}")
        self.statusBar().showMessage("磁盘识别失败")

    def _on_discovery_thread_finished(self):
        self._discovery_thread = None

    def selection_changed(self, row):
        if row < 0 or row >= len(self.devices):
            return
        if self._read_thread is not None and self._read_thread.isRunning():
            return

        device = self.devices[row]
        self._reading_device = device
        self.device_list.setEnabled(False)
        self._clear_views()
        self.status.setText(f"正在读取 {device.model or device.name} 的健康和分区信息……")
        self.statusBar().showMessage(f"正在读取：{device.model or device.name}")

        # Discovery already queried SMART to obtain each disk's identity.
        # Reuse that first result once, avoiding a duplicate SMART command on initial selection.
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
        payload = payload or {}
        self.current_payload = payload or None

        rows = summarize(payload) if payload else []
        rows.extend([
            ("Windows 磁盘编号", str(device.windows_disk_number) if device.windows_disk_number is not None else "未关联"),
            ("分区表（Windows）", device.partition_style or "未知"),
            ("总线类型", device.bus_type or device.protocol or "未知"),
            ("设备状态", device.operational_status or "未知"),
            ("是否离线", "是" if device.is_offline else "否"),
            ("是否只读", "是" if device.is_read_only else "否"),
            ("SMART 读取状态", "已读取" if payload else "不可用 / 未支持"),
        ])
        self.details.setRowCount(len(rows))
        for index, (key, value) in enumerate(rows):
            self.details.setItem(index, 0, QTableWidgetItem(key))
            self.details.setItem(index, 1, QTableWidgetItem(value))

        log = payload.get("nvme_smart_health_information_log", {})
        if payload:
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
            self.raw.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            metrics = {name: "SMART 不支持 / 不可用" for name in self.health_labels}
            self.raw.setPlainText(
                "该物理磁盘已由 Windows 识别，但没有取得 SMART 数据。\n"
                f"原因：{smart_error or '设备或 USB 转接桥未提供 SMART 直通'}\n\n"
                "分区表和文件系统识别会独立进行，不会因为 SMART 不可用而跳过磁盘。"
            )
        for name, value in metrics.items():
            self.health_labels[name].setText(value)

        self.current_layout = layout
        self._show_partitions(layout, payload)
        self._show_hfsplus_reports(hfsplus_reports)
        device_name = device.model or device.name
        if payload:
            status = f"读取完成：{device_name}。SMART 与分区检测均为只读。"
        else:
            status = f"已读取 {device_name} 的分区信息；SMART 不可用：{smart_error or '设备不支持'}"
        self.status.setText(status)
        self.statusBar().showMessage(status)

    def _show_hfsplus_reports(self, reports):
        reports = reports or []
        self.hfs_table.setRowCount(0)
        if not reports:
            self.hfs_summary.setText("当前选中磁盘未发现可读取的 HFS+ / HFSX 分区。")
            return

        rows = []
        summaries = []
        for report_index, report in enumerate(reports, start=1):
            if report.get("error"):
                summaries.append(f"分区 {report_index}：读取失败：{report['error']}")
                continue

            catalog = report.get("catalog", {})
            volume_name = catalog.get("volume_name") or "未命名卷"
            free_bytes = report.get("free_bytes")
            total_bytes = report.get("total_bytes")
            summaries.append(
                f"{volume_name} · {report.get('filesystem')} v{report.get('version')} · "
                f"文件 {report.get('file_count', 0):,} 个 / 文件夹 {report.get('folder_count', 0):,} 个 · "
                f"已列出 {catalog.get('entries_returned', 0):,} 项 · "
                f"空闲 {human_bytes(free_bytes)} / {human_bytes(total_bytes)}"
            )
            for warning in report.get("warnings", []):
                summaries.append(f"提示：{warning}")

            for entry in catalog.get("entries", []):
                if len(rows) >= 500:
                    break
                if entry.get("kind") == "文件":
                    size_text = human_bytes(entry.get("size"))
                else:
                    size_text = f"{int(entry.get('child_count') or 0):,} 项"
                rows.append((
                    str(entry.get("kind") or ""),
                    str(entry.get("name") or ""),
                    str(entry.get("id") or ""),
                    size_text,
                    str(entry.get("path") or ""),
                ))

        self.hfs_summary.setText("\n".join(summaries) if summaries else "HFS+ 分区未能读取。")
        self.hfs_table.setRowCount(len(rows))
        for row_index, values in enumerate(rows):
            for column, value in enumerate(values):
                self.hfs_table.setItem(row_index, column, QTableWidgetItem(value))
            self.hfs_table.setRowHeight(row_index, 28)

    def _on_device_read_failed(self, message):
        self.status.setText(f"读取失败：{message}")
        self.statusBar().showMessage("磁盘信息读取失败")
        self.raw.setPlainText(message)

    def _on_read_thread_finished(self):
        self.device_list.setEnabled(True)
        self._read_thread = None
        self._reading_device = None

    def _clear_views(self):
        self.current_payload = None
        self.current_layout = None
        self.details.setRowCount(0)
        self.partition_table.setRowCount(0)
        self.partition_map.set_data([], 0)
        self.partition_legend.setText("正在读取所选磁盘的分区信息……")
        self.raw.clear()
        self.hfs_summary.setText("当前选中磁盘未发现可读取的 HFS+ / HFSX 分区。")
        self.hfs_table.setRowCount(0)
        for label in self.health_labels.values():
            label.setText("—")

    def _show_partitions(self, layout, payload):
        payload = payload or {}
        if layout.get("error"):
            self.partition_title.setText("分区容量与空间使用（无法可靠对应）")
            self.partition_legend.setText(layout["error"])
            self.partition_map.set_data([], 0)
            self.partition_table.setRowCount(0)
            return

        parts = layout.get("partitions", [])
        windows_total = int(layout.get("disk_size") or 0)
        smart_total = int(payload.get("user_capacity", {}).get("bytes") or 0)
        total = windows_total or smart_total
        table_style = layout.get("partition_table_style") or layout.get("partition_style") or "未知"
        table_source = layout.get("partition_table_source") or "未知来源"
        self.partition_title.setText(
            f"物理磁盘容量：{human_bytes(total)}（Windows 磁盘 {layout.get('disk_number')}；"
            f"分区表：{table_style}；识别来源：{table_source}）"
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
            letter = PartitionMap.partition_name(part)
            partition_type = part.get("PartitionTypeName") or part.get("Type") or "未知分区类型"
            label = part.get("FileSystemLabel") or part.get("PartitionName") or partition_type
            fs = part.get("DetectedFileSystem") or part.get("FileSystem") or "未识别 / 未挂载"
            fs_source = part.get("FileSystemSource") or ""
            fs_detail = f"{label} / {fs}" + (f"（{fs_source}）" if fs_source and fs_source != "Windows Get-Volume" else "")
            rows.append((letter, fs_detail, human_bytes(size), human_bytes(used), human_bytes(free), usage))

        self.partition_table.setRowCount(len(rows))
        for r, values in enumerate(rows):
            for col, value in enumerate(values):
                if col == 5:
                    part = parts[r]
                    used = part.get("SizeUsed")
                    free = part.get("SizeRemaining")
                    if isinstance(used, (int, float)) and isinstance(free, (int, float)) and used + free > 0:
                        bar = QProgressBar()
                        bar.setRange(0, 1000)
                        ratio = percent(used, used + free)
                        bar.setValue(round(ratio * 10))
                        bar.setFormat(f"{ratio:.1f}% 已用")
                        bar.setTextVisible(True)
                        bar.setStyleSheet(
                            "QProgressBar{border:1px solid #8993a2;border-radius:5px;text-align:center;}"
                            "QProgressBar::chunk{background:#397fd6;border-radius:4px;}"
                        )
                        self.partition_table.setCellWidget(r, col, bar)
                    else:
                        self.partition_table.setItem(r, col, QTableWidgetItem("未知 / 未挂载"))
                else:
                    self.partition_table.setItem(r, col, QTableWidgetItem(str(value)))
            self.partition_table.setRowHeight(r, 36)

        unallocated = max(total - allocated, 0)
        smart_text = f"SMART 命名空间容量：{human_bytes(smart_total)}" if smart_total else ""
        recognized_fs = sum(
            1 for part in parts
            if (part.get("DetectedFileSystem") or "").strip()
            and "未识别" not in str(part.get("DetectedFileSystem"))
        )
        raw_warning = str(layout.get("raw_read_warning") or "").strip()
        warning_text = (
            f"\n原始扇区提示：{raw_warning}；部分隐藏分区的文件系统可能只能按分区类型推断。"
            if raw_warning else ""
        )
        partition_warning = str(layout.get("partition_warning") or "").strip()
        volume_warning = str(layout.get("volume_warning") or "").strip()
        detail_warnings = "；".join(value for value in (partition_warning, volume_warning) if value)
        if detail_warnings:
            warning_text += f"\nWindows 分区/卷信息提示：{detail_warnings}"
        self.partition_legend.setText(
            f"分区表：{table_style}（{table_source}）　分区 {len(parts)} 个　"
            f"已识别文件系统 {recognized_fs} 个\n"
            f"已分配 {human_bytes(allocated)}　未分配 {human_bytes(unallocated)}　"
            f"已用 {human_bytes(known_used)}　可用 {human_bytes(known_free)}　{smart_text}"
            f"{warning_text}"
        )
