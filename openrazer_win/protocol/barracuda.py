"""The Barracuda X (2022) protocol.

Not a BlackShark's, though the registers will look familiar.  The headset's
USB-C receiver is a tunnel: host and headset pass each other *frames*, cut into
chunks that travel in 64-byte HID reports with id 1 on interface 3.  Each
report is ``01 80 COUNT`` and then COUNT bytes of the stream.  Requests have to
leave through the interface's interrupt OUT endpoint -- a SET_REPORT is not
answered -- and everything the receiver says comes back as input reports::

    host:    50 41 FAMILY SEQ LEN PAYLOAD        (family 0x06: LEN is 16 bits)
    device:  50 49 FAMILY SEQ xx xx xx xx LEN16 DATA

A frame from the device may be longer than one report, and then goes on in the
next without a header of its own.  The receiver acknowledges a request with a
frame of family 1 that echoes its sequence number; what was asked for follows
in a frame numbered by a counter of the receiver's own, so an answer is told
by coming after its acknowledgment.  The headset's registers, family 8, are
answered without one, and only while the receiver has been told to pass
requests on to the headset.

Upstream has no driver for it.  This follows leag/razer-barracuda (MIT), whose
kernel driver and notes worked the tunnel out on the hardware; the layout of
the register frames it took from TarikTopalovic/razer-barracuda-2.4-linux.
Only the default and game presets were tried on a headset there: the rest of
the registers come from Razer's own software and are as yet unproven over USB.
"""
from __future__ import annotations

from typing import Optional, Sequence

#: The receiver; the headset has no data on its own cable.
PID_WIRELESS = 0x0552
PRODUCT_IDS = frozenset((PID_WIRELESS,))

#: Report 2 on the same interface is the play/pause button.
REPORT_ID = 0x01
REPORT_SIZE = 64
INTERFACE = 3

_TUNNEL = 0x80
_CHUNK_MAX = REPORT_SIZE - 3
_HOST = b'\x50\x41'
_DEVICE = b'\x50\x49'

#: A frame from the device: ten bytes, then as many as its last two say.
FRAME_HEADER = 10
FRAME_MAX = 256

#: Sequence numbers go round in this range.
SEQUENCE_FIRST = 0x60
SEQUENCE_LAST = 0x7F


class Family:
    ACK = 0x01
    #: Not asked for anything but how much may be read at once.
    DIAG = 0x06
    #: The headset's registers.
    CUSTOMER = 0x08
    #: The receiver itself.
    LINK = 0x0E


class Link:
    """What the receiver is asked; it answers ``COMMAND VALUE``."""

    GET_ROUTE = 0xE0
    SET_ROUTE = 0xE1
    #: 1 with a headset on and linked, 0 without.
    GET_LINK = 0xE3
    GET_TRANSPORT = 0xE6


#: Where requests for the headset end up: at the receiver, or passed on.
ROUTE_LOCAL = 0x00
ROUTE_REMOTE = 0x01
#: The bit of the transport status that says the headset can be passed things.
TRANSPORT_HEADSET = 0x08

#: Razer's software sends this before it asks for settings, and is told 240.
READ_MAX_LEN = bytes((0x25, 0x34, 0x12, 0x5A, 0x5A, 0x01, 0x00, 0x00, 0x00,
                      0xF0, 0x00, 0x00, 0x00))
READ_MAX_LEN_RESULT = bytes((0xF0, 0x00, 0x00, 0x00))


class Register:
    """The headset's registers, by the id that reads them."""

    EQ_PRESET = 0x13
    EQ_BANDS = 0x15
    LINK = 0x20
    BATTERY = 0x21
    DO_NOT_DISTURB = 0x27
    CHARGING = 0x2A
    IDLE_MINUTES = 0x2C


#: Set on a register to write it rather than read it.
WRITE = 0x80

_OP_GET = 0x00
_OP_REPLY = 0x01
#: What the headset says unasked: its battery, its cable, its link.
_OP_REPORT = 0x02

#: Equalizer presets kept in the headset, by name.
EQ_PRESETS = {'flat': 0, 'game': 7, 'music': 8, 'movie': 9, 'custom': 255}
EQ_FREQUENCIES = (31, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)
#: Steps rather than decibels: nobody has measured what one is worth.
EQ_MIN = -5
EQ_MAX = 5
_EQ_OFFSET = 5

BATTERY_MAX = 100
#: The only sleep timers it takes, in minutes; 0 is never.
IDLE_MINUTES = (0, 5, 15, 30, 45, 60)


class ProtocolError(Exception):
    """A reply that says the request failed."""


def next_sequence(sequence: int) -> int:
    """The sequence number after `sequence`."""
    if sequence < SEQUENCE_FIRST or sequence >= SEQUENCE_LAST:
        return SEQUENCE_FIRST
    return sequence + 1


