"""Onboard button remapping and profile reading (Basilisk V3)."""
from __future__ import annotations

import pytest

from openrazer_win.protocol.buttons import (
    Binding, binding_from_dict, binding_to_dict, button_name, describe, parse_action,
    parse_button,
)


def b(fn, *data, raw_size=-1):
    return Binding(fn, bytes(data).ljust(5, b'\0'), raw_size)


# -- actions ----------------------------------------------------------------

def test_parse_action():
    assert parse_action('middle') == b(1, 3)
    assert parse_action('key:ctrl+shift+esc') == b(2, 0x03, 0x29)
    assert parse_action('media:volup') == b(10, 0x00, 0xE9)
    assert parse_action('dpi:cycle') == b(6, 6)
    assert parse_action('off') == b(0)
    assert parse_action('double').fn == 11


@pytest.mark.parametrize('bad', ['key:ctrl+nosuchkey', 'banana', 'media:nope', 'key:',
                                 'dpi:clutch:50', 'dpi:clutch:x'])
def test_parse_action_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_action(bad)


def test_describe():
    assert describe(b(1, 5)) == 'forward'
    assert describe(b(2, 0x01, 0x06)) == 'key:ctrl+c'
    assert describe(b(0x22, 1, 2, 3, 4, 5, raw_size=3)) == 'fn=0x22 data=01 02 03'


@pytest.mark.parametrize('text', ['left', 'key:ctrl+shift+esc', 'media:volup', 'dpi:cycle',
                                  'off', 'double', 'key:f13', 'dpi:clutch:400', 'key:lctrl'])
def test_describe_round_trips_parse(text):
    assert describe(parse_action(text)) == text


def test_buttons():
    assert parse_button('back') == 4
    assert parse_button('0x34') == 0x34
    assert button_name(0x77) == 'btn0x77'
    with pytest.raises(ValueError):
        parse_button('nope')


def test_bindings_seen_on_a_real_basilisk_v3():
    clutch = Binding.from_raw(6, 5, bytes([5, 0x01, 0x90, 0x01, 0x90]))
    assert describe(clutch) == 'dpi:clutch:400'
    assert parse_action('dpi:clutch:400') == clutch
    assert parse_action('dpi:clutch:400').size == 5
    assert describe(Binding.from_raw(2, 2, bytes([0, 0xE0, 0, 0, 0]))) == 'key:lctrl'
    assert parse_action('key:lctrl') == b(2, 0, 0xE0)


def test_the_rest_of_what_synapse_stores():
    """Function ids razerctl (from Synapse's own code) and OpenSnek decoded."""
    assert describe(Binding.from_raw(0x03, 3, bytes([0x16, 0x15, 0x01]))) == 'macro:1615:once'
    assert describe(Binding.from_raw(0x03, 3, bytes([0x16, 0x15, 0x04]))) == 'macro:1615:4x'
    assert describe(Binding.from_raw(0x04, 2, bytes([0x0F, 0x7B]))) == 'macro:0f7b:held'
    assert describe(Binding.from_raw(0x05, 2, bytes([0x0F, 0x7B]))) == 'macro:0f7b:toggle'
    assert describe(Binding.from_raw(0x0E, 3, bytes([0x68, 0x00, 0x14]))) == 'turbo:tiltleft:20'
    assert describe(Binding.from_raw(0x0D, 4, bytes([0x01, 0x06, 0x00, 0x8E]))) == 'turbo:key:ctrl+c:142'
    assert describe(Binding.from_raw(0x07, 1, bytes([0x04]))) == 'profile:next'
    assert describe(Binding.from_raw(0x12, 1, bytes([0x01]))) == 'wheelmode'


@pytest.mark.parametrize('text', ['profile:next', 'wheelmode', 'turbo:tiltleft:20', 'turbo:left:142'])
def test_settable_ones_round_trip(text):
    assert describe(parse_action(text)) == text


def test_the_wheel_tilt_default_is_what_the_mouse_stores():
    assert parse_action('turbo:tiltleft:20') == Binding.from_raw(0x0E, 3, bytes([0x68, 0x00, 0x14]))


@pytest.mark.parametrize('bad', ['macro:1615:once', 'turbo:left:0', 'turbo:left:x', 'turbo:nope:20',
                                 'profile:previous'])
