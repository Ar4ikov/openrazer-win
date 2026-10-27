"""Turning the keyboard driver's key names into Windows keys.

``keys.json`` -- generated from the kernel driver -- speaks in Linux evdev
names (``KEY_F1``, ``KEY_MUTE``), USB HID names for what the vendor report
turns into (``USB_HID_KEY_F13``) and the driver's own pseudo-keys
(``RAZER_GAME_KEY``).  This module is the one place those meet Windows: which
physical key a name is, so the hook can recognise it, and what to send or do
when the translation calls for it.

These tables describe the PC keyboard and Windows, not any Razer device, so
they are written here rather than generated.
"""
from __future__ import annotations

from typing import NamedTuple, Optional


class Key(NamedTuple):
    """A key as Windows sees it: scan code (set 1), extended flag, virtual key."""

    scan: int
    extended: bool
    vk: int


#: evdev names -> the physical key.  Scan codes rather than virtual keys are
#: what identify a key on any layout: the key labelled Q on a US board is A on
#: a French one, and the Fn layer is defined by position.
KEYS = {
    'KEY_ESC': Key(0x01, False, 0x1B),
    'KEY_1': Key(0x02, False, 0x31), 'KEY_2': Key(0x03, False, 0x32),
    'KEY_3': Key(0x04, False, 0x33), 'KEY_4': Key(0x05, False, 0x34),
    'KEY_5': Key(0x06, False, 0x35), 'KEY_6': Key(0x07, False, 0x36),
    'KEY_7': Key(0x08, False, 0x37), 'KEY_8': Key(0x09, False, 0x38),
    'KEY_9': Key(0x0A, False, 0x39), 'KEY_0': Key(0x0B, False, 0x30),
    'KEY_MINUS': Key(0x0C, False, 0xBD), 'KEY_EQUAL': Key(0x0D, False, 0xBB),
    'KEY_BACKSPACE': Key(0x0E, False, 0x08), 'KEY_TAB': Key(0x0F, False, 0x09),
    'KEY_Q': Key(0x10, False, 0x51), 'KEY_W': Key(0x11, False, 0x57),
    'KEY_E': Key(0x12, False, 0x45), 'KEY_R': Key(0x13, False, 0x52),
    'KEY_T': Key(0x14, False, 0x54), 'KEY_Y': Key(0x15, False, 0x59),
    'KEY_U': Key(0x16, False, 0x55), 'KEY_I': Key(0x17, False, 0x49),
    'KEY_O': Key(0x18, False, 0x4F), 'KEY_P': Key(0x19, False, 0x50),
    'KEY_LEFTBRACE': Key(0x1A, False, 0xDB), 'KEY_RIGHTBRACE': Key(0x1B, False, 0xDD),
    'KEY_ENTER': Key(0x1C, False, 0x0D), 'KEY_LEFTCTRL': Key(0x1D, False, 0xA2),
    'KEY_A': Key(0x1E, False, 0x41), 'KEY_S': Key(0x1F, False, 0x53),
    'KEY_D': Key(0x20, False, 0x44), 'KEY_F': Key(0x21, False, 0x46),
    'KEY_G': Key(0x22, False, 0x47), 'KEY_H': Key(0x23, False, 0x48),
    'KEY_J': Key(0x24, False, 0x4A), 'KEY_K': Key(0x25, False, 0x4B),
    'KEY_L': Key(0x26, False, 0x4C), 'KEY_SEMICOLON': Key(0x27, False, 0xBA),
    'KEY_APOSTROPHE': Key(0x28, False, 0xDE), 'KEY_GRAVE': Key(0x29, False, 0xC0),
    'KEY_LEFTSHIFT': Key(0x2A, False, 0xA0), 'KEY_BACKSLASH': Key(0x2B, False, 0xDC),
    'KEY_Z': Key(0x2C, False, 0x5A), 'KEY_X': Key(0x2D, False, 0x58),
    'KEY_C': Key(0x2E, False, 0x43), 'KEY_V': Key(0x2F, False, 0x56),
    'KEY_B': Key(0x30, False, 0x42), 'KEY_N': Key(0x31, False, 0x4E),
    'KEY_M': Key(0x32, False, 0x4D), 'KEY_COMMA': Key(0x33, False, 0xBC),
    'KEY_DOT': Key(0x34, False, 0xBE), 'KEY_SLASH': Key(0x35, False, 0xBF),
    'KEY_RIGHTSHIFT': Key(0x36, False, 0xA1), 'KEY_KPASTERISK': Key(0x37, False, 0x6A),
    'KEY_LEFTALT': Key(0x38, False, 0xA4), 'KEY_SPACE': Key(0x39, False, 0x20),
    'KEY_CAPSLOCK': Key(0x3A, False, 0x14),
    'KEY_NUMLOCK': Key(0x45, True, 0x90), 'KEY_SCROLLLOCK': Key(0x46, False, 0x91),
    'KEY_KPENTER': Key(0x1C, True, 0x0D), 'KEY_RIGHTCTRL': Key(0x1D, True, 0xA3),
    'KEY_RIGHTALT': Key(0x38, True, 0xA5), 'KEY_SYSRQ': Key(0x37, True, 0x2C),
    'KEY_HOME': Key(0x47, True, 0x24), 'KEY_UP': Key(0x48, True, 0x26),
    'KEY_PAGEUP': Key(0x49, True, 0x21), 'KEY_LEFT': Key(0x4B, True, 0x25),
    'KEY_RIGHT': Key(0x4D, True, 0x27), 'KEY_END': Key(0x4F, True, 0x23),
    'KEY_DOWN': Key(0x50, True, 0x28), 'KEY_PAGEDOWN': Key(0x51, True, 0x22),
    'KEY_INSERT': Key(0x52, True, 0x2D), 'KEY_DELETE': Key(0x53, True, 0x2E),
    # Pause is the odd one out: the hook reports it as scan 0x45, the same as
    # Num Lock without its extended flag, so it is told apart by virtual key.
    'KEY_PAUSE': Key(0x45, False, 0x13),
}
for _n in range(1, 11):
    KEYS['KEY_F{0}'.format(_n)] = Key(0x3A + _n, False, 0x6F + _n)
