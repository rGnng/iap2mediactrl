# MediaCtrl 移植到 M5Stack Tab5 实现指南

> 目标硬件：M5Stack Tab5（ESP32-P4 + ESP32-C6，16 MB Flash、32 MB PSRAM）。
> 参考实现：本仓库的 Python 版 `auth_test.py`、`web_app.py`、
> `ref_iap2_link_layer.py`、`hid_async.py`、`csm.py`、`mfi_local.py`。
> 本文只讨论固件如何写，不讨论是否值得移植，也不做 Linux 小板与 Tab5 的路线比较。
> AFC 取数据库/封面的独立路径见 `tab5-afc-db-pull-feasibility.md`。

## 1. 固件边界

Tab5 固件是一套 ESP-IDF 应用，不是 Python 代码的逐行翻译。需要重写的部分包括：

| Python 依赖 | 固件替代 |
|---|---|
| CPython + asyncio | FreeRTOS 任务、队列、`esp_timer` |
| pyusb / hidapi | ESP-IDF USB Host 驱动，直接操作中断端点和控制传输 |
| cryptography | mbedTLS ECDSA P-256 |
| http.server | `esp_http_server`（可选） |
| Pillow | `esp_jpeg` 硬件解码；缩略图由独立 AFC 导入阶段生成 |
| JSON DOM / 大字典 | SQLite + 分页查询，或等价的紧凑存储 |

固件有两种互斥的 USB 工作模式：

1. **iAP2 模式**：USB configuration 2，HID interface 2，用于认证、媒体控制、
   Now Playing、媒体库同步、封面/队列/播放列表的 FT 传输。
2. **AFC 模式**：USB configuration 3 或 4，用于 usbmuxd/lockdown/AFC 读取
   `MediaLibrary.sqlitedb` 和 `Artwork/Originals`。

同一时刻只能选择一种配置。需要真实 `date_added` 或媒体库封面时，把 AFC
做成独立的启动模式或独立固件；导入完成后重启进入 iAP2 模式。不要在同一个
iAP2 会话里尝试同时跑两条 USB 路径。

第一版固件建议只实现 iAP2 模式，先完成 USB HID、链路层、认证、Now Playing、
HID 控制和封面 FT，再接入媒体库同步和 UI。AFC 模式后置。

## 2. 运行时架构

```text
USB Host 中断/回调
        |
        v
usb_host_task  ---- 枚举、SET_CONFIGURATION、claim interface 2
        |            interrupt IN 0x83 / control OUT SET_REPORT
        v
hid_rx_task   ---- 按 report ID 重组、剥掉 2 字节头
        |
        v  byte stream queue
iap2_task     ---- FF5A 解析、ACK/重传、session 分发
        |            control session / FT session 12
        +--> csm_dispatch ----> app_service
        |                          |
        |                          +--> now_playing
        |                          +--> library_store
        |                          +--> queue / history
        |
        +--> ft_reassembler ----> artwork / queue / playlist handlers

ui_task       ---- LVGL，读 app_service 快照，提交命令
http_task     ---- 可选，esp_http_server，读写同一快照/命令队列
storage_task  ---- SQLite 批量提交、封面文件写入、缩略图
```

任务划分原则：

- `iap2_task` 是链路层、CSM、FT 状态的唯一所有者。不要在 USB 回调或 UI 任务里
  直接修改协议状态。
- USB 回调只做最短的数据搬运：把 packet 放入队列，或者提交下一次 transfer。
- `ui_task` 和 `http_task` 只读快照；播放、暂停、切歌等操作通过命令队列投递给
  `iap2_task`。
- `storage_task` 负责 SQLite 和文件写入。协议任务不要等待 SD 卡或数据库事务。
- 大缓冲区放在 PSRAM；任务栈、DMA 缓冲和频繁访问的小结构放在内部 SRAM。

推荐任务优先级从高到低：

```text
usb_host_task  > iap2_task > storage_task > ui_task > http_task
```

具体数值按 ESP-IDF 的 `usb_host` 示例调整；原则是 USB 和链路定时器不能被
UI 或 HTTP 阻塞。

## 3. 工程结构

建议按 ESP-IDF component 组织：

```text
main/
  app_main.c
  app_service.c/.h          # 状态机、快照、命令队列
  board_tab5.c/.h           # USB-A 5V、屏幕、SD、按键
usb/
  usb_host.c/.h             # 枚举、配置选择、端点管理
  hid_report.c/.h           # report 重组与输出分片
iap2/
  iap2_link.c/.h            # FF5A、seq/ack、重传、session
  iap2_csm.c/.h             # TLV 编解码
  iap2_messages.c/.h        # identification / now playing / library / playback
  iap2_ft.c/.h              # FT 状态机与文件类型分发
mfi/
  mfi_p256.c/.h             # 证书、私钥、签名
store/
  library_store.c/.h        # SQLite schema、全量/增量合并
  artwork_store.c/.h        # 封面、缩略图、LRU
  queue_history.c/.h        # Up Next、播放历史
ui/
  ui_lvgl.c/.h              # LVGL 页面
net/
  http_api.c/.h             # 可选 HTTP JSON API
```

