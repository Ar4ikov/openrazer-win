"""The libusb-win32 filter driver, for the Razer mice Windows' HID driver cannot set up.

The DeathAdder 3.5G and its Black edition predate the 90-byte report.  Upstream
sets them with a USB ``SET_REPORT`` of report type 0 -- a request no report of
theirs declares, which Windows' HID driver will not send for anyone (issue #29).

libusb-win32's filter driver sits on top of one device's own driver: the mouse
stays a mouse, and a program can send it control requests through
``libusb0.dll``.  Nothing here installs it on its own.  ``openrazer-win driver
install`` (Studio offers it) does, with administrator rights:

* downloads libusb-win32 1.4.0.2 from its GitHub release and checks the archive
  and every file used from it against the hashes below;
* copies ``libusb0.sys`` and ``libusb0.dll`` into ``System32``, leaving any
  copies already there alone -- another program may have put them there;
* runs the release's ``install-filter.exe`` for these product ids only, which
  adds the filter to the mouse and restarts it.

``openrazer-win driver remove`` takes the filter off the mouse again.
"""
from __future__ import annotations

import ctypes
import hashlib
import io
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

VERSION = '1.4.0.2'
ZIP_NAME = 'libusb-win32-bin-{0}.zip'.format(VERSION)
ZIP_URL = ('https://github.com/mcuee/libusb-win32/releases/download/'
           'release_{0}/{1}'.format(VERSION, ZIP_NAME))
ZIP_SHA256 = '00004c92cdb99be36e17fb2377165eb97e63b48ba895bfc04a642ea9c3e26d94'

#: Every file used from the archive, by architecture, with its SHA-256.
FILES = {
    'amd64': {
        'libusb0.sys': 'cda0070fe4f7c5de5d3bb06fa6d7e29e6f6740c0f2fa319fb5c1af235d78d616',
        'libusb0.dll': '0cd18719d5701e9cc642fc93a83f13830931d6f29f664fedbdf212bcb7c349bd',
        'install-filter.exe': '95f4f575639b9a4bf4f8a2f2ee226720cf87fb962aa33b6327ef0c704b63a3c4',
    },
    'arm64': {
        'libusb0.sys': 'd9d279b943aa71bd4933b9791afa53c4d5751829d4e78c12b3a4632839a9e60c',
        'libusb0.dll': '8b85274c64261d7affbc897a15ccfe78ee35b682a4595f870980e17667aac7d1',
        'install-filter.exe': '56dcc6d31efa89dcbcf071525689a81a22c8fbe176db9d9f83a884d32fecd9ef',
    },
}

#: Where the installer keeps the release's files, for taking the filter off later.
def _home() -> str:
    base = os.environ.get('ProgramData') or os.environ.get('ALLUSERSPROFILE') or tempfile.gettempdir()
    return os.path.join(base, 'openrazer-win', 'libusb-win32-' + VERSION)


class DriverError(Exception):
    """The filter driver could not be installed, removed or used."""


def architecture() -> str:
    machine = (os.environ.get('PROCESSOR_ARCHITEW6432') or os.environ.get('PROCESSOR_ARCHITECTURE')
               or platform.machine()).lower()
    if machine in ('arm64', 'aarch64'):
        return 'arm64'
    if machine in ('amd64', 'x86_64', 'x64'):
        return 'amd64'
    raise DriverError('libusb-win32 is set up here for 64-bit Windows only, not {0}'.format(machine))


def hardware_id(pid: int) -> str:
    return 'USB\\VID_1532&PID_{0:04X}'.format(pid)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _system_dir() -> str:
    return os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32')


def is_admin() -> bool:
    if sys.platform != 'win32':
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


# -- getting the release -----------------------------------------------------
def download(fetch=None) -> bytes:
    """The release archive, checked against `ZIP_SHA256`."""
    if fetch is None:
        def fetch(url):
            request = urllib.request.Request(url, headers={'User-Agent': 'openrazer-win'})
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed https URL
                return response.read()
    data = fetch(ZIP_URL)
    if _sha256(data) != ZIP_SHA256:
        raise DriverError('the downloaded {0} is not the one this version checks for'.format(ZIP_NAME))
    return data


def unpack(archive: bytes, target: str, arch: str) -> dict:
    """Write the files this needs from `archive` into `target`, each checked.  Name -> path."""
    os.makedirs(target, exist_ok=True)
    paths = {}
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        for name, digest in FILES[arch].items():
            member = 'libusb-win32-bin-{0}/bin/{1}/{2}'.format(VERSION, arch, name)
            data = bundle.read(member)
            if _sha256(data) != digest:
                raise DriverError('{0} in the archive is not the one this version checks for'.format(name))
            path = os.path.join(target, name)
            with open(path, 'wb') as handle:
                handle.write(data)
            paths[name] = path
    return paths


