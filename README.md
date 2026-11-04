# iap2mediactrl

`iap2mediactrl` is a proof-of-concept (PoC) implementation of an iAP2
accessory that runs on Linux and controls media playback on an iOS device over
USB HID. It also mirrors the device media library and exposes a browser-based
control interface.

The terms "iOS device" and "iPod touch" are used deliberately: the former
refers to the general protocol target, and the latter refers to the specific
device used for testing.

The project is a protocol reference for a future port to an embedded platform
with USB host support, such as the ESP32-P4. It is not production firmware, it
is not MFi certified, and it is not intended to be deployed as-is.

## Implementation status

The reference device is an iPod touch (7th generation, model A2178) running
iOS 15.7.8 and enumerating as USB `05ac:12aa`. The USB layer also accepts an
iPhone (`05ac:12a8`); both expose the iAP2 HID interface in configuration 2.
The reference host is Linux on aarch64 with Python 3.14. The development media
library contained 20,564 tracks, 2,095 albums, and 5,045 artists.

| Capability | Status | Implementation |
|---|---|---|
| USB HID transport | Verified | `hid_async.py` selects USB configuration 2, uses HID interface 2 and interrupt endpoint `0x83`, reassembles input reports, and sends output reports with `SET_REPORT`. |
| MFi 3.0 authentication | Verified | `mfi_local.py` loads a P-7B certificate and a PKCS#8 P-256 private key, signs the 32-byte challenge, and returns a 64-byte raw ECDSA signature. |
| iAP2 link layer | Verified, experimental | `ref_iap2_link_layer.py` implements `FF 5A` framing, header and payload checksums, SYN/ACK negotiation, session multiplexing, and file-transfer session 12. |
| Identification | Verified | `auth_test.py` declares the iAP2 HID component (`0x0012`) together with the required sent and received message lists. |
| Now Playing | Verified | `0x5000` subscription and `0x5001` parsing in `web_app.py`; metadata, playback state, elapsed time, queue position, shuffle and repeat mode, application name, and artwork transfer ID. |
| Media library mirror | Verified | `0x4C03` subscription and `0x4C04` updates, including reset, deletion, and revision handling; each device is persisted to `data/<library_id>/iap2.db`. |
| First-seen order | Implemented | Monotonic `first_seen_seq` batches in `iap2.db`, independent of RTC time; existing rows migrate as batch 1. |
| HID media controls | Verified | Play, pause, next, previous, stop, shuffle, repeat, volume step, and mute through `0x6802`. |
| Playback selection | Verified | `0x4C06` resumes the current selection; `0x4C07` plays an ordered item list; `0x4C08` plays an album, artist, or device playlist. |
| Artwork transfer | Verified | iAP2 file-transfer type `0x0002`; 200x200 and 600x600 JPEG images; latest image kept in memory and a 128-entry LRU disk cache. |
| Device queue | Verified | Queue request through `0x5000`; file-transfer types `0x0003` and `0x0006`; the browser Up Next view prefers the device queue. |
| Device playlists | Verified, read-only | Playlist groups from `0x4C04` and file-transfer type `0x0004`; the content is a big-endian array of 64-bit item persistent identifiers. |
| Web interface and HTTP API | Implemented | `web_app.py` serves a single-page interface and a JSON API from a background iAP2 session. |
| Date-added metadata | Verified, out-of-band | `media_db_import.py` reads `MediaLibrary.sqlitedb` over AFC and writes the `dates` table in `media-cache.db`. |
| Library artwork | Verified, out-of-band | `media_db_artwork_import.py` copies artwork over AFC and writes image files, thumbnails, and the `artwork_*` tables in `media-cache.db`. |
| MediaLibrary web sync | Implemented | Split AFC/iAP2 panel: pairing, manual AFC cache load/update, incremental `Quick Sync`, destructive `Full Refresh`, AFC cache clearing, iAP2 library/artwork clearing, incremental sync, numeric progress, cancellation, and optional automatic return to iAP2 (off by default). |
| Embedded firmware | Not implemented | Architecture and feasibility notes are provided in `docs/PORTING_CPP.md` and the Tab5 documents. |

## Architecture

