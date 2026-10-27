"""Macro keys and the Fn layer, handled the way the kernel driver does.

Issue #1: the M-keys of a BlackWidow did nothing without Synapse.  In driver
mode the keyboard leaves its macro keys -- and its whole Fn layer -- to the
host; these tests drive the user-space stand-in for the kernel's handling with
emulated vendor reports and hook events.
"""
from __future__ import annotations

import time

import pytest

from openrazer_win.core.persistence import Persistence
from openrazer_win.daemon.protocol import INVALID_PARAMS, RpcError
from openrazer_win.daemon.server import DaemonService
from openrazer_win.hid.fake import FakeHidBackend, FakeRazerDevice
from openrazer_win.keys import KeyService, MacroError, MacroStore, load_key_data
from openrazer_win.keys.codes import KEYS, parse_sequence
from openrazer_win.keys.hook import KeyEvent
from openrazer_win.keys.inject import RecordingInjector
from openrazer_win.keys.macros import validate

BLACKWIDOW_CHROMA = 0x0203
BLADE = 0x0210            # a Blade laptop keyboard: firmware does all of this

F1 = KEYS['KEY_F1']
F10 = KEYS['KEY_F10']
F12 = KEYS['KEY_F12']
F9 = KEYS['KEY_F9']
A = KEYS['KEY_A']
FN, M1, M2 = 0x01, 0x20, 0x21
VK_MUTE, VK_F13, VK_LWIN = 0xAD, 0x7C, 0x5B


class FakeHook:
    running = False

    def start(self):
        self.running = True
        return True

    def stop(self):
        self.running = False


def key(k, down, t=0, injected=False):
    return KeyEvent(vk=k.vk, scan=k.scan, extended=k.extended, down=down,
                    injected=injected, ours=False, time=t)


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture
def rig(tmp_path):
    """A daemon service with key handling, a BlackWidow, and a recording injector."""
    fake = FakeRazerDevice(BLACKWIDOW_CHROMA, 'Razer BlackWidow Chroma', interface=2, keys=True)
    backend = FakeHidBackend([fake])
    injector = RecordingInjector()
    launched = []
    keys = KeyService(backend, macros=MacroStore(str(tmp_path / 'macros.json')),
                      injector=injector, hook=FakeHook(), launcher=launched.append)
    service = DaemonService(backend=backend, persistence=Persistence(str(tmp_path / 'p.json')),
                            enable_effects=False, keys=keys, macros=keys.macros)
    service.start()
    device = service.manager.devices[0]
    yield service, keys, device, fake, injector, launched
    service.stop()


def keys_sent(injector):
    return [(entry[1] if isinstance(entry[1], int) else entry[1].vk, entry[2])
            for entry in injector.sent if entry[0] == 'key']


# -- the generated tables ---------------------------------------------------

def test_the_tables_come_from_the_kernel_driver():
    data = load_key_data()
    default = dict(data['tables'][data['default_table']])
    assert default['KEY_F1'] == 'KEY_MUTE'
    assert default['KEY_F9'] == 'RAZER_MACRO_KEY'
    assert data['vendor_codes']['20'] == 'USB_HID_KEY_F13'
    assert data['tables_by_pid']['0257'] == 'chroma_keys_3'       # Huntsman Mini


# -- taking a keyboard over -------------------------------------------------

def test_a_keyboard_with_a_key_report_is_handled_and_enters_driver_mode(rig):
    service, keys, device, fake, _, _ = rig
    assert keys.handles(device)
    assert fake.state.device_mode == (0x03, 0x00)


def test_a_keyboard_without_one_keeps_its_own_fn_layer(tmp_path):
    fake = FakeRazerDevice(BLACKWIDOW_CHROMA, 'Razer BlackWidow Chroma', interface=2)
    backend = FakeHidBackend([fake])
    keys = KeyService(backend, macros=MacroStore(str(tmp_path / 'm.json')),
                      injector=RecordingInjector(), hook=FakeHook())
    service = DaemonService(backend=backend, persistence=Persistence(str(tmp_path / 'p.json')),
                            enable_effects=False, keys=keys)
    service.start()
    try:
        assert not keys.handles(service.manager.devices[0])
        assert fake.state.device_mode == (0x00, 0x00)
    finally:
        service.stop()


