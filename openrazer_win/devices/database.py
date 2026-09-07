"""The supported-device database, generated from upstream OpenRazer.

``tools/extract_device_db.py`` imports ``openrazer_daemon.hardware`` from an
OpenRazer checkout and writes ``data/devices.json``; this module is the runtime
view over that file.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterator, Optional

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
DEVICES_PATH = os.path.join(DATA_DIR, 'devices.json')

#: Device type -> which kernel driver upstream implements it in.  The recipe
#: tables are keyed by driver, so this is how a product id finds its recipes.
DRIVER_FOR_TYPE = {
    'keyboard': 'kbd',
    'keypad': 'kbd',
    'mouse': 'mouse',
    'mousemat': 'accessory',
    'accessory': 'accessory',
    'core': 'accessory',
    'headset': 'accessory',
    'mug': 'accessory',
}


@dataclass(frozen=True)
class DeviceInfo:
    """Static metadata for one supported product id."""

    name: str
    vid: int
    pid: int
    type: str
    class_name: str = ''
    has_matrix: bool = False
    matrix_dims: Optional[tuple] = None
    dedicated_macro_keys: bool = False
    driver_mode: bool = False
    wave_dirs: tuple = (1, 2)
    poll_rates: Optional[tuple] = None
    dpi_max: Optional[int] = None
    image: Optional[str] = None
    methods: frozenset = field(default_factory=frozenset)

    @property
    def driver(self) -> str:
        return DRIVER_FOR_TYPE.get(self.type, 'accessory')

    @property
    def key(self) -> str:
        return '{0:04x}:{1:04x}'.format(self.vid, self.pid)

    def has(self, method: str) -> bool:
        return method in self.methods

    def zones(self) -> list:
        """Lighting zones this device exposes, in a stable order."""
        found = []
        for zone in ('backlight', 'logo', 'scroll', 'left', 'right',
                     'charging', 'fast_charging', 'fully_charged'):
            prefix = 'set_{0}_'.format(zone)
            if zone == 'backlight':
                if any(m.startswith('set_backlight_') for m in self.methods) or \
                        self.has('set_static_effect') or self.has('set_spectrum_effect'):
                    found.append(zone)
                continue
            if any(m.startswith(prefix) for m in self.methods):
                found.append(zone)
        return found

    def to_dict(self) -> dict:
        return {
            'name': self.name,
            'vid': self.vid,
            'pid': self.pid,
            'type': self.type,
            'driver': self.driver,
            'has_matrix': self.has_matrix,
            'matrix_dims': list(self.matrix_dims) if self.matrix_dims else None,
            'dedicated_macro_keys': self.dedicated_macro_keys,
            'wave_dirs': list(self.wave_dirs),
            'poll_rates': list(self.poll_rates) if self.poll_rates else None,
            'dpi_max': self.dpi_max,
            'image': self.image,
            'methods': sorted(self.methods),
            'zones': self.zones(),
        }


class DeviceDatabase:
    """Lookup table over every product id OpenRazer knows about."""

    def __init__(self, entries: list):
        self._by_pid: dict = {}
        self._entries: list = []
        for entry in entries:
            self._entries.append(entry)
            self._by_pid[(entry.vid, entry.pid)] = entry
        self._entries.sort(key=lambda e: (e.type, e.name))

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[DeviceInfo]:
        return iter(self._entries)

    def get(self, vid: int, pid: int) -> Optional[DeviceInfo]:
        return self._by_pid.get((vid, pid))

    def find(self, text: str) -> list:
        """Case-insensitive substring search over device names."""
        needle = text.lower()
        return [e for e in self._entries if needle in e.name.lower()]

    def by_type(self, device_type: str) -> list:
        return [e for e in self._entries if e.type == device_type]

    @property
    def product_ids(self) -> set:
        return {pid for _vid, pid in self._by_pid}


def _load(path: str = DEVICES_PATH) -> DeviceDatabase:
    with open(path, encoding='utf-8') as handle:
        payload = json.load(handle)
    entries = []
    for raw in payload['devices']:
        entries.append(DeviceInfo(
            name=raw['name'],
            vid=raw['vid'],
            pid=raw['pid'],
            type=raw['type'],
            class_name=raw.get('class_name', ''),
            has_matrix=raw.get('has_matrix', False),
            matrix_dims=tuple(raw['matrix_dims']) if raw.get('matrix_dims') else None,
            dedicated_macro_keys=raw.get('dedicated_macro_keys', False),
            driver_mode=raw.get('driver_mode', False),
            wave_dirs=tuple(raw.get('wave_dirs') or (1, 2)),
            poll_rates=tuple(raw['poll_rates']) if raw.get('poll_rates') else None,
            dpi_max=raw.get('dpi_max'),
            image=raw.get('image'),
            methods=frozenset(raw.get('methods', [])),
        ))
    return DeviceDatabase(entries)


@lru_cache(maxsize=1)
def get_database() -> DeviceDatabase:
    """The device database, parsed once per process."""
    return _load()


def lookup(vid: int, pid: int) -> Optional[DeviceInfo]:
    return get_database().get(vid, pid)
