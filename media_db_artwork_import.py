#!/usr/bin/env python3
"""Export artwork referenced by MediaLibrary.sqlitedb through AFC/ifuse.

Separate from the iAP2/HID service.  It writes:
  media-artwork/<sha1>.<ext>   image files
  media-db-artwork.json        item/album -> local artwork mapping
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

APPLE_EPOCH = 978307200
DEFAULT_DB = "artifacts/media-db-snapshot/media-db-backup.sqlitedb"


def run(cmd, timeout=30):
    print("+ " + " ".join(str(x) for x in cmd), flush=True)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.stdout.strip():
        print(p.stdout.strip(), flush=True)
    if p.stderr.strip():
        print(p.stderr.strip(), file=sys.stderr, flush=True)
    return p


def find_device():
    p = run(["idevice_id", "-l"], timeout=10)
    ids = [x.strip() for x in p.stdout.splitlines() if x.strip()]
    return ids[0] if ids else None


def ensure_paired(udid=None):
    cmd = ["idevicepair"]
    if udid:
        cmd += ["-u", udid]
    cmd += ["validate"]
    p = run(cmd, timeout=10)
    if p.returncode == 0:
        return True
    print("Device is not paired; accept the Trust dialog on the iPod.",
          file=sys.stderr)
    return False


def mount_afc(udid=None):
    mnt = Path(tempfile.mkdtemp(prefix="ipod-afc-"))
    cmd = ["ifuse", str(mnt)]
    if udid:
        cmd[1:1] = ["--udid", udid]
    p = run(cmd, timeout=30)
    if p.returncode != 0:
        shutil.rmtree(mnt, ignore_errors=True)
        raise RuntimeError("ifuse mount failed")
    return mnt


def unmount_afc(mnt: Path):
    for cmd in (["fusermount3", "-u", str(mnt)], ["umount", str(mnt)]):
        try:
            if run(cmd, timeout=15).returncode == 0:
                break
        except Exception:
            pass
    try:
        mnt.rmdir()
    except OSError:
        pass


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
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # Direct item-level artwork.
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path(DEFAULT_DB))
    ap.add_argument("--mount", action="store_true")
    ap.add_argument("--udid")
    ap.add_argument("--artwork-root", type=Path,
                    help="already mounted iTunes_Control/Artwork directory")
    ap.add_argument("--artwork-dir", type=Path, default=Path("media-artwork"))
    ap.add_argument("--out", type=Path, default=Path("media-db-artwork.json"))
    ap.add_argument("--limit", type=int, default=0,
                    help="limit unique files (testing)")
    ap.add_argument("--force", action="store_true",
                    help="re-copy existing artwork files")
    args = ap.parse_args()

    started = time.monotonic()
    mnt = None
    mount_seconds = 0.0
    root = args.artwork_root
    try:
        if args.mount:
            udid = args.udid or find_device()
            if not udid:
                raise RuntimeError("no iOS device found (is usbmuxd running?)")
            if not ensure_paired(udid):
                return 2
            t = time.monotonic()
            mnt = mount_afc(udid)
            mount_seconds = time.monotonic() - t
            root = mnt / "iTunes_Control" / "iTunes" / "Artwork" / "Originals"
        if root is None:
            raise RuntimeError("use --mount or --artwork-root")
        if not root.is_dir():
            raise RuntimeError(f"artwork directory not found: {root}")
        if not args.db.exists():
            raise RuntimeError(f"database not found: {args.db}")

        t = time.monotonic()
        item_art, album_art, items, albums, conn = read_mapping(args.db)
        conn.close()
        db_seconds = time.monotonic() - t

        args.artwork_dir.mkdir(parents=True, exist_ok=True)
        thumb_dir = args.artwork_dir / "thumbs"
        thumb_dir.mkdir(parents=True, exist_ok=True)
        # Unique source paths, preserving art for item and album maps.
        rels = sorted({v["rel"] for v in item_art.values()} |
                      {v["rel"] for v in album_art.values()})
        if args.limit:
            rels = rels[:args.limit]
        rel_to_file = {}
        copied = skipped = failed = 0
        total_bytes = 0
        thumb_bytes = 0
        thumbs = 0
        copy_seconds = 0.0
        t = time.monotonic()
        for i, rel in enumerate(rels, 1):
            src = root / rel
            if not src.is_file():
                failed += 1
                print(f"missing artwork {rel}", file=sys.stderr, flush=True)
                continue
            try:
                sha = hashlib.sha1(rel.encode("utf-8")).hexdigest()
                # Detect extension before the final name.
                tmp = args.artwork_dir / (sha + ".tmp")
                with src.open("rb") as fsrc, tmp.open("wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst, 1024 * 1024)
                ext, mime = image_kind(tmp)
                dst = args.artwork_dir / f"{sha}.{ext}"
                if dst.exists() and not args.force:
                    tmp.unlink(missing_ok=True)
                    skipped += 1
                else:
                    os.replace(tmp, dst)
                    copied += 1
                    total_bytes += dst.stat().st_size
                thumb = thumb_dir / f"{sha}.jpg"
                if not thumb.exists():
                    try:
                        from PIL import Image, ImageOps
                        with Image.open(dst) as im:
                            im = ImageOps.exif_transpose(im)
                            im.thumbnail((160, 160))
                            if im.mode not in ("RGB", "L"):
                                im = im.convert("RGB")
                            im.save(thumb, "JPEG", quality=82, optimize=True)
                        thumbs += 1
                        thumb_bytes += thumb.stat().st_size
                    except Exception as exc:
                        print(f"thumbnail {rel}: {exc}", file=sys.stderr,
                              flush=True)
                rel_to_file[rel] = {
                    "file": f"{args.artwork_dir.name}/{dst.name}",
                    "thumb": (f"{args.artwork_dir.name}/thumbs/{thumb.name}"
                              if thumb.exists() else None),
                    "mime": mime,
                }
                if i % 250 == 0:
                    print(f"  artwork {i}/{len(rels)}", flush=True)
            except Exception as exc:
                failed += 1
                print(f"artwork {rel}: {exc}", file=sys.stderr, flush=True)
        copy_seconds = time.monotonic() - t

        def public(entry):
            if not entry:
                return None
            f = rel_to_file.get(entry["rel"])
            if not f:
                return None
            return {
                "token": entry["token"],
                "file": f["file"],
                "thumb": f.get("thumb"),
                "mime": f["mime"],
                "fallback": entry.get("fallback", False),
            }

        def pid_hex(value):
            # MediaLibrary stores signed 64-bit PIDs; iAP2 uses unsigned hex.
            return f"{int(value) & 0xffffffffffffffff:016x}"

        item_out = {}
        for pid, entry in item_art.items():
            pub = public(entry)
            if pub:
                item_out[pid_hex(pid)] = pub
        album_out = {}
        for ap, entry in album_art.items():
            pub = public(entry)
            if pub:
                album_out[pid_hex(ap)] = pub
        elapsed = time.monotonic() - started
        result = {
            "ok": True,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": {
                "database": str(args.db),
                "artwork_root": str(root),
                "artwork_dir": str(args.artwork_dir),
            },
            "stats": {
                "db_items": len(items),
                "db_albums": len(albums),
                "item_art": len(item_out),
                "album_art": len(album_out),
                "unique_source_paths": len(rels),
                "copied": copied,
                "skipped": skipped,
                "failed": failed,
                "original_bytes": total_bytes,
                "thumbnails": thumbs,
                "thumb_bytes": thumb_bytes,
                "bytes_copied": total_bytes + thumb_bytes,
                "mount_seconds": round(mount_seconds, 3),
                "db_seconds": round(db_seconds, 3),
                "copy_seconds": round(copy_seconds, 3),
                "total_seconds": round(elapsed, 3),
            },
            "items": item_out,
            "albums": album_out,
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.out.with_suffix(args.out.suffix + ".tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, args.out)
        print(json.dumps(result["stats"], ensure_ascii=False, indent=2))
        print(f"wrote {args.out}", flush=True)
        return 0
    finally:
        if mnt is not None:
            unmount_afc(mnt)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
