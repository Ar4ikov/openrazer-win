"""OpenRazer for Windows -- Razer device control without the kernel module.

The Linux original ships a kernel driver plus a D-Bus daemon.  On Windows the
same protocol runs entirely in user space: :mod:`openrazer_win.hid` talks to the
HID class driver through ctypes, :mod:`openrazer_win.protocol` builds the vendor
reports, and :mod:`openrazer_win.devices` carries the per-device quirks
transpiled from the upstream kernel sources.
"""
from ._version import UPSTREAM_VERSION, __version__  # noqa: F401

__all__ = ['__version__', 'UPSTREAM_VERSION']
