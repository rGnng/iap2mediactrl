__all__ = ["IAP2Connection", "IAP2Stream"]

import asyncio
import os
from collections import namedtuple
from dataclasses import dataclass
from functools import reduce
from struct import Struct
from typing import ClassVar, List, Callable, Any

VERBOSE = os.environ.get("IAP2_VERBOSE") == "1"


def _log(*args, **kwargs):
    """Packet-level logging is opt-in: huge FT payloads would flood stdout."""
    if VERBOSE:
        print(*args, **kwargs)

CONTROL_SYN = 0x80
CONTROL_ACK = 0x40
CONTROL_EAK = 0x20
CONTROL_RST = 0x10


@dataclass
class LinkPacketHeader:
    struct: ClassVar = Struct(">HHBBBB")
    start: ClassVar = 0xFF5A
    length: int
    control: int
    seq: int
    ack: int
    session_id: int

    @staticmethod
    def from_bytes(header_bytes):
        if not check_checksum(header_bytes):
            return None
        (start, length, control, seq, ack,
         session_id) = LinkPacketHeader.struct.unpack(header_bytes[:-1])
        if start != LinkPacketHeader.start:
            return None
        return LinkPacketHeader(length, control, seq, ack, session_id)

    def pack(self):
        header_bytes = LinkPacketHeader.struct.pack(LinkPacketHeader.start,
                                                    self.length, self.control,
                                                    self.seq, self.ack,
                                                    self.session_id)
        return header_bytes + bytes([gen_checksum(header_bytes)])


def signed_add(a, b):
    return (a + b) & 0xff


def gen_checksum(packet):
    return -reduce(signed_add, packet) & 0xff


def check_checksum(packet):
    return reduce(signed_add, packet) == 0


LSPSession = namedtuple('LSPSession', 'id type version')


@dataclass
class LinkSynchronizationPayload:
    struct: ClassVar = Struct(">BBHHHBB")
    version: ClassVar = 0x01
    max_outgoing: int
    max_len: int
    retransmission_timeout: int
    ack_timeout: int
    max_retransmissions: int
    max_ack: int
    sessions: list

    @staticmethod
    def from_bytes(payload):
        (version, max_outgoing, max_len, retransmission_timeout, ack_timeout,
         max_retransmissions,
         max_ack) = LinkSynchronizationPayload.struct.unpack(payload[:10])
        if version != LinkSynchronizationPayload.version:
            return None

        sessions_bytes = payload[10:]
        sessions = []
        for i in range(0, len(sessions_bytes), 3):
            sessions.append(LSPSession._make(sessions_bytes[i:i + 3]))
        return LinkSynchronizationPayload(max_outgoing, max_len,
                                          retransmission_timeout, ack_timeout,
                                          max_retransmissions, max_ack,
                                          sessions)

    def pack(self):
        payload = LinkSynchronizationPayload.struct.pack(
            LinkSynchronizationPayload.version, self.max_outgoing,
            self.max_len, self.retransmission_timeout, self.ack_timeout,
            self.max_retransmissions, self.max_ack)
        return payload + b''.join([bytes([*s]) for s in self.sessions])


STATE_DETECT_IAP2_SUPPORT = 0
STATE_NEGOTIATE = 1
STATE_NORMAL = 2
STATE_DEAD = 3

IAP2_MARKER = b'\xFF\x55\x02\x00\xEE\x10'
EA_SESSION_ID_STRUCT = Struct(">H")


class IAP2Packet:
    def __init__(self, data: bytes, psn: int = None, session_id: int = 0):
        self.psn = psn
        self.data = data
        self.session_id = session_id


