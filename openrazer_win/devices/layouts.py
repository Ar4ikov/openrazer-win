"""Keyboards whose LED matrix is not laid out like every other Razer board's.

A Razer keyboard's matrix has Esc at row 0, column 1 and M1-M5 down column 0,
and everything here that places a key -- the reactive effects, what a game
draws through Chroma -- counts on it.  The BlackWidow V4 and V4 Pro do not:
column 1 is theirs for the dial (or M6) and the macro keys, numbered from the
bottom, and every other key is one column further on (issue #19).
"""
from __future__ import annotations

from typing import Optional, Tuple

#: Product id -> how many columns later than usual the keys start.
KEY_COLUMN_SHIFT = {
    0x0287: 1,      # BlackWidow V4
    0x028D: 1,      # BlackWidow V4 Pro
}


#: The four media keys over the numpad, which these boards have in their
#: matrix where others have nothing.  The volume roller beside them has no
#: light of its own (issue #19): ``None`` keeps a turn of it from lighting
#: the key the standard map has it on.
_MEDIA_KEYS = {
    'MEDIA_BACK': (0, 19), 'MEDIA_PLAY': (0, 20), 'MEDIA_FORWARD': (0, 21),
    'MUTE': (0, 22), 'VOL_UP': None, 'VOL_DOWN': None,
}

#: Product id -> keys of its own, by the name a press of them goes by, at the
#: matrix cell they light, or None for a key that lights nothing.  The dial is the V4 Pro's, where the V4 has M6.
BOARD_KEYS = {
    0x0287: dict(_MEDIA_KEYS),
    0x028D: dict(_MEDIA_KEYS, DIAL=(0, 1)),
}


def board_keys(pid: Optional[int]) -> dict:
    """Keys only this board has, or that it has somewhere of its own."""
    return BOARD_KEYS.get(pid, {}) if pid is not None else {}


def key_column_shift(pid: Optional[int]) -> int:
    return KEY_COLUMN_SHIFT.get(pid, 0) if pid is not None else 0


def key_cell(pid: Optional[int], row: int, column: int) -> Tuple[int, int]:
    """Where the key at ``(row, column)`` of the standard matrix is on `pid`."""
    shift = key_column_shift(pid)
    if not shift:
        return row, column
    if column == 0:
        # The macro column: M6 or the dial on top, then M5 down to M1.
        return (6 - row if 1 <= row <= 5 else row), shift
    return row, column + shift
