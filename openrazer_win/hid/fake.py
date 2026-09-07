"""An in-process Razer device emulator.

Used by the test suite and by ``openrazer-win --demo``, so the daemon, client
library, CLI and tray can all be exercised end to end without hardware.  The
emulator implements enough of the vendor protocol to be indistinguishable from
a real device at the transport level: it validates the CRC, echoes the command
class and id back, and keeps per-LED state so reads return what was written.
"""
from __future__ import annotations

import itertools
from typing import Optional

from ..protocol.report import REPORT_SIZE, RazerReport, Status, calculate_crc
from .base import HidDeviceInfo, HidError

_SERIAL_COUNTER = itertools.count(1)


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
        self.poll_rate = 500
        self.battery = 0.55
        self.charging = False
        self.idle_time = 900
        self.low_battery_threshold = 0x0C
        self.frames: dict = {}
        self.sent: list = []


class FakeHidHandle:
    """A handle onto a :class:`FakeRazerDevice`."""

    def __init__(self, device: 'FakeRazerDevice', info: Optional[HidDeviceInfo] = None):
        self.info = info or device.info
        self._device = device
        self._closed = False

    def send_feature_report(self, data: bytes) -> None:
        if self._closed:
            raise HidError('handle is closed')
        if self.info.feature_length == FakeRazerDevice.ARGB_BUFFER_SIZE:
            self._device.argb_frames.append(bytes(data[1:]))
            return
        self._device.handle_set_feature(bytes(data))

    def get_feature_report(self, length: int, report_id: int = 0x00) -> bytes:
        if self._closed:
            raise HidError('handle is closed')
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

    def __init__(self, product_id: int, name: str = 'Fake Razer Device',
                 interface: int = 1, serial: Optional[str] = None,
                 vendor_id: int = 0x1532, argb: bool = False):
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
        self.argb_frames: list = []
        self.state = FakeDeviceState(
            serial or 'FAKE{0:010d}'.format(next(_SERIAL_COUNTER)))
        self._response = self._blank_response()
        #: Set to a :class:`Status` value to make every command fail.
        self.force_status: Optional[int] = None

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

    # -- protocol ----------------------------------------------------------
    def _respond(self, request: RazerReport) -> RazerReport:
        response = RazerReport.unpack(request.pack())
        response.status = self.force_status or Status.SUCCESSFUL
        if self.force_status:
            return response

        cls, cmd = request.command_class, request.command_id
        state = self.state
        args = request.arguments

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
            state.dpi = ((args[1] << 8) | args[2], (args[3] << 8) | args[4])
        elif (cls, cmd) == (0x04, 0x85):  # get dpi
            response.set_argument(1, (state.dpi[0] >> 8) & 0xFF)
            response.set_argument(2, state.dpi[0] & 0xFF)
            response.set_argument(3, (state.dpi[1] >> 8) & 0xFF)
            response.set_argument(4, state.dpi[1] & 0xFF)
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


class FakeHidBackend:
    """A :class:`~openrazer_win.hid.base.HidBackend` over emulated devices."""

    name = 'fake'

    def __init__(self, devices=None):
        self.devices: list = list(devices or [])

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
        return result

    def open(self, info: HidDeviceInfo) -> FakeHidHandle:
        for device in self.devices:
            if device.info.path == info.path:
                return FakeHidHandle(device)
            if device.argb_info is not None and device.argb_info.path == info.path:
                return FakeHidHandle(device, device.argb_info)
        raise HidError('no such fake device: {0}'.format(info.path))

    def device_for(self, info: HidDeviceInfo) -> Optional[FakeRazerDevice]:
        for device in self.devices:
            if device.info.path == info.path:
                return device
        return None
