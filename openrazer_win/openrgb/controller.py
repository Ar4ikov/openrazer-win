"""Each device as OpenRGB sees it: one RGB controller.

Its zones, LEDs and modes all come from what the device says it can do, so
nothing here is written per model.  A device has LEDs in one of three ways:

- a **frame**: a matrix it takes a custom frame on, colour by colour -- a
  keyboard's keys, a mousepad's edge, a mouse's underglow;
- **zones** that each take one colour of their own, a mouse's logo and wheel;
- a headset's **ears**, which take their colours together.

Its modes are the effects it has of its own -- static, breathing, spectrum,
wave, reactive, starlight -- and Direct, the per-LED colours OpenRGB streams.
A device that can show neither a frame nor a colour per zone has no Direct:
there is nothing honest to show a colour per LED on.
"""
from __future__ import annotations

import dataclasses
import math
from typing import Callable, List, Optional

from .._version import __version__
from ..effects.frame import keymap_for
from . import protocol as p

#: Our device types, as OpenRGB has them.
DEVICE_TYPES = {
    'keyboard': p.KEYBOARD, 'mouse': p.MOUSE, 'mousemat': p.MOUSEMAT,
    'headset': p.HEADSET, 'keypad': p.KEYPAD,
}

#: Accessories OpenRGB has a type of its own for, by a word in their name.
ACCESSORY_TYPES = (
    ('Base Station', p.HEADSET_STAND), ('Nommo', p.SPEAKER),
    ('Addressable RGB', p.LEDSTRIP), ('Lian Li', p.CASE), ('Tomahawk', p.CASE),
)

#: A zone of ours, by the name OpenRGB users know it by.
ZONE_NAMES = {
    'backlight': 'Backlight', 'logo': 'Logo', 'scroll': 'Scroll Wheel', 'left': 'Left',
    'right': 'Right', 'charging': 'Charging', 'fast_charging': 'Fast Charging',
    'fully_charged': 'Fully Charged',
}

#: What the one zone of a frame is called, by the kind of device.
FRAME_NAMES = {'keyboard': 'Keyboard', 'keypad': 'Keypad', 'mouse': 'Mouse',
               'mousemat': 'Mousemat', 'headset': 'Headset'}

FRAME, ZONES, EARS = 'frame', 'zones', 'ears'

#: The byte a wave's speed is sent as when none was chosen, as the driver does.
WAVE_SPEED_DEFAULT = 40

BLACK = (0, 0, 0)


class Layout:
    """Where a device's LEDs are.  `places` has one entry per LED: its
    ``(row, column)`` in a frame, its zone, or its ear's place in ``zone_order``."""

    def __init__(self, kind: str, zones: list, leds: list, places: list, rows: int = 1,
                 columns: int = 1):
        self.kind = kind
        self.zones = zones
        self.leds = leds
        self.places = places
        self.rows = rows
        self.columns = columns


def device_type(device) -> int:
    kind = DEVICE_TYPES.get(device.type)
    if kind is not None:
        return kind
    for word, kind in ACCESSORY_TYPES:
        if word in device.name:
            return kind
    return p.ACCESSORY


def _key_names(device) -> dict:
    """``(row, column)`` -> the key there, an F key before a media key on it."""
    names: dict = {}
    keymap = keymap_for(device.type, device.name, getattr(device, 'pid', None))
    for key, cell in sorted(keymap.items(), key=lambda item: not item[0][1:].isdigit()):
        names.setdefault(tuple(cell), key.replace('_', ' '))
    return names


def _frame(device, caps: dict, rows: int, columns: int) -> Layout:
    leds, places, zones = [], [], []
    if device.type in ('keyboard', 'keypad') or (device.type == 'mouse' and rows > 1):
        names = _key_names(device) if device.type != 'mouse' else {}
        for row in range(rows):
            for column in range(columns):
                key = names.get((row, column))
                leds.append('Key: {0}'.format(key) if key else
                            'Row {0}, Column {1}'.format(row + 1, column + 1))
                places.append((row, column))
        matrix = [[row * columns + column for column in range(columns)] for row in range(rows)]
        zones.append(p.Zone(FRAME_NAMES.get(device.type, 'Matrix'), p.ZONE_MATRIX,
                            rows * columns, matrix=matrix))
    else:
        # A strip, or several: the channels of an ARGB controller, a speaker each.
        label = 'Channel' if 'Addressable RGB' in device.name else 'Row'
        for row in range(rows):
            name = FRAME_NAMES.get(device.type, 'LEDs') if rows == 1 else '{0} {1}'.format(
                label, row + 1)
            zones.append(p.Zone(name, p.ZONE_LINEAR, columns))
            for column in range(columns):
                leds.append('LED {0}'.format(column + 1) if rows == 1 else
                            '{0}, LED {1}'.format(name, column + 1))
                places.append((row, column))
    return Layout(FRAME, zones, leds, places, rows, columns)


