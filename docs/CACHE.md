# MediaCtrl 缓存机制

本文描述当前 Python 实现里的所有缓存/持久化状态、失效规则，以及移植到嵌入式设备时的建议。
最后更新：2026-11-04。

## 1. 总览

| 数据 | 位置 | 是否持久化 | 失效/更新触发 |
|---|---|---|---|
| Now Playing（标题/专辑/艺术家/进度/队列/随机/循环） | `STATE["now_playing"]` | 否 | 设备 `0x5001` 推送 |
| 媒体库信息（MPB / IRTL 两个库的 UUID） | `STATE["libraries"]` | 否 | 设备 `0x4C01` 推送 |
| 最新封面 JPEG（内存） | `STATE["artwork_bytes"]` | 否 | 设备 FT artwork 完成；磁盘副本见下一行 |
| 媒体库镜像（约 20.5k 首） | `data/<library_id>/iap2.db` | **是** | 全量同步 / `0x4C04` 增量 |
| 条目首次发现顺序 | `iap2.db` (`items.first_seen_seq`) | **是** | 新发现批次递增，与 RTC 无关 |
| 真实添加时间 | `data/<library_id>/media-cache.db` (`dates`) | **是** | 独立 `media_db_import.py` 读取 `MediaLibrary.sqlitedb` |
| 封面映射 | `data/<library_id>/media-cache.db` (`artwork_*`) | **是** | 独立 `media_db_artwork_import.py` 读取 Artwork Originals |
| 本地封面 | `data/<library_id>/media-artwork/` | **是** | 原图 + 160px 缩略图 |
| SQLite 快照 | `data/<library_id>/snapshot/` | **是** | 手动/独立导入工具生成；含隐私元数据 |
| 专辑索引 | `LIBRARY["_summary"]` | 否 | 媒体库 `version` 变化 |
| 艺术家索引（5,045 位） | `LIBRARY["_artists"]` | 否 | 媒体库 `version` 变化 |
| 歌曲排序索引（约 20.5k 首） | `LIBRARY["_songs"]` | 否 | 媒体库 `version` 变化 |
| 本地播放列表 | `data/<library_id>/localPlaylist.csv` | **是** | 添加/删除歌曲 |
| 播放历史日志 | `data/<library_id>/history.log` | **是** | 曲目变化追加 JSON 行 |
| 封面磁盘缓存（最多 128 张） | `data/<library_id>/artwork-cache/<id>.jpg` | **是** | FT 完成 / LRU |
| Up Next 队列 | `QUEUE`（内存） | 否 | 设备 FT 0x0003/0x0006 或网页发起播放 |
| HID report size 映射 | `HIDReader._report_sizes` | 否（启动时计算） | 代码/固件变化时人工确认 |

进程内缓存只在**一个进程**内使用：iAP2 会话线程负责写，HTTP 线程负责读。跨线程用
`STATE_LOCK`、`LIBRARY_LOCK`、`PLAYLIST_LOCK` 三个互斥锁保护。所有持久化数据按
`library_id`（AFC `_UUID` / iAP2 `0x4C01` UUID 前缀）分目录；旧的全局 JSON
文件只用于首次迁移。

### 1.1 独立媒体库 DB 导入结果

- `media_db_import.py` 是独立进程，通过 afcclient/ifuse 读取
  `/var/mobile/Media/iTunes_Control/iTunes/MediaLibrary.sqlitedb`。
- 输出 `data/<library_id>/media-cache.db` 的 `dates` 表：
  `pid → unix_timestamp`，来源为 `item.date_added`（约 20.5k 个 PID 匹配当前镜像）。
- `web_app.py` 只读该 SQLite 缓存，不 import libimobiledevice/ifuse。
- `library_recent()` 和 `library_songs(sort=recent)` 优先使用
  `first_seen_seq`，再用 `date_added`/`first_seen`；RTC 不参与发现顺序。
- 数据库是 WAL 模式；afcclient 直接拉取主库和 WAL，本地 checkpoint/校验后再
  生成快照。经 ifuse/FUSE 的大文件读取可能被页缓存撕裂。
- 封面文件在 `iTunes/Artwork/Originals/`，不在数据库文件旁；映射为
  `best_artwork_token → artwork.relative_path`。原始封面 2,187 张（1.34GB），
  缩略图 2,179 张；首次导入约 113.6 秒。
