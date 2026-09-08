"""Shared fixtures."""
from __future__ import annotations

import os
import sys
from typing import Optional

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openrazer_win.core.device import RazerDevice  # noqa: E402
from openrazer_win.core.persistence import Persistence  # noqa: E402
from openrazer_win.core.transport import Transport  # noqa: E402
from openrazer_win.devices import get_database, get_recipes  # noqa: E402
from openrazer_win.hid.fake import FakeHidBackend, FakeRazerDevice  # noqa: E402
from openrazer_win.ble import Advertiser, BleUnavailable  # noqa: E402
from openrazer_win.core.ble_device import BleDevice  # noqa: E402
from openrazer_win.protocol import razer_ble  # noqa: E402

#: A BlackWidow Chroma (classic matrix protocol) and a Viper (extended matrix).
KEYBOARD_PID = 0x0203
MOUSE_PID = 0x0078

KITTY_V2_BT = 0x0562

#: The advertised LE address of the headset this was developed against, and
#: the classic address it advertises alongside it -- one byte apart.
ADDRESS = 0x445ECD583460
CLASSIC_ADDRESS = 0x445ECD573460

#: Razer's manufacturer data, exactly as captured from the headset.
MANUFACTURER_DATA = bytes.fromhex('05620060345 7cd5e4400'.replace(' ', ''))

#: What a sweep hears from it.
ADVERT = Advertiser(ADDRESS, 'Razer Stereo', -55, KITTY_V2_BT, CLASSIC_ADDRESS)

RED = (255, 0, 0)
BLUE = (0, 0, 255)


class FakeBleTransport:
    """Records what would have gone out over the air."""

    def __init__(self, address: int = ADDRESS):
        self.address = address
        self.writes: list = []
        self.closed = False
        #: A real device stops advertising while a link is up, so discovery
        #: asks the transport instead of trusting silence.
        self.connected = False
        #: The colour the transport has been asked to keep asserting.
        self.held: Optional[bytes] = None
        #: Opcodes asked for, and what to answer them with.  The defaults are
        #: the values the real headset gave: 87% charge, not charging, full
        #: brightness.
        self.reads: list = []
        self.answers = {
            razer_ble.OP_BATTERY: bytes((87,)),
            razer_ble.OP_CHARGING: bytes((0,)),
            razer_ble.read_opcode_for(razer_ble.OP_BRIGHTNESS): bytes((0xFF,)),
        }

    def write(self, payload: bytes, hold: bool = False) -> None:
        self.writes.append(bytes(payload))
        if hold:
            self.held = bytes(payload)

    def release(self) -> None:
        self.held = None

    def read(self, opcode: int) -> bytes:
        """Answer a request the way the hardware does."""
        self.reads.append(opcode)
        if opcode not in self.answers:
            raise BleUnavailable(
                'device did not answer 0x{0:02x}'.format(opcode))
        return self.answers[opcode]

    def close(self) -> None:
        self.closed = True
        self.connected = False
        self.held = None

    def is_connected(self) -> bool:
        return self.connected

    @property
    def last(self) -> bytes:
        assert self.writes, 'nothing was written'
        return self.writes[-1]

    @property
    def colour_writes(self) -> list:
        """Only the colour commands, ignoring takeover and brightness."""
        return [w for w in self.writes if w[:1] == bytes((razer_ble.OP_COLOUR,))]


@pytest.fixture
def headset(persistence):
    meta = get_database().get(0x1532, KITTY_V2_BT)
    assert meta is not None, 'the Bluetooth headset is missing from the database'
    transport = FakeBleTransport()
    return BleDevice(meta, transport, persistence), transport



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
