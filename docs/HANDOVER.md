# MediaCtrl Handover

## 1. 目标

在 Linux 上实现一个软件 iAP2 配件，通过 USB HID 连接 iPod touch（iOS 15.7.8），实现：

1. ✅ 读取 Now Playing（标题、专辑、艺术家、播放状态、进度、队列、随机/循环模式）
2. ✅ 读取媒体库（20,564 首曲目 / 2,095 张专辑 / 5,045 位艺术家，含完整元数据）
3. ✅ 控制播放（播放/暂停/停止/上一首/下一首）+ **音量 ± / 静音 / 随机 / 循环**（HID）
4. ✅ **按曲目/专辑/艺术家/专辑艺术家/流派/本地播放列表点播**（0x4C06 / 0x4C07 / 0x4C08）
5. ✅ 封面——200×200（27,498 字节）与 600×600（186,918 字节）JPEG 均 `djpeg` 零警告、PIL 严格解码；
   封面落盘 LRU 缓存
6. ✅ Web UI：专辑 / 艺术家 / 专辑艺术家 / 流派 / 歌曲 / 最近添加 / 设备播放列表七个视图；
   搜索、分页、排序、Up Next、播放历史、智能列表；本地播放列表后端/API 保留，但当前浏览器界面仅展示设备播放列表

当前阶段：**完整可用的控制系统（协议允许范围内）。** 媒体库采用“全量同步 + revision 增量”模型，
镜像持久化到 `media-library.json`；本地播放列表持久化到 `playlists.json`；播放历史 `history.json`；
封面缓存 `artwork-cache/`。缓存机制见 `CACHE.md`，C++/嵌入式移植草稿见 `PORTING_CPP.md`。

## 2. 运行环境

| 项目 | 值 |
|---|---|
| 开发机 | `ops@10.0.0.85` |
| 系统 | Ubuntu，kernel `7.0.0-38-generic`，aarch64 |
| 虚拟化 | VMware 虚拟机，USB 直通 |
| 工作目录 | `/home/ops/iap2mediactrl` |
| Python | 3.14.4 |
| 虚拟环境 | `/home/ops/iap2mediactrl/.venv`（含 pyusb / hidapi / cryptography / **pillow**） |
| 权限 | `ops` 可免密 sudo |
| USB 角色 | Linux 是 USB Host，iOS device 是 USB Device |
| Gadget | 无 `/sys/class/udc`，不能走 USB Device/Gadget 路线 |

### 测试设备（iOS device）

| 项目 | 值 |
|---|---|
| 型号 | iPod touch 7th Gen（A2178） |
| 系统 | iOS 15.7.8 |
| USB VID:PID | `05ac:12aa` |
| 备注 | `lsusb` 显示 "iPod Touch 5.Gen [A1421]" 是 `usb.ids` 名称过旧，以设备 Settings 为准 |

### 证书

| 项目 | 值 |
|---|---|
| 证书 | `/home/ops/iap2cert/certificate.p7b`（项目内相对路径 `../iap2cert/certificate.p7b`） |
| 私钥 | `/home/ops/iap2cert/identity.pk8`（项目内相对路径 `../iap2cert/identity.pk8`） |
| 类型 | MFi 3.0，ECDSA P-256 |
| 有效期 | 到 2049-12-31 |
| 状态 | 开发/测试用，合法拥有；**不得提交到 Git、不得贴到聊天/日志、不得对外分发** |

代码只通过路径加载证书，不会打印证书或私钥内容。

## 3. 关键结论

### 3.1 USB 路线

iOS device 的 USB descriptor 有 5 个 configuration：

| Config | 内容 | 用途 |
|---|---|---|
| 1 | Imaging / PTP | 默认 |
| 2 | Audio + HID（interface 2） | **iAP2 HID 路线** |
| 3 | Imaging + USBMUX | CarPlay 相关 |
| 4 | Imaging + USBMUX + Apple Ethernet | CarPlay 相关 |
| 5 | Imaging + USBMUX + NCM | CarPlay 相关 |

我们走 **configuration 2 + HID interface 2**：

- HID report descriptor：208 bytes
- `EP 0x83`（interrupt IN, wMaxPacketSize=64）
- **HID 报告格式（已用 report descriptor + 原始抓包确认）**：每个输入报告 = `[report_id][LCB][payload]`，按 report ID 的 size class 补齐（`INPUT_REPORTS`，见 §3.9）。大报告被 USB 切成 64 字节包，`hidapi.read()` 每个包返回一次；同一报告只有第一包带 2 字节头。**必须按 report ID 的固定长度重组报告并剥掉 2 字节头**，只把 payload 交给链路层。此前“把所有 URB 当纯字节流”的做法会把每个报告头当成 iAP2 数据（FT 大帧每 768 字节插入 2 字节，242 个报告头 = 484 字节），造成封面横向撕裂、link length/CSM length “少算”的假象。已修复（见 §3.9）。
- 输出报告通过 `SET_REPORT` 控制传输发送；输入报告从 `EP 0x83` 读取。

### 3.2 iAP2 链路

- 检测标记：`FF 55 02 00 EE 10`
- 链路帧：`FF 5A | u16 length | u8 control | u8 seq | u8 ack | u8 session | u8 header checksum | payload | u8 payload checksum`
- **length 含 FF5A+头+payload+payload checksum（帧总长）。**
- **校验和 = 字节和的补码（mod 256 == 0），头/载荷各一个。**
- 设备 LSP：`max_outgoing=127, max_len=65535, rto=4000, ack_timeout=500, max_retransmissions=4, max_ack=3`
- 设备 session：control 10、EA 11、**file transfer 12**（双向 LSP 均回传 session 12 type 1 version 2 ✓ 已验证）
- SYN/SYN-ACK 协商成功；设备会回传我们的 session 表。
- FT 帧的 link length 是**正确**的；此前的“少算”是 HID 报告头未剥离造成的假象。剥头后 payload checksum 全部通过（§3.9）。
- FT 数据报 >65535 时被拆成多个 link 帧，每帧 length=0xFFFF（合法最大值），按声明长度读取即可。
- 调试环境变量：`HID_LOG_RAW=1` 打印每个 URB 原始字节。

### 3.3 MFi 认证

已成功：

```text
0xAA00 RequestAuthenticationCertificate
→ 0xAA01 发送 607 字节 certificate.p7b
→ 0xAA02 32 字节 challenge
→ 0xAA03 64 字节 raw ECDSA P-256 签名
→ 0xAA05 AuthenticationSucceeded
```

