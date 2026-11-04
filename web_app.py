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
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import csm
import hid_async
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
    send_csm,
    send_hid_command,
    send_hid_start,
    set_configuration,
)
from mfi_local import LocalMfiProvider

PORT = int(os.environ.get("PORT", "8080"))
LIBRARY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "media-library.json")
PLAYLISTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "playlists.json")
HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "history.json")
ARTWORK_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "artwork-cache")
MEDIA_DATES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "media-db-dates.json")
MEDIA_ARTWORK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "media-db-artwork.json")
MEDIA_ARTWORK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "media-artwork")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

STATE_LOCK = threading.Lock()
FT_PLAYLIST_FILES = {}
FT_QUEUE_FILES = {}
MEDIA_DATES = {"mtime": 0.0, "dates": {}}
MEDIA_ARTWORK = {"mtime": 0.0, "items": {}, "albums": {}}
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
        "artwork_id": None,
        "updated_at": 0.0,
    },
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
    "revision": None,
    "items": {},
    "playlists": {},
    "first_seen": {},
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

# ---------------------------------------------------------------------------
# Accessory-side playlists.  iAP2's media library is read-only, so these are
# assembled by the accessory and played with 0x4C07 (PlayMediaLibraryItems).
# ---------------------------------------------------------------------------
PLAYLIST_LOCK = threading.Lock()
PLAYLISTS = {"playlists": {}}
MAX_PLAYLIST_ITEMS = 2000

# Accessory-side queue/history used for "Up Next" and smart playlists.
QUEUE_LOCK = threading.Lock()
QUEUE = {"items": [], "source": "", "updated": 0.0,
         "base": 0, "meta": {}}
HISTORY = []
HISTORY_MAX = 500


def load_playlists():
    try:
        with open(PLAYLISTS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return
    except Exception as exc:  # noqa: BLE001
        print(f"playlists load failed: {exc}")
        return
    playlists = data.get("playlists", {})
    if not isinstance(playlists, dict):
        return
    with PLAYLIST_LOCK:
        PLAYLISTS["playlists"] = playlists
    print(f"playlists loaded: {len(playlists)}")


def save_playlists():
    with PLAYLIST_LOCK:
        tmp = PLAYLISTS_PATH + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(PLAYLISTS, fh, ensure_ascii=False)
            os.replace(tmp, PLAYLISTS_PATH)
        except Exception as exc:  # noqa: BLE001
            print(f"playlists save failed: {exc}")


def _playlist_meta(pl):
    return {
        "id": pl["id"],
        "name": pl["name"],
        "tracks": len(pl.get("items", [])),
        "created": pl.get("created", 0),
        "updated": pl.get("updated", 0),
    }


def playlist_list():
    with PLAYLIST_LOCK:
        rows = [_playlist_meta(p) for p in PLAYLISTS["playlists"].values()]
    rows.sort(key=lambda p: ((p["name"] or "").lower(), p["id"]))
    return {"playlists": rows, "total": len(rows)}


def playlist_get(pl_id):
    with PLAYLIST_LOCK:
        pl = PLAYLISTS["playlists"].get(pl_id)
        if pl is None:
            return {"ok": False, "error": "playlist not found"}
        meta = _playlist_meta(pl)
        ids = list(pl.get("items", []))
    wanted = set(ids)
    with LIBRARY_LOCK:
        by_id = {it.get("id"): it for it in LIBRARY["items"].values()
                 if it.get("id") in wanted}
    items = []
    missing = 0
    for item_id in ids:
        it = by_id.get(item_id)
        if it is None:
            missing += 1
            continue
        items.append(song_entry(it))
    meta["missing"] = missing
    return {"ok": True, "playlist": meta, "items": items}


def playlist_create(name):
    name = (name or "").strip()
    if not name:
        raise ValueError("Playlist name cannot be empty")
    if len(name) > 120:
        raise ValueError("Playlist name too long")
    now = time.time()
    pl = {"id": uuid.uuid4().hex, "name": name, "items": [],
          "created": now, "updated": now}
    with PLAYLIST_LOCK:
        PLAYLISTS["playlists"][pl["id"]] = pl
    save_playlists()
    return _playlist_meta(pl)


def playlist_rename(pl_id, name):
    name = (name or "").strip()
    if not name:
        raise ValueError("Playlist name cannot be empty")
    with PLAYLIST_LOCK:
        pl = PLAYLISTS["playlists"].get(pl_id)
        if pl is None:
            raise KeyError("playlist not found")
        pl["name"] = name[:120]
        pl["updated"] = time.time()
        meta = _playlist_meta(pl)
    save_playlists()
    return meta


def playlist_delete(pl_id):
    with PLAYLIST_LOCK:
        pl = PLAYLISTS["playlists"].pop(pl_id, None)
    if pl is None:
        raise KeyError("playlist not found")
    save_playlists()
    return _playlist_meta(pl)


def playlist_add_item(pl_id, item_id):
    with LIBRARY_LOCK:
        item = LIBRARY["items"].get(item_id)
    if item is None:
        raise KeyError("Track not found")
    with PLAYLIST_LOCK:
        pl = PLAYLISTS["playlists"].get(pl_id)
        if pl is None:
            raise KeyError("playlist not found")
        if item_id in pl["items"]:
            return {"ok": True, "already": True, "playlist": _playlist_meta(pl)}
        if len(pl["items"]) >= MAX_PLAYLIST_ITEMS:
            raise ValueError(f"Playlist supports at most {MAX_PLAYLIST_ITEMS} tracks")
        pl["items"].append(item_id)
        pl["updated"] = time.time()
        meta = _playlist_meta(pl)
    save_playlists()
    return {"ok": True, "already": False, "playlist": meta,
            "item": song_entry(item)}


def playlist_add_items(pl_id, item_ids):
    """Add several items at once (album -> playlist)."""
    ids = [str(x) for x in (item_ids or []) if x]
    with LIBRARY_LOCK:
        valid = [x for x in ids if LIBRARY["items"].get(x)]
    if not ids:
        raise ValueError("No tracks to add")
    missing = len(ids) - len(valid)
    with PLAYLIST_LOCK:
        pl = PLAYLISTS["playlists"].get(pl_id)
        if pl is None:
            raise KeyError("playlist not found")
        seen = set(pl["items"])
        new_ids = []
        already = 0
        room = MAX_PLAYLIST_ITEMS - len(pl["items"])
        for item_id in valid:
            if item_id in seen:
                already += 1
                continue
            if len(new_ids) >= room:
                raise ValueError(f"Playlist supports at most {MAX_PLAYLIST_ITEMS} tracks")
            new_ids.append(item_id)
            seen.add(item_id)
        pl["items"].extend(new_ids)
        pl["updated"] = time.time()
        meta = _playlist_meta(pl)
    save_playlists()
    return {"ok": True, "added": len(new_ids), "already": already,
            "missing": missing, "playlist": meta}


def playlist_remove_item(pl_id, item_id):
    with PLAYLIST_LOCK:
        pl = PLAYLISTS["playlists"].get(pl_id)
        if pl is None:
            raise KeyError("playlist not found")
        before = len(pl["items"])
        pl["items"] = [x for x in pl["items"] if x != item_id]
        removed = before - len(pl["items"])
        pl["updated"] = time.time()
        meta = _playlist_meta(pl)
    save_playlists()
    return {"ok": True, "removed": removed, "playlist": meta}


def playlist_move(pl_id, from_index, to_index):
    with PLAYLIST_LOCK:
        pl = PLAYLISTS["playlists"].get(pl_id)
        if pl is None:
            raise KeyError("playlist not found")
        items = pl["items"]
        if not (0 <= from_index < len(items) and 0 <= to_index < len(items)):
            raise ValueError("Index out of range")
        item = items.pop(from_index)
        items.insert(to_index, item)
        pl["updated"] = time.time()
        meta = _playlist_meta(pl)
    save_playlists()
    return {"ok": True, "playlist": meta}


def playlist_clear(pl_id):
    with PLAYLIST_LOCK:
        pl = PLAYLISTS["playlists"].get(pl_id)
        if pl is None:
            raise KeyError("playlist not found")
        pl["items"] = []
        pl["updated"] = time.time()
        meta = _playlist_meta(pl)
    save_playlists()
    return {"ok": True, "playlist": meta}


def playlists_import(items):
    created = 0
    skipped = 0
    for item in (items or []):
        name = (item.get("name") or "").strip()
        if not name:
            skipped += 1
            continue
        try:
            pl = playlist_create(name)
            ids = [str(x) for x in item.get("items", []) if x]
            if ids:
                playlist_add_items(pl["id"], ids)
            created += 1
        except Exception:  # noqa: BLE001
            skipped += 1
    return {"ok": True, "created": created, "skipped": skipped}


def playlist_play(pl_id, start_index=0):
    data = playlist_get(pl_id)
    if not data.get("ok"):
        raise KeyError("playlist not found")
    ids = [item["id"] for item in data["items"]]
    if not ids:
        raise ValueError("Playlist is empty")
    SESSION.play_item_list(ids, start_index)
    return {"ok": True, "played": len(ids),
            "skipped": data["playlist"].get("missing", 0)}


def load_history():
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return
    except Exception as exc:  # noqa: BLE001
        print(f"history load failed: {exc}")
        return
    entries = data.get("entries", []) if isinstance(data, dict) else data
    if isinstance(entries, list):
        with QUEUE_LOCK:
            HISTORY[:] = entries[-HISTORY_MAX:]
    print(f"history loaded: {len(HISTORY)} entries")


def save_history():
    with QUEUE_LOCK:
        payload = {"entries": list(HISTORY[-HISTORY_MAX:])}
    tmp = HISTORY_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, HISTORY_PATH)
    except Exception as exc:  # noqa: BLE001
        print(f"history save failed: {exc}")