def _place(source: str, destination: str, digest: str) -> bool:
    """Copy `source` to `destination` unless a file is there already.  True if copied."""
    if os.path.exists(destination):
        with open(destination, 'rb') as handle:
            same = _sha256(handle.read()) == digest
        logger.info('%s is already there%s; left as it is', destination,
                    '' if same else ' (another version, put there by another program)')
        return False
    shutil.copyfile(source, destination)
    return True


def _check(path: str, digest: str) -> None:
    with open(path, 'rb') as handle:
        if _sha256(handle.read()) != digest:
            raise DriverError('{0} changed after it was checked'.format(path))


def _run_filter_tool(tool: str, action: str, pid: int, run=None) -> None:
    run = run or subprocess.run
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    result = run([tool, action, '--device=' + hardware_id(pid)], capture_output=True,
                 text=True, creationflags=flags, timeout=120)
    if result.returncode != 0:
        said = ((result.stdout or '') + (result.stderr or '')).strip()
        raise DriverError('install-filter {0} failed ({1}){2}'.format(
            action, result.returncode, ': ' + said[-400:] if said else ''))


def install(pids: Iterable[int], fetch=None, run=None, home: Optional[str] = None,
            system_dir: Optional[str] = None) -> list:
    """Install the filter on `pids`.  Needs administrator rights.  Returns what was done."""
    pids = sorted(set(pids))
    if not pids:
        raise DriverError('no device to install the filter on')
    arch = architecture()
    home = home or _home()
    system_dir = system_dir or _system_dir()
    done = []
    paths = unpack(download(fetch), home, arch)
    digests = FILES[arch]
    # Checked again just before use: the folder outlives this call.
    for name, path in paths.items():
        _check(path, digests[name])
    if _place(paths['libusb0.sys'], os.path.join(system_dir, 'drivers', 'libusb0.sys'),
              digests['libusb0.sys']):
        done.append('copied libusb0.sys')
    if _place(paths['libusb0.dll'], os.path.join(system_dir, 'libusb0.dll'), digests['libusb0.dll']):
        done.append('copied libusb0.dll')
    for pid in pids:
        _run_filter_tool(paths['install-filter.exe'], 'install', pid, run)
        done.append('filter on ' + hardware_id(pid))
    _record(home, sorted(set(recorded_pids(home)) | set(pids)))
    return done


def _record(home: str, pids: list) -> None:
    with open(os.path.join(home, 'filtered.json'), 'w', encoding='utf-8') as handle:
        json.dump({'pids': pids}, handle)


def recorded_pids(home: Optional[str] = None) -> list:
    """The product ids `install` has put the filter on, so it can come off unplugged ones too."""
    try:
        with open(os.path.join(home or _home(), 'filtered.json'), encoding='utf-8') as handle:
            return sorted(int(pid) for pid in json.load(handle).get('pids', []))
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def installed_here(home: Optional[str] = None) -> bool:
    """Whether `install` has run on this machine (its files are still kept)."""
    return os.path.exists(os.path.join(home or _home(), 'install-filter.exe'))


def remove(pids: Iterable[int], run=None, home: Optional[str] = None, purge: bool = False) -> list:
    """Take the filter off `pids`.  Needs administrator rights.

    ``libusb0.sys`` and ``libusb0.dll`` stay in System32: another program may
    use them, and with no filter left they do nothing.  `purge` also deletes
    the release's files kept here, as Studio's uninstall does.
    """
    home = home or _home()
    pids = sorted(set(pids))
    done = []
    if pids:
        arch = architecture()
        tool = os.path.join(home, 'install-filter.exe')
        if not os.path.exists(tool):
            raise DriverError('{0} is missing; reinstall the driver first, or remove the filter '
                              'with libusb-win32\'s own tools'.format(tool))
        _check(tool, FILES[arch]['install-filter.exe'])
        for pid in pids:
            _run_filter_tool(tool, 'uninstall', pid, run)
            done.append('filter off ' + hardware_id(pid))
        left = [pid for pid in recorded_pids(home) if pid not in pids]
        if os.path.isdir(home):
            _record(home, left)
    if purge and os.path.isdir(home):
        shutil.rmtree(home, ignore_errors=True)
        done.append('deleted ' + home)
    return done


