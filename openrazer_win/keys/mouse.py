"""A low-level mouse hook for the side buttons, which may hold a press back.

Razer's mice have no command for what a button does: Synapse reassigns them on
the PC, and so does this.  The hook sees every mouse event on the system, so it
is only installed while a connected mouse has a side button reassigned, and
it decides about nothing but the two side buttons.  Everything else -- every
movement -- is passed straight on.

Windows does not say which mouse an event came from, here or anywhere a
program can stop it: a button reassigned on one mouse is reassigned on every
mouse.  Raw input names the device, but comes after the hooks, and not at all
for an event a hook has stopped.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
from typing import Callable, Optional

from .inject import MARKER

logger = logging.getLogger(__name__)

WH_MOUSE_LL = 14
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C
WM_TIMER = 0x0113
WM_QUIT = 0x0012
XBUTTON1 = 0x0001
XBUTTON2 = 0x0002

#: The side buttons, by the name a binding is kept under: Back and Forward,
#: "Mouse Button 4" and "Mouse Button 5" in Synapse.
BUTTONS = {XBUTTON1: 'MOUSE4', XBUTTON2: 'MOUSE5'}
BUTTON_KEYS = tuple(BUTTONS.values())

#: How often the hook is put in afresh.  Windows takes a low-level hook away,
#: without a word, once it has been too slow to answer, and a mouse that goes
#: unheard looks just like one nobody is moving.
REARM_MS = 10_000

#: How long a thread of the daemon may hold Python while the hook waits for it.
#: Every movement of the mouse goes through the hook, so this is how much a
#: busy daemon could delay one.
SWITCH_INTERVAL = 0.001


class _POINT(ctypes.Structure):
    _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('pt', _POINT), ('mouseData', ctypes.c_ulong), ('flags', ctypes.c_ulong),
                ('time', ctypes.c_ulong), ('dwExtraInfo', ctypes.c_size_t)]


def is_available() -> bool:
    return sys.platform == 'win32'


class ButtonHook:
    """Calls ``on_button(name, down) -> bool`` for each side button; True swallows it.

    What is sent by the daemon itself is never shown to it.
    """

    def __init__(self, on_button: Callable[[str, bool], bool]):
        self._on_button = on_button
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._post = None
        self._ready = threading.Event()
        self.running = False
        #: Callbacks that raised.
        self.errors = 0
        self._saved_interval: Optional[float] = None

    def start(self) -> bool:
        if not is_available():
            return False
        if self._thread is not None and self._thread.is_alive():
            return self.running
        self._ready.clear()
        self._thread_id = None
        self._thread = threading.Thread(target=self._run, name='mouse-buttons', daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)
        if self.running and self._saved_interval is None:
            self._saved_interval = sys.getswitchinterval()
            sys.setswitchinterval(min(self._saved_interval, SWITCH_INTERVAL))
        return self.running

    def stop(self) -> None:
        if self._thread is None:
            return
        if self._thread_id and self._post is not None:
            self._post(self._thread_id, WM_QUIT, 0, 0)
        self._thread.join(timeout=2.0)
        self._thread = None
        self._thread_id = None
        self.running = False
        if self._saved_interval is not None:
            sys.setswitchinterval(self._saved_interval)
            self._saved_interval = None

    def _run(self) -> None:  # pragma: no cover - needs a Windows desktop
        from ctypes import wintypes

        procedure_type = ctypes.WINFUNCTYPE(
            wintypes.LPARAM, ctypes.c_int, wintypes.WPARAM, ctypes.POINTER(_MSLLHOOKSTRUCT))
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        user32.SetWindowsHookExW.argtypes = (
            ctypes.c_int, procedure_type, wintypes.HINSTANCE, wintypes.DWORD)
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.CallNextHookEx.argtypes = (
            wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, ctypes.POINTER(_MSLLHOOKSTRUCT))
        user32.CallNextHookEx.restype = wintypes.LPARAM
        user32.GetMessageW.argtypes = (
            ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
        user32.GetMessageW.restype = wintypes.BOOL
        user32.PostThreadMessageW.argtypes = (
            wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.PostThreadMessageW.restype = wintypes.BOOL
        user32.SetTimer.argtypes = (wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p)
        user32.SetTimer.restype = ctypes.c_size_t
        user32.KillTimer.argtypes = (wintypes.HWND, ctypes.c_size_t)
        user32.KillTimer.restype = wintypes.BOOL
        self._post = user32.PostThreadMessageW
        call_next = user32.CallNextHookEx
        on_button = self._on_button
        hook = [None]

        def callback(code, message, data):
            # Every movement comes through here: the test is kept to one line.
            if code >= 0 and (message == WM_XBUTTONDOWN or message == WM_XBUTTONUP):
                raw = data.contents
                name = BUTTONS.get((raw.mouseData >> 16) & 0xFFFF)
                if name is not None and raw.dwExtraInfo != MARKER:
                    try:
                        if on_button(name, message == WM_XBUTTONDOWN):
                            return 1
                    except Exception:  # noqa: BLE001 - a hook must never raise
                        self.errors += 1
            return call_next(None, code, message, data)

        def install() -> bool:
            fresh = user32.SetWindowsHookExW(
                WH_MOUSE_LL, self._procedure, kernel32.GetModuleHandleW(None), 0)
            if not fresh:
                logger.warning('cannot install the mouse button hook: WinError %d',
                               ctypes.get_last_error())
                return False
            stale, hook[0] = hook[0], fresh
            if stale:
                user32.UnhookWindowsHookEx(stale)
            return True

        # Held for as long as the hook is installed, or Windows calls into a
        # trampoline the garbage collector has freed.
        self._procedure = procedure_type(callback)
        if not install():
            self._ready.set()
            return
        timer = user32.SetTimer(None, 0, REARM_MS, None)
        self._thread_id = kernel32.GetCurrentThreadId()
        self.running = True
        self._ready.set()
        logger.info('reassigning mouse side buttons')

        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            if message.message == WM_TIMER:
                install()
        if timer:
            user32.KillTimer(None, timer)
        user32.UnhookWindowsHookEx(hook[0])
        self._procedure = None
        self.running = False
        logger.info('mouse side buttons left alone again')

