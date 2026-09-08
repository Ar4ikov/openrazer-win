"""Named lighting profiles.

A profile is a snapshot of what the daemon would restore, so these tests cover
both the store itself and a round trip through a real device object -- one on
the HID bus and one over Bluetooth, since the whole point is that profiles are
not specific to the devices that forget things.
"""
from __future__ import annotations

import json

import pytest

from openrazer_win.core.profiles import (
    MAX_NAME_LENGTH, ProfileError, ProfileStore, apply_profile, clean_name,
    snapshot,
)

from .conftest import KEYBOARD_PID, make_device


@pytest.fixture
def store(tmp_path):
    return ProfileStore(str(tmp_path / 'profiles.json'))


def state(effect='static', colours=(255, 0, 0), brightness=80) -> dict:
    return {
        'device': {},
        'zones': {'backlight': {'effect': effect,
                                'colors': list(colours) + [0] * (9 - len(colours)),
                                'brightness': brightness,
                                'speed': 1, 'wave_dir': 1}},
    }


# -- the store --------------------------------------------------------------

def test_a_saved_profile_comes_back(store):
    store.put('SERIAL1', 'night', state(brightness=20))
    assert store.names('SERIAL1') == ['night']
    assert store.get('SERIAL1', 'night')['zones']['backlight']['brightness'] == 20


def test_profiles_are_per_device(store):
    store.put('SERIAL1', 'night', state())
    store.put('SERIAL2', 'day', state())
    assert store.names('SERIAL1') == ['night']
    assert store.names('SERIAL2') == ['day']


def test_saving_the_same_name_replaces_it(store):
    store.put('SERIAL1', 'night', state(brightness=20))
    store.put('SERIAL1', 'night', state(brightness=90))
    assert store.names('SERIAL1') == ['night']
    assert store.get('SERIAL1', 'night')['zones']['backlight']['brightness'] == 90


def test_names_are_matched_regardless_of_case(store):
    store.put('SERIAL1', 'Night', state())
    assert store.get('SERIAL1', 'NIGHT')['name'] == 'Night'
    store.delete('SERIAL1', 'night')
    assert store.names('SERIAL1') == []


def test_a_profile_can_be_deleted(store):
    store.put('SERIAL1', 'night', state())
    store.put('SERIAL1', 'day', state())
    store.delete('SERIAL1', 'night')
    assert store.names('SERIAL1') == ['day']


def test_deleting_something_absent_says_so(store):
    with pytest.raises(ProfileError):
        store.delete('SERIAL1', 'nope')


def test_the_number_of_profiles_is_capped(tmp_path):
    store = ProfileStore(str(tmp_path / 'p.json'), limit=3)
    for index in range(3):
        store.put('SERIAL1', 'p{0}'.format(index), state())
    with pytest.raises(ProfileError, match='already has 3'):
        store.put('SERIAL1', 'one too many', state())
    # ... but replacing an existing one is still fine at the cap.
    store.put('SERIAL1', 'p0', state(brightness=1))
    assert len(store.names('SERIAL1')) == 3


@pytest.mark.parametrize('name', ['', '   ', None, 'x' * (MAX_NAME_LENGTH + 1)])
def test_unusable_names_are_refused(name):
    with pytest.raises(ProfileError):
        clean_name(name)


def test_surrounding_whitespace_is_tidied():
    assert clean_name('  night   shift ') == 'night shift'


def test_the_store_survives_a_reload(store):
    store.put('SERIAL1', 'night', state(brightness=33))
    reloaded = ProfileStore(store.path)
    assert reloaded.get('SERIAL1', 'night')['zones']['backlight']['brightness'] == 33


def test_an_unreadable_store_does_not_break_startup(tmp_path):
    path = tmp_path / 'broken.json'
    path.write_text('{not json', encoding='utf-8')
    assert ProfileStore(str(path)).names('SERIAL1') == []


def test_the_stored_file_is_json_a_human_can_read(store):
    store.put('SERIAL1', 'night', state())
    with open(store.path, encoding='utf-8') as handle:
        assert 'night' in json.load(handle)['SERIAL1'][0]['name']


def test_a_returned_profile_is_a_copy(store):
    store.put('SERIAL1', 'night', state(brightness=50))
    fetched = store.get('SERIAL1', 'night')
    fetched['zones']['backlight']['brightness'] = 1
    assert store.get('SERIAL1', 'night')['zones']['backlight']['brightness'] == 50


# -- round trips through a device -------------------------------------------

def test_a_keyboard_profile_round_trips(persistence, store):
    """Profiles are not a Bluetooth feature; they work on the HID bus too."""
    device, fake = make_device(KEYBOARD_PID, persistence)
    try:
        _ = device.serial
        device.set_static(255, 0, 0)
        device.set_brightness(90)
        store.put(device.serial, 'red', snapshot(device, persistence))

        device.set_static(0, 0, 255)
        device.set_brightness(20)

        apply_profile(device, persistence, store.get(device.serial, 'red'))
        assert persistence.get(device.serial, 'backlight', 'colors')[:3] == [255, 0, 0]
        assert round(persistence.get(device.serial, 'backlight', 'brightness')) == 90
    finally:
        device.close()


def test_a_bluetooth_profile_round_trips_colour_and_brightness(headset, store):
    from openrazer_win.protocol import razer_ble

    device, transport = headset
    device.set_zone_colours([(255, 0, 0), (0, 0, 255)])
    device.set_brightness(100)
    store.put(device.serial, 'red-blue', snapshot(device, persistence=device.persistence))

    device.set_zone_colours([(0, 255, 0), (128, 0, 255)])
    device.set_brightness(40)
    transport.writes.clear()

    apply_profile(device, device.persistence,
                  store.get(device.serial, 'red-blue'))

    assert transport.colour_writes[-1] == razer_ble.colour_command(
        [(255, 0, 0), (0, 0, 255)])
    # Brightness has to come back too: loading a profile is asking for exactly
    # the state that was saved, and the device would otherwise keep the old one.
    assert razer_ble.brightness_command(0xFF) in transport.writes


def test_loading_a_profile_holds_the_colour(headset, store):
    from openrazer_win.protocol import razer_ble

    device, transport = headset
    device.set_zone_colours([(255, 0, 0), (0, 0, 255)])
    store.put(device.serial, 'red-blue', snapshot(device, device.persistence))
    device.set_none()
    apply_profile(device, device.persistence, store.get(device.serial, 'red-blue'))
    assert transport.held == razer_ble.colour_command([(255, 0, 0), (0, 0, 255)])


def test_a_snapshot_covers_every_zone_the_device_has(headset, store):
    device, _ = headset
    device.set_zone_colours([(255, 0, 0), (0, 0, 255)])
    taken = snapshot(device, device.persistence)
    assert set(taken['zones']) == set(device.capabilities()['zones'])
    # The Bluetooth colour lives at device level, not in a zone.
    assert 'zone_colours' in taken['device']


def test_a_name_in_any_language_stays_legible_in_the_file(store):
    store.put('SERIAL1', 'вечер', state())
    with open(store.path, encoding='utf-8') as handle:
        assert 'вечер' in handle.read()
    assert store.get('SERIAL1', 'вечер')['name'] == 'вечер'
