"""Remembering per-device lighting state across restarts.

Razer devices forget most settings when they lose power, and Windows cuts USB
power on sleep and shutdown.  The daemon therefore records what was last
applied per serial number and replays it when a device reappears -- the same
job ``persistence.conf`` does in upstream OpenRazer.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

ZONES = ('backlight', 'logo', 'scroll', 'left', 'right',
         'charging', 'fast_charging', 'fully_charged')

DEFAULT_ZONE_STATE = {
    'effect': 'spectrum',
    'brightness': 75,
    'colors': [0, 255, 0, 0, 255, 255, 0, 0, 255],
    'speed': 1,
    'wave_dir': 1,
}


def default_config_dir() -> str:
    """``%LOCALAPPDATA%\\openrazer-win``, or a sensible fallback."""
    base = os.environ.get('LOCALAPPDATA') or os.environ.get('XDG_CONFIG_HOME')
    if not base:
        base = os.path.join(os.path.expanduser('~'), '.config')
    return os.path.join(base, 'openrazer-win')


class Persistence:
    """A small JSON store keyed by device serial number."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(default_config_dir(), 'persistence.json')
        self._lock = threading.RLock()
        self._data: dict = {}
        self._dirty = False
        self.load()

    # -- storage -----------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            try:
                with open(self.path, encoding='utf-8') as handle:
                    self._data = json.load(handle)
            except FileNotFoundError:
                self._data = {}
            except (OSError, ValueError):
                logger.warning('ignoring unreadable persistence file %s', self.path,
                               exc_info=True)
                self._data = {}

    def save(self) -> None:
        """Atomically write the store, so a crash cannot truncate it."""
        with self._lock:
            if not self._dirty:
                return
            directory = os.path.dirname(self.path)
            os.makedirs(directory, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                'w', encoding='utf-8', dir=directory, delete=False, suffix='.tmp')
            try:
                json.dump(self._data, handle, indent=1, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                handle.close()
            os.replace(handle.name, self.path)
            self._dirty = False

    # -- accessors ---------------------------------------------------------
    def device(self, serial: str) -> dict:
        with self._lock:
            return self._data.setdefault(serial, {})

    def zone(self, serial: str, zone: str) -> dict:
        device = self.device(serial)
        zones = device.setdefault('zones', {})
        state = zones.setdefault(zone, dict(DEFAULT_ZONE_STATE))
        for key, value in DEFAULT_ZONE_STATE.items():
            state.setdefault(key, value)
        return state

    def get(self, serial: str, zone: str, key: str) -> Any:
        return self.zone(serial, zone).get(key)

    def set(self, serial: str, zone: str, key: str, value: Any) -> None:
        with self._lock:
            self.zone(serial, zone)[key] = value
            self._dirty = True

    def get_device_value(self, serial: str, key: str, default: Any = None) -> Any:
        return self.device(serial).get(key, default)

    def set_device_value(self, serial: str, key: str, value: Any) -> None:
        with self._lock:
            self.device(serial)[key] = value
            self._dirty = True

    def forget(self, serial: str) -> None:
        with self._lock:
            self._data.pop(serial, None)
            self._dirty = True

    def all_serials(self) -> list:
        with self._lock:
            return sorted(self._data)

    def as_dict(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data))
