# MediaCtrl C++ 移植草稿（嵌入式 Linux）

本文是当前 Python 原型移植到 C++ 的架构草稿，目标是运行在带 USB Host 的嵌入式 Linux
（ARM Cortex-A7/A53、128–512 MB RAM、只读 rootfs + 可写数据分区）。不是逐行翻译指南，
而是把协议层、缓存层和 UI 层拆成可测试的 C++ 模块。

> 目标平台说明：本文针对嵌入式 Linux。ESP32-P4 / M5Stack Tab5 的实现指南见
> `tab5-port-assessment.md` 与 `tab5-afc-db-pull-feasibility.md`。

最后更新：2026-11-04。

## 1. 目标与约束

- USB HID 连接 iPod touch（iOS 15.7.8），configuration 2 / HID interface 2 / EP 0x83。
- 功能：MFi 认证、识别、Now Playing、封面 FT、媒体库全量/增量、
  专辑/艺术家/专辑艺术家/流派/歌曲/最近添加/播放列表视图、
  单曲/专辑/艺术家/流派/专辑艺术家/本地列表播放、Up Next/播放历史/智能列表、
  HID 播放控制/音量/静音/停止/随机/循环。
- 资源约束：单核 1 GHz、256 MB RAM、SQLite 数据库约 30–60 MB、封面缓存上限 64 MB。
- 实时性：USB 读线程不能阻塞；iAP2 ACK/重传定时器精度 100 ms 级即可。
- 可靠性：掉电后媒体库镜像和播放列表不能损坏；网络/UI 线程崩溃不能拖死 iAP2 会话。

## 2. 分层架构

```text
┌──────────────────────────────────────────────────────────────┐
│  Web UI / HTTP API (cpp-httplib 或 CivetWeb)                │
│  /api/state /api/library /api/library/album /api/playlists  │
└───────────────▲──────────────────────────────┬───────────────┘
                │ snapshot / commands          │ JSON
┌───────────────┴──────────────────────────────▼───────────────┐
│  AppService (thread-safe facade)                             │
│  NowPlayingState | MediaLibraryStore | PlaylistStore         │
└───────────────▲──────────────────────────────┬───────────────┘
                │ callbacks                    │ CSM commands
┌───────────────┴──────────────────────────────▼───────────────┐
│  Iap2Session                                                 │
│  LinkLayer | CsmCodec | FtAssembler | MfiProvider            │
└───────────────▲──────────────────────────────┬───────────────┘
                │ HID reports                  │ HID reports
┌───────────────┴──────────────────────────────▼───────────────┐
│  HidTransport (libusb / hidapi)                              │
│  report reassembly + 2-byte header stripping                 │
└──────────────────────────────────────────────────────────────┘
```

推荐目录：

```text
src/
  main.cpp
  app/AppService.{h,cpp}
  iap2/LinkLayer.{h,cpp}
  iap2/CsmCodec.{h,cpp}
  iap2/Iap2Session.{h,cpp}
  iap2/FtAssembler.{h,cpp}
  mfi/MfiProvider.{h,cpp}
  hid/HidTransport.{h,cpp}
  hid/HidReportMap.{h,cpp}
  store/MediaLibraryStore.{h,cpp}
  store/PlaylistStore.{h,cpp}
  http/HttpServer.{h,cpp}
  http/ApiHandlers.{h,cpp}
  util/ByteOrder.h  util/Log.h  util/AtomicFile.h
third_party/
  asio/ mbedtls/ sqlite3/ httplib/ nlohmann_json/
```

## 3. 线程与事件模型

推荐 **3 类线程**：

1. **USB/协议线程**（单线程 `asio::io_context`）：
   - 阻塞读 `hid_read_timeout()` 放到独立 `std::thread`，把 `std::vector<uint8_t>` 投递到
     `asio::post()`；或直接用 libusb 异步 transfer。
   - 链路层定时器（ACK、重传、EAK）全部用 `asio::steady_timer`。
   - 所有 `LinkLayer` / `Iap2Session` / `MfiProvider` 只在这个线程访问。
