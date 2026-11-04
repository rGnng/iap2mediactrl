# Tab5 单独取 MediaLibrary.sqlitedb 可行性调研

> 目标：只实现“从 iOS device 取 `MediaLibrary.sqlitedb`”这一个功能，是否能在
> M5Stack Tab5（ESP32-P4，16 MB Flash / 32 MB PSRAM / microSD / USB-A Host）
> 上独立完成；如果空间不够，精简 `libimobiledevice` 是否现实。
>
> 调研日期：2026-11-04。实测设备：iPod touch，iOS 15.7.8，USB 05ac:12aa。

## 0. 结论摘要

1. **能实现，而且不需要 iAP2/MFi/HID。** 取 DB 走的是另一条独立协议栈：
   USB 配置 3/4 → Apple USB Multiplexor → usbmux v2 → lockdown → AFC。
   现有 iAP2 的配置 2（HID）和它互斥，不能同时用。
2. **真正的限制不是 16 MB Flash，而是 48.3 MiB 的数据库。**
   代码本身预计 1.5–2.5 MB，16 MB Flash 绰绰有余；32 MB PSRAM 也放不下
   50 MB 的 DB。必须写 microSD，或者通过 Wi-Fi 流式转给手机/PC。
   如果确定有 16 GB SD 专门放中转数据和 album art，这个限制就消失了；
   剩下要算的只是固件/分区和 PSRAM 预算（见 §3.1）。
3. **“精简 libimobiledevice”不现实，也不是省空间的好办法。**
   `libimobiledevice` 只做 lockdown/AFC；USB 传输在 `usbmuxd`（GPL，
   约 5,300 行 C，依赖 libusb/POSIX/udev/Unix socket）。ESP-IDF 没有
   libusb host，也没有 usbmuxd。要在 Tab5 上用它，等于把 usbmuxd 也重写。
   自己写一个最小客户端反而更小、更可控。
4. **一个关键实测结论：AFC 服务不需要 TLS。**
   本机实测 `com.apple.afc` 的 `EnableServiceSSL = 0`；但 lockdown 的
   `StartSession` 返回 `EnableSessionSSL = 1`。所以只需要用 mbedTLS 保护
   lockdown 那一小段握手，AFC 数据本身是明文。这比“全链路 TLS”简单很多。
5. **最大的产品风险是配对，不是代码。**
   当前 iOS device 已经和 Linux 主机配对，可以直接把 9.4 KB 的 pair record
   复制到 Tab5。若要支持“任意新设备/首次配对”，还要实现 RSA 密钥/证书生成、
   Trust 弹窗流程，工作量会明显上升。

## 1. 实测结果（不是纸面推算）

在现有 Linux 主机上，把 iOS device 切到 usbmux 配置后实测：

| 项目 | 实测值 |
|---|---|
| 设备 | iPod touch，iOS 15.7.8 |
| usbmuxd 选择的配置 | configuration 4（PTP + Apple Mobile Device + Ethernet） |
| 也可用的最小配置 | configuration 3（PTP + Apple Mobile Device；结构上有 mux interface，建议 PoC 单独验证） |
| mux 接口 | interface 1，class 0xff/0xfe/0x02，EP 0x04 OUT / 0x85 IN，bulk 512 |
| AFC 根目录 | `/iTunes_Control/iTunes/MediaLibrary.sqlitedb` |
| 主 DB 大小 | **50,552,832 B（48.2 MiB）** |
| `-wal` | 156,592 B |
| `-shm` | 32,768 B |
| 合计 | 约 **50.7 MB** |
| Linux AFC 读取 50 MB | 1.88 s，约 **28.8 MB/s** |
| `lockdownd StartSession` | `EnableSessionSSL = 1`（需要 TLS） |
| `com.apple.afc` | `EnableServiceSSL = 0`（**不需要 TLS**） |
| `com.apple.mobile.house_arrest` | `EnableServiceSSL = 1`（对照，需要 TLS） |

补充：

- 设备未越狱，AFC 只暴露 `/var/mobile/Media` 这棵子树，正好包含
  `iTunes_Control/iTunes/MediaLibrary.sqlitedb`。
- 设备需要处于解锁/已信任状态；锁屏时可能返回 `PasswordProtected`。
- 复制时建议主库、`-wal`、`-shm` 一起取。本次主库单独打开
  `PRAGMA integrity_check` 也是 ok，但 WAL 模式不能保证每次都这样。
