"""Kraken headsets, which do not speak the standard control protocol.

Everything else in the range takes a 90-byte feature report describing an
effect.  A Kraken instead exposes its lighting controller's RAM: colours go to
fixed addresses and a one-byte effect selector says what to do with them.  That
is different enough that the recipe interpreter has nothing to offer, so this
class implements the driver's behaviour directly.

Reading state back is not supported.  The kernel driver collects it from
unsolicited HID *input* reports, which would need a reader thread and a
different transport; the daemon serves those values from its persistence file
instead, exactly as upstream's daemon does for devices that cannot report.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..devices.database import DeviceInfo
from ..devices.recipes import NotSupported, RecipeTable
from ..protocol import kraken as protocol
from .device import DeviceError, RazerDevice, _clamp, _rgb_bytes
from .persistence import Persistence
from .transport import Transport

logger = logging.getLogger(__name__)

#: What a Kraken can do, in the vocabulary the rest of the port uses.
KRAKEN_EFFECTS = ('none', 'static', 'spectrum', 'breath_single', 'breath_dual',
                  'breath_triple', 'custom')


class KrakenDevice(RazerDevice):
    """A Kraken headset."""

    def __init__(self, info: DeviceInfo, transport: Transport,
                 persistence: Optional[Persistence] = None,
                 recipes: Optional[RecipeTable] = None):
        super().__init__(info, transport, persistence, recipes)
        self.addresses = protocol.layout_for(info.pid)
        self.has_colour = info.pid not in protocol.COLOURLESS_PIDS

    # -- capability model --------------------------------------------------
    def supports(self, attribute: str) -> bool:
        """No recipe applies to a Kraken; nothing here goes through them."""
        return False

    def run(self, attribute: str, buf: bytes = b'', require_response: bool = False,
            **variables):
        raise NotSupported(
            '{0} uses the Kraken protocol, which has no sysfs recipes'.format(self.name))

    def _effect_names(self) -> list:
        if self.addresses is None:
            return []
        available = []
        for effect in KRAKEN_EFFECTS:
            if effect in ('static', 'custom') and not self.has_colour:
                continue
            if effect == 'breath_dual' and self.addresses.breathing_modes < 2:
                continue
            if effect == 'breath_triple' and self.addresses.breathing_modes < 3:
                continue
            if effect.startswith('breath') and not self.has_colour:
                continue
            available.append(effect)
        return available

    def has_effect(self, effect: str, zone: str = 'backlight') -> bool:
        return zone == 'backlight' and effect in self._effect_names()

    def available_effects(self, zone: str = 'backlight') -> list:
        return self._effect_names() if zone == 'backlight' else []

    def capabilities(self) -> dict:
        if self._capabilities is not None:
            return self._capabilities
        effects = self._effect_names()
        self._capabilities = {
            'name': self.info.name,
            'type': self.info.type,
            'vid': self.info.vid,
            'pid': self.info.pid,
            'image': self.info.image,
            'protocol': 'kraken',
            'zones': {'backlight': {'effects': effects, 'brightness': False}} if effects else {},
            'matrix': False,
            'matrix_dimensions': None,
            'dedicated_macro_keys': False,
            'dpi': False, 'dpi_stages': False, 'max_dpi': None,
            'poll_rate': False, 'supported_poll_rates': [],
            'battery': False, 'idle_time': False,
            'game_mode': False, 'macro_mode': False, 'keyboard_layout': False,
            'scroll_mode': False, 'custom_frame': False, 'software_effects': False,
            'readback': False,
            'transport': {
                'path': self.transport.kraken_info.path if self.transport.kraken_info else None,
                'interface': (self.transport.kraken_info.interface
                              if self.transport.kraken_info else None),
                'wait_us': int(protocol.WRITE_SETTLE_SECONDS * 1_000_000),
            },
        }
        return self._capabilities

    # -- identity ----------------------------------------------------------
    @property
    def serial(self) -> str:
        """Krakens report their serial over input reports the port cannot read."""
        if self._serial is None:
            self._serial = self._fallback_serial()
        return self._serial

    @property
    def firmware_version(self) -> str:
        raise NotSupported(
            '{0} reports its firmware over input reports, which this port '
            'does not read'.format(self.name))

    def enter_driver_mode(self) -> None:
        """Krakens have no driver mode."""

    # -- sending -----------------------------------------------------------
    def _send(self, reports: list) -> None:
        if self.addresses is None:
            raise NotSupported('{0} has no known controller layout'.format(self.name))
        # The controller needs roughly 15 ms per byte to commit each write.
        settle = protocol.WRITE_SETTLE_SECONDS
        with self._lock:
            try:
                self.transport.send_kraken(reports, settle=settle)
            except NotSupported:
                raise
            except Exception as error:  # noqa: BLE001 - reported as a device error
                raise DeviceError('{0}: {1}'.format(self.name, error)) from error

    def _remember(self, effect: str, colours=None) -> None:
        self.persistence.set(self.serial, 'backlight', 'effect', effect)
        if colours:
            padded = list(colours) + [0] * (9 - len(colours))
            self.persistence.set(self.serial, 'backlight', 'colors', padded)

    def _require(self, effect: str) -> None:
        if effect not in self._effect_names():
            raise NotSupported('{0} does not support {1}'.format(self.name, effect))

    # -- effects -----------------------------------------------------------
    def set_none(self, zone: str = 'backlight') -> None:
        self._require('none')
        self._send(protocol.effect_none(self.addresses))
        self._remember('none')

    def set_spectrum(self, zone: str = 'backlight') -> None:
        self._require('spectrum')
        self._send(protocol.effect_spectrum(self.addresses))
        self._remember('spectrum')

    def set_static(self, red: int, green: int, blue: int,
                   zone: str = 'backlight', intensity: Optional[int] = None) -> None:
        self._require('static')
        colour = _rgb_bytes(red, green, blue)
        self._send(protocol.effect_static(
            self.addresses, colour[0], colour[1], colour[2],
            None if intensity is None else _clamp(int(intensity), 0, 255),
            with_colour=self.has_colour))
        self._remember('static', list(colour))

    def set_custom(self, zone: str = 'backlight', red: int = 0, green: int = 255,
                   blue: int = 0, intensity: Optional[int] = None) -> None:
        self._require('custom')
        colour = _rgb_bytes(red, green, blue)
        self._send(protocol.effect_custom(
            self.addresses, colour[0], colour[1], colour[2],
            None if intensity is None else _clamp(int(intensity), 0, 255)))
        self._remember('custom', list(colour))

    def set_breath_single(self, red: int, green: int, blue: int,
                          zone: str = 'backlight') -> None:
        self._require('breath_single')
        colour = _rgb_bytes(red, green, blue)
        self._send(protocol.effect_breathing(self.addresses, [colour]))
        self._remember('breath_single', list(colour))

    def set_breath_dual(self, red1: int, green1: int, blue1: int,
                        red2: int, green2: int, blue2: int,
                        zone: str = 'backlight') -> None:
        self._require('breath_dual')
        first = _rgb_bytes(red1, green1, blue1)
        second = _rgb_bytes(red2, green2, blue2)
        self._send(protocol.effect_breathing(self.addresses, [first, second]))
        self._remember('breath_dual', list(first) + list(second))

    def set_breath_triple(self, red1: int, green1: int, blue1: int,
                          red2: int, green2: int, blue2: int,
                          red3: int, green3: int, blue3: int,
                          zone: str = 'backlight') -> None:
        self._require('breath_triple')
        colours = [_rgb_bytes(red1, green1, blue1),
                   _rgb_bytes(red2, green2, blue2),
                   _rgb_bytes(red3, green3, blue3)]
        self._send(protocol.effect_breathing(self.addresses, colours))
        self._remember('breath_triple', [c for colour in colours for c in colour])

    # -- everything the Kraken cannot do -----------------------------------
    def get_brightness(self, zone: str = 'backlight') -> float:
        raise NotSupported('{0} has no brightness control'.format(self.name))

    def set_brightness(self, percent: float, zone: str = 'backlight') -> None:
        raise NotSupported('{0} has no brightness control'.format(self.name))

    def restore(self) -> None:
        """Re-apply the last effect this port set."""
        state = self.persistence.zone(self.serial, 'backlight')
        effect = state.get('effect')
        colours = state.get('colors') or [0, 255, 0, 0, 255, 255, 0, 0, 255]
        try:
            if effect == 'static':
                self.set_static(*colours[:3])
            elif effect == 'breath_single':
                self.set_breath_single(*colours[:3])
            elif effect == 'breath_dual':
                self.set_breath_dual(*colours[:6])
            elif effect == 'breath_triple':
                self.set_breath_triple(*colours[:9])
            elif effect == 'custom':
                self.set_custom(red=colours[0], green=colours[1], blue=colours[2])
            elif effect == 'none':
                self.set_none()
            elif effect == 'spectrum':
                self.set_spectrum()
        except (DeviceError, NotSupported):
            logger.debug('could not restore %s', self.name, exc_info=True)
