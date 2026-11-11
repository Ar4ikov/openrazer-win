"""Keyswitch optimization on the Huntsman V2: Typing or Gaming, as in Synapse.

The daemon's and the command line's side are in test_daemon.py and test_cli.py.
"""
from __future__ import annotations

import pytest

from openrazer_win.core.device import DeviceError, RazerDevice
from openrazer_win.core.persistence import Persistence
from openrazer_win.core.transport import Transport
from openrazer_win.devices import NotSupported, get_database, get_recipes
from openrazer_win.hid.fake import FakeHidBackend, FakeRazerDevice
from openrazer_win.protocol.report import Status

from .conftest import make_device

HUNTSMAN_V2_TKL = 0x026B
HUNTSMAN_V2 = 0x026C

#: Both commands' arguments for a mode, from the captures in upstream's driver.
TYPING_SET = (0x00, 0x14, 0x00, 0x28, 0x00)
GAMING_SET = (0x00, 0x00, 0x00, 0x00, 0x00)
WIRE = {'typing': (TYPING_SET, (0x01, 0x00, 0x14, 0x00, 0x28, 0x00)),
        'gaming': (GAMING_SET, (0x01, 0x00, 0x00, 0x00, 0x00, 0x00))}


@pytest.fixture(params=(HUNTSMAN_V2_TKL, HUNTSMAN_V2), ids=('tenkeyless', 'full-size'))
def huntsman(request, persistence):
    device, fake = make_device(request.param, persistence)
    _ = device.serial       # keep the serial exchange out of the reports under test
    fake.state.sent.clear()
    yield device, fake
    device.close()


def class_two(fake) -> list:
    return [r for r in fake.sent_reports if r.command_class == 0x02]


def saved(device):
    return device.persistence.get_device_value(device.serial, 'keyswitch_optimization')


@pytest.mark.parametrize('mode', ['typing', 'gaming'])
def test_both_commands_go_in_order_and_the_read_after_them_as_transaction_1f(huntsman, mode):
    device, fake = huntsman
    device.set_keyswitch_optimization(mode)
    device.get_keyswitch_optimization()
    first, second, read = class_two(fake)
    assert [(r.command_id, r.transaction_id) for r in (first, second, read)] == [
        (0x02, 0x1F), (0x15, 0x1F), (0x82, 0x1F)]
    assert tuple(first.arguments[:5]) == WIRE[mode][0]
    assert tuple(second.arguments[:6]) == WIRE[mode][1]


@pytest.mark.parametrize('modes', [('gaming', 'typing'), ('typing', 'gaming'), ('GAMING',)])
def test_modes_round_trip_through_the_emulator_from_typing(huntsman, modes):
    device, fake = huntsman
    assert device.get_keyswitch_optimization() == 'typing'
    for mode in modes:
        device.set_keyswitch_optimization(mode)
        mode = mode.lower()
        assert device.get_keyswitch_optimization() == mode
        assert (fake.state.keyswitch_set, fake.state.keyswitch_store) == WIRE[mode]


@pytest.mark.parametrize('answer, mode', [
    (GAMING_SET, 'gaming'), ((0x00, 0x05, 0x00, 0x28, 0x00), 'gaming'), (TYPING_SET, 'typing'),
])
def test_the_mode_comes_from_the_response_not_the_recipe_state(huntsman, answer, mode):
    """The transpiled recipe ends in ``state = 0``, whatever the keyboard said;
    anything but the typing marker reads as gaming."""
    device, fake = huntsman
    fake.state.keyswitch_set = answer
    assert device.run('read:keyswitch_optimization', require_response=True).vars['state'] == 0
    assert device.get_keyswitch_optimization() == mode


@pytest.mark.parametrize('mode', ['fast', '', None])
def test_anything_else_is_refused_before_it_reaches_the_keyboard(huntsman, mode):
    device, fake = huntsman
    with pytest.raises(ValueError):
        device.set_keyswitch_optimization(mode)
    assert class_two(fake) == []
    assert saved(device) is None


def test_the_mode_is_saved_only_once_set_and_restoring_leaves_it_alone(huntsman):
    """Like the polling rate: restore() is lighting, and runs after every game."""
    device, fake = huntsman
    fake.force_status = Status.NOT_SUPPORTED
    with pytest.raises(DeviceError):
        device.set_keyswitch_optimization('gaming')
    assert saved(device) is None
    fake.force_status = None
    device.set_keyswitch_optimization('gaming')
    assert saved(device) == 'gaming'
    fake.state.sent.clear()
    device.restore()
    assert class_two(fake) == []


def test_only_the_two_huntsmans_have_it(tmp_path):
    database = get_database()
    recipes = get_recipes()
    persistence = Persistence(str(tmp_path / 'p.json'))
    found = set()
    for meta in database:
        if meta.is_bluetooth or meta.pid in recipes.kraken_pids:
            continue
        fake = FakeRazerDevice(meta.pid, meta.name)
        transport = Transport(FakeHidBackend([fake]), fake.info, wait_us=0)
        device = RazerDevice(meta, transport, persistence, recipes)
        if device.capabilities()['keyswitch_optimization']:
            found.add(meta.pid)
    assert found == {HUNTSMAN_V2_TKL, HUNTSMAN_V2}


def test_a_keyboard_without_it_refuses_rather_than_pretends(keyboard):
    device, fake = keyboard
    with pytest.raises(NotSupported):
        device.set_keyswitch_optimization('gaming')
    with pytest.raises(NotSupported):
        device.get_keyswitch_optimization()
    assert class_two(fake) == []
    assert saved(device) is None
