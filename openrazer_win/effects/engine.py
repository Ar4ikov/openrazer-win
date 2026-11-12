"""Software effects the hardware cannot do on its own.

Anything the device firmware implements (static, spectrum, breathing, wave) is
sent as a single command and then forgotten about.  Effects that depend on what
the user is doing -- the ripple that spreads out from whichever key was just
pressed -- have to be rendered frame by frame by the host, which is what this
module does.
"""
from __future__ import annotations

import colorsys
import logging
import math
import threading
import time

from ..core.device import RazerDevice
from ..devices.recipes import NotSupported, RecipeError
from ..integrations.accent import ACCENT_POLL, WINDOWS_BLUE, read_accent
from .audio import open_meter
from .frame import Frame, keymap_for
from .keyboard_hook import KeyboardHook
from .layers import BREATH_PERIOD, STARLIGHT_PERIOD, LayerStack, layers_from_options, twinkle_phase

logger = logging.getLogger(__name__)

#: Frames per second for host-rendered effects.  Upstream's ripple uses 25.
DEFAULT_REFRESH = 0.040

#: How long one ripple takes to cross the board, in seconds.
RIPPLE_DURATION = 0.6

#: Lit by what the PC is playing rather than by the clock or the keyboard.
MUSIC = 'music'

#: An effect of its own for each of a device's zones: a headset's two ears.
ZONES = 'zones'

#: In the colour Windows is set to, following it when it changes.
ACCENT = 'accent'

#: Drawn by the host for a device that cannot breathe or twinkle by itself.
BREATH_SOFT = 'breath_soft'
STARLIGHT_SOFT = 'starlight_soft'

SOFTWARE_EFFECTS = ('ripple', 'ripple_random', 'wave_soft', 'spectrum_soft', 'layers',
                    MUSIC, ZONES, BREATH_SOFT, STARLIGHT_SOFT, ACCENT)

#: What one zone of ``zones`` can show.  The same drawing as on the whole
#: device, taken at that zone's place in it -- so a wave on both ears still
#: travels from one to the other.
ZONE_EFFECTS = ('none', 'static', 'spectrum_soft', 'wave_soft', MUSIC,
                BREATH_SOFT, STARLIGHT_SOFT, ACCENT)

#: Drawn cell by cell from the clock, the same way on a whole device or a zone.
CELL_EFFECTS = (BREATH_SOFT, STARLIGHT_SOFT, ACCENT)

#: Zone effects a device can show by itself, with nothing drawn by the host.
STILL_ZONE_EFFECTS = ('none', 'static')

#: Where a device's host-drawn effect is remembered in persistence, so the
#: daemon can start it again after a restart or a reconnect.
SAVED_KEY = 'software'

#: The effects driven by key presses rather than by a clock.
KEY_REACTIVE = ('ripple', 'ripple_random')

#: How many addressable cells a ripple needs before it means anything.  It
#: spreads outward from the key that was pressed, so on a two-cell device every
#: press covers the whole thing at once -- a blink, not a ripple.  A keypad,
#: the smallest thing upstream ripples on, has twenty.
MIN_RIPPLE_CELLS = 8

#: Music is mastered loud from end to end, so its beat is not in the level but
#: in how far the level drops below its own recent peak.  That peak is forgotten
#: this slowly, which keeps the effect pulsing at any volume ...
MUSIC_PEAK_FALL = 0.998

#: ... down to here, under which the sound is not worth lighting anything for.
MUSIC_QUIETEST = 0.02

#: Each frame the light falls to this much of the last one.  Up at once and down
#: slowly is what the eye reads as a beat; the raw level is only a flicker.
MUSIC_FALL = 0.88

#: How much is left with nothing playing, so that the effect is seen to be on.
MUSIC_FLOOR = 0.06