实现细节：P7B，leaf cert P-256；私钥 PKCS#8 DER；`ec.ECDSA(utils.Prehashed(hashes.SHA256()))` 直接对 32 字节 digest 签名；DER→raw：`r.to_bytes(32)+s.to_bytes(32)`；加载时验证私钥与证书公钥匹配。

### 3.4 Identification

已成功接受的最小 identification 参数（含媒体库、HID）：

- 0 name / 1 modelIdentifier / 2 manufacturer / 3 serialNumber / 4 firmwareVersion / 5 hardwareVersion
- 6 messages sent by accessory / 7 messages received from accessory
- 8 power providing capability / 9 maximum current drawn
- 12 current language / 13 supported languages
- 15 USB Device Transport group
- **0x0012 iAP2 HID Component**（见 §3.6）

** identification 的消息列表必须成对完整（Start+Stop），否则 param 6/7 会被拒。**

当前接受的消息列表：

```text
sent:     AA01, AA03, 5000, 5002, 4C00, 4C02, 4C03, 4C05, 4C06, 4C07, 4C08, 4C09, 6800, 6802, 6803
received: AA00, AA02, AA04, AA05, 5001, 4C01, 4C04, 6801, 6804
```

**不要发送：**

- 参数 10（External Accessory Protocol）：设备会拒绝
- 参数 6/7 中的 identification 消息（0x1D00/0x1D02/0x1D03）：设备会拒绝
- 参数 6/7 中的 EA 相关消息，除非确实需要

### 3.5 Now Playing

已成功，真实播放中数据（iOS device 正在播放 *Shifting Temple*）：

```text
RX CSM 0x5001
  media item:
    title='Shifting Temple'
    duration_ms=166440
    album='誰が為のエンドロール'
    artist='黒魔'
    artwork_id=133..151   (每次 +1)
  playback:
    status=1 (playing) / 2 (paused)
    elapsed_ms 递增
    app='Music'
```

- media item 组内参数：1=title、4=duration_ms、6=album、12=artist、26=artwork transfer id
- playback 组内参数：0=status、1=elapsed_ms、7=app
- 空媒体项时 title/album/artist 为空串、duration_ms=0、artwork_id 递增（已验证）
- 设备会周期性重发 5001（artwork_id 变化时触发 FT artwork 提供，见 §3.7）

### 3.6 HID（播放/暂停/上一首/下一首）——已实测

Identification 中携带 HID 组件（**param id 0x0012 = iAP2 HID Component**；0x0011 是 Bluetooth Transport，发了会被拒）：

```text
group(0x12, [
    u16(0, 0x14E9),          # HIDComponentIdentifier
    string(1, "MediaCtrl HID"),  # HIDComponentName
    u8(2, 0x01),             # HIDComponentFunction = MediaPlaybackRemote
])
```

**StartHID (0x6800)** 参数：

```text
u16(0, 0x14E9)   # component id（与 identification 一致）
u16(1, 0x18D1)   # vendor id
u16(2, 0x4E26)   # product id
param(4, <36 字节 HID report descriptor>)   # Consumer page 0x0C: play/pause/next/prev/shuffle/repeat/vol+/vol-
```

**AccessoryHIDReport (0x6802)** 参数：

```text
u16(0, 0x14E9)   # component id
param(1, b'\x01')  # 或 b'\x00'；先按下(0x01<<bit)，再松开(0x00)
```

Report bit（Consumer page usage）：0=Play(0xB0)，1=Pause(0xB1)，2=Next(0xB5)，3=Previous(0xB6)，4=Shuffle(0xB9)，5=Repeat(0xBC)，6=VolumeIncrement(0xE9)，7=VolumeDecrement(0xEA)

**实测（hid-test-03/04/05/06/07）：**

```text
HID_COMMAND=play      → 5001 status: 2 → 1（播放），elapsed_ms 递增 ✓
HID_COMMAND=pause     → 5001 status: 1 → 2（暂停） ✓
HID_COMMAND=next      → 切到下一首 'Astaluego' ✓
HID_COMMAND=previous  → 切到上一首 '旅の終わりに' ✓
```

环境变量 `HID_COMMAND=play|pause|next|previous|shuffle|repeat|volume_up|volume_down`（auth_test.py 在 identification 接受后 2 秒发送对应命令；shuffle/repeat/volume 未实测）。

0x6801 DeviceHIDReport 未观察到设备主动发送（正常；设备不需要回传时不发）。

### 3.7 文件传输（封面）——已实现并验证

**协议（session 12, type 1, version 2）：**

```text
Device → Accessory : Setup   [ft_id][0x04][u64 BE file size][u16 BE file type]
Accessory → Device : Start   [ft_id][0x01]
Device → Accessory : FirstAndOnlyData (0xC0) / FirstData(0x80) / Data(0x00) / LastData(0x40)
Accessory → Device : Success [ft_id][0x05]（或 Failure 0x06）
```

- 设备在 NowPlayingUpdate 出现 artwork_id 后**主动**发起 Setup（ft_id == artwork_id，file type 0x0002 = NowPlayingArtworkData）。
- FT 帧长度和 payload checksum 都是标准的：剥掉 HID 报告头后，每帧 `sum(frame) % 256 == 0`，按 link length 读取即可。`_pending_ft` 帧尾恢复仅作为兜底保留（正常路径不会触发）。
- **大帧（>64KiB）的 FT 数据报被拆成多个 link 帧**（例：600×600 artwork 拆成 3 帧，每帧 length=0xFFFF）；FT Setup 里的 size 字段也是**真实文件大小**。
- FT 帧内容是 **EXIF JPEG**，实测两种尺寸：200×200（27,498 字节）与 **600×600（186,918 字节，PIL / djpeg 严格解码通过）**。
- 空 artwork（设备报 size=0 → FirstAndOnlyData 0 字节）按空文件处理，Success 已回，不落盘。

**已落盘：** `artwork-251.jpg`（200×200，27,498B，djpeg clean）、`artwork-600-clean.jpg`（600×600，186,918B，djpeg clean）。

### 3.8 媒体库（0x4C00/4C01/4C03/4C04）——完整协议

依据 MFi Accessory Interface Specification 的参数表 + 实测：

**0x4C01 MediaLibraryInformation**：每组 MediaLibraryInformation 含

```text
0: MediaLibraryName                    (utf8)   e.g. "iPod touch" / "Apple Music Radio"
1: MediaLibraryUniqueIdentifier        (utf8)   ...-MPB-15.7.8 / ...-IRTL-15.7.8
2: MediaLibraryType                    (u8)     0 = 本地库, 2 = Apple Music Radio
```