KEYS['KEY_F11'] = Key(0x57, False, 0x7A)
KEYS['KEY_F12'] = Key(0x58, False, 0x7B)
for _n in range(13, 24):
    KEYS['KEY_F{0}'.format(_n)] = Key(0x64 + _n - 13, False, 0x6F + _n)
KEYS['KEY_F24'] = Key(0x76, False, 0x87)

#: Keys with no scan code a keyboard would send, sent by virtual key instead.
VIRTUAL_KEYS = {
    'KEY_MUTE': 0xAD, 'KEY_VOLUMEDOWN': 0xAE, 'KEY_VOLUMEUP': 0xAF,
    'KEY_NEXTSONG': 0xB0, 'KEY_PREVIOUSSONG': 0xB1, 'KEY_STOPCD': 0xB2,
    'KEY_PLAYPAUSE': 0xB3, 'KEY_SLEEP': 0x5F, 'KEY_CALC': 0xB7,
}

#: What the vendor report's USB HID names become.
USB_HID_NAMES = {
    'USB_HID_KEY_MEDIA_MUTE': 'KEY_MUTE',
    'USB_HID_KEY_MEDIA_VOLUMEDOWN': 'KEY_VOLUMEDOWN',
    'USB_HID_KEY_MEDIA_VOLUMEUP': 'KEY_VOLUMEUP',
    'USB_HID_KEY_MEDIA_NEXTSONG': 'KEY_NEXTSONG',
    'USB_HID_KEY_MEDIA_PREVIOUSSONG': 'KEY_PREVIOUSSONG',
    'USB_HID_KEY_MEDIA_PLAYPAUSE': 'KEY_PLAYPAUSE',
    'USB_HID_KEY_MEDIA_SLEEP': 'KEY_SLEEP',
}
for _n in range(13, 25):
    USB_HID_NAMES['USB_HID_KEY_F{0}'.format(_n)] = 'KEY_F{0}'.format(_n)

#: The driver's pseudo-keys, and the Fn functions that have no Windows key,
#: mapped to what the daemon does instead.
ACTIONS = {
    'RAZER_MACRO_KEY': 'macro_record',
    'RAZER_GAME_KEY': 'game_mode',
    'RAZER_BRIGHTNESS_DOWN': 'brightness_down',
    'RAZER_BRIGHTNESS_UP': 'brightness_up',
    # The Joro's keyboard backlight keys: the same thing under evdev names.
    'KEY_KBDILLUMDOWN': 'brightness_down',
    'KEY_KBDILLUMUP': 'brightness_up',
}

#: Fn-layer targets that are themselves macro keys (the BlackWidow V3 Mini puts
#: M1-M4 on Fn+Del/PgUp/PgDn/Ins).
MACRO_TARGETS = {'KEY_MACRO1': 'M1', 'KEY_MACRO2': 'M2', 'KEY_MACRO3': 'M3',
                 'KEY_MACRO4': 'M4'}

#: Vendor report codes that are bindable keys, by the name a binding uses.
#: The rest -- the media codes -- are sent on as they are.
MACRO_KEYS = {0x20: 'M1', 0x21: 'M2', 0x22: 'M3', 0x23: 'M4', 0x24: 'M5', 0x25: 'M6',
              0x60: 'DIAL', 0x63: 'SIDE1', 0x64: 'SIDE2', 0x65: 'SIDE3'}

#: Targets that Windows has nothing for and that are best left alone: display
#: brightness is not a key on Windows, and Bluetooth pairing is the keyboard's.
IGNORED = frozenset(('KEY_BRIGHTNESSDOWN', 'KEY_BRIGHTNESSUP', 'KEY_BLUETOOTH'))

VK_LWIN = 0x5B
VK_RWIN = 0x5C

_BY_SCAN = {(key.scan, key.extended): name for name, key in KEYS.items()
            if name != 'KEY_PAUSE'}


