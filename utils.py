"""utils.py — 配置读写、PyInstaller 路径兼容、Windows 开机自启、日志。

这个文件不依赖 Qt / bleak，方便单独测试，也避免 GUI 与通信层出现循环导入。

路径策略（很重要）：
    * 开发环境：所有"可写数据"放在本文件所在目录（即项目根目录）。
    * PyInstaller 单文件环境：
        - 只读资源（例如 app.ico）在 sys._MEIPASS 临时目录；
        - 可写数据（config.json、日志）放在 exe 所在目录；
        - 如果 exe 目录不可写（例如装在 Program Files），自动退回到
          %APPDATA%\\YUNM-Plug-Monitor。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Any

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

APP_DISPLAY_NAME = "YUNM 蓝牙计量插座监控"
APP_ID = "YUNM-PLUG-MONITOR"
CONFIG_FILENAME = "config.json"
LOG_FILENAME = "yunm_monitor.log"

# HKEY_CURRENT_USER 下的开机自启注册表位置
AUTOSTART_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_VALUE_NAME = APP_ID

# 指标元数据：GUI 表头、托盘 Tooltip、格式化都从这里取，避免多处硬编码。
# fmt 使用 Python 的 format 语法；以后协议解析出来直接填数字即可。
METRIC_DEFS: dict[str, dict[str, str]] = {
    "power": {"label": "功率", "unit": "W", "fmt": "{:.1f}"},
    "voltage": {"label": "电压", "unit": "V", "fmt": "{:.1f}"},
    "current": {"label": "电流", "unit": "A", "fmt": "{:.3f}"},
    "energy": {"label": "电量", "unit": "kWh", "fmt": "{:.2f}"},
    "power_factor": {"label": "功率因数", "unit": "", "fmt": "{:.2f}"},
    "cost": {"label": "金额", "unit": "元", "fmt": "{:.2f}"},
}
METRIC_ORDER: tuple[str, ...] = tuple(METRIC_DEFS.keys())

DEFAULT_CONFIG: dict[str, Any] = {
    "devices": [],                 # [{"name": "插座1", "address": "AA:BB:..."}]
    "paused_devices": [],          # 用户手动“断开”的地址；重启后仍保持断开
    "metrics": {                   # 托盘/界面/悬浮窗需要显示的指标
        "power": True,
        "voltage": True,
        "current": False,
        "energy": False,
        "power_factor": False,
        "cost": False,
    },
    "poll_interval": 3.0,          # 轮询间隔（秒）
    "scan_timeout": 8.0,           # 扫描附近设备的持续时间（秒）
    "auto_start": False,           # 仅作记录，真实状态以注册表为准

    # 贴任务栏悬浮窗（类似 TrafficMonitor）
    "floating_enabled": True,
    "floating_in_taskbar": True,
    "icon_show_value": True,
    "floating_rows": 2,
    "floating_columns": 2,
    "floating_rotate_seconds": 5.0,
    "floating_font_size": 9,
    "floating_opacity": 0.86,
    "floating_x": None,
    "floating_y": None,
}

_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}$")
_CONFIG_LOCK = threading.RLock()
_DATA_DIR: str | None = None

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 路径处理
# ---------------------------------------------------------------------------

def is_frozen() -> bool:
    """是否运行在 PyInstaller 生成的 exe 中。"""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> str:
    """程序所在目录。

    * 打包后 = exe 所在目录（不是 _MEIPASS）。
    * 开发时 = 本文件所在目录。
    """
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(relative_path: str) -> str:
    """获取只读资源（图标等）的绝对路径。

    PyInstaller 单文件模式会把 --add-data 的内容解压到 sys._MEIPASS，
    运行结束后该目录会被删除，所以这里只用于读取，绝不能写入。
    """
    base = getattr(sys, "_MEIPASS", None) or app_dir()
    return os.path.abspath(os.path.join(base, relative_path))


def _directory_writable(directory: str) -> bool:
    """不留下持久文件地测试目录是否可写。"""
    try:
        os.makedirs(directory, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", prefix=".yunm-write-test-", dir=directory, delete=True
        ):
            return True
    except OSError:
        return False


def data_dir() -> str:
    """可写数据目录，带缓存。

    优先使用 exe 同目录，方便用户直接看到 config.json；
    不可写时回退到 %APPDATA%\\YUNM-Plug-Monitor。
    """
    global _DATA_DIR
    if _DATA_DIR:
        return _DATA_DIR

    if is_frozen():
        candidate = app_dir()
        if _directory_writable(candidate):
            _DATA_DIR = candidate
        else:
            appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
            fallback = os.path.join(appdata, APP_ID)
            os.makedirs(fallback, exist_ok=True)
            _DATA_DIR = fallback
            logger.warning("exe 目录不可写，配置改存到：%s", fallback)
    else:
        _DATA_DIR = app_dir()

    os.makedirs(_DATA_DIR, exist_ok=True)
    return _DATA_DIR


def config_path() -> str:
    return os.path.join(data_dir(), CONFIG_FILENAME)


def log_path() -> str:
    return os.path.join(data_dir(), LOG_FILENAME)


# ---------------------------------------------------------------------------
# 蓝牙地址
# ---------------------------------------------------------------------------

def normalize_address(address: str | None) -> str:
    """统一成 AA:BB:CC:DD:EE:FF 形式（Windows BLE 使用 MAC 地址）。"""
    value = (address or "").strip().replace("-", ":").upper()
    return value


def is_valid_address(address: str | None) -> bool:
    return bool(address and _MAC_RE.match(address.strip()))


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------

def default_config() -> dict[str, Any]:
    """返回一份全新的默认配置，避免调用方改到模块常量。"""
    return copy.deepcopy(DEFAULT_CONFIG)


def _clamp_float(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, number))


def normalize_config(raw: Any) -> dict[str, Any]:
    """把磁盘上可能损坏/旧版本的配置修正为当前结构。

    原则：尽量保留用户数据；单个设备非法时只跳过该设备，不让整个配置失效。
    """
    config = default_config()
    if not isinstance(raw, dict):
        return config

    # 设备列表：去重、校验收敛地址
    devices: list[dict[str, str]] = []
    seen: set[str] = set()
    raw_devices = raw.get("devices")
    if isinstance(raw_devices, list):
        for item in raw_devices:
            if not isinstance(item, dict):
                continue
            address = normalize_address(str(item.get("address", "")))
            if not is_valid_address(address) or address in seen:
                continue
            name = str(item.get("name", "")).strip()[:40] or address[-5:]
            devices.append({"name": name, "address": address})
            seen.add(address)
    config["devices"] = devices

    # 手动断开（暂停连接）的设备：只保留仍存在于设备列表中的地址。
    # 这样即使用户删除设备后再添加，旧的暂停状态也不会错误地生效。
    paused: list[str] = []
    raw_paused = raw.get("paused_devices")
    if isinstance(raw_paused, list):
        device_addresses = {device["address"] for device in devices}
        for item in raw_paused:
            address = normalize_address(str(item))
            if is_valid_address(address) and address in device_addresses:
                paused.append(address)
    config["paused_devices"] = sorted(set(paused))

    # 指标勾选
    raw_metrics = raw.get("metrics")
    if isinstance(raw_metrics, dict):
        for key in METRIC_ORDER:
            if key in raw_metrics:
                config["metrics"][key] = bool(raw_metrics[key])

    # 数值项
    config["poll_interval"] = _clamp_float(
        raw.get("poll_interval"), 1.0, 60.0, config["poll_interval"]
    )
    config["scan_timeout"] = _clamp_float(
        raw.get("scan_timeout"), 3.0, 30.0, config["scan_timeout"]
    )
    config["auto_start"] = bool(raw.get("auto_start", config["auto_start"]))

    # 悬浮窗设置
    config["floating_enabled"] = bool(
        raw.get("floating_enabled", config["floating_enabled"])
    )
    config["floating_in_taskbar"] = bool(
        raw.get("floating_in_taskbar", config["floating_in_taskbar"])
    )
    config["icon_show_value"] = bool(
        raw.get("icon_show_value", config["icon_show_value"])
    )
    config["floating_rows"] = int(
        _clamp_float(raw.get("floating_rows"), 1, 4, config["floating_rows"])
    )
    config["floating_columns"] = int(
        _clamp_float(raw.get("floating_columns"), 1, 2, config["floating_columns"])
    )
    config["floating_rotate_seconds"] = _clamp_float(
        raw.get("floating_rotate_seconds"),
        0.0,
        60.0,
        config["floating_rotate_seconds"],
    )
    config["floating_font_size"] = int(
        _clamp_float(
            raw.get("floating_font_size"), 8, 24, config["floating_font_size"]
        )
    )
    config["floating_opacity"] = _clamp_float(
        raw.get("floating_opacity"), 0.3, 1.0, config["floating_opacity"]
    )
    for axis in ("floating_x", "floating_y"):
        value = raw.get(axis)
        if isinstance(value, (int, float)) and -20000 <= value <= 20000:
            config[axis] = int(value)
        else:
            config[axis] = None
    return config


def _backup_broken_config(path: str) -> None:
    """配置文件损坏时先备份，方便用户手工找回设备地址。"""
    try:
        suffix = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, f"{path}.broken-{suffix}")
    except OSError:
        logger.exception("备份损坏的配置文件失败")


def load_config() -> dict[str, Any]:
    """读取 config.json；不存在或损坏时返回安全默认值。"""
    path = config_path()
    try:
        with open(path, "r", encoding="utf-8") as fp:
            raw = json.load(fp)
    except FileNotFoundError:
        return default_config()
    except json.JSONDecodeError:
        logger.exception("配置文件不是合法 JSON，已使用默认配置：%s", path)
        _backup_broken_config(path)
        return default_config()
    except OSError:
        logger.exception("读取配置文件失败，已使用默认配置：%s", path)
        return default_config()

    return normalize_config(raw)


def save_config(config: dict[str, Any]) -> dict[str, Any]:
    """原子写入 config.json（先写临时文件，再 os.replace）。

    原子写入可以避免断电/崩溃时留下半个 JSON 文件。
    返回实际写入的规范化配置。
    """
    normalized = normalize_config(config)
    path = config_path()
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)

    tmp_path = ""
    with _CONFIG_LOCK:
        try:
            fd, tmp_path = tempfile.mkstemp(
                prefix=".config-", suffix=".tmp", dir=directory
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump(normalized, fp, ensure_ascii=False, indent=2)
                fp.write("\n")
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp_path, path)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
    return normalized


# ---------------------------------------------------------------------------
# Windows 开机自启
# ---------------------------------------------------------------------------

def _winreg():
    """延迟导入 winreg，并明确提示平台限制。"""
    if os.name != "nt":
        raise RuntimeError("开机自启仅支持 Windows")
    import winreg  # type: ignore[import-not-found]

    return winreg


def build_launch_command(autostart: bool = False) -> str:
    """生成写进注册表 Run 项的启动命令。

    打包后：
        "C:\\path\\YUNM-Plug-Monitor.exe" --autostart
    开发时：
        "C:\\...\\python.exe" "C:\\...\\main.py" --autostart

    注意：不能只写 sys.executable。开发时那是 python.exe，双击启动后
    不知道要运行哪个脚本，会直接打开一个空白的 Python 解释器。
    """
    executable = os.path.abspath(sys.executable)
    suffix = " --autostart" if autostart else ""
    if is_frozen():
        return f'"{executable}"{suffix}'

    script = ""
    if sys.argv and sys.argv[0]:
        script = os.path.abspath(sys.argv[0])

    # 兼容 `python -m ...` / 某些 IDE 启动方式：优先保证指向 main.py
    if not script.lower().endswith(".py") or not os.path.isfile(script):
        script = os.path.join(app_dir(), "main.py")

    return f'"{executable}" "{os.path.abspath(script)}"{suffix}'


def get_autostart_command() -> str | None:
    """返回当前注册表里的启动命令；未设置返回 None。"""
    winreg = _winreg()
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            AUTOSTART_RUN_KEY,
            0,
            winreg.KEY_QUERY_VALUE,
        ) as key:
            value, _ = winreg.QueryValueEx(key, AUTOSTART_VALUE_NAME)
            return str(value) if value else None
    except FileNotFoundError:
        return None


def is_autostart_enabled() -> bool:
    return bool(get_autostart_command())


def set_autostart(enabled: bool) -> str | None:
    """启用/禁用开机自启，返回当前（或删除前）的启动命令。"""
    winreg = _winreg()
    if enabled:
        command = build_launch_command(autostart=True)
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            AUTOSTART_RUN_KEY,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(
                key, AUTOSTART_VALUE_NAME, 0, winreg.REG_SZ, command
            )
        logger.info("已启用开机自启：%s", command)
        return command

    old_command = get_autostart_command()
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            AUTOSTART_RUN_KEY,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.DeleteValue(key, AUTOSTART_VALUE_NAME)
    except FileNotFoundError:
        # 注册表项或值本来就不存在，视为已经关闭
        pass
    logger.info("已关闭开机自启")
    return old_command


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

def setup_logging(level: int = logging.INFO) -> None:
    """配置控制台 + 滚动文件日志。

    控制台用于开发时观察解密后的 Hex；打包为 --windowed 后没有控制台，
    日志仍会写入 exe 同目录的 yunm_monitor.log，方便排查。
    """
    root = logging.getLogger()
    if getattr(root, "_yunm_configured", False):
        root.setLevel(level)
        return

    root.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(fmt)
        root.addHandler(console)

    try:
        file_handler = RotatingFileHandler(
            log_path(),
            maxBytes=2 * 1024 * 1024,
            backupCount=2,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
            )
        )
        root.addHandler(file_handler)
    except OSError:
        logger.exception("创建日志文件失败，仅使用控制台输出")

    # bleak 的调试日志非常吵，保留警告以上即可
    logging.getLogger("bleak").setLevel(logging.WARNING)
    setattr(root, "_yunm_configured", True)
    logger.info("日志初始化完成，配置文件：%s", config_path())