def test_ones_that_cannot_be_written(bad):
    with pytest.raises(ValueError):
        parse_action(bad)


def test_basilisk_buttons_have_names():
    assert [button_name(i) for i in (0x0F, 0x34, 0x35, 0x0E, 0x6A)] == [
        'clutch', 'tiltleft', 'tiltright', 'profile', 'wheelmode']
    assert parse_button('clutch') == 0x0F


def test_right_modifiers():
    assert describe(b(2, 0x10, 0x06)) == 'key:rctrl+c'
    assert parse_action('key:rshift+ralt+f13') == b(2, 0x60, 0x68)


# -- plain-data form, as it travels over RPC --------------------------------

def test_dict_form():
    assert binding_to_dict(parse_action('key:ctrl+c')) == {
        'action': 'key:ctrl+c', 'fn': 2, 'size': 2, 'data': [1, 6, 0, 0, 0]}
    for binding in (parse_action('dpi:clutch:400'), Binding.from_raw(0x22, 3, bytes([1, 2, 3]))):
        back = binding_from_dict(binding_to_dict(binding))
        assert back == binding and back.size == binding.size


def test_dict_form_rejects_bad_bytes():
    with pytest.raises(ValueError):
        binding_from_dict({'fn': 1, 'size': 1, 'data': [999]})


# -- the device, over the emulator -------------------------------------------

from openrazer_win.core.device import DeviceError  # noqa: E402
from openrazer_win.devices.recipes import NotSupported  # noqa: E402

from .conftest import make_device  # noqa: E402

ONBOARD_CLASSES = (0x02, 0x05)


@pytest.fixture
def basilisk(persistence):
    device, fake = make_device(0x0099, persistence)
    yield device, fake
    device.transport.close()


def onboard_reports(fake, command_id=None):
    return [r for r in fake.sent_reports if r.command_class in ONBOARD_CLASSES
            and (command_id is None or r.command_id == command_id)]


def test_profiles_are_read_from_the_mouse(basilisk):
    device, _ = basilisk
    assert device.get_onboard_profiles() == {
        'active': 1, 'max': 5,
        'profiles': [{'id': 1, 'name': None}, {'id': 2, 'name': '1'}, {'id': 3, 'name': '2'},
                     {'id': 4, 'name': 'TEJAS-LOQ-Default'}]}


def test_buttons_come_in_the_mouse_order(basilisk):
    device, _ = basilisk
    buttons = device.get_buttons()
    assert [x['name'] for x in buttons][:5] == ['left', 'right', 'middle', 'forward', 'back']
    assert buttons[5] == {'id': 0x34, 'name': 'tiltleft'}
    assert len(buttons) == 13


def test_bindings_are_decoded(basilisk):
    device, _ = basilisk
    assert device.get_button_binding(1, 0x0F, 'hypershift')['action'] == 'dpi:clutch:400'
    assert device.get_button_binding(1, 0x0F)['action'] == 'key:lctrl'
    assert device.get_button_binding(1, 0x0E) == {
        'action': 'profile:next', 'fn': 7, 'size': 1, 'data': [4, 0, 0, 0, 0]}
    assert device.get_button_binding(1, 3, 'hypershift')['action'] == 'macro:1615:once'
    unknown = Binding.from_raw(0x22, 2, bytes([1, 2]))
    assert describe(unknown) == 'fn=0x22 data=01 02'


def test_onboard_reports_use_the_device_transaction_id(basilisk):
    device, fake = basilisk
    device.get_onboard_profiles()
    device.get_button_binding(1, 1)
    assert onboard_reports(fake)
    assert all(r.transaction_id == 0x1F for r in onboard_reports(fake))


def test_a_busy_read_is_an_error_not_a_binding(basilisk):
    device, fake = basilisk
    fake.state.busy_reads = 1
    with pytest.raises(DeviceError):
        device.get_button_binding(1, 1)


def test_firmware_without_an_active_profile_read(basilisk):
    device, fake = basilisk
    fake.state.onboard['active'] = None
    assert device.get_onboard_profiles()['active'] is None


def test_the_active_profile_alone_is_one_read(basilisk):
    device, fake = basilisk
    before = len(onboard_reports(fake))
    assert device.get_active_onboard_profile() == 1
    fake.state.onboard['active'] = 3  # the profile button on the mouse was pressed
    assert device.get_active_onboard_profile() == 3
    assert len(onboard_reports(fake)) - before == 2
    fake.state.onboard['active'] = None
    assert device.get_active_onboard_profile() is None