def source_name(scan: int, extended: bool, vk: int) -> Optional[str]:
    """The evdev name of the physical key a hook event came from."""
    if vk == KEYS['KEY_PAUSE'].vk:
        return 'KEY_PAUSE'
    return _BY_SCAN.get((scan, extended))


def output_for(name: str):
    """How to produce `name`: a :class:`Key`, a virtual key code, or ``None``."""
    name = USB_HID_NAMES.get(name, name)
    if name in KEYS:
        return KEYS[name]
    return VIRTUAL_KEYS.get(name)


#: Friendlier names for keys in a written key sequence ("ctrl+shift+esc").
_ALIASES = {
    'ctrl': 'KEY_LEFTCTRL', 'control': 'KEY_LEFTCTRL', 'lctrl': 'KEY_LEFTCTRL',
    'rctrl': 'KEY_RIGHTCTRL', 'shift': 'KEY_LEFTSHIFT', 'lshift': 'KEY_LEFTSHIFT',
    'rshift': 'KEY_RIGHTSHIFT', 'alt': 'KEY_LEFTALT', 'lalt': 'KEY_LEFTALT',
    'ralt': 'KEY_RIGHTALT', 'altgr': 'KEY_RIGHTALT', 'win': 'KEY_LEFTMETA',
    'super': 'KEY_LEFTMETA', 'cmd': 'KEY_LEFTMETA',
    'esc': 'KEY_ESC', 'escape': 'KEY_ESC', 'enter': 'KEY_ENTER', 'return': 'KEY_ENTER',
    'tab': 'KEY_TAB', 'space': 'KEY_SPACE', 'backspace': 'KEY_BACKSPACE',
    'del': 'KEY_DELETE', 'delete': 'KEY_DELETE', 'ins': 'KEY_INSERT', 'insert': 'KEY_INSERT',
    'home': 'KEY_HOME', 'end': 'KEY_END', 'pgup': 'KEY_PAGEUP', 'pageup': 'KEY_PAGEUP',
    'pgdn': 'KEY_PAGEDOWN', 'pagedown': 'KEY_PAGEDOWN', 'up': 'KEY_UP', 'down': 'KEY_DOWN',
    'left': 'KEY_LEFT', 'right': 'KEY_RIGHT', 'printscreen': 'KEY_SYSRQ', 'prtsc': 'KEY_SYSRQ',
    'pause': 'KEY_PAUSE', 'capslock': 'KEY_CAPSLOCK', 'numlock': 'KEY_NUMLOCK',
    'scrolllock': 'KEY_SCROLLLOCK', 'minus': 'KEY_MINUS', 'equal': 'KEY_EQUAL',
    'mute': 'KEY_MUTE', 'volup': 'KEY_VOLUMEUP', 'voldown': 'KEY_VOLUMEDOWN',
    'play': 'KEY_PLAYPAUSE', 'next': 'KEY_NEXTSONG', 'prev': 'KEY_PREVIOUSSONG',
}
KEYS.setdefault('KEY_LEFTMETA', Key(0x5B, True, 0x5B))
KEYS.setdefault('KEY_RIGHTMETA', Key(0x5C, True, 0x5C))


def key_named(name: str):
    """A :class:`Key` or virtual key code for a name such as ``ctrl`` or ``F5``."""
    lowered = name.strip().lower()
    full = _ALIASES.get(lowered, 'KEY_' + lowered.upper())
    if full in KEYS:
        return KEYS[full]
    if full in VIRTUAL_KEYS:
        return VIRTUAL_KEYS[full]
    raise ValueError('unknown key {0!r}'.format(name))


def parse_sequence(text: str) -> list:
    """Turn ``"ctrl+c, ctrl+v"`` into the events of a key macro.

    Each comma- or space-separated chord presses its keys in order and lets
    them go in reverse, the way a hand would.
    """
    events = []
    for chord in text.replace(',', ' ').split():
        keys = [key_named(part) for part in chord.split('+') if part]
        for key in keys:
            events.append(_event(key, True))
        for key in reversed(keys):
            events.append(_event(key, False))
    if not events:
        raise ValueError('no keys given')
    return events


def _event(key, down: bool) -> dict:
    if isinstance(key, Key):
        return {'vk': key.vk, 'scan': key.scan, 'ext': key.extended, 'down': down, 'delay': 0}
    return {'vk': int(key), 'scan': 0, 'ext': int(key) in (0xAD, 0xAE, 0xAF, 0xB0, 0xB1,
                                                              0xB2, 0xB3, 0xB7),
            'down': down, 'delay': 0}


def describe_event(event: dict) -> str:
    """A short readable name for one recorded event, for listings."""
    for name, key in KEYS.items():
        if event.get('scan') and key.scan == event['scan'] and key.extended == event.get('ext'):
            return name[4:]
    for name, vk in VIRTUAL_KEYS.items():
        if vk == event.get('vk'):
            return name[4:]
    return 'vk{0:02x}'.format(event.get('vk', 0))