- `media_jobs.py` 负责停止 iAP2、切 configuration、重启 usbmuxd、运行导入。
  默认停留在 MediaLibrary 模式；只有勾选自动切换（`settings.json` 的
  `auto_switch_iap2`）时才恢复 configuration 2 并重启 iAP2。

### 1.2 手动加载与清理

- Web 启动时**不加载** `dates`/`artwork` 到内存；普通库请求也不会自动加载。
- `Load MediaLibrary` / `Load Artwork` 只读当前设备的 `media-cache.db`。
- `Update MediaLibrary` / `Update Artwork` 会从设备更新，并加载更新后的结果。
- `Clear AFC Cache` 删除当前设备的 `media-cache.db`、`media-artwork/`、
  `snapshot/`，并卸载内存中的日期/封面；`iap2.db` 和 `artwork-cache/` 保留。
- `Clear iAP2 Library` 清空当前设备的 `iap2.db`；协议会自动做一次全量 resync，
  不提供“清空后暂停/自动 resync”开关。
- `Clear iAP2 Artwork` 清空 `artwork-cache/` 和 Now Playing 内存图。
- `settings.json` 持久化 UI 选项，默认 `auto_switch_iap2=false`。

## 2. 持久化缓存

### 2.1 `iap2.db`（核心缓存）

这是设备媒体库的**完整镜像**，用于：

- 重连时带 `LastKnownMediaLibraryRevision` 做增量同步，避免每次全量拉约 20.5k 条；
- Web UI 的专辑/艺术家/歌曲/搜索视图；
- 本地播放列表解析曲目元数据。

SQLite 表：

```sql
meta(key, value)                         -- library_id, revision, next_seen_seq
items(pid, ..., first_seen, first_seen_seq, data_json)
playlists(pid, name, parent_pid, data_json)
seen(pid, first_seen_seq)
```

- 每条 item 以 **MediaItemPersistentIdentifier**（16 位十六进制字符串）为 key。
- `first_seen_seq` 是**首次发现顺序**，按同步批次递增，不依赖 RTC：
  旧数据迁移为 1，新发现批次为 2、3、……。一次完整同步内的条目共享同一序号；
  同步空闲超时后才结束该批次。
- `first_seen` 时间戳保留用于显示/调试，但 `library_recent` 先按
  `first_seen_seq` 降序，再按 `date_added`/`first_seen` 排序。
- `playlists` 存设备下发的 `MediaPlaylist` 组；内容来自 FT type `0x0004`。
- 每次保存使用一个事务，全量替换 items/playlists/seen；WAL 模式。
- 周期性保存：每 10 秒检查一次，只有 `dirty=True` 且距离最后一次变更 >5 秒才写盘；
  会话结束时也会保存。
- `revision` 变化（即使没有条目变化）也会置 `dirty`，保证下次重连从最新 revision 增量同步。
- 设备切换时按 `library_id` 加载对应 `iap2.db`，旧设备的镜像不会串到新设备。

同步流程与缓存状态：

```text
首次/清空后：
  4C03(revision="0", item-props, playlist-props)
  → 4C04 param6 reset：清空内存镜像
  → 大量 4C04 param4/param5：逐条删除（通常本地此时已为空）
  → 最后一条 param7=0x5A
  → 重新发 4C03（带最新 revision）
  → 4C04 param2：MediaItem 组，逐条写入 items
  → 保存 iap2.db

之后重连：
  4C03(revision=<已存值>)
  → 设备只发增量：param2 新增/修改、param4/5 删除、param6 reset（少见）
  → 合并后保存
```

失效规则：

- 收到 `param6 reset`：清空 `items` / `playlists`，`version += 1`，所有派生索引失效。
- 收到 `param4/param5` 删除：从 `items`/`playlists` 删除；不存在时静默忽略（规范要求）。
- 收到 `param2/param3`：按 ID 覆盖/插入；`version += 1`。
- `version` 每变一次，`_summary` / `_artists` / `_songs` 下一次请求时重建。
- 只有 `revision` 变化、条目未变：只更新 `revision` 并持久化，**不**重建索引。

### 2.1.1 设备播放列表内容（FT 0x0004）

- `0x4C03` 的 MediaPlaylistProperties 合法 ID 是 0–5；ID 5 是
  `MediaPlaylistContainedMediaItemsFileTransferIdentifier`。
