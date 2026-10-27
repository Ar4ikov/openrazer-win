"""Sending keys to Windows, as if typed.

Everything sent from here is tagged in ``dwExtraInfo`` so the daemon's own
hook can tell its injections from keys someone actually pressed -- without
that, a translated key would be translated again, and a macro would record
itself.
"""
from __future__ import annotations

import ctypes
import logging
import sys
from typing import Union

from .codes import Key

logger = logging.getLogger(__name__)

#: "Sent by openrazer-win" -- what the hook looks for in dwExtraInfo.
MARKER = 0x52415A52      # 'RAZR'

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

#: Virtual keys whose events must carry the extended flag even when sent by
#: virtual key: Windows otherwise treats them as their keypad twins.
_EXTENDED_VKS = frozenset((
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D,
    0x6F, 0x90, 0xA3, 0xA5, 0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3, 0xB7,
))


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', ctypes.c_ushort), ('wScan', ctypes.c_ushort),
                ('dwFlags', ctypes.c_ulong), ('time', ctypes.c_ulong),
                ('dwExtraInfo', ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):
    # Never sent; present so the union below has the size Windows expects.
    _fields_ = [('dx', ctypes.c_long), ('dy', ctypes.c_long),
                ('mouseData', ctypes.c_ulong), ('dwFlags', ctypes.c_ulong),
                ('time', ctypes.c_ulong), ('dwExtraInfo', ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [('ki', _KEYBDINPUT), ('mi', _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [('type', ctypes.c_ulong), ('union', _INPUTUNION)]


class Injector:
    """Sends key events through ``SendInput``."""

    def __init__(self):
        self._send = None
        if sys.platform == 'win32':
            user32 = ctypes.WinDLL('user32', use_last_error=True)
            user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int)
            user32.SendInput.restype = ctypes.c_uint
            self._send = user32.SendInput

    def _emit(self, events: list) -> None:
        if self._send is None or not events:
            return
        array = (_INPUT * len(events))()
        for slot, (vk, scan, flags) in zip(array, events):
            slot.type = INPUT_KEYBOARD
            slot.union.ki = _KEYBDINPUT(vk, scan, flags, 0, MARKER)
        sent = self._send(len(events), array, ctypes.sizeof(_INPUT))
        if sent != len(events):
            logger.debug('SendInput sent %d of %d events: WinError %d', sent, len(events),
                         ctypes.get_last_error())

    def key(self, target: Union[Key, int], down: bool) -> None:
        """Press or release a key: a :class:`Key` by scan code, an int by virtual key."""
        flags = 0 if down else KEYEVENTF_KEYUP
        if isinstance(target, Key):
            if target.vk in (0x13,) or target.scan >= 0x64:
                # Pause and F13-F24 go by virtual key: their scan codes are not
                # mapped the same way on every keyboard layout.
                vk_flags = flags | (KEYEVENTF_EXTENDEDKEY if target.extended else 0)
                self._emit([(target.vk, target.scan, vk_flags)])
                return
            flags |= KEYEVENTF_SCANCODE
            if target.extended:
                flags |= KEYEVENTF_EXTENDEDKEY
            self._emit([(0, target.scan, flags)])
            return
        if target in _EXTENDED_VKS:
            flags |= KEYEVENTF_EXTENDEDKEY
        self._emit([(int(target), 0, flags)])

    def tap(self, target: Union[Key, int]) -> None:
        self.key(target, True)
        self.key(target, False)

    def raw(self, vk: int, scan: int, extended: bool, down: bool) -> None:
        """Replay a key exactly as it was recorded."""
        flags = 0 if down else KEYEVENTF_KEYUP
        if extended:
            flags |= KEYEVENTF_EXTENDEDKEY
        if scan:
            flags |= KEYEVENTF_SCANCODE
            self._emit([(0, scan, flags)])
        else:
            self._emit([(vk, 0, flags)])

    def text(self, text: str) -> None:
        """Type `text` character by character, independent of keyboard layout."""
        events = []
        for unit in _utf16_units(text):
            events.append((0, unit, KEYEVENTF_UNICODE))
            events.append((0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
        self._emit(events)


def _utf16_units(text: str) -> list:
    data = text.encode('utf-16-le')
    return [int.from_bytes(data[i:i + 2], 'little') for i in range(0, len(data), 2)]


class RecordingInjector:
    """An :class:`Injector` stand-in that writes down what it would send."""

    def __init__(self):
        self.sent: list = []

    def key(self, target, down: bool) -> None:
        self.sent.append(('key', target, down))

    def tap(self, target) -> None:
        self.key(target, True)
        self.key(target, False)

    def raw(self, vk: int, scan: int, extended: bool, down: bool) -> None:
        self.sent.append(('raw', vk, scan, extended, down))

    def text(self, text: str) -> None:
        self.sent.append(('text', text))
