"""A daemon started at logon, before its devices are ready.

At logon the daemon comes up before the Bluetooth stack has settled and
before anyone has switched their headset on.  Everything here is about not
giving up on what it finds then: a device that will not take its settings yet
is tried again, an unexpected error does not end the start-up, the radio is
listened to more eagerly for the first minutes, and a second daemon cannot
start beside the first.
"""
from __future__ import annotations

import sys
import time

import pytest

from openrazer_win.core.manager import DeviceManager
from openrazer_win.daemon import server as server_module
from openrazer_win.daemon.server import DaemonService
from openrazer_win.hid.fake import FakeHidBackend

from .conftest import ADVERT


class Flaky:
    """Stands in for a device that refuses to be set up a few times."""

    def __init__(self, failures: int, error: Exception):
        self.name = 'Flaky'
        self.serial = 'FLAKY'
        self.failures = failures
        self.error = error
        self.restored = 0

    def enter_driver_mode(self, keys_handled=False):
        pass

    def restore(self):
        if self.failures:
            self.failures -= 1
            raise self.error
        self.restored += 1

    def close(self):
        pass


@pytest.fixture
def service(persistence, monkeypatch):
    service = DaemonService(backend=FakeHidBackend(), persistence=persistence,
                            enable_effects=False)
    monkeypatch.setattr(server_module, 'RETRY_DELAYS', (0.0,))
    yield service
    service.stop()


def adopt(service, device, monkeypatch):
    """Make the manager report `device` as found."""
    monkeypatch.setattr(service.manager, 'scan', lambda force_bluetooth=False: [device])
    monkeypatch.setattr(type(service.manager), 'devices',
                        property(lambda self: [device]))


def test_a_device_that_is_not_ready_is_tried_again(service, monkeypatch):
    from openrazer_win.core.device import DeviceError

    device = Flaky(2, DeviceError('link not up yet'))
    adopt(service, device, monkeypatch)
    service.start()
    assert device.restored == 0
    assert service.rpc_version()['pending'] == ['Flaky']
    service.poll_hotplug()
    service.poll_hotplug()
    assert device.restored == 1
    assert service.rpc_version()['pending'] == []


def test_an_unexpected_error_does_not_end_start_up(service, monkeypatch):
    # What a hung Bluetooth call raises: not a DeviceError at all.
    device = Flaky(1, TimeoutError())
    adopt(service, device, monkeypatch)
    service.start()
    assert service.state == 'ready'
    service.poll_hotplug()
    assert device.restored == 1


def test_the_daemon_says_when_it_is_still_starting(service):
    assert service.rpc_version()['state'] == 'starting'
    service.start()
    assert service.rpc_version()['state'] == 'ready'


def test_a_rescan_sweeps_the_radio_even_when_it_is_not_due(persistence, fake_radio,
                                                           monkeypatch):
    import openrazer_win.ble as ble

    sweeps = []
    monkeypatch.setattr(ble, 'scan', lambda *a, **k: sweeps.append(1) or [ADVERT])
    service = DaemonService(backend=FakeHidBackend(), persistence=persistence,
                            enable_effects=False)
    service.manager.bluetooth_interval = 3600
    try:
        service.start()
        service.poll_hotplug()
        assert len(sweeps) == 1
        service.rpc_rescan()
        assert len(sweeps) == 2
    finally:
        service.stop()


def test_the_radio_is_swept_eagerly_just_after_start(persistence, fake_radio, monkeypatch):
    from openrazer_win.core import manager as manager_module

    manager = DeviceManager(backend=FakeHidBackend(), persistence=persistence,
                            bluetooth_interval=3600)
    now = time.monotonic()
    manager._bluetooth_swept_at = now - manager_module.BLUETOOTH_STARTUP_INTERVAL - 1
    assert manager._bluetooth_due(now)
    # Once the start-up window is over, the normal interval applies again.
    manager._created_at = now - manager_module.BLUETOOTH_STARTUP_WINDOW - 1
    assert not manager._bluetooth_due(now)
    manager.close()


def test_scanning_the_radio_does_not_hold_up_requests(persistence, fake_radio, monkeypatch):
    """A sweep listens for seconds; it must not do so holding the device lock."""
    import openrazer_win.ble as ble

    manager = DeviceManager(backend=FakeHidBackend(), persistence=persistence)
    held = []
    monkeypatch.setattr(ble, 'scan', lambda *a, **k: held.append(
        manager._lock.acquire(blocking=False)) or [ADVERT])
    manager.scan(force_bluetooth=True)
    assert held == [True]
    manager._lock.release()
    manager.close()


@pytest.mark.skipif(sys.platform != 'win32', reason='named mutexes are a Windows thing')
def test_only_one_daemon_per_session():
    from openrazer_win.daemon.instance import SingleInstance

    name = 'Local\\openrazer-win-test-{0}'.format(time.monotonic_ns())
    first = SingleInstance(name)
    second = SingleInstance(name)
    try:
        assert first.acquire()
        assert not second.acquire()
        first.release()
        assert second.acquire()
    finally:
        first.release()
        second.release()
