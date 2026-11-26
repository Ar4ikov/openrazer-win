"""An in-process Razer device emulator.

Used by the test suite and by ``openrazer-win --demo``, so the daemon, client
library, CLI and tray can all be exercised end to end without hardware.  The
emulator implements enough of the vendor protocol to be indistinguishable from
a real device at the transport level: it validates the CRC, echoes the command
class and id back, and keeps per-LED state so reads return what was written.
"""
from __future__ import annotations

import itertools
import queue
from typing import Optional

from ..protocol import barracuda, blackshark, blackshark_v3x
from ..protocol.report import REPORT_SIZE, RazerReport, Status, calculate_crc
from .base import HidDeviceInfo, HidError

_SERIAL_COUNTER = itertools.count(1)


class FakeHeadset:
    """The registers of a BlackShark V2 HyperSpeed, and its way of answering."""

    def __init__(self, serial: str, wired: bool):
        self.serial = serial
        self.wired = wired
        #: Off means the receiver is there and the headset does not answer it.
        self.switched_on = True
        self.registers = {
            blackshark.Command.FIRMWARE: bytes((1, 7, 0, 0)),
            blackshark.Command.EQ_PRESET: b'\x00',
            blackshark.Command.EQ_BANDS: bytes(10),
            blackshark.Command.SIDETONE: b'\x01',
            blackshark.Command.SIDETONE_LEVEL: b'\x05',
            blackshark.Command.EQ_ENABLED: b'\x01',
            blackshark.Command.BATTERY: b'\x55',
            blackshark.Command.DO_NOT_DISTURB: b'\x00',
            blackshark.Command.CHARGING: b'\x01' if wired else b'\x00',
            blackshark.Command.IDLE_MINUTES: b'\x1e',
            blackshark.Command.MIC_MUTED: b'\x00',
            blackshark.Command.DONGLE_LED: b'\x01',
        }
        #: Every request, for a test to look at the order they came in.
        self.requests: list = []
        #: Set to a status to make every command fail with it.
        self.force_status: Optional[int] = None

    def answer(self, data: bytes) -> Optional[bytes]:
        """The input report `data` draws, or ``None`` if nothing would reply."""
        if len(data) != blackshark.REPORT_SIZE or data[0] != blackshark.REPORT_ID:
            raise HidError('bad BlackShark report')
        checksum = 0
        for byte in data[:62]:
            checksum ^= byte
        if data[62] != checksum:
            return None         # the microcontroller drops these without a word
        domain, command, count = data[9], data[10], data[12]
        payload = bytes(data[13:13 + count])
        self.requests.append((domain, command, payload))
        local = domain == blackshark.DOMAIN_LOCAL
        if not self.wired and not local and not self.switched_on:
            return None

        if self.force_status is not None:
            status, body = self.force_status, b'\x00'
        elif command & blackshark.WRITE:
            status, body = self._write(command & ~blackshark.WRITE, payload)
        else:
            status, body = self._read(command)
        reply = bytearray(blackshark.REPORT_SIZE)
        reply[0] = blackshark.REPORT_ID
        reply[1] = status
        reply[2] = data[2]
        reply[6] = 4 + len(body)
        reply[8] = 0x80
        # On the cable the headset signs its replies as it would on the radio.
        reply[9] = blackshark.DOMAIN_HEADSET if self.wired else domain
        reply[10] = command
        reply[11] = 1
        reply[12] = len(body)
        reply[13:13 + len(body)] = body
        for byte in reply[1:62]:
            reply[62] ^= byte
        return bytes(reply)

    def _read(self, command: int) -> tuple:
        if command == blackshark.Command.SERIAL:
            return blackshark.Status.OK, self.serial.encode('ascii')[:16].ljust(16, b'\x00')
        if command == blackshark.Command.DONGLE_LED and self.wired:
            return blackshark.Status.UNSUPPORTED, b''
        if command in self.registers:
            return blackshark.Status.OK, self.registers[command]
        return blackshark.Status.UNSUPPORTED, b''

    def _write(self, command: int, payload: bytes) -> tuple:
        if command not in self.registers:
            return blackshark.Status.UNSUPPORTED, b'\x01'
        if command == blackshark.Command.EQ_BANDS:
            payload = bytes((byte - 5) & 0xFF for byte in payload)
        self.registers[command] = payload
        return blackshark.Status.OK, b'\x00'


