"""The BlackShark V2 Pro (2023) on its 2.4 GHz receiver, 1532:0555 (issue #32).

Its settings are the BlackShark V2 HyperSpeed's -- the same register numbers
(:class:`~.blackshark.Command`) -- but in another envelope: the "PA" requests
and "PI" replies of the Barracuda X family, sent as output report 2 on the
receiver's interface 3, with no checksum and no sequence number::

    02 80 LL 00 00 'P' 'A' FAM 00 BLEN  REG 00 N  data...        LL = 5 + BLEN
    02 LL 'P' 'I' FAM .. .. .. .. 00 BLEN  00 REG OP N  data...  (a reply, OP 01)

Family 0x08 is the headset's registers; family 0x0E register 0xE1 puts the
receiver in "remote" mode, in which the headset answers.  The link to the
headset dozes after half a second with nothing said and drops the first frame
after, so every request is preceded by a remote-mode frame, as every tool that
drives this headset does.

From openrazer/openrazer#2862 (not merged; its driver was tested on the
headset), matej-basic/blackshark-battery (a reply captured from the hardware),
Ashesh3/razer-device-control and AbdullahSAhmad/blackshark-control.  Only facts
are taken from them -- frame layouts and register numbers -- not code.
"""
from __future__ import annotations

from typing import Optional, Sequence

PID_WIRELESS = 0x0555
PRODUCT_IDS = frozenset((PID_WIRELESS,))

REPORT_ID = 0x02
REPORT_SIZE = 64
INTERFACE = 3
USAGE_PAGE = 0xFF00

_HOST = b'PA'
_DEVICE = b'PI'
FAMILY_HEADSET = 0x08
FAMILY_RECEIVER = 0x0E
REMOTE_MODE = 0xE1
WRITE = 0x80
_OP_REPLY = 0x01

#: Reply layout, report id at 0.
_FAMILY = 4
_REGISTER = 12
_OP = 13
_COUNT = 14
_DATA = 15

#: What Synapse offers.  The firmware's own limits are not known, so nothing
#: beyond them is sent.
SIDETONE_MAX = 10
SIDETONE_DEFAULT = 7
IDLE_MINUTES = (15, 30, 45, 60)


def frame(family: int, body: Sequence[int]) -> bytes:
    body = bytes(body)
    report = bytes((REPORT_ID, 0x80, 5 + len(body), 0x00, 0x00)) + _HOST + \
        bytes((family, 0x00, len(body))) + body
    if len(report) > REPORT_SIZE:
        raise ValueError('a frame of {0} bytes does not fit a report'.format(len(report)))
    return report.ljust(REPORT_SIZE, b'\x00')


def remote(on: bool = True) -> bytes:
    """Receiver: pass what follows on to the headset (and wake the link)."""
    return frame(FAMILY_RECEIVER, (REMOTE_MODE, 0x01 if on else 0x00))


def read(register: int) -> bytes:
    return frame(FAMILY_HEADSET, (register, 0x00, 0x00))


def write(register: int, data: Sequence[int]) -> bytes:
    data = bytes(int(value) & 0xFF for value in data)
    return frame(FAMILY_HEADSET, bytes((register | WRITE, 0x00, len(data))) + data)


def reply(report: bytes, register: int) -> Optional[bytes]:
    """What a reply to `register` holds, or ``None`` if `report` is not one."""
    if (len(report) <= _COUNT or report[0] != REPORT_ID or report[2:4] != _DEVICE
            or report[_FAMILY] != FAMILY_HEADSET or report[_REGISTER] != register
            or report[_OP] != _OP_REPLY):
        return None
    count = report[_COUNT]
    return bytes(report[_DATA:_DATA + count])
