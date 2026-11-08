"""ble_manager.py — YUNM 蓝牙计量插座的异步通信层。

设计要点：
    1. bleak 是纯 asyncio 库，本模块内部维护一个专属事件循环线程，
       与 PySide6 主线程完全隔离；GUI 卡顿不会拖慢蓝牙，反之亦然。
    2. 本模块不 import PySide6。数据通过普通 Python 回调抛出，
       由 main.py 的 BleBridge 转成 Qt Signal，保证跨线程安全。
    3. 每台设备一个 asyncio Task：连接、订阅 TX、定时向 RX 写指令、
       解密、回调数据；异常断开后按指数退避自动重连。

已确认的 Microchip 透明 UART 参数：
    设备名前缀 : YUNM
    Service    : 49535343-fe7d-4ae5-8fa9-9fafd205e455
    RX (Write) : 49535343-8841-43f4-a8d4-ecbe34729bb3
    TX (Notify): 49535343-1e4d-4bd9-ba61-23c647249616

协议已在 2026-11-08 通过 HCI 抓包确认并实现：

    1. 按 MAC 派生 XOR key 和 CRC16，生成本地查询命令；
    2. 写 RX 后，插座通过 TX 返回 EC ED 帧；
    3. 通知可能分片（20 + 18 字节），需要先拼接；
    4. 帧内 32 字节数据再 XOR 0x63，然后按大端解析功率/电压/电流/电量。

因此当前代码不再依赖官方小程序或云端，可以离线读取计量数据。
详见项目根目录 PROTOCOL_NOTES.md。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable
from typing import Any

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from utils import is_valid_address, normalize_address

# ---------------------------------------------------------------------------
# 协议常量
# ---------------------------------------------------------------------------

DEVICE_NAME_PREFIX = "YUNM"
SERVICE_UUID = "49535343-fe7d-4ae5-8fa9-9fafd205e455"
RX_CHAR_UUID = "49535343-8841-43f4-a8d4-ecbe34729bb3"   # 手机/电脑 -> 插座
TX_CHAR_UUID = "49535343-1e4d-4bd9-ba61-23c647249616"   # 插座 -> 手机/电脑

# 已验证的真实协议参数（2026-11-08 用 HCI 抓包确认）：
#   写 RX 的完整命令（MAC 25:02:25:00:59:68）：
#       E1 E0 0D 0E 46 4E 46 53 24
#   MAC 异或 key = MAC 六字节之和 & 0xFF = 0x0D
#   解密后的命令帧：
#       EC ED 00 03 4B 43 4B 5E 29
#   插座返回帧头 EC ED，长度 + 6 为整帧；
#   帧内 32 字节数据再用 0x63 异或后解析。
COMMAND_PAYLOAD = bytes([0x4B, 0x43, 0x4B])   # 明文载荷 "KCK"
DATA_XOR_KEY = 0x63                           # 返回数据载荷异或 key
FRAME_HEADER = bytes([0xEC, 0xED])
FRAME_OVERHEAD = 6                            # 头 2 + 长度 2 + CRC 2

# 兼容旧调试脚本的别名；正式轮询不再使用固定命令。
COMMAND = bytes.fromhex("E1 E0 0D 0E 46 4E 46 53 24")
XOR_KEY = 0x0D

CONNECT_TIMEOUT = 25.0       # 单次连接最长等待（秒）
RETRY_INITIAL = 2.0          # 首次重连等待（秒）
RETRY_MAX = 30.0             # 重连等待上限（秒）
DEFAULT_POLL_INTERVAL = 3.0  # 默认轮询间隔（秒）

logger = logging.getLogger(__name__)

# 类型别名：让 main.py 里的回调签名一目了然
DataCallback = Callable[[str, dict[str, float], str, str], None]
StatusCallback = Callable[[str, str, str], None]
LogCallback = Callable[[str], None]
ScanDoneCallback = Callable[[list[dict[str, Any]] | None, Exception | None], None]


# ---------------------------------------------------------------------------
# 数据解析
# ---------------------------------------------------------------------------

# 返回帧的 32 字节载荷先 XOR 0x63，再按大端读取。
# 字段偏移来自 2026-11-08 HCI 抓包和官方界面数值对照。
METRIC_LAYOUT: dict[str, dict[str, Any]] = {
    "power": {
        "offset": 11,
        "length": 2,
        "scale": 0.1,
        "signed": False,
        "order": "big",
    },
    "voltage": {
        "offset": 13,
        "length": 2,
        "scale": 0.1,
        "signed": False,
        "order": "big",
    },
    "current": {
        "offset": 15,
        "length": 2,
        "scale": 0.001,
        "signed": False,
        "order": "big",
    },
    "energy": {
        "offset": 5,
        "length": 2,
        "scale": 0.01,
        "signed": False,
        "order": "big",
    },
}


def format_hex(data: bytes) -> str:
    """把字节流格式化为 'E1 E0 0D ...' 形式。"""
    return " ".join(f"{byte:02X}" for byte in data)


def address_to_bytes(address: str) -> bytes:
    """把 AA:BB:CC:DD:EE:FF 转成 6 字节，用于派生 key/CRC。"""
    parts = address.strip().replace("-", ":").split(":")
    if len(parts) != 6:
        raise ValueError(f"非法蓝牙地址: {address}")
    return bytes(int(part, 16) for part in parts)


def mac_xor_key(address: str) -> int:
    """官方协议：XOR key = MAC 六字节之和 & 0xFF。"""
    return sum(address_to_bytes(address)) & 0xFF


def crc16(data: bytes, init: int, poly: int) -> int:
    """官方协议使用的 CRC16（reflected，多项式由 MAC 派生）。"""
    value = init
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = ((value >> 1) ^ poly) if (value & 1) else (value >> 1)
    return value & 0xFFFF


def build_command(address: str) -> bytes:
    """按设备 MAC 生成真实的 9 字节查询命令。

    明文帧：EC ED 00 03 4B 43 4B <CRC16>
    再与 MAC 派生 key 逐字节异或。
    """
    mac = address_to_bytes(address)
    init = (mac[5] + mac[4] + 10) & 0xFFFF
    poly = (((mac[5] + 11) << 8) | mac[4]) & 0xFFFF
    crc = crc16(COMMAND_PAYLOAD, init, poly)
    frame = (
        FRAME_HEADER
        + len(COMMAND_PAYLOAD).to_bytes(2, "big")
        + COMMAND_PAYLOAD
        + crc.to_bytes(2, "big")
    )
    key = mac_xor_key(address)
    return bytes(byte ^ key for byte in frame)


def decode_payload(payload: bytes) -> bytes:
    """把返回帧的 32 字节载荷异或 0x63，得到可解析的数据。"""
    return bytes(byte ^ DATA_XOR_KEY for byte in payload)


def parse_measurement(plain: bytes) -> dict[str, float]:
    """把已经完成 0x63 异或的数据载荷解析为指标字典。

    返回示例：{"power": 149.3, "voltage": 239.9, "current": 0.691}
    额外保留 power_factor、energy、cost，便于以后扩展界面。
    """
    result: dict[str, float] = {}

    for key, layout in METRIC_LAYOUT.items():
        offset = layout.get("offset")
        if offset is None:
            continue

        try:
            offset = int(offset)
            length = int(layout.get("length", 2))
            scale = float(layout.get("scale", 1.0))
            signed = bool(layout.get("signed", False))
            order = str(layout.get("order", "big"))
        except (TypeError, ValueError):
            logger.warning("指标 %s 的 METRIC_LAYOUT 配置非法，已跳过", key)
            continue

        if offset < 0 or length <= 0 or offset + length > len(plain):
            continue

        raw_value = int.from_bytes(
            plain[offset : offset + length],
            byteorder=order,
            signed=signed,
        )
        result[key] = raw_value * scale

    # 官方界面上的功率因数、金额也在同一数据段里，先放在结果中备用。
    if len(plain) > 17:
        result["power_factor"] = plain[17] / 100.0
    if len(plain) >= 28:
        result["cost"] = int.from_bytes(plain[26:28], "big") / 100.0

    return result


# ---------------------------------------------------------------------------
# 异步通信管理器
# ---------------------------------------------------------------------------

class BleManager:
    """在独立线程中运行 asyncio 事件循环，管理多台 BLE 插座。"""

    def __init__(
        self,
        on_data: DataCallback | None = None,
        on_status: StatusCallback | None = None,
        on_log: LogCallback | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self._on_data: DataCallback = on_data or (lambda *args: None)
        self._on_status: StatusCallback = on_status or (lambda *args: None)
        self._on_log: LogCallback = on_log or (lambda message: None)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._startup_error: BaseException | None = None

        self._lock = threading.RLock()
        self._devices: dict[str, dict[str, str]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._poll_interval = min(60.0, max(1.0, float(poll_interval)))
        self._scanning = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        """启动后台事件循环线程（幂等）。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return

            self._stopping.clear()
            self._ready.clear()
            self._startup_error = None
            self._thread = threading.Thread(
                target=self._thread_main,
                name="yunm-ble-asyncio",
                daemon=True,
            )
            self._thread.start()

        # 最多等 5 秒，避免事件循环创建失败时 GUI 一直无响应
        if not self._ready.wait(5.0):
            raise RuntimeError("蓝牙事件循环启动超时")
        if self._startup_error is not None:
            raise RuntimeError(f"蓝牙事件循环启动失败：{self._startup_error}")

        # 启动期间可能已经调用过 set_devices()，这里补一次调度，
        # 确保缓存下来的设备也会开始连接。
        self._submit(self._reconcile())

    def stop(self, timeout: float = 5.0) -> None:
        """停止事件循环并取消所有设备任务。"""
        self._stopping.set()
        with self._lock:
            thread = self._thread
            loop = self._loop

        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)

        if thread is not None and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                logger.warning("蓝牙线程未在 %.1f 秒内退出（daemon 线程）", timeout)

        with self._lock:
            self._thread = None
            self._loop = None
            self._tasks.clear()
        logger.info("蓝牙管理器已停止")

    def _thread_main(self) -> None:
        """线程入口：创建 asyncio loop 并永久运行。"""
        loop: asyncio.AbstractEventLoop | None = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            with self._lock:
                self._loop = loop
            # 注意：不能在这里直接 set ready。run_forever() 还没执行时
            # loop.is_running() 仍为 False，set_devices() 会误判为“未启动”。
            # 用 call_soon 把 set 放到 loop 真正开始运行之后。
            loop.call_soon(self._ready.set)
            logger.info("蓝牙 asyncio 事件循环已启动")
            loop.run_forever()
        except BaseException as exc:  # noqa: BLE001 - 需要把启动错误传回 start()
            self._startup_error = exc
            logger.exception("蓝牙事件循环异常退出")
            self._ready.set()
        finally:
            self._ready.set()
            if loop is not None:
                self._shutdown_loop(loop)
            with self._lock:
                self._loop = None

    @staticmethod
    def _shutdown_loop(loop: asyncio.AbstractEventLoop) -> None:
        """run_forever 返回后，取消残留任务并关闭 loop。"""
        try:
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
        except Exception:
            logger.exception("清理蓝牙事件循环时出错")
        finally:
            with contextlib.suppress(Exception):
                loop.close()

    # ------------------------------------------------------------------
    # 对外配置接口（可从 GUI 线程调用）
    # ------------------------------------------------------------------

    def set_devices(self, devices: list[dict[str, str]] | None) -> None:
        """更新要监控的设备列表；新增/删除会自动建立/取消对应任务。"""
        normalized: dict[str, dict[str, str]] = {}
        for item in devices or []:
            if not isinstance(item, dict):
                continue
            address = normalize_address(str(item.get("address", "")))
            if not is_valid_address(address):
                logger.warning("忽略非法蓝牙地址：%r", item.get("address"))
                continue
            name = str(item.get("name", "")).strip()[:40] or address[-5:]
            normalized[address] = {"name": name, "address": address}

        with self._lock:
            self._devices = normalized

        future = self._submit(self._reconcile())
        if future is None:
            logger.warning("蓝牙事件循环尚未启动，设备列表已缓存")

    def set_poll_interval(self, seconds: float) -> None:
        with self._lock:
            self._poll_interval = min(60.0, max(1.0, float(seconds)))
        logger.info("轮询间隔调整为 %.1f 秒", self._poll_interval)

    @property
    def poll_interval(self) -> float:
        with self._lock:
            return self._poll_interval

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._loop is not None and self._loop.is_running()

    # ------------------------------------------------------------------
    # 扫描
    # ------------------------------------------------------------------

    def scan(
        self,
        timeout: float = 8.0,
        on_done: ScanDoneCallback | None = None,
    ) -> None:
        """异步扫描附近 YUNM 设备。

        on_done(result, error) 会在蓝牙事件循环线程里被调用；
        main.py 的桥接层负责把结果转成 Qt Signal，不能直接操作 GUI。
        """

        async def runner() -> None:
            try:
                result = await self._scan(timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 扫描失败要反馈到界面
                logger.exception("扫描 BLE 设备失败")
                self._safe_callback(on_done, None, exc)
            else:
                self._safe_callback(on_done, result, None)

        future = self._submit(runner())
        if future is None:
            self._safe_callback(
                on_done, None, RuntimeError("蓝牙事件循环尚未启动")
            )

    async def _scan(self, timeout: float) -> list[dict[str, Any]]:
        """真正执行扫描的协程，按 MAC 去重并保留信号最强的结果。"""
        if self._scanning:
            raise BleakError("扫描正在进行中，请稍候")

        self._scanning = True
        found: dict[str, dict[str, Any]] = {}

        def on_detection(device: Any, advertisement: Any) -> None:
            # advertisement.local_name 通常比 device.name 更早可用
            name = (
                getattr(advertisement, "local_name", None)
                or getattr(device, "name", None)
                or ""
            ).strip()
            if not name.upper().startswith(DEVICE_NAME_PREFIX):
                return

            address = normalize_address(getattr(device, "address", ""))
            if not is_valid_address(address):
                return

            rssi = getattr(advertisement, "rssi", None)
            old = found.get(address)
            if (
                old is None
                or (rssi is not None and (old["rssi"] is None or rssi > old["rssi"]))
            ):
                found[address] = {
                    "name": name[:40],
                    "address": address,
                    "rssi": rssi,
                }

        scanner = BleakScanner(detection_callback=on_detection)
        try:
            await scanner.start()
            await asyncio.sleep(max(3.0, float(timeout)))
        finally:
            with contextlib.suppress(Exception):
                await scanner.stop()
            self._scanning = False

        # 信号强的排前面
        return sorted(
            found.values(),
            key=lambda item: (
                item["rssi"] is None,
                -(
                    item["rssi"]
                    if item["rssi"] is not None
                    else -999
                ),
            ),
        )

    # ------------------------------------------------------------------
    # 内部调度
    # ------------------------------------------------------------------

    def _submit(self, coroutine: Any) -> Any:
        """把协程提交到后台 loop；loop 未启动时安全关闭协程。"""
        loop = self._loop
        if loop is None or not loop.is_running():
            coroutine.close()
            return None
        try:
            return asyncio.run_coroutine_threadsafe(coroutine, loop)
        except RuntimeError:
            coroutine.close()
            logger.exception("提交蓝牙任务失败")
            return None

    async def _reconcile(self) -> None:
        """对比期望设备与运行中任务，做增删。只在 loop 线程执行。"""
        with self._lock:
            desired = dict(self._devices)

        for address, task in list(self._tasks.items()):
            if address not in desired:
                logger.info("停止监控设备 %s", address)
                task.cancel()
                self._tasks.pop(address, None)

        for address, config in desired.items():
            task = self._tasks.get(address)
            if task is None or task.done():
                self._tasks[address] = asyncio.create_task(
                    self._device_loop(config),
                    name=f"yunm-device-{address}",
                )
                logger.info("开始监控设备 %s (%s)", config["name"], address)
                self._emit_log(f"开始监控 {config['name']} ({address})")

    async def _device_loop(self, config: dict[str, str]) -> None:
        """单台设备的生命周期：连接 -> 订阅 -> 轮询 -> 断线重连。"""
        address = config["address"]
        name = config["name"]
        backoff = RETRY_INITIAL
        attempt = 0

        while not self._stopping.is_set():
            client: BleakClient | None = None
            disconnected = asyncio.Event()

            def on_disconnect(_client: BleakClient) -> None:
                # bleak 会在事件循环线程中回调这里
                disconnected.set()

            try:
                # 第一次叫“连接中”，重试时直接显示“重连中”（托盘为红色）
                self._emit_status(
                    address,
                    "connecting" if attempt == 0 else "reconnecting",
                    "正在搜索并连接设备…" if attempt == 0 else "连接已断开，正在重连…",
                )

                client = BleakClient(address, disconnected_callback=on_disconnect)
                await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
                if not client.is_connected:
                    raise BleakError("connect() 返回但设备未连接")

                # 校验 UUID，尽早给出明确错误，而不是每 3 秒写一次特征
                self._check_characteristics(client)

                self._emit_status(address, "connected", "已连接")
                logger.info("[%s] 已连接，开始订阅 TX 并轮询", name)
                backoff = RETRY_INITIAL
                notify_seen = False
                empty_polls = 0
                warned_no_notify = False
                command = build_command(address)
                rx_buffer = bytearray()

                def on_notify(_sender: Any, data: bytearray) -> None:
                    nonlocal notify_seen
                    notify_seen = True
                    self._handle_notification(address, name, data, rx_buffer)

                await client.start_notify(TX_CHAR_UUID, on_notify)
                attempt = 0
                logger.info("[%s] 查询命令: %s", name, format_hex(command))

                # 给模组一点准备时间，随后立即发送第一条查询指令
                await asyncio.sleep(0.2)

                while not self._stopping.is_set():
                    if disconnected.is_set() or not client.is_connected:
                        raise BleakError("BLE 连接已断开")

                    await self._write_command(client, command)
                    empty_polls += 1

                    if (
                        not notify_seen
                        and not warned_no_notify
                        and empty_polls >= 3
                    ):
                        warned_no_notify = True
                        logger.warning(
                            "[%s] 已连续写入 %d 次查询指令，但未收到 TX 通知；"
                            "请核对指令、UUID 或设备是否需要额外握手",
                            name,
                            empty_polls,
                        )
                        self._emit_log(
                            f"{name}: 已写入查询指令但未收到 TX 通知，"
                            "请核对协议参数"
                        )
                        self._emit_status(
                            address,
                            "connected",
                            "已连接，但尚未收到 TX 通知（请查看日志）",
                        )

                    # 用 Event 替代 sleep，断线时可以立刻醒来重连
                    try:
                        await asyncio.wait_for(
                            disconnected.wait(), timeout=self.poll_interval
                        )
                    except asyncio.TimeoutError:
                        pass

                # 主动停止时不必再发 stop_notify，disconnect 会清理订阅
                with contextlib.suppress(Exception):
                    await client.stop_notify(TX_CHAR_UUID)

            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 任何异常都进入重连流程
                if self._stopping.is_set():
                    break

                attempt += 1
                detail = self._short_error(exc)
                logger.warning("[%s] 通信异常：%s", name, detail)
                self._emit_status(
                    address,
                    "reconnecting",
                    f"{detail}；{backoff:.0f} 秒后重试",
                )

                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
                backoff = min(backoff * 2.0, RETRY_MAX)
            finally:
                if client is not None:
                    with contextlib.suppress(Exception):
                        await client.disconnect()

        logger.info("[%s] 设备任务已结束", name)

    @staticmethod
    def _check_characteristics(client: BleakClient) -> None:
        """连接后检查 Service/RX/TX 是否存在。"""
        try:
            services = client.services
        except BleakError:
            # 某些后端可能延迟发现服务；不阻塞连接，后续读写会给出真实错误
            logger.warning("无法读取 GATT 服务列表，跳过 UUID 预检查")
            return

        if services.get_service(SERVICE_UUID) is None:
            logger.warning("设备未暴露预期 Service UUID：%s", SERVICE_UUID)
        if services.get_characteristic(RX_CHAR_UUID) is None:
            raise BleakError(f"未找到 RX 写特征：{RX_CHAR_UUID}")
        if services.get_characteristic(TX_CHAR_UUID) is None:
            raise BleakError(f"未找到 TX 通知特征：{TX_CHAR_UUID}")

    @staticmethod
    async def _write_command(client: BleakClient, command: bytes) -> None:
        """向 RX 写入按设备 MAC 生成的查询命令。"""
        try:
            await client.write_gatt_char(RX_CHAR_UUID, command, response=False)
        except Exception as first_error:  # noqa: BLE001
            logger.debug("Write Without Response 失败，改用带响应写：%s", first_error)
            await client.write_gatt_char(RX_CHAR_UUID, command, response=True)

    # ------------------------------------------------------------------
    # 数据解密与回调
    # ------------------------------------------------------------------

    def _handle_notification(
        self,
        address: str,
        name: str,
        data: bytearray | bytes,
        rx_buffer: bytearray,
    ) -> None:
        """TX 通知回调：MAC XOR -> 拼接分片 -> 0x63 解数据 -> 解析。"""
        raw = bytes(data)
        if not raw:
            logger.debug("[%s] 收到空通知", name)
            return

        xor_key = mac_xor_key(address)
        plain = bytes(byte ^ xor_key for byte in raw)
        logger.info("[%s] RX 密文(%d 字节): %s", name, len(raw), format_hex(raw))
        logger.info("[%s] RX MAC-XOR(%d 字节): %s", name, len(plain), format_hex(plain))

        # BLE 通知可能把 38 字节的数据帧拆成 20 + 18 两包。
        rx_buffer.extend(plain)
        while len(rx_buffer) >= 4:
            if rx_buffer[:2] != FRAME_HEADER:
                header_at = rx_buffer.find(FRAME_HEADER)
                if header_at < 0:
                    logger.warning("[%s] 丢弃无帧头数据: %s", name, format_hex(bytes(rx_buffer)))
                    rx_buffer.clear()
                    return
                del rx_buffer[:header_at]

            length = int.from_bytes(rx_buffer[2:4], "big")
            total = length + FRAME_OVERHEAD
            if length <= 0 or length > 4096:
                logger.warning("[%s] 非法帧长度 %d，清空缓冲", name, length)
                rx_buffer.clear()
                return
            if len(rx_buffer) < total:
                return

            frame = bytes(rx_buffer[:total])
            del rx_buffer[:total]
            payload = frame[4 : 4 + length]
            data_plain = decode_payload(payload)

            logger.info(
                "[%s] RX 完整帧(%d 字节): %s", name, len(frame), format_hex(frame)
            )
            logger.info(
                "[%s] RX 数据明文(%d 字节): %s",
                name,
                len(data_plain),
                format_hex(data_plain),
            )

            try:
                measurement = parse_measurement(data_plain)
            except Exception:  # noqa: BLE001 - 解析错误不能中断蓝牙循环
                logger.exception("[%s] 解析数据帧失败", name)
                measurement = {}

            if measurement:
                logger.info(
                    "[%s] 解析结果: 功率=%.1fW 电压=%.1fV 电流=%.3fA",
                    name,
                    measurement.get("power", 0.0),
                    measurement.get("voltage", 0.0),
                    measurement.get("current", 0.0),
                )
            self._emit_data(
                address,
                measurement,
                format_hex(data_plain),
                format_hex(frame),
            )

    @staticmethod
    def _short_error(exc: BaseException) -> str:
        text = str(exc).strip() or exc.__class__.__name__
        return text if len(text) <= 160 else text[:157] + "..."

    @staticmethod
    def _safe_callback(callback: Any, *args: Any) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:  # noqa: BLE001 - 回调异常不能影响通信线程
            logger.exception("蓝牙回调执行失败")

    def _emit_status(self, address: str, status: str, message: str) -> None:
        if self._stopping.is_set():
            return
        self._safe_callback(self._on_status, address, status, message)

    def _emit_log(self, message: str) -> None:
        self._safe_callback(self._on_log, message)

    def _emit_data(
        self,
        address: str,
        measurement: dict[str, float],
        plain_hex: str,
        raw_hex: str,
    ) -> None:
        if self._stopping.is_set():
            return
        self._safe_callback(
            self._on_data, address, measurement, plain_hex, raw_hex
        )