class FakeHeadsetV3X:
    """What a BlackShark V3 X keeps, and its way of answering."""

    def __init__(self, serial: str, wired: bool):
        self.serial = serial
        self.wired = wired
        #: Off means the receiver is there with no headset to pass things on to.
        self.switched_on = True
        self.firmware = (1, 0, 14)
        self.battery = 85
        self.charging = wired
        self.idle_seconds = 600
        self.power_saving = False
        self.sidetone = 0
        self.noise_cancelling = False
        self.preset = 0
        #: Each preset keeps a curve of its own.
        self.curves = {preset: [0] * len(blackshark_v3x.EQ_FREQUENCIES)
                       for preset in blackshark_v3x.EQ_PRESETS.values()}
        #: Every request, as (class, command, arguments), in the order they came.
        self.requests: list = []
        #: What a receiver took for a headset that was off, and never passed on.
        self.lost: list = []
        #: Set to a status to make every command answer with it.
        self.force_status: Optional[int] = None
        #: How many reads come too soon, and get the reply before this one.
        self.slow = 0
        self._reply = bytes(blackshark_v3x.REPORT_SIZE)
        self._previous = self._reply

    def set_feature(self, data: bytes) -> None:
        if len(data) != blackshark_v3x.REPORT_SIZE or data[0] != blackshark_v3x.REPORT_ID:
            raise HidError('bad BlackShark V3 X report')
        if data[62] != blackshark_v3x.checksum(data):
            return              # dropped without a word
        command_class, command = data[7], data[8]
        arguments = bytes(data[9:9 + data[6]])
        self.requests.append((command_class, command, arguments))
        status, body = self._run(command_class, command, arguments)
        reply = bytearray(blackshark_v3x.REPORT_SIZE)
        reply[0] = blackshark_v3x.REPORT_ID
        reply[1] = status
        reply[2] = data[2]
        reply[6] = len(body)
        reply[7] = command_class
        reply[8] = command
        reply[9:9 + len(body)] = body
        reply[62] = blackshark_v3x.checksum(reply)
        self._previous, self._reply = self._reply, bytes(reply)

    def get_feature(self, length: int, report_id: int) -> bytes:
        if report_id != blackshark_v3x.REPORT_ID:
            raise HidError('unexpected report id 0x{0:02x}'.format(report_id))
        if self.slow:
            self.slow -= 1
            return self._previous[:length]
        return self._reply[:length]

    def _run(self, command_class: int, command: int, arguments: bytes) -> tuple:
        status = blackshark_v3x.Status
        if self.force_status is not None:
            return self.force_status, b''
        reading = bool(command & blackshark_v3x.READ)
        command &= ~blackshark_v3x.READ
        linked = self.wired or self.switched_on
        if command_class == blackshark_v3x.Class.INFO:
            # The receiver answers these itself, with zeroes for a headset it
            # has not heard from.
            answers = {
                blackshark_v3x.Command.SERIAL: self.serial.encode('ascii')[:22].ljust(22, b'\x00'),
                blackshark_v3x.Command.FIRMWARE: bytes(self.firmware) + b'\x00',
            }
            if not reading or command not in answers:
                return status.UNSUPPORTED, b''
            return status.OK, answers[command] if linked else bytes(len(answers[command]))
        if reading:
            body = self._read(command_class, command)
            if body is None:
                return status.UNSUPPORTED, b''
            # Well formed and all 0xFF is what a headset out of reach looks like.
            return status.OK, body if linked else b'\xff' * len(body)
        if not linked:
            self.lost.append((command_class, command, arguments))
            return status.OK, arguments
        if not self._write(command_class, command, arguments):
            return status.UNSUPPORTED, b''
        return status.OK, arguments

    def _read(self, command_class: int, command: int) -> Optional[bytes]:
        cls, cmd = blackshark_v3x.Class, blackshark_v3x.Command
        return {
            (cls.POWER, cmd.BATTERY): bytes((0, self.battery)),
            (cls.POWER, cmd.CHARGING): bytes((0, self.charging)),
            (cls.POWER, cmd.SLEEP): bytes((self.idle_seconds >> 8, self.idle_seconds & 0xFF)),
            (cls.POWER, cmd.POWER_SAVING): bytes((0, self.power_saving)),
            (cls.AUDIO, cmd.EQ_PRESET): bytes((0, 1, 0, self.preset)),
            (cls.AUDIO, cmd.EQ_BANDS): bytes((0, 1)) + blackshark_v3x.encode_bands(
                self.curves[self.preset]),
            (cls.AUDIO, cmd.SIDETONE): bytes((0, 3, self.sidetone)),
            (cls.AUDIO, cmd.NOISE_CANCELLING): bytes((0, self.noise_cancelling)),
        }.get((command_class, command))

    def _write(self, command_class: int, command: int, arguments: bytes) -> bool:
        cls, cmd = blackshark_v3x.Class, blackshark_v3x.Command
        key = (command_class, command)
        if key == (cls.POWER, cmd.SLEEP):
            seconds = (arguments[0] << 8) | arguments[1]
            if seconds < 300:
                return False        # nothing under five minutes, and no "never"
            self.idle_seconds = seconds
        elif key == (cls.POWER, cmd.POWER_SAVING):
            self.power_saving = bool(arguments[1])
        elif key == (cls.AUDIO, cmd.EQ_PRESET):
            self.preset = arguments[3]
        elif key == (cls.AUDIO, cmd.EQ_BANDS):
            self.curves[self.preset] = blackshark_v3x.decode_bands(arguments[2:12])
        elif key == (cls.AUDIO, cmd.SIDETONE):
            self.sidetone = arguments[2]
        elif key == (cls.AUDIO, cmd.NOISE_CANCELLING):
            self.noise_cancelling = bool(arguments[1])
        else:
            return False
        return True


def _onboard_seed() -> dict:
    """Onboard memory as read from a real Basilisk V3 (fw 1.x, 2026-11-16).

    Bindings are ``(profile, button, layer) -> (fn, size, data)``.  Profile 0 is
    the live copy of the active one, so every profile starts out the same.
    """
    buttons = [1, 2, 3, 5, 4, 0x34, 0x35, 0x60, 0x0F, 0x0E, 0x6A, 9, 10]
    special = {
        (3, 1): (0x03, 3, (0x16, 0x15, 0x01)),
        (0x34, 0): (0x03, 3, (0x3C, 0xAE, 0x01)), (0x34, 1): (0x0E, 3, (0x68, 0x00, 0x14)),
        (0x35, 0): (0x03, 3, (0x03, 0x6D, 0x01)), (0x35, 1): (0x0E, 3, (0x69, 0x00, 0x14)),
        (0x60, 0): (0x06, 1, (0x06,)), (0x60, 1): (0x06, 1, (0x06,)),
        (0x0F, 0): (0x02, 2, (0x00, 0xE0)), (0x0F, 1): (0x06, 5, (0x05, 0x01, 0x90, 0x01, 0x90)),
        (0x0E, 0): (0x07, 1, (0x04,)), (0x0E, 1): (0x07, 1, (0x04,)),
        (0x6A, 0): (0x12, 1, (0x01,)), (0x6A, 1): (0x12, 1, (0x01,)),
    }
    bindings = {}
    for profile in range(5):
        for button in buttons:
            for layer in (0, 1):
                fn, size, data = special.get((button, layer), (0x01, 1, (button,)))
                bindings[(profile, button, layer)] = (fn, size, bytes(data).ljust(5, b'\0'))
    macros = {  # id -> (name, event bytes), as the same mouse stores them
        0x1615: ('Macro 6', bytes.fromhex('01e3012b022b02e31114')),
        0x3CAE: ('Macro 2', bytes.fromhex('01e001e30150025002e3110302e01114')),
        0x036D: ('Macro 4', bytes.fromhex('01e001e3014f024f02e3110302e01114')),
    }
    return {'max': 5, 'active': 1, 'profiles': [1, 2, 3, 4],
            'names': {2: '1', 3: '2', 4: 'TEJAS-LOQ-Default'},
            'buttons': buttons, 'bindings': bindings, 'macros': macros}


