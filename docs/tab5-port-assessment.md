# MediaCtrl 迁移到 M5Stack Tab5 可行性评估

> 评估对象：本目录的 iAP2 配件实现（`auth_test.py`、`web_app.py`、
> `ref_iap2_link_layer.py`、`hid_async.py`、`csm.py`、`mfi_local.py`）。
> 目标硬件：M5Stack Tab5（ESP32-P4 + ESP32-C6）。
> 评估日期：2026-11-03；媒体库同步与 HID 报告结论于 2026-11-04 更新。

## 结论

**硬件完全够，甚至有余量；但现有项目不能直接跑，需要按 ESP-IDF 用
C/C++ 重写。重写后 Now Playing、封面、播放控制可以很流畅，真正的风险在
USB Host/iAP2 适配，不在 Tab5 的性能。**

一句话：**Tab5 跑得动，而且能跑得流畅；但它不是“迁移”，是“重写”。**

## 1. 为什么不能直接跑

Tab5 是 **ESP32-P4 单片机**（360 MHz 双核 RISC-V、32 MB PSRAM、
16 MB Flash），不是 Linux 主机。现有工程依赖：

- CPython 3.14 + asyncio
- pyusb / hidapi（Linux USB Host）
- cryptography（MFi P-256 签名）
- http.server（Web UI）
- Pillow（封面验证与缩略图生成）

这些在 Tab5 上都没有现成对应物。MicroPython 虽然有 ESP32-P4 移植，但
**没有 USB Host 栈**，也没有 pyusb/hidapi/cryptography，不能作为捷径。
所以必须用 ESP-IDF 重写；核心协议代码约 1,500 行，Web UI/媒体库逻辑约 4,400 行，
C 版本预计 4,000–6,000 行（不含完整 UI）。

## 2. 硬件核对：很合适

| 项目 | Tab5 | 对本项目 |
|---|---|---|
| CPU | P4 双核 360 MHz | 跑链路层、CSM、HTTP 绰绰有余 |
| 内存 | 768 KB SRAM + 32 MB PSRAM | 181 KB 封面、65 KB 帧、JPEG 解码缓冲都够 |
| USB-A | P4 USB2 OTG **HS Host**（480 Mbps） | iPod 用 USB-A 转 Lightning，正合适 |
| USB-C | P4 USB1 OTG（FS） | 用于供电/烧录，不接 iPod |
| 屏幕 | 5" 1280×720 MIPI-DSI + PPA | LVGL 简单播放器 UI 可到 30 fps+ |
| 解码 | 硬件 JPEG 解码器 | 600×600 封面解码几毫秒 |
| 无线 | ESP32-C6，Wi-Fi 6（SDIO） | 可继续提供 Web UI，但多一层 esp_hosted |
| 供电 | NP-F550 7.4 V 2000 mAh，USB-A 5 V 负载开关 | 能带 iPod，但边充边用续航会缩短 |

关键数据：iPod 的 HID 端点 `0x83` 是 **64 字节 / bInterval=1**。在 HS 下
理论 512 KB/s；Linux 上实测 181 KB 封面约 **0.4 秒**完成。Tab5 的 USB-A
也走 HS，量级相同，封面不会成为瓶颈。

## 3. 重写后的流畅度预估

- 按键 → iPod：**< 50 ms**，体感即时
- Now Playing 更新：iPod 本身约 0.5 s 一次
- 181 KB 封面：**0.4–1 s**（HS，取决于 USB Host 栈调度）
- 600×600 JPEG 解码：硬件解码几毫秒
- 720p LVGL 简单界面：**30 fps+**，静态播放器 UI 很流畅；全屏复杂动画不保证 60 fps
- 开机到可用：约 2–5 s（含 Wi-Fi）
- CPU/内存占用：很低，32 MB PSRAM 用不到一半

## 4. 软件重写范围

| 模块 | 现有实现 | Tab5 上需要做什么 |
|---|---|---|
| USB Host | Linux libusb/hidapi | ESP-IDF USB Host；选 config 2，claim interface 2，读 EP `0x83`，control transfer 发 `SET_REPORT` |
| HID | libhidapi 自动处理 report ID/LCB | 绕过标准 HID class driver，按 iPod 的 report ID/LCB 规则直接收发 |
| 链路层 | Python asyncio | C/FreeRTOS，逻辑可照搬；注意 RTOS 并发与缓冲 |
| CSM | `csm.py` | C 版 TLV 编解码，逻辑简单 |
| MFi | cryptography P-256 | mbedTLS ECDSA P-256；证书/私钥预置，Flash 加密 |
| 封面 | Pillow 验证/缩略图 | P4 硬件 JPEG 解码 + PSRAM 缓冲 |
| UI | 浏览器 HTML | Tab5 没有浏览器；用 LVGL 重写，或继续由手机/PC 访问 Tab5 提供的 Web UI |
| 媒体库 | 4C01/4C04 | 已实现全量/增量同步与专辑/艺术家/歌曲索引；Tab5 上需用 SQLite/分页持久化，不能照搬整库 JSON |

