#!/usr/bin/env python3
"""iAP2 authentication + identification probe for the iPod touch."""
import asyncio
import os
import struct
import sys
import time

import hid
import usb.core
import usb.util

import csm
from hid_async import HIDReader, HIDWriter
from mfi_local import LocalMfiProvider
import ref_iap2_link_layer as link_layer

APPLE_VID = 0x05AC
APPLE_PID = 0x12AA
CONFIG = 2

# Certificates live outside the project: ../iap2cert/
CERT_PATH = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "iap2cert", "certificate.p7b"))
KEY_PATH = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "iap2cert", "identity.pk8"))

INPUT_REPORTS = {
    1: 5, 2: 9, 3: 13, 4: 17, 5: 25, 6: 49,
    7: 95, 8: 193, 9: 257, 10: 385, 11: 513, 12: 767,
}
OUTPUT_REPORTS = {
    13: 5, 14: 9, 15: 13, 16: 17, 17: 25, 18: 49,
    19: 95, 20: 193, 21: 255,
}


def set_configuration():
    dev = usb.core.find(idVendor=APPLE_VID, idProduct=APPLE_PID)
    if dev is None:
        sys.exit("No Apple device")
    try:
        dev.set_configuration(CONFIG)
    except Exception as exc:  # noqa: BLE001
        print(f"set_configuration: {exc}")
    time.sleep(1)
    try:
        usb.util.dispose_resources(dev)
    except Exception:  # noqa: BLE001
        pass


def open_hid():
    dev = hid.device()
    dev.open(APPLE_VID, APPLE_PID)
    print(
        f"Opened HID: {dev.get_manufacturer_string()} / "
        f"{dev.get_product_string()}"
    )
    return dev


LIB_STATE = {"count": 0, "followed": False}
STATE_REV = [None]
local_uuid = [None]


def identification_params():
    sent = [
        0xAA01, 0xAA03,
        0x5000, 0x5002,
        0x4C00, 0x4C02, 0x4C03, 0x4C05, 0x4C06, 0x4C07, 0x4C08, 0x4C09,
        0x6800, 0x6802, 0x6803,
    ]
    received = [
        0xAA00, 0xAA02, 0xAA04, 0xAA05,
        0x5001,
        0x4C01, 0x4C04,
        0x6801, 0x6804,
    ]
    return [
        csm.string(0, "MediaCtrl"),
        csm.string(1, "MediaCtrl-1"),
        csm.string(2, "MediaCtrl"),
        csm.string(3, "MC-0001"),
        csm.string(4, "0.1"),
        csm.string(5, "1.0"),
        csm.u16_list(6, sent),
        csm.u16_list(7, received),
        csm.u8(8, 0),
        csm.u16(9, 20),
        csm.string(12, "en"),
        csm.param(13, b"en\x00"),
        csm.group(
            15,
            [
                csm.u16(0, 0),
                csm.string(1, "USB Device"),
                csm.void(2),
            ],
        ),
        csm.group(
            0x0012,
            [
                csm.u16(0, 0x14E9),
                csm.string(1, "MediaCtrl HID"),
                csm.u8(2, 0x01),
            ],
        ),
    ]


def now_playing_params():
    return [
        csm.group(
            0,
            [csm.void(i) for i in
             (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14,
              16, 18, 19, 26)],
        ),
        # PlaybackAttributes: status, elapsed, queue index/count/chapter,
        # shuffle mode, repeat mode, app name, and queue-list transfer size.
        # ID 17 = PlaybackQueueListContentTransferSize; 0 means "full list".
        csm.group(
            1,
            [csm.void(i) for i in range(0, 17)] +
            [csm.param(17, (0).to_bytes(4, "big"))],
        ),
        # PlaybackQueueListContentTransferInfoRequest: request queue item
        # fields. ID 0 (PID) is the minimum; the rest give display metadata.
        csm.group(
            2,
            [csm.void(i) for i in (0, 1, 6, 12, 14, 16, 18)],
        ),
    ]


def parse_string(payload):
    return payload.rstrip(b"\x00").decode("utf-8", errors="replace")


def parse_u32(payload):
    return int.from_bytes(payload, "big") if payload else None


