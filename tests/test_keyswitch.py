"""Keyswitch optimization on the Huntsman V2: Typing or Gaming, as in Synapse."""
from __future__ import annotations

import threading

import pytest

from openrazer_win.cli import main
from openrazer_win.client import DeviceManager
from openrazer_win.client.rpc import RpcClient
from openrazer_win.core.device import DeviceError, RazerDevice
from openrazer_win.core.persistence import Persistence
from openrazer_win.core.transport import Transport
from openrazer_win.daemon.demo import build_demo_backend
from openrazer_win.daemon.protocol import INVALID_PARAMS, NOT_SUPPORTED, RpcError
from openrazer_win.daemon.server import DEVICE_METHODS, DaemonServer, DaemonService
from openrazer_win.devices import NotSupported, get_database, get_recipes
from openrazer_win.hid.fake import FakeHidBackend, FakeRazerDevice
from openrazer_win.protocol import chroma
from openrazer_win.protocol.report import Status

from .conftest import KEYBOARD_PID, make_device

HUNTSMAN_V2_TKL = 0x026B
HUNTSMAN_V2 = 0x026C
PIDS = (HUNTSMAN_V2_TKL, HUNTSMAN_V2)

TYPING_SET = (0x00, 0x14, 0x00, 0x28, 0x00)
GAMING_SET = (0x00, 0x00, 0x00, 0x00, 0x00)
TYPING_STORE = (0x01, 0x00, 0x14, 0x00, 0x28, 0x00)
GAMING_STORE = (0x01, 0x00, 0x00, 0x00, 0x00, 0x00)

#: Both commands' arguments for a mode, from the captures in upstream's driver.
WIRE = {'typing': (TYPING_SET, TYPING_STORE), 'gaming': (GAMING_SET, GAMING_STORE)}


@pytest.fixture(params=PIDS, ids=('tenkeyless', 'full-size'))
def huntsman(request, persistence):
    device, fake = make_device(request.param, persistence)
    _ = device.serial       # keep the serial exchange out of the reports under test
    fake.state.sent.clear()
    yield device, fake
    device.close()


def class_two(fake) -> list:
    return [r for r in fake.sent_reports if r.command_class == 0x02]


# -- the protocol ---------------------------------------------------------------

@pytest.mark.parametrize('byte, first, second', [
    (0x00, TYPING_SET, TYPING_STORE),
    (0x01, GAMING_SET, GAMING_STORE),
])
def test_the_builders_put_the_upstream_bytes_on_the_wire(byte, first, second):
    report = chroma.misc_set_keyswitch_optimization_command1(byte)
    assert (report.command_class, report.command_id, report.data_size) == (0x02, 0x02, 0x04)
    assert tuple(report.arguments[:5]) == first
    assert not any(report.arguments[5:])

    report = chroma.misc_set_keyswitch_optimization_command2(byte)
    assert (report.command_class, report.command_id, report.data_size) == (0x02, 0x15, 0x05)
    assert tuple(report.arguments[:6]) == second
    assert not any(report.arguments[6:])


def test_the_read_builder_asks_with_no_arguments():
    report = chroma.misc_get_keyswitch_optimization()
    assert (report.command_class, report.command_id, report.data_size) == (0x02, 0x82, 0x04)
    assert not any(report.arguments)


@pytest.mark.parametrize('mode', ['typing', 'gaming'])
def test_a_write_sends_both_commands_in_order_as_transaction_1f(huntsman, mode):
    device, fake = huntsman
    device.set_keyswitch_optimization(mode)
    first, second = class_two(fake)
    assert (first.command_id, second.command_id) == (0x02, 0x15)
    assert (first.transaction_id, second.transaction_id) == (0x1F, 0x1F)
    assert tuple(first.arguments[:5]) == WIRE[mode][0]
    assert tuple(second.arguments[:6]) == WIRE[mode][1]