**0x4C03 StartMediaLibraryUpdates** 参数：

```text
0: MediaLibraryUniqueIdentifier        (utf8, 必须)
1: LastKnownMediaLibraryRevision       (utf8, 可选；不带=全量)
2: MediaItemProperties                 (group of voids，属性 ID 见下)
3: MediaPlaylistProperties             (group of voids)
```

MediaItemProperties 子参数 ID（与 Now Playing 的 media item 属性一致）：
0=PersistentID, 1=Title, 2=MediaType, 3=Rating, 4=DurationMs, 5=AlbumPID, 6=AlbumTitle,
7=AlbumTrackNumber, 8=AlbumTrackCount, 9=DiscNumber, 10=DiscCount, 11=ArtistPID, 12=Artist,
13=AlbumArtistPID, 14=AlbumArtist, 15=GenrePID, 16=Genre, 17=ComposerPID, 18=Composer, 19=Compilation…
（MediaPlaylistProperties：0=PersistentID, 1=Name, 2=ParentPID, 3=IsGeniusMix, 4=IsFolder, 5=ContainedMediaItems）

**0x4C04 MediaLibraryUpdate** 参数：

```text
0: MediaLibraryUniqueIdentifier        (utf8)
1: MediaLibraryRevision                (utf8, 十进制字符串，单调递增)
2: MediaItem                            (group, 0+)
3: MediaPlaylist                        (group, 0+)
4: MediaItemDeletePersistentIdentifier   (u64, 0+)
5: MediaPlaylistDeletePersistentIdentifier (u64, 0+)
6: MediaLibraryReset                    (void; 收到后必须清空本地镜像并准备接收全量)
7: 实测存在但规范 R2 未列出（1 字节标志；0x00=普通，0x5A=该批为"完整数据库"类消息）
```

**实测流程（iOS device：iPod touch / iPhone，iOS 15.7.8）：**

1. 首次同步发送 4C03：`[uuid, revision="0", item-props, playlist-props]`
2. 设备先发 `4C04(param6=reset)`，随后是**大量 param4/param5 删除消息**（所有条目 ID，约 1.5 万条/6 万个 ID）
3. 删除阶段最后一条带 `param7=0x5A`；此时**配件必须重新发 4C03**（带最新 revision，参数同上）——设备随后下发**真正的 media item 组**（param2，批量约 4 条/消息，含全部请求的属性）
4. 之后 4C03 只要带上次的 revision，设备就只发增量（实测带最新 revision 时 0 条消息）
5. **同一连接内只能请求一次全量**（规范 16.1.2）；Web 服务的"重新同步"通过断开重连实现

**此前观察到的“帧长度少算”已确认是 HID 报告头未剥离造成的假象（§3.9）：**

- **FT 数据帧**：剥掉每个 HID 报告的 2 字节 `[report_id][LCB]` 后，link length 与 payload checksum 都正确；原来的 `_pending_ft` 尾恢复保留为兜底。
- **大 4C04（MediaItem 组）**：剥头后 link/CSM length 预期也恢复正确；`_fix_csm_length` 作为兜底保留。全量同步实测若仍出现 “control frame short length”，再按原始抓包确认。

**持久化**：Web 服务把 {revision, items{pid: {...}}, playlists} 存到 `media-library.json`（约 10.7MB，20,564 条 + first_seen 映射），重连时带 revision 订阅，避免重复全量。

### 3.8.1 设备播放列表与 FT 内容（新打通）

- `0x4C03` 的 MediaPlaylistProperties **合法 ID 是 0–5**；ID 5 是
  `MediaPlaylistContainedMediaItemsFileTransferIdentifier`（u8），不是条目本身。
- 订阅后设备会下发 `0x4C04 param3` 的 MediaPlaylist 组，例如：
  `{'id': 'e28be67340efc3b1', 'name': 'PlaylistSync', 'parent_pid': '0', 'genius': False, 'folder': False, 'contained_ft_id': 200}`
- 随后设备主动发 FT Setup（file type **0x0004**），内容是一个**扁平的大端 u64 PID 数组**
  （336 字节 = 42 首）。`parse_playlist_ft()` 每 8 字节解析一个 MediaItem PID，写入
  `LIBRARY["playlists"][id]["items"]`。
- 播放：`0x4C08 CollectionType=0 (Playlist)`，参数 `[playlist PID][type=0][start=0][库 UUID]`。
  真机验证 `PlaylistSync` 队列 `0/42`，第一首 `Knightmare`。
- 设备播放列表是**只读**的；UI 显示为“设备播放列表”，仅查看/播放；本地播放列表 UI 已移除。
- 当前设备只有 `PlaylistSync`（42 首，跨很多专辑）。用户已确认它**不是**蓝牙上看到的
  Recently Added；它是一个普通设备播放列表。

### 3.8.2 R29 PDF 结论：0x4C09、真实队列与 Recently Added

后来拿到了 **R29 全文（1019 页，2018-08-01）**，之前几个关键猜测可以定案。

#### 0x4C09 PlayMediaLibrarySpecial（page 943，Table 97-125）

参数表是：

| ID | 名称 | 类型 | 说明 |
|---|---|---|---|
| 0 | MediaLibraryUniqueIdentifier | utf8 | 媒体库 UUID |
| 1 | AllSongs | none | 播放整个媒体库的所有歌曲 |
| 2 | StartingMediaItemPersistentIdentifier | uint64, 0/1 | 起始曲目 |

所以 **0x4C09 不是 Recently Added / 特殊集合**，而是“播放全部歌曲”。之前猜
`type/start/uuid`、`uuid/type/start`、`playlist PID` 等无效，是因为参数含义完全不同。
后续不应再把 0x4C09 当作 Recently Added 的入口。

#### 设备真实队列（Playback Queue List Transfers）

R29 page 376、943–946、465 的流程：

1. `0x5000 StartNowPlayingUpdates` 的 PlaybackAttributes 组里请求
   **ID 17 = PlaybackQueueListContentTransferSize**（uint32，`0` 表示完整队列）。
2. `0x5000` 的第三个参数是 **PlaybackQueueListContentTransferInfoRequest** 组，
   请求队列条目的字段：`0=PID, 1=title, 6=album, 12=artist, 14=album artist, 16=genre, 18=composer`。
3. 设备在 `0x5001 NowPlayingUpdate` 里回：
   - `14 = PlaybackQueueListAvail`（bool）
   - `15 = PlaybackQueueListTransferID`（uint8）
   - `17 = PlaybackQueueListContentTransfer`（none，表示内容可用）