- 设备发 `0x4C04 param3` 的 MediaPlaylist 组后，会主动发 FT Setup（file type 0x0004）。
- 文件内容是**扁平的大端 u64 PID 数组**（例如 336 字节 = 42 首）。
- `apply_playlist_ft()` 每 8 字节解析一个 PID，写入 `LIBRARY["playlists"][id]["items"]`，
  随 `iap2.db` 持久化。
- 播放用 `0x4C08 CollectionType=0 (Playlist)`；设备端只读。

### 2.2 `localPlaylist.csv`（唯一本地播放列表）

iAP2 的媒体库是只读的；本地只保留一个名为 `localPlaylist` 的配件侧播放列表。

- 位置：`data/<library_id>/localPlaylist.csv`
- 用户只能添加/删除歌曲，不能创建、改名或删除播放列表。
- CSV 记录完整轨道信息：PID、标题、专辑、艺术家、专辑艺术家、专辑/艺术家 PID、
  流派、作曲、rating、media_type、时长、track/disc、first_seen/seq、date_added 等。
- 每台设备最多 2,000 首。
- 播放时取有序 PID 列表，发 `0x4C07 PlayMediaLibraryItems`。

### 2.3 `history.log`（播放历史日志）

- 位置：`data/<library_id>/history.log`
- 每次曲目变化追加一行 JSON，包含 PID、标题、专辑、艺术家、时间戳和播放状态。
- 不再维护结构化 history JSON 或智能列表；日志仅用于调试。

### 2.4 `artwork-cache/`（封面磁盘缓存）

- FT artwork 完成后写 `data/<library_id>/artwork-cache/<artwork_id>.jpg`（临时文件 + rename）。
- 最多保留 128 张，按 mtime 淘汰最旧。
- Now Playing 带 `artwork_id` 且缓存命中时，先显示缓存图；FT 完成后覆盖。
- iAP2 不提供媒体库条目的 artwork ID（property 20 实测未下发），因此无法通过 iAP2
  按条目请求封面；专辑/曲目缩略图由独立 AFC 导入器生成（`media-cache.db`
  的 `artwork_*` 表、`media-artwork/thumbs/`），Now Playing 封面仍通过 FT 缓存。

## 3. 内存缓存

### 3.1 Now Playing / 封面

`STATE["now_playing"]` 保存设备最近一次 `0x5001` 的字段：

- 媒体项：title / album / artist / duration_ms / artwork_id
- 播放属性：status / elapsed_ms / queue_index / queue_count / queue_chapter /
  shuffle / repeat / app / queue_available / queue_transfer_id / queue_transfer
- `updated_at` 用于前端本地插值进度条（设备不会每秒推送 elapsed）。

`STATE["artwork_bytes"]` 保存最新封面 JPEG（当前 171 KB / 600×600）。HTTP `/api/artwork`
直接读内存；ETag 用 `artwork_ready_id` 作为查询参数 `?vid=N`，避免浏览器把旧图当新图。
内存中的最新封面不持久化，但 FT 完成时会同时写入 `data/<library_id>/artwork-cache/<artwork_id>.jpg`；
收到同一 `artwork_id` 时可从磁盘缓存加载。若设备没有上报 `artwork_id`，无法选择缓存图。
嵌入式建议见 §5。

### 3.2 媒体库派生索引

`LIBRARY` 里除 `items` 外还有：

| 字段 | 说明 |
|---|---|
| `version` | 条目变化计数；每次增删/重置 +1 |
| `_summary` + `_summary_version` | 专辑列表（按 `album_pid` 聚合） |
| `_artists` + `_artists_version` | 艺术家列表（按 `artist_pid` 聚合） |
| `_songs` + `_songs_version` | 按标题排序的歌曲数组，带小写搜索串 |

- 专辑按 `album_pid` 聚合：同一专辑的不同标题/缺失标题会合并，取出现次数最多的标题和艺术家。
- 艺术家优先按 `artist_pid` 聚合；没有 PID 时按艺术家名聚合。
- 歌曲索引缓存了 `_search = (title + artist + album).lower()`，搜索时线性过滤；
  约 20.5k 条在 Python 里约几十毫秒。
