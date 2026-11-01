"""The BlackShark V2 HyperSpeed protocol.

This headset is built around a MediaTek chip, and its receiver speaks neither
the 90-byte control report nor the Kraken's RAM writes.  A request is a 64-byte
HID *output* report with id 2, sent to the vendor collection on interface 3.
That interface has no OUT endpoint, so Windows turns it into a SET_REPORT; the
answer comes back as a 64-byte *input* report, matched to the request by its
sequence number and command.

Upstream has no driver for it yet.  The layout and registers were worked out
by justik13 (razer-blackshark-v2-hyperspeed-webhid) and bbesli
(Synapse-for-Linux), both MIT; the status byte and the rules a reply has to
pass were checked on firmware 1.7 in openrazer/openrazer#2974.
"""
from __future__ import annotations

from typing import Sequence

#: The receiver, and the headset itself on its USB cable.
PID_WIRELESS = 0x0565
PID_WIRED = 0x056E
PRODUCT_IDS = frozenset((PID_WIRELESS, PID_WIRED))

REPORT_ID = 0x02
REPORT_SIZE = 64
MAX_PAYLOAD = 48

#: Where the control collection lives.  Interface 3 on usage page 0xFF14 on the
#: firmware that has been looked at; 0xFF00 is reported for others.
INTERFACE = 3
USAGE_PAGE = 0xFF14
OTHER_USAGE_PAGE = 0xFF00

#: Above the five-bit sequence counter, every client sets these bits.
CLIENT = 0x60
SEQUENCE_MASK = 0x1F

#: Who a request is for.  Through the receiver the headset is 0x80 and the
#: receiver itself 0x00; on the cable there is only the headset, at 0x00.
DOMAIN_LOCAL = 0x00
DOMAIN_HEADSET = 0x80

#: Set on a command to write its register rather than read it.
WRITE = 0x80

# Offsets into a report, counting the report id.
_STATUS = 1
_SEQUENCE = 2
_LENGTH = 6
_DIRECTION = 8
_DOMAIN = 9
_COMMAND = 10
_ACK = 11
_COUNT = 12
_PAYLOAD = 13
_CHECKSUM = 62

_FROM_DEVICE = 0x80


class Status:
    """Byte 1 of a reply."""

    BUSY = 0x01
    OK = 0x02
    TIMED_OUT = 0x04
    UNSUPPORTED = 0x05


class Command:
    """The registers, by the id that reads them."""

    SERIAL = 0x00
    FIRMWARE = 0x02
    EQ_PRESET = 0x13
    EQ_BANDS = 0x15
    SIDETONE = 0x18
    SIDETONE_LEVEL = 0x19
    EQ_ENABLED = 0x1E
    BATTERY = 0x21
    DO_NOT_DISTURB = 0x27
    CHARGING = 0x2A
    IDLE_MINUTES = 0x2C
    MIC_MUTED = 0x55
    DONGLE_LED = 0x66


#: Equalizer presets kept in the headset, by name.
EQ_PRESETS = {'flat': 0, 'game': 7, 'music': 8, 'movie': 9, 'custom': 255}
EQ_FREQUENCIES = (31, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)
EQ_MIN_DB = -9
EQ_MAX_DB = 6
#: The firmware keeps gains 5 dB below what it is sent.
_EQ_OFFSET = 5

SIDETONE_MAX = 15
#: The register is a byte of minutes; Synapse only offers up to 60 of them.
IDLE_MINUTES_MAX = 255

DONGLE_LED_MODES = {'off': 0, 'link': 1, 'battery': 2, 'warning': 3}


class ProtocolError(Exception):
    """A reply that says the request failed."""


class Unsupported(ProtocolError):
    """The firmware does not have this command."""