4. 随后设备用 FT v2 发队列内容：
   - **file type 0x0003**：扁平大端 u64 PID 数组；
   - **file type 0x0006**：MediaItemList（`count` + `start index` + 有序 MediaItem 组）。
5. 真机验证：
   - 专辑队列 52 首：416 字节 = 52 × 8；
   - `PlaylistSync` 队列 42 首：336 字节 = 42 × 8；
   - Web UI 的“接下来”现在显示**设备真实队列**（`QUEUE["source"]="device"`）。

代码：`now_playing_params()` 请求 ID 17/组 2，`apply_queue_ft()` 解析 0x0003/0x0006，
`queue_snapshot()` 返回 `source/base_index/total`。`0x0006` 已实现解析，但当前设备的
队列条目都在媒体库里，所以实测走的是 0x0003。

#### Recently Added

- R29 `MediaItem` 属性表（page 940）和 `MediaItemProperties`（page 938）都**没有**
  DateAdded / 添加日期字段。
- `MediaLibraryCollectionType` 只有 Playlist / Artist / Album / Album Artist / Genre / Composer。
- `0x4C09` 是 All Songs，不是 Recently Added。
- 因此 **USB iAP2 没有原生的“最近添加”排序/集合**；配件侧 `first_seen` 只能作近似，
  但独立 AFC 导入器可以读取 `MediaLibrary.sqlitedb.item.date_added`（见 §3.8.3）。
  蓝牙 AVRCP Browsing 的 `Recently Added` 虚拟目录是另一条可选路径。

当前开发机仍无蓝牙适配器（`/sys/class/bluetooth` 不存在），所以 AVRCP 浏览只能等
USB dongle 或 btsnoop/HCI 抓包。

### 3.8.3 独立 MediaLibrary.sqlitedb 导入（真实 date_added）

独立工具 `media_db_import.py` 已打通 AFC/ifuse 路径：

- 读取 `/var/mobile/Media/iTunes_Control/iTunes/MediaLibrary.sqlitedb`
- WAL 模式下用 SQLite backup API 取得一致快照，避免直接复制导致 malformed
- `item.item_pid` 与 iAP2 MediaItem PID 完全对应
- `item.date_added` 是 Apple/Core Data 时间（+978307200 转 Unix）
- 当前结果：20,688 行，20,564 个 PID 全部匹配；日期范围 2024-03-21 至 2026-10-22
- 输出 `media-db-dates.json`；`web_app.py` 只读取这个结果文件，不调用 ifuse/libimobiledevice

Web UI 的 Recently Added 现在优先使用真实 `date_added`，并在表格显示 Add 日期；`first_seen` 仅作兜底。
封面也已通过独立工具 `media_db_artwork_import.py` 拉取：2,187 个唯一原图（1.34GB）、
20,629 个 item 映射、2,090 个 album 映射；Browser 专辑表显示缩略图，详情显示原图。
首次全量拉取实测 **113.56 秒**（约 11.8 MB/s）。
导入时 iAP2/HID 服务与 AFC 不能同时占用 iOS device（USB config 2 vs 3），操作步骤见 `MEDIA_DB_IMPORT.md`。

### 3.9 HID 报告重组（封面撕裂的真正根因）

**根因：** HID 输入报告是 `[report_id][LCB][payload]`，且按 report ID 的 size class 补齐。`hidapi.read()` 对大报告每次只返回一个 64 字节 USB 包；此前 `HIDReader` 把所有包原样拼进 iAP2 字节流，导致每个报告头（2 字节）被当成链路数据。FT 大帧使用 report ID 12（每报告 768 字节），于是每 766 字节就插入 2 字节垃圾：600×600 封面的 242 个报告头 = 484 字节，正好解释 187,402 vs 真实 186,918，以及“每帧长度少算 170 字节/checksum 不对”的全部症状。

**修复：** `hid_async.HIDReader._extract_payload()` 按 report ID 的固定总长重组报告，剥掉 2 字节头，只把 payload 交给链路层。总长计算：
`total = INPUT_REPORTS[rid] + 1`；若 `total > 64` 且 `total % 64 <= 2`，设备会把末尾 1-2 字节的 padding 省掉，总长向下取整到 64 的倍数（实测 ID 8: 194→192，ID 9-11 同理；ID 7 的 96 字节保留）。
报告尾部的零 padding 仍交给链路层，由 FF5A 同步扫描自然跳过。

**验证：** 同一份 `artifacts/logs/artwork-raw-01.log` 离线剥头后，3 个 FT 帧的 `sum(frame)%256==0` 全部通过，组装出 186,918 字节 JPEG，`djpeg` 零警告、PIL 严格解码通过；硬件实测 200×200（27,498B）与 600×600（186,918B）封面均干净。

**保留的兜底逻辑：**

1. `ref_iap2_link_layer.py` 的 `_pending_ft` / `_dispatch_pending_ft`：若未来固件真的少算 FT 长度，仍可从下一帧前的非零尾部恢复。
2. `_fix_csm_length`：大 4C04 若出现 1 字节欠计数，仍可修正。
3. `feed_eof()` sentinel、FF5A 同步扫描、无 `input.reset()` 等仍保留。

## 4. 当前文件

目录：`/home/ops/iap2mediactrl`