def can_render(device) -> bool:
    """Whether host-rendered effects are meaningful on this device.

    A frame has to be worth drawing: a mouse with a single addressable LED
    reports a 1x1 matrix, and a ripple across one cell is nothing at all.
    Upstream draws the same line -- its ripple manager refuses devices without
    matrix capabilities.
    """
    capabilities = device.capabilities()
    if not capabilities.get('custom_frame'):
        return False
    dimensions = capabilities.get('matrix_dimensions') or (1, 1)
    return dimensions[0] * dimensions[1] > 1


def _cells(device) -> int:
    rows, columns = device.capabilities().get('matrix_dimensions') or (1, 1)
    return rows * columns


def can_render_keys(device) -> bool:
    """Whether a key-reactive effect is meaningful on this device."""
    return can_render(device) and _cells(device) >= MIN_RIPPLE_CELLS


def zone_order(device) -> list:
    """The zones of `device` that can each be given an effect, left to right.

    Only a device that says where its zones sit in its frame has any: zone
    ``i`` is column ``i``.
    """
    return list(device.capabilities().get('zone_order') or [])


def _clean_colour(value, default=(0, 255, 0)) -> list:
    try:
        return [min(max(int(part), 0), 255) for part in list(value)[:3]] if value else list(default)
    except (TypeError, ValueError) as error:
        raise ValueError('a colour is three numbers 0-255: {0!r}'.format(value)) from error


def clean_zones(device, zones) -> dict:
    """A validated copy of a ``zones`` effect's zones, every zone of `device` in it."""
    order = zone_order(device)
    if not order:
        raise NotSupported('{0} has no zones to give effects of their own'.format(device.name))
    if not isinstance(zones, dict):
        raise ValueError('"zones" maps each zone to {"effect": ..., "colour": [r, g, b]}')
    clean = {}
    for zone in order:
        wanted = zones.get(zone) or {'effect': 'none'}
        if not isinstance(wanted, dict):
            raise ValueError('zone {0!r}: expected {{"effect": ..., "colour": ...}}'.format(zone))
        effect = wanted.get('effect') or 'none'
        if effect not in ZONE_EFFECTS:
            raise ValueError('zone {0!r}: unknown effect {1!r}; expected one of {2}'.format(
                zone, effect, ', '.join(ZONE_EFFECTS)))
        clean[zone] = {'effect': effect, 'colour': _clean_colour(wanted.get('colour'))}
    unknown = sorted(set(zones) - set(order))
    if unknown:
        raise ValueError('{0} has no zone {1}; it has {2}'.format(
            device.name, ', '.join(unknown), ', '.join(order)))
    return clean


def _scale(colour, amount: float) -> tuple:
    return int(colour[0] * amount), int(colour[1] * amount), int(colour[2] * amount)


