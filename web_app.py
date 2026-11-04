#!/usr/bin/env python3
"""MediaCtrl web UI: long-lived iAP2 session + HTTP control panel.

Run as root (USB/HID access):
    sudo .venv/bin/python web_app.py            # http://<host>:8080/
    PORT=9000 sudo .venv/bin/python web_app.py  # custom port

The iAP2 session runs in its own thread with its own asyncio loop and
reconnects automatically. The HTTP server only reads a snapshot of shared
state and forwards commands to the session.
"""
import asyncio
import json
import mimetypes
import os
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import csm
import device_store
import hid_async
import media_cache
import media_jobs
import ref_iap2_link_layer as link_layer
from auth_test import (
    CERT_PATH,
    HID_BITS,
    INPUT_REPORTS,
    KEY_PATH,
    OUTPUT_REPORTS,
    identification_params,
    now_playing_params,
    open_hid,
    parse_library_info,
    power_update_params,
    send_csm,
    send_hid_command,
    send_hid_start,
    send_now_playing_info,
    set_configuration,
)
from mfi_local import LocalMfiProvider

PORT = int(os.environ.get("PORT", "8080"))
LIBRARY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "media-library.json")
PLAYLISTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "playlists.json")
ARTWORK_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "artwork-cache")
MEDIA_DATES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "media-db-dates.json")
MEDIA_ARTWORK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "media-db-artwork.json")
MEDIA_ARTWORK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "media-artwork")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEDIA_CACHE_PATH = os.path.join(BASE_DIR, "media-cache.db")
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")

STATE_LOCK = threading.Lock()
CACHE_LOCK = threading.Lock()
SETTINGS_LOCK = threading.Lock()
FT_PLAYLIST_FILES = {}
FT_QUEUE_FILES = {}
MEDIA_DATES = {"mtime": 0.0, "dates": {}, "loaded": False}
MEDIA_ARTWORK = {"mtime": 0.0, "items": {}, "albums": {},
                 "loaded": False, "files": 0}
SETTINGS = {"auto_switch_iap2": False}
POWER_DEFAULTS = {
    "max_current_ma": None,
    "device_will_charge": None,
    "accessory_power_mode": None,
    "external_charger": None,
    "charging_state": None,
    "battery_level": None,
    "supported": None,
    "updated_at": 0.0,
}
STATE = {
    "connected": False,
    "link_state": None,
    "now_playing": {
        "title": "",
        "album": "",
        "artist": "",
        "album_artist": "",
        "genre": "",
        "composer": "",
        "duration_ms": 0,
        "rating": None,
        "media_type": None,
        "track": None,
        "track_count": None,
        "disc": None,
        "disc_count": None,
        "compilation": None,
        "album_pid": None,
        "artist_pid": None,
        "album_artist_pid": None,
        "status": None,
        "elapsed_ms": 0,
        "queue_index": None,
        "queue_count": None,
        "queue_chapter": None,
        "queue_available": None,
        "queue_transfer_id": None,
        "queue_transfer": False,
        "shuffle": None,
        "repeat": None,
        "app": "",
        "app_bundle_id": "",
        "playback_speed": None,
        "seek_available": None,
        "chapter_count": None,
        "like_supported": None,
        "ban_supported": None,
        "liked": None,
        "banned": None,
        "artwork_id": None,
        "updated_at": 0.0,
    },
    "power": dict(POWER_DEFAULTS),
    "device": {
        "name": "",
        "language": "",
        "time_unix": None,
        "timezone_offset_minutes": None,
        "daylight_offset_minutes": None,
        "uuid": "",
        "updated_at": 0.0,
    },
    "library_progress": None,
    "library_hiding_remote": None,
    "play_all_songs_capable": None,
    "artwork_bytes": b"",
    "artwork_ready_id": None,
    "artwork_mime": "image/jpeg",
    "libraries": [],
    "library_revision": None,
    "last_error": None,
    "last_event": "",
}

# ---------------------------------------------------------------------------
# Media library mirror (persisted between sessions; keyed by persistent ID).
# ---------------------------------------------------------------------------
LIBRARY_LOCK = threading.Lock()
LIBRARY = {
    "library_id": None,
    "revision": None,
    "items": {},
    "playlists": {},
    "first_seen": {},
    "first_seen_seq": {},
    "next_seen_seq": 1,
    "batch_seq": None,
    "batch_last": 0.0,
    "paused": False,
    "dirty": False,
    "syncing": False,
    "last_change": 0.0,
    "last_update": 0.0,
    "version": 0,
    "_summary": None,
    "_summary_version": -1,
    "_artists": None,
    "_artists_version": -1,
    "_songs": None,
    "_songs_version": -1,
    "_genres": None,
    "_genres_version": -1,
    "_album_artists": None,
    "_album_artists_version": -1,
}

LIBRARY_RESYNC_REQUESTED = threading.Event()  # kept for external hooks


def _library_id():
    return LIBRARY.get("library_id")


def _device_paths(library_id=None):
    lid = library_id or _library_id()
    if lid:
        return device_store.paths(lid)
    return {
        "dir": BASE_DIR,
        "iap2_db": None,
        "media_cache": MEDIA_CACHE_PATH,
        "artwork": MEDIA_ARTWORK_DIR,
        "now_playing": ARTWORK_CACHE_DIR,
        "snapshot": os.path.join(BASE_DIR, "artifacts", "media-db-snapshot"),
        "snapshot_db": os.path.join(BASE_DIR, "artifacts",
                                    "media-db-snapshot",
                                    "media-db-backup.sqlitedb"),
    }


def _media_cache_path():
    return str(_device_paths()["media_cache"])


def _artwork_dir():
    return str(_device_paths()["artwork"])


def _now_playing_dir():
    return str(_device_paths()["now_playing"])


def _snapshot_dir():
    return str(_device_paths()["snapshot"])


def _snapshot_db():
    return str(_device_paths()["snapshot_db"])


# ---------------------------------------------------------------------------
# Accessory-side playlists.  i2P's media library is read-only, so these are
# assembled by the accessory and played with 0x4C07 (PlayMediaLibraryItems).
# ---------------------------------------------------------------------------
PLAYLIST_LOCK = threading.Lock()
PLAYLISTS = {"playlists": {}}
MAX_PLAYLIST_ITEMS = 2000

# Accessory-side queue/history used for "Up Next" and smart playlists.
QUEUE_LOCK = threading.Lock()
QUEUE = {"items": [], "source": "", "updated": 0.0,
         "base": 0, "meta": {}}
# Editable accessory-side queue.  It is shown in Up Next while non-empty and
# sent to the device as one ordered list when the user presses Play queue.
QUEUE_BUILDER = {"items": [], "updated": 0.0}
LAST_HISTORY = {"id": None, "ts": 0.0}



LOCAL_PLAYLIST_ID = "localPlaylist"


def _row_for_item(item):
    return {
        "pid": item.get("id"),
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
        "track_count": item.get("track_count",
                                item.get("tracks")) or 0,
        "disc": item.get("disc") or 1,
        "disc_count": item.get("disc_count", item.get("discs")) or 1,
        "first_seen": item.get("first_seen") or 0.0,
        "first_seen_seq": item.get("first_seen_seq") or 1,
        "date_added": item.get("date_added") or "",
        "extra_25": item.get("extra_25") or "",
        "added_at": time.time(),
    }


def _row_to_entry(row):
    return {
        "id": row.get("pid") or row.get("id"),
        "title": row.get("title") or "",
        "artist": row.get("artist") or "",
        "album": row.get("album") or "",
        "album_artist": row.get("album_artist") or "",
        "album_pid": row.get("album_pid") or None,
        "artist_pid": row.get("artist_pid") or None,
        "genre": row.get("genre") or "",
        "composer": row.get("composer") or "",
        "rating": row.get("rating") or 0,
        "media_type": row.get("media_type"),
        "compilation": bool(row.get("compilation")),
        "duration_ms": row.get("duration_ms") or 0,
        "track": row.get("track") or 0,
        "disc": row.get("disc") or 1,
        "first_seen": row.get("first_seen") or 0.0,
        "first_seen_seq": row.get("first_seen_seq") or 1,
        "date_added": row.get("date_added") or None,
    }


def _local_playlist_locked():
    return PLAYLISTS["playlists"].setdefault(
        LOCAL_PLAYLIST_ID,
        {"id": LOCAL_PLAYLIST_ID, "name": LOCAL_PLAYLIST_ID,
         "rows": {}, "items": [], "updated": 0.0})


def load_playlists():
    library_id = _library_id()
    rows = {}
    if library_id:
        rows = device_store.load_local_playlist(library_id)
    else:
        try:
            with open(PLAYLISTS_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            playlists = data.get("playlists", {})
            source = playlists.get(LOCAL_PLAYLIST_ID)
            if source is None and playlists:
                source = next(iter(playlists.values()))
            if source:
                with LIBRARY_LOCK:
                    by_id = dict(LIBRARY["items"])
                for pid in source.get("items", []):
                    item = by_id.get(pid)
                    if item:
                        rows[pid] = _row_for_item(item)
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            print(f"local playlist load failed: {exc}")
    with PLAYLIST_LOCK:
        PLAYLISTS["playlists"] = {
            LOCAL_PLAYLIST_ID: {
                "id": LOCAL_PLAYLIST_ID,
                "name": LOCAL_PLAYLIST_ID,
                "rows": rows,
                "items": list(rows.keys()),
                "updated": time.time(),
            }
        }
    print(f"localPlaylist loaded: {len(rows)} tracks")


def save_playlists():
    with PLAYLIST_LOCK:
        pl = _local_playlist_locked()
        rows = dict(pl.get("rows", {}))
        items = list(pl.get("items", []))
        library_id = _library_id()
    try:
        if library_id:
            device_store.save_local_playlist(library_id, rows)
        else:
            payload = {"playlists": {
                LOCAL_PLAYLIST_ID: {
                    "id": LOCAL_PLAYLIST_ID,
                    "name": LOCAL_PLAYLIST_ID,
                    "items": items,
                }
            }}
            tmp = PLAYLISTS_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, PLAYLISTS_PATH)
    except Exception as exc:  # noqa: BLE001
        print(f"localPlaylist save failed: {exc}")


def _playlist_meta(pl):
    return {
        "id": pl["id"],
        "name": pl["name"],
        "tracks": len(pl.get("items", [])),
        "updated": pl.get("updated", 0),
    }


def playlist_list():
    with PLAYLIST_LOCK:
        pl = _local_playlist_locked()
        rows = [_playlist_meta(pl)]
    return {"playlists": rows, "total": len(rows)}


def playlist_get(pl_id):
    if pl_id != LOCAL_PLAYLIST_ID:
        return {"ok": False, "error": "playlist not found"}
    with PLAYLIST_LOCK:
        pl = _local_playlist_locked()
        meta = _playlist_meta(pl)
        rows = [dict(row) for row in pl.get("rows", {}).values()]
    items = [_row_to_entry(row) for row in rows]
    meta["missing"] = 0
    return {"ok": True, "playlist": meta, "items": items}


def playlist_create(name):
    raise ValueError("localPlaylist is the only local playlist")


def playlist_rename(pl_id, name):
    raise ValueError("localPlaylist cannot be renamed")


def playlist_delete(pl_id):
    raise ValueError("localPlaylist cannot be deleted")


def playlist_add_item(pl_id, item_id):
    if pl_id != LOCAL_PLAYLIST_ID:
        raise KeyError("playlist not found")
    with LIBRARY_LOCK:
        item = LIBRARY["items"].get(item_id)
    if item is None:
        raise KeyError("Track not found")
    row = _row_for_item(item)
    with PLAYLIST_LOCK:
        pl = _local_playlist_locked()
        if item_id in pl["rows"]:
            return {"ok": True, "already": True, "playlist": _playlist_meta(pl)}
        if len(pl["rows"]) >= MAX_PLAYLIST_ITEMS:
            raise ValueError(f"Playlist supports at most {MAX_PLAYLIST_ITEMS} tracks")
        pl["rows"][item_id] = row
        pl["items"].append(item_id)
        pl["updated"] = time.time()
        meta = _playlist_meta(pl)
    save_playlists()
    return {"ok": True, "already": False, "playlist": meta,
            "item": _row_to_entry(row)}


def playlist_add_items(pl_id, item_ids):
    if pl_id != LOCAL_PLAYLIST_ID:
        raise KeyError("playlist not found")
    ids = [str(x) for x in (item_ids or []) if x]
    if not ids:
        raise ValueError("No tracks to add")
    with LIBRARY_LOCK:
        by_id = dict(LIBRARY["items"])
    added = 0
    already = 0
    missing = 0
    with PLAYLIST_LOCK:
        pl = _local_playlist_locked()
        for item_id in ids:
            if item_id in pl["rows"]:
                already += 1
                continue
            item = by_id.get(item_id)
            if item is None:
                missing += 1
                continue
            if len(pl["rows"]) >= MAX_PLAYLIST_ITEMS:
                raise ValueError(f"Playlist supports at most {MAX_PLAYLIST_ITEMS} tracks")
            pl["rows"][item_id] = _row_for_item(item)
            pl["items"].append(item_id)
            added += 1
        pl["updated"] = time.time()
        meta = _playlist_meta(pl)
    save_playlists()
    return {"ok": True, "added": added, "already": already,
            "missing": missing, "playlist": meta}


def playlist_remove_item(pl_id, item_id):
    if pl_id != LOCAL_PLAYLIST_ID:
        raise KeyError("playlist not found")
    with PLAYLIST_LOCK:
        pl = _local_playlist_locked()
        removed = 1 if pl["rows"].pop(item_id, None) is not None else 0
        pl["items"] = [x for x in pl["items"] if x != item_id]
        pl["updated"] = time.time()
        meta = _playlist_meta(pl)
    save_playlists()
    return {"ok": True, "removed": removed, "playlist": meta}


def playlist_move(pl_id, from_index, to_index):
    if pl_id != LOCAL_PLAYLIST_ID:
        raise KeyError("playlist not found")
    with PLAYLIST_LOCK:
        pl = _local_playlist_locked()
        items = pl["items"]
        if not (0 <= from_index < len(items) and 0 <= to_index < len(items)):
            raise ValueError("Index out of range")
        item = items.pop(from_index)
        items.insert(to_index, item)
        rows = pl["rows"]
        pl["rows"] = {pid: rows[pid] for pid in items}
        pl["updated"] = time.time()
        meta = _playlist_meta(pl)
    save_playlists()
    return {"ok": True, "playlist": meta}


def playlist_clear(pl_id):
    if pl_id != LOCAL_PLAYLIST_ID:
        raise KeyError("playlist not found")
    with PLAYLIST_LOCK:
        pl = _local_playlist_locked()
        pl["rows"] = {}
        pl["items"] = []
        pl["updated"] = time.time()
        meta = _playlist_meta(pl)
    save_playlists()
    return {"ok": True, "playlist": meta}


def playlists_import(items):
    raise ValueError("localPlaylist is the only local playlist")


def playlist_play(pl_id, start_index=0):
    data = playlist_get(pl_id)
    if not data.get("ok"):
        raise KeyError("playlist not found")
    ids = [item["id"] for item in data["items"]]
    if not ids:
        raise ValueError("Playlist is empty")
    SESSION.play_item_list(ids, start_index)
    return {"ok": True, "played": len(ids), "skipped": 0}


def set_queue(item_ids, source="", base=0, meta=None):
    with QUEUE_LOCK:
        QUEUE["items"] = [x for x in item_ids if x]
        QUEUE["source"] = source
        QUEUE["updated"] = time.time()
        QUEUE["base"] = max(0, int(base or 0))
        QUEUE["meta"] = dict(meta or {})


def queue_add(item_ids):
    """Append known library items to the editable Up Next queue."""
    ids = [str(x) for x in (item_ids or []) if x]
    with LIBRARY_LOCK:
        known = {item_id: LIBRARY["items"].get(item_id)
                 for item_id in ids}
    valid = [item_id for item_id in ids if known.get(item_id) is not None]
    missing = len(ids) - len(valid)
    if not valid:
        with QUEUE_LOCK:
            return 0, missing, len(QUEUE_BUILDER["items"])
    with QUEUE_LOCK:
        QUEUE_BUILDER["items"].extend(valid)
        QUEUE_BUILDER["updated"] = time.time()
        total = len(QUEUE_BUILDER["items"])
    return len(valid), missing, total


def queue_clear():
    with QUEUE_LOCK:
        QUEUE_BUILDER["items"] = []
        QUEUE_BUILDER["updated"] = time.time()


def queue_builder_ids():
    with QUEUE_LOCK:
        return list(QUEUE_BUILDER["items"])


def _resolve_items(item_ids, fallback=None):
    with LIBRARY_LOCK:
        by_id = {item_id: LIBRARY["items"].get(item_id)
                 for item_id in item_ids}
    out = []
    for item_id in item_ids:
        it = by_id.get(item_id)
        if it is None and fallback:
            it = fallback.get(item_id)
        if it is not None:
            out.append(song_entry(it))
    return out


def parse_pid_array(blob):
    """A flat array of big-endian u64 MediaItem persistent identifiers."""
    if not blob or len(blob) % 8:
        return None
    return [f"{int.from_bytes(blob[i:i + 8], 'big'):016x}"
            for i in range(0, len(blob), 8)]


def parse_media_item_list(blob):
    """Parse Table 56-17 MediaItemList: count, start index, ordered items."""
    if not blob:
        return None
    params = csm.parse_params(blob)
    count = None
    start = None
    items = []
    for pid, payload in params:
        if pid == 0:
            count = int.from_bytes(payload, "big")
        elif pid == 1:
            start = int.from_bytes(payload, "big")
        elif pid == 2:
            item = parse_media_item(payload)
            if item.get("id"):
                items.append(item)
    if count is not None and count != len(items):
        print(f"MediaItemList: count={count} parsed={len(items)}", flush=True)
    return {"count": count, "start": start or 0, "items": items}


def apply_playlist_ft(fid, blob, ftype=0x0004):
    if ftype == 0x0007:
        parsed = parse_media_item_list(blob)
        ids = [it["id"] for it in parsed["items"]] if parsed else None
    else:
        ids = parse_pid_array(blob)
    if ids is None:
        print(f"PLAYLIST FT id={fid}: bad length {len(blob)}", flush=True)
        return
    with LIBRARY_LOCK:
        matched = False
        for pl in LIBRARY["playlists"].values():
            if pl.get("contained_ft_id") == fid:
                pl["items"] = ids
                matched = True
                print(f"PLAYLIST ITEMS: {pl.get('name')} -> {len(ids)}",
                      flush=True)
        if matched:
            LIBRARY["dirty"] = True
            LIBRARY["version"] += 1
            LIBRARY["_summary"] = None


def apply_queue_ft(fid, blob, ftype):
    """Apply FT 0x0003 (PID array) or 0x0006 (MediaItemList) to the queue."""
    if ftype == 0x0006:
        parsed = parse_media_item_list(blob)
        if parsed is None:
            print(f"QUEUE FT id={fid}: bad MediaItemList", flush=True)
            return
        entries = parsed["items"]
        ids = [it["id"] for it in entries]
        meta = {it["id"]: it for it in entries}
        start = parsed["start"]
    else:
        ids = parse_pid_array(blob)
        meta = {}
        start = 0
        if ids is None:
            print(f"QUEUE FT id={fid}: bad length {len(blob)}", flush=True)
            return
    set_queue(ids, "device", base=start, meta=meta)
    print(f"DEVICE QUEUE: {len(ids)} items, start={start}", flush=True)
    with STATE_LOCK:
        STATE["last_event"] = f"Device queue: {len(ids)} tracks"


def library_device_playlist(pl_id):
    with LIBRARY_LOCK:
        pl = LIBRARY["playlists"].get(pl_id)
        if pl is None:
            return {"ok": False, "error": "device playlist not found"}
        meta = {
            "id": pl.get("id"),
            "name": pl.get("name") or "",
            "tracks": len(pl.get("items", [])),
            "parent_pid": pl.get("parent_pid"),
            "genius": bool(pl.get("genius")),
            "folder": bool(pl.get("folder")),
        }
        ids = list(pl.get("items", []))
    return {"ok": True, "playlist": meta,
            "items": _resolve_items(ids)}


def queue_snapshot(limit=20):
    with QUEUE_LOCK:
        builder_ids = list(QUEUE_BUILDER["items"])
        ids = list(QUEUE["items"])
        source = QUEUE["source"]
        base = QUEUE.get("base", 0)
        meta = dict(QUEUE.get("meta") or {})
    if builder_ids:
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = 20
        window = builder_ids[:max(limit, 50)]
        return {
            "source": "queued",
            "index": 0,
            "base_index": 0,
            "total": len(builder_ids),
            "items": _resolve_items(window),
        }
    with STATE_LOCK:
        np = dict(STATE["now_playing"])
    index = np.get("queue_index")
    if index is None:
        index = base
    index = index - base
    if index < 0 or index >= len(ids):
        index = 0
    # Show the current item first, then up to `limit` upcoming items.
    start = index
    window = ids[start:start + max(1, limit)]
    items = _resolve_items(window, fallback=meta)
    return {
        "source": source,
        "index": index,
        "base_index": base,
        "total": len(ids),
        "items": items,
    }


def history_add(item_id, np=None):
    """Append a single track-change record to the per-device history log."""
    if not item_id:
        return
    with LIBRARY_LOCK:
        it = LIBRARY["items"].get(item_id)
    if it is None:
        return
    now = time.time()
    with QUEUE_LOCK:
        if (LAST_HISTORY["id"] == item_id
                and now - LAST_HISTORY["ts"] < 30):
            return
        LAST_HISTORY["id"] = item_id
        LAST_HISTORY["ts"] = now
    entry = _row_for_item(it)
    entry["ts"] = now
    if np:
        entry["playback_status"] = np.get("status")
        entry["elapsed_ms"] = np.get("elapsed_ms")
        entry["queue_index"] = np.get("queue_index")
        entry["queue_count"] = np.get("queue_count")
    device_store.append_history_log(_library_id(), entry)


def record_now_playing(np):
    """Record each track change to history.log, with or without a queue."""
    title = (np.get("title") or "").strip()
    artist = (np.get("artist") or "").strip()
    album = (np.get("album") or "").strip()
    if not title:
        return
    with QUEUE_LOCK:
        ids = list(QUEUE["items"])
    if ids:
        for it in _resolve_items(ids):
            if ((it.get("title") or "").strip() == title
                    and (it.get("artist") or "").strip() == artist):
                history_add(it["id"], np)
                return
    now = time.time()
    key = ("now-playing", title, artist, album)
    with QUEUE_LOCK:
        if (LAST_HISTORY["id"] == key
                and now - LAST_HISTORY["ts"] < 30):
            return
        LAST_HISTORY["id"] = key
        LAST_HISTORY["ts"] = now
    entry = {
        "id": None,
        "title": title,
        "artist": artist,
        "album": album,
        "ts": now,
        "playback_status": np.get("status"),
        "elapsed_ms": np.get("elapsed_ms"),
        "app": np.get("app"),
    }
    device_store.append_history_log(_library_id(), entry)


def history_recent(limit=100):
    return []


def history_top(limit=100):
    return []


def item_ids_for_album(pid):
    with LIBRARY_LOCK:
        rows = [it for it in LIBRARY["items"].values()
                if it.get("album_pid") == pid]
    rows.sort(key=lambda it: (it.get("disc") or 1,
                              it.get("track") or 0,
                              (it.get("title") or "").lower()))
    return [it["id"] for it in rows if it.get("id")]


def item_ids_for_artist(pid):
    with LIBRARY_LOCK:
        rows = [it for it in LIBRARY["items"].values()
                if it.get("artist_pid") == pid]
    rows.sort(key=lambda it: ((it.get("album") or "").lower(),
                              it.get("disc") or 1,
                              it.get("track") or 0))
    return [it["id"] for it in rows if it.get("id")]


def item_ids_for_genre(name):
    with LIBRARY_LOCK:
        rows = [it for it in LIBRARY["items"].values()
                if (it.get("genre") or "").strip() == name]
    rows.sort(key=lambda it: ((it.get("album") or "").lower(),
                              it.get("disc") or 1,
                              it.get("track") or 0))
    return [it["id"] for it in rows if it.get("id")]


def item_ids_for_album_artist(name):
    with LIBRARY_LOCK:
        rows = [it for it in LIBRARY["items"].values()
                if (it.get("albumArtist") or "").strip() == name]
    rows.sort(key=lambda it: ((it.get("album") or "").lower(),
                              it.get("disc") or 1,
                              it.get("track") or 0))
    return [it["id"] for it in rows if it.get("id")]


def _artwork_cache_path(artwork_id):
    return os.path.join(_now_playing_dir(), f"{artwork_id}.jpg")


def load_cached_artwork(artwork_id):
    if artwork_id is None:
        return False
    path = _artwork_cache_path(artwork_id)
    try:
        with open(path, "rb") as fh:
            blob = fh.read()
    except OSError:
        return False
    if not blob or not blob.startswith(b"\xff\xd8"):
        return False
    with STATE_LOCK:
        STATE["artwork_bytes"] = blob
        STATE["artwork_ready_id"] = artwork_id
        STATE["artwork_mime"] = "image/jpeg"
        STATE["last_event"] = f"Artwork cached id={artwork_id} ({len(blob)} bytes)"
    return True


def save_artwork_cache(artwork_id, blob):
    if artwork_id is None or not blob:
        return
    try:
        cache_dir = _now_playing_dir()
        os.makedirs(cache_dir, exist_ok=True)
        path = _artwork_cache_path(artwork_id)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, path)
        # Keep the cache bounded (oldest by mtime).
        files = [os.path.join(cache_dir, name)
                 for name in os.listdir(cache_dir)
                 if name.endswith(".jpg")]
        if len(files) > 128:
            files.sort(key=lambda p: os.path.getmtime(p))
            for old in files[:-128]:
                try:
                    os.unlink(old)
                except OSError:
                    pass
    except OSError as exc:
        print(f"artwork cache save failed: {exc}")