def _single_profile_seed() -> dict:
    """Onboard memory of a mouse with no profile button: the Viper Ultimate.

    It lists no profiles and cannot say which is active; its bindings are
    profile 0 alone (issue #24, as the reporter's unit answers).
    """
    buttons = [1, 2, 3, 4, 5, 6, 7, 0x60, 9, 10]
    # The right-hand pair does what the left-hand one does: back and forward.
    does = {6: (0x01, 4), 7: (0x01, 5), 0x60: (0x06, 6)}
    bindings = {}
    for button in buttons:
        fn, arg = does.get(button, (0x01, button))
        for layer in (0, 1):
            bindings[(0, button, layer)] = (fn, 1, bytes((arg,)).ljust(5, b'\0'))
    return {'max': 0, 'active': None, 'profiles': [], 'names': {},
            'buttons': buttons, 'bindings': bindings, 'macros': {}}


#: Products whose onboard memory has no profile list.
SINGLE_PROFILE_PIDS = frozenset({0x007A, 0x007B})

#: Mice that ignore the plain DPI command and only go by the DPI stages of
#: their active profile: the Basilisk V4 Pro, wired and on its receiver
#: (openrazer/openrazer#2975).  A DPI write leaves them as they were, and the
#: DPI they report is the active stage's.
STAGE_DPI_PIDS = frozenset({0x00E2, 0x00E3})


#: Onboard-memory commands (class 0x02 bindings, 0x05 profiles, 0x06 macro reads).
ONBOARD_COMMANDS = frozenset(((0x02, 0x84), (0x02, 0x8C), (0x02, 0x0C),
                              (0x05, 0x81), (0x05, 0x84), (0x05, 0x88), (0x05, 0x04),
                              (0x06, 0x8B), (0x06, 0x8C), (0x06, 0x88), (0x06, 0x89)))


class FakeBlackSharkV2Pro:
    """A BlackShark V2 Pro (2023)'s receiver and the headset behind it (issue #32).

    "PA" frames in, "PI" replies out, written through the interrupt endpoint.
    The headset answers only once the receiver is in remote mode, as the real
    one does after its link dozes.
    """

    def __init__(self):
        from ..protocol import blackshark_v2pro as v2pro
        self.protocol = v2pro
        #: Off means the receiver is there and the headset does not answer it.
        self.on = True
        self.remote = False
        self.registers = {0x21: 64, 0x2A: 0, 0x2C: 15, 0x18: 0, 0x19: 7, 0x27: 0, 0x55: 0,
                          0x02: 2}
        self.frames: list = []

    def write(self, data: bytes) -> list:
        v2pro = self.protocol
        self.frames.append(bytes(data))
        if data[5:7] != b'PA':
            return []
        family, length = data[7], data[9]
        body = data[10:10 + length]
        if family == v2pro.FAMILY_RECEIVER and body[:1] == bytes((v2pro.REMOTE_MODE,)):
            self.remote = body[1:2] == b'\x01'
            return []
        if family != v2pro.FAMILY_HEADSET or not self.remote or not self.on:
            return []
        register = body[0]
        if register & v2pro.WRITE:
            count = body[2]
            if count:
                self.registers[register & ~v2pro.WRITE] = body[3]
            return [self._reply(register, b'\x00')]
        value = self.registers.get(register)
        if value is None:
            return []
        return [self._reply(register, bytes((value,)) if register != 0x02 else bytes((2, 6)) + bytes(22))]

    @staticmethod
    def _reply(register: int, data: bytes) -> bytes:
        body = bytes((0x00, register, 0x01, len(data))) + data
        report = bytes((0x02, 13 + len(data), 0x50, 0x49, 0x08, 0xCE, 0x2D, 0x83, 0x03, 0x00,
                        len(body) - 1)) + body
        return report.ljust(64, b'\x00')


