"""Button bindings stored in a Razer mouse's onboard memory.

Class 0x02 of the control protocol: a binding is a function id plus up to five
data bytes, per profile, per button, per layer (normal / Hypershift).  Upstream
OpenRazer does not implement this; the layout was decoded by razerctl, OpenSnek
and GavinViney/openrazer-win, and checked on a Basilisk V3 (1532:0099).

Actions are written as short strings -- ``middle``, ``key:ctrl+c``,
``media:volup``, ``dpi:clutch:400`` -- and a binding this module has no name
for is shown as ``fn=0x.. data=..`` and carried through unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# -- tables ----------------------------------------------------------------
FN_OFF, FN_MOUSE, FN_KEY, FN_DPI, FN_MEDIA, FN_DOUBLE, FN_HYPERSHIFT = 0, 1, 2, 6, 10, 11, 12
#: Macros stored on the mouse (id + playback), from Synapse's rzDevice25 layer
#: as razerctl documents it; read here, not written.
FN_MACRO_TIMES, FN_MACRO_HELD, FN_MACRO_TOGGLE = 3, 4, 5
#: Next onboard profile, and the scroll wheel's free-spin / clicky switch
#: (OpenSnek, Basilisk family factory defaults).
FN_PROFILE, FN_WHEEL_MODE = 7, 0x12
#: A key or mouse button that repeats while held, every ``rate`` (16-bit).
FN_TURBO_KEY, FN_TURBO_BUTTON = 0x0D, 0x0E
SIZES = {FN_OFF: 0, FN_MOUSE: 1, FN_KEY: 2, FN_DPI: 1, FN_MEDIA: 2, FN_DOUBLE: 1, FN_HYPERSHIFT: 1,
         FN_PROFILE: 1, FN_WHEEL_MODE: 1, FN_TURBO_BUTTON: 3}
PROFILE_NEXT = 0x04

#: Bytes of data a binding can carry.
DATA_SIZE = 5

MOUSE = {'left': 1, 'right': 2, 'middle': 3, 'back': 4, 'forward': 5,
         'wheelup': 9, 'wheeldown': 10, 'tiltleft': 0x68, 'tiltright': 0x69}
DPI = {'up': 1, 'down': 2, 'cycle': 6}
MODS = {'ctrl': 0x01, 'shift': 0x02, 'alt': 0x04, 'win': 0x08,
        'rctrl': 0x10, 'rshift': 0x20, 'ralt': 0x40, 'rwin': 0x80}
MEDIA = {'volup': 0x00E9, 'voldown': 0x00EA, 'mute': 0x00E2,
         'playpause': 0x00CD, 'next': 0x00B5, 'prev': 0x00B6}
KEYS = {
    **{chr(ord('a') + i): 0x04 + i for i in range(26)},
    **{str(i): 0x1D + i for i in range(1, 10)}, '0': 0x27,
    'enter': 0x28, 'esc': 0x29, 'backspace': 0x2A, 'tab': 0x2B, 'space': 0x2C,
    **{'f{0}'.format(i): 0x39 + i for i in range(1, 13)},
    **{'f{0}'.format(i): 0x68 + i - 13 for i in range(13, 25)},
    'printscreen': 0x46, 'home': 0x4A, 'pageup': 0x4B, 'delete': 0x4C, 'end': 0x4D,
    'pagedown': 0x4E, 'right': 0x4F, 'left': 0x50, 'down': 0x51, 'up': 0x52,
    'lctrl': 0xE0, 'lshift': 0xE1, 'lalt': 0xE2, 'lwin': 0xE3,  # a modifier sent as a key
}
DPI_CLUTCH, DPI_MAX = 5, 26000
BUTTONS = {'left': 1, 'right': 2, 'middle': 3, 'back': 4, 'forward': 5,
           'wheelup': 9, 'wheeldown': 10, 'dpi': 0x60,
           # Basilisk V3: named after the factory function each holds (OpenSnek's
           # Basilisk notes, and what a Synapse-configured unit reads back).
           'clutch': 0x0F, 'tiltleft': 0x34, 'tiltright': 0x35, 'profile': 0x0E,
           'wheelmode': 0x6A,
           # Viper Ultimate: its right-hand side buttons, as a real unit reports
           # them (issue #24).
           'rightback': 0x06, 'rightforward': 0x07}

#: Layer names as clients spell them, to the byte the mouse uses.
LAYERS = {'normal': 0, 'hypershift': 1}


def _rev(d):
    return {v: k for k, v in d.items()}


@dataclass(frozen=True)
class Binding:
    fn: int
    data: bytes  # always DATA_SIZE bytes, zero beyond ``size``
    raw_size: int = field(default=-1, compare=False)  # -1: derive from fn

    @property
    def size(self) -> int:
        return self.raw_size if self.raw_size >= 0 else SIZES.get(self.fn, DATA_SIZE)

    @classmethod
    def from_raw(cls, fn: int, size: int, data: bytes) -> 'Binding':
        size = min(size, DATA_SIZE)
        return cls(fn, bytes(data[:size]).ljust(DATA_SIZE, b'\0'), size)


def _b(fn: int, *data: int) -> Binding:
    return Binding(fn, bytes(data).ljust(DATA_SIZE, b'\0'))


def parse_action(text: str) -> Binding:
    text = text.strip().lower()
    kind, _, arg = text.partition(':')
    if not arg:
        if text in MOUSE:
            return _b(FN_MOUSE, MOUSE[text])
        if text == 'off':
            return _b(FN_OFF)
        if text == 'double':
            return _b(FN_DOUBLE, 1)
        if text == 'hypershift':
            return _b(FN_HYPERSHIFT, 1)
        if text == 'wheelmode':
            return _b(FN_WHEEL_MODE, 1)
    elif text == 'profile:next':
        return _b(FN_PROFILE, PROFILE_NEXT)
    elif kind == 'turbo':
        button, _, rate = arg.partition(':')
        if button in MOUSE and rate.isdigit() and 0 < int(rate) <= 0xFFFF:
            return Binding(FN_TURBO_BUTTON,
                           bytes((MOUSE[button], int(rate) >> 8, int(rate) & 0xFF, 0, 0)), 3)
    elif kind == 'key':
        mods, usage = 0, 0
        for token in arg.split('+'):
            if token in MODS:
                mods |= MODS[token]
            elif token in KEYS and not usage:
                usage = KEYS[token]
            else:
                raise ValueError('unknown key {0!r} in {1!r}'.format(token, text))
        return _b(FN_KEY, mods, usage)
    elif kind == 'media' and arg in MEDIA:
        return _b(FN_MEDIA, MEDIA[arg] >> 8, MEDIA[arg] & 0xFF)
    elif kind == 'dpi' and arg in DPI:
        return _b(FN_DPI, DPI[arg])
    elif kind == 'dpi' and arg.startswith('clutch:'):
        dpi = arg[len('clutch:'):]
        if dpi.isdigit() and 100 <= int(dpi) <= DPI_MAX:
            high, low = int(dpi) >> 8, int(dpi) & 0xFF
            return Binding(FN_DPI, bytes((DPI_CLUTCH, high, low, high, low)), 5)
    raise ValueError('unknown action {0!r}'.format(text))


def _keys(mods: int, usage: int):
    """``ctrl+c`` for a modifier byte and HID usage, or None if a part has no name."""
    if usage and usage not in _rev(KEYS):
        return None
    parts = [name for name, bit in MODS.items() if mods & bit]
    if usage:
        parts.append(_rev(KEYS)[usage])
    return '+'.join(parts) or None


def describe(b: Binding) -> str:
    d = b.data
    if b.fn == FN_OFF:
        return 'off'
    if b.fn == FN_MOUSE and d[0] in _rev(MOUSE):
        return _rev(MOUSE)[d[0]]
    if b.fn == FN_KEY and _keys(d[0], d[1]):
        return 'key:' + _keys(d[0], d[1])
    if b.fn == FN_MACRO_TIMES and b.size == 3 and d[2]:
        return 'macro:{0:02x}{1:02x}:{2}'.format(d[0], d[1], 'once' if d[2] == 1 else
                                                 '{0}x'.format(d[2]))
    if b.fn in (FN_MACRO_HELD, FN_MACRO_TOGGLE) and b.size == 2:
        return 'macro:{0:02x}{1:02x}:{2}'.format(
            d[0], d[1], 'held' if b.fn == FN_MACRO_HELD else 'toggle')
    if b.fn == FN_PROFILE and d[0] == PROFILE_NEXT:
        return 'profile:next'
    if b.fn == FN_WHEEL_MODE and d[0] == 1:
        return 'wheelmode'
    if b.fn == FN_TURBO_BUTTON and b.size == 3 and d[0] in _rev(MOUSE):
        return 'turbo:{0}:{1}'.format(_rev(MOUSE)[d[0]], d[1] << 8 | d[2])
    if b.fn == FN_TURBO_KEY and b.size == 4 and _keys(d[0], d[1]):
        return 'turbo:key:{0}:{1}'.format(_keys(d[0], d[1]), d[2] << 8 | d[3])
    if b.fn == FN_MEDIA and (d[0] << 8 | d[1]) in _rev(MEDIA):
        return 'media:' + _rev(MEDIA)[d[0] << 8 | d[1]]
    if b.fn == FN_DPI and d[0] in _rev(DPI):
        return 'dpi:' + _rev(DPI)[d[0]]
    if b.fn == FN_DPI and d[0] == DPI_CLUTCH and d[1:3] == d[3:5]:
        return 'dpi:clutch:{0}'.format(d[1] << 8 | d[2])
    if b.fn == FN_DOUBLE:
        return 'double'
    if b.fn == FN_HYPERSHIFT:
        return 'hypershift'
    return 'fn=0x{0:02x} data={1}'.format(b.fn, ' '.join('{0:02x}'.format(x) for x in d[:b.size]))


def parse_button(text: str) -> int:
    text = text.strip().lower()
    if text in BUTTONS:
        return BUTTONS[text]
    try:
        value = int(text[3:] if text.startswith('btn') else text, 0)
    except ValueError:
        raise ValueError('unknown button {0!r} (use a name or a number like 0x34)'.format(
            text)) from None
    if not 0 < value < 256:
        raise ValueError('button id out of range: {0!r}'.format(text))
    return value


def button_name(button: int) -> str:
    return _rev(BUTTONS).get(button, 'btn0x{0:02x}'.format(button))


def binding_to_dict(b: Binding) -> dict:
    """The JSON-friendly form a binding travels in over RPC and in backups."""
    return {'action': describe(b), 'fn': b.fn, 'size': b.size, 'data': list(b.data)}


def _byte(value, what: str) -> int:
    if type(value) is not int or not 0 <= value <= 0xFF:
        raise ValueError('{0} must be a byte, got {1!r}'.format(what, value))
    return value


def binding_from_dict(d: dict) -> Binding:
    """Inverse of :func:`binding_to_dict`; ``action`` is ignored, the bytes rule."""
    fn, size, data = _byte(d['fn'], 'fn'), d['size'], d['data']
    if type(size) is not int or not 0 <= size <= DATA_SIZE:
        raise ValueError('size must be 0..{0}, got {1!r}'.format(DATA_SIZE, size))
    if not isinstance(data, (list, tuple)) or len(data) > DATA_SIZE:
        raise ValueError('data must be a list of up to {0} bytes, got {1!r}'.format(
            DATA_SIZE, data))
    return Binding.from_raw(fn, size, bytes(_byte(x, 'data') for x in data))
