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
NAMES = ('accent', 'chroma')


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