- Linux 上 28.8 MB/s 不代表 Tab5；Tab5 受 ESP-IDF USB Host、SD 卡和
  FATFS 影响，预计 5–15 MB/s，50 MB 大约 3–10 秒。

## 2. Tab5 上需要写什么

如果只做“取 DB”这一个功能，最小固件只需要下面几层：

```text
USB-A (P4 HS Host, 480 Mbps)
  └─ 枚举时选 configuration 3（或 4；3 更小，需 PoC 确认），claim interface 1
      └─ usbmux v2：version 握手 + setup 0x07 + TCP-like SYN/ACK
          ├─ lockdown (port 62078)：StartSession（TLS）→ StartService
          └─ AFC (动态端口)：FILE_OPEN / FILE_READ / FILE_CLOSE（明文）
              └─ 写 microSD（FATFS）或 HTTP 流式转发
```

不需要：

- iAP2、HID、MFi 认证、媒体库 0x4C03/0x4C04、封面 FT；
- SQLite（如果只是把文件取走）；
- 蓝牙、AVRCP、Web UI（可以只做几个按钮/进度条）。

### 2.1 最小协议栈的工作量

| 模块 | 说明 | 预计代码量 |
|---|---|---|
| USB Host 板级 | PI4IOE 初始化、USB-A 5V 使能、枚举过滤选 config 3 | 200–400 行 |
| usbmux v2 | 单设备、2 条 TCP 流，SYN/ACK、序号、窗口、ZLP | 300–600 行 |
| lockdown | XML plist 组包/解包、StartSession、TLS、StartService | 400–700 行 |
| AFC | 40 字节头、open/read/close、错误码 | 200–300 行 |

lockdown 的实际顺序是：

```text
连 62078
  → QueryType（明文）
  → StartSession（明文请求，带 HostID / SystemBUID）
  ← 响应里有 SessionID + EnableSessionSSL=1
  → 同一条流升级 TLS（客户端证书/私钥用 pair record 里的
     RootCertificate + RootPrivateKey，服务端证书不校验）
  → StartService "com.apple.afc"（TLS 内）
  ← Port + EnableServiceSSL=0
另开一条 mux TCP 流到该 Port，AFC 明文收发
```

`libimobiledevice` 的 mbedTLS 路径就是用 `RootCertificate` 当客户端证书、
`RootPrivateKey` 当私钥；`HostCertificate`/`HostPrivateKey` 不是这条路径
实际使用的字段。pair record 约 9.4 KB，可以只把需要的字段预置到 NVS。
| 存储/传输 | SD 写入、临时文件+rename，或 HTTP chunked | 200–400 行 |
| UI/状态 | LVGL 进度、错误提示、重试 | 200–400 行 |
| **合计** | 不含 iAP2 | **约 1,500–3,000 行 C** |

有 ESP-IDF/USB 经验的开发者，**5–10 个工作日**可以做出能跑通的最小版本；
不熟悉 USB Host/协议栈的话，2–3 周更现实。

## 3. 空间账：Flash、PSRAM、SD 要分开看

| 资源 | Tab5 | 这个功能需要 | 结论 |
|---|---|---|---|
| Flash | 16 MB | 固件 1.5–2.5 MB（含 mbedTLS、FATFS、USB Host） | **足够**，即使双 OTA 分区也够 |
| PSRAM | 32 MB | USB/TLS/AFC 缓冲、FATFS 缓存，< 2–4 MB | **足够** |
| 内部 Flash 存 DB | 16 MB | 50.7 MB | **放不下，不要尝试** |
| PSRAM 存 DB | 32 MB | 50.7 MB | **放不下** |
| microSD | 有卡槽 | 50.7 MB + 余量 | **推荐**，≥ 256 MB 卡即可 |
| Wi-Fi 流式转发 | C6 Wi-Fi 6 | 只需几 KB 缓冲 | **可选**，不占 SD |

所以：

- **“空间不够”要分清是哪种空间。** 代码空间完全够；数据空间不够。
- 如果只是把 DB 交给手机/PC，Tab5 可以完全不用 SD/SQLite，做成
  “USB-AFC → HTTP 下载”桥；50 MB 走 Wi-Fi，10–30 秒量级。
- 如果 Tab5 要自己解析 `date_added`，才需要 SD + SQLite（或自己解析
  SQLite 文件）。最终日期表位于 `media-cache.db`，体积很小。

### 3.1 16 GB SD + 完整 libimobiledevice 的体积估算

先明确“完整实现”指什么：

