#!/usr/bin/env python3
"""Asyncio HID reader/writer adapted from the wiomoc/iap2 USB HID transport."""
import asyncio
import os
import queue
import threading

HID_LOG_RAW = os.environ.get("HID_LOG_RAW") == "1"

LCB_CONTINUATION = 1
LCB_MORE_TO_FOLLOW = 2


class HIDReader:
    def __init__(self, hid_device, input_report_ids):
        self._loop = asyncio.get_event_loop()
        self._hid_device = hid_device
        self._input_report_ids = input_report_ids
        self._read_buffer_semaphore = threading.Semaphore(value=32)
        self._read_buffer_queue = asyncio.Queue()
        self._max_len = max(input_report_ids.values())
        self.eof = False
        self._read_buffer = None
        # Each HID input report is [report_id][link control byte][payload].
        # The device pads reports to the report-ID size class (and to whole
        # 64-byte USB packets), so the payload after the 2-byte header is
        # pure iAP2 link bytes. Feeding the header bytes to the link layer
        # corrupts large files (one 2-byte insertion every report); strip
        # them here and let the link layer see only the link byte stream.
        self._report_sizes = {}
        for rid, size in input_report_ids.items():
            total = size + 1  # report ID + (LCB + payload)
            # The device drops a 1-2 byte tail after a whole number of
            # 64-byte USB packets (e.g. report 8's descriptor length is
            # 194, but it is sent as 192 bytes). Larger tails (report 7:
            # 96 bytes) are sent in full.
            if total > 64 and total % 64 <= 2:
                total = (total // 64) * 64
            self._report_sizes[rid] = total
        self._report_remaining = None
        self._report_id = None
        self._raw_buffer = bytearray()
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _extract_payload(self, chunk):
        """Drop the per-report [report_id][LCB] header from raw HID data.

        Keeps report padding in the returned bytes; the link layer's FF 5A
        scan skips whatever sits between link packets. Returns the payload
        bytes that are ready for the link layer.
        """
        self._raw_buffer.extend(chunk)
        out = bytearray()
        while self._raw_buffer:
            if self._report_remaining is None:
                if len(self._raw_buffer) < 2:
                    break
                rid = self._raw_buffer[0]
                total = self._report_sizes.get(rid)
                if total is None or total <= 2:
                    # Unknown report ID: pass the bytes through unchanged
                    # rather than desynchronising on a guess.
                    out.extend(self._raw_buffer)
                    self._raw_buffer.clear()
                    break
                del self._raw_buffer[:2]
                self._report_id = rid
                self._report_remaining = total - 2
                continue
            take = min(self._report_remaining, len(self._raw_buffer))
            if take:
                out.extend(self._raw_buffer[:take])
                del self._raw_buffer[:take]
                self._report_remaining -= take
            if self._report_remaining:
                break
            self._report_remaining = None
        return bytes(out)

    async def readexactly(self, nbytes):
        if not self._read_buffer or len(self._read_buffer) == 0:
            self._read_buffer_semaphore.release()
            if self.eof:
                raise asyncio.IncompleteReadError(
                    partial=self._read_buffer, expected=nbytes
                )
            self._read_buffer = await self._read_buffer_queue.get()

        if len(self._read_buffer) >= nbytes:
            data = self._read_buffer[:nbytes]
            self._read_buffer = self._read_buffer[nbytes:]
            return data

        data = bytearray(self._read_buffer)
        while len(data) < nbytes:
            self._read_buffer_semaphore.release()
            if self.eof:
                raise asyncio.IncompleteReadError(
                    partial=self._read_buffer, expected=nbytes
                )
            data.extend(await self._read_buffer_queue.get())
        self._read_buffer = data[nbytes:]
        return bytes(data[:nbytes])

    def reset(self):
        self._read_buffer = None

    def _read_loop(self):
        try:
            while not self.eof:
                self._read_buffer_semaphore.acquire()
                while not self.eof:
                    report = self._hid_device.read(self._max_len + 2)
                    if len(report) <= 2:
                        continue
                    if HID_LOG_RAW:
                        print(
                            f"RAW urb len={len(report)}: {bytes(report).hex(' ')}",
                            flush=True,
                        )
                    # HID reports carry a 2-byte [report_id][LCB] prefix.
                    # Strip it and reassemble the iAP2 link byte stream;
                    # otherwise a 2-byte insertion every report corrupts
                    # large file transfers (artwork, media library frames).
                    packet = self._extract_payload(bytes(report))
                    if not packet:
                        continue
                    self._loop.call_soon_threadsafe(
                        lambda p=packet: self._read_buffer_queue.put_nowait(p)
                    )
                    break
        except Exception as exc:
            print(f"HID reader crashed: {exc}", flush=True)
            self.feed_eof()

    def feed_eof(self):
        self.eof = True
        # Wake any pending readexactly() by feeding it a sentinel.
        try:
            self._loop.call_soon_threadsafe(
                lambda: self._read_buffer_queue.put_nowait(b""))
        except Exception:  # noqa: BLE001
            pass


class HIDWriter:
    def __init__(self, hid_device, output_report_ids):
        self.closed = False
        self._hid_device = hid_device
        self._output_report_ids = output_report_ids
        self._write_buffer_queue = queue.Queue()
        threading.Thread(target=self._write_loop, daemon=True).start()

    def write(self, buffer):
        if self.closed:
            raise IOError("closed")
        self._write_buffer_queue.put_nowait(buffer)

    async def drain(self):
        return None

    def close(self):
        self.closed = True
        self._write_buffer_queue.put_nowait(None)

    def _write_loop(self):
        while True:
            first = True
            buf = self._write_buffer_queue.get()
            if buf is None or self.closed:
                return
            while len(buf) > 0:
                report_id = None
                report_count = None
                for rid, count in self._output_report_ids:
                    count -= 1
                    report_id = rid
                    report_count = count
                    if count > len(buf):
                        break
                lcb = 0
                if first:
                    first = False
                else:
                    lcb |= LCB_CONTINUATION
                if report_count < len(buf):
                    lcb |= LCB_MORE_TO_FOLLOW
                padding = b"\x00" * max(report_count - len(buf), 0)
                self._hid_device.write(
                    bytes([report_id, lcb]) + buf[:report_count] + padding
                )
                buf = buf[report_count:]
