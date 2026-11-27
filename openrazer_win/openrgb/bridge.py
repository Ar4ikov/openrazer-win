"""OpenRGB's clients, lighting the daemon's devices.

A client sees each device as a controller (:mod:`.controller`).  A mode it
picks -- Static, Wave, Off -- is put on the device the way Studio puts one
there, through the daemon: it is kept, comes back after a restart, and goes
into the profile the device follows.

Direct is different.  A client streams a colour for each LED, an effects
plugin sixty times a second, and those are shown, not kept: while a client
draws on a device that way it has the device, as a game has it through Chroma,
and when the last one drawing on it goes, the device gets back what it had.
Frames are passed on at most :data:`FRAMES_PER_SECOND` times a second, the
newest each time, and only when something in them changed.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
from typing import Callable, Optional

from ..effects.frame import Frame
from . import protocol as p
from .controller import EARS, FRAME, Spec, apply, describe, saved_colours

logger = logging.getLogger(__name__)

#: The most frames a second Direct colours are passed on at, as for Chroma games.
FRAMES_PER_SECOND = 30


class OpenRGBBridge:
    """Controllers out, modes and colours in."""

    def __init__(self, devices: Callable[[], list], call: Callable, persistence, effects=None,
                 busy: Optional[Callable] = None, on_release: Optional[Callable] = None):
        self._devices = devices
        #: ``call(serial, method, args, kwargs)``: the daemon's own way of changing a device.
        self._call = call
        self.persistence = persistence
        self.effects = effects
        #: Whether something with a better claim -- a game -- is lighting a device.
        self._busy = busy or (lambda device: False)
        self.on_release = on_release
        self._lock = threading.RLock()
        self._specs: dict = {}
        #: The serials clients were last shown, in order.
        self._listed: list = []
        #: In Direct: serial -> the clients drawing on it, and what it is to show.
        self._drawers: dict = {}
        self._colours: dict = {}
        self._dirty: set = set()
        #: Devices Direct colours have been put on, and what was put there last.
        self._taken: dict = {}
        self._shown: dict = {}
        self._custom_set: set = set()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name='openrgb', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self.release_all()

    # -- the devices clients see -------------------------------------------
    def _spec(self, device) -> Optional[Spec]:
        with self._lock:
            spec = self._specs.get(device.serial)
            if spec is None:
                try:
                    spec = Spec(device)
                except Exception:  # noqa: BLE001 - one device must not hide the rest
                    logger.debug('could not describe %s to OpenRGB', device.name, exc_info=True)
                    return None
                self._specs[device.serial] = spec
            return spec

    def controllers(self) -> list:
        """``(device, spec)`` for each device clients see: controller ``i`` is the i-th.

        A device with no lighting at all -- a headset that has only sound
        settings -- is left out: there is nothing in it for OpenRGB.
        """
        listed = []
        for device in list(self._devices()):
            spec = self._spec(device)
            if spec is not None and spec.modes:
                listed.append((device, spec))
        return listed

    def _controller(self, index: int):
        listed = self.controllers()
        return listed[index] if 0 <= index < len(listed) else (None, None)

    def refresh(self) -> bool:
        """Whether the devices clients see changed since they were last looked at."""
        serials = [device.serial for device, _ in self.controllers()]
        with self._lock:
            changed = serials != self._listed
            self._listed = serials
            gone = [serial for serial in set(self._drawers) | set(self._taken)
                    if serial not in serials]
        for serial in gone:
            self.release(serial, restore=False)
        return changed

    def describe(self, index: int, version: int) -> Optional[bytes]:
        device, spec = self._controller(index)
        if device is None:
            return None
        with self._lock:
            direct = device.serial in self._drawers
            colours = self._colours.get(device.serial)
        drawn = direct or (self.effects is not None and self.effects.drawing(device))
        return p.controller_data(describe(device, spec, self.persistence, colours, drawn), version)

    def drawing(self, device) -> bool:
        return device.serial in self._taken

    def status(self) -> dict:
        with self._lock:
            return {'drawing': sorted(self._taken)}

    # -- what clients ask for ----------------------------------------------
    def update_leds(self, client, index: int, colours, zone: Optional[int] = None,
                    led: Optional[int] = None) -> None:
        """New colours for the LEDs, a zone's or one: the device goes into Direct.

        A client that sends colours means them to be seen, and not every
        client asks for Direct first.
        """
        device, spec = self._controller(index)
        if device is None or spec.layout is None:
            return
        if zone is not None and not 0 <= zone < len(spec.layout.zones):
            return
        with self._lock:
            shown = self._enter(client, device, spec)
            if led is not None:
                if 0 <= led < len(shown) and colours:
                    shown[led] = tuple(colours[0])
            elif zone is not None:
                start = sum(z.count for z in spec.layout.zones[:zone])
                count = min(spec.layout.zones[zone].count, len(colours))
                shown[start:start + count] = [tuple(c) for c in colours[:count]]
            else:
                count = min(len(shown), len(colours))
                shown[:count] = [tuple(c) for c in colours[:count]]
            self._dirty.add(device.serial)
        self._wake.set()

    def set_custom_mode(self, client, index: int) -> None:
        """SETCUSTOMMODE: Direct, showing the colours it has."""
        device, spec = self._controller(index)
        if device is None or spec.layout is None:
            return
        with self._lock:
            self._enter(client, device, spec)
            self._dirty.add(device.serial)
        self._wake.set()

    def _enter(self, client, device, spec) -> list:
        serial = device.serial
        self._drawers.setdefault(serial, set()).add(client)
        if serial not in self._colours:
            self._colours[serial] = list(saved_colours(device, spec, self.persistence))
        return self._colours[serial]

    def update_mode(self, client, index: int, mode_index: int, sent: p.Mode,
                    version: int = p.VERSION) -> None:
        """UPDATEMODE and SAVEMODE: one of the device's own effects, or Direct.

        Both do the same: whatever the daemon puts on a device, it keeps.
        """
        device, spec = self._controller(index)
        if device is None or not 0 <= mode_index < len(spec.modes):
            return
        key, template = spec.modes[mode_index]
        if key == 'direct':
            self.set_custom_mode(client, index)
            return
        mode = _values(template, sent)
        # Told only when it was moved: a client before protocol 3 sends none,
        # and the 75 % the daemon assumes of a device never set is a guess.
        now = self.persistence.zone(device.serial, spec.main).get('brightness') if spec.main else None
        if version < 3 or int(round(float(now or 0))) == mode.brightness:
            mode.flags &= ~p.HAS_BRIGHTNESS
        self.release(device.serial, restore=False)
        if self.effects is not None:
            # A mode is the whole device's: what the host was drawing would paint over it.
            self.effects.stop_for(device)

        def call(method, args, kwargs):
            try:
                self._call(device.serial, method, args, kwargs)
            except Exception as error:  # noqa: BLE001 - the other zones still get it
                logger.info('OpenRGB could not %s on %s: %s', method, device.name, error)

        logger.info('OpenRGB puts %s on %s', template.name, device.name)
        apply(device, spec, key, mode, call)

    def client_gone(self, client) -> None:
        """A client disconnected: devices only it was drawing on get their own lighting back."""
        with self._lock:
            alone = []
            for serial, drawers in self._drawers.items():
                drawers.discard(client)
                if not drawers:
                    alone.append(serial)
        for serial in alone:
            self.release(serial)

    # -- drawing -----------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(0.5)
            self._wake.clear()
            if self._stop.is_set():
                break
            self.render()
            self._stop.wait(1.0 / FRAMES_PER_SECOND)

    def render(self) -> None:
        """Put the newest Direct colours on every device that has new ones, once."""
        with self._lock:
            if not self._dirty:
                return
            dirty, self._dirty = self._dirty, set()
        for device, spec in self.controllers():
            if device.serial not in dirty:
                continue
            # Under the lock, so that a release cannot come between a frame and
            # the device, and have a stale frame land on what it put back.
            with self._lock:
                colours = self._colours.get(device.serial)
                if colours is None or self._busy(device):
                    continue
                output = _output(spec, colours)
                if self._shown.get(device.serial) == output:
                    continue
                self._take(device)
                try:
                    self._write(device, spec, output)
                except Exception:  # noqa: BLE001 - one device must not stop the rest
                    logger.debug('could not draw on %s', device.name, exc_info=True)
                    continue
                self._shown[device.serial] = output

    def _write(self, device, spec, output: tuple) -> None:
        layout = spec.layout
        if layout.kind == EARS:
            device.set_zone_colours([list(colour) for colour in output])
            return
        if layout.kind != FRAME:
            before = dict(self._shown.get(device.serial) or ())
            for zone, colour in output:
                if before.get(zone) != colour:
                    device.set_static(*colour, zone=zone)
            return
        frame = Frame(layout.rows, layout.columns)
        for (row, column), colour in zip(layout.places, output):
            frame.set(row, column, *colour)
        # The ARGB controller wipes a frame written before the switch to custom
        # mode, so there the mode goes first, and once.
        if getattr(getattr(device, 'transport', None), 'argb_info', None) is not None:
            if device.serial not in self._custom_set:
                device.set_custom()
                self._custom_set.add(device.serial)
            device.set_key_row(frame.to_payload())
            return
        device.set_key_row(frame.to_payload())
        device.set_custom()

    def _take(self, device) -> None:
        if device.serial in self._taken:
            return
        if self.effects is not None:
            # Stopped, not forgotten: it comes back when OpenRGB is done.
            self.effects.clear_serial(device.serial)
        self.persistence.hold_lighting(device.serial)
        self._taken[device.serial] = device
        logger.info('OpenRGB is lighting %s', device.name)

    def release(self, serial: str, restore: bool = True) -> None:
        """Out of Direct; with `restore`, back to the lighting it had before."""
        with self._lock:
            self._drawers.pop(serial, None)
            self._colours.pop(serial, None)
            self._dirty.discard(serial)
            self._shown.pop(serial, None)
            self._custom_set.discard(serial)
            device = self._taken.pop(serial, None)
        if device is None:
            return
        self.persistence.release_lighting(serial)
        if not restore or self._busy(device):
            return
        try:
            device.restore()
            if self.effects is not None:
                self.effects.restore(device)
            if self.on_release is not None:
                self.on_release(device)
        except Exception:  # noqa: BLE001 - it is gone, or comes back on its own
            logger.debug('could not give %s its lighting back', device.name, exc_info=True)
        logger.info('%s has its own lighting back from OpenRGB', device.name)

    def release_all(self) -> None:
        with self._lock:
            serials = set(self._drawers) | set(self._taken)
        for serial in serials:
            self.release(serial)


def _output(spec, colours: list) -> tuple:
    """What `colours` come to on the device, in a form that compares."""
    if spec.layout.kind in (EARS, FRAME):
        return tuple(colours)
    return tuple(zip(spec.layout.places, colours))


def _clamp(value: int, one: int, other: int) -> int:
    low, high = min(one, other), max(one, other)
    return max(low, min(high, int(value)))


def _values(template: p.Mode, sent: p.Mode) -> p.Mode:
    """`template` with what a client may change in it taken from `sent`."""
    mode = dataclasses.replace(template, colors=list(template.colors))
    if mode.flags & p.HAS_SPEED:
        mode.speed = _clamp(sent.speed, mode.speed_min, mode.speed_max)
    if mode.flags & p.HAS_BRIGHTNESS:
        mode.brightness = _clamp(sent.brightness, 0, 100)
    if mode.flags & p.HAS_DIRECTION_LR:
        mode.direction = p.DIRECTION_RIGHT if sent.direction == p.DIRECTION_RIGHT else p.DIRECTION_LEFT
    if sent.color_mode == p.COLORS_RANDOM and mode.flags & p.HAS_RANDOM_COLOR:
        mode.color_mode = p.COLORS_RANDOM
    elif mode.flags & p.HAS_MODE_SPECIFIC_COLOR:
        mode.color_mode = p.COLORS_MODE_SPECIFIC
    if mode.colors_max:
        colours = [tuple(c) for c in sent.colors[:mode.colors_max]] or [(255, 255, 255)]
        mode.colors = colours + [colours[0]] * (mode.colors_min - len(colours))
    return mode