def test_the_app_can_switch_the_mouse_profile(basilisk):
    device, fake = basilisk
    device.set_active_onboard_profile(3)
    assert fake.state.onboard['active'] == 3
    assert device.get_active_onboard_profile() == 3
    # Profile 0 is the live copy: it now holds profile 3's bindings.
    device.set_button_binding(3, 4, 'middle')
    assert device.get_button_binding(0, 4)['action'] == 'middle'


@pytest.mark.parametrize('bad', [0, 5, 9, '2', True])
def test_only_a_profile_the_mouse_has_can_be_switched_to(basilisk, bad):
    device, fake = basilisk
    before = len(onboard_reports(fake, 0x04))
    with pytest.raises(ValueError):
        device.set_active_onboard_profile(bad)
    assert len(onboard_reports(fake, 0x04)) == before


def test_a_switch_the_mouse_ignores_is_an_error(basilisk):
    device, fake = basilisk
    fake.state.ignore_profile_switch = True
    with pytest.raises(DeviceError, match='still on profile 1'):
        device.set_active_onboard_profile(2)


def test_layer_is_checked(basilisk):
    device, fake = basilisk
    with pytest.raises(ValueError):
        device.get_button_binding(1, 1, 'upside-down')
    assert not onboard_reports(fake)


def test_other_mice_are_not_offered_it(persistence, basilisk):
    other, fake = make_device(0x0078, persistence)
    try:
        with pytest.raises(NotSupported):
            other.get_buttons()
        assert not onboard_reports(fake)
        assert other.capabilities()['onboard_buttons'] is False
    finally:
        other.transport.close()
    assert basilisk[0].capabilities()['onboard_buttons'] is True


# -- writing ---------------------------------------------------------------

def test_a_button_is_rebound(basilisk):
    device, _ = basilisk
    old = device.set_button_binding(1, 5, 'middle')
    assert old['action'] == 'forward'
    assert device.get_button_binding(1, 5)['action'] == 'middle'
    assert device.get_button_binding(0, 5)['action'] == 'middle'  # the live copy follows


def test_a_bad_action_sends_nothing(basilisk):
    device, fake = basilisk
    with pytest.raises(ValueError):
        device.set_button_binding(1, 5, 'banana')
    with pytest.raises(ValueError):
        device.set_button_binding(1, 5, 'middle', layer=2)
    assert not onboard_reports(fake)


def test_a_write_the_mouse_gets_wrong_is_rolled_back(basilisk):
    device, fake = basilisk
    fake.state.corrupt_writes = 1
    with pytest.raises(DeviceError, match='old binding restored'):
        device.set_button_binding(1, 5, 'middle')
    assert device.get_button_binding(1, 5)['action'] == 'forward'


def test_a_rollback_that_fails_says_so(basilisk):
    device, fake = basilisk
    fake.state.corrupt_writes = 2
    with pytest.raises(DeviceError, match='restore FAILED'):
        device.set_button_binding(1, 5, 'middle')


def test_a_busy_read_before_a_write_writes_nothing(basilisk):
    device, fake = basilisk
    fake.state.busy_reads = 1
    with pytest.raises(DeviceError):
        device.set_button_binding(1, 5, 'middle')
    assert not onboard_reports(fake, 0x0C)


def test_backup_and_restore(basilisk):
    device, fake = basilisk
    doc = device.backup_button_bindings(1)
    assert doc['profile'] == 1 and len(doc['bindings']) == 13 * 2
    device.set_button_binding(1, 5, 'middle')
    device.set_button_binding(1, 0x0E, 'off')
    assert device.restore_button_bindings(doc) == 2
    assert device.get_button_binding(1, 5)['action'] == 'forward'
    assert fake.state.onboard['bindings'][(1, 0x0E, 0)] == (0x07, 1, bytes([4, 0, 0, 0, 0]))
    assert device.restore_button_bindings(doc) == 0


