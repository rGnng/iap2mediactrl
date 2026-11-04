#!/usr/bin/env python3
"""SQLite cache for MediaLibrary dates and artwork mappings.

The AFC importers write this database; the web service only reads it.  The
schema is intentionally small and portable so the same layout can be used by
the future Tab5 firmware.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

SCHEMA_VERSION = 1
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS dates (
    pid        TEXT PRIMARY KEY,
    date_added REAL NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS artwork_files (
    sha1        TEXT PRIMARY KEY,
    rel         TEXT,
    file        TEXT NOT NULL,
    thumb       TEXT,
    mime        TEXT NOT NULL,
    bytes       INTEGER NOT NULL DEFAULT 0,
    thumb_bytes INTEGER NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS artwork_items (
    pid      TEXT PRIMARY KEY,
    token    TEXT,
    sha1     TEXT NOT NULL,
    fallback INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS artwork_items_sha1 ON artwork_items(sha1);

CREATE TABLE IF NOT EXISTS artwork_albums (
    pid      TEXT PRIMARY KEY,
    token    TEXT,
    sha1     TEXT NOT NULL,
    fallback INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS artwork_albums_sha1 ON artwork_albums(sha1);
"""


def open_db(path, create=True):
    path = Path(path)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    ensure_schema(conn)
    return conn


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),))
        conn.commit()
    elif int(row["value"]) != SCHEMA_VERSION:
        raise RuntimeError(
            f"media cache schema {row['value']} is not supported "
            f"(expected {SCHEMA_VERSION})")


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return row["value"]


def set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value, ensure_ascii=False)))


def _sha_from_file(file_path):
    if not file_path:
        return None
    stem = os.path.splitext(os.path.basename(file_path))[0].lower()
    if SHA1_RE.match(stem):
        return stem
    return hashlib.sha1(str(file_path).encode("utf-8")).hexdigest()


def _stat_size(base_dir, rel_path):
    if not rel_path:
        return 0
    try:
        return os.stat(os.path.join(base_dir or "", rel_path)).st_size
    except OSError:
        return 0


def _migrate_json(conn, dates_path, artwork_path, base_dir):
    """Import the legacy JSON result files once, when the DB is empty."""
    migrated = get_meta(conn, "legacy_json_migrated", False)
    if migrated:
        return
    now = time.time()
    if dates_path and os.path.exists(dates_path):
        if conn.execute("SELECT COUNT(*) FROM dates").fetchone()[0] == 0:
            try:
                with open(dates_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                dates = data.get("dates", {})
                if isinstance(dates, dict) and dates:
                    conn.executemany(
                        "INSERT OR REPLACE INTO dates(pid, date_added) "
                        "VALUES(?, ?)",
                        [(str(pid), float(ts)) for pid, ts in dates.items()])
                    set_meta(conn, "dates_source", {
                        "source": str(dates_path),
                        "migrated_from_json": True,
                        "generated_at": data.get("generated_at"),
                        "stats": data.get("stats", {}),
                    })
                    print(f"media cache: migrated {len(dates)} dates from "
                          f"{dates_path}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"media cache: dates migration failed: {exc}",
                      flush=True)

    if artwork_path and os.path.exists(artwork_path):
        if conn.execute("SELECT COUNT(*) FROM artwork_items").fetchone()[0] == 0:
            try:
                with open(artwork_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                _insert_legacy_artwork(
                    conn, data.get("items", {}), data.get("albums", {}),
                    base_dir, now)
                set_meta(conn, "artwork_source", {
                    "source": str(artwork_path),
                    "migrated_from_json": True,
                    "generated_at": data.get("generated_at"),
                    "stats": data.get("stats", {}),
                })
                print("media cache: migrated legacy artwork JSON",
                      flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"media cache: artwork migration failed: {exc}",
                      flush=True)

    set_meta(conn, "legacy_json_migrated", True)
    if not get_meta(conn, "dates_updated_at"):
        set_meta(conn, "dates_updated_at", now)
    conn.commit()


def _insert_legacy_artwork(conn, items, albums, base_dir, now):
    files = {}
    for entry in list(items.values()) + list(albums.values()):
        if not isinstance(entry, dict):
            continue
        file_path = entry.get("file")
        if not file_path:
            continue
        sha1 = _sha_from_file(file_path)
        if not sha1:
            continue
        thumb = entry.get("thumb")
        mime = entry.get("mime") or "application/octet-stream"
        files[sha1] = {
            "sha1": sha1,
            "rel": None,
            "file": file_path,
            "thumb": thumb,
            "mime": mime,
            "bytes": _stat_size(base_dir, file_path),
            "thumb_bytes": _stat_size(base_dir, thumb),
            "updated_at": now,
        }
    if files:
        conn.executemany(
            """INSERT OR REPLACE INTO artwork_files
               (sha1, rel, file, thumb, mime, bytes, thumb_bytes, updated_at)
               VALUES(:sha1, :rel, :file, :thumb, :mime, :bytes,
                      :thumb_bytes, :updated_at)""",
            list(files.values()))

    def mapping_rows(source):
        rows = []
        for pid, entry in source.items():
            if not isinstance(entry, dict):
                continue
            sha1 = _sha_from_file(entry.get("file"))
            if not sha1:
                continue
            rows.append((str(pid), entry.get("token"), sha1,
                         1 if entry.get("fallback") else 0))
        return rows

    item_rows = mapping_rows(items)
    album_rows = mapping_rows(albums)
    if item_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO artwork_items(pid, token, sha1, fallback) "
            "VALUES(?, ?, ?, ?)", item_rows)
    if album_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO artwork_albums(pid, token, sha1, fallback) "
            "VALUES(?, ?, ?, ?)", album_rows)