def set_queue(item_ids, source="", base=0, meta=None):
    with QUEUE_LOCK:
        QUEUE["items"] = [x for x in item_ids if x]
        QUEUE["source"] = source
        QUEUE["updated"] = time.time()
        QUEUE["base"] = max(0, int(base or 0))
        QUEUE["meta"] = dict(meta or {})


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
        ids = list(QUEUE["items"])
        source = QUEUE["source"]
        base = QUEUE.get("base", 0)
        meta = dict(QUEUE.get("meta") or {})
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
    if not item_id:
        return
    with LIBRARY_LOCK:
        it = LIBRARY["items"].get(item_id)
    if it is None:
        return
    entry = {
        "id": item_id,
        "title": (it.get("title") or "").strip(),
        "artist": (it.get("artist") or "").strip(),
        "album": (it.get("album") or "").strip(),
        "ts": time.time(),
    }
    with QUEUE_LOCK:
        if HISTORY:
            last = HISTORY[-1]
            if last.get("id") == item_id and time.time() - last.get("ts", 0) < 30:
                return
        HISTORY.append(entry)
        del HISTORY[:-HISTORY_MAX]
    save_history()


def record_now_playing(np):
    """Match a Now Playing update to our queue and record it in history."""
    title = (np.get("title") or "").strip()
    artist = (np.get("artist") or "").strip()
    if not title:
        return
    with QUEUE_LOCK:
        ids = list(QUEUE["items"])
    if not ids:
        return
    for it in _resolve_items(ids):
        if ((it.get("title") or "").strip() == title
                and (it.get("artist") or "").strip() == artist):
            history_add(it["id"], np)
            return


def history_recent(limit=100):
    with QUEUE_LOCK:
        entries = list(reversed(HISTORY))
    seen = set()
    ids = []
    for e in entries:
        item_id = e.get("id")
        if item_id and item_id not in seen:
            seen.add(item_id)
            ids.append(item_id)
        if len(ids) >= limit:
            break
    return _resolve_items(ids)