def test_without_the_hook_nothing_is_taken_over(tmp_path):
    class DeadHook(FakeHook):
        def start(self):
            return False

    fake = FakeRazerDevice(BLACKWIDOW_CHROMA, 'Razer BlackWidow Chroma', interface=2, keys=True)
    keys = KeyService(FakeHidBackend([fake]), macros=MacroStore(str(tmp_path / 'm.json')),
                      injector=RecordingInjector(), hook=DeadHook())
    service = DaemonService(backend=keys.backend, enable_effects=False, keys=keys,
                            persistence=Persistence(str(tmp_path / 'p.json')))
    service.start()
    try:
        assert fake.state.device_mode == (0x00, 0x00)
    finally:
        service.stop()


def test_blade_laptops_are_left_to_their_firmware():
    keys = KeyService(FakeHidBackend(), macros=MacroStore('unused.json'),
                      injector=RecordingInjector(), hook=FakeHook())
    assert '{0:04x}'.format(BLADE) in load_key_data()['no_translation_pids']

    class Blade:
        driver = 'kbd'

        class info:
            pid = BLADE
            driver_mode = True

    assert not keys.wants(Blade())


def test_stopping_the_daemon_hands_the_keyboard_back(rig):
    service, _, _, fake, _, _ = rig
    service.stop()
    assert fake.state.device_mode == (0x00, 0x00)


# -- macro keys -------------------------------------------------------------

def test_an_unbound_macro_key_sends_f13(rig):
    _, _, _, fake, injector, _ = rig
    fake.hold(M1)
    fake.hold()
    assert wait_for(lambda: keys_sent(injector) == [(VK_F13, True), (VK_F13, False)])


def test_a_bound_macro_key_plays_its_binding_instead(rig):
    service, _, device, fake, injector, _ = rig
    service.rpc_macro_set(device.serial, 'M1', {'type': 'text', 'text': 'hello'})
    fake.hold(M1)
    fake.hold()
    assert wait_for(lambda: ('text', 'hello') in injector.sent)
    assert keys_sent(injector) == []


def test_a_launch_binding_opens_its_target(rig):
    service, _, device, fake, _, launched = rig
    service.rpc_macro_set(device.serial, 'M2', {'type': 'launch', 'target': 'notepad.exe'})
    fake.hold(M2)
    assert wait_for(lambda: launched == ['notepad.exe'])


def test_a_disabled_key_does_nothing(rig):
    service, _, device, fake, injector, _ = rig
    service.rpc_macro_set(device.serial, 'M1', {'type': 'disabled'})
    fake.hold(M1)
    fake.hold()
    time.sleep(0.2)
    assert injector.sent == []


def test_a_key_macro_never_leaves_a_key_held(tmp_path):
    keys = KeyService(FakeHidBackend(), macros=MacroStore(str(tmp_path / 'm.json')),
                      injector=RecordingInjector(), hook=FakeHook())
    events = parse_sequence('shift+a')[:2]          # presses, and never lets go
    keys.play({'type': 'keys', 'events': events})
    released = [entry for entry in keys.injector.sent if entry[0] == 'raw' and not entry[4]]
    assert len(released) == 2


# -- the Fn layer -----------------------------------------------------------

def test_fn_and_f1_is_mute(rig):
    _, keys, _, fake, injector, _ = rig
    fake.hold(FN)
    assert wait_for(lambda: any(b.fn for b in keys._bindings.values()))
    assert keys.handle_key(key(F1, True)) is True
    assert keys.handle_key(key(F1, False)) is True
    assert keys_sent(injector) == [(VK_MUTE, True), (VK_MUTE, False)]


def test_f1_without_fn_is_f1(rig):
    _, keys, _, _, injector, _ = rig
    assert keys.handle_key(key(F1, True)) is False
    assert injector.sent == []


def test_letting_go_of_fn_first_still_releases_the_translated_key(rig):
    _, keys, _, fake, injector, _ = rig
    fake.hold(FN)
    assert wait_for(lambda: any(b.fn for b in keys._bindings.values()))
    keys.handle_key(key(F1, True))
    fake.hold()
    assert wait_for(lambda: not any(b.fn for b in keys._bindings.values()))
    assert keys.handle_key(key(F1, False)) is True
    assert keys_sent(injector)[-1] == (VK_MUTE, False)


def test_injected_keys_are_never_translated(rig):
    _, keys, _, fake, _, _ = rig
    fake.hold(FN)
    assert wait_for(lambda: any(b.fn for b in keys._bindings.values()))
    assert keys.handle_key(key(F1, True, injected=True)) is False


