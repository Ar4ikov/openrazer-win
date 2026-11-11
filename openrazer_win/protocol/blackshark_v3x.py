"""The BlackShark V3 X protocol.

Not the V2 HyperSpeed's, for all the name: this headset is asked through
64-byte *feature* reports with id 7, and answers the same way.  A request is
written with SET_REPORT and its reply read back with GET_REPORT a moment later,
as with the 90-byte control report, but the layout is its own::

    [0]     report id, 0x07
    [1]     status, 0 on a request
    [2]     transaction id, echoed by the reply
    [6]     how many arguments follow
    [7]     command class
    [8]     command; reading is the command that writes, with 0x80 set
    [9..]   arguments
    [62]    XOR of bytes 3 to 60

Upstream has no driver for it yet.  This follows the one proposed in
openrazer/openrazer#2899 by tolunayozturk, who tested it on both product ids;
its command ids match the notes of everyoneexe (blackshark-linux).
"""
from __future__ import annotations

from typing import Sequence

#: The headset on its cable, and its HyperSpeed receiver.
PID_WIRED = 0x057C
PID_WIRELESS = 0x057D
PRODUCT_IDS = frozenset((PID_WIRED, PID_WIRELESS))

REPORT_ID = 0x07
REPORT_SIZE = 64
#: A reply's arguments end where the checksum's reach does.
MAX_ARGUMENTS = 52

# Offsets into a report, counting the report id.
_STATUS = 1
_TRANSACTION = 2
_COUNT = 6
_CLASS = 7
_COMMAND = 8
_ARGUMENTS = 9
_CHECKSUM = 62

#: Set on a command to read what it would otherwise write.
READ = 0x80

#: What a read sends for arguments.
QUERY = (0x00, 0x00)

#: How long the headset takes to have a reply ready, and how often to ask again.
WAIT_SECONDS = 0.050
ATTEMPTS = 4
#: A setting takes up to 200 ms to commit; read sooner, it gives the old value
#: or nothing.  183 ms was the longest the driver's author measured.
SETTLE_SECONDS = 0.250


class Status:
    """Byte 1 of a reply, as in the 90-byte control report."""

    NOT_RUN = 0x00
    BUSY = 0x01
    OK = 0x02
    FAILED = 0x03
    TIMED_OUT = 0x04
    UNSUPPORTED = 0x05


class Class:
    INFO = 0x00
    POWER = 0x07
    AUDIO = 0x18


class Command:
    """The commands that write; a few can only be read."""

    # Class.INFO
    SERIAL = 0x02
    #: The headset's own firmware.  0x01 is its speaker's, a separate part.
    FIRMWARE = 0x07
    # Class.POWER
    BATTERY = 0x00
    SLEEP = 0x03
    CHARGING = 0x04
    POWER_SAVING = 0x08
    # Class.AUDIO
    EQ_PRESET = 0x00
    EQ_BANDS = 0x01
    SIDETONE = 0x03
    NOISE_CANCELLING = 0x0F


#: Equalizer presets by name.  There is no custom one: each keeps a curve of
#: its own, and writing the bands changes the curve of the one chosen.
EQ_PRESETS = {'default': 0, 'game': 1, 'music': 2, 'movie': 3}
EQ_FREQUENCIES = (31, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)
EQ_MIN_DB = -6
EQ_MAX_DB = 6

SIDETONE_MAX = 15
BATTERY_MAX = 100
SERIAL_LENGTH = 22
#: What a receiver with no headset in reach gives for the sleep timer, and
#: for every other byte it is asked for.
NOT_LINKED = 0xFFFF


class ProtocolError(Exception):
    """A reply that says the request failed."""


class Unsupported(ProtocolError):
    """The firmware does not have this command."""


def checksum(report) -> int:
    value = 0
    for byte in report[3:61]:
        value ^= byte
    return value


def request(transaction: int, command_class: int, command: int,
            arguments: Sequence[int] = ()) -> bytes:
    """Build one request, ready to send as a feature report."""
    data = bytes(arguments)
    if len(data) > MAX_ARGUMENTS:
        raise ValueError('request too long: {0} arguments'.format(len(data)))
    report = bytearray(REPORT_SIZE)
    report[0] = REPORT_ID
    report[_TRANSACTION] = transaction & 0xFF
    report[_COUNT] = len(data)
    report[_CLASS] = command_class
    report[_COMMAND] = command
    report[_ARGUMENTS:_ARGUMENTS + len(data)] = data
    report[_CHECKSUM] = checksum(report)
    return bytes(report)


def answers(report: bytes, sent: bytes, at_least: int = 0) -> bool:
    """Whether `report` is the finished reply to the request `sent`.

    One read too soon is none of these things yet: it is the reply to the
    request before, or has fewer arguments than the answer needs.  A refusal
    is finished whatever it carries.
    """
    if len(report) < REPORT_SIZE or report[0] != REPORT_ID:
        return False
    if report[_TRANSACTION] != sent[_TRANSACTION]:
        return False
    if (report[_CLASS], report[_COMMAND]) != (sent[_CLASS], sent[_COMMAND]):
        return False
    if report[_CHECKSUM] != checksum(report):
        return False
    if report[_STATUS] in (Status.FAILED, Status.TIMED_OUT, Status.UNSUPPORTED):
        return True
    return at_least <= report[_COUNT] <= MAX_ARGUMENTS


def result(report: bytes) -> bytes:
    """The arguments a reply carries, or the reason it carries none."""
    status = report[_STATUS]
    command = report[_COMMAND]
    if status == Status.UNSUPPORTED:
        raise Unsupported('command 0x{0:02x} is not supported'.format(command))
    if status == Status.TIMED_OUT:
        raise ProtocolError('the headset did not answer')
    if status == Status.NOT_RUN:
        # The firmware handed the request straight back without running it.
        raise ProtocolError('command 0x{0:02x} was not run'.format(command))
    # Busy is an answer too: some commands say it and have still done the work.
    if status not in (Status.OK, Status.BUSY):
        raise ProtocolError('command 0x{0:02x} failed (status 0x{1:02x})'.format(
            command, status))
    return bytes(report[_ARGUMENTS:_ARGUMENTS + report[_COUNT]])


def encode_bands(gains: Sequence[int]) -> bytes:
    """Ten gains in dB, the way a write wants them: a sign bit and a size."""
    if len(gains) != len(EQ_FREQUENCIES):
        raise ValueError('the equalizer has {0} bands, not {1}'.format(
            len(EQ_FREQUENCIES), len(gains)))
    for gain in gains:
        if not EQ_MIN_DB <= gain <= EQ_MAX_DB:
            raise ValueError('a band goes from {0} to {1} dB, not {2}'.format(
                EQ_MIN_DB, EQ_MAX_DB, gain))
    return bytes(0x80 | -int(gain) if gain < 0 else int(gain) for gain in gains)


def decode_bands(payload: bytes) -> list:
    """Ten gains in dB from a read."""
    if len(payload) != len(EQ_FREQUENCIES):
        raise ProtocolError('expected {0} equalizer bands, got {1}'.format(
            len(EQ_FREQUENCIES), len(payload)))
    gains = [-(byte & 0x7F) if byte & 0x80 else byte for byte in payload]
    if not all(EQ_MIN_DB <= gain <= EQ_MAX_DB for gain in gains):
        raise ProtocolError('equalizer bands out of range: {0}'.format(gains))
    return gains
