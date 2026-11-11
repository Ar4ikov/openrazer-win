"""The active profile: chosen once, kept up to date, and switched live.

Choosing a profile makes it the device's active one.  From then on every change
the daemon keeps is written into it as well, so nothing has to be saved by
hand; loading another puts back everything it carries -- lighting, the
keyswitch mode, DPI, polling rate and, on a keyboard, Snap Tap.
"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from openrazer_win.cli import _print_profiles
from openrazer_win.core.device import DeviceError
from openrazer_win.core.persistence import Persistence
from openrazer_win.core.profiles import ProfileStore
from openrazer_win.daemon.demo import build_demo_backend
from openrazer_win.daemon.protocol import RpcError
from openrazer_win.daemon.server import DaemonService
from openrazer_win.keys.snaptap import SnapTapSettings
from openrazer_win.keys.snaptap_host import SnapTapHost

from .test_snaptap import FakeHook, FakeWatch, RecordingInjector, wait_for

BLACKWIDOW = 0x0203
MOUSE = 0x0078
HUNTSMAN = 0x026B


def make_service(tmp_path, effects=False, product_ids=(BLACKWIDOW, MOUSE, HUNTSMAN)):
    host = SnapTapHost(SnapTapSettings(str(tmp_path / 'snaptap.json')),
                       injector=RecordingInjector(), hook_factory=FakeHook,
                       watch_factory=FakeWatch, foreground=lambda: 'notepad.exe',
                       is_down=lambda vk: False)
    return DaemonService(backend=build_demo_backend(product_ids), enable_effects=effects,
                         persistence=Persistence(str(tmp_path / 'p.json')),
                         profiles=ProfileStore(str(tmp_path / 'profiles.json')),
                         snaptap=host)


@pytest.fixture
def service(tmp_path):
    made = make_service(tmp_path)
    made.start()
    yield made
    made.stop()


def by_pid(service, pid):
    return next(d for d in service.manager.devices if d.info.pid == pid)


def active_rows(service, serial):
    return [row['name'] for row in service.rpc_profile_list(serial) if row['active']]


def stored(service, serial, name):
    """A profile as the file has it, without whether it is active."""
    profile = ProfileStore(service.profiles.path).get(serial, name)
    profile.pop('active', None)
    return profile


# -- which profile is active --------------------------------------------------

def test_the_profile_last_saved_or_loaded_is_the_active_one(service):
    serial = by_pid(service, HUNTSMAN).serial
    service.profiles.put(serial, 'Old', {'device': {}, 'zones': {}})
    assert active_rows(service, serial) == []
    service.rpc_profile_save(serial, 'Desktop')
    service.rpc_profile_save(serial, 'Gaming')
    assert active_rows(service, serial) == ['Gaming']
    service.rpc_profile_load(serial, 'desktop')
    assert active_rows(service, serial) == ['Desktop']
    service.rpc_profile_delete(serial, 'Desktop')
    assert active_rows(service, serial) == [] and service.profiles.active(serial) is None


def test_a_file_from_before_active_profiles_still_loads(tmp_path, service):
    keyboard = by_pid(service, HUNTSMAN)
    path = tmp_path / 'old.json'
    path.write_text(json.dumps({keyboard.serial: [{
        'name': 'Old', 'saved': '2026-01-01T00:00:00', 'device': {},
        'zones': {'backlight': {'effect': 'static', 'colors': [1, 2, 3, 0, 0, 0, 0, 0, 0],
                                'brightness': 50, 'speed': 1, 'wave_dir': 1}}}]}),
        encoding='utf-8')
    service.profiles = ProfileStore(str(path))
    assert active_rows(service, keyboard.serial) == []
    keyboard.set_keyswitch_optimization('gaming')
    service.rpc_profile_load(keyboard.serial, 'Old')
    # Nothing in it says what the keyswitch should be, so it is left alone.
    assert keyboard.get_keyswitch_optimization() == 'gaming'
    assert active_rows(service, keyboard.serial) == ['Old']


# -- changes land in the active profile ---------------------------------------

@pytest.mark.parametrize('change, check', [
    (lambda s, serial: s.rpc_call(serial, 'set_static', [0, 0, 255]),
     lambda p: p['zones']['backlight']['colors'][:3] == [0, 0, 255]),
    (lambda s, serial: s.rpc_effect(serial, 'wave_soft', {}),
     lambda p: p['device']['software']['effect'] == 'wave_soft'),
    (lambda s, serial: s.rpc_snaptap_set(enabled=True, acknowledged=True),
     lambda p: p['snaptap']['enabled'] is True),
], ids=['call', 'software effect', 'snap tap'])
def test_a_change_is_written_into_the_active_profile(tmp_path, change, check):
    service = make_service(tmp_path, effects=True)
    service.start()
    try:
        serial = by_pid(service, BLACKWIDOW).serial
        service.rpc_profile_save(serial, 'Desktop')
        change(service, serial)
        assert check(stored(service, serial, 'Desktop'))
    finally:
        service.stop()


def test_without_an_active_profile_nothing_is_written(service):
    keyboard = by_pid(service, HUNTSMAN)
    service.profiles.put(keyboard.serial, 'Desktop', {'device': {}, 'zones': {}})
    service.rpc_call(keyboard.serial, 'set_keyswitch_optimization', ['gaming'])
    assert 'keyswitch_optimization' not in stored(service, keyboard.serial, 'Desktop')['device']


def test_reading_or_reloading_does_not_rewrite_the_profile(service):
    serial = by_pid(service, HUNTSMAN).serial
    service.rpc_profile_save(serial, 'Desktop')
    before = open(service.profiles.path, encoding='utf-8').read()
    service.rpc_call(serial, 'get_keyswitch_optimization')
    service.rpc_profile_load(serial, 'Desktop')
    assert open(service.profiles.path, encoding='utf-8').read() == before


def test_a_change_does_not_ask_the_device_anything(service, monkeypatch):
    mouse = by_pid(service, MOUSE)
    service.rpc_profile_save(mouse.serial, 'Desktop')
    asked = []
    monkeypatch.setattr(mouse, 'get_poll_rate', lambda: asked.append(1))
    for level in (10, 20, 30):
        service.rpc_call(mouse.serial, 'set_brightness', [level])
    assert asked == []


def test_a_change_made_during_a_switch_stays_out_of_the_profile_left(service, monkeypatch):
    keyboard = by_pid(service, HUNTSMAN)
    serial = keyboard.serial
    service.rpc_call(serial, 'set_static', [255, 0, 0])
    service.rpc_profile_save(serial, 'Red')
    service.rpc_call(serial, 'set_static', [0, 0, 255])
    service.rpc_profile_save(serial, 'Blue')
    service.rpc_profile_load(serial, 'Red')
    red = stored(service, serial, 'Red')
    switching, finish = threading.Event(), threading.Event()
    restore = keyboard.restore

    def slow_restore():
        # Blue's lighting is in persistence by now, and Red is still active.
        switching.set()
        finish.wait(2)
        restore()

    monkeypatch.setattr(keyboard, 'restore', slow_restore)
    load = threading.Thread(target=service.rpc_profile_load, args=(serial, 'Blue'))
    load.start()
    assert switching.wait(2)
    change = threading.Thread(target=service.rpc_call, args=(serial, 'set_brightness', [10]))
    change.start()
    # Made on the device; now waiting to be kept, until the switch is over.
    assert wait_for(lambda: service.persistence.zone(serial, 'backlight')['brightness'] == 10)
    finish.set()
    load.join(2)
    change.join(2)
    assert stored(service, serial, 'Red') == red
    assert stored(service, serial, 'Blue')['zones']['backlight']['brightness'] == 10


# -- switching profiles switches the device -----------------------------------

@pytest.mark.parametrize('pid, method, first, second, read', [
    (HUNTSMAN, 'set_keyswitch_optimization', ['typing'], ['gaming'],
     lambda d: [d.get_keyswitch_optimization()]),
    (MOUSE, 'set_dpi', [800, 800], [1600, 1600], lambda d: list(d.get_dpi())),
    (MOUSE, 'set_poll_rate', [500], [1000], lambda d: [d.get_poll_rate()]),
], ids=['keyswitch', 'dpi', 'polling rate'])
def test_loading_a_profile_switches_the_device(service, pid, method, first, second, read):
    device = by_pid(service, pid)
    service.rpc_call(device.serial, method, first)
    service.rpc_profile_save(device.serial, 'Desktop')
    service.rpc_profile_save(device.serial, 'Gaming')
    service.rpc_call(device.serial, method, second)
    service.rpc_profile_load(device.serial, 'Desktop')
    assert read(device) == first
    service.rpc_profile_load(device.serial, 'Gaming')
    assert read(device) == second


@pytest.mark.parametrize('action', ['save', 'create'])
def test_a_profile_does_not_take_settings_from_the_one_before(tmp_path, action):
    service = make_service(tmp_path, product_ids=(HUNTSMAN,))
    # Already at 1000 Hz and in Typing, before anything here changes them.
    service.manager.backend.devices[0].state.poll_rate = 1000
    service.start()
    try:
        keyboard = service.manager.devices[0]
        serial = keyboard.serial
        getattr(service, 'rpc_profile_' + action)(serial, 'Desktop')
        service.rpc_profile_create(serial, 'Gaming')
        service.rpc_call(serial, 'set_poll_rate', [8000])
        service.rpc_call(serial, 'set_keyswitch_optimization', ['gaming'])
        service.rpc_profile_load(serial, 'Desktop')
        assert (keyboard.get_poll_rate(), keyboard.get_keyswitch_optimization()) == (1000, 'typing')
        service.rpc_call(serial, 'set_brightness', [10])
        device = stored(service, serial, 'Desktop')['device']
        assert (device['poll_rate'], device['keyswitch_optimization']) == (1000, 'typing')
    finally:
        service.stop()


def test_switching_sets_the_polling_rate_after_the_lighting(service, monkeypatch):
    keyboard = by_pid(service, HUNTSMAN)
    serial = keyboard.serial
    service.rpc_call(serial, 'set_poll_rate', [1000])
    service.rpc_profile_save(serial, 'Desktop')
    service.rpc_profile_create(serial, 'Gaming')
    service.rpc_call(serial, 'set_poll_rate', [8000])
    fake = next(d for d in service.manager.backend.devices if d.state.serial == serial)
    restore = keyboard.restore

    def restore_with_reset():
        # As if replaying the lighting put the keyboard back at another rate.
        restore()
        fake.state.poll_rate = 8000

    monkeypatch.setattr(keyboard, 'restore', restore_with_reset)
    service.rpc_profile_load(serial, 'Desktop')
    assert keyboard.get_poll_rate() == 1000


def test_a_load_that_fails_after_writing_state_still_moves_the_active_profile(service,
                                                                           monkeypatch):
    keyboard = by_pid(service, HUNTSMAN)
    service.rpc_call(keyboard.serial, 'set_static', [255, 0, 0])
    service.rpc_profile_save(keyboard.serial, 'Red')
    service.rpc_profile_save(keyboard.serial, 'Blue')
    service.rpc_call(keyboard.serial, 'set_static', [0, 0, 255])
    service.rpc_profile_load(keyboard.serial, 'Red')
    red = stored(service, keyboard.serial, 'Red')

    def broken():
        raise DeviceError('unplugged half way')

    monkeypatch.setattr(keyboard, 'restore', broken)
    with pytest.raises(RpcError):
        service.rpc_profile_load(keyboard.serial, 'Blue')
    # Persistence now holds Blue's lighting; it must not be kept as Red.
    assert service.profiles.active(keyboard.serial) == 'Blue'
    monkeypatch.undo()
    service.rpc_call(keyboard.serial, 'set_brightness', [10])
    assert stored(service, keyboard.serial, 'Red') == red


# -- Snap Tap goes with the keyboard's profile --------------------------------

def test_snap_tap_is_saved_with_a_keyboard_profile_and_comes_back(service):
    keyboard = by_pid(service, HUNTSMAN)
    service.rpc_profile_save(keyboard.serial, 'Desktop')
    service.rpc_profile_save(keyboard.serial, 'Gaming')
    # Gaming is the active profile now, so this goes into it alone.
    service.rpc_snaptap_set(enabled=True, acknowledged=True,
                            pairs=[['KEY_A', 'KEY_D'], ['KEY_W', 'KEY_S']])

    service.rpc_profile_load(keyboard.serial, 'Desktop')
    status = service.rpc_snaptap_status()
    assert (status['enabled'], status['pairs']) == (False, [['KEY_A', 'KEY_D']])
    service.rpc_profile_load(keyboard.serial, 'Gaming')
    status = service.rpc_snaptap_status()
    assert (status['enabled'], status['pairs']) == (True, [['KEY_A', 'KEY_D'], ['KEY_W', 'KEY_S']])
    # A mouse's profiles have no Snap Tap of their own.
    service.rpc_profile_save(by_pid(service, MOUSE).serial, 'Mouse')
    assert 'snaptap' not in stored(service, by_pid(service, MOUSE).serial, 'Mouse')


def test_a_profile_never_turns_snap_tap_on_without_the_warning_acknowledged(service):
    keyboard = by_pid(service, HUNTSMAN)
    service.profiles.put(keyboard.serial, 'Imported', {
        'device': {}, 'zones': {},
        'snaptap': {'enabled': True, 'pairs': [['KEY_W', 'KEY_S']]}})
    service.rpc_profile_load(keyboard.serial, 'Imported')
    status = service.rpc_snaptap_status()
    assert status['enabled'] is False and status['acknowledged'] is False
    # The pairs are the user's own and harmless while it is off.
    assert status['pairs'] == [['KEY_W', 'KEY_S']]


# -- new profiles and copies --------------------------------------------------

def test_a_new_profile_starts_from_the_defaults_not_the_active_one(service):
    keyboard = by_pid(service, HUNTSMAN)
    service.rpc_call(keyboard.serial, 'set_static', [0, 0, 255])
    service.rpc_profile_save(keyboard.serial, 'Gaming')
    service.rpc_call(keyboard.serial, 'set_keyswitch_optimization', ['gaming'])
    service.rpc_snaptap_set(enabled=True, acknowledged=True,
                            pairs=[['KEY_A', 'KEY_D'], ['KEY_W', 'KEY_S']])

    service.rpc_profile_create(keyboard.serial, 'Desktop')

    created = stored(service, keyboard.serial, 'Desktop')
    assert created['device']['keyswitch_optimization'] == 'typing'
    assert created['zones']['backlight']['effect'] == 'spectrum'
    assert created['snaptap'] == {'enabled': False, 'pairs': [['KEY_A', 'KEY_D']]}
    # ... and it is put on the device at once, as the active profile.
    assert keyboard.get_keyswitch_optimization() == 'typing'
    assert service.rpc_snaptap_status()['enabled'] is False
    assert active_rows(service, keyboard.serial) == ['Desktop']
    assert stored(service, keyboard.serial, 'Gaming')['device']['keyswitch_optimization'] == 'gaming'


def test_a_new_profile_keeps_the_current_dpi_and_polling_rate(service):
    mouse = by_pid(service, MOUSE)
    service.rpc_call(mouse.serial, 'set_dpi', [1600, 1600])
    service.rpc_profile_create(mouse.serial, 'Fresh')
    created = stored(service, mouse.serial, 'Fresh')
    assert created['device']['dpi'] == [1600, 1600]
    assert created['device']['poll_rate'] == mouse.get_poll_rate()
    assert tuple(mouse.get_dpi()) == (1600, 1600)


@pytest.mark.parametrize('name, source, refusal', [
    ('desktop', None, 'already'),
    ('Copy', 'Nope', 'no profile'),
])
def test_creating_refuses_what_it_cannot_do(service, name, source, refusal):
    serial = by_pid(service, HUNTSMAN).serial
    service.rpc_profile_save(serial, 'Desktop')
    with pytest.raises(RpcError, match=refusal):
        service.rpc_profile_create(serial, name, source=source)


def test_a_duplicate_copies_its_source_and_leaves_it_alone(service):
    keyboard = by_pid(service, HUNTSMAN)
    service.rpc_call(keyboard.serial, 'set_keyswitch_optimization', ['gaming'])
    service.rpc_snaptap_set(enabled=True, acknowledged=True)
    service.rpc_profile_save(keyboard.serial, 'Gaming')
    source = stored(service, keyboard.serial, 'Gaming')

    service.rpc_profile_create(keyboard.serial, 'Gaming 2', source='gaming')
    copy = stored(service, keyboard.serial, 'Gaming 2')
    assert [copy[key] for key in ('device', 'zones', 'snaptap')] == [
        source[key] for key in ('device', 'zones', 'snaptap')]
    assert active_rows(service, keyboard.serial) == ['Gaming 2']

    # Changes now go into the copy only.
    service.rpc_call(keyboard.serial, 'set_keyswitch_optimization', ['typing'])
    assert stored(service, keyboard.serial, 'Gaming') == source


def test_the_cli_list_marks_the_active_profile(capsys):
    device = SimpleNamespace(name='Huntsman', profiles=[
        {'name': 'Desktop', 'saved': '2026-01-01T00:00:00', 'zones': ['backlight'],
         'active': False},
        {'name': 'Gaming', 'saved': '2026-01-01T00:00:00', 'zones': ['backlight'],
         'active': True}])
    _print_profiles(device, SimpleNamespace(json=False))
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].startswith('    Desktop')
    assert lines[2].startswith('  * Gaming')
