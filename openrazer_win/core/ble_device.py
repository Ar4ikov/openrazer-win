"""Razer devices reached over Bluetooth LE rather than USB HID.

The Kraken Kitty V2 BT has no USB data mode at all -- Razer lists it as a
Bluetooth headset, and plugging it in produces no USB device node -- so the
only way to its lighting is the vendor GATT service.

What the device itself implements is a static colour per zone.  Synapse's
breathing, spectrum and audio-reactive effects are host-rendered: it streams a
new colour every frame, which is why closing Synapse stops the traffic dead.
This class does the same through :mod:`openrazer_win.effects`.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..ble.transport import BleTransport, BleUnavailable
from ..devices.database import DeviceInfo
from ..devices.recipes import NotSupported
from ..protocol import razer_ble
from .device import DeviceError, _rgb_bytes
from .persistence import Persistence

logger = logging.getLogger(__name__)

#: What a Bluetooth Razer device does on its own.  Everything else is drawn by
#: the host, one frame at a time.
NATIVE_EFFECTS = ('none', 'static')

#: The two lighting zones, in the order their bytes appear on the wire.
#: Verified on the hardware: a command carrying red then blue lit the LEFT ear
#: red and the right ear blue, as worn -- so the first triple is the left ear.
ZONE_NAMES = razer_ble.ZONES

#: ``backlight`` is not a zone the device knows about; it is the name every
#: other device in the port uses for "the whole thing", so writing to it here
#: writes to both ears.  Naming an ear addresses just that ear.
AGGREGATE_ZONE = 'backlight'


class BleDevice:
    """A Razer device whose lighting lives behind a GATT characteristic.

    Deliberately not a :class:`~openrazer_win.core.device.RazerDevice`: there
    is no control report, no recipe table and no serial to read back, so
    inheriting would mean overriding almost everything with "not supported".
    """

    def __init__(self, info: DeviceInfo, transport: BleTransport,
                 persistence: Optional[Persistence] = None):
        self.info = info
        self.transport = transport
        self.persistence = persistence or Persistence()
        self._capabilities: Optional[dict] = None

    # -- identity ----------------------------------------------------------
    @property
    def name(self) -> str:
        return self.info.name

    @property
    def type(self) -> str:
        return self.info.type

    @property
    def pid(self) -> int:
        return self.info.pid

    @property
    def vid(self) -> int:
        return self.info.vid

    @property
    def serial(self) -> str:
        """Bluetooth devices expose no serial, so the address stands in."""
        return 'BLE{0:012X}'.format(self.transport.address)

    @property
    def firmware_version(self) -> str:
        raise NotSupported(
            '{0} does not report a firmware version over Bluetooth'.format(self.name))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return '<BleDevice {0} {1:012X}>'.format(self.name, self.transport.address)

    # -- capabilities ------------------------------------------------------
    def capabilities(self) -> dict:
        if self._capabilities is None:
            self._capabilities = {
                'name': self.info.name,
                'type': self.info.type,
                'vid': self.info.vid,
                'pid': self.info.pid,
                'image': self.info.image,
                'protocol': 'razer-ble',
                'zones': {
                    zone: {'effects': list(NATIVE_EFFECTS), 'brightness': False}
                    for zone in (AGGREGATE_ZONE,) + tuple(ZONE_NAMES)
                },
                #: The zones a colour can be written to individually, in the
                #: order their bytes go out on the wire.
                'zone_order': list(ZONE_NAMES),
                'zone_colours': True,
                'matrix': True,
                'matrix_dimensions': [1, len(ZONE_NAMES)],
                'dedicated_macro_keys': False,
                'dpi': False, 'dpi_stages': False, 'max_dpi': None,
                'poll_rate': False, 'supported_poll_rates': [],
                'battery': False, 'idle_time': False,
                'game_mode': False, 'macro_mode': False,
                'keyboard_layout': False, 'scroll_mode': False,
                'custom_frame': True,
                'software_effects': True,
                'readback': False,
                'transport': {
                    'path': '{0:012X}'.format(self.transport.address),
                    'interface': None,
                    'wait_us': 0,
                },
            }
        return self._capabilities

    def has_effect(self, effect: str, zone: str = AGGREGATE_ZONE) -> bool:
        return effect in NATIVE_EFFECTS

    def available_effects(self, zone: str = AGGREGATE_ZONE) -> list:
        return list(NATIVE_EFFECTS)

    def supports(self, attribute: str) -> bool:
        return False

    def run(self, attribute: str, buf: bytes = b'', require_response: bool = False,
            **variables):
        raise NotSupported(
            '{0} is a Bluetooth device and has no sysfs recipes'.format(self.name))

    # -- lighting ----------------------------------------------------------
    def _send(self, payload: bytes, hold: bool = False) -> None:
        """Write to the device; with `hold`, keep the colour asserted.

        The headset does not store what it is told: it reverts to the colour
        saved in it the moment the link drops, so a colour meant to stay put
        has to be held rather than written once.  A frame of an animation is
        not held -- the next frame is along in a moment.
        """
        try:
            self.transport.write(payload, hold=hold)
        except BleUnavailable as error:
            raise DeviceError('{0}: {1}'.format(self.name, error)) from error

    def set_zone_colours(self, colours, hold: bool = True) -> None:
        """Set every zone at once; `colours` is one ``(r, g, b)`` per zone.

        One write lights both ears, so the two never disagree even for an
        instant -- which is the whole point of having this alongside
        :meth:`set_static`.
        """
        try:
            colours = [_rgb_bytes(*tuple(colour)[:3]) for colour in colours]
            if len(colours) == 1:
                # One colour means "all of them", and the stored state has to
                # say so too or the next per-ear write would read it back wrong.
                colours = colours * len(ZONE_NAMES)
            payload = razer_ble.colour_command(colours)
        except (TypeError, ValueError) as error:
            raise DeviceError('{0}: {1}'.format(self.name, error)) from error
        self._send(payload, hold=hold)
        flat: list = []
        for colour in colours:
            flat.extend(colour)
        self.persistence.set(self.serial, AGGREGATE_ZONE, 'effect', 'static')
        #: Stored per device rather than in the zone state: a zone's default
        #: ``colors`` is the green/cyan/blue triple every other device starts
        #: from, and inheriting that would make an ear that was never written
        #: read back as cyan.
        self.persistence.set_device_value(self.serial, 'zone_colours', flat)

    def _current_colours(self) -> list:
        """What each ear is showing, black for one never written to."""
        stored = self.persistence.get_device_value(self.serial, 'zone_colours') or []
        colours = []
        for index in range(len(ZONE_NAMES)):
            chunk = list(stored[index * 3:index * 3 + 3])
            colours.append(tuple(chunk) if len(chunk) == 3 else (0, 0, 0))
        return colours

    def set_static(self, red: int, green: int, blue: int,
                   zone: str = AGGREGATE_ZONE) -> None:
        colour = _rgb_bytes(red, green, blue)
        if zone in ZONE_NAMES:
            colours = self._current_colours()
            colours[ZONE_NAMES.index(zone)] = colour
        else:
            colours = [colour] * len(ZONE_NAMES)
        self.set_zone_colours(colours)

    def set_none(self, zone: str = AGGREGATE_ZONE) -> None:
        if zone in ZONE_NAMES:
            self.set_static(0, 0, 0, zone)
            return
        # Nothing to hold once it is dark, so the link can go and the device
        # can start advertising again.
        self._send(razer_ble.off_command())
        self._release()
        self.persistence.set(self.serial, AGGREGATE_ZONE, 'effect', 'none')
        self.persistence.set_device_value(
            self.serial, 'zone_colours', [0] * (3 * len(ZONE_NAMES)))

    # -- per-key style access, so the effect engine can drive it -----------
    @property
    def matrix_dimensions(self) -> tuple:
        return (1, len(ZONE_NAMES))

    def _release(self) -> None:
        release = getattr(self.transport, 'release', None)
        if release is not None:
            release()

    def set_key_row(self, payload: bytes) -> None:
        """Accept a matrix frame: ``row, start_col, stop_col, r, g, b, ...``."""
        data = bytes(payload)
        if len(data) < 3:
            raise DeviceError('frame too short')
        start, stop = data[1], data[2]
        colours = self._current_colours()
        body = data[3:]
        for offset, column in enumerate(range(start, stop + 1)):
            chunk = body[offset * 3:offset * 3 + 3]
            if len(chunk) == 3 and 0 <= column < len(ZONE_NAMES):
                colours[column] = tuple(chunk)
        # Not held: the next frame is along in a moment, and holding one would
        # leave it lit after the animation stops.
        self.set_zone_colours(colours, hold=False)

    def set_custom(self, zone: str = AGGREGATE_ZONE) -> None:
        """The frame is applied as it is written, so this is a no-op."""

    # -- everything a Bluetooth headset cannot do --------------------------
    def get_brightness(self, zone: str = AGGREGATE_ZONE) -> float:
        raise NotSupported('{0} has no brightness control'.format(self.name))

    def set_brightness(self, percent: float, zone: str = AGGREGATE_ZONE) -> None:
        raise NotSupported('{0} has no brightness control'.format(self.name))

    def enter_driver_mode(self) -> None:
        """Bluetooth devices have no driver mode."""

    def restore(self) -> None:
        state = self.persistence.zone(self.serial, AGGREGATE_ZONE)
        if state.get('effect') == 'static':
            try:
                self.set_zone_colours(self._current_colours())
            except DeviceError:
                logger.debug('could not restore %s', self.name, exc_info=True)

    def is_connected(self) -> bool:
        """Whether the link is up, which is not the same as advertising.

        A connected device stops advertising, so discovery has to ask this
        rather than conclude from silence that the device has gone.
        """
        try:
            return bool(self.transport.is_connected())
        except Exception:  # noqa: BLE001 - unknown means not connected
            return False

    def close(self) -> None:
        self.transport.close()