外部组件至少需要：

```text
usb_host（ESP-IDF USB Host 组件）
mbedtls
esp_lcd / esp_lvgl_port / lvgl
fatfs / sdmmc
esp_jpeg
esp_http_server（可选）
esp_hosted / esp_wifi_remote（使用 C6 Wi-Fi 时）
SQLite（ESP-IDF 组件或自行集成）
```

## 4. USB Host 与 HID

### 4.1 枚举与配置选择

iOS device 有 5 个 configuration，iAP2 使用 **configuration 2**：Audio + HID，
HID interface 为 interface 2，输入端点为 `0x83`（interrupt IN，64 字节，
bInterval 1）。输出通过控制端点发送 `SET_REPORT`。

实现顺序：

1. 安装 USB Host 库，注册 client 事件回调。
2. 等待 Apple 设备 `05ac:12aa` 枚举完成。
3. 选择 configuration 2。多数 USB Host 栈默认使用 configuration 1；如果栈没有
   直接提供设置 configuration 的 API，就通过设备的默认控制端点 EP0 提交标准
   控制请求：`bmRequestType=0x00`、`bRequest=0x09`（SET_CONFIGURATION）、
   `wValue=2`、`wIndex=0`、无数据阶段。设置完成后重新读取
   configuration/interface 描述符。
4. 找到 interface 2（HID class），claim interface，不要绑定标准 HID class driver。
5. 找到 endpoint `0x83`，提交 interrupt IN transfer。每次 transfer 的缓冲为
   64 字节；大 report 会跨多次 transfer。
6. 输出 report 通过 interface 2 的 `SET_REPORT` 控制传输发送。

调试时先只做“枚举 + 读 HID report descriptor + 打印 endpoint”，不要一开始
就接链路层。确认 configuration 2、interface 2、EP `0x83` 都正确，再继续。

### 4.2 HID report 映射

Python 参考实现的 report size class：

```text
输入 report ID:
  1:5   2:9   3:13  4:17  5:25  6:49
  7:95  8:193 9:257 10:385 11:513 12:767

输出 report ID:
  13:5  14:9  15:13 16:17 17:25 18:49
  19:95 20:193 21:255
```

`INPUT_REPORTS[id]` 的值加 1 才是 report 总长（包含 report ID）。设备可能省略
64 字节整包之后的 1–2 字节尾部 padding：

```c
uint16_t total = input_report_size[rid] + 1;
if (total > 64 && total % 64 <= 2) {
    total = (total / 64) * 64;
}
```

换机型或固件时，先读设备的 HID report descriptor，核对 report ID 和长度；
不要假设这张表永远不变。

### 4.3 输入报告重组

每个输入 report 的格式：

```text
[report_id: u8] [LCB: u8] [payload: N bytes]
```

`hidapi` 每次 `read` 可能只返回一个 64 字节 USB 包；同一 report 只有第一个包
带 2 字节头。必须按 report ID 重组后再交给 iAP2 链路层：

```c
void hid_rx_feed(const uint8_t *buf, size_t len) {
    append(raw, buf, len);
    while (raw_len > 0) {
        if (remaining == 0) {
            if (raw_len < 2) break;
            uint8_t rid = raw[0];
            uint16_t total = input_report_size[rid] + 1;
            if (total > 64 && total % 64 <= 2)
                total = (total / 64) * 64;
            drop(raw, 2);              // report_id + LCB
            remaining = total - 2;
        }
        size_t take = min(remaining, raw_len);
        emit(raw, take);                // 送链路层
        drop(raw, take);
        remaining -= take;
        if (remaining) break;
    }
}
```

注意：

- 不要把 report 头当成 iAP2 数据。每个 report 插入 2 字节会导致大帧每 766 字节
  多出 2 字节，表现为 FT length/checksum 错误和封面撕裂。
- 尾部零 padding 可以原样交给链路层，由 `FF 5A` 同步扫描跳过；也可以在确认不会
  截断有效数据后丢弃。第一版建议原样传递，减少判断。
- 如果一次 transfer 里包含多个完整 report，状态机应循环处理，而不是假设
  “一次 transfer = 一个 report”。

### 4.4 输出报告分片

Python `HIDWriter` 的行为：

1. 按输出 report ID 从小到大选择，取第一个 `size - 1 > len(buffer)` 的 report；
   如果都不够大，取最大的 report。
2. 每次发送 `[report_id][lcb][chunk][padding]`，`chunk` 最多为 `size - 1` 字节。
3. 第一个 report 不置 `LCB_CONTINUATION`；后续 report 置
   `LCB_CONTINUATION=0x01`。
