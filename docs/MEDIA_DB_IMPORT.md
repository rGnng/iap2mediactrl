# Media DB Import (separate from iAP2)

This is an **out-of-band** importer. It does not touch the iAP2/HID session
code and `web_app.py` does not import `libimobiledevice`.

```text
iOS device MediaLibrary.sqlitedb
        │  AFC / ifuse
        ▼
media_db_import.py  ──►  media-db-dates.json
                                  │
                                  ▼
                          web_app.py (read-only result consumer)
```

## What was found

- Device: iPod touch, iOS 15.7.8
- DB: `/var/mobile/Media/iTunes_Control/iTunes/MediaLibrary.sqlitedb`
- DB is WAL mode; a plain file copy can be inconsistent. The importer takes a
  consistent SQLite backup (including WAL pages).
- Table/column: `item.item_pid` + `item.date_added`
- `date_added` is Apple/Core Data epoch: add `978307200` for Unix time.
- Current snapshot:
  - 20,688 DB rows
  - 20,564 unique PIDs matched to `media-library.json`
  - date range: 2024-03-21 .. 2026-10-22
- Result file: `media-db-dates.json` (PID → Unix timestamp)
- DB snapshot: `artifacts/media-db-snapshot/media-db-backup.sqlitedb` (inspection only)

The web UI loads `media-db-dates.json` and uses real `date_added` for
**Recently Added**; `first_seen` remains the fallback when the result file is
missing.

## Usage

The iAP2 service uses USB configuration 2 (HID). AFC uses the Apple Mobile
Device interface (configuration 3 or 4), so the two paths must not run at the
same time.

1. Stop the web service.
2. Switch the iOS device to configuration 3 or 4.
3. Start usbmuxd as root (the packaged systemd service drops to `usbmux` and may
   not have permission to the USB device node).
4. Pair/trust the device.
5. Run the importer with `--mount`.
6. Restore configuration 2 and restart the web service.

Example once the device is in AFC mode:

```bash
sudo python3 media_db_import.py --mount
# => media-db-dates.json
```

Pairing:

```bash
idevicepair pair
idevicepair validate
```

If `ifuse` is not installed:

```bash
sudo apt install libimobiledevice-utils ifuse usbmuxd
```

## Result schema

```json
{
  "ok": true,
  "source": {
    "database": "artifacts/media-db-snapshot/media-db-backup.sqlitedb",
    "table": "item",
    "pid_column": "item_pid",
    "date_column": "date_added",
    "formats": {"apple_epoch": 20688}
  },
  "stats": {
    "rows": 20688,
    "unique_pids": 20688,
    "library_items": 20564,
    "matched_library_items": 20564
  },
  "dates": {"<16-hex-item-pid>": 1792630831.0}
}
```

## Safety / limitations

- Read-only: the importer only copies/reads the database; it does not write to
  the iOS device.
- `date_added` is the device database's date. It may reflect a bulk import/sync
  time and may not distinguish cloud/Apple Music Radio items.
- Keep the DB snapshot private: it contains library metadata.
- This path is intentionally separate from iAP2. If AFC access is blocked or
  the schema changes, iAP2/HID continues to work unchanged.

## Artwork import (second phase)

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

Run while the device is in AFC mode:

```bash
sudo python3 media_db_artwork_import.py --mount
```

Outputs:

- `media-artwork/<sha1>.jpg` — original artwork
- `media-artwork/thumbs/<sha1>.jpg` — 160px thumbnails for the Browser table
- `media-db-artwork.json` — item/album PID → file mapping

Measured full first pull on this device (USB AFC + SQLite backup already present):

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
originals are truncated on the device, so 8 thumbnails could not be generated;
the API falls back to the original for those.

Web endpoint:

```text
GET /api/library/artwork?pid=<item_pid>&size=thumb|full
GET /api/library/artwork?album_pid=<album_pid>&size=thumb|full
```

The Browser album **grid** uses thumbnails; album detail uses full artwork.
The album grid has a sort menu (Recently Added / Name / Artist, plus
Ascending / Descending), defaulting to Recently Added descending.

## Future wrapper

A wrapper can automate USB configuration switching and service stop/start, but
the actual data path should remain:

```text
ifuse/AFC → SQLite result → web_app
```

not a shared in-process dependency.