class FakeBarracuda:
    """A Barracuda X's receiver, the headset behind it, and their way of answering."""

    #: The play/pause button going down, on the report it has to itself.
    MEDIA_PRESS = bytes((0x02, 0x00, 0x02, 0x00, 0x00))
    #: The transport status with a headset linked, as one gave it.
    TRANSPORT = 0x1B

    def __init__(self, emit=None):
        #: Where what it says unasked goes: the queue of input reports.
        self._emit = emit or (lambda report: None)
        self._switched_on = True
        #: Where requests for the headset go; they are only passed on at 1.
        self.route = barracuda.ROUTE_LOCAL
        register = barracuda.Register
        self.registers = {
            register.EQ_PRESET: b'\x00',
            register.EQ_BANDS: bytes((5,)) * 10,
            register.BATTERY: b'\x55',
            register.DO_NOT_DISTURB: b'\x00',
            register.CHARGING: b'\x00',
            register.IDLE_MINUTES: b'\x0f',
        }
        #: Every request, as (family, payload), in the order they came.
        self.requests: list = []
        #: Cut every frame of a reply in two, across two input reports.
        self.split = False
        #: Input reports that arrive ahead of every reply, and between the two
        #: halves of one that is split.  Only another report id can come in the
        #: middle of a frame; a notification there would be part of it.
        self.before_reply: list = []
        self.inside_reply: list = []
        #: Requests that draw nothing at all, as (family, first payload byte).
        self.silent: set = set()
        #: Registers whose writes are taken, answered as done, and not kept.
        self.forgetful: set = set()
        #: Told to change the route, the receiver says yes and does not.
        self.stuck_route = False
        #: What the read length is answered with: 240, as the reference saw,
        #: or 0, as a receiver on Windows said (issue #21).
        self.read_length_result = barracuda.READ_MAX_LEN_RESULT
        #: Set to a status to have every request acknowledged with it.
        self.force_status: Optional[int] = None
        #: The receiver numbers what it sends itself.
        self._counter = 0xC0

    # -- the headset ---------------------------------------------------------
    @property
    def switched_on(self) -> bool:
        """Off means the receiver is there with no headset linked to it."""
        return self._switched_on

    @switched_on.setter
    def switched_on(self, value: bool) -> None:
        value = bool(value)
        if value == self._switched_on:
            return
        self._switched_on = value
        # The receiver says so at once, twice over.
        self._emit(self.report(self._link_frame(barracuda.Link.GET_LINK, int(value))))
        self._emit(self.report(self.notification(barracuda.Register.LINK, int(value))))

    # -- frames --------------------------------------------------------------
    def _frame(self, family: int, data: bytes) -> bytes:
        self._counter = (self._counter + 1) & 0xFF
        return (bytes((0x50, 0x49, family, self._counter, 0, 0, 0, 0,
                       len(data) & 0xFF, len(data) >> 8)) + data)

    def _link_frame(self, command: int, value: int) -> bytes:
        return self._frame(barracuda.Family.LINK, bytes((command, value)))

    def notification(self, register: int, value: int) -> bytes:
        """The frame the headset sends unasked when `register` changes."""
        return self._frame(barracuda.Family.CUSTOMER, bytes((register, 0x02, 0x01, value)))

    @staticmethod
    def report(chunk: bytes) -> bytes:
        """One input report carrying `chunk` of the stream."""
        return (bytes((barracuda.REPORT_ID, 0x80, len(chunk))) + chunk).ljust(
            barracuda.REPORT_SIZE, b'\x00')

    def _reports(self, frames: list) -> list:
        reports = list(self.before_reply) if frames else []
        for frame in frames:
            if self.split:
                # Inside the header, where a careless reader would trip.
                reports.append(self.report(frame[:7]))
                reports.extend(self.inside_reply)
                reports.append(self.report(frame[7:]))
            else:
                reports.append(self.report(frame))
        return reports

    # -- answering -----------------------------------------------------------
    def answer(self, data: bytes) -> None:
        """A SET_REPORT, which the real receiver lets go by unanswered."""
        raise HidError('the Barracuda X takes its reports on the interrupt endpoint')

    def write(self, data: bytes) -> list:
        """The input reports that `data`, written to the interrupt endpoint, draws."""
        if (len(data) != barracuda.REPORT_SIZE or data[0] != barracuda.REPORT_ID
                or data[1] != 0x80 or data[3:5] != b'\x50\x41'):
            raise HidError('bad Barracuda report')
        family, sequence = data[5], data[6]
        start = 9 if family == barracuda.Family.DIAG else 8
        length = data[7] | (data[8] << 8 if start == 9 else 0)
        if data[2] != start + length - 3:
            raise HidError('bad Barracuda report length')
        payload = bytes(data[start:start + length])
        self.requests.append((family, payload))
        if not payload or (family, payload[0]) in self.silent:
            return []
        if family == barracuda.Family.LINK:
            return self._reports(self._link(sequence, payload))
        if family == barracuda.Family.DIAG:
            good = payload == barracuda.READ_MAX_LEN
            return self._reports([self._ack(family, sequence, 0 if good else 1,
                                            self.read_length_result if good else b'')])
        if family == barracuda.Family.CUSTOMER:
            return self._reports(self._customer(payload))
        return []

    def _ack(self, family: int, sequence: int, status: int = 0, result: bytes = b'') -> bytes:
        if self.force_status is not None:
            status, result = self.force_status, b''
        return self._frame(barracuda.Family.ACK,
                           bytes((family, sequence | 0x80, status)) + result)

    def _link(self, sequence: int, payload: bytes) -> list:
        link = barracuda.Link
        command = payload[0]
        frames = [self._ack(barracuda.Family.LINK, sequence)]
        if self.force_status:
            return frames
        if command == link.GET_LINK:
            value = int(self._switched_on)
        elif command == link.GET_TRANSPORT:
            value = self.TRANSPORT if self._switched_on else 0x00
        elif command == link.GET_ROUTE:
            value = self.route
        elif command == link.SET_ROUTE and len(payload) == 2:
            if not self.stuck_route:
                self.route = payload[1]
            value = payload[1]
        else:
            return frames
        frames.append(self._link_frame(command, value))
        return frames

    def _customer(self, payload: bytes) -> list:
        # Nothing answers unless the receiver passes it on and there is a
        # headset to pass it to; and the headset is never acknowledged.
        if self.route != barracuda.ROUTE_REMOTE or not self._switched_on:
            return []
        if len(payload) < 3 or payload[1] != 0 or len(payload) != 3 + payload[2]:
            return []
        register, value = payload[0], payload[3:]
        if not register & barracuda.WRITE:
            if register not in self.registers or value:
                return []
            body = self.registers[register]
        else:
            body = bytes((0 if self._store(register & ~barracuda.WRITE, value) else 1,))
        return [self._frame(barracuda.Family.CUSTOMER,
                            bytes((register, 0x01, len(body))) + body)]

    def _store(self, register: int, value: bytes) -> bool:
        names = barracuda.Register
        allowed = {
            names.EQ_PRESET: len(value) == 1 and value[0] in barracuda.EQ_PRESETS.values(),
            names.EQ_BANDS: len(value) == 10 and all(byte <= 10 for byte in value),
            names.DO_NOT_DISTURB: len(value) == 1 and value[0] <= 1,
            names.IDLE_MINUTES: len(value) == 1 and value[0] in barracuda.IDLE_MINUTES,
        }
        if not allowed.get(register, False):
            return False
        if register not in self.forgetful:
            self.registers[register] = bytes(value)
        return True