def history_top(limit=100):
    with QUEUE_LOCK:
        entries = list(HISTORY)
    counts = {}
    for e in entries:
        item_id = e.get("id")
        if item_id:
            counts[item_id] = counts.get(item_id, 0) + 1
    ids = [item_id for item_id, _ in
           sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))][:limit]
    items = _resolve_items(ids)
    for item in items:
        item["play_count"] = counts.get(item["id"], 0)
    return items


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
    return os.path.join(ARTWORK_CACHE_DIR, f"{artwork_id}.jpg")


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
        os.makedirs(ARTWORK_CACHE_DIR, exist_ok=True)
        path = _artwork_cache_path(artwork_id)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, path)
        # Keep the cache bounded (oldest by mtime).
        files = [os.path.join(ARTWORK_CACHE_DIR, name)
                 for name in os.listdir(ARTWORK_CACHE_DIR)
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


def load_library():
    try:
        with open(LIBRARY_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return
    except Exception as exc:  # noqa: BLE001
        print(f"media library load failed: {exc}")
        return
    with LIBRARY_LOCK:
        LIBRARY["revision"] = data.get("revision")
        items = data.get("items", {})
        first_seen = data.get("first_seen", {})
        # Legacy items have no known add time: keep 0.0 so a later
        # "recently added" view doesn't pretend they are new.
        for item_id, it in items.items():
            ts = first_seen.get(item_id)
            if ts is None:
                ts = it.get("first_seen") or 0.0
                first_seen[item_id] = ts
            it["first_seen"] = ts
        LIBRARY["items"] = items
        LIBRARY["first_seen"] = first_seen
        LIBRARY["playlists"] = data.get("playlists", {})
        LIBRARY["version"] += 1
    print(f"media library loaded: {len(data.get('items', {}))} items, "
          f"revision {data.get('revision')}")


def load_media_dates():
    """Load the separate MediaLibrary.sqlitedb importer result.

    The iAP2 service never talks to libimobiledevice/ifuse; it only reads the
    normalized result file produced by media_db_import.py.
    """
    try:
        stat = os.stat(MEDIA_DATES_PATH)
    except OSError:
        return False
    with STATE_LOCK:
        if stat.st_mtime <= MEDIA_DATES["mtime"]:
            return False
    try:
        with open(MEDIA_DATES_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        dates = data.get("dates", {})
        if not isinstance(dates, dict):
            return False
    except Exception as exc:  # noqa: BLE001
        print(f"media date result load failed: {exc}")
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
    print(f"media date result loaded: {count} matched items")
    return True


def load_media_artwork():
    """Load the artwork importer result; no AFC code lives in this process."""
    try:
        stat = os.stat(MEDIA_ARTWORK_PATH)
    except OSError:
        return False
    if stat.st_mtime <= MEDIA_ARTWORK["mtime"]:
        return False
    try:
        with open(MEDIA_ARTWORK_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        items = data.get("items", {})
        albums = data.get("albums", {})
        if not isinstance(items, dict) or not isinstance(albums, dict):
            return False
    except Exception as exc:  # noqa: BLE001
        print(f"media artwork result load failed: {exc}")
        return False
    MEDIA_ARTWORK["mtime"] = stat.st_mtime
    MEDIA_ARTWORK["items"] = items
    MEDIA_ARTWORK["albums"] = albums
    print(f"media artwork result loaded: {len(items)} items, "
          f"{len(albums)} albums")
    return True


def save_library():
    with LIBRARY_LOCK:
        if not LIBRARY["dirty"]:
            return
        LIBRARY["dirty"] = False
        payload = {
            "revision": LIBRARY["revision"],
            "items": LIBRARY["items"],
            "playlists": LIBRARY["playlists"],
            "first_seen": LIBRARY["first_seen"],
        }
    tmp = LIBRARY_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, LIBRARY_PATH)
        print(f"media library saved: {len(payload['items'])} items")
    except Exception as exc:  # noqa: BLE001
        print(f"media library save failed: {exc}")


def library_summary():
    load_media_dates()
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
            "titles": {}, "artists": {}, "tracks": 0, "date_added": 0.0})
        g["tracks"] += 1
        g["date_added"] = max(g["date_added"], it.get("date_added") or 0.0)
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
                       "date_added": g["date_added"]}
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
            continue
        key = ("name", artist, album)
        entry = albums.get(key)
        if entry is None:
            entry = {"pid": None, "artist": artist, "album": album,
                     "tracks": 0, "date_added": 0.0}
            albums[key] = entry
        entry["tracks"] += 1
        entry["date_added"] = max(entry.get("date_added") or 0.0,
                                  it.get("date_added") or 0.0)
    summary = {
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
            "duration_ms": it.get("duration_ms") or 0,
            "track": it.get("track") or 0,
            "disc": it.get("disc") or 1,
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
    load_media_dates()
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


def library_recent(limit=100, offset=0):
    """Best-effort 'recently added' — iAP2 has no date-added field.

    Items first observed after this feature was enabled carry a real
    first_seen timestamp; older items carry 0.  The response says whether
    it is only an estimate / empty.
    """
    load_media_dates()
    with LIBRARY_LOCK:
        rows = [it for it in LIBRARY["items"].values()
                if (it.get("date_added") or 0) > 0
                or (it.get("first_seen") or 0) > 0]
    rows.sort(key=lambda it: (-(it.get("date_added") or 0),
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
        "note": ("Date added from MediaLibrary.sqlitedb (via separate AFC "
                 "importer)." if MEDIA_DATES["dates"] else
                 ("Device does not provide date-added; sorted by MediaCtrl "
                  "first-seen time. Legacy items are hidden.")),
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

    # -- lifecycle ---------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

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
            save_library()

    async def _periodic_save(self):
        while True:
            await asyncio.sleep(10)
            with LIBRARY_LOCK:
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
                    elif cid == 26:
                        np["artwork_id"] = child[0] if child else None
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
                    elif cid == 14 and child:
                        np["queue_available"] = bool(child[0])
                    elif cid == 15 and child:
                        np["queue_transfer_id"] = child[0]
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
                await send_csm(conn, 0x1D01, identification_params())
            elif msg_id == 0x1D02:
                with STATE_LOCK:
                    STATE["connected"] = True
                    STATE["last_error"] = None
                await send_csm(conn, 0x5000, now_playing_params())
                await send_csm(conn, 0x4C00, [])
                await send_hid_start(conn)
                self._event("Connected")
            elif msg_id == 0x1D03:
                with STATE_LOCK:
                    STATE["last_error"] = "identification rejected"
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
            elif msg_id == 0x4C01:
                libs = parse_library_info(body)
                with STATE_LOCK:
                    STATE["libraries"] = libs
                local = self._local_library(libs)
                if local:
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
            LIBRARY["last_update"] = time.time()
        params = [
            csm.string(0, uuid),
            # The device starts a full sync only when a revision is present;
            # "0" means "nothing known yet".
            csm.string(1, revision if revision else "0"),
            csm.group(2, [csm.void(i) for i in range(0, 27)]),
            csm.group(3, [csm.void(i) for i in range(0, 6)]),
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
        with LIBRARY_LOCK:
            changed = False
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
                    ts = LIBRARY["first_seen"].get(key)
                    if ts is None:
                        ts = time.time()
                        LIBRARY["first_seen"][key] = ts
                    item["first_seen"] = ts
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
        return rev, last7, items, playlists


SESSION = Session()


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
<style>
  :root {
    --bg: #22262a;
    --card: #2a2f34;
    --card2: #2a2f34;
    --text: #e6e8ea;
    --muted: #aab0b7;
    --accent: #c8cdd3;
    --ok: #b9c8bf;
    --warn: #d1c5ad;
    --err: #d7b0b0;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    min-height: 100vh;
    display: flex;
    align-items: flex-start;
    justify-content: center;
    background:
      radial-gradient(900px 500px at 15% -10%, #1b2a5e55, transparent 60%),
      radial-gradient(800px 500px at 110% 110%, #3a1b5e44, transparent 55%),
      var(--bg);
    color: var(--text);
    font-family: -apple-system, "Segoe UI", "Noto Sans SC", Roboto, sans-serif;
  }
  .wrap { width: min(1360px, 98vw); padding: 14px 0; margin: 0 auto; }
  .topbar {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 10px; padding: 0 4px; color: var(--muted); font-size: 13px;
  }
  .dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%;
         background: var(--err); margin-right: 7px; vertical-align: 1px; }
  .dot.ok { background: var(--ok); box-shadow: 0 0 10px #b9c8bf88; }
  .dot.warn { background: var(--warn); box-shadow: 0 0 10px #d1c5ad88; }
  .brand { letter-spacing: .08em; text-transform: uppercase; font-size: 12px; }
  .layout {
    display: grid; grid-template-columns: 360px minmax(0, 1fr);
    gap: 12px; align-items: start;
  }
  .left { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
  .card {
    display: flex; flex-direction: column; gap: 8px; padding: 14px;
    background: linear-gradient(160deg, var(--card), var(--card2));
    border: 1px solid #ffffff10;
    border-radius: 16px;
    box-shadow: 0 18px 45px #00000055, inset 0 1px 0 #ffffff0d;
  }
  .now-head { display: flex; gap: 12px; align-items: flex-start; min-width: 0; }
  .art-box {
    flex: 0 0 120px; width: 120px; height: 120px; border-radius: 10px;
    overflow: hidden; position: relative;
    background: linear-gradient(135deg, #232a3d, #171b28);
    box-shadow: 0 8px 20px #00000077;
  }
  .art-box img { width: 100%; height: 100%; object-fit: cover; display: none; }
  .art-ph {
    position: absolute; inset: 0; display: flex; align-items: center;
    justify-content: center; font-size: 44px; color: #ffffff22;
  }
  .meta { flex: 1; min-width: 0; display: flex; flex-direction: column; }
  .now-card { display: flex; flex-direction: column; }
  .now-card .badge { font-size: 10px; padding: 3px 8px; margin-bottom: 6px; }
  .now-card h1 { font-size: 20px; margin: 0 0 3px; }
  .now-card .artist { font-size: 13px; margin: 0 0 2px; }
  .now-card .album { font-size: 11px; }
  .now-card .queue, .now-card .np-details { font-size: 10px; margin-top: 2px; }
  .now-card .progress { padding-top: 8px; }
  .now-card .bar { height: 4px; }
  .now-card .times { font-size: 10px; margin-top: 4px; }
  .now-card .controls { gap: 6px; margin-top: 8px; }
  .now-card .controls button,
  .now-card .controls button.main {
    flex: 1 1 0; width: auto; min-width: 0; height: 36px;
    padding: 0 6px; font-size: 11px;
  }
  .now-card .extra-controls { gap: 5px; margin-top: 8px; }
  .now-card .extra-controls button {
    padding: 5px 8px; font-size: 10px; border-radius: 7px;
  }
  .volume-row { display: flex; gap: 5px; width: 100%; }
  .volume-row button { flex: 1 1 0; min-width: 0; }
  .now-card .hint { margin-top: 6px; font-size: 10px; }
  .queue-panel .upnext { max-height: 180px; }
  .session-panel .kv, .library-info-panel .kv {
    padding: 3px 0; font-size: 12px;
  }
  .left .panel { padding: 12px 14px; }
  .library-info-panel .lib {
    display: flex; justify-content: space-between; gap: 10px;
    padding: 3px 0; border-bottom: 1px solid var(--line-soft);
    font-size: 12px; line-height: 1.55;
  }
  .library-info-panel .lib:last-child { border-bottom: 0; }
  .library-info-panel .lib-name { color: var(--text); }
  .library-info-panel .lib-kind { color: #d2d6da; }
  .small-btn {
    width: auto; height: auto; min-width: 0; padding: 3px 7px;
    font-size: 9px; margin: 0; line-height: 1.2;
  }
  .library-panel {
    min-width: 0; padding: 16px 18px;
    display: flex; flex-direction: column;
    height: auto; min-height: 0; overflow-y: auto;
    scrollbar-gutter: stable;
  }
  .library-panel .media-wrap {
    flex: 1; min-height: 0; max-height: none;
    scrollbar-gutter: stable;
  }
  .library-panel .lib-tabs, .library-panel .lib-tools { flex: 0 0 auto; }
  .library-panel .tab-pane.active {
    display: flex; flex-direction: column; flex: 1; min-height: 0;
  }
  .library-panel .album-list {
    flex: 1; min-height: 0; display: flex; flex-direction: column;
  }
  .library-panel .album-grid { flex: 1; min-height: 0; }
  .library-panel .more-row { flex: 0 0 auto; }
  .library-panel .track-list .media-wrap {
    flex: none; max-height: 320px;
  }
  .panel-head {
    display: flex; align-items: center; justify-content: space-between;
    gap: 8px; flex: 0 0 auto; min-height: 24px;
    margin: 0 0 8px; padding: 0 0 7px;
    border-bottom: 1px solid var(--line);
  }
  .panel-head h3 {
    margin: 0; padding: 0; border: 0;
    font-size: 11px; line-height: 16px;
    letter-spacing: .14em; text-transform: uppercase;
    color: #c2c7cd;
  }
  .panel-note {
    font-size: 10px; color: var(--muted); letter-spacing: .05em;
  }
  html { height: 100%; }
  body {
    display: block; height: 100vh; overflow: hidden;
  }
  .wrap {
    height: 100vh; display: flex; flex-direction: column;
    padding: 10px 0;
  }
  .topbar { flex: 0 0 auto; margin-bottom: 8px; }
  .layout { flex: 1; min-height: 0; align-items: stretch; }
  .left {
    min-height: 0; overflow-y: auto; scrollbar-gutter: stable;
    padding-right: 2px;
  }
  .badge {
    align-self: flex-start; font-size: 12px; letter-spacing: .06em;
    padding: 4px 11px; border-radius: 999px; margin-bottom: 10px;
    background: #ffffff10; color: var(--muted);
  }
  .badge.playing { background: #b9c8bf22; color: var(--ok); }
  .badge.paused  { background: #d1c5ad22; color: var(--warn); }
  h1 { margin: 0 0 5px; font-size: 26px; line-height: 1.2; font-weight: 650;
       overflow-wrap: anywhere; }
  .artist { margin: 0 0 3px; font-size: 16px; color: #d2d6da; }
  .album { margin: 0; font-size: 13px; color: var(--muted); overflow-wrap: anywhere; }
  .progress { margin-top: auto; padding-top: 18px; }
  .bar { height: 6px; border-radius: 99px; background: #ffffff12; overflow: hidden; }
  .bar span { display: block; height: 100%; width: 0%;
              background: linear-gradient(90deg, var(--accent), #d6dade);
              border-radius: 99px; transition: width .25s linear; }
  .times { display: flex; justify-content: space-between; margin-top: 7px;
           font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .controls { display: flex; align-items: center; gap: 10px; margin-top: 18px; }
  button {
    border: 0; cursor: pointer; color: var(--text);
    background: #ffffff0d; border: 1px solid #ffffff12;
    width: 46px; height: 46px; border-radius: 50%;
    font-size: 18px; display: flex; align-items: center; justify-content: center;
    transition: .15s;
  }
  button:hover:not(:disabled) { background: #ffffff1c; transform: translateY(-1px); }
  button:disabled { opacity: .35; cursor: not-allowed; }
  button.main {
    width: 60px; height: 60px; font-size: 24px;
    background: linear-gradient(140deg, #c8cdd3, #d6dade); border: 0;
    box-shadow: 0 10px 30px #c8cdd344;
  }
  button.main:hover:not(:disabled) { filter: brightness(1.1); }
  .hint { margin-top: 10px; font-size: 12px; color: var(--muted); }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 12px; }
  .panel {
    background: #2a2f34cc; border: 1px solid #ffffff0d; border-radius: 14px;
    padding: 14px 16px; font-size: 13px; color: var(--muted);
  }
  .panel h3 { margin: 0 0 10px; font-size: 12px; letter-spacing: .08em;
              text-transform: uppercase; color: #9aa0a7; }
  .lib { display: flex; justify-content: space-between; gap: 10px;
         padding: 5px 0; border-bottom: 1px solid #ffffff08; }
  .lib:last-child { border-bottom: 0; }
  .lib b { color: var(--text); font-weight: 600; }
  .lib-rev { color: #5f6a83; font-variant-numeric: tabular-nums; }
  .kv { display: flex; justify-content: space-between; gap: 12px; padding: 4px 0; }
  .kv span:last-child { color: #d2d6da; font-variant-numeric: tabular-nums; }
  .err { color: var(--err); word-break: break-all; }
  .toast {
    position: fixed; left: 50%; bottom: 30px; transform: translateX(-50%);
    background: #d7b0b0ee; color: #fff; padding: 10px 18px; border-radius: 10px;
    font-size: 14px; opacity: 0; pointer-events: none; transition: .25s;
  }
  .toast.show { opacity: 1; }
  .lib-head { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
  .lib-head h3 { margin: 0; }
  .lib-stat { margin-left: auto; font-size: 12px; color: var(--muted); }
  .lib-tools { display: flex; gap: 8px; margin-bottom: 10px; }
  .lib-tools input {
    flex: 1; background: #1e2226; border: 1px solid #ffffff12; color: var(--text);
    border-radius: 9px; padding: 8px 12px; font-size: 13px; outline: none;
  }
  .lib-tools input:focus { border-color: #c8cdd388; }
  .lib-tools button {
    width: auto; height: auto; border-radius: 9px; padding: 8px 14px;
    font-size: 12px;
  }
  .album-list { display: block; padding: 0; }
  .album-toolbar { position: relative; display: flex; justify-content: flex-end;
                   flex: 0 0 auto; margin-bottom: 8px; }
  .sort-btn {
    width: auto; height: 28px; padding: 0 9px; border-radius: 0;
    font-size: 10px; letter-spacing: .04em;
  }
  .sort-menu {
    display: none; position: absolute; z-index: 20; top: 32px; right: 0;
    min-width: 158px; padding: 4px; background: #2a2f34;
    border: 1px solid var(--line); box-shadow: 0 12px 30px #00000055;
  }
  .sort-menu.show { display: flex; flex-direction: column; }
  .sort-menu button {
    width: 100%; height: 26px; justify-content: flex-start;
    padding: 0 8px; border: 0; background: transparent;
    font-size: 10px; text-align: left;
  }
  .sort-menu button:hover, .sort-menu button.active { background: #3e464e; }
  .sort-sep { height: 1px; margin: 4px 0; background: var(--line); }
  .album-grid {
    flex: 1; min-height: 0; overflow-y: auto; align-content: start;
    display: grid; grid-template-columns: repeat(auto-fill, minmax(148px, 1fr));
    gap: 10px; padding: 1px;
  }
  .album-card {
    min-width: 0; padding: 7px; cursor: pointer; display: flex;
    flex-direction: column; gap: 5px; background: #2a2f34;
    border: 1px solid var(--line);
  }
  .album-card:hover { background: #383f46; }
  .album-card.active { background: #3e464e; border-color: #6b747e; }
  .album-cover {
    aspect-ratio: 1; position: relative; overflow: hidden;
    background: #1e2226; border: 1px solid var(--line-soft);
    display: grid; place-items: center;
  }
  .album-cover img { width: 100%; height: 100%; object-fit: cover;
                     display: block; }
  .album-cover.missing::after {
    content: "NO ART"; font-size: 9px; letter-spacing: .1em;
    color: var(--muted);
  }
  .album-card .album-name {
    color: var(--text); font-size: 11px; line-height: 1.3;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .album-card .album-artist {
    color: var(--muted); font-size: 10px; line-height: 1.3;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .album-card .album-meta { color: #9aa0a7; font-size: 9px; }
  .album-grid .empty {
    grid-column: 1 / -1; padding: 24px; text-align: center;
    color: var(--muted); font-size: 11px;
  }
  .media-wrap {
    max-height: 390px; overflow: auto; background: #10141f;
    border: 1px solid #ffffff0d; border-radius: 12px;
  }
  .media-table {
    width: 100%; border-collapse: separate; border-spacing: 0;
    table-layout: fixed; font-size: 12px;
  }
  .media-table th {
    position: sticky; top: 0; z-index: 1; background: #171c29;
    color: #aab0b7; font-weight: 500; text-align: left;
    padding: 8px 10px; border-bottom: 1px solid #ffffff12;
    white-space: nowrap;
  }
  .media-table td {
    padding: 7px 10px; border-bottom: 1px solid #ffffff06;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    color: #d2d6da;
  }
  .media-table tbody tr:last-child td { border-bottom: 0; }
  .media-table tbody tr { cursor: pointer; }
  .media-table tbody tr:hover { background: #ffffff0a; }
  .media-table tbody tr.active { background: #c8cdd31f; }
  .media-table .title { width: 34%; color: #e6e8ea; }
  .media-table .artist { width: 23%; color: #b4bac1; }
  .media-table .album-col { width: 22%; color: #aab0b7; }
  .media-table .date-col { width: 90px; }
  .media-table .cover { width: 42px; padding: 4px 6px; }
  .media-table .cover img { width: 30px; height: 30px; object-fit: cover;
                            display: block; border: 1px solid var(--line); }
  .media-table .num { width: 62px; text-align: right; color: #aab0b7;
                      font-variant-numeric: tabular-nums; }
  .media-table .col-index { width: 42px; }
  .media-table .actions { width: 124px; text-align: right; }
  .media-table .actions .icon-btn {
    width: auto !important; min-width: 22px; height: 22px !important;
    padding: 0 4px !important; font-size: 9px !important;
    white-space: nowrap;
  }
  .media-table .empty { padding: 22px 10px; text-align: center;
                        color: #9aa0a7; cursor: default; }
  .media-table tr.album, .media-table tr.track {
    display: table-row; padding: 0; gap: 0; border-radius: 0;
  }
  .media-table tr.track .tt, .media-table tr.album .tt {
    display: table-cell; flex: none;
  }
  .media-table .actions .icon-btn { vertical-align: middle; }
  .media-table .play { color: #c8cdd3; opacity: .45; margin-left: 4px; }
  .media-table tbody tr:hover .play { opacity: 1; }
  .album-detail {
    display: none; margin-top: 14px; padding-top: 12px;
    border-top: 1px solid #ffffff12;
  }
  .album-detail.show { display: block; }
  .detail-head { display: flex; align-items: center; gap: 10px;
                 margin-bottom: 8px; min-width: 0; }
  .detail-cover { width: 56px; height: 56px; object-fit: cover;
                  border: 1px solid var(--line); display: none; }
  .detail-head .t { font-size: 14px; color: var(--text); font-weight: 600;
                    white-space: nowrap; overflow: hidden;
                    text-overflow: ellipsis; }
  .detail-head .s { font-size: 11px; color: var(--muted); white-space: nowrap; }
  .detail-head button {
    width: auto; height: auto; border-radius: 8px; padding: 6px 12px;
    font-size: 12px; margin-left: auto;
  }
  .track-list { max-height: 320px; overflow-y: auto; }
  .track { display: flex; align-items: center; gap: 10px; padding: 7px 8px;
           border-radius: 8px; cursor: pointer; }
  .track:hover { background: #ffffff0a; }
  .track .idx { width: 28px; text-align: right; color: #9aa0a7;
                font-variant-numeric: tabular-nums; font-size: 12px; }
  .track .tt { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden;
               text-overflow: ellipsis; color: #e6e8ea; }
  .track .du { font-size: 11px; color: #aab0b7;
               font-variant-numeric: tabular-nums; }
  .track .play { opacity: 0; color: #c8cdd3; }
  .track:hover .play { opacity: 1; }
  .track-actions { display: flex; gap: 4px; }
  .icon-btn {
    width: 26px !important; height: 26px !important; padding: 0 !important;
    border-radius: 7px !important; font-size: 13px !important;
    background: #ffffff0d !important; color: #c2c7cd !important;
    box-shadow: none !important;
  }
  .icon-btn:hover { background: #ffffff1a !important; color: #fff !important; }
  .lib-tabs { display: flex; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; }
  .tab {
    width: auto; height: auto; padding: 7px 14px; border-radius: 9px;
    font-size: 12px; background: #ffffff08; color: #b4bac1;
    box-shadow: none; border: 1px solid transparent;
  }
  .tab:hover { background: #ffffff0f; color: #eef0f2; }
  .tab.active {
    background: #c8cdd322; color: #eef0f2; border-color: #c8cdd344;
  }
  .tab-pane { display: none; }
  .tab-pane.active { display: block; }
  .playlist-bar { display: flex; gap: 8px; margin-bottom: 10px; }
  .playlist-bar input {
    flex: 1; background: #1e2226; border: 1px solid #ffffff12;
    color: var(--text); border-radius: 9px; padding: 8px 12px;
    font-size: 13px; outline: none;
  }
  .playlist-bar input:focus { border-color: #c8cdd388; }
  .playlist-bar button {
    width: auto; height: auto; border-radius: 9px; padding: 8px 14px;
    font-size: 12px;
  }
  .more-row { display: flex; justify-content: center; margin-top: 8px; }
  .more-row button {
    width: auto; height: auto; padding: 7px 16px; border-radius: 9px;
    font-size: 12px;
  }
  .detail-actions { display: flex; gap: 8px; margin: 8px 0; flex-wrap: wrap; }
  .detail-actions button {
    width: auto; height: auto; padding: 6px 12px; border-radius: 8px;
    font-size: 12px;
  }
  .queue { font-size: 11px; color: var(--muted); margin-top: 4px; }
  .np-details { font-size: 11px; color: #9aa0a7; margin-top: 2px; }
  .sort-select {
    width: 100%; background: #1e2226; border: 1px solid #ffffff12;
    color: var(--text); border-radius: 9px; padding: 8px 12px;
    font-size: 13px; outline: none;
  }
  .lib-note { font-size: 11px; color: #aab0b7; margin-bottom: 8px;
              line-height: 1.5; }
  .smart-list { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
  .smart-row {
    padding: 8px 12px; border-radius: 9px; background: #c8cdd314;
    border: 1px solid #c8cdd32a; color: #e6e8ea; font-size: 12px;
    cursor: pointer;
  }
  .smart-row:hover { background: #c8cdd326; }
  .import-label {
    display: inline-flex; align-items: center; padding: 8px 14px;
    border-radius: 9px; background: #ffffff0d; color: #c2c7cd;
    font-size: 12px; cursor: pointer;
  }
  .upnext { max-height: 220px; overflow-y: auto; }
  .upnext-row {
    display: flex; gap: 8px; padding: 6px 4px; border-radius: 8px;
    cursor: pointer; font-size: 12px; color: #d2d6da;
  }
  .upnext-row:hover { background: #ffffff0a; }
  .upnext-row .i { color: #9aa0a7; width: 20px; text-align: right; }
  .upnext-row .t { flex: 1; min-width: 0; white-space: nowrap;
                   overflow: hidden; text-overflow: ellipsis; }
  .upnext-row .a { color: #aab0b7; font-size: 11px; }
  .upnext-row.cur { background: #c8cdd318; }
  .extra-controls {
    display: flex; gap: 8px; margin-top: 14px; flex-wrap: wrap;
  }
  .extra-controls button {
    width: auto; height: auto; padding: 7px 12px; border-radius: 9px;
    font-size: 12px;
  }
  .extra-controls .on { color: #e6e8ea; border-color: #c8cdd366; }
  .modal {
    position: fixed; inset: 0; background: #000000aa; display: none;
    align-items: center; justify-content: center; z-index: 50;
    padding: 20px;
  }
  .modal.show { display: flex; }
  .modal-card {
    width: min(420px, 100%); background: #171c2b; border-radius: 16px;
    border: 1px solid #ffffff14; padding: 18px;
    box-shadow: 0 24px 80px #00000088;
  }
  .modal-card h3 { margin: 0 0 12px; font-size: 15px; color: var(--text); }
  .modal-list { display: flex; flex-direction: column; gap: 6px;
                max-height: 260px; overflow-y: auto; }
  .modal-item {
    padding: 9px 10px; border-radius: 9px; background: #ffffff08;
    cursor: pointer; color: #e6e8ea; font-size: 13px;
    display: flex; justify-content: space-between; gap: 10px;
  }
  .modal-item:hover { background: #c8cdd322; }
  .modal-item .n { color: #aab0b7; font-size: 11px; }
  .modal-new { display: flex; gap: 8px; margin-top: 12px; }
  .modal-new input {
    flex: 1; background: #1e2226; border: 1px solid #ffffff12;
    color: var(--text); border-radius: 9px; padding: 8px 12px;
    font-size: 13px; outline: none;
  }
  .modal-close {
    width: 100%; margin-top: 12px; padding: 8px; border-radius: 9px;
    background: #ffffff0d; color: #c2c7cd; font-size: 12px;
  }

  /* -- box theme: flat dark panels, square corners, visible frame lines -- */
  :root {
    --bg: #22262a;
    --card: #2a2f34;
    --card2: #2a2f34;
    --text: #e6e8ea;
    --muted: #aab0b7;
    --accent: #d6dade;
    --ok: #b9c8bf;
    --warn: #d1c5ad;
    --err: #d7b0b0;
    --line: #4b535b;
    --line-soft: #394046;
  }
  body { background: var(--bg); }
  .dot { display: none; }
  .lib-head { border-bottom: 1px solid var(--line); padding-bottom: 8px; }
  .topbar { border-bottom: 1px solid var(--line); padding-bottom: 8px;
            margin-bottom: 10px; }
  .card, .panel, .modal-card {
    background: var(--card);
    border: 1px solid var(--line);
    box-shadow: none;
  }
  .card { background: var(--card); }
  .panel h3 {
    border-bottom: 1px solid var(--line); padding-bottom: 6px;
    margin-bottom: 8px; color: #c2c7cd;
  }
  .art-box {
    background: #1e2226; border: 1px solid var(--line); box-shadow: none;
  }
  .badge {
    background: #2b3035; border: 1px solid var(--line);
  }
  .badge.playing {
    background: #38433d; border-color: #5b6d62; color: var(--ok);
  }
  .badge.paused {
    background: #464138; border-color: #6d6251; color: var(--warn);
  }
  button {
    background: #2b3035; border: 1px solid var(--line); box-shadow: none;
  }
  button:hover:not(:disabled) { background: #1a2028; transform: none; }
  button.main {
    background: #182333; border: 1px solid #6b747e;
    box-shadow: none;
  }
  button.main:hover:not(:disabled) { background: #203047; filter: none; }
  .lib-tools input, .playlist-bar input, .sort-select, .modal-new input {
    background: #1e2226; border: 1px solid var(--line);
  }
  .lib-tools input:focus, .playlist-bar input:focus, .sort-select:focus,
  .modal-new input:focus { border-color: #7a838d; }
  .tab { background: transparent; border: 1px solid transparent; }
  .tab:hover { background: #383f46; }
  .tab.active {
    background: #3e464e; border-color: #6b747e; color: #eef0f2;
  }
  .kv { border-bottom: 1px solid var(--line-soft); }
  .kv:last-child { border-bottom: 0; }
  .media-wrap {
    background: #1e2226; border: 1px solid var(--line);
  }
  .media-table th {
    background: #30363c; border-right: 1px solid var(--line);
    border-bottom: 1px solid var(--line); color: #b4bac1;
  }
  .media-table th:last-child { border-right: 0; }
  .media-table td {
    border-right: 1px solid var(--line-soft);
    border-bottom: 1px solid var(--line-soft);
  }
  .media-table td:last-child { border-right: 0; }
  .media-table tbody tr:hover { background: #383f46; }
  .media-table tbody tr.active { background: #3e464e; }
  .upnext-row { border-bottom: 1px solid var(--line-soft); }
  .upnext-row.cur { background: #3e464e; }
  .smart-row, .import-label {
    background: #2b3035; border: 1px solid var(--line);
  }
  .bar { background: #3a4047; border-radius: 0; }
  .bar span {
    border-radius: 0;
    background: var(--accent);
  }
  .toast {
    background: #30363c; border: 1px solid #555d66; color: var(--text);
  }
  .modal { background: #000000d0; }
  .modal-card { box-shadow: none; }
  .modal-item {
    background: #1e2226; border: 1px solid var(--line-soft);
  }
  .modal-item:hover { background: #3e464e; }
  .modal-close { background: #2b3035; border: 1px solid var(--line); }
  button, input, select, .card, .panel, .art-box, .badge, .tab, .smart-row,
  .import-label, .media-wrap, .media-table, .modal-card, .modal-item,
  .modal-new input, .modal-close, .toast, .icon-btn, .upnext-row {
    border-radius: 0 !important;
  }

  body {
    font-family: "Iosevka", "JetBrains Mono", "IBM Plex Mono", ui-monospace,
                 SFMono-Regular, Menlo, Consolas, monospace;
    letter-spacing: .01em;
  }
  .brand { color: #eef0f2; font-weight: 700; letter-spacing: .14em; }
  .brand::before {
    content: ""; display: inline-block; width: 7px; height: 7px;
    margin-right: 8px; border: 1px solid var(--accent);
    vertical-align: 1px;
  }
  .panel h3, .lib-head h3 {
    text-transform: uppercase; letter-spacing: .14em;
    font-size: 11px; color: #c2c7cd;
  }
  .panel .panel-head h3 {
    margin: 0; padding: 0; border: 0;
    font-size: 11px; line-height: 18px;
    letter-spacing: .14em; text-transform: uppercase;
    color: #c2c7cd;
  }
  .panel-head .small-btn {
    height: 18px; min-height: 18px; padding: 0 7px;
    line-height: 1; display: inline-flex; align-items: center;
    align-self: center;
  }
  button, .tab, .badge {
    text-transform: uppercase; letter-spacing: .05em;
  }
  .tab { font-size: 10px; }
  .lib-tabs {
    flex-wrap: nowrap; gap: 0; overflow-x: auto;
    border: 1px solid var(--line);
    width: max-content; max-width: 100%;
  }
  .lib-tabs .tab {
    border: 0; border-right: 1px solid var(--line);
    border-radius: 0 !important;
  }
  .lib-tabs .tab:last-child { border-right: 0; }
  .media-table th {
    text-transform: uppercase; letter-spacing: .1em;
    font-size: 10px;
  }
  .media-table td { font-size: 11.5px; }
  .art-box { background: #1e2226; }
  .badge { background: #30363c; }
  .card, .panel, .modal-card { background: #2a2f34; }
  button { background: #2b3035; }
  button:hover:not(:disabled) { background: #383f46; }
  button.main { background: #3a4148; border-color: #6b747e; }
  button.main:hover:not(:disabled) { background: #424a52; }
  .lib-tools input, .playlist-bar input, .sort-select, .modal-new input {
    background: #1e2226;
  }
  .tab.active { background: #3e464e; border-color: #6b747e; }
  .media-wrap { background: #1e2226; }
  .media-table th { background: #30363c; }
  .media-table tbody tr:hover { background: #383f46; }
  .media-table tbody tr.active { background: #3e464e; }
  .upnext-row.cur { background: #3e464e; }
  .smart-row, .import-label { background: #2b3035; }
  .toast { background: #30363c; }
  .modal-item { background: #1e2226; }
  .modal-item:hover { background: #3e464e; }
  .modal-close { background: #2b3035; }
  .bar span { background: var(--accent); }
  *, *::before, *::after { animation: none !important; }
  button, .tab, .toast, .bar span { transition: none !important; }

  @media (max-width: 900px) {
    body { height: auto; overflow: auto; }
    .wrap { width: 96vw; height: auto; min-height: 100vh; padding: 12px 0; }
    .layout { display: flex; flex-direction: column; flex: none; }
    .now-card { display: flex; flex-direction: column; }
    .now-card .controls { margin-top: 8px; }
    .art-box { flex-basis: 110px; width: 110px; height: 110px; }
    .now-card h1 { font-size: 18px; }
    .panel { padding: 12px; }
    .left { overflow: visible; scrollbar-gutter: auto; padding-right: 0; }
    .library-panel {
      height: auto; overflow: visible; scrollbar-gutter: auto;
    }
    .library-panel .media-wrap { flex: none; max-height: 520px; }
  }
  @media (max-width: 760px) {
    .media-table .album-col, .media-table .artist { display: none; }
    .media-table .title { width: 55%; }
    .media-table th, .media-table td { padding: 7px 8px; }
    .media-table .num { width: 54px; }
    .media-table .actions { width: 104px; }
  }
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div class="brand">MediaCtrl / iAP2</div>
    <div><span class="dot" id="conn-dot"></span><span id="conn-text">Connecting…</span></div>
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
        <div class="bar"><span id="bar"></span></div>
        <div class="times"><span id="elapsed">0:00</span><span id="duration">0:00</span></div>
      </div>

      <div class="controls">
        <button id="btn-prev" title="Previous">Prev</button>
        <button id="btn-toggle" class="main" title="Play/Pause">Play</button>
        <button id="btn-next" title="Next">Next</button>
      </div>
      <div class="extra-controls">
        <div class="volume-row">
          <button id="btn-vol-down" title="Volume down">Vol -</button>
          <button id="btn-vol-up" title="Volume up">Vol +</button>
          <button id="btn-mute" title="Mute">Mute</button>
        </div>
        <button id="btn-shuffle" title="Shuffle">Shuffle Off</button>
        <button id="btn-repeat" title="Repeat mode">Repeat: Off</button>
        <button id="btn-stop" title="Stop">Stop</button>
        <button id="btn-current-selection" title="Resume iPod queue">Resume queue</button>
      </div>
    </div>
    <div class="panel queue-panel">
      <div class="panel-head">
        <h3>Up Next</h3>
        <span class="panel-note" id="upnext-note"></span>
      </div>
      <div class="upnext" id="upnext"></div>
    </div>
    <div class="panel library-info-panel">
      <div class="panel-head">
        <h3>Library</h3>
        <button id="btn-resync" class="small-btn" title="Clear local mirror and resync">Resync</button>
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
      <div class="kv"><span>Artwork ID / Size</span><span id="kv-art">—</span></div>
      <div class="kv"><span>Last event</span><span id="kv-event">—</span></div>
      <div class="kv"><span>Error</span><span id="kv-err" class="err">—</span></div>
    </div>
    </aside>
    <div class="panel library-panel">
      <div class="panel-head">
        <h3>Browser</h3>
      </div>
      <div class="lib-tabs">
        <button class="tab active" data-tab="albums">Albums</button>
        <button class="tab" data-tab="artists">Artists</button>
        <button class="tab" data-tab="album-artists">Album Artists</button>
        <button class="tab" data-tab="genres">Genres</button>
        <button class="tab" data-tab="songs">Songs</button>
        <button class="tab" data-tab="recent">Recently Added</button>
        <button class="tab" data-tab="playlists">Device Playlists</button>
      </div>
      <div class="lib-tools">
        <input id="lib-search" placeholder="Search albums…">
      </div>
      <div class="tab-pane active" id="pane-albums">
        <div class="album-toolbar">
          <button id="albums-sort-btn" class="sort-btn">Sort: Recently Added / Desc</button>
          <div class="sort-menu" id="albums-sort-menu">
            <button data-album-sort="recent">Recently Added</button>
            <button data-album-sort="name">Name</button>
            <button data-album-sort="artist">Artist</button>
            <div class="sort-sep"></div>
            <button data-album-dir="desc">Descending</button>
            <button data-album-dir="asc">Ascending</button>
          </div>
        </div>
        <div class="album-grid" id="lib-albums"></div>
      </div>
      <div class="tab-pane" id="pane-artists">
        <div class="album-list" id="lib-artists"></div>
        <div class="more-row">
          <button id="btn-more-artists" style="display:none">Load more artists</button>
        </div>
      </div>
      <div class="tab-pane" id="pane-album-artists">
        <div class="album-list" id="lib-album-artists"></div>
        <div class="more-row">
          <button id="btn-more-album-artists" style="display:none">Load more album artists</button>
        </div>
      </div>
      <div class="tab-pane" id="pane-genres">
        <div class="album-list" id="lib-genres"></div>
        <div class="more-row">
          <button id="btn-more-genres" style="display:none">Load more genres</button>
        </div>
      </div>
      <div class="tab-pane" id="pane-songs">
        <div class="lib-tools" style="margin-bottom:8px">
          <select id="songs-sort" class="sort-select">
            <option value="title">Title</option>
            <option value="artist">Artist</option>
            <option value="album">Album</option>
            <option value="duration">Duration</option>
            <option value="recent">Recently Added</option>
          </select>
        </div>
        <div class="album-list" id="lib-songs"></div>
        <div class="more-row">
          <button id="btn-more-songs" style="display:none">Load more songs</button>
        </div>
      </div>
      <div class="tab-pane" id="pane-recent">
        <div class="lib-note" id="recent-note"></div>
        <div class="album-list" id="lib-recent"></div>
        <div class="more-row">
          <button id="btn-more-recent" style="display:none">Load more</button>
        </div>
      </div>
      <div class="tab-pane" id="pane-playlists">
        <div class="album-list" id="lib-playlists"></div>
      </div>
      <div class="album-detail" id="lib-detail">
        <div class="detail-head">
          <img class="detail-cover" id="detail-cover" alt="">
          <div class="t" id="detail-title"></div>
          <div class="s" id="detail-sub"></div>
          <button id="btn-play-detail">Play</button>
          <button id="btn-detail-close">Close</button>
        </div>
        <div class="detail-actions" id="detail-actions"></div>
        <div class="track-list" id="detail-tracks"></div>
      </div>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>
<div class="modal" id="pl-modal">
  <div class="modal-card">
    <h3 id="pl-modal-title">Add to playlist</h3>
    <div class="modal-list" id="pl-modal-list"></div>
    <div class="modal-new">
      <input id="pl-modal-new" placeholder="New playlist name…">
      <button id="pl-modal-create">Create & add</button>
    </div>
    <button class="modal-close" id="pl-modal-close">Cancel</button>
  </div>
</div>

<script>
const $ = (s) => document.querySelector(s);
const STATUS = {0: "Stopped", 1: "Playing", 2: "Paused", 3: "Seeking forward", 4: "Seeking backward"};
const LINK = {0: "Detecting", 1: "Negotiating", 2: "Ready", 3: "Disconnected"};
let state = null;
let shownArt = null;

function fmt(ms) {
  ms = Math.max(0, ms || 0);
  const s = Math.floor(ms / 1000);
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
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

function render(s) {
  const np = s.now_playing || {};
  const connected = !!s.connected;
  const linkOk = s.link_state === 2;

  $("#conn-dot").className = "dot " + (connected && linkOk ? "ok" : (connected ? "warn" : ""));
  $("#conn-text").textContent = connected
      ? (linkOk ? "Connected" : "Negotiating link…")
      : "Waiting for iPod…";

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
  $("#btn-shuffle").textContent = np.shuffle == null
      ? "Shuffle —" : (np.shuffle ? "Shuffle On" : "Shuffle Off");
  $("#btn-repeat").textContent = "Repeat: " +
      (np.repeat == null ? "—" : (REPEAT[np.repeat] || np.repeat));
  $("#btn-shuffle").classList.toggle("on",
      np.shuffle === 1 || np.shuffle === 2);
  $("#btn-repeat").classList.toggle("on",
      np.repeat === 1 || np.repeat === 2);
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
  $("#upnext-note").textContent = q.source === "device"
      ? "Device queue" : (q.total ? "Local queue" : "");
  if (q.items && q.items.length) {
    const qbase = q.base_index || 0;
    $("#upnext").innerHTML = q.items.map((t, i) => `
        <div class="upnext-row ${i === 0 ? "cur" : ""}"
             data-id="${esc(t.id)}">
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
  $("#btn-toggle").textContent = status === 1 ? "Pause" : "Play";

  $("#kv-conn").textContent = connected ? "Authenticated" : "Disconnected";
  $("#kv-link").textContent = LINK[s.link_state] ?? "—";
  $("#kv-app").textContent = np.app || "—";
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
}

function tick() {
  if (!state) return;
  const np = state.now_playing || {};
  let elapsed = np.elapsed_ms || 0;
  if (np.status === 1 && np.updated_at) elapsed += Date.now() - np.updated_at;
  const dur = np.duration_ms || 0;
  if (dur > 0) elapsed = Math.min(elapsed, dur);
  $("#elapsed").textContent = fmt(elapsed);
  $("#duration").textContent = fmt(dur);
  $("#bar").style.width = dur > 0 ? (100 * elapsed / dur).toFixed(1) + "%" : "0%";
}

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
let pendingAddItem = null;

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
    playlists: "Search device playlists…",
  }[name] || "Search…";
  $("#lib-search").placeholder = ph;
  $("#lib-search").value = "";
  renderActiveTab(true);
}

function compareAlbums(a, b) {
  const dir = albumDir === "asc" ? 1 : -1;
  let av, bv;
  if (albumSort === "name") {
    av = (a.album || "").toLowerCase();
    bv = (b.album || "").toLowerCase();
  } else if (albumSort === "artist") {
    av = (a.artist || "").toLowerCase();
    bv = (b.artist || "").toLowerCase();
  } else {
    av = Number(a.date_added) || 0;
    bv = Number(b.date_added) || 0;
  }
  if (av < bv) return -1 * dir;
  if (av > bv) return 1 * dir;
  return (a.album || "").localeCompare(b.album || "") ||
         (a.artist || "").localeCompare(b.artist || "");
}

function updateAlbumSortUI() {
  const labels = {recent: "Recently Added", name: "Name", artist: "Artist"};
  $("#albums-sort-btn").textContent =
      `Sort: ${labels[albumSort]} / ${albumDir === "asc" ? "Asc" : "Desc"}`;
  document.querySelectorAll("#albums-sort-menu button").forEach(btn => {
    const match = (btn.dataset.albumSort && btn.dataset.albumSort === albumSort) ||
                  (btn.dataset.albumDir && btn.dataset.albumDir === albumDir);
    btn.classList.toggle("active", !!match);
  });
}

function renderAlbums() {
  if (!library) return;
  const q = ($("#lib-search").value || "").trim().toLowerCase();
  const albums = (library.albums || []).filter(a =>
      !q || (a.album || "").toLowerCase().includes(q) ||
      (a.artist || "").toLowerCase().includes(q));
  // Rebuild only when the library revision or the search term changes;
  // otherwise keep the current DOM (and scroll position) intact.
  const key = (library.revision || "") + "|" + q + "|" +
      albumSort + "|" + albumDir;
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
  if (reset) {
    recentOffset = 0;
    $("#lib-recent").innerHTML = "";
  }
  try {
    const d = await apiJson(
        `/api/library/recent?limit=100&offset=${recentOffset}`,
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
        <span class="play">Play</span>
      </td>
    </tr>`;
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
    $("#btn-play-detail").textContent = "Play album";
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
    $("#btn-play-detail").textContent = "Play artist";
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
      <td class="actions"><span class="play">Play</span></td>
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
    $("#btn-play-detail").textContent = "Play genre";
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
    $("#btn-play-detail").textContent = "Play album artist";
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
    $("#btn-play-detail").textContent = "Play playlist";
    $("#btn-play-detail").style.display = d.items.length ? "" : "none";
    $("#detail-actions").innerHTML = `
        <button data-action="rename-playlist">Rename</button>
        <button data-action="clear-playlist">Clear</button>
        <button data-action="delete-playlist">Delete</button>`;
    $("#detail-tracks").innerHTML = mediaTable(
        TABLE_COLS.songs,
        d.items.map((t, i) => {
          const actions = `
              <button class="icon-btn" data-move-up="1" title="Move up">Up</button>
              <button class="icon-btn" data-move-down="1" title="Move down">Down</button>
              <button class="icon-btn" data-remove="1" title="Remove">Remove</button>`;
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
    $("#btn-play-detail").textContent = "Play device playlist";
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

function openAddModal(ids, title) {
  pendingAddItem = {ids: ids, title: title || ""};
  $("#pl-modal-title").textContent = title
      ? `Add to playlist: ${title}` : "Add to playlist";
  $("#pl-modal-new").value = "";
  $("#pl-modal-list").innerHTML = '<div class="modal-item">Loading…</div>';
  $("#pl-modal").classList.add("show");
  apiJson("/api/playlists", {cache: "no-store"}).then(d => {
    const rows = (d.playlists || []).map(p => `
      <div class="modal-item" data-id="${esc(p.id)}">
        <span>${esc(p.name)}</span><span class="n">${p.tracks} tracks</span>
      </div>`);
    $("#pl-modal-list").innerHTML = rows.join("") ||
        '<div class="modal-item">No playlists yet. Create one below.</div>';
  }).catch(() => {
    $("#pl-modal-list").innerHTML =
        '<div class="modal-item">Playlist load failed</div>';
  });
}

async function addPendingToPlaylist(plId) {
  if (!pendingAddItem) return;
  try {
    const j = await postJson(
        `/api/playlists/${encodeURIComponent(plId)}/add`,
        {item_ids: pendingAddItem.ids});
    if (!j.ok) { toast(j.error || "Add failed"); return; }
    const parts = [];
    if (j.added) parts.push(`Added ${j.added} tracks`);
    if (j.already) parts.push(`${j.already} already present`);
    if (j.missing) parts.push(`${j.missing} missing`);
    toast(parts.join(", ") || "Done");
    $("#pl-modal").classList.remove("show");
    if (activeTab === "playlists") loadPlaylists();
  } catch (e) {
    toast("Add failed");
  }
}

async function createPlaylistAndAdd(name) {
  name = (name || "").trim();
  if (!name) { toast("Enter a playlist name"); return; }
  try {
    const c = await postJson("/api/playlists", {name: name});
    if (!c.ok) { toast(c.error || "Create failed"); return; }
    await addPendingToPlaylist(c.playlist.id);
  } catch (e) {
    toast("Create failed");
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
    else loadPlaylists();
  }, 250);
};

$("#btn-more-artists").onclick = () => loadArtists(false);
$("#btn-more-album-artists").onclick = () => loadAlbumArtists(false);
$("#btn-more-genres").onclick = () => loadGenres(false);
$("#btn-more-songs").onclick = () => loadSongs(false);
$("#btn-more-recent").onclick = () => loadRecent(false);
$("#songs-sort").onchange = () => loadSongs(true);

$("#albums-sort-btn").onclick = (ev) => {
  ev.stopPropagation();
  $("#albums-sort-menu").classList.toggle("show");
};

$("#albums-sort-menu").onclick = (ev) => {
  ev.stopPropagation();
  const field = ev.target.closest("[data-album-sort]");
  const dir = ev.target.closest("[data-album-dir]");
  if (field) albumSort = field.dataset.albumSort;
  if (dir) albumDir = dir.dataset.albumDir;
  if (field || dir) {
    lastAlbumKey = null;
    renderAlbums();
  }
  $("#albums-sort-menu").classList.remove("show");
};

document.addEventListener("click", () => {
  const menu = $("#albums-sort-menu");
  if (menu) menu.classList.remove("show");
});

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

$("#upnext").onclick = (ev) => {
  const el = ev.target.closest(".upnext-row[data-id]");
  if (el) playItem(el.dataset.id);
};

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

$("#pl-modal-list").onclick = (ev) => {
  const el = ev.target.closest(".modal-item[data-id]");
  if (el) addPendingToPlaylist(el.dataset.id);
};

$("#pl-modal-create").onclick =
    () => createPlaylistAndAdd($("#pl-modal-new").value);

$("#pl-modal-close").onclick =
    () => $("#pl-modal").classList.remove("show");

$("#pl-modal").onclick = (ev) => {
  if (ev.target.id === "pl-modal") $("#pl-modal").classList.remove("show");
};

$("#btn-resync").onclick = async () => {
  if (!confirm("Clear the local media mirror and resync from iPod?")) return;
  try {
    const j = await postJson("/api/library/sync", {});
    toast(j.ok ? "Resync requested" : (j.error || "Resync request failed"));
    setTimeout(pollLibrary, 1500);
  } catch (e) {
    toast("Resync request failed");
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
                offset=(qs.get("offset") or ["0"])[0]))
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
            load_media_artwork()
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
            root = os.path.abspath(MEDIA_ARTWORK_DIR)
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
            elif path == "/api/library/sync":
                SESSION.resync_library()
                self._json({"ok": True, "message": "resync requested"})
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
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "error": str(exc)}, 500)


def main():
    print(f"MediaCtrl web UI on http://0.0.0.0:{PORT}/")
    load_library()
    load_media_dates()
    load_media_artwork()
    load_playlists()
    load_history()
    SESSION.start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