## 5. 真正的风险点

1. **多配置枚举**：iPod 有 5 个 configuration，必须选 config 2。ESP-IDF
   需要打开 `CONFIG_USB_HOST_ENABLE_ENUM_FILTER_CALLBACK`，用枚举过滤
   回调选 config 2。这是第一个要验证的点。
2. **HID 不是标准用法**：iPod 的 208 字节 report descriptor 和设备实际
   发送方式不标准，标准 HID class driver 可能不适用。建议绕过 HID 驱动，
   直接 claim interface 2，用 control transfer 发 `SET_REPORT`，用
   interrupt transfer 读 `0x83`。
3. **HID 报告头必须剥离**：iPod 的每个输入报告包含 `[report_id][LCB]` 和填充；
   如果把报告头当成 iAP2 字节流，会在大帧中每 766 字节插入 2 字节，表现为 FT
   length/checksum 错误和封面撕裂。剥头后 link length 和 checksum 是标准的；
   >64 KB 的 FT 数据报会拆成多个 link 帧，每帧 `length=0xFFFF`。C 重写必须保留
   报告重组，不能把原始 URB 直接拼接。
4. **链路层是实验级**：现在是 per-packet ACK、容忍一个 seq gap，没有
   完整窗口/重传。RTOS 并发和 USB Host 回调时序与 asyncio 不同，可能
   暴露竞态，生产前要补状态机。
5. **USB-A 供电**：要实测 iPod 充电时的 5 V 电压/电流；Tab5 的 USB-A 有
   负载开关，带 iPod 应该可以，但电池续航会明显下降。
6. **MFi 证书**：这是 NDA 开发证书。Tab5 上建议开 Flash 加密/NVS 加密，
   但它仍然不是“已获 MFi 认证的产品”，对外销售要先解决授权。
7. **媒体库数据量和持久化**：完整媒体库同步已实现（20,564 首），但 4C04 数据量大，
   Tab5 上需要用 SQLite/分页并处理 WAL/掉电安全；AFC 数据库约 50 MB，不能放入
   16 MB Flash 或 32 MB PSRAM。
8. **Web UI 不能直接显示**：Tab5 没有浏览器。要么用 LVGL 重写，要么把
   现有 HTML 通过 Wi-Fi 给手机/PC 看；后者 Tab5 只当服务器。

## 6. 建议的验证顺序（PoC）

先花几天做最小闭环，任何一步不通都应及时止损：

1. USB Host 枚举 iPod，选 config 2，读到 HID report descriptor。
2. 发 detect marker（`FF 55 02 00 EE 10`），收到 `FF 5A` 链路帧。
3. MFi 认证：`0xAA00 → 0xAA01 → 0xAA02 → 0xAA03 → 0xAA05`。
4. Identification（含 HID 组件 `0x0012`）被接受。
5. `0x5000/0x5001` 拿到真实 Now Playing。
6. `0x6802` 发 play/pause，iPod 状态变化。
7. FT session 12 收到 200×200 与 600×600 封面。

## 7. 工作量建议

| 路线 | 工作量 | 说明 |
|---|---|---|
| Tab5 独立重写（PoC：USB config 2 + 认证 + Now Playing + HID） | 1–2 周 | 熟悉 ESP-IDF/USB 的开发者 |
| Tab5 完整独立设备（LVGL + 封面 + 重连 + OTA） | 1–2 个月 | 产品级再加 1 个月 |
| Tab5 只做显示/控制端，Linux 继续做 USB 主机 | 约 1 周 | 最稳、最省事，但 Tab5 不是独立设备 |
| 换 Linux 小板（Pi Zero 2 W / Radxa Zero 3W 等） | 1–3 天 | 现有 Python 几乎不改，但没有 Tab5 的集成度和屏幕 |

## 8. 最终判断

- **想要 Tab5 独立跑**：值得做，硬件没问题；先做 USB Host PoC，只要能
  枚举到 config 2、收到 `FF 5A` 链路帧并完成 MFi 认证，后面就顺了。
- **想少改代码**：不要选 Tab5，选 Linux 小板；或者让 Tab5 只做屏，
  Linux 继续做 USB 主机。
- **想做产品**：先确认 MFi 授权和证书安全，再谈 UI 和量产。

## 参考

- 本目录 `HANDOVER.md`：协议、认证、HID、FT、媒体库的实测结论
- 本目录 `ft-file-transfer.md`：FT 会话与命令速查
- M5Stack Tab5 文档：<https://docs.m5stack.com/en/core/Tab5>
- ESP-IDF USB Host 文档：<https://docs.espressif.com/projects/esp-usb/en/latest/esp32p4/usb_host.html>
- ESP-IDF JPEG 解码器文档：<https://docs.espressif.com/projects/esp-idf/en/stable/esp32p4/api-reference/peripherals/jpeg.html>
