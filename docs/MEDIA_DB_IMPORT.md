# Media DB Import (separate from iAP2)

This is an **out-of-band** importer. The iAP2 service never links
libimobiledevice; the two paths exchange data through `media-cache.db` and the
artwork directory.

```text
iOS device MediaLibrary.sqlitedb / Artwork
        │  AFC (afcclient / ifuse)
        ▼
media_db_import.py         ──►  media-cache.db (dates)
media_db_artwork_import.py ──►  media-cache.db (artwork_*)
                              media-artwork/
                                  │
                                  ▼
                          web_app.py (read-only cache consumer)
```

`media_jobs.py` owns the USB mode transition and runs the importers as
subprocesses. The browser talks to it through `/api/media/action`.

## What was found

- Device: iPod touch, iOS 15.7.8
- DB: `/var/mobile/Media/iTunes_Control/iTunes/MediaLibrary.sqlitedb`
- DB is WAL mode. `afcclient` pulls the database and WAL directly over AFC;
  the pulled copy is checkpointed and validated locally before use. A FUSE
  copy through `ifuse` can be torn while the device is writing.
- Table/column: `item.item_pid` + `item.date_added`
- `date_added` is Apple/Core Data epoch: add `978307200` for Unix time.
- Current snapshot:
  - 20,688 DB rows
  - 20,555 unique PIDs matched to the iAP2 mirror in `iap2.db`
  - date range: 2024-03-21 .. 2026-10-22
- Local cache: `data/<library_id>/media-cache.db`
- DB snapshot: `data/<library_id>/snapshot/media-db-backup.sqlitedb`
  (inspection only)

The web UI reads the `dates` table for **Recently Added**; `first_seen_seq` is
the primary order, with `first_seen` as a fallback.

## SQLite cache

Everything is keyed by the media-library GUID shared by AFC
(`_MLDatabaseProperties._UUID`) and iAP2 (`0x4C01` UUID prefix):

```text
data/<library_id>/
  iap2.db                  iAP2 mirror, device playlists, first_seen_seq
  media-cache.db           AFC dates and artwork mappings
  media-artwork/           AFC artwork
  artwork-cache/           iAP2 Now Playing artwork
  snapshot/                MediaLibrary.sqlitedb snapshot
  localPlaylist.csv        single local playlist
  history.log              track-change log
```

`iap2.db` schema:

```sql
meta(key TEXT PRIMARY KEY, value TEXT)
-- library_id, revision, next_seen_seq

items(
  pid TEXT PRIMARY KEY,
  title TEXT, album TEXT, artist TEXT, album_artist TEXT,
  genre TEXT, composer TEXT, media_type INTEGER, rating INTEGER,
  duration_ms INTEGER, album_pid TEXT, artist_pid TEXT,
  album_artist_pid TEXT, track INTEGER, track_count INTEGER,
  disc INTEGER, disc_count INTEGER, compilation INTEGER,
  first_seen REAL, first_seen_seq INTEGER NOT NULL,
  data_json TEXT NOT NULL
) WITHOUT ROWID

playlists(pid TEXT PRIMARY KEY, name TEXT, parent_pid TEXT,
          data_json TEXT NOT NULL) WITHOUT ROWID

seen(pid TEXT PRIMARY KEY, first_seen_seq INTEGER NOT NULL) WITHOUT ROWID
```

`first_seen_seq` is monotonic and independent of RTC time. All legacy items
migrate as sequence 1; new discovery batches continue at 2, 3, ...

`media-cache.db` uses a small schema that is also suitable for the future Tab5
firmware:

```sql
meta(key TEXT PRIMARY KEY, value TEXT)

dates(
  pid TEXT PRIMARY KEY,
  date_added REAL NOT NULL
) WITHOUT ROWID

artwork_files(
  sha1 TEXT PRIMARY KEY,
  rel TEXT,
  file TEXT NOT NULL,
  thumb TEXT,
  mime TEXT NOT NULL,
  bytes INTEGER NOT NULL DEFAULT 0,
  thumb_bytes INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL
) WITHOUT ROWID

artwork_items(
  pid TEXT PRIMARY KEY,
  token TEXT,
  sha1 TEXT NOT NULL,
  fallback INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID

artwork_albums(
  pid TEXT PRIMARY KEY,
  token TEXT,
  sha1 TEXT NOT NULL,
  fallback INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID
```

The legacy JSON files (`media-db-dates.json`, `media-db-artwork.json`) are
imported into SQLite once when the cache is first created, then left as
historical files.

## Web workflow

Use the `MediaLibrary` button in the title bar. The panel is split into two
columns.

### Left: AFC

- `Pair / Trust` — validate pairing; if needed, run `idevicepair pair` and wait
  for the Trust dialog.
- `Load MediaLibrary` / `Update MediaLibrary` — load the existing `dates`
  table, or pull a fresh database snapshot and update it.
- `Load Artwork` / `Update Artwork` — load the existing artwork mapping, or
  refresh the mapping and copy missing/changed artwork.
- `Quick Sync` — update both AFC caches incrementally.
- `Full Refresh` — update the database and delete/re-download every cover.
- `Clear AFC Cache` — remove `media-cache.db`, `media-artwork/`, and
  `snapshot/` for the active device. The iAP2 database and artwork are kept.
- `Cancel` — stop the running AFC task.

Cache loading is manual. The web process does not load dates or artwork at
startup and does not reload them on ordinary library requests. Clicking an
`Update` action loads the updated result; clicking `Load` reads an existing
local cache without touching the device.

### Right: iAP2

- `Sync iAP2` — request an incremental `0x4C03` update with the current
  revision.