4. 只要还有剩余数据，就置 `LCB_MORE_TO_FOLLOW=0x02`；第一段也可能是
   `0x02`。
5. 不足一个 report 的部分补零。

C 实现建议先照搬这个选择算法，不要直接固定用最大 report。等设备行为确认后，
再考虑简化。

`SET_REPORT` 的原始控制传输布局需要在硬件上确认：hidapi 的 `write` 缓冲把
report ID 放在第一个字节；标准 HID `SET_REPORT` 通常把 report ID 放在
`wValue` 的低字节，数据阶段只放 report 内容。两者不要混用。第一版可以先用
USB 分析仪或现有 Python 日志核对 `SET_REPORT` 的 `wValue`、`wIndex` 和数据长度。

## 5. iAP2 链路层

### 5.1 帧格式

```text
FF 5A | u16 length | u8 control | u8 seq | u8 ack | u8 session
      | u8 header checksum | payload | u8 payload checksum
```

- `length` 是帧总长，包含 9 字节头和 payload checksum，最大 65535。
- 头校验：前 8 字节求和的补码，使 9 字节头 `sum % 256 == 0`。
- 载荷校验：payload 求和的补码，使 `sum % 256 == 0`。
- control 位：`SYN=0x80`、`ACK=0x40`、`EAK=0x20`、`RST=0x10`。
- session：control=10，EA=11，file transfer=12。

链路建立：

1. 每 1 秒发送检测标记 `FF 55 02 00 EE 10`，直到收到 `FF 5A` 链路帧。
2. 进入协商状态，发送带 LSP 的 SYN 帧。
3. 解析设备 LSP，发送 ACK；收到设备的 SYN/ACK 后进入正常状态。
4. 设备 LSP 的实测值：`max_outgoing=127`、`max_len=65535`、
   `retransmission_timeout=4000`、`ack_timeout=500`、
   `max_retransmissions=4`、`max_ack=3`。
5. 本地 session 表：control 10 / type 0 / version 1；EA 11 / type 2 / version 1；
   FT 12 / type 1 / version 2。

### 5.2 状态机

```text
DETECT  --收到 FF5A-->  NEGOTIATE  --收到 SYN/ACK-->  NORMAL
   |                       |                           |
   +-------超时/错误-------+-----------RST/错误--------> DEAD
                                                       |
                                                       +--> 关闭端点，2 秒后重试
```

实现建议：

- `DETECT`：定时发送 marker；每次重试 1 秒。
- `NEGOTIATE`：每 500 ms 重发 SYN，直到收到设备 LSP 和 ACK。
- `NORMAL`：处理数据、ACK、EAK、重传；维护未确认队列。
- `DEAD`：停止定时器，关闭 transfer，通知 app_service 断线；2 秒后重新初始化
  USB 和链路。

### 5.3 序号、ACK 与重传

第一版按 Python 的行为实现“逐包 ACK + 容忍一个序号空洞”即可：

- 8 位 PSN，使用 `(a - b) & 0xFF` 计算距离。
- 每收到一个数据包，更新 `last_received` 并发送 ACK。
- 每累计 `max_ack` 个包，或 500 ms 定时器到期，补发 ACK。
- 未确认包保留在队列中；4 秒未确认则重传；连续 4 次失败进入 `DEAD`。
- 收到 EAK 时，按缺失序号列表重传。
- 本地 `max_outgoing` 可先限制为 30，避免 PSRAM 和队列压力；设备允许 127。
- 收到 `RST` 立即进入 `DEAD`。

不要在第一版实现复杂的窗口协议。先把认证、Now Playing 和封面跑通，再补完整的
窗口和流控。生产实现需要对照 iAP2 规范补全重传、窗口和异常处理。

### 5.4 接收与发送缓冲

- RX：一个 PSRAM ring buffer，至少能容纳两个最大链路帧（2 × 64 KB）。
- TX：一个待发送队列，元素为 `{session_id, data, len, psn, timeout, retries}`。
  单个 payload 最大 65525 字节。
- CSM 消息可能跨多个链路帧，必须在 control session 上按 CSM length 累积完整
  body 后再解析，不能假设“一帧 = 一条消息”。
- FT 消息按 `[transfer_id][command]` 在 FT 层重组；大文件会分成多个
  `FirstData`/`Data`/`LastData` 消息。不要假设一个文件只对应一个链路帧。

### 5.5 长度异常的处理

Python 版保留了两个兜底：

- `_pending_ft`：处理历史抓包中 FT 长度偏短的尾部。
- `_fix_csm_length`：处理大 `0x4C04` 的 CSM length 少 1 字节。

