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

A profile also carries what the daemon keeps beyond lighting -- keyswitch
mode, DPI, polling rate -- and loading it puts those back too.  The profile
last saved or loaded is the device's *active* one: the daemon writes every
later change into it, so switching is all there is to do.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime
from typing import Optional

from .device import KEYSWITCH_DEFAULT
from .persistence import DEFAULT_ZONE_STATE, default_config_dir

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

        The lock is held to the end: the daemon saves from a thread per
        request, and two writes finishing out of order would leave the older
        state in the file.
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
                     'active': entry.get('active') is True}
                    for entry in self._data.get(serial, [])]

    def active(self, serial: str) -> Optional[str]:
        """The name of the profile `serial` is following, if it follows one."""
        with self._lock:
            for entry in self._data.get(serial, []):
                if entry.get('active') is True:
                    return entry['name']
        return None

    def get(self, serial: str, name: str) -> dict:
        wanted = clean_name(name).lower()
        with self._lock:
            for entry in self._data.get(serial, []):
                if entry['name'].lower() == wanted:
                    return json.loads(json.dumps(entry))     # a private copy
        raise ProfileError('no profile called {0!r}'.format(name))

    # -- writing -----------------------------------------------------------
    def put(self, serial: str, name: str, state: dict, activate: bool = False) -> dict:
        """Save `state` under `name`, replacing any profile of that name."""
        name = clean_name(name)
        entry = _entry(name, state)
        with self._lock:
            profiles = self._data.setdefault(serial, [])
            for index, existing in enumerate(profiles):
                if existing['name'].lower() == name.lower():
                    if existing.get('active') is True:
                        entry['active'] = True
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

    def create(self, serial: str, name: str, state: dict) -> dict:
        """Add a profile holding `state`.

        Unlike :meth:`put`, a name already taken is refused: this is for a new
        profile, and quietly overwriting one with defaults would lose it.
        """
        name = clean_name(name)
        with self._lock:
            if any(entry['name'].lower() == name.lower()
                   for entry in self._data.get(serial, [])):
                raise ProfileError('there is already a profile called {0!r}'.format(name))
            return self.put(serial, name, state)

    def activate(self, serial: str, name: str) -> str:
        """Make `name` the profile `serial` follows; returns its stored name."""
        wanted = clean_name(name).lower()
        with self._lock:
            if not any(entry['name'].lower() == wanted
                       for entry in self._data.get(serial, [])):
                raise ProfileError('no profile called {0!r}'.format(name))
            found = self._mark_active(serial, wanted)
        self.save()
        return found

    def update_active(self, serial: str, state: dict) -> bool:
        """Write `state` into the active profile, if there is one and it differs.

        Compared before writing: the daemon calls this after every request that
        touches persistence, reads included, and rewriting the file -- with a
        new time on it -- for a request that changed nothing would be churn.
        """
        with self._lock:
            for index, existing in enumerate(self._data.get(serial, [])):
                if existing.get('active') is not True:
                    continue
                entry = _entry(existing['name'], state)
                if _content(entry) == _content(existing):
                    return False
                entry['active'] = True
                self._data[serial][index] = entry
                break
            else:
                return False
        self.save()
        return True

    def _mark_active(self, serial: str, wanted: str) -> str:
        found = ''
        for entry in self._data.get(serial, []):
            if entry['name'].lower() == wanted.lower():
                entry['active'] = True
                found = entry['name']
            else:
                entry.pop('active', None)
        return found

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
    entry = {
        'name': name,
        'saved': datetime.now().replace(microsecond=0).isoformat(),
        # Through JSON: a copy that shares nothing with live state, in the
        # shape it will have once read back from the file.
        'device': json.loads(json.dumps(state.get('device') or {})),
        'zones': json.loads(json.dumps(state.get('zones') or {})),
    }
    if state.get('snaptap') is not None:
        entry['snaptap'] = json.loads(json.dumps(state['snaptap']))
    return entry


def _content(entry: dict) -> dict:
    """What a profile holds, leaving out when it was saved and whether it is active."""
    return {key: value for key, value in entry.items()
            if key not in ('saved', 'active')}


def default_state(device) -> dict:
    """What a new profile holds: the state the daemon gives a device it knows nothing of.

    The lighting is persistence's default zone state, which is what a profile
    of an untouched device would have recorded anyway.  The keyswitch mode is
    the one a keyboard comes in.  DPI and polling rate have no default here --
    they differ from mouse to mouse -- so they are left out, and a new profile
    does not change them.
    """
    capabilities = device.capabilities()
    stored = {}
    if capabilities.get('keyswitch_optimization'):
        stored['keyswitch_optimization'] = KEYSWITCH_DEFAULT
    zones = {zone: json.loads(json.dumps(DEFAULT_ZONE_STATE))
             for zone in capabilities.get('zones', {})}
    return {'device': stored, 'zones': zones}


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

    _apply_performance(device, profile.get('device') or {})
    device.restore()


def _apply_performance(device, stored: dict) -> None:
    """Put back what a profile holds beyond lighting, where the device has it.

    restore() replays lighting only: a reconnecting device keeps these itself.
    A profile being loaded is a switch, though -- Typing for the desktop,
    Gaming and another DPI for a game -- so they are written here.  A key the
    profile does not have is left as it is, which keeps profiles saved before
    they carried these doing what they always did.
    """
    capabilities = device.capabilities()
    steps = []
    if capabilities.get('keyswitch_optimization') and 'keyswitch_optimization' in stored:
        steps.append(('keyswitch', lambda: device.set_keyswitch_optimization(
            stored['keyswitch_optimization'])))
    stages = stored.get('dpi_stages')
    if capabilities.get('dpi_stages') and isinstance(stages, dict) and stages.get('stages'):
        steps.append(('dpi stages', lambda: device.set_dpi_stages(
            stages.get('active', 1), [tuple(stage) for stage in stages['stages']])))
    # After the stages: setting them can move the DPI, and the profile's own
    # DPI is the one it was saved with.
    dpi = stored.get('dpi')
    if capabilities.get('dpi') and isinstance(dpi, (list, tuple)) and dpi:
        steps.append(('dpi', lambda: device.set_dpi(*dpi[:2])))
    if capabilities.get('poll_rate') and stored.get('poll_rate') is not None:
        steps.append(('polling rate', lambda: device.set_poll_rate(stored['poll_rate'])))
    for what, step in steps:
        try:
            step()
        except Exception as error:  # noqa: BLE001 - the rest of the profile still loads
            logger.debug('could not set the %s of %s: %s', what, device.name, error)
