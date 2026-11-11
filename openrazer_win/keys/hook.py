"""A low-level keyboard hook that may hold a key back.

The effects engine's hook only watches.  This one is the user-space half of
what the kernel driver does with a Razer keyboard in driver mode: a key
pressed with Fn held has to be *replaced* -- F1 swallowed, Mute sent in its
place -- so the hook must be able to keep the original from reaching anything
else.  It runs on its own thread with its own message loop, and the decision
is made synchronously; anything slow is handed off by the callback.

Work for this thread can be queued with :meth:`FilterHook.defer`.  It runs once
the callback in progress has returned, which is what lets a key be sent *after*
the event being decided has gone on, and what lets state belonging to this
thread be changed without a lock in the callback.
"""
from __future__ import annotations

import collections
import ctypes
import logging
import sys
import threading
import time
from typing import Callable, NamedTuple, Optional

from .inject import MARKER

logger = logging.getLogger(__name__)

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012
#: Posted to the hook's thread to have it run what :meth:`FilterHook.defer` queued.
WM_DEFERRED = 0x8001         # WM_APP + 1
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


class Timings:
    """How long the last few thousand callbacks took: stored by the callback,
    into a list that never grows, and sorted by whoever asks."""

    SIZE = 4096

    def __init__(self):
        self._samples = [0] * self.SIZE
        self._count = 0

    def add(self, nanoseconds: int) -> None:
        self._samples[self._count % self.SIZE] = nanoseconds
        self._count += 1

    def summary(self) -> dict:
        """Median, 99th percentile and worst case, in microseconds."""
        samples = sorted(self._samples[:min(self._count, self.SIZE)])
        if not samples:
            return {'samples': 0, 'p50': None, 'p99': None, 'max': None}

        def at(fraction: float) -> float:
            return round(samples[int((len(samples) - 1) * fraction)] / 1000.0, 1)

        return {'samples': len(samples), 'p50': at(0.5), 'p99': at(0.99), 'max': at(1.0)}


class FilterHook:
    """Calls ``on_event(KeyEvent) -> bool`` for every key; True swallows it."""

    def __init__(self, on_event: Callable[[KeyEvent], bool]):
        self._on_event = on_event
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._procedure = None
        self._hook = None
        self._post = None
        self._install = None
        self._jobs: collections.deque = collections.deque()
        self._ready = threading.Event()
        self.running = False
        self.timings = Timings()
        #: ``perf_counter_ns`` of the last callback, or of the last install.
        self.last_callback = 0
        #: Callbacks that raised.  Counted, as a log line here would be I/O
        #: with every key on the system waiting behind it.
        self.errors = 0

    def start(self) -> bool:
        if not is_available():
            return False
        if self._thread is not None and self._thread.is_alive():
            return self.running
        # Still set from the last run, these would have this return before the
        # new hook exists, and have defer() post to a thread that has ended.
        self._ready.clear()
        self._thread_id = None
        self._thread = threading.Thread(target=self._run, name='key-filter', daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)
        return self.running

    def stop(self) -> None:
        if self._thread is None:
            return
        if self._thread_id:
            self._post(self._thread_id, WM_QUIT, 0, 0)
        self._thread.join(timeout=2.0)
        self._thread = None
        self._thread_id = None
        self.running = False

    def defer(self, job: Callable[[], None]) -> bool:
        """Run `job` on the hook's thread, after any callback in progress.

        False if there is no such thread to run it.  Callable from any thread,
        the hook's own included.
        """
        thread_id = self._thread_id
        if not thread_id or not self.running:
            return False
        self._jobs.append(job)
        if self._post(thread_id, WM_DEFERRED, 0, 0):
            return True
        # The queue is full or the thread has gone.  The job may have been run
        # by a message posted for another, in which case it is no longer here.
        try:
            self._jobs.remove(job)
        except ValueError:
            return True
        return False

    def rearm(self) -> bool:
        """Install a new hook, then remove the old.  Only on the hook's thread.

        Windows removes a hook that answers too slowly without saying so.  This
        order leaves no moment without a hook, and no key is seen twice: this
        thread is busy here, so a key waits until the old hook has gone.
        """
        return self._install is not None and self._install()

    def _run_jobs(self) -> None:
        while self._jobs:
            job = self._jobs.popleft()
            try:
                job()
            except Exception:  # noqa: BLE001 - the thread outlives one bad job
                logger.warning('deferred key job failed', exc_info=True)

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
        user32.PostThreadMessageW.argtypes = (
            wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.PostThreadMessageW.restype = wintypes.BOOL
        self._post = user32.PostThreadMessageW
        clock = time.perf_counter_ns
        timings = self.timings

        def callback(code, message, data):
            entered = clock()
            self.last_callback = entered
            swallow = False
            if code >= 0 and message in (WM_KEYDOWN, WM_SYSKEYDOWN, WM_KEYUP, WM_SYSKEYUP):
                raw = data.contents
                event = KeyEvent(
                    vk=raw.vkCode, scan=raw.scanCode,
                    extended=bool(raw.flags & LLKHF_EXTENDED),
                    down=message in (WM_KEYDOWN, WM_SYSKEYDOWN),
                    injected=bool(raw.flags & LLKHF_INJECTED),
                    ours=raw.dwExtraInfo == MARKER, time=raw.time)
                try:
                    swallow = self._on_event(event)
                except Exception:  # noqa: BLE001 - a hook must never raise
                    self.errors += 1
            result = 1 if swallow else user32.CallNextHookEx(None, code, message, data)
            timings.add(clock() - entered)
            return result

        def install() -> bool:
            fresh = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, self._procedure, kernel32.GetModuleHandleW(None), 0)
            if not fresh:
                logger.warning('cannot install the key filter: WinError %d',
                               ctypes.get_last_error())
                return False
            stale, self._hook = self._hook, fresh
            if stale:
                # Fails if Windows has already taken it away, which is fine.
                user32.UnhookWindowsHookEx(stale)
            self.last_callback = clock()
            return True

        # Held for as long as the hook is installed, or Windows calls into a
        # trampoline the garbage collector has freed.
        self._procedure = procedure_type(callback)
        if not install():
            self._ready.set()
            return
        self._install = install
        self._thread_id = kernel32.GetCurrentThreadId()
        self.running = True
        self._ready.set()

        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            if message.message == WM_DEFERRED:
                self._run_jobs()
        user32.UnhookWindowsHookEx(self._hook)
        self._hook = None
        self._install = None
        self._jobs.clear()
        self._procedure = None
        self.running = False
