"""Macros stored in a Razer mouse's onboard memory (class 0x06).

A button bound to ``macro:<id>:...`` (see :mod:`.buttons`) plays the macro with
that 16-bit id.  The macro itself is a list of events, encoded as razerctl
documents from Synapse's rzDevice25 layer and checked against a Basilisk V3:
key down/up as HID usages, mouse-button state masks, wheel notches, delays and
cursor moves.  This module only reads them.
"""
from __future__ import annotations

from .buttons import KEYS, _rev

#: Event type bytes.
KEY_DOWN, KEY_UP, BUTTONS, SCROLL, MOVE = 0x01, 0x02, 0x08, 0x0A, 0x15
#: Delay events carry their milliseconds in 1 to 4 big-endian bytes.
DELAYS = {0x11: 1, 0x12: 2, 0x13: 3, 0x14: 4}

#: Right-hand modifiers as HID usages; the left-hand ones are in ``KEYS``.
_KEY_NAMES = {**_rev(KEYS), 0xE4: 'rctrl', 0xE5: 'rshift', 0xE6: 'ralt', 0xE7: 'rwin'}
_MOUSE_BITS = ('left', 'right', 'middle', 'back', 'forward')
_SAY = {'lctrl': 'Ctrl', 'rctrl': 'Ctrl', 'lshift': 'Shift', 'rshift': 'Shift',
        'lalt': 'Alt', 'ralt': 'Alt', 'lwin': 'Win', 'rwin': 'Win', 'esc': 'Esc'}
#: Pauses shorter than this are the gaps Synapse records between keys, not waits.
_NOTICEABLE_MS = 100


def decode_events(data: bytes) -> list:
    """The events in a macro's data; anything unreadable ends the list as ``raw``."""
    events, i = [], 0
    while i < len(data):
        kind = data[i]
        if kind in (KEY_DOWN, KEY_UP) and i + 1 < len(data):
            usage = data[i + 1]
            events.append({'key': _KEY_NAMES.get(usage, 'key 0x{0:02x}'.format(usage)),
                           'down': kind == KEY_DOWN})
            i += 2
        elif kind == BUTTONS and i + 1 < len(data):
            events.append({'buttons': [name for bit, name in enumerate(_MOUSE_BITS)
                                       if data[i + 1] >> bit & 1]})
            i += 2
        elif kind == SCROLL and i + 1 < len(data) and data[i + 1] in (0x01, 0xFF):
            events.append({'scroll': 1 if data[i + 1] == 0x01 else -1})
            i += 2
        elif kind in DELAYS and i + DELAYS[kind] < len(data):
            width = DELAYS[kind]
            events.append({'delay': int.from_bytes(data[i + 1:i + 1 + width], 'big')})
            i += 1 + width
        elif kind == MOVE and i + 4 < len(data):
            events.append({'move': [int.from_bytes(data[i + 1:i + 3], 'big', signed=True),
                                    int.from_bytes(data[i + 3:i + 5], 'big', signed=True)]})
            i += 5
        else:
            events.append({'raw': ' '.join('{0:02x}'.format(b) for b in data[i:])})
            break
    return events


def _say(key: str) -> str:
    if key in _SAY:
        return _SAY[key]
    return key.upper() if len(key) == 1 or key[1:].isdigit() else key.capitalize()


def summarize(events: list) -> str:
    """What a macro does in a few words: ``Ctrl + Win + Left``, ``A, wait 500 ms, B``."""
    steps, chord, held, buttons = [], [], set(), set()
    for event in events:
        if 'key' in event:
            if event['down']:
                held.add(event['key'])
                if _say(event['key']) not in chord:
                    chord.append(_say(event['key']))
            else:
                held.discard(event['key'])
                if not held and chord:
                    steps.append(' + '.join(chord))
                    chord = []
        elif 'buttons' in event:
            pressed = [b for b in event['buttons'] if b not in buttons]
            steps.extend('{0} click'.format(b.capitalize()) for b in pressed)
            buttons = set(event['buttons'])
        elif 'scroll' in event:
            steps.append('Scroll up' if event['scroll'] > 0 else 'Scroll down')
        elif 'move' in event:
            steps.append('Move the pointer')
        elif 'delay' in event:
            if not held and event['delay'] >= _NOTICEABLE_MS:
                steps.append('wait {0} ms'.format(event['delay']))
        elif 'raw' in event:
            steps.append('(not fully decoded)')
    if chord:
        steps.append(' + '.join(chord) + ' (held)')
    # A trailing pause is the gap after the last key, not a step of its own.
    while steps and steps[-1].startswith('wait '):
        steps.pop()
    return ', '.join(steps) or 'Nothing'