def layout_for(device) -> Optional[Layout]:
    """Where `device`'s LEDs are, or ``None`` if it has none it can colour one by one."""
    caps = device.capabilities()
    order = caps.get('zone_order') or []
    if order and hasattr(device, 'set_zone_colours'):
        names = ['{0} Ear'.format(ZONE_NAMES.get(zone, zone.title())) for zone in order]
        return Layout(EARS, [p.Zone(name, p.ZONE_SINGLE, 1) for name in names], names,
                      list(range(len(order))))
    rows, columns = caps.get('matrix_dimensions') or (1, 1)
    if caps.get('custom_frame') and rows * columns > 1:
        return _frame(device, caps, rows, columns)
    zones = [zone for zone, zone_caps in (caps.get('zones') or {}).items()
             if 'static' in (zone_caps.get('effects') or [])]
    if not zones:
        return None
    names = [ZONE_NAMES.get(zone, zone.title()) for zone in zones]
    return Layout(ZONES, [p.Zone(name, p.ZONE_SINGLE, 1) for name in names], names, zones)


# -- modes ---------------------------------------------------------------------------
def wave_percent(byte: int) -> int:
    """A wave's delay byte as a speed of 0 to 100, as Studio shows it.

    The byte is a delay: halving it doubles the speed, so the scale is
    logarithmic, or the whole slow half would sit at one end of a slider.
    """
    byte = max(1, min(255, int(byte)))
    return int(round(100 * (1 - math.log(byte) / math.log(255))))


def wave_byte(percent: int) -> int:
    return int(max(1, min(255, round(255 ** (1 - max(0, min(100, percent)) / 100.0)))))


class Spec:
    """What a device's controller is made of: what stays the same while it is plugged in."""

    def __init__(self, device):
        caps = device.capabilities()
        self.layout = layout_for(device)
        self.zones = {zone: set(zone_caps.get('effects') or [])
                      for zone, zone_caps in (caps.get('zones') or {}).items()}
        self.bright = [zone for zone, zone_caps in (caps.get('zones') or {}).items()
                       if zone_caps.get('brightness')]
        self.wave_speed = {zone for zone, zone_caps in (caps.get('zones') or {}).items()
                           if zone_caps.get('wave_speed')}
        #: Zones that light as part of ``backlight``, and so are not told twice.
        self.within_backlight = set(caps.get('zone_order') or []) if 'backlight' in self.zones else set()
        self.main = 'backlight' if 'backlight' in self.zones else next(iter(self.zones), None)
        self.modes = self._modes()

    def has(self, *effects) -> bool:
        return any(effect in found for found in self.zones.values() for effect in effects)

    def _modes(self) -> list:
        """``(key, template)`` for each mode, Direct first: OpenRGB's custom mode."""
        bright = p.HAS_BRIGHTNESS if self.bright else 0
        kept = p.AUTOMATIC_SAVE | bright
        modes = []
        if self.layout is not None:
            modes.append(('direct', p.Mode('Direct', flags=p.HAS_PER_LED_COLOR,
                                           color_mode=p.COLORS_PER_LED)))
        if self.has('none'):
            modes.append(('none', p.Mode('Off', flags=p.AUTOMATIC_SAVE)))
        if self.has('static'):
            modes.append(('static', _coloured('Static', kept, 1)))
        elif self.has('on'):
            modes.append(('on', p.Mode('On', flags=p.AUTOMATIC_SAVE)))
        colours = 3 if self.has('breath_triple') else 2 if self.has('breath_dual') else \
            1 if self.has('breath_single') else 0
        if colours or self.has('breath_random'):
            modes.append(('breath', _coloured('Breathing', kept, colours, self.has('breath_random'))))
        if self.has('spectrum'):
            modes.append(('spectrum', p.Mode('Spectrum Cycle', flags=kept)))
        if self.has('wave'):
            flags = kept | p.HAS_DIRECTION_LR | (p.HAS_SPEED if self.wave_speed else 0)
            modes.append(('wave', p.Mode('Wave', flags=flags, speed_min=0,
                                         speed_max=100 if self.wave_speed else 0)))
        if self.has('reactive'):
            # Faster is higher in OpenRGB; the device takes how long a key stays lit,
            # and for starlight how slowly it twinkles.
            modes.append(('reactive', _coloured('Reactive', kept | p.HAS_SPEED, 1,
                                                speed=(1, 4))))
        colours = 2 if self.has('starlight_dual') else 1 if self.has('starlight_single') else 0
        if colours or self.has('starlight_random'):
            modes.append(('starlight', _coloured('Starlight', kept | p.HAS_SPEED, colours,
                                                 self.has('starlight_random'), speed=(1, 3))))
        if self.has('blinking'):
            modes.append(('blinking', _coloured('Blinking', kept, 1)))
        return modes

    def index(self, key: str) -> Optional[int]:
        for index, (found, _) in enumerate(self.modes):
            if found == key:
                return index
        return None


