"""Looking beyond HID, so ``doctor`` can explain a device it cannot reach.

The HID enumeration only sees collections the port can talk to.  When a Razer
device is attached but missing from that list, the useful question is what
Windows *does* see it as -- a Bluetooth audio endpoint, a webcam, a composite
USB device with no control interface -- because that is usually the answer.
"""
from __future__ import annotations

import ctypes
import re
import sys
from dataclasses import dataclass
from typing import Optional

DIGCF_PRESENT = 0x00000002
DIGCF_ALLCLASSES = 0x00000004

SPDRP_DEVICEDESC = 0x00000000
SPDRP_FRIENDLYNAME = 0x0000000C
SPDRP_CLASS = 0x00000007

#: USB spells it ``VID_1532``; Bluetooth spells it ``VID&00011532``, where the
#: leading four digits say where the id came from and the vendor is the last
#: four. Match either width and keep the tail.
_VENDOR_RE = re.compile(r'VID[_&]([0-9A-Fa-f]{8}|[0-9A-Fa-f]{4})', re.IGNORECASE)
_PRODUCT_RE = re.compile(r'PID[_&]([0-9A-Fa-f]{8}|[0-9A-Fa-f]{4})', re.IGNORECASE)


def _identifier(match) -> Optional[int]:
    return int(match.group(1)[-4:], 16) if match else None


@dataclass(frozen=True)
class AttachedDevice:
    """One device node Windows currently has present."""

    instance_id: str
    description: str
    device_class: str
    vendor_id: Optional[int]
    product_id: Optional[int]

    @property
    def transport(self) -> str:
        head = self.instance_id.split('\\', 1)[0].upper()
        return {'USB': 'USB', 'HID': 'HID', 'BTHENUM': 'Bluetooth',
                'BTHLEDEVICE': 'Bluetooth LE', 'BTHHFENUM': 'Bluetooth',
                'SWD': 'software'}.get(head, head or 'unknown')

    def describe(self) -> str:
        return '{0:<11} {1:04x}:{2}  {3}'.format(
            self.transport,
            self.vendor_id or 0,
            '{0:04x}'.format(self.product_id) if self.product_id else '????',
            self.description or self.device_class or self.instance_id)


def _property(setupapi, dev_info, dev_info_data, prop: int) -> str:
    buffer = ctypes.create_unicode_buffer(512)
    size = ctypes.c_ulong(0)
    ok = setupapi.SetupDiGetDeviceRegistryPropertyW(
        dev_info, ctypes.byref(dev_info_data), prop, None,
        ctypes.byref(buffer), ctypes.sizeof(buffer), ctypes.byref(size))
    return buffer.value if ok else ''


def list_attached(vendor_id: Optional[int] = None) -> list:
    """Every present device node, optionally filtered by USB vendor id.

    Returns an empty list off Windows -- callers treat it as "nothing extra to
    say" rather than an error.
    """
    if sys.platform != 'win32':
        return []

    from ctypes import wintypes

    from .win32 import GUID, SP_DEVINFO_DATA

    setupapi = ctypes.WinDLL('setupapi', use_last_error=True)
    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
    setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
    setupapi.SetupDiEnumDeviceInfo.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(SP_DEVINFO_DATA)]
    setupapi.SetupDiEnumDeviceInfo.restype = wintypes.BOOL
    setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.LPWSTR,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    setupapi.SetupDiGetDeviceInstanceIdW.restype = wintypes.BOOL
    setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    setupapi.SetupDiGetDeviceRegistryPropertyW.restype = wintypes.BOOL
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]

    dev_info = setupapi.SetupDiGetClassDevsW(
        None, None, None, DIGCF_PRESENT | DIGCF_ALLCLASSES)
    if dev_info == ctypes.c_void_p(-1).value:
        return []

    found = []
    try:
        index = 0
        while True:
            data = SP_DEVINFO_DATA()
            data.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)
            if not setupapi.SetupDiEnumDeviceInfo(dev_info, index, ctypes.byref(data)):
                break
            index += 1

            buffer = ctypes.create_unicode_buffer(512)
            if not setupapi.SetupDiGetDeviceInstanceIdW(
                    dev_info, ctypes.byref(data), buffer,
                    ctypes.sizeof(buffer) // ctypes.sizeof(ctypes.c_wchar), None):
                continue
            instance_id = buffer.value

            vendor_value = _identifier(_VENDOR_RE.search(instance_id))
            if vendor_id is not None and vendor_value != vendor_id:
                continue

            found.append(AttachedDevice(
                instance_id=instance_id,
                description=(_property(setupapi, dev_info, data, SPDRP_FRIENDLYNAME)
                             or _property(setupapi, dev_info, data, SPDRP_DEVICEDESC)),
                device_class=_property(setupapi, dev_info, data, SPDRP_CLASS),
                vendor_id=vendor_value,
                product_id=_identifier(_PRODUCT_RE.search(instance_id)),
            ))
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(dev_info)
    return found


def summarise(devices: list) -> list:
    """Collapse the node list to one line per physical device.

    Windows exposes a headset as a dozen nodes -- audio endpoints, transports,
    a composite parent.  Grouping by product id and transport keeps the report
    readable.
    """
    groups: dict = {}
    for device in devices:
        key = (device.product_id, device.transport)
        groups.setdefault(key, []).append(device)

    lines = []
    for (product_id, transport), members in sorted(
            groups.items(), key=lambda item: (item[0][1], item[0][0] or 0)):
        # The shortest description is the plain product name; the longer ones
        # are transport wrappers around it ("AVRCP transport for ...",
        # "Standard Serial over Bluetooth link (COM4)").
        named = sorted((m.description for m in members if m.description), key=len)
        lines.append({
            'product_id': product_id,
            'transport': transport,
            'name': named[0] if named else members[0].device_class,
            'classes': sorted({m.device_class for m in members if m.device_class}),
            'nodes': len(members),
        })
    return lines