The control path and the media-database import path are deliberately separate.
They use different USB configurations and cannot operate at the same time.

### Control path

```text
iOS device
  |  USB configuration 2 / HID interface 2 / endpoint 0x83
  v
hid_async.py             HID report reassembly and asynchronous I/O
  v
ref_iap2_link_layer.py   iAP2 link layer, sessions, and file transfer
  v
csm.py / mfi_local.py    control-session messages and MFi authentication
  v
web_app.py               session state, library mirror, queue, HTTP API
  v
browser
```

`auth_test.py` contains the shared protocol helpers used by `web_app.py`,
including identification parameters, the HID report descriptor, command
encoding, and library parsing. It can also be executed as a standalone probe.

### Out-of-band media database and artwork path

```text
data/<library_id>/
  iap2.db                  iAP2 mirror, playlists, first_seen_seq
  media-cache.db           AFC dates and artwork mappings
  media-artwork/           AFC artwork
  artwork-cache/           iAP2 Now Playing artwork
  snapshot/                MediaLibrary.sqlitedb snapshot
```

`<library_id>` is the media-library GUID shared by AFC
(`_MLDatabaseProperties._UUID`) and iAP2 (`0x4C01` library UUID prefix).
`web_app.py` does not import or call libimobiledevice. The two paths exchange
data through SQLite/files, which keeps the iAP2 service independent of AFC
availability and device pairing state.

## Protocol implementation notes

### HID report framing

Each input report has the form:

```text
[report_id: u8] [LCB: u8] [payload: N bytes]
```

The device pads reports to the size class associated with the report ID and may
omit one or two trailing padding bytes after a whole number of 64-byte USB
packets. `hid_async.HIDReader` computes the expected report length from the
report ID, reassembles the USB packets into complete reports, and removes the
two-byte header before passing the payload to the iAP2 link layer.

This step is required for correct file transfer. Treating the report headers as
part of the iAP2 byte stream inserts two bytes at every report boundary and
corrupts large artwork transfers.

### iAP2 link layer

The link layer uses the following frame format:

```text
FF 5A | u16 length | u8 control | u8 seq | u8 ack | u8 session
      | u8 header checksum | payload | u8 payload checksum
```

The length field covers the complete frame. Each checksum is the two's
complement of the byte sum, so the sum of the covered bytes modulo 256 is zero.
Control messages use session 0, and file transfers use session 12.

The implementation is experimental. It uses per-packet acknowledgements and
tolerates a single sequence gap. It does not implement the complete iAP2
window, retransmission, and flow-control state machine required for a
production accessory.

### MFi authentication

The authentication exchange is:

```text
0xAA00 RequestAuthenticationCertificate
0xAA01 certificate
0xAA02 challenge
0xAA03 signature
0xAA05 AuthenticationSucceeded
```

The signature is ECDSA over the 32-byte SHA-256 challenge with the P-256
private key. The implementation converts the DER-encoded signature to the
64-byte `r || s` form expected by the device.

### Media library synchronization

The accessory subscribes with `0x4C03` and receives `0x4C04` updates. The
device may first send a reset indication, followed by a deletion phase for the
existing database, and then the full media-item set. The implementation
recognizes the end of the deletion phase, re-subscribes with the current
revision, and stores the resulting mirror in
`data/<library_id>/iap2.db`.

Subsequent connections send the stored revision and receive incremental
updates. Item persistent identifiers are represented as 16 hexadecimal digits.
Items first discovered in one sync pass share a monotonic `first_seen_seq`;
this order is independent of RTC time and is used by Recently Added. Existing
items migrate as sequence 1; later discovery batches continue at 2, 3, ...

The iAP2 media library protocol does not provide a Date Added field. `0x4C09`
`PlayMediaLibrarySpecial` is the All Songs command, not a Recently Added
collection, and is not used by the browser interface.

## Date-added metadata and artwork recovery

The device database is read out-of-band over AFC, not through iAP2. The
database path below the AFC root is:

```text
iTunes_Control/iTunes/MediaLibrary.sqlitedb
```

On the device this corresponds to
`/var/mobile/Media/iTunes_Control/iTunes/MediaLibrary.sqlitedb`.

