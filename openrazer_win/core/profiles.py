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

A profile also carries the keyswitch mode, DPI and polling rate.  The one last
saved or loaded is the device's *active* profile, and the daemon writes every
later change into it.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime
from typing import Optional

from .persistence import DEFAULT_ZONE_STATE, default_config_dir

logger = logging.getLogger(__name__)

#: How many profiles one device may have.  Enough to cover a set of moods
#: without turning the list into something that needs paging.
MAX_PROFILES = 10

#: Profile names are used in the CLI and as JSON keys, so they are kept plain.
MAX_NAME_LENGTH = 32

#: What a profile carries beyond lighting: the persistence key, which is also
#: the capability's name, how to read it from the device and how to write it.
#: The stages come before the DPI: setting them can move the DPI, and the
#: profile's own DPI is the one it was saved with.
PERFORMANCE = (
    ('keyswitch_optimization', lambda device: device.get_keyswitch_optimization(),
     lambda device, mode: device.set_keyswitch_optimization(mode)),
    ('dpi_stages', lambda device: dict(zip(('active', 'stages'), device.get_dpi_stages())),
     lambda device, stages: device.set_dpi_stages(stages.get('active', 1), stages['stages'])),
    ('dpi', lambda device: list(device.get_dpi()),
     lambda device, dpi: device.set_dpi(*dpi[:2])),
    ('poll_rate', lambda device: device.get_poll_rate(),
     lambda device, rate: device.set_poll_rate(rate)),
)


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
    """Named snapshots, one set per device serial, at most one of them active."""

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
        """Write the store out, replacing the file atomically.

        Under the lock to the end: two of the daemon's threads finishing out of
        order would leave the older state in the file.
        """
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
                     'zones': sorted(entry.get('zones', {})),
                     'active': bool(entry.get('active'))}
                    for entry in self._data.get(serial, [])]

    def active(self, serial: str) -> Optional[str]:
        """The name of the profile `serial` is following, if it follows one."""
        with self._lock:
            return next((entry['name'] for entry in self._data.get(serial, [])
                         if entry.get('active')), None)

    def get(self, serial: str, name: str) -> dict:
        wanted = clean_name(name).lower()
        with self._lock:
            for entry in self._data.get(serial, []):
                if entry['name'].lower() == wanted:
                    return json.loads(json.dumps(entry))     # a private copy
        raise ProfileError('no profile called {0!r}'.format(name))

    # -- writing -----------------------------------------------------------
    def put(self, serial: str, name: str, state: dict, activate: bool = False,
            replace: bool = True) -> dict:
        """Save `state` under `name`, replacing any profile of that name unless
        `replace` is false -- a new profile must not quietly overwrite one."""
        name = clean_name(name)
        entry = _entry(name, state)
        with self._lock:
            profiles = self._data.setdefault(serial, [])
            for index, existing in enumerate(profiles):
                if existing['name'].lower() == name.lower():
                    if not replace:
                        raise ProfileError('there is already a profile called {0!r}'.format(name))
                    profiles[index] = entry
                    break
            else:
                if len(profiles) >= self.limit:
                    raise ProfileError(
                        'this device already has {0} profiles; delete one '
                        'first'.format(self.limit))
                profiles.append(entry)
            if activate:
                self._mark_active(serial, name)
            self.save()
        return entry

    def activate(self, serial: str, name: str) -> None:
        """Make `name` the profile `serial` follows; with no such profile, none."""
        with self._lock:
            self._mark_active(serial, name)
            self.save()

    def update_active(self, serial: str, state: dict) -> None:
        """Write `state` into the active profile, unless it holds that already:
        rewriting the file, with a new time on it, for no change would be churn."""
        with self._lock:
            for existing in self._data.get(serial, []):
                if existing.get('active'):
                    entry = _entry(existing['name'], state)
                    if _content(entry) != _content(existing):
                        existing.clear()
                        existing.update(entry, active=True)
                        self.save()
                    return

    def _mark_active(self, serial: str, name: str) -> None:
        for entry in self._data.get(serial, []):
            if entry['name'].lower() == name.lower():
                entry['active'] = True
            else:
                entry.pop('active', None)

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


def _entry(name: str, state: dict) -> dict:
    held = {'device': state.get('device') or {}, 'zones': state.get('zones') or {}}
    if state.get('snaptap') is not None:
        held['snaptap'] = state['snaptap']
    # Through JSON: a copy that shares nothing with live state, in the shape it
    # will have once read back from the file.
    return {'name': name, 'saved': datetime.now().replace(microsecond=0).isoformat(),
            **json.loads(json.dumps(held))}


def _content(entry: dict) -> dict:
    """What a profile holds, leaving out when it was saved and whether it is active."""
    return {key: value for key, value in entry.items()
            if key not in ('saved', 'active')}


def capture_current(device, persistence) -> None:
    """Read from the device what :data:`PERFORMANCE` persistence has no value for.

    Called before a profile is saved or created, so that it holds all of them:
    a profile without, say, a polling rate would keep whatever the profile
    before it left on the device.  Once read, the value is in persistence and
    the device is not asked again.
    """
    capabilities = device.capabilities()
    for key, read, _ in PERFORMANCE:
        if capabilities.get(key) and persistence.get_device_value(device.serial, key) is None:
            try:
                persistence.set_device_value(device.serial, key, read(device))
            except Exception as error:  # noqa: BLE001 - the profile is saved without it
                logger.warning('could not read the %s of %s: %s', key, device.name, error)


def default_state(device, persistence) -> dict:
    """What a new profile holds: the default lighting, the keyswitch mode a
    keyboard comes in, and the DPI and polling rate the device has now."""
    capabilities = device.capabilities()
    stored = persistence.device(device.serial)
    held = {key: stored[key] for key, _, _ in PERFORMANCE
            if capabilities.get(key) and stored.get(key) is not None}
    if capabilities.get('keyswitch_optimization'):
        held['keyswitch_optimization'] = 'typing'
    return {'device': held,
            'zones': dict.fromkeys(capabilities.get('zones', {}), DEFAULT_ZONE_STATE)}


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
    # restore() replays lighting only, as a reconnecting device keeps the rest
    # itself, but a profile being loaded is a switch: Typing and one DPI for the
    # desktop, Gaming and another for a game.  Written after the lighting, which
    # could otherwise undo them, and only what the profile has, so profiles
    # saved before they carried these do what they always did.
    stored = profile.get('device') or {}
    for key, _, write in PERFORMANCE:
        if device.capabilities().get(key) and stored.get(key) is not None:
            try:
                write(device, stored[key])
            except Exception as error:  # noqa: BLE001 - the rest of the profile still loads
                logger.debug('could not set the %s of %s: %s', key, device.name, error)