def _adopt_library_data(data, library_id):
    items = data.get("items", {})
    playlists = data.get("playlists", {})
    seen = dict(data.get("seen") or {})
    next_seq = int(data.get("next_seen_seq") or 1)
    first_seen = {}
    max_seq = 0
    for item_id, item in items.items():
        item.setdefault("id", item_id)
        seq = int(item.get("first_seen_seq") or seen.get(item_id) or 1)
        item["first_seen_seq"] = seq
        seen[item_id] = seq
        ts = item.get("first_seen") or 0.0
        item["first_seen"] = ts
        first_seen[item_id] = ts
        max_seq = max(max_seq, seq)
    if next_seq <= max_seq:
        next_seq = max_seq + 1
    with LIBRARY_LOCK:
        LIBRARY["library_id"] = library_id
        LIBRARY["revision"] = data.get("revision")
        LIBRARY["items"] = items
        LIBRARY["playlists"] = playlists
        LIBRARY["first_seen"] = first_seen
        LIBRARY["first_seen_seq"] = seen
        LIBRARY["next_seen_seq"] = next_seq
        LIBRARY["batch_seq"] = None
        LIBRARY["batch_last"] = 0.0
        LIBRARY["paused"] = False
        LIBRARY["dirty"] = False
        LIBRARY["version"] += 1
        LIBRARY["_summary"] = None
        LIBRARY["_artists"] = None
        LIBRARY["_songs"] = None
        LIBRARY["_genres"] = None
        LIBRARY["_album_artists"] = None


def load_library():
    lid = device_store.active_library_id()
    if lid:
        device_store.migrate_legacy(lid)
        if device_store.iap2_exists(lid):
            data = device_store.load_iap2(lid)
            _adopt_library_data(data, lid)
            print(f"media library loaded: {len(data['items'])} items, "
                  f"revision {data.get('revision')}, library {lid}")
            return
    try:
        with open(LIBRARY_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return
    except Exception as exc:  # noqa: BLE001
        print(f"media library load failed: {exc}")
        return
    # Legacy global mirror; it is migrated to a per-device iap2.db as soon as
    # the first iAP2 library UUID is known.
    for item in data.get("items", {}).values():
        item.setdefault("first_seen_seq", 1)
    data.setdefault("next_seen_seq", 2)
    _adopt_library_data(data, None)
    print(f"media library loaded (legacy): {len(data.get('items', {}))} "
          f"items, revision {data.get('revision')}")


def switch_library(library_id, name=None):
    library_id = device_store.base_library_id(library_id)
    if not library_id:
        return
    if LIBRARY.get("library_id") == library_id:
        return
    save_library()
    device_store.migrate_legacy(library_id)
    device_store.set_active(library_id, name=name)
    data = device_store.load_iap2(library_id)
    with STATE_LOCK:
        MEDIA_DATES.update({"mtime": 0.0, "dates": {}, "loaded": False})
        MEDIA_ARTWORK.update({"mtime": 0.0, "items": {}, "albums": {},
                              "loaded": False, "files": 0})
        STATE["artwork_bytes"] = b""
        STATE["artwork_ready_id"] = None
        STATE["artwork_mime"] = "image/jpeg"
        STATE["library_revision"] = None
        np = STATE["now_playing"]
        for key in list(np.keys()):
            np[key] = None
        np.update({"title": "", "album": "", "artist": "",
                   "album_artist": "", "genre": "", "composer": "",
                   "duration_ms": 0, "elapsed_ms": 0, "app": "",
                   "updated_at": 0.0})
    _adopt_library_data(data, library_id)
    with QUEUE_LOCK:
        LAST_HISTORY["id"] = None
        LAST_HISTORY["ts"] = 0.0
    load_playlists()
    load_media_artwork()
    print(f"active library: {library_id} ({name or 'unknown'}), "
          f"{len(data['items'])} items", flush=True)


def load_media_dates(force=False):
    """Load dates from the MediaLibrary SQLite cache.

    The iAP2 service never talks to libimobiledevice/ifuse; it only reads the
    normalized cache written by media_db_import.py.
    """
    try:
        cache_path = _media_cache_path()
        stat = os.stat(cache_path)
    except OSError:
        with STATE_LOCK:
            MEDIA_DATES["loaded"] = False
        return False
    with STATE_LOCK:
        if not force and stat.st_mtime <= MEDIA_DATES["mtime"]:
            return False
    try:
        conn = media_cache.open_db(cache_path)
        try:
            dates = media_cache.load_dates(conn)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        print(f"media date cache load failed: {exc}")
        return False
    with LIBRARY_LOCK:
        count = 0
        for pid, ts in dates.items():
            it = LIBRARY["items"].get(pid)
            if it is None:
                continue
            try:
                it["date_added"] = float(ts)
                it["date_added_source"] = "MediaLibrary.sqlitedb"
                count += 1
            except (TypeError, ValueError):
                pass
        if count:
            LIBRARY["version"] += 1
            LIBRARY["_summary"] = None
            LIBRARY["_songs"] = None
            LIBRARY["_artists"] = None
            LIBRARY["_genres"] = None
            LIBRARY["_album_artists"] = None
    with STATE_LOCK:
        MEDIA_DATES["mtime"] = stat.st_mtime
        MEDIA_DATES["dates"] = dates
        MEDIA_DATES["loaded"] = True
    print(f"media date cache loaded: {count} matched items")
    return True


def load_media_artwork(force=False):
    """Load artwork mappings from the SQLite cache."""
    try:
        cache_path = _media_cache_path()
        stat = os.stat(cache_path)
    except OSError:
        with STATE_LOCK:
            MEDIA_ARTWORK["loaded"] = False
        return False
    with STATE_LOCK:
        if not force and stat.st_mtime <= MEDIA_ARTWORK["mtime"]:
            return False
    try:
        conn = media_cache.open_db(cache_path)
        try:
            items, albums = media_cache.load_artwork(conn)
            file_count = media_cache.artwork_file_count(conn)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        print(f"media artwork cache load failed: {exc}")
        return False
    with STATE_LOCK:
        MEDIA_ARTWORK["mtime"] = stat.st_mtime
        MEDIA_ARTWORK["loaded"] = True
        MEDIA_ARTWORK["files"] = file_count
    MEDIA_ARTWORK["items"] = items
    MEDIA_ARTWORK["albums"] = albums
    print(f"media artwork cache loaded: {len(items)} items, "
          f"{len(albums)} albums, {file_count} files")
    return True


def save_library():
    with LIBRARY_LOCK:
        if not LIBRARY["dirty"]:
            return
        LIBRARY["dirty"] = False
        library_id = LIBRARY["library_id"]
        revision = LIBRARY["revision"]
        items = LIBRARY["items"]
        playlists = LIBRARY["playlists"]
        next_seq = LIBRARY["next_seen_seq"]
        first_seen = LIBRARY["first_seen"]
        seen = LIBRARY["first_seen_seq"]
        if library_id:
            try:
                device_store.save_iap2(
                    library_id, items, playlists, revision=revision,
                    next_seen_seq=next_seq, seen=seen)
                print(f"media library saved: {len(items)} items -> "
                      f"{device_store.paths(library_id)['iap2_db']}")
            except Exception as exc:  # noqa: BLE001
                print(f"media library save failed: {exc}")
                LIBRARY["dirty"] = True
            return
        payload = {
            "revision": revision,
            "items": items,
            "playlists": playlists,
            "first_seen": first_seen,
            "first_seen_seq": seen,
            "next_seen_seq": next_seq,
        }
    tmp = LIBRARY_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, LIBRARY_PATH)
        print(f"media library saved (legacy): {len(payload['items'])} items")
    except Exception as exc:  # noqa: BLE001
        print(f"media library save failed: {exc}")


def load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return
    except Exception as exc:  # noqa: BLE001
        print(f"settings load failed: {exc}")
        return
    with SETTINGS_LOCK:
        SETTINGS["auto_switch_iap2"] = bool(
            data.get("auto_switch_iap2", False))


def save_settings():
    with SETTINGS_LOCK:
        payload = dict(SETTINGS)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_PATH)


def settings_snapshot():
    with SETTINGS_LOCK:
        return dict(SETTINGS)


def media_cache_state():
    with STATE_LOCK:
        return {
            "dates_loaded": bool(MEDIA_DATES["loaded"]),
            "dates_count": len(MEDIA_DATES["dates"]),
            "artwork_loaded": bool(MEDIA_ARTWORK["loaded"]),
            "artwork_items": len(MEDIA_ARTWORK["items"]),
            "artwork_albums": len(MEDIA_ARTWORK["albums"]),
            "artwork_files": int(MEDIA_ARTWORK["files"]),
            "cache_db": os.path.exists(_media_cache_path()),
            "snapshot": os.path.exists(_snapshot_db()),
            "library_id": _library_id(),
        }


def load_media_cache(target="all"):
    """Load the on-disk cache into memory; never talks to the device."""
    result = {}
    with CACHE_LOCK:
        if target in ("library", "all"):
            result["library"] = load_media_dates(force=True)
        if target in ("artwork", "all"):
            result["artwork"] = load_media_artwork(force=True)
    return result


def _count_files(path):
    if not os.path.isdir(path):
        return 0
    total = 0
    for _, _, names in os.walk(path):
        total += len(names)
    return total


def clear_local_cache():
    """Delete the AFC-derived database/cache files and unload them."""
    with CACHE_LOCK:
        with STATE_LOCK:
            dates_count = len(MEDIA_DATES["dates"])
            items_count = len(MEDIA_ARTWORK["items"])
            albums_count = len(MEDIA_ARTWORK["albums"])
        cache_path = _media_cache_path()
        artwork_dir = _artwork_dir()
        snapshot_dir = _snapshot_dir()
        stats = {
            "library_id": _library_id(),
            "dates_cleared": dates_count,
            "artwork_items_cleared": items_count,
            "artwork_albums_cleared": albums_count,
            "artwork_files": _count_files(artwork_dir),
            "snapshot_files": _count_files(snapshot_dir),
            "cache_db_removed": False,
        }

        with LIBRARY_LOCK:
            for item in LIBRARY["items"].values():
                item.pop("date_added", None)
                item.pop("date_added_source", None)
            LIBRARY["version"] += 1
            LIBRARY["_summary"] = None
            LIBRARY["_songs"] = None
            LIBRARY["_artists"] = None
            LIBRARY["_genres"] = None
            LIBRARY["_album_artists"] = None
        with STATE_LOCK:
            MEDIA_DATES.update({"mtime": 0.0, "dates": {},
                                "loaded": False})
            MEDIA_ARTWORK.update({"mtime": 0.0, "items": {}, "albums": {},
                                  "loaded": False, "files": 0})
            STATE["artwork_bytes"] = b""
            STATE["artwork_ready_id"] = None
            STATE["artwork_mime"] = "image/jpeg"
            STATE["last_event"] = "Local MediaLibrary cache cleared"

        delete_paths = [cache_path, cache_path + "-wal",
                        cache_path + "-shm",
                        MEDIA_CACHE_PATH, MEDIA_CACHE_PATH + "-wal",
                        MEDIA_CACHE_PATH + "-shm",
                        MEDIA_DATES_PATH, MEDIA_ARTWORK_PATH]
        for path in delete_paths:
            try:
                os.unlink(path)
                if path == cache_path:
                    stats["cache_db_removed"] = True
            except FileNotFoundError:
                pass
            except OSError as exc:
                print(f"cache delete {path}: {exc}")

        for path in {artwork_dir, MEDIA_ARTWORK_DIR}:
            shutil.rmtree(path, ignore_errors=True)
            os.makedirs(path, exist_ok=True)

        if os.path.isdir(snapshot_dir):
            for name in os.listdir(snapshot_dir):
                path = os.path.join(snapshot_dir, name)
                if os.path.isfile(path):
                    try:
                        os.unlink(path)
                    except OSError as exc:
                        print(f"cache delete {path}: {exc}")
        return stats


def clear_iap2_library():
    """Clear the iAP2 mirror and let the protocol perform a full resync."""
    with LIBRARY_LOCK:
        stats = {
            "items_cleared": len(LIBRARY["items"]),
            "playlists_cleared": len(LIBRARY["playlists"]),
            "library_id": LIBRARY["library_id"],
        }
        LIBRARY["items"].clear()
        LIBRARY["playlists"].clear()
        LIBRARY["first_seen"].clear()
        LIBRARY["first_seen_seq"].clear()
        LIBRARY["revision"] = None
        LIBRARY["next_seen_seq"] = 1
        LIBRARY["batch_seq"] = None
        LIBRARY["batch_last"] = 0.0
        LIBRARY["dirty"] = False
        LIBRARY["version"] += 1
        LIBRARY["_summary"] = None
        LIBRARY["_songs"] = None
        LIBRARY["_artists"] = None
        LIBRARY["_genres"] = None
        LIBRARY["_album_artists"] = None
        library_id = LIBRARY["library_id"]
    if library_id:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(str(device_store.paths(library_id)["iap2_db"]) +
                          suffix)
            except OSError:
                pass
    else:
        try:
            os.unlink(LIBRARY_PATH)
        except OSError:
            pass
    with STATE_LOCK:
        STATE["library_revision"] = None
        STATE["last_event"] = "iAP2 media library cleared; resyncing"
    conn = SESSION.conn
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return stats


def clear_iap2_artwork():
    cache_dir = _now_playing_dir()
    stats = {"now_playing_files": _count_files(cache_dir)}
    shutil.rmtree(cache_dir, ignore_errors=True)
    os.makedirs(cache_dir, exist_ok=True)
    with STATE_LOCK:
        STATE["artwork_bytes"] = b""
        STATE["artwork_ready_id"] = None
        STATE["artwork_mime"] = "image/jpeg"
        STATE["last_event"] = "iAP2 artwork cache cleared"
    return stats


def library_summary():
    with LIBRARY_LOCK:
        version = LIBRARY["version"]
        revision = LIBRARY["revision"]
        syncing = (LIBRARY["syncing"]
                   and time.time() - LIBRARY["last_update"] < 8)
        if (LIBRARY["_summary"] is not None
                and LIBRARY["_summary_version"] == version):
            out = dict(LIBRARY["_summary"])
            out["syncing"] = syncing
            out["revision"] = revision
            return out
        items = list(LIBRARY["items"].values())
        playlists = len(LIBRARY["playlists"])
    # Group by album persistent ID first (many items carry the PID but
    # omit the album title); pick the most common title/artist in each
    # group. Items without a PID fall back to (artist, title).
    groups = {}
    for it in items:
        pid = it.get("album_pid")
        if not pid:
            continue
        g = groups.setdefault(pid, {
            "titles": {}, "artists": {}, "tracks": 0, "date_added": 0.0,
            "first_seen_seq": None, "last_seen_seq": 0})
        g["tracks"] += 1
        g["date_added"] = max(g["date_added"], it.get("date_added") or 0.0)
        seq = int(it.get("first_seen_seq") or 0)
        if seq:
            g["first_seen_seq"] = (seq if g["first_seen_seq"] is None
                                   else min(g["first_seen_seq"], seq))
            g["last_seen_seq"] = max(g["last_seen_seq"], seq)
        title = (it.get("album") or "").strip()
        if title:
            g["titles"][title] = g["titles"].get(title, 0) + 1
        artist = it.get("albumArtist") or it.get("artist") or ""
        if artist:
            g["artists"][artist] = g["artists"].get(artist, 0) + 1
    albums = {}
    album_by_name = {}
    for pid, g in groups.items():
        if not g["titles"]:
            continue
        title = max(g["titles"], key=g["titles"].get)
        artist = (max(g["artists"], key=g["artists"].get)
                  if g["artists"] else "")
        albums[pid] = {"pid": pid, "artist": artist, "album": title,
                       "tracks": g["tracks"],
                       "date_added": g["date_added"],
                       "first_seen_seq": g["first_seen_seq"] or 0,
                       "last_seen_seq": g["last_seen_seq"] or 0}
        album_by_name.setdefault((artist, title), pid)
    for it in items:
        if it.get("album_pid"):
            continue
        album = (it.get("album") or "").strip()
        if not album:
            continue
        artist = it.get("albumArtist") or it.get("artist") or ""
        pid = album_by_name.get((artist, album))
        if pid:
            albums[pid]["tracks"] += 1
            albums[pid]["date_added"] = max(
                albums[pid].get("date_added") or 0.0,
                it.get("date_added") or 0.0)
            seq = int(it.get("first_seen_seq") or 0)
            if seq:
                prev = int(albums[pid].get("first_seen_seq") or 0)
                albums[pid]["first_seen_seq"] = (
                    seq if not prev else min(prev, seq))
                albums[pid]["last_seen_seq"] = max(
                    int(albums[pid].get("last_seen_seq") or 0), seq)
            continue
        key = ("name", artist, album)
        entry = albums.get(key)
        if entry is None:
            entry = {"pid": None, "artist": artist, "album": album,
                     "tracks": 0, "date_added": 0.0,
                     "first_seen_seq": 0, "last_seen_seq": 0}
            albums[key] = entry
        entry["tracks"] += 1
        entry["date_added"] = max(entry.get("date_added") or 0.0,
                                  it.get("date_added") or 0.0)
        seq = int(it.get("first_seen_seq") or 0)
        if seq:
            prev = int(entry.get("first_seen_seq") or 0)
            entry["first_seen_seq"] = seq if not prev else min(prev, seq)
            entry["last_seen_seq"] = max(
                int(entry.get("last_seen_seq") or 0), seq)
    summary = {
        "version": version,
        "track_count": len(items),
        "album_count": len(albums),
        "playlist_count": playlists,
        "revision": revision,
        "syncing": syncing,
        "albums": sorted(
            albums.values(),
            key=lambda a: ((a["album"] or "").lower(), a["artist"] or "")),
    }
    with LIBRARY_LOCK:
        LIBRARY["_summary"] = summary
        LIBRARY["_summary_version"] = version
    return dict(summary)


def library_album(pid=None, artist=None, album=None):
    """Return the tracks of one album (by PID, or by artist/title)."""
    with LIBRARY_LOCK:
        items = list(LIBRARY["items"].values())
    tracks = []
    for it in items:
        if pid:
            if it.get("album_pid") != pid:
                continue
        else:
            if (it.get("album") or "").strip() != (album or "").strip():
                continue
            if artist and (
                    (it.get("albumArtist") or it.get("artist") or "")
                    != artist):
                continue
        tracks.append({
            "id": it.get("id"),
            "title": it.get("title") or "",
            "artist": it.get("artist") or "",
            "album": it.get("album") or "",
            "duration_ms": it.get("duration_ms") or 0,
            "track": it.get("track") or 0,
            "disc": it.get("disc") or 1,
            "first_seen": it.get("first_seen") or 0.0,
            "first_seen_seq": it.get("first_seen_seq") or 1,
            "date_added": it.get("date_added"),
        })
    tracks.sort(key=lambda t: (t["disc"], t["track"],
                               (t["title"] or "").lower()))
    meta = None
    if pid:
        summary = library_summary()
        meta = next((a for a in summary["albums"]
                     if a.get("pid") == pid), None)
    if meta is None:
        meta = {"pid": pid, "artist": artist or "", "album": album or "",
                "tracks": len(tracks)}
        if tracks:
            first = next((it for it in items
                          if it.get("id") == tracks[0]["id"]), None)
            if first:
                meta["album"] = ((first.get("album") or "").strip()
                                 or meta["album"])
                meta["artist"] = (
                    (first.get("albumArtist") or first.get("artist") or "")
                    or meta["artist"])
    return {"ok": True, "album": meta, "items": tracks}


def song_entry(it):
    return {
        "id": it.get("id"),
        "title": (it.get("title") or "").strip(),
        "artist": (it.get("artist") or "").strip(),
        "album": (it.get("album") or "").strip(),
        "album_artist": (it.get("albumArtist") or "").strip(),
        "album_pid": it.get("album_pid"),
        "artist_pid": it.get("artist_pid"),
        "genre": (it.get("genre") or "").strip(),
        "composer": (it.get("composer") or "").strip(),
        "rating": it.get("rating") or 0,
        "media_type": it.get("media_type"),
        "compilation": it.get("compilation"),
        "duration_ms": it.get("duration_ms") or 0,
        "track": it.get("track") or 0,
        "disc": it.get("disc") or 1,
        "first_seen": it.get("first_seen") or 0.0,
        "first_seen_seq": it.get("first_seen_seq") or 1,
        "date_added": it.get("date_added"),
        "date_added_source": it.get("date_added_source"),
    }


def library_artists(query="", limit=200, offset=0):
    """Artists grouped by persistent ID, with optional search/pagination."""
    with LIBRARY_LOCK:
        version = LIBRARY["version"]
        if (LIBRARY["_artists"] is not None
                and LIBRARY["_artists_version"] == version):
            artists = LIBRARY["_artists"]
        else:
            artists = None
            items = list(LIBRARY["items"].values())
    if artists is None:
        groups = {}
        for it in items:
            name = (it.get("artist") or it.get("albumArtist") or "").strip()
            pid = it.get("artist_pid")
            if not name and not pid:
                continue
            key = pid or ("name", name)
            g = groups.get(key)
            if g is None:
                g = {"pid": pid, "name": name, "tracks": 0,
                     "albums": set()}
                groups[key] = g
            # Prefer a non-empty name; some rows carry only the PID.
            if name and (not g["name"] or len(name) > len(g["name"])):
                g["name"] = name
            g["tracks"] += 1
            if it.get("album_pid"):
                g["albums"].add(it["album_pid"])
        artists = [
            {"pid": g["pid"], "name": g["name"] or "Unknown artist",
             "tracks": g["tracks"], "albums": len(g["albums"])}
            for g in groups.values()
        ]
        artists.sort(key=lambda a: (a["name"].lower(), a["pid"] or ""))
        with LIBRARY_LOCK:
            LIBRARY["_artists"] = artists
            LIBRARY["_artists_version"] = version
    q = (query or "").strip().lower()
    matches = ([a for a in artists if q in a["name"].lower()]
               if q else artists)
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 200
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    return {"total": len(matches), "offset": offset, "limit": limit,
            "artists": matches[offset:offset + limit]}


def library_artist(pid=None, name=None):
    """Albums (and a track count) for one artist."""
    with LIBRARY_LOCK:
        items = list(LIBRARY["items"].values())
    tracks = []
    display_name = name or ""
    for it in items:
        if pid:
            if it.get("artist_pid") != pid:
                continue
        elif name:
            if (it.get("artist") or it.get("albumArtist") or "") != name:
                continue
        else:
            continue
        tracks.append(it)
        track_name = (it.get("artist") or it.get("albumArtist") or "").strip()
        if track_name and not display_name:
            display_name = track_name
    albums = {}
    for it in tracks:
        album = (it.get("album") or "").strip()
        apid = it.get("album_pid")
        if not album or not apid:
            continue
        entry = albums.get(apid)
        if entry is None:
            entry = {"pid": apid,
                     "artist": it.get("albumArtist") or it.get("artist") or "",
                     "album": album, "tracks": 0}
            albums[apid] = entry
        entry["tracks"] += 1
    album_list = sorted(albums.values(),
                        key=lambda a: ((a["album"] or "").lower(),
                                       a["artist"] or ""))
    meta = {"pid": pid, "name": display_name or name or "Unknown artist",
            "tracks": len(tracks), "albums": len(albums)}
    return {"ok": True, "artist": meta, "albums": album_list}


def library_genres(query="", limit=200, offset=0):
    with LIBRARY_LOCK:
        version = LIBRARY["version"]
        if (LIBRARY["_genres"] is not None
                and LIBRARY["_genres_version"] == version):
            genres = LIBRARY["_genres"]
        else:
            genres = None
            items = list(LIBRARY["items"].values())
    if genres is None:
        groups = {}
        for it in items:
            name = (it.get("genre") or "").strip()
            if not name:
                continue
            g = groups.setdefault(name, {"name": name, "tracks": 0,
                                         "albums": set()})
            g["tracks"] += 1
            if it.get("album_pid"):
                g["albums"].add(it["album_pid"])
        genres = [
            {"name": g["name"], "tracks": g["tracks"],
             "albums": len(g["albums"])}
            for g in groups.values()
        ]
        genres.sort(key=lambda g: (g["name"].lower(),))
        with LIBRARY_LOCK:
            LIBRARY["_genres"] = genres
            LIBRARY["_genres_version"] = version
    q = (query or "").strip().lower()
    matches = ([g for g in genres if q in g["name"].lower()]
               if q else genres)
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 200
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    return {"total": len(matches), "offset": offset, "limit": limit,
            "genres": matches[offset:offset + limit]}


def library_genre(name):
    with LIBRARY_LOCK:
        items = [it for it in LIBRARY["items"].values()
                 if (it.get("genre") or "").strip() == name]
    albums = {}
    for it in items:
        album = (it.get("album") or "").strip()
        apid = it.get("album_pid")
        if not album or not apid:
            continue
        entry = albums.setdefault(apid, {
            "pid": apid,
            "artist": it.get("albumArtist") or it.get("artist") or "",
            "album": album, "tracks": 0})
        entry["tracks"] += 1
    album_list = sorted(albums.values(),
                        key=lambda a: ((a["album"] or "").lower(),
                                       a["artist"] or ""))
    return {"ok": True,
            "genre": {"name": name, "tracks": len(items),
                      "albums": len(albums)},
            "albums": album_list}


def library_album_artists(query="", limit=200, offset=0):
    with LIBRARY_LOCK:
        version = LIBRARY["version"]
        if (LIBRARY["_album_artists"] is not None
                and LIBRARY["_album_artists_version"] == version):
            artists = LIBRARY["_album_artists"]
        else:
            artists = None
            items = list(LIBRARY["items"].values())
    if artists is None:
        groups = {}
        for it in items:
            name = (it.get("albumArtist") or "").strip()
            if not name:
                continue
            g = groups.setdefault(name, {"name": name, "tracks": 0,
                                         "albums": set()})
            g["tracks"] += 1
            if it.get("album_pid"):
                g["albums"].add(it["album_pid"])
        artists = [
            {"name": g["name"], "tracks": g["tracks"],
             "albums": len(g["albums"])}
            for g in groups.values()
        ]
        artists.sort(key=lambda a: (a["name"].lower(),))
        with LIBRARY_LOCK:
            LIBRARY["_album_artists"] = artists
            LIBRARY["_album_artists_version"] = version
    q = (query or "").strip().lower()
    matches = ([a for a in artists if q in a["name"].lower()]
               if q else artists)
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 200
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    return {"total": len(matches), "offset": offset, "limit": limit,
            "album_artists": matches[offset:offset + limit]}


