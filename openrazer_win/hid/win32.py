"""Windows HID backend built on ``setupapi.dll`` and ``hid.dll`` via ctypes.

This replaces the Linux kernel module: instead of ``usb_control_msg`` from
kernel space we issue ``HidD_SetFeature`` / ``HidD_GetFeature`` from user space,
which the Windows HID class driver turns into the same USB control transfers
(``SET_REPORT`` / ``GET_REPORT``, report type Feature, report id 0).

No third-party packages are required -- everything comes from the OS.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Optional

from .base import HidDeviceInfo, HidError

if sys.platform == 'win32':  # pragma: no branch - guarded import
    _setupapi = ctypes.WinDLL('setupapi', use_last_error=True)
    _hid = ctypes.WinDLL('hid', use_last_error=True)
    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
else:  # pragma: no cover - allows importing the module for docs/tests on CI
    _setupapi = _hid = _kernel32 = None

INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000

DIGCF_PRESENT = 0x00000002
DIGCF_DEVICEINTERFACE = 0x00000010

ERROR_INSUFFICIENT_BUFFER = 122
ERROR_NO_MORE_ITEMS = 259


class GUID(ctypes.Structure):
    _fields_ = [('Data1', ctypes.c_ulong),
                ('Data2', ctypes.c_ushort),
                ('Data3', ctypes.c_ushort),
                ('Data4', ctypes.c_ubyte * 8)]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD),
                ('InterfaceClassGuid', GUID),
                ('Flags', wintypes.DWORD),
                ('Reserved', ctypes.POINTER(ctypes.c_ulong))]


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD),
                ('ClassGuid', GUID),
                ('DevInst', wintypes.DWORD),
                ('Reserved', ctypes.POINTER(ctypes.c_ulong))]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [('Size', wintypes.ULONG),
                ('VendorID', ctypes.c_ushort),
                ('ProductID', ctypes.c_ushort),
                ('VersionNumber', ctypes.c_ushort)]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [('Usage', ctypes.c_ushort),
                ('UsagePage', ctypes.c_ushort),
                ('InputReportByteLength', ctypes.c_ushort),
                ('OutputReportByteLength', ctypes.c_ushort),
                ('FeatureReportByteLength', ctypes.c_ushort),
                ('Reserved', ctypes.c_ushort * 17),
                ('NumberLinkCollectionNodes', ctypes.c_ushort),
                ('NumberInputButtonCaps', ctypes.c_ushort),
                ('NumberInputValueCaps', ctypes.c_ushort),
                ('NumberInputDataIndices', ctypes.c_ushort),
                ('NumberOutputButtonCaps', ctypes.c_ushort),
                ('NumberOutputValueCaps', ctypes.c_ushort),
                ('NumberOutputDataIndices', ctypes.c_ushort),
                ('NumberFeatureButtonCaps', ctypes.c_ushort),
                ('NumberFeatureValueCaps', ctypes.c_ushort),
                ('NumberFeatureDataIndices', ctypes.c_ushort)]


def _configure_prototypes() -> None:
    _hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
    _hid.HidD_GetHidGuid.restype = None

    _hid.HidD_GetAttributes.argtypes = [wintypes.HANDLE, ctypes.POINTER(HIDD_ATTRIBUTES)]
    _hid.HidD_GetAttributes.restype = wintypes.BOOLEAN

    _hid.HidD_GetPreparsedData.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
    _hid.HidD_GetPreparsedData.restype = wintypes.BOOLEAN

    _hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
    _hid.HidD_FreePreparsedData.restype = wintypes.BOOLEAN

    _hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
    _hid.HidP_GetCaps.restype = ctypes.c_long

    for name in ('HidD_GetManufacturerString', 'HidD_GetProductString',
                 'HidD_GetSerialNumberString'):
        func = getattr(_hid, name)
        func.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_ulong]
        func.restype = wintypes.BOOLEAN

    _hid.HidD_SetFeature.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_ulong]
    _hid.HidD_SetFeature.restype = wintypes.BOOLEAN

    _hid.HidD_GetFeature.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_ulong]
    _hid.HidD_GetFeature.restype = wintypes.BOOLEAN

    _hid.HidD_SetNumInputBuffers.argtypes = [wintypes.HANDLE, ctypes.c_ulong]
    _hid.HidD_SetNumInputBuffers.restype = wintypes.BOOLEAN

    _setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
    _setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE

    _setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID), wintypes.DWORD,
        ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
    _setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL

    _setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(SP_DEVINFO_DATA)]
    _setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL

    _setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
    _setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel32.CreateFileW.restype = wintypes.HANDLE

    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL


if _hid is not None:
    _configure_prototypes()


def _read_string(func, handle) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    if not func(handle, buffer, ctypes.sizeof(buffer)):
        return ''
    return buffer.value


def _open_handle(path: str, writable: bool = True):
    """Open a HID collection, falling back to a zero-access handle.

    Windows gives the system exclusive read/write access to keyboard and mouse
    collections, but feature-report IOCTLs are ``FILE_ANY_ACCESS`` -- so a
    handle opened with no access rights still lets us talk to the device.
    """
    attempts = []
    if writable:
        attempts.append(GENERIC_READ | GENERIC_WRITE)
        attempts.append(GENERIC_WRITE)
    attempts.append(0)
    for access in attempts:
        handle = _kernel32.CreateFileW(
            path, access, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
            OPEN_EXISTING, 0, None)
        if handle and handle != INVALID_HANDLE_VALUE:
            return handle
    raise HidError('cannot open HID device {0}: WinError {1}'.format(
        path, ctypes.get_last_error()))


class Win32HidHandle:
    """An open HID collection."""

    def __init__(self, info: HidDeviceInfo, handle):
        self.info = info
        self._handle = handle

    def send_feature_report(self, data: bytes) -> None:
        """Send a feature report.  ``data[0]`` must be the report id."""
        buffer = ctypes.create_string_buffer(bytes(data), len(data))
        if not _hid.HidD_SetFeature(self._handle, buffer, len(data)):
            raise HidError('HidD_SetFeature failed: WinError {0}'.format(
                ctypes.get_last_error()))

    def get_feature_report(self, length: int, report_id: int = 0x00) -> bytes:
        buffer = ctypes.create_string_buffer(length)
        buffer[0] = bytes((report_id,))
        if not _hid.HidD_GetFeature(self._handle, buffer, length):
            raise HidError('HidD_GetFeature failed: WinError {0}'.format(
                ctypes.get_last_error()))
        return bytes(buffer.raw[:length])

    def close(self) -> None:
        if self._handle:
            _kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Win32HidBackend:
    """Enumerate and open HID collections through the Windows setup API."""

    name = 'win32'

    def __init__(self):
        if _hid is None:
            raise HidError('the win32 HID backend requires Windows')
        self._hid_guid = GUID()
        _hid.HidD_GetHidGuid(ctypes.byref(self._hid_guid))

    # -- enumeration -------------------------------------------------------
    def _interface_paths(self):
        dev_info = _setupapi.SetupDiGetClassDevsW(
            ctypes.byref(self._hid_guid), None, None,
            DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
        if dev_info == INVALID_HANDLE_VALUE:
            raise HidError('SetupDiGetClassDevs failed: WinError {0}'.format(
                ctypes.get_last_error()))
        try:
            index = 0
            while True:
                interface_data = SP_DEVICE_INTERFACE_DATA()
                interface_data.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
                if not _setupapi.SetupDiEnumDeviceInterfaces(
                        dev_info, None, ctypes.byref(self._hid_guid), index,
                        ctypes.byref(interface_data)):
                    break
                index += 1

                required = wintypes.DWORD(0)
                _setupapi.SetupDiGetDeviceInterfaceDetailW(
                    dev_info, ctypes.byref(interface_data), None, 0,
                    ctypes.byref(required), None)
                if required.value == 0:
                    continue

                buffer = ctypes.create_string_buffer(required.value)
                # SP_DEVICE_INTERFACE_DETAIL_DATA_W.cbSize is 8 on 64-bit and 6
                # on 32-bit -- it counts the DWORD plus one WCHAR, aligned.
                cb_size = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
                ctypes.memmove(buffer, ctypes.byref(wintypes.DWORD(cb_size)), 4)
                if not _setupapi.SetupDiGetDeviceInterfaceDetailW(
                        dev_info, ctypes.byref(interface_data), buffer,
                        required.value, None, None):
                    continue
                path = ctypes.wstring_at(ctypes.addressof(buffer) + 4)
                if path:
                    yield path
        finally:
            _setupapi.SetupDiDestroyDeviceInfoList(dev_info)

    def _describe(self, path: str) -> Optional[HidDeviceInfo]:
        try:
            handle = _open_handle(path, writable=False)
        except HidError:
            return None
        try:
            attributes = HIDD_ATTRIBUTES()
            attributes.Size = ctypes.sizeof(HIDD_ATTRIBUTES)
            if not _hid.HidD_GetAttributes(handle, ctypes.byref(attributes)):
                return None

            caps = HIDP_CAPS()
            preparsed = ctypes.c_void_p()
            if _hid.HidD_GetPreparsedData(handle, ctypes.byref(preparsed)):
                try:
                    _hid.HidP_GetCaps(preparsed, ctypes.byref(caps))
                finally:
                    _hid.HidD_FreePreparsedData(preparsed)

            return HidDeviceInfo(
                path=path,
                vendor_id=attributes.VendorID,
                product_id=attributes.ProductID,
                version=attributes.VersionNumber,
                interface=HidDeviceInfo.parse_interface(path),
                collection=HidDeviceInfo.parse_collection(path),
                usage_page=caps.UsagePage,
                usage=caps.Usage,
                feature_length=caps.FeatureReportByteLength,
                input_length=caps.InputReportByteLength,
                output_length=caps.OutputReportByteLength,
                manufacturer=_read_string(_hid.HidD_GetManufacturerString, handle),
                product=_read_string(_hid.HidD_GetProductString, handle),
                serial=_read_string(_hid.HidD_GetSerialNumberString, handle),
            )
        finally:
            _kernel32.CloseHandle(handle)

    def enumerate(self, vendor_id: Optional[int] = None,
                  product_id: Optional[int] = None) -> list:
        found = []
        for path in self._interface_paths():
            if vendor_id is not None:
                # Fast path: the device path embeds vid/pid, so skip the open.
                if 'vid_{0:04x}'.format(vendor_id).lower() not in path.lower():
                    continue
            if product_id is not None:
                if 'pid_{0:04x}'.format(product_id).lower() not in path.lower():
                    continue
            info = self._describe(path)
            if info is None:
                continue
            if vendor_id is not None and info.vendor_id != vendor_id:
                continue
            if product_id is not None and info.product_id != product_id:
                continue
            found.append(info)
        return found

    def open(self, info: HidDeviceInfo) -> Win32HidHandle:
        return Win32HidHandle(info, _open_handle(info.path, writable=True))