2. **HTTP 线程池**（2–4 线程）：
   - 只读 `AppService` 的快照（`std::shared_mutex` 或 `std::atomic<std::shared_ptr<const Snapshot>>`）。
   - 写操作（播放、播放列表 CRUD）通过 `asio::post()` 投递到协议线程，等待 `future`。
3. **存储线程**（可选）：
   - SQLite 写入批量提交；或由协议线程每 10 秒同步提交（20k 条事务很快）。

跨线程命令示例：

```cpp
struct PlayCommand { std::vector<uint64_t> item_pids; uint32_t start_index; };
std::future<Result> AppService::playItems(std::vector<uint64_t> ids, uint32_t start) {
  auto promise = std::make_shared<std::promise<Result>>();
  asio::post(iap2_ctx_, [this, ids = std::move(ids), start, promise] {
    promise->set_value(session_.playMediaLibraryItems(ids, start));
  });
  return promise->get_future();
}
```

## 4. HID 传输层（最重要的移植点）

### 4.1 报告格式

每个输入报告：

```text
[report_id: u8][LCB: u8][payload: N bytes]
```

- 报告总长由 report ID 的 size class 决定，见 `INPUT_REPORTS`（1:5, 2:9, …, 12:767）。
- 总长（含 report ID）= `size + 1`。
- 设备会省略整 64 字节包之后的 1–2 字节 padding：若 `total > 64 && total % 64 <= 2`，
  实际总长向下取整到 64 的倍数（ID 8: 194→192；ID 9–11 同理；ID 7 的 96 保留）。
- `hid_read()` 对大报告通常每次返回一个 64 字节 USB 包；同一报告只有第一包带 2 字节头。

### 4.2 C++ 报告重组

```cpp
class HidReportReassembler {
 public:
  explicit HidReportReassembler(std::array<uint8_t, 13> sizes) : sizes_(sizes) {}

  // 输入任意 USB 包，输出可送给 iAP2 链路层的字节。
  void feed(std::span<const uint8_t> chunk, std::vector<uint8_t>& out) {
    raw_.insert(raw_.end(), chunk.begin(), chunk.end());
    while (!raw_.empty()) {
      if (remaining_ == 0) {
        if (raw_.size() < 2) break;
        uint8_t rid = raw_[0];
        uint16_t total = sizes_[rid];
        if (total <= 2) {  // 未知 report id：保守透传
          out.insert(out.end(), raw_.begin(), raw_.end());
          raw_.clear();
          break;
        }
        raw_.erase(raw_.begin(), raw_.begin() + 2);  // 去掉 [id][LCB]
        remaining_ = total - 2;
      }
      size_t take = std::min<size_t>(remaining_, raw_.size());
      out.insert(out.end(), raw_.begin(), raw_.begin() + take);
      raw_.erase(raw_.begin(), raw_.begin() + take);
      remaining_ -= take;
    }
  }

 private:
  std::array<uint8_t, 13> sizes_{};
  std::vector<uint8_t> raw_;
  size_t remaining_ = 0;
};
```

> 不要“把所有 USB 包直接拼成 iAP2 流”。那会每 766 字节插入 2 字节报告头，
> 600×600 封面会多出 242×2=484 字节，表现为右侧竖向撕裂。

### 4.3 输出报告

```cpp
// [report_id][LCB][payload...]
// LCB: bit0=continuation, bit1=more_to_follow
void HidTransport::writeReport(uint8_t report_id, uint8_t lcb,
                               std::span<const uint8_t> payload);
```

## 5. iAP2 链路层

### 5.1 帧格式

```text
FF 5A | u16 length | u8 control | u8 seq | u8 ack | u8 session | u8 hdr_sum |
payload (length-9 bytes, 最后 1 字节是 payload checksum)
```

- `length` 是帧总长，最大 65535。
- 校验和：整帧字节和 `% 256 == 0`；头 9 字节单独校验。
- control 位：RST/SYN/ACK/EAK。
- session：control=10，EA=11，file transfer=12。

