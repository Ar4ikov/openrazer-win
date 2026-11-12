"""Games lighting Razer devices through the Chroma SDK, drawn here instead.

A game opens a session, then shows effects on kinds of device -- "the
keyboard", "the mouse" -- for as long as it runs.  Whichever session showed
something last is the one on the devices; when it ends, or goes quiet for
longer than the SDK allows, each device gets back what the user had on it.

While a game draws on a device, nothing it shows is remembered as the user's
lighting: the daemon's saved state of that device is left as it was.
"""
from __future__ import annotations

import itertools
import logging
import threading
import time
import uuid
from typing import Callable, Optional

from ..devices.layouts import key_cell, key_column_shift
from ..effects.frame import Frame
from . import grid as grids
from .grid import ChromaError

logger = logging.getLogger(__name__)

#: A session nobody has heard from for this long has ended, as in Razer's SDK.
SESSION_TIMEOUT = 15.0

#: The most frames a second a game's effects are passed on at.
FRAMES_PER_SECOND = 30

#: Which of the SDK's kinds of device each of ours is drawn as.  Anything else
#: -- a dock, a mug, a laptop's lid -- is lit as Chroma Link.
KIND_OF = {
    'keyboard': 'keyboard',
    'mouse': 'mouse',
    'headset': 'headset',
    'mousemat': 'mousepad',
    'keypad': 'keypad',
}

#: What the SDK says about itself.
VERSION = {'core': '3.36.1', 'device': '3.36.1', 'version': '3.36.1'}


class Session:
    """One program using the SDK."""

    def __init__(self, number: int, info: dict, now: float):
        self.number = number
        self.title = str((info or {}).get('title') or 'A Chroma app')[:80]
        self.last_seen = now
        #: When it last put something on the devices.
        self.shown_at = float('-inf')
        #: What it shows on each kind of device.
        self.looks: dict = {}
        #: Effects made to be shown later, by id.
        self.effects: dict = {}
        self.ticks = 0