- 所有派生索引都以 `version` 为 key；版本不匹配时重建，匹配时直接返回。
- 索引只在**媒体库条目变化**时失效；`revision` 单独变化不重建。

### 3.3 HID 报告映射

`HIDReader._report_sizes` 由 `INPUT_REPORTS` 计算：

```text
total = INPUT_REPORTS[report_id] + 1
if total > 64 and total % 64 <= 2:
    total = (total // 64) * 64   # 设备会省掉 1-2 字节尾部 padding
```

这是静态配置，不需要持久化。换 iOS device 或固件时如果报告长度变化，先看 `HID_LOG_RAW=1`
的原始报告头和 `artifacts/probes/hid_probe.py` 读到的 report descriptor
（该 probe 是本地开发资料，不随仓库分发）。

### 3.4 Up Next / 播放历史

- `QUEUE["items"]`：队列条目，可能是：
  - **设备真实队列**：`0x5001` 的 PlaybackAttributes 14/15/17 触发 FT **0x0003**
    （扁平 u64 PID 数组）或 **0x0006**（MediaItemList）；`source="device"`。
  - **网页发起的本地队列**：单曲/专辑/艺术家/流派/专辑艺术家/本地列表；`source="local"`。
  - 只在内存；重启后由设备重新推送。
- `QUEUE["base"]`：队列窗口起始索引；`QUEUE["meta"]`：不在媒体库里的队列条目的元数据。
- `queue_snapshot()` 用 Now Playing 的 `queue_index - base` 定位当前项，返回当前 +
  接下来最多 20 首，并带 `source/base_index/total`。

设备队列请求参数（R29 page 943-946）：

```text
0x5000 param 1 PlaybackAttributes:
  IDs 0..16 + ID 17 (PlaybackQueueListContentTransferSize, uint32, 0=full)
0x5000 param 2 PlaybackQueueListContentTransferInfoRequest:
  IDs 0,1,6,12,14,16,18
```
- `history.log`：每次曲目变化追加一行 JSON。`record_now_playing()` 在
  `0x5001` 到达时把标题/艺术家与队列条目匹配，匹配成功就记录；同一曲目 30 秒内不重复。
- 智能列表：
  - `history_recent(limit)`：按最近出现去重；
  - `history_top(limit)`：按出现次数排序，附 `play_count`。

## 4. 一致性与并发

- **写者唯一**：只有 iAP2 会话线程写 `LIBRARY` / `STATE`；HTTP 线程只读。
- **读快照**：`snapshot()` 在 `STATE_LOCK` 下复制 Now Playing；`library_*()` 在
  `LIBRARY_LOCK` 下复制条目列表或索引引用。
- **重复更新**：设备可能重复发送同一 MediaItem；按 ID 覆盖即可，不影响正确性。
- **删除不存在项**：规范要求忽略，不视为错误。
- **全量同步中途失败**：内存镜像已清空，但 `iap2.db` 只有在 `dirty` 时才写；
  若进程崩溃，旧镜像仍然在磁盘上（除非已经保存过空镜像）。建议在“重新同步”前用
  SQLite Backup API 备份当前设备的 `iap2.db`。
- **重新同步**：清空内存镜像、`revision=None`、断开 iAP2 连接；重连后按首次全量流程拉取。

## 5. 封面缓存（当前与建议）

### 已实现

- 内存保存最新一张封面；FT 完成后同时原子写入 `data/<library_id>/artwork-cache/<artwork_id>.jpg`。
- 最多 128 张，按 mtime 淘汰最旧；启动后命中 `artwork_id` 时先显示缓存图。
- 设备每次 Now Playing 变化会主动发 FT Setup；配件回 Start，收完后回 Success 并覆盖缓存。
- `/api/artwork` 带 `Cache-Control: no-store`，前端用 `?vid=<artwork_id>` 区分版本。

### 仍然限制

- iAP2 媒体库条目没有可用的 artwork ID（请求的 MediaItem property 20 实测未下发），
  因此无法通过 iAP2 按条目请求封面；逐专辑/逐曲缩略图改由 AFC 导入器提供。
- 重启后如果设备没有在 0x5001 里带 `artwork_id`，无法知道该用哪张 Now Playing
  缓存图；当前只按 ID 命中，不做“按标题猜测”。

### 建议（嵌入式/长期运行）