def test_game_mode_holds_back_the_windows_key(rig):
    _, keys, device, fake, _, _ = rig
    win = KeyEvent(vk=VK_LWIN, scan=0x5B, extended=True, down=True, injected=False,
                   ours=False, time=0)
    assert keys.handle_key(win) is False
    fake.hold(FN)
    assert wait_for(lambda: any(b.fn for b in keys._bindings.values()))
    keys.handle_key(key(F10, True))
    keys.handle_key(key(F10, False))
    assert wait_for(lambda: keys.handle_key(win) is True)


def test_fn_brightness_steps_the_backlight(rig):
    _, keys, device, fake, _, _ = rig
    device.set_brightness(50)
    fake.hold(FN)
    assert wait_for(lambda: any(b.fn for b in keys._bindings.values()))
    keys.handle_key(key(F12, True))
    keys.handle_key(key(F12, False))
    assert wait_for(lambda: round(device.get_brightness()) == 60)


# -- recording --------------------------------------------------------------

def test_fn_f9_records_a_macro_to_the_next_macro_key(rig):
    _, keys, device, fake, _, _ = rig
    fake.hold(FN)
    assert wait_for(lambda: any(b.fn for b in keys._bindings.values()))
    keys.handle_key(key(F9, True))
    keys.handle_key(key(F9, False))
    assert wait_for(lambda: keys._recording is not None)
    fake.hold(M2)
    fake.hold()
    assert wait_for(lambda: keys._recording.key == 'M2')
    keys.handle_key(key(A, True, t=100))
    keys.handle_key(key(A, False, t=180))
    fake.hold(FN)
    assert wait_for(lambda: any(b.fn for b in keys._bindings.values()))
    keys.handle_key(key(F9, True))
    keys.handle_key(key(F9, False))
    assert wait_for(lambda: keys.macros.get(device.serial, 'M2') is not None)
    events = keys.macros.get(device.serial, 'M2')['events']
    assert [(e['scan'], e['down']) for e in events] == [(A.scan, True), (A.scan, False)]
    assert events[1]['delay'] == 80


def test_recording_from_the_daemon_api(rig):
    service, keys, device, _, _, _ = rig
    service.rpc_macro_record_start(device.serial, 'M3')
    keys.handle_key(key(A, True, t=0))
    keys.handle_key(key(A, False, t=40))
    saved = service.rpc_macro_record_stop()
    assert saved['type'] == 'keys' and len(saved['events']) == 2
    assert 'M3' in service.rpc_macro_list(device.serial)['bindings']


def test_recording_nothing_clears_the_key(rig):
    service, _, device, _, _, _ = rig
    service.rpc_macro_set(device.serial, 'M4', {'type': 'text', 'text': 'x'})
    service.rpc_macro_record_start(device.serial, 'M4')
    assert service.rpc_macro_record_stop() is None
    assert 'M4' not in service.rpc_macro_list(device.serial)['bindings']


# -- the RPC surface and the store ------------------------------------------

def test_the_daemon_lists_a_keyboard_s_macro_keys(rig):
    service, _, device, _, _, _ = rig
    listed = service.rpc_macro_list(device.serial)
    assert listed['keys'] == ['M1', 'M2', 'M3', 'M4', 'M5']
    assert listed['handled'] is True
    status = service.rpc_keys_status()
    assert status['running'] and status['devices'][0]['handled']


def test_a_malformed_binding_is_refused(rig):
    service, _, device, _, _, _ = rig
    with pytest.raises(RpcError) as raised:
        service.rpc_macro_set(device.serial, 'M1', {'type': 'teleport'})
    assert raised.value.code == INVALID_PARAMS


def test_bindings_survive_a_restart(tmp_path):
    path = str(tmp_path / 'macros.json')
    MacroStore(path).set('SERIAL', 'M1', {'type': 'text', 'text': 'hi'})
    assert MacroStore(path).get('SERIAL', 'M1') == {'type': 'text', 'text': 'hi'}


@pytest.mark.parametrize('action', [
    None, {}, {'type': 'keys', 'events': []}, {'type': 'text', 'text': ''},
    {'type': 'keys', 'events': [{'vk': 0, 'scan': 0}]},
    {'type': 'keys', 'events': [{'vk': 0x41, 'delay': 10 ** 9}]},
    {'type': 'launch', 'target': '  '},
])
def test_validation_rejects_nonsense(action):
    with pytest.raises(MacroError):
        validate(action)


def test_written_sequences_press_and_release_like_a_hand():
    events = parse_sequence('ctrl+c')
    assert [(e['vk'], e['down']) for e in events] == [
        (0xA2, True), (0x43, True), (0x43, False), (0xA2, False)]
    with pytest.raises(ValueError):
        parse_sequence('ctrl+nonsense')
