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
from .frame import Frame, keymap_for
from .keyboard_hook import KeyboardHook

logger = logging.getLogger(__name__)

#: Frames per second for host-rendered effects.  Upstream's ripple uses 25.
DEFAULT_REFRESH = 0.040

#: How long one ripple takes to cross the board, in seconds.
RIPPLE_DURATION = 0.6

SOFTWARE_EFFECTS = ('ripple', 'ripple_random', 'wave_soft', 'spectrum_soft')


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
        self.keymap = keymap_for(device.type, device.name)
        self.refresh = float(options.get('refresh_rate', DEFAULT_REFRESH))
        self.colour = tuple(options.get('colour', (0, 255, 0)))[:3]

    # -- input -------------------------------------------------------------
    def on_key(self, key_name: str) -> None:
        position = self.keymap.get(key_name)
        if position is None:
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
        if self.effect in ('ripple', 'ripple_random'):
            self._hook_source.subscribe(self.on_key)
        try:
            self._loop()
        finally:
            if self.effect in ('ripple', 'ripple_random'):
                self._hook_source.unsubscribe(self.on_key)

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

    def _render_spectrum(self, phase: float) -> None:
        for column in range(self.frame.columns):
            hue = ((phase / 5.0) + column / max(self.frame.columns, 1)) % 1.0
            red, green, blue = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
            for row in range(self.frame.rows):
                self.frame.set(row, column, int(red * 255), int(green * 255),
                               int(blue * 255))

    def _render_wave(self, phase: float) -> None:
        red, green, blue = self.colour
        for column in range(self.frame.columns):
            position = (column / max(self.frame.columns - 1, 1)) - (phase / 2.0) % 1.0
            intensity = max(0.0, math.cos(position * math.pi * 2) * 0.5 + 0.5)
            for row in range(self.frame.rows):
                self.frame.set(row, column, int(red * intensity),
                               int(green * intensity), int(blue * intensity))

    def _push(self) -> None:
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

    def __init__(self, manager):
        self.manager = manager
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
        with self._lock:
            thread = self._threads.pop(device.serial, None)
        if thread is not None:
            thread.stop()
            thread.join(timeout=1.0)

    def set_effect(self, device: RazerDevice, effect: str, options: dict) -> dict:
        if effect in ('none', 'off', ''):
            self.clear(device)
            return {'effect': None}
        if effect not in SOFTWARE_EFFECTS:
            raise NotSupported('unknown software effect: {0}'.format(effect))
        if not device.capabilities()['custom_frame']:
            raise NotSupported(
                '{0} has no addressable matrix, so it cannot run {1}'.format(
                    device.name, effect))

        self.clear(device)
        thread = EffectThread(device, effect, options, self.keys)
        with self._lock:
            self._threads[device.serial] = thread
        thread.start()
        return {'effect': effect, 'refresh_rate': thread.refresh,
                'keyboard_hook': self.keys.running}

    def status(self) -> dict:
        with self._lock:
            active = {serial: thread.effect
                      for serial, thread in self._threads.items()
                      if thread.is_alive()}
        return {'enabled': True, 'active': active,
                'keyboard_hook': self.keys.running,
                'available': list(SOFTWARE_EFFECTS)}