class EffectThread(threading.Thread):
    """Renders one software effect onto one device."""

    def __init__(self, device: RazerDevice, effect: str, options: dict,
                 hook_source: 'KeyPressSource'):
        super().__init__(name='effect-{0}'.format(device.serial), daemon=True)
        self.device = device
        self.effect = effect
        self.options = options
        self._hook_source = hook_source
        # Not ``_stop``: threading.Thread already has a private ``_stop()``
        # method that join() calls, and shadowing it breaks join().
        self._stop_event = threading.Event()
        self._presses: list = []
        self._presses_lock = threading.Lock()
        rows, columns = device.matrix_dimensions or (1, 1)
        self.frame = Frame(rows, columns)
        self.keymap = keymap_for(device.type, device.name, getattr(device, 'pid', None))
        self.refresh = float(options.get('refresh_rate', DEFAULT_REFRESH))
        self.colour = tuple(options.get('colour', (0, 255, 0)))[:3]
        self.stack = None
        self.meter = None
        self.recent_peak = 0.0
        self.level = 0.0
        #: For ``zones``: each zone's effect and colour, in frame order.
        self.zone_effects = [(zone['effect'], tuple(zone['colour']))
                             for zone in (options.get('zones') or {}).values()]
        self.started = time.monotonic()
        #: Nothing on it moves: what is shown is held, the way a colour set
        #: by hand is, rather than left to fade on a device that forgets.
        self.still = effect == ACCENT or (effect == ZONES and all(
            zone_effect in STILL_ZONE_EFFECTS + (ACCENT,) for zone_effect, _ in self.zone_effects))
        self._accent = WINDOWS_BLUE
        self._accent_read_at: float = float('-inf')
        if effect == 'layers':
            self.stack = LayerStack(layers_from_options(options, self.frame.rows,
                                                        self.frame.columns),
                                    self.frame.rows, self.frame.columns)
        self._custom_mode_set = False

    @property
    def needs_keys(self) -> bool:
        if self.stack is not None:
            return self.stack.key_driven
        return self.effect in KEY_REACTIVE

    # -- input -------------------------------------------------------------
    def on_key(self, key_name: str) -> None:
        position = self.keymap.get(key_name)
        if position is None:
            return
        if self.stack is not None and not (0 <= position[0] < self.frame.rows
                                           and 0 <= position[1] < self.frame.columns):
            return
        colour = self.colour
        if self.effect == 'ripple_random':
            colour = _random_colour()
        with self._presses_lock:
            self._presses.append((time.monotonic(), position, colour))

    def stop(self) -> None:
        self._stop_event.set()

    # -- rendering ---------------------------------------------------------
    def run(self) -> None:
        listening = self.needs_keys
        if listening:
            self._hook_source.subscribe(self.on_key)
        if self.effect == MUSIC or any(effect == MUSIC for effect, _ in self.zone_effects):
            # Here rather than in __init__: COM belongs to the thread that uses it.
            self.meter = open_meter()
        try:
            if self.stack is not None:
                self._loop_layers()
            else:
                self._loop()
        finally:
            if listening:
                self._hook_source.unsubscribe(self.on_key)
            if self.meter is not None:
                self.meter.close()

    def _loop_layers(self) -> None:
        """Draw the layer stack, pushing a frame only when the picture changes.

        A stack of static layers is drawn once and then left alone, rather than
        resent twenty-five times a second for as long as the daemon runs.
        """
        started = time.monotonic()
        shown = None
        while not self._stop_event.is_set():
            now = time.monotonic()
            with self._presses_lock:
                longest = self.stack.longest_press
                self._presses = [p for p in self._presses if now - p[0] < longest]
                presses = [(now - at, (position[0], position[1]))
                           for at, position, _colour in self._presses]
            try:
                self.stack.render(self.frame, now - started, presses)
                if self.frame != shown:
                    self._push()
                    shown = self.frame.copy()
            except (NotSupported, RecipeError) as error:
                logger.warning('stopping layers on %s: %s', self.device.name, error)
                return
            except Exception:  # noqa: BLE001 - keep the daemon alive
                logger.debug('layer frame failed', exc_info=True)
                return
            busy = self.stack.animated or self.stack.key_driven
            self._stop_event.wait(self.refresh if busy else 1.0)

    def _loop(self) -> None:
        phase = 0.0
        while not self._stop_event.wait(self.refresh):
            try:
                if self.effect in ('ripple', 'ripple_random'):
                    if not self._render_ripple():
                        continue
                elif self.effect == 'spectrum_soft':
                    self._render_spectrum(phase)
                elif self.effect == 'wave_soft':
                    self._render_wave(phase)
                elif self.effect == MUSIC:
                    if not self._render_music():
                        continue
                elif self.effect in (ZONES,) + CELL_EFFECTS:
                    if self.effect == ZONES:
                        changed = self._render_zones(phase)
                    else:
                        changed = self._render_cells(self.effect, self.colour)
                    phase = (phase + self.refresh) % 10.0
                    if changed:
                        self._push()
                    continue
                else:
                    return
                phase = (phase + self.refresh) % 10.0
                self._push()
            except (NotSupported, RecipeError) as error:
                logger.warning('stopping %s on %s: %s',
                               self.effect, self.device.name, error)
                return
            except Exception:  # noqa: BLE001 - keep the daemon alive
                logger.debug('effect frame failed', exc_info=True)
                return

    def _render_ripple(self) -> bool:
        """Draw every live ripple.  Returns False when there is nothing to draw."""
        now = time.monotonic()
        with self._presses_lock:
            self._presses = [p for p in self._presses
                             if now - p[0] < RIPPLE_DURATION]
            presses = list(self._presses)
        if not presses:
            # Nothing active: blank once, then idle until the next key press.
            if any(self.frame.get(r, c) != (0, 0, 0)
                   for r in range(self.frame.rows)
                   for c in range(self.frame.columns)):
                self.frame.clear()
                return True
            return False

        self.frame.clear()
        # Columns are roughly twice as dense as rows on a keyboard, so scale the
        # vertical axis to keep the wavefront circular rather than elliptical.
        radius_scale = max(self.frame.columns, 1) / RIPPLE_DURATION
        for started, (origin_row, origin_col), colour in presses:
            radius = (now - started) * radius_scale
            for row in range(self.frame.rows):
                for column in range(self.frame.columns):
                    distance = math.hypot((row - origin_row) * 2.0,
                                          column - origin_col)
                    delta = abs(distance - radius)
                    if delta > 1.2:
                        continue
                    intensity = max(0.0, 1.0 - delta / 1.2)
                    intensity *= max(0.0, 1.0 - (now - started) / RIPPLE_DURATION)
                    existing = self.frame.get(row, column)
                    self.frame.set(
                        row, column,
                        max(existing[0], int(colour[0] * intensity)),
                        max(existing[1], int(colour[1] * intensity)),
                        max(existing[2], int(colour[2] * intensity)))
        return True

    def _fill_column(self, column: int, colour) -> None:
        for row in range(self.frame.rows):
            self.frame.set(row, column, *colour)

    def _spectrum_at(self, column: int, phase: float) -> tuple:
        hue = ((phase / 5.0) + column / max(self.frame.columns, 1)) % 1.0
        red, green, blue = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
        return int(red * 255), int(green * 255), int(blue * 255)

    def _wave_at(self, column: int, phase: float, colour) -> tuple:
        # One period spans the device, so on two cells the light goes from
        # one to the other and back, rather than pulsing on both at once.
        position = column / max(self.frame.columns, 1) - (phase / 2.0) % 1.0
        return _scale(colour, max(0.0, math.cos(position * math.pi * 2) * 0.5 + 0.5))

    def _hear(self) -> float:
        """How brightly to light up for what is playing, from the floor to 1."""
        peak = max(self.meter.peak(), 0.0)
        self.recent_peak = max(peak, self.recent_peak * MUSIC_PEAK_FALL, MUSIC_QUIETEST)
        heard = (peak / self.recent_peak) ** 2
        self.level = max(heard, self.level * MUSIC_FALL)
        return max(self.level, MUSIC_FLOOR)

    def _music_at(self, column: int, level: float, colour) -> tuple:
        centre = (self.frame.columns - 1) / 2.0
        distance = abs(column - centre) / max(centre, 1.0)
        return _scale(colour, level * (1.0 - distance * (1.0 - level)))

    def _render_spectrum(self, phase: float) -> None:
        for column in range(self.frame.columns):
            self._fill_column(column, self._spectrum_at(column, phase))

    def _render_wave(self, phase: float) -> None:
        for column in range(self.frame.columns):
            self._fill_column(column, self._wave_at(column, phase, self.colour))

    def _render_music(self) -> bool:
        """Light the device by how loud the PC is.  False if nothing changed.

        From the middle outwards: a quiet passage glows at the centre, and it
        takes something loud to reach the edges.  A device of two cells has no
        middle, and simply pulses.
        """
        level = self._hear()
        before = self.frame.copy()
        for column in range(self.frame.columns):
            self._fill_column(column, self._music_at(column, level, self.colour))
        return self.frame != before

    def _accent_colour(self) -> tuple:
        """The Windows accent colour, looked at again every couple of seconds."""
        now = time.monotonic()
        if now - self._accent_read_at >= ACCENT_POLL:
            self._accent_read_at = now
            self._accent = read_accent() or self._accent
        return self._accent

    def _cell_at(self, effect: str, colour, row: int, column: int) -> tuple:
        """One cell of an effect drawn from the clock alone."""
        elapsed = time.monotonic() - self.started
        if effect == BREATH_SOFT:
            return _scale(colour, 0.5 - 0.5 * math.cos(elapsed / BREATH_PERIOD * 2 * math.pi))
        if effect == STARLIGHT_SOFT:
            # The same twinkle as Studio's starlight layer: a short flash, then dark.
            cycle = elapsed / STARLIGHT_PERIOD + twinkle_phase(row, column) * 7.0
            return _scale(colour, max(0.0, math.sin(cycle * 2 * math.pi)) ** 6)
        if effect == ACCENT:
            return self._accent_colour()
        return (0, 0, 0)

    def _render_cells(self, effect: str, colour) -> bool:
        """Draw a clock-driven effect over the whole device.  False if nothing changed."""
        before = self.frame.copy()
        for row in range(self.frame.rows):
            for column in range(self.frame.columns):
                self.frame.set(row, column, *self._cell_at(effect, colour, row, column))
        return self.frame != before

    def _render_zones(self, phase: float) -> bool:
        """Draw each zone's own effect in its column.  False if nothing changed."""
        level = self._hear() if self.meter is not None else 0.0
        before = self.frame.copy()
        for column, (effect, colour) in enumerate(self.zone_effects):
            if effect in CELL_EFFECTS:
                for row in range(self.frame.rows):
                    self.frame.set(row, column, *self._cell_at(effect, colour, row, column))
                continue
            if effect == 'static':
                shown = colour
            elif effect == 'spectrum_soft':
                shown = self._spectrum_at(column, phase)
            elif effect == 'wave_soft':
                shown = self._wave_at(column, phase, colour)
            elif effect == MUSIC:
                shown = self._music_at(column, level, colour)
            else:
                shown = (0, 0, 0)
            self._fill_column(column, shown)
        return self.frame != before

    def _push(self) -> None:
        if self.still and hasattr(self.device, 'set_zone_colours'):
            self.device.set_zone_colours([self.frame.get(0, column)
                                          for column in range(self.frame.columns)])
            return
        # The ARGB controller wipes a frame written before the switch to custom
        # mode, so there the mode goes first, and only once.
        transport = getattr(self.device, 'transport', None)
        if getattr(transport, 'argb_info', None) is not None:
            if not self._custom_mode_set:
                self.device.set_custom()
                self._custom_mode_set = True
            self.device.set_key_row(self.frame.to_payload())
            return
        self.device.set_key_row(self.frame.to_payload())
        self.device.set_custom()


