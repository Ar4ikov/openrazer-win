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

from openrazer_win.hid.fake import FakeRazerDevice

from .conftest import ADVERT, MOUSE_PID


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
    monkeypatch.setattr(service.manager, 'scan',
                        lambda force_bluetooth=False, sweep_bluetooth=True: [device])
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
        # Start-up looks at USB only; the radio is first swept by a poll.
        assert sweeps == []
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


def mouse_backend():
    return FakeHidBackend([FakeRazerDevice(MOUSE_PID, 'Razer Viper', interface=0)])


def test_a_usb_device_is_ready_before_any_sweep_has_run(persistence, fake_radio, monkeypatch):
    import openrazer_win.ble as ble

    sweeps = []
    monkeypatch.setattr(ble, 'scan', lambda *a, **k: sweeps.append(1) or [ADVERT])
    service = DaemonService(backend=mouse_backend(), persistence=persistence,
                            enable_effects=False)
    try:
        service.start()
        assert service.state == 'ready'
        assert sweeps == []
        assert [device.pid for device in service.manager.devices] == [MOUSE_PID]
    finally:
        service.stop()


def test_a_bluetooth_device_is_found_on_a_later_poll_and_the_usb_one_is_kept(
        persistence, fake_radio, monkeypatch):
    service = DaemonService(backend=mouse_backend(), persistence=persistence,
                            enable_effects=False)
    try:
        service.start()
        assert len(service.manager.devices) == 1
        service.poll_hotplug()
        keys = [device.pid for device in service.manager.devices]
        assert len(keys) == 2 and MOUSE_PID in keys
        assert service.state == 'ready'
    finally:
        service.stop()


def test_the_first_sweep_runs_at_once_and_clears_the_pending_flag(
        persistence, fake_radio, monkeypatch):
    import openrazer_win.ble as ble

    sweeps = []
    monkeypatch.setattr(ble, 'scan', lambda *a, **k: sweeps.append(1) or [ADVERT])
    service = DaemonService(backend=mouse_backend(), persistence=persistence,
                            enable_effects=False)
    try:
        service.start()
        # Ready, but the radio has not been heard yet: Studio must not say
        # "no devices" for a Bluetooth-only PC.
        assert service.state == 'ready'
        assert service.rpc_version()['first_sweep_pending'] is True
        service.first_sweep()
        assert sweeps == [1]
        assert len(service.manager.devices) == 2
        assert service.rpc_version()['first_sweep_pending'] is False
    finally:
        service.stop()


def test_the_pending_flag_is_cleared_even_when_the_first_sweep_fails(persistence):
    service = DaemonService(backend=mouse_backend(), persistence=persistence,
                            enable_effects=False)

    def broken(*args, **kwargs):
        raise OSError('radio went away')

    service.poll_hotplug = broken
    with pytest.raises(OSError):
        service.first_sweep()
    assert service.first_sweep_pending is False


def test_a_usb_only_scan_does_not_drop_a_bluetooth_device(persistence, fake_radio):
    manager = DeviceManager(backend=FakeHidBackend(), persistence=persistence,
                            bluetooth_interval=3600)
    assert len(manager.scan()) == 1
    # Not swept, so not heard: that is not the headset going away.
    assert len(manager.scan(sweep_bluetooth=False)) == 1
    manager.close()


def test_a_usb_only_first_scan_leaves_the_sweep_due(persistence, fake_radio):
    manager = DeviceManager(backend=mouse_backend(), persistence=persistence,
                            bluetooth_interval=3600)
    assert len(manager.scan(sweep_bluetooth=False)) == 1
    assert manager._bluetooth_swept_at is None
    assert len(manager.scan()) == 2
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


def test_the_device_loop_sweeps_before_it_first_waits(persistence, fake_radio):
    service = DaemonService(backend=mouse_backend(), persistence=persistence,
                            enable_effects=False)
    seen = []

    class Stop:
        def wait(self, timeout):
            seen.append(('wait', timeout, service.first_sweep_pending,
                         len(service.manager.devices)))
            return True

    try:
        server_module.device_loop(service, Stop())
        # Exactly one wait, and by then the radio had been swept.
        assert seen == [('wait', server_module.HOTPLUG_INTERVAL, False, 2)]
    finally:
        service.stop()


def test_a_known_device_gets_the_collections_of_the_latest_scan(persistence):
    import dataclasses

    fake = FakeRazerDevice(MOUSE_PID, 'Razer Viper', interface=0)
    backend = FakeHidBackend([fake])
    manager = DeviceManager(backend=backend, persistence=persistence)
    try:
        manager.scan(sweep_bluetooth=False)
        device = manager.devices[0]
        first = list(device.hid_collections)
        fake.info = dataclasses.replace(fake.info, path=fake.info.path + '-moved')
        manager.scan(sweep_bluetooth=False)
        assert manager.devices[0] is device
        assert [c.path for c in device.hid_collections] == [fake.info.path]
        assert device.hid_collections != first
    finally:
        manager.close()
