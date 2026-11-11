"""Start-up should not wait for things that cannot happen.

A PC with no Bluetooth radio used to spend 6 s of its start-up listening to
nothing, a stale endpoint file made Studio wait out a refused connection, and
every hot-plug poll read the same USB strings again.  These pin the shortcuts.
"""
from __future__ import annotations

import asyncio
import os
import socket
import sys
import time
import types

import pytest

from openrazer_win.ble import transport as ble_transport
from openrazer_win.client import rpc
from openrazer_win.client.rpc import RpcClient, process_alive
from openrazer_win.core.manager import DeviceManager
from openrazer_win.core.persistence import Persistence
from openrazer_win.daemon.protocol import Endpoint
from openrazer_win.daemon.server import DaemonService
from openrazer_win.hid import win32
from openrazer_win.hid.base import HidDeviceInfo
from openrazer_win.hid.fake import FakeHidBackend, FakeRazerDevice
from openrazer_win.keys import KeyService, MacroStore
from openrazer_win.keys.inject import RecordingInjector

BLACKWIDOW_CHROMA = 0x0203
NO_SUCH_PROCESS = 0x7FFFFFF0


# -- the sweep stops when there is nothing to hear -----------------------------

FakeStatus = types.SimpleNamespace(CREATED=0, STARTED=1, ABORTED=3)


def install_winrt(monkeypatch, adapter, status_after_start):
    """Stand-ins for the WinRT modules the sweep imports."""
    watchers = []

    class Watcher:
        def __init__(self):
            self.status = FakeStatus.CREATED
            self.started = False
            self.stopped = False
            watchers.append(self)

        def add_received(self, handler):
            pass

        def start(self):
            self.started = True
            self.status = status_after_start

        def stop(self):
            self.stopped = True

    class Adapter:
        @staticmethod
        async def get_default_async():
            return adapter

    names = ['winrt', 'winrt.windows', 'winrt.windows.devices',
             'winrt.windows.devices.bluetooth',
             'winrt.windows.devices.bluetooth.advertisement']
    modules = {name: types.ModuleType(name) for name in names}
    modules['winrt.windows.devices.bluetooth'].BluetoothAdapter = Adapter
    advertisement = modules['winrt.windows.devices.bluetooth.advertisement']
    advertisement.BluetoothLEAdvertisementWatcher = Watcher
    advertisement.BluetoothLEAdvertisementWatcherStatus = FakeStatus
    advertisement.BluetoothLEScanningMode = types.SimpleNamespace(ACTIVE=1)
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return watchers


def test_a_sweep_with_no_radio_returns_at_once(monkeypatch):
    watchers = install_winrt(monkeypatch, adapter=None, status_after_start=FakeStatus.STARTED)
    started = time.monotonic()
    assert asyncio.run(ble_transport._scan(30.0, None)) == {}
    assert time.monotonic() - started < 1.0
    assert watchers == []     # no watcher was even made


@pytest.mark.parametrize('status, seconds, fast', [
    (FakeStatus.ABORTED, 30.0, True),       # no radio after all, or it went away
    (FakeStatus.STARTED, 0.3, False),       # a working radio is listened to
])
def test_a_sweep_listens_until_its_window_ends_or_its_watcher_aborts(
        monkeypatch, status, seconds, fast):
    watchers = install_winrt(monkeypatch, adapter=object(), status_after_start=status)
    started = time.monotonic()
    assert asyncio.run(ble_transport._scan(seconds, None)) == {}
    elapsed = time.monotonic() - started
    assert elapsed < 1.0 if fast else elapsed >= 0.29
    assert watchers[0].started and watchers[0].stopped


# -- enumeration reads each collection once ------------------------------------

def collection(path):
    return HidDeviceInfo(path=path, vendor_id=0x1532, product_id=0x0078, product='x', serial='s',
                         usage_page=0xFF00, usage=1)


@pytest.fixture
def backend():
    """A Win32 backend whose Windows calls are replaced, so it runs anywhere."""
    backend = object.__new__(win32.Win32HidBackend)
    backend._described = {}
    backend.paths = []
    backend.reads = []
    backend._interface_paths = lambda: iter(list(backend.paths))

    def describe(path):
        backend.reads.append(path)
        return collection(path)

    backend._describe = describe
    return backend


def test_a_collection_is_read_once_however_often_it_is_enumerated(backend):
    backend.paths = ['vid_1532&pid_0078&mi_00', 'vid_1532&pid_0078&mi_01']
    for _ in range(5):
        assert len(backend.enumerate(vendor_id=0x1532)) == 2
    assert len(backend.reads) == 2


