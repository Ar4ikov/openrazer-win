"""The 90-byte Razer control report.

Every Razer peripheral speaks the same vendor protocol: a fixed 90-byte
structure delivered as HID feature report 0x00 over the control endpoint.  This
module is a direct translation of ``struct razer_report`` from
``driver/razercommon.h`` in upstream OpenRazer.

::

    offset  size  field
    0       1     status
    1       1     transaction_id
    2       2     remaining_packets (big endian)
    4       1     protocol_type (always 0)
    5       1     data_size (payload length, <= 80)
    6       1     command_class
    7       1     command_id (bit 7 set = device -> host)
    8       80    arguments
    88      1     crc (XOR of bytes 2..87)
    89      1     reserved
"""
from __future__ import annotations

import struct
from typing import Optional

REPORT_SIZE = 90
ARGUMENT_SIZE = 80

#: Razer's USB vendor id.  Every supported device uses it.
VENDOR_ID = 0x1532

_STRUCT = struct.Struct('>BBHBBBB80sBB')
assert _STRUCT.size == REPORT_SIZE


class Status:
    """Values of the ``status`` byte in a device response."""

    NEW_COMMAND = 0x00
    BUSY = 0x01
    SUCCESSFUL = 0x02
    FAILURE = 0x03
    TIMEOUT = 0x04
    NOT_SUPPORTED = 0x05

    NAMES = {
        0x00: 'new command',
        0x01: 'busy',
        0x02: 'successful',
        0x03: 'failure',
        0x04: 'timeout',
        0x05: 'not supported',
    }

    @classmethod
    def name(cls, value: int) -> str:
        return cls.NAMES.get(value, 'unknown (0x{0:02x})'.format(value))


class VarStore:
    """Whether the device should persist the setting across power cycles."""

    NOSTORE = 0x00
    VARSTORE = 0x01


class Led:
    """LED identifiers, from ``razercommon.h``."""

    ZERO = 0x00
    SCROLL_WHEEL = 0x01
    BATTERY = 0x03
    LOGO = 0x04
    BACKLIGHT = 0x05
    MACRO = 0x07
    GAME = 0x08
    RED_PROFILE = 0x0C
    GREEN_PROFILE = 0x0D
    BLUE_PROFILE = 0x0E
    RIGHT_SIDE = 0x10
    LEFT_SIDE = 0x11
    ARGB_CH_1 = 0x1A
    ARGB_CH_2 = 0x1B
    ARGB_CH_3 = 0x1C
    ARGB_CH_4 = 0x1D
    ARGB_CH_5 = 0x1E
    ARGB_CH_6 = 0x1F
    CHARGING = 0x20
    FAST_CHARGING = 0x21
    FULLY_CHARGED = 0x22


class ClassicEffect:
    STATIC = 0x00
    BLINKING = 0x01
    BREATHING = 0x02
    SPECTRUM = 0x04


class MatrixEffect:
    OFF = 0x00
    WAVE = 0x01
    REACTIVE = 0x02
    BREATHING = 0x03
    SPECTRUM = 0x04
    CUSTOMFRAME = 0x05
    STATIC = 0x06
    STARLIGHT = 0x19


