"""Named lighting profiles, kept by the host.

Razer devices keep a few settings themselves -- this port has confirmed
brightness and the auto-shutoff timer surviving a power cycle on the Bluetooth
headset -- but not the colour.  A colour lives only as long as something is
driving the device, which is why vendor software sits in the background
re-asserting it, and why this port's daemon does the same.

Given that, a profile is a named snapshot of what the daemon would restore:
per-zone effect, colours, brightness and speed.  Saving one records the state
that is currently applied; loading one writes it back and replays it through
:meth:`~openrazer_win.core.device.RazerDevice.restore`, which is the same path
a reconnected device takes.  So profiles work for every device the port
supports, not just the ones that forget things.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime
from typing import Optional

from .persistence import default_config_dir

logger = logging.getLogger(__name__)

#: How many profiles one device may have.  Enough to cover a set of moods
#: without turning the list into something that needs paging.
MAX_PROFILES = 10

#: Profile names are used in the CLI and as JSON keys, so they are kept plain.
MAX_NAME_LENGTH = 32


class ProfileError(Exception):
    """A profile could not be saved, loaded or removed."""


def clean_name(name: str) -> str:
    """Normalise a profile name, or explain why it will not do."""
    cleaned = ' '.join(str(name or '').split())
    if not cleaned:
        raise ProfileError('a profile needs a name')
    if len(cleaned) > MAX_NAME_LENGTH:
        raise ProfileError('profile names are limited to {0} characters'.format(
            MAX_NAME_LENGTH))
    return cleaned


class ProfileStore:
    """Named lighting snapshots, one set per device serial."""

    def __init__(self, path: Optional[str] = None, limit: int = MAX_PROFILES):
        self.path = path or os.path.join(default_config_dir(), 'profiles.json')
        self.limit = limit
        self._lock = threading.RLock()
        self._data: dict = {}
        self.load()

    # -- storage -----------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            try:
                with open(self.path, encoding='utf-8') as handle:
                    loaded = json.load(handle)
                self._data = loaded if isinstance(loaded, dict) else {}
            except FileNotFoundError:
                self._data = {}
            except (OSError, ValueError):
                logger.warning('ignoring unreadable profile file %s', self.path,
                               exc_info=True)
                self._data = {}

    def save(self) -> None:
        """Write the store out, replacing the file atomically."""
        with self._lock:
            # ensure_ascii=False: the file is meant to be readable, and a
            # profile named in any language should look like its name in it.
            payload = json.dumps(self._data, indent=1, sort_keys=True,
                                 ensure_ascii=False)
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            'w', encoding='utf-8', dir=directory or None, delete=False,
            prefix='profiles-', suffix='.json')
        try:
            with handle:
                handle.write(payload)
            os.replace(handle.name, self.path)
        except OSError:
            logger.warning('could not write %s', self.path, exc_info=True)
            try:
                os.unlink(handle.name)
            except OSError:
                pass

    # -- reading -----------------------------------------------------------
    def names(self, serial: str) -> list:
        """Profile names for one device, in the order they were saved."""
        with self._lock:
            return [entry['name'] for entry in self._data.get(serial, [])]

    def summaries(self, serial: str) -> list:
        with self._lock:
            return [{'name': entry['name'], 'saved': entry.get('saved'),
                     'zones': sorted(entry.get('zones', {}))}
                    for entry in self._data.get(serial, [])]

    def get(self, serial: str, name: str) -> dict:
        wanted = clean_name(name).lower()
        with self._lock:
            for entry in self._data.get(serial, []):
                if entry['name'].lower() == wanted:
                    return json.loads(json.dumps(entry))     # a private copy
        raise ProfileError('no profile called {0!r}'.format(name))

    # -- writing -----------------------------------------------------------
    def put(self, serial: str, name: str, state: dict) -> dict:
        """Save `state` under `name`, replacing any profile of that name."""
        name = clean_name(name)
        entry = {
            'name': name,
            'saved': datetime.now().replace(microsecond=0).isoformat(),
            'device': dict(state.get('device') or {}),
            'zones': {zone: dict(values)
                      for zone, values in (state.get('zones') or {}).items()},
        }
        with self._lock:
            profiles = self._data.setdefault(serial, [])
            for index, existing in enumerate(profiles):
                if existing['name'].lower() == name.lower():
                    profiles[index] = entry
                    break
            else:
                if len(profiles) >= self.limit:
                    raise ProfileError(
                        'this device already has {0} profiles; delete one '
                        'first'.format(self.limit))
                profiles.append(entry)
        self.save()
        return entry

    def delete(self, serial: str, name: str) -> None:
        wanted = clean_name(name).lower()
        with self._lock:
            profiles = self._data.get(serial, [])
            kept = [p for p in profiles if p['name'].lower() != wanted]
            if len(kept) == len(profiles):
                raise ProfileError('no profile called {0!r}'.format(name))
            if kept:
                self._data[serial] = kept
            else:
                self._data.pop(serial, None)
        self.save()

    def forget(self, serial: str) -> None:
        with self._lock:
            self._data.pop(serial, None)
        self.save()

    def as_dict(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data))


def snapshot(device, persistence) -> dict:
    """Everything about a device's lighting that a profile should carry.

    Read from persistence rather than from the hardware: that is what the
    daemon replays, most devices cannot be asked what they are showing, and it
    keeps a profile identical in shape to the state that restores it.
    """
    serial = device.serial
    zones = {}
    for zone in device.capabilities().get('zones', {}):
        zones[zone] = dict(persistence.zone(serial, zone))
    stored = dict(persistence.device(serial))
    stored.pop('zones', None)
    return {'device': stored, 'zones': zones}


def apply_profile(device, persistence, profile: dict) -> None:
    """Write a profile into persistence, then have the device show it."""
    serial = device.serial
    for key, value in (profile.get('device') or {}).items():
        if key != 'zones':
            persistence.set_device_value(serial, key, value)
    zones = profile.get('zones') or {}
    for zone, values in zones.items():
        for key, value in values.items():
            persistence.set(serial, zone, key, value)

    # Brightness is applied here rather than left to restore().  A reconnecting
    # device is not told its brightness, because some keep it themselves and
    # overwriting would fight them -- but loading a profile is someone asking
    # for exactly this state, brightness included.
    capabilities = device.capabilities().get('zones', {})
    for zone, values in zones.items():
        level = values.get('brightness')
        if level is None or not capabilities.get(zone, {}).get('brightness'):
            continue
        try:
            device.set_brightness(level, zone)
        except Exception as error:  # noqa: BLE001 - reported, not fatal
            logger.debug('could not set %s brightness on %s: %s',
                         zone, device.name, error)

    device.restore()