def library_album_artist(name):
    with LIBRARY_LOCK:
        items = [it for it in LIBRARY["items"].values()
                 if (it.get("albumArtist") or "").strip() == name]
    albums = {}
    for it in items:
        album = (it.get("album") or "").strip()
        apid = it.get("album_pid")
        if not album or not apid:
            continue
        entry = albums.setdefault(apid, {
            "pid": apid, "artist": name,
            "album": album, "tracks": 0})
        entry["tracks"] += 1
    album_list = sorted(albums.values(),
                        key=lambda a: ((a["album"] or "").lower(),
                                       a["artist"] or ""))
    return {"ok": True,
            "album_artist": {"name": name, "tracks": len(items),
                             "albums": len(albums)},
            "albums": album_list}


def library_songs(query="", limit=100, offset=0, sort="title",
                  genre="", album_artist=""):
    """Paginated song list with search, filters and sorting."""
    with LIBRARY_LOCK:
        version = LIBRARY["version"]
        if (LIBRARY["_songs"] is None
                or LIBRARY["_songs_version"] != version):
            entries = []
            for it in LIBRARY["items"].values():
                e = song_entry(it)
                e["_search"] = (
                    (e["title"] + " " + e["artist"] + " " + e["album"] +
                     " " + e["album_artist"] + " " + e["genre"])
                    .lower())
                entries.append(e)
            entries.sort(
                key=lambda e: ((e["title"] or "").lower(),
                               (e["artist"] or "").lower()))
            LIBRARY["_songs"] = entries
            LIBRARY["_songs_version"] = version
        entries = LIBRARY["_songs"]
    q = (query or "").strip().lower()
    matches = entries
    if q:
        matches = [e for e in matches if q in e["_search"]]
    if genre:
        matches = [e for e in matches if e["genre"] == genre]
    if album_artist:
        matches = [e for e in matches
                   if e["album_artist"] == album_artist]
    sort = (sort or "title").lower()
    if sort == "recent":
        matches = sorted(matches, key=lambda e: (
            -(e.get("first_seen_seq") or 1),
            -(e.get("date_added") or 0),
            -(e.get("first_seen") or 0),
            (e["title"] or "").lower()))
    elif sort == "artist":
        matches = sorted(matches, key=lambda e: (
            (e["artist"] or "").lower(),
            (e["album"] or "").lower(),
            e["disc"], e["track"]))
    elif sort == "album":
        matches = sorted(matches, key=lambda e: (
            (e["album"] or "").lower(), e["disc"], e["track"],
            (e["title"] or "").lower()))
    elif sort == "duration":
        matches = sorted(matches, key=lambda e: (
            e["duration_ms"], (e["title"] or "").lower()))
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 100
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    total = len(matches)
    page = matches[offset:offset + limit]
    items = []
    for e in page:
        item = dict(e)
        item.pop("_search", None)
        items.append(item)
    return {"total": total, "offset": offset, "limit": limit,
            "sort": sort, "items": items}


def library_recent(limit=100, offset=0, query=""):
    """Best-effort 'recently added' — iAP2 has no date-added field.

    Items first observed after this feature was enabled carry a real
    first_seen timestamp; older items carry 0.  The response says whether
    it is only an estimate / empty.
    """
    with LIBRARY_LOCK:
        rows = [it for it in LIBRARY["items"].values()
                if (it.get("date_added") or 0) > 0
                or (it.get("first_seen") or 0) > 0]
    q = (query or "").strip().lower()
    if q:
        rows = [it for it in rows if q in (
            (it.get("title") or "") + " " + (it.get("artist") or "") + " " +
            (it.get("album") or "") + " " +
            (it.get("album_artist") or "") + " " +
            (it.get("genre") or "")).lower()]
    rows.sort(key=lambda it: (-(it.get("first_seen_seq") or 1),
                              -(it.get("date_added") or 0),
                              -(it.get("first_seen") or 0),
                              (it.get("title") or "").lower()))
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 100
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    total = len(rows)
    page = rows[offset:offset + limit]
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [song_entry(it) for it in page],
        "supported": bool(MEDIA_DATES["dates"]),
        "note": (
            "Date added from MediaLibrary.sqlitedb (via separate AFC importer)."
            if MEDIA_DATES["dates"] else
            ("MediaLibrary date cache is not loaded; use Load MediaLibrary."
             if os.path.exists(_media_cache_path()) else
             ("No MediaLibrary cache; sorted by MediaCtrl first-seen time. "
              "Legacy items are hidden."))),
        "estimated": not bool(MEDIA_DATES["dates"]),
    }


def clean_text(payload):
    """Decode a UTF-8 string param, dropping stray control/TLV bytes."""
    text = payload.rstrip(b"\x00").decode("utf-8", "replace")
    return "".join(ch for ch in text if ch >= " ")


def parse_media_item(group):
    fields = {1: "title", 6: "album", 12: "artist", 14: "albumArtist",
              16: "genre", 18: "composer"}
    item = {}
    for cid, child in csm.parse_params(group):
        if cid in fields:
            text = clean_text(child)
            if text:
                item[fields[cid]] = text
        elif cid == 0:
            item["id"] = f"{int.from_bytes(child, 'big'):016x}"
        elif cid == 2 and child:
            item["media_type"] = child[0]
        elif cid == 3 and child:
            item["rating"] = child[0]
        elif cid == 4:
            item["duration_ms"] = int.from_bytes(child, "big")
        elif cid == 5:
            item["album_pid"] = f"{int.from_bytes(child, 'big'):016x}"
        elif cid == 7:
            item["track"] = int.from_bytes(child, "big")
        elif cid == 8:
            item["tracks"] = int.from_bytes(child, "big")
        elif cid == 9:
            item["disc"] = int.from_bytes(child, "big")
        elif cid == 10:
            item["discs"] = int.from_bytes(child, "big")
        elif cid == 11:
            item["artist_pid"] = f"{int.from_bytes(child, 'big'):016x}"
        elif cid == 13:
            item["album_artist_pid"] = f"{int.from_bytes(child, 'big'):016x}"
        elif cid == 15:
            item["genre_pid"] = f"{int.from_bytes(child, 'big'):016x}"
        elif cid == 17:
            item["composer_pid"] = f"{int.from_bytes(child, 'big'):016x}"
        elif cid == 19 and child:
            item["compilation"] = bool(child[0])
        elif cid == 20 and child:
            item["artwork_id"] = child[0]
        elif cid == 25 and child:
            item["extra_25"] = child[0]
    return item


def parse_media_playlist(group):
    pl = {}
    for cid, child in csm.parse_params(group):
        if cid == 0:
            pl["id"] = f"{int.from_bytes(child, 'big'):016x}"
        elif cid == 1:
            pl["name"] = child.rstrip(b"\x00").decode("utf-8", "replace")
        elif cid == 2:
            pl["parent_pid"] = f"{int.from_bytes(child, 'big'):016x}"
        elif cid == 3 and child:
            pl["genius"] = bool(child[0])
        elif cid == 4 and child:
            pl["folder"] = bool(child[0])
        elif cid == 5 and child:
            pl["contained_ft_id"] = child[0]
    return pl


def snapshot():
    """JSON-safe copy of the shared state."""
    with STATE_LOCK:
        np = dict(STATE["now_playing"])
        lib_summary = {
            "track_count": len(LIBRARY["items"]),
            "revision": LIBRARY["revision"],
            "syncing": (LIBRARY["syncing"]
                        and time.time() - LIBRARY["last_update"] < 8),
        }
        out = {
            "connected": STATE["connected"],
            "link_state": STATE["link_state"],
            "now_playing": np,
            "power": dict(STATE["power"]),
            "device": dict(STATE["device"]),
            "library_progress": STATE["library_progress"],
            "library_hiding_remote": STATE["library_hiding_remote"],
            "play_all_songs_capable": STATE["play_all_songs_capable"],
            "has_artwork": bool(STATE["artwork_bytes"]),
            "artwork_ready_id": STATE["artwork_ready_id"],
            "artwork_size": len(STATE["artwork_bytes"]),
            "artwork_mime": STATE["artwork_mime"],
            "libraries": list(STATE["libraries"]),
            "library_revision": STATE["library_revision"],
            "library": lib_summary,
            "last_error": STATE["last_error"],
            "last_event": STATE["last_event"],
        }
    out["queue"] = queue_snapshot(12)
    out["media"] = MEDIA_JOBS.snapshot()
    out["media"]["cache"] = media_cache_state()
    out["settings"] = settings_snapshot()
    return out


def parse_string(payload):
    return payload.rstrip(b"\x00").decode("utf-8", errors="replace")


def parse_u32(payload):
    return int.from_bytes(payload, "big") if payload else None


class Session:
    """Owns one HID/iAP2 session at a time and reconnects on failure."""

    def __init__(self):
        self.loop = None
        self.conn = None
        self._thread = None
        self._stop = threading.Event()
        self._resynced = False
        self._saved_version = -1
        self._product = ""
        self._power_capability = {}
        self._power_updates = True

    # -- lifecycle ---------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

    def stop(self, timeout=8.0):
        """Stop the reconnect loop and release the HID device."""
        self._stop.set()
        conn = self.conn
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None
        with STATE_LOCK:
            STATE["connected"] = False
            STATE["link_state"] = None

    def _thread_main(self):
        while not self._stop.is_set():
            try:
                asyncio.run(self._run_once())
            except Exception as exc:  # noqa: BLE001
                self._set_error(f"{type(exc).__name__}: {exc}")
            finally:
                self.loop = None
                self.conn = None
                with STATE_LOCK:
                    STATE["connected"] = False
            self._stop.wait(2.0)

    def _set_error(self, text):
        with STATE_LOCK:
            STATE["last_error"] = text

    def _event(self, text):
        with STATE_LOCK:
            STATE["last_event"] = text

    async def _run_once(self):
        self.loop = asyncio.get_running_loop()
        self._resynced = False
        set_configuration()
        hid_device = open_hid()
        try:
            self._product = hid_device.get_product_string() or ""
        except Exception:  # noqa: BLE001
            self._product = ""
        # Both tested devices accept the power messages. Remember a rejection
        # per product so an incompatible device is not retried every time.
        self._power_updates = self._power_capability.setdefault(
            self._product, True)
        with STATE_LOCK:
            STATE["power"] = dict(POWER_DEFAULTS)
        reader = hid_async.HIDReader(hid_device, INPUT_REPORTS)
        writer = hid_async.HIDWriter(hid_device, sorted(OUTPUT_REPORTS.items()))
        conn = link_layer.IAP2Connection(
            writer, reader, self.loop, max_outgoing=30
        )
        conn.file_transfer_handler = self._ft_handler
        self.conn = conn
        self._event("link handshake")
        conn.start()
        task = asyncio.create_task(self._handle_control(conn))
        saver = asyncio.create_task(self._periodic_save())
        try:
            while not self._stop.is_set() and conn.state != link_layer.STATE_DEAD:
                with STATE_LOCK:
                    STATE["link_state"] = conn.state
                await asyncio.sleep(0.5)
            if conn.state == link_layer.STATE_DEAD:
                self._event("link lost, reconnecting")
        finally:
            task.cancel()
            saver.cancel()
            writer.close()
            try:
                hid_device.close()
            except Exception:  # noqa: BLE001
                pass
            with LIBRARY_LOCK:
                LIBRARY["syncing"] = False
                LIBRARY["batch_seq"] = None
            save_library()

    async def _periodic_save(self):
        while True:
            await asyncio.sleep(10)
            with LIBRARY_LOCK:
                if (LIBRARY["syncing"]
                        and time.time() - LIBRARY["last_update"] > 60):
                    LIBRARY["syncing"] = False
                    LIBRARY["batch_seq"] = None
                dirty = LIBRARY["dirty"]
                idle = time.time() - LIBRARY["last_change"]
                version = LIBRARY["version"]
            if dirty and idle > 5 and version != self._saved_version:
                save_library()
                self._saved_version = version

    # -- commands ----------------------------------------------------
    def send_command(self, command):
        if command not in HID_BITS:
            raise ValueError(f"unknown command: {command}")
        if not self.loop or not self.conn:
            raise RuntimeError("iPod is not connected")
        fut = asyncio.run_coroutine_threadsafe(
            self._send_command(command), self.loop)
        return fut.result(timeout=5)

    async def _send_command(self, command):
        conn = self.conn
        if conn is None or conn.state != link_layer.STATE_NORMAL:
            raise RuntimeError("Link is not ready")
        await send_hid_command(conn, HID_BITS[command])

    def seek(self, elapsed_ms=None, queue_index=None):
        """0x5003 SetNowPlayingInformation: seek and/or jump in the queue."""
        if elapsed_ms is None and queue_index is None:
            raise ValueError("elapsed_ms or queue_index is required")
        if not self.loop or not self.conn:
            raise RuntimeError("iPod is not connected")
        if elapsed_ms is not None:
            with STATE_LOCK:
                available = STATE["now_playing"].get("seek_available")
            if available is not True:
                raise RuntimeError(
                    "Device has not advertised seek support "
                    "(SetElapsedTimeAvailable is false)")
        fut = asyncio.run_coroutine_threadsafe(
            self._seek(elapsed_ms, queue_index), self.loop)
        result = fut.result(timeout=5)
        with STATE_LOCK:
            if elapsed_ms is not None:
                STATE["now_playing"]["elapsed_ms"] = int(elapsed_ms)
                STATE["now_playing"]["updated_at"] = time.time() * 1000.0
            if queue_index is not None:
                STATE["now_playing"]["queue_index"] = int(queue_index)
        return result

    async def _seek(self, elapsed_ms, queue_index):
        conn = self.conn
        if conn is None or conn.state != link_layer.STATE_NORMAL:
            raise RuntimeError("Link is not ready")
        await send_now_playing_info(
            conn, elapsed_ms=elapsed_ms, queue_index=queue_index)
        self._event(
            f"SetNowPlayingInformation elapsed={elapsed_ms} "
            f"queue_index={queue_index}")

    def _library_uuid(self):
        with STATE_LOCK:
            libs = list(STATE["libraries"])
        local = next(
            (l for l in libs if l.get("uuid", "").endswith("-MPB-15.7.8")),
            libs[0] if libs else None)
        if not local:
            raise RuntimeError("Media library information is not ready")
        return local["uuid"]

    def play_item(self, item_id):
        """Play one media-library item (0x4C07 PlayMediaLibraryItems)."""
        if not self.loop or not self.conn:
            raise RuntimeError("iPod is not connected")
        raw = bytes.fromhex(item_id)
        if len(raw) != 8:
            raise ValueError("Track ID must be 16 hex digits")
        fut = asyncio.run_coroutine_threadsafe(self._play_item(raw),
                                               self.loop)
        result = fut.result(timeout=5)
        set_queue([item_id], "single")
        return result

    async def _play_item(self, raw):
        conn = self.conn
        if conn is None or conn.state != link_layer.STATE_NORMAL:
            raise RuntimeError("Link is not ready")
        uuid = self._library_uuid()
        await send_csm(conn, 0x4C07, [
            csm.bytes_param(0, raw),
            csm.param(1, (0).to_bytes(4, "big")),
            csm.string(2, uuid),
        ])
        self._event(f"Play track {raw.hex()}")

    def play_album(self, album_pid):
        """Play a whole album (0x4C08 PlayMediaLibraryCollection, type 2)."""
        if not self.loop or not self.conn:
            raise RuntimeError("iPod is not connected")
        raw = bytes.fromhex(album_pid)
        if len(raw) != 8:
            raise ValueError("Album ID must be 16 hex digits")
        fut = asyncio.run_coroutine_threadsafe(self._play_album(raw),
                                               self.loop)
        result = fut.result(timeout=5)
        set_queue(item_ids_for_album(album_pid), "album")
        return result

    async def _play_album(self, raw):
        conn = self.conn
        if conn is None or conn.state != link_layer.STATE_NORMAL:
            raise RuntimeError("Link is not ready")
        uuid = self._library_uuid()
        await send_csm(conn, 0x4C08, [
            csm.bytes_param(0, raw),
            csm.param(1, bytes([2])),  # MediaLibraryCollectionTypeAlbum
            csm.param(2, (0).to_bytes(4, "big")),
            csm.string(3, uuid),
        ])
        self._event(f"Play album {raw.hex()}")

    def play_artist(self, artist_pid):
        """Play an artist (0x4C08 PlayMediaLibraryCollection, type 1)."""
        if not self.loop or not self.conn:
            raise RuntimeError("iPod is not connected")
        raw = bytes.fromhex(artist_pid)
        if len(raw) != 8:
            raise ValueError("Artist ID must be 16 hex digits")
        fut = asyncio.run_coroutine_threadsafe(self._play_artist(raw),
                                               self.loop)
        result = fut.result(timeout=5)
        set_queue(item_ids_for_artist(artist_pid), "artist")
        return result

    async def _play_artist(self, raw):
        conn = self.conn
        if conn is None or conn.state != link_layer.STATE_NORMAL:
            raise RuntimeError("Link is not ready")
        uuid = self._library_uuid()
        await send_csm(conn, 0x4C08, [
            csm.bytes_param(0, raw),
            csm.param(1, bytes([1])),  # MediaLibraryCollectionTypeArtist
            csm.param(2, (0).to_bytes(4, "big")),
            csm.string(3, uuid),
        ])
        self._event(f"Play artist {raw.hex()}")

    def send_raw_csm(self, msg_id, params):
        """Experimental: send an arbitrary CSM message (id, [(pid, bytes)])."""
        if not self.loop or not self.conn:
            raise RuntimeError("iPod is not connected")
        fut = asyncio.run_coroutine_threadsafe(
            self._send_raw_csm(msg_id, params), self.loop)
        return fut.result(timeout=5)

    async def _send_raw_csm(self, msg_id, params):
        conn = self.conn
        if conn is None or conn.state != link_layer.STATE_NORMAL:
            raise RuntimeError("Link is not ready")
        await send_csm(conn, msg_id,
                       [csm.param(pid, payload) for pid, payload in params])
        self._event(f"Experimental CSM 0x{msg_id:04x} ({len(params)} params)")

    def play_device_playlist(self, pl_id):
        """Play a device-side playlist via 0x4C08 CollectionType=0."""
        if not self.loop or not self.conn:
            raise RuntimeError("iPod is not connected")
        data = library_device_playlist(pl_id)
        if not data.get("ok"):
            raise KeyError("device playlist not found")
        items = data.get("items") or []
        if not items:
            raise ValueError("Device playlist is empty or not synced yet")
        raw = bytes.fromhex(pl_id)
        fut = asyncio.run_coroutine_threadsafe(
            self._play_device_playlist(raw), self.loop)
        result = fut.result(timeout=5)
        set_queue([item["id"] for item in items], "device-playlist")
        return result

    async def _play_device_playlist(self, raw):
        conn = self.conn
        if conn is None or conn.state != link_layer.STATE_NORMAL:
            raise RuntimeError("Link is not ready")
        uuid = self._library_uuid()
        await send_csm(conn, 0x4C08, [
            csm.bytes_param(0, raw),
            csm.param(1, bytes([0])),  # MediaLibraryCollectionTypePlaylist
            csm.param(2, (0).to_bytes(4, "big")),
            csm.string(3, uuid),
        ])
        self._event(f"Play device playlist {raw.hex()}")

    def play_item_list(self, item_ids, start_index=0, source="list"):
        """Play an arbitrary ordered list of media items (accessory playlist)."""
        if not self.loop or not self.conn:
            raise RuntimeError("iPod is not connected")
        item_ids = [x for x in item_ids if x]
        ids = [bytes.fromhex(x) for x in item_ids]
        if not ids:
            raise RuntimeError("Playlist is empty")
        if len(ids) > 2000:
            raise RuntimeError("Playlist exceeds 2000 tracks and cannot be sent at once")
        blob = b"".join(ids)
        start = max(0, int(start_index))
        fut = asyncio.run_coroutine_threadsafe(
            self._play_item_list(blob, start), self.loop)
        result = fut.result(timeout=5)
        set_queue(item_ids, source)
        return result

    def play_current_selection(self):
        """Resume the device's current selection (0x4C06)."""
        if not self.loop or not self.conn:
            raise RuntimeError("iPod is not connected")
        fut = asyncio.run_coroutine_threadsafe(
            self._play_current_selection(), self.loop)
        return fut.result(timeout=5)

    async def _play_current_selection(self):
        conn = self.conn
        if conn is None or conn.state != link_layer.STATE_NORMAL:
            raise RuntimeError("Link is not ready")
        uuid = self._library_uuid()
        await send_csm(conn, 0x4C06, [csm.string(0, uuid)])
        set_queue([], "current-selection")
        self._event("Play iPod current selection")

    def play_genre(self, name):
        ids = item_ids_for_genre(name)
        if not ids:
            raise RuntimeError("This genre has no tracks")
        if len(ids) > 2000:
            ids = ids[:2000]
        return self.play_item_list(ids, 0, source="genre")

    def play_album_artist(self, name):
        ids = item_ids_for_album_artist(name)
        if not ids:
            raise RuntimeError("This album artist has no tracks")
        if len(ids) > 2000:
            ids = ids[:2000]
        return self.play_item_list(ids, 0, source="album-artist")

    async def _play_item_list(self, blob, start_index):
        conn = self.conn
        if conn is None or conn.state != link_layer.STATE_NORMAL:
            raise RuntimeError("Link is not ready")
        uuid = self._library_uuid()
        await send_csm(conn, 0x4C07, [
            csm.bytes_param(0, blob),
            csm.param(1, start_index.to_bytes(4, "big")),
            csm.string(2, uuid),
        ])
        self._event(f"Play track list ({len(blob) // 8} tracks)")

    def resync_library(self):
        """Drop the stored revision and force a reconnect so the next
        session performs a full media library sync (the device only allows
        one full-database request per iAP2 connection)."""
        with LIBRARY_LOCK:
            LIBRARY["revision"] = None
            LIBRARY["items"].clear()
            LIBRARY["playlists"].clear()
            LIBRARY["_summary"] = None
            LIBRARY["_artists"] = None
            LIBRARY["_songs"] = None
            LIBRARY["_genres"] = None
            LIBRARY["_album_artists"] = None
            LIBRARY["version"] += 1
            LIBRARY["dirty"] = True
        if self.conn is not None:
            self._event("Resync media library: reconnect")
            self.conn.close()
        return True

    def sync_library_incremental(self):
        """Ask the device for an incremental media-library update."""
        if not self.conn or not self.loop:
            raise RuntimeError("iAP2 is not connected")
        future = asyncio.run_coroutine_threadsafe(
            self._sync_library_incremental(), self.loop)
        return future.result(timeout=5)

    async def _sync_library_incremental(self):
        conn = self.conn
        if conn is None:
            raise RuntimeError("iAP2 is not connected")
        with STATE_LOCK:
            libs = list(STATE["libraries"])
        local = self._local_library(libs)
        if not local:
            raise RuntimeError("Media library information is not ready")
        await self._subscribe_library(conn, local["uuid"])
        self._event("Incremental media library sync requested")

    # -- protocol handlers ------------------------------------------
    def _ft_handler(self, data):
        if len(data) < 2:
            return
        fid, cmd, payload = data[0], data[1], data[2:]
        if not hasattr(self, "_transfers"):
            self._transfers = {}
        transfers = self._transfers
        if cmd == 0x04:  # Setup
            size = int.from_bytes(payload[:8], "big") if len(payload) >= 8 else 0
            ftype = int.from_bytes(payload[8:10], "big") if len(payload) >= 10 else None
            transfers[fid] = {"size": size, "filetype": ftype, "data": bytearray()}
            self._event(f"Artwork transfer started id={fid} ({size} bytes)")
            self._send_ft(bytes([fid, 0x01]))  # Start
        elif cmd in (0x80, 0x00, 0x40, 0xC0):  # data frames
            t = transfers.get(fid)
            if t is None:
                return
            t["data"] += payload
            if cmd in (0x40, 0xC0):
                blob = bytes(t["data"])
                ftype = t.get("filetype")
                if ftype in (0x0004, 0x0007):
                    FT_PLAYLIST_FILES[fid] = blob
                    print(
                        f"PLAYLIST FT id={fid} type={ftype:#06x} "
                        f"len={len(blob)} head={blob[:64].hex(' ')}",
                        flush=True)
                    self._event(
                        f"Playlist contents id={fid} ({len(blob)} bytes)")
                    apply_playlist_ft(fid, blob, ftype)
                elif ftype in (0x0003, 0x0006):
                    FT_QUEUE_FILES[fid] = blob
                    print(
                        f"QUEUE FT id={fid} type={ftype:#06x} "
                        f"len={len(blob)} head={blob[:64].hex(' ')}",
                        flush=True)
                    self._event(
                        f"Queue contents id={fid} ({len(blob)} bytes)")
                    apply_queue_ft(fid, blob, ftype)
                elif ftype == 0x0002 or (ftype is None and
                                         blob.startswith(b"\xff\xd8")):
                    with STATE_LOCK:
                        if blob:
                            STATE["artwork_bytes"] = blob
                            STATE["artwork_ready_id"] = fid
                            STATE["artwork_mime"] = (
                                "image/png" if blob.startswith(b"\x89PNG")
                                else "image/jpeg")
                        STATE["last_event"] = (
                            f"Artwork complete id={fid} ({len(blob)} bytes)")
                    if blob.startswith(b"\xff\xd8"):
                        save_artwork_cache(fid, blob)
                else:
                    print(
                        f"FT file id={fid} type={ftype} len={len(blob)} "
                        f"head={blob[:64].hex(' ')}", flush=True)
                self._send_ft(bytes([fid, 0x05]))  # Success
                del transfers[fid]
        elif cmd == 0x02:
            transfers.pop(fid, None)

    def _send_ft(self, data):
        conn = self.conn
        if conn is not None:
            conn.send_packet(
                link_layer.IAP2Packet(
                    data, session_id=link_layer.IAP2Connection.FT_SESSION_ID))

    def _handle_now_playing(self, body):
        np = STATE["now_playing"]
        for pid, payload in csm.parse_params(body):
            if pid == 0:  # media item group
                for cid, child in csm.parse_params(payload):
                    if cid == 1:
                        np["title"] = parse_string(child)
                    elif cid == 2:
                        np["media_type"] = child[0] if child else None
                    elif cid == 3:
                        np["rating"] = child[0] if child else None
                    elif cid == 4:
                        np["duration_ms"] = parse_u32(child) or 0
                    elif cid == 5:
                        np["album_pid"] = (
                            f"{int.from_bytes(child, 'big'):016x}"
                            if child else None)
                    elif cid == 6:
                        np["album"] = parse_string(child)
                    elif cid == 7:
                        np["track"] = int.from_bytes(child, "big")
                    elif cid == 8:
                        np["track_count"] = int.from_bytes(child, "big")
                    elif cid == 9:
                        np["disc"] = int.from_bytes(child, "big")
                    elif cid == 10:
                        np["disc_count"] = int.from_bytes(child, "big")
                    elif cid == 11:
                        np["artist_pid"] = (
                            f"{int.from_bytes(child, 'big'):016x}"
                            if child else None)
                    elif cid == 12:
                        np["artist"] = parse_string(child)
                    elif cid == 13:
                        np["album_artist_pid"] = (
                            f"{int.from_bytes(child, 'big'):016x}"
                            if child else None)
                    elif cid == 14:
                        np["album_artist"] = parse_string(child)
                    elif cid == 16:
                        np["genre"] = parse_string(child)
                    elif cid == 18:
                        np["composer"] = parse_string(child)
                    elif cid == 19:
                        np["compilation"] = child[0] if child else None
                    elif cid in (21, 22, 23, 24) and child:
                        key = {21: "like_supported", 22: "ban_supported",
                               23: "liked", 24: "banned"}[cid]
                        np[key] = bool(child[0])
                    elif cid == 26:
                        np["artwork_id"] = child[0] if child else None
                    elif cid == 27:
                        np["chapter_count"] = parse_u32(child)
            elif pid == 1:  # playback group
                for cid, child in csm.parse_params(payload):
                    if cid == 0:
                        np["status"] = child[0] if child else None
                    elif cid == 1:
                        np["elapsed_ms"] = parse_u32(child) or 0
                    elif cid == 2:
                        np["queue_index"] = parse_u32(child)
                    elif cid == 3:
                        np["queue_count"] = parse_u32(child)
                    elif cid == 4:
                        np["queue_chapter"] = parse_u32(child)
                    elif cid == 5:
                        np["shuffle"] = child[0] if child else None
                    elif cid == 6:
                        np["repeat"] = child[0] if child else None
                    elif cid == 7:
                        np["app"] = parse_string(child)
                    elif cid == 12:
                        np["playback_speed"] = parse_u32(child)
                    elif cid == 13 and child:
                        np["seek_available"] = bool(child[0])
                    elif cid == 14 and child:
                        np["queue_available"] = bool(child[0])
                    elif cid == 15 and child:
                        np["queue_transfer_id"] = child[0]
                    elif cid == 16:
                        np["app_bundle_id"] = parse_string(child)
                    elif cid == 17:
                        np["queue_transfer"] = True
        np["updated_at"] = time.time() * 1000.0
        record_now_playing(np)

    async def _handle_control(self, conn):
        stream = conn.control_session
        import struct
        mfi = LocalMfiProvider(CERT_PATH, KEY_PATH)
        while True:
            try:
                header = await stream.readexactly(6)
            except Exception:  # noqa: BLE001
                return
            start, length, msg_id = struct.unpack(">HHH", header)
            if start != 0x4040:
                continue
            body = await stream.readexactly(length - 6) if length > 6 else b""
            self._event(f"RX 0x{msg_id:04x}")

            if msg_id == 0xAA00:
                await send_csm(conn, 0xAA01, [csm.bytes_param(0, mfi.certificate_blob())])
            elif msg_id == 0xAA02:
                params = dict(csm.parse_params(body))
                await send_csm(conn, 0xAA03, [
                    csm.bytes_param(0, mfi.sign_challenge(params.get(0, b"")))])
            elif msg_id == 0xAA04:
                self._set_error("MFi authentication failed")
            elif msg_id == 0x1D00:
                await send_csm(
                    conn, 0x1D01,
                    identification_params(include_power=self._power_updates))
            elif msg_id == 0x1D02:
                with STATE_LOCK:
                    STATE["connected"] = True
                    STATE["last_error"] = None
                    STATE["power"]["supported"] = self._power_updates
                await send_csm(conn, 0x5000, now_playing_params())
                await send_csm(conn, 0x4C00, [])
                if self._power_updates:
                    await send_csm(conn, 0xAE00, power_update_params())
                await send_hid_start(conn)
                self._event("Connected")
            elif msg_id == 0x1D03:
                rejected = []
                rejected_pids = set()
                for pid, payload in csm.parse_params(body):
                    rejected_pids.add(pid)
                    if pid in (6, 7) and payload:
                        ids = [f"0x{int.from_bytes(payload[i:i + 2], 'big'):04x}"
                               for i in range(0, len(payload) - 1, 2)]
                        label = ("sent" if pid == 6 else "received")
                        rejected.append(f"{label}: {', '.join(ids)}")
                    else:
                        rejected.append(f"param {pid}")
                if (self._power_updates
                        and (6 in rejected_pids or 7 in rejected_pids)):
                    self._power_capability[self._product] = False
                    self._power_updates = False
                    with STATE_LOCK:
                        STATE["power"]["supported"] = False
                        STATE["last_error"] = (
                            "identification rejected with power updates; "
                            "retrying without them")
                    self._event(
                        "Power updates rejected; reconnecting without them")
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    return
                with STATE_LOCK:
                    STATE["last_error"] = (
                        "identification rejected"
                        + (": " + "; ".join(rejected) if rejected else ""))
            elif msg_id == 0x5001:
                with STATE_LOCK:
                    self._handle_now_playing(body)
                    artwork_id = STATE["now_playing"].get("artwork_id")
                    ready_id = STATE["artwork_ready_id"]
                    queue_available = STATE["now_playing"].get(
                        "queue_available")
                if queue_available is False:
                    set_queue([], "device")
                if artwork_id is not None and artwork_id != ready_id:
                    load_cached_artwork(artwork_id)
            elif msg_id == 0xAE01:
                with STATE_LOCK:
                    power = STATE["power"]
                    for pid, payload in csm.parse_params(body):
                        if pid == 0:
                            power["max_current_ma"] = int.from_bytes(
                                payload, "big")
                        elif pid == 1 and payload:
                            power["device_will_charge"] = bool(payload[0])
                        elif pid == 2 and payload:
                            power["accessory_power_mode"] = payload[0]
                        elif pid == 4 and payload:
                            power["external_charger"] = bool(payload[0])
                        elif pid == 5 and payload:
                            power["charging_state"] = payload[0]
                        elif pid == 6 and payload:
                            # BatteryChargeLevel is uint16 per R29 Table 97-143.
                            power["battery_level"] = int.from_bytes(
                                payload, "big")
                    power["supported"] = True
                    power["updated_at"] = time.time() * 1000.0
            elif msg_id in (0x4E09, 0x4E0A, 0x4E0B, 0x4E0C):
                with STATE_LOCK:
                    device = STATE["device"]
                    for pid, payload in csm.parse_params(body):
                        if msg_id == 0x4E09 and pid == 0:
                            device["name"] = parse_string(payload)
                        elif msg_id == 0x4E0A and pid == 0:
                            device["language"] = parse_string(payload)
                        elif msg_id == 0x4E0B and pid == 0:
                            device["time_unix"] = int.from_bytes(
                                payload, "big")
                        elif msg_id == 0x4E0B and pid == 1:
                            device["timezone_offset_minutes"] = \
                                int.from_bytes(payload, "big", signed=True)
                        elif msg_id == 0x4E0B and pid == 2:
                            device["daylight_offset_minutes"] = \
                                int.from_bytes(payload, "big", signed=True)
                        elif msg_id == 0x4E0C and pid == 0:
                            device["uuid"] = parse_string(payload)
                    device["updated_at"] = time.time() * 1000.0
            elif msg_id == 0x4C01:
                libs = parse_library_info(body)
                with STATE_LOCK:
                    STATE["libraries"] = libs
                local = self._local_library(libs)
                if local:
                    library_id = device_store.base_library_id(
                        local.get("uuid"))
                    if library_id and library_id != LIBRARY["library_id"]:
                        switch_library(library_id, local.get("name"))
                    await self._subscribe_library(conn, local["uuid"])
            elif msg_id == 0x4C04:
                rev, last7, items, playlists = self._apply_library_update(body)
                if rev:
                    with STATE_LOCK:
                        STATE["library_revision"] = rev
                # The device completes the "delete everything" phase with
                # flag 0x5A on a message without item/playlist groups. Then
                # re-subscribe with the latest revision so it sends the full
                # database (observed behaviour; see HANDOVER §3.8).
                if (last7 == 0x5A and not self._resynced
                        and not items and not playlists and rev):
                    self._resynced = True
                    with STATE_LOCK:
                        libs = list(STATE["libraries"])
                    local = self._local_library(libs)
                    if local:
                        self._event("Delete phase finished; resubscribing for full media library")
                        await self._subscribe_library(conn, local["uuid"])

    @staticmethod
    def _local_library(libs):
        return next(
            (l for l in libs if l.get("uuid", "").endswith("-MPB-15.7.8")),
            libs[0] if libs else None)

    async def _subscribe_library(self, conn, uuid):
        with LIBRARY_LOCK:
            revision = LIBRARY["revision"]
            LIBRARY["syncing"] = True
            LIBRARY["batch_seq"] = None
            LIBRARY["batch_last"] = 0.0
            LIBRARY["last_update"] = time.time()
        params = [
            csm.string(0, uuid),
            # The device starts a full sync only when a revision is present;
            # "0" means "nothing known yet".
            csm.string(1, revision if revision else "0"),
            csm.group(2, [csm.void(i) for i in range(0, 28)]),
            csm.group(3, [csm.void(i) for i in range(0, 6)]),
            # Request update progress, resident-only state, and All Songs
            # capability in addition to the items/playlists above.
            csm.void(4), csm.void(5), csm.void(6),
        ]
        self._event(f"Media library subscription revision={revision or '0'}")
        await send_csm(conn, 0x4C03, params)

    def _apply_library_update(self, body):
        rev = None
        last7 = None
        reset = False
        items = []
        playlists = []
        deleted_items = []
        deleted_playlists = []
        hiding_remote = None
        play_all_songs = None
        for pid, payload in csm.parse_params(body):
            if pid == 1:
                rev = parse_string(payload)
            elif pid == 6:
                reset = True
            elif pid == 2:
                items.append(parse_media_item(payload))
            elif pid == 3:
                playlists.append(parse_media_playlist(payload))
            elif pid == 4 and len(payload) == 8:
                deleted_items.append(f"{int.from_bytes(payload, 'big'):016x}")
            elif pid == 5 and len(payload) == 8:
                deleted_playlists.append(f"{int.from_bytes(payload, 'big'):016x}")
            elif pid == 7 and payload:
                last7 = payload[0]
            elif pid == 8 and payload:
                hiding_remote = bool(payload[0])
            elif pid == 9 and payload:
                play_all_songs = bool(payload[0])
        with LIBRARY_LOCK:
            changed = False
            now = time.time()
            if (not LIBRARY["syncing"]
                    and LIBRARY["batch_seq"] is not None
                    and now - LIBRARY["batch_last"] > 5.0):
                LIBRARY["batch_seq"] = None
            old_revision = LIBRARY["revision"]
            if reset and (LIBRARY["items"] or LIBRARY["playlists"]):
                LIBRARY["items"].clear()
                LIBRARY["playlists"].clear()
                changed = True
            for pid in deleted_items:
                if LIBRARY["items"].pop(pid, None) is not None:
                    changed = True
            for pid in deleted_playlists:
                if LIBRARY["playlists"].pop(pid, None) is not None:
                    changed = True
            for item in items:
                key = item.get("id")
                if key:
                    existing = LIBRARY["items"].get(key)
                    ts = LIBRARY["first_seen"].get(key)
                    if ts is None:
                        ts = now
                        LIBRARY["first_seen"][key] = ts
                        if LIBRARY["batch_seq"] is None:
                            LIBRARY["batch_seq"] = \
                                LIBRARY["next_seen_seq"]
                            LIBRARY["next_seen_seq"] += 1
                        seq = LIBRARY["batch_seq"]
                        LIBRARY["batch_last"] = now
                    else:
                        seq = int(
                            LIBRARY["first_seen_seq"].get(key)
                            or (existing or {}).get("first_seen_seq")
                            or 1)
                    item["first_seen"] = ts
                    item["first_seen_seq"] = seq
                    LIBRARY["first_seen_seq"][key] = seq
                    LIBRARY["items"][key] = item
                    changed = True
            for pl in playlists:
                key = pl.get("id")
                if key:
                    LIBRARY["playlists"][key] = pl
                    changed = True
                    print(f"PLAYLIST GROUP: {pl}", flush=True)
            if rev:
                LIBRARY["revision"] = rev
                if rev != old_revision:
                    # Persist revision-only updates too, otherwise every
                    # restart re-requests the same no-op increment.
                    LIBRARY["dirty"] = True
            if changed:
                LIBRARY["dirty"] = True
                LIBRARY["version"] += 1
                LIBRARY["last_change"] = time.time()
                LIBRARY["_summary"] = None
                LIBRARY["_artists"] = None
                LIBRARY["_songs"] = None
                LIBRARY["_genres"] = None
                LIBRARY["_album_artists"] = None
            LIBRARY["last_update"] = time.time()
        for pl in playlists:
            ftid = pl.get("contained_ft_id")
            if ftid is not None and ftid in FT_PLAYLIST_FILES:
                apply_playlist_ft(ftid, FT_PLAYLIST_FILES[ftid])
        if last7 is not None or hiding_remote is not None or play_all_songs is not None:
            with STATE_LOCK:
                if last7 is not None:
                    STATE["library_progress"] = last7
                if hiding_remote is not None:
                    STATE["library_hiding_remote"] = hiding_remote
                if play_all_songs is not None:
                    STATE["play_all_songs_capable"] = play_all_songs
        return rev, last7, items, playlists


