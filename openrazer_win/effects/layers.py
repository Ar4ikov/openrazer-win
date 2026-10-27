"""Per-key lighting built from layers, drawn by the host.

This is what OpenRazer Studio's lighting editor sends: a stack of layers, each
painting one effect onto a chosen set of matrix cells.  Lower layers show
through where an upper one is dark or has not been triggered, so a static
white keyboard with a red reactive layer on top lights red under each key
pressed and white everywhere else.

A layer is a plain JSON object::

    {"kind": "breath", "cells": [[2, 3], [2, 4]], "colours": [[255, 0, 0]],
     "speed": 1.0, "direction": 1, "name": "WASD", "enabled": true}

Everything here is a pure function of the layers, the time and the key
presses, so the editor's preview -- which runs the same arithmetic -- shows
what the device will.  The pseudo-random twinkle of ``starlight`` is a hash of
the cell, not ``random``, for the same reason.
"""
from __future__ import annotations

import colorsys
import math
from typing import Iterable, List, Optional, Sequence, Tuple

from .frame import Frame

#: Every kind of layer, in the order the editor offers them.
KINDS = ('static', 'gradient', 'breath', 'spectrum', 'wave', 'starlight',
         'reactive', 'ripple')

#: Kinds that let the layers below show through where they are dark.
SEE_THROUGH = ('starlight', 'reactive', 'ripple')

#: Kinds that need to hear about key presses.
KEY_DRIVEN = ('reactive', 'ripple')

#: Kinds that change over time even without key presses.
ANIMATED = ('breath', 'spectrum', 'wave', 'starlight')

MAX_LAYERS = 16
MAX_COLOURS = 4
MAX_NAME = 40

#: Seconds, at speed 1.
BREATH_PERIOD = 4.0
SPECTRUM_PERIOD = 6.0
WAVE_PERIOD = 3.0
STARLIGHT_PERIOD = 2.5
REACTIVE_FADE = 1.0
RIPPLE_DURATION = 0.8

Colour = Tuple[int, int, int]
Cell = Tuple[int, int]


class LayerError(ValueError):
    """A layer that cannot be drawn."""


def _colour(value) -> Colour:
    try:
        red, green, blue = (int(part) for part in list(value)[:3])
    except (TypeError, ValueError) as error:
        raise LayerError('a colour is three numbers 0-255: {0!r}'.format(value)) from error
    return (min(max(red, 0), 255), min(max(green, 0), 255), min(max(blue, 0), 255))


def clean_layers(layers, rows: int, columns: int) -> list:
    """A validated copy of `layers` for a ``rows x columns`` matrix.

    Cells outside the matrix are dropped rather than refused, so a stack made
    for one keyboard can be loaded onto a smaller one.
    """
    if not isinstance(layers, list):
        raise LayerError('"layers" is a list of layers')
    if len(layers) > MAX_LAYERS:
        raise LayerError('at most {0} layers'.format(MAX_LAYERS))
    clean = []
    for layer in layers:
        if not isinstance(layer, dict):
            raise LayerError('a layer is an object with a "kind"')
        kind = layer.get('kind')
        if kind not in KINDS:
            raise LayerError('unknown layer kind {0!r}; expected one of {1}'.format(
                kind, ', '.join(KINDS)))
        cells = []
        seen = set()
        for cell in layer.get('cells') or []:
            try:
                row, column = int(cell[0]), int(cell[1])
            except (TypeError, ValueError, IndexError) as error:
                raise LayerError('a cell is [row, column]: {0!r}'.format(cell)) from error
            if 0 <= row < rows and 0 <= column < columns and (row, column) not in seen:
                seen.add((row, column))
                cells.append([row, column])
        colours = [list(_colour(c)) for c in (layer.get('colours') or [])[:MAX_COLOURS]]
        if not colours:
            colours = [[0, 255, 0]]
        try:
            speed = float(layer.get('speed', 1.0))
        except (TypeError, ValueError) as error:
            raise LayerError('speed is a number') from error
        speed = min(max(speed, 0.1), 5.0)
        direction = 2 if layer.get('direction') == 2 else 1
        clean.append({
            'kind': kind,
            'cells': cells,
            'colours': colours,
            'speed': speed,
            'direction': direction,
            'name': str(layer.get('name') or '')[:MAX_NAME],
            'enabled': layer.get('enabled', True) is not False,
        })
    return clean


def twinkle_phase(row: int, column: int) -> float:
    """A fixed pseudo-random number in [0, 1) for one cell."""
    return (((row * 31 + column * 17 + 7) * 2654435761) & 0xFFFFFFFF) / 4294967296.0


def _hue(hue: float) -> Colour:
    red, green, blue = colorsys.hsv_to_rgb(hue % 1.0, 1.0, 1.0)
    return (int(red * 255), int(green * 255), int(blue * 255))