`media_db_import.py` uses `idevice_id`, `idevicepair`, and `afcclient` from
libimobiledevice, validates that the device is paired, and pulls the database
over AFC. `ifuse` is used for the artwork tree and as a fallback for the
database. The database is in WAL mode. `afcclient` avoids the FUSE page-cache
issue that can make a large SQLite file copy inconsistent; the pulled files are
checkpointed and validated locally before the snapshot is accepted.

The relevant mapping is:

```text
item.item_pid    -> iAP2 MediaItem persistent identifier
item.date_added  -> seconds since 2001-01-01 UTC
```

The importer adds `978307200` to convert the Apple/Core Data timestamp to a
Unix timestamp. It writes the `dates` table in
`data/<library_id>/media-cache.db`, which maps each 16-hex item identifier to a
Unix timestamp. The album view uses the latest `date_added` value among the
tracks in each album.

Artwork is stored separately from the database:

```text
iTunes_Control/iTunes/Artwork/Originals/<relative_path>
```

The mapping is:

```text
item.item_pid
  -> best_artwork_token.entity_pid
  -> best_artwork_token.available_artwork_token
  -> artwork.artwork_token
  -> artwork.relative_path
```

`media_db_artwork_import.py` copies the referenced original images to
`data/<library_id>/media-artwork/`, creates 160-pixel thumbnails under
`media-artwork/thumbs/`, and writes the `artwork_files`, `artwork_items`, and
`artwork_albums` tables in `media-cache.db`. The web interface reads the
SQLite cache and image files directly. It does not require AFC to be available
after the import has completed.

The two USB paths are mutually exclusive:

- iAP2 control uses USB configuration 2 with the HID interface.
- AFC import uses USB configuration 3 or 4 with the Apple Mobile Device
  interface.

`web_app.py` owns the transition through `media_jobs.py`:

1. Stop the iAP2 session.
2. Switch the device to configuration 3.
3. Restart usbmuxd (the device may settle in configuration 4).
4. Pull the database and artwork over AFC.
5. If `Auto switch to iAP2` is enabled (default off), switch back to
   configuration 2 and restart the iAP2 session.

`Quick Sync` takes a new database snapshot and only copies new or changed
artwork. `Full Refresh` deletes the local artwork cache and downloads every
cover again. The importers are read-only with respect to the device.

## Repository layout

| Path | Purpose |
|---|---|
| `auth_test.py` | Standalone authentication, identification, Now Playing, HID, artwork, and media-library probe; also provides shared helpers for `web_app.py`. |
| `web_app.py` | Long-lived iAP2 session, media-library mirror, queue and history state, HTTP API, and single-page browser interface. |
| `hid_async.py` | Asynchronous HID reader and writer with report reassembly. |
| `ref_iap2_link_layer.py` | iAP2 link layer, session multiplexing, and file-transfer transport. |
| `csm.py` | Control-session message encoding and parsing. |
| `mfi_local.py` | Local MFi 3.0 P-256 authentication provider. |
| `media_db_import.py` | AFC importer for `MediaLibrary.sqlitedb` date-added values. |
| `media_db_artwork_import.py` | AFC importer for artwork files and item/album mappings. |
| `media_cache.py` | SQLite schema, legacy JSON migration, and AFC cache queries. |
| `device_store.py` | Per-device `iap2.db`, registry, localPlaylist CSV, and history log. |
| `media_jobs.py` | USB mode transition, MediaLibrary task runner, progress and cancel. |
| `run_web.sh` | Launcher for the web service with the permissions required for USB access. |
| `requirements.txt` | Python dependencies. |
| `docs/` | Handover notes, cache design, C++ porting notes, AFC importer notes, and Tab5 feasibility studies. |
| `data/` | Per-device runtime data keyed by library GUID; excluded from Git. |
| `static/bootstrap-icons/` | Vendored Bootstrap Icons CSS/fonts; served locally, no CDN. |
| `artifacts/` | Local development logs, captures, snapshots, and early probe scripts; excluded from Git. |

## Requirements

### Host