# -- talking to a filtered device ----------------------------------------------
class _DeviceDescriptor(ctypes.Structure):
    _pack_ = 1
    _fields_ = [('bLength', ctypes.c_ubyte), ('bDescriptorType', ctypes.c_ubyte),
                ('bcdUSB', ctypes.c_ushort), ('bDeviceClass', ctypes.c_ubyte),
                ('bDeviceSubClass', ctypes.c_ubyte), ('bDeviceProtocol', ctypes.c_ubyte),
                ('bMaxPacketSize0', ctypes.c_ubyte), ('idVendor', ctypes.c_ushort),
                ('idProduct', ctypes.c_ushort), ('bcdDevice', ctypes.c_ushort),
                ('iManufacturer', ctypes.c_ubyte), ('iProduct', ctypes.c_ubyte),
                ('iSerialNumber', ctypes.c_ubyte), ('bNumConfigurations', ctypes.c_ubyte)]


class _Device(ctypes.Structure):
    _pack_ = 1


class _Bus(ctypes.Structure):
    _pack_ = 1


_PATH_MAX = 512
_Device._fields_ = [('next', ctypes.POINTER(_Device)), ('prev', ctypes.POINTER(_Device)),
                    ('filename', ctypes.c_char * _PATH_MAX), ('bus', ctypes.POINTER(_Bus)),
                    ('descriptor', _DeviceDescriptor), ('config', ctypes.c_void_p),
                    ('dev', ctypes.c_void_p), ('devnum', ctypes.c_ubyte),
                    ('num_children', ctypes.c_ubyte), ('children', ctypes.c_void_p)]
_Bus._fields_ = [('next', ctypes.POINTER(_Bus)), ('prev', ctypes.POINTER(_Bus)),
                 ('dirname', ctypes.c_char * _PATH_MAX), ('devices', ctypes.POINTER(_Device)),
                 ('location', ctypes.c_ulong), ('root_dev', ctypes.POINTER(_Device))]


class Libusb0:
    """``libusb0.dll``, as installed with the filter driver."""

    def __init__(self, path: Optional[str] = None):
        if sys.platform != 'win32' and path is None:
            raise DriverError('libusb0.dll exists on Windows only')
        try:
            self.dll = ctypes.CDLL(path or os.path.join(_system_dir(), 'libusb0.dll'))
        except OSError as error:
            raise DriverError('libusb0.dll is not installed: {0}'.format(error)) from error
        dll = self.dll
        dll.usb_init.restype = None
        dll.usb_find_busses.restype = ctypes.c_int
        dll.usb_find_devices.restype = ctypes.c_int
        dll.usb_get_busses.restype = ctypes.POINTER(_Bus)
        dll.usb_open.argtypes = [ctypes.POINTER(_Device)]
        dll.usb_open.restype = ctypes.c_void_p
        dll.usb_close.argtypes = [ctypes.c_void_p]
        dll.usb_close.restype = ctypes.c_int
        dll.usb_control_msg.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        dll.usb_control_msg.restype = ctypes.c_int
        dll.usb_strerror.restype = ctypes.c_char_p
        dll.usb_init()

    def _find(self, vid: int, pid: int):
        self.dll.usb_find_busses()
        self.dll.usb_find_devices()
        bus = self.dll.usb_get_busses()
        while bus:
            device = bus.contents.devices
            while device:
                descriptor = device.contents.descriptor
                if descriptor.idVendor == vid and descriptor.idProduct == pid:
                    return device
                device = device.contents.next
            bus = bus.contents.next
        return None

    def present(self, vid: int, pid: int) -> bool:
        """Whether the filter has the device: libusb-win32 lists only devices it drives."""
        return self._find(vid, pid) is not None

    def control_out(self, vid: int, pid: int, request_type: int, request: int, value: int,
                    index: int, data: bytes, timeout_ms: int = 1000) -> int:
        device = self._find(vid, pid)
        if device is None:
            raise DriverError('{0:04x}:{1:04x} is not reachable through libusb-win32'.format(vid, pid))
        handle = self.dll.usb_open(device)
        if not handle:
            raise DriverError('libusb-win32 could not open {0:04x}:{1:04x}'.format(vid, pid))
        try:
            buffer = ctypes.create_string_buffer(bytes(data), len(data))
            sent = self.dll.usb_control_msg(handle, request_type, request, value, index,
                                            buffer, len(data), timeout_ms)
            if sent < 0:
                said = self.dll.usb_strerror()
                raise DriverError('control request failed: {0}'.format(
                    said.decode('ascii', 'replace') if said else sent))
            return sent
        finally:
            self.dll.usb_close(handle)


def open_library() -> Optional[Libusb0]:
    """The installed ``libusb0.dll``, or ``None`` when the driver is not installed."""
    try:
        return Libusb0()
    except DriverError:
        return None