SESSION = Session()


def _session_ready():
    with STATE_LOCK:
        return bool(STATE["connected"] and STATE["link_state"] == 2)


def _reload_media_cache(target="all"):
    with CACHE_LOCK:
        if target in ("library", "all"):
            load_media_dates(force=True)
        if target in ("artwork", "all"):
            load_media_artwork(force=True)


def _auto_switch_iap2():
    return bool(settings_snapshot().get("auto_switch_iap2", False))


MEDIA_JOBS = media_jobs.MediaJobs(
    stop_session=SESSION.stop,
    start_session=SESSION.start,
    session_ready=_session_ready,
    reload_cache=_reload_media_cache,
    auto_switch=_auto_switch_iap2,
    clear_cache=clear_local_cache,
)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>MediaCtrl / iAP2</title>
<link rel="stylesheet" href="/static/bootstrap-icons/bootstrap-icons.min.css">
<style>
/* =========================================================================
   MediaCtrl UI — one token-based theme
   Type scale: 10 micro / 11 meta / 12 body / 13 emphasis / 15 title / 20 display
   Color roles: text > text-2 > text-3 > text-4 on surface layers
   ========================================================================= */
:root {
  color-scheme: dark;
  --bg: #1b1f23;
  --surface: #23282d;
  --surface-2: #282e34;
  --surface-3: #313840;
  --surface-hover: #363f47;
  --well: #1d2226;
  --line: #3c444c;
  --line-soft: #30373e;
  --line-strong: #59636d;
  --text: #e7eaed;
  --text-2: #c1c8ce;
  --text-3: #98a1a9;
  --text-4: #7d858d;
  --accent: #cdd4da;
  --ok: #a9c2b2;
  --green: #3fb950;
  --warn: #cfc0a2;
  --err: #d8aaaa;
  --fs-micro: 10px;
  --fs-meta: 11px;
  --fs-body: 12px;
  --fs-em: 13px;
  --fs-title: 15px;
  --fs-display: 20px;
  --font: "Iosevka", "JetBrains Mono", "IBM Plex Mono", ui-monospace,
          SFMono-Regular, Menlo, Consolas, monospace;
}

* { box-sizing: border-box; }
html { height: 100%; }
body {
  margin: 0;
  height: 100vh;
  overflow: hidden;
  background: var(--bg);
  color: var(--text);
  font-family: var(--font);
  font-size: var(--fs-body);
  line-height: 1.45;
  letter-spacing: .01em;
}
button, input, select, textarea { font-family: inherit; }
::selection { background: #4a555f; color: #fff; }
::-webkit-scrollbar { width: 9px; height: 9px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--line); border: 2px solid var(--bg); }
::-webkit-scrollbar-thumb:hover { background: var(--line-strong); }