- **A. 完整 libimobiledevice 服务库**（30 个 service，含 backup/restore、
  installation_proxy、debugserver 等）：源码约 16,100 行；Linux aarch64
  上 `.text + .rodata` 约 **180 KB**（已 strip，-O2）。
- **B. 完整 USB 栈**：还需要 usbmuxd 的 USB/mux 层（约 5,300 行，
  `.text + .rodata` 约 **54 KB**）、libplist（约 **70 KB**）、
  libimobiledevice-glue（约 **47 KB**）、libusbmuxd（约 **20 KB**）。
- **C. TLS**：用 ESP-IDF 自带 mbedTLS，按 TLS client + RSA/X.509 配置，
  预计 **350–600 KB**。

所以协议栈本体的代码量很小：

| 组成 | Flash 估算 | 说明 |
|---|---|---|
| libimobiledevice（全部 service） | 180–300 KB | aarch64 实测 180 KB；RISC-V 可能略大 |
| libplist | 70–120 KB | XML/二进制 plist |
| libimobiledevice-glue | 50–90 KB | socket/thread/collection |
| libusbmuxd + usbmuxd mux/USB | 70–150 KB | USB 层改为 ESP-IDF Host 后会变化 |
| mbedTLS | 350–600 KB | 不要用 OpenSSL |
| **协议栈小计** | **0.7–1.3 MB** | |
| ESP-IDF（Wi-Fi、lwIP、USB Host、FATFS、VFS、FreeRTOS） | 1.0–1.6 MB | 取决于组件 |
| LVGL + 字体 + UI | 0.3–0.8 MB | 字体可大可小 |
| 应用逻辑（AFC、缓存、JSON、日志） | 0.2–0.5 MB | |
| **固件合计** | **约 2.5–4.5 MB** | release 构建 |

结论：**16 MB Flash 放得下，而且可以保留双 OTA。** 一个可用的分区示例：

```text
# name,     type, subtype,  offset,    size
nvs,        data, nvs,      0x9000,    0x6000
otadata,    data, ota,      0xF000,    0x2000
phy_init,   data, phy,      0x11000,   0x1000
ota_0,      app,  ota_0,    0x20000,   0x500000   # 5 MB
ota_1,      app,  ota_1,    0x520000,  0x500000   # 5 MB
storage,    data, littlefs, 0xA20000,  0x5E0000   # 6 MB
```

- 双 5 MB OTA：单槽可放 4.5 MB 固件，仍有约 0.5 MB 余量；如果实际
  固件只有 3 MB，余量更充足。
- 内部 6 MB `storage` 放配置、证书、UI 资源、日志即可；DB 和 album art
  全部放 16 GB SD，不占内部 Flash。
- 如果以后固件超过 5 MB，可改成双 6 MB OTA + 3.7 MB storage；16 MB
  仍然够。只有改用 OpenSSL 才会明显吃紧（+5–7 MB）。

**PSRAM 预算**（32 MB）：

| 项 | 估算 |
|---|---|
| USB/mux 缓冲 | 0.5–2 MB |
| TLS / plist / JSON 临时 | 1–4 MB |
| LVGL 帧缓冲（1280×720） | 1.8 MB（RGB565，单缓冲）～3.6 MB（双缓冲） |
| 600×600 封面解码 | 0.7–1.5 MB |
| SQLite 页缓存（如果用） | 2–8 MB |
| 其他/栈/堆 | 1–2 MB |
| **合计** | **约 7–20 MB**，32 MB 够用 |

注意：不要把 50 MB 的 AFC DB 或完整的 `iap2.db` 媒体库镜像整个读进 PSRAM。
DB 放 SD，媒体库用 SQLite/分页；否则 32 MB 会被吃得很紧。

**16 GB SD 的容量预算**：

| 数据 | 估算 |
|---|---|
| DB + WAL + 一份备份 | 约 100–150 MB |
| album art（按专辑 600×600 JPEG，约 2,095 张 × 150–250 KB） | 约 300–500 MB |
| 如果按曲目存 600×600（20k 首） | 约 3–5 GB |
| 日志/临时/其他中转 | < 100 MB |
| **典型合计** | **< 1 GB**；16 GB 很充裕 |

建议：album art 按**专辑**存 200×200 或 300×300 缩略图（每张 15–40 KB，
全库约 30–80 MB），Now Playing 再单独存 600×600。这样 16 GB 用不到
10%，也给 DB 备份、日志和以后功能留足空间。注意 16 GB 卡通常是
SDHC，建议格式化为 **FAT32**；FAT32 单文件上限 4 GB，DB/封面都没问题，
但如果以后要存整机备份（可能 >4 GB），需要 exFAT 或分卷。

