"""The active profile: chosen once, kept up to date, and switched live.

Choosing a profile makes it the device's active one.  From then on every change
the daemon keeps is written into it as well, so nothing has to be saved by
hand; loading another puts back everything it carries -- lighting, the
keyswitch mode, DPI, polling rate and, on a keyboard, Snap Tap.
"""
from __future__ import annotations

import json

import pytest

from openrazer_win.core.persistence import Persistence
from openrazer_win.core.profiles import ProfileStore
from openrazer_win.daemon.demo import build_demo_backend
from openrazer_win.daemon.server import DaemonService
from openrazer_win.keys.snaptap import SnapTapSettings
from openrazer_win.keys.snaptap_host import SnapTapHost

from .test_snaptap import FakeWatch, RecordingInjector, SyncHook

MOUSE = 0x0078
HUNTSMAN_V2_TKL = 0x026B


def make_service(tmp_path, effects=False, product_ids=None):
    def factory(on_event):
        hook = SyncHook(on_event)
        hook.start()
        return hook

    host = SnapTapHost(SnapTapSettings(str(tmp_path / 'snaptap.json')),
                       injector=RecordingInjector(), hook_factory=factory,
                       watch_factory=FakeWatch, foreground=lambda: 'notepad.exe',
                       is_down=lambda vk: False)
    backend = build_demo_backend() if product_ids is None else build_demo_backend(product_ids)
    return DaemonService(backend=backend, enable_effects=effects,
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


def held(profile):
    """What a profile holds, whichever profile is active."""
    return {key: value for key, value in profile.items() if key != 'active'}


def active_rows(service, serial):
    return [row['name'] for row in service.rpc_profile_list(serial) if row['active']]


# -- which profile is active --------------------------------------------------

def test_nothing_is_active_until_a_profile_is_chosen(service):
    keyboard = by_pid(service, HUNTSMAN_V2_TKL)
    service.profiles.put(keyboard.serial, 'Desktop', {'device': {}, 'zones': {}})
    assert service.rpc_profile_list(keyboard.serial)[0]['active'] is False


def test_saving_a_profile_makes_it_the_active_one(service):
    keyboard = by_pid(service, HUNTSMAN_V2_TKL)
    service.rpc_profile_save(keyboard.serial, 'Desktop')
    service.rpc_profile_save(keyboard.serial, 'Gaming')
    assert active_rows(service, keyboard.serial) == ['Gaming']


def test_loading_a_profile_makes_it_the_active_one(service):
    keyboard = by_pid(service, HUNTSMAN_V2_TKL)
    service.rpc_profile_save(keyboard.serial, 'Desktop')
    service.rpc_profile_save(keyboard.serial, 'Gaming')
    service.rpc_profile_load(keyboard.serial, 'desktop')
    assert active_rows(service, keyboard.serial) == ['Desktop']


def test_deleting_the_active_profile_leaves_none_active(service):
    keyboard = by_pid(service, HUNTSMAN_V2_TKL)
    service.rpc_profile_save(keyboard.serial, 'Desktop')
    service.rpc_profile_save(keyboard.serial, 'Gaming')
    service.rpc_profile_delete(keyboard.serial, 'Gaming')
    assert active_rows(service, keyboard.serial) == []
    assert service.profiles.active(keyboard.serial) is None


def test_the_active_profile_survives_a_restart(tmp_path):
    store = ProfileStore(str(tmp_path / 'profiles.json'))
    store.put('SERIAL1', 'Desktop', {'device': {}, 'zones': {}})
    store.put('SERIAL1', 'Gaming', {'device': {}, 'zones': {}})
    store.activate('SERIAL1', 'gaming')
    assert ProfileStore(store.path).active('SERIAL1') == 'Gaming'


def test_the_active_profile_is_per_device(tmp_path):
    store = ProfileStore(str(tmp_path / 'profiles.json'))
    store.put('SERIAL1', 'Desktop', {'device': {}, 'zones': {}})
    store.put('SERIAL2', 'Gaming', {'device': {}, 'zones': {}})
    store.activate('SERIAL1', 'Desktop')
    assert store.active('SERIAL2') is None


def test_a_file_from_before_active_profiles_still_loads(tmp_path, service):
    keyboard = by_pid(service, HUNTSMAN_V2_TKL)
    path = tmp_path / 'old.json'
    path.write_text(json.dumps({keyboard.serial: [{
        'name': 'Old', 'saved': '2026-01-01T00:00:00', 'device': {},
        'zones': {'backlight': {'effect': 'static', 'colors': [1, 2, 3, 0, 0, 0, 0, 0, 0],
                                'brightness': 50, 'speed': 1, 'wave_dir': 1}}}]}),
        encoding='utf-8')
    service.profiles = ProfileStore(str(path))
    assert service.rpc_profile_list(keyboard.serial)[0]['active'] is False
    keyboard.set_keyswitch_optimization('gaming')
    service.rpc_profile_load(keyboard.serial, 'Old')
    # Nothing in it says what the keyswitch should be, so it is left alone.
    assert keyboard.get_keyswitch_optimization() == 'gaming'
    assert active_rows(service, keyboard.serial) == ['Old']


# -- changes land in the active profile ---------------------------------------

def test_a_change_is_written_into_the_active_profile(service):
    keyboard = by_pid(service, HUNTSMAN_V2_TKL)
    service.rpc_call(keyboard.serial, 'set_keyswitch_optimization', ['typing'])
    service.rpc_profile_save(keyboard.serial, 'Desktop')
    service.rpc_call(keyboard.serial, 'set_keyswitch_optimization', ['gaming'])
    service.rpc_call(keyboard.serial, 'set_static', [0, 0, 255])
    stored = ProfileStore(service.profiles.path).get(keyboard.serial, 'Desktop')
    assert stored['device']['keyswitch_optimization'] == 'gaming'
    assert stored['zones']['backlight']['colors'][:3] == [0, 0, 255]


def test_without_an_active_profile_nothing_is_written(service):
    keyboard = by_pid(service, HUNTSMAN_V2_TKL)
    service.profiles.put(keyboard.serial, 'Desktop', {'device': {}, 'zones': {}})
    service.rpc_call(keyboard.serial, 'set_keyswitch_optimization', ['gaming'])
    assert 'keyswitch_optimization' not in service.profiles.get(keyboard.serial, 'Desktop')['device']


def test_reading_something_does_not_rewrite_the_profile(service):
    keyboard = by_pid(service, HUNTSMAN_V2_TKL)
    service.rpc_profile_save(keyboard.serial, 'Desktop')
    with open(service.profiles.path, encoding='utf-8') as handle:
        before = handle.read()
    service.rpc_call(keyboard.serial, 'get_keyswitch_optimization')
    with open(service.profiles.path, encoding='utf-8') as handle:
        assert handle.read() == before


def test_loading_a_profile_does_not_rewrite_it(service):
    keyboard = by_pid(service, HUNTSMAN_V2_TKL)
    service.rpc_profile_save(keyboard.serial, 'Desktop')
    saved = service.profiles.get(keyboard.serial, 'Desktop')
    service.rpc_profile_load(keyboard.serial, 'Desktop')
    assert service.profiles.get(keyboard.serial, 'Desktop') == saved


def test_a_software_effect_is_written_into_the_active_profile(tmp_path):
    service = make_service(tmp_path, effects=True)
    service.start()
    try:
        keyboard = by_pid(service, 0x0203)
        service.rpc_profile_save(keyboard.serial, 'Party')
        service.rpc_effect(keyboard.serial, 'wave_soft', {})
        stored = service.profiles.get(keyboard.serial, 'Party')
        assert stored['device']['software']['effect'] == 'wave_soft'
    finally:
        service.stop()


# -- switching profiles switches the device -----------------------------------

def test_loading_a_profile_switches_the_keyswitch_mode(service):
    keyboard = by_pid(service, HUNTSMAN_V2_TKL)
    service.rpc_call(keyboard.serial, 'set_keyswitch_optimization', ['typing'])
    service.rpc_profile_save(keyboard.serial, 'Desktop')
    service.rpc_profile_save(keyboard.serial, 'Gaming')
    service.rpc_call(keyboard.serial, 'set_keyswitch_optimization', ['gaming'])

    service.rpc_profile_load(keyboard.serial, 'Desktop')
    assert keyboard.get_keyswitch_optimization() == 'typing'
    service.rpc_profile_load(keyboard.serial, 'Gaming')
    assert keyboard.get_keyswitch_optimization() == 'gaming'


def test_loading_a_profile_switches_dpi_and_polling_rate(service):
    mouse = by_pid(service, MOUSE)
    service.rpc_call(mouse.serial, 'set_dpi', [800, 800])
    service.rpc_call(mouse.serial, 'set_poll_rate', [500])
    service.rpc_profile_save(mouse.serial, 'Desktop')
    service.rpc_profile_save(mouse.serial, 'Gaming')
    service.rpc_call(mouse.serial, 'set_dpi', [1600, 1600])
    service.rpc_call(mouse.serial, 'set_poll_rate', [1000])

    service.rpc_profile_load(mouse.serial, 'Desktop')
    assert tuple(mouse.get_dpi()) == (800, 800)
    assert mouse.get_poll_rate() == 500


@pytest.mark.parametrize('pid', [0x026B, 0x026C], ids=['tenkeyless', 'full-size'])
@pytest.mark.parametrize('action', ['save', 'create'])
def test_switching_back_restores_a_keyboards_initial_polling_rate(tmp_path, pid, action):
    service = make_service(tmp_path, product_ids=(pid,))
    # The keyboard already runs at 1000 Hz, before Studio changes anything.
    service.manager.backend.devices[0].state.poll_rate = 1000
    service.start()
    try:
        keyboard = by_pid(service, pid)
        serial = keyboard.serial
        assert service.persistence.get_device_value(serial, 'poll_rate') is None
        create = getattr(service, 'rpc_profile_' + action)
        create(serial, 'Desktop')
        service.rpc_profile_create(serial, 'Gaming')
        service.rpc_call(serial, 'set_poll_rate', [8000])

        # Every edit belongs to the active profile, without saving or applying.
        stored = ProfileStore(service.profiles.path)
        assert stored.get(serial, 'Desktop')['device']['poll_rate'] == 1000
        assert stored.get(serial, 'Gaming')['device']['poll_rate'] == 8000
        for name, expected in [('Desktop', 1000), ('Gaming', 8000), ('Desktop', 1000)]:
            service.rpc_profile_load(serial, name)
            assert service.rpc_call(serial, 'get_poll_rate') == expected
            assert service.persistence.get_device_value(serial, 'poll_rate') == expected

        # Editing Desktop is saved automatically and does not change Gaming.
        service.rpc_call(serial, 'set_poll_rate', [500])
        service.rpc_profile_load(serial, 'Gaming')
        assert keyboard.get_poll_rate() == 8000
        service.rpc_profile_load(serial, 'Desktop')
        assert keyboard.get_poll_rate() == 500
        assert ProfileStore(service.profiles.path).get(serial, 'Desktop')['device']['poll_rate'] == 500
    finally:
        service.stop()


def test_a_mouse_profile_carries_no_snap_tap(service):
    mouse = by_pid(service, MOUSE)
    service.rpc_profile_save(mouse.serial, 'Desktop')
    assert 'snaptap' not in service.profiles.get(mouse.serial, 'Desktop')


# -- Snap Tap goes with the keyboard's profile --------------------------------

def test_snap_tap_is_saved_with_a_keyboard_profile_and_comes_back(service):
    keyboard = by_pid(service, HUNTSMAN_V2_TKL)
    service.rpc_profile_save(keyboard.serial, 'Desktop')
    service.rpc_profile_save(keyboard.serial, 'Gaming')
    # Gaming is the active profile now, so this goes into it alone.
    service.rpc_snaptap_set(enabled=True, acknowledged=True,
                            pairs=[['KEY_A', 'KEY_D'], ['KEY_W', 'KEY_S']])

    service.rpc_profile_load(keyboard.serial, 'Gaming')
    status = service.rpc_snaptap_status()
    assert status['enabled'] is True
    assert status['pairs'] == [['KEY_A', 'KEY_D'], ['KEY_W', 'KEY_S']]

    service.rpc_profile_load(keyboard.serial, 'Desktop')
    status = service.rpc_snaptap_status()
    assert status['enabled'] is False
    assert status['pairs'] == [['KEY_A', 'KEY_D']]


def test_a_snap_tap_change_is_written_into_the_active_keyboard_profile(service):
    keyboard = by_pid(service, HUNTSMAN_V2_TKL)
    service.rpc_profile_save(keyboard.serial, 'Gaming')
    service.rpc_snaptap_set(enabled=True, acknowledged=True)
    assert service.profiles.get(keyboard.serial, 'Gaming')['snaptap']['enabled'] is True


def test_a_profile_never_turns_snap_tap_on_without_the_warning_acknowledged(service):
    keyboard = by_pid(service, HUNTSMAN_V2_TKL)
    service.profiles.put(keyboard.serial, 'Imported', {
        'device': {}, 'zones': {},
        'snaptap': {'enabled': True, 'pairs': [['KEY_W', 'KEY_S']]}})
    service.rpc_profile_load(keyboard.serial, 'Imported')
    status = service.rpc_snaptap_status()
    assert status['enabled'] is False and status['acknowledged'] is False
    # The pairs are the user's own and harmless while it is off.
    assert status['pairs'] == [['KEY_W', 'KEY_S']]


# -- a new profile starts from the defaults -----------------------------------

def test_a_new_profile_starts_from_the_defaults_not_the_active_one(service):
    keyboard = by_pid(service, HUNTSMAN_V2_TKL)
    service.rpc_call(keyboard.serial, 'set_static', [0, 0, 255])
    service.rpc_profile_save(keyboard.serial, 'Gaming')
    service.rpc_call(keyboard.serial, 'set_keyswitch_optimization', ['gaming'])
    service.rpc_snaptap_set(enabled=True, acknowledged=True,
                            pairs=[['KEY_A', 'KEY_D'], ['KEY_W', 'KEY_S']])

    service.rpc_profile_create(keyboard.serial, 'Desktop')

    created = service.profiles.get(keyboard.serial, 'Desktop')
    assert created['device']['keyswitch_optimization'] == 'typing'
    assert created['zones']['backlight']['effect'] == 'spectrum'
    assert created['snaptap'] == {'enabled': False, 'pairs': [['KEY_A', 'KEY_D']]}
    # ... and it is put on the device at once, as the active profile.
    assert keyboard.get_keyswitch_optimization() == 'typing'
    assert service.rpc_snaptap_status()['enabled'] is False
    assert active_rows(service, keyboard.serial) == ['Desktop']
    assert service.profiles.get(keyboard.serial, 'Gaming')['device'][
        'keyswitch_optimization'] == 'gaming'


def test_a_new_profile_keeps_the_current_polling_rate_without_resetting_dpi(service):
    mouse = by_pid(service, MOUSE)
    service.rpc_call(mouse.serial, 'set_dpi', [1600, 1600])
    service.rpc_profile_create(mouse.serial, 'Fresh')
    created = service.profiles.get(mouse.serial, 'Fresh')
    assert not {'dpi', 'dpi_stages'} & set(created['device'])
    assert created['device']['poll_rate'] == mouse.get_poll_rate()
    assert tuple(mouse.get_dpi()) == (1600, 1600)


def test_creating_does_not_replace_a_profile_of_that_name(service):
    from openrazer_win.daemon.protocol import RpcError
    keyboard = by_pid(service, HUNTSMAN_V2_TKL)
    service.rpc_profile_save(keyboard.serial, 'Desktop')
    with pytest.raises(RpcError, match='already'):
        service.rpc_profile_create(keyboard.serial, 'desktop')


# -- duplicating ----------------------------------------------------------------

def test_a_duplicate_copies_its_source_and_leaves_it_alone(service):
    keyboard = by_pid(service, HUNTSMAN_V2_TKL)
    service.rpc_call(keyboard.serial, 'set_keyswitch_optimization', ['gaming'])
    service.rpc_snaptap_set(enabled=True, acknowledged=True)
    service.rpc_profile_save(keyboard.serial, 'Gaming')
    source = service.profiles.get(keyboard.serial, 'Gaming')

    service.rpc_profile_create(keyboard.serial, 'Gaming 2', source='gaming')
    copy = service.profiles.get(keyboard.serial, 'Gaming 2')
    assert copy['device'] == source['device'] and copy['zones'] == source['zones']
    assert copy['snaptap'] == source['snaptap']
    assert active_rows(service, keyboard.serial) == ['Gaming 2']

    # Changes now go into the copy only.
    service.rpc_call(keyboard.serial, 'set_keyswitch_optimization', ['typing'])
    assert held(service.profiles.get(keyboard.serial, 'Gaming')) == held(source)


def test_duplicating_something_absent_says_so(service):
    from openrazer_win.daemon.protocol import RpcError
    keyboard = by_pid(service, HUNTSMAN_V2_TKL)
    with pytest.raises(RpcError, match='no profile'):
        service.rpc_profile_create(keyboard.serial, 'Copy', source='Nope')


def test_profile_create_is_served_over_rpc(tmp_path):
    from openrazer_win.daemon.server import DaemonServer
    server = DaemonServer(make_service(tmp_path), port=0)
    try:
        assert 'profile.create' in server.methods
    finally:
        server.server_close()


# -- a load that fails half way -------------------------------------------------

def test_a_load_that_fails_after_writing_state_still_moves_the_active_profile(service,
                                                                           monkeypatch):
    from openrazer_win.core.device import DeviceError
    from openrazer_win.daemon.protocol import RpcError
    keyboard = by_pid(service, HUNTSMAN_V2_TKL)
    service.rpc_call(keyboard.serial, 'set_static', [255, 0, 0])
    service.rpc_profile_save(keyboard.serial, 'Red')
    service.rpc_profile_save(keyboard.serial, 'Blue')
    service.rpc_call(keyboard.serial, 'set_static', [0, 0, 255])
    service.rpc_profile_load(keyboard.serial, 'Red')
    red = service.profiles.get(keyboard.serial, 'Red')

    def broken():
        raise DeviceError('unplugged half way')

    monkeypatch.setattr(keyboard, 'restore', broken)
    with pytest.raises(RpcError):
        service.rpc_profile_load(keyboard.serial, 'Blue')
    # Persistence now holds Blue's lighting; it must not be kept as Red.
    assert service.profiles.active(keyboard.serial) == 'Blue'
    monkeypatch.undo()
    service.rpc_call(keyboard.serial, 'set_brightness', [10])
    assert held(service.profiles.get(keyboard.serial, 'Red')) == held(red)


# -- the store is written in order ----------------------------------------------

def test_the_store_holds_its_lock_while_the_file_is_replaced(tmp_path, monkeypatch):
    import threading

    from openrazer_win.core import profiles as module

    store = ProfileStore(str(tmp_path / 'profiles.json'))
    seen = []
    real = module.os.replace

    def replace(source, target):
        # Another thread must not get in between the copy and the replace.
        probe = threading.Thread(target=lambda: seen.append(store._lock.acquire(blocking=False)))
        probe.start()
        probe.join()
        real(source, target)

    monkeypatch.setattr(module.os, 'replace', replace)
    store.put('SERIAL1', 'Desktop', {'device': {}, 'zones': {}})
    assert seen == [False]


# -- the command line -------------------------------------------------------------

def test_the_cli_help_says_a_saved_profile_becomes_active():
    from openrazer_win.cli import PROFILE_HELP
    assert 'active' in PROFILE_HELP


def test_the_cli_list_marks_the_active_profile(capsys):
    from types import SimpleNamespace

    from openrazer_win.cli import _print_profiles
    device = SimpleNamespace(name='Huntsman', profiles=[
        {'name': 'Desktop', 'saved': '2026-01-01T00:00:00', 'zones': ['backlight'],
         'active': False},
        {'name': 'Gaming', 'saved': '2026-01-01T00:00:00', 'zones': ['backlight'],
         'active': True}])
    _print_profiles(device, SimpleNamespace(json=False))
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].startswith('    Desktop')
    assert lines[2].startswith('  * Gaming')