| 文件 | 状态 | 说明 |
|---|---|---|
| `artifacts/probes/ipod_probe.py` | 本地参考 | 枚举 USB、打印 configuration、切 config 2、读 HID report descriptor；不随仓库分发 |
| `artifacts/probes/hid_probe.py` | 本地参考 | 发送 detect marker，读 HID echo（raw libusb SET_REPORT）；不随仓库分发 |
| `artifacts/probes/hid_transport.py` | 本地参考 | pyusb HID transport（自动选 report ID、raw 回调）；不随仓库分发 |
| `hid_async.py` | **已修复（HID 报告重组）** | asyncio HID reader/writer；按 `INPUT_REPORTS` 的 report ID 长度重组报告、剥掉 2 字节 `[report_id][LCB]` 头，只把 payload 交给链路层；`HID_LOG_RAW=1` 调试打印 |
| `artifacts/probes/pyusb_hid.py` | 本地参考 | 直接 pyusb EP 0x83 传输；`IAP2_TRANSPORT=pyusb` 当前未打通（链路停在 state=0），正式路径仍用 hidapi |
| `ref_iap2_link_layer.py` | **可用** | 参考 wiomoc 的 iAP2 链路；**FF5A 同步扫描、无 reset()、feed_eof 唤醒**；`pending_ft` / `_fix_csm_length` 作为兜底保留；常量 `FT_SESSION_ID=12`、`file_transfer_handler` 钩子 |
| `csm.py` | 可用 | CSM 0x4040 编码/解析 |
| `mfi_local.py` | 可用 | 本地 MFi provider |
| `auth_test.py` | **可用（全部特性）** | 完整流程：link → auth → identification(含 HID) → 0x5000/0x4C00 → **HID play/pause/next/previous 实测** → **FT artwork 落盘** → 媒体库 0x4C03/0x4C04 实验（`LIB_EXPERIMENT=uuid|uuid+rev|full`）；`HID_COMMAND=play|pause|next|...` 发送控制命令 |
| `web_app.py` | **可用（常驻 Web UI）** | 后台线程维护 iAP2 会话（断线自动重连）+ HTTP API + 单页控制台；Now Playing/封面/专辑/艺术家/歌曲/设备播放列表/音量/随机/循环 |
| `run_web.sh` | 可用 | 启动 Web UI 的包装脚本（`PORT=8080 ./run_web.sh`） |
| `docs/CACHE.md` | 文档 | 缓存/持久化机制、失效规则、嵌入式建议 |
| `docs/reference/788716769-Accessory-Interface-Specification-R29.pdf` | 本地参考 | Apple R29 全文（1019 页）；0x4C09、队列 FT、MediaItem 属性表的权威依据；不纳入 Git |
| `media_db_import.py` | **独立工具** | 通过 ifuse/AFC 读取 `MediaLibrary.sqlitedb`，生成 `media-db-dates.json`；不导入 iAP2/HID 代码 |
| `docs/MEDIA_DB_IMPORT.md` | 文档 | 媒体库 DB 独立导入说明、命令、结果格式 |
| `media-db-dates.json` | 数据 | 独立导入结果：20,688 个 PID；20,564 个匹配当前镜像 |
| `artifacts/media-db-snapshot/` | 数据 | 只读 SQLite 快照（约 49MB）；含隐私元数据，勿公开；不纳入 Git |
| `media_db_artwork_import.py` | **独立工具** | 通过 AFC 读取 `iTunes/Artwork/Originals`，生成 `media-db-artwork.json` 和本地缩略图 |
| `media-db-artwork.json` | 数据 | 20,629 个 item / 2,090 个 album 的封面映射 |
| `media-artwork/` | 数据 | 2,187 张原始封面（1.34GB）+ 2,179 张 160px 缩略图 |
| `docs/PORTING_CPP.md` | 文档 | C++/嵌入式移植草稿（架构、类草图、SQLite schema、测试计划） |
| `media-library.json` | 数据 | 媒体库镜像（约 10.7 MB，20,564 条，含 first_seen 映射；revision 随同步递增） |
| `playlists.json` | 数据 | 配件侧本地播放列表（iAP2 媒体库只读，不能改设备端列表） |
| `history.json` | 数据 | 最近 500 次播放记录（智能列表/Up Next 历史） |
| `artwork-cache/` | 数据 | 封面磁盘缓存（最多 128 张，LRU） |
| `artifacts/probes/link_probe.py` | 本地参考 | 早期 link probe，未用；不随仓库分发 |
| `artifacts/probes/syn_debug.py` | 本地参考 | 原始 SYN 调试；不随仓库分发 |
| `artifacts/probes/wiomoc_link_test.py` | 本地参考 | wiomoc link layer 参考；不随仓库分发 |
| `artifacts/logs/hid-test-01..07.log` | 日志 | HID component 0x0012 识别 → StartHID → **play/pause/next/previous 全部实测** |
| `artifacts/logs/media-library-01..15.log` | 日志 | 媒体库调试全过程（含 4C01 原始 TLV、4C03/4C04、FT artwork 过程） |
| `artifacts/logs/media-library-06/10/13.log` | 日志 | **HID 帧实验：URB 原始字节 dump**（`HID_LOG_RAW=1`），确定了 iOS device 的 URB 分帧方式 |
| `artifacts/captures/media-library-body-4c01.bin` / `-4c04.bin` | dump | 媒体库原始 body（4C01 含两库 UUID/修订号；4C04 含修订信息） |
| `artifacts/artwork/artwork-251.jpg` / `artwork-600-clean.jpg` | 产物/调试 | 干净版：200×200，27,498B；600×600，186,918B；旧文件保留为不同阶段的撕裂样本 |

## 5. 如何运行

```bash
cd /home/ops/iap2mediactrl
source .venv/bin/activate
```

### 5.1 只读 USB 描述符

```bash
sudo .venv/bin/python artifacts/probes/ipod_probe.py
```

### 5.2 HID marker 测试

```bash
sudo .venv/bin/python artifacts/probes/hid_probe.py
```

`artifacts/probes/` 是本地开发资料，不随仓库分发；新环境可直接使用
`auth_test.py` 完成完整流程。

### 5.3 完整流程（认证 + 识别 + Now Playing + HID + 封面 + 媒体库）

```bash
sudo .venv/bin/python auth_test.py
```

`auth_test.py` 顶部的 `CERT_PATH` / `KEY_PATH` 指向 `../iap2cert/`（即 `/home/ops/iap2cert/`）。

环境变量：

| 变量 | 值 | 说明 |
|---|---|---|
| `LIB_EXPERIMENT` | `uuid`（默认）/ `uuid+rev` / `full` | 0x4C03 参数实验；`full` 会让设备静默（已验证） |
| `HID_COMMAND` | `play` / `pause` / `next` / `previous` / ... | identification 后 2 秒发送 HID 命令；空=不发送 |
| `HID_LOG_RAW` | `1` | 打印每个 URB 原始字节（调试） |

注意：每次运行都是完整 `identification → 拒绝/接受 → 5000/4C00` 流程，约 30 秒后自动退出。iOS device 每次重连都需要重新走完整 identification（不支持会话恢复）。

### 5.4 Web UI（常驻）

```bash
cd /home/ops/iap2mediactrl
./run_web.sh                 # 默认 8080，后台服务另开终端；Ctrl+C 停止
# 或：PORT=9000 ./run_web.sh
```

浏览器打开 `http://<开发机IP>:8080/`（本机 `http://10.0.0.85:8080/`）。

