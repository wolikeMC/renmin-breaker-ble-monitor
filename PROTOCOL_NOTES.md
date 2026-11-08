# YUNM 官方小程序协议分析笔记

> **2026-11-08 更新：协议已完全打通。** 命令可按 MAC 本地生成，返回数据
> XOR `0x63` 后可直接解析功率/电压/电流/电量；项目已离线读取成功。
> 下面的“云端下发”描述是官方 App 的流程，保留作为分析过程记录。

> 本文只记录互操作分析结论，不包含官方小程序源码。
> 来源：微信 4.x 在本机的小程序缓存（AppID `wx69980405ad3c47f6`），
> 已用离线方式解密并只读分析。

## 结论先行

当前上位机里的这条假设：

```text
固定查询指令：D4 D5 D5 C4 DD
收到数据：逐字节 XOR 0x91
```

**对这台 YUNM 计量插座不成立。**

官方小程序不是“本地固定指令 + 固定 XOR”的模型，而是：

1. 手机先连接插座；
2. 通过已登录的 MQTT/HTTP 会话向云端请求一条
   `meter_ble_read` 指令；
3. 云端返回一段**完整的 BLE 写入包**（字段名 `code`）；
4. 小程序把这段 `code` 原样写到 RX；
5. 插座返回后，小程序把返回内容再发给云端；
6. 云端返回下一步指令或最终的总电量/剩余电量/开关状态；
7. 整个过程可能有三步。

也就是说，**BLE 包是服务端按会话下发的**，不是本地能拼出来的固定 5 字节。

## 真实 BLE 帧特征

从官方代码中确认到：

- Service：`49535343-fe7d-4ae5-8fa9-9fafd205e455`
- RX：`49535343-8841-43f4-a8d4-ecbe34729bb3`
- TX：`49535343-1e4d-4bd9-ba61-23c647249616`
- 设备名前缀：`YUNM`

长包帧使用 MAC 派生密钥：

```text
xor_key = (MAC 六个字节之和) & 0xFF
crc_init = (MAC[5] + MAC[4] + 10) & 0xFFFF
crc_poly = ((MAC[5] + 11) << 8 | MAC[4]) & 0xFFFF
```

帧头按类型为：

```text
EC ED
CE DE
93 92
F0 0F   （另一类“开锁/打开”包）
```

帧内还有两字节长度、CRC16，最后对整包逐字节 XOR `xor_key`。
部分载荷还要经过 AES-ECB 加解密，密钥同样与 MAC 有关。

因此：

- 不是固定 `XOR 0x91`；
- 不是固定 5 字节；
- 不是随便写一条指令就会回包。

## 为什么会出现“写成功但没通知”

- BLE 写入成功只代表 GATT 写操作完成；
- 插座固件收到不完整/不合法帧后直接丢弃，不会回 TX；
- 官方小程序能从云端拿到合法包，所以它能工作；
- 本地只发 `D4 D5 D5 C4 DD` 时，固件不会响应。

这也解释了配对测试结果：

```text
CONNECTED
PAIR_FAILED: ACCESS_DENIED
10 次写入成功
TX 通知数量 0
```

配对不是原因；指令/会话才是。

## 2026-11-08：HCI 抓包 + 命令重放成功

通过在一台 Android 手机上启用 `/data/log/bt` 的 HCI 日志，抓到了官方 App 的
真实 ATT 交互：

```text
写入 handle 0x0013：
E1 E0 0D 0E 46 4E 46 53 24
```

对示例设备，按 MAC 计算 `xor_key = 0x0D`，异或后为：

```text
EC ED 00 03 4B 43 4B 5E 29
```

- 帧头 `EC ED`
- 长度 `00 03`
- 载荷 `4B 43 4B`
- CRC16 `5E 29`（与 MAC 派生的 init/poly 匹配）

用这段命令在 Windows 上重放，插座确实返回了 TX 通知：

```text
EC ED 00 20 ...
```

因此 BLE 链路和命令本身已验证。返回帧格式：

```text
EC ED | 00 20 | 32 字节数据 | CRC
```

32 字节数据再与 `0x63` 异或后得到明文。用官方界面数值
（功率 149.3 W / 电压 239.9 V / 电流 0.69 A）对照，字段已确认：

| 字段 | 偏移 | 长度 | 解析 |
| --- | ---: | ---: | --- |
| 累计电量 | 5 | 2 | 大端 / 100，单位 kWh |
| 功率 | 11 | 2 | 大端 / 10，单位 W |
| 电压 | 13 | 2 | 大端 / 10，单位 V |
| 电流 | 15 | 2 | 大端 / 1000，单位 A |
| 功率因数 | 17 | 1 | / 100 |
| 累计金额 | 26 | 2 | 大端 / 100，单位元 |

例如抓到的数据载荷：

```text
08 1D 00 00 00 2C 16 00 01 AD A8 05 D5 09 5F 02 B3 5A ...
```

解析结果：

```text
电量 = 0x2C16 / 100 = 112.86 kWh
功率 = 0x05D5 / 10  = 149.3 W
电压 = 0x095F / 10  = 239.9 V
电流 = 0x02B3 / 1000 = 0.691 A
功率因数 = 0x5A / 100 = 0.90
金额 = 0x160B / 100 = 56.43 元
```

`ble_manager.py` 已按这个格式实现：

- 按 MAC 生成命令和 CRC；
- 拼接 20 + 18 字节分片；
- 做 MAC XOR 和 `0x63` 解数据；
- 解析功率/电压/电流/电量。

## 历史路线（现已不需要）

### A. 抓官方 App 的云端响应（比 HCI 更直接）

官方 `meter_ble_read` 的云端响应中就有完整的 BLE `code`。
如果能截获这次 MQTT/HTTP 响应，就可以直接得到可重放的包。

### B. 抓官方 App 的 JS 日志

官方代码里有 `ymDevLog` 和 `ymBleWriteBuf` 日志。
可以尝试：

```powershell
adb logcat -c
adb logcat -v time | Select-String -Pattern "ymBle|YUNM|49535343|writeCharacteristic"
```

然后打开官方小程序读一次数据，观察是否把 `ymBleWriteBuf` 的 buffer
打到了 logcat。若能看到，就能直接拿包，不需要 HCI。

### C. 硬件 BLE 嗅探

nRF52840 + nRF Sniffer + Wireshark，直接抓手机和插座之间的 ATT 包。
这是不 root、不依赖厂商云端的最可靠路线。

### D. Root 手机 + Frida

Hook `BluetoothGatt.writeCharacteristic`，直接打印写入 byte[]。
比抓 HCI 简单，但需要 root。

## 当前上位机的处理

- 已用真实命令离线读取，不依赖官方小程序或云端；
- 扫描、连接、订阅、分片拼接、解密、解析、断线重连、托盘、开机自启全部保留；
- 当前 `config.json` 中已保存实际设备，启动即可读取；
- 如果换另一台插座，命令会按 MAC 重新生成，数据解析偏移目前按同系列固件处理。

## 历史排查记录（已不再需要）

- 测试用 Android 手机上 `/data/misc/bluetooth/logs` 普通 ADB 不可读；
- `setprop` 被拒绝，`adb root` 不可用；
- 最终通过 `settings put secure bluetooth_btsnoop_default_mode 2` 和
  `settings put global bluetooth_hci_snoop_log 1`，并重启蓝牙，
  成功写出 `/data/log/bt/btsnoop_hci_*.log`；
- 从这个日志中抓到了真实命令和返回帧。

## 旧的后续路线（已完成，无需执行）

硬件嗅探器、root/Frida、抓云端响应都不再是必需项。

