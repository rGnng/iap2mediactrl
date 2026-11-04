#!/usr/bin/env python3
"""Import the iPod/iPhone MediaLibrary SQLite database via AFC/ifuse.

This program is intentionally separate from the iAP2/HID service.  It only
produces a normalized result file that the UI can consume later.

Examples:
    sudo python3 media_db_import.py --mount
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

APPLE_EPOCH = 978307200  # 2001-01-01 UTC
DEFAULT_DB = "MediaLibrary.sqlitedb"
DATE_RE = re.compile(r"(date.*add|add.*date|date.*creat|creat.*date|added)", re.I)
PID_RE = re.compile(r"(item_pid|itempersistent|persistent.*id)", re.I)


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


def snapshot_db(db: Path, out_dir: Path):
    """Take a consistent SQLite snapshot, including any live WAL pages."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / db.name
    # A stale SQLite file can survive a previous failed copy.  Always start
    # from a clean destination, otherwise backup() can leave old pages behind.
    if db.resolve() != dst.resolve():
        for candidate in (dst, Path(str(dst) + "-wal"),
                          Path(str(dst) + "-shm")):
            try:
                candidate.unlink()
            except OSError:
                pass
    try:
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
        out = sqlite3.connect(dst)
        try:
            src.backup(out)
        finally:
            out.close()
            src.close()
        print(f"sqlite backup {db} -> {dst}", flush=True)
        return dst
    except Exception as exc:
        print(f"sqlite backup failed ({exc}); falling back to file copy",
              flush=True)
    copied = []
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(db) + suffix)
        if src.exists():
            target = out_dir / (db.name + suffix)
            shutil.copy2(src, target)
            copied.append(str(target))
            print(f"copied {src} -> {target}", flush=True)
    if not copied:
        raise RuntimeError(f"database not found: {db}")
    return dst


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


def inspect_and_extract(db_path: Path, library_path: Path | None):
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
                    help="mount the device with ifuse and copy the DB")
    ap.add_argument("--db", type=Path, help="use an existing DB copy")
    ap.add_argument("--udid", help="device UDID for ifuse/idevicepair")
    ap.add_argument("--library", type=Path,
                    default=Path("media-library.json"),
                    help="current media-library.json for match statistics")
    ap.add_argument("--out", type=Path,
                    default=Path("media-db-dates.json"))
    ap.add_argument("--snapshot-dir", type=Path,
                    default=Path("artifacts/media-db-snapshot"))
    args = ap.parse_args()

    mnt = None
    db = args.db
    try:
        if args.mount or db is None:
            udid = args.udid or find_device()
            if not udid:
                raise RuntimeError("no iOS device found (is usbmuxd running?)")
            print(f"device: {udid}", flush=True)
            if not ensure_paired(udid):
                return 2
            mnt = mount_afc(udid)
            found = find_media_db(mnt)
            if not found:
                raise RuntimeError("MediaLibrary.sqlitedb not found on mount")
            db = found
        if not db.exists():
            raise RuntimeError(f"database not found: {db}")
        snapshot = snapshot_db(db, args.snapshot_dir)
        report = inspect_and_extract(snapshot, args.library)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.out.with_suffix(args.out.suffix + ".tmp")
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, args.out)
        print(f"\nwrote {args.out}", flush=True)
        print(json.dumps({k: report.get(k) for k in ("ok", "source", "stats")},
                         ensure_ascii=False, indent=2), flush=True)
        return 0 if report.get("ok") else 1
    finally:
        if mnt is not None:
            unmount_afc(mnt)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