### 5.2 C++ 状态机

```cpp
enum class LinkState { Detect, Negotiate, Normal, Dead };
enum class LinkFlag : uint8_t { Syn=0x80, Ack=0x40, Eak=0x20, Rst=0x10 };

class LinkLayer {
 public:
  void onBytes(std::span<const uint8_t> data);   // 来自 HID 重组
  void sendCsm(uint16_t msg_id, std::span<const uint8_t> body);
  void sendFt(std::span<const uint8_t> datagram);
  void onTimer();
 private:
  void parseFrame(std::span<const uint8_t> frame);
  void handleAck(uint8_t ack);
  void handleEak(std::span<const uint8_t> seqs);
  void sendAck();
  void sendEak(std::vector<uint8_t> missing);
  uint8_t psn_ = 0, last_received_ = 0, last_acked_ = 0;
  std::map<uint8_t, PendingPacket> pending_;
  std::deque<uint8_t> rx_;
};
```

关键点：

- 保留 Python 版的 **FF5A 同步扫描**：输入任意对齐，逐字节找 `FF 5A`，跳过报告 padding。
- 单一 `max_outgoing=30` 窗口，per-packet ACK；超时 4 s，ACK 超时 500 ms。
- 收到重复 seq：回 ACK，不重复投递。
- 收到 `d > 2` 的乱序：回 EAK，列出缺失 seq。
- 不要在每个帧后 `reset()` 输入缓冲；字节流不能丢。
- FT 帧的 `_pending_ft` 兜底可以保留，但正常路径不应触发（报告头修好后 length/checksum 正确）。

### 5.3 CSM 编码

```cpp
struct CsmParam { uint16_t id; std::vector<uint8_t> value; };

std::vector<uint8_t> csmFrame(uint16_t msg_id, const std::vector<CsmParam>& params) {
  size_t body_len = 0;
  for (auto& p : params) body_len += 4 + p.value.size();
  std::vector<uint8_t> out;
  appendBE16(out, 0x4040);
  appendBE16(out, static_cast<uint16_t>(body_len + 6));
  appendBE16(out, msg_id);
  for (auto& p : params) {
    appendBE16(out, static_cast<uint16_t>(p.value.size() + 4));
    appendBE16(out, p.id);
    out.insert(out.end(), p.value.begin(), p.value.end());
  }
  return out;
}
```

## 6. MFi 认证

- 复用一个 MFi coprocessor 或本地证书/私钥（当前原型用 `../iap2cert/certificate.p7b` + `../iap2cert/identity.pk8`）。
- 嵌入式建议：
  - 证书/私钥放在受保护的 flash 分区或安全元件；不要放在 rootfs 明文。
  - 用 **mbedTLS** 的 ECDSA P-256 + SHA-256 实现 `AA02 → AA03`。
  - 私钥只在认证时短暂解密到 RAM；认证后 `memset` 清零。
- 消息流：
  - `AA00` RequestAuthenticationCertificate → `AA01` certificate blob
  - `AA02` challenge → `AA03` signature
  - `AA04` failed / `AA05` succeeded
- 识别：`0x1D00` → `0x1D01`，必须包含 HID component `0x0012`（当前 HID component ID 0x14E9）。
- 识别被拒时，设备只回空 payload 的 `0x1D03`；用二分法定位被拒参数。

## 7. 媒体库存储（SQLite）

### 7.1 Schema

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
-- meta: revision, library_uuid, last_sync_ms

CREATE TABLE items(
  pid            INTEGER PRIMARY KEY,
  title          TEXT NOT NULL,
  media_type     INTEGER,
  rating         INTEGER,
  duration_ms    INTEGER,
  album_pid      INTEGER,
  album          TEXT,
  track          INTEGER,
  track_count    INTEGER,
  disc           INTEGER,
  disc_count     INTEGER,
  artist_pid     INTEGER,
  artist         TEXT,
  album_artist   TEXT,
  genre          TEXT,
  composer       TEXT,
  is_compilation INTEGER
);
CREATE INDEX idx_items_album  ON items(album_pid);
CREATE INDEX idx_items_artist ON items(artist_pid);
CREATE INDEX idx_items_title  ON items(title COLLATE NOCASE);