def test_a_read_is_transaction_1f(huntsman):
    device, fake = huntsman
    device.get_keyswitch_optimization()
    request, = class_two(fake)
    assert (request.command_id, request.transaction_id) == (0x82, 0x1F)


# -- the emulator and the device ------------------------------------------------

def test_a_keyboard_starts_in_typing_mode(huntsman):
    device, _ = huntsman
    assert device.get_keyswitch_optimization() == 'typing'


@pytest.mark.parametrize('first, second', [('gaming', 'typing'), ('typing', 'gaming')])
def test_modes_round_trip_through_the_emulator(huntsman, first, second):
    device, fake = huntsman
    for mode in (first, second):
        device.set_keyswitch_optimization(mode)
        assert device.get_keyswitch_optimization() == mode
        assert (fake.state.keyswitch_set, fake.state.keyswitch_store) == WIRE[mode]


def test_the_mode_comes_from_the_response_not_the_recipe_state(huntsman):
    """The transpiled recipe ends in ``state = 0``, whatever the keyboard said."""
    device, fake = huntsman
    fake.state.keyswitch_set = GAMING_SET
    env = device.run('read:keyswitch_optimization', require_response=True)
    assert env.vars['state'] == 0
    assert device.get_keyswitch_optimization() == 'gaming'


def test_anything_but_the_marker_reads_as_gaming(huntsman):
    device, fake = huntsman
    fake.state.keyswitch_set = (0x00, 0x05, 0x00, 0x28, 0x00)
    assert device.get_keyswitch_optimization() == 'gaming'


def test_modes_are_not_case_sensitive(huntsman):
    device, _ = huntsman
    device.set_keyswitch_optimization('GAMING')
    assert device.get_keyswitch_optimization() == 'gaming'


@pytest.mark.parametrize('mode', ['fast', '', 0, 1, True, False, None])
def test_anything_else_is_refused_before_it_reaches_the_keyboard(huntsman, mode):
    device, fake = huntsman
    with pytest.raises(ValueError):
        device.set_keyswitch_optimization(mode)
    assert class_two(fake) == []
    assert device.persistence.get_device_value(device.serial, 'keyswitch_optimization') is None


def test_a_refused_write_saves_nothing(huntsman):
    device, fake = huntsman
    fake.force_status = Status.NOT_SUPPORTED
    with pytest.raises(DeviceError):
        device.set_keyswitch_optimization('gaming')
    assert device.persistence.get_device_value(device.serial, 'keyswitch_optimization') is None


def test_the_mode_is_saved_under_the_serial(huntsman):
    device, _ = huntsman
    device.set_keyswitch_optimization('gaming')
    assert device.persistence.get_device_value(
        device.serial, 'keyswitch_optimization') == 'gaming'


def test_restoring_does_not_touch_the_keyswitches(huntsman):
    """Like the polling rate: restore() is lighting, and runs after every game."""
    device, fake = huntsman
    device.set_keyswitch_optimization('gaming')
    fake.state.sent.clear()
    device.restore()
    assert class_two(fake) == []


# -- which devices have it ------------------------------------------------------

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
    assert found == set(PIDS)


def test_a_keyboard_without_it_refuses_rather_than_pretends(keyboard):
    device, fake = keyboard
    assert device.capabilities()['keyswitch_optimization'] is False
    with pytest.raises(NotSupported):
        device.set_keyswitch_optimization('gaming')
    with pytest.raises(NotSupported):
        device.get_keyswitch_optimization()
    assert class_two(fake) == []
    assert device.persistence.get_device_value(device.serial, 'keyswitch_optimization') is None


def test_a_device_of_another_class_reports_the_capability_as_absent(headset):
    device, _ = headset
    assert device.capabilities()['keyswitch_optimization'] is False


# -- the daemon and the client --------------------------------------------------