这两个兜底是在 HID report 头未剥离时期留下的。C 实现应先按“HID 头已剥离、
length/checksum 标准”来写，再在真机上用原始日志验证。只有在确认设备确实存在
长度异常时，才加入有边界检查的恢复逻辑；不要无条件复制旧兜底代码。

## 6. CSM 与消息构造

### 6.1 CSM 编码

```text
CSM header: 0x4040 | u16 length | u16 msg_id
param:      u16 length | u16 param_id | payload
```

- `length` 包含自身 4 字节头；CSM `length` 包含 6 字节头。
- 所有多字节整数使用大端。
- 字符串参数以 UTF-8 + `0x00` 结尾（与 `csm.string()` 一致）。
- `void` 参数长度为 4，无 payload。
- `group` 参数的长度包含子参数总长度。

实现一组固定缓冲区的 builder：

```c
csm_buf_t b;
csm_begin(&b, 0x1D01);
csm_string(&b, 0, "MediaCtrl");
csm_u16_list(&b, 6, sent_ids, sent_count);
csm_group_begin(&b, 2);
csm_void(&b, 0);
csm_group_end(&b);
csm_send(&b);
```

不要在中断或 USB 回调里构造 CSM；在 `iap2_task` 中构造并排队发送。

### 6.2 Identification

`0x1D00` 到达后回复 `0x1D01`。参数按 Python 版发送：

```text
0  name              "MediaCtrl"
1  modelIdentifier   "MediaCtrl-1"
2  manufacturer      "MediaCtrl"
3  serialNumber      "MC-0001"
4  firmwareVersion   "0.1"
5  hardwareVersion   "1.0"
6  messages sent
7  messages received
8  power providing capability
9  maximum current drawn
12 current language  "en"
13 supported languages "en\0"
15 USB Device Transport group
0x12 iAP2 HID Component group
```

HID component 组：

```text
u16(0, 0x14E9)        HIDComponentIdentifier
string(1, "MediaCtrl HID")
u8(2, 0x01)           HIDComponentFunction = MediaPlaybackRemote
```

消息列表必须成对完整；当前可用集合：

```text
sent:     AA01, AA03, 5000, 5002, 4C00, 4C02, 4C03,
          4C05, 4C06, 4C07, 4C08, 4C09, 6800, 6802, 6803
received: AA00, AA02, AA04, AA05, 5001, 4C01, 4C04, 6801, 6804
```

不要加入参数 10（External Accessory Protocol），也不要把 identification 消息
放进参数 6/7。Identification 被拒时设备只回空 payload 的 `0x1D03`，需要用
二分法定位被拒参数。

### 6.3 Now Playing

`0x5000` 订阅三组参数：

- group 0：媒体项属性 1、2、3、4、5、6、7、8、9、10、11、12、13、14、16、
  18、19、26。
- group 1：PlaybackAttributes 0–16，另加 17（队列传输大小，u32，0 表示完整队列）。
- group 2：PlaybackQueueListContentTransferInfoRequest，请求字段
  0、1、6、12、14、16、18。

`0x5001` 解析：

```text
group 0:
  1 title, 2 media_type, 3 rating, 4 duration_ms,
  5 album_pid, 6 album, 7 track, 8 track_count, 9 disc, 10 disc_count,
  11 artist_pid, 12 artist, 13 album_artist_pid, 14 album_artist,
  16 genre, 18 composer, 19 compilation, 26 artwork_id

group 1:
  0 status, 1 elapsed_ms, 2 queue_index, 3 queue_count, 4 queue_chapter,
  5 shuffle, 6 repeat, 7 app, 14 queue_available,
  15 queue_transfer_id, 17 queue_transfer
```

收到新的 `artwork_id` 后等待 FT Setup；`0x5001` 可能在暂停时继续上报标题、
专辑和艺术家。

### 6.4 媒体库同步

`0x4C01` 返回库信息。选择本地库：UUID 中包含 `-MPB-` 的项优先（当前实测为
`...-MPB-15.7.8`），否则取第一个。保存 UUID。

首次同步发送 `0x4C03`：

```text
0  MediaLibraryUniqueIdentifier   (utf8)
1  LastKnownMediaLibraryRevision  (utf8, "0" 表示未知)
2  MediaItemProperties            (group: void 0..26)
3  MediaPlaylistProperties        (group: void 0..5)
```

`0x4C04` 解析：

```text
0  library UUID
1  revision (十进制字符串)
2  MediaItem group
3  MediaPlaylist group
4  deleted MediaItem PID (u64)
5  deleted MediaPlaylist PID (u64)
6  reset (void)
7  flag (实测 0x5A 表示删除阶段结束)
```

完整同步流程：

1. 发送 `0x4C03`，revision 为已存值或 `"0"`。
2. 收到 `0x4C04` reset：清空本地 items/playlists。
3. 收到大量 delete 参数：逐条删除；不存在的项忽略。
4. 删除阶段最后一条通常带 `param7=0x5A`；此时配件重新发送 `0x4C03`，
   带最新 revision，设备随后下发真正的 MediaItem 组。
