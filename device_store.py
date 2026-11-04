#!/usr/bin/env python3
"""Per-device storage for the iAP2 mirror and AFC-derived caches.

The common device/library key is the media-library GUID:
  AFC:  _MLDatabaseProperties._UUID
  iAP2: 0x4C01 library UUID prefix before -MPB- / -4954524C-
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
REGISTRY_PATH = DATA_DIR / "devices.json"

# Legacy global files (migrated once).
LEGACY_LIBRARY = BASE_DIR / "media-library.json"
LEGACY_CACHE_DB = BASE_DIR / "media-cache.db"
LEGACY_ARTWORK = BASE_DIR / "media-artwork"
LEGACY_NOW_PLAYING = BASE_DIR / "artwork-cache"
LEGACY_SNAPSHOT = BASE_DIR / "artifacts" / "media-db-snapshot"
LEGACY_DATES = BASE_DIR / "media-db-dates.json"
LEGACY_ARTWORK_JSON = BASE_DIR / "media-db-artwork.json"
LEGACY_PLAYLISTS = BASE_DIR / "playlists.json"
LEGACY_HISTORY = BASE_DIR / "history.json"

LIBRARY_ID_RE = re.compile(
    r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")

LOCAL_PLAYLIST_FIELDS = [
    "pid", "title", "album", "artist", "album_artist",
    "album_pid", "artist_pid", "album_artist_pid",
    "genre", "composer", "genre_pid", "composer_pid", "artwork_id",
    "rating", "media_type", "compilation", "duration_ms",
    "track", "track_count", "disc", "disc_count",
    "first_seen", "first_seen_seq", "date_added", "extra_25", "added_at",
]

_REG_LOCK = threading.Lock()
_REGISTRY = {"active": None, "devices": {}}


def base_library_id(value):
    """Return the 36-character GUID from an iAP2 or AFC library UUID."""
    if not value:
        return None
    text = str(value).strip().upper()
    match = re.match(
        r"^([0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12})",
        text)
    if match:
        return match.group(1)
    return text


def _valid_id(library_id):
    return bool(library_id and LIBRARY_ID_RE.match(library_id.upper()))


def paths(library_id):
    library_id = base_library_id(library_id)
    directory = DATA_DIR / library_id
    return {
        "dir": directory,
        "iap2_db": directory / "iap2.db",
        "media_cache": directory / "media-cache.db",
        "artwork": directory / "media-artwork",
        "now_playing": directory / "artwork-cache",
        "snapshot": directory / "snapshot",
        "snapshot_db": directory / "snapshot" / "media-db-backup.sqlitedb",
        "local_playlist": directory / "localPlaylist.csv",
        "history_log": directory / "history.log",
    }


def ensure_device(library_id, name=None, udid=None):
    library_id = base_library_id(library_id)
    if not _valid_id(library_id):
        raise ValueError(f"invalid library id: {library_id!r}")
    p = paths(library_id)
    p["dir"].mkdir(parents=True, exist_ok=True)
    for key in ("artwork", "now_playing", "snapshot"):
        p[key].mkdir(parents=True, exist_ok=True)
    with _REG_LOCK:
        entry = _REGISTRY["devices"].setdefault(library_id, {})
        if name:
            entry["name"] = name
        if udid:
            entry["udid"] = udid
        entry["last_seen"] = time.time()
        _REGISTRY["active"] = library_id
        _save_registry_locked()
    return p


def load_registry():
    global _REGISTRY
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return
    except Exception as exc:  # noqa: BLE001
        print(f"device registry load failed: {exc}")
        return
    with _REG_LOCK:
        active = base_library_id(data.get("active"))
        devices = data.get("devices", {})
        if isinstance(devices, dict):
            _REGISTRY["devices"] = {
                base_library_id(key): value
                for key, value in devices.items()
                if _valid_id(base_library_id(key))
            }
        _REGISTRY["active"] = active if _valid_id(active) else None


def _save_registry_locked():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = str(REGISTRY_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(_REGISTRY, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, REGISTRY_PATH)


def save_registry():
    with _REG_LOCK:
        _save_registry_locked()


def active_library_id():
    with _REG_LOCK:
        return _REGISTRY["active"]


def set_active(library_id, name=None, udid=None):
    return ensure_device(library_id, name=name, udid=udid)


def registry_snapshot():
    with _REG_LOCK:
        return json.loads(json.dumps(_REGISTRY))


# ---------------------------------------------------------------------------
# iAP2 SQLite store
# ---------------------------------------------------------------------------

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS items (
  pid              TEXT PRIMARY KEY,
  title            TEXT,
  album            TEXT,
  artist           TEXT,
  album_artist     TEXT,
  genre            TEXT,
  composer         TEXT,
  media_type       INTEGER,
  rating           INTEGER,
  duration_ms      INTEGER,
  album_pid        TEXT,
  artist_pid       TEXT,
  album_artist_pid TEXT,
  track            INTEGER,
  track_count      INTEGER,
  disc             INTEGER,
  disc_count       INTEGER,
  compilation      INTEGER,
  first_seen       REAL,
  first_seen_seq   INTEGER NOT NULL,
  data_json        TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS playlists (
  pid TEXT PRIMARY KEY,
  name TEXT,
  parent_pid TEXT,
  data_json TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS seen (
  pid TEXT PRIMARY KEY,
  first_seen_seq INTEGER NOT NULL
) WITHOUT ROWID;
"""