@pytest.fixture
def daemon(tmp_path):
    service = DaemonService(backend=build_demo_backend(),
                            persistence=Persistence(str(tmp_path / 'p.json')),
                            enable_effects=False)
    service.start()
    server = DaemonServer(service, port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05},
                              daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        service.stop()
        thread.join(timeout=2)


@pytest.fixture
def manager(daemon):
    instance = DeviceManager(endpoint=daemon.endpoint)
    yield instance
    instance.close()


@pytest.fixture
def rpc(daemon):
    connection = RpcClient(daemon.endpoint, timeout=5)
    yield connection
    connection.close()


def by_pid(manager, pid):
    return next(d for d in manager.devices if d.product_id == pid)


def test_the_rpc_allowlist_has_both_calls():
    assert {'get_keyswitch_optimization', 'set_keyswitch_optimization'} <= DEVICE_METHODS


def test_the_client_round_trips_a_mode(manager, daemon):
    keyboard = by_pid(manager, HUNTSMAN_V2_TKL)
    assert keyboard.has('keyswitch_optimization')
    assert keyboard.keyswitch_optimization == 'typing'
    keyboard.keyswitch_optimization = 'gaming'
    assert keyboard.keyswitch_optimization == 'gaming'
    # The daemon saved it, rather than holding it in memory until it stops.
    stored = Persistence(daemon.service.persistence.path)
    assert stored.get_device_value(keyboard.serial, 'keyswitch_optimization') == 'gaming'


def test_the_capability_is_absent_on_the_demo_blackwidow(manager):
    assert not by_pid(manager, KEYBOARD_PID).has('keyswitch_optimization')


def test_a_bad_mode_is_a_bad_request_not_a_daemon_error(rpc, manager):
    serial = by_pid(manager, HUNTSMAN_V2_TKL).serial
    with pytest.raises(RpcError) as raised:
        rpc.call('device.call', serial=serial, method='set_keyswitch_optimization',
                 args=['warp'])
    assert raised.value.code == INVALID_PARAMS


def test_a_keyboard_without_it_answers_not_supported(rpc, manager):
    serial = by_pid(manager, KEYBOARD_PID).serial
    with pytest.raises(RpcError) as raised:
        rpc.call('device.call', serial=serial, method='set_keyswitch_optimization',
                 args=['gaming'])
    assert raised.value.code == NOT_SUPPORTED


# -- the command line -----------------------------------------------------------

@pytest.fixture
def cli(daemon, tmp_path, monkeypatch):
    """The CLI, pointed at the emulated daemon and nothing else."""
    path = str(tmp_path / 'daemon.json')
    daemon.endpoint.write(path)
    monkeypatch.setattr('openrazer_win.daemon.protocol.endpoint_path', lambda: path)
    return daemon


def test_the_cli_reads_and_sets_a_mode(cli, capsys):
    assert main(['keyswitch', '--device', 'huntsman']) == 0
    assert 'typing' in capsys.readouterr().out
    assert main(['keyswitch', 'gaming', '--device', 'huntsman']) == 0
    assert 'keyswitch optimization gaming' in capsys.readouterr().out
    assert main(['keyswitch', '--device', 'huntsman']) == 0
    assert 'gaming' in capsys.readouterr().out


def test_the_cli_says_so_when_no_keyboard_has_it(cli, capsys):
    assert main(['keyswitch', 'gaming', '--device', 'viper']) == 2
    assert 'keyswitch optimization' in capsys.readouterr().err


def test_the_cli_only_takes_the_two_modes(capsys):
    with pytest.raises(SystemExit):
        main(['keyswitch', 'warp'])
    assert 'invalid choice' in capsys.readouterr().err


def test_info_shows_the_mode_only_where_there_is_one(cli, capsys):
    assert main(['info', 'huntsman']) == 0
    assert 'keyswitch   typing' in capsys.readouterr().out
    assert main(['info', 'viper']) == 0
    assert 'keyswitch' not in capsys.readouterr().out