def test_a_replugged_device_is_read_afresh(backend):
    path = 'vid_1532&pid_0078&mi_00'
    backend.paths = [path]
    backend.enumerate(vendor_id=0x1532)
    backend.paths = []
    assert backend.enumerate(vendor_id=0x1532) == []
    backend.paths = [path]
    backend.enumerate(vendor_id=0x1532)
    assert backend.reads == [path, path]


@pytest.mark.parametrize('first', [
    None,                                   # held by another program
    HidDeviceInfo(path='p', vendor_id=0x1532, product_id=0x0078,
                  product='x', serial='s'),  # no usage page: capabilities unread
])
def test_an_incomplete_read_is_tried_again_and_then_remembered(backend, first):
    path = 'vid_1532&pid_0078&mi_00'
    backend.paths = [path]
    answers = iter([first, collection(path)])
    backend._describe = lambda p: backend.reads.append(p) or next(answers)
    backend.enumerate(vendor_id=0x1532)
    assert backend.enumerate(vendor_id=0x1532)[0].usage_page == 0xFF00
    backend.enumerate(vendor_id=0x1532)
    assert len(backend.reads) == 2


# -- key handling reuses what the scan found -----------------------------------

class CountingBackend(FakeHidBackend):
    def __init__(self, devices):
        super().__init__(devices)
        self.enumerations = 0

    def enumerate(self, vendor_id=None, product_id=None):
        self.enumerations += 1
        return super().enumerate(vendor_id, product_id)


class LiveHook:
    running = True

    def start(self):
        return True

    def stop(self):
        pass


def blackwidow_with_keys():
    return FakeRazerDevice(BLACKWIDOW_CHROMA, 'Razer BlackWidow Chroma',
                           interface=2, keys=True)


def test_attaching_a_keyboard_does_not_enumerate_again(tmp_path):
    fake = blackwidow_with_keys()
    backend = CountingBackend([fake])
    keys = KeyService(backend, macros=MacroStore(str(tmp_path / 'm.json')),
                      injector=RecordingInjector(), hook=LiveHook())
    service = DaemonService(backend=backend, persistence=Persistence(str(tmp_path / 'p.json')),
                            enable_effects=False, keys=keys)
    service.start()
    try:
        device = service.manager.devices[0]
        assert keys.handles(device)
        # One for the scan; attaching used its result.
        assert backend.enumerations == 1
        # The serial still comes from the device itself.
        assert device.serial == fake.state.serial
    finally:
        service.stop()


def test_a_device_without_scan_results_is_enumerated_by_the_key_service(tmp_path):
    backend = CountingBackend([blackwidow_with_keys()])
    manager = DeviceManager(backend=backend, persistence=Persistence(str(tmp_path / 'p.json')))
    manager.scan()
    device = manager.devices[0]
    del device.hid_collections
    keys = KeyService(backend, macros=MacroStore(str(tmp_path / 'm.json')),
                      injector=RecordingInjector(), hook=LiveHook())
    before = backend.enumerations
    assert keys.attach(device)
    assert backend.enumerations == before + 1
    keys.stop()
    manager.close()


# -- a stale endpoint is given up on quickly -----------------------------------

def closed_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def test_this_process_is_alive_and_a_missing_one_is_not():
    assert process_alive(os.getpid())
    assert process_alive(0)          # no pid recorded: unknown, so alive
    assert not process_alive(NO_SUCH_PROCESS)


@pytest.mark.parametrize('host', ['127.0.0.1', '127.0.0.2', '::1', 'localhost'])
def test_a_dead_pid_on_loopback_is_given_up_on_without_connecting(monkeypatch, host):
    def refuse(*args, **kwargs):
        raise AssertionError('connected')

    monkeypatch.setattr(rpc.socket, 'create_connection', refuse)
    client = RpcClient(Endpoint(host, closed_port(), 't', NO_SUCH_PROCESS, ''))
    with pytest.raises(rpc.DaemonUnavailable, match='stale'):
        client.connect()


def test_a_live_daemon_is_reached_and_keeps_the_normal_timeout():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        client = RpcClient(Endpoint('127.0.0.1', listener.getsockname()[1], 't',
                                    os.getpid(), ''), timeout=12.0)
        client.connect()
        try:
            assert client._socket.gettimeout() == 12.0
        finally:
            client.close()


def test_a_non_loopback_endpoint_is_not_checked(monkeypatch):
    waits = []

    def record(address, timeout=None):
        waits.append(timeout)
        raise socket.timeout()

    monkeypatch.setattr(rpc.socket, 'create_connection', record)
    client = RpcClient(Endpoint('192.0.2.1', 1, 't', NO_SUCH_PROCESS, ''), timeout=15.0)
    with pytest.raises(rpc.DaemonUnavailable):
        client.connect()
    assert waits == [15.0]