def _random_colour() -> tuple:
    import random
    red, green, blue = colorsys.hsv_to_rgb(random.random(), 1.0, 1.0)
    return int(red * 255), int(green * 255), int(blue * 255)


class KeyPressSource:
    """Shares one OS-level keyboard hook between every effect that needs it."""

    def __init__(self):
        self._subscribers: list = []
        self._lock = threading.Lock()
        self._hook = KeyboardHook(self._broadcast)

    def _broadcast(self, key_name: str) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            callback(key_name)

    def subscribe(self, callback) -> None:
        with self._lock:
            self._subscribers.append(callback)
            needs_hook = len(self._subscribers) == 1
        if needs_hook:
            self._hook.start()

    def unsubscribe(self, callback) -> None:
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)
            idle = not self._subscribers
        if idle:
            self._hook.stop()

    @property
    def running(self) -> bool:
        return self._hook.running

    def stop(self) -> None:
        with self._lock:
            self._subscribers.clear()
        self._hook.stop()


class EffectEngine:
    """Tracks which device is running which software effect."""

    def __init__(self, manager, persistence=None):
        self.manager = manager
        #: Remembers each device's effect, so it comes back with the daemon.
        self.persistence = persistence if persistence is not None else getattr(
            manager, 'persistence', None)
        self.keys = KeyPressSource()
        self._threads: dict = {}
        self._lock = threading.RLock()

    def start(self) -> None:
        """Nothing to do until an effect is requested."""

    def stop(self) -> None:
        with self._lock:
            for thread in self._threads.values():
                thread.stop()
            for thread in self._threads.values():
                thread.join(timeout=1.0)
            self._threads.clear()
        self.keys.stop()

    def clear(self, device: RazerDevice) -> None:
        self.clear_serial(device.serial)

    def clear_serial(self, serial: str) -> None:
        """Stop the effect on one device, by serial.

        By serial rather than by device because the caller that most needs
        this -- hot-plug, noticing something has gone -- no longer has the
        device to hand.  A thread left running on a device the manager has
        dropped keeps writing to it forever, and on Bluetooth that also keeps
        the link up and so keeps the device from ever being rediscovered.
        """
        with self._lock:
            thread = self._threads.pop(serial, None)
        if thread is not None:
            logger.info('stopping %s on %s', thread.effect, serial)
            thread.stop()
            thread.join(timeout=1.0)

    def active_serials(self) -> list:
        with self._lock:
            return list(self._threads)

    # -- remembering -------------------------------------------------------
    def saved(self, serial: str):
        """The effect remembered for `serial`, as ``{"effect", "options"}``."""
        if self.persistence is None:
            return None
        saved = self.persistence.get_device_value(serial, SAVED_KEY)
        if isinstance(saved, dict) and saved.get('effect') in SOFTWARE_EFFECTS:
            return saved
        return None

    def _remember(self, serial: str, effect: str, options: dict) -> None:
        if self.persistence is not None:
            self.persistence.set_device_value(serial, SAVED_KEY,
                                              {'effect': effect, 'options': options})

    def forget(self, serial: str) -> None:
        if self.persistence is not None and self.saved(serial) is not None:
            self.persistence.clear_device_value(serial, SAVED_KEY)

    def restore(self, device: RazerDevice) -> bool:
        """Start the effect remembered for `device` again, if there is one."""
        saved = self.saved(device.serial)
        if saved is None:
            return False
        try:
            self.set_effect(device, saved['effect'], dict(saved.get('options') or {}))
        except (NotSupported, RecipeError, ValueError) as error:
            logger.warning('could not bring back %s on %s: %s',
                           saved['effect'], device.name, error)
            return False
        return True

    def stop_for(self, device: RazerDevice) -> bool:
        """Stop and forget the host-drawn effect: the device is getting one of its own."""
        if device.serial not in self.active_serials() and self.saved(device.serial) is None:
            return False
        self.set_effect(device, 'none', {})
        return True

    def _running(self, serial: str):
        with self._lock:
            thread = self._threads.get(serial)
        return thread if thread is not None and thread.is_alive() else None

    def drawing(self, device) -> bool:
        """Whether the host is drawing on `device` right now."""
        return self._running(device.serial) is not None

    def zones_now(self, device) -> dict:
        """What each of `device`'s zones shows now, as a ``zones`` effect has it."""
        order = zone_order(device)
        running = self._running(device.serial)
        if running is not None and running.effect == ZONES:
            return {zone: dict(state) for zone, state in running.options['zones'].items()}
        if running is not None and running.effect in ZONE_EFFECTS:
            # The same effect on the whole device is the same effect on each zone.
            return {zone: {'effect': running.effect, 'colour': list(running.colour)}
                    for zone in order}
        reader = getattr(device, 'zone_colours_now', None)
        shown = reader() if reader is not None else []
        zones = {}
        for index, zone in enumerate(order):
            colour = list(shown[index]) if index < len(shown) else [0, 0, 0]
            zones[zone] = {'effect': 'static' if any(colour) else 'none', 'colour': colour}
        return zones

    def set_zone_effect(self, device, zone: str, effect: str, options: dict) -> dict:
        """Give one zone an effect of its own, leaving the others as they are."""
        if zone not in zone_order(device):
            raise NotSupported('{0} cannot light {1} on its own; it draws {2} on the '
                               'whole device'.format(device.name, zone, effect))
        effect = 'none' if effect in ('off', '') else effect
        if effect not in ZONE_EFFECTS:
            raise NotSupported('{0} is drawn on the whole device, not on one zone; a zone '
                               'can show {1}'.format(effect, ', '.join(ZONE_EFFECTS)))
        zones = self.zones_now(device)
        zones[zone] = {'effect': effect, 'colour': options.get('colour')}
        settings = {key: value for key, value in options.items() if key == 'refresh_rate'}
        return self.set_effect(device, ZONES, dict(settings, zones=zones))

    def _show_still(self, device, zones: dict) -> dict:
        """Zones that need nothing drawn: the device is told once, and holds them."""
        self.clear(device)
        self.forget(device.serial)
        colours = [zone['colour'] if zone['effect'] == 'static' else [0, 0, 0]
                   for zone in zones.values()]
        if hasattr(device, 'set_zone_colours'):
            device.set_zone_colours(colours)
        else:
            for name, colour in zip(zones, colours):
                device.set_static(*colour, zone=name)
        return {'effect': None, 'zones': zones}

    def set_effect(self, device: RazerDevice, effect: str, options: dict) -> dict:
        options = dict(options)
        zone = options.pop('zone', None)
        if zone not in (None, '', 'backlight'):
            return self.set_zone_effect(device, zone, effect, options)
        if effect in ('none', 'off', ''):
            self.clear(device)
            self.forget(device.serial)
            return {'effect': None}
        if effect not in SOFTWARE_EFFECTS:
            raise NotSupported('unknown software effect: {0}'.format(effect))
        if effect == ZONES:
            options['zones'] = clean_zones(device, options.get('zones'))
            if all(state['effect'] in STILL_ZONE_EFFECTS for state in options['zones'].values()):
                return self._show_still(device, options['zones'])
        if not can_render(device):
            raise NotSupported(
                '{0} has no addressable matrix big enough to render {1}'.format(
                    device.name, effect))
        if effect in KEY_REACTIVE and not can_render_keys(device):
            raise NotSupported(
                '{0} has {1} addressable cells; a ripple needs somewhere to '
                'spread'.format(device.name, _cells(device)))

        if effect == 'layers':
            rows, columns = device.matrix_dimensions or (1, 1)
            # Raises LayerError, a ValueError, on a stack it cannot draw.
            options['layers'] = layers_from_options(options, rows, columns)

        self.clear(device)
        thread = EffectThread(device, effect, options, self.keys)
        with self._lock:
            self._threads[device.serial] = thread
        thread.start()
        self._remember(device.serial, effect, options)
        return {'effect': effect, 'refresh_rate': thread.refresh,
                'keyboard_hook': self.keys.running}

    def status(self) -> dict:
        with self._lock:
            running = {serial: thread for serial, thread in self._threads.items()
                       if thread.is_alive()}
        return {'enabled': True,
                'active': {serial: thread.effect for serial, thread in running.items()},
                'options': {serial: thread.options for serial, thread in running.items()},
                'keyboard_hook': self.keys.running,
                'available': list(SOFTWARE_EFFECTS)}
