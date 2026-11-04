#!/usr/bin/env python3
"""Minimal iAP2 CSM encoding helpers."""
import struct

CSM_START = 0x4040


def param(pid, payload):
    return struct.pack(">HH", len(payload) + 4, pid) + payload


def frame(msg_id, params):
    body = b"".join(params)
    return struct.pack(">HHH", CSM_START, len(body) + 6, msg_id) + body


def string(pid, value):
    return param(pid, value.encode("utf-8") + b"\x00")


def u8(pid, value):
    return param(pid, struct.pack(">B", value))


def u16(pid, value):
    return param(pid, struct.pack(">H", value))


def u32(pid, value):
    return param(pid, struct.pack(">I", value))


def u64(pid, value):
    return param(pid, struct.pack(">Q", value))


def u16_list(pid, values):
    return param(pid, b"".join(struct.pack(">H", value) for value in values))


def void(pid):
    return param(pid, b"")


def group(pid, children):
    return param(pid, b"".join(children))


def bytes_param(pid, value):
    return param(pid, value)


def parse_params(body):
    params = []
    offset = 0
    while offset + 4 <= len(body):
        length, pid = struct.unpack(">HH", body[offset:offset + 4])
        if length < 4 or offset + length > len(body):
            break
        params.append((pid, body[offset + 4:offset + length]))
        offset += length
    return params


def parse_frame(data):
    if len(data) < 6:
        return None
    start, length, msg_id = struct.unpack(">HHH", data[:6])
    if start != CSM_START or length < 6 or length > len(data):
        return None
    return msg_id, parse_params(data[6:length])
