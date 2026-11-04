#!/usr/bin/env python3
"""Import the iPod/iPhone MediaLibrary SQLite database via AFC.

This program is intentionally separate from the iAP2/HID service.  It writes
the normalized date and artwork cache in ``media-cache.db`` for the web UI.

Examples:
    sudo python3 media_db_import.py --mount --progress-json
    python3 media_db_import.py --db /path/to/MediaLibrary.sqlitedb
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import media_cache

APPLE_EPOCH = 978307200  # 2001-01-01 UTC
DEFAULT_DB = "MediaLibrary.sqlitedb"
SNAPSHOT_NAME = "media-db-backup.sqlitedb"
BASE_DIR = Path(__file__).resolve().parent
DATE_RE = re.compile(r"(date.*add|add.*date|date.*creat|creat.*date|added)", re.I)
PID_RE = re.compile(r"(item_pid|itempersistent|persistent.*id)", re.I)


def progress_line(enabled, **fields):
    if enabled:
        print("PROGRESS " + json.dumps(fields, ensure_ascii=False),
              flush=True)


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
    print(
        "Device is not paired. Accept the Trust dialog on the iPod and run:\n"
        "  idevicepair pair\n",
        file=sys.stderr,
    )
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
            p = run(cmd, timeout=15)
            if p.returncode == 0:
                break
        except Exception:
            pass
    try:
        mnt.rmdir()
    except OSError:
        pass


def find_media_db(root: Path):
    candidates = [
        root / "iTunes_Control" / "iTunes" / DEFAULT_DB,
        root / "iTunes_Control" / "iTunes" / "Library.itdb",
    ]
    for c in candidates:
        if c.exists():
            return c
    print("Searching for SQLite media databases under mount...", flush=True)
    hits = []
    for pattern in ("MediaLibrary.sqlitedb", "*.itdb", "*.sqlitedb"):
        hits.extend(root.rglob(pattern))
    # Avoid app/photo databases.
    hits = [p for p in hits if "iTunes_Control" in str(p)]
    return hits[0] if hits else None


def _db_signature(db: Path):
    signature = []
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(db) + suffix)
        try:
            stat = path.stat()
        except OSError:
            continue
        signature.append((suffix, stat.st_size, stat.st_mtime_ns))
    return tuple(signature)


def _wait_for_stable_db(db: Path, timeout=20.0, stable_for=2.0,
                        progress=None):
    """Wait until the device has stopped writing the database and WAL."""
    started = time.monotonic()
    last = None
    last_change = started
    while time.monotonic() - started < timeout:
        sig = _db_signature(db)
        now = time.monotonic()
        if sig != last:
            last = sig
            last_change = now
        elif now - last_change >= stable_for:
            return True
        if progress:
            progress(stage="db_wait",
                     message="Waiting for MediaLibrary database")
        time.sleep(0.4)
    return False


def _copy_with_progress(src: Path, dst: Path, done_base: int, all_bytes: int,
                        progress=None):
    copied = 0
    # 128 KiB reads are reliable through ifuse/AFC.  Larger reads can return
    # stale pages and produce a snapshot that fails SQLite's integrity check.
    with src.open("rb") as fsrc, dst.open("wb") as fdst:
        while True:
            chunk = fsrc.read(128 * 1024)
            if not chunk:
                break
            fdst.write(chunk)
            copied += len(chunk)
            if progress:
                progress(stage="db_copy", done=done_base + copied,
                         total=all_bytes, unit="bytes",
                         bytes_done=done_base + copied,
                         bytes_total=all_bytes)
    return copied


def _build_snapshot(sources, db_name, dst_tmp, progress):
    """Copy source files locally, checkpoint them, and build dst_tmp."""
    total_bytes = sum(p.stat().st_size for p, _ in sources)
    if progress:
        progress(stage="db_copy", done=0, total=total_bytes,
                 unit="bytes", bytes_done=0, bytes_total=total_bytes)
    tmp_dir = Path(tempfile.mkdtemp(prefix="media-db-copy-"))
    try:
        copied = 0
        for src, name_in_tmp in sources:
            target = tmp_dir / name_in_tmp
            copied += _copy_with_progress(
                src, target, copied, total_bytes, progress=progress)
            print(f"copied {src} -> {target}", flush=True)

        local = sqlite3.connect(str(tmp_dir / db_name))
        try:
            local.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            out = sqlite3.connect(str(dst_tmp))
            try:
                local.backup(out)
                out.execute("PRAGMA journal_mode=DELETE")
            finally:
                out.close()
        finally:
            local.close()

        check = sqlite3.connect(f"file:{dst_tmp}?mode=ro", uri=True)
        try:
            integrity = check.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            check.close()
        if integrity != "ok":
            first = integrity.splitlines()[0] if integrity else "unknown"
            return False, RuntimeError(
                f"snapshot integrity check failed: {first}")
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, exc
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def snapshot_db(db: Path, out_dir: Path, progress=None,
                name=SNAPSHOT_NAME):
    """Create a consistent local snapshot of the device database.

    The device database is in WAL mode.  A SQLite backup straight through
    ifuse/FUSE can be malformed while the device is writing.  Copy the
    database (and WAL when possible) locally, checkpoint the local copy, and
    build the final snapshot from local files.  If the WAL copy is transiently
    inconsistent, fall back to the main database file, which is also a valid
    (slightly older) snapshot.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / name
    if db.resolve() == dst.resolve():
        return dst
    dst_tmp = out_dir / (name + ".new")

    def clean_tmp():
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(dst_tmp) + suffix).unlink()
            except OSError:
                pass

    clean_tmp()
    _wait_for_stable_db(db, timeout=30, stable_for=3.0, progress=progress)
    last_error = None
    for attempt in range(3):
        wal = Path(str(db) + "-wal")
        variants = []
        if wal.exists():
            variants.append(("wal", [(db, db.name),
                                     (wal, db.name + "-wal")]))
        variants.append(("database", [(db, db.name)]))
        for variant, sources in variants:
            ok, error = _build_snapshot(
                sources, db.name, dst_tmp, progress)
            if ok:
                for suffix in ("-wal", "-shm"):
                    try:
                        Path(str(dst) + suffix).unlink()
                    except OSError:
                        pass
                os.replace(dst_tmp, dst)
                print(f"sqlite snapshot {db} -> {dst} ({variant})",
                      flush=True)
                return dst
            last_error = error
            print(f"snapshot {variant} attempt {attempt + 1} failed: "
                  f"{error}", flush=True)
            clean_tmp()

        if attempt < 2:
            _wait_for_stable_db(db, timeout=15, stable_for=3.0,
                                progress=progress)
            time.sleep(2.0)

    raise RuntimeError(f"cannot create a consistent database snapshot: "
                       f"{last_error}")