## 4. 精简 libimobiledevice 现实吗？

### 4.1 它不是一个“单库”

| 组件 | 作用 | 代码量（约） | Tab5 上的问题 |
|---|---|---|---|
| libimobiledevice | lockdown、AFC、service 抽象 | 6,200 行核心 | 依赖 POSIX socket/pthread/文件系统 |
| libplist | XML/二进制 plist | 10,900 行 | 可移植，但会占代码空间 |
| libusbmuxd | 连 usbmuxd 的 Unix socket 客户端 | 1,800 行 | **不能直连 USB**，需要 usbmuxd |
| usbmuxd | USB ↔ TCP-like mux 守护进程 | 5,300 行 | 依赖 libusb、udev、Unix socket、poll |
| OpenSSL/mbedTLS | TLS | mbedTLS 约 300–500 KB | libimobiledevice 支持 mbedTLS，可复用 |

编译后体积并不大：Linux 上 `libimobiledevice` 323 KB、libplist 131 KB、
libusbmuxd 66 KB、glue 131 KB；即使加上 mbedTLS，也就 1–2 MB 量级。
**所以“Flash 不够所以精简 libimobiledevice”这个前提本身不成立。**

### 4.2 真正的工作量在 usbmuxd

`libimobiledevice` 通过 `libusbmuxd` 连本机的 `/var/run/usbmuxd`，它自己
不碰 USB。ESP-IDF 上没有 usbmuxd，也没有 libusb host。你要么：

1. 把 usbmuxd 的 USB 层、TCP-like mux、client 多路复用、pairing 管理
   全部移植到 ESP-IDF（5,000+ 行，且是 GPL-2.0/3.0）；
2. 或者写一个只服务本功能的 in-process 最小 mux（300–600 行），
   再调用/移植 lockdown + AFC。

方案 2 明显更现实。也就是说：**“精简 libimobiledevice”最终会变成
“重写 usbmuxd 的 USB 传输 + 保留 libimobiledevice 的上层”**，并不比
自己写一个最小客户端省事。自己写还能避开 GPL/LGPL 的静态链接合规问题。

### 4.3 如果一定要复用

可以做，但建议只复用协议逻辑/参考实现，不要原样链接：

- 只编译 `idevice.c`、`lockdown.c`、`afc.c`、`service.c`、
  `property_list_service.c`、`userpref.c`；
- TLS 用 mbedTLS；
- 用 ESP-IDF USB Host 重写 usbmuxd 的 `usb.c` + `device.c`；
- 处理 POSIX socket/poll/线程/文件路径的差异；
- 评估 LGPL-2.1（libimobiledevice）和 GPL-2.0/3.0（usbmuxd）的合规。

这条路适合“已经有大量 libimobiledevice 定制代码”的团队，不适合只做
一个取 DB 功能的产品。

## 5. 和现有 iAP2 功能共存的问题

现有 MediaCtrl 用 **configuration 2**（Audio + HID）跑 iAP2；AFC 用
**configuration 3/4**。同一时刻只能选一个配置。

- ESP-IDF 5.x/6.x 的 USB Host 支持“枚举时选配置”
  （`CONFIG_USB_HOST_ENABLE_ENUM_FILTER_CALLBACK`），但**运行中切换配置
  不是稳定的公开 API**。
- 所以如果 Tab5 既要当播放器又要取 DB，需要：
  1. 停止 iAP2，释放 HID interface；
  2. 通过 Tab5 的 USB-A 5V 开关（PI4IOE1 P3 / `USB5V_EN`）给 iOS device
     断电再上电，强制重新枚举；
  3. 这次枚举选 config 3/4，做 AFC；
  4. 再断电/上电，选回 config 2，重连 iAP2。
- 这会打断播放，iOS device 可能需要重新解锁；适合“手动同步一次”，不适合
  后台自动频繁同步。

如果 Tab5 只做“取 DB”，开机就选 config 3/4，不做 iAP2，就完全没有这个
问题——这也是建议先做的 MVP。

## 6. 建议的 PoC 顺序

先花 3–5 天验证最危险的几步，任何一步不通就止损：

1. **USB Host 枚举**：P4 USB-A 认到 05ac:12aa，枚举时选 config 3，
   claim interface 1，读 version 包，收到 v2.0。