HID_REPORT_DESCRIPTOR = bytes([
    0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x15, 0x00, 0x25, 0x01,
    0x75, 0x01, 0x95, 0x0C,
    0x09, 0xB0, 0x09, 0xB1, 0x09, 0xB5, 0x09, 0xB6, 0x09, 0xB9, 0x09, 0xBC,
    0x09, 0xE9, 0x09, 0xEA, 0x09, 0xE2, 0x09, 0xB7, 0x09, 0xB3, 0x09, 0xB4,
    0x81, 0x02, 0xC0,
])
HID_COMPONENT_ID = 0x14E9
HID_VENDOR_ID = 0x18D1
HID_PRODUCT_ID = 0x4E26
HID_BITS = {
    "play": 0, "pause": 1, "next": 2, "previous": 3,
    "shuffle": 4, "repeat": 5, "volume_up": 6, "volume_down": 7,
    "mute": 8, "stop": 9, "fast_forward": 10, "rewind": 11,
}
HID_REPORT_BYTES = 2


async def send_hid_start(conn):
    await send_csm(conn, 0x6800, [
        csm.u16(0, HID_COMPONENT_ID),
        csm.u16(1, HID_VENDOR_ID),
        csm.u16(2, HID_PRODUCT_ID),
        csm.bytes_param(4, HID_REPORT_DESCRIPTOR),
    ])


async def send_hid_report(conn, report: bytes):
    await send_csm(conn, 0x6802, [
        csm.u16(0, HID_COMPONENT_ID),
        csm.bytes_param(1, report),
    ])


