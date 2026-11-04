#!/usr/bin/env python3
"""Device-mode and MediaLibrary job controller for the web service.

iAP2/HID and AFC cannot use the device at the same time.  This module owns the
serialized transition between the two modes and runs the AFC import tools as
subprocesses while publishing progress for the HTTP API.
"""
from __future__ import annotations

import copy
import glob
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import device_store
from auth_test import find_device, set_configuration

BASE_DIR = Path(__file__).resolve().parent
DB_TOOL = BASE_DIR / "media_db_import.py"
ART_TOOL = BASE_DIR / "media_db_artwork_import.py"


def _active_paths():
    library_id = device_store.active_library_id()
    if not library_id:
        raise RuntimeError(
            "No active iAP2 library; connect the device in iAP2 mode first")
    return device_store.paths(library_id)

ACTIONS = {"pair", "quick", "full", "db", "artwork", "clear"}

STAGE_MESSAGES = {
    "queued": "Queued",
    "switch_to_afc": "Switching to MediaLibrary mode",
    "wait_usbmuxd": "Waiting for the device",
    "pair_check": "Checking pairing",
    "pair": "Waiting for Trust on the device",
    "mount": "Mounting AFC",
    "afc_settle": "Waiting for the device to settle",
    "db_find": "Locating MediaLibrary database",
    "db_copy": "Copying MediaLibrary database",
    "db_parse": "Parsing library dates",
    "cache_write": "Writing SQLite cache",
    "artwork_wipe": "Deleting local artwork cache",
    "artwork_scan": "Scanning artwork",
    "artwork_copy": "Copying artwork",
    "clear": "Clearing local MediaLibrary cache",
    "finalize": "Finalizing cache",
    "switch_to_iap2": "Switching back to iAP2",
    "done": "Complete",
}


class _Cancelled(Exception):
    pass


def _capture(cmd, timeout=15):
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]}: timed out"