5. 收到 item/playlist 组：按 PID 覆盖插入；重复项幂等。
6. 更新 revision，批量提交数据库。
7. 同一连接内只请求一次全量；需要重新全量时断开重连。UI 的 Resync 按钮
   应实现为“清空 revision + 关闭链路 + 重连”，不要在同一条连接上重复发
   `0x4C03` 全量请求。

大 `0x4C04` 可能很大，必须允许 CSM body 跨多个链路帧；不要用固定 4 KB
缓冲区。

### 6.5 播放控制

| 操作 | 消息 | 参数 |
|---|---|---|
| 恢复设备当前选择 | `0x4C06` | library UUID |
| 单曲/有序列表 | `0x4C07` | blob(8B PID × N), start_index u32, library UUID |
| 专辑 | `0x4C08` | album PID, type=2, start_index, UUID |
| 艺术家 | `0x4C08` | artist PID, type=1, start_index, UUID |
| 设备播放列表 | `0x4C08` | playlist PID, type=0, start_index, UUID |
| HID 控制 | `0x6800` / `0x6802` | component id + report |

`0x4C07` 的 PID 列表受 CSM 64 KB 限制；实际限制在 2,000 首以内。流派、专辑
艺术家等集合先在本地按 PID 排序，再走 `0x4C07`。

### 6.6 HID 控制

`0x6800` StartHID：

```text
u16(0, 0x14E9)   component id
u16(1, 0x18D1)   vendor id
u16(2, 0x4E26)   product id
param(4, <HID report descriptor>)
```

`0x6802` AccessoryHIDReport：

```text
u16(0, 0x14E9)
param(1, <2 字节 report>)
```

HID report 的 12 个 bit：

| bit | usage | 功能 |
|---|---|---|
| 0 | 0xB0 | Play |
| 1 | 0xB1 | Pause |
| 2 | 0xB5 | Next |
| 3 | 0xB6 | Previous |
| 4 | 0xB9 | Shuffle |
| 5 | 0xBC | Repeat |
| 6 | 0xE9 | Volume + |
| 7 | 0xEA | Volume − |
| 8 | 0xE2 | Mute |
| 9 | 0xB7 | Stop |
| 10 | 0xB3 | Fast Forward |
| 11 | 0xB4 | Rewind |

发送按键时先置位对应 bit，再发送全 0 report 释放。FF/RW 短按在真机上表现为
next/previous；绝对 seek 和绝对音量不在 iAP2 中提供。

### 6.7 FT 文件类型

FT 消息格式：

```text
[transfer_id: u8] [command: u8] [payload...]
```

命令值：

```text
0x01 Start
0x02 Cancel
0x04 Setup
0x00 Data
0x80 FirstData
0x40 LastData
0xC0 FirstAndOnly
0x05 Success
0x06 Failure
```

文件类型：

| file type | 内容 | 处理 |
|---|---|---|
| `0x0002` | Now Playing artwork JPEG | 校验 SOI/EOI，写缓存，交 JPEG 解码 |
| `0x0003` | 播放队列 PID 数组 | 每 8 字节一个大端 u64 |
| `0x0006` | MediaItemList | TLV：count、start index、有序 MediaItem |
| `0x0004` | 设备播放列表 PID 数组 | 每 8 字节一个大端 u64 |
| `0x0007` | 播放列表 MediaItemList | 与 `0x0006` 类似 |

收到 Setup 后回 Start；收到 LastData/FirstAndOnly 后回 Success。空文件
（size=0）也回 Success，但不写入缓存。大文件会分成多个数据消息，按
`transfer_id` 累积，不要按链路帧直接当完整文件。

Python 参考实现把每个 session 12 payload 当作一个 FT 消息处理；真机日志中的
600×600 封面会分成多个 `FirstData/Data/LastData` 消息。如果目标 USB Host 把
一个 FT 消息拆成多个 link frame，需要在 link 层先按 FT 消息边界重组，再交给
FT 层；不要对每个 link frame 都重新解析 `[id][cmd]`。

## 7. MFi 认证

### 7.1 密钥与证书

- 证书：P-7B，leaf 为 P-256。
- 私钥：PKCS#8 DER、PEM 或 32 字节原始 scalar 均可。
- 启动时解析 P-7B，取 leaf 公钥；校验证书公钥与私钥匹配。
- 生产固件把证书和私钥放在加密 NVS 或受保护的 flash 分区，启用 Flash
  Encryption / NVS Encryption；不要放在明文 FATFS 或代码常量里。
- 私钥只在认证期间解密到 RAM，认证完成后清零。

### 7.2 认证流程

