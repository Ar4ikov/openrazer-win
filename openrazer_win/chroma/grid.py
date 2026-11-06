"""What a game asks Razer's Chroma SDK to show, as a grid of colours.

The SDK addresses device *kinds*, never devices: a game draws "the keyboard"
on a 6 x 22 grid, "the mouse" on 9 x 7, and Synapse places that on whatever is
plugged in.  Effects come here as the bodies of Razer's REST API -- what the
SDK's own REST server takes, and what this port's stand-in for the SDK's DLL
sends -- and leave as rows of ``(r, g, b)``.

Colours are Windows ``COLORREF`` values: red in the low byte, blue in the
third.
"""
from __future__ import annotations

from typing import List, Tuple

Colour = Tuple[int, int, int]
Grid = List[List[Colour]]

#: Every kind of device the SDK draws on, and its grid.
GRIDS = {
    'keyboard': (6, 22),
    'mouse': (9, 7),
    'headset': (1, 5),
    'mousepad': (1, 15),
    'keypad': (4, 5),
    'chromalink': (1, 5),
}

#: The keyboard's extended grid has a row and a column of its own on every
#: side of the 6 x 22 one, which sits inside it from (1, 1).
EXTENDED_OFFSET = (1, 1)

#: ``Key`` entries of ``CHROMA_CUSTOM_KEY`` that set a key carry this flag.
KEY_FLAG = 0x01000000

#: Nothing larger than this is a grid any SDK version draws on.
MAX_SIDE = 32

BLACK = (0, 0, 0)


class ChromaError(ValueError):
    """An effect that cannot be drawn."""


def colour_of(value) -> Colour:
    """A ``COLORREF`` as ``(r, g, b)``."""
    try:
        value = int(value) & 0xFFFFFF
    except (TypeError, ValueError) as error:
        raise ChromaError('a colour is a number: {0!r}'.format(value)) from error
    return value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF


def blank(category: str) -> Grid:
    rows, columns = GRIDS[category]
    return [[BLACK] * columns for _ in range(rows)]


def _rows(param, what: str) -> list:
    if not isinstance(param, list) or not param or len(param) > MAX_SIDE:
        raise ChromaError('{0} is a list of rows'.format(what))
    if all(isinstance(row, list) for row in param):
        rows = param
    else:
        rows = [param]        # a strip of LEDs is one row
    if any(len(row) > MAX_SIDE for row in rows):
        raise ChromaError('{0} is wider than any device'.format(what))
    return [[colour_of(value) for value in row] for row in rows]


def parse(category: str, body: dict) -> Grid:
    """The colours `body` asks `category` to show."""
    if category not in GRIDS:
        raise ChromaError('no such kind of device: {0!r}'.format(category))
    if not isinstance(body, dict):
        raise ChromaError('an effect is an object with "effect" and "param"')
    effect = body.get('effect')
    param = body.get('param')
    rows, columns = GRIDS[category]
    if effect == 'CHROMA_NONE':
        return blank(category)
    if effect == 'CHROMA_STATIC':
        colour = colour_of((param or {}).get('color') if isinstance(param, dict) else param)
        return [[colour] * columns for _ in range(rows)]
    if effect == 'CHROMA_CUSTOM':
        return _rows(param, 'CHROMA_CUSTOM')
    if effect == 'CHROMA_CUSTOM_KEY' and category == 'keyboard':
        if not isinstance(param, dict):
            raise ChromaError('CHROMA_CUSTOM_KEY takes {"color": rows, "key": rows}')
        return _keys_over(_rows(param.get('color'), 'color'), param.get('key'))
    if effect == 'CHROMA_CUSTOM2':
        if category == 'keyboard':
            if not isinstance(param, dict):
                raise ChromaError('CHROMA_CUSTOM2 takes {"color": rows, "key": rows}')
            extended = _rows(param.get('color'), 'color')
            top, left = EXTENDED_OFFSET
            grid = [[row[column] if column < len(row) else BLACK
                     for column in range(left, left + columns)]
                    for row in extended[top:top + rows]]
            return _keys_over(grid, param.get('key'))
        return _rows(param, 'CHROMA_CUSTOM2')
    raise ChromaError('{0} cannot be drawn on a {1}'.format(effect, category))


def _keys_over(grid: Grid, keys) -> Grid:
    """Keys flagged in `keys` drawn over `grid`."""
    if not keys:
        return grid
    for row, line in zip(grid, _raw_rows(keys)):
        for column, value in enumerate(line[:len(row)]):
            if int(value) & KEY_FLAG:
                row[column] = colour_of(value)
    return grid


def _raw_rows(param) -> list:
    if not isinstance(param, list) or len(param) > MAX_SIDE:
        raise ChromaError('"key" is a list of rows')
    rows = param if all(isinstance(row, list) for row in param) else [param]
    try:
        return [[int(value) for value in row[:MAX_SIDE]] for row in rows]
    except (TypeError, ValueError) as error:
        raise ChromaError('a key is a number') from error


def at(grid: Grid, row: int, column: int) -> Colour:
    if 0 <= row < len(grid) and 0 <= column < len(grid[row]):
        return grid[row][column]
    return BLACK


def average(colours) -> Colour:
    colours = list(colours)
    if not colours:
        return BLACK
    return tuple(sum(channel) // len(colours) for channel in zip(*colours))


def strip(grid: Grid) -> List[Colour]:
    """Every colour of `grid`, row after row."""
    return [colour for row in grid for colour in row]


def resample(colours: List[Colour], count: int) -> List[Colour]:
    """`colours` spread over `count` LEDs."""
    if not colours or count <= 0:
        return [BLACK] * max(count, 0)
    if count == 1:
        return [colours[0]]
    last = len(colours) - 1
    return [colours[round(index * last / (count - 1))] for index in range(count)]


#: Where Razer's mouse grid puts each part of a mouse, as ``(row, column)``.
MOUSE_SCROLL = (2, 3)
MOUSE_LOGO = (7, 3)
MOUSE_BACKLIGHT = (4, 3)
MOUSE_LEFT = [(row, 0) for row in range(1, 8)]
MOUSE_RIGHT = [(row, 6) for row in range(1, 8)]


def mouse_zone(grid: Grid, zone: str) -> Colour:
    """The colour Razer's mouse grid gives one of a mouse's zones."""
    if zone == 'scroll':
        return at(grid, *MOUSE_SCROLL)
    if zone == 'logo':
        return at(grid, *MOUSE_LOGO)
    if zone == 'left':
        return average(at(grid, *cell) for cell in MOUSE_LEFT)
    if zone == 'right':
        return average(at(grid, *cell) for cell in MOUSE_RIGHT)
    return at(grid, *MOUSE_BACKLIGHT)


def mouse_strip(grid: Grid, count: int) -> List[Colour]:
    """A mouse's row of LEDs: the logo for one, its sides for more."""
    if count == 1:
        return [at(grid, *MOUSE_LOGO)]
    sides = [at(grid, *cell) for cell in MOUSE_LEFT] + [at(grid, *cell) for cell in MOUSE_RIGHT]
    return resample(sides, count)