class FakeDeviceState:
    """Mutable state a fake device remembers between reports."""

    def __init__(self, serial: str, firmware: tuple = (1, 15)):
        self.serial = serial
        self.firmware = firmware
        self.device_mode = (0x00, 0x00)
        self.brightness: dict = {}
        self.led_state: dict = {}
        self.led_effect: dict = {}
        self.led_rgb: dict = {}
        self.dpi = (1800, 1800)
        self.dpi_stages = (1, [(800, 800), (1800, 1800), (3200, 3200)])
        #: The DPI is the active stage's, and the plain DPI command does nothing.
        self.dpi_from_stages = False
        #: The wheel's mode on the newer command (class 0x02, 0x30 / 0xB0).
        self.scroll_mode = 0
        self.poll_rate = 500
        #: The arguments of the two keyswitch optimization commands, kept apart
        #: so a test can tell a keyboard that was only sent one of them.  A
        #: keyboard starts in Typing mode.
        self.keyswitch_set = (0x00, 0x14, 0x00, 0x28, 0x00)
        self.keyswitch_store = (0x01, 0x00, 0x14, 0x00, 0x28, 0x00)
        self.battery = 0.55
        self.charging = False
        self.idle_time = 900
        self.low_battery_threshold = 0x0C
        self.frames: dict = {}
        self.sent: list = []
        self.onboard = _onboard_seed()
        #: Upcoming onboard reads answered BUSY, as a slow mouse does; the
        #: transport hands that reply back as if it were the answer.
        self.busy_reads = 0
        #: Upcoming binding writes the mouse stores wrongly.
        self.corrupt_writes = 0
        #: A mouse that acknowledges a profile switch and stays where it was.
        self.ignore_profile_switch = False


class FakeInputReader:
    """Reads the input reports a test queues on a :class:`FakeRazerDevice`."""

    def __init__(self, device: 'FakeRazerDevice', info: HidDeviceInfo):
        self.info = info
        self._device = device
        self._closed = False

    def read(self, timeout: float = 0.5) -> Optional[bytes]:
        if self._closed:
            raise HidError('reader is closed')
        try:
            report = self._device.input_reports.get(timeout=timeout)
        except queue.Empty:
            return None
        if report is None:          # the device was unplugged
            raise HidError('device disconnected')
        return report

    def close(self) -> None:
        self._closed = True