2. **usbmux v2**：发 setup `0x07`，开一条 TCP 流到 62078，能收到
   lockdown 的 `QueryType` 响应。
3. **lockdown + TLS**：用复制过来的 pair record 做 `StartSession`
   （`EnableSessionSSL=1`），拿到 `SessionID`；再 `StartService`
   `com.apple.afc`，确认 `EnableServiceSSL=0`。
4. **AFC**：开第二条 mux 流到 AFC 端口，`FILE_OPEN` 主库，按 1 MB
   分块读到 SD；再做 `-wal`/`-shm`。
5. **校验**：在 PC 上对 Tab5 取回的文件跑 `PRAGMA integrity_check`、
   `SELECT count(*) FROM item`，与 Linux 直接取的 SHA-256 对比。
6. **加 UI/传输**：进度条、错误重试、HTTP 下载或 SD 落盘。

## 7. 风险清单

| 风险 | 影响 | 应对 |
|---|---|---|
| 配对记录 | 新设备/恢复后无法直接连 | 预置当前设备的 pair record；产品化再做完整 pairing |
| pair record 私钥 | 泄露可冒充主机 | NVS/Flash 加密；不要进 git；不要公开日志 |
| 配置切换 | 与 iAP2 互斥 | 只做 AFC 模式；或 USB-A 断电重枚举 |
| 设备锁屏 | `PasswordProtected` | UI 提示解锁；必要时用 EscrowBag |
| WAL 一致性 | 只拷主库可能少最近变更 | 主库 + `-wal` + `-shm` 一起取；或取完做 checkpoint |
| SQLite on FATFS | WAL/共享内存支持不确定 | 只取文件最稳；要在 Tab5 解析就先做 SQLite+WAL 验证 |
| USB mux 细节 | ACK/窗口/ZLP/16 KB 包 | 用 usbmuxd/limrun 做参考，真机抓包 |
| config 3 未单独实测 | 可能某些固件只认 config 4/5 | PoC 先试 3，失败就用 4；两者 mux interface 相同 |
| SD 卡 | 空间/速度/掉电 | ≥256 MB、临时文件+rename、校验大小 |
| 许可 | usbmuxd GPL、libimobiledevice LGPL | 自写最小实现，或做合规评估 |
| iOS 版本 | 协议行为变化 | 先锁定 15.7.8；保留日志和重试 |

## 8. 建议

- **如果目标只是“把 DB 拿给现有 Linux/PC 流程用”**：
  不要让 Tab5 解析 DB。做一个 **USB-AFC → HTTP 下载桥**，或者干脆继续
  用 Linux/ifuse。前者是 Tab5 上最省事、最稳的“一个功能”。
- **如果目标是 Tab5 独立显示 Recently Added**：
  按第 6 节做 PoC；SD 是必需项；先在 PC 上验证 SQLite/WAL 解析，再决定
  是否把 SQLite 放到 Tab5。
- **如果目标是完整替代 Linux 主机（iAP2 + AFC）**：
  这不是“一个功能”，而是两套 USB 配置 + 两套协议栈 + 配置切换 UX。
  先做 AFC 单功能 MVP，再考虑合并。
- **如果目标是尽快出货**：
  Linux 小板（Pi Zero 2 W / Radxa Zero 3W）仍然是最稳的路线；
  Tab5 只做显示/控制端，或只做 AFC 桥。

## 附录：本次实测的关键命令

```bash
# 停止 iAP2 服务，切到 usbmux 配置
sudo pkill -f '[w]eb_app.py'
sudo usbmuxd -f -v

# 配对/服务检查
idevicepair validate
ideviceinfo -k ProductVersion          # 15.7.8
afcclient ls /iTunes_Control/iTunes
afcclient info /iTunes_Control/iTunes/MediaLibrary.sqlitedb
afcclient get /iTunes_Control/iTunes/MediaLibrary.sqlitedb /tmp/MediaLibrary.sqlitedb
```

`afcclient info` 的结果：

```json
{"st_size": 50552832, "st_ifmt": "S_IFREG"}
{"st_size": 156592,   "st_ifmt": "S_IFREG"}   // -wal
{"st_size": 32768,    "st_ifmt": "S_IFREG"}   // -shm
```

`lockdownd_start_session` / `lockdownd_start_service` 实测：

```text
StartSession: ssl_enabled = 1
com.apple.afc: port = 50018, ssl_enabled = 0
com.apple.mobile.house_arrest: port = 50020, ssl_enabled = 1
```