class RazerReportError(Exception):
    """A device replied with a non-success status, or did not reply at all."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class RazerReport:
    """A mutable 90-byte control report."""

    __slots__ = ('status', 'transaction_id', 'remaining_packets', 'protocol_type',
                 'data_size', 'command_class', 'command_id', 'arguments', 'reserved')

    def __init__(self, command_class: int = 0x00, command_id: int = 0x00,
                 data_size: int = 0x00):
        self.status = 0x00
        self.transaction_id = 0x00
        self.remaining_packets = 0x0000
        self.protocol_type = 0x00
        self.data_size = data_size
        self.command_class = command_class
        self.command_id = command_id
        self.arguments = bytearray(ARGUMENT_SIZE)
        self.reserved = 0x00

    # -- serialisation -----------------------------------------------------
    def pack(self) -> bytes:
        """Serialise the report, computing the CRC over bytes 2..87."""
        body = _STRUCT.pack(
            self.status & 0xFF,
            self.transaction_id & 0xFF,
            self.remaining_packets & 0xFFFF,
            self.protocol_type & 0xFF,
            self.data_size & 0xFF,
            self.command_class & 0xFF,
            self.command_id & 0xFF,
            bytes(self.arguments).ljust(ARGUMENT_SIZE, b'\x00')[:ARGUMENT_SIZE],
            0x00,
            self.reserved & 0xFF,
        )
        crc = calculate_crc(body)
        return body[:88] + bytes((crc, self.reserved & 0xFF))

    @classmethod
    def unpack(cls, raw: bytes) -> 'RazerReport':
        if len(raw) < REPORT_SIZE:
            raise ValueError(
                'report too short: {0} bytes, expected {1}'.format(len(raw), REPORT_SIZE))
        (status, transaction_id, remaining, protocol_type, data_size,
         command_class, command_id, arguments, _crc, reserved) = _STRUCT.unpack(
            raw[:REPORT_SIZE])
        report = cls(command_class, command_id, data_size)
        report.status = status
        report.transaction_id = transaction_id
        report.remaining_packets = remaining
        report.protocol_type = protocol_type
        report.arguments = bytearray(arguments)
        report.reserved = reserved
        return report

    # -- convenience -------------------------------------------------------
    @property
    def payload(self) -> bytes:
        """The ``data_size`` leading argument bytes."""
        return bytes(self.arguments[:min(self.data_size, ARGUMENT_SIZE)])

    def set_argument(self, index: int, value: int) -> None:
        if 0 <= index < ARGUMENT_SIZE:
            self.arguments[index] = value & 0xFF

    def set_arguments(self, index: int, values) -> None:
        data = bytes(values)
        end = min(index + len(data), ARGUMENT_SIZE)
        self.arguments[index:end] = data[:end - index]

    def matches(self, other: 'RazerReport') -> bool:
        """True when `other` is a plausible response to this request."""
        return (other.remaining_packets == self.remaining_packets
                and other.command_class == self.command_class
                and other.command_id == self.command_id)

    def describe(self) -> str:
        args = ' '.join('{0:02x}'.format(b) for b in self.arguments[:16])
        return ('status={0} txid=0x{1:02x} class=0x{2:02x} id=0x{3:02x} '
                'size={4} args={5}').format(
            Status.name(self.status), self.transaction_id, self.command_class,
            self.command_id, self.data_size, args)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return '<RazerReport {0}>'.format(self.describe())

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RazerReport):
            return NotImplemented
        return self.pack() == other.pack()


def calculate_crc(data: bytes) -> int:
    """XOR bytes 2..87 inclusive, exactly like ``razer_calculate_crc``."""
    crc = 0
    for byte in data[2:88]:
        crc ^= byte
    return crc & 0xFF


def get_razer_report(command_class: int, command_id: int, data_size: int) -> RazerReport:
    """Mirror of ``get_razer_report`` from the kernel driver."""
    return RazerReport(command_class, command_id, data_size)


def clamp(value: int, low: int, high: int) -> int:
    """The kernel's ``clamp()`` macro."""
    return low if value < low else (high if value > high else value)


class RGB(tuple):
    """A 3-tuple of 0..255 channel values."""

    __slots__ = ()

    def __new__(cls, red: int = 0, green: int = 0, blue: int = 0):
        return super().__new__(cls, (red & 0xFF, green & 0xFF, blue & 0xFF))

    @property
    def r(self) -> int:
        return self[0]

    @property
    def g(self) -> int:
        return self[1]

    @property
    def b(self) -> int:
        return self[2]

    @classmethod
    def from_bytes(cls, data: bytes, offset: int = 0) -> 'RGB':
        chunk = bytes(data[offset:offset + 3]).ljust(3, b'\x00')
        return cls(chunk[0], chunk[1], chunk[2])
