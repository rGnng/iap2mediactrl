#!/usr/bin/env python3
"""Export artwork referenced by MediaLibrary.sqlitedb through AFC.

The canonical output is the shared SQLite cache (`media-cache.db`) plus the
content-addressed image files.  An optional JSON export is available for
debugging.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import media_cache
from media_db_import import (
    BASE_DIR,
    SNAPSHOT_NAME,
    ensure_paired,
    find_device,
    find_media_db,
    mount_afc,
    progress_line,
    read_library_uuid,
    snapshot_db,
    snapshot_db_afc,
    unmount_afc,
)

SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_ARTWORK_DIR = BASE_DIR / "media-artwork"
DEFAULT_CACHE_DB = BASE_DIR / "media-cache.db"
DEFAULT_SNAPSHOT_DIR = BASE_DIR / "artifacts" / "media-db-snapshot"
DEFAULT_DB = DEFAULT_SNAPSHOT_DIR / SNAPSHOT_NAME


def image_kind(path: Path):
    try:
        with path.open("rb") as fh:
            head = fh.read(32)
    except OSError:
        return "bin", "application/octet-stream"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "gif", "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp", "image/webp"
    if head[:4] == b"\x00\x00\x01\x00":
        return "ico", "image/x-icon"
    return "bin", "application/octet-stream"


def read_mapping(db: Path):
    """Read item/album artwork references from the device database."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        """SELECT b.entity_pid AS pid, b.available_artwork_token AS token,
                  a.relative_path AS rel, b.artwork_type AS atype
           FROM best_artwork_token b
           JOIN artwork a ON a.artwork_token = b.available_artwork_token
           WHERE b.entity_type = 0
             AND b.artwork_type = 1
             AND b.available_artwork_token IS NOT NULL
             AND b.available_artwork_token != ''"""
    ))
    item_art = {}
    for r in rows:
        item_art[int(r["pid"])] = {
            "token": r["token"], "rel": r["rel"], "fallback": False,
        }

    items = {int(pid): (int(album) if album is not None else None)
             for pid, album in conn.execute(
                 "SELECT item_pid, album_pid FROM item")}
    albums = {int(ap): (int(rp) if rp is not None else None)
              for ap, rp in conn.execute(
                  "SELECT album_pid, representative_item_pid FROM album")}

    # Album cover: representative item first, then any item in that album.
    album_art = {}
    for ap, rp in albums.items():
        if rp is not None and rp in item_art:
            album_art[ap] = item_art[rp]
            continue
        hit = next((p for p, a in items.items()
                    if a == ap and p in item_art), None)
        if hit is not None:
            album_art[ap] = item_art[hit]

    # Item fallback: use its album's cover when the item has no own artwork.
    for pid, ap in items.items():
        if pid in item_art:
            continue
        if ap is not None and ap in album_art:
            item_art[pid] = {**album_art[ap], "fallback": True}

    return item_art, album_art, items, albums, conn


def sha1_rel(rel: str) -> str:
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()


def pid_hex(value) -> str:
    # MediaLibrary stores signed 64-bit PIDs; iAP2 uses unsigned hex.
    return f"{int(value) & 0xffffffffffffffff:016x}"


def local_path(path_text):
    if not path_text:
        return None
    path = Path(path_text)
    return path if path.is_absolute() else BASE_DIR / path


def public_path(path: Path) -> str:
    try:
        return os.path.relpath(str(path), str(BASE_DIR))
    except ValueError:
        return str(path)


def usable_file(path_text) -> Path | None:
    path = local_path(path_text)
    if path is None:
        return None
    try:
        if path.is_file() and path.stat().st_size > 0:
            return path
    except OSError:
        return None
    return None


def existing_artwork(artwork_dir: Path):
    """Map SHA1 -> existing top-level artwork file."""
    found = {}
    if not artwork_dir.is_dir():
        return found
    for path in artwork_dir.iterdir():
        if not path.is_file():
            continue
        stem = path.stem.lower()
        if SHA1_RE.match(stem):
            found[stem] = path
    return found


def make_thumb(src: Path, thumb: Path) -> bool:
    if thumb.exists() and thumb.stat().st_size > 0:
        return True
    try:
        from PIL import Image, ImageOps
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((160, 160))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(thumb, "JPEG", quality=82, optimize=True)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"thumbnail {src}: {exc}", file=sys.stderr, flush=True)
        return False


def file_record(sha: str, rel: str, path: Path, thumb: Path | None,
                mime: str):
    thumb_bytes = 0
    if thumb is not None and thumb.exists():
        try:
            thumb_bytes = thumb.stat().st_size
        except OSError:
            thumb = None
    return {
        "sha1": sha,
        "rel": rel,
        "file": public_path(path),
        "thumb": public_path(thumb) if thumb is not None else None,
        "mime": mime,
        "bytes": path.stat().st_size if path.exists() else 0,
        "thumb_bytes": thumb_bytes,
        "updated_at": time.time(),
    }


def wipe_artwork(artwork_dir: Path):
    """Delete all local artwork files for a full refresh."""
    if not artwork_dir.is_dir():
        return 0
    removed = 0
    for child in list(artwork_dir.iterdir()):
        try:
            if child.is_dir():
                removed += sum(1 for path in child.rglob("*")
                               if path.is_file())
                shutil.rmtree(child)
            else:
                child.unlink()
                removed += 1
        except OSError as exc:
            print(f"delete {child}: {exc}", file=sys.stderr, flush=True)
    return removed


def garbage_collect(artwork_dir: Path, referenced: set[str]):
    """Remove cached files that are no longer referenced."""
    removed = 0
    for folder in (artwork_dir, artwork_dir / "thumbs"):
        if not folder.is_dir():
            continue
        for path in list(folder.iterdir()):
            if not path.is_file():
                continue
            stem = path.stem.lower()
            stale_sha = SHA1_RE.match(stem) and stem not in referenced
            if stale_sha or path.suffix == ".tmp":
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
    return removed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=None,
                    help="MediaLibrary snapshot (default: refresh from device)")
    ap.add_argument("--mount", action="store_true")
    ap.add_argument("--udid")
    ap.add_argument("--artwork-root", type=Path,
                    help="already mounted iTunes_Control/Artwork directory")
    ap.add_argument("--artwork-dir", type=Path, default=DEFAULT_ARTWORK_DIR)
    ap.add_argument("--cache-db", type=Path, default=DEFAULT_CACHE_DB)
    ap.add_argument("--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT_DIR)
    ap.add_argument("--mode", choices=("quick", "full"), default="quick",
                    help="quick reuses existing files; full deletes and recopies")
    ap.add_argument("--force", action="store_true",
                    help="legacy alias for --mode full")
    ap.add_argument("--limit", type=int, default=0,
                    help="limit unique files (testing)")
    ap.add_argument("--progress-json", action="store_true")
    ap.add_argument("--out", type=Path, default=None,
                    help="optional legacy JSON export")
    args = ap.parse_args()
    if args.force:
        args.mode = "full"

    def progress(**fields):
        progress_line(args.progress_json, **fields)

    started = time.monotonic()
    mnt = None
    conn = None
    root = args.artwork_root
    db = args.db
    try:
        if args.mount:
            progress(stage="wait_usbmuxd", message="Looking for iOS device")
            udid = args.udid or find_device()
            if not udid:
                raise RuntimeError("no iOS device found (is usbmuxd running?)")
            progress(stage="pair_check", message="Checking pairing",
                     udid=udid)
            if not ensure_paired(udid):
                progress(stage="pair_required",
                         message="Device is not paired; run Pair / Trust")
                return 2
            progress(stage="mount", message="Mounting AFC")
            mnt = mount_afc(udid)
            root = mnt / "iTunes_Control" / "iTunes" / "Artwork" / "Originals"
            if db is None:
                progress(stage="db_find",
                         message="Locating MediaLibrary database")
                found = find_media_db(mnt)
                if not found:
                    raise RuntimeError("MediaLibrary.sqlitedb not found on mount")
                remote = "/" + str(found.relative_to(mnt))
                if shutil.which("afcclient"):
                    try:
                        db = snapshot_db_afc(
                            udid, remote, args.snapshot_dir,
                            progress=progress)
                    except Exception as exc:  # noqa: BLE001
                        print(f"AFC snapshot failed ({exc}); using ifuse "
                              f"fallback", flush=True)
                        db = snapshot_db(
                            found, args.snapshot_dir, progress=progress)
                else:
                    db = snapshot_db(
                        found, args.snapshot_dir, progress=progress)
        if db is None:
            db = DEFAULT_DB
        if root is None:
            raise RuntimeError("use --mount or --artwork-root")
        if not root.is_dir():
            raise RuntimeError(f"artwork directory not found: {root}")
        if not db.exists():
            raise RuntimeError(f"database not found: {db}")
        library_id = read_library_uuid(db)

        progress(stage="artwork_scan", message="Reading artwork mapping",
                 stats={"library_id": library_id})
        t = time.monotonic()
        item_art, album_art, items, albums, db_conn = read_mapping(db)
        db_conn.close()
        db_seconds = time.monotonic() - t

        args.artwork_dir.mkdir(parents=True, exist_ok=True)
        thumb_dir = args.artwork_dir / "thumbs"
        thumb_dir.mkdir(parents=True, exist_ok=True)
        media_cache.ensure_cache(
            args.cache_db,
            legacy_dates_path=BASE_DIR / "media-db-dates.json",
            legacy_artwork_path=BASE_DIR / "media-db-artwork.json",
            base_dir=str(BASE_DIR))
        conn = media_cache.open_db(args.cache_db)
        old_files = media_cache.load_file_records(conn)

        rels = sorted({v["rel"] for v in item_art.values()} |
                      {v["rel"] for v in album_art.values()})
        if args.limit:
            rels = rels[:args.limit]

        deleted = 0
        if args.mode == "full":
            progress(stage="artwork_wipe",
                     message="Deleting local artwork cache")
            deleted = wipe_artwork(args.artwork_dir)
            thumb_dir.mkdir(parents=True, exist_ok=True)
        existing = existing_artwork(args.artwork_dir)

        files = {}
        need_copy = []
        reused = 0
        thumbs = 0
        for rel in rels:
            sha = sha1_rel(rel)
            record = old_files.get(sha)
            path = usable_file(record["file"]) if record else None
            if path is None:
                path = usable_file(existing.get(sha))
            if path is not None:
                thumb = thumb_dir / f"{sha}.jpg"
                had_thumb = thumb.exists()
                made_thumb = make_thumb(path, thumb)
                if made_thumb and not had_thumb:
                    thumbs += 1
                mime = record.get("mime") if record else \
                    image_kind(path)[1]
                # Reuse the original even when the device file is truncated
                # and a thumbnail cannot be generated; the web API falls back
                # to the original image in that case.
                files[sha] = file_record(
                    sha, rel, path, thumb if made_thumb else None, mime)
                reused += 1
                continue
            need_copy.append(rel)

        copy_bytes_total = 0
        for rel in need_copy:
            try:
                copy_bytes_total += (root / rel).stat().st_size
            except OSError:
                pass
        progress(stage="artwork_scan", done=0, total=len(rels),
                 unit="files",
                 message=f"{len(need_copy)} new artwork files",
                 stats={
                     "required_files": len(rels),
                     "reused_files": reused,
                     "copy_files": len(need_copy),
                     "deleted_files": deleted,
                 })

        copied = failed = 0
        bytes_copied = 0
        copy_seconds = 0.0
        t = time.monotonic()
        for done, rel in enumerate(need_copy, 1):
            src = root / rel
            if not src.is_file():
                failed += 1
                print(f"missing artwork {rel}", file=sys.stderr, flush=True)
            else:
                try:
                    sha = sha1_rel(rel)
                    tmp = args.artwork_dir / (sha + ".tmp")
                    shutil.copyfile(src, tmp)
                    ext, mime = image_kind(tmp)
                    dst = args.artwork_dir / f"{sha}.{ext}"
                    os.replace(tmp, dst)
                    copied += 1
                    bytes_copied += dst.stat().st_size
                    thumb = thumb_dir / f"{sha}.jpg"
                    had_thumb = thumb.exists()
                    if make_thumb(dst, thumb) and not had_thumb:
                        thumbs += 1
                    files[sha] = file_record(sha, rel, dst, thumb, mime)
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    print(f"artwork {rel}: {exc}", file=sys.stderr, flush=True)
            if done % 5 == 0 or done == len(need_copy):
                progress(stage="artwork_copy", done=done,
                         total=len(need_copy), unit="files",
                         bytes_done=bytes_copied,
                         bytes_total=copy_bytes_total,
                         stats={
                             "required_files": len(rels),
                             "reused_files": reused,
                             "copy_files": len(need_copy),
                             "copied": copied,
                             "failed": failed,
                             "thumbs": thumbs,
                         })
        copy_seconds = time.monotonic() - t

        def mapping(source):
            out = {}
            for pid, entry in source.items():
                sha = sha1_rel(entry["rel"])
                if sha not in files:
                    continue
                out[pid_hex(pid)] = {
                    "token": entry.get("token"),
                    "sha1": sha,
                    "fallback": bool(entry.get("fallback")),
                }
            return out

        item_out = mapping(item_art)
        album_out = mapping(album_art)
        progress(stage="cache_write", message="Updating SQLite cache",
                 stats={"items": len(item_out), "albums": len(album_out),
                        "files": len(files)})
        cache_stats = media_cache.replace_artwork(
            conn, item_out, album_out, files,
            source={
                "database": str(db),
                "library_id": library_id,
                "artwork_root": str(root),
                "artwork_dir": str(args.artwork_dir),
                "mode": args.mode,
            })
        removed = garbage_collect(args.artwork_dir, set(files))

        elapsed = time.monotonic() - started
        stats = {
            "mode": args.mode,
            "library_id": library_id,
            "db_items": len(items),
            "db_albums": len(albums),
            "required_files": len(rels),
            "copy_files": len(need_copy),
            "reused_files": reused,
            "copied": copied,
            "failed": failed,
            "thumbs": thumbs,
            "deleted_files": deleted,
            "removed_files": removed,
            "bytes_copied": bytes_copied,
            "new_items": cache_stats["items"]["new"],
            "changed_items": cache_stats["items"]["updated"],
            "removed_items": cache_stats["items"]["removed"],
            "new_albums": cache_stats["albums"]["new"],
            "new_files": cache_stats["files"]["new"],
            "cache": cache_stats,
            "db_seconds": round(db_seconds, 3),
            "copy_seconds": round(copy_seconds, 3),
            "total_seconds": round(elapsed, 3),
        }
        progress(stage="done", done=len(rels), total=len(rels),
                 unit="files", message="Artwork cache updated",
                 stats=stats)

        if args.out:
            exported_items, exported_albums = media_cache.load_artwork(conn)
            result = {
                "ok": True,
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime()),
                "source": {
                    "database": str(db),
                    "artwork_root": str(root),
                    "artwork_dir": str(args.artwork_dir),
                },
                "stats": stats,
                "items": exported_items,
                "albums": exported_albums,
            }
            args.out.parent.mkdir(parents=True, exist_ok=True)
            tmp = args.out.with_suffix(args.out.suffix + ".tmp")
            tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, args.out)
            print(f"wrote {args.out}", flush=True)

        if not args.progress_json:
            print(json.dumps(stats, ensure_ascii=False, indent=2), flush=True)
        return 0
    finally:
        if conn is not None:
            conn.close()
        if mnt is not None:
            unmount_afc(mnt)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
