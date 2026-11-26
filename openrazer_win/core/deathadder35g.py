"""The DeathAdder 3.5G and 3.5G Black, through the libusb-win32 filter (issue #29).

These mice predate the 90-byte report.  Upstream's driver keeps their whole
state -- polling rate, DPI, profile, lights -- and sends all four bytes at once
with a USB ``SET_REPORT`` of report type 0 whenever any of it changes::

    bmRequestType 0x21, bRequest 0x09, wValue 0x0010, wIndex 0, data: poll dpi profile leds

Nothing can be read back, so the state lives in persistence, and all of it is
sent again whenever the mouse turns up: it starts at its defaults each time.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from ..devices.database import DeviceInfo
from ..devices.recipes import NotSupported
from .device import DeviceError
from .persistence import Persistence

logger = logging.getLogger(__name__)

#: DPI the mouse knows, with the byte it takes for each (upstream).
DPI_CODES = {450: 4, 900: 3, 1800: 2, 3500: 1}
#: Polling rates, likewise.
POLL_CODES = {1000: 1, 500: 2, 125: 3}
#: Bits of the lights byte.
LED_BITS = {'logo': 0x01, 'scroll': 0x02}
#: The 3.5G Black has no lights to switch.
LIGHTED_PIDS = frozenset({0x0016})

DEFAULT_DPI = 3500
DEFAULT_POLL = 1000
#: Upstream's pause after each request.
WAIT_S = 0.003


def nearest(value: int, choices) -> int:
    return min(choices, key=lambda choice: (abs(choice - int(value)), choice))


class DeathAdder35GDevice:
    """One DeathAdder 3.5G, set up through `libusb` (a :class:`~..usbfilter.Libusb0`)."""

    def __init__(self, info: DeviceInfo, libusb, persistence: Optional[Persistence] = None):
        self.info = info
        self.libusb = libusb
        self.persistence = persistence or Persistence()
        self._lock = threading.RLock()
        self.hid_collections: list = []

    # -- identity ----------------------------------------------------------
    @property
    def name(self) -> str:
        return self.info.name

    @property
    def type(self) -> str:
        return self.info.type

    @property
    def driver(self) -> str:
        return self.info.driver

    @property
    def pid(self) -> int:
        return self.info.pid

    @property
    def vid(self) -> int:
        return self.info.vid

    @property
    def serial(self) -> str:
        # It has none to give; this is the name every other port device without one gets.
        return 'RZ{0:04X}{1:04X}'.format(self.info.vid, self.info.pid)

    @property
    def firmware_version(self) -> str:
        raise NotSupported('{0} does not report its firmware'.format(self.name))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return '<DeathAdder35GDevice {0:04x}>'.format(self.info.pid)

    # -- what it can do ------------------------------------------------------
    def _zones(self) -> list:
        return list(LED_BITS) if self.info.pid in LIGHTED_PIDS else []

    def capabilities(self) -> dict:
        zones = {zone: {'effects': ['none', 'on'], 'brightness': False, 'wave_speed': False}
                 for zone in self._zones()}
        return {
            'name': self.info.name, 'type': self.info.type, 'vid': self.info.vid,
            'pid': self.info.pid, 'image': self.info.image, 'zones': zones,
            'matrix': False, 'matrix_dimensions': None, 'dedicated_macro_keys': False,
            'dpi': True, 'dpi_stages': False, 'max_dpi': max(DPI_CODES),
            'dpi_choices': sorted(DPI_CODES),
            'poll_rate': True, 'supported_poll_rates': sorted(POLL_CODES),
            'battery': False, 'idle_time': False, 'game_mode': False, 'macro_mode': False,
            'keyboard_layout': False, 'keyswitch_optimization': False, 'scroll_mode': False,
            'custom_frame': False, 'onboard_buttons': False, 'software_effects': False,
            'transport': {'path': 'libusb-win32', 'interface': 0,
                          'wait_us': int(WAIT_S * 1_000_000)},
        }

    def supports(self, attribute: str) -> bool:
        return False

    def has_effect(self, effect: str, zone: str = 'backlight') -> bool:
        return zone in self._zones() and effect in ('none', 'on')

    def available_effects(self, zone: str = 'backlight') -> list:
        return ['none', 'on'] if zone in self._zones() else []

    # -- the state, and sending it ---------------------------------------------
    def _dpi(self) -> int:
        saved = self.persistence.get_device_value(self.serial, 'dpi', None)
        value = saved[0] if isinstance(saved, (list, tuple)) and saved else saved
        return nearest(value or DEFAULT_DPI, DPI_CODES)

    def _poll(self) -> int:
        return nearest(self.persistence.get_device_value(self.serial, 'poll_rate', None)
                       or DEFAULT_POLL, POLL_CODES)

    def _leds(self) -> int:
        byte = 0
        for zone, bit in LED_BITS.items():
            effect = (self.persistence.saved_zone(self.serial, zone) or {}).get('effect', 'on')
            if effect != 'none':
                byte |= bit
        return byte

    def state_bytes(self) -> bytes:
        """The four bytes upstream sends: poll, DPI, profile, lights."""
        return bytes((POLL_CODES[self._poll()], DPI_CODES[self._dpi()], 0x01, self._leds()))

    def _send(self) -> None:
        payload = self.state_bytes()
        with self._lock:
            try:
                self.libusb.control_out(self.info.vid, self.info.pid, 0x21, 0x09, 0x0010, 0x0000,
                                        payload)
            except Exception as error:  # noqa: BLE001 - any failure of the library is the device's
                raise DeviceError('{0}: {1}'.format(self.name, error)) from error
            time.sleep(WAIT_S)

    # -- lighting ------------------------------------------------------------
    def _set_zone(self, zone: str, effect: str) -> None:
        if zone not in self._zones():
            raise NotSupported('{0} has no {1} light'.format(self.name, zone))
        self.persistence.set(self.serial, zone, 'effect', effect)
        self._send()

    def set_on(self, zone: str = 'logo') -> None:
        self._set_zone(zone, 'on')

    def set_none(self, zone: str = 'logo') -> None:
        self._set_zone(zone, 'none')

    # -- DPI and polling -------------------------------------------------------
    def get_dpi(self) -> tuple:
        dpi = self._dpi()
        return dpi, dpi

    def set_dpi(self, dpi_x: int, dpi_y: Optional[int] = None) -> None:
        dpi = nearest(dpi_x, DPI_CODES)
        self.persistence.set_device_value(self.serial, 'dpi', [dpi, dpi])
        self._send()

    def get_dpi_stages(self) -> tuple:
        raise NotSupported('{0} has no DPI stages'.format(self.name))

    def get_poll_rate(self) -> int:
        return self._poll()

    def set_poll_rate(self, rate: int) -> None:
        self.persistence.set_device_value(self.serial, 'poll_rate', nearest(rate, POLL_CODES))
        self._send()

    def supported_poll_rates(self) -> list:
        return sorted(POLL_CODES)

    # -- lifecycle ---------------------------------------------------------
    def enter_driver_mode(self, keys_handled: bool = False) -> None:
        """It has no driver mode."""

    def restore(self) -> None:
        """Send the whole saved state: the mouse came up at its defaults.

        A light never set is on, as the mouse starts; that is written down, so
        Studio has an effect to show for it rather than none."""
        for zone in self._zones():
            if (self.persistence.saved_zone(self.serial, zone) or {}).get('effect') is None:
                self.persistence.set(self.serial, zone, 'effect', 'on')
        self._send()

    def close(self) -> None:
        """Nothing is held open between requests."""
