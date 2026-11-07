"""How long a key takes to get through the daemon's hook.

Sends F24 through ``SendInput`` and timestamps it again where a game would read
it, at a Raw Input reader in this process.  The difference, compared across
runs with the daemon's hook on and off, is what the hook adds to every key.
Nothing is pressed for real and F24 is on no keyboard here, but it does go to
the program in front, so run it over something that ignores it.

    openrazer-win snaptap off
    python tools/snaptap_latency.py --label off-idle
    python tools/snaptap_latency.py --label off-loaded --load 4
    openrazer-win snaptap on
    python tools/snaptap_latency.py --label on-idle --gate
    python tools/snaptap_latency.py --label on-loaded --load 4 --gate

``--load`` runs that many processes that do nothing but use the CPU.  ``--gate``
exits 1 if p99 is 1 ms or more, or the worst case 20 ms or more, which is the
proposed bar for a loaded machine.  Each run also prints what the daemon itself
measured, entering and leaving its callback, when a daemon is there to ask.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import multiprocessing
import os
import sys
import threading
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openrazer_win.keys.codes import KEYS  # noqa: E402

GATE_P99_MS = 1.0
GATE_MAX_MS = 20.0

F24 = KEYS['KEY_F24']
WM_INPUT = 0x00FF
WM_QUIT = 0x0012
HWND_MESSAGE = -3
RID_INPUT = 0x10000003
RIM_TYPEKEYBOARD = 1
RIDEV_INPUTSINK = 0x00000100


def percentile(sorted_values: list, fraction: float) -> float:
    return sorted_values[int((len(sorted_values) - 1) * fraction)]


def summarise(milliseconds: list) -> dict:
    values = sorted(milliseconds)
    return {'count': len(values), 'p50_ms': round(percentile(values, 0.5), 4),
            'p99_ms': round(percentile(values, 0.99), 4), 'max_ms': round(values[-1], 4)}


def passes_gate(summary: dict) -> bool:
    return summary['p99_ms'] < GATE_P99_MS and summary['max_ms'] < GATE_MAX_MS


def burn() -> None:
    while True:
        pass


class RawReader:
    """A message-only window that notes when each F24 arrives through Raw Input."""

    def __init__(self):
        self.arrived = threading.Event()
        self.stamp = 0
        self._thread_id = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name='raw-reader', daemon=True)

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(5.0):
            raise SystemExit('the Raw Input reader did not start')

    def stop(self) -> None:
        ctypes.WinDLL('user32').PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        from ctypes import wintypes

        class RAWINPUTDEVICE(ctypes.Structure):
            _fields_ = [('usUsagePage', wintypes.USHORT), ('usUsage', wintypes.USHORT),
                        ('dwFlags', wintypes.DWORD), ('hwndTarget', wintypes.HWND)]

        class RAWINPUTHEADER(ctypes.Structure):
            _fields_ = [('dwType', wintypes.DWORD), ('dwSize', wintypes.DWORD),
                        ('hDevice', wintypes.HANDLE), ('wParam', wintypes.WPARAM)]

        class RAWKEYBOARD(ctypes.Structure):
            _fields_ = [('MakeCode', wintypes.USHORT), ('Flags', wintypes.USHORT),
                        ('Reserved', wintypes.USHORT), ('VKey', wintypes.USHORT),
                        ('Message', wintypes.UINT), ('ExtraInformation', wintypes.ULONG)]

        class RAWINPUT(ctypes.Structure):
            _fields_ = [('header', RAWINPUTHEADER), ('keyboard', RAWKEYBOARD)]

        procedure_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                            wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [('style', wintypes.UINT), ('lpfnWndProc', procedure_type),
                        ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int),
                        ('hInstance', wintypes.HINSTANCE), ('hIcon', wintypes.HANDLE),
                        ('hCursor', wintypes.HANDLE), ('hbrBackground', wintypes.HANDLE),
                        ('lpszMenuName', wintypes.LPCWSTR), ('lpszClassName', wintypes.LPCWSTR)]

        user32 = ctypes.WinDLL('user32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        user32.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                          wintypes.LPARAM)
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.RegisterClassW.argtypes = (ctypes.POINTER(WNDCLASSW),)
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.CreateWindowExW.argtypes = (
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HANDLE,
            wintypes.HINSTANCE, wintypes.LPVOID)
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.GetRawInputData.argtypes = (wintypes.HANDLE, wintypes.UINT, wintypes.LPVOID,
                                           ctypes.POINTER(wintypes.UINT), wintypes.UINT)
        user32.GetRawInputData.restype = wintypes.UINT
        user32.RegisterRawInputDevices.argtypes = (ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT,
                                                   wintypes.UINT)
        user32.RegisterRawInputDevices.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                                       wintypes.UINT)
        user32.GetMessageW.restype = wintypes.BOOL
        user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
        user32.DispatchMessageW.restype = ctypes.c_ssize_t

        def window_procedure(window, message, wparam, lparam):
            if message == WM_INPUT:
                stamp = time.perf_counter_ns()
                raw = RAWINPUT()
                size = wintypes.UINT(ctypes.sizeof(raw))
                read = user32.GetRawInputData(lparam, RID_INPUT, ctypes.byref(raw),
                                              ctypes.byref(size),
                                              ctypes.sizeof(RAWINPUTHEADER))
                # Either will do: not every layout turns this scan code into F24.
                if (read != 0xFFFFFFFF and raw.header.dwType == RIM_TYPEKEYBOARD
                        and (raw.keyboard.VKey == F24.vk or raw.keyboard.MakeCode == F24.scan)):
                    self.stamp = stamp
                    self.arrived.set()
            return user32.DefWindowProcW(window, message, wparam, lparam)

        procedure = procedure_type(window_procedure)
        instance = kernel32.GetModuleHandleW(None)
        window_class = WNDCLASSW(lpfnWndProc=procedure, hInstance=instance,
                                 lpszClassName='openrazer-win-latency')
        if not user32.RegisterClassW(ctypes.byref(window_class)):
            raise SystemExit('RegisterClass failed: {0}'.format(ctypes.get_last_error()))
        window = user32.CreateWindowExW(0, 'openrazer-win-latency', '', 0, 0, 0, 0, 0,
                                        HWND_MESSAGE, None, instance, None)
        device = RAWINPUTDEVICE(0x01, 0x06, RIDEV_INPUTSINK, window)
        if not window or not user32.RegisterRawInputDevices(ctypes.byref(device), 1,
                                                            ctypes.sizeof(device)):
            raise SystemExit('Raw Input is not available: {0}'.format(ctypes.get_last_error()))
        self._thread_id = kernel32.GetCurrentThreadId()
        self._ready.set()

        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.DispatchMessageW(ctypes.byref(message))


def measure(count: int, interval_ms: float) -> tuple:
    """Latency of `count` press-and-release pairs, and how many never arrived."""
    from openrazer_win.keys.inject import Injector

    injector = Injector()
    reader = RawReader()
    reader.start()
    latencies, lost = [], 0
    try:
        for index in range(count * 2):
            reader.arrived.clear()
            sent = time.perf_counter_ns()
            injector.batch([(F24.scan, F24.extended, index % 2 == 0)])
            if reader.arrived.wait(0.5):
                latencies.append((reader.stamp - sent) / 1e6)
            else:
                lost += 1
            time.sleep(interval_ms / 1000.0)
    finally:
        # A run cut short must not leave F24 held down.
        injector.batch([(F24.scan, F24.extended, False)])
        reader.stop()
    return latencies, lost


def daemon_status() -> Optional[dict]:
    from openrazer_win.client import DaemonUnavailable
    from openrazer_win.client.rpc import RpcClient
    from openrazer_win.daemon.protocol import RpcError

    try:
        client = RpcClient(timeout=5.0)
    except DaemonUnavailable:
        return None
    try:
        return client.call('snaptap.status')
    except (RpcError, OSError):
        return None
    finally:
        client.close()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description='Measure what the daemon\'s hook adds to a key.')
    parser.add_argument('--label', default='run', help='what this run is, for the results')
    parser.add_argument('--count', type=int, default=1000, help='presses to send')
    parser.add_argument('--interval-ms', type=float, default=5.0)
    parser.add_argument('--load', type=int, default=0, help='processes to keep the CPU busy with')
    parser.add_argument('--gate', action='store_true',
                        help='exit 1 unless p99 < {0} ms and max < {1} ms'.format(
                            GATE_P99_MS, GATE_MAX_MS))
    args = parser.parse_args(argv)
    if sys.platform != 'win32':
        print('this measures Windows input', file=sys.stderr)
        return 2

    burners = [multiprocessing.Process(target=burn, daemon=True) for _ in range(args.load)]
    for burner in burners:
        burner.start()
    try:
        latencies, lost = measure(args.count, args.interval_ms)
    finally:
        for burner in burners:
            burner.terminate()
    if not latencies:
        print('nothing arrived at the Raw Input reader', file=sys.stderr)
        return 2
    summary = summarise(latencies)
    status = daemon_status()
    print(json.dumps({
        'label': args.label, 'load': args.load, 'lost': lost, 'latency': summary,
        'snaptap_running': status['running'] if status else None,
        'daemon_callback_us': status['latency_us'] if status else None,
    }, indent=2))
    return 1 if args.gate and (lost or not passes_gate(summary)) else 0


if __name__ == '__main__':
    raise SystemExit(main())