def ensure_cache(path, legacy_dates_path=None, legacy_artwork_path=None,
                 base_dir=None):
    """Open the cache, creating the schema and importing legacy JSON once."""
    conn = open_db(path)
    try:
        _migrate_json(conn, legacy_dates_path, legacy_artwork_path, base_dir)
        set_meta(conn, "last_opened_at", time.time())
        conn.commit()
    finally:
        conn.close()


def load_dates(conn):
    return {row["pid"]: float(row["date_added"])
            for row in conn.execute("SELECT pid, date_added FROM dates")}


def replace_dates(conn, dates, source=None):
    dates = {str(pid): float(ts) for pid, ts in dates.items()}
    old = load_dates(conn)
    old_keys = set(old)
    new_keys = set(dates)
    updated = sum(1 for key in old_keys & new_keys
                  if abs(old[key] - dates[key]) > 0.5)
    stats = {
        "rows": len(dates),
        "new": len(new_keys - old_keys),
        "updated": updated,
        "removed": len(old_keys - new_keys),
    }
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM dates")
        conn.executemany(
            "INSERT INTO dates(pid, date_added) VALUES(?, ?)",
            list(dates.items()))
        set_meta(conn, "dates_updated_at", time.time())
        if source is not None:
            set_meta(conn, "dates_source", source)
        set_meta(conn, "dates_stats", stats)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return stats


def _mapping_tuple(row):
    return (row["token"], row["sha1"], int(row["fallback"] or 0))


def _mapping_diff(old, new):
    old_keys = set(old)
    new_keys = set(new)
    return {
        "new": len(new_keys - old_keys),
        "updated": sum(1 for key in old_keys & new_keys
                       if old[key] != new[key]),
        "removed": len(old_keys - new_keys),
    }


def load_mapping_rows(conn):
    items = {row["pid"]: _mapping_tuple(row)
             for row in conn.execute(
                 "SELECT pid, token, sha1, fallback FROM artwork_items")}
    albums = {row["pid"]: _mapping_tuple(row)
              for row in conn.execute(
                  "SELECT pid, token, sha1, fallback FROM artwork_albums")}
    return items, albums


def load_file_records(conn):
    return {
        row["sha1"]: {
            "sha1": row["sha1"],
            "rel": row["rel"],
            "file": row["file"],
            "thumb": row["thumb"],
            "mime": row["mime"],
            "bytes": int(row["bytes"] or 0),
            "thumb_bytes": int(row["thumb_bytes"] or 0),
        }
        for row in conn.execute(
            "SELECT sha1, rel, file, thumb, mime, bytes, thumb_bytes "
            "FROM artwork_files")
    }


