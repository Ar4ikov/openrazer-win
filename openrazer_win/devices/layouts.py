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