CREATE TABLE playlists(
  id TEXT PRIMARY KEY, name TEXT NOT NULL,
  created_ms INTEGER, updated_ms INTEGER
);
CREATE TABLE playlist_items(
  playlist_id TEXT NOT NULL, position INTEGER NOT NULL, item_pid INTEGER NOT NULL,
  PRIMARY KEY(playlist_id, position)
);
CREATE INDEX idx_playlist_items_pid ON playlist_items(item_pid);
```

- `items.pid` 用 64 位无符号整数（MediaItemPersistentIdentifier），不要存十六进制字符串。
- 每批 `0x4C04`（例如 4 条 MediaItem）用一个事务；全量同步时每 500–1000 条提交一次。
- `revision` 存在 `meta` 表；重连时读出来发 `0x4C03`。
- 删除：`DELETE FROM items WHERE pid=?`；reset：`DELETE FROM items; DELETE FROM playlists;`。
- 全量同步前用 SQLite Backup API 备份到 `media-library.db.bak`。

### 7.2 同步状态机

```text
Idle
  → subscribe(revision)                 // 4C03
  → Deleting (param6/param4/5)         // 逐条删除，最后 param7=0x5A
  → Resubscribe (4C03 with latest rev) // 仅全量时
  → Ingest (param2/3)                  // 批量事务
  → Idle
```

规范要求同一 iAP2 连接最多一次全量；重新全量必须断开重连（当前 Python 版就是这样做的）。

## 8. Now Playing 与播放控制

### 8.1 订阅

```text
0x5000 StartNowPlayingUpdates
  group 0 MediaItemAttributes: 1(title), 2(media_type), 3(rating), 4(duration),
          5(album_pid), 6(album), 7(track), 8(track_count), 9(disc),
          10(disc_count), 11(artist_pid), 12(artist), 13(album_artist_pid),
          14(album_artist), 16(genre), 18(composer), 19(compilation), 26(artwork_id)
  group 1 PlaybackAttributes: 0(status), 1(elapsed), 2(queue_index),
          3(queue_count), 4(queue_chapter), 5(shuffle), 6(repeat), 7(app),
          8(library_uuid), 12(speed), 13(set_elapsed_available),
          14(queue_list_avail), 15(queue_transfer_id),
          16(app_bundle_id), 17(queue_list_size u32; 0=full)
  group 2 PlaybackQueueListContentTransferInfoRequest:
          0(PID), 1(title), 6(album), 12(artist), 14(album_artist),
          16(genre), 18(composer)
