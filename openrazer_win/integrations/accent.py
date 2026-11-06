"""The accent colour Windows is set to, and lighting that follows it.

Windows keeps the colour in the user's registry, where the Settings app writes
it the moment it is changed.  Nothing announces the change to a program
without a window, so it is looked at every couple of seconds: one registry
read, too cheap to matter.
"""
from __future__ import annotations

import logging
import sys
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

#: How often to look whether the colour has changed, in seconds.
ACCENT_POLL = 2.0

DWM_KEY = r'Software\Microsoft\Windows\DWM'

#: What Windows starts with, for when the colour cannot be read.
WINDOWS_BLUE = (0x00, 0x78, 0xD4)


def read_accent() -> Optional[tuple]:
    """The accent colour as ``(r, g, b)``, or ``None`` where there is none to read."""
    if sys.platform != 'win32':
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, DWM_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, 'AccentColor')
    except OSError:
        return None
    if not isinstance(value, int):
        return None
    # A DWORD laid out 0xAABBGGRR: red is the low byte.
    return value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF


class AccentWatch:
    """Hands `apply` the accent colour when switched on, and again on every change."""

    def __init__(self, apply: Callable[[tuple], None],
                 read: Callable[[], Optional[tuple]] = read_accent,
                 interval: float = ACCENT_POLL):
        self._apply = apply
        self._read = read
        self.interval = interval
        #: The colour last handed on.
        self.colour: Optional[tuple] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, name='accent', daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            thread, self._thread = self._thread, None
            self._stop.set()
        if thread is not None:
            thread.join(timeout=2.0)
        self.colour = None

    def check(self, force: bool = False) -> Optional[tuple]:
        """Look at the colour now; hand it on if it changed, or if `force`."""
        colour = self._read()
        if colour is None:
            return None
        colour = tuple(colour)
        if force or colour != self.colour:
            self.colour = colour
            try:
                self._apply(colour)
            except Exception:  # noqa: BLE001 - one bad device must not end the watch
                logger.warning('could not follow the accent colour', exc_info=True)
        return colour

    def _run(self) -> None:
        self.check(force=True)
        while not self._stop.wait(self.interval):
            self.check()