- 实时显示 Now Playing（标题/艺术家/专辑/进度/状态/队列/随机/循环/流派/音轨/App）与封面
- 按钮：上一首 / 播放·暂停 / 下一首；**音量 −/+、静音、停止、随机、循环、继续当前队列**
- **布局**：桌面两列（左：正在播放 / 队列 / Library 统计 / 会话；右：Browser 媒体库），页面顶部对齐；灰色深色框线、直角、等宽字体、全英文界面。Library 统计含曲目/专辑/同步/rev/库列表，Resync 小按钮在左侧。
- **本地播放列表 UI 已移除**：不显示创建/导入/导出/加入播放列表；仅保留设备播放列表查看/播放。
- **媒体库面板**：曲目数/专辑数/同步状态/revision；列表统一为紧凑表格，七个标签页：
  - **专辑**：封面网格（可搜索，Recently Added / Name / Artist 排序，默认 Recently Added 降序），点专辑 → 曲目列表；
  - **艺术家**：5,045 位艺术家（可搜索/分页），点艺术家 → 专辑列表 → 曲目；
  - **专辑艺术家**：1,059 位（可搜索/分页），点条目 → 专辑列表；
  - **流派**：168 个（可搜索/分页），点流派 → 专辑列表；
  - **歌曲**：20,564 首（可搜索/分页），支持按标题/艺术家/专辑/时长/最近添加排序；
  - **最近添加**：优先使用 `media-db-dates.json` 中的真实 `date_added`；未导入结果时回退到 `first_seen`，旧库条目可能不显示；
  - **设备播放列表**：只读，可查看/播放；本地播放列表 UI 暂时移除。
- **Up Next**：优先显示设备真实队列（FT 0x0003/0x0006）；网页发起的本地列表播放也会更新队列，
  显示当前及接下来曲目（点击可单曲播放）。
- **播放历史**：`history.json` 记录最近 500 次曲目变化；生成“最近播放”“最常播放”智能列表。
- **本地播放列表**：后端和 HTTP API 保留；当前浏览器界面不提供创建/编辑入口。
- **播放**：单曲 `0x4C07`；整张专辑/艺术家 `0x4C08`（CollectionType=2/1）；
  流派/专辑艺术家用 `0x4C07` 有序 PID 列表；本地播放列表 `0x4C07`；
  恢复 iOS device 当前队列 `0x4C06`。队列信息由 0x5001 回传。
- **封面缓存**：FT 完成后写 `artwork-cache/<id>.jpg`，最多 128 张，LRU 清理。
- 断线（iOS device 休眠等）时每 2 秒自动重连；页面每秒轮询状态、进度条本地插值

