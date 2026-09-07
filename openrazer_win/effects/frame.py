"""An RGB frame buffer for matrix devices."""
from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Iterable, Optional, Tuple

KEYMAP_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'devices', 'data', 'keymap.json')


@lru_cache(maxsize=1)
def get_keymaps() -> dict:
    """Key name -> ``(row, column)``, per keyboard family."""
    with open(KEYMAP_PATH, encoding='utf-8') as handle:
        return {family: {key: tuple(pos) for key, pos in mapping.items()}
                for family, mapping in json.load(handle).items()}


def keymap_for(device_type: str, name: str = '') -> dict:
    keymaps = get_keymaps()
    lowered = name.lower()
    if 'tartarus' in lowered:
        return keymaps.get('tartarus', {})
    if 'orbweaver' in lowered:
        return keymaps.get('orbweaver', {})
    if 'naga hex v2' in lowered:
        return keymaps.get('naga_hex_v2', {})
    return keymaps.get('default', {})


class Frame:
    """A ``rows x columns`` grid of RGB triples."""

    __slots__ = ('rows', 'columns', '_data')

    def __init__(self, rows: int, columns: int):
        self.rows = max(int(rows), 1)
        self.columns = max(int(columns), 1)
        self._data = bytearray(self.rows * self.columns * 3)

    def clear(self) -> 'Frame':
        self._data = bytearray(len(self._data))
        return self

    def fill(self, red: int, green: int, blue: int) -> 'Frame':
        self._data[:] = bytes((red & 0xFF, green & 0xFF, blue & 0xFF)) * (
            self.rows * self.columns)
        return self

    def _offset(self, row: int, column: int) -> int:
        return (row * self.columns + column) * 3

    def set(self, row: int, column: int, red: int, green: int, blue: int) -> 'Frame':
        if 0 <= row < self.rows and 0 <= column < self.columns:
            offset = self._offset(row, column)
            self._data[offset:offset + 3] = bytes(
                (red & 0xFF, green & 0xFF, blue & 0xFF))
        return self

    def get(self, row: int, column: int) -> Tuple[int, int, int]:
        offset = self._offset(row, column)
        chunk = self._data[offset:offset + 3]
        return (chunk[0], chunk[1], chunk[2])

    def row_bytes(self, row: int) -> bytes:
        start = self._offset(row, 0)
        return bytes(self._data[start:start + self.columns * 3])

    def to_payload(self, rows: Optional[Iterable[int]] = None) -> bytes:
        """Serialise as the ``matrix_custom_frame`` sysfs payload."""
        payload = bytearray()
        for row in (range(self.rows) if rows is None else rows):
            payload += bytes((row, 0, self.columns - 1))
            payload += self.row_bytes(row)
        return bytes(payload)

    def copy(self) -> 'Frame':
        clone = Frame(self.rows, self.columns)
        clone._data = bytearray(self._data)
        return clone

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Frame):
            return NotImplemented
        return (self.rows, self.columns, self._data) == (
            other.rows, other.columns, other._data)