```

`0x5001` 解析后写入 `NowPlayingState`；HTTP 端用快照读。

### 8.1.1 真实播放队列（FT 0x0003 / 0x0006）

- 设备在 `0x5001` 回 PlaybackAttributes `14=PlaybackQueueListAvail`、
  `15=PlaybackQueueListTransferID`、`17=PlaybackQueueListContentTransfer`。
- 随后 FT v2：
  - **0x0003** = 扁平大端 u64 PID 数组；
  - **0x0006** = MediaItemList（u32 count + u32 start_index + 有序 MediaItem 组）。
- 解析后替换本地 `QUEUE`，`source="device"`；`queue_index - start_index` 为当前项。
- 当前真机验证专辑 52 首、PlaylistSync 42 首（均为 0x0003）。0x0006 已实现但未触发，
  因为队列条目都在媒体库中。

### 8.2 播放

| 操作 | 消息 | 参数 |
|---|---|---|
| 单曲 | `0x4C07` | blob(8B PID), start_index u32, library UUID |
| 专辑 | `0x4C08` | album PID, type=2, start_index, UUID |
| 艺术家 | `0x4C08` | artist PID, type=1, start_index, UUID |
| 流派 / 专辑艺术家 / 本地列表 | `0x4C07` | 有序 PID blob, start_index, UUID |
| 继续当前选择 | `0x4C06` | library UUID |
| 播放/暂停/停止/上一首/下一首 | `0x6802` | HID component + 2 字节 report |
| 随机/循环/音量±/静音 | `0x6802` | HID report bits |

HID report descriptor（12 bit，2 字节 report）：

```text
05 0C 09 01 A1 01 15 00 25 01 75 01 95 0C
09 B0 09 B1 09 B5 09 B6 09 B9 09 BC 09 E9 09 EA
09 E2 09 B7 09 B3 09 B4
81 02 C0
```

bit 顺序：play, pause, next, previous, shuffle(Random Play), repeat,
volume up, volume down, mute, stop, fast-forward, rewind。
`sendHidCommand(bit)`：在 2 字节 report 的对应位置 1，发送；再发送全 0 report。
FF/RW 短按在真机上表现为 next/previous；需要长按语义时发送 press、延时、release。

### 8.3 封面 FT

- 设备在 Now Playing 变化后主动发 FT Setup（file type 2）。
- 回 `[ft_id, 0x01]` Start；收 `0x80/0x00/0x40/0xC0` 数据；完整后回 `[ft_id, 0x05]` Success。
- 数据按 FT datagram 拼接：`data[0]=ft_id, data[1]=cmd, data[2:]=file bytes`。
- 大文件跨多个 link 帧，每帧 65535 字节；按 length 读即可。
- 建议收到完整 JPEG 后校验 `FF D8 ... FF D9`，写入 `artwork-cache/<id>.jpg` LRU
  （最多 128 张，临时文件 + rename）。

## 9. 播放列表

### 9.1 设备播放列表（只读）

- `0x4C03` 的 MediaPlaylistProperties **合法 ID 是 0–5**；ID 5 是
  `MediaPlaylistContainedMediaItemsFileTransferIdentifier`。
- 设备发 `0x4C04 param3` 的 MediaPlaylist 组后，会主动发 FT Setup（file type **0x0004**）。
- 文件内容是**扁平的大端 u64 PID 数组**（336 字节 = 42 首）。
- 解析后存 SQLite/JSON；播放用 `0x4C08 CollectionType=0 (Playlist)`：
  `[collection PID][type=0][start u32][library UUID]`。
- 当前真机 `PlaylistSync` 42 首验证成功。设备端只读，不能增删改。
- `0x4C09 PlayMediaLibrarySpecial`（R29 page 943，Table 97-125）参数为
  `[MediaLibraryUniqueIdentifier utf8][AllSongs none][StartingPID uint64 0/1]`；
  它是“播放全部歌曲”，**不是** Recently Added。未做 UI 入口。

### 9.2 本地播放列表

- iAP2 媒体库只读，设备端不能创建/删除播放列表；本地播放列表由配件维护。
- SQLite 表见 §7.1；也可以先用 JSON 文件（与 Python 版一致）。
- 播放：把有序 PID 列表打包成 blob，发 `0x4C07`。
- 限制：单条 CSM 消息最大 65535 字节；8 字节 PID + 4 字节 param 头 → 最多约 5,400 首。
  实际建议限制 2,000 首。
- 播放前批量查询缺失 PID，跳过并在 UI 显示 `missing` 数量。
- 播放历史：`history.json` / SQLite `history` 表，记录最近 500 次曲目变化；
  生成“最近播放”“最常播放”智能列表。
- `first_seen`：iAP2 媒体库没有添加日期；可选的独立导入器 `media_db_import.py`
  可通过 AFC/ifuse 读取 `MediaLibrary.sqlitedb` 的 `item.date_added`，生成
  `media-db-dates.json`；iAP2 程序只读该结果，不链接 libimobiledevice。

## 10. HTTP / JSON / UI

- 推荐 **cpp-httplib**（header-only，依赖少）或 CivetWeb（更成熟）。
- JSON 用 **nlohmann/json**（方便）或 RapidJSON（省内存）。
- 静态页面放只读 flash，`/` 返回 `index.html`；API 返回 `application/json`。
- 端点：
  - `GET /api/state`：Now Playing + 缓存状态快照
  - `GET /api/artwork`：最新封面（可加磁盘缓存）
  - `GET /api/library`：曲目数/专辑数/revision + 专辑列表
  - `GET /api/library/album?pid=`
  - `GET /api/library/artists?q=&limit=&offset=`
  - `GET /api/library/artist?pid=`
  - `GET /api/library/songs?q=&limit=&offset=`
  - `POST /api/play`：`item_id` / `album_pid` / `artist_pid` / `item_ids`
  - `GET/POST/DELETE /api/playlists...`
  - `POST /api/command`：HID 命令
  - `POST /api/library/sync`：重新全量
- 快照设计：
  ```cpp
  struct StateSnapshot {
    NowPlaying now_playing;
    bool connected;
    std::string revision;
    size_t track_count, album_count;
    std::vector<AlbumRow> albums;  // 或分页
    std::vector<uint8_t> artwork;
  };
  using SnapshotPtr = std::shared_ptr<const StateSnapshot>;
  ```
  HTTP 线程拿到 `SnapshotPtr` 后序列化，不持有锁。
- 无鉴权，仅建议受信 LAN；如果要暴露公网，加 token 或反向代理认证。

## 11. 内存与性能

| 项 | 预算 |
|---|---|
| SQLite 页缓存 | 8–16 MB |
| 媒体库条目（20k） | 6–10 MB（取决于字符串） |
| 专辑/艺术家/歌曲索引 | 10–20 MB |
| 封面 LRU | 32–64 MB |
| 链路层缓冲 | 2×64 KB |
| HTTP/JSON 临时 | 4–16 MB |

优化点：

- 歌曲搜索不要每次全表扫描；SQLite `LIKE` + 索引，或维护 FTS5。
- 专辑/艺术家列表分页返回；不要一次 JSON 序列化 2,000 张专辑 + 5,000 位艺术家。
- 封面解码/缩放用 libjpeg-turbo；缩略图预生成并缓存。
- 避免 `std::string` 大量拷贝；用 `std::string_view` 解析，存储时再拷贝。
- 结构体按 8 字节对齐；PID 用 `uint64_t`，不要用字符串。

## 12. 构建与依赖

```cmake
find_package(PkgConfig REQUIRED)
pkg_check_modules(LIBUSB REQUIRED libusb-1.0)
pkg_check_modules(HIDAPI REQUIRED hidapi-libusb)
pkg_check_modules(MBEDTLS REQUIRED mbedtls)
pkg_check_modules(SQLITE3 REQUIRED sqlite3)
# asio header-only；httplib/nlohmann 可直接 vendored
```

- C++20（`std::span`、`std::jthread`、`std::chrono`）。
- 交叉编译：`aarch64-linux-gnu-g++` 或 Yocto/Buildroot recipe。
- 静态链接 libusb/hidapi/mbedTLS/SQLite 以减少 rootfs 依赖。

## 13. 测试计划

1. **单元测试**
   - checksum、CSM 编解码、大小端。
   - HID report reassembly：用真实 `artifacts/logs/artwork-raw-01.log` 的 URB 序列做 golden test，断言输出
     186,918 字节 JPEG 且 SHA-256 匹配。
   - 媒体库增量合并：新增/删除/reset/重复消息。
   - 播放列表 CRUD 和缺失 PID。
2. **回放测试**
   - 用抓包日志回放 `FF5A` 字节流，断言 link 状态机不丢帧。
   - Fuzz：随机插入/删除字节，确保不崩溃、能重新同步。
3. **真机集成**
   - 认证 → 识别 → Now Playing → 真实队列 FT 0x0003/0x0006 → 封面 → 全量媒体库
     → 增量 → 播放单曲/专辑/艺术家/列表。
   - 断线重连 10 次，检查 revision 增量、播放列表持久化、封面缓存。
4. **资源测试**
   - 20k 条媒体库导入时间、RSS、SQLite 大小。
   - 封面 LRU 达到上限后的淘汰。
   - 24 小时循环播放 + 随机 HID 命令。

## 14. 移植检查清单

- [ ] USB 枚举 configuration 2，claim HID interface 2，EP 0x83。
- [ ] 读取 HID report descriptor（208 bytes），确认 report ID → size。
- [ ] 实现 HID 报告重组（剥 2 字节头，处理 1–2 字节尾部省略）。
- [ ] 实现 FF5A 链路帧解析、校验和、ACK/EAK/重传。
- [ ] 实现 MFi 认证（证书/私钥安全存储）。
- [ ] 识别参数包含 HID component 0x0012。
- [ ] Now Playing 订阅 0x5000，解析 0x5001（含队列/随机/循环）。
- [ ] `0x4C00/4C01/4C03/4C04` 全量 + 增量媒体库。
- [ ] `0x4C07/4C08` 单曲/专辑/艺术家/流派/专辑艺术家/本地列表播放。
- [ ] `0x4C06` 恢复设备当前选择。
- [ ] `0x6802` HID 控制（play/pause/stop/next/prev/shuffle/repeat/volume/mute）。
- [ ] 封面 FT + JPEG 校验 + `artwork-cache` LRU。
- [ ] SQLite/播放列表/播放历史/`first_seen` 持久化 + 原子写。
- [ ] HTTP API + 静态 UI + 分页/搜索/排序/Up Next/智能列表。
- [ ] 断线重连 + revision 增量。
- [ ] 资源/掉电/长稳测试。

## 15. 风险与限制

- **MFi 认证**是最大风险：证书/私钥必须合法且安全存储；不同 iOS 版本可能收紧。
- **设备端长度/报告格式**可能随固件变化；保留 `HID_LOG_RAW` 和抓包回放测试。
- **iAP2 媒体库只读**：设备端播放列表不能改；本地播放列表是配件侧方案。
- **音量**没有绝对读数：iAP2 无音量消息，只能 HID Volume Increment/Decrement；
  静音用 HID Mute，绝对音量未验证。
- **进度条/seek**：Now Playing 只给 elapsed，没有 seek 命令；HID FF/RW 已加入 descriptor，
  但短按在真机上表现为 next/previous，长按 seek 未验证。
- **添加日期**：R29 的 MediaItem/MediaItemProperties 没有 date-added 字段；但外部
  AFC/ifuse 导入器可读取 `MediaLibrary.sqlitedb.item.date_added`，已匹配 20,564 个 PID。
  Web UI 使用真实日期，`first_seen` 仅兜底。0x4C09 仍是 All Songs，不是 Recently Added；
  蓝牙原生 Recently Added 仍需 AVRCP Browsing。
- **设备真实队列内容**：已打通。`0x5000` 的 PlaybackAttributes ID 17 +
  PlaybackQueueListContentTransferInfoRequest 组触发；设备用 FT **0x0003**（PID 数组）
  或 **0x0006**（MediaItemList）发送。真机验证 52/42 首。0x0004 仍用于设备播放列表。
  蓝牙上的 Recently Added 仍可能是 AVRCP browsing 的虚拟目录。
- **多设备/多会话**：当前是单台 iOS device、单 iAP2 连接；多设备需要会话管理器和 USB 热插拔。
- **性能**：20k 条 JSON 在低端 ARM 上会慢；必须用 SQLite/分页，不要照搬 Python 的
  “一次返回全部专辑/歌曲”。

## 16. 粗略工作量

| 模块 | 人日（有经验） |
|---|---|
| HID transport + 报告重组 | 2–3 |
| 链路层 + CSM | 4–6 |
| MFi 认证 | 3–5（取决于证书/安全元件） |
| 媒体库 SQLite + 同步 | 5–8 |
| Now Playing + 封面 | 2–3 |
| 播放控制 + 播放列表 + 历史/Up Next | 5–7 |
| HTTP/JSON/UI | 4–6 |
| 测试/长稳/移植 | 5–10 |
| **合计** | **30–48 人日** |

如果只做“车载式无屏配件 + 简单 HTTP 控制”，可以砍掉 SQLite 和复杂 UI，压到 15–20 人日。