- `Full Resync` — clear the revision and reconnect for a full synchronization.
- `Clear iAP2 Library` — delete the active device's `iap2.db` and clear the
  in-memory mirror. The protocol immediately performs a full resync; no
  separate auto-resync option is exposed.
- `Clear iAP2 Artwork` — delete the active device's `artwork-cache/` and the
  in-memory Now Playing artwork.
- `Switch to iAP2` — manually switch the device back to configuration 2.
- `Auto switch to iAP2 after AFC tasks` — optional; off by default. The value
  is stored in `settings.json`.

AFC update tasks stay in MediaLibrary mode unless the auto-switch option is
enabled. While a task is running, mode changes are rejected with HTTP 409
until it is cancelled.

Mode transition (AFC update actions only):

1. Stop the iAP2 session.
2. Switch USB configuration to 3.
3. Restart usbmuxd (the device may settle in configuration 4).
4. Pair/validate, mount AFC, and pull the database.
5. Copy artwork.
6. If auto-switch is enabled, restore configuration 2 and restart iAP2.

`/api/state` exposes the active mode, `library_id`, pairing state, UDID, AFC
cache loaded counts, `settings.auto_switch_iap2`, and the running or last task
with numeric progress.

## Local playlist and history

- Device playlists are stored in the per-device `iap2.db` `playlists` table.
- The only local playlist is `localPlaylist`, stored in
  `data/<library_id>/localPlaylist.csv`. The CSV contains the full track
  metadata used for debugging. Users can add/remove tracks, but cannot create,
  rename, or delete the playlist.
- Playback history is not stored as structured JSON. Track changes are appended
  as JSON lines to `data/<library_id>/history.log`.

## Command-line usage

The tools are still available for diagnostics. The device must already be in
AFC mode and usbmuxd must see it.

```bash
sudo python3 media_db_import.py --mount --progress-json \
  --cache-db data/<library_id>/media-cache.db \
  --snapshot-dir data/<library_id>/snapshot
sudo python3 media_db_artwork_import.py --mount --mode quick --progress-json \
  --cache-db data/<library_id>/media-cache.db \
  --artwork-dir data/<library_id>/media-artwork \
  --snapshot-dir data/<library_id>/snapshot
```

Pairing:

```bash
idevicepair pair
idevicepair validate
```

If `afcclient` or `ifuse` is missing:

```bash
sudo apt install libimobiledevice-utils ifuse usbmuxd
```

## Progress protocol

Both importers accept `--progress-json` and print stable machine-readable lines:

```text
PROGRESS {"stage":"db_copy","done":512,"total":2048,"unit":"pages",...}
PROGRESS {"stage":"db_parse","done":20688,"total":20688,"unit":"rows",
          "stats":{"rows":20688,"matched_library_items":20555}}
PROGRESS {"stage":"artwork_scan","done":0,"total":2187,"unit":"files",...}
PROGRESS {"stage":"artwork_copy","done":1234,"total":2187,"unit":"files",
          "bytes_done":891234567,"bytes_total":1340441950,
          "stats":{"copied":1100,"failed":2,"thumbs":1100}}
PROGRESS {"stage":"done",...}
```

`media_jobs.py` parses these lines and merges the fields into the `media.job`
object returned by `/api/state`.

## Safety / limitations

- Read-only: the importers only copy/read the device database and artwork; they
  do not write to the iOS device.
- `date_added` is the device database's date. It may reflect a bulk import/sync
  time and may not distinguish cloud/Apple Music Radio items.
- Keep the DB snapshot and `media-cache.db` private: they contain library
  metadata.
- The iAP2 and AFC paths remain mutually exclusive. The web service serializes
  them; do not run a second importer while a web task is active.
- If AFC access is blocked or the schema changes, iAP2/HID continues to work
  unchanged.

## Artwork import

Artwork is not stored next to the DB; the files are under:

```text
/var/mobile/Media/iTunes_Control/iTunes/Artwork/Originals/<relative_path>
```

The mapping is in the DB:

```text
item.item_pid
  -> best_artwork_token.entity_pid
  -> best_artwork_token.available_artwork_token
  -> artwork.artwork_token
  -> artwork.relative_path
```

Outputs:

- `data/<library_id>/media-artwork/<sha1>.<ext>` — original artwork
- `data/<library_id>/media-artwork/thumbs/<sha1>.jpg` — 160px thumbnails
- `data/<library_id>/media-cache.db` `artwork_*` rows

Measured full first pull on this device (USB AFC + SQLite snapshot already
present):

| Metric | Value |
|---|---:|
| Unique artwork files | 2,187 |
| Downloaded originals | 1,337,726,237 bytes (1.34 GB) |
| Thumbnails | 2,179 |
| Thumbnail bytes | 19,857,512 |
| Mount | 0.06 s |
| DB mapping | 0.08 s |
| Copy + thumbnails | 113.32 s |
| Total importer time | **113.56 s** |
| Approx throughput | ~11.8 MB/s |

Mappings: 20,629 item PIDs (including album fallback), 2,090 albums. A few
originals are truncated on the device, so 8 thumbnails cannot be generated;
the API falls back to the original for those. `Quick Sync` reuses those
originals without downloading them again.

Web endpoint:

```text
GET /api/library/artwork?pid=<item_pid>&size=thumb|full
GET /api/library/artwork?album_pid=<album_pid>&size=thumb|full
```

The Browser album **grid** uses thumbnails; album detail uses full artwork.
The album grid has a sort menu (Recently Added / Name / Artist, plus
Ascending / Descending), defaulting to Recently Added descending.
