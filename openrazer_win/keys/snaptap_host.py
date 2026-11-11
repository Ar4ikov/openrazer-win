"""Snap Tap on Windows: the engine, and what it needs from around it.

Everything the engine owns is touched by one thread, the hook's: the callback
runs there, and so do the window and desktop notifications and any change of
settings, handed over with ``defer()``.  That is why the callback takes no lock
-- waiting for one there would hold up every key on the system, and Windows
removes a hook that keeps it waiting.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
from functools import lru_cache
from typing import Callable, Optional

from .codes import KEYS
from .hook import FilterHook, Timings, is_available
from .inject import Injector
from .snaptap import PAIRABLE, PASS, SnapTapEngine, SnapTapSettings, key_ids, pause_reason

logger = logging.getLogger(__name__)

#: While Snap Tap is on, how long the interpreter lets another thread run before
#: the hook's may take over.  At the default 5 ms, a daemon busy drawing effects
#: could keep a key waiting that long at every step.
SWITCH_INTERVAL = 0.001

#: Silence after which the hook is assumed gone.  Windows gives no sign of it,
#: and no key is the only symptom, so an idle keyboard looks the same and gets
#: the same treatment: replacing a healthy hook costs nothing.  Sending a key to
#: see whether it arrives would be input nobody typed.
SILENCE_NS = 30 * 1000 * 1000 * 1000
WATCH_PERIOD = 5.0

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_DESKTOPSWITCH = 0x0020
WINEVENT_OUTOFCONTEXT = 0x0000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

#: What a pair may be made of, letters first, being what most pairs are.
OFFERED_KEYS = sorted(PAIRABLE, key=lambda name: (not 0x41 <= KEYS[name].vk <= 0x5A, name))


@lru_cache(maxsize=1)
def _win32():
    """The Win32 calls, declared once; ``None`` where there is no Win32."""
    if not is_available():
        return None
    from ctypes import wintypes

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
    user32.GetAsyncKeyState.restype = ctypes.c_short
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    procedure_type = ctypes.WINFUNCTYPE(
        None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG, wintypes.LONG,
        wintypes.DWORD, wintypes.DWORD)
    user32.SetWinEventHook.argtypes = (
        wintypes.UINT, wintypes.UINT, wintypes.HMODULE, procedure_type, wintypes.DWORD,
        wintypes.DWORD, wintypes.UINT)
    user32.SetWinEventHook.restype = wintypes.HANDLE
    user32.UnhookWinEvent.argtypes = (wintypes.HANDLE,)
    user32.UnhookWinEvent.restype = wintypes.BOOL
    return user32, kernel32, procedure_type


def foreground_exe() -> Optional[str]:
    """The lower-cased file name of the program in front, if it can be had."""
    win32 = _win32()
    if win32 is None:
        return None
    from ctypes import wintypes

    user32, kernel32, _ = win32
    window = user32.GetForegroundWindow()
    if not window:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
    process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not process:
        return None
    try:
        size = wintypes.DWORD(1024)
        path = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
            return None
    finally:
        kernel32.CloseHandle(process)
    return os.path.basename(path.value).lower()


def key_is_down(vk: int) -> bool:
    win32 = _win32()
    return win32 is not None and bool(win32[0].GetAsyncKeyState(vk) & 0x8000)


class WinEventWatch:
    """Calls `on_change` when the foreground window or the desktop changes.

    Out of context, so the calls arrive on the thread that called :meth:`start`
    through its message loop, and that thread has to be the hook's.
    """

    def __init__(self, on_change: Callable[[], None]):
        self._on_change = on_change
        self._procedure = None
        self._hooks: list = []

    def start(self) -> bool:
        """Whether both events are watched."""
        win32 = _win32()
        if win32 is None:
            return False
        user32, _, procedure_type = win32

        def callback(_hook, _event, _window, _object, _child, _thread, _time):
            try:
                self._on_change()
            except Exception:  # noqa: BLE001 - nothing to raise it to
                logger.debug('window change handler failed', exc_info=True)

        # Referenced for as long as Windows may call it, as with the key hook.
        self._procedure = procedure_type(callback)
        # Two hooks, as one range between these would also be every menu,
        # drag and dialog in between.
        for event in (EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_DESKTOPSWITCH):
            hook = user32.SetWinEventHook(event, event, None, self._procedure, 0, 0,
                                          WINEVENT_OUTOFCONTEXT)
            if hook:
                self._hooks.append(hook)
            else:
                logger.warning('cannot watch for window event %#x: WinError %d', event,
                               ctypes.get_last_error())
        return len(self._hooks) == 2

    def stop(self) -> None:
        for hook in self._hooks:
            _win32()[0].UnhookWinEvent(hook)
        self._hooks = []
        self._procedure = None


class SnapTapHost:
    """Runs the engine on the key service's hook, or on a hook of its own.

    With the key service, Snap Tap sees what the Fn layer lets through.  Under
    ``--no-keys`` there is no such hook, so one is made for as long as Snap Tap
    is on.
    """

    def __init__(self, settings: SnapTapSettings, keys=None, injector=None,
                 hook_factory: Optional[Callable] = None, watch_factory: Optional[Callable] = None,
                 foreground: Optional[Callable[[], Optional[str]]] = None,
                 is_down: Optional[Callable[[int], bool]] = None):
        self.settings = settings
        self.keys = keys
        self.injector = injector if injector is not None else (
            keys.injector if keys is not None else Injector())
        self.engine = SnapTapEngine()
        self._hook_factory = hook_factory or FilterHook
        self._watch_factory = watch_factory or WinEventWatch
        self._foreground = foreground or foreground_exe
        self._is_down = is_down or key_is_down
        #: Only on callers' threads, never in the callback.
        self._lock = threading.Lock()
        self._hook = keys.hook if keys is not None else None
        #: Arming has been asked of the hook's thread; `_watching` says it got done.
        self._armed = False
        self._saved_interval: Optional[float] = None
        self._guard_stop: Optional[threading.Event] = None
        self._guard: Optional[threading.Thread] = None
        # Hook thread only, from here on; read by status() one value at a time.
        self._watch = None
        self._watching = False
        self._deny: frozenset = frozenset()
        self._allow: frozenset = frozenset()
        self.foreground: Optional[str] = None
        self.paused: Optional[str] = None
        self.resets = 0
        self.rearms = 0
        self.failed_sends = 0
        if keys is not None:
            keys.snap_tap = self

    # -- the hook's thread -------------------------------------------------
    def handle(self, event) -> bool:
        """One key event.  True keeps it from everything else."""
        decision = self.engine.handle(event)
        if decision is PASS:
            return False
        if decision.now:
            self._send(decision.now)
        if decision.later and not self._hook.defer(self._resume):
            # Nothing to run it, so it does not run: a key left up is better
            # than a press sent ahead of the release it has to follow.
            self.engine.take_resumes()
        return decision.swallow

    def swallowed(self, event) -> None:
        self.engine.swallowed(event)

    def _resume(self) -> None:
        due = self.engine.take_resumes()
        if due:
            self._send(due)

    def _send(self, events) -> None:
        if self.injector.batch(events) != len(events):
            self.failed_sends += 1

    def on_system_change(self) -> None:
        """The window in front, or the desktop, has changed."""
        self.resets += 1
        self.engine.reset(self._is_down)
        self._look_at_foreground()

    def _look_at_foreground(self) -> None:
        self.foreground = self._foreground()
        self.paused = pause_reason(self.foreground, self._deny, self._allow)
        self.engine.paused = self.paused is not None

    def _arm(self, pairs: list, deny: list, allow: list) -> None:
        self._deny = frozenset(deny)
        self._allow = frozenset(allow)
        if self._watch is None:
            self._watch = self._watch_factory(self.on_system_change)
            self._watching = self._watch.start()
        # Unwatched, nothing would reset a key whose release went to another
        # desktop, or pause it for a denied program brought to the front.
        self.engine.configure(key_ids(pairs) if self._watching else ())
        self.engine.reset(self._is_down)
        self._look_at_foreground()

    def _disarm(self) -> None:
        self.engine.configure(())
        self.engine.paused = False
        self.paused = None
        self.foreground = None
        if self._watch is not None:
            self._watch.stop()
            self._watch = None
        self._watching = False

    def _rearm(self) -> None:
        if self._armed and self._hook.rearm():
            self.rearms += 1
            self.engine.reset(self._is_down, keep_active=True)

    # -- everywhere else ---------------------------------------------------
    def _on_hook_thread(self, job: Callable[[], None]) -> None:
        """Run `job` where the engine lives, and wait for it.

        A job still waiting after the timeout runs later, in order, so one
        queued after it, such as turning off, still comes after it.
        """
        done = threading.Event()

        def run() -> None:
            try:
                job()
            finally:
                done.set()

        hook = self._hook
        if hook is not None and hook.defer(run):
            done.wait(timeout=2.0)
        else:
            job()       # no thread is running, so nothing else is touching it

    def start(self) -> None:
        with self._lock:
            self._apply()

    def configure(self, **changes) -> None:
        with self._lock:
            self.settings.update(**changes)
            self._apply()

    def _apply(self) -> None:
        if self.settings.enabled:
            self._turn_on()
        else:
            self._turn_off()

    def _turn_on(self) -> None:
        own = self.keys is None
        if own and self._hook is None:
            self._hook = self._hook_factory(self.handle)
            self._hook.start()
        if not self._hook.running:
            # Nothing tries again by itself: it stays off until the next change.
            if own:
                self._hook.stop()
                self._hook = None
            return
        settings = self.settings.as_dict()
        if self._saved_interval is None:
            self._saved_interval = sys.getswitchinterval()
            sys.setswitchinterval(SWITCH_INTERVAL)
        self._on_hook_thread(
            lambda: self._arm(settings['pairs'], settings['deny'], settings['allow']))
        self._armed = True
        if self._guard is None:
            self._guard_stop = threading.Event()
            self._guard = threading.Thread(target=self._guard_loop, args=(self._guard_stop,),
                                           name='snaptap-guard', daemon=True)
            self._guard.start()

    def _turn_off(self) -> None:
        if not self._armed:
            return
        if self._guard is not None:
            self._guard_stop.set()
            self._guard.join(timeout=2.0)
            self._guard = None
        self._armed = False
        self._on_hook_thread(self._disarm)
        if self._saved_interval is not None:
            sys.setswitchinterval(self._saved_interval)
            self._saved_interval = None
        if self.keys is None and self._hook is not None:
            self._hook.stop()
            self._hook = None

    def stop(self) -> None:
        with self._lock:
            self._turn_off()

    def _guard_loop(self, stop: threading.Event) -> None:
        while not stop.wait(WATCH_PERIOD):
            self.check_hook()

    def check_hook(self) -> None:
        """Replace the hook if nothing has been heard from it for too long."""
        hook = self._hook
        if hook is None or not self._armed:
            return
        if time.perf_counter_ns() - hook.last_callback >= SILENCE_NS:
            hook.defer(self._rearm)

    def status(self) -> dict:
        settings = self.settings.as_dict()
        hook = self._hook
        return {
            'enabled': settings['enabled'],
            'running': self._armed and self._watching and hook is not None and hook.running,
            'acknowledged': settings['acknowledged'],
            'pairs': settings['pairs'],
            'deny': settings['deny'],
            'allow': settings['allow'],
            'foreground': self.foreground,
            'paused': self.paused,
            'keys': OFFERED_KEYS,
            'latency_us': (hook.timings if hook is not None else Timings()).summary(),
            'resets': self.resets,
            'rearms': self.rearms,
            'failed_sends': self.failed_sends,
            'hook_errors': hook.errors if hook is not None else 0,
        }