def _wait_for_device(timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rc, out = _capture(["idevice_id", "-l"], timeout=5)
        if rc == 0:
            for line in out.splitlines():
                line = line.strip()
                if line:
                    return line
        time.sleep(0.4)
    return None


def _restart_usbmuxd():
    errors = []
    for cmd in (["systemctl", "restart", "usbmuxd"],
                ["service", "usbmuxd", "restart"]):
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
                stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            errors.append(f"{cmd[0]}: timed out")
            continue
        if proc.returncode == 0:
            return
        errors.append(
            ((proc.stderr or proc.stdout or "").strip()
             or f"{cmd[0]} exited {proc.returncode}"))
    raise RuntimeError("cannot restart usbmuxd: " + "; ".join(errors))


def _cleanup_mounts():
    for path in glob.glob("/tmp/ipod-afc-*"):
        for cmd in (["fusermount3", "-u", path], ["umount", path]):
            try:
                subprocess.run(cmd, capture_output=True, text=True,
                               timeout=10, stdin=subprocess.DEVNULL)
            except Exception:  # noqa: BLE001
                pass
        try:
            os.rmdir(path)
        except OSError:
            pass


def _device_product():
    try:
        dev = find_device()
        product = getattr(dev, "product", None)
        return str(product) if product else None
    except Exception:  # noqa: BLE001
        return None


class MediaJobs:
    """One serialized MediaLibrary controller shared by HTTP threads."""

    def __init__(self, stop_session, start_session, session_ready,
                 reload_cache, auto_switch, clear_cache):
        self._stop_session = stop_session
        self._start_session = start_session
        self._session_ready = session_ready
        self._reload_cache = reload_cache
        self._auto_switch = auto_switch
        self._clear_cache = clear_cache
        self._lock = threading.RLock()
        self._thread = None
        self._proc = None
        self._cancel = threading.Event()
        self._mode = "iap2"
        self._paired = None
        self._udid = None
        self._device = _device_product()
        self._job = None

    # -- public state -------------------------------------------------
    def snapshot(self):
        with self._lock:
            return {
                "mode": self._mode,
                "paired": self._paired,
                "udid": self._udid,
                "device": self._device,
                "job": copy.deepcopy(self._job),
            }

    def busy(self):
        with self._lock:
            return bool(self._job and
                        self._job["state"] in ("running", "cancelling"))

    def start(self, action):
        if action not in ACTIONS:
            return False, f"unknown action: {action}"
        with self._lock:
            if self._job and self._job["state"] in ("running", "cancelling"):
                return False, "A MediaLibrary task is already running"
            if self._thread and self._thread.is_alive():
                return False, "A MediaLibrary task is already running"
            self._cancel.clear()
            self._job = self._new_job(action)
            self._thread = threading.Thread(
                target=self._run, args=(action,), daemon=True)
            self._thread.start()
        return True, None

    def cancel(self):
        with self._lock:
            job = self._job
            if not job or job["state"] not in ("running", "cancelling"):
                return False, "No MediaLibrary task is running"
            job["state"] = "cancelling"
            job["message"] = "Cancelling…"
            job["updated_at"] = time.time()
            proc = self._proc
        self._cancel.set()
        if proc is not None:
            self._signal(proc, signal.SIGINT)
        return True, None

    def request_iap2(self):
        with self._lock:
            if self._job and self._job["state"] in ("running", "cancelling"):
                return False, "Cancel the running MediaLibrary task first"
            if self._thread and self._thread.is_alive():
                return False, "Cancel the running MediaLibrary task first"
            if self._mode == "iap2":
                return True, None
            self._cancel.clear()
            self._job = self._new_job("mode")
            self._thread = threading.Thread(
                target=self._manual_iap2, daemon=True)
            self._thread.start()
        return True, None

    # -- job state ----------------------------------------------------
    @staticmethod
    def _new_job(action):
        now = time.time()
        return {
            "action": action,
            "state": "running",
            "stage": "queued",
            "message": STAGE_MESSAGES["queued"],
            "done": 0,
            "total": 0,
            "unit": "",
            "bytes_done": 0,
            "bytes_total": 0,
            "stats": {},
            "error": None,
            "started_at": now,
            "updated_at": now,
            "finished_at": None,
        }

    def _set_job(self, **fields):
        with self._lock:
            if self._job is None:
                return
            stats = fields.pop("stats", None)
            self._job.update(fields)
            if stats:
                self._job["stats"].update(stats)
            self._job["updated_at"] = time.time()

    def _finish(self, state, message=None, error=None):
        with self._lock:
            if self._job is not None:
                self._job["state"] = state
                if message:
                    self._job["message"] = message
                self._job["error"] = error
                self._job["finished_at"] = time.time()
                self._job["updated_at"] = self._job["finished_at"]
            self._proc = None

    def _set_mode(self, mode):
        with self._lock:
            self._mode = mode

    def _apply_progress(self, data):
        fields = {}
        for key in ("stage", "done", "total", "unit", "bytes_done",
                    "bytes_total", "message", "stats"):
            if key in data:
                fields[key] = data[key]
        if "stage" in fields and "message" not in fields:
            fields["message"] = STAGE_MESSAGES.get(
                fields["stage"], fields["stage"])
        self._set_job(**fields)

    # -- execution ----------------------------------------------------
    def _run(self, action):
        try:
            if action == "clear":
                self._set_job(stage="clear",
                              message="Clearing local MediaLibrary cache")
                stats = self._clear_cache()
                self._set_job(stage="done", stats=stats,
                              message="Local cache cleared")
                self._finish("done", "Local cache cleared")
                return

            if action == "pair":
                self._enter_afc()
                self._ensure_paired()
                self._set_job(stage="done", message="Pairing complete")
                self._finish("done", "Pairing complete")
                return

            self._enter_afc()
            self._ensure_paired()
            if action in ("db", "quick", "full"):
                self._run_db()
                self._reload("library")
            if action in ("artwork", "quick", "full"):
                self._run_artwork(full=(action == "full"),
                                  fresh_db=(action == "artwork"))
                self._reload("artwork")

            if self._auto_switch():
                self._set_job(
                    stage="finalize",
                    message="Cache updated; switching to iAP2")
                self._sleep(1.5)
                self._enter_iap2()
                self._set_job(stage="done", message="Complete")
                self._finish("done", "Complete")
            else:
                self._set_job(
                    stage="done",
                    message="Complete; staying in MediaLibrary mode")
                self._finish("done", "Complete")
        except _Cancelled:
            self._finish("cancelled", "Cancelled")
        except Exception as exc:  # noqa: BLE001
            print(f"media job {action} failed: {type(exc).__name__}: {exc}",
                  flush=True)
            self._finish("error", f"{type(exc).__name__}: {exc}",
                         error=f"{type(exc).__name__}: {exc}")

    def _manual_iap2(self):
        try:
            self._enter_iap2()
            self._set_job(stage="done", message="iAP2 mode")
            self._finish("done", "iAP2 mode")
        except _Cancelled:
            self._finish("cancelled", "Cancelled")
        except Exception as exc:  # noqa: BLE001
            print(f"iAP2 switch failed: {type(exc).__name__}: {exc}",
                  flush=True)
            self._finish("error", f"{type(exc).__name__}: {exc}",
                         error=f"{type(exc).__name__}: {exc}")

    def _enter_afc(self):
        self._set_mode("switching")
        self._set_job(stage="switch_to_afc",
                      message="Stopping iAP2 session")
        self._cancel_check()
        self._stop_session()
        self._cancel_check()
        self._set_job(stage="switch_to_afc",
                      message="Switching USB to configuration 3")
        set_configuration(3)
        self._set_mode("afc")
        self._cancel_check()
        self._set_job(stage="wait_usbmuxd",
                      message="Restarting usbmuxd")
        _restart_usbmuxd()
        self._cancel_check()
        self._set_job(stage="wait_usbmuxd",
                      message="Waiting for the device")
        udid = _wait_for_device(12)
        if not udid:
            raise RuntimeError(
                "device did not appear in AFC mode (usbmuxd)")
        self._udid = udid
        self._device = _device_product() or self._device
        self._paired = None
        self._set_job(stage="afc_settle",
                      message="Waiting for the device to settle")
        # The device finishes writing after the iAP2 session stops.  The AFC
        # pull is consistent, but a short pause avoids an immediate reset.
        self._sleep(1.5)
        self._set_mode("afc")
        self._set_job(stage="afc_ready", message="MediaLibrary mode",
                      stats={"udid": udid})

    def _enter_iap2(self):
        self._set_mode("switching")
        self._set_job(stage="switch_to_iap2",
                      message="Unmounting AFC")
        _cleanup_mounts()
        self._cancel_check()
        self._set_job(stage="switch_to_iap2",
                      message="Switching USB to configuration 2")
        set_configuration(2)
        self._set_job(stage="switch_to_iap2",
                      message="Starting iAP2 session")
        self._start_session()
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if self._session_ready():
                break
            self._sleep(0.25)
        self._set_mode("iap2")
        self._set_job(stage="done", message="iAP2 mode")

    def _ensure_paired(self):
        udid = self._udid
        if not udid:
            raise RuntimeError("device UDID is unknown")
        self._set_job(stage="pair_check", message="Checking pairing")
        rc, out = _capture(["idevicepair", "-u", udid, "validate"], 15)
        if rc == 0:
            self._paired = True
            return
        self._set_job(
            stage="pair",
            message="Unlock the device and tap Trust")
        self._run_tool(
            ["idevicepair", "-u", udid, "pair"],
            stage="pair", timeout=120)
        rc, out = _capture(["idevicepair", "-u", udid, "validate"], 15)
        if rc != 0:
            raise RuntimeError("pairing validation failed: " + out.strip())
        self._paired = True

    def _run_db(self):
        p = _active_paths()
        cmd = [sys.executable, str(DB_TOOL), "--mount",
               "--progress-json", "--cache-db", str(p["media_cache"]),
               "--snapshot-dir", str(p["snapshot"])]
        if self._udid:
            cmd += ["--udid", self._udid]
        self._run_tool(cmd, stage="db_copy", timeout=180)

    def _run_artwork(self, full=False, fresh_db=False):
        p = _active_paths()
        cmd = [sys.executable, str(ART_TOOL), "--mount",
               "--progress-json", "--cache-db", str(p["media_cache"]),
               "--artwork-dir", str(p["artwork"]),
               "--mode", "full" if full else "quick"]
        if not fresh_db:
            cmd += ["--db", str(p["snapshot_db"])]
        cmd += ["--snapshot-dir", str(p["snapshot"])]
        if self._udid:
            cmd += ["--udid", self._udid]
        self._run_tool(cmd, stage="artwork_copy", timeout=1800)

    def _reload(self, target="all"):
        try:
            self._reload_cache(target)
        except Exception as exc:  # noqa: BLE001
            print(f"cache reload failed: {exc}", flush=True)

    def _run_tool(self, cmd, stage, timeout):
        self._set_job(stage=stage, message=STAGE_MESSAGES.get(stage, stage))
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(BASE_DIR),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            raise RuntimeError(f"cannot start {Path(cmd[0]).name}: {exc}")
        with self._lock:
            self._proc = proc
        lines = queue.Queue()

        def _reader():
            try:
                if proc.stdout is not None:
                    for line in proc.stdout:
                        lines.put(line)
            finally:
                lines.put(None)

        threading.Thread(target=_reader, daemon=True).start()
        tail = deque(maxlen=30)
        deadline = time.monotonic() + timeout
        try:
            while True:
                self._cancel_check()
                if time.monotonic() > deadline:
                    self._terminate(proc)
                    raise TimeoutError(
                        f"{stage} timed out after {timeout}s")
                try:
                    line = lines.get(timeout=0.25)
                except queue.Empty:
                    continue
                if line is None:
                    break
                text = line.rstrip()
                if text.startswith("PROGRESS "):
                    try:
                        self._apply_progress(json.loads(text[9:]))
                    except Exception:  # noqa: BLE001
                        pass
                elif text:
                    tail.append(text)
                    print(text, flush=True)
            rc = proc.wait(timeout=10)
        finally:
            with self._lock:
                if self._proc is proc:
                    self._proc = None
            if proc.poll() is None:
                self._terminate(proc)
        if self._cancel.is_set():
            raise _Cancelled()
        if rc != 0:
            detail = " | ".join(tail) if tail else f"exit {rc}"
            raise RuntimeError(f"{stage} failed: {detail}")

    def _signal(self, proc, sig):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _terminate(self, proc):
        if proc.poll() is not None:
            return
        self._signal(proc, signal.SIGINT)
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass
        self._signal(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            pass
        self._signal(proc, signal.SIGKILL)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass

    def _cancel_check(self):
        if self._cancel.is_set():
            raise _Cancelled()

    def _sleep(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self._cancel_check()
            time.sleep(min(0.2, max(0.0, end - time.monotonic())))