def _afc_info(udid, remote):
    try:
        proc = subprocess.run(
            ["afcclient", "-u", udid, "info", remote],
            capture_output=True, text=True, timeout=20,
            stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return int(json.loads(proc.stdout).get("st_size") or 0)
    except (TypeError, ValueError):
        return None


def _afc_get(udid, remote, local: Path, done_base: int, total_bytes: int,
             progress=None):
    local.parent.mkdir(parents=True, exist_ok=True)
    try:
        local.unlink()
    except OSError:
        pass
    try:
        proc = subprocess.Popen(
            ["afcclient", "-u", udid, "get", remote, str(local)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise RuntimeError(f"cannot start afcclient: {exc}")
    while proc.poll() is None:
        try:
            size = local.stat().st_size
        except OSError:
            size = 0
        if progress:
            progress(stage="db_copy", done=done_base + size,
                     total=total_bytes, unit="bytes",
                     bytes_done=done_base + size,
                     bytes_total=total_bytes)
        time.sleep(0.2)
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"afcclient get failed: {remote} (exit {rc})")
    size = local.stat().st_size
    if progress:
        progress(stage="db_copy", done=done_base + size,
                 total=total_bytes, unit="bytes",
                 bytes_done=done_base + size, bytes_total=total_bytes)
    return size


def snapshot_db_afc(udid, remote_db, out_dir, progress=None,
                    name=SNAPSHOT_NAME):
    """Pull the database over AFC directly, then build a local snapshot.

    afcclient avoids the FUSE page cache issue that can make a large SQLite
    file copy inconsistent.  The pulled files are checkpointed and validated
    locally, with a main-database-only fallback if the WAL is transient.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / name
    tmp_dir = Path(tempfile.mkdtemp(prefix="media-db-afc-"))
    try:
        db_size = _afc_info(udid, remote_db)
        if db_size is None:
            raise RuntimeError("cannot stat MediaLibrary database over AFC")
        remote_wal = remote_db + "-wal"
        wal_size = _afc_info(udid, remote_wal) or 0
        total_bytes = db_size + wal_size
        if progress:
            progress(stage="db_copy", done=0, total=total_bytes,
                     unit="bytes", bytes_done=0, bytes_total=total_bytes)

        local_db = tmp_dir / Path(remote_db).name
        done = _afc_get(udid, remote_db, local_db, 0, total_bytes,
                        progress=progress)
        local_wal = None
        if wal_size:
            local_wal = tmp_dir / (local_db.name + "-wal")
            try:
                done += _afc_get(udid, remote_wal, local_wal, done,
                                 total_bytes, progress=progress)
            except Exception as exc:  # noqa: BLE001
                print(f"WAL pull skipped: {exc}", flush=True)
                local_wal = None

        last_error = None
        variants = []
        if local_wal is not None and local_wal.exists():
            variants.append(("wal", [(local_db, local_db.name),
                                     (local_wal, local_db.name + "-wal")]))
        variants.append(("database", [(local_db, local_db.name)]))
        dst_tmp = out_dir / (name + ".new")
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(dst_tmp) + suffix).unlink()
            except OSError:
                pass
        for variant, sources in variants:
            ok, error = _build_snapshot(
                sources, local_db.name, dst_tmp, progress)
            if ok:
                for suffix in ("-wal", "-shm"):
                    try:
                        Path(str(dst) + suffix).unlink()
                    except OSError:
                        pass
                os.replace(dst_tmp, dst)
                print(f"sqlite snapshot {remote_db} -> {dst} ({variant})",
                      flush=True)
                return dst
            last_error = error
            print(f"afc snapshot {variant} failed: {error}", flush=True)
            for suffix in ("", "-wal", "-shm"):
                try:
                    Path(str(dst_tmp) + suffix).unlink()
                except OSError:
                    pass
        raise RuntimeError(
            f"cannot create a consistent AFC snapshot: {last_error}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def read_library_uuid(db: Path):
    """Read the iAP2 media-library UUID from the AFC database."""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
        try:
            row = conn.execute(
                "SELECT value FROM _MLDatabaseProperties WHERE key='_UUID'"
            ).fetchone()
            return str(row[0]).strip().upper() if row and row[0] else None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


def table_columns(conn, table):
    return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]


def all_tables(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]


def find_candidate(conn):
    tables = all_tables(conn)
    candidates = []
    for table in tables:
        cols = table_columns(conn, table)
        date_cols = [c for c in cols if DATE_RE.search(c)]
        pid_cols = [c for c in cols if PID_RE.search(c)]
        if date_cols:
            candidates.append({
                "table": table,
                "date_cols": date_cols,
                "pid_cols": pid_cols,
                "columns": cols,
            })
    return tables, candidates


def parse_epoch(value):
    """Return (epoch_seconds, format) or (None, reason)."""
    if value is None:
        return None, "null"
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None, "empty"
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
            return d.timestamp(), "iso8601"
        except ValueError:
            pass
        try:
            value = float(s)
        except ValueError:
            return None, "text"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None, "non-numeric"
    if v <= 0:
        return None, "zero"
    # Common encodings: Unix seconds/ms/us/ns, Apple/Core Data seconds.
    if v > 1e16:  # nanoseconds since Unix
        return v / 1e9, "unix_ns"
    if v > 1e13:  # microseconds since Unix
        return v / 1e6, "unix_us"
    if v > 1e11:  # milliseconds since Unix
        return v / 1e3, "unix_ms"
    if 300_000_000 <= v < 1_200_000_000:
        # Apple/Core Data epoch 2001-01-01 (plausible 2010-2039)
        return v + APPLE_EPOCH, "apple_epoch"
    return v, "unix_seconds"


def inspect_and_extract(db_path: Path, library_path: Path | None,
                        progress=None):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    tables, candidates = find_candidate(conn)
    print("\nTables:", ", ".join(tables), flush=True)
    print("\nDate-column candidates:", flush=True)
    for c in candidates:
        print(
            f"  {c['table']}: dates={c['date_cols']} pids={c['pid_cols']}",
            flush=True,
        )
    # Prefer item/item_extra rows with a PID.
    candidates.sort(key=lambda c: (
        0 if c["pid_cols"] else 1,
        0 if c["table"] == "item" else 1,
        len(c["table"]),
    ))
    if not candidates:
        print("No date-like columns found. Dump schema follows:", flush=True)
        for table in tables:
            print(f"\n[{table}]", table_columns(conn, table), flush=True)
        return {"ok": False, "error": "no date-like column", "tables": tables}

    # Try candidates until we get plausible dates.
    selected = None
    rows = []
    reasons = []
    for c in candidates:
        date_col = c["date_cols"][0]
        pid_col = c["pid_cols"][0] if c["pid_cols"] else None
        if not pid_col:
            reasons.append(f"{c['table']}: no PID column")
            continue
        query = (
            f'SELECT "{pid_col}" AS pid, "{date_col}" AS raw_date '
            f'FROM "{c["table"]}" '
            f'WHERE "{date_col}" IS NOT NULL AND "{date_col}" != 0 '
            f'ORDER BY "{date_col}" DESC LIMIT 100000'
        )
        try:
            candidate_rows = list(conn.execute(query))
        except Exception as exc:
            reasons.append(f"{c['table']}: {exc}")
            continue
        parsed = []
        formats = {}
        for r in candidate_rows:
            ts, fmt = parse_epoch(r["raw_date"])
            if ts is not None:
                parsed.append((r["pid"], ts))
                formats[fmt] = formats.get(fmt, 0) + 1
        # Keep dates in a sane range: 2000-01-01 .. 2100-01-01.
        parsed = [(pid, ts) for pid, ts in parsed
                  if 946684800 <= ts <= 4102444800]
        if parsed:
            selected = {**c, "date_col": date_col, "pid_col": pid_col}
            rows = parsed
            selected["formats"] = formats
            break
        reasons.append(f"{c['table']}: no plausible parsed dates")

    if selected is None:
        print("No usable PID/date pair found: " + "; ".join(reasons), flush=True)
        return {"ok": False, "error": "no usable pid/date pair",
                "candidates": candidates, "tables": tables}

    dates = {}
    for pid, ts in rows:
        try:
            key = f"{int(pid):016x}"
        except (TypeError, ValueError):
            key = str(pid)
        dates[key] = ts

    matched = 0
    library_count = 0
    if library_path and library_path.exists():
        data = json.loads(library_path.read_text(encoding="utf-8"))
        items = data.get("items", {})
        library_count = len(items)
        matched = sum(1 for pid in dates if pid in items)

    if progress:
        progress(stage="db_parse", done=len(rows), total=len(rows),
                 unit="rows",
                 stats={"rows": len(rows), "unique_pids": len(dates),
                        "library_items": library_count,
                        "matched_library_items": matched})

    report = {
        "ok": True,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": {
            "database": str(db_path),
            "table": selected["table"],
            "pid_column": selected["pid_col"],
            "date_column": selected["date_col"],
            "formats": selected.get("formats", {}),
        },
        "stats": {
            "rows": len(rows),
            "unique_pids": len(dates),
            "library_items": library_count,
            "matched_library_items": matched,
        },
        "dates": dates,
    }
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mount", action="store_true",
                    help="connect to the device and refresh the DB snapshot")
    ap.add_argument("--db", type=Path, help="use an existing DB copy")
    ap.add_argument("--udid", help="device UDID for ifuse/idevicepair")
    ap.add_argument("--library", type=Path,
                    default=BASE_DIR / "media-library.json",
                    help="current media-library.json for match statistics")
    ap.add_argument("--cache-db", type=Path,
                    default=BASE_DIR / "media-cache.db",
                    help="SQLite cache written by the importer")
    ap.add_argument("--out", type=Path, default=None,
                    help="optional legacy JSON export")
    ap.add_argument("--snapshot-dir", type=Path,
                    default=BASE_DIR / "artifacts" / "media-db-snapshot")
    ap.add_argument("--progress-json", action="store_true",
                    help="print machine-readable PROGRESS lines")
    args = ap.parse_args()

    def progress(**fields):
        progress_line(args.progress_json, **fields)

    mnt = None
    udid = args.udid
    remote_db = None
    db = args.db
    try:
        if args.mount or db is None:
            progress(stage="wait_usbmuxd", message="Looking for iOS device")
            udid = udid or find_device()
            if not udid:
                raise RuntimeError("no iOS device found (is usbmuxd running?)")
            print(f"device: {udid}", flush=True)
            progress(stage="pair_check", message="Checking pairing",
                     udid=udid)
            if not ensure_paired(udid):
                progress(stage="pair_required",
                         message="Device is not paired; run Pair / Trust")
                return 2
            progress(stage="mount", message="Mounting AFC")
            mnt = mount_afc(udid)
            progress(stage="db_find", message="Locating MediaLibrary database")
            found = find_media_db(mnt)
            if not found:
                raise RuntimeError("MediaLibrary.sqlitedb not found on mount")
            db = found
            remote_db = "/" + str(found.relative_to(mnt))
        if not db.exists():
            raise RuntimeError(f"database not found: {db}")
        if remote_db and udid and shutil.which("afcclient"):
            try:
                snapshot = snapshot_db_afc(
                    udid, remote_db, args.snapshot_dir, progress=progress)
            except Exception as exc:  # noqa: BLE001
                print(f"AFC snapshot failed ({exc}); using ifuse fallback",
                      flush=True)
                snapshot = snapshot_db(
                    db, args.snapshot_dir, progress=progress)
        else:
            snapshot = snapshot_db(db, args.snapshot_dir, progress=progress)
        library_id = read_library_uuid(snapshot)
        report = inspect_and_extract(snapshot, args.library, progress=progress)
        if not report.get("ok"):
            progress(stage="error", message=report.get("error", "DB parse failed"))
            return 1
        report["source"]["library_id"] = library_id
        report["stats"]["library_id"] = library_id

        progress(stage="cache_write", message="Updating SQLite cache",
                 stats={"library_id": library_id})
        media_cache.ensure_cache(
            args.cache_db,
            legacy_dates_path=BASE_DIR / "media-db-dates.json",
            legacy_artwork_path=BASE_DIR / "media-db-artwork.json",
            base_dir=str(BASE_DIR))
        conn = media_cache.open_db(args.cache_db)
        try:
            cache_stats = media_cache.replace_dates(
                conn, report["dates"],
                source={"database": str(snapshot),
                        "library_id": library_id,
                        "table": report["source"]["table"],
                        "pid_column": report["source"]["pid_column"],
                        "date_column": report["source"]["date_column"],
                        "formats": report["source"].get("formats", {}),
                        "stats": report["stats"]})
        finally:
            conn.close()
        report["stats"]["cache"] = cache_stats

        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            tmp = args.out.with_suffix(args.out.suffix + ".tmp")
            tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, args.out)
            print(f"\nwrote {args.out}", flush=True)

        progress(stage="done", done=cache_stats["rows"],
                 total=cache_stats["rows"], unit="rows",
                 message="Database cache updated",
                 stats={**report["stats"], **cache_stats})
        if not args.progress_json:
            print(json.dumps(
                {k: report.get(k) for k in ("ok", "source", "stats")},
                ensure_ascii=False, indent=2), flush=True)
        return 0
    finally:
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