/* ---- buttons: one base shape, clear primary / danger / small roles ----- */
button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 30px;
  padding: 0 12px;
  border: 1px solid var(--line);
  border-radius: 0;
  background: var(--surface-2);
  color: var(--text-2);
  font-size: var(--fs-meta);
  line-height: 1;
  letter-spacing: .06em;
  text-transform: uppercase;
  white-space: nowrap;
  cursor: pointer;
}
button:hover:not(:disabled) {
  background: var(--surface-hover);
  border-color: var(--line-strong);
  color: var(--text);
}
button:disabled { opacity: .4; cursor: not-allowed; }
button.main {
  background: var(--accent);
  border-color: var(--accent);
  color: #171b1f;
}
button.main:hover:not(:disabled) {
  background: #e2e7eb;
  border-color: #e2e7eb;
  color: #171b1f;
}
button.danger { color: var(--err); border-color: #6a4c4c; }
button.danger:hover:not(:disabled) {
  background: #3a2f30;
  border-color: #8a5f5f;
  color: #f0cccc;
}
button.icon-only { padding: 0; }
input, select { outline: none; }
input::placeholder { color: var(--text-4); }

/* ---- app frame --------------------------------------------------------- */
.wrap {
  width: min(1360px, 98vw);
  height: 100vh;
  margin: 0 auto;
  padding: 10px 0;
  display: flex;
  flex-direction: column;
}
.topbar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
  padding: 0 2px 8px;
  border-bottom: 1px solid var(--line);
  color: var(--text-3);
  font-size: var(--fs-meta);
}
.brand {
  color: var(--text);
  font-size: var(--fs-meta);
  font-weight: 700;
  letter-spacing: .16em;
  text-transform: uppercase;
}
.brand::before {
  content: "";
  display: inline-block;
  width: 7px;
  height: 7px;
  margin-right: 8px;
  border: 1px solid var(--accent);
  vertical-align: 1px;
}
.top-actions { display: flex; align-items: center; gap: 10px; }
.top-btn { height: 24px; padding: 0 9px; font-size: var(--fs-micro); }
.top-btn.active {
  background: var(--surface-3);
  border-color: var(--line-strong);
  color: var(--text);
}
.top-btn .num {
  color: var(--text-4);
  font-size: var(--fs-micro);
  letter-spacing: .04em;
}
.dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--err);
  margin-right: 7px;
  vertical-align: 1px;
}
.dot.ok { background: var(--ok); box-shadow: 0 0 8px #a9c2b266; }
.dot.warn { background: var(--warn); box-shadow: 0 0 8px #cfc0a266; }

.layout {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 372px minmax(0, 1fr);
  gap: 12px;
  align-items: stretch;
}
.left {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  padding-right: 2px;
  scrollbar-gutter: stable;
}
.card, .panel { background: var(--surface); border: 1px solid var(--line); }
.card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 14px;
}
.panel {
  padding: 12px 14px;
  color: var(--text-3);
  font-size: var(--fs-body);
}

/* ---- now playing ------------------------------------------------------- */
.now-head { display: flex; gap: 12px; align-items: flex-start; min-width: 0; }
.art-box {
  flex: 0 0 112px;
  width: 112px;
  height: 112px;
  position: relative;
  overflow: hidden;
  display: grid;
  place-items: center;
  background: var(--well);
  border: 1px solid var(--line);
}
.art-box img { width: 100%; height: 100%; object-fit: cover; display: none; }
.art-ph {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-4);
  font-size: var(--fs-micro);
  letter-spacing: .18em;
  text-transform: uppercase;
}
.meta { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.badge {
  align-self: flex-start;
  padding: 3px 8px;
  border: 1px solid var(--line);
  background: var(--surface-2);
  color: var(--text-3);
  font-size: var(--fs-micro);
  letter-spacing: .1em;
  text-transform: uppercase;
}
.badge.playing { background: #33403a; border-color: #526558; color: var(--ok); }
.badge.paused { background: #403b31; border-color: #6a5e4a; color: var(--warn); }
.now-card h1 {
  margin: 6px 0 3px;
  font-size: var(--fs-display);
  line-height: 1.2;
  font-weight: 600;
  overflow-wrap: anywhere;
}
.now-card .artist { margin: 0 0 2px; font-size: var(--fs-em); color: var(--text-2); }
.now-card .album { margin: 0; font-size: var(--fs-meta); color: var(--text-3); overflow-wrap: anywhere; }
.now-card .queue,
.now-card .np-details { margin-top: 3px; font-size: var(--fs-micro); color: var(--text-4); }

.progress { margin-top: 4px; padding-top: 4px; }
.progress input[type="range"] {
  width: 100%;
  height: 18px;
  margin: 0;
  padding: 0;
  accent-color: var(--accent);
  background: transparent;
  cursor: pointer;
}
.progress input[type="range"]:disabled { opacity: .35; cursor: default; }
.times {
  display: flex;
  justify-content: space-between;
  margin-top: 5px;
  color: var(--text-3);
  font-size: var(--fs-micro);
  font-variant-numeric: tabular-nums;
}

/* ---- transport: 4 + 3 + 3 --------------------------------------------- */
.controls {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 6px;
  margin-top: 8px;
}
.controls button,
.controls button.main { width: 100%; height: 40px; padding: 0; }
.controls .bi { font-size: 22px; }
.controls button.main .bi { font-size: 26px; }
.extra-controls { display: flex; flex-direction: column; gap: 6px; margin-top: 8px; }
.control-group { display: flex; gap: 6px; width: 100%; }
.control-group button { flex: 1 1 0; min-width: 0; height: 28px; padding: 0; }
.control-group .bi { font-size: 15px; }
.extra-controls .on {
  background: var(--surface-3);
  border-color: var(--accent);
  color: var(--text);
}

/* ---- panels / key-value rows ------------------------------------------ */
.panel-head {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-height: 20px;
  margin: 0 0 8px;
  padding: 0 0 6px;
  border-bottom: 1px solid var(--line);
}
.panel-head h3 {
  margin: 0;
  border: 0;
  color: var(--text-3);
  font-size: var(--fs-micro);
  font-weight: 600;
  line-height: 1.4;
  letter-spacing: .16em;
  text-transform: uppercase;
}
.panel-note {
  color: var(--text-4);
  font-size: var(--fs-micro);
  letter-spacing: .04em;
}
.small-btn { height: 20px; min-height: 20px; padding: 0 6px; font-size: var(--fs-micro); }
.kv {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 4px 0;
  border-bottom: 1px solid var(--line-soft);
  font-size: var(--fs-body);
}
.kv:last-child { border-bottom: 0; }
.kv span:first-child { flex: 0 0 auto; color: var(--text-3); white-space: nowrap; }
.kv span:last-child {
  min-width: 0;
  color: var(--text-2);
  text-align: right;
  font-variant-numeric: tabular-nums;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.kv .err { color: var(--err); }
#kv-battery > .bi,
#kv-battery > .battery-state { margin-left: 6px; }
#kv-battery .bi { color: var(--text-3); }
#kv-battery .battery-state {
  position: relative;
  display: inline-block;
  width: 1em;
  height: 1em;
  vertical-align: -0.125em;
}
#kv-battery .battery-state .bi {
  position: absolute;
  top: 0;
  left: 0;
  width: 1em;
  height: 1em;
  margin: 0;
  line-height: 1;
  text-align: center;
}
#kv-battery .battery-state .bolt {
  top: 50%;
  left: 50%;
  font-size: .7em;
  color: var(--green);
  transform: translate(-50%, -50%) translateX(-.04em);
}
#libs .kv span:first-child { color: var(--text-2); }
#libs .kv span:last-child { color: var(--text-3); }
.upnext { max-height: 220px; overflow-y: auto; }
.upnext-row {
  display: flex;
  gap: 8px;
  padding: 5px 4px;
  border-bottom: 1px solid var(--line-soft);
  color: var(--text-2);
  font-size: var(--fs-meta);
  cursor: pointer;
}
.upnext-row:hover { background: var(--surface-2); }
.upnext-row .i {
  width: 20px;
  color: var(--text-4);
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.upnext-row .t { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.upnext-row .a { color: var(--text-3); font-size: var(--fs-micro); }
.upnext-row.cur { background: var(--surface-3); }
.queue-panel .upnext { max-height: 180px; }
.queue-actions { display: flex; gap: 6px; margin: 0 0 6px; }
.queue-actions .small-btn { flex: 1 1 0; height: 24px; }

/* ---- library panel / tabs / search ------------------------------------ */
.library-panel {
  min-width: 0;
  min-height: 0;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  scrollbar-gutter: stable;
}
.library-panel .media-wrap { max-height: none; }
.lib-head-row {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
  min-width: 0;
}
.lib-tabs {
  display: flex;
  gap: 0;
  width: max-content;
  max-width: 100%;
  overflow-x: auto;
  border: 1px solid var(--line);
}
.tab {
  width: 40px;
  height: 30px;
  padding: 0;
  border: 0;
  border-right: 1px solid var(--line);
  background: transparent;
  color: var(--text-3);
}
.tab:last-child { border-right: 0; }
.tab:hover { background: var(--surface-2); color: var(--text); }
.tab.active {
  background: var(--surface-3);
  color: var(--text);
  box-shadow: inset 0 -2px 0 var(--accent);
}
.tab .bi { font-size: 16px; vertical-align: 0; }
.lib-head-row .lib-tools { flex: 0 1 300px; margin-left: auto; margin-bottom: 0; }
.lib-tools { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.lib-tools .lib-note { margin-bottom: 0; }
.lib-tools input { width: 100%; }
input:not([type="checkbox"]), .select {
  height: 30px;
  padding: 0 9px;
  border: 1px solid var(--line);
  border-radius: 0;
  background: var(--well);
  color: var(--text);
  font-size: var(--fs-body);
  letter-spacing: 0;
  text-transform: none;
}
input:not([type="checkbox"]):focus,
.select:focus { border-color: var(--line-strong); }
.select {
  appearance: none;
  padding-right: 26px;
  background-color: var(--well);
  background-image:
    linear-gradient(45deg, transparent 50%, var(--text-3) 50%),
    linear-gradient(135deg, var(--text-3) 50%, transparent 50%);
  background-position: calc(100% - 13px) calc(50% - 2px),
                       calc(100% - 8px) calc(50% - 2px);
  background-size: 5px 5px, 5px 5px;
  background-repeat: no-repeat;
  cursor: pointer;
}
.list-toolbar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 6px 10px;
  margin-bottom: 8px;
}
.toolbar-field { display: inline-flex; align-items: center; gap: 6px; }
.toolbar-label {
  color: var(--text-4);
  font-size: var(--fs-micro);
  letter-spacing: .12em;
  text-transform: uppercase;
}
.list-toolbar .select { height: 28px; }

/* ---- tab panes / lists ------------------------------------------------ */
.tab-pane { display: none; }
.tab-pane.active { display: flex; flex-direction: column; flex: 1; min-height: 0; }
.album-list { display: flex; flex-direction: column; flex: 1; min-height: 0; padding: 0; }
.album-list > .media-wrap { flex: 1; min-height: 0; }
.lib-note {
  margin-bottom: 8px;
  color: var(--text-3);
  font-size: var(--fs-meta);
  line-height: 1.5;
}
.more-row { flex: 0 0 auto; display: flex; justify-content: center; margin-top: 8px; }
.more-row button { height: 28px; padding: 0 14px; font-size: var(--fs-meta); }

/* ---- album grid ------------------------------------------------------- */
.album-grid {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  align-content: start;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(148px, 1fr));
  gap: 10px;
  padding: 1px;
}
.album-card {
  min-width: 0;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 5px;
  background: var(--surface-2);
  border: 1px solid var(--line-soft);
  cursor: pointer;
}
.album-card:hover { background: var(--surface-3); border-color: var(--line); }
.album-card.active { background: var(--surface-3); border-color: var(--accent); }
.album-cover {
  aspect-ratio: 1;
  position: relative;
  overflow: hidden;
  display: grid;
  place-items: center;
  background: var(--well);
  border: 1px solid var(--line-soft);
}
.album-cover img { width: 100%; height: 100%; object-fit: cover; display: block; }
.album-cover.missing::after {
  content: "NO ART";
  color: var(--text-4);
  font-size: var(--fs-micro);
  letter-spacing: .12em;
}
.album-card .album-name {
  color: var(--text);
  font-size: var(--fs-em);
  line-height: 1.3;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.album-card .album-artist {
  color: var(--text-3);
  font-size: var(--fs-meta);
  line-height: 1.3;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.album-card .album-meta { color: var(--text-4); font-size: var(--fs-micro); }
.album-card .album-dates {
  display: flex;
  flex-direction: column;
  gap: 1px;
  color: var(--text-3);
  font-size: var(--fs-micro);
  line-height: 1.3;
  font-variant-numeric: tabular-nums;
}
.empty {
  padding: 22px;
  color: var(--text-4);
  font-size: var(--fs-meta);
  text-align: center;
}
.album-grid .empty { grid-column: 1 / -1; }

/* ---- tables ----------------------------------------------------------- */
.media-wrap {
  max-height: 390px;
  overflow: auto;
  background: var(--well);
  border: 1px solid var(--line);
}
.media-table {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
  font-size: var(--fs-body);
}
.media-table th {
  position: sticky;
  top: 0;
  z-index: 1;
  padding: 7px 10px;
  border-right: 1px solid var(--line-soft);
  border-bottom: 1px solid var(--line);
  background: var(--surface-3);
  color: var(--text-3);
  font-size: var(--fs-micro);
  font-weight: 500;
  letter-spacing: .12em;
  text-align: left;
  text-transform: uppercase;
  white-space: nowrap;
}
.media-table th:last-child { border-right: 0; }
.media-table td {
  padding: 7px 10px;
  border-right: 1px solid var(--line-soft);
  border-bottom: 1px solid var(--line-soft);
  color: var(--text-2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.media-table td:last-child { border-right: 0; }
.media-table tbody tr:last-child td { border-bottom: 0; }
.media-table tbody tr { cursor: pointer; }
.media-table tbody tr:hover { background: var(--surface-2); }
.media-table tbody tr.active { background: var(--surface-3); }
.media-table .title { width: auto; color: var(--text); }
.media-table .artist { width: 20%; color: var(--text-2); }
.media-table .album-col { width: 18%; color: var(--text-3); }
.media-table .cover { width: 42px; padding: 4px 6px; }
.media-table .cover img {
  width: 30px;
  height: 30px;
  display: block;
  object-fit: cover;
  border: 1px solid var(--line-soft);
}
.media-table .num {
  width: 58px;
  color: var(--text-3);
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.media-table .col-index { width: 40px; }
.media-table .date-col { width: 86px; color: var(--text-4); }
.media-table .actions { width: 116px; text-align: right; }
.media-table th.title,
.media-table th.artist,
.media-table th.album-col,
.media-table th.date-col { color: var(--text-3); }
.media-table .actions .icon-btn {
  width: auto !important;
  min-width: 22px;
  height: 22px !important;
  padding: 0 5px !important;
  font-size: var(--fs-micro) !important;
}
.media-table .empty {
  padding: 22px 10px;
  color: var(--text-4);
  font-size: var(--fs-meta);
  text-align: center;
  cursor: default;
}
.media-table .play { margin-left: 4px; color: var(--text-4); opacity: .5; }
.media-table tbody tr:hover .play { opacity: 1; color: var(--text); }
.media-table .play .bi { font-size: var(--fs-body); }
.media-table td.tt { color: var(--text); }
/* Table rows use class "track" too; keep them as real table rows. */
.media-table tr.track { display: table-row; gap: 0; padding: 0; border: 0; }
.media-table tr.track .tt { display: table-cell; flex: none; }

.icon-btn {
  width: 22px !important;
  height: 22px !important;
  min-width: 22px;
  padding: 0 !important;
  border: 1px solid var(--line) !important;
  background: var(--surface-3) !important;
  color: var(--text-3) !important;
  font-size: var(--fs-micro) !important;
  letter-spacing: 0;
  text-transform: none;
}
.icon-btn:hover:not(:disabled) { background: var(--surface-hover) !important; color: var(--text) !important; }
.icon-btn .bi { font-size: 12px; }

/* ---- detail ----------------------------------------------------------- */
.album-detail {
  display: none;
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--line);
}
.album-detail.show { display: block; }
.detail-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 8px;
  min-width: 0;
}
.detail-cover {
  width: 52px;
  height: 52px;
  display: none;
  object-fit: cover;
  border: 1px solid var(--line);
}
.detail-head .t {
  min-width: 0;
  color: var(--text);
  font-size: var(--fs-title);
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.detail-head .s {
  min-width: 0;
  margin-right: auto;
  color: var(--text-3);
  font-size: var(--fs-meta);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.detail-head button { height: 28px; padding: 0 9px; font-size: var(--fs-micro); }
.detail-actions { display: flex; gap: 6px; margin: 8px 0; flex-wrap: wrap; }
.detail-actions button { height: 28px; padding: 0 9px; font-size: var(--fs-micro); }
.track-list { max-height: 320px; overflow-y: auto; border-top: 1px solid var(--line-soft); }
.track {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 4px;
  border-bottom: 1px solid var(--line-soft);
  color: var(--text-2);
  font-size: var(--fs-body);
  cursor: pointer;
}
.track:hover { background: var(--surface-2); }
.track .idx {
  width: 26px;
  color: var(--text-4);
  font-size: var(--fs-meta);
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.track .tt { flex: 1; min-width: 0; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.track .du { color: var(--text-3); font-size: var(--fs-meta); font-variant-numeric: tabular-nums; }
.track-list .play { color: var(--text-4); opacity: 0; }
.track-list .track:hover .play { opacity: 1; }
.track-list .play .bi { font-size: var(--fs-body); }

/* ---- modal / toast ---------------------------------------------------- */
.modal {
  position: fixed;
  inset: 0;
  z-index: 50;
  display: none;
  align-items: center;
  justify-content: center;
  padding: 20px;
  background: #000000c0;
}
.modal.show { display: flex; }
.modal-card {
  width: min(440px, 100%);
  max-height: 90vh;
  overflow-y: auto;
  padding: 16px;
  background: var(--surface);
  border: 1px solid var(--line);
}
.modal-card h3 {
  margin: 0 0 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--line);
  color: var(--text);
  font-size: var(--fs-title);
  font-weight: 600;
}
.media-card { width: min(780px, 100%); }
.media-section { margin: 10px 0 4px; }
.media-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-top: 10px;
}
.media-column { min-width: 0; padding: 10px 12px; border: 1px solid var(--line-soft); }
.media-column h4 {
  margin: 0 0 8px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--line-soft);
  color: var(--text-3);
  font-size: var(--fs-micro);
  font-weight: 600;
  letter-spacing: .16em;
  text-transform: uppercase;
}
.media-actions { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0; }
.media-actions button { height: 28px; padding: 0 10px; font-size: var(--fs-micro); }
.media-check {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 10px 0;
  color: var(--text-3);
  font-size: var(--fs-meta);
}
.media-check input { width: 13px; height: 13px; margin: 0; accent-color: var(--accent); }
.bar.media-bar { height: 6px; margin-top: 6px; }
.media-nums {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-top: 6px;
  color: var(--text-3);
  font-size: var(--fs-meta);
}
.media-error { margin-top: 6px; color: var(--err); font-size: var(--fs-meta); white-space: pre-wrap; }
.media-hint { margin-top: 10px; color: var(--text-4); font-size: var(--fs-meta); line-height: 1.55; }
.modal-close { width: 100%; height: 30px; margin-top: 12px; font-size: var(--fs-meta); }
.toast {
  position: fixed;
  right: 24px;
  bottom: 24px;
  z-index: 100;
  max-width: calc(100vw - 48px);
  padding: 9px 14px;
  border: 1px solid var(--line-strong);
  background: var(--surface-3);
  color: var(--text);
  font-size: var(--fs-body);
  opacity: 0;
  pointer-events: none;
  transition: opacity .2s;
}
.toast.show { opacity: 1; }

/* ---- icons ------------------------------------------------------------ */
.bi { vertical-align: -0.08em; line-height: 1; }
button .bi { font-size: 1.15em; }
.top-btn .bi { font-size: 12px; }
.small-btn .bi { font-size: 11px; }

/* ---- responsive ------------------------------------------------------- */
@media (max-width: 900px) {
  body { height: auto; overflow: auto; }
  .wrap { width: 96vw; height: auto; min-height: 100vh; padding: 12px 0; }
  .layout { display: flex; flex-direction: column; flex: none; }
  .left { overflow: visible; padding-right: 0; }
  .library-panel { height: auto; overflow: visible; }
  .library-panel .tab-pane.active { display: block; }
  .library-panel .album-list,
  .library-panel .album-grid { flex: none; min-height: 0; }
  .library-panel .album-grid { max-height: 70vh; overflow-y: auto; }
  .library-panel .media-wrap { max-height: 520px; }
  .art-box { flex-basis: 104px; width: 104px; height: 104px; }
  .now-card h1 { font-size: 18px; }
  .media-grid { grid-template-columns: 1fr; }
}
@media (max-width: 760px) {
  .topbar { flex-wrap: wrap; gap: 8px; }
  .top-actions { flex-wrap: wrap; justify-content: flex-end; }
  .lib-head-row .lib-tools { flex: 1 1 100%; margin-left: 0; }
  .media-table .album-col,
  .media-table .artist { display: none; }
  .media-table th,
  .media-table td { padding: 7px 8px; }
  .media-table .num { width: 52px; }
  .media-table .date-col { width: 78px; }
  .media-table .actions { width: 104px; }
  .detail-head .s { margin-right: 0; }
}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div class="brand">MediaCtrl / iAP2</div>
    <div class="top-actions">
      <button id="btn-media" class="top-btn"><i class="bi bi-collection"></i> MediaLibrary</button>
      <div><span class="dot" id="conn-dot"></span><span id="conn-text">Connecting…</span></div>
    </div>
  </div>

  <div class="layout">
    <aside class="left">
    <div class="card now-card">
      <div class="now-head">
        <div class="art-box">
          <img id="art" alt="Cover">
          <div class="art-ph" id="art-ph">Cover</div>
        </div>
        <div class="meta">
          <div class="badge" id="state-badge">Waiting for device…</div>
          <h1 id="title">Not playing</h1>
          <p class="artist" id="artist"></p>
          <p class="album" id="album"></p>
        </div>
      </div>
      <div class="queue" id="queue"></div>
      <div class="np-details" id="np-details"></div>

      <div class="progress">
        <input id="seek" type="range" min="0" max="1" value="0" step="1000"
               disabled title="Seek" aria-label="Seek">
        <div class="times"><span id="elapsed">0:00</span><span id="duration">0:00</span></div>
      </div>

      <div class="controls">
        <button id="btn-prev" class="icon-only" title="Previous" aria-label="Previous"><i class="bi bi-skip-start-fill"></i></button>
        <button id="btn-toggle" class="main icon-only" title="Play/Pause" aria-label="Play"><i class="bi bi-play-fill"></i></button>
        <button id="btn-next" class="icon-only" title="Next" aria-label="Next"><i class="bi bi-skip-end-fill"></i></button>
        <button id="btn-stop" class="icon-only" title="Stop" aria-label="Stop"><i class="bi bi-stop-fill"></i></button>
      </div>
      <div class="extra-controls">
        <div class="control-group volume-row">
          <button id="btn-vol-down" class="icon-only" title="Volume down" aria-label="Volume down"><i class="bi bi-volume-down-fill"></i></button>
          <button id="btn-vol-up" class="icon-only" title="Volume up" aria-label="Volume up"><i class="bi bi-volume-up-fill"></i></button>
          <button id="btn-mute" class="icon-only" title="Mute" aria-label="Mute"><i class="bi bi-volume-mute-fill"></i></button>
        </div>
        <div class="control-group mode-row">
          <button id="btn-shuffle" class="icon-only" title="Shuffle off" aria-label="Shuffle"><i class="bi bi-shuffle"></i></button>
          <button id="btn-repeat" class="icon-only" title="Repeat off" aria-label="Repeat"><i class="bi bi-repeat"></i></button>
          <button id="btn-current-selection" class="icon-only" title="Resume queue" aria-label="Resume queue"><i class="bi bi-collection-play"></i></button>
        </div>
      </div>
    </div>
    <div class="panel queue-panel">
      <div class="panel-head">
        <h3>Up Next</h3>
        <span class="panel-note" id="upnext-note"></span>
      </div>
      <div class="queue-actions" id="queue-actions" style="display:none">
        <button id="btn-play-queue" class="small-btn main" title="Play queued tracks"><i class="bi bi-play-fill"></i> Play queue</button>
        <button id="btn-clear-queue" class="small-btn" title="Clear queue"><i class="bi bi-x-lg"></i> Clear</button>
      </div>
      <div class="upnext" id="upnext"></div>
    </div>
    <div class="panel library-info-panel">
      <div class="panel-head">
        <h3>Library</h3>
        <button id="btn-resync" class="small-btn" title="Clear local mirror and resync"><i class="bi bi-arrow-clockwise"></i> Resync</button>
      </div>
      <div class="kv"><span>Tracks</span><span id="lib-tracks">—</span></div>
      <div class="kv"><span>Albums</span><span id="lib-albums-count">—</span></div>
      <div class="kv"><span>Status</span><span id="lib-sync">—</span></div>
      <div class="kv"><span>Revision</span><span id="lib-rev">—</span></div>
      <div id="libs"><div class="kv"><span>Library</span><span>—</span></div></div>
    </div>
    <div class="panel session-panel">
      <div class="panel-head"><h3>Session</h3></div>
      <div class="kv"><span>Connection</span><span id="kv-conn">—</span></div>
      <div class="kv"><span>Link state</span><span id="kv-link">—</span></div>
      <div class="kv"><span>App</span><span id="kv-app">—</span></div>
      <div class="kv"><span>Device</span><span id="kv-device">—</span></div>
      <div class="kv"><span>Battery</span><span id="kv-battery">—</span></div>
      <div class="kv"><span>Artwork ID / Size</span><span id="kv-art">—</span></div>
      <div class="kv"><span>Last event</span><span id="kv-event">—</span></div>
      <div class="kv"><span>Error</span><span id="kv-err" class="err">—</span></div>
    </div>
    </aside>
    <div class="panel library-panel">
      <div class="panel-head">
        <h3>Browser</h3>
      </div>
      <div class="lib-head-row">
        <div class="lib-tabs">
          <button class="tab active" data-tab="albums" title="Albums" aria-label="Albums"><i class="bi bi-disc"></i></button>
          <button class="tab" data-tab="artists" title="Artists" aria-label="Artists"><i class="bi bi-person"></i></button>
          <button class="tab" data-tab="album-artists" title="Album Artists" aria-label="Album Artists"><i class="bi bi-people"></i></button>
          <button class="tab" data-tab="genres" title="Genres" aria-label="Genres"><i class="bi bi-tag"></i></button>
          <button class="tab" data-tab="songs" title="Songs" aria-label="Songs"><i class="bi bi-music-note-list"></i></button>
          <button class="tab" data-tab="recent" title="Recently Added" aria-label="Recently Added"><i class="bi bi-clock-history"></i></button>
          <button class="tab" data-tab="localPlaylist" title="localPlaylist" aria-label="localPlaylist"><i class="bi bi-list-check"></i></button>
          <button class="tab" data-tab="playlists" title="Device Playlists" aria-label="Device Playlists"><i class="bi bi-collection-play"></i></button>
        </div>
        <div class="lib-tools">
          <input id="lib-search" placeholder="Search albums / artists…">
        </div>
      </div>
      <div class="tab-pane active" id="pane-albums">
        <div class="list-toolbar">
          <label class="toolbar-field">
            <span class="toolbar-label">Sort</span>
            <select id="albums-sort" class="select">
              <option value="recent">Recently Added</option>
              <option value="name">Name</option>
              <option value="artist">Artist</option>
            </select>
          </label>
          <label class="toolbar-field">
            <span class="toolbar-label">Order</span>
            <select id="albums-dir" class="select">
              <option value="desc">Descending</option>
              <option value="asc">Ascending</option>
            </select>
          </label>
        </div>
        <div class="album-grid" id="lib-albums"></div>
      </div>
      <div class="tab-pane" id="pane-artists">
        <div class="album-list" id="lib-artists"></div>
        <div class="more-row">
          <button id="btn-more-artists" style="display:none"><i class="bi bi-chevron-down"></i> Load more artists</button>
        </div>
      </div>
      <div class="tab-pane" id="pane-album-artists">
        <div class="album-list" id="lib-album-artists"></div>
        <div class="more-row">
          <button id="btn-more-album-artists" style="display:none"><i class="bi bi-chevron-down"></i> Load more album artists</button>
        </div>
      </div>
      <div class="tab-pane" id="pane-genres">
        <div class="album-list" id="lib-genres"></div>
        <div class="more-row">
          <button id="btn-more-genres" style="display:none"><i class="bi bi-chevron-down"></i> Load more genres</button>
        </div>
      </div>
      <div class="tab-pane" id="pane-songs">
        <div class="list-toolbar">
          <label class="toolbar-field">
            <span class="toolbar-label">Sort</span>
            <select id="songs-sort" class="select">
              <option value="title">Title</option>
              <option value="artist">Artist</option>
              <option value="album">Album</option>
              <option value="duration">Duration</option>
              <option value="recent">Recently Added</option>
            </select>
          </label>
        </div>
        <div class="album-list" id="lib-songs"></div>
        <div class="more-row">
          <button id="btn-more-songs" style="display:none"><i class="bi bi-chevron-down"></i> Load more songs</button>
        </div>
      </div>
      <div class="tab-pane" id="pane-recent">
        <div class="lib-note" id="recent-note"></div>
        <div class="album-list" id="lib-recent"></div>
        <div class="more-row">
          <button id="btn-more-recent" style="display:none"><i class="bi bi-chevron-down"></i> Load more</button>
        </div>
      </div>
      <div class="tab-pane" id="pane-localPlaylist">
        <div class="lib-tools">
          <button id="btn-play-localPlaylist"><i class="bi bi-play-fill"></i> Play localPlaylist</button>
          <span class="lib-note" id="localPlaylist-note"></span>
        </div>
        <div class="album-list" id="lib-localPlaylist"></div>
      </div>
      <div class="tab-pane" id="pane-playlists">
        <div class="album-list" id="lib-playlists"></div>
      </div>
      <div class="album-detail" id="lib-detail">
        <div class="detail-head">
          <img class="detail-cover" id="detail-cover" alt="">
          <div class="t" id="detail-title"></div>
          <div class="s" id="detail-sub"></div>
          <button id="btn-queue-detail" title="Add to queue"><i class="bi bi-plus-lg"></i> Queue</button>
          <button id="btn-play-detail" class="main"><i class="bi bi-play-fill"></i> Play</button>
          <button id="btn-detail-close"><i class="bi bi-x-lg"></i> Close</button>
        </div>
        <div class="detail-actions" id="detail-actions"></div>
        <div class="track-list" id="detail-tracks"></div>
      </div>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>
<div class="modal" id="media-modal">
  <div class="modal-card media-card">
    <h3>MediaLibrary</h3>
    <div class="media-section">
      <div class="kv"><span>Mode</span><span id="media-mode">—</span></div>
      <div class="kv"><span>Device</span><span id="media-device">—</span></div>
      <div class="kv"><span>Library ID</span><span id="media-library-id">—</span></div>
      <div class="kv"><span>Pairing</span><span id="media-paired">—</span></div>
    </div>
    <div class="media-grid">
      <section class="media-column">
        <h4>AFC</h4>
        <div class="media-actions">
          <button id="media-pair">Pair / Trust</button>
          <button id="media-clear" class="danger">Clear AFC Cache</button>
        </div>
        <div class="media-section">
          <div class="kv"><span>MediaLibrary cache</span><span id="media-library-state">Not loaded</span></div>
          <div class="media-actions">
            <button id="media-load-library">Load MediaLibrary</button>
            <button id="media-update-library">Update MediaLibrary</button>
          </div>
        </div>
        <div class="media-section">
          <div class="kv"><span>Artwork cache</span><span id="media-artwork-state">Not loaded</span></div>
          <div class="media-actions">
            <button id="media-update-artwork">Update Artwork</button>
          </div>
        </div>
        <div class="media-section">
          <div class="kv"><span>Combined sync</span><span>Updates both AFC caches</span></div>
          <div class="media-actions">
            <button id="media-quick" class="main">Quick Sync</button>
            <button id="media-full">Full Refresh</button>
            <button id="media-cancel" class="danger" style="display:none">Cancel</button>
          </div>
        </div>
      </section>
      <section class="media-column">
        <h4>iAP2</h4>
        <div class="media-actions">
          <button id="media-switch-iap2">Switch to iAP2</button>
        </div>
        <div class="media-section">
          <div class="kv"><span>iAP2 library</span><span id="iap2-library-state">Connected</span></div>
          <div class="media-actions">
            <button id="iap2-sync">Sync iAP2</button>
            <button id="iap2-resync">Full Resync</button>
          </div>
        </div>
        <div class="media-section">
          <div class="media-actions">
            <button id="iap2-clear-library" class="danger">Clear iAP2 Library</button>
            <button id="iap2-clear-artwork" class="danger">Clear iAP2 Artwork</button>
          </div>
        </div>
        <label class="media-check">
          <input type="checkbox" id="media-auto-iap2">
          Auto switch to iAP2 after AFC tasks
        </label>
      </section>
    </div>
    <div class="media-hint">
      AFC: loading reads an existing local cache; updating switches the device
      to AFC, pulls from the device, and loads the result. iAP2: Sync asks for
      an incremental update; Full Resync clears the revision and reconnects.
      Clearing the iAP2 library lets the protocol perform a full resync.
    </div>
    <div class="media-section">
      <div class="kv"><span>Stage</span><span id="media-stage">Idle</span></div>
      <div class="bar media-bar"><span id="media-bar"></span></div>
      <div class="media-nums" id="media-nums"></div>
      <div class="media-error" id="media-error"></div>
    </div>
    <button class="modal-close" id="media-close">Close</button>
  </div>
</div>

<script>
const $ = (s) => document.querySelector(s);
const STATUS = {0: "Stopped", 1: "Playing", 2: "Paused", 3: "Seeking forward", 4: "Seeking backward"};
const LINK = {0: "Detecting", 1: "Negotiating", 2: "Ready", 3: "Disconnected"};
let state = null;
let shownArt = null;
let lastLibraryId = null;
let lastMediaJobState = null;
let seekDragging = false;

function fmt(ms) {
  ms = Math.max(0, ms || 0);
  const s = Math.floor(ms / 1000);
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

function fmtNum(n) {
  return Number(n || 0).toLocaleString("en-US");
}

function fmtBytes(n) {
  n = Number(n || 0);
  if (n >= 1024 * 1024 * 1024) return (n / (1024 ** 3)).toFixed(2) + " GB";
  if (n >= 1024 * 1024) return (n / (1024 ** 2)).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
  return fmtNum(n) + " B";
}

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), 2200);
}

async function poll() {
  try {
    const r = await fetch("/api/state", {cache: "no-store"});
    state = await r.json();
    render(state);
  } catch (e) {
    $("#conn-dot").className = "dot";
    $("#conn-text").textContent = "Service unreachable";
  }
}

function batteryStateIcon(state) {
  if (state === 0) {
    return '<i class="bi bi-lightning-charge" role="img" ' +
        'title="not charging" aria-label="not charging"></i>';
  }
  if (state === 1) {
    return '<i class="bi bi-battery-charging" role="img" ' +
        'title="charging" aria-label="charging"></i>';
  }
  if (state === 2) {
    return '<span class="battery-state" role="img" ' +
        'title="charged" aria-label="charged">' +
        '<i class="bi bi-battery"></i>' +
        '<i class="bi bi-lightning-charge-fill bolt"></i></span>';
  }
  return "";
}

function render(s) {
  const np = s.now_playing || {};
  const connected = !!s.connected;
  const linkOk = s.link_state === 2;
  const media = s.media || {};
  const activeLibraryId = (media.cache || {}).library_id;
  if (activeLibraryId && activeLibraryId !== lastLibraryId) {
    lastLibraryId = activeLibraryId;
    lastAlbumKey = null;
    setTimeout(refreshLibraryView, 100);
  }
  const mediaJob = media.job || null;
  const mediaBusy = !!(mediaJob &&
      (mediaJob.state === "running" || mediaJob.state === "cancelling"));

  $("#btn-media").classList.toggle(
      "active", media.mode === "afc" || mediaBusy);
  if (mediaBusy && mediaJob.total > 0) {
    $("#btn-media").innerHTML = '<i class="bi bi-collection"></i> MediaLibrary ' +
        `<span class="num">${fmtNum(mediaJob.done)}/${fmtNum(mediaJob.total)}</span>`;
  } else {
    $("#btn-media").innerHTML =
        '<i class="bi bi-collection"></i> MediaLibrary';
  }

  if (media.mode === "afc") {
    $("#conn-dot").className = "dot warn";
    $("#conn-text").textContent = "MediaLibrary mode";
  } else if (media.mode === "switching") {
    $("#conn-dot").className = "dot warn";
    $("#conn-text").textContent = "Switching…";
  } else {
    $("#conn-dot").className = "dot " +
        (connected && linkOk ? "ok" : (connected ? "warn" : ""));
    $("#conn-text").textContent = connected
        ? (linkOk ? "Connected" : "Negotiating link…")
        : "Waiting for device…";
  }

  const status = np.status;
  const badge = $("#state-badge");
  if (status === 1) { badge.textContent = "Playing"; badge.className = "badge playing"; }
  else if (status === 2) { badge.textContent = "Paused"; badge.className = "badge paused"; }
  else if (status === 0) { badge.textContent = "Stopped"; badge.className = "badge"; }
  else if (status === null || status === undefined) { badge.textContent = "Waiting for device…"; badge.className = "badge"; }
  else { badge.textContent = STATUS[status] || ("Status " + status); badge.className = "badge"; }

  $("#title").textContent = np.title || (connected ? "Not playing" : "Disconnected");
  $("#artist").textContent = np.artist || "";
  $("#album").textContent = np.album || "";
  if (np.queue_count) {
    const idx = (np.queue_index ?? 0) + 1;
    $("#queue").textContent = `Queue ${idx} / ${np.queue_count}` +
        (np.queue_chapter ? ` · Chapter ${np.queue_chapter + 1}` : "");
  } else {
    $("#queue").textContent = "";
  }
  const REPEAT = ["Off", "One", "All"];
  const shuffleOn = np.shuffle === 1 || np.shuffle === 2;
  const repeatOn = np.repeat === 1 || np.repeat === 2;
  $("#btn-shuffle").innerHTML = '<i class="bi bi-shuffle"></i>';
  $("#btn-shuffle").title = np.shuffle == null
      ? "Shuffle unknown" : (shuffleOn ? "Shuffle on" : "Shuffle off");
  $("#btn-shuffle").setAttribute("aria-label", $("#btn-shuffle").title);
  $("#btn-repeat").innerHTML = np.repeat === 1
      ? '<i class="bi bi-repeat-1"></i>' : '<i class="bi bi-repeat"></i>';
  $("#btn-repeat").title = "Repeat " +
      (np.repeat == null ? "unknown" : (REPEAT[np.repeat] || np.repeat));
  $("#btn-repeat").setAttribute("aria-label", $("#btn-repeat").title);
  $("#btn-shuffle").classList.toggle("on", shuffleOn);
  $("#btn-repeat").classList.toggle("on", repeatOn);
  const details = [];
  if (np.album_artist) details.push(np.album_artist);
  if (np.genre) details.push(np.genre);
  if (np.composer) details.push("Composer " + np.composer);
  if (np.track) {
    details.push(np.track_count
        ? `Track ${np.track}/${np.track_count}` : `Track ${np.track}`);
  }
  if (np.disc && np.disc_count) {
    details.push(`Disc ${np.disc}/${np.disc_count}`);
  }
  $("#np-details").textContent = details.join(" · ");

  const q = s.queue || {items: []};
  const queued = q.source === "queued";
  $("#queue-actions").style.display = queued && q.total ? "flex" : "none";
  $("#btn-play-queue").disabled = !(connected && linkOk);
  if (queued) {
    $("#upnext-note").textContent =
        `Queued · ${q.total} ` + (q.total === 1 ? "track" : "tracks");
  } else {
    $("#upnext-note").textContent = q.source === "device"
        ? "Device queue" : (q.total ? "Local queue" : "");
  }
  if (q.items && q.items.length) {
    const qbase = q.base_index || 0;
    $("#upnext").innerHTML = q.items.map((t, i) => `
        <div class="upnext-row ${!queued && i === 0 ? "cur" : ""}"
             data-id="${esc(t.id)}" data-index="${i}">
          <span class="i">${qbase + (q.index || 0) + i + 1}</span>
          <span class="t">${esc(t.title)}
            <div class="a">${esc(t.artist)}</div></span>
        </div>`).join("");
  } else {
    $("#upnext").innerHTML =
        '<div class="upnext-row"><span class="t">Queue appears after playback starts</span></div>';
  }

  if (s.has_artwork && s.artwork_ready_id !== null) {
    if (shownArt !== s.artwork_ready_id) {
      shownArt = s.artwork_ready_id;
      const img = $("#art");
      img.onload = () => { img.style.display = "block"; $("#art-ph").style.display = "none"; };
      img.onerror = () => { img.style.display = "none"; $("#art-ph").style.display = "flex"; };
      img.src = "/api/artwork?vid=" + encodeURIComponent(s.artwork_ready_id);
    }
  } else if (!s.has_artwork) {
    shownArt = null;
    $("#art").style.display = "none";
    $("#art-ph").style.display = "flex";
  }

  const canControl = connected && linkOk;
  for (const id of ["#btn-prev", "#btn-toggle", "#btn-next",
                    "#btn-shuffle", "#btn-repeat",
                    "#btn-vol-up", "#btn-vol-down",
                    "#btn-mute", "#btn-stop", "#btn-current-selection"]) {
    $(id).disabled = !canControl;
  }
  $("#btn-toggle").innerHTML = status === 1
      ? '<i class="bi bi-pause-fill"></i>' : '<i class="bi bi-play-fill"></i>';
  $("#btn-toggle").title = status === 1 ? "Pause" : "Play";
  $("#btn-toggle").setAttribute("aria-label", $("#btn-toggle").title);

  $("#kv-conn").textContent = connected ? "Authenticated" : "Disconnected";
  $("#kv-link").textContent = LINK[s.link_state] ?? "—";
  $("#kv-app").textContent = np.app || "—";
  const dev = s.device || {};
  const power = s.power || {};
  $("#kv-device").textContent = dev.name || "—";
  let battery = power.battery_level == null
      ? (!connected ? "—"
         : power.supported === false ? "not supported" : "waiting…")
      : power.battery_level + "%";
  if (power.battery_level != null) {
    battery += batteryStateIcon(power.charging_state);
    if (power.external_charger) {
      battery += '<i class="bi bi-lightning-charge-fill" role="img" ' +
          'title="external power" aria-label="external power"></i>';
    }
  }
  $("#kv-battery").innerHTML = battery;
  $("#kv-art").textContent = (np.artwork_id ?? "—") + " / " +
      (s.artwork_size ? Math.round(s.artwork_size / 1024) + " KB" : "—");
  $("#kv-event").textContent = s.last_event || "—";
  $("#kv-err").textContent = s.last_error || "—";

  const libs = s.libraries || [];
  if (libs.length) {
    $("#libs").innerHTML = libs.map(l => {
      const name = (l.name || "Unknown library").replace(/[<>&]/g, "");
      const kind = ({
        0: "Local library",
        2: "Radio library",
      })[l.type] || "Library";
      const uuid = (l.uuid || "").replace(/[<>&]/g, "");
      return `<div class="kv" title="${uuid}">
        <span>${name}</span><span>${kind}</span></div>`;
    }).join("");
  } else {
    $("#libs").innerHTML =
        '<div class="kv"><span>Library</span><span>—</span></div>';
  }

  renderMedia(media, s.settings || {});
}

function mediaModeLabel(mode) {
  return ({
    iap2: "iAP2",
    afc: "MediaLibrary (AFC)",
    switching: "Switching…",
    unknown: "Unknown",
  })[mode] || "—";
}

function renderMedia(media, settings) {
  const job = media.job || null;
  const cache = media.cache || {};
  const busy = !!(job &&
      (job.state === "running" || job.state === "cancelling"));
  if (job && job.state === "done" && lastMediaJobState !== "done") {
    setTimeout(refreshLibraryView, 200);
  }
  lastMediaJobState = job ? job.state : null;
  $("#media-mode").textContent = mediaModeLabel(media.mode);
  $("#media-device").textContent = media.device || media.udid || "—";
  $("#media-library-id").textContent = cache.library_id || "—";
  $("#media-paired").textContent = media.paired === true
      ? "Paired" : (media.paired === false ? "Not paired" : "Unknown");
  const libs = (state && state.libraries) || [];
  const localLib = libs.find(l => l.type === 0) || libs[0];
  if (localLib) {
    const libText = `${localLib.name || "Unknown"} · ${libs.length} libraries`;
    $("#iap2-library-state").textContent = libText;
    $("#iap2-library-state").title = libText;
  } else {
    $("#iap2-library-state").textContent = "Not available";
  }

  let libState;
  if (cache.dates_loaded) {
    libState = `Loaded · ${fmtNum(cache.dates_count)} dates`;
  } else if (cache.cache_db) {
    libState = "Not loaded · cache file present";
  } else {
    libState = "No cache file";
  }
  $("#media-library-state").textContent = libState;
  $("#media-library-state").title = libState;

  let artState;
  if (cache.artwork_loaded) {
    artState = `Loaded · ${fmtNum(cache.artwork_items)} items · ` +
        `${fmtNum(cache.artwork_files)} files`;
  } else if (cache.cache_db) {
    artState = "Not loaded · cache file present";
  } else {
    artState = "No cache file";
  }
  $("#media-artwork-state").textContent = artState;
  $("#media-artwork-state").title = artState;

  $("#media-auto-iap2").checked = !!(settings && settings.auto_switch_iap2);
  $("#media-auto-iap2").disabled = busy;

  $("#media-stage").textContent = job
      ? (job.message || job.stage || "—") : "Idle";
  let pct = 0;
  if (job && job.total > 0) {
    pct = Math.max(0, Math.min(100, 100 * job.done / job.total));
  }
  $("#media-bar").style.width = pct.toFixed(1) + "%";

  const stats = (job && job.stats) || {};
  const rows = [];
  const add = (label, value) => {
    if (value !== undefined && value !== null && value !== "") {
      rows.push(`<div class="kv"><span>${label}</span><span>${value}</span></div>`);
    }
  };
  if (job && job.total > 0) {
    const unit = job.unit || "items";
    add("Progress", `${fmtNum(job.done)} / ${fmtNum(job.total)} ${unit}`);
  }
  if (job && job.bytes_total > 0) {
    add("Data", `${fmtBytes(job.bytes_done)} / ${fmtBytes(job.bytes_total)}`);
  }
  add("Library rows", stats.rows !== undefined ? fmtNum(stats.rows) : null);
  add("Matched items",
      stats.matched_library_items !== undefined
          ? fmtNum(stats.matched_library_items) : null);
  add("New dates", stats.new !== undefined ? fmtNum(stats.new) : null);
  add("Updated dates", stats.updated !== undefined ? fmtNum(stats.updated) : null);
  add("Removed dates", stats.removed !== undefined ? fmtNum(stats.removed) : null);
  add("Required artwork",
      stats.required_files !== undefined ? fmtNum(stats.required_files) : null);
  add("Reused artwork",
      stats.reused_files !== undefined ? fmtNum(stats.reused_files) : null);
  add("New artwork", stats.copy_files !== undefined
      ? fmtNum(stats.copy_files) : null);
  add("Copied", stats.copied !== undefined ? fmtNum(stats.copied) : null);
  add("Skipped", stats.skipped !== undefined ? fmtNum(stats.skipped) : null);
  add("Failed", stats.failed !== undefined ? fmtNum(stats.failed) : null);
  add("Thumbnails", stats.thumbs !== undefined ? fmtNum(stats.thumbs) : null);
  add("Deleted", stats.deleted_files !== undefined
      ? fmtNum(stats.deleted_files) : null);
  add("New item mappings", stats.new_items !== undefined
      ? fmtNum(stats.new_items) : null);
  add("Changed item mappings", stats.changed_items !== undefined
      ? fmtNum(stats.changed_items) : null);
  add("Removed item mappings", stats.removed_items !== undefined
      ? fmtNum(stats.removed_items) : null);
  add("New album mappings", stats.new_albums !== undefined
      ? fmtNum(stats.new_albums) : null);
  add("New artwork files", stats.new_files !== undefined
      ? fmtNum(stats.new_files) : null);
  add("Removed files", stats.removed_files !== undefined
      ? fmtNum(stats.removed_files) : null);
  add("Cache items", stats.items !== undefined ? fmtNum(stats.items) : null);
  add("Cache albums", stats.albums !== undefined ? fmtNum(stats.albums) : null);
  add("Cache files", stats.files !== undefined ? fmtNum(stats.files) : null);
  add("Cleared dates", stats.dates_cleared !== undefined
      ? fmtNum(stats.dates_cleared) : null);
  add("Cleared artwork files", stats.artwork_files !== undefined
      ? fmtNum(stats.artwork_files) : null);
  add("Cleared Now Playing", stats.now_playing_files !== undefined
      ? fmtNum(stats.now_playing_files) : null);
  add("Cleared snapshots", stats.snapshot_files !== undefined
      ? fmtNum(stats.snapshot_files) : null);
  add("Cache DB removed", stats.cache_db_removed !== undefined
      ? (stats.cache_db_removed ? "Yes" : "No") : null);
  $("#media-nums").innerHTML = rows.join("");
  $("#media-error").textContent = (job && job.error) || "";

  $("#media-pair").disabled = busy;
  $("#media-quick").disabled = busy;
  $("#media-full").disabled = busy;
  $("#media-load-library").disabled = busy || !cache.cache_db;
  $("#media-update-library").disabled = busy;
  $("#media-update-artwork").disabled = busy;
  $("#media-switch-iap2").disabled = busy || media.mode === "iap2";
  $("#media-clear").disabled = busy;
  const iap2Ready = media.mode === "iap2" && state && state.connected;
  $("#iap2-sync").disabled = busy || !iap2Ready;
  $("#iap2-resync").disabled = busy || !iap2Ready;
  $("#iap2-clear-library").disabled = busy;
  $("#iap2-clear-artwork").disabled = busy;
  $("#media-cancel").style.display = busy ? "inline-flex" : "none";
  $("#media-cancel").disabled = !busy;
}

function tick() {
  if (!state) return;
  const np = state.now_playing || {};
  let elapsed = np.elapsed_ms || 0;
  if (np.status === 1 && np.updated_at) elapsed += Date.now() - np.updated_at;
  const dur = np.duration_ms || 0;
  if (dur > 0) elapsed = Math.min(elapsed, dur);
  const seek = $("#seek");
  const canSeek = !!(state.connected && state.link_state === 2 &&
                     np.seek_available === true && dur > 0);
  seek.disabled = !canSeek;
  seek.max = String(Math.max(1, dur));
  seek.title = canSeek ? "Seek" :
      "Device has not advertised seek support (SetElapsedTimeAvailable)";
  if (!seekDragging) {
    seek.value = String(Math.min(elapsed, Math.max(1, dur)));
    $("#elapsed").textContent = fmt(elapsed);
  }
  $("#duration").textContent = fmt(dur);
}

const seekEl = $("#seek");
seekEl.addEventListener("input", () => {
  seekDragging = true;
  $("#elapsed").textContent = fmt(Number(seekEl.value));
});
seekEl.addEventListener("change", async () => {
  const ms = Number(seekEl.value);
  seekDragging = false;
  try {
    const j = await postJson("/api/seek", {elapsed_ms: ms});
    if (!j.ok) toast(j.error || "Seek failed");
    setTimeout(poll, 200);
  } catch (e) {
    toast("Seek failed");
  }
});
seekEl.addEventListener("pointercancel", () => { seekDragging = false; });

async function command(cmd) {
  try {
    const r = await fetch("/api/command", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({command: cmd}),
    });
    const j = await r.json();
    if (!j.ok) toast(j.error || "Command failed");
    setTimeout(poll, 250);
  } catch (e) {
    toast("Failed to send command");
  }
}

$("#btn-prev").onclick = () => command("previous");
$("#btn-next").onclick = () => command("next");
$("#btn-toggle").onclick = () => command(
    state && state.now_playing && state.now_playing.status === 1 ? "pause" : "play");
$("#btn-shuffle").onclick = () => command("shuffle");
$("#btn-repeat").onclick = () => command("repeat");
$("#btn-vol-down").onclick = () => command("volume_down");
$("#btn-vol-up").onclick = () => command("volume_up");
$("#btn-mute").onclick = () => command("mute");
$("#btn-stop").onclick = () => command("stop");
$("#btn-current-selection").onclick = async () => {
  try {
    const j = await postJson("/api/play", {current_selection: true});
    toast(j.ok ? "Resume iPod queue" : (j.error || "Playback failed"));
    setTimeout(poll, 500);
  } catch (e) { toast("Playback failed"); }
};

// -- media library ---------------------------------------------------------
let library = null;
let currentDetail = null;
let activeTab = "albums";
let albumSort = "recent";
let albumDir = "desc";
let lastAlbumKey = null;

let artistsOffset = 0;
let artistsTotal = 0;
let artistsQuery = null;
let albumArtistsOffset = 0;
let albumArtistsTotal = 0;
let albumArtistsQuery = null;
let genresOffset = 0;
let genresTotal = 0;
let genresQuery = null;
let recentOffset = 0;
let recentQuery = "";
let recentTotal = 0;
let songsOffset = 0;
let songsTotal = 0;
let songsQuery = null;

function esc(s) {
  return (s || "").replace(/[<>&"]/g,
      c => ({"<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;"}[c]));
}

function fmtDuration(ms) {
  const s = Math.max(0, Math.round((ms || 0) / 1000));
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

function fmtDate(ts) {
  if (!ts) return "—";
  const d = new Date(Number(ts) * 1000);
  return isNaN(d.getTime()) ? "—" : d.toISOString().slice(0, 10);
}

const TABLE_COLS = {
 albums: [
    {label: "Album", cls: "title"},
    {label: "Artist", cls: "artist"},
    {label: "Tracks", cls: "num"},
  ],
  artists: [
    {label: "Artist", cls: "title"},
    {label: "Albums", cls: "num"},
    {label: "Tracks", cls: "num"},
  ],
  albumArtists: [
    {label: "Album Artist", cls: "title"},
    {label: "Albums", cls: "num"},
    {label: "Tracks", cls: "num"},
  ],
  genres: [
    {label: "Genre", cls: "title"},
    {label: "Albums", cls: "num"},
    {label: "Tracks", cls: "num"},
  ],
  songs: [
    {label: "#", cls: "num col-index"},
    {label: "Song", cls: "title"},
    {label: "Artist", cls: "artist"},
    {label: "Album", cls: "album-col"},
    {label: "Time", cls: "num"},
    {label: "", cls: "actions"},
  ],
  recent: [
    {label: "#", cls: "num col-index"},
    {label: "Song", cls: "title"},
    {label: "Artist", cls: "artist"},
    {label: "Album", cls: "album-col"},
    {label: "Time", cls: "num"},
    {label: "Added", cls: "num date-col"},
    {label: "", cls: "actions"},
  ],
  localPlaylist: [
    {label: "#", cls: "num col-index"},
    {label: "Song", cls: "title"},
    {label: "Artist", cls: "artist"},
    {label: "Album", cls: "album-col"},
    {label: "Time", cls: "num"},
    {label: "", cls: "actions"},
  ],
  playlists: [
    {label: "Playlist", cls: "title"},
    {label: "Tracks", cls: "num"},
  ],
  detailAlbums: [
    {label: "Album", cls: "title"},
    {label: "Artist", cls: "artist"},
    {label: "Tracks", cls: "num"},
    {label: "", cls: "actions"},
  ],
};

function mediaTable(headers, rows, emptyText) {
  const head = headers.map(h =>
      `<th class="${h.cls || ""}">${h.label}</th>`).join("");
  const body = rows.length ? rows.join("") :
      `<tr class="empty"><td colspan="${headers.length}">` +
      `${esc(emptyText || "—")}</td></tr>`;
  return `<div class="media-wrap"><table class="media-table">` +
      `<thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function appendMediaRows(container, headers, rows, emptyText) {
  let table = container.querySelector("table.media-table");
  if (!table) {
    container.innerHTML = mediaTable(headers, rows, emptyText);
    return;
  }
  if (!rows.length) return;
  const tbody = table.tBodies[0];
  const empty = tbody.querySelector("tr.empty");
  if (empty) empty.remove();
  tbody.insertAdjacentHTML("beforeend", rows.join(""));
}

async function apiJson(url, opts) {
  const r = await fetch(url, opts);
  return r.json();
}

async function postJson(url, body) {
  return apiJson(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body || {}),
  });
}

async function pollLibrary() {
  try {
    library = await apiJson("/api/library", {cache: "no-store"});
    updateLibraryStat();
    if (activeTab === "albums") renderAlbums();
  } catch (e) { /* keep the previous list */ }
}

async function refreshLibraryView() {
  await pollLibrary();
  renderActiveTab(true);
}

function updateLibraryStat() {
  if (!library) return;
  $("#lib-tracks").textContent =
      Number(library.track_count || 0).toLocaleString();
  $("#lib-albums-count").textContent =
      Number(library.album_count || 0).toLocaleString();
  $("#lib-sync").textContent = library.syncing ? "Syncing…" : "Idle";
  $("#lib-rev").textContent = library.revision || "—";
}

function renderActiveTab(force) {
  if (activeTab === "albums") renderAlbums();
  else if (activeTab === "artists") loadArtists(true);
  else if (activeTab === "album-artists") loadAlbumArtists(true);
  else if (activeTab === "genres") loadGenres(true);
  else if (activeTab === "recent") loadRecent(true);
  else if (activeTab === "songs") loadSongs(true);
  else if (activeTab === "localPlaylist") loadLocalPlaylist();
  else if (activeTab === "playlists") loadPlaylists();
}

function setTab(name) {
  activeTab = name;
  document.querySelectorAll(".tab").forEach(b =>
      b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab-pane").forEach(p =>
      p.classList.toggle("active", p.id === "pane-" + name));
  const ph = {
    albums: "Search albums / artists…",
    artists: "Search artists…",
    "album-artists": "Search album artists…",
    genres: "Search genres…",
    songs: "Search songs / artists / albums…",
    recent: "Search recently added…",
    localPlaylist: "Search localPlaylist…",
    playlists: "Search device playlists…",
  }[name] || "Search…";
  $("#lib-search").placeholder = ph;
  $("#lib-search").value = "";
  renderActiveTab(true);
}

function albumSeenLabel(a) {
  const lo = Number(a.first_seen_seq) || 0;
  const hi = Number(a.last_seen_seq) || 0;
  if (!lo && !hi) return "—";
  if (!hi || hi === lo) return `#${lo || hi}`;
  return `#${lo}–${hi}`;
}

function albumRecencyCompare(a, b) {
  const ad = Number(a.date_added) || 0;
  const bd = Number(b.date_added) || 0;
  const as = Number(a.last_seen_seq || a.first_seen_seq) || 0;
  const bs = Number(b.last_seen_seq || b.first_seen_seq) || 0;
  // No AFC date means the album was discovered after the last AFC sync;
  // treat it as newer and order those albums by iAP2 seen sequence.
  if (!ad || !bd) {
    if (!ad && !bd) return as - bs;
    return ad ? -1 : 1;
  }
  if (ad !== bd) return ad - bd;
  if (as !== bs) return as - bs;
  return 0;
}

function compareAlbums(a, b) {
  if (albumSort === "name" || albumSort === "artist") {
    const dir = albumDir === "asc" ? 1 : -1;
    const av = albumSort === "name"
        ? (a.album || "").toLowerCase()
        : (a.artist || "").toLowerCase();
    const bv = albumSort === "name"
        ? (b.album || "").toLowerCase()
        : (b.artist || "").toLowerCase();
    if (av < bv) return -1 * dir;
    if (av > bv) return 1 * dir;
  } else {
    let cmp = albumRecencyCompare(a, b);
    if (albumDir === "desc") cmp = -cmp;
    if (cmp !== 0) return cmp;
  }
  return (a.album || "").localeCompare(b.album || "") ||
         (a.artist || "").localeCompare(b.artist || "");
}

function updateAlbumSortUI() {
  const sort = $("#albums-sort");
  const dir = $("#albums-dir");
  if (sort) sort.value = albumSort;
  if (dir) dir.value = albumDir;
}

function renderAlbums() {
  if (!library) return;
  const q = ($("#lib-search").value || "").trim().toLowerCase();
  const albums = (library.albums || []).filter(a =>
      !q || (a.album || "").toLowerCase().includes(q) ||
      (a.artist || "").toLowerCase().includes(q));
  // Rebuild only when the library revision or the search term changes;
  // otherwise keep the current DOM (and scroll position) intact.
  const key = (library.version || "") + "|" + (library.revision || "") +
      "|" + lastLibraryId + "|" + q + "|" + albumSort + "|" + albumDir;
  if (key === lastAlbumKey) return;
  lastAlbumKey = key;
  albums.sort(compareAlbums);
  $("#lib-albums").innerHTML = albums.map(a => `
      <div class="album album-card" data-pid="${esc(a.pid || "")}"
           data-artist="${esc(a.artist)}" data-album="${esc(a.album)}">
        <div class="album-cover ${a.pid ? "" : "missing"}">
          ${a.pid ? `<img loading="lazy"
            src="/api/library/artwork?album_pid=${encodeURIComponent(a.pid)}&size=thumb"
            alt="" onerror="this.style.display='none';this.parentElement.classList.add('missing')">` : ""}
        </div>
        <div class="album-name">${esc(a.album || "—")}</div>
        <div class="album-artist">${esc(a.artist || "")}</div>
        <div class="album-meta">${a.tracks || 0} tracks</div>
        <div class="album-dates"
             title="iAP2 seen: ${albumSeenLabel(a)} · AFC date added: ${fmtDate(a.date_added)}">
          <span>Seen ${albumSeenLabel(a)}</span>
          <span>Added ${fmtDate(a.date_added)}</span>
        </div>
      </div>`).join("") || '<div class="empty">No albums</div>';
  updateAlbumSortUI();
  highlightAlbum();
}

function highlightAlbum() {
  if (!currentDetail || currentDetail.type !== "album") return;
  const a = currentDetail.data;
  for (const el of document.querySelectorAll("#lib-albums .album")) {
    const match = a.pid
        ? el.dataset.pid === a.pid
        : (el.dataset.album === a.album && el.dataset.artist === a.artist);
    el.classList.toggle("active", match);
  }
}

async function loadArtists(reset) {
  const q = ($("#lib-search").value || "").trim().toLowerCase();
  if (reset || q !== artistsQuery) {
    artistsOffset = 0;
    artistsQuery = q;
    $("#lib-artists").innerHTML = "";
  }
  try {
    const d = await apiJson(
        `/api/library/artists?q=${encodeURIComponent(q)}` +
        `&limit=100&offset=${artistsOffset}`, {cache: "no-store"});
    artistsTotal = d.total || 0;
    const rows = (d.artists || []).map(a => `
        <tr class="album" data-artist-pid="${esc(a.pid || "")}"
            data-artist-name="${esc(a.name)}">
          <td class="tt">${esc(a.name || "—")}</td>
          <td class="num">${a.albums || 0}</td>
          <td class="num">${a.tracks || 0}</td>
        </tr>`);
    appendMediaRows($("#lib-artists"), TABLE_COLS.artists, rows,
                    "No artists");
    artistsOffset += (d.artists || []).length;
    $("#btn-more-artists").style.display =
        artistsOffset < artistsTotal ? "" : "none";
  } catch (e) {
    toast("Artist load failed");
  }
}

async function loadAlbumArtists(reset) {
  const q = ($("#lib-search").value || "").trim().toLowerCase();
  if (reset || q !== albumArtistsQuery) {
    albumArtistsOffset = 0;
    albumArtistsQuery = q;
    $("#lib-album-artists").innerHTML = "";
  }
  try {
    const d = await apiJson(
        `/api/library/album-artists?q=${encodeURIComponent(q)}` +
        `&limit=100&offset=${albumArtistsOffset}`, {cache: "no-store"});
    albumArtistsTotal = d.total || 0;
    const rows = (d.album_artists || []).map(a => `
        <tr class="album" data-album-artist-name="${esc(a.name)}">
          <td class="tt">${esc(a.name || "—")}</td>
          <td class="num">${a.albums || 0}</td>
          <td class="num">${a.tracks || 0}</td>
        </tr>`);
    appendMediaRows($("#lib-album-artists"), TABLE_COLS.albumArtists, rows,
                    "No album artists");
    albumArtistsOffset += (d.album_artists || []).length;
    $("#btn-more-album-artists").style.display =
        albumArtistsOffset < albumArtistsTotal ? "" : "none";
  } catch (e) {
    toast("Album artist load failed");
  }
}

async function loadGenres(reset) {
  const q = ($("#lib-search").value || "").trim().toLowerCase();
  if (reset || q !== genresQuery) {
    genresOffset = 0;
    genresQuery = q;
    $("#lib-genres").innerHTML = "";
  }
  try {
    const d = await apiJson(
        `/api/library/genres?q=${encodeURIComponent(q)}` +
        `&limit=100&offset=${genresOffset}`, {cache: "no-store"});
    genresTotal = d.total || 0;
    const rows = (d.genres || []).map(g => `
        <tr class="album" data-genre-name="${esc(g.name)}">
          <td class="tt">${esc(g.name || "—")}</td>
          <td class="num">${g.albums || 0}</td>
          <td class="num">${g.tracks || 0}</td>
        </tr>`);
    appendMediaRows($("#lib-genres"), TABLE_COLS.genres, rows,
                    "No genres");
    genresOffset += (d.genres || []).length;
    $("#btn-more-genres").style.display =
        genresOffset < genresTotal ? "" : "none";
  } catch (e) {
    toast("Genre load failed");
  }
}

async function loadRecent(reset) {
  const q = ($("#lib-search").value || "").trim().toLowerCase();
  if (reset || q !== recentQuery) {
    recentOffset = 0;
    recentQuery = q;
    $("#lib-recent").innerHTML = "";
  }
  try {
    const d = await apiJson(
        `/api/library/recent?limit=100&offset=${recentOffset}` +
        `&q=${encodeURIComponent(q)}`,
        {cache: "no-store"});
    recentTotal = d.total || 0;
    $("#recent-note").textContent = d.note || "";
    const rows = (d.items || []).map((t, i) =>
        trackRow(t, recentOffset + i + 1, null, true));
    appendMediaRows($("#lib-recent"), TABLE_COLS.recent, rows,
                    "No recently added tracks");
    recentOffset += (d.items || []).length;
    $("#btn-more-recent").style.display =
        recentOffset < recentTotal ? "" : "none";
  } catch (e) {
    toast("Recently added load failed");
  }
}

async function loadSongs(reset) {
  const q = ($("#lib-search").value || "").trim().toLowerCase();
  if (reset || q !== songsQuery) {
    songsOffset = 0;
    songsQuery = q;
    $("#lib-songs").innerHTML = "";
  }
  try {
    const sort = ($("#songs-sort") && $("#songs-sort").value) || "title";
    const d = await apiJson(
        `/api/library/songs?q=${encodeURIComponent(q)}` +
        `&limit=100&offset=${songsOffset}` +
        `&sort=${encodeURIComponent(sort)}`, {cache: "no-store"});
    songsTotal = d.total || 0;
    const rows = (d.items || []).map((t, i) =>
        trackRow(t, songsOffset + i + 1));
    appendMediaRows($("#lib-songs"), TABLE_COLS.songs, rows,
                    "No matching songs");
    songsOffset += (d.items || []).length;
    $("#btn-more-songs").style.display =
        songsOffset < songsTotal ? "" : "none";
  } catch (e) {
    toast("Song load failed");
  }
}


async function loadLocalPlaylist() {
  try {
    const d = await apiJson("/api/playlists/localPlaylist",
                            {cache: "no-store"});
    if (!d.ok) {
      $("#lib-localPlaylist").innerHTML =
          mediaTable(TABLE_COLS.localPlaylist, [], "localPlaylist unavailable");
      return;
    }
    const q = ($("#lib-search").value || "").trim().toLowerCase();
    const items = (d.items || []).filter(t =>
        !q || (t.title || "").toLowerCase().includes(q) ||
        (t.artist || "").toLowerCase().includes(q) ||
        (t.album || "").toLowerCase().includes(q));
    $("#lib-localPlaylist").innerHTML = mediaTable(
        TABLE_COLS.localPlaylist,
        items.map((t, i) => trackRow(
            t, i + 1,
            '<button class="icon-btn" data-remove="1" title="Remove"><i class="bi bi-dash-lg"></i></button>')),
        "localPlaylist is empty");
    $("#localPlaylist-note").textContent =
        `${(d.items || []).length} tracks · CSV-backed`;
  } catch (e) {
    $("#lib-localPlaylist").innerHTML =
        mediaTable(TABLE_COLS.localPlaylist, [], "localPlaylist load failed");
  }
}

async function loadPlaylists() {
  const q = ($("#lib-search").value || "").trim().toLowerCase();
  const rows = [];
  const dev = (library && library.playlists) || [];
  for (const p of dev) {
    if (q && !(p.name || "").toLowerCase().includes(q)) continue;
    rows.push(`
      <tr class="album" data-device-playlist="1"
          data-device-playlist-id="${esc(p.id)}">
        <td class="tt">${esc(p.name || "—")}</td>
        <td class="num">${p.tracks || 0}</td>
      </tr>`);
  }
  $("#lib-playlists").innerHTML =
      mediaTable(TABLE_COLS.playlists, rows, "No device playlists");
}

function trackRow(t, idx, actions, withDate) {
  return `
    <tr class="track" data-id="${esc(t.id)}">
      <td class="num col-index">${idx}</td>
      <td class="tt">${esc(t.title || "—")}</td>
      <td class="artist">${esc(t.artist || "")}</td>
      <td class="album-col">${esc(t.album || "")}</td>
      <td class="num">${fmtDuration(t.duration_ms)}</td>
      ${withDate ? `<td class="num date-col">${fmtDate(t.date_added)}</td>` : ""}
      <td class="actions">
        ${actions || ""}
        <button class="icon-btn" data-queue="1" title="Add to queue"
                aria-label="Add to queue"><i class="bi bi-plus-lg"></i></button>
        <span class="play" title="Play"><i class="bi bi-play-fill"></i></span>
      </td>
    </tr>`;
}

function setPlayDetail(label) {
  $("#btn-play-detail").innerHTML =
      '<i class="bi bi-play-fill"></i> ' + label;
}

function showDetail() {
  const cover = $("#detail-cover");
  const d = currentDetail;
  if (d && d.type === "album" && d.data && d.data.pid) {
    cover.src = "/api/library/artwork?album_pid=" +
        encodeURIComponent(d.data.pid) + "&size=full";
    cover.style.display = "block";
  } else {
    cover.style.display = "none";
    cover.removeAttribute("src");
  }
  const canQueue = !!(d && (d.type === "album" || d.type === "playlist" ||
      d.type === "device-playlist") && (d.items || []).length);
  $("#btn-queue-detail").style.display = canQueue ? "" : "none";
  $("#lib-detail").classList.add("show");
}

function hideDetail() {
  $("#lib-detail").classList.remove("show");
  currentDetail = null;
  highlightAlbum();
}

async function showAlbum(pid, artist, album) {
  const qs = new URLSearchParams();
  if (pid) qs.set("pid", pid);
  else { qs.set("artist", artist || ""); qs.set("album", album || ""); }
  try {
    const d = await apiJson("/api/library/album?" + qs.toString(),
                            {cache: "no-store"});
    if (!d.ok) { toast(d.error || "Album load failed"); return; }
    currentDetail = {type: "album", data: d.album, items: d.items};
    $("#detail-title").textContent = d.album.album || album || "Unknown album";
    $("#detail-sub").textContent =
        (d.album.artist || artist || "") + " · " + d.items.length + " tracks";
    setPlayDetail("Play album");
    $("#btn-play-detail").style.display = d.album.pid ? "" : "none";
    $("#detail-actions").innerHTML = "";
    $("#detail-tracks").innerHTML = mediaTable(
        TABLE_COLS.songs,
        d.items.map((t, i) => trackRow(t, t.track || (i + 1))),
        "No tracks");
    showDetail();
    highlightAlbum();
  } catch (e) {
    toast("Album load failed");
  }
}

async function showArtist(pid, name) {
  const qs = new URLSearchParams();
  if (pid) qs.set("pid", pid);
  else qs.set("name", name || "");
  try {
    const d = await apiJson("/api/library/artist?" + qs.toString(),
                            {cache: "no-store"});
    if (!d.ok) { toast(d.error || "Artist load failed"); return; }
    currentDetail = {type: "artist", data: d.artist, items: d.albums};
    $("#detail-title").textContent = d.artist.name || name || "Unknown artist";
    $("#detail-sub").textContent =
        `${d.artist.albums} albums · ${d.artist.tracks} tracks`;
    setPlayDetail("Play artist");
    $("#btn-play-detail").style.display = d.artist.pid ? "" : "none";
    $("#detail-actions").innerHTML = "";
    $("#detail-tracks").innerHTML = mediaTable(
        TABLE_COLS.detailAlbums, (d.albums || []).map(albumRow),
        "No albums");
    showDetail();
  } catch (e) {
    toast("Artist load failed");
  }
}

function albumRow(a) {
  return `
    <tr class="track" data-album-pid="${esc(a.pid || "")}"
        data-album-artist="${esc(a.artist)}"
        data-album-title="${esc(a.album)}">
      <td class="tt">${esc(a.album || "—")}</td>
      <td class="artist">${esc(a.artist || "")}</td>
      <td class="num">${a.tracks || 0}</td>
      <td class="actions"><span class="play" title="Play"><i class="bi bi-play-fill"></i></span></td>
    </tr>`;
}

async function showGenre(name) {
  try {
    const d = await apiJson(
        `/api/library/genre?name=${encodeURIComponent(name)}`,
        {cache: "no-store"});
    if (!d.ok) { toast(d.error || "Genre load failed"); return; }
    currentDetail = {type: "genre", data: d.genre, items: d.albums};
    $("#detail-title").textContent = name;
    $("#detail-sub").textContent =
        `${d.genre.albums} albums · ${d.genre.tracks} tracks`;
    setPlayDetail("Play genre");
    $("#btn-play-detail").style.display = d.genre.tracks ? "" : "none";
    $("#detail-actions").innerHTML = "";
    $("#detail-tracks").innerHTML = mediaTable(
        TABLE_COLS.detailAlbums, (d.albums || []).map(albumRow),
        "No albums");
    showDetail();
  } catch (e) {
    toast("Genre load failed");
  }
}

async function showAlbumArtist(name) {
  try {
    const d = await apiJson(
        `/api/library/album-artist?name=${encodeURIComponent(name)}`,
        {cache: "no-store"});
    if (!d.ok) { toast(d.error || "Album artist load failed"); return; }
    currentDetail = {type: "album-artist", data: d.album_artist,
                     items: d.albums};
    $("#detail-title").textContent = name;
    $("#detail-sub").textContent =
        `${d.album_artist.albums} albums · ${d.album_artist.tracks} tracks`;
    setPlayDetail("Play album artist");
    $("#btn-play-detail").style.display =
        d.album_artist.tracks ? "" : "none";
    $("#detail-actions").innerHTML = "";
    $("#detail-tracks").innerHTML = mediaTable(
        TABLE_COLS.detailAlbums, (d.albums || []).map(albumRow),
        "No albums");
    showDetail();
  } catch (e) {
    toast("Album artist load failed");
  }
}

async function showPlaylist(plId) {
  try {
    const d = await apiJson(`/api/playlists/${encodeURIComponent(plId)}`,
                            {cache: "no-store"});
    if (!d.ok) { toast(d.error || "Playlist load failed"); return; }
    currentDetail = {type: "playlist", data: d.playlist, items: d.items};
    $("#detail-title").textContent = d.playlist.name || "Playlist";
    $("#detail-sub").textContent =
        `${d.playlist.tracks} tracks` +
        (d.playlist.missing ? ` · ${d.playlist.missing} missing` : "");
    setPlayDetail("Play localPlaylist");
    $("#btn-play-detail").style.display = d.items.length ? "" : "none";
    $("#detail-actions").innerHTML = "";
    $("#detail-tracks").innerHTML = mediaTable(
        TABLE_COLS.songs,
        d.items.map((t, i) => {
          const actions = `
              <button class="icon-btn" data-move-up="1" title="Move up"><i class="bi bi-arrow-up"></i></button>
              <button class="icon-btn" data-move-down="1" title="Move down"><i class="bi bi-arrow-down"></i></button>
              <button class="icon-btn" data-remove="1" title="Remove"><i class="bi bi-dash-lg"></i></button>`;
          return trackRow(t, i + 1, actions);
        }),
        "Playlist is empty");
    showDetail();
  } catch (e) {
    toast("Playlist load failed");
  }
}


async function showDevicePlaylist(id) {
  try {
    const d = await apiJson(
        `/api/library/device-playlist?id=${encodeURIComponent(id)}`,
        {cache: "no-store"});
    if (!d.ok) { toast(d.error || "Failed to load device playlist"); return; }
    currentDetail = {type: "device-playlist", data: d.playlist,
                     items: d.items};
    $("#detail-title").textContent = d.playlist.name || "Device playlist";
    $("#detail-sub").textContent =
        `${d.playlist.tracks} tracks · Managed by iPod`;
    setPlayDetail("Play device playlist");
    $("#btn-play-detail").style.display = d.items.length ? "" : "none";
    $("#detail-actions").innerHTML = "";
    $("#detail-tracks").innerHTML = mediaTable(
        TABLE_COLS.songs, d.items.map((t, i) => trackRow(t, i + 1)),
        "Playlist is empty or not synced yet");
    showDetail();
  } catch (e) {
    toast("Failed to load device playlist");
  }
}

async function playItem(id) {
  try {
    const j = await postJson("/api/play", {item_id: id});
    toast(j.ok ? "Playback started" : (j.error || "Playback failed"));
    setTimeout(poll, 400);
  } catch (e) {
    toast("Playback failed");
  }
}

async function addToQueue(ids) {
  ids = (ids || []).filter(Boolean);
  if (!ids.length) return;
  try {
    const j = await postJson("/api/queue/add", {item_ids: ids});
    if (!j.ok) { toast(j.error || "Add to queue failed"); return; }
    if (!j.added) { toast("No matching tracks"); return; }
    let msg = `Queued ${j.added} ` + (j.added === 1 ? "track" : "tracks");
    if (j.missing) msg += ` · ${j.missing} missing`;
    msg += ` · ${j.total} in queue`;
    toast(msg);
    setTimeout(poll, 250);
  } catch (e) {
    toast("Add to queue failed");
  }
}

async function playQueue(startIndex) {
  try {
    const j = await postJson("/api/queue/play",
                             {start_index: startIndex || 0});
    toast(j.ok ? `Playing queue (${j.count} tracks)`
               : (j.error || "Playback failed"));
    setTimeout(poll, 500);
  } catch (e) {
    toast("Playback failed");
  }
}

async function playPlaylist(plId, startIndex) {
  try {
    const j = await postJson(
        `/api/playlists/${encodeURIComponent(plId)}/play`,
        {start_index: startIndex || 0});
    toast(j.ok ? `Playing (${j.played} tracks)` : (j.error || "Playback failed"));
    setTimeout(poll, 500);
  } catch (e) {
    toast("Playback failed");
  }
}

async function openAddModal(ids, title) {
  try {
    const j = await postJson("/api/playlists/localPlaylist/add",
                             {item_ids: ids});
    if (!j.ok) { toast(j.error || "Add failed"); return; }
    const parts = [];
    if (j.added) parts.push(`Added ${j.added} tracks`);
    if (j.already) parts.push(`${j.already} already present`);
    if (j.missing) parts.push(`${j.missing} missing`);
    toast(parts.join(", ") || "Added to localPlaylist");
    if (activeTab === "localPlaylist") loadLocalPlaylist();
  } catch (e) {
    toast("Add failed");
  }
}

// -- tab / view events -----------------------------------------------------
document.querySelectorAll(".tab").forEach(btn => {
  btn.onclick = () => setTab(btn.dataset.tab);
});

let searchTimer = null;
$("#lib-search").oninput = () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    if (activeTab === "albums") renderAlbums();
    else if (activeTab === "artists") loadArtists(true);
    else if (activeTab === "album-artists") loadAlbumArtists(true);
    else if (activeTab === "genres") loadGenres(true);
    else if (activeTab === "songs") loadSongs(true);
    else if (activeTab === "recent") loadRecent(true);
    else if (activeTab === "localPlaylist") loadLocalPlaylist();
    else loadPlaylists();
  }, 250);
};

$("#btn-more-artists").onclick = () => loadArtists(false);
$("#btn-more-album-artists").onclick = () => loadAlbumArtists(false);
$("#btn-more-genres").onclick = () => loadGenres(false);
$("#btn-more-songs").onclick = () => loadSongs(false);
$("#btn-more-recent").onclick = () => loadRecent(false);
$("#songs-sort").onchange = () => loadSongs(true);

$("#albums-sort").onchange = () => {
  albumSort = $("#albums-sort").value;
  lastAlbumKey = null;
  renderAlbums();
};

$("#albums-dir").onchange = () => {
  albumDir = $("#albums-dir").value;
  lastAlbumKey = null;
  renderAlbums();
};

$("#lib-albums").onclick = (ev) => {
  const el = ev.target.closest(".album[data-album]");
  if (!el) return;
  showAlbum(el.dataset.pid || "", el.dataset.artist || "",
            el.dataset.album || "");
};

$("#lib-artists").onclick = (ev) => {
  const el = ev.target.closest(".album[data-artist-name]");
  if (!el) return;
  showArtist(el.dataset.artistPid || "", el.dataset.artistName || "");
};

$("#lib-album-artists").onclick = (ev) => {
  const el = ev.target.closest(".album[data-album-artist-name]");
  if (el) showAlbumArtist(el.dataset.albumArtistName || "");
};

$("#lib-genres").onclick = (ev) => {
  const el = ev.target.closest(".album[data-genre-name]");
  if (el) showGenre(el.dataset.genreName || "");
};

async function jumpQueue(index) {
  try {
    const j = await postJson("/api/seek", {queue_index: index});
    if (!j.ok) toast(j.error || "Queue jump failed");
    setTimeout(poll, 250);
  } catch (e) {
    toast("Queue jump failed");
  }
}

$("#upnext").onclick = (ev) => {
  const el = ev.target.closest(".upnext-row[data-id]");
  if (!el) return;
  const q = state && state.queue;
  if (q && q.source === "queued") {
    playQueue(Number(el.dataset.index) || 0);
  } else if (q && q.source === "device" &&
             state.now_playing && state.now_playing.seek_available === true) {
    const target = (q.base_index || 0) + (q.index || 0) +
        (Number(el.dataset.index) || 0);
    jumpQueue(target);
  } else {
    playItem(el.dataset.id);
  }
};

// Queue buttons in track tables must not trigger the row's play handler.
document.addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-queue]");
  if (!btn) return;
  const row = btn.closest(".track[data-id]");
  if (!row) return;
  ev.preventDefault();
  ev.stopPropagation();
  addToQueue([row.dataset.id]);
}, true);