async def send_hid_command(conn, bit: int):
    report = bytearray(HID_REPORT_BYTES)
    report[bit // 8] = 1 << (bit % 8)
    await send_hid_report(conn, bytes(report))
    await send_hid_report(conn, bytes(HID_REPORT_BYTES))


def send_ft(conn, data):
    conn.send_packet(link_layer.IAP2Packet(data, session_id=link_layer.IAP2Connection.FT_SESSION_ID))


def make_ft_handler(conn, artwork_log):
    transfers = {}

    def handler(data):
        if len(data) < 2:
            print(f"  FT datagram too short: {data.hex(' ')}")
            return
        fid, cmd = data[0], data[1]
        payload = data[2:]
        if cmd == 0x04:  # Setup
            size = int.from_bytes(payload[:8], "big") if len(payload) >= 8 else 0
            ftype = (
                int.from_bytes(payload[8:10], "big") if len(payload) >= 10 else None
            )
            print(f"  FT Setup id={fid} size={size} filetype={ftype} -> Start")
            transfers[fid] = {"size": size, "filetype": ftype, "data": bytearray()}
            send_ft(conn, bytes([fid, 0x01]))  # Start
        elif cmd == 0x01:  # Start
            print(f"  FT Start id={fid}")
        elif cmd in (0x80, 0x00, 0x40, 0xC0):  # FirstData / Data / LastData / FirstAndOnly
            t = transfers.get(fid)
            if t is None:
                print(f"  FT data for unknown id={fid}: {len(payload)} bytes")
                return
            t["data"] += payload
            total = len(t["data"])
            if cmd in (0x40, 0xC0):
                print(
                    f"  FT complete id={fid} filetype={t['filetype']} "
                    f"bytes={total} expected={t['size']}"
                )
                if total:
                    artwork_log.append((fid, t["filetype"], bytes(t["data"])))
                else:
                    print("  (empty file; not saving)")
                send_ft(conn, bytes([fid, 0x05]))  # Success
                del transfers[fid]
            else:
                print(f"  FT data id={fid} +{len(payload)} bytes (total {total})")
        elif cmd == 0x02:
            print(f"  FT Cancel id={fid}")
            transfers.pop(fid, None)
        elif cmd == 0x05:
            print(f"  FT Success id={fid}")
        elif cmd == 0x06:
            print(f"  FT Failure id={fid}")
        else:
            print(f"  FT unknown cmd={cmd:#04x} id={fid} {data.hex(' ')}")

    return handler


def parse_library_info(body):
    """Parse 0x4C01 MediaLibraryInformation into a list of library dicts."""
    libraries = []
    for group_id, group in csm.parse_params(body):
        if group_id != 0:
            continue
        lib = {"group_id": group_id}
        for pid, payload in csm.parse_params(group):
            if pid == 0:
                lib["name"] = payload.rstrip(b"\x00").decode("utf-8", errors="replace")
            elif pid == 1:
                lib["uuid"] = payload.rstrip(b"\x00").decode("utf-8", errors="replace")
            elif pid == 2:
                lib["type"] = int.from_bytes(payload, "big") if payload else None
            else:
                lib[f"param_{pid}"] = payload.hex(" ")
        libraries.append(lib)
    return libraries


def print_now_playing(body):
    for pid, payload in csm.parse_params(body):
        if pid == 0:
            print("  media item:")
            for child_id, child in csm.parse_params(payload):
                if child_id == 1:
                    print(f"    title={parse_string(child)!r}")
                elif child_id == 6:
                    print(f"    album={parse_string(child)!r}")
                elif child_id == 12:
                    print(f"    artist={parse_string(child)!r}")
                elif child_id == 4:
                    print(f"    duration_ms={parse_u32(child)}")
                elif child_id == 26:
                    print(f"    artwork_id={child[0] if child else None}")
        elif pid == 1:
            print("  playback:")
            for child_id, child in csm.parse_params(payload):
                if child_id == 0:
                    print(f"    status={child[0] if child else None}")
                elif child_id == 1:
                    print(f"    elapsed_ms={parse_u32(child)}")
                elif child_id == 7:
                    print(f"    app={parse_string(child)!r}")


async def send_csm(conn, msg_id, params):
    data = csm.frame(msg_id, params)
    print(f"TX CSM 0x{msg_id:04x} bytes={len(data)}")
    conn.control_session.write(data)
    await conn.control_session.drain()


async def handle_control(conn, mfi):
    stream = conn.control_session
    while True:
        try:
            header = await stream.readexactly(6)
        except Exception as exc:  # noqa: BLE001
            print(f"control session ended: {exc}")
            return

        start, length, msg_id = struct.unpack(">HHH", header)
        if start != 0x4040:
            print(f"bad CSM start: {header.hex(' ')}")
            continue
        body = await stream.readexactly(length - 6) if length > 6 else b""
        print(f"RX CSM 0x{msg_id:04x} length={length} body={len(body)}")

        if msg_id == 0xAA00:
            await send_csm(conn, 0xAA01, [csm.bytes_param(0, mfi.certificate_blob())])
        elif msg_id == 0xAA02:
            params = dict(csm.parse_params(body))
            challenge = params.get(0, b"")
            print(f"  challenge bytes={len(challenge)}")
            signature = mfi.sign_challenge(challenge)
            print(f"  signature bytes={len(signature)}")
            await send_csm(conn, 0xAA03, [csm.bytes_param(0, signature)])
        elif msg_id == 0xAA04:
            print("  authentication failed")
        elif msg_id == 0xAA05:
            print("  authentication succeeded")
        elif msg_id == 0x1D00:
            print("  start identification")
            await send_csm(conn, 0x1D01, identification_params())
        elif msg_id == 0x1D02:
            print("  identification accepted")
            await send_csm(conn, 0x5000, now_playing_params())
            await send_csm(conn, 0x4C00, [])
            await send_hid_start(conn)
            hid_command = os.environ.get("HID_COMMAND")
            if hid_command:
                bit = HID_BITS[hid_command]

                async def _run():
                    await asyncio.sleep(float(os.environ.get("HID_DELAY", "2")))
                    print(f"  sending HID command {hid_command} (bit {bit})")
                    await send_hid_command(conn, bit)

                asyncio.get_event_loop().create_task(_run())
        elif msg_id == 0x1D03:
            print("  identification rejected")
            for pid, payload in csm.parse_params(body):
                print(f"    rejected param {pid}: {payload.hex(' ')}")
        elif msg_id == 0x5001:
            print("  now playing update")
            print_now_playing(body)
        elif msg_id == 0x6801 or msg_id == 0x6804:
            print(f"  HID message 0x{msg_id:04x}")
            for pid, payload in csm.parse_params(body):
                print(f"    param {pid}: {payload.hex(' ')}")
        elif msg_id == 0x4C01:
            print("  media library information")
            for lib in parse_library_info(body):
                print(f"    library: {lib}")
            # Experiment: start media library updates for the local library.
            # Per the MFi spec (16.2.2) StartMediaLibraryUpdates enumerates
            # the media-item / media-playlist properties of interest and
            # carries LastKnownMediaLibraryRevision.
            libs = parse_library_info(body)
            local = next(
                (l for l in libs if l.get("uuid", "").endswith("-MPB-15.7.8")),
                libs[0] if libs else None,
            )
            exp = os.environ.get("LIB_EXPERIMENT", "uuid")
            params = []
            if local and exp != "none":
                uuid = local["uuid"]
                item_props = list(range(0, 27))
                playlist_props = list(range(0, 6))
                rev = local.get("revision", 0)
                if exp == "uuid":
                    params.append(csm.string(0, uuid))
                elif exp == "uuid+rev":
                    params.append(csm.string(0, uuid))
                    params.append(csm.param(2, bytes([rev])))
                elif exp == "rev64":
                    params.append(csm.string(0, uuid))
                    params.append(csm.param(1, (0).to_bytes(8, "big")))
                elif exp == "rev32":
                    params.append(csm.string(0, uuid))
                    params.append(csm.param(1, (0).to_bytes(4, "big")))
                elif exp == "revstr":
                    params.append(csm.string(0, uuid))
                    params.append(csm.string(1, "0"))
                elif exp in ("props", "props2"):
                    params.append(csm.string(0, uuid))
                    params.append(csm.param(1, (0).to_bytes(8, "big")))
                    params.append(csm.group(2, [csm.void(i) for i in item_props]))
                    params.append(csm.group(3, [csm.void(i) for i in playlist_props]))
                elif exp == "props0":
                    params.append(csm.string(0, uuid))
                    params.append(csm.group(2, [csm.void(i) for i in item_props]))
                    params.append(csm.group(3, [csm.void(i) for i in playlist_props]))
                elif exp == "props-wide":
                    params.append(csm.string(0, uuid))
                    params.append(csm.param(1, (0).to_bytes(8, "big")))
                    params.append(csm.group(2, [csm.void(i) for i in range(0, 64)]))
                    params.append(csm.group(3, [csm.void(i) for i in range(0, 32)]))
                elif exp == "props1":
                    params.append(csm.string(0, uuid))
                    params.append(csm.param(1, (0).to_bytes(8, "big")))
                    params.append(csm.group(2, [csm.void(1)]))
                    params.append(csm.group(3, [csm.void(1)]))
                elif exp == "props45":
                    params.append(csm.string(0, uuid))
                    params.append(csm.param(1, (0).to_bytes(8, "big")))
                    params.append(csm.group(4, [csm.void(i) for i in item_props]))
                    params.append(csm.group(5, [csm.void(i) for i in playlist_props]))
                elif exp == "propsrev":
                    params.append(csm.string(0, uuid))
                    params.append(csm.string(1, os.environ.get("LIB_REVISION", "0")))
                    params.append(csm.group(2, [csm.void(i) for i in item_props]))
                    params.append(csm.group(3, [csm.void(i) for i in playlist_props]))
                elif exp == "rev64void":
                    params.append(csm.string(0, uuid))
                    params.append(csm.param(1, (0).to_bytes(8, "big")))
                    params.append(csm.void(2))
                    params.append(csm.void(3))
                elif exp == "full":  # legacy experiment (device stays silent)
                    params.append(csm.string(0, uuid))
                    params.append(csm.param(2, bytes([rev])))
                    params.append(csm.u16_list(3, [0, 1, 4, 6, 12, 26]))
                    params.append(csm.u16_list(4, [0, 1, 7]))
            local_uuid[0] = local["uuid"] if local else None
            print(f"  LIB_EXPERIMENT={exp} -> sending 0x4C03 with {len(params)} params")
            if exp != "none":
                await send_csm(conn, 0x4C03, params)
            if exp in ("ft4", "ft7", "ft3"):
                ftype = {"ft3": 3, "ft4": 4, "ft7": 7}[exp]
                setup = (bytes([0xF0 + ftype]) + b"\x04" +
                         (0).to_bytes(8, "big") + ftype.to_bytes(2, "big"))
                print(f"  FT request: filetype={ftype} setup={setup.hex(' ')}")
                send_ft(conn, setup)
            with open("media-library-body-4c01.bin", "wb") as fh:
                fh.write(body)
        elif msg_id == 0x4C04:
            params = csm.parse_params(body)
            rev = None
            did_print = False
            for pid, payload in params:
                if pid == 1:
                    rev = payload.rstrip(b"\x00").decode("utf-8", "replace")
                elif pid == 6:
                    print("  !!! MEDIA LIBRARY RESET")
                    did_print = True
                elif pid == 2:
                    item = {}
                    fields = {1: "title", 6: "album", 12: "artist",
                              14: "albumArtist", 16: "genre", 18: "composer"}
                    for cid, child in csm.parse_params(payload):
                        if cid in fields:
                            item[fields[cid]] = child.rstrip(b"\x00").decode(
                                "utf-8", "replace")
                        elif cid == 0:
                            item["id"] = f"{int.from_bytes(child, 'big'):016x}"
                        elif cid == 4:
                            item["duration_ms"] = int.from_bytes(child, "big")
                        elif cid == 7:
                            item["track"] = int.from_bytes(child, "big")
                        elif cid == 8:
                            item["tracks"] = int.from_bytes(child, "big")
                    print(f"  *** MEDIA ITEM: {item}")
                    did_print = True
                elif pid == 3:
                    print("  *** PLAYLIST GROUP ***")
                    for cid, child in csm.parse_params(payload):
                        print(f"      param {cid}: {child!r}")
                    did_print = True
            # track the last revision and end-of-stream marker (param 7)
            last7 = next((v[0] for pid, v in params if pid == 7 and v), None)
            if rev:
                STATE_REV[0] = rev
            if not did_print and LIB_STATE["count"] % 2000 == 0:
                print(f"    raw body ({len(body)} bytes): {body.hex(' ')}")
            if rev:
                print(f"    [update rev={rev} param7={last7}]")
            LIB_STATE["count"] += 1
            with open("media-library-body-4c04.bin", "wb") as fh:
                fh.write(body)
            # Experiment: after the deletion stream ends (param7==0x5A), send
            # 0x4C03 again with the latest revision to request the full DB.
            if (os.environ.get("LIB_EXPERIMENT", "") == "props2"
                    and last7 == 0x5A and not LIB_STATE["followed"]
                    and rev and local_uuid[0]):
                LIB_STATE["followed"] = True
                print(f"  -> deletion phase done, re-sending 0x4C03 rev={rev}")
                await send_csm(conn, 0x4C03, [
                    csm.string(0, local_uuid[0]),
                    csm.string(1, rev),
                    csm.group(2, [csm.void(i) for i in range(0, 27)]),
                    csm.group(3, [csm.void(i) for i in range(0, 6)]),
                ])
        else:
            print(f"  body={body.hex(' ')}")


async def main():
    set_configuration()
    if os.environ.get("IAP2_TRANSPORT") == "pyusb":
        from pyusb_hid import open_pyusb_hid
        hid_device = open_pyusb_hid()
        print("HID: pyusb direct (EP 0x83)")
    else:
        hid_device = open_hid()
    mfi = LocalMfiProvider(CERT_PATH, KEY_PATH)
    print(
        f"MFi loaded: certificate={len(mfi.certificate_blob())} bytes, "
        f"curve={mfi.public_key.curve.name}"
    )

    reader = HIDReader(hid_device, INPUT_REPORTS)
    writer = HIDWriter(hid_device, sorted(OUTPUT_REPORTS.items()))

    loop = asyncio.get_event_loop()

    conn = link_layer.IAP2Connection(writer, reader, loop, max_outgoing=30)
    conn.start()

    artwork_log = []
    conn.file_transfer_handler = make_ft_handler(conn, artwork_log)

    task = loop.create_task(handle_control(conn, mfi))
    try:
        await asyncio.sleep(float(os.environ.get("RUN_SECONDS", "30")))
    finally:
        task.cancel()
        writer.close()
        try:
            hid_device.close()
        except Exception:  # noqa: BLE001
            pass
        print(f"state={conn.state}")
        for fid, ftype, blob in artwork_log:
            ext = {0x0002: "jpg", 0x0008: "png"}.get(ftype, "bin")
            path = f"artwork-{fid}.{ext}"
            with open(path, "wb") as fh:
                fh.write(blob)
            print(f"saved {path}: {len(blob)} bytes")


if __name__ == "__main__":
    asyncio.run(main())