class FakeHidHandle:
    """A handle onto a :class:`FakeRazerDevice`."""

    def __init__(self, device: 'FakeRazerDevice', info: Optional[HidDeviceInfo] = None):
        self.info = info or device.info
        self._device = device
        self._closed = False

    def send_feature_report(self, data: bytes) -> None:
        if self._closed:
            raise HidError('handle is closed')
        self._device.feature_reports.append((self.info, bytes(data)))
        if self.info is self._device.headset_info:
            self._device.headset.set_feature(bytes(data))
            return
        if self.info.feature_length == FakeRazerDevice.ARGB_BUFFER_SIZE:
            self._device.argb_frames.append(bytes(data[1:]))
            return
        self._device.handle_set_feature(bytes(data))

    def send_output_report(self, data: bytes) -> None:
        if self._closed:
            raise HidError('handle is closed')
        if self.info is self._device.headset_info:
            answer = self._device.headset.answer(bytes(data))
            if answer is not None:
                self._device.input_reports.put(answer)
            return
        self._device.handle_output_report(bytes(data))

    def write_output_report(self, data: bytes) -> None:
        """What goes to the interrupt OUT endpoint, which few devices have."""
        if self._closed:
            raise HidError('handle is closed')
        if self.info is self._device.headset_info and hasattr(self._device.headset, 'write'):
            for report in self._device.headset.write(bytes(data)):
                self._device.input_reports.put(report)
            return
        raise HidError('no interrupt OUT endpoint on this collection')

    def get_feature_report(self, length: int, report_id: int = 0x00) -> bytes:
        if self._closed:
            raise HidError('handle is closed')
        if self.info is self._device.headset_info:
            return self._device.headset.get_feature(length, report_id)
        return self._device.handle_get_feature(length, report_id)

    def close(self) -> None:
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeRazerDevice:
    """Emulates one Razer control interface."""

    #: Length of the addressable-RGB feature report, including the report id.
    ARGB_BUFFER_SIZE = 321

    #: The Kraken family's output report, including the report id.
    KRAKEN_REPORT_SIZE = 37
    #: The vendor event report: report id 0x04 and up to 15 held key codes.
    KEY_REPORT_SIZE = 16

    def __init__(self, product_id: int, name: str = 'Fake Razer Device',
                 interface: int = 1, serial: Optional[str] = None,
                 vendor_id: int = 0x1532, argb: bool = False,
                 kraken: bool = False, keys: bool = False, headset: bool = False):
        self.info = HidDeviceInfo(
            path='\\\\?\\hid#vid_{0:04x}&pid_{1:04x}&mi_{2:02x}#fake'.format(
                vendor_id, product_id, interface),
            vendor_id=vendor_id,
            product_id=product_id,
            interface=interface,
            usage_page=0xFF00,
            usage=0x0002,
            feature_length=REPORT_SIZE + 1,
            product=name,
        )
        #: Devices with addressable strips expose a second, wider collection.
        self.argb_info = HidDeviceInfo(
            path=self.info.path + '&argb',
            vendor_id=vendor_id,
            product_id=product_id,
            interface=interface,
            usage_page=0xFF00,
            usage=0x0002,
            feature_length=self.ARGB_BUFFER_SIZE,
            product=name,
        ) if argb else None
        #: Krakens are reached through an output report on their own
        #: collection, not the shared feature report.
        self.kraken_info = HidDeviceInfo(
            path=self.info.path + '&kraken',
            vendor_id=vendor_id,
            product_id=product_id,
            interface=3,
            usage_page=0xFF00,
            usage=0x0002,
            output_length=self.KRAKEN_REPORT_SIZE,
            product=name,
        ) if kraken else None
        #: A keyboard's vendor event collection, where macro keys and Fn are
        #: reported in driver mode.
        self.keys_info = HidDeviceInfo(
            path=self.info.path + '&keys',
            vendor_id=vendor_id,
            product_id=product_id,
            interface=1,
            usage_page=0x0001,
            usage=0x0000,
            input_length=self.KEY_REPORT_SIZE,
            product=name,
        ) if keys else None
        #: A BlackShark's one vendor collection.  The V2 HyperSpeed's has no
        #: feature reports; the V3 X's is where its feature report is.  The
        #: Barracuda X's is on interface 3 like the V2's; its usage page is a
        #: guess, as nobody has read the descriptor out on Windows.
        v3x = product_id in blackshark_v3x.PRODUCT_IDS
        tunnel = product_id in barracuda.PRODUCT_IDS
        v2pro = product_id == 0x0555
        self.headset_info = HidDeviceInfo(
            path=self.info.path + '&headset',
            vendor_id=vendor_id,
            product_id=product_id,
            interface=0 if v3x else blackshark.INTERFACE,
            usage_page=0xFF82 if v3x else 0xFF00 if (tunnel or v2pro) else blackshark.USAGE_PAGE,
            usage=0x0001,
            feature_length=blackshark_v3x.REPORT_SIZE if v3x else 0,
            input_length=blackshark.REPORT_SIZE,
            output_length=blackshark.REPORT_SIZE,
            product=name,
        ) if headset else None
        self.headset = None
        self.input_reports: queue.Queue = queue.Queue()
        self.argb_frames: list = []
        self.feature_reports: list = []
        #: Address -> bytes written, plus the raw report stream.
        self.kraken_ram: dict = {}
        self.kraken_writes: list = []
        self.state = FakeDeviceState(
            serial or 'FAKE{0:010d}'.format(next(_SERIAL_COUNTER)))
        if product_id in SINGLE_PROFILE_PIDS:
            self.state.onboard = _single_profile_seed()
        self.state.dpi_from_stages = product_id in STAGE_DPI_PIDS
        if headset and v3x:
            self.headset = FakeHeadsetV3X(self.state.serial,
                                          product_id in blackshark_v3x.WIRED_PRODUCT_IDS)
        elif headset and tunnel:
            self.headset = FakeBarracuda(self.input_reports.put)
        elif headset and v2pro:
            self.headset = FakeBlackSharkV2Pro()
        elif headset:
            self.headset = FakeHeadset(self.state.serial,
                                       product_id == blackshark.PID_WIRED)
        self._response = self._blank_response()
        #: Set to a :class:`Status` value to make every command fail.
        self.force_status: Optional[int] = None

    def hold(self, *codes: int) -> None:
        """Report `codes` as held on the vendor event collection; none releases all."""
        report = bytes((0x04,) + tuple(codes))
        self.input_reports.put(report + bytes(self.KEY_REPORT_SIZE - len(report)))

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _blank_response() -> bytes:
        return b'\x00' * (REPORT_SIZE + 1)

    @property
    def sent_reports(self) -> list:
        return self.state.sent

    # -- transport ---------------------------------------------------------
    def handle_set_feature(self, data: bytes) -> None:
        if len(data) < REPORT_SIZE + 1:
            raise HidError('short feature report: {0} bytes'.format(len(data)))
        if data[0] != 0x00:
            raise HidError('unexpected report id 0x{0:02x}'.format(data[0]))
        payload = data[1:REPORT_SIZE + 1]
        if calculate_crc(payload) != payload[88]:
            raise HidError('bad CRC')
        request = RazerReport.unpack(payload)
        self.state.sent.append(request)
        self._response = b'\x00' + self._respond(request).pack()

    def handle_get_feature(self, length: int, report_id: int = 0x00) -> bytes:
        return self._response[:length].ljust(length, b'\x00')

    def handle_output_report(self, data: bytes) -> None:
        """Record a Kraken RAM write."""
        if len(data) != self.KRAKEN_REPORT_SIZE:
            raise HidError('Kraken report must be {0} bytes, got {1}'.format(
                self.KRAKEN_REPORT_SIZE, len(data)))
        if data[0] != 0x04:
            raise HidError('unexpected Kraken report id 0x{0:02x}'.format(data[0]))
        destination, length = data[1], data[2]
        address = (data[3] << 8) | data[4]
        payload = bytes(data[5:5 + length])
        self.kraken_writes.append(
            {'destination': destination, 'address': address, 'data': payload})
        if destination == 0x40:
            self.kraken_ram[address] = payload

    # -- protocol ----------------------------------------------------------
    def _respond(self, request: RazerReport) -> RazerReport:
        response = RazerReport.unpack(request.pack())
        response.status = self.force_status or Status.SUCCESSFUL
        if self.force_status:
            return response

        cls, cmd = request.command_class, request.command_id
        state = self.state
        args = request.arguments
        if (cls, cmd) in ONBOARD_COMMANDS:
            return self._respond_onboard(request, response)

        if (cls, cmd) == (0x00, 0x82):  # get serial
            response.set_arguments(0, state.serial.encode('ascii')[:22].ljust(22, b'\x00'))
        elif (cls, cmd) == (0x00, 0x81):  # get firmware
            response.set_argument(0, state.firmware[0])
            response.set_argument(1, state.firmware[1])
        elif (cls, cmd) == (0x00, 0x84):  # get device mode
            response.set_argument(0, state.device_mode[0])
            response.set_argument(1, state.device_mode[1])
        elif (cls, cmd) == (0x00, 0x04):  # set device mode
            state.device_mode = (args[0], args[1])
        elif (cls, cmd) == (0x03, 0x03):  # set led brightness
            state.brightness[args[1]] = args[2]
        elif (cls, cmd) == (0x03, 0x83):  # get led brightness
            response.set_argument(2, state.brightness.get(args[1], 0xFF))
        elif (cls, cmd) == (0x0F, 0x04):  # extended brightness
            state.brightness[args[1]] = args[2]
        elif (cls, cmd) == (0x0F, 0x84):
            response.set_argument(2, state.brightness.get(args[1], 0xFF))
        elif (cls, cmd) == (0x03, 0x00):  # set led state
            state.led_state[args[1]] = args[2]
        elif (cls, cmd) == (0x03, 0x80):
            response.set_argument(2, state.led_state.get(args[1], 0x00))
        elif (cls, cmd) == (0x03, 0x02):  # set led effect
            state.led_effect[args[1]] = args[2]
        elif (cls, cmd) == (0x03, 0x82):
            response.set_argument(2, state.led_effect.get(args[1], 0x00))
        elif (cls, cmd) == (0x03, 0x01):  # set led rgb
            state.led_rgb[args[1]] = (args[2], args[3], args[4])
        elif (cls, cmd) == (0x03, 0x81):
            rgb = state.led_rgb.get(args[1], (0, 0, 0))
            response.set_arguments(2, rgb)
        elif (cls, cmd) == (0x04, 0x05):  # set dpi
            if not state.dpi_from_stages:
                state.dpi = ((args[1] << 8) | args[2], (args[3] << 8) | args[4])
        elif (cls, cmd) == (0x04, 0x85):  # get dpi
            dpi = state.dpi
            if state.dpi_from_stages:
                active, stages = state.dpi_stages
                dpi = stages[active - 1] if 1 <= active <= len(stages) else dpi
            response.set_argument(1, (dpi[0] >> 8) & 0xFF)
            response.set_argument(2, dpi[0] & 0xFF)
            response.set_argument(3, (dpi[1] >> 8) & 0xFF)
            response.set_argument(4, dpi[1] & 0xFF)
        elif (cls, cmd) == (0x02, 0x30):  # set scroll mode (newer command)
            state.scroll_mode = args[1]
        elif (cls, cmd) == (0x02, 0xB0):
            response.set_argument(1, state.scroll_mode)
        elif (cls, cmd) == (0x04, 0x01):  # set dpi (byte form)
            state.dpi = (args[0] * 100, args[1] * 100)
        elif (cls, cmd) == (0x04, 0x81):
            response.set_argument(0, min(state.dpi[0] // 100, 0xFF))
            response.set_argument(1, min(state.dpi[1] // 100, 0xFF))
        elif (cls, cmd) == (0x04, 0x06):  # set dpi stages
            count = args[2]
            stages = []
            offset = 3
            for _ in range(count):
                stages.append(((args[offset + 1] << 8) | args[offset + 2],
                               (args[offset + 3] << 8) | args[offset + 4]))
                offset += 7
            state.dpi_stages = (args[1], stages)
        elif (cls, cmd) == (0x04, 0x86):  # get dpi stages
            active, stages = state.dpi_stages
            response.set_argument(1, active)
            response.set_argument(2, len(stages))
            offset = 3
            for index, (dpi_x, dpi_y) in enumerate(stages):
                response.set_argument(offset, index)
                response.set_argument(offset + 1, (dpi_x >> 8) & 0xFF)
                response.set_argument(offset + 2, dpi_x & 0xFF)
                response.set_argument(offset + 3, (dpi_y >> 8) & 0xFF)
                response.set_argument(offset + 4, dpi_y & 0xFF)
                offset += 7
        elif (cls, cmd) == (0x00, 0x05):  # set polling rate
            state.poll_rate = {0x01: 1000, 0x02: 500, 0x08: 125}.get(args[0], 500)
        elif (cls, cmd) == (0x00, 0x85):
            response.set_argument(0, {1000: 0x01, 500: 0x02, 125: 0x08}.get(
                state.poll_rate, 0x02))
        elif (cls, cmd) == (0x00, 0x40):  # set polling rate (extended)
            codes = {0x01: 8000, 0x02: 4000, 0x04: 2000, 0x08: 1000,
                     0x10: 500, 0x20: 250, 0x40: 125}
            state.poll_rate = codes.get(args[1], 500)
        elif (cls, cmd) == (0x00, 0xC0):
            # The driver reads the code back out of argument 1, matching where
            # the setter puts it.
            codes = {8000: 0x01, 4000: 0x02, 2000: 0x04, 1000: 0x08,
                     500: 0x10, 250: 0x20, 125: 0x40}
            response.set_argument(1, codes.get(state.poll_rate, 0x10))
        elif (cls, cmd) == (0x02, 0x02):  # set keyswitch optimization
            state.keyswitch_set = tuple(args[:5])
        elif (cls, cmd) == (0x02, 0x15):  # ... and its stored copy
            state.keyswitch_store = tuple(args[:6])
        elif (cls, cmd) == (0x02, 0x82):
            response.set_arguments(0, state.keyswitch_set)
        elif (cls, cmd) == (0x07, 0x80):  # battery level
            response.set_argument(1, int(round(state.battery * 255)))
        elif (cls, cmd) == (0x07, 0x84):  # charging status
            response.set_argument(1, 0x01 if state.charging else 0x00)
        elif (cls, cmd) == (0x07, 0x03):  # set idle time
            state.idle_time = (args[0] << 8) | args[1]
        elif (cls, cmd) == (0x07, 0x83):
            response.set_argument(0, (state.idle_time >> 8) & 0xFF)
            response.set_argument(1, state.idle_time & 0xFF)
        elif (cls, cmd) == (0x07, 0x01):  # low battery threshold
            state.low_battery_threshold = args[0]
        elif (cls, cmd) == (0x07, 0x81):
            response.set_argument(0, state.low_battery_threshold)
        elif (cls, cmd) in ((0x03, 0x0B), (0x0F, 0x03), (0x03, 0x0C)):
            state.frames[len(state.frames)] = bytes(request.arguments)
        return response

    def _respond_onboard(self, request: RazerReport, response: RazerReport) -> RazerReport:
        cls, cmd, args = request.command_class, request.command_id, request.arguments
        onboard = self.state.onboard
        if cmd & 0x80 and self.state.busy_reads:
            self.state.busy_reads -= 1
            response.status = Status.BUSY  # arguments stay an echo of the request
            return response
        if (cls, cmd) == (0x05, 0x81):
            response.set_arguments(0, bytes([onboard['max'], *onboard['profiles']]))
        elif (cls, cmd) == (0x05, 0x84):
            if onboard['active'] is None:
                response.status = Status.NOT_SUPPORTED
            else:
                response.set_argument(0, onboard['active'])
        elif (cls, cmd) == (0x05, 0x04):
            profile = args[0]
            if profile not in onboard['profiles']:
                response.status = Status.FAILURE
            elif not self.state.ignore_profile_switch:
                onboard['active'] = profile
                for (slot, button, layer), binding in list(onboard['bindings'].items()):
                    if slot == profile:  # profile 0 becomes a copy of the new one
                        onboard['bindings'][(0, button, layer)] = binding
        elif (cls, cmd) == (0x05, 0x88):
            slot, offset = args[0], (args[1] << 8) | args[2]
            meta = bytearray(0xFA)
            name = onboard['names'].get(slot, '').encode('ascii')
            meta[0x10:0x10 + len(name)] = name
            response.set_arguments(5, bytes(meta[offset:offset + 75]))
        elif (cls, cmd) == (0x02, 0x84):
            response.set_arguments(0, bytes([len(onboard['buttons']), *onboard['buttons']]))
        elif cls == 0x06:
            self._respond_macro(cmd, args, response)
        elif (cls, cmd) == (0x02, 0x8C):
            key = (args[0], args[1], args[2])
            if key not in onboard['bindings']:
                response.status = Status.FAILURE
            else:
                fn, size, data = onboard['bindings'][key]
                response.set_arguments(3, bytes([fn, size]) + data)
        elif (cls, cmd) == (0x02, 0x0C):
            key = (args[0], args[1], args[2])
            if key[1] not in onboard['buttons'] or key[2] not in (0, 1):
                response.status = Status.FAILURE
                return response
            data = bytes(args[5:10])
            if self.state.corrupt_writes:
                self.state.corrupt_writes -= 1
                data = bytes((data[0] ^ 0xFF,)) + data[1:]  # a byte the write did not ask for
            onboard['bindings'][key] = (args[3], args[4], data)
            if key[0] == onboard['active']:  # profile 0 mirrors the active one
                onboard['bindings'][(0,) + key[1:]] = (args[3], args[4], data)
        return response

    def _respond_macro(self, cmd: int, args, response: RazerReport) -> None:
        """Class 0x06 reads: the id list, a name block, a data size, a data chunk."""
        macros = self.state.onboard['macros']
        if cmd == 0x8B:
            offset = args[0] << 8 | args[1]
            ids = sorted(macros)[offset:offset + 32]
            payload = bytes((args[0], args[1], len(ids) >> 8, len(ids) & 0xFF))
            response.set_arguments(0, payload + b''.join(i.to_bytes(2, 'big') for i in ids))
            return
        macro_id = args[0] << 8 | args[1]
        if macro_id not in macros:
            response.status = Status.FAILURE
            return
        name, data = macros[macro_id]
        if cmd == 0x8C:
            block = bytearray(250)
            block[16:20] = len(data).to_bytes(4, 'little')
            block[20:20 + len(name)] = name.encode('utf-8')
            offset = args[3]
            response.set_arguments(4, bytes((0, 250)) + bytes(block[offset:offset + 64]))
        elif cmd == 0x88:
            response.set_arguments(2, len(data).to_bytes(4, 'big'))
        elif cmd == 0x89:
            offset, length = int.from_bytes(bytes(args[2:6]), 'big'), args[6]
            chunk = data[offset:offset + length]
            response.set_arguments(6, bytes((len(chunk),)) + chunk)


class FakeLibusb:
    """libusb-win32's ``libusb0.dll``, with its filter on the product ids in `filtered`.

    Keeps every control request sent, for tests to read (issue #29)."""

    def __init__(self, filtered=()):
        self.filtered = set(filtered)
        self.sent: list = []
        self.fail = False

    def present(self, vid: int, pid: int) -> bool:
        return vid == 0x1532 and pid in self.filtered

    def control_out(self, vid, pid, request_type, request, value, index, data, timeout_ms=1000):
        if self.fail or not self.present(vid, pid):
            raise OSError('libusb0-sys: could not open {0:04x}:{1:04x}'.format(vid, pid))
        self.sent.append((pid, request_type, request, value, index, bytes(data)))
        return len(data)


class FakeHidBackend:
    """A :class:`~openrazer_win.hid.base.HidBackend` over emulated devices."""

    name = 'fake'

    def __init__(self, devices=None, libusb=None):
        self.devices: list = list(devices or [])
        #: What the manager reaches filter-driven devices through, when set.
        self.libusb = libusb

    def add(self, device: FakeRazerDevice) -> FakeRazerDevice:
        self.devices.append(device)
        return device

    def remove(self, device: FakeRazerDevice) -> None:
        self.devices = [d for d in self.devices if d is not device]

    def enumerate(self, vendor_id=None, product_id=None) -> list:
        result = []
        for device in self.devices:
            if vendor_id is not None and device.info.vendor_id != vendor_id:
                continue
            if product_id is not None and device.info.product_id != product_id:
                continue
            result.append(device.info)
            if device.argb_info is not None:
                result.append(device.argb_info)
            if device.kraken_info is not None:
                result.append(device.kraken_info)
            if device.keys_info is not None:
                result.append(device.keys_info)
            if device.headset_info is not None:
                result.append(device.headset_info)
        return result

    def open(self, info: HidDeviceInfo) -> FakeHidHandle:
        for device in self.devices:
            if device.info.path == info.path:
                return FakeHidHandle(device)
            if device.argb_info is not None and device.argb_info.path == info.path:
                return FakeHidHandle(device, device.argb_info)
            if device.kraken_info is not None and device.kraken_info.path == info.path:
                return FakeHidHandle(device, device.kraken_info)
            if device.headset_info is not None and device.headset_info.path == info.path:
                return FakeHidHandle(device, device.headset_info)
        raise HidError('no such fake device: {0}'.format(info.path))

    def open_input(self, info: HidDeviceInfo) -> FakeInputReader:
        for device in self.devices:
            for readable in (device.keys_info, device.headset_info):
                if readable is not None and readable.path == info.path:
                    return FakeInputReader(device, info)
        raise HidError('no readable fake collection: {0}'.format(info.path))

    def device_for(self, info: HidDeviceInfo) -> Optional[FakeRazerDevice]:
        for device in self.devices:
            if device.info.path == info.path:
                return device
        return None