def replace_artwork(conn, items, albums, files, source=None):
    """Replace item/album mappings and file metadata.

    ``items``/``albums`` map PID -> {token, sha1, fallback}; ``files`` maps
    SHA1 -> {rel, file, thumb, mime, bytes, thumb_bytes}.
    """
    old_items, old_albums = load_mapping_rows(conn)
    new_items = {str(pid): (entry.get("token"), entry["sha1"],
                            1 if entry.get("fallback") else 0)
                 for pid, entry in items.items()}
    new_albums = {str(pid): (entry.get("token"), entry["sha1"],
                             1 if entry.get("fallback") else 0)
                  for pid, entry in albums.items()}
    item_diff = _mapping_diff(old_items, new_items)
    album_diff = _mapping_diff(old_albums, new_albums)
    old_files = load_file_records(conn)
    file_diff = {
        "new": len(set(files) - set(old_files)),
        "removed": len(set(old_files) - set(files)),
    }
    now = time.time()
    file_rows = []
    for sha1, entry in files.items():
        file_rows.append({
            "sha1": sha1,
            "rel": entry.get("rel"),
            "file": entry["file"],
            "thumb": entry.get("thumb"),
            "mime": entry.get("mime") or "application/octet-stream",
            "bytes": int(entry.get("bytes") or 0),
            "thumb_bytes": int(entry.get("thumb_bytes") or 0),
            "updated_at": entry.get("updated_at") or now,
        })
    item_rows = [(pid, entry.get("token"), entry["sha1"],
                  1 if entry.get("fallback") else 0)
                 for pid, entry in items.items()]
    album_rows = [(pid, entry.get("token"), entry["sha1"],
                   1 if entry.get("fallback") else 0)
                  for pid, entry in albums.items()]

    conn.execute("BEGIN IMMEDIATE")
    try:
        if file_rows:
            conn.executemany(
                """INSERT INTO artwork_files
                   (sha1, rel, file, thumb, mime, bytes, thumb_bytes,
                    updated_at)
                   VALUES(:sha1, :rel, :file, :thumb, :mime, :bytes,
                          :thumb_bytes, :updated_at)
                   ON CONFLICT(sha1) DO UPDATE SET
                     rel=excluded.rel, file=excluded.file,
                     thumb=excluded.thumb, mime=excluded.mime,
                     bytes=excluded.bytes, thumb_bytes=excluded.thumb_bytes,
                     updated_at=excluded.updated_at""",
                file_rows)
        conn.execute("DELETE FROM artwork_items")
        if item_rows:
            conn.executemany(
                "INSERT INTO artwork_items(pid, token, sha1, fallback) "
                "VALUES(?, ?, ?, ?)", item_rows)
        conn.execute("DELETE FROM artwork_albums")
        if album_rows:
            conn.executemany(
                "INSERT INTO artwork_albums(pid, token, sha1, fallback) "
                "VALUES(?, ?, ?, ?)", album_rows)
        conn.execute(
            """DELETE FROM artwork_files
               WHERE sha1 NOT IN (
                   SELECT sha1 FROM artwork_items
                   UNION
                   SELECT sha1 FROM artwork_albums)""")
        set_meta(conn, "artwork_updated_at", time.time())
        if source is not None:
            set_meta(conn, "artwork_source", source)
        stats = {
            "items": item_diff,
            "albums": album_diff,
            "files": file_diff,
            "item_count": len(item_rows),
            "album_count": len(album_rows),
            "file_count": len(file_rows),
        }
        set_meta(conn, "artwork_stats", stats)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return stats


def artwork_file_count(conn):
    return int(conn.execute(
        "SELECT COUNT(*) FROM artwork_files").fetchone()[0])


def load_artwork(conn):
    """Return item/album mappings in the JSON-API shape used by web_app."""
    def rows(table):
        query = f"""
            SELECT m.pid, m.token, m.fallback,
                   f.file, f.thumb, f.mime
            FROM {table} m
            JOIN artwork_files f ON f.sha1 = m.sha1
        """
        return {
            row["pid"]: {
                "token": row["token"],
                "file": row["file"],
                "thumb": row["thumb"],
                "mime": row["mime"],
                "fallback": bool(row["fallback"]),
            }
            for row in conn.execute(query)
        }

    return rows("artwork_items"), rows("artwork_albums")