class IAP2Stream:
    def __init__(self, conn: "IAP2Connection", session_id: int, stream_id: int = None):
        self.conn = conn
        self.session_id = session_id
        self.stream_id = stream_id
        self.out_buffer = bytearray()
        self.in_buffer = bytearray()
        self.in_waiter_fut = None
        self.in_waiter_count = None
        if self.stream_id != None:
            self.out_buffer += EA_SESSION_ID_STRUCT.pack(self.stream_id)
        self.closed = False

    def write(self, data):
        if self.closed:
            raise IOError("closed")
        if len(self.out_buffer) == 0:
            self.out_buffer = data
        else:
            self.out_buffer += data
        while len(self.out_buffer) >= self.conn.lsp.max_len and self.conn.write_allowed_event.is_set():
            self.conn.send_packet(
                IAP2Packet(self.out_buffer[:self.conn.lsp.max_len],
                           session_id=self.session_id))
            del self.out_buffer[:self.conn.lsp.max_len]

    async def drain(self):
        if self.closed:
            raise IOError("closed")
        if len(self.out_buffer) == 0:
            return
        await self.conn.write_allowed_event.wait()
        self.conn.send_packet(
            IAP2Packet(self.out_buffer, session_id=self.session_id))
        self.out_buffer = bytearray()
        if self.stream_id != None:
            self.out_buffer += EA_SESSION_ID_STRUCT.pack(self.stream_id)

    def received_data(self, data):
        self.in_buffer += data
        if self.in_waiter_fut and self.in_waiter_count <= len(self.in_buffer):
            self.in_waiter_fut.set_result(True)
            self.in_waiter_fut = None

    async def readexactly(self, nbytes):
        if self.in_waiter_fut:
            return

        if len(self.in_buffer) < nbytes:
            if self.closed:
                raise asyncio.exceptions.IncompleteReadError(partial=self.in_buffer, expected=nbytes)
            self.in_waiter_count = nbytes
            fut = self.conn._loop.create_future()
            self.in_waiter_fut = fut
            await fut
            if self.closed:
                raise asyncio.exceptions.IncompleteReadError(partial=self.in_buffer, expected=nbytes)

        d = self.in_buffer[:nbytes]
        del self.in_buffer[:nbytes]
        return d

    def feed_eof(self):
        self.closed = True
        if self.in_waiter_fut:
            self.in_waiter_fut.set_result(True)
            self.in_waiter_fut = None