def _coloured(name: str, flags: int, colours: int, random: bool = False, speed=None) -> p.Mode:
    mode = p.Mode(name, flags=flags | (p.HAS_MODE_SPECIFIC_COLOR if colours else 0)
                  | (p.HAS_RANDOM_COLOR if random else 0),
                  colors_min=1 if colours else 0, colors_max=colours,
                  color_mode=p.COLORS_MODE_SPECIFIC if colours else p.COLORS_RANDOM)
    if speed is not None:
        mode.speed_min, mode.speed_max = speed
    return mode


#: Our effects, by the mode they are shown as.
MODE_OF = {
    'none': 'none', 'on': 'on', 'static': 'static', 'spectrum': 'spectrum', 'wave': 'wave',
    'reactive': 'reactive', 'blinking': 'blinking', 'custom': 'direct',
    'breath_random': 'breath', 'breath_single': 'breath', 'breath_dual': 'breath',
    'breath_triple': 'breath', 'starlight_random': 'starlight',
    'starlight_single': 'starlight', 'starlight_dual': 'starlight',
}


def _triples(flat) -> list:
    flat = (list(flat or []) + [0] * 9)[:9]
    return [tuple(int(c) for c in flat[i:i + 3]) for i in range(0, 9, 3)]


def _shown(key: str, template: p.Mode, state: dict, wave_speed) -> p.Mode:
    """`template` with the values the device has now, as far as the daemon knows them."""
    mode = dataclasses.replace(template, colors=list(template.colors))
    effect = state.get('effect') or ''
    if mode.colors_max:
        saved = _triples(state.get('colors'))
        # One colour unless the device shows more now: a client that sets one
        # colour sets them all, and gets the one-colour effect back.
        several = effect in ('breath_dual', 'breath_triple', 'starlight_dual') and MODE_OF[effect] == key
        mode.colors = saved[:mode.colors_max] if several else [saved[0]] * mode.colors_max
        if mode.flags & p.HAS_RANDOM_COLOR and effect.endswith('_random'):
            mode.color_mode = p.COLORS_RANDOM
    if mode.flags & p.HAS_BRIGHTNESS:
        mode.brightness_max = 100
        mode.brightness = int(round(float(state.get('brightness') or 0)))
    if key == 'wave':
        mode.direction = p.DIRECTION_RIGHT if state.get('wave_dir') == 2 else p.DIRECTION_LEFT
        if mode.flags & p.HAS_SPEED:
            mode.speed = wave_percent(wave_speed or WAVE_SPEED_DEFAULT)
    elif key == 'reactive':
        mode.speed = 5 - max(1, min(4, int(state.get('speed') or 1)))
    elif key == 'starlight':
        mode.speed = 4 - max(1, min(3, int(state.get('speed') or 1)))
    return mode


def describe(device, spec: Spec, persistence, colours: Optional[list], drawn: bool) -> p.Controller:
    """The controller for `device`: `colours` are what Direct last showed on it, if
    anything, and `drawn` says the host is drawing it, frame by frame."""
    serial = device.serial
    state = persistence.zone(serial, spec.main) if spec.main else {}
    wave_speed = persistence.get(serial, spec.main, 'wave_speed') if spec.main else None
    modes = [_shown(key, template, state, wave_speed) for key, template in spec.modes]
    active = spec.index('direct') if drawn else spec.index(MODE_OF.get(state.get('effect'), ''))
    if active is None:
        active = spec.index('direct') if spec.index('direct') is not None else 0
    layout = spec.layout
    if colours is None:
        colours = saved_colours(device, spec, persistence)
    return p.Controller(
        type=device_type(device), name=device.name, vendor='Razer',
        description='Razer {0}, driven by openrazer-win'.format(device.type),
        version='openrazer-win {0}'.format(__version__), serial=serial,
        location='openrazer-win {0:04X}:{1:04X}'.format(device.vid, device.pid),
        modes=modes, active_mode=active,
        zones=list(layout.zones) if layout else [], leds=list(layout.leds) if layout else [],
        colors=list(colours))


