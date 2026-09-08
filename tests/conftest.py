"""Shared fixtures."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openrazer_win.core.device import RazerDevice  # noqa: E402
from openrazer_win.core.persistence import Persistence  # noqa: E402
from openrazer_win.core.transport import Transport  # noqa: E402
from openrazer_win.devices import get_database, get_recipes  # noqa: E402
from openrazer_win.hid.fake import FakeHidBackend, FakeRazerDevice  # noqa: E402

#: A BlackWidow Chroma (classic matrix protocol) and a Viper (extended matrix).
KEYBOARD_PID = 0x0203
MOUSE_PID = 0x0078


@pytest.fixture
def database():
    return get_database()


@pytest.fixture
def recipes():
    return get_recipes()


@pytest.fixture
def persistence(tmp_path):
    return Persistence(str(tmp_path / 'persistence.json'))


def make_device(pid, persistence, *, argb=False):
    """Build a RazerDevice backed by the emulator."""
    database = get_database()
    recipes = get_recipes()
    meta = database.get(0x1532, pid)
    assert meta is not None, 'unknown product id 0x{0:04x}'.format(pid)
    params = recipes.transport_params(meta.driver, meta.pid)
    fake = FakeRazerDevice(pid, meta.name, interface=params.get('index') or 0,
                           argb=argb)
    backend = FakeHidBackend([fake])
    transport = Transport(backend, fake.info, wait_us=0, argb_info=fake.argb_info)
    device = RazerDevice(meta, transport, persistence, recipes)
    return device, fake


def last_report(fake, command_class=None, command_id=None):
    """The most recent report the emulator saw, optionally filtered.

    Device calls persist their new state, and persistence is keyed by serial --
    so the first write to a device is followed by a get-serial exchange.
    Filtering by command keeps assertions about the interesting report stable.
    """
    for report in reversed(fake.sent_reports):
        if command_class is not None and report.command_class != command_class:
            continue
        if command_id is not None and report.command_id != command_id:
            continue
        return report
    raise AssertionError('no report matching class={0} id={1} was sent'.format(
        command_class, command_id))


@pytest.fixture
def keyboard(persistence):
    device, fake = make_device(KEYBOARD_PID, persistence)
    _ = device.serial      # warm the cache so it does not interleave later
    yield device, fake
    device.close()


@pytest.fixture
def mouse(persistence):
    device, fake = make_device(MOUSE_PID, persistence)
    _ = device.serial
    yield device, fake
    device.close()


@pytest.fixture(autouse=True)
def no_bluetooth_radio(monkeypatch):
    """Keep the test suite off the actual Bluetooth radio.

    A scan listens for advertisements for several seconds and would pick up
    whichever hardware happens to be in the room, so every test runs with the
    radio reported as unavailable unless it asks otherwise.
    """
    import openrazer_win.ble as ble
    monkeypatch.setattr(ble, 'is_available', lambda: False)
    return ble
