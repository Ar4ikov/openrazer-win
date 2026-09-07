"""The device object: capabilities, effects and settings.

This is the Windows equivalent of the OpenRazer daemon's device classes.  Where
the daemon writes decimal strings and packed bytes into sysfs files, this class
runs the matching transpiled recipe against a :class:`~openrazer_win.core.transport.Transport`.

Payload formats are kept identical to the sysfs interface on purpose -- that is
what the recipes were extracted against, so ``set_breath_single`` sends the same
three bytes the Linux daemon would have written.
"""
from __future__ import annotations

import logging
import struct
import threading
from typing import Iterable, Optional

from ..devices.database import DeviceInfo
from ..devices.recipes import (
    Environment, NotSupported, RecipeError, RecipeTable, execute, get_recipes,
)
from ..protocol.report import RazerReport, RazerReportError
from .persistence import Persistence
from .transport import Transport

logger = logging.getLogger(__name__)

#: Effect names as the client API and CLI use them.
EFFECTS = ('none', 'on', 'static', 'spectrum', 'wave', 'wheel', 'reactive',
           'breath_random', 'breath_single', 'breath_dual', 'breath_triple',
           'starlight_random', 'starlight_single', 'starlight_dual',
           'blinking', 'custom')

#: Zone -> sysfs attribute prefixes, in the order to try them.  Keyboards call
#: the main zone ``matrix_effect_*`` while mice call it ``backlight_matrix_effect_*``.
ZONE_PREFIXES = {
    'backlight': ('', 'backlight_'),
    'logo': ('logo_',),
    'scroll': ('scroll_',),
    'left': ('left_',),
    'right': ('right_',),
    'charging': ('charging_',),
    'fast_charging': ('fast_charging_',),
    'fully_charged': ('fully_charged_',),
}

#: Zone -> brightness attribute candidates.
ZONE_BRIGHTNESS = {
    'backlight': ('matrix_brightness', 'backlight_led_brightness', 'led_brightness'),
    'logo': ('logo_led_brightness',),
    'scroll': ('scroll_led_brightness',),
    'left': ('left_led_brightness',),
    'right': ('right_led_brightness',),
    'charging': ('charging_led_brightness',),
    'fast_charging': ('fast_charging_led_brightness',),
    'fully_charged': ('fully_charged_led_brightness',),
}

#: Effect name -> the suffix upstream's daemon uses in its ``METHODS`` list.
#: The recipe table alone is too permissive -- a kernel driver's ``default:``
#: branch answers for every product id, including zones a device does not have
#: -- so capability checks are intersected with what the device class declares.
EFFECT_METHOD_SUFFIX = {
    'none': 'none',
    'on': 'on',
    'static': 'static',
    'spectrum': 'spectrum',
    'wave': 'wave',
    'wheel': 'wheel',
    'reactive': 'reactive',
    'breath_random': 'breath_random',
    'breath_single': 'breath_single',
    'breath_dual': 'breath_dual',
    'breath_triple': 'breath_triple',
    'starlight_random': 'starlight_random',
    'starlight_single': 'starlight_single',
    'starlight_dual': 'starlight_dual',
    'blinking': 'blinking',
    'custom': 'custom',
}

#: Zone -> how the daemon spells its methods.  ``backlight`` is the odd one out:
#: it is both the unprefixed ``set_static_effect`` form and ``set_backlight_static``.
ZONE_METHOD_FORMS = {
    'backlight': ('set_{suffix}_effect', 'set_backlight_{suffix}'),
    'logo': ('set_logo_{suffix}',),
    'scroll': ('set_scroll_{suffix}',),
    'left': ('set_left_{suffix}',),
    'right': ('set_right_{suffix}',),
    'charging': ('set_charging_{suffix}',),
    'fast_charging': ('set_fast_charging_{suffix}',),
    'fully_charged': ('set_fully_charged_{suffix}',),
}

#: Zone -> the daemon's brightness method names.
ZONE_BRIGHTNESS_METHODS = {
    'backlight': ('set_brightness', 'set_backlight_brightness'),
    'logo': ('set_logo_brightness',),
    'scroll': ('set_scroll_brightness',),
    'left': ('set_left_brightness',),
    'right': ('set_right_brightness',),
    'charging': ('set_charging_brightness',),
    'fast_charging': ('set_fast_charging_brightness',),
    'fully_charged': ('set_fully_charged_brightness',),
}

