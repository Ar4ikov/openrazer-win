"""Running this program again as administrator, behind Windows' own UAC prompt."""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from typing import Optional

SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_NO_CONSOLE = 0x00008000
SW_HIDE = 0
INFINITE = 0xFFFFFFFF
#: ShellExecuteEx's answer when the person said no at the prompt.
ERROR_CANCELLED = 1223


class Declined(Exception):
    """The UAC prompt was answered no."""


def own_command() -> list:
    """How this program was started: the frozen exe, or Python with the script."""
    if getattr(sys, 'frozen', False):
        return [sys.executable]
    script = sys.argv[0]
    if script.lower().endswith('.exe'):
        return [script]
    return [sys.executable, script]


def _shell_execute_info():
    # Built here, not at import: ctypes.wintypes is Windows' business.
    from ctypes import wintypes

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('fMask', ctypes.c_ulong), ('hwnd', wintypes.HWND),
                    ('lpVerb', wintypes.LPCWSTR), ('lpFile', wintypes.LPCWSTR),
                    ('lpParameters', wintypes.LPCWSTR), ('lpDirectory', wintypes.LPCWSTR),
                    ('nShow', ctypes.c_int), ('hInstApp', wintypes.HINSTANCE),
                    ('lpIDList', ctypes.c_void_p), ('lpClass', wintypes.LPCWSTR),
                    ('hkeyClass', wintypes.HKEY), ('dwHotKey', wintypes.DWORD),
                    ('hIconOrMonitor', wintypes.HANDLE), ('hProcess', wintypes.HANDLE)]

    return ShellExecuteInfo(), wintypes.DWORD


def run_elevated(args: list, command: Optional[list] = None) -> int:
    """Run this program with `args` as administrator, wait, and return its exit code."""
    command = (command or own_command()) + list(args)
    info, dword = _shell_execute_info()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NO_CONSOLE
    info.lpVerb = 'runas'
    info.lpFile = command[0]
    info.lpParameters = subprocess.list2cmdline(command[1:])
    info.lpDirectory = os.getcwd()
    info.nShow = SW_HIDE
    shell32 = ctypes.WinDLL('shell32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.c_void_p]
    shell32.ShellExecuteExW.restype = ctypes.c_int
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == ERROR_CANCELLED:
            raise Declined('the administrator prompt was declined')
        raise ctypes.WinError(error)
    try:
        kernel32.WaitForSingleObject(info.hProcess, INFINITE)
        code = dword()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)