HTTP API：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state` | 当前状态 JSON（含队列/随机/循环/Up Next） |
| GET | `/api/artwork` | 最新封面（JPEG；缺失 EOI 时自动补 `FF D9`） |
| GET | `/api/library` | 媒体库摘要 + 专辑列表（{pid, artist, album, tracks}） |
| GET | `/api/library/album?pid=...` | 某专辑的曲目；无 pid 时可用 `artist`+`album` |
| GET | `/api/library/artists?q=&limit=&offset=` | 艺术家列表 |
| GET | `/api/library/artist?pid=...` | 某艺术家的专辑列表；也可用 `name` |
| GET | `/api/library/album-artists?q=&limit=&offset=` | 专辑艺术家列表 |
| GET | `/api/library/album-artist?name=...` | 某专辑艺术家的专辑列表 |
| GET | `/api/library/genres?q=&limit=&offset=` | 流派列表 |
| GET | `/api/library/genre?name=...` | 某流派的专辑列表 |
| GET | `/api/library/recent?limit=&offset=` | 最近添加（优先 `date_added`，回退 `first_seen`；见 `note` 字段） |
| GET | `/api/library/songs?q=&limit=&offset=&sort=` | 歌曲列表；sort=title/artist/album/duration/recent |
| GET | `/api/queue` | 队列快照（设备队列或本地队列；当前 + 接下来） |
| GET | `/api/history?limit=` | 最近播放 |
| GET | `/api/history/top?limit=` | 最常播放 |
| GET | `/api/smart/recent` / `/api/smart/frequent` | 智能列表别名 |
| GET | `/api/playlists` | 本地播放列表摘要 |
| GET | `/api/playlists/<id>` | 播放列表详情 + 已解析曲目 |
| GET | `/api/playlists/export` | 导出全部本地播放列表 JSON |
| POST | `/api/playlists/import` | `{"playlists":[{"name","items"}...]}` 导入 |
| POST | `/api/playlists` | `{"name":"..."}` 新建 |
| POST | `/api/playlists/<id>/rename` | 重命名 |
| POST | `/api/playlists/<id>/add` | `{"item_id":"..."}` 或 `{"item_ids":[...]}` |
| POST | `/api/playlists/<id>/remove` | 移除 |
| POST | `/api/playlists/<id>/move` | `{"from":n,"to":m}` 排序 |
| POST | `/api/playlists/<id>/clear` | 清空 |
| POST | `/api/playlists/<id>/play` | `{"start_index":n}` 播放 |
| DELETE | `/api/playlists/<id>` | 删除 |
| POST | `/api/command` | `play/pause/stop/next/previous/shuffle/repeat/volume_up/volume_down/mute` |
| POST | `/api/play` | `item_id` / `album_pid` / `artist_pid` / `genre` / `album_artist` / `item_ids` / `current_selection` |
| POST | `/api/library/sync` | 清空媒体库镜像并断开重连，触发全量同步 |

注意：Web 服务无鉴权，仅建议在受信局域网使用；它和 `auth_test.py` **不能同时运行**（USB HID 独占）。

## 6. 功能状态

### 6.1 媒体库（已完成，20,564 首 / 2,095 张专辑 / 5,045 位艺术家）

- 全量同步流程、4C03/4C04 参数、revision 增量、reset/删除阶段、重新订阅时机见 §3.8 ✓
- Web 服务持久化 `media-library.json`（revision + 全部曲目），重连带 revision 订阅只收增量 ✓
- 专辑按 `album_pid` 聚合；20,563 首有专辑，1 首没有 album 属性（单曲等，正常）
- 派生索引：专辑（`_summary`）、艺术家（`_artists`）、歌曲（`_songs`），按库 `version` 失效
- 设备播放列表：`0x4C04 param3` 当前有 `PlaylistSync`（42 首），内容通过
  FT type `0x0004` 获取；设备端只读。本地播放列表 UI 暂时移除，后端数据和 API
  仍保留在 `playlists.json` / `/api/playlists`，未从协议层删除。

### 6.2 Now Playing

- 订阅 `0x5000`，请求 MediaItem 属性 1/4/6/12/26 与 PlaybackAttributes 0–7。
- `0x5001` 现在上报 status / elapsed / queue_index / queue_count / queue_chapter /
  shuffle / repeat / app；Web UI 显示“队列 3/12、随机 歌曲、循环 全部”。
- status=2（暂停）时标题/专辑/艺术家仍上报；artwork_id 变化触发 FT Setup。

### 6.3 HID 控制

HID report descriptor 12 bit（`B0 B1 B5 B6 B9 BC E9 EA E2 B7 B3 B4`，2 字节 report）：

| bit | usage | 功能 |
|---|---|---|
| 0 | 0xB0 Play | 播放 |
| 1 | 0xB1 Pause | 暂停 |
| 2 | 0xB5 Scan Next | 下一首 |
| 3 | 0xB6 Scan Previous | 上一首 |
| 4 | 0xB9 Random Play | 随机（Off→Songs→Albums） |
| 5 | 0xBC Repeat | 循环（Off→One→All） |
| 6 | 0xE9 Volume Increment | 音量 + |
| 7 | 0xEA Volume Decrement | 音量 − |
| 8 | 0xE2 Mute | 静音（toggle） |
| 9 | 0xB7 Stop | 停止（设备回 status=2） |
| 10 | 0xB3 Fast Forward | 快进（短按被设备当作下一首；长按未验证） |
| 11 | 0xB4 Rewind | 快退（短按被设备当作上一首；长按未验证） |

实测：扩展 descriptor 被设备接受；play/pause/next/previous、shuffle、repeat、stop、mute
均能发送（stop 使 status→2，shuffle/repeat 有 0x5001 回传）。音量没有绝对值——iAP2
规范没有音量消息，只能 HID 步进。FF/RW 短按实际表现为 next/previous，所以 UI 没有暴露为
“seek”；绝对 seek 属于协议不支持。

### 6.4 Web UI / 播放列表

- 七个标签页：专辑 / 艺术家 / 专辑艺术家 / 流派 / 歌曲 / 最近添加 / 设备播放列表；
- 专辑/艺术家/专辑艺术家/流派：点条目 → 专辑 → 曲目；歌曲：点歌曲即播放；
- 歌曲排序：标题 / 艺术家 / 专辑 / 时长 / 最近添加；
- 设备播放列表：只查看/播放；本地播放列表 UI 已移除；
- 智能列表：最近播放 / 最常播放（来自 `history.json`）；
- 本地播放列表：后端和 HTTP API 保留；当前浏览器界面不提供创建/编辑入口；
- Up Next：设备真实队列（FT 0x0003/0x0006）+ 网页发起的本地有序队列；
- 播放：单曲 0x4C07、专辑 0x4C08 type=2、艺术家 0x4C08 type=1、
  流派/专辑艺术家/本地列表 0x4C07 有序 blob、继续当前队列 0x4C06；
- 队列/随机/循环/流媒体信息来自 0x5001；设备队列内容来自 FT 0x0003/0x0006；真机验证：
  - 2 首列表播放 `0/2 → next → 1/2`；
  - 艺术家播放队列 `0/131`；
  - 流派 Acoustic 队列 `0/26`；
  - 专辑艺术家 #ffffff Records 队列 `0/6`；
  - 单曲播放队列 `0/1`，Up Next/历史正常。

**最近添加的数据来源：** iAP2 媒体库没有原生的“添加日期”字段（MediaItem property 0–20
无日期）。`library_recent` 优先读取独立导入器生成的 `media-db-dates.json`
（`item.date_added`）；未导入时回退到 **首次发现** 时间 `first_seen`，旧库条目可能没有
时间而不显示。新加入 iOS device 的曲目会在下一次增量同步时获得 `first_seen` 时间；如果随后
完成 AFC 导入，则使用数据库中的真实 `date_added`。UI 的 `note` 字段会说明当前数据来源。

### 6.5 封面

✅ 已修复并验证：200×200（27,498B）与 600×600（186,918B）均 `djpeg` 零警告、PIL 严格解码，画面正常。空 artwork 按 0 字节处理（不保存）。根因与修复见 §3.9。

### 6.6 协议不支持 / 未实现

- **绝对音量**：iAP2 没有音量消息；HID 只有 Volume Increment/Decrement。静音已通过
  HID 0xE2 实现；绝对音量（0–100）未验证，设备可能只认步进。
- **进度 seek/scrub**：Now Playing 只给 elapsed，没有 seek 命令；HID Fast Forward/Rewind
  （0xB3/0xB4）已加入 descriptor，但短按被设备当作 next/previous，长按 seek 未验证。
- **“添加日期”/Recently Added 原始字段**：iAP2 MediaItem property 0–20 没有日期；
  当前“最近添加”优先使用 AFC 导入的 `MediaLibrary.sqlitedb.item.date_added`，
  未导入时回退到 `first_seen`。未接入设备原生 Recently Added 特殊集合（见下）。
- **设备端播放列表编辑**：iAP2 媒体库只读；配件侧本地播放列表仅保留在后端/API。
- **设备播放列表内容**：已打通！FT file type **0x0004** 是扁平 u64 PID 数组；
  `MediaPlaylistProperties` 必须请求合法 ID 0–5（尤其是 5 = FT identifier）。
  当前设备有 `PlaylistSync`（42 首），可查看/播放；设备端只读。
- **设备真实队列内容**：已打通！`0x5000` 请求 PlaybackAttributes ID 17
  （PlaybackQueueListContentTransferSize）并带第三个参数
  PlaybackQueueListContentTransferInfoRequest；设备回 `0x5001` 的 14/15/17，
  再用 FT **0x0003**（PID 数组）或 **0x0006**（MediaItemList）发送队列。
  真机验证专辑 52 首、PlaylistSync 42 首；Up Next 现在优先显示设备队列。
- **0x4C09 PlayMediaLibrarySpecial**：R29 page 943 已确认参数为
  `[媒体库 UUID][AllSongs none][起始 PID]`，是“播放全部歌曲”，不是 Recently Added；
  未做 UI 入口（避免误触发全库播放）。
- **逐曲/专辑封面缩略图**：MediaItem 设备数据里没有 property 20 的 artwork ID
  （实测只发 0–19 和 25），因此无法通过 iAP2 按条目请求封面；Now Playing 封面仍走 FT。
  媒体库专辑/曲目缩略图由独立 AFC 导入器 `media_db_artwork_import.py` 生成，
  存放在 `media-artwork/` 与 `media-db-artwork.json`。
- **电池百分比**：`0xAE01 PowerUpdate` 只有 accessory power mode，没有设备电池电量。
- **设备信息/时间/语言通知**：0x4E09–0x4E0C 存在，但当前未加入 identification 的
  received 列表（避免识别风险）；价值低，未接入。
- **歌词 / EQ / crossfade / 音效**：iAP2 媒体库/Now Playing 不提供。
- **Web 鉴权 / 多设备**：当前无鉴权（仅 LAN）、单台 iOS device、单 iAP2 连接；生产需加 token/会话管理。

### 6.7 代码健壮性

- **空 artwork FT**：设备有时先发 size=0 的 Setup（此时无封面），FirstAndOnlyData 0 字节 → 已回 Success，按空处理。
- **0x4C04 重复性**：设备可能重复发送同一 MediaItem；按 PID 覆盖，幂等。
- **大帧**：报告头修复后 link/CSM checksum 全部通过；`_pending_ft` / `_fix_csm_length` 仅作兜底。
- **链路层**：实验级实现，无完整窗口/重传状态机（生产参考 DiPlay 的 Iap2LinkEngine.kt）。
- **播放列表缺失项**：读取/播放时跳过缺失 PID，并在 API 里报告 `missing`。

### 6.8 其他

- `IAP2Connection.ea_streams` 仍是透传 dict，未做 EA session（参数 10 被拒，也不需要）。
- 迁移新机器时按 §7 检查清单。
- 缓存/持久化细节见 `docs/CACHE.md`；C++/嵌入式移植见 `docs/PORTING_CPP.md`。

## 7. 迁移到新 agent 的检查清单

1. 复制整个 `/home/ops/iap2mediactrl` 到新机器（**不要复制 `iap2cert` 到任何不安全位置**）。
2. 复制 `/home/ops/iap2cert` 到新机器，或修改 `auth_test.py` 的 `CERT_PATH` / `KEY_PATH`。
3. 安装系统包：

```bash
sudo apt update
sudo apt install -y \
  python3 python3-venv python3-pip \
  libusb-1.0-0-dev libhidapi-hidraw0 libhidapi-dev \
  usbutils
