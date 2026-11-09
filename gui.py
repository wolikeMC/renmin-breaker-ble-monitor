"""gui.py — PySide6 主设置窗口。

界面上只做三件事：
    1. 维护设备列表（添加/删除/扫描）；
    2. 维护设置（开机自启、托盘显示指标、轮询间隔）；
    3. 展示后台通过 Qt Signal 送来的状态与解密后的数据。

线程原则：本文件不直接调用 bleak，也不自己起线程；所有蓝牙数据都由
main.py 的 BleBridge 转成 Qt Signal 后，在 GUI 主线程更新控件。
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from PySide6.QtCore import QSignalBlocker, Qt, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QFont, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import utils

# 状态文本 / 颜色：黄=连接中，绿=正常，红=断开
STATUS_TEXT = {
    "connecting": "连接中",
    "connected": "已连接",
    "reconnecting": "重连中",
    "disconnected": "断线",
    "paused": "已断开",       # 用户手动断开，释放给手机小程序
    "error": "错误",
}
STATUS_COLORS = {
    "connecting": "#F9A825",   # 琥珀黄
    "connected": "#2E7D32",    # 绿
    "reconnecting": "#C62828",  # 红
    "disconnected": "#C62828",
    "paused": "#757575",       # 灰：人为断开，不是故障
    "error": "#B71C1C",
}

# 表格列定义
COL_NAME = 0
COL_ADDRESS = 1
COL_STATUS = 2
COL_METRIC_START = 3
COL_UPDATED = COL_METRIC_START + len(utils.METRIC_ORDER)
COLUMN_COUNT = COL_UPDATED + 1


class MainWindow(QMainWindow):
    """主窗口。

    信号：
        config_changed(dict) : 配置发生变化，main.py 用来重设蓝牙设备/托盘
        scan_requested()     : 用户点击扫描，main.py 转给 BleManager
        hidden_to_tray()     : 首次/每次点击 X 隐藏时通知托盘
        show_requested()     : 托盘菜单请求显示窗口
        quit_requested()     : 托盘菜单请求退出程序
    """

    config_changed = Signal(dict)
    scan_requested = Signal()
    hidden_to_tray = Signal()
    show_requested = Signal()
    quit_requested = Signal()

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__()
        self.config = config

        # 运行期缓存，key 均为规范化后的 MAC 地址
        self._statuses: dict[str, str] = {}
        self._status_messages: dict[str, str] = {}
        self._latest: dict[str, dict[str, float]] = {}
        self._last_seen: dict[str, str] = {}
        self._row_by_address: dict[str, int] = {}
        self._name_by_address: dict[str, str] = {}
        # 用户手动断开的设备地址：不连接，但保留在设备列表里
        device_addresses = {
            device["address"] for device in self.config.get("devices", [])
        }
        self._paused: set[str] = {
            utils.normalize_address(address)
            for address in self.config.get("paused_devices", [])
            if utils.normalize_address(address) in device_addresses
        }

        self._scan_results: list[dict[str, Any]] = []
        self._scanning = False
        self._tray_available = True   # main.py 创建托盘后会确认
        self._force_close = False

        self._build_ui()
        self._load_from_config()
        self._connect_signals()

        self._append_log("程序已启动。请扫描并添加 YUNM 插座。")
        if not self.config["devices"]:
            self.statusBar().showMessage("尚未添加设备：点击“扫描附近设备”开始")

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.setWindowTitle(utils.APP_DISPLAY_NAME)
        self.resize(1060, 760)
        self.setMinimumSize(900, 620)

        icon_file = utils.resource_path("app.ico")
        if os.path.isfile(icon_file):
            self.setWindowIcon(QIcon(icon_file))

        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 10)
        root.setSpacing(10)

        # 顶部标题 + 汇总
        header = QHBoxLayout()
        title = QLabel("YUNM 蓝牙计量插座监控")
        title_font = QFont()
        title_font.setPointSize(16)
        title_font.setBold(True)
        title.setFont(title_font)
        self.summary_label = QLabel("已连接 0 / 0")
        self.summary_label.setStyleSheet("color: #616161;")
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.summary_label)
        root.addLayout(header)

        # 设备表格
        device_group = QGroupBox("设备列表")
        device_layout = QVBoxLayout(device_group)

        headers = ["名称", "蓝牙地址", "状态"]
        for key in utils.METRIC_ORDER:
            meta = utils.METRIC_DEFS[key]
            unit = meta.get("unit", "")
            headers.append(
                f"{meta['label']}({unit})" if unit else meta["label"]
            )
        headers.append("最后更新")

        self.table = QTableWidget(0, COLUMN_COUNT)
        self.table.setHorizontalHeaderLabels(headers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.table.setWordWrap(False)
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(
            COL_NAME, QHeaderView.ResizeMode.Stretch
        )
        header_view.setStretchLastSection(False)
        self.table.setColumnWidth(COL_ADDRESS, 165)
        self.table.setColumnWidth(COL_STATUS, 80)
        for index in range(len(utils.METRIC_ORDER)):
            self.table.setColumnWidth(COL_METRIC_START + index, 105)
        self.table.setColumnWidth(COL_UPDATED, 90)
        device_layout.addWidget(self.table)

        table_buttons = QHBoxLayout()
        self.connection_btn = QPushButton("断开设备")
        self.connection_btn.setEnabled(False)
        self.connection_btn.setToolTip(
            "手动释放 BLE 连接，让手机小程序可以连接；设备仍保留在列表中。"
        )
        self.delete_btn = QPushButton("删除选中设备")
        self.delete_btn.setEnabled(False)
        hint = QLabel("断开后手机小程序才能连接；关闭窗口只是隐藏到托盘。")
        hint.setStyleSheet("color: #757575;")
        table_buttons.addWidget(self.connection_btn)
        table_buttons.addSpacing(8)
        table_buttons.addWidget(self.delete_btn)
        table_buttons.addSpacing(12)
        table_buttons.addWidget(hint)
        table_buttons.addStretch(1)
        device_layout.addLayout(table_buttons)
        root.addWidget(device_group, 3)

        # 添加设备表单
        add_group = QGroupBox("添加设备")
        add_layout = QGridLayout(add_group)
        add_layout.setColumnStretch(1, 1)
        add_layout.setColumnStretch(2, 1)

        add_layout.addWidget(QLabel("显示名称"), 0, 0)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：客厅插座")
        self.name_edit.setMaxLength(40)
        add_layout.addWidget(self.name_edit, 0, 1, 1, 3)

        add_layout.addWidget(QLabel("蓝牙地址"), 1, 0)
        self.address_edit = QLineEdit()
        self.address_edit.setPlaceholderText("AA:BB:CC:DD:EE:FF")
        self.address_edit.setMaxLength(17)
        add_layout.addWidget(self.address_edit, 1, 1, 1, 2)

        self.scan_btn = QPushButton("扫描附近设备")
        add_layout.addWidget(self.scan_btn, 1, 3)

        add_layout.addWidget(QLabel("扫描结果"), 2, 0)
        self.scan_combo = QComboBox()
        self.scan_combo.addItem("（点击“扫描附近设备”，也可手动填写上面的地址）", None)
        self.scan_combo.setEnabled(False)
        add_layout.addWidget(self.scan_combo, 2, 1, 1, 3)

        self.add_btn = QPushButton("添加设备")
        self.add_btn.setDefault(True)
        add_layout.addWidget(self.add_btn, 3, 3)
        root.addWidget(add_group)

        # 设置区
        settings_group = QGroupBox("设置")
        settings_layout = QHBoxLayout(settings_group)

        self.autostart_cb = QCheckBox("开机自启")
        settings_layout.addWidget(self.autostart_cb)
        settings_layout.addSpacing(18)

        settings_layout.addWidget(QLabel("显示指标（托盘/悬浮窗）："))
        self.metric_checks: dict[str, QCheckBox] = {}
        for key in utils.METRIC_ORDER:
            check = QCheckBox(utils.METRIC_DEFS[key]["label"])
            self.metric_checks[key] = check
            settings_layout.addWidget(check)

        settings_layout.addStretch(1)
        settings_layout.addWidget(QLabel("轮询间隔："))
        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(1.0, 60.0)
        self.interval_spin.setDecimals(1)
        self.interval_spin.setSingleStep(1.0)
        self.interval_spin.setSuffix(" 秒")
        settings_layout.addWidget(self.interval_spin)
        root.addWidget(settings_group)

        # 悬浮窗设置
        floating_group = QGroupBox("任务栏悬浮窗")
        floating_layout = QHBoxLayout(floating_group)

        self.floating_cb = QCheckBox("显示悬浮窗")
        floating_layout.addWidget(self.floating_cb)
        floating_layout.addSpacing(14)

        floating_layout.addWidget(QLabel("排列："))
        self.floating_layout_combo = QComboBox()
        self.floating_layout_combo.addItem("两行 × 两列（4 个）", (2, 2))
        self.floating_layout_combo.addItem("一行 × 两列（2 个，轮播）", (1, 2))
        self.floating_layout_combo.addItem("两行 × 一列（2 个，轮播）", (2, 1))
        self.floating_layout_combo.addItem("四行 × 一列（4 个）", (4, 1))
        self.floating_layout_combo.addItem("一行 × 一列（1 个，轮播）", (1, 1))
        floating_layout.addWidget(self.floating_layout_combo)

        floating_layout.addSpacing(12)
        floating_layout.addWidget(QLabel("轮播："))
        self.floating_rotate_spin = QSpinBox()
        self.floating_rotate_spin.setRange(0, 60)
        self.floating_rotate_spin.setSuffix(" 秒")
        self.floating_rotate_spin.setSpecialValueText("不轮播")
        floating_layout.addWidget(self.floating_rotate_spin)

        floating_layout.addSpacing(12)
        floating_layout.addWidget(QLabel("字号："))
        self.floating_font_spin = QSpinBox()
        self.floating_font_spin.setRange(8, 24)
        self.floating_font_spin.setSuffix(" pt")
        floating_layout.addWidget(self.floating_font_spin)

        floating_layout.addSpacing(12)
        floating_layout.addWidget(QLabel("透明度："))
        self.floating_opacity_spin = QDoubleSpinBox()
        self.floating_opacity_spin.setRange(0.3, 1.0)
        self.floating_opacity_spin.setDecimals(2)
        self.floating_opacity_spin.setSingleStep(0.05)
        floating_layout.addWidget(self.floating_opacity_spin)

        floating_layout.addStretch(1)
        floating_hint = QLabel("拖动移动 · 左键打开 · 右键菜单")
        floating_hint.setStyleSheet("color: #757575;")
        floating_layout.addWidget(floating_hint)
        root.addWidget(floating_group)

        # 日志
        log_group = QGroupBox("通信日志（解密后的明文 Hex）")
        log_layout = QVBoxLayout(log_group)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(800)
        log_font = QFont("Consolas", 9)
        log_font.setStyleHint(QFont.StyleHint.Monospace)
        self.log_view.setFont(log_font)
        self.log_view.setPlaceholderText(
            "连接后，这里会显示 TX 通知解密后的 Hex，用于分析功率等字段偏移。"
        )
        log_layout.addWidget(self.log_view)

        log_bottom = QHBoxLayout()
        self.log_hint = QLabel("提示：明文每字节 = 密文每字节 XOR 0x91")
        self.log_hint.setStyleSheet("color: #757575;")
        clear_btn = QPushButton("清空日志")
        clear_btn.clicked.connect(self.log_view.clear)
        log_bottom.addWidget(self.log_hint)
        log_bottom.addStretch(1)
        log_bottom.addWidget(clear_btn)
        log_layout.addLayout(log_bottom)
        root.addWidget(log_group, 2)

        self.statusBar().showMessage("就绪")

    # ------------------------------------------------------------------
    # 初始化与信号连接
    # ------------------------------------------------------------------

    def _load_from_config(self) -> None:
        """把 config 填进控件，初始化期间屏蔽 toggled/valueChanged。"""
        device_addresses = {
            device["address"] for device in self.config.get("devices", [])
        }
        self._paused &= device_addresses
        self.config["paused_devices"] = sorted(self._paused)

        with QSignalBlocker(self.autostart_cb):
            actual_autostart = False
            try:
                actual_autostart = utils.is_autostart_enabled()
            except Exception as exc:  # noqa: BLE001 - 注册表异常不应阻止启动
                self._append_log(f"读取开机自启状态失败：{exc}")
            self.autostart_cb.setChecked(actual_autostart)
            self.config["auto_start"] = actual_autostart

        if actual_autostart:
            try:
                command = utils.get_autostart_command()
                if command and command != utils.build_launch_command():
                    self._append_log(
                        "注意：注册表中的自启命令与当前程序路径不同，"
                        "重新勾选一次可更新。"
                    )
            except Exception:
                pass

        for key, check in self.metric_checks.items():
            with QSignalBlocker(check):
                check.setChecked(bool(self.config["metrics"].get(key, False)))

        with QSignalBlocker(self.interval_spin):
            self.interval_spin.setValue(float(self.config["poll_interval"]))

        with QSignalBlocker(self.floating_cb):
            self.floating_cb.setChecked(bool(self.config.get("floating_enabled", True)))
        with QSignalBlocker(self.floating_layout_combo):
            target = (
                int(self.config.get("floating_rows", 2)),
                int(self.config.get("floating_columns", 2)),
            )
            for index in range(self.floating_layout_combo.count()):
                if self.floating_layout_combo.itemData(index) == target:
                    self.floating_layout_combo.setCurrentIndex(index)
                    break
        with QSignalBlocker(self.floating_rotate_spin):
            self.floating_rotate_spin.setValue(
                int(round(float(self.config.get("floating_rotate_seconds", 5.0))))
            )
        with QSignalBlocker(self.floating_font_spin):
            self.floating_font_spin.setValue(
                int(self.config.get("floating_font_size", 11))
            )
        with QSignalBlocker(self.floating_opacity_spin):
            self.floating_opacity_spin.setValue(
                float(self.config.get("floating_opacity", 0.86))
            )

        self._refresh_table()
        self._apply_metric_visibility()
        self._update_summary()

    def _connect_signals(self) -> None:
        self.add_btn.clicked.connect(self._add_device)
        self.connection_btn.clicked.connect(self._toggle_connection)
        self.delete_btn.clicked.connect(self._delete_selected)
        self.scan_btn.clicked.connect(self._request_scan)
        self.scan_combo.currentIndexChanged.connect(self._choose_scan_result)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.autostart_cb.toggled.connect(self._on_autostart_toggled)
        self.interval_spin.valueChanged.connect(self._on_interval_changed)
        self.floating_cb.toggled.connect(self._on_floating_toggled)
        self.floating_layout_combo.currentIndexChanged.connect(
            self._on_floating_layout_changed
        )
        self.floating_rotate_spin.valueChanged.connect(
            self._on_floating_rotation_changed
        )
        self.floating_font_spin.valueChanged.connect(
            self._on_floating_font_changed
        )
        self.floating_opacity_spin.valueChanged.connect(
            self._on_floating_opacity_changed
        )

        for key, check in self.metric_checks.items():
            check.toggled.connect(
                lambda checked, metric_key=key: self._on_metric_toggled(
                    metric_key, checked
                )
            )

    # ------------------------------------------------------------------
    # 配置持久化
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        """保存配置并广播；保存失败只写日志，不能让界面状态丢失。"""
        try:
            normalized = utils.save_config(self.config)
            # 保持 self.config 对象引用不变（main.py / 托盘持有同一个 dict）
            self.config.clear()
            self.config.update(normalized)
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"保存配置失败：{exc}")
        self.config_changed.emit(self.config)

    def _apply_metric_visibility(self) -> None:
        for index, key in enumerate(utils.METRIC_ORDER):
            visible = bool(self.config["metrics"].get(key, False))
            self.table.setColumnHidden(COL_METRIC_START + index, not visible)

    # ------------------------------------------------------------------
    # 设备表格
    # ------------------------------------------------------------------

    @staticmethod
    def _make_item(text: str, center: bool = False) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        if center:
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        return item

    def _refresh_table(self) -> None:
        self.table.setRowCount(0)
        self._row_by_address.clear()
        self._name_by_address.clear()

        devices = self.config.get("devices", [])
        self.table.setRowCount(len(devices))

        for row, device in enumerate(devices):
            address = device["address"]
            name = device["name"]
            self._row_by_address[address] = row
            self._name_by_address[address] = name

            name_item = self._make_item(name)
            name_item.setToolTip(f"蓝牙地址：{address}")
            address_item = self._make_item(address)
            address_item.setToolTip(address)
            self.table.setItem(row, COL_NAME, name_item)
            self.table.setItem(row, COL_ADDRESS, address_item)

            if address in self._paused:
                status = "paused"
                status_message = "已手动断开，手机小程序可以连接"
            else:
                status = self._statuses.get(address, "disconnected")
                status_message = self._status_messages.get(address, "尚未连接")
            status_item = self._make_item("")
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            status_item.setToolTip(status_message)
            self._paint_status_item(status_item, status)
            self.table.setItem(row, COL_STATUS, status_item)

            self._write_metric_cells(row, address)
            self._write_last_seen(row, address)

        # 重建表格会清空选择，顺便同步底部按钮状态
        self._on_selection_changed()

    def _write_metric_cells(self, row: int, address: str) -> None:
        data = self._latest.get(address, {})
        for index, key in enumerate(utils.METRIC_ORDER):
            value = data.get(key)
            text = self._format_metric(key, value)
            meta = utils.METRIC_DEFS[key]
            self._set_cell(
                row,
                COL_METRIC_START + index,
                text,
                center=True,
                tooltip=(
                    f"{meta['label']}：{text}"
                    + ("" if value is None else f" {meta['unit']}")
                ),
            )

    def _set_cell(
        self,
        row: int,
        column: int,
        text: str,
        *,
        center: bool = False,
        tooltip: str = "",
    ) -> None:
        """更新单元格内容但复用 QTableWidgetItem。

        数据每 3 秒刷新一次；如果每次都 new 一个 item，会打断用户当前
        选中的行（删除设备时很别扭）。复用 item 可以保持选择状态。
        """
        item = self.table.item(row, column)
        if item is None:
            item = self._make_item(text, center=center)
            self.table.setItem(row, column, item)
        else:
            item.setText(text)
            if center:
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        item.setToolTip(tooltip)

    @staticmethod
    def _format_metric(key: str, value: Any) -> str:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return "--"
        meta = utils.METRIC_DEFS[key]
        try:
            return meta["fmt"].format(float(value))
        except (ValueError, KeyError):
            return f"{float(value):g}"

    def _write_last_seen(self, row: int, address: str) -> None:
        text = self._last_seen.get(address, "--")
        self._set_cell(
            row,
            COL_UPDATED,
            text,
            center=True,
            tooltip="最近一次收到 TX 通知的时间",
        )

    @staticmethod
    def _paint_status_item(item: QTableWidgetItem, status: str) -> None:
        item.setText(STATUS_TEXT.get(status, status or "未知"))
        item.setForeground(
            QBrush(QColor(STATUS_COLORS.get(status, "#616161")))
        )

    def _update_summary(self) -> None:
        devices = self.config.get("devices", [])
        total = len(devices)
        connected = sum(
            1
            for device in devices
            if device["address"] not in self._paused
            and self._statuses.get(device["address"]) == "connected"
        )
        paused = sum(
            1 for device in devices if device["address"] in self._paused
        )
        text = f"已连接 {connected} / {total}"
        if paused:
            text += f" · 手动断开 {paused}"
        self.summary_label.setText(text)

        active_total = total - paused
        if active_total == 0:
            color = "#616161"
        elif connected == active_total:
            color = "#2E7D32"
        elif connected:
            color = "#F9A825"
        else:
            color = "#C62828"
        self.summary_label.setStyleSheet(f"color: {color}; font-weight: bold;")

    def _device_name(self, address: str) -> str:
        return self._name_by_address.get(address, address[-5:])

    def _on_selection_changed(self) -> None:
        address = self._selected_address()
        has_selection = address is not None
        self.delete_btn.setEnabled(has_selection)
        self.connection_btn.setEnabled(has_selection)

        if not has_selection:
            self.connection_btn.setText("断开设备")
            self.connection_btn.setToolTip(
                "手动释放 BLE 连接，让手机小程序可以连接；设备仍保留在列表中。"
            )
            return

        if address in self._paused:
            self.connection_btn.setText("恢复连接")
            self.connection_btn.setToolTip(
                "重新连接这台设备；请先关闭手机小程序，避免它继续占用设备。"
            )
        else:
            self.connection_btn.setText("断开设备")
            self.connection_btn.setToolTip(
                "手动释放 BLE 连接，让手机小程序可以连接；设备仍保留在列表中。"
            )

    def _selected_address(self) -> str | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.table.item(rows[0].row(), COL_ADDRESS)
        return item.text() if item is not None else None

    def _refresh_row_status(self, address: str) -> None:
        row = self._row_by_address.get(address)
        if row is None:
            return
        item = self.table.item(row, COL_STATUS)
        if item is not None:
            if address in self._paused:
                self._paint_status_item(item, "paused")
                item.setToolTip("已手动断开，手机小程序可以连接")
            else:
                status = self._statuses.get(address, "disconnected")
                self._paint_status_item(item, status)
                item.setToolTip(
                    self._status_messages.get(address, "尚未连接")
                )
        self._write_metric_cells(row, address)
        self._write_last_seen(row, address)

    def _toggle_connection(self) -> None:
        """断开/恢复选中设备，不删除配置。

        断开后 BleBridge 会把该地址从活动设备列表中移除，BleManager 取消
        对应 task 并断开 BLE，从而释放给手机小程序。断开状态会写入
        config.json，程序重启后也不会自动抢连。
        """
        address = self._selected_address()
        if not address:
            return

        name = self._device_name(address)
        pausing = address not in self._paused
        if pausing:
            self._paused.add(address)
            self._statuses[address] = "paused"
            self._status_messages[address] = "已手动断开，手机小程序可以连接"
            # 清掉旧读数，避免恢复连接后短暂显示断开前的陈旧数据
            self._latest.pop(address, None)
            self._last_seen.pop(address, None)
        else:
            self._paused.discard(address)
            self._statuses[address] = "connecting"
            self._status_messages[address] = "正在恢复连接…"

        self.config["paused_devices"] = sorted(self._paused)
        self._refresh_row_status(address)
        self._update_summary()
        self._on_selection_changed()
        self._persist()

        if pausing:
            self._append_log(
                f"手动断开设备：{name} ({address})，BLE 已释放给手机"
            )
            self.statusBar().showMessage(
                "已断开。手机小程序现在可以连接这台插座。", 6000
            )
        else:
            self._append_log(f"恢复连接设备：{name} ({address})")
            self.statusBar().showMessage(
                "正在恢复连接；如果手机小程序还连着，请先关闭它。", 6000
            )

    # ------------------------------------------------------------------
    # 添加 / 删除 / 扫描
    # ------------------------------------------------------------------

    def _add_device(self) -> None:
        name = self.name_edit.text().strip()[:40]
        address = utils.normalize_address(self.address_edit.text())

        if not name:
            QMessageBox.warning(self, "无法添加", "请填写设备显示名称。")
            self.name_edit.setFocus()
            return
        if not utils.is_valid_address(address):
            QMessageBox.warning(
                self,
                "无法添加",
                "蓝牙地址格式不正确，应为 AA:BB:CC:DD:EE:FF。",
            )
            self.address_edit.setFocus()
            return
        if any(
            device["address"] == address
            for device in self.config["devices"]
        ):
            QMessageBox.information(self, "设备已存在", "该蓝牙地址已经添加过了。")
            return

        self.config["devices"].append({"name": name, "address": address})
        self._paused.discard(address)
        self.config["paused_devices"] = sorted(self._paused)
        self._refresh_table()
        self._update_summary()
        self._persist()
        self._append_log(f"添加设备：{name} ({address})")

        self.name_edit.clear()
        self.address_edit.clear()
        self.scan_combo.setCurrentIndex(0)
        self.scan_combo.setEnabled(False)
        self.statusBar().showMessage(f"已添加 {name}，正在后台连接…", 5000)

    def _delete_selected(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "未选择", "请先在表格中选择一台设备。")
            return

        row = rows[0].row()
        address_item = self.table.item(row, COL_ADDRESS)
        if address_item is None:
            return
        address = address_item.text()
        name = self._device_name(address)

        answer = QMessageBox.question(
            self,
            "删除设备",
            f"确定不再监控“{name}”({address}) 吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.config["devices"] = [
            device
            for device in self.config["devices"]
            if device["address"] != address
        ]
        self._paused.discard(address)
        self.config["paused_devices"] = sorted(self._paused)
        self._statuses.pop(address, None)
        self._status_messages.pop(address, None)
        self._latest.pop(address, None)
        self._last_seen.pop(address, None)

        self._refresh_table()
        self._update_summary()
        self.delete_btn.setEnabled(False)
        self._persist()
        self._append_log(f"删除设备：{name} ({address})")
        self.statusBar().showMessage(f"已删除 {name}", 5000)

    def _request_scan(self) -> None:
        if self._scanning:
            return
        self._scanning = True
        self.scan_btn.setEnabled(False)
        self.scan_btn.setText("扫描中…")
        self.scan_combo.clear()
        self.scan_combo.addItem("正在扫描附近的 YUNM 设备，请稍候…", None)
        self.scan_combo.setEnabled(False)
        self.statusBar().showMessage("正在扫描 BLE 广播…")
        self.scan_requested.emit()

    @Slot(list)
    def on_scan_finished(self, devices: list[dict[str, Any]]) -> None:
        self._restore_scan_button()
        self._scan_results = list(devices or [])
        self.scan_combo.clear()

        if not self._scan_results:
            self.scan_combo.addItem("未发现名称以 YUNM 开头的设备", None)
            self.scan_combo.setEnabled(False)
            self._append_log("扫描完成：未发现 YUNM 设备")
            QMessageBox.information(
                self,
                "扫描完成",
                "没有发现 YUNM 设备。\n请确认插座已上电、蓝牙未被手机占用，"
                "并靠近电脑后重试。",
            )
            return

        added = {device["address"] for device in self.config["devices"]}
        for device in self._scan_results:
            rssi = device.get("rssi")
            rssi_text = f"{rssi} dBm" if rssi is not None else "RSSI 未知"
            suffix = "（已添加）" if device["address"] in added else ""
            self.scan_combo.addItem(
                f"{device['name']}  |  {device['address']}  |  {rssi_text}{suffix}",
                device,
            )
        self.scan_combo.setEnabled(True)
        self.scan_combo.setCurrentIndex(0)
        self._choose_scan_result(0)
        self._append_log(f"扫描完成：发现 {len(self._scan_results)} 台设备")
        self.statusBar().showMessage(
            f"发现 {len(self._scan_results)} 台设备，请选择后添加", 6000
        )

    @Slot(str)
    def on_scan_failed(self, message: str) -> None:
        self._restore_scan_button()
        self.scan_combo.clear()
        self.scan_combo.addItem("扫描失败，请检查蓝牙适配器", None)
        self.scan_combo.setEnabled(False)
        self._append_log(f"扫描失败：{message}")
        QMessageBox.warning(self, "扫描失败", f"无法扫描 BLE 设备：\n{message}")

    def _restore_scan_button(self) -> None:
        self._scanning = False
        self.scan_btn.setEnabled(True)
        self.scan_btn.setText("扫描附近设备")

    def _choose_scan_result(self, index: int) -> None:
        data = self.scan_combo.itemData(index)
        if not isinstance(data, dict):
            return
        self.name_edit.setText(str(data.get("name", "")))
        self.address_edit.setText(str(data.get("address", "")))

    # ------------------------------------------------------------------
    # 设置项
    # ------------------------------------------------------------------

    def _on_metric_toggled(self, key: str, checked: bool) -> None:
        self.config["metrics"][key] = bool(checked)
        self._apply_metric_visibility()
        self._persist()
        self.statusBar().showMessage(
            f"{'显示' if checked else '隐藏'}“{utils.METRIC_DEFS[key]['label']}”",
            3000,
        )

    def set_floating_enabled(self, enabled: bool) -> None:
        """供主程序右键菜单同步“显示悬浮窗”勾选状态。"""
        with QSignalBlocker(self.floating_cb):
            self.floating_cb.setChecked(bool(enabled))
        self.config["floating_enabled"] = bool(enabled)

    def _on_floating_toggled(self, checked: bool) -> None:
        self.config["floating_enabled"] = bool(checked)
        self._persist()
        self.statusBar().showMessage(
            f"悬浮窗已{'显示' if checked else '隐藏'}", 3000
        )

    def _on_floating_layout_changed(self, _index: int) -> None:
        data = self.floating_layout_combo.currentData()
        if not data:
            return
        rows, columns = data
        self.config["floating_rows"] = int(rows)
        self.config["floating_columns"] = int(columns)
        self._persist()

    def _on_floating_rotation_changed(self, value: int) -> None:
        self.config["floating_rotate_seconds"] = float(value)
        self._persist()

    def _on_floating_font_changed(self, value: int) -> None:
        self.config["floating_font_size"] = int(value)
        self._persist()

    def _on_floating_opacity_changed(self, value: float) -> None:
        self.config["floating_opacity"] = float(value)
        self._persist()

    def _on_autostart_toggled(self, checked: bool) -> None:
        try:
            utils.set_autostart(checked)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(
                self,
                "设置失败",
                f"无法{'启用' if checked else '关闭'}开机自启：\n{exc}",
            )
            with QSignalBlocker(self.autostart_cb):
                self.autostart_cb.setChecked(not checked)
            return

        self.config["auto_start"] = bool(checked)
        self._persist()
        self._append_log(
            f"开机自启已{'启用' if checked else '关闭'}"
            + (f"：{utils.build_launch_command()}" if checked else "")
        )

    def _on_interval_changed(self, value: float) -> None:
        self.config["poll_interval"] = round(float(value), 1)
        self._persist()
        self.statusBar().showMessage(
            f"轮询间隔已设为 {self.config['poll_interval']:.1f} 秒", 3000
        )

    # ------------------------------------------------------------------
    # 后台数据 / 状态槽（全部在主线程被 Qt 调用）
    # ------------------------------------------------------------------

    @Slot(str, dict, str, str)
    def on_device_data(
        self,
        address: str,
        measurement: dict[str, float],
        plain_hex: str,
        raw_hex: str,
    ) -> None:
        # 手动断开后，旧 task 可能还有一两个排队信号；不要覆盖“已断开”
        if address in self._paused:
            return

        self._latest[address] = dict(measurement or {})
        now = datetime.now().strftime("%H:%M:%S")
        self._last_seen[address] = now

        row = self._row_by_address.get(address)
        if row is not None:
            self._write_metric_cells(row, address)
            self._write_last_seen(row, address)
            name_item = self.table.item(row, COL_NAME)
            if name_item is not None:
                name_item.setToolTip(
                    f"蓝牙地址：{address}\n"
                    f"最近明文：{plain_hex}\n"
                    f"最近密文：{raw_hex}"
                )

        name = self._device_name(address)
        self._append_log(f"{name} 解密: {plain_hex}")
        if measurement:
            visible_count = sum(
                1
                for key in utils.METRIC_ORDER
                if key in measurement and self.config["metrics"].get(key, False)
            )
            self.log_hint.setText(
                f"最近数据 {now} · 已解析 {visible_count} 个指标"
            )
        else:
            self.log_hint.setText(
                f"最近数据 {now} · 明文 {len(plain_hex.split())} 字节 · "
                "解析偏移待配置（见 ble_manager.py）"
            )

    @Slot(str, str, str)
    def on_device_status(self, address: str, status: str, message: str) -> None:
        # 手动断开的设备保持灰色“已断开”，不被后台重连状态覆盖
        if address in self._paused:
            return

        previous = self._statuses.get(address)
        self._statuses[address] = status
        self._status_messages[address] = message

        row = self._row_by_address.get(address)
        if row is not None:
            item = self.table.item(row, COL_STATUS)
            if item is not None:
                self._paint_status_item(item, status)
                item.setToolTip(message)

        self._update_summary()
        self.statusBar().showMessage(
            f"{self._device_name(address)}：{message}", 5000
        )

        if status != previous:
            self._append_log(
                f"{self._device_name(address)} 状态: "
                f"{STATUS_TEXT.get(status, status)}（{message}）"
            )

    # ------------------------------------------------------------------
    # 日志与窗口行为
    # ------------------------------------------------------------------

    def _append_log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"{stamp}  {message}")

    @Slot(str)
    def append_system_log(self, message: str) -> None:
        """供 main.py / 托盘把非设备日志写入界面。"""
        self._append_log(message)

    def set_tray_available(self, available: bool) -> None:
        self._tray_available = bool(available)

    def show_window(self) -> None:
        """从托盘恢复窗口。"""
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def prepare_exit(self) -> None:
        """真正退出前调用，避免 closeEvent 再次把窗口藏起来。"""
        self._force_close = True
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """点击 X 只隐藏；没有托盘时则正常退出，避免无窗口可恢复。"""
        if self._force_close:
            event.accept()
            return

        if not self._tray_available:
            self._force_close = True
            event.accept()
            app = QApplication.instance()
            if app is not None:
                app.quit()
            return

        event.ignore()
        self.hide()
        self.hidden_to_tray.emit()
