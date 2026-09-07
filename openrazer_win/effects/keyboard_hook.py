"""A low-level Windows keyboard hook, used to drive key-reactive effects.

The Linux daemon reads key presses from ``/dev/input`` to place ripples.  There
is no equivalent device node on Windows, so this installs a ``WH_KEYBOARD_LL``
hook on its own thread and translates virtual-key codes into the same key names
OpenRazer's matrix map uses.

The hook only observes: it never swallows or synthesises input, and every event
is passed straight on to the next hook in the chain.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
from ctypes import wintypes
from typing import Callable, Optional

logger = logging.getLogger(__name__)

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
LLKHF_EXTENDED = 0x01

#: Windows virtual-key code -> the key name used by OpenRazer's matrix map.
VK_TO_KEY = {
    0x1B: 'ESC', 0xC0: 'BACKTICK', 0x09: 'TAB', 0x14: 'CAPSLK',
    0xA0: 'LEFTSHIFT', 0xA1: 'RIGHTSHIFT', 0xA2: 'LEFTCTRL', 0xA3: 'RIGHTCTRL',
    0xA4: 'LEFTALT', 0xA5: 'RIGHTALT', 0x20: 'SPACE', 0x0D: 'RETURN',
    # Upstream's matrix calls the Windows key SUPER and the menu key CTXMENU.
    0x08: 'BACKSPACE', 0x5B: 'SUPER', 0x5C: 'SUPER', 0x5D: 'CTXMENU',
    0x2C: 'PRTSCR', 0x91: 'SCRLK', 0x13: 'PAUSE',
    0x2D: 'INS', 0x24: 'HOME', 0x21: 'PAGEUP',
    0x2E: 'DELETE', 0x23: 'END', 0x22: 'PAGEDOWN',
    0x26: 'UPARROW', 0x28: 'DOWNARROW', 0x25: 'LEFTARROW', 0x27: 'RIGHTARROW',
    0x90: 'NUMLK', 0x6F: 'NPFORWARDSLASH', 0x6A: 'NPASTERISK', 0x6D: 'NPDASH',
    0x6B: 'NPPLUS', 0x6E: 'NPPERIOD',
    0xBD: 'DASH', 0xBB: 'EQUALS', 0xDB: 'LEFTSQUAREBRACKET',
    0xDD: 'RIGHTSQUAREBRACKET', 0xDC: 'BACKSLASH', 0xBA: 'SEMICOLON',
    0xDE: 'APOSTROPHE', 0xBC: 'COMMA', 0xBE: 'PERIOD', 0xBF: 'FORWARDSLASH',
}

for _index in range(10):
    VK_TO_KEY[0x30 + _index] = str(_index)          # top-row digits
    VK_TO_KEY[0x60 + _index] = 'NP{0}'.format(_index)  # numeric keypad
for _index in range(26):
    VK_TO_KEY[0x41 + _index] = chr(ord('A') + _index)
for _index in range(1, 13):
    VK_TO_KEY[0x70 + _index - 1] = 'F{0}'.format(_index)

#: The numeric keypad's Enter shares a virtual-key code with Return and is told
#: apart only by the extended-key flag.  Upstream's matrix calls it ENTER and
#: reserves RETURN for the main one.
EXTENDED_OVERRIDES = {0x0D: 'ENTER'}


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('vkCode', wintypes.DWORD),
                ('scanCode', wintypes.DWORD),
                ('flags', wintypes.DWORD),
                ('time', wintypes.DWORD),
                ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong))]


HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_int, wintypes.WPARAM, ctypes.POINTER(KBDLLHOOKSTRUCT))


def is_available() -> bool:
    return sys.platform == 'win32'


class KeyboardHook:
    """Calls `on_key(name)` on its own thread for every key press."""

    def __init__(self, on_key: Callable[[str], None]):
        self._on_key = on_key
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._hook = None
        self._ready = threading.Event()
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> bool:
        if not is_available():
            logger.info('key-reactive effects need Windows; hook not installed')
            return False
        if self._thread is not None:
            return True
        self._thread = threading.Thread(target=self._run, name='keyboard-hook',
                                        daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)
        return self._running

    def stop(self) -> None:
        if self._thread is None:
            return
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        if self._thread_id:
            # WM_QUIT == 0x0012; breaks the message loop below.
            user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)
        self._thread.join(timeout=2.0)
        self._thread = None
        self._running = False

    def _run(self) -> None:
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.CallNextHookEx.argtypes = [
            wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM,
            ctypes.POINTER(KBDLLHOOKSTRUCT)]
        user32.CallNextHookEx.restype = ctypes.c_long

        def callback(code, message, data):
            if code >= 0 and message in (WM_KEYDOWN, WM_SYSKEYDOWN):
                try:
                    self._dispatch(data.contents)
                except Exception:  # noqa: BLE001 - a hook must never raise
                    logger.debug('key handler failed', exc_info=True)
            return user32.CallNextHookEx(None, code, message, data)

        procedure = HOOKPROC(callback)
        self._hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, procedure, kernel32.GetModuleHandleW(None), 0)
        if not self._hook:
            logger.warning('SetWindowsHookEx failed: WinError %d',
                           ctypes.get_last_error())
            self._ready.set()
            return

        self._thread_id = kernel32.GetCurrentThreadId()
        self._running = True
        self._ready.set()

        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

        user32.UnhookWindowsHookEx(self._hook)
        self._hook = None
        self._running = False

    def _dispatch(self, event: KBDLLHOOKSTRUCT) -> None:
        name = None
        if event.flags & LLKHF_EXTENDED:
            name = EXTENDED_OVERRIDES.get(event.vkCode)
        if name is None:
            name = VK_TO_KEY.get(event.vkCode)
        if name:
            self._on_key(name)