class IAP2Connection:
    CONTROL_SESSION_ID = 10
    EA_SESSION_ID = 11
    FT_SESSION_ID = 12

    def __init__(self,
                 output: asyncio.StreamWriter,
                 input: asyncio.StreamReader,
                 loop: asyncio.AbstractEventLoop = None,
                 max_outgoing: int = 30,
                 max_outgoing_delta: int = 0,
                 ack_timeout=500,
                 on_error: Callable[[Any], None] = None):
        self.on_error = on_error
        self.state = None
        self.lsp = LinkSynchronizationPayload(
            max_outgoing=max_outgoing,
            max_len=65535,
            retransmission_timeout=4000,
            ack_timeout=ack_timeout,
            max_retransmissions=4,
            max_ack=3,
            sessions=[
                LSPSession(id=IAP2Connection.CONTROL_SESSION_ID,
                           type=0,
                           version=1),
                LSPSession(id=IAP2Connection.EA_SESSION_ID, type=2, version=1),
                LSPSession(id=12, type=1, version=2),
            ])
        self._max_outgoing_delta = max_outgoing_delta
        self._sent_psn = 99
        self._last_sent_acknowledged_psn = None
        self._unack_packets = []
        self._queued_packets = []

        self._last_received_in_sequence_psn = 0
        self._last_acked_psn = None
        self._initial_received_psn = None
        self._received_out_of_sequence = []
        self._cumulative_received = 0
        self._loop = loop if loop is not None else asyncio.get_event_loop()
        self._output = output
        self._input = input
        self._send_ack_timer = None
        self._recv_ack_timer = None
        self._pending_ft = None
        self._pending_ft_seq = None
        self.write_allowed_event = asyncio.Event()
        self.control_session = IAP2Stream(self,
                                          IAP2Connection.CONTROL_SESSION_ID)
        self.ea_streams = dict()
        self.file_transfer_handler = None
        self._receive_loop_task = None

    def create_ea_stream(self, stream_id):
        stream = IAP2Stream(self, IAP2Connection.EA_SESSION_ID, stream_id)
        self.ea_streams[stream_id] = stream
        return stream

    def start(self):
        if self.state:
            return
        self._receive_loop_task = self._loop.create_task(self._receive_loop())
        self.state = STATE_DETECT_IAP2_SUPPORT
        self._send_detect_iap2_support()

    def close(self):
        self._input.feed_eof()

    def _write_packet(self, payload=None, seq=0, control=0, session_id=0):
        self._cumulative_received = 0
        if payload:
            length = len(payload) + 10
        else:
            length = 9
        header = LinkPacketHeader(control=control,
                                  length=length,
                                  seq=seq,
                                  ack=self._last_received_in_sequence_psn,
                                  session_id=session_id)
        _log(">", header, payload)
        header_bytes = header.pack()
        if payload:
            self._output.write(header_bytes + payload +
                               bytes([gen_checksum(payload)]))

        else:
            self._output.write(header_bytes)

    def _send_ack(self):
        self._write_packet(seq=self._sent_psn, control=CONTROL_ACK)

    def _send_eak(self, num):
        self._write_packet(bytes(num), seq=self._sent_psn, control=CONTROL_EAK)

    def _send_data(self, p):
        self._write_packet(p.data,
                           seq=p.psn,
                           control=CONTROL_ACK,
                           session_id=p.session_id)

    def _send_detect_iap2_support(self):
        if self.state != STATE_DETECT_IAP2_SUPPORT:
            return
        self._output.write(IAP2_MARKER)
        self._loop.call_later(1, self._send_detect_iap2_support)

    def _send_negotiate(self):
        if self.state != STATE_NEGOTIATE:
            return
        lsp_bytes = self.lsp.pack()
        self._write_packet(lsp_bytes, self._sent_psn, CONTROL_SYN)
        self._loop.call_later(0.5, self._send_negotiate)

    async def _receive_loop(self):
        try:
            recv_marker = await self._input.readexactly(len(IAP2_MARKER))
            while True:
                if recv_marker == IAP2_MARKER:
                    break
                recv_marker = recv_marker[1:] + await self._input.readexactly(1)
            if hasattr(self._input, "reset"):
                pass  # stream transport: never drop unread bytes
            self.state = STATE_NEGOTIATE
            self._send_negotiate()
            while True:
                skipped = bytearray()
                header_bytes = await self._input.readexactly(9)
                while True:
                    if int(header_bytes[0]) << 8 | int(
                            header_bytes[1]) == LinkPacketHeader.start:
                        break
                    skipped.append(header_bytes[0])
                    header_bytes = header_bytes[1:] + await self._input.readexactly(
                        1)
                # Bytes skipped before the next FF 5A belong to the previous
                # FT frame (its true tail), plus zero padding and the next
                # frame's 2-byte prefix. Finish that frame before parsing the
                # new one.
                if self._pending_ft is not None:
                    self._dispatch_pending_ft(bytes(skipped))
                header = LinkPacketHeader.from_bytes(header_bytes)
                if not header:
                    _log(f"!! header checksum/parse failed: {header_bytes.hex(' ')}")
                    continue
                payload = None
                if header.length > 9:
                    payload_with_checksum = await self._input.readexactly(
                        header.length - 9)
                    if not check_checksum(payload_with_checksum):
                        if header.session_id == IAP2Connection.FT_SESSION_ID:
                            # The iPod's FT length field undercounts data
                            # frames (and saturates at 0xFFFF for >64 KiB
                            # frames), so the frame's true tail is delivered
                            # with the bytes before the next frame. Keep the
                            # payload pending and advance the sequence now.
                            d = distance(header.seq,
                                         self._last_received_in_sequence_psn)
                            if d == 0:
                                self._send_ack()
                                continue
                            if d > 2:
                                self._handle_data(
                                    IAP2Packet(b"", header.seq, header.session_id),
                                    deliver=False)
                                continue
                            print(
                                f"!! FT frame short length: seq={header.seq} "
                                f"len={header.length} (tail pending)")
                            self._pending_ft = payload_with_checksum
                            self._pending_ft_seq = header.seq
                            self._handle_data(
                                IAP2Packet(b"", header.seq, header.session_id),
                                deliver=False)
                            continue
                        elif header.session_id == IAP2Connection.CONTROL_SESSION_ID:
                            # Large MediaLibraryUpdate (0x4C04) messages carry a
                            # trailing flag parameter whose value byte is not
                            # counted in the link length, nor in the CSM length
                            # field. Recover it and fix up the CSM length.
                            extra = await self._input.readexactly(1)
                            fixed = _fix_csm_length(
                                (payload_with_checksum + extra)[:-1])
                            if fixed is not None:
                                _log(
                                    f"!! control frame short length repaired: "
                                    f"seq={header.seq} len={header.length}")
                                payload = fixed
                            else:
                                print(
                                    f"!! payload checksum failed: seq={header.seq} "
                                    f"session={header.session_id} "
                                    f"len={header.length}")
                                continue
                        else:
                            print(
                                f"!! payload checksum failed: seq={header.seq} "
                                f"session={header.session_id} "
                                f"len={header.length}")
                            continue
                    else:
                        payload = payload_with_checksum[:-1]
                _log("<", header, payload)
                if (header.control & CONTROL_RST) != 0:
                    self._bailout("device sent reset message")
                if (header.control & CONTROL_SYN) != 0:
                    lsp = LinkSynchronizationPayload.from_bytes(payload)
                    if not lsp:
                        continue
                    self._handle_syn(lsp, header.seq)
                if (header.control & CONTROL_ACK) != 0:
                    self._cumulative_received += 1
                    self._handle_ack(header.ack)
                    if payload is None:
                        # ACK-only packets also consume a link sequence number.
                        # Without advancing here, the next data packet looks
                        # out of order and is never acknowledged.
                        if distance(
                            header.seq, self._last_received_in_sequence_psn
                        ) == 1:
                            self._last_received_in_sequence_psn = header.seq
                            self._last_acked_psn = header.seq
                if (header.control & CONTROL_EAK) != 0 and payload:
                    self._handle_eak([int(x) for x in payload])
                if (header.control & ~CONTROL_ACK) == 0 and payload != None:
                    self._handle_data(
                        IAP2Packet(payload, header.seq, header.session_id))
                if self._cumulative_received >= self.lsp.max_ack:
                    self._cumulative_received = 0
                    self._last_acked_psn = self._last_received_in_sequence_psn
                    self._send_ack()
        except asyncio.exceptions.IncompleteReadError:
            self._bailout(None)
        except Exception as e:
            self._bailout(e)

    def _bailout(self, error):
        if self.state == STATE_DEAD:
            return
        self._disarm_send_ack_timer()
        self._disarm_recv_ack_timer()
        self.state = STATE_DEAD
        try:
            self._output.close()
        except:
            pass
        try:
            self.control_session.feed_eof()
            for stream in self.ea_streams.values():
                stream.feed_eof()
        except:
            pass
        if self._receive_loop_task:
            try:
                self._receive_loop_task.cancel()
            except:
                pass
        if error is not None and self.on_error:
            self.on_error(error)

    def send_packet(self, p: IAP2Packet):
        if distance(self._sent_psn, self._last_sent_acknowledged_psn
                    ) > self.lsp.max_outgoing or self.state != STATE_NORMAL:
            self._queued_packets.append(p)
            self.write_allowed_event.clear()
            return

        self._sent_psn = signed_add(self._sent_psn, 1)
        p.counter = 0
        p.psn = self._sent_psn
        p.timeout = self._loop.time() + self.lsp.retransmission_timeout / 1000
        self._disarm_send_ack_timer()
        self._send_data(p)
        self._last_acked_psn = self._last_received_in_sequence_psn
        self._rearm_recv_ack_timer(p.timeout)
        self._unack_packets.append(p)

    def _handle_syn(self, lsp: LinkSynchronizationPayload, psn: int):
        if self.state != STATE_NEGOTIATE:
            return
        _log("Device:", lsp)
        _log("Accessory:", self.lsp)
        self.lsp = lsp
        self._last_received_in_sequence_psn = psn
        self._last_acked_psn = psn
        self._send_ack()

    def _handle_ack(self, num: int):
        if self.state == STATE_NEGOTIATE:
            self.state = STATE_NORMAL
            self.write_allowed_event.set()
        self._last_sent_acknowledged_psn = num

        while len(self._unack_packets) != 0:
            d = distance(
                self._unack_packets[0].psn,
                self._last_sent_acknowledged_psn)
            if 0 < d <= self.lsp.max_ack + 10:
                self._rearm_recv_ack_timer(self._unack_packets[0].timeout)
                break
            else:
                del self._unack_packets[0]
        else:
            self._disarm_recv_ack_timer()

        while distance(self._sent_psn, self._last_sent_acknowledged_psn
                       ) < self.lsp.max_outgoing and len(
            self._queued_packets) > 0:
            self.send_packet(self._queued_packets.pop(0))
            self.write_allowed_event.set()

    def _on_expect_ack_timer(self):
        if len(self._unack_packets) == 0 or self.state != STATE_NORMAL:
            return
        unack_packets = sorted(self._unack_packets, key=lambda x: x.timeout)
        p = unack_packets[0]
        p.timeout = self._loop.time() + self.lsp.retransmission_timeout / 1000
        p.counter += 1
        if p.counter == self.lsp.max_retransmissions:
            self._bailout(p)
            return
        self._send_data(p)
        self._rearm_recv_ack_timer(unack_packets[0 if len(unack_packets) == 1 else 1].timeout)

    def _handle_eak(self, nums: List[int]):
        if self.state != STATE_NORMAL:
            return
        for p in self._unack_packets:
            if p.psn in nums:
                p.counter += 1
                if p.counter == self.lsp.max_retransmissions:
                    self._bailout(p)
                    continue
                self._send_data(p)
                self._disarm_send_ack_timer()
                self._rearm_recv_ack_timer(p.timeout)

    def _on_send_ack_timer(self):
        if self.state != STATE_NORMAL:
            return
        self._last_acked_psn = self._last_received_in_sequence_psn
        self._send_ack()

    def _handle_data(self, p: IAP2Packet, deliver: bool = True):
        d = distance(p.psn, self._last_received_in_sequence_psn)
        if d == 0:
            self._send_ack()
            return

        if d > 2:
            # Far out of order: ask for the missing packets.
            missing = []
            sequence = self._last_received_in_sequence_psn
            while distance(p.psn, sequence) > 1:
                sequence = signed_add(sequence, 1)
                missing.append(sequence)
            self._disarm_send_ack_timer()
            self._send_eak(missing)
            return

        # Accept in-order data and a one-packet gap. The gap can be an
        # ACK-only packet that does not need application delivery.
        if deliver:
            self._received_data(p)
        self._last_received_in_sequence_psn = p.psn
        self._last_acked_psn = p.psn
        self._disarm_send_ack_timer()
        self._send_ack()

    def _dispatch_pending_ft(self, tail_bytes):
        """Deliver an FT frame whose true tail arrived before the next FF 5A.

        The iPod's FT length field undercounts (and saturates for frames
        larger than 64 KiB), so after the declared payload the remaining
        frame bytes sit between it and the next frame's 2-byte prefix,
        followed by zero padding. Recover them here; the trailing byte is the
        frame's payload checksum (the iPod computes it non-standardly, so it
        cannot be validated and is dropped).
        """
        base = self._pending_ft
        seq = self._pending_ft_seq
        self._pending_ft = None
        self._pending_ft_seq = None
        if base is None:
            return
        body = tail_bytes[:-2] if len(tail_bytes) >= 2 else b""
        end = len(body)
        while end > 0 and body[end - 1] == 0:
            end -= 1
        content = bytes(base) + bytes(body[:end])
        if content and content[-1] != 0:
            content = content[:-1]
        self._received_data(IAP2Packet(content, seq,
                                       IAP2Connection.FT_SESSION_ID))

    def _received_data(self, p: IAP2Packet):
        if p.session_id == IAP2Connection.CONTROL_SESSION_ID:
            self.control_session.received_data(p.data)
        elif p.session_id == IAP2Connection.EA_SESSION_ID and len(p.data) >= 2:
            stream_id = EA_SESSION_ID_STRUCT.unpack(p.data[:2])[0]
            stream = self.ea_streams.get(stream_id)
            if stream:
                stream.received_data(p.data[2:])
        elif p.session_id == IAP2Connection.FT_SESSION_ID:
            if self.file_transfer_handler:
                self.file_transfer_handler(p.data)
            else:
                print(f"!! FT datagram unhandled: {p.data.hex(' ')}")

    def _disarm_send_ack_timer(self):
        if self._send_ack_timer:
            self._send_ack_timer.cancel()
            self._send_ack_timer = None

    def _rearm_send_ack_timer(self):
        if self._send_ack_timer:
            self._send_ack_timer.cancel()
        self._send_ack_timer = self._loop.call_later(self.lsp.ack_timeout / 1000,
                                                     self._on_send_ack_timer)

    def _disarm_recv_ack_timer(self):
        if self._recv_ack_timer:
            self._recv_ack_timer.cancel()
            self._recv_ack_timer = None

    def _rearm_recv_ack_timer(self, time):
        if self._recv_ack_timer:
            self._recv_ack_timer.cancel()
        self._recv_ack_timer = self._loop.call_at(time, self._on_expect_ack_timer)


def distance(a: int, b: int):
    if b is None:
        return 0
    elif a >= b:
        return a - b
    else:
        return a + 256 - b


def _fix_csm_length(payload):
    """Repair a CSM frame whose length field is one byte short.

    Large MediaLibraryUpdate messages end with a flag parameter whose value
    byte the device does not include in the CSM length (nor the link length).
    The byte has just been recovered; bump the CSM length by one so the
    control-session parser consumes the full body. Returns the patched
    payload, or None if it does not look like a repairable CSM frame.
    """
    if len(payload) < 7 or payload[0:2] != b"\x40\x40":
        return None
    csm_len = int.from_bytes(payload[2:4], "big")
    if csm_len + 1 != len(payload):
        return None
    patched = bytearray(payload)
    patched[2:4] = (csm_len + 1).to_bytes(2, "big")
    return bytes(patched)
