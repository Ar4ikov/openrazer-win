"""How loud the PC is, from the meter Windows keeps on its sound output.

The same number the volume mixer draws its bars from: whatever is playing, in
any program.  Reading it needs no capture stream and no permission, and asks
nothing of the audio driver, so it is reached through plain COM calls rather
than a dependency.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import time
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

#: The sound output can change under a running effect, so it is looked up again
#: this often.  Asking is cheap, and there is no need to hear about it sooner.
REOPEN_SECONDS = 3.0

_CLSID_ENUMERATOR = uuid.UUID('BCDE0395-E52F-467C-8E3D-C4579291692E')
_IID_ENUMERATOR = uuid.UUID('A95664D2-9614-4F35-A746-DE8DB63617E6')
_IID_METER = uuid.UUID('C02216F6-8C67-4B5B-9D00-D008E73E0064')

_COINIT_MULTITHREADED = 0
_CLSCTX_ALL = 0x17
_RENDER = 0
_MULTIMEDIA = 1

# Where each method sits in its interface's table.
_RELEASE = 2
_GET_DEFAULT_AUDIO_ENDPOINT = 4
_ACTIVATE = 3
_GET_PEAK_VALUE = 3

HRESULT = ctypes.c_long


class SilentMeter:
    """What there is to hear where there is no Windows to ask."""

    def peak(self) -> float:
        return 0.0

    def close(self) -> None:
        pass


def _guid(value: uuid.UUID):
    return ctypes.create_string_buffer(value.bytes_le, 16)


def _call(interface, index: int, restype, argtypes: tuple, *args):
    """Call method `index` of a COM interface."""
    table = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
    method = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(table.contents[index])
    return method(interface, *args)


def _release(interface) -> None:
    if interface:
        _call(interface, _RELEASE, ctypes.c_ulong, ())


class WindowsMeter:
    """The peak meter on the default sound output.  One thread only."""

    def __init__(self):
        self._ole32 = ctypes.WinDLL('ole32')
        self._ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self._ole32.CoInitializeEx.restype = HRESULT
        self._ole32.CoCreateInstance.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p)]
        self._ole32.CoCreateInstance.restype = HRESULT
        self._ole32.CoUninitialize.argtypes = []
        self._ole32.CoUninitialize.restype = None
        self._initialised = self._ole32.CoInitializeEx(None, _COINIT_MULTITHREADED) >= 0
        self._meter: Optional[ctypes.c_void_p] = None
        self._opened_at: Optional[float] = None

    def _open(self) -> Optional[ctypes.c_void_p]:
        """The meter on whichever output is the default now."""
        enumerator = ctypes.c_void_p()
        if self._ole32.CoCreateInstance(_guid(_CLSID_ENUMERATOR), None, _CLSCTX_ALL,
                                        _guid(_IID_ENUMERATOR), ctypes.byref(enumerator)) < 0:
            return None
        endpoint = ctypes.c_void_p()
        meter = ctypes.c_void_p()
        try:
            if _call(enumerator, _GET_DEFAULT_AUDIO_ENDPOINT, HRESULT,
                     (ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)),
                     _RENDER, _MULTIMEDIA, ctypes.byref(endpoint)) < 0:
                return None     # no sound output at all
            if _call(endpoint, _ACTIVATE, HRESULT,
                     (ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
                      ctypes.POINTER(ctypes.c_void_p)),
                     _guid(_IID_METER), _CLSCTX_ALL, None, ctypes.byref(meter)) < 0:
                return None
            return meter
        finally:
            _release(endpoint)
            _release(enumerator)

    def _reopen(self) -> None:
        _release(self._meter)
        self._meter = self._open()
        self._opened_at = time.monotonic()

    def peak(self) -> float:
        """The loudest sample of the last moment, from 0 to 1."""
        if self._opened_at is None or time.monotonic() - self._opened_at >= REOPEN_SECONDS:
            self._reopen()
        if not self._meter:
            return 0.0
        value = ctypes.c_float(0.0)
        if _call(self._meter, _GET_PEAK_VALUE, HRESULT, (ctypes.POINTER(ctypes.c_float),),
                 ctypes.byref(value)) < 0:
            # The output went away.  The next look finds out what replaced it.
            _release(self._meter)
            self._meter = None
            return 0.0
        return float(value.value)

    def close(self) -> None:
        _release(self._meter)
        self._meter = None
        if self._initialised:
            self._ole32.CoUninitialize()
            self._initialised = False


def open_meter():
    """A meter for this thread: silence, rather than an error, if there is none."""
    if sys.platform != 'win32':
        return SilentMeter()
    try:
        return WindowsMeter()
    except OSError:
        logger.debug('no audio meter', exc_info=True)
        return SilentMeter()