def saved_colours(device, spec: Spec, persistence) -> list:
    """Each LED's colour from what the daemon keeps: a zone's colour when it is static."""
    layout = spec.layout
    if layout is None:
        return []

    def static(zone):
        state = persistence.saved_zone(device.serial, zone) or {}
        return _triples(state.get('colors'))[0] if state.get('effect') == 'static' else BLACK

    if layout.kind == EARS:
        flat = persistence.get_device_value(device.serial, 'zone_colours') or []
        return [_triples(flat[i * 3:i * 3 + 3])[0] for i in layout.places]
    if layout.kind == ZONES:
        return [static(zone) for zone in layout.places]
    return [static('backlight')] * len(layout.leds)


# -- putting a mode on the device ------------------------------------------------------
def apply(device, spec: Spec, key: str, mode: p.Mode, call: Callable) -> None:
    """Show `mode` on every zone of `device` that has it, through `call` --
    ``call(method, args, kwargs)``, the daemon's own way of changing a device,
    which keeps what it is given and stops whatever the host was drawing."""
    colours = [tuple(c) for c in mode.colors] or [(255, 255, 255)]
    first = list(colours[0])
    for zone in _zones_for(spec, key):
        kwargs = {'zone': zone}
        if key == 'none':
            call('set_none', [], kwargs)
        elif key == 'on':
            call('set_on', [], kwargs)
        elif key == 'static':
            call('set_static', first, kwargs)
        elif key == 'blinking':
            call('set_blinking', first, kwargs)
        elif key == 'spectrum':
            call('set_spectrum', [], kwargs)
        elif key == 'wave':
            direction = 2 if mode.direction == p.DIRECTION_RIGHT else 1
            if zone in spec.wave_speed and mode.flags & p.HAS_SPEED:
                call('set_wave_with_speed', [direction, wave_byte(mode.speed)], kwargs)
            else:
                call('set_wave', [direction], kwargs)
        elif key == 'reactive':
            call('set_reactive', first + [5 - max(1, min(4, mode.speed))], kwargs)
        elif key == 'breath':
            _breathe(spec, zone, mode, colours, call)
        elif key == 'starlight':
            _twinkle(spec, zone, mode, colours, call)
    if mode.flags & p.HAS_BRIGHTNESS and key != 'none':
        for zone in spec.bright:
            if zone not in spec.within_backlight:
                call('set_brightness', [max(0, min(100, mode.brightness))], {'zone': zone})


def _zones_for(spec: Spec, key: str) -> List[str]:
    effects = {'breath': ('breath_single', 'breath_dual', 'breath_triple', 'breath_random'),
               'starlight': ('starlight_single', 'starlight_dual', 'starlight_random')}
    names = effects.get(key, (key,))
    return [zone for zone in spec.zones
            if spec.zones[zone] & set(names) and zone not in spec.within_backlight]


def _distinct(colours: list, most: int) -> list:
    """The colours to show, the same one given over and over counting once."""
    colours = colours[:most]
    return colours[:1] if all(colour == colours[0] for colour in colours) else colours


def _flat(colours) -> list:
    return [int(part) for colour in colours for part in colour]


def _choose(found: set, wanted: list, options: tuple):
    """Of `options` -- ``(colours, effect)``, most colours first -- the effect that shows
    `wanted` best: one with as many colours as were given, or the nearest the device has."""
    for count, effect in options:
        if count <= len(wanted) and effect in found:
            return effect, wanted[:count]
    for count, effect in reversed(options):
        if effect in found:
            return effect, (wanted * count)[:count]
    return None, []


def _breathe(spec: Spec, zone: str, mode: p.Mode, colours: list, call: Callable) -> None:
    found = spec.zones[zone]
    coloured = found & {'breath_single', 'breath_dual', 'breath_triple'}
    if 'breath_random' in found and (mode.color_mode == p.COLORS_RANDOM or not coloured):
        call('set_breath_random', [], {'zone': zone})
        return
    effect, shown = _choose(found, _distinct(colours, 3), (
        (3, 'breath_triple'), (2, 'breath_dual'), (1, 'breath_single')))
    if effect is not None:
        call('set_' + effect, _flat(shown), {'zone': zone})


def _twinkle(spec: Spec, zone: str, mode: p.Mode, colours: list, call: Callable) -> None:
    found = spec.zones[zone]
    speed = 4 - max(1, min(3, mode.speed))
    coloured = found & {'starlight_single', 'starlight_dual'}
    if 'starlight_random' in found and (mode.color_mode == p.COLORS_RANDOM or not coloured):
        call('set_starlight_random', [speed], {'zone': zone})
        return
    effect, shown = _choose(found, _distinct(colours, 2), (
        (2, 'starlight_dual'), (1, 'starlight_single')))
    if effect is not None:
        call('set_' + effect, _flat(shown) + [speed], {'zone': zone})