1. 在 `artwork-cache/index.json` 里记录 `{artwork_id, title, artist, album}`，
   重启/重连时可按 Now Playing 元数据匹配缓存。
2. 内存里维护 `artwork_id -> path` 的 LRU 索引，避免每次查目录。
3. 如果后续拿到媒体库 artwork ID，按专辑缓存 200×200 缩略图，而不是每首歌 600×600。

## 6. HTTP / 浏览器缓存

- 所有 API 响应带 `Cache-Control: no-store`，避免代理/浏览器缓存过期状态。
- 页面每 1 秒轮询 `/api/state`，每 10 秒轮询 `/api/library`。
- 封面 URL 带 `vid`，图片内容变化时 URL 变化，浏览器不会复用旧图。
- 歌曲/艺术家等视图按需请求，不在 10 秒轮询里重复拉取。

## 7. 故障恢复清单

| 故障 | 行为 | 恢复 |
|---|---|---|
| `iap2.db` 不存在 | 启动为空镜像 | 首次连接全量同步 |
| JSON 损坏 | 打印错误，保留空镜像 | 删除文件或点“重新同步” |
| 全量同步中断 | 内存可能不完整 | 重新同步；必要时用备份 |
| 播放列表引用已删除曲目 | 读取时跳过并计数 | 删除该曲目或重新添加 |
| `history.log` 损坏 | 打印/忽略错误 | 删除日志文件，重新累积 |
| 封面缓存文件损坏 | 校验 JPEG SOI 失败则忽略 | 删除对应文件，等下一次 FT |
| 重启后 Up Next 为空 | 队列只在内存 | 重新从网页发起播放；历史仍保留 |
| 封面 FT 中断 | 不覆盖旧封面 | 等下一次 Now Playing/FT |
| iAP2 断开 | 会话线程 2 秒重连 | 自动带 revision 增量同步 |
| HID reader 读错误 | `feed_eof()` 唤醒、线程重启 | 自动重连 |

## 8. 嵌入式移植建议

1. **媒体库用 SQLite**，不要用 JSON DOM：
   ```sql
   CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
   CREATE TABLE items(
     pid INTEGER PRIMARY KEY, title TEXT, album_pid INTEGER, album TEXT,
     artist_pid INTEGER, artist TEXT, album_artist TEXT, genre TEXT,
     composer TEXT, duration_ms INTEGER, track INTEGER, disc INTEGER,
     media_type INTEGER, rating INTEGER, date_added REAL);
   CREATE INDEX idx_items_album ON items(album_pid);
   CREATE INDEX idx_items_artist ON items(artist_pid);
   CREATE INDEX idx_items_title ON items(title COLLATE NOCASE);
   CREATE TABLE playlists(id TEXT PRIMARY KEY, name TEXT, created INTEGER, updated INTEGER);
   CREATE TABLE playlist_items(
     playlist_id TEXT, position INTEGER, item_pid INTEGER,
     PRIMARY KEY(playlist_id, position));
   CREATE TABLE first_seen(pid INTEGER PRIMARY KEY, ts REAL); -- 0 = legacy
   CREATE TABLE history(
     seq INTEGER PRIMARY KEY AUTOINCREMENT, pid INTEGER, ts REAL);
   ```
   每批 `0x4C04` 用一个事务提交；`PRAGMA journal_mode=WAL`、`synchronous=NORMAL`。
   `date_added` 由独立导入器 `media_db_import.py` 的结果填充；没有结果时留空，
   `first_seen` 仅作回退。
2. **派生索引**（专辑/艺术家/歌曲）用 SQL 视图或启动时构建的数组，不必持久化。
3. **封面 LRU** 放独立分区，文件名用 artwork ID；写入用 `tmp + rename`，限制总大小。
4. **revision 单独存 meta 表**；全量同步前先备份数据库文件（SQLite backup API）。
5. **内存预算**：20k 条 × 约 300 字节 ≈ 6 MB；SQLite 页缓存 2-8 MB；封面 64 MB 上限。
6. **闪存寿命**：不要每条 MediaItem 都 fsync；按批事务 + 周期保存（例如 10 秒）。
7. **掉电安全**：WAL + 原子 rename；启动时校验 `meta.revision` 与数据库条目数。
8. **播放列表**：只存 item PID；播放前批量查询缺失项；超过 2,000 首时分段或拒绝。
