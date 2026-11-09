"""main.py — 程序入口：Qt 主窗口 + 系统托盘 + 后台蓝牙事件循环。

线程模型：
    ┌──────────────────────────────┐
    │ PySide6 GUI 主线程            │
    │  · MainWindow                 │
    │  · TrayController（pystray）  │
    │  · BleBridge（Qt Signal 桥）  │
    └──────────────┬───────────────┘
                   │  Qt Signal / Slot（跨线程自动排队）
    ┌──────────────┴───────────────┐
    │ yunm-ble-asyncio 线程         │
    │  · BleManager                 │
    │  · 每设备一个 asyncio Task    │
    └──────────────────────────────┘

关键点：pystray 的菜单回调运行在托盘线程，绝不能直接操作 QWidget；
所以菜单回调只 emit Qt Signal，由主线程真正显示/退出窗口。
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import logging
import os
import sys
import threading
from typing import Any

from PIL import Image, ImageDraw
from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox
import pystray

import utils
from ble_manager import BleManager
from gui import MainWindow

logger = logging.getLogger(__name__)

# 托盘颜色：灰=无设备/停止，黄=连接中，绿=正常，红=断开
TRAY_COLORS: dict[str, tuple[int, int, int, int]] = {
    "idle": (117, 117, 117, 255),
    "connecting": (249, 168, 37, 255),
    "connected": (46, 125, 50, 255),
    "error": (198, 40, 40, 255),
}

TRAY_STATUS_TEXT = {
    "connecting": "连接中",
    "connected": "已连接",
    "reconnecting": "重连中",
    "disconnected": "断线",
    "error": "错误",
}


def install_thread_excepthook() -> None:
    """把后台线程（尤其是 pystray）的未捕获异常写入日志。

    Qt / pystray 都可能创建自己的线程；默认 threading 只把异常打印到
    stderr，打包成无控制台 exe 后就丢失了。这里统一转给 logging。
    """

    def hook(args: threading.ExceptHookArgs) -> None:
        logger.error(
            "线程 %s 未捕获异常",
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = hook


# ---------------------------------------------------------------------------
# 图标绘制
# ---------------------------------------------------------------------------

def make_tray_image(color: tuple[int, int, int, int], size: int = 64) -> Image.Image:
    """用 Pillow 画一个带闪电标记的圆形托盘图标，无需额外图片资源。"""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # 圆形底色 + 白色描边
    draw.ellipse(
        (2, 2, size - 3, size - 3),
        fill=color,
        outline=(255, 255, 255, 245),
        width=max(2, size // 20),
    )

    # 闪电形状（按 64x64 设计，再按比例缩放）
    bolt = [
        (0.58, 0.10),
        (0.27, 0.55),
        (0.46, 0.55),
        (0.38, 0.90),
        (0.73, 0.43),
        (0.52, 0.43),
    ]
    draw.polygon(
        [(int(x * size), int(y * size)) for x, y in bolt],
        fill=(255, 255, 255, 255),
    )
    return image


# ---------------------------------------------------------------------------
# Qt 与 bleak 的桥
# ---------------------------------------------------------------------------

class BleBridge(QObject):
    """把 BleManager 的普通回调转成 Qt Signal。

    BleManager 运行在独立线程，它的回调也在该线程触发；这里只做 emit，
    真正的 GUI 更新由 Qt 的 queued connection 投递回主线程。
    """

    data_received = Signal(str, dict, str, str)
    status_changed = Signal(str, str, str)
    log_message = Signal(str)
    scan_finished = Signal(list)
    scan_failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._scan_lock = threading.Lock()
        self._scan_inflight = False
        self.manager = BleManager(
            on_data=self._on_data,
            on_status=self._on_status,
            on_log=self._on_log,
            poll_interval=utils.DEFAULT_CONFIG["poll_interval"],
        )

    # 下面三个回调由 bleak 线程调用，只 emit 信号，不碰控件
    def _on_data(
        self,
        address: str,
        measurement: dict[str, float],
        plain_hex: str,
        raw_hex: str,
    ) -> None:
        self.data_received.emit(address, measurement, plain_hex, raw_hex)

    def _on_status(self, address: str, status: str, message: str) -> None:
        self.status_changed.emit(address, status, message)

    def _on_log(self, message: str) -> None:
        self.log_message.emit(message)

    def start(self, config: dict[str, Any]) -> None:
        self.manager.start()
        self.apply_config(config)

    def apply_config(self, config: dict[str, Any]) -> None:
        self.manager.set_poll_interval(float(config.get("poll_interval", 3.0)))

        # 手动断开的设备仍然保留在 config["devices"] 中，但不交给
        # BleManager；移除地址会触发对应 task 取消并断开 BLE。
        paused = {
            utils.normalize_address(str(address))
            for address in config.get("paused_devices", [])
        }
        active_devices = [
            device
            for device in config.get("devices", [])
            if utils.normalize_address(str(device.get("address", "")))
            not in paused
        ]
        self.manager.set_devices(active_devices)

    def start_scan(self, timeout: float = 8.0) -> None:
        with self._scan_lock:
            if self._scan_inflight:
                return
            self._scan_inflight = True

        def done(
            result: list[dict[str, Any]] | None,
            error: Exception | None,
        ) -> None:
            with self._scan_lock:
                self._scan_inflight = False
            if error is not None:
                self.scan_failed.emit(str(error))
            else:
                self.scan_finished.emit(list(result or []))

        try:
            self.manager.scan(timeout, done)
        except Exception as exc:  # noqa: BLE001
            with self._scan_lock:
                self._scan_inflight = False
            self.scan_failed.emit(str(exc))

    def shutdown(self) -> None:
        self.manager.stop()


# ---------------------------------------------------------------------------
# 托盘
# ---------------------------------------------------------------------------

class TrayController(QObject):
    """pystray 托盘封装。

    TrayController 是 QObject，因此 bridge.data_received 连接到它的槽时，
    Qt 会自动排队到 GUI 主线程执行。所有 pystray 更新都在主线程完成。
    """

    def __init__(self, window: MainWindow, config: dict[str, Any]) -> None:
        super().__init__()
        self.window = window
        self.config = config
        self.statuses: dict[str, str] = {}
        self.latest: dict[str, dict[str, float]] = {}

        self._images = {
            key: make_tray_image(color) for key, color in TRAY_COLORS.items()
        }
        self._last_icon_key = "idle"
        self._last_tooltip = utils.APP_DISPLAY_NAME
        self._hide_notified = False
        self._stopped = False
        self._start_failed = False
        self._ready = threading.Event()

        self.icon = pystray.Icon(
            "YUNM-Plug-Monitor",
            icon=self._images["idle"],
            title=self._last_tooltip,
            menu=self._build_menu(),
        )

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(
                "打开主界面", self._menu_show, default=True
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._menu_quit),
        )

    # 菜单回调运行在 pystray 线程：只 emit 信号，不直接操作窗口
    def _menu_show(self, _icon: Any, _item: Any) -> None:
        self.window.show_requested.emit()

    def _menu_quit(self, _icon: Any, _item: Any) -> None:
        self.window.quit_requested.emit()

    def _setup(self, icon: pystray.Icon) -> None:
        # 启动已经判定失败时，setup 线程可能刚被放行；此时不要再创建图标
        if self._start_failed:
            self._ready.set()
            return
        icon.visible = True
        self._ready.set()
        logger.info("托盘图标已显示")

    def start(self) -> bool:
        """启动 pystray；成功返回 True。"""
        try:
            # run_detached 在 Windows 上会启动托盘自己的消息循环线程，
            # 这样 Qt 主循环可以继续运行。
            self.icon.run_detached(setup=self._setup)
        except Exception:  # noqa: BLE001
            logger.exception("启动托盘失败")
            self._start_failed = True
            self._release_setup_thread()
            return False

        if not self._ready.wait(3.0):
            logger.warning("托盘图标在 3 秒内未就绪")
            self._start_failed = True
            try:
                self.icon.stop()
            except Exception:
                pass
            self._release_setup_thread()
            return False

        self._refresh()
        return True

    def _release_setup_thread(self) -> None:
        """pystray 启动失败时的兜底清理。

        pystray 的 setup 线程是非 daemon 的，如果底层 Win32 创建托盘窗口失败
        （例如被安全软件/远程桌面策略拦截），该线程会一直等在一个 Queue 上，
        导致 Python 进程无法退出。这里主动放行并 join，正常启动时不会走到这里。
        """
        try:
            queue = getattr(self.icon, "_Icon__queue", None)
            if queue is not None:
                queue.put(True)
            thread = getattr(self.icon, "_setup_thread", None)
            if (
                thread is not None
                and thread.ident != threading.current_thread().ident
            ):
                thread.join(1.0)
        except Exception:
            logger.debug("清理 pystray setup 线程失败", exc_info=True)

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        try:
            self.icon.stop()
        except Exception:  # noqa: BLE001
            logger.exception("停止托盘失败")

    # ------------------------------------------------------------------
    # 主线程槽
    # ------------------------------------------------------------------

    @Slot(str, dict, str, str)
    def on_data(
        self,
        address: str,
        measurement: dict[str, float],
        _plain_hex: str,
        _raw_hex: str,
    ) -> None:
        if address in set(self.config.get("paused_devices", [])):
            return
        self.latest[address] = dict(measurement or {})
        self._refresh()

    @Slot(str, str, str)
    def on_status(self, address: str, status: str, _message: str) -> None:
        if address in set(self.config.get("paused_devices", [])):
            return
        self.statuses[address] = status
        self._refresh()

    @Slot(dict)
    def on_config_changed(self, config: dict[str, Any]) -> None:
        self.config = config
        paused = set(config.get("paused_devices", []))
        for address in paused:
            # 手动断开后清除旧状态和旧读数，恢复时不要显示过期数据
            self.statuses.pop(address, None)
            self.latest.pop(address, None)
        self._refresh()

    @Slot()
    def on_hidden_to_tray(self) -> None:
        """首次隐藏时弹一次气泡；之后静默。"""
        if self._hide_notified:
            return
        try:
            if self.icon.visible:
                self.icon.notify(
                    "程序仍在托盘运行，双击图标可恢复窗口。",
                    "YUNM 插座监控",
                )
                self._hide_notified = True
        except Exception:  # noqa: BLE001
            logger.warning("显示托盘气泡失败", exc_info=True)

    # ------------------------------------------------------------------
    # 刷新 Tooltip / 颜色
    # ------------------------------------------------------------------

    def _status_of(self, address: str) -> str:
        return self.statuses.get(address, "connecting")

    def _overall_key(self) -> str:
        devices = self.config.get("devices", [])
        if not devices:
            return "idle"

        paused = set(self.config.get("paused_devices", []))
        active = [
            device for device in devices if device["address"] not in paused
        ]
        if not active:
            # 全部由用户手动断开：灰色表示“空闲”，不是故障红
            return "idle"

        states = [self._status_of(device["address"]) for device in active]
        if any(
            state in ("disconnected", "reconnecting", "error")
            for state in states
        ):
            return "error"
        if any(state == "connecting" for state in states):
            return "connecting"
        if all(state == "connected" for state in states):
            return "connected"
        return "error"

    def _format_value(self, key: str, value: float) -> str:
        meta = utils.METRIC_DEFS[key]
        try:
            text = meta["fmt"].format(float(value))
        except (TypeError, ValueError, KeyError):
            text = f"{float(value):g}"
        # 120.0W -> 120W，220.1V 保持不变
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return f"{text}{meta['unit']}"

    def _build_tooltip(self) -> str:
        devices = self.config.get("devices", [])
        if not devices:
            return f"{utils.APP_DISPLAY_NAME}（尚未添加设备）"

        visible_keys = [
            key
            for key in utils.METRIC_ORDER
            if self.config["metrics"].get(key, False)
        ]
        paused = set(self.config.get("paused_devices", []))

        parts: list[str] = []
        for device in devices:
            address = device["address"]
            name = device.get("name") or address[-5:]
            status = self._status_of(address)

            if address in paused:
                parts.append(f"{name}: 已断开")
                continue

            if status == "connected":
                values: list[str] = []
                for key in visible_keys:
                    value = self.latest.get(address, {}).get(key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        values.append(self._format_value(key, value))
                if values:
                    parts.append(f"{name}: " + " ".join(values))
                elif not visible_keys:
                    parts.append(f"{name}: 已连接")
                else:
                    parts.append(f"{name}: 已连接(待解析)")
            else:
                parts.append(
                    f"{name}: {TRAY_STATUS_TEXT.get(status, '断线')}"
                )

        # Windows 托盘 Tooltip 上限约 127 个字符，留一点余量
        text = " | ".join(parts)
        return text if len(text) <= 120 else text[:119] + "…"

    def _refresh(self) -> None:
        tooltip = self._build_tooltip()
        if tooltip != self._last_tooltip:
            self._last_tooltip = tooltip
            try:
                self.icon.title = tooltip
            except Exception:  # noqa: BLE001
                logger.warning("更新托盘 Tooltip 失败", exc_info=True)

        key = self._overall_key()
        if key != self._last_icon_key:
            self._last_icon_key = key
            try:
                self.icon.icon = self._images[key]
            except Exception:  # noqa: BLE001
                logger.warning("更新托盘图标失败", exc_info=True)


# ---------------------------------------------------------------------------
# 单实例保护
# ---------------------------------------------------------------------------

class SingleInstance(QObject):
    """Windows 单实例保护。

    主守卫使用命名 Mutex（不依赖 Qt 网络权限），第二个实例检测到 Mutex
    已存在后立即退出；如果第一个实例的 QLocalServer 可用，还会发送
    "show" 让已有窗口从托盘恢复。
    """

    show_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        user_hash = hashlib.sha1(
            getpass.getuser().encode("utf-8", "replace")
        ).hexdigest()[:12]
        self._name = f"YUNM-Plug-Monitor-{user_hash}"
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._accept_connections)
        self._started = False

        # Windows 命名 Mutex：进程退出（包括崩溃）时由系统自动释放。
        self._mutex_handle: int | None = None
        self._already_running = False
        if os.name == "nt":
            try:
                import ctypes

                self._mutex_handle = ctypes.windll.kernel32.CreateMutexW(
                    None, False, f"Local\\{self._name}"
                )
                self._already_running = (
                    ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
                )
            except Exception:
                logger.exception("创建单实例 Mutex 失败")

    def notify_existing(self, timeout_ms: int = 800) -> bool:
        """如果已有实例，请求它显示窗口并返回 True。"""
        if not self._already_running:
            return False

        socket = QLocalSocket()
        socket.connectToServer(self._name)
        if socket.waitForConnected(timeout_ms):
            socket.write(b"show")
            socket.waitForBytesWritten(timeout_ms)
            socket.disconnectFromServer()
        else:
            # Mutex 已存在但本地 socket 不可用（例如被安全策略拦截）：
            # 仍然必须退出，不能启动第二个窗口。
            logger.info("已有实例运行，但无法连接其显示通道")
        return True

    def start(self) -> bool:
        """作为第一个实例启动本地 server（用于唤醒窗口）。"""
        if self._already_running:
            return False
        # 清理上次异常退出留下的命名管道
        QLocalServer.removeServer(self._name)
        self._started = self._server.listen(self._name)
        if not self._started:
            logger.warning("单实例 server 启动失败：%s", self._server.errorString())
        return self._started

    def stop(self) -> None:
        if self._started:
            self._server.close()
            self._started = False
        if self._mutex_handle:
            try:
                import ctypes

                ctypes.windll.kernel32.CloseHandle(self._mutex_handle)
            except Exception:
                pass
            self._mutex_handle = None

    def _accept_connections(self) -> None:
        while self._server.hasPendingConnections():
            connection = self._server.nextPendingConnection()
            if connection is None:
                continue
            connection.readyRead.connect(
                lambda conn=connection: self._read_request(conn)
            )
            connection.disconnected.connect(connection.deleteLater)

    def _read_request(self, connection: QLocalSocket) -> None:
        data = bytes(connection.readAll())
        if data:
            logger.info("收到第二个实例的显示请求")
            self.show_requested.emit()


# ---------------------------------------------------------------------------
# 应用装配
# ---------------------------------------------------------------------------

class YUNMApplication:
    def __init__(self, qt_argv: list[str], no_tray: bool = False) -> None:
        self.no_tray = no_tray
        self.qt_app = QApplication(qt_argv)
        self.qt_app.setApplicationName(utils.APP_DISPLAY_NAME)
        self.qt_app.setQuitOnLastWindowClosed(False)

        # 必须在创建窗口、托盘和蓝牙线程之前判断。
        # 第二个实例只负责唤醒第一个实例，然后立即退出。
        self.single = SingleInstance()
        self._already_running = self.single.notify_existing()
        if self._already_running:
            logger.info("检测到已有实例，已请求其显示窗口，本实例退出")
            return

        icon_file = utils.resource_path("app.ico")
        if os.path.isfile(icon_file):
            self.qt_app.setWindowIcon(QIcon(icon_file))

        # Windows 任务栏分组/图标
        if os.name == "nt":
            try:
                import ctypes

                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "YUNM.Plug.Monitor"
                )
            except Exception:
                pass

        self.config = utils.load_config()
        # 首次运行落盘一份默认配置，用户无需手动创建
        try:
            utils.save_config(self.config)
        except Exception:
            logger.exception("初始化配置文件失败")

        self.window = MainWindow(self.config)
        self.bridge = BleBridge()
        self.tray = TrayController(self.window, self.config)

        self._connect_signals()
        self.single.show_requested.connect(self.window.show_window)
        self.single.start()

        self._shutdown_done = False
        self.qt_app.aboutToQuit.connect(self._shutdown)

    def _connect_signals(self) -> None:
        # 蓝牙 -> GUI / 托盘（跨线程自动排队）
        self.bridge.data_received.connect(self.window.on_device_data)
        self.bridge.data_received.connect(self.tray.on_data)
        self.bridge.status_changed.connect(self.window.on_device_status)
        self.bridge.status_changed.connect(self.tray.on_status)
        self.bridge.log_message.connect(self.window.append_system_log)
        self.bridge.scan_finished.connect(self.window.on_scan_finished)
        self.bridge.scan_failed.connect(self.window.on_scan_failed)

        # GUI -> 蓝牙 / 托盘
        self.window.config_changed.connect(self._on_config_changed)
        self.window.scan_requested.connect(self._start_scan)
        self.window.hidden_to_tray.connect(self.tray.on_hidden_to_tray)

        # 托盘菜单（托盘线程）-> 主线程
        self.window.show_requested.connect(self.window.show_window)
        self.window.quit_requested.connect(self.qt_app.quit)

    def _on_config_changed(self, config: dict[str, Any]) -> None:
        self.config = config
        self.bridge.apply_config(config)
        self.tray.on_config_changed(config)

    def _start_scan(self) -> None:
        timeout = float(self.config.get("scan_timeout", 8.0))
        self.bridge.start_scan(timeout)

    def run(self) -> int:
        if self._already_running:
            return 0

        self.window.show()

        if self.no_tray:
            self.window.set_tray_available(False)
            self.window.append_system_log(
                "以 --no-tray 模式运行：关闭窗口即退出程序。"
            )
        else:
            tray_ok = self.tray.start()
            self.window.set_tray_available(tray_ok)
            if not tray_ok:
                self.window.append_system_log(
                    "托盘启动失败，关闭窗口将直接退出程序。"
                )
                self.window.statusBar().showMessage(
                    "警告：系统托盘不可用，关闭窗口会退出程序"
                )

        try:
            self.bridge.start(self.config)
        except Exception as exc:  # noqa: BLE001
            logger.exception("启动蓝牙管理器失败")
            self.window.append_system_log(f"启动蓝牙管理器失败：{exc}")
            QMessageBox.critical(
                self.window,
                "蓝牙初始化失败",
                "无法启动蓝牙后台线程。\n"
                "请检查 Python 依赖（bleak）是否完整，然后重启程序。\n\n"
                f"{exc}",
            )

        return self.qt_app.exec()

    def _shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        logger.info("正在退出程序…")

        try:
            self.single.stop()
        except Exception:
            logger.exception("停止单实例服务失败")

        try:
            self.window.prepare_exit()
        except Exception:
            logger.exception("关闭主窗口失败")

        try:
            self.tray.stop()
        except Exception:
            logger.exception("停止托盘失败")

        try:
            self.bridge.shutdown()
        except Exception:
            logger.exception("停止蓝牙管理器失败")

        logger.info("程序已退出")


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------

def parse_args() -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        description="YUNM 蓝牙计量插座 Windows 托盘监控"
    )
    parser.add_argument(
        "--no-tray",
        action="store_true",
        help="不创建托盘图标（调试用；关闭窗口会直接退出）",
    )
    args, qt_args = parser.parse_known_args()
    # 只把 Qt 不认识的参数剔除，其它交给 QApplication
    return args, [sys.argv[0], *qt_args]


def main() -> int:
    utils.setup_logging()
    install_thread_excepthook()
    args, qt_argv = parse_args()
    logger.info("=" * 60)
    logger.info("启动 %s", utils.APP_DISPLAY_NAME)

    try:
        app = YUNMApplication(qt_argv, no_tray=args.no_tray)
    except Exception:
        logger.exception("应用初始化失败")
        return 1
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
