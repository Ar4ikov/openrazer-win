"""Bluetooth LE support, for Razer devices with no USB data mode.

Optional: needs ``pip install "openrazer-win[ble]"``. Everything reachable
over USB HID works without it.
"""
from .transport import (  # noqa: F401
    Advertiser, BleTransport, BleUnavailable, is_available, require_available, scan,
)

__all__ = ['Advertiser', 'BleTransport', 'BleUnavailable', 'is_available',
           'require_available', 'scan']
