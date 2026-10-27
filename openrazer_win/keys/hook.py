"""A low-level keyboard hook that may hold a key back.

The effects engine's hook only watches.  This one is the user-space half of
what the kernel driver does with a Razer keyboard in driver mode: a key
pressed with Fn held has to be *replaced* -- F1 swallowed, Mute sent in its
place -- so the hook must be able to keep the original from reaching anything
else.  It runs on its own thread with its own message loop, and the decision
is made synchronously; anything slow is handed off by the callback.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
from typing import Callable, NamedTuple, Optional

from .inject import MARKER

logger = logging.getLogger(__name__)

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012
LLKHF_EXTENDED = 0x01
LLKHF_INJECTED = 0x10


class KeyEvent(NamedTuple):
    vk: int
    scan: int
    extended: bool
    down: bool
    #: Sent by software rather than typed.
    injected: bool
    #: Sent by this daemon.
    ours: bool
    #: Milliseconds, from the system's tick count.
    time: int


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('vkCode', ctypes.c_ulong), ('scanCode', ctypes.c_ulong),
                ('flags', ctypes.c_ulong), ('time', ctypes.c_ulong),
                ('dwExtraInfo', ctypes.c_size_t)]


def is_available() -> bool:
    return sys.platform == 'win32'


class FilterHook:
    """Calls ``on_event(KeyEvent) -> bool`` for every key; True swallows it."""

    def __init__(self, on_event: Callable[[KeyEvent], bool]):
        self._on_event = on_event
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._procedure = None
        self._ready = threading.Event()
        self.running = False

    def start(self) -> bool:
        if not is_available():
            return False
        if self._thread is not None:
            return self.running
        self._thread = threading.Thread(target=self._run, name='key-filter', daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)
        return self.running

    def stop(self) -> None:
        if self._thread is None:
            return
        if self._thread_id:
            from ctypes import wintypes

            user32 = ctypes.WinDLL('user32', use_last_error=True)
            user32.PostThreadMessageW.argtypes = (
                wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
            user32.PostThreadMessageW.restype = wintypes.BOOL
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        self._thread.join(timeout=2.0)
        self._thread = None
        self.running = False

    def _run(self) -> None:
        from ctypes import wintypes

        procedure_type = ctypes.WINFUNCTYPE(
            wintypes.LPARAM, ctypes.c_int, wintypes.WPARAM,
            ctypes.POINTER(_KBDLLHOOKSTRUCT))
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        # Every handle-returning call gets its restype declared: the c_int
        # default truncates 64-bit handles, which is what once made
        # SetWindowsHookEx fail with ERROR_MOD_NOT_FOUND.
        kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        user32.SetWindowsHookExW.argtypes = (
            ctypes.c_int, procedure_type, wintypes.HINSTANCE, wintypes.DWORD)
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.CallNextHookEx.argtypes = (
            wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM,
            ctypes.POINTER(_KBDLLHOOKSTRUCT))
        user32.CallNextHookEx.restype = wintypes.LPARAM
        user32.GetMessageW.argtypes = (
            ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
        user32.GetMessageW.restype = wintypes.BOOL

        def callback(code, message, data):
            if code >= 0 and message in (WM_KEYDOWN, WM_SYSKEYDOWN, WM_KEYUP, WM_SYSKEYUP):
                raw = data.contents
                event = KeyEvent(
                    vk=raw.vkCode, scan=raw.scanCode,
                    extended=bool(raw.flags & LLKHF_EXTENDED),
                    down=message in (WM_KEYDOWN, WM_SYSKEYDOWN),
                    injected=bool(raw.flags & LLKHF_INJECTED),
                    ours=raw.dwExtraInfo == MARKER, time=raw.time)
                try:
                    if self._on_event(event):
                        return 1
                except Exception:  # noqa: BLE001 - a hook must never raise
                    logger.debug('key filter failed', exc_info=True)
            return user32.CallNextHookEx(None, code, message, data)

        # Held for as long as the hook is installed, or Windows calls into a
        # trampoline the garbage collector has freed.
        self._procedure = procedure_type(callback)
        hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._procedure, kernel32.GetModuleHandleW(None), 0)
        if not hook:
            logger.warning('cannot install the key filter: WinError %d', ctypes.get_last_error())
            self._ready.set()
            return
        self._thread_id = kernel32.GetCurrentThreadId()
        self.running = True
        self._ready.set()

        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            pass
        user32.UnhookWindowsHookEx(hook)
        self._procedure = None
        self.running = False