@pytest.mark.parametrize('rows', [
    [{'button': 4, 'layer': 0, 'fn': 1, 'size': 1, 'data': [4, 0, 0, 0, 0]}, 'junk'],
    [{'button': 'x', 'layer': 0, 'fn': 1, 'size': 1, 'data': [4]}],
    [{'button': 4, 'layer': 0, 'fn': 1, 'size': 1, 'data': [999]}],
    [{'button': 1, 'layer': 2, 'fn': 1, 'size': 1, 'data': [1]}],
    [{'button': 1, 'layer': 0}],
    [{'button': 4, 'layer': 0, 'fn': 1, 'size': 1, 'data': [4]},
     {'button': 5, 'layer': 1.0, 'fn': 1, 'size': 1, 'data': [5]}],
    [{'button': 4, 'layer': 0, 'fn': 0x100, 'size': 1, 'data': [4]}],
    [{'button': 4, 'layer': 0, 'fn': 1, 'size': -1, 'data': [4]}],
    [{'button': 4, 'layer': 0, 'fn': 1, 'size': 1, 'data': {1: 2}}],
])
def test_a_bad_backup_restores_nothing(basilisk, rows):
    device, fake = basilisk
    with pytest.raises((ValueError, TypeError)):
        device.restore_button_bindings({'profile': 1, 'bindings': rows})
    assert not onboard_reports(fake)


def test_a_backup_missing_its_rows_is_a_value_error(basilisk):
    device, fake = basilisk
    with pytest.raises(ValueError, match='bindings'):
        device.restore_button_bindings({'profile': 1})
    assert not onboard_reports(fake)


def test_device_error_carries_the_status():
    assert DeviceError('busy', status=0x01).status == 0x01
    assert DeviceError('plain').status is None


def test_a_razerbind_backup_restores_here(basilisk):
    """razerbind writes the same file; layer as 0/1, data as five ints."""
    device, _ = basilisk
    doc = {'profile': 1, 'bindings': [
        {'button': 5, 'layer': 0, 'fn': 1, 'size': 1, 'data': [3, 0, 0, 0, 0]}]}
    assert device.restore_button_bindings(doc) == 1
    assert device.get_button_binding(1, 5)['action'] == 'middle'


# -- over the daemon ----------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    import threading

    from openrazer_win.client.rpc import RpcClient
    from openrazer_win.core.persistence import Persistence
    from openrazer_win.daemon.demo import build_demo_backend
    from openrazer_win.daemon.server import DaemonServer, DaemonService

    service = DaemonService(backend=build_demo_backend((0x0099, 0x0078)),
                            persistence=Persistence(str(tmp_path / 'p.json')),
                            enable_effects=False)
    service.start()
    server = DaemonServer(service, port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05},
                              daemon=True)
    thread.start()
    connection = RpcClient(server.endpoint, timeout=5)
    try:
        yield connection
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        service.stop()
        thread.join(timeout=2)


def _serial(client, pid):
    return next(d['serial'] for d in client.call('devices.list') if d['pid'] == pid)


def test_bindings_over_rpc(client):
    serial = _serial(client, 0x0099)
    reply = client.call('device.call', serial=serial, method='get_button_binding',
                        args=[1, 15], kwargs={'layer': 'hypershift'})
    assert reply['action'] == 'dpi:clutch:400'
    old = client.call('device.call', serial=serial, method='set_button_binding',
                      args=[1, 5, 'middle'], kwargs={})
    assert old['action'] == 'forward'
    doc = client.call('device.call', serial=serial, method='backup_button_bindings',
                      args=[1], kwargs={})
    assert client.call('device.call', serial=serial, method='restore_button_bindings',
                       args=[doc], kwargs={}) == 0


def test_a_bad_backup_is_invalid_params_over_rpc(client):
    from openrazer_win.daemon.protocol import INVALID_PARAMS, RpcError

    with pytest.raises(RpcError) as raised:
        client.call('device.call', serial=_serial(client, 0x0099),
                    method='restore_button_bindings', args=[{'profile': 1}], kwargs={})
    assert raised.value.code == INVALID_PARAMS


def test_other_mice_say_not_supported_over_rpc(client):
    from openrazer_win.daemon.protocol import NOT_SUPPORTED, RpcError

    with pytest.raises(RpcError) as raised:
        client.call('device.call', serial=_serial(client, 0x0078), method='set_button_binding',
                    args=[1, 5, 'middle'], kwargs={})
    assert raised.value.code == NOT_SUPPORTED