```

4. 重建 venv：

```bash
cd /home/ops/iap2mediactrl
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

5. 确认 iOS device 已直通到 Linux：

```bash
lsusb -d 05ac:
lsusb -t
```

6. 用 root 或配置 udev 权限运行 USB/HID 操作。
7. 先跑 `auth_test.py`。如需底层调试，再运行本地的
   `artifacts/probes/ipod_probe.py` 和 `artifacts/probes/hid_probe.py`（不随仓库分发）。
8. 迁移后先跑一次完整的 `auth_test.py`，确认 identification 被接受（accepted）、Now Playing 出现真实曲目名、FT artwork 落盘、`HID_COMMAND=play` 能让 iOS device 开始播放。
## 8. 重要注意事项

- **证书/私钥是敏感材料**：不要提交到 Git，不要贴到日志、聊天、issue。
- `ref_iap2_link_layer.py` 是从 wiomoc/iap2 参考并修改的；如果项目要分发，请确认许可证并保留 attribution。当前仅用于开发/测试。
- 当前 link layer 为了 demo 做了简化：per-packet ACK、容忍一个 seq gap；生产实现应使用完整的 iAP2 重传/窗口逻辑（可参考 DiPlay 的 `Iap2LinkEngine.kt`）。
- **HID 输入报告必须按 report ID 重组并剥掉 2 字节头**。iOS device 的报告格式是 `[report_id][LCB][payload]`，按 size class 补齐；若把报告头混入 iAP2 流，会在大帧中每 766 字节插入 2 字节，造成封面撕裂和“length/checksum 少算”的假象。换固件/机型时先跑 `HID_LOG_RAW=1` 确认 report ID → 总长映射（尤其 1-2 字节尾部是否省略）。
- **Identification 是脆弱点**：任何新参数加入前，先双向确认 0x1D03 会拒绝哪个 param（设备只给空 payload 的 rejected param id，二分定位）。
- 如果 VMware USB 直通不稳定，优先在裸机 Linux 或 Raspberry Pi 上测试。
- pyusb 在 `set_configuration()` 后必须 `dispose_resources()`，否则后续 libhidapi 打不开 HID（早期 observed 过）。

## 9. 关键日志摘要

### 认证成功

```text
RX CSM 0xaa00
TX CSM 0xaa01 bytes=617
RX CSM 0xaa02 challenge bytes=32
TX CSM 0xaa03 signature bytes=64
RX CSM 0xaa05 authentication succeeded
```

### 识别成功（含 HID）

```text
RX CSM 0x1d00
TX CSM 0x1d01 bytes=221   ← 含 param 0x12 iAP2 HID Component
RX CSM 0x1d02 identification accepted
```

### Now Playing（真实播放中）

```text
TX CSM 0x5000
RX CSM 0x5001
  media item:
    title='Shifting Temple'
    duration_ms=166440
    album='誰が為のエンドロール'
    artist='黒魔'
    artwork_id=143
  playback:
    status=1
    elapsed_ms 递增
    app='Music'
```

### HID 控制（实测）

```text
HID_COMMAND=play      → RX CSM 0x5001 status=2 → 1，elapsed 递增 ✓
HID_COMMAND=pause     → RX CSM 0x5001 status=1 → 2 ✓
HID_COMMAND=next      → 切到 'Astaluego' ✓
HID_COMMAND=previous  → 切到 '旅の終わりに' ✓
TX CSM 0x6802 bytes=18   ← report [0x01 0x00] / [0x00 0x00]
```

### 封面（FT session 12）

```text
RX CSM 0x5001 (artwork_id=...)
< session 12: Setup id=... filetype=2 size=186918
> session 12: Start [... 01]
  FT complete id=... filetype=2 bytes=186918 expected=186918
> session 12: Success [... 05]
saved artwork-600-clean.jpg: 186918 bytes
  (600×600 JPEG, djpeg 零警告, PIL 严格解码通过)
```

### 媒体库（全量同步）

```text
RX CSM 0x4c01  ← 库信息（iPod touch / Apple Music Radio）
TX CSM 0x4c03  ← [uuid, revision="0", MediaItemProperties(0..19+), MediaPlaylistProperties]
RX CSM 0x4c04  ← param6=reset（清空本地镜像）
RX CSM 0x4c04  ← 大量 param4/param5（删除项，约 6 万个 ID，最后一条 param7=0x5A）
TX CSM 0x4c03  ← 删除阶段结束后重发（带最新 revision）
RX CSM 0x4c04  ← param2 = MediaItem 组（title/album/artist/duration/track…）
                 例：title='Fighter Girl' album='Radical Destruction' artist='F9'
                     albumArtist='Amateras Records' duration_ms=251973
总计 20,564 首 / 2,095 张专辑（按 album_pid 聚合；当前 revision 约 882406，随播放/库变化递增）
```
