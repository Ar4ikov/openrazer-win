"""What a macro key does, kept on disk per device.

A binding is one of:

* ``{"type": "keys", "events": [...]}`` -- a recorded or written sequence of
  key presses and releases, each ``{"vk", "scan", "ext", "down", "delay"}``
  with the delay in milliseconds before the event;
* ``{"type": "text", "text": "..."}`` -- typed as is, whatever the layout;
* ``{"type": "launch", "target": "..."}`` -- a program, document or URL;
* ``{"type": "disabled"}`` -- the key does nothing at all.

A key with no binding sends what upstream's driver turns it into -- F13-F17
for M1-M5 -- so it can be bound to anything in any program that takes
hotkeys.  The file is ``%LOCALAPPDATA%\\openrazer-win\\macros.json``, plain
JSON keyed by serial number and then by key name.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
from typing import Optional

from ..core.persistence import default_config_dir

logger = logging.getLogger(__name__)

TYPES = ('keys', 'text', 'launch', 'disabled')

#: Generous limits that still stop a broken client filling the disk or
#: pinning the daemon in a ten-minute "macro".
MAX_EVENTS = 1000
MAX_DELAY_MS = 30000
MAX_TEXT = 4000


class MacroError(ValueError):
    """A binding that cannot be stored."""


def validate(action) -> dict:
    """A clean copy of `action`, or :class:`MacroError` saying what is wrong."""
    if not isinstance(action, dict):
        raise MacroError('a binding is an object with a "type"')
    kind = action.get('type')
    if kind not in TYPES:
        raise MacroError('unknown binding type {0!r}; expected one of {1}'.format(
            kind, ', '.join(TYPES)))
    if kind == 'keys':
        events = action.get('events')
        if not isinstance(events, list) or not events:
            raise MacroError('a key macro needs at least one event')
        if len(events) > MAX_EVENTS:
            raise MacroError('at most {0} events'.format(MAX_EVENTS))
        clean = []
        for event in events:
            try:
                vk = int(event.get('vk', 0))
                scan = int(event.get('scan', 0))
                delay = int(event.get('delay', 0))
            except (AttributeError, TypeError, ValueError) as error:
                raise MacroError('malformed event: {0!r}'.format(event)) from error
            if not (0 <= vk <= 0xFF and 0 <= scan <= 0xFFFF) or not (vk or scan):
                raise MacroError('an event needs a virtual key or a scan code')
            if not 0 <= delay <= MAX_DELAY_MS:
                raise MacroError('delays are 0-{0} ms'.format(MAX_DELAY_MS))
            clean.append({'vk': vk, 'scan': scan, 'ext': bool(event.get('ext', False)),
                          'down': bool(event.get('down', True)), 'delay': delay})
        return {'type': 'keys', 'events': clean}
    if kind == 'text':
        text = action.get('text')
        if not isinstance(text, str) or not text:
            raise MacroError('a text macro needs some text')
        if len(text) > MAX_TEXT:
            raise MacroError('at most {0} characters'.format(MAX_TEXT))
        return {'type': 'text', 'text': text}
    if kind == 'launch':
        target = action.get('target')
        if not isinstance(target, str) or not target.strip():
            raise MacroError('a launch binding needs a program, file or URL')
        return {'type': 'launch', 'target': target.strip()}
    return {'type': 'disabled'}


class MacroStore:
    """Bindings per device serial, saved as JSON."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(default_config_dir(), 'macros.json')
        self._lock = threading.RLock()
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding='utf-8') as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            logger.warning('ignoring unreadable macro file %s', self.path, exc_info=True)
            return
        if isinstance(data, dict):
            self._data = {serial: keys for serial, keys in data.items()
                          if isinstance(keys, dict)}

    def _save(self) -> None:
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        handle = tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=directory,
                                             delete=False, suffix='.tmp')
        try:
            json.dump(self._data, handle, indent=1, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(handle.name, self.path)

    def bindings(self, serial: str) -> dict:
        with self._lock:
            return copy.deepcopy(self._data.get(serial, {}))

    def get(self, serial: str, key: str) -> Optional[dict]:
        with self._lock:
            action = self._data.get(serial, {}).get(key)
            return copy.deepcopy(action) if action is not None else None

    def set(self, serial: str, key: str, action) -> dict:
        clean = validate(action)
        with self._lock:
            self._data.setdefault(serial, {})[key] = clean
            self._save()
        return clean

    def delete(self, serial: str, key: str) -> bool:
        with self._lock:
            keys = self._data.get(serial, {})
            if key not in keys:
                return False
            del keys[key]
            if not keys:
                self._data.pop(serial, None)
            self._save()
        return True
