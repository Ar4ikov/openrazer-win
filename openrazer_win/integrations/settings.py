"""Which integrations are switched on, kept beside the daemon's other state."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from typing import Optional

from ..core.persistence import default_config_dir

logger = logging.getLogger(__name__)

#: Every integration there is a switch for.
NAMES = ('accent', 'chroma', 'openrgb')

#: Integrations that listen on a port of the user's choosing.
PORTS = ('openrgb',)


class IntegrationSettings:
    """A small JSON file of switches, all off until turned on."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(default_config_dir(), 'integrations.json')
        self._lock = threading.Lock()
        self._data: dict = {}
        try:
            with open(self.path, encoding='utf-8') as handle:
                data = json.load(handle)
            self._data = data if isinstance(data, dict) else {}
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            logger.warning('ignoring unreadable %s', self.path, exc_info=True)

    def enabled(self, name: str) -> bool:
        with self._lock:
            return self._data.get(name) is True

    def set(self, name: str, enabled: bool) -> None:
        if name not in NAMES:
            raise ValueError('no integration called {0!r}; there are {1}'.format(
                name, ', '.join(NAMES)))
        with self._lock:
            self._data[name] = bool(enabled)
            self._write()

    def port(self, name: str, default: int) -> int:
        """The port `name` listens on: the one chosen, or `default`."""
        with self._lock:
            chosen = self._data.get(name + '_port')
        return chosen if type(chosen) is int and 0 < chosen < 65536 else default

    def set_port(self, name: str, port: int) -> None:
        if name not in PORTS:
            raise ValueError('{0} has no port to choose'.format(name))
        if type(port) is not int or not 0 < port < 65536:
            raise ValueError('a port is a number from 1 to 65535, not {0!r}'.format(port))
        with self._lock:
            self._data[name + '_port'] = port
            self._write()

    def _write(self) -> None:
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        handle = tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=directory,
                                             delete=False, suffix='.tmp')
        try:
            json.dump(self._data, handle, indent=1, sort_keys=True)
        finally:
            handle.close()
        os.replace(handle.name, self.path)

    def as_dict(self) -> dict:
        with self._lock:
            return {name: self._data.get(name) is True for name in NAMES}