def _mix(below: Colour, above: Colour, amount: float) -> Colour:
    amount = min(max(amount, 0.0), 1.0)
    return (int(below[0] + (above[0] - below[0]) * amount),
            int(below[1] + (above[1] - below[1]) * amount),
            int(below[2] + (above[2] - below[2]) * amount))


def _scale(colour: Sequence[int], amount: float) -> Colour:
    return (int(colour[0] * amount), int(colour[1] * amount), int(colour[2] * amount))


class LayerStack:
    """Layers ready to draw, with what the effect thread needs to know about them."""

    def __init__(self, layers: list, rows: int, columns: int):
        self.rows = rows
        self.columns = columns
        self.layers = [layer for layer in layers if layer['enabled'] and layer['cells']]
        kinds = {layer['kind'] for layer in self.layers}
        self.animated = bool(kinds & set(ANIMATED))
        self.key_driven = bool(kinds & set(KEY_DRIVEN))
        self._spans = []
        for layer in self.layers:
            columns_used = [cell[1] for cell in layer['cells']]
            self._spans.append((min(columns_used), max(columns_used)))

    @property
    def longest_press(self) -> float:
        """How long a key press can still affect the picture, in seconds."""
        longest = 0.0
        for layer in self.layers:
            if layer['kind'] == 'reactive':
                longest = max(longest, REACTIVE_FADE / layer['speed'])
            elif layer['kind'] == 'ripple':
                longest = max(longest, RIPPLE_DURATION / layer['speed'])
        return longest

    def render(self, frame: Frame, elapsed: float,
               presses: Iterable[Tuple[float, Cell]] = ()) -> Frame:
        """Draw the stack at `elapsed` seconds; presses are ``(age, (row, column))``."""
        presses = list(presses)
        frame.clear()
        for layer, span in zip(self.layers, self._spans):
            self._draw(frame, layer, span, elapsed, presses)
        return frame

    def _draw(self, frame: Frame, layer: dict, span: Tuple[int, int],
              elapsed: float, presses: List[Tuple[float, Cell]]) -> None:
        kind = layer['kind']
        colours = layer['colours']
        speed = layer['speed']
        first = tuple(colours[0])
        left, right = span
        width = max(right - left, 1)

        if kind == 'static':
            for row, column in layer['cells']:
                frame.set(row, column, *first)
        elif kind == 'gradient':
            last = tuple(colours[-1]) if len(colours) > 1 else (0, 0, 0)
            for row, column in layer['cells']:
                position = (column - left) / width
                if layer['direction'] == 2:
                    position = 1.0 - position
                frame.set(row, column, *_mix(first, last, position))
        elif kind == 'breath':
            cycle = elapsed * speed / BREATH_PERIOD
            colour = colours[int(cycle) % len(colours)]
            level = 0.5 - 0.5 * math.cos(cycle * 2 * math.pi)
            shown = _scale(colour, level)
            for row, column in layer['cells']:
                frame.set(row, column, *shown)
        elif kind == 'spectrum':
            shown = _hue(elapsed * speed / SPECTRUM_PERIOD)
            for row, column in layer['cells']:
                frame.set(row, column, *shown)
        elif kind == 'wave':
            travel = elapsed * speed / WAVE_PERIOD
            sign = -1.0 if layer['direction'] == 2 else 1.0
            for row, column in layer['cells']:
                position = (column - left) / (width + 1)
                frame.set(row, column, *_hue(position - sign * travel))
        elif kind == 'starlight':
            for row, column in layer['cells']:
                phase = twinkle_phase(row, column)
                cycle = elapsed * speed / STARLIGHT_PERIOD + phase * 7.0
                # A short bright flash, then dark for most of the cycle.
                level = max(0.0, math.sin(cycle * 2 * math.pi)) ** 6
                colour = colours[int(phase * 997) % len(colours)]
                frame.set(row, column, *_mix(frame.get(row, column), tuple(colour), level))
        elif kind == 'reactive':
            fade = REACTIVE_FADE / speed
            lit = {}
            for age, cell in presses:
                if age < fade:
                    lit[cell] = max(lit.get(cell, 0.0), 1.0 - age / fade)
            for row, column in layer['cells']:
                level = lit.get((row, column))
                if level:
                    frame.set(row, column, *_mix(frame.get(row, column), first, level))
        elif kind == 'ripple':
            duration = RIPPLE_DURATION / speed
            reach = max(frame.columns, 1)
            for row, column in layer['cells']:
                level = 0.0
                for age, (origin_row, origin_column) in presses:
                    if age >= duration:
                        continue
                    radius = age / duration * reach
                    distance = math.hypot((row - origin_row) * 2.0, column - origin_column)
                    delta = abs(distance - radius)
                    if delta < 1.2:
                        level = max(level, (1.0 - delta / 1.2) * (1.0 - age / duration))
                if level > 0.0:
                    frame.set(row, column, *_mix(frame.get(row, column), first, level))


def layers_from_options(options: Optional[dict], rows: int, columns: int) -> list:
    return clean_layers((options or {}).get('layers'), rows, columns)