class ChromaBridge:
    """Sessions in, frames out."""

    def __init__(self, devices: Callable[[], list], effects=None, persistence=None,
                 on_release: Optional[Callable] = None, clock: Callable[[], float] = time.monotonic):
        self._devices = devices
        self.effects = effects
        self.persistence = persistence
        self.on_release = on_release
        self._clock = clock
        self._lock = threading.RLock()
        self._sessions: dict = {}
        self._numbers = itertools.count(1)
        #: Devices a game is drawing on, by serial.
        self._taken: dict = {}
        #: What was last written to each, so an unchanged frame is not sent again.
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
        self._thread = threading.Thread(target=self._run, name='chroma', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        with self._lock:
            self._sessions.clear()
        self.release_all()

    # -- sessions ----------------------------------------------------------
    def open(self, info: dict) -> int:
        with self._lock:
            number = next(self._numbers)
            session = Session(number, info if isinstance(info, dict) else {}, self._clock())
            self._sessions[number] = session
        logger.info('%s started using Razer Chroma', session.title)
        return number

    def close(self, number: int) -> None:
        with self._lock:
            session = self._sessions.pop(number, None)
        if session is not None:
            logger.info('%s stopped using Razer Chroma', session.title)
        self._wake.set()

    def _session(self, number: int) -> Session:
        session = self._sessions.get(number)
        if session is None:
            raise KeyError(number)
        session.last_seen = self._clock()
        return session

    def heartbeat(self, number: int) -> int:
        with self._lock:
            session = self._session(number)
            session.ticks += 1
            return session.ticks

    def show(self, number: int, kind: str, body: dict) -> None:
        look = grids.parse(kind, body)
        with self._lock:
            session = self._session(number)
            session.looks[kind] = look
            session.shown_at = self._clock()
        self._wake.set()

    def create(self, number: int, kind: str, body: dict) -> str:
        look = grids.parse(kind, body)
        with self._lock:
            session = self._session(number)
            effect = str(uuid.uuid4())
            session.effects[effect] = (kind, look)
            return effect

    def apply(self, number: int, effects: list) -> None:
        with self._lock:
            session = self._session(number)
            for effect in effects:
                if effect not in session.effects:
                    raise ChromaError('no effect {0}'.format(effect))
                kind, look = session.effects[effect]
                session.looks[kind] = [list(row) for row in look]
            session.shown_at = self._clock()
        self._wake.set()

    def delete(self, number: int, effects: list) -> None:
        with self._lock:
            session = self._session(number)
            for effect in effects:
                session.effects.pop(effect, None)

    # -- what is going on --------------------------------------------------
    def _expire(self) -> None:
        now = self._clock()
        with self._lock:
            gone = [n for n, s in self._sessions.items() if now - s.last_seen > SESSION_TIMEOUT]
            for number in gone:
                logger.info('%s went quiet; giving its devices back',
                            self._sessions[number].title)
                del self._sessions[number]

    def _active(self) -> Optional[Session]:
        shown = [s for s in self._sessions.values() if s.looks]
        return max(shown, key=lambda s: s.shown_at) if shown else None

    def drawing(self, device) -> bool:
        return device.serial in self._taken

    def status(self) -> dict:
        with self._lock:
            active = self._active()
            return {
                'apps': [s.title for s in sorted(self._sessions.values(), key=lambda s: s.number)],
                'showing': active.title if active is not None else None,
                'drawing': sorted(self._taken),
            }

    # -- drawing -----------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(0.5)
            self._wake.clear()
            if self._stop.is_set():
                break
            self._expire()
            self.render()
            self._stop.wait(1.0 / FRAMES_PER_SECOND)

    def render(self) -> None:
        """Put the active session's effects on the devices, once."""
        with self._lock:
            active = self._active()
            looks = {kind: [list(row) for row in look]
                     for kind, look in (active.looks.items() if active else ())}
        painted = set()
        for device in list(self._devices()):
            try:
                if self._paint(device, looks):
                    painted.add(device.serial)
            except Exception:  # noqa: BLE001 - one device must not stop the rest
                logger.debug('could not draw on %s', device.name, exc_info=True)
        for serial in set(self._taken) - painted:
            self.release(serial)

    def _paint(self, device, looks: dict) -> bool:
        kind = KIND_OF.get(device.type, 'chromalink')
        look = looks.get(kind)
        if look is None:
            return False
        output = self.output(device, kind, look)
        if output is None:
            return False
        self._take(device)
        if self._shown.get(device.serial) == output:
            return True
        self._write(device, output)
        self._shown[device.serial] = output
        return True

    @staticmethod
    def output(device, kind: str, look) -> Optional[tuple]:
        """What `look` comes to on `device`, ready to write."""
        caps = device.capabilities()
        dimensions = caps.get('matrix_dimensions') or (1, 1)
        cells = dimensions[0] * dimensions[1]
        if caps.get('zone_order') and hasattr(device, 'set_zone_colours'):
            # A headset's ears: the first of the SDK's headset LEDs is the left.
            count = len(caps['zone_order'])
            colours = grids.strip(look) if kind == 'headset' else grids.resample(grids.strip(look), count)
            colours = (colours + [colours[0] if colours else grids.BLACK] * count)[:count]
            return ('ears', tuple(colours))
        if caps.get('custom_frame') and cells > 1:
            rows, columns = dimensions
            if kind == 'keyboard' and key_column_shift(caps.get('pid')):
                # A board with its keys elsewhere in the matrix: each key of
                # the SDK's grid goes to where this board has it.
                colours = [grids.BLACK] * cells
                for row, line in enumerate(look):
                    for column, colour in enumerate(line):
                        at_row, at_column = key_cell(caps.get('pid'), row, column)
                        if at_row < rows and at_column < columns:
                            colours[at_row * columns + at_column] = colour
            elif kind in ('keyboard', 'keypad') or (kind == 'mouse' and rows > 1):
                colours = [grids.at(look, row, column)
                           for row in range(rows) for column in range(columns)]
            elif kind == 'mouse':
                colours = grids.mouse_strip(look, columns)
            else:
                colours = grids.resample(grids.strip(look), cells)
            return ('frame', rows, columns, tuple(colours))
        zones = [zone for zone, zone_caps in (caps.get('zones') or {}).items()
                 if 'static' in (zone_caps.get('effects') or [])]
        if not zones:
            return None
        if kind == 'mouse':
            colours = {zone: grids.mouse_zone(look, zone) for zone in zones}
        else:
            colours = {zone: grids.average(grids.strip(look)) for zone in zones}
        return ('zones', tuple(sorted(colours.items())))

    def _write(self, device, output: tuple) -> None:
        if output[0] == 'ears':
            device.set_zone_colours([list(colour) for colour in output[1]])
            return
        if output[0] == 'zones':
            shown = self._shown.get(device.serial)
            before = dict(shown[1]) if shown and shown[0] == 'zones' else {}
            for zone, colour in output[1]:
                if before.get(zone) != colour:
                    device.set_static(*colour, zone=zone)
            return
        _kind, rows, columns, colours = output
        frame = Frame(rows, columns)
        for index, colour in enumerate(colours):
            frame.set(index // columns, index % columns, *colour)
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
            # Stopped, not forgotten: it comes back when the game is done.
            self.effects.clear_serial(device.serial)
        if self.persistence is not None:
            self.persistence.hold_lighting(device.serial)
        self._taken[device.serial] = device
        logger.info('a game is lighting %s', device.name)

    def release(self, serial: str) -> None:
        """Give a device back the lighting it had before a game took it."""
        device = self._taken.pop(serial, None)
        self._shown.pop(serial, None)
        self._custom_set.discard(serial)
        if device is None:
            return
        if self.persistence is not None:
            self.persistence.release_lighting(serial)
        try:
            device.restore()
            if self.effects is not None:
                self.effects.restore(device)
            if self.on_release is not None:
                self.on_release(device)
        except Exception:  # noqa: BLE001 - it is gone, or comes back on its own
            logger.debug('could not give %s its lighting back', device.name, exc_info=True)
        logger.info('%s has its own lighting back', device.name)

    def release_all(self) -> None:
        for serial in list(self._taken):
            self.release(serial)