def request(family: int, sequence: int, payload: Sequence[int]) -> bytes:
    """Build one request, ready to write to the interrupt endpoint."""
    data = bytes(payload)
    # The one family with a length of two bytes starts its payload a byte later.
    start = 9 if family == Family.DIAG else 8
    if start + len(data) > REPORT_SIZE:
        raise ValueError('request too long: {0} bytes'.format(len(data)))
    report = bytearray(REPORT_SIZE)
    report[0] = REPORT_ID
    report[1] = _TUNNEL
    report[2] = start + len(data) - 3
    report[3:5] = _HOST
    report[5] = family
    report[6] = sequence & 0xFF
    report[7] = len(data)
    report[start:start + len(data)] = data
    return bytes(report)


def get(register: int) -> bytes:
    """The payload that reads a register."""
    return bytes((register, _OP_GET, 0x00))


def put(register: int, value: Sequence[int]) -> bytes:
    """The payload that writes one."""
    data = bytes(value)
    return bytes((register | WRITE, 0x00, len(data))) + data


class Reassembler:
    """Puts the frames back together from the reports they arrive in.

    Other reports, the play/pause button's for one, may come in between two
    halves of a frame and are let by.  After anything that cannot be right the
    stream starts again at the next report, rather than be searched for
    something that looks like a header.
    """

    def __init__(self):
        self._frame = bytearray()
        self._expected = 0

    def reset(self) -> None:
        self._frame.clear()
        self._expected = 0

    def feed(self, report: bytes) -> list:
        """Take one input report; return the frames it completed."""
        frames: list = []
        if not report or report[0] != REPORT_ID:
            return frames
        if (len(report) < 3 or report[1] != _TUNNEL or report[2] > _CHUNK_MAX
                or len(report) < 3 + report[2]):
            self.reset()
            return frames
        for byte in report[3:3 + report[2]]:
            self._frame.append(byte)
            used = len(self._frame)
            if used <= 2 and byte != _DEVICE[used - 1]:
                self.reset()
                return frames
            if used == FRAME_HEADER:
                self._expected = FRAME_HEADER + self._frame[8] + (self._frame[9] << 8)
                if self._expected > FRAME_MAX:
                    self.reset()
                    return frames
            if used >= FRAME_HEADER and used == self._expected:
                frames.append(bytes(self._frame))
                self.reset()
        return frames


def ack(frame: bytes, family: int, sequence: int) -> Optional[bytes]:
    """What the acknowledgment of that request carries; ``None`` if `frame` is not it."""
    if (len(frame) < 13 or frame[2] != Family.ACK or frame[10] != family
            or frame[11] != (sequence | 0x80) & 0xFF):
        return None
    if frame[12]:
        raise ProtocolError('the receiver refused the request (status 0x{0:02x})'.format(
            frame[12]))
    return bytes(frame[13:])


def link_reply(frame: bytes, command: int) -> Optional[int]:
    """The value the receiver gives for `command`; ``None`` if `frame` is not that."""
    if (len(frame) != 12 or frame[2] != Family.LINK or frame[8] != 2 or frame[9] != 0
            or frame[10] != command):
        return None
    return frame[11]


def register_reply(frame: bytes, register: int) -> Optional[bytes]:
    """What the headset answers for `register`; ``None`` if `frame` is not that.

    `register` with :data:`WRITE` set is the answer to a write.  What the
    headset reports unasked has another byte where a reply has 1, and is never
    taken for one: it may be older than the question.
    """
    if (len(frame) < 13 or frame[2] != Family.CUSTOMER or frame[9] != 0
            or frame[10] != register or frame[11] != _OP_REPLY):
        return None
    length = frame[12]
    if len(frame) != 13 + length or frame[8] != 3 + length:
        return None
    return bytes(frame[13:])


def notification(frame: bytes) -> Optional[tuple]:
    """``(register, value)`` for what the headset reports unasked, else ``None``."""
    if (len(frame) != 14 or frame[2] != Family.CUSTOMER or frame[8] != 4 or frame[9] != 0
            or frame[11] != _OP_REPORT or frame[12] != 1):
        return None
    return frame[10], frame[13]


def encode_bands(gains: Sequence[int]) -> bytes:
    """Ten bands, the way a write wants them: 0 to 10, with 5 in the middle."""
    if len(gains) != len(EQ_FREQUENCIES):
        raise ValueError('the equalizer has {0} bands, not {1}'.format(
            len(EQ_FREQUENCIES), len(gains)))
    for gain in gains:
        if not EQ_MIN <= gain <= EQ_MAX:
            raise ValueError('a band goes from {0} to {1}, not {2}'.format(
                EQ_MIN, EQ_MAX, gain))
    return bytes(int(gain) + _EQ_OFFSET for gain in gains)


def decode_bands(payload: bytes) -> list:
    """Ten bands from a read."""
    if len(payload) != len(EQ_FREQUENCIES):
        raise ProtocolError('expected {0} equalizer bands, got {1}'.format(
            len(EQ_FREQUENCIES), len(payload)))
    if any(byte > EQ_MAX + _EQ_OFFSET for byte in payload):
        raise ProtocolError('equalizer bands out of range: {0}'.format(list(payload)))
    return [byte - _EQ_OFFSET for byte in payload]