$("#lib-recent").onclick = (ev) => {
  const add = ev.target.closest(".add-btn");
  if (add) {
    ev.stopPropagation();
    const row = add.closest(".track[data-id]");
    if (row) {
      const title = row.querySelector(".tt")?.childNodes[0]?.textContent || "";
      openAddModal([row.dataset.id], title.trim());
    }
    return;
  }
  const row = ev.target.closest(".track[data-id]");
  if (row) playItem(row.dataset.id);
};

$("#lib-songs").onclick = (ev) => {
  const add = ev.target.closest(".add-btn");
  if (add) {
    ev.stopPropagation();
    const row = add.closest(".track[data-id]");
    if (row) {
      const title = row.querySelector(".tt")?.childNodes[0]?.textContent || "";
      openAddModal([row.dataset.id], title.trim());
    }
    return;
  }
  const row = ev.target.closest(".track[data-id]");
  if (row) playItem(row.dataset.id);
};

$("#lib-playlists").onclick = (ev) => {
  const dev = ev.target.closest(".album[data-device-playlist-id]");
  if (dev) { showDevicePlaylist(dev.dataset.devicePlaylistId); return; }
  const el = ev.target.closest(".album[data-playlist-id]");
  if (el) showPlaylist(el.dataset.playlistId);
};

