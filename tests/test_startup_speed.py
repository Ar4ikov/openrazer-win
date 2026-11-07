"""Start-up should not wait for things that cannot happen.

A PC with no Bluetooth radio used to spend 6 s of its start-up listening to
nothing, a stale endpoint file made Studio wait out a refused connection, and
every hot-plug poll read the same USB strings again.  These pin the shortcuts.
"""
from __future__ import annotations

import asyncio
import enum
import os
import socket
import sys
import threading
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

class FakeStatus(enum.Enum):
    CREATED = 0
    STARTED = 1
    STOPPED = 2
    ABORTED = 3


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


def test_a_sweep_whose_watcher_aborts_returns_at_once(monkeypatch):
    watchers = install_winrt(monkeypatch, adapter=object(),
                             status_after_start=FakeStatus.ABORTED)
    started = time.monotonic()
    assert asyncio.run(ble_transport._scan(30.0, None)) == {}
    assert time.monotonic() - started < 1.0
    assert watchers[0].started and watchers[0].stopped


def test_a_sweep_with_a_working_radio_still_listens_for_its_window(monkeypatch):
    watchers = install_winrt(monkeypatch, adapter=object(),
                             status_after_start=FakeStatus.STARTED)
    started = time.monotonic()
    asyncio.run(ble_transport._scan(0.3, None))
    assert time.monotonic() - started >= 0.29
    assert watchers[0].stopped


# -- enumeration reads each collection once ------------------------------------

def collection(path, vid=0x1532, pid=0x0078):
    return HidDeviceInfo(path=path, vendor_id=vid, product_id=pid, product='x', serial='s',
                         usage_page=0xFF00, usage=1)


@pytest.fixture
def backend():
    """A Win32 backend whose Windows calls are replaced, so it runs anywhere."""
    backend = object.__new__(win32.Win32HidBackend)
    backend._described = {}
    backend._described_lock = threading.Lock()
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


def test_a_collection_that_could_not_be_read_is_tried_again(backend):
    path = 'vid_1532&pid_0078&mi_00'
    backend.paths = [path]
    answers = iter([None, collection(path)])
    backend._describe = lambda p: backend.reads.append(p) or next(answers)
    assert backend.enumerate(vendor_id=0x1532) == []
    assert len(backend.enumerate(vendor_id=0x1532)) == 1
    assert len(backend.reads) == 2


def test_a_half_described_collection_is_not_remembered(backend):
    """No usage page means the capabilities could not be read; try again later."""
    path = 'vid_1532&pid_0078&mi_00'
    backend.paths = [path]
    partial = HidDeviceInfo(path=path, vendor_id=0x1532, product_id=0x0078,
                            product='x', serial='s')
    answers = iter([partial, collection(path)])
    backend._describe = lambda p: backend.reads.append(p) or next(answers)
    assert len(backend.enumerate(vendor_id=0x1532)) == 1
    assert backend._described == {}
    assert backend.enumerate(vendor_id=0x1532)[0].usage_page == 0xFF00
    backend.enumerate(vendor_id=0x1532)
    assert len(backend.reads) == 2          # now complete, so remembered


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


def test_a_dead_pid_in_the_endpoint_is_given_up_on_without_connecting(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError('connected')

    monkeypatch.setattr(rpc.socket, 'create_connection', refuse)
    client = RpcClient(Endpoint('127.0.0.1', closed_port(), 't', NO_SUCH_PROCESS, ''))
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


def test_a_slow_connect_to_a_live_pid_gets_the_full_timeout(monkeypatch):
    """The short wait must never turn a busy daemon into a missing one."""
    waits = []

    def slow(address, timeout=None):
        waits.append(timeout)
        if timeout == rpc.LOOPBACK_CONNECT_TIMEOUT:
            raise socket.timeout()
        return types.SimpleNamespace(settimeout=lambda t: None,
                                     makefile=lambda mode: None)

    monkeypatch.setattr(rpc.socket, 'create_connection', slow)
    client = RpcClient(Endpoint('127.0.0.1', 1, 't', os.getpid(), ''), timeout=15.0)
    client.connect()
    assert waits == [rpc.LOOPBACK_CONNECT_TIMEOUT, 15.0]


def test_a_non_loopback_endpoint_keeps_the_normal_timeout(monkeypatch):
    waits = []

    def record(address, timeout=None):
        waits.append(timeout)
        raise socket.timeout()

    monkeypatch.setattr(rpc.socket, 'create_connection', record)
    client = RpcClient(Endpoint('192.0.2.1', 1, 't', NO_SUCH_PROCESS, ''), timeout=15.0)
    with pytest.raises(rpc.DaemonUnavailable):
        client.connect()
    assert waits == [15.0]


def test_no_timeout_at_all_is_accepted(monkeypatch):
    waits = []

    def record(address, timeout=None):
        waits.append(timeout)
        return types.SimpleNamespace(settimeout=lambda t: None,
                                     makefile=lambda mode: None)

    monkeypatch.setattr(rpc.socket, 'create_connection', record)
    RpcClient(Endpoint('127.0.0.1', 1, 't', os.getpid(), ''), timeout=None).connect()
    assert waits == [None]


def test_any_127_address_is_loopback(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError('connected')

    monkeypatch.setattr(rpc.socket, 'create_connection', refuse)
    client = RpcClient(Endpoint('127.0.0.2', 1, 't', NO_SUCH_PROCESS, ''))
    with pytest.raises(rpc.DaemonUnavailable, match='stale'):
        client.connect()


def test_a_short_timeout_with_a_dead_pid_is_final(monkeypatch):
    waits = []

    def slow(address, timeout=None):
        waits.append(timeout)
        raise socket.timeout()

    monkeypatch.setattr(rpc.socket, 'create_connection', slow)
    # The pid is checked up front, then once more after the timeout: make it
    # alive for the first look and gone for the second.
    answers = iter([True, False])
    monkeypatch.setattr(rpc, 'process_alive', lambda pid: next(answers))
    client = RpcClient(Endpoint('127.0.0.1', 1, 't', 4242, ''), timeout=15.0)
    with pytest.raises(rpc.DaemonUnavailable):
        client.connect()
    assert waits == [rpc.LOOPBACK_CONNECT_TIMEOUT]


def test_a_refused_connection_is_not_retried_with_the_long_wait(monkeypatch):
    waits = []

    def refuse(address, timeout=None):
        waits.append(timeout)
        raise ConnectionRefusedError()

    monkeypatch.setattr(rpc.socket, 'create_connection', refuse)
    client = RpcClient(Endpoint('127.0.0.1', 1, 't', os.getpid(), ''), timeout=15.0)
    with pytest.raises(rpc.DaemonUnavailable):
        client.connect()
    assert waits == [rpc.LOOPBACK_CONNECT_TIMEOUT]