```text
0xAA00 RequestAuthenticationCertificate
  -> 0xAA01 certificate blob
0xAA02 32-byte challenge
  -> 0xAA03 64-byte raw ECDSA signature
0xAA05 AuthenticationSucceeded
```

签名步骤：

1. challenge 已经是 32 字节 SHA-256 digest；直接作为 ECDSA 输入，**不要再次
   做 SHA-256**。
2. 用 P-256 私钥做 ECDSA，得到 DER 签名。
3. 把 DER `(r,s)` 转成 64 字节 `r || s`，每边 32 字节大端。
4. 通过 `0xAA03` 发送。

mbedTLS 侧使用 ECDSA P-256；不要在回调里做证书解析或大内存分配。认证失败时
记录 `0xAA04`，进入重连。

## 8. 应用状态与存储

### 8.1 数据模型

```c
typedef struct {
    uint64_t pid;
    char *title;
    char *album;
    char *artist;
    char *album_artist;
    char *genre;
    char *composer;
    uint64_t album_pid;
    uint64_t artist_pid;
    uint32_t duration_ms;
    uint16_t track, tracks, disc, discs;
    uint8_t media_type, rating, compilation;
    double date_added;       // 0 表示未知
    double first_seen;
} media_item_t;

typedef struct {
    uint64_t pid;
    char *name;
    uint64_t parent_pid;
    bool genius, folder;
    uint8_t contained_ft_id;
    uint64_t *items;
    size_t item_count;
} media_playlist_t;
```

运行时状态：

```c
typedef struct {
    now_playing_t now_playing;
    bool connected;
    const char *link_state;
    size_t track_count, album_count;
    const char *revision;
} app_snapshot_t;
```

UI/HTTP 只读 `app_snapshot_t`。用互斥锁保护，或使用双缓冲/原子指针发布快照；
不要在 UI 里遍历媒体库大表。

### 8.2 SQLite

建议表结构：

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE items(
  pid INTEGER PRIMARY KEY,
  title TEXT, album TEXT, album_pid INTEGER,
  artist TEXT, artist_pid INTEGER, album_artist TEXT,
  genre TEXT, composer TEXT,
  duration_ms INTEGER, track INTEGER, tracks INTEGER,
  disc INTEGER, discs INTEGER,
  media_type INTEGER, rating INTEGER, compilation INTEGER,
  date_added REAL, first_seen REAL
);
CREATE INDEX idx_items_album ON items(album_pid);
CREATE INDEX idx_items_artist ON items(artist_pid);
CREATE INDEX idx_items_title ON items(title COLLATE NOCASE);
CREATE INDEX idx_items_date ON items(date_added);

