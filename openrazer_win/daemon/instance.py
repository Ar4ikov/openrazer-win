"""One daemon per logon session.

Nothing used to stop a second daemon from starting.  At logon that is exactly
what happened: the autostart copy was still finding devices -- which takes
seconds, longer with a Bluetooth sweep -- so it was not answering yet, the
control panel concluded no daemon was running and started another, and the
two then fought over the same hardware, each re-asserting its own colour over
the other's.  A named mutex settles it before either touches a device.
"""
from __future__ import annotations

import logging
import sys
from typing import Optional

logger = logging.getLogger(__name__)

#: ``Local\`` scopes the name to this logon session, so each user signed in to
#: the machine still gets a daemon of their own.
MUTEX_NAME = 'Local\\openrazer-win-daemon'

ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    """Holds the session-wide daemon mutex for as long as it is acquired."""

    def __init__(self, name: str = MUTEX_NAME):
        self.name = name
        self._handle: Optional[int] = None

    def acquire(self) -> bool:
        """True if this process is now the session's daemon."""
        if sys.platform != 'win32':
            return True
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            # Unable to tell either way; refusing to start would be worse.
            logger.debug('CreateMutexW failed: %d', ctypes.get_last_error())
            return True
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        import ctypes

        ctypes.WinDLL('kernel32').CloseHandle(self._handle)
        self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
