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
    WAVE_BUILDER, Environment, NotSupported, RecipeError, RecipeTable, execute, get_recipes,
    uses_builder,
)
from ..protocol import buttons
from ..protocol import macros as macros_protocol
from ..protocol.report import RazerReport, RazerReportError, Status
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

#: Keyswitch optimization -> the mode byte upstream's driver takes.  Named rather
#: than the bool upstream's client uses: "optimization on" says nothing about
#: which of the two a keyboard is in, and they are the names Synapse shows.
KEYSWITCH_MODES = {'typing': 0x00, 'gaming': 0x01}

#: What ``read:keyswitch_optimization`` finds in argument 1 in Typing mode.
KEYSWITCH_TYPING_MARKER = 0x14


class DeviceError(Exception):
    """A device operation failed; ``status`` is the reply's status byte when one came."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


#: Product ids whose onboard button memory has been read and written on real
#: hardware.  The commands are not in upstream OpenRazer, so nothing else is
#: offered them until someone checks.
ONBOARD_BUTTON_PIDS = frozenset({0x0099})  # Basilisk V3

#: Metadata block of an onboard profile: 250 bytes, read 75 at a time, with the
#: profile's name as ASCII from 0x10 to 0x73.
_PROFILE_META_LENGTH = 0xFA
_PROFILE_META_CHUNK = 75
_PROFILE_NAME = slice(0x10, 0x74)

#: How much of what a mouse says about its macros is believed: a reply that
#: never says "that was the last" must not keep the daemon reading for good.
_MACRO_IDS_MAX = 256
_MACRO_BYTES_MAX = 0x4000


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
        #: Set once this port has put the device into driver mode, so that it
        #: is handed back on the way out and only then.
        self._in_driver_mode = False

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

    def enter_driver_mode(self, keys_handled: bool = False) -> None:
        """Put the device in the mode upstream leaves it in once set up.

        Two layers, as on Linux.  The kernel driver sets a mode when it binds
        -- most accessories go into driver mode "just to function", keyboards
        are reset to normal mode -- and that is the ``probe:device_mode``
        recipe.  The daemon then moves macro-capable devices into driver mode.

        On a keyboard, driver mode hands the macro keys *and the Fn layer* to
        the host: the firmware stops turning Fn+F1 into Mute and leaves that to
        whoever reads the vendor report.  So a keyboard only goes there when
        `keys_handled` says something on this side is reading it; otherwise it
        stays in normal mode, where the firmware keeps Fn working by itself.
        """
        if self.supports('probe:device_mode'):
            self.run('probe:device_mode')
        if self.info.driver_mode and (keys_handled or self.driver != 'kbd'):
            self.set_device_mode(0x03, 0x00)
            self._in_driver_mode = True

    def leave_driver_mode(self) -> None:
        """Hand the device back to its firmware, as upstream's daemon does on exit.

        A keyboard left in driver mode with nothing reading its vendor report
        has dead macro keys and a dead Fn layer until it is unplugged.
        """
        if self._in_driver_mode:
            self.set_device_mode(0x00, 0x00)
            self._in_driver_mode = False

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

    def _remember_colours(self, zone: str, *colours) -> None:
        """Save the colours an effect was given, so restore() and profiles see them.

        Slots the effect did not use keep what they held, the way upstream's
        persistence keeps a zone's three colours.
        """
        saved = list(self.persistence.get(self.serial, zone, 'colors') or [])
        saved = (saved + [0] * 9)[:9]
        for index, colour in enumerate(colours[:3]):
            saved[index * 3:index * 3 + 3] = [int(c) for c in colour]
        self.persistence.set(self.serial, zone, 'colors', saved)

    def set_none(self, zone: str = 'backlight') -> None:
        self._apply(zone, 'none')

    def set_on(self, zone: str = 'backlight') -> None:
        self._apply(zone, 'on')

    def set_static(self, red: int, green: int, blue: int,
                   zone: str = 'backlight') -> None:
        self._apply(zone, 'static', _rgb_bytes(red, green, blue))
        self._remember_colours(zone, (red, green, blue))

    def set_spectrum(self, zone: str = 'backlight') -> None:
        self._apply(zone, 'spectrum')

    def has_wave_speed(self, zone: str = 'backlight') -> bool:
        """Whether the wave on `zone` is the kind that takes a speed."""
        try:
            attribute = self._effect_attribute(zone, 'wave')
            steps, _ = self.recipes.lookup(self.driver, attribute, self.info.pid)
        except (NotSupported, RecipeError):
            return False
        return uses_builder(steps, WAVE_BUILDER)

    def set_wave(self, direction: int = 1, zone: str = 'backlight',
                 speed: Optional[int] = None) -> None:
        """The wave; `speed` is the raw byte the device takes, 1 to 255.

        The byte is a delay: on the Mouse Dock Pro 10 is a very fast wave and
        200 a slow one (issue #22).  The driver always sends 40 there.
        """
        direction = _clamp(int(direction), 0, 2)
        variables = {'direction': direction}
        if speed is not None:
            if not self.has_wave_speed(zone):
                raise NotSupported("{0}'s wave has no speed to set".format(self.name))
            speed = _clamp(int(speed), 1, 255)
            variables['wave_speed'] = speed
        self._apply(zone, 'wave', str(direction).encode('ascii'), **variables)
        self.persistence.set(self.serial, zone, 'wave_dir', direction)
        if speed is not None or self.persistence.get(self.serial, zone, 'wave_speed') is not None:
            self.persistence.set(self.serial, zone, 'wave_speed', speed)

    def set_wave_with_speed(self, direction: int, speed: int, zone: str = 'backlight') -> None:
        """:meth:`set_wave` with the speed second, for a client that names the
        zone apart from the arguments, as Studio does."""
        self.set_wave(direction, zone, speed=speed)

    def set_wheel(self, direction: int = 1, zone: str = 'backlight') -> None:
        direction = _clamp(int(direction), 1, 2)
        self._apply(zone, 'wheel', str(direction).encode('ascii'), direction=direction)
        self.persistence.set(self.serial, zone, 'wave_dir', direction)

    def set_reactive(self, red: int, green: int, blue: int, speed: int = 1,
                     zone: str = 'backlight') -> None:
        speed = _clamp(int(speed), 1, 4)
        payload = bytes((speed,)) + _rgb_bytes(red, green, blue)
        self._apply(zone, 'reactive', payload, speed=speed)
        self.persistence.set(self.serial, zone, 'speed', speed)
        self._remember_colours(zone, (red, green, blue))

    def trigger_reactive(self) -> None:
        self.run('write:matrix_reactive_trigger', b'1')

    def set_blinking(self, red: int, green: int, blue: int,
                     zone: str = 'backlight') -> None:
        self._apply(zone, 'blinking', _rgb_bytes(red, green, blue))
        self._remember_colours(zone, (red, green, blue))

    def set_breath_random(self, zone: str = 'backlight') -> None:
        self._apply(zone, 'breath_random', b'1')

    def set_breath_single(self, red: int, green: int, blue: int,
                          zone: str = 'backlight') -> None:
        self._apply(zone, 'breath_single', _rgb_bytes(red, green, blue))
        self._remember_colours(zone, (red, green, blue))

    def set_breath_dual(self, red1: int, green1: int, blue1: int,
                        red2: int, green2: int, blue2: int,
                        zone: str = 'backlight') -> None:
        self._apply(zone, 'breath_dual',
                    _rgb_bytes(red1, green1, blue1) + _rgb_bytes(red2, green2, blue2))
        self._remember_colours(zone, (red1, green1, blue1), (red2, green2, blue2))

    def set_breath_triple(self, red1: int, green1: int, blue1: int,
                          red2: int, green2: int, blue2: int,
                          red3: int, green3: int, blue3: int,
                          zone: str = 'backlight') -> None:
        self._apply(zone, 'breath_triple',
                    _rgb_bytes(red1, green1, blue1) + _rgb_bytes(red2, green2, blue2)
                    + _rgb_bytes(red3, green3, blue3))
        self._remember_colours(zone, (red1, green1, blue1), (red2, green2, blue2),
                               (red3, green3, blue3))

    def set_starlight_random(self, speed: int = 1, zone: str = 'backlight') -> None:
        speed = _clamp(int(speed), 1, 3)
        self._apply(zone, 'starlight_random', bytes((speed,)), speed=speed)
        self.persistence.set(self.serial, zone, 'speed', speed)

    def set_starlight_single(self, red: int, green: int, blue: int, speed: int = 1,
                             zone: str = 'backlight') -> None:
        speed = _clamp(int(speed), 1, 3)
        self._apply(zone, 'starlight_single',
                    bytes((speed,)) + _rgb_bytes(red, green, blue), speed=speed)
        self.persistence.set(self.serial, zone, 'speed', speed)
        self._remember_colours(zone, (red, green, blue))

    def set_starlight_dual(self, red1: int, green1: int, blue1: int,
                           red2: int, green2: int, blue2: int, speed: int = 1,
                           zone: str = 'backlight') -> None:
        speed = _clamp(int(speed), 1, 3)
        payload = (bytes((speed,)) + _rgb_bytes(red1, green1, blue1)
                   + _rgb_bytes(red2, green2, blue2))
        self._apply(zone, 'starlight_dual', payload, speed=speed)
        self.persistence.set(self.serial, zone, 'speed', speed)
        self._remember_colours(zone, (red1, green1, blue1), (red2, green2, blue2))

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
        attribute = self._effect_attribute('backlight', 'custom')
        # On the ARGB controller, switching to custom mode after the frame wipes
        # the colours just written, so there the mode goes first.
        if self.transport.argb_info is not None:
            self.run(attribute, b'1')
        for row_index, start_col, colours in rows:
            colours = bytes(colours)
            stop_col = start_col + len(colours) // 3 - 1
            self.set_key_row(bytes((row_index, start_col, stop_col)) + colours)
        if self.transport.argb_info is None:
            self.run(attribute, b'1')

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

    def get_keyswitch_optimization(self) -> str:
        """``'typing'`` or ``'gaming'``, as the keyboard reports it."""
        env = self.run('read:keyswitch_optimization', require_response=True)
        # Not env.vars['state']: the driver sets it inside an if, and the
        # transpiled recipe keeps the assignment without the test, so it is
        # always 0.  Anything but the marker is Gaming, as in the driver.
        marker = env.response.arguments[1]
        return 'typing' if marker == KEYSWITCH_TYPING_MARKER else 'gaming'

    def set_keyswitch_optimization(self, mode: str) -> None:
        """Choose ``'typing'`` (debounce delay) or ``'gaming'`` (no debounce)."""
        name = str(mode).lower()
        if name not in KEYSWITCH_MODES:
            # The builders send an empty report for a byte they do not know,
            # which would pass for Gaming on the keyboard.
            raise ValueError('keyswitch optimization is one of {0}, not {1!r}'.format(
                ', '.join(KEYSWITCH_MODES), mode))
        value = KEYSWITCH_MODES[name]
        self.run('write:keyswitch_optimization', str(value).encode('ascii'), mode=value)
        self.persistence.set_device_value(self.serial, 'keyswitch_optimization', name)

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

    # -- mouse: onboard button memory --------------------------------------
    def _onboard_txid(self) -> int:
        """The transaction id this device's own recipes use, 0x1F if none say."""
        try:
            steps, _ = self.recipes.lookup(self.driver, 'read:dpi', self.info.pid)
        except (NotSupported, RecipeError):
            return 0x1F
        for step in steps:
            if step.get('op') == 'txid' and step['value'].get('k') == 'const':
                return int(step['value']['value']) & 0xFF
        return 0x1F

    def _require_onboard(self) -> None:
        if self.info.pid not in ONBOARD_BUTTON_PIDS:
            raise NotSupported('{0} has no verified onboard button memory'.format(self.name))

    def _onboard(self, command_class: int, command_id: int, size: int,
                 args: bytes = b'') -> bytes:
        """Send one onboard-memory command; returns the reply's full argument block.

        The full block, not ``payload``: the reply's data size may only echo the
        request's.  A read that is still BUSY is an error -- the transport hands
        BUSY back as success, and its arguments are then just our own request.
        """
        self._require_onboard()
        request = RazerReport(command_class, command_id, size)
        request.transaction_id = self._onboard_txid()
        request.set_arguments(0, args)
        try:
            response = self.transport.send_payload(request)
        except RazerReportError as error:
            raise DeviceError(str(error), status=error.status) from error
        if command_id & 0x80 and response.status != Status.SUCCESSFUL:
            raise DeviceError('{0} replied {1} to 0x{2:02x},0x{3:02x}'.format(
                self.name, Status.name(response.status), command_class, command_id),
                status=response.status)
        return bytes(response.arguments)

    @staticmethod
    def _onboard_ids(profile, button, layer) -> tuple:
        """Check ids before any report goes out; ``layer`` is a name or 0/1.

        Strict about types: these arrive as JSON, where ``1.0`` or ``true``
        would otherwise slip past a range check and fail half-way through.
        """
        layer_id = buttons.LAYERS.get(layer) if isinstance(layer, str) else layer
        if type(layer_id) is not int or layer_id not in buttons.LAYERS.values():
            raise ValueError('unknown layer {0!r}: use normal or hypershift'.format(layer))
        if type(profile) is not int or not 0 <= profile <= 0xFF:
            raise ValueError('profile must be 0..255, got {0!r}'.format(profile))
        if type(button) is not int or not 0 < button <= 0xFF:
            raise ValueError('button must be 1..255, got {0!r}'.format(button))
        return profile, button, layer_id

    def get_onboard_profiles(self) -> dict:
        """Profiles stored on the mouse: ``{active, max, profiles: [{id, name}]}``."""
        self._require_onboard()
        with self._lock:
            inventory = self._onboard(0x05, 0x81, 0x00)
            active = self.get_active_onboard_profile()
            profiles = [{'id': pid, 'name': self._onboard_profile_name(pid)}
                        for pid in inventory[1:] if pid]
        return {'active': active, 'max': inventory[0], 'profiles': profiles}

    def get_active_onboard_profile(self) -> Optional[int]:
        """The profile the mouse is on -- one read, cheap enough to poll for the
        profile button.  ``None`` on firmware that cannot tell."""
        try:
            return self._onboard(0x05, 0x84, 0x00)[0]
        except DeviceError as error:
            if error.status not in (Status.FAILURE, Status.NOT_SUPPORTED):
                raise  # busy or timed out: say so rather than guess
            return None

    def set_active_onboard_profile(self, profile: int) -> None:
        """Switch the mouse to one of its stored profiles, as its profile button does.

        Read back afterwards: a mouse that did not switch says so.
        """
        self._require_onboard()
        if type(profile) is not int:
            raise ValueError('profile must be a number, got {0!r}'.format(profile))
        with self._lock:
            inventory = self._onboard(0x05, 0x81, 0x00)
            if profile not in [p for p in inventory[1:] if p]:
                raise ValueError('{0} has no profile {1}'.format(self.name, profile))
            self._onboard(0x05, 0x04, 0x01, bytes((profile,)))
            now = self.get_active_onboard_profile()
            if now != profile:
                raise DeviceError('{0} is still on profile {1}'.format(self.name, now))

    def _onboard_profile_name(self, profile: int) -> Optional[str]:
        meta = bytearray(_PROFILE_META_LENGTH)
        try:
            for offset in range(0, _PROFILE_NAME.stop, _PROFILE_META_CHUNK):
                args = bytes((profile, offset >> 8, offset & 0xFF, 0x00, _PROFILE_META_LENGTH))
                reply = self._onboard(0x05, 0x88, 0x50, args)
                meta[offset:offset + _PROFILE_META_CHUNK] = reply[5:5 + _PROFILE_META_CHUNK]
        except DeviceError:
            return None
        raw = bytes(meta[_PROFILE_NAME]).split(b'\0', 1)[0]
        if raw and all(0x20 <= c <= 0x7E for c in raw):
            return raw.decode('ascii')
        return None

    def get_buttons(self) -> list:
        """The buttons the mouse can bind, in its own order: ``[{id, name}]``."""
        reply = self._onboard(0x02, 0x84, 0x50)
        return [{'id': i, 'name': buttons.button_name(i)} for i in reply[1:1 + reply[0]]]

    def _read_binding(self, profile: int, button: int, layer: int) -> buttons.Binding:
        reply = self._onboard(0x02, 0x8C, 0x0A, bytes((profile, button, layer)))
        return buttons.Binding.from_raw(reply[3], reply[4], reply[5:10])

    def get_button_binding(self, profile: int, button: int, layer='normal') -> dict:
        """``{action, fn, size, data}`` for one button; unknown functions keep their bytes."""
        self._require_onboard()
        profile, button, layer = self._onboard_ids(profile, button, layer)
        return buttons.binding_to_dict(self._read_binding(profile, button, layer))

    def _write_binding(self, profile: int, button: int, layer: int,
                       binding: buttons.Binding) -> None:
        self._onboard(0x02, 0x0C, 0x0A,
                      bytes((profile, button, layer, binding.fn, binding.size)) + binding.data)

    def _set_binding(self, profile: int, button: int, layer: int,
                     new: buttons.Binding) -> buttons.Binding:
        """Write, read back, and put the old binding back if the mouse disagrees."""
        old = self._read_binding(profile, button, layer)
        self._write_binding(profile, button, layer, new)
        got = self._read_binding(profile, button, layer)
        if got != new:
            self._write_binding(profile, button, layer, old)
            back = self._read_binding(profile, button, layer)
            outcome = ('old binding restored' if back == old else
                       'restore FAILED, it is now {0} (was {1})'.format(
                           buttons.describe(back), buttons.describe(old)))
            raise DeviceError('{0} kept {1} instead of {2} on {3}; {4}'.format(
                self.name, buttons.describe(got), buttons.describe(new),
                buttons.button_name(button), outcome))
        return old

    def set_button_binding(self, profile: int, button: int, action: str,
                           layer='normal') -> dict:
        """Rebind one button in onboard memory; returns what it was bound to."""
        self._require_onboard()
        profile, button, layer = self._onboard_ids(profile, button, layer)
        new = buttons.parse_action(str(action))
        with self._lock:
            return buttons.binding_to_dict(self._set_binding(profile, button, layer, new))

    def backup_button_bindings(self, profile: int) -> dict:
        """Every binding of one profile, both layers, in razerbind's file format."""
        self._require_onboard()
        profile = self._onboard_ids(profile, 1, 0)[0]
        rows = []
        with self._lock:
            for entry in self.get_buttons():
                for layer in buttons.LAYERS.values():
                    binding = self._read_binding(profile, entry['id'], layer)
                    rows.append({'button': entry['id'], 'layer': layer, 'fn': binding.fn,
                                 'size': binding.size, 'data': list(binding.data)})
        return {'profile': profile, 'bindings': rows}

    def restore_button_bindings(self, doc: dict) -> int:
        """Write back a backup; returns how many bindings changed.

        The whole document is checked before the first report goes out, so a
        bad row cannot leave a profile half restored.
        """
        self._require_onboard()
        rows = []
        try:
            for row in doc['bindings']:
                profile, button, layer = self._onboard_ids(doc['profile'], row['button'],
                                                           row['layer'])
                rows.append((profile, button, layer, buttons.binding_from_dict(row)))
        except KeyError as error:
            # A missing field is a bad document, not a bug in the daemon.
            raise ValueError('backup is missing {0}'.format(error)) from None
        written = 0
        with self._lock:
            for profile, button, layer, want in rows:
                if self._read_binding(profile, button, layer) != want:
                    self._set_binding(profile, button, layer, want)
                    written += 1
        return written

    def get_onboard_macros(self) -> list:
        """Macros stored on the mouse: ``[{id, name, summary, events}]``. Read only."""
        self._require_onboard()
        macros = []
        with self._lock:
            for macro_id in self._onboard_macro_ids():
                key = bytes((macro_id >> 8, macro_id & 0xFF))
                meta = b''.join(self._onboard(0x06, 0x8C, 0x50, key + bytes((0, offset)))[6:6 + 64]
                                for offset in (0, 64))
                name = bytes(meta[20:120]).split(b'\0', 1)[0].decode('utf-8', 'replace')
                reply = self._onboard(0x06, 0x88, 0x06, key)
                size = min(int.from_bytes(reply[2:6], 'big'), _MACRO_BYTES_MAX)
                data = bytearray()
                while len(data) < size:
                    chunk = min(72, size - len(data))
                    reply = self._onboard(0x06, 0x89, 0x50,
                                          key + len(data).to_bytes(4, 'big') + bytes((chunk,)))
                    if not reply[6]:
                        break  # nothing more is coming; keep what was read
                    data += reply[7:7 + reply[6]]
                events = macros_protocol.decode_events(bytes(data))
                macros.append({'id': macro_id, 'name': name, 'events': events,
                               'summary': macros_protocol.summarize(events)})
        return macros

    def _onboard_macro_ids(self) -> list:
        ids: list = []
        while len(ids) < _MACRO_IDS_MAX:
            reply = self._onboard(0x06, 0x8B, 0x50, bytes((len(ids) >> 8, len(ids) & 0xFF)))
            count = min(reply[2] << 8 | reply[3], 38)  # 38 ids fill the 80-byte reply
            page = [reply[4 + 2 * i] << 8 | reply[5 + 2 * i] for i in range(count)]
            if count < 32 or set(page) <= set(ids):
                # Synapse reads 32 a time, so fewer means that was the last; a
                # page of ids already read means the mouse is repeating itself.
                return ids + [i for i in page if i not in ids]
            ids += page
        return ids[:_MACRO_IDS_MAX]

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
                zones[zone] = {'effects': effects, 'brightness': has_brightness,
                               'wave_speed': 'wave' in effects and self.has_wave_speed(zone)}

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
            'keyswitch_optimization': self._feature('read:keyswitch_optimization',
                                                    'get_keyswitch_optimization'),
            'scroll_mode': self._feature('read:scroll_mode', 'get_scroll_mode'),
            'custom_frame': self._feature('write:matrix_custom_frame', 'set_key_row'),
            'onboard_buttons': self.info.pid in ONBOARD_BUTTON_PIDS,
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
            state = self.persistence.saved_zone(serial, zone)
            if state is None:
                continue
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
                elif effect == 'breath_triple':
                    self.set_breath_triple(*colours[:9], zone=zone)
                elif effect == 'breath_random':
                    self.set_breath_random(zone)
                elif effect == 'blinking':
                    self.set_blinking(colours[0], colours[1], colours[2], zone)
                elif effect == 'wave':
                    speed = state.get('wave_speed')
                    self.set_wave(state.get('wave_dir', 1), zone,
                                  speed=speed if speed and self.has_wave_speed(zone) else None)
                elif effect == 'wheel':
                    self.set_wheel(state.get('wave_dir', 1), zone)
                elif effect == 'reactive':
                    self.set_reactive(colours[0], colours[1], colours[2],
                                      state.get('speed', 1), zone)
                elif effect == 'starlight_random':
                    self.set_starlight_random(state.get('speed', 1), zone)
                elif effect == 'starlight_single':
                    self.set_starlight_single(colours[0], colours[1], colours[2],
                                              state.get('speed', 1), zone)
                elif effect == 'starlight_dual':
                    self.set_starlight_dual(*colours[:6], speed=state.get('speed', 1),
                                            zone=zone)
                elif effect == 'none':
                    self.set_none(zone)
                elif effect == 'spectrum':
                    self.set_spectrum(zone)
            except (NotSupported, DeviceError, RecipeError):
                logger.debug('could not restore %s on %s', zone, self.name, exc_info=True)