def _open(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _meta(conn):
    return {row["key"]: row["value"]
            for row in conn.execute("SELECT key, value FROM meta")}


def _set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)))


def iap2_exists(library_id):
    return paths(library_id)["iap2_db"].exists()


def load_iap2(library_id):
    library_id = base_library_id(library_id)
    p = paths(library_id)
    if not p["iap2_db"].exists():
        return {"items": {}, "playlists": {}, "seen": {},
                "revision": None, "next_seen_seq": 1}
    conn = _open(p["iap2_db"])
    try:
        meta = _meta(conn)
        items = {}
        for row in conn.execute("SELECT * FROM items"):
            try:
                item = json.loads(row["data_json"])
            except Exception:  # noqa: BLE001
                item = {"id": row["pid"]}
            item.setdefault("id", row["pid"])
            item["first_seen"] = float(row["first_seen"] or 0.0)
            item["first_seen_seq"] = int(row["first_seen_seq"] or 1)
            items[item["id"]] = item
        playlists = {}
        for row in conn.execute("SELECT * FROM playlists"):
            try:
                pl = json.loads(row["data_json"])
            except Exception:  # noqa: BLE001
                pl = {"id": row["pid"], "name": row["name"] or ""}
            pl.setdefault("id", row["pid"])
            playlists[pl["id"]] = pl
        seen = {row["pid"]: int(row["first_seen_seq"] or 1)
                for row in conn.execute(
                    "SELECT pid, first_seen_seq FROM seen")}
        for pid, item in items.items():
            seen.setdefault(pid, int(item.get("first_seen_seq") or 1))
        next_seq = int(meta.get("next_seen_seq") or 1)
        if next_seq < 1:
            next_seq = 1
        return {
            "items": items,
            "playlists": playlists,
            "seen": seen,
            "revision": meta.get("revision") or None,
            "next_seen_seq": next_seq,
        }
    finally:
        conn.close()


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def save_iap2(library_id, items, playlists, revision=None,
              next_seen_seq=1, seen=None):
    library_id = base_library_id(library_id)
    p = ensure_device(library_id)
    conn = _open(p["iap2_db"])
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM items")
        conn.execute("DELETE FROM playlists")
        conn.execute("DELETE FROM seen")
        rows = []
        for item in items.values():
            item_id = item.get("id")
            if not item_id:
                continue
            seq = int(item.get("first_seen_seq") or 1)
            item["first_seen_seq"] = seq
            rows.append((
                item_id,
                item.get("title"),
                item.get("album"),
                item.get("artist"),
                item.get("albumArtist") or item.get("album_artist"),
                item.get("genre"),
                item.get("composer"),
                _int(item.get("media_type")),
                _int(item.get("rating")),
                _int(item.get("duration_ms")),
                item.get("album_pid"),
                item.get("artist_pid"),
                item.get("album_artist_pid"),
                _int(item.get("track")),
                _int(item.get("track_count", item.get("tracks"))),
                _int(item.get("disc")),
                _int(item.get("disc_count", item.get("discs"))),
                1 if item.get("compilation") else 0,
                float(item.get("first_seen") or 0.0),
                seq,
                json.dumps(item, ensure_ascii=False),
            ))
        conn.executemany(
            """INSERT INTO items(
                 pid, title, album, artist, album_artist, genre, composer,
                 media_type, rating, duration_ms, album_pid, artist_pid,
                 album_artist_pid, track, track_count, disc, disc_count,
                 compilation, first_seen, first_seen_seq, data_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows)
        pl_rows = []
        for pl in playlists.values():
            pid = pl.get("id")
            if not pid:
                continue
            pl_rows.append((pid, pl.get("name"), pl.get("parent_pid"),
                            json.dumps(pl, ensure_ascii=False)))
        conn.executemany(
            "INSERT INTO playlists(pid, name, parent_pid, data_json) "
            "VALUES(?,?,?,?)", pl_rows)
        if seen is None:
            seen = {item["id"]: int(item.get("first_seen_seq") or 1)
                    for item in items.values() if item.get("id")}
        conn.executemany(
            "INSERT INTO seen(pid, first_seen_seq) VALUES(?,?)",
            [(pid, int(seq or 1)) for pid, seq in seen.items()])
        _set_meta(conn, "library_id", library_id)
        _set_meta(conn, "revision", revision or "")
        _set_meta(conn, "next_seen_seq", int(next_seen_seq))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Local playlist CSV and history log
# ---------------------------------------------------------------------------

def load_local_playlist(library_id):
    p = paths(library_id)
    if not p["local_playlist"].exists():
        return {}
    rows = {}
    try:
        with open(p["local_playlist"], "r", encoding="utf-8",
                  newline="") as fh:
            for row in csv.DictReader(fh):
                pid = (row.get("pid") or "").strip()
                if not pid:
                    continue
                for key in ("rating", "media_type", "duration_ms",
                            "track", "track_count", "disc", "disc_count",
                            "first_seen_seq"):
                    row[key] = _int(row.get(key))
                for key in ("first_seen", "date_added", "added_at"):
                    try:
                        row[key] = float(row.get(key) or 0.0)
                    except (TypeError, ValueError):
                        row[key] = 0.0
                row["compilation"] = str(row.get("compilation") or "").lower() in (
                    "1", "true", "yes")
                row["id"] = pid
                rows[pid] = row
    except Exception as exc:  # noqa: BLE001
        print(f"local playlist load failed: {exc}")
    return rows


def save_local_playlist(library_id, rows):
    p = ensure_device(library_id)
    tmp = str(p["local_playlist"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=LOCAL_PLAYLIST_FIELDS,
                                extrasaction="ignore")
        writer.writeheader()
        for row in rows.values():
            writer.writerow({key: row.get(key, "") for key in
                             LOCAL_PLAYLIST_FIELDS})
    os.replace(tmp, p["local_playlist"])


def append_history_log(library_id, entry):
    if not library_id:
        return
    p = ensure_device(library_id)
    record = dict(entry)
    record.setdefault("ts", time.time())
    with open(p["history_log"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Legacy migration
# ---------------------------------------------------------------------------

def _merge_move(src: Path, dst: Path):
    if not src.exists():
        return
    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        for child in list(src.iterdir()):
            target = dst / child.name
            if target.exists():
                if target.is_dir() and child.is_dir():
                    _merge_move(child, target)
                continue
            shutil.move(str(child), str(target))
        try:
            src.rmdir()
        except OSError:
            pass
    else:
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))


def _legacy_rows(items):
    rows = {}
    for pid, item in items.items():
        rows[pid] = {
            "pid": pid,
            "title": item.get("title") or "",
            "album": item.get("album") or "",
            "artist": item.get("artist") or "",
            "album_artist": item.get("albumArtist") or "",
            "album_pid": item.get("album_pid") or "",
            "artist_pid": item.get("artist_pid") or "",
            "album_artist_pid": item.get("album_artist_pid") or "",
            "genre": item.get("genre") or "",
            "composer": item.get("composer") or "",
            "genre_pid": item.get("genre_pid") or "",
            "composer_pid": item.get("composer_pid") or "",
            "artwork_id": item.get("artwork_id") or "",
            "rating": item.get("rating") or 0,
            "media_type": item.get("media_type") or "",
            "compilation": 1 if item.get("compilation") else 0,
            "duration_ms": item.get("duration_ms") or 0,
            "track": item.get("track") or 0,
            "track_count": item.get("track_count", item.get("tracks")) or 0,
            "disc": item.get("disc") or 1,
            "disc_count": item.get("disc_count", item.get("discs")) or 1,
            "first_seen": item.get("first_seen") or 0.0,
            "first_seen_seq": item.get("first_seen_seq") or 1,
            "date_added": item.get("date_added") or "",
            "extra_25": item.get("extra_25") or "",
            "added_at": time.time(),
        }
    return rows


def migrate_legacy(library_id):
    library_id = base_library_id(library_id)
    p = ensure_device(library_id)
    migrated = False
    legacy_items = {}

    if not p["iap2_db"].exists() and LEGACY_LIBRARY.exists():
        try:
            with open(LEGACY_LIBRARY, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            items = data.get("items", {})
            legacy_items = items
            for item in items.values():
                item.setdefault("first_seen_seq", 1)
                item.setdefault("first_seen", 0.0)
            playlists = data.get("playlists", {})
            save_iap2(library_id, items, playlists,
                      revision=data.get("revision"), next_seen_seq=2)
            os.replace(LEGACY_LIBRARY,
                       p["dir"] / "media-library.json.migrated")
            migrated = True
            print(f"migrated {len(items)} items to {p['iap2_db']}",
                  flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"iap2 legacy migration failed: {exc}", flush=True)

    _merge_move(LEGACY_CACHE_DB, p["media_cache"])
    for suffix in ("-wal", "-shm"):
        _merge_move(Path(str(LEGACY_CACHE_DB) + suffix),
                    Path(str(p["media_cache"]) + suffix))
    _merge_move(LEGACY_ARTWORK, p["artwork"])
    _merge_move(LEGACY_NOW_PLAYING, p["now_playing"])
    _merge_move(LEGACY_SNAPSHOT, p["snapshot"])
    for legacy in (LEGACY_DATES, LEGACY_ARTWORK_JSON):
        if legacy.exists():
            try:
                os.replace(legacy, p["dir"] / (legacy.name + ".migrated"))
            except OSError:
                pass

    if not p["local_playlist"].exists() and LEGACY_PLAYLISTS.exists():
        try:
            with open(LEGACY_PLAYLISTS, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            playlists = data.get("playlists", {})
            source = playlists.get("localPlaylist")
            if source is None and playlists:
                source = next(iter(playlists.values()))
            if source:
                item_map = legacy_items
                if not item_map and LEGACY_LIBRARY.exists():
                    with open(LEGACY_LIBRARY, "r", encoding="utf-8") as fh:
                        item_map = json.load(fh).get("items", {})
                rows = _legacy_rows(item_map)
                chosen = {}
                for pid in source.get("items", []):
                    if pid in rows:
                        chosen[pid] = rows[pid]
                save_local_playlist(library_id, chosen)
            os.replace(LEGACY_PLAYLISTS,
                       p["dir"] / "playlists.json.migrated")
        except Exception as exc:  # noqa: BLE001
            print(f"local playlist migration failed: {exc}", flush=True)

    if not p["history_log"].exists() and LEGACY_HISTORY.exists():
        try:
            with open(LEGACY_HISTORY, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            with open(p["history_log"], "a", encoding="utf-8") as out:
                for entry in data.get("entries", []):
                    out.write(json.dumps(entry, ensure_ascii=False) + "\n")
            os.replace(LEGACY_HISTORY,
                       p["dir"] / "history.json.migrated")
        except Exception as exc:  # noqa: BLE001
            print(f"history migration failed: {exc}", flush=True)

    return migrated