- Linux with USB host support and permission to access the Apple device.
- Python 3.10 or newer; the reference environment used Python 3.14.
- Root privileges or suitable udev rules for the USB device.

System packages for the iAP2 path:

```bash
sudo apt install libusb-1.0-0-dev libhidapi-hidraw0 libhidapi-dev usbutils
```

System packages for the AFC import path:

```bash
sudo apt install usbmuxd libimobiledevice-utils ifuse
```

### Python

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

### MFi identity

The implementation expects an MFi identity outside the repository:

```text
../iap2cert/certificate.p7b
../iap2cert/identity.pk8
```

The certificate must contain a P-256 leaf certificate. The private key may be
PKCS#8 DER, PEM, or a raw 32-byte scalar. The paths are set at the top of
`auth_test.py`.

The certificate and private key are not included in this repository. They are
subject to MFi licensing and non-disclosure terms and must not be committed,
published, or shared.

## Operation

### Standalone probe

```bash
sudo .venv/bin/python auth_test.py
```

The probe performs link negotiation, MFi authentication, identification, Now
Playing subscription, media-library subscription, and HID setup. It exits after
a fixed interval. Useful environment variables:

| Variable | Purpose |
|---|---|
| `HID_COMMAND` | Send one HID command after identification, for example `play`, `pause`, `next`, or `previous`. |
| `HID_DELAY` | Delay in seconds before sending `HID_COMMAND`. |
| `HID_LOG_RAW` | Set to `1` to print raw HID reports. |
| `IAP2_VERBOSE` | Set to `1` to print iAP2 packet-level logging. |
| `RUN_SECONDS` | Duration of the standalone probe. |

### Web service

```bash
sudo ./run_web.sh
# or
PORT=9000 sudo ./run_web.sh
```

Open `http://<host>:8080/` in a browser. The service runs the iAP2 session in a
background thread and reconnects every two seconds after a link loss. On Linux
the phone may first enumerate in a PTP or USB-Ethernet configuration; the
service switches it to configuration 2 before opening the HID interface.

The browser interface provides:

- Now Playing metadata, playback state, elapsed time, queue position, shuffle
  and repeat mode, application name, and artwork.
- Play, pause, previous, next, stop, volume step, mute, shuffle, and repeat
  controls.
- Albums, artists, album artists, genres, songs, recently added tracks,
  `localPlaylist`, and device playlists.
- Search, pagination, album sorting, album detail views, and playback of
  individual tracks, albums, artists, genres, album artists, and device
  playlists. The album grid shows the iAP2 `Seen` batch and AFC `Added` date;
  albums without an AFC date sort ahead of dated albums using their Seen
  sequence. `localPlaylist` supports adding/removing tracks; it is stored as
  a CSV debug file and cannot be created, renamed, or deleted.
- An Up Next view that uses the device queue when available and a local queue
  for playback initiated from the browser.
- Library status, revision, session state, and a resynchronization action.
- A two-column `MediaLibrary` panel: AFC on the left, iAP2 on the right.
  AFC side: `Pair / Trust`, manual cache load/update, `Quick Sync`,
  `Full Refresh`, `Clear AFC Cache`. iAP2 side: `Sync iAP2`, `Full Resync`,
  `Clear iAP2 Library`, `Clear iAP2 Artwork`, `Switch to iAP2`.
  Cache loading is manual. `Auto switch to iAP2 after AFC tasks` is off by
  default and persisted in `settings.json`. Bootstrap Icons are vendored under
  `static/bootstrap-icons/`; the page loads no external CDN assets.

The web interface does not implement authentication. It binds to `0.0.0.0` and
should only be exposed on a trusted network.

### Media database and artwork import

Use the `MediaLibrary` panel in the web interface for the normal workflow. The
panel switches USB modes, pairs the device, runs the import, shows numeric
progress, and loads the cache only when requested. `Auto switch to iAP2` is
optional and off by default.

The command-line tools remain available for diagnostics:

```bash
sudo python3 media_db_import.py --mount --progress-json
sudo python3 media_db_artwork_import.py --mount --mode quick --progress-json
sudo python3 media_db_artwork_import.py --mount --mode full --progress-json
```

Expected outputs:

```text
data/<library_id>/media-cache.db
data/<library_id>/media-artwork/<sha1>.<ext>
data/<library_id>/media-artwork/thumbs/<sha1>.jpg
data/<library_id>/snapshot/media-db-backup.sqlitedb
```

`--mode quick` reuses existing artwork and only copies missing/changed files.
`--mode full` deletes the local artwork cache and downloads every cover again.

See `docs/MEDIA_DB_IMPORT.md` for the complete procedure, pairing notes, and
measured import results.

## HTTP API

The web service exposes a JSON API used by the single-page interface. The main
endpoints are:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/state` | Current Now Playing and session state. |
| `GET` | `/api/artwork` | Latest Now Playing artwork. |
| `GET` | `/api/library` | Library summary and album list. |
| `GET` | `/api/library/album?pid=...` | Tracks in an album. |
| `GET` | `/api/library/songs?q=&limit=&offset=&sort=` | Song list with search, pagination, and sorting. |
| `GET` | `/api/library/recent?limit=&offset=` | Recently added tracks, using database dates when available. |
| `GET` | `/api/library/artwork?pid=...` or `?album_pid=...` | Imported library artwork, original or thumbnail. |
| `GET` | `/api/queue` | Current Up Next snapshot. |
| `POST` | `/api/command` | HID control command. |
| `POST` | `/api/play` | Play an item, album, artist, genre, album artist, device playlist, or item list. |
| `POST` | `/api/library/sync` | Clear the iAP2 revision and reconnect for a full synchronization. |
| `POST` | `/api/library/sync-incremental` | Request an incremental `0x4C03` library update with the current revision. |
| `POST` | `/api/library/clear-iap2` | Clear the active device's iAP2 mirror; the protocol performs a full resync. |
| `POST` | `/api/library/clear-artwork` | Clear the active device's iAP2 Now Playing artwork cache. |
| `POST` | `/api/media/action` | Start an AFC action: `pair`, `quick`, `full`, `db`, `artwork`, or `clear`. |
| `POST` | `/api/media/cancel` | Cancel the running MediaLibrary task. |
| `POST` | `/api/media/mode` | Switch back to iAP2 mode (`{"mode":"iap2"}`). |
| `POST` | `/api/media/load` | Load an on-disk cache into the web process: `{"target":"library"|"artwork"|"all"}`. |
| `POST` | `/api/settings` | Persist UI settings, currently `{"auto_switch_iap2":bool}`. |

The `/api/state` response includes a `media` object with the current device
mode, active `library_id`, pairing state, UDID, AFC cache loaded counts, and
the running/last task with numeric progress. Top-level `settings` contains the
persisted UI options. Additional endpoints for artists, album artists, genres,
and playlists are implemented in `web_app.py`.

## Persistent data and Git policy

The repository excludes runtime state, device-derived metadata, credentials, and
Apple reference material. The main excluded paths are:

| Path | Reason |
|---|---|
| `data/<library_id>/iap2.db` | Per-device iAP2 mirror, device playlists, and `first_seen_seq`. |
| `data/<library_id>/media-cache.db` | Per-device AFC dates/artwork SQLite cache. |
| `data/<library_id>/media-artwork/` | Imported AFC artwork and thumbnails. |
| `data/<library_id>/artwork-cache/` | iAP2 Now Playing artwork LRU cache. |
| `data/<library_id>/snapshot/` | MediaLibrary.sqlitedb snapshot. |
| `data/<library_id>/localPlaylist.csv` | Single local playlist CSV for debug. |
| `data/<library_id>/history.log` | Track-change debug log. |
| `data/devices.json` | Device registry and active library ID. |
| `settings.json` | UI settings such as automatic return to iAP2. |
| `artifacts/` | Development logs, captures, and SQLite snapshots. |
| `docs/reference/` | Apple specification, not redistributable. |
| `iap2cert/`, `*.p7b`, `*.pk8`, `*.pem`, `*.key` | MFi identity and private keys. |

Library state is written atomically with a temporary file and `os.replace()`.
The media-library mirror is saved when it is dirty and has been idle for more
than five seconds; the service checks every ten seconds. `localPlaylist.csv`
keeps one local playlist only; playlist creation/deletion is disabled. Track
changes are appended to `history.log`; no structured history is kept. The
local playlist is limited to 2,000 items because of the iAP2 message size
limit.

## Limitations

- The link layer is experimental and does not provide a complete production
  retransmission and flow-control implementation.
- iAP2 does not provide absolute volume control or a seek command. Volume is
  controlled through HID increment and decrement usages. Fast-forward and
  rewind usages are present in the HID descriptor, but a short press is
  interpreted by the device as next or previous; seeking has not been verified.
- Date Added is not part of the iAP2 media library. It is recovered from the
  device SQLite database over AFC. The value reflects the database content and
  may represent a bulk import or synchronization time.
- The iAP2 media-item data does not include per-item artwork identifiers on the
  tested device. Now Playing artwork is transferred over iAP2, while library
  artwork is imported over AFC.
- Device playlists are read-only. Accessory-side playlists are implemented in
  the backend and HTTP API, but the current browser interface exposes only
  device playlists.
- The web interface has no authentication and supports a single active device
  and a single iAP2 session. MediaLibrary actions run privileged AFC tools, so
  keep the interface on a trusted network.
- Device separation uses the media-library GUID shared by AFC and iAP2. Two
  devices restored from the same media-library backup could share that GUID.
- The iAP2 control path and the AFC import path require different USB
  configurations and cannot run concurrently.
- The project requires an MFi certificate and private key. It is not an MFi
  certified product.

## Embedded porting

The Python implementation is a reference for a rewrite, not a codebase that can
be cross-compiled for a microcontroller. A port to a platform such as the
ESP32-P4 requires:

- An ESP-IDF USB host implementation that selects USB configuration 2, claims
  HID interface 2, reads endpoint `0x83`, and sends output reports with
  `SET_REPORT`.
- The same HID report reassembly rules, including report ID size classes and
  the possible omission of one or two trailing padding bytes.
- A C or C++ implementation of the iAP2 link layer, CSM codec, and file-transfer
  state machine.
- mbedTLS ECDSA P-256 for MFi authentication in place of the Python
  `cryptography` package.
- Persistent storage for the media-library mirror, the SQLite database, and
  artwork, or a streaming path to a host.
- A display and input implementation, such as LVGL, or retention of the HTTP
  interface for a browser on another device.

The 50.7 MB `MediaLibrary.sqlitedb` does not fit in 16 MB of flash or 32 MB of
PSRAM. A P4-class device must stream the database to SD storage or to a host;
the firmware code size itself is not the limiting factor. The AFC service does
not require TLS, but the lockdown session does, and a valid pairing record is
required.

Relevant design documents:

- `docs/PORTING_CPP.md` — C++ module layout, threading model, and embedded
  constraints.
- `docs/tab5-port-assessment.md` — ESP32-P4 and M5Stack Tab5 implementation
  guide.
- `docs/tab5-afc-db-pull-feasibility.md` — AFC database extraction, memory
  budget, and minimal protocol stack.

## Documentation

- `docs/HANDOVER.md` — protocol findings, implementation status, and operational
  notes.
- `docs/CACHE.md` — cache and persistence design.
- `docs/PORTING_CPP.md` — C++ and embedded port draft.
- `docs/MEDIA_DB_IMPORT.md` — AFC database/artwork import and SQLite cache.
- `docs/ft-file-transfer.md` — iAP2 file-transfer concepts and command values.
- `docs/tab5-port-assessment.md` — Tab5 implementation guide.
- `docs/tab5-afc-db-pull-feasibility.md` — AFC database pull feasibility.

The Apple Accessory Interface Specification is not included in this repository.
It is restricted reference material and must be obtained through the appropriate
Apple program.

## Attribution and licensing

The iAP2 link-layer and HID transport implementations are derived from the
`wiomoc/iap2` project and have been modified for this codebase. Review and
preserve the upstream attribution and license terms before redistributing this
repository.

No license file is currently included. Add an explicit license before
redistributing the project.

This project is not affiliated with, endorsed by, or certified by Apple Inc.
iPod, iPhone, iPad, MFi, and related marks are trademarks of Apple Inc.