def request(sequence: int, domain: int, command: int,
            payload: Sequence[int] = ()) -> bytes:
    """Build one request, ready to send as an output report."""
    data = bytes(payload)
    if len(data) > MAX_PAYLOAD:
        raise ValueError('request too long: {0} bytes'.format(len(data)))
    report = bytearray(REPORT_SIZE)
    report[0] = REPORT_ID
    report[_SEQUENCE] = CLIENT | (sequence & SEQUENCE_MASK)
    report[_LENGTH] = 4 + len(data)
    report[_DOMAIN] = domain
    report[_COMMAND] = command
    report[_COUNT] = len(data)
    report[_PAYLOAD:_PAYLOAD + len(data)] = data
    report[_CHECKSUM] = _xor(report[:_CHECKSUM])
    return bytes(report)


def _xor(data) -> int:
    checksum = 0
    for byte in data:
        checksum ^= byte
    return checksum


def is_reply(report: bytes) -> bool:
    """Whether `report` is a well-formed reply at all.

    Replies usually leave the report id out of their checksum, but not always,
    so a sum with it is as good as one without.
    """
    if len(report) < REPORT_SIZE or report[0] != REPORT_ID:
        return False
    if report[_DIRECTION] != _FROM_DEVICE or report[_COUNT] > MAX_PAYLOAD:
        return False
    if report[_LENGTH] != 4 + report[_COUNT]:
        return False
    checksum = _xor(report[1:_CHECKSUM])
    return report[_CHECKSUM] in (checksum, checksum ^ REPORT_ID)


def answers(report: bytes, sequence: int, domain: int, command: int,
            wired: bool = False) -> bool:
    """Whether `report` is the reply to that request.

    On the cable, requests go to the local domain but the headset still
    signs its replies 0x80.
    """
    if not is_reply(report) or report[_COMMAND] != command:
        return False
    if report[_SEQUENCE] & SEQUENCE_MASK != sequence & SEQUENCE_MASK:
        return False
    return report[_DOMAIN] == domain or (
        wired and domain == DOMAIN_LOCAL and report[_DOMAIN] == DOMAIN_HEADSET)


def result(report: bytes, command: int) -> bytes:
    """What a reply carries, or the reason it carries nothing."""
    status = report[_STATUS]
    if status == Status.UNSUPPORTED:
        raise Unsupported('command 0x{0:02x} is not supported'.format(command))
    if status == Status.BUSY:
        raise ProtocolError('the headset is busy')
    if status == Status.TIMED_OUT:
        raise ProtocolError('the headset did not answer the receiver')
    if status != Status.OK or report[_ACK] != 1:
        raise ProtocolError('command 0x{0:02x} failed (status 0x{1:02x})'.format(
            command, status))
    payload = bytes(report[_PAYLOAD:_PAYLOAD + report[_COUNT]])
    if command & WRITE and payload != b'\x00':
        # The ACK flag only says it was received; a write that took says 0.
        raise ProtocolError('command 0x{0:02x} was refused'.format(command))
    return payload


def encode_bands(gains: Sequence[int]) -> bytes:
    """Ten gains in dB, the way a write wants them."""
    if len(gains) != len(EQ_FREQUENCIES):
        raise ValueError('the equalizer has {0} bands, not {1}'.format(
            len(EQ_FREQUENCIES), len(gains)))
    for gain in gains:
        if not EQ_MIN_DB <= gain <= EQ_MAX_DB:
            raise ValueError('a band goes from {0} to {1} dB, not {2}'.format(
                EQ_MIN_DB, EQ_MAX_DB, gain))
    return bytes((int(gain) + _EQ_OFFSET) & 0xFF for gain in gains)


def decode_bands(payload: bytes) -> list:
    """Ten gains in dB from a read, which gives back what is stored."""
    if len(payload) != len(EQ_FREQUENCIES):
        raise ProtocolError('expected {0} equalizer bands, got {1}'.format(
            len(EQ_FREQUENCIES), len(payload)))
    return [byte - 256 if byte > 127 else byte for byte in payload]