$("#lib-localPlaylist").onclick = async (ev) => {
  const row = ev.target.closest(".track[data-id]");
  if (!row) return;
  if (ev.target.closest("[data-remove]")) {
    ev.stopPropagation();
    try {
      const j = await postJson("/api/playlists/localPlaylist/remove",
                               {item_id: row.dataset.id});
      toast(j.ok ? "Removed" : (j.error || "Remove failed"));
      loadLocalPlaylist();
    } catch (e) { toast("Remove failed"); }
    return;
  }
  playItem(row.dataset.id);
};

$("#btn-play-localPlaylist").onclick = async () => {
  try {
    const j = await postJson("/api/playlists/localPlaylist/play",
                             {start_index: 0});
    toast(j.ok ? `Playing ${j.played} tracks`
               : (j.error || "Playback failed"));
    setTimeout(poll, 500);
  } catch (e) { toast("Playback failed"); }
};

$("#detail-tracks").onclick = async (ev) => {
  const add = ev.target.closest(".add-btn");
  if (add) {
    ev.stopPropagation();
    const row = add.closest(".track[data-id]");
    if (row) {
      const title = row.querySelector(".tt")?.childNodes[0]?.textContent || "";
      openAddModal([row.dataset.id], title.trim());
    }
    return;
  }
  if (!currentDetail) return;
  if (currentDetail.type === "playlist") {
    const plId = currentDetail.data.id;
    const row = ev.target.closest(".track[data-id]");
    if (!row) return;
    if (ev.target.closest("[data-remove]")) {
      ev.stopPropagation();
      try {
        await postJson(`/api/playlists/${plId}/remove`,
                       {item_id: row.dataset.id});
        showPlaylist(plId);
        loadPlaylists();
      } catch (e) { toast("Remove failed"); }
      return;
    }
    if (ev.target.closest("[data-move-up]") ||
        ev.target.closest("[data-move-down]")) {
      ev.stopPropagation();
      const rows = [...document.querySelectorAll(
          "#detail-tracks .track[data-id]")];
      const from = rows.indexOf(row);
      const to = ev.target.closest("[data-move-up]") ? from - 1 : from + 1;
      if (to < 0 || to >= rows.length) return;
      try {
        await postJson(`/api/playlists/${plId}/move`, {from, to});
        showPlaylist(plId);
      } catch (e) { toast("Move failed"); }
      return;
    }
  }
  const album = ev.target.closest(".track[data-album-pid]");
  if (album) {
    showAlbum(album.dataset.albumPid || "",
              album.dataset.albumArtist || "",
              album.dataset.albumTitle || "");
    return;
  }
  const row = ev.target.closest(".track[data-id]");
  if (row) playItem(row.dataset.id);
};

