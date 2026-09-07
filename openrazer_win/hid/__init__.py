"""HID transport backends."""
from __future__ import annotations

import sys
from typing import Optional

from .base import HidBackend, HidDeviceInfo, HidError, HidHandle  # noqa: F401
from .fake import FakeHidBackend, FakeRazerDevice  # noqa: F401

__all__ = ['HidBackend', 'HidDeviceInfo', 'HidError', 'HidHandle',
           'FakeHidBackend', 'FakeRazerDevice', 'get_backend', 'is_supported_platform']


def is_supported_platform() -> bool:
    return sys.platform == 'win32'


def get_backend(name: Optional[str] = None):
    """Return a HID backend.

    ``name`` may be ``'win32'``, ``'fake'`` or ``None`` to pick automatically.
    """
    if name == 'fake':
        return FakeHidBackend()
    if name in (None, 'auto', 'win32'):
        if is_supported_platform():
            from .win32 import Win32HidBackend
            return Win32HidBackend()
        if name == 'win32':
            raise HidError('the win32 HID backend requires Windows')
        return FakeHidBackend()
    raise HidError('unknown HID backend: {0}'.format(name))