CREATE TABLE playlists(
  pid INTEGER PRIMARY KEY, name TEXT, parent_pid INTEGER,
  genius INTEGER, folder INTEGER, contained_ft_id INTEGER
);
CREATE TABLE playlist_items(
  playlist_pid INTEGER, position INTEGER, item_pid INTEGER,
  PRIMARY KEY(playlist_pid, position)
);
CREATE TABLE history(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  item_pid INTEGER, ts REAL
);
```

实现要求：

- 每批 `0x4C04` 用一个事务提交；不要每条 item 一次 fsync。
- 全量 reset 时在一个事务里清空 items/playlists，再批量插入。
- revision 单独存 `meta`；重连时读取。
- SQLite 数据库放 SD 卡，不要放 16 MB 内部 Flash。
- 掉电安全：WAL + 定期 checkpoint；封面/导入结果等文件使用临时文件 +
  rename；启动时校验 schema 和 revision。
- 媒体库查询必须分页；不要一次把 20k 条序列化成 JSON 或全部塞进 LVGL。

### 8.3 媒体库索引与查询

UI 需要的视图：

- Albums：按 `album_pid` 聚合，取最常见的标题/艺术家，`date_added` 取该专辑
  曲目的最大值。
- Artists：按 `artist_pid` 聚合。
- Album Artists：按 `album_artist` 聚合。
- Genres：按 `genre` 聚合。
- Songs：按标题/艺术家/专辑/时长/添加时间排序。
- Recently Added：`date_added` 优先，`first_seen` 回退。
- Device Playlists：只读。

这些都可以用 SQL 查询 + 索引完成；不要照搬 Python 的内存字典索引。

### 8.4 封面与缩略图

- Now Playing 封面：iAP2 FT `0x0002`，通常为 200×200 或 600×600 JPEG。
  校验 `FF D8` 开头、`FF D9` 结尾；写入 `artwork-cache/<artwork_id>.jpg`。
- 封面缓存：限制数量（例如 128 张）或总大小，按 LRU 淘汰。
- 媒体库封面：iAP2 不提供逐条 artwork ID。需要从 AFC 的
  `Artwork/Originals` 导入，生成 160px 缩略图；映射存 `media-db-artwork.json`
  或 SQLite 表。该步骤不在 iAP2 会话内运行。
- LVGL 显示：用 `esp_jpeg` 解码到 PSRAM，再转 RGB565；不要在主循环里反复解码
  大图，缓存当前封面和少量缩略图。

### 8.5 队列、播放列表与历史

- 设备真实队列：`0x5001` 的 14/15/17 触发 FT `0x0003` 或 `0x0006`；
  解析后更新 Up Next。`source=device`。
- 网页/UI 发起的播放：本地维护有序 PID 列表，`source=local`。
- 设备播放列表：`0x4C04 param3` + FT `0x0004`；只读。
- 本地播放列表：如果产品需要，存 SQLite；播放走 `0x4C07`，最多 2,000 首。
- 播放历史：最近 500 次曲目变化，写入 SQLite 或 JSON；用于“最近播放/最常播放”。
- `0x4C09` 是 All Songs，不是 Recently Added；不要把它接到“最近添加”按钮。

### 8.6 添加时间与 AFC 模式

iAP2 媒体库没有 Date Added 字段。需要真实日期时：

1. 进入 AFC 模式（configuration 3/4）。
2. 通过 usbmuxd/lockdown/AFC 读取
   `iTunes_Control/iTunes/MediaLibrary.sqlitedb`。
3. 用 SQLite backup API 取一致快照，读取 `item.item_pid` 和
   `item.date_added`；`date_added` 是 Apple/Core Data 时间，加 `978307200`
   转 Unix 时间。
4. 写入 `date_added` 表或导入结果文件。
5. 重启进入 iAP2 模式，UI 优先使用 `date_added`，缺失时回退 `first_seen`。

AFC 路径的详细协议、内存预算和最小实现见
`tab5-afc-db-pull-feasibility.md`。两条路径不能同时占用 USB。

## 9. UI

### 9.1 LVGL 页面

Tab5 有 5 英寸 1280×720 MIPI-DSI 屏和触摸。推荐页面：

1. Now Playing：封面、标题、艺术家、专辑、进度、播放/暂停/上一首/下一首、
   音量步进、静音、随机、循环。
2. Library：Albums / Artists / Album Artists / Genres / Songs /
   Recently Added / Device Playlists。
3. Album detail：封面、曲目表、Play album。
4. Up Next：当前队列和接下来曲目。
5. Settings：亮度、Wi-Fi、AFC 导入模式、重新同步、日志导出。

布局用两列：左侧 Now Playing + Up Next，右侧 Library。列表使用 LVGL table
或自定义 row；专辑页使用缩略图网格。封面和列表按页加载，不要一次创建 20k
个对象。

### 9.2 命令与快照

```c
typedef enum {
    CMD_PLAY_PAUSE, CMD_NEXT, CMD_PREV, CMD_STOP,
    CMD_VOLUME_UP, CMD_VOLUME_DOWN, CMD_MUTE,
    CMD_SHUFFLE, CMD_REPEAT,
    CMD_PLAY_ITEM, CMD_PLAY_ALBUM, CMD_PLAY_ARTIST,
    CMD_PLAY_DEVICE_PLAYLIST, CMD_RESYNC
} app_command_type_t;
```

UI 只向 `app_command_queue` 投递命令；`iap2_task` 执行并更新状态。UI 定时器
每 100–200 ms 读一次快照，避免每次 `0x5001` 都重绘整个页面。

### 9.3 HTTP（可选）

如果使用 ESP32-C6 Wi-Fi，可以保留现有浏览器的 JSON API：

- `GET /api/state`
- `GET /api/library`、`/api/library/album`、`/api/library/songs`
- `GET /api/artwork`
- `GET /api/queue`
- `POST /api/command`、`POST /api/play`
- `POST /api/library/sync`

HTTP 服务只读快照，命令走同一个队列。当前 Python 版没有鉴权；固件版至少加
token 或限制在受信 LAN。HTTP 不是第一版必需功能，先把 LVGL 主界面跑通。

### 9.4 字体与内存

- CJK 字体按需子集化；全量 CJK 字体会占用大量 Flash/PSRAM。
- 专辑标题可能包含日文、中文、特殊符号；准备 fallback 字体和截断策略。
- LVGL 帧缓冲、JPEG 解码缓冲、SQLite 页缓存都放 PSRAM；内部 SRAM 留给
  USB DMA、任务栈和中断。

## 10. 资源预算

| 项目 | 预算 | 说明 |
|---|---|---|
| 固件 | 1.5–2.5 MB | ESP-IDF + mbedTLS + LVGL + 应用；16 MB Flash 可双 OTA |
| 证书/NVS | < 64 KB | 加密分区 |
| USB/链路缓冲 | 2 × 64 KB | RX/TX 最大帧 |
| FT/封面缓冲 | 0.2–1 MB | 600×600 JPEG + 解码缓冲 |
| LVGL 帧缓冲 | 1.8–3.6 MB | 1280×720 RGB565，双缓冲 |
| SQLite 页缓存 | 4–8 MB | 20k 条媒体库 |
| 媒体库 DB | 约 50 MB | 必须放 SD |
| 封面原图/缩略图 | 1.3 GB / 20 MB | SD；按需加载 |
| 历史/播放列表 | < 10 MB | SQLite 或 JSON |

PSRAM 32 MB 足够上述运行时缓冲；内部 16 MB Flash 只放固件、证书、NVS 和少量
资源，不放 SQLite 数据库或大封面。

## 11. 实现顺序与验收

按以下顺序做，每一步都能独立验收：

| 阶段 | 内容 | 验收 |
|---|---|---|
| M0 | USB Host 枚举 | 识别 `05ac:12aa`，选择 configuration 2，读到 HID report descriptor |
| M1 | HID 传输 | 收到 `FF 5A`，能发送 `SET_REPORT`，report 重组无错位 |
| M2 | 链路层 | 完成 marker、SYN/ACK、进入 NORMAL；ACK/重传日志正确 |
| M3 | 认证与识别 | `AA00→AA05` 成功；`0x1D01` 被接受；HID component 被接受 |
| M4 | Now Playing + HID | 解析 `0x5001`；`0x6802` play/pause/next/previous 生效 |
| M5 | 封面 FT | 200×200 与 600×600 JPEG 完整、可解码、缓存正确 |
| M6 | 媒体库 | 全量 20k 条、reset/delete/增量、revision 持久化正确 |
| M7 | 队列/播放列表 | FT `0x0003/0x0006/0x0004`；`0x4C06/4C07/4C08` 播放正确 |
| M8 | UI/存储 | LVGL 页面、SQLite、断线重连、长稳运行 |
| M9 | AFC 模式（可选） | 读取 DB/封面，生成日期与缩略图，重启回 iAP2 |

每个阶段都保存原始 USB/HID 日志到 SD 卡；出现异常时先确认 HID report 头和
链路帧边界，再查上层协议。Python 版的 `HID_LOG_RAW=1` 和 `IAP2_VERBOSE=1`
可以作为日志格式参考。

## 12. 容易写错的地方

1. **HID report 头**：每个 report 的 `[report_id][LCB]` 必须剥离；否则大帧
   每 766 字节多 2 字节，封面和媒体库都会损坏。
2. **report 长度**：按 report ID 的 size class 重组；64 字节整包后的 1–2 字节
   padding 可能被省略。
3. **配置选择**：iAP2 必须使用 configuration 2；不要用默认 configuration 1
   去 claim interface 2。
4. **不要绑定标准 HID class driver**：设备实际收发方式不符合标准 HID 使用方式，
   直接 claim interface + interrupt/control transfer 更可控。
5. **CSM 跨帧**：一条 CSM 消息可能跨多个 link frame；按 CSM length 累积。
6. **FT 大文件**：按 `transfer_id` 累积多个 `FirstData/Data/LastData` 消息；
   不要把一个 link frame 当完整文件。
7. **4C04 全量**：同一连接只请求一次全量；删除阶段结束的 `0x5A` 后要重新发
   `0x4C03`；重新全量必须断开重连。
8. **Identification 脆弱**：不要加参数 10，不要把 identification 消息放进
   参数 6/7；新增参数前先做二分测试。
9. **MFi 签名格式**：设备要 64 字节 `r||s`，不是 DER；challenge 是 32 字节
   digest，不要重复 SHA-256。
10. **内存**：不要在 PSRAM 里保留整库 JSON 或多个 600×600 解码缓冲；SQLite
    分页 + 单张封面缓存。
11. **USB 供电**：Tab5 的 USB-A 有负载开关，实测 iOS device 充电时的电压/电流；
    电池续航会明显下降。
12. **AFC 与 iAP2 互斥**：configuration 2 与 3/4 不能同时使用；切换需要
    重新枚举 USB。

## 13. 参考

- `docs/HANDOVER.md`：协议、认证、HID、FT、媒体库的实测结论
- `docs/CACHE.md`：缓存、持久化、失效规则
- `docs/PORTING_CPP.md`：C++ 模块和嵌入式 Linux 架构草稿
- `docs/ft-file-transfer.md`：FT 命令与文件类型速查
- `docs/tab5-afc-db-pull-feasibility.md`：AFC 取 DB/封面的独立实现
- ESP-IDF USB Host: <https://docs.espressif.com/projects/esp-usb/en/latest/esp32p4/usb_host.html>
- ESP-IDF JPEG: <https://docs.espressif.com/projects/esp-idf/en/stable/esp32p4/api-reference/peripherals/jpeg.html>
- M5Stack Tab5: <https://docs.m5stack.com/en/core/Tab5>
