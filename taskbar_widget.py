"""taskbar_widget.py — 贴在任务栏上方的计量悬浮窗。

类似 TrafficMonitor：无边框、置顶、不进任务栏，显示功率/电压/电流/电量等。
支持一行/两行/四行、每行一列/两列，以及超过一页时定时轮播。
"""

from __future__ import annotations

import math
from typing import Any

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QApplication, QMenu, QWidget

import utils


class FloatingMonitor(QWidget):
    """贴任务栏的悬浮数据窗。"""

    show_main_requested = Signal()
    hide_requested = Signal()
    quit_requested = Signal()
    position_changed = Signal(int, int)

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)

        self._config = config
        self._address: str | None = None
        self._measurement: dict[str, float] = {}
        self._status = "idle"

        self._rows = 2
        self._columns = 2
        self._rotate_seconds = 5.0
        self._font_size = 11
        self._opacity = 0.86

        self._pages: list[list[str]] = []
        self._page = 0
        self._cell_widths: list[int] = []
        self._line_height = 18
        self._pad = 9
        self._gap = 12
        self._dot_width = 10

        self._drag_offset: QPoint | None = None
        self._drag_start: QPoint | None = None
        self._dragged = False

        self._font = QFont("Microsoft YaHei UI", self._font_size)
        self._metrics_font = QFont(self._font)

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.CoarseTimer)
        self._timer.timeout.connect(self._next_page)

        self._menu = QMenu(self)
        self._action_show = self._menu.addAction("打开主界面")
        self._action_hide = self._menu.addAction("隐藏悬浮窗")
        self._menu.addSeparator()
        self._action_quit = self._menu.addAction("退出程序")
        self._action_show.triggered.connect(self.show_main_requested.emit)
        self._action_hide.triggered.connect(self.hide_requested.emit)
        self._action_quit.triggered.connect(self.quit_requested.emit)

        self.apply_config(config, initial=True)

    # ------------------------------------------------------------------
    # 配置与数据
    # ------------------------------------------------------------------
    def apply_config(self, config: dict[str, Any], initial: bool = False) -> None:
        self._config = config
        self._rows = max(1, min(4, int(config.get("floating_rows", 2))))
        self._columns = max(1, min(2, int(config.get("floating_columns", 2))))
        self._rotate_seconds = float(config.get("floating_rotate_seconds", 5.0))
        self._font_size = max(8, min(24, int(config.get("floating_font_size", 11))))
        self._opacity = max(0.3, min(1.0, float(config.get("floating_opacity", 0.86))))
        self._font = QFont("Microsoft YaHei UI", self._font_size)
        self._metrics_font = QFont(self._font)

        devices = config.get("devices", [])
        addresses = {item.get("address") for item in devices if isinstance(item, dict)}
        if hasattr(self, "_address") and self._address not in addresses:
            self._address = None
            self._measurement = {}
            self._status = "idle"
            if devices and isinstance(devices[0], dict):
                self._address = devices[0].get("address")

        self._rebuild_pages()
        self._restore_position(initial=initial)

        if config.get("floating_enabled", True):
            if not self.isVisible():
                self.show()
            self.raise_()
        else:
            self.hide()

    def on_data(
        self,
        address: str,
        measurement: dict[str, float],
        plain_hex: str = "",
        raw_hex: str = "",
    ) -> None:
        if self._address is None:
            self._address = address
        if address != self._address:
            return
        self._measurement = dict(measurement)
        self._status = "connected"
        self._update_geometry()
        self.update()

    def on_status(self, address: str, status: str, message: str = "") -> None:
        if self._address is not None and address != self._address:
            return
        if self._address is None:
            self._address = address
        self._status = status
        if status in {"disconnected", "error"}:
            # 断线后不要把旧读数当成实时数据继续展示。
            self._measurement = {}
        self.update()

    def selected_metrics(self) -> list[str]:
        return [
            key
            for key in utils.METRIC_ORDER
            if bool(self._config.get("metrics", {}).get(key, False))
        ]

    # ------------------------------------------------------------------
    # 页面与尺寸
    # ------------------------------------------------------------------
    def _rebuild_pages(self) -> None:
        metrics = self.selected_metrics()
        capacity = max(1, self._rows * self._columns)
        if not metrics:
            self._pages = [[]]
        else:
            self._pages = [
                metrics[start : start + capacity]
                for start in range(0, len(metrics), capacity)
            ]
        self._page = min(self._page, max(0, len(self._pages) - 1))
        if len(self._pages) > 1 and self._rotate_seconds > 0:
            self._timer.start(max(1000, int(self._rotate_seconds * 1000)))
        else:
            self._timer.stop()
            self._page = 0
        self._update_geometry()
        self.update()

    def _tokens_for_page(self, page_index: int) -> list[str]:
        if not self._pages:
            return []
        page = self._pages[max(0, min(page_index, len(self._pages) - 1))]
        tokens: list[str] = []
        for key in page:
            meta = utils.METRIC_DEFS.get(key)
            if not meta:
                continue
            value = self._measurement.get(key)
            label = meta["label"]
            unit = meta.get("unit", "")
            if value is None:
                tokens.append(f"{label} --")
            else:
                try:
                    text = meta["fmt"].format(float(value))
                except (TypeError, ValueError):
                    text = "--"
                tokens.append(f"{label} {text}{unit}")
        return tokens

    def _update_geometry(self) -> None:
        if not self.selected_metrics():
            page_tokens = [["未选择指标"]]
        elif not self._pages:
            page_tokens = [["等待数据"]]
        else:
            page_tokens = [
                self._tokens_for_page(index) or ["等待数据"]
                for index in range(len(self._pages))
            ]

        fm = QFontMetrics(self._font)
        self._line_height = fm.height() + 4
        min_width = fm.horizontalAdvance("功率 9999.9W") + 8
        cell_widths = [min_width for _ in range(self._columns)]
        max_rows = 1
        for tokens in page_tokens:
            max_rows = max(max_rows, max(1, math.ceil(len(tokens) / self._columns)))
            for index, token in enumerate(tokens):
                column = index % self._columns
                cell_widths[column] = max(
                    cell_widths[column], fm.horizontalAdvance(token) + 6
                )

        self._cell_widths = cell_widths
        width = (
            self._pad * 2
            + self._dot_width
            + sum(self._cell_widths)
            + self._gap * (self._columns - 1)
        )
        height = self._pad * 2 + max_rows * self._line_height
        self.setFixedSize(width, height)
        self._clamp_position()

    def _next_page(self) -> None:
        if len(self._pages) <= 1:
            return
        self._page = (self._page + 1) % len(self._pages)
        self._update_geometry()
        self.update()

    # ------------------------------------------------------------------
    # 位置与拖动
    # ------------------------------------------------------------------
    def _restore_position(self, initial: bool) -> None:
        x = self._config.get("floating_x")
        y = self._config.get("floating_y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            self.move(int(x), int(y))
            self._clamp_position()
            return
        if initial or not self.isVisible():
            screen = QApplication.primaryScreen()
            if screen is None:
                return
            area = screen.availableGeometry()
            self.move(
                area.right() - self.width() - 12,
                area.bottom() - self.height() - 12,
            )

    def _clamp_position(self) -> None:
        screen = QApplication.screenAt(self.frameGeometry().center())
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.geometry()
        max_x = max(area.left(), area.right() - self.width() + 1)
        max_y = max(area.top(), area.bottom() - self.height() + 1)
        x = min(max(self.x(), area.left()), max_x)
        y = min(max(self.y(), area.top()), max_y)
        if x != self.x() or y != self.y():
            self.move(x, y)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.RightButton:
            self._menu.exec(event.globalPosition().toPoint())
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            self._drag_start = event.globalPosition().toPoint()
            self._dragged = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is None:
            return
        current = event.globalPosition().toPoint()
        if self._drag_start is not None:
            if (current - self._drag_start).manhattanLength() < 4:
                return
        self._dragged = True
        self.move(current - self._drag_offset)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._drag_offset is not None:
            if self._dragged:
                self._clamp_position()
                self.position_changed.emit(self.x(), self.y())
            else:
                self.show_main_requested.emit()
            self._drag_offset = None
            self._drag_start = None
            self._dragged = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        alpha = int(255 * self._opacity)
        painter.setPen(QPen(QColor(255, 255, 255, 26), 1))
        painter.setBrush(QColor(20, 22, 26, alpha))
        painter.drawRoundedRect(
            QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 9, 9
        )

        # 状态圆点：绿=有数据，黄=连接中，红=断线，灰=等待
        dot_color = {
            "connected": QColor(76, 175, 80),
            "connecting": QColor(255, 193, 7),
            "reconnecting": QColor(255, 152, 0),
            "disconnected": QColor(244, 67, 54),
            "error": QColor(244, 67, 54),
        }.get(self._status, QColor(140, 140, 140))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(dot_color)
        dot_y = self._pad + max(0, (self._line_height - 8) // 2)
        painter.drawEllipse(QRectF(self._pad, dot_y, 7, 7))

        tokens = self._tokens_for_page(self._page)
        if not tokens:
            tokens = ["未选择指标"]

        painter.setFont(self._font)
        painter.setPen(QColor(245, 246, 248))
        fm = QFontMetrics(self._font)
        text_x = self._pad + self._dot_width
        for index, token in enumerate(tokens):
            row = index // self._columns
            column = index % self._columns
            x = text_x + sum(self._cell_widths[:column]) + self._gap * column
            y = self._pad + row * self._line_height
            text = fm.elidedText(
                token,
                Qt.TextElideMode.ElideRight,
                max(10, self._cell_widths[column]),
            )
            painter.drawText(
                QRectF(x, y, self._cell_widths[column], self._line_height),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                text,
            )