$("#detail-actions").onclick = async (ev) => {
  const btn = ev.target.closest("button[data-action]");
  if (!btn || !currentDetail) return;
  const action = btn.dataset.action;
  if (action === "add-album" && currentDetail.type === "album") {
    openAddModal(currentDetail.items.map(t => t.id),
                 currentDetail.data.album || "");
    return;
  }
  if (action === "add-smart" && currentDetail.type === "smart") {
    openAddModal(currentDetail.items.map(t => t.id),
                 currentDetail.data.title || "");
    return;
  }
  if (action === "add-device-playlist" &&
      currentDetail.type === "device-playlist") {
    openAddModal(currentDetail.items.map(t => t.id),
                 currentDetail.data.name || "");
    return;
  }
  if (currentDetail.type !== "playlist") return;
  const plId = currentDetail.data.id;
  if (action === "rename-playlist") {
    const name = prompt("New playlist name:", currentDetail.data.name || "");
    if (!name || !name.trim()) return;
    try {
      const j = await postJson(`/api/playlists/${plId}/rename`,
                               {name: name.trim()});
      toast(j.ok ? "Renamed" : (j.error || "Rename failed"));
      if (j.ok) { showPlaylist(plId); loadPlaylists(); }
    } catch (e) { toast("Rename failed"); }
  } else if (action === "clear-playlist") {
    if (!confirm("Clear this playlist?")) return;
    try {
      await postJson(`/api/playlists/${plId}/clear`, {});
      toast("Cleared");
      showPlaylist(plId);
      loadPlaylists();
    } catch (e) { toast("Clear failed"); }
  } else if (action === "delete-playlist") {
    if (!confirm("Delete this playlist?")) return;
    try {
      await fetch(`/api/playlists/${plId}`, {method: "DELETE"});
      toast("Deleted");
      hideDetail();
      loadPlaylists();
    } catch (e) { toast("Delete failed"); }
  }
};

$("#btn-play-detail").onclick = async () => {
  if (!currentDetail) return;
  if (currentDetail.type === "album") {
    if (!currentDetail.data.pid) return;
    try {
      const j = await postJson("/api/play",
                               {album_pid: currentDetail.data.pid});
      toast(j.ok ? "Playing album" : (j.error || "Playback failed"));
      setTimeout(poll, 500);
    } catch (e) { toast("Playback failed"); }
  } else if (currentDetail.type === "artist") {
    if (!currentDetail.data.pid) return;
    try {
      const j = await postJson("/api/play",
                               {artist_pid: currentDetail.data.pid});
      toast(j.ok ? "Playing artist" : (j.error || "Playback failed"));
      setTimeout(poll, 500);
    } catch (e) { toast("Playback failed"); }
  } else if (currentDetail.type === "genre") {
    try {
      const j = await postJson("/api/play",
                               {genre: currentDetail.data.name});
      toast(j.ok ? "Playing genre" : (j.error || "Playback failed"));
      setTimeout(poll, 500);
    } catch (e) { toast("Playback failed"); }
  } else if (currentDetail.type === "album-artist") {
    try {
      const j = await postJson("/api/play",
                               {album_artist: currentDetail.data.name});
      toast(j.ok ? "Playing album artist" : (j.error || "Playback failed"));
      setTimeout(poll, 500);
    } catch (e) { toast("Playback failed"); }
  } else if (currentDetail.type === "smart") {
    try {
      const ids = currentDetail.items.map(t => t.id);
      const j = await postJson("/api/play", {item_ids: ids});
      toast(j.ok ? "Playback started" : (j.error || "Playback failed"));
      setTimeout(poll, 500);
    } catch (e) { toast("Playback failed"); }
  } else if (currentDetail.type === "device-playlist") {
    try {
      const j = await postJson("/api/play",
          {device_playlist_id: currentDetail.data.id});
      toast(j.ok ? "Playing device playlist" : (j.error || "Playback failed"));
      setTimeout(poll, 500);
    } catch (e) { toast("Playback failed"); }
  } else if (currentDetail.type === "playlist") {
    playPlaylist(currentDetail.data.id, 0);
  }
};

$("#btn-detail-close").onclick = hideDetail;

$("#btn-queue-detail").onclick = () => {
  if (!currentDetail) return;
  const ids = (currentDetail.items || []).map(t => t.id).filter(Boolean);
  addToQueue(ids);
};

$("#btn-play-queue").onclick = () => playQueue(0);

$("#btn-clear-queue").onclick = async () => {
  try {
    const j = await postJson("/api/queue/clear", {});
    toast(j.ok ? "Queue cleared" : (j.error || "Clear failed"));
    setTimeout(poll, 250);
  } catch (e) {
    toast("Clear failed");
  }
};

$("#btn-resync").onclick = async () => {
  if (!confirm("Clear the local media mirror and resync from iPod?")) return;
  try {
    const j = await postJson("/api/library/sync", {});
    toast(j.ok ? "Resync requested" : (j.error || "Resync request failed"));
    setTimeout(refreshLibraryView, 1500);
  } catch (e) {
    toast("Resync request failed");
  }
};

async function mediaAction(action) {
  try {
    const j = await postJson("/api/media/action", {action});
    if (!j.ok) {
      toast(j.error || "MediaLibrary request failed");
      return;
    }
    toast(action === "pair" ? "Pairing…" :
          (action === "clear" ? "Clearing local cache…" :
           "MediaLibrary task started"));
    if (j.media) {
      renderMedia(j.media, j.settings || (state && state.settings) || {});
    }
    setTimeout(poll, 400);
  } catch (e) {
    toast("MediaLibrary request failed");
  }
}

async function mediaLoad(target) {
  try {
    const j = await postJson("/api/media/load", {target});
    const any = j.ok && Object.values(j.loaded || {}).some(Boolean);
    toast(j.ok ? (any ? "Cache loaded" : "No cache file to load")
               : (j.error || "Load failed"));
    setTimeout(poll, 300);
    setTimeout(refreshLibraryView, 400);
  } catch (e) {
    toast("Load failed");
  }
}

$("#btn-media").onclick = () => {
  renderMedia((state && state.media) || {},
              (state && state.settings) || {});
  $("#media-modal").classList.add("show");
};
$("#media-close").onclick =
    () => $("#media-modal").classList.remove("show");
$("#media-modal").onclick = (ev) => {
  if (ev.target.id === "media-modal") {
    $("#media-modal").classList.remove("show");
  }
};
$("#media-pair").onclick = () => mediaAction("pair");
$("#media-quick").onclick = () => mediaAction("quick");
$("#media-full").onclick = () => {
  if (confirm("Full Refresh deletes the local artwork cache and " +
              "downloads every cover again. Continue?")) {
    mediaAction("full");
  }
};
$("#media-load-library").onclick = () => mediaLoad("library");
$("#media-update-library").onclick = () => mediaAction("db");
$("#media-update-artwork").onclick = () => mediaAction("artwork");
$("#media-clear").onclick = () => {
  if (confirm("Clear this device's AFC cache: MediaLibrary database " +
              "snapshot, dates/artwork SQLite cache, and library artwork? " +
              "The iAP2 library and artwork caches are kept.")) {
    mediaAction("clear");
  }
};
$("#media-cancel").onclick = async () => {
  try {
    const j = await postJson("/api/media/cancel", {});
    toast(j.ok ? "Cancelling…" : (j.error || "Cancel failed"));
    setTimeout(poll, 300);
  } catch (e) {
    toast("Cancel failed");
  }
};
$("#media-switch-iap2").onclick = async () => {
  try {
    const j = await postJson("/api/media/mode", {mode: "iap2"});
    toast(j.ok ? "Switching to iAP2…" : (j.error || "Switch failed"));
    setTimeout(poll, 300);
  } catch (e) {
    toast("Switch failed");
  }
};
$("#iap2-sync").onclick = async () => {
  try {
    const j = await postJson("/api/library/sync-incremental", {});
    toast(j.ok ? "Incremental sync requested" : (j.error || "Sync failed"));
  } catch (e) {


    toast("Sync failed");
  }
};
$("#iap2-resync").onclick = async () => {
  if (!confirm("Clear the iAP2 revision and request a full media-library " +
               "sync?")) return;
  try {
    const j = await postJson("/api/library/sync", {});
    toast(j.ok ? "Full resync requested" : (j.error || "Resync failed"));
    setTimeout(poll, 500);
  } catch (e) {
    toast("Resync failed");
  }
};
$("#iap2-clear-library").onclick = async () => {
  if (!confirm("Clear the iAP2 media-library mirror for the active device? " +
               "The protocol will perform a full resync.")) return;
  try {
    const j = await postJson("/api/library/clear-iap2", {});
    toast(j.ok ? "iAP2 library cleared; resyncing"
               : (j.error || "Clear failed"));
    setTimeout(poll, 500);
    setTimeout(refreshLibraryView, 1500);
  } catch (e) {
    toast("Clear failed");
  }
};
$("#iap2-clear-artwork").onclick = async () => {
  if (!confirm("Clear the iAP2 Now Playing artwork cache for this device?"))
    return;
  try {
    const j = await postJson("/api/library/clear-artwork", {});
    toast(j.ok ? "iAP2 artwork cache cleared" : (j.error || "Clear failed"));
    setTimeout(poll, 300);
  } catch (e) {
    toast("Clear failed");
  }
};
$("#media-auto-iap2").onchange = async () => {
  const value = $("#media-auto-iap2").checked;
  try {
    const j = await postJson("/api/settings", {auto_switch_iap2: value});
    toast(j.ok ? (value ? "Auto switch enabled" : "Auto switch disabled")
               : (j.error || "Setting save failed"));
    setTimeout(poll, 200);
  } catch (e) {
    toast("Setting save failed");
  }
};

setInterval(pollLibrary, 10000);
pollLibrary();

setInterval(poll, 1000);
setInterval(tick, 250);
poll().then(tick);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "MediaCtrl/1.0"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.encode(), "text/html; charset=utf-8")
        elif path.startswith("/static/"):
            root = os.path.abspath(os.path.join(BASE_DIR, "static"))
            rel = path[len("/static/"):]
            full = os.path.abspath(os.path.join(root, rel))
            if (not full.startswith(root + os.sep)
                    or not os.path.isfile(full)):
                self._json({"error": "not found"}, 404)
                return
            mime = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if full.endswith(".woff2"):
                mime = "font/woff2"
            elif full.endswith(".woff"):
                mime = "font/woff"
            try:
                with open(full, "rb") as fh:
                    blob = fh.read()
            except OSError:
                self._json({"error": "not found"}, 404)
                return
            self._send(200, blob, mime)
        elif path == "/api/state":
            self._json(snapshot())
        elif path == "/api/library":
            summary = library_summary()
            summary["playlists"] = [
                {"id": pl["id"], "name": pl.get("name", ""),
                 "tracks": len(pl.get("items", []))}
                for pl in list(LIBRARY["playlists"].values())[:200]
                if pl.get("name")
            ]
            self._json(summary)
        elif path == "/api/library/album":
            qs = parse_qs(urlparse(self.path).query)
            pid = (qs.get("pid") or [None])[0]
            artist = (qs.get("artist") or [None])[0]
            album = (qs.get("album") or [None])[0]
            self._json(library_album(pid=pid, artist=artist, album=album))
        elif path == "/api/library/artists":
            qs = parse_qs(urlparse(self.path).query)
            self._json(library_artists(
                query=(qs.get("q") or [""])[0],
                limit=(qs.get("limit") or ["200"])[0],
                offset=(qs.get("offset") or ["0"])[0]))
        elif path == "/api/library/artist":
            qs = parse_qs(urlparse(self.path).query)
            pid = (qs.get("pid") or [None])[0]
            name = (qs.get("name") or [None])[0]
            self._json(library_artist(pid=pid, name=name))
        elif path == "/api/library/songs":
            qs = parse_qs(urlparse(self.path).query)
            self._json(library_songs(
                query=(qs.get("q") or [""])[0],
                limit=(qs.get("limit") or ["100"])[0],
                offset=(qs.get("offset") or ["0"])[0],
                sort=(qs.get("sort") or ["title"])[0],
                genre=(qs.get("genre") or [""])[0],
                album_artist=(qs.get("album_artist") or [""])[0]))
        elif path == "/api/library/genres":
            qs = parse_qs(urlparse(self.path).query)
            self._json(library_genres(
                query=(qs.get("q") or [""])[0],
                limit=(qs.get("limit") or ["200"])[0],
                offset=(qs.get("offset") or ["0"])[0]))
        elif path == "/api/library/genre":
            qs = parse_qs(urlparse(self.path).query)
            name = (qs.get("name") or [""])[0]
            self._json(library_genre(name))
        elif path == "/api/library/album-artists":
            qs = parse_qs(urlparse(self.path).query)
            self._json(library_album_artists(
                query=(qs.get("q") or [""])[0],
                limit=(qs.get("limit") or ["200"])[0],
                offset=(qs.get("offset") or ["0"])[0]))
        elif path == "/api/library/album-artist":
            qs = parse_qs(urlparse(self.path).query)
            name = (qs.get("name") or [""])[0]
            self._json(library_album_artist(name))
        elif path == "/api/library/recent":
            qs = parse_qs(urlparse(self.path).query)
            self._json(library_recent(
                limit=(qs.get("limit") or ["100"])[0],
                offset=(qs.get("offset") or ["0"])[0],
                query=(qs.get("q") or [""])[0]))
        elif path == "/api/library/device-playlist":
            qs = parse_qs(urlparse(self.path).query)
            pl_id = (qs.get("id") or [""])[0]
            self._json(library_device_playlist(pl_id))
        elif path == "/api/queue":
            self._json(queue_snapshot(20))
        elif path == "/api/history/top":
            qs = parse_qs(urlparse(self.path).query)
            self._json({"items": history_top(
                int((qs.get("limit") or ["100"])[0] or 100))})
        elif path in ("/api/history", "/api/smart/recent"):
            qs = parse_qs(urlparse(self.path).query)
            self._json({"items": history_recent(
                int((qs.get("limit") or ["100"])[0] or 100))})
        elif path == "/api/smart/frequent":
            qs = parse_qs(urlparse(self.path).query)
            self._json({"items": history_top(
                int((qs.get("limit") or ["100"])[0] or 100))})
        elif path == "/api/playlists/export":
            with PLAYLIST_LOCK:
                export = []
                for pl in PLAYLISTS["playlists"].values():
                    export.append({"name": pl.get("name", ""),
                                   "items": list(pl.get("items", []))})
            self._json({"version": 1, "playlists": export})
        elif path == "/api/playlists":
            self._json(playlist_list())
        elif path.startswith("/api/playlists/"):
            pl_id = path.rstrip("/").split("/")[-1]
            self._json(playlist_get(pl_id))
        elif path == "/api/library/artwork":
            qs = parse_qs(urlparse(self.path).query)
            pid = (qs.get("pid") or [None])[0]
            album_pid = (qs.get("album_pid") or [None])[0]
            size = (qs.get("size") or ["thumb"])[0]
            entry = None
            if pid:
                entry = MEDIA_ARTWORK["items"].get(pid)
            elif album_pid:
                entry = MEDIA_ARTWORK["albums"].get(album_pid)
            if not entry:
                self._json({"error": "no artwork"}, 404)
                return
            rel = entry.get("thumb") if size != "full" else None
            if not rel:
                rel = entry.get("file")
            full = os.path.abspath(os.path.join(BASE_DIR, rel or ""))
            root = os.path.abspath(_artwork_dir())
            if not (full == root or full.startswith(root + os.sep)):
                self._json({"error": "invalid artwork path"}, 404)
                return
            try:
                with open(full, "rb") as fh:
                    blob = fh.read()
            except OSError:
                self._json({"error": "artwork not found"}, 404)
                return
            mime = entry.get("mime") or "image/jpeg"
            if mime == "image/jpeg" and not blob.endswith(b"\xff\xd9"):
                blob += b"\xff\xd9"
            self._send(200, blob, mime)
        elif path == "/api/artwork":
            with STATE_LOCK:
                blob = STATE["artwork_bytes"]
                mime = STATE["artwork_mime"]
            if not blob:
                self._json({"error": "no artwork"}, 404)
            else:
                # Some iPod artwork JPEGs lack the EOI marker; browsers are
                # happier with it appended (and it is harmless otherwise).
                if mime == "image/jpeg" and not blob.endswith(b"\xff\xd9"):
                    blob = blob + b"\xff\xd9"
                self._send(200, blob, mime)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            payload = json.loads(raw or b"{}") if raw else {}
            if path == "/api/command":
                command = payload.get("command", "")
                if not command:
                    self._json({"ok": False, "error": "missing command"}, 400)
                    return
                SESSION.send_command(command)
                self._json({"ok": True, "command": command})
            elif path == "/api/seek":
                elapsed = payload.get("elapsed_ms")
                queue_index = payload.get("queue_index")
                if elapsed is None and queue_index is None:
                    self._json({"ok": False,
                                "error": "elapsed_ms or queue_index required"},
                               400)
                    return
                try:
                    elapsed = int(elapsed) if elapsed is not None else None
                    queue_index = (int(queue_index)
                                   if queue_index is not None else None)
                except (TypeError, ValueError):
                    self._json({"ok": False, "error": "invalid position"}, 400)
                    return
                SESSION.seek(elapsed_ms=elapsed, queue_index=queue_index)
                self._json({"ok": True, "elapsed_ms": elapsed,
                            "queue_index": queue_index})
            elif path == "/api/library/sync":
                SESSION.resync_library()
                self._json({"ok": True, "message": "resync requested"})
            elif path == "/api/library/sync-incremental":
                SESSION.sync_library_incremental()
                self._json({"ok": True, "message": "incremental sync requested"})
            elif path == "/api/library/clear-iap2":
                if MEDIA_JOBS.busy():
                    self._json({"ok": False,
                                "error": "Cancel the running task first"},
                               409)
                    return
                self._json({"ok": True, "stats": clear_iap2_library()})
            elif path == "/api/library/clear-artwork":
                if MEDIA_JOBS.busy():
                    self._json({"ok": False,
                                "error": "Cancel the running task first"},
                               409)
                    return
                self._json({"ok": True, "stats": clear_iap2_artwork()})
            elif path == "/api/media/action":
                ok, error = MEDIA_JOBS.start(
                    str(payload.get("action") or ""))
                if not ok:
                    self._json({"ok": False, "error": error}, 409)
                    return
                media = MEDIA_JOBS.snapshot()
                media["cache"] = media_cache_state()
                self._json({"ok": True, "media": media,
                            "settings": settings_snapshot()})
            elif path == "/api/media/cancel":
                ok, error = MEDIA_JOBS.cancel()
                if not ok:
                    self._json({"ok": False, "error": error}, 409)
                    return
                self._json({"ok": True, "media": MEDIA_JOBS.snapshot()})
            elif path == "/api/media/mode":
                if payload.get("mode") != "iap2":
                    self._json({"ok": False,
                                "error": "mode must be iap2"}, 400)
                    return
                ok, error = MEDIA_JOBS.request_iap2()
                if not ok:
                    self._json({"ok": False, "error": error}, 409)
                    return
                self._json({"ok": True, "media": MEDIA_JOBS.snapshot()})
            elif path == "/api/media/load":
                if MEDIA_JOBS.busy():
                    self._json({"ok": False,
                                "error": "Cancel the running task first"},
                               409)
                    return
                target = str(payload.get("target") or "all")
                if target not in ("library", "artwork", "all"):
                    self._json({"ok": False, "error": "invalid target"}, 400)
                    return
                loaded = load_media_cache(target)
                self._json({"ok": True, "loaded": loaded,
                            "cache": media_cache_state()})
            elif path == "/api/settings":
                if not isinstance(payload.get("auto_switch_iap2"), bool):
                    self._json({"ok": False,
                                "error": "auto_switch_iap2 must be boolean"},
                               400)
                    return
                with SETTINGS_LOCK:
                    SETTINGS["auto_switch_iap2"] = payload["auto_switch_iap2"]
                save_settings()
                self._json({"ok": True, "settings": settings_snapshot()})
            elif path == "/api/queue/add":
                album_pid = payload.get("album_pid")
                item_ids = payload.get("item_ids")
                if album_pid:
                    ids = item_ids_for_album(str(album_pid))
                elif isinstance(item_ids, list) and item_ids:
                    ids = [str(x) for x in item_ids]
                else:
                    self._json({"ok": False,
                                "error": "missing album_pid or item_ids"}, 400)
                    return
                if not ids:
                    self._json({"ok": False,
                                "error": "no matching tracks"}, 400)
                    return
                added, missing, total = queue_add(ids)
                self._json({"ok": True, "added": added,
                            "missing": missing, "total": total})
            elif path == "/api/queue/play":
                ids = queue_builder_ids()
                if not ids:
                    self._json({"ok": False, "error": "Queue is empty"}, 400)
                    return
                try:
                    start = int(payload.get("start_index") or 0)
                except (TypeError, ValueError):
                    start = 0
                start = max(0, min(start, len(ids) - 1))
                SESSION.play_item_list(ids, start, source="queue")
                queue_clear()
                self._json({"ok": True, "count": len(ids),
                            "start_index": start})
            elif path == "/api/queue/clear":
                queue_clear()
                self._json({"ok": True})
            elif path == "/api/play":
                item_id = payload.get("item_id")
                album_pid = payload.get("album_pid")
                artist_pid = payload.get("artist_pid")
                current_selection = payload.get("current_selection")
                genre = payload.get("genre")
                album_artist = payload.get("album_artist")
                device_playlist_id = payload.get("device_playlist_id")
                item_ids = payload.get("item_ids")
                if item_id:
                    SESSION.play_item(str(item_id))
                    self._json({"ok": True, "item_id": item_id})
                elif album_pid:
                    SESSION.play_album(str(album_pid))
                    self._json({"ok": True, "album_pid": album_pid})
                elif artist_pid:
                    SESSION.play_artist(str(artist_pid))
                    self._json({"ok": True, "artist_pid": artist_pid})
                elif current_selection:
                    SESSION.play_current_selection()
                    self._json({"ok": True, "current_selection": True})
                elif genre:
                    SESSION.play_genre(str(genre))
                    self._json({"ok": True, "genre": genre})
                elif album_artist:
                    SESSION.play_album_artist(str(album_artist))
                    self._json({"ok": True, "album_artist": album_artist})
                elif device_playlist_id:
                    SESSION.play_device_playlist(str(device_playlist_id))
                    self._json({"ok": True,
                                "device_playlist_id": device_playlist_id})
                elif item_ids:
                    ids = [str(x) for x in item_ids]
                    SESSION.play_item_list(
                        ids, int(payload.get("start_index") or 0))
                    self._json({"ok": True, "count": len(ids)})
                else:
                    self._json(
                        {"ok": False,
                         "error": "missing item_id / album_pid / "
                                  "artist_pid / item_ids"}, 400)
            elif path == "/api/experiment/csm":
                if os.environ.get("ENABLE_EXPERIMENT") != "1":
                    self._json({"ok": False,
                                "error": "experiment disabled"}, 403)
                else:
                    def _int(v):
                        return int(v, 0) if isinstance(v, str) else int(v)
                    msg_id = _int(payload.get("msg_id", 0))
                    params = []
                    for item in payload.get("params", []):
                        params.append((_int(item.get("id", 0)),
                                       bytes.fromhex(item.get("hex", ""))))
                    SESSION.send_raw_csm(msg_id, params)
                    self._json({"ok": True, "msg_id": msg_id,
                                "params": len(params)})
            elif path == "/api/playlists/import":
                self._json(playlists_import(payload.get("playlists", [])))
            elif path == "/api/playlists":
                meta = playlist_create(payload.get("name", ""))
                self._json({"ok": True, "playlist": meta})
            elif path.startswith("/api/playlists/"):
                parts = path.strip("/").split("/")
                if len(parts) < 3:
                    raise ValueError("bad playlist path")
                pl_id = parts[2]
                action = parts[3] if len(parts) > 3 else "rename"
                if action == "rename":
                    meta = playlist_rename(pl_id, payload.get("name", ""))
                    self._json({"ok": True, "playlist": meta})
                elif action == "add":
                    item_ids = payload.get("item_ids")
                    if item_ids:
                        self._json(playlist_add_items(pl_id, item_ids))
                    else:
                        self._json(playlist_add_item(
                            pl_id, str(payload.get("item_id", ""))))
                elif action == "remove":
                    self._json(playlist_remove_item(
                        pl_id, str(payload.get("item_id", ""))))
                elif action == "move":
                    self._json(playlist_move(
                        pl_id, int(payload.get("from", 0)),
                        int(payload.get("to", 0))))
                elif action == "clear":
                    self._json(playlist_clear(pl_id))
                elif action == "play":
                    self._json(playlist_play(
                        pl_id, int(payload.get("start_index") or 0)))
                else:
                    self._json({"ok": False, "error": "not found"}, 404)
            else:
                self._json({"ok": False, "error": "not found"}, 404)
        except KeyError as exc:
            self._json({"ok": False, "error": str(exc)}, 404)
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
        except RuntimeError as exc:
            self._json({"ok": False, "error": str(exc)}, 409)
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "error": str(exc)}, 500)

    def do_DELETE(self):
        path = urlparse(self.path).path
        try:
            parts = path.strip("/").split("/")
            if (len(parts) == 3 and parts[0] == "api"
                    and parts[1] == "playlists"):
                meta = playlist_delete(parts[2])
                self._json({"ok": True, "playlist": meta})
            else:
                self._json({"ok": False, "error": "not found"}, 404)
        except KeyError as exc:
            self._json({"ok": False, "error": str(exc)}, 404)
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
        except RuntimeError as exc:
            self._json({"ok": False, "error": str(exc)}, 409)
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "error": str(exc)}, 500)


def main():
    print(f"MediaCtrl web UI on http://0.0.0.0:{PORT}/")
    device_store.load_registry()
    load_settings()
    load_library()
    load_playlists()
    cache_path = _media_cache_path()
    if (os.path.exists(cache_path)
            or (not _library_id()
                and (os.path.exists(MEDIA_DATES_PATH)
                     or os.path.exists(MEDIA_ARTWORK_PATH)))):
        try:
            media_cache.ensure_cache(
                cache_path,
                legacy_dates_path=(MEDIA_DATES_PATH
                                   if not _library_id() else None),
                legacy_artwork_path=(MEDIA_ARTWORK_PATH
                                     if not _library_id() else None),
                base_dir=BASE_DIR)
        except Exception as exc:  # noqa: BLE001
            print(f"media cache initialization failed: {exc}")
    if os.path.exists(cache_path):
        try:
            load_media_cache("artwork")
        except Exception as exc:  # noqa: BLE001
            print(f"artwork cache load failed: {exc}")
    SESSION.start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