#: Effect name -> the ``matrix_effect_*`` attribute suffix it writes to.
EFFECT_ATTRIBUTE = {
    'none': 'matrix_effect_none',
    'on': 'matrix_effect_on',
    'static': 'matrix_effect_static',
    'spectrum': 'matrix_effect_spectrum',
    'wave': 'matrix_effect_wave',
    'wheel': 'matrix_effect_wheel',
    'reactive': 'matrix_effect_reactive',
    'breath_random': 'matrix_effect_breath',
    'breath_single': 'matrix_effect_breath',
    'breath_dual': 'matrix_effect_breath',
    'breath_triple': 'matrix_effect_breath',
    'starlight_random': 'matrix_effect_starlight',
    'starlight_single': 'matrix_effect_starlight',
    'starlight_dual': 'matrix_effect_starlight',
    'blinking': 'matrix_effect_blinking',
    'custom': 'matrix_effect_custom',
}


class DeviceError(Exception):
    """A device operation failed."""


def _clamp(value, low, high):
    return low if value < low else (high if value > high else value)


def _rgb_bytes(*values) -> bytes:
    return bytes(_clamp(int(v), 0, 255) for v in values)


class RazerDevice:
    """One connected Razer peripheral."""

    def __init__(self, info: DeviceInfo, transport: Transport,
                 persistence: Optional[Persistence] = None,
                 recipes: Optional[RecipeTable] = None):
        self.info = info
        self.transport = transport
        self.persistence = persistence or Persistence()
        self.recipes = recipes or get_recipes()
        self._lock = threading.RLock()
        self._serial: Optional[str] = None
        self._firmware: Optional[str] = None
        self._capabilities: Optional[dict] = None

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

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return '<RazerDevice {0} {1:04x}:{2:04x}>'.format(
            self.info.name, self.info.vid, self.info.pid)

    # -- recipe plumbing ---------------------------------------------------
    def supports(self, attribute: str) -> bool:
        """Whether this product id implements a sysfs attribute."""
        return self.recipes.has(self.driver, attribute, self.info.pid)

    def run(self, attribute: str, buf: bytes = b'', require_response: bool = False,
            **variables) -> Environment:
        """Execute the recipe for `attribute` with `buf` as the sysfs payload."""
        steps, bindings = self.recipes.lookup(self.driver, attribute, self.info.pid)
        merged = dict(bindings)
        merged.update(variables)
        merged.setdefault('varstore', 0x01)
        # A handful of pre-Chroma devices are stateless over USB, so the kernel
        # driver keeps a shadow copy of their settings.  Serve that from the
        # persistence store instead.
        for name, default in (('orochi2011_led', 0x03), ('orochi2011_poll', 0x02),
                              ('orochi2011_dpi', 0x4C)):
            merged.setdefault(name, self.persistence.get_device_value(
                self._serial or self._fallback_serial(), name, default))
        env = Environment(buf, **merged)

        def send(request: RazerReport, want_response: bool) -> Optional[RazerReport]:
            return self.transport.send_payload(request, want_response)

        # ARGB frames bypass the 90-byte report entirely, so the interpreter
        # reaches them through an attribute on the sender.
        send.send_argb = self.transport.send_argb  # type: ignore[attr-defined]

        with self._lock:
            try:
                execute(steps, env, send)
            except RecipeError:
                raise
            except RazerReportError as error:
                raise DeviceError('{0} failed: {1}'.format(attribute, error)) from error
        if require_response and env.response is None:
            raise NotSupported(
                '{0} produced no device response on {1}'.format(attribute, self.name))
        return env

    def _first_supported(self, attributes: Iterable[str]) -> str:
        candidates = list(attributes)
        for attribute in candidates:
            if self.supports(attribute):
                return attribute
        raise NotSupported('{0} supports none of {1}'.format(self.name, candidates))

    # -- device information ------------------------------------------------
    @property
    def serial(self) -> str:
        """The device serial, cached after the first successful read."""
        if self._serial is None:
            try:
                env = self.run('read:device_serial', require_response=True)
                raw = bytes(env.response.arguments[:22])
                text = raw.split(b'\x00')[0].decode('ascii', 'ignore').strip()
                self._serial = text or self._fallback_serial()
            except (RecipeError, DeviceError):
                self._serial = self._fallback_serial()
        return self._serial

    def _fallback_serial(self) -> str:
        return 'RZ{0:04X}{1:04X}'.format(self.info.vid, self.info.pid)

    @property
    def firmware_version(self) -> str:
        if self._firmware is None:
            env = self.run('read:firmware_version', require_response=True)
            self._firmware = 'v{0}.{1}'.format(
                env.response.arguments[0], env.response.arguments[1])
        return self._firmware

    def get_device_mode(self) -> str:
        env = self.run('read:device_mode', require_response=True)
        return '{0}:{1}'.format(env.response.arguments[0], env.response.arguments[1])

    def set_device_mode(self, mode: int, param: int = 0) -> None:
        self.run('write:device_mode', bytes((mode & 0xFF, param & 0xFF)),
                 mode=mode, param=param)

    def enter_driver_mode(self) -> None:
        """Switch a macro-capable device into driver mode, like the daemon does."""
        if self.info.driver_mode:
            self.set_device_mode(0x03, 0x00)

    # -- brightness --------------------------------------------------------
    def _brightness_attribute(self, zone: str, write: bool) -> str:
        prefix = 'write:' if write else 'read:'
        candidates = ZONE_BRIGHTNESS.get(zone)
        if not candidates:
            raise NotSupported('unknown zone {0!r}'.format(zone))
        return self._first_supported(prefix + name for name in candidates)

    def get_brightness(self, zone: str = 'backlight') -> float:
        """Brightness as a percentage.

        Some devices accept a brightness but never report one back; upstream's
        daemon answers those from its persistence file, and so do we.
        """
        try:
            attribute = self._brightness_attribute(zone, write=False)
        except NotSupported:
            return float(self.persistence.get(self.serial, zone, 'brightness') or 0.0)
        env = self.run(attribute, require_response=True)
        raw = env.vars.get('brightness')
        if raw is None:
            raw = env.response.arguments[2]
        return round(int(raw) * 100.0 / 255.0, 1)

    def set_brightness(self, percent: float, zone: str = 'backlight') -> None:
        percent = _clamp(float(percent), 0.0, 100.0)
        raw = int(round(percent * 255.0 / 100.0))
        attribute = self._brightness_attribute(zone, write=True)
        # A couple of accessories echo the brightness they were last given
        # rather than reading it back, so the driver caches it; mirror that.
        self.run(attribute, str(raw).encode('ascii'),
                 brightness=raw, saved_brightness=raw)
        self.persistence.set(self.serial, zone, 'brightness', percent)

    # -- effects -----------------------------------------------------------
    def _declares(self, method_names: Iterable[str]) -> bool:
        """Whether the upstream device class lists any of these methods.

        Devices missing from the database (a product id newer than the bundled
        data) fall back to recipe support alone rather than reporting nothing.
        """
        if not self.info.methods:
            return True
        return any(name in self.info.methods for name in method_names)

    def _effect_attribute(self, zone: str, effect: str) -> str:
        suffix = EFFECT_ATTRIBUTE.get(effect)
        if suffix is None:
            raise NotSupported('unknown effect {0!r}'.format(effect))
        prefixes = ZONE_PREFIXES.get(zone)
        if prefixes is None:
            raise NotSupported('unknown zone {0!r}'.format(zone))
        return self._first_supported(
            'write:{0}{1}'.format(prefix, suffix) for prefix in prefixes)

    def _declares_effect(self, zone: str, effect: str) -> bool:
        suffix = EFFECT_METHOD_SUFFIX.get(effect)
        forms = ZONE_METHOD_FORMS.get(zone)
        if suffix is None or forms is None:
            return False
        names = [form.format(suffix=suffix) for form in forms]
        if effect == 'on' and zone != 'backlight':
            names.append('set_{0}_active'.format(zone))
        return self._declares(names)

    def has_effect(self, effect: str, zone: str = 'backlight') -> bool:
        if not self._declares_effect(zone, effect):
            return False
        try:
            self._effect_attribute(zone, effect)
        except NotSupported:
            return False
        return True

    def available_effects(self, zone: str = 'backlight') -> list:
        return [effect for effect in EFFECTS if self.has_effect(effect, zone)]

    def _apply(self, zone: str, effect: str, payload: bytes = b'\x01',
               **variables) -> None:
        attribute = self._effect_attribute(zone, effect)
        self.run(attribute, payload, **variables)
        self.persistence.set(self.serial, zone, 'effect', effect)

    def set_none(self, zone: str = 'backlight') -> None:
        self._apply(zone, 'none')

    def set_on(self, zone: str = 'backlight') -> None:
        self._apply(zone, 'on')

    def set_static(self, red: int, green: int, blue: int,
                   zone: str = 'backlight') -> None:
        self._apply(zone, 'static', _rgb_bytes(red, green, blue))
        self.persistence.set(self.serial, zone, 'colors',
                             [red, green, blue, 0, 255, 255, 0, 0, 255])

    def set_spectrum(self, zone: str = 'backlight') -> None:
        self._apply(zone, 'spectrum')

    def set_wave(self, direction: int = 1, zone: str = 'backlight') -> None:
        direction = _clamp(int(direction), 0, 2)
        self._apply(zone, 'wave', str(direction).encode('ascii'), direction=direction)
        self.persistence.set(self.serial, zone, 'wave_dir', direction)

    def set_wheel(self, direction: int = 1, zone: str = 'backlight') -> None:
        direction = _clamp(int(direction), 1, 2)
        self._apply(zone, 'wheel', str(direction).encode('ascii'), direction=direction)

    def set_reactive(self, red: int, green: int, blue: int, speed: int = 1,
                     zone: str = 'backlight') -> None:
        speed = _clamp(int(speed), 1, 4)
        payload = bytes((speed,)) + _rgb_bytes(red, green, blue)
        self._apply(zone, 'reactive', payload, speed=speed)
        self.persistence.set(self.serial, zone, 'speed', speed)

    def trigger_reactive(self) -> None:
        self.run('write:matrix_reactive_trigger', b'1')

    def set_blinking(self, red: int, green: int, blue: int,
                     zone: str = 'backlight') -> None:
        self._apply(zone, 'blinking', _rgb_bytes(red, green, blue))

    def set_breath_random(self, zone: str = 'backlight') -> None:
        self._apply(zone, 'breath_random', b'1')

    def set_breath_single(self, red: int, green: int, blue: int,
                          zone: str = 'backlight') -> None:
        self._apply(zone, 'breath_single', _rgb_bytes(red, green, blue))

    def set_breath_dual(self, red1: int, green1: int, blue1: int,
                        red2: int, green2: int, blue2: int,
                        zone: str = 'backlight') -> None:
        self._apply(zone, 'breath_dual',
                    _rgb_bytes(red1, green1, blue1) + _rgb_bytes(red2, green2, blue2))

    def set_breath_triple(self, red1: int, green1: int, blue1: int,
                          red2: int, green2: int, blue2: int,
                          red3: int, green3: int, blue3: int,
                          zone: str = 'backlight') -> None:
        self._apply(zone, 'breath_triple',
                    _rgb_bytes(red1, green1, blue1) + _rgb_bytes(red2, green2, blue2)
                    + _rgb_bytes(red3, green3, blue3))

    def set_starlight_random(self, speed: int = 1, zone: str = 'backlight') -> None:
        speed = _clamp(int(speed), 1, 3)
        self._apply(zone, 'starlight_random', bytes((speed,)), speed=speed)

    def set_starlight_single(self, red: int, green: int, blue: int, speed: int = 1,
                             zone: str = 'backlight') -> None:
        speed = _clamp(int(speed), 1, 3)
        self._apply(zone, 'starlight_single',
                    bytes((speed,)) + _rgb_bytes(red, green, blue), speed=speed)

    def set_starlight_dual(self, red1: int, green1: int, blue1: int,
                           red2: int, green2: int, blue2: int, speed: int = 1,
                           zone: str = 'backlight') -> None:
        speed = _clamp(int(speed), 1, 3)
        payload = (bytes((speed,)) + _rgb_bytes(red1, green1, blue1)
                   + _rgb_bytes(red2, green2, blue2))
        self._apply(zone, 'starlight_dual', payload, speed=speed)

    # -- custom frames -----------------------------------------------------
    @property
    def matrix_dimensions(self) -> Optional[tuple]:
        return self.info.matrix_dims

    def set_key_row(self, payload: bytes) -> None:
        """Write raw row data: ``row, start_col, stop_col, r, g, b, ...``."""
        self.run('write:matrix_custom_frame', bytes(payload))

    def set_custom(self, zone: str = 'backlight') -> None:
        """Display the frame most recently pushed with :meth:`set_key_row`."""
        self._apply(zone, 'custom')

    def set_custom_frame(self, rows: Iterable) -> None:
        """Push a full frame.

        `rows` is an iterable of ``(row_index, start_col, colours)`` where
        `colours` is a flat RGB byte sequence.
        """
        for row_index, start_col, colours in rows:
            colours = bytes(colours)
            stop_col = start_col + len(colours) // 3 - 1
            self.set_key_row(bytes((row_index, start_col, stop_col)) + colours)
        self.run(self._effect_attribute('backlight', 'custom'), b'1')

    # -- mouse: DPI and polling -------------------------------------------
    def get_dpi(self) -> tuple:
        env = self.run('read:dpi', require_response=True)
        if self.uses_byte_dpi:
            args = env.response.arguments
            return args[0] * 100, args[1] * 100
        if 'dpi_x' in env.vars:
            return int(env.vars['dpi_x']), int(env.vars.get('dpi_y', env.vars['dpi_x']))
        args = env.response.arguments
        return (args[1] << 8) | args[2], (args[3] << 8) | args[4]

    @property
    def uses_byte_dpi(self) -> bool:
        """Older mice take DPI as two scaled bytes rather than two 16-bit words."""
        return 'set_dpi_xy_byte' in self.info.methods

    def set_dpi(self, dpi_x: int, dpi_y: Optional[int] = None) -> None:
        if dpi_y is None:
            dpi_y = dpi_x
        maximum = self.info.dpi_max or 45000
        dpi_x = _clamp(int(dpi_x), 100, maximum)
        dpi_y = _clamp(int(dpi_y), 100, maximum)
        if self.uses_byte_dpi:
            scaled = (_clamp(dpi_x // 100, 1, 255), _clamp(dpi_y // 100, 1, 255))
            self.run('write:dpi', struct.pack('>BB', *scaled),
                     dpi_x_byte=scaled[0], dpi_y_byte=scaled[1],
                     dpi_x=dpi_x, dpi_y=dpi_y)
        else:
            self.run('write:dpi', struct.pack('>HH', dpi_x, dpi_y),
                     dpi_x=dpi_x, dpi_y=dpi_y)
        self.persistence.set_device_value(self.serial, 'dpi', [dpi_x, dpi_y])

    def get_dpi_stages(self) -> tuple:
        env = self.run('read:dpi_stages', require_response=True)
        args = env.response.arguments
        active = args[1]
        count = args[2]
        stages = []
        offset = 3
        for _ in range(min(count, 10)):
            stages.append(((args[offset + 1] << 8) | args[offset + 2],
                           (args[offset + 3] << 8) | args[offset + 4]))
            offset += 7
        return active, stages

    def set_dpi_stages(self, active_stage: int, stages: list) -> None:
        """`stages` is a list of ``(dpi_x, dpi_y)`` pairs."""
        flat: list = []
        payload = struct.pack('B', _clamp(int(active_stage), 1, max(len(stages), 1)))
        for dpi_x, dpi_y in stages:
            payload += struct.pack('>HH', int(dpi_x), int(dpi_y))
            flat.extend((int(dpi_x), int(dpi_y)))
        self.run('write:dpi_stages', payload,
                 stages_count=len(stages), active_stage=int(active_stage), dpi=flat)
        self.persistence.set_device_value(
            self.serial, 'dpi_stages', {'active': active_stage, 'stages': stages})

    def get_poll_rate(self) -> int:
        env = self.run('read:poll_rate')
        rate = env.vars.get('polling_rate')
        if rate is None:
            raise NotSupported('{0} does not report its polling rate'.format(self.name))
        return int(rate)

    def set_poll_rate(self, rate: int) -> None:
        rate = int(rate)
        self.run('write:poll_rate', str(rate).encode('ascii'), polling_rate=rate)
        self.persistence.set_device_value(self.serial, 'poll_rate', rate)
        # The Orochi 2011 has no readback; the driver shadows the wire code.
        self.persistence.set_device_value(
            self.serial, 'orochi2011_poll', {1000: 0x01, 500: 0x02, 125: 0x08}.get(rate, 0x02))

    def supported_poll_rates(self) -> list:
        if self.info.poll_rates:
            return list(self.info.poll_rates)
        return [125, 500, 1000]

    # -- power -------------------------------------------------------------
    def get_battery_level(self) -> float:
        env = self.run('read:charge_level', require_response=True)
        return round(env.response.arguments[1] * 100.0 / 255.0, 1)

    def is_charging(self) -> bool:
        env = self.run('read:charge_status', require_response=True)
        return bool(env.response.arguments[1])

    def get_idle_time(self) -> int:
        env = self.run('read:device_idle_time', require_response=True)
        args = env.response.arguments
        return (args[0] << 8) | args[1]

    def set_idle_time(self, seconds: int) -> None:
        seconds = _clamp(int(seconds), 60, 900)
        self.run('write:device_idle_time', str(seconds).encode('ascii'),
                 idle_time=seconds)

    def get_low_battery_threshold(self) -> float:
        env = self.run('read:charge_low_threshold', require_response=True)
        return round(env.response.arguments[0] * 100.0 / 255.0, 1)

    def set_low_battery_threshold(self, percent: float) -> None:
        raw = int(round(_clamp(float(percent), 0.0, 100.0) * 255.0 / 100.0))
        self.run('write:charge_low_threshold', str(raw).encode('ascii'), threshold=raw)

    # -- keyboard extras ---------------------------------------------------
    def get_game_mode(self) -> bool:
        env = self.run('read:game_led_state', require_response=True)
        return bool(env.response.arguments[2])

    def set_game_mode(self, enabled: bool) -> None:
        value = 1 if enabled else 0
        self.run('write:game_led_state', str(value).encode('ascii'), enabled=value)

    def get_macro_mode(self) -> bool:
        env = self.run('read:macro_led_state', require_response=True)
        return bool(env.response.arguments[2])

    def set_macro_mode(self, enabled: bool) -> None:
        value = 1 if enabled else 0
        self.run('write:macro_led_state', str(value).encode('ascii'), enabled=value)

    def set_macro_effect(self, effect: int) -> None:
        self.run('write:macro_led_effect', str(int(effect)).encode('ascii'),
                 enabled=int(effect))

    def set_fn_toggle(self, state: int) -> None:
        self.run('write:fn_toggle', str(int(state)).encode('ascii'), state=int(state))

    def get_keyboard_layout(self) -> str:
        env = self.run('read:kbd_layout', require_response=True)
        return 'layout_{0:02x}'.format(env.response.arguments[2])

    # -- mouse extras ------------------------------------------------------
    def get_scroll_mode(self) -> int:
        env = self.run('read:scroll_mode', require_response=True)
        return int(env.response.arguments[1])

    def set_scroll_mode(self, mode: int) -> None:
        self.run('write:scroll_mode', str(int(mode)).encode('ascii'),
                 scroll_mode=int(mode))

    def get_scroll_acceleration(self) -> bool:
        env = self.run('read:scroll_acceleration', require_response=True)
        return bool(env.response.arguments[1])

    def set_scroll_acceleration(self, enabled: bool) -> None:
        value = 1 if enabled else 0
        self.run('write:scroll_acceleration', str(value).encode('ascii'),
                 acceleration=value)

    def get_scroll_smart_reel(self) -> bool:
        env = self.run('read:scroll_smart_reel', require_response=True)
        return bool(env.response.arguments[1])

    def set_scroll_smart_reel(self, enabled: bool) -> None:
        value = 1 if enabled else 0
        self.run('write:scroll_smart_reel', str(value).encode('ascii'),
                 smart_reel=value)

    # -- capability report -------------------------------------------------
    def _feature(self, attribute: str, *method_names: str) -> bool:
        """A feature is present when both the recipe and the device class agree."""
        return self._declares(method_names) and self.supports(attribute)

    def _software_effects_possible(self) -> bool:
        """Whether a host-rendered effect has enough LEDs to be worth drawing."""
        if not self._feature('write:matrix_custom_frame', 'set_key_row'):
            return False
        rows, columns = self.info.matrix_dims or (1, 1)
        return rows * columns > 1

    def capabilities(self) -> dict:
        """What this device can actually do, probed against the recipe table."""
        if self._capabilities is not None:
            return self._capabilities

        zones = {}
        for zone in ZONE_PREFIXES:
            effects = self.available_effects(zone)
            has_brightness = False
            if self._declares(ZONE_BRIGHTNESS_METHODS.get(zone, ())):
                try:
                    self._brightness_attribute(zone, write=True)
                    has_brightness = True
                except NotSupported:
                    pass
            # ``custom`` is device-wide rather than per-zone, so a zone that
            # offers nothing else is not a real lighting zone -- a mouse would
            # otherwise appear to have a backlight it does not have.
            if effects == ['custom'] and not has_brightness:
                continue
            if effects or has_brightness:
                zones[zone] = {'effects': effects, 'brightness': has_brightness}

        capabilities = {
            'name': self.info.name,
            'type': self.info.type,
            'vid': self.info.vid,
            'pid': self.info.pid,
            'image': self.info.image,
            'zones': zones,
            'matrix': bool(self.info.has_matrix),
            'matrix_dimensions': list(self.info.matrix_dims) if self.info.matrix_dims else None,
            'dedicated_macro_keys': self.info.dedicated_macro_keys,
            'dpi': self._feature('read:dpi', 'get_dpi_xy', 'get_dpi_xy_byte'),
            'dpi_stages': self._feature('read:dpi_stages', 'get_dpi_stages'),
            'max_dpi': self.info.dpi_max,
            'poll_rate': self._feature('read:poll_rate', 'get_poll_rate'),
            'supported_poll_rates': self.supported_poll_rates(),
            'battery': self._feature('read:charge_level', 'get_battery'),
            'idle_time': self._feature('read:device_idle_time', 'get_idle_time'),
            'game_mode': self._feature('read:game_led_state', 'get_game_mode'),
            'macro_mode': self._feature('read:macro_led_state', 'get_macro_mode'),
            'keyboard_layout': self._feature('read:kbd_layout', 'get_keyboard_layout'),
            'scroll_mode': self._feature('read:scroll_mode', 'get_scroll_mode'),
            'custom_frame': self._feature('write:matrix_custom_frame', 'set_key_row'),
            'software_effects': self._software_effects_possible(),
            'transport': {
                'path': self.transport.info.path,
                'interface': self.transport.info.interface,
                'wait_us': int(self.transport.wait * 1_000_000),
            },
        }
        self._capabilities = capabilities
        return capabilities

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self.transport.close()

    def restore(self) -> None:
        """Re-apply the last known lighting state for every zone."""
        serial = self.serial
        for zone in self.capabilities()['zones']:
            state = self.persistence.zone(serial, zone)
            effect = state.get('effect')
            colours = state.get('colors') or [0, 255, 0, 0, 255, 255, 0, 0, 255]
            try:
                if state.get('brightness') is not None:
                    self.set_brightness(state['brightness'], zone)
                if effect == 'static':
                    self.set_static(colours[0], colours[1], colours[2], zone)
                elif effect == 'breath_single':
                    self.set_breath_single(colours[0], colours[1], colours[2], zone)
                elif effect == 'breath_dual':
                    self.set_breath_dual(*colours[:6], zone=zone)
                elif effect == 'breath_random':
                    self.set_breath_random(zone)
                elif effect == 'wave':
                    self.set_wave(state.get('wave_dir', 1), zone)
                elif effect == 'reactive':
                    self.set_reactive(colours[0], colours[1], colours[2],
                                      state.get('speed', 1), zone)
                elif effect == 'none':
                    self.set_none(zone)
                elif effect == 'spectrum':
                    self.set_spectrum(zone)
            except (NotSupported, DeviceError, RecipeError):
                logger.debug('could not restore %s on %s', zone, self.name, exc_info=True)
