"""The Bluetooth LE path: protocol, device class, discovery and the CLI.

Nothing here touches a radio.  The protocol expectations are the bytes Razer
Synapse was captured sending -- see ``openrazer_win/protocol/razer_ble.py`` --
so a change that stops matching the hardware fails here.
"""
from __future__ import annotations

import threading
import time

import pytest

from openrazer_win.core.ble_device import AGGREGATE_ZONE, ZONE_NAMES, BleDevice
from openrazer_win.core.device import DeviceError
from openrazer_win.core.manager import DeviceManager
from openrazer_win.devices import get_database
from openrazer_win.devices.recipes import NotSupported
from openrazer_win.ble import Advertiser, BleUnavailable  # noqa: F401
from openrazer_win.hid.fake import FakeHidBackend
from openrazer_win.protocol import razer_ble

from .conftest import (
    ADDRESS, ADVERT, BLUE, CLASSIC_ADDRESS, KITTY_V2_BT, MANUFACTURER_DATA, RED,
    FakeBleTransport,
)

@pytest.fixture
def fake_radio(monkeypatch, no_bluetooth_radio):
    """Make discovery see one advertising headset, without a radio."""
    ble = no_bluetooth_radio
    built: list = []

    def build(address, *args, **kwargs):
        transport = FakeBleTransport(address)
        built.append(transport)
        return transport

    monkeypatch.setattr(ble, 'is_available', lambda: True)
    monkeypatch.setattr(ble, 'scan', lambda *a, **k: [ADVERT])
    monkeypatch.setattr(ble, 'BleTransport', build)
    return built


# -- protocol ---------------------------------------------------------------

def test_the_colour_command_is_a_header_and_one_triple_per_zone():
    assert razer_ble.colour_command([RED, BLUE]) == bytes(
        (0xC4, 0x00, 0x06, 0xFF, 0x00, 0x00, 0x00, 0x00, 0xFF))


def test_the_first_triple_is_the_left_ear():
    # Verified on the hardware: red then blue lit the left ear red.
    assert ZONE_NAMES == ('left', 'right')
    payload = razer_ble.colour_command([RED, BLUE])
    left = payload[3:6]
    assert left == bytes(RED)


@pytest.mark.parametrize('colour, expected', [
    (RED, bytes((0xFF, 0x00, 0x00))),
    ((0, 255, 0), bytes((0x00, 0xFF, 0x00))),
    (BLUE, bytes((0x00, 0x00, 0xFF))),
])
def test_one_colour_is_repeated_across_both_zones(colour, expected):
    """Exactly what Synapse sent when a single colour was picked."""
    payload = razer_ble.colour_command([colour])
    assert payload == bytes((0xC4, 0x00, 0x06)) + expected * 2


def test_channels_are_clamped():
    assert razer_ble.colour_command([(300, -5, 12)]) == bytes(
        (0xC4, 0x00, 0x06, 0xFF, 0x00, 0x0C, 0xFF, 0x00, 0x0C))


def test_off_is_every_zone_black():
    assert razer_ble.off_command() == bytes((0xC4, 0x00, 0x06)) + bytes(6)


@pytest.mark.parametrize('colours', [[], [RED, BLUE, RED]])
def test_a_wrong_number_of_colours_is_rejected(colours):
    with pytest.raises(ValueError):
        razer_ble.colour_command(colours)


def test_the_headset_advertises_a_name_we_recognise():
    assert razer_ble.ADVERTISED_NAMES['Razer Stereo'] == (KITTY_V2_BT,)


def test_the_product_id_is_read_out_of_the_manufacturer_data():
    """Razer puts the product id in the advertisement, first two bytes."""
    assert razer_ble.product_id_from_advertisement(
        MANUFACTURER_DATA) == KITTY_V2_BT


def test_the_classic_address_is_read_out_of_the_manufacturer_data():
    assert razer_ble.classic_address_from_advertisement(
        MANUFACTURER_DATA) == CLASSIC_ADDRESS


@pytest.mark.parametrize('data', [b'', bytes((0x05,)), None])
def test_a_short_advertisement_yields_nothing_rather_than_raising(data):
    assert razer_ble.product_id_from_advertisement(data) is None
    assert razer_ble.classic_address_from_advertisement(data) is None


# -- the device class -------------------------------------------------------

def test_a_static_colour_lights_both_ears(headset):
    device, transport = headset
    device.set_static(*RED)
    assert transport.last == razer_ble.colour_command([RED, RED])


def test_each_ear_can_hold_its_own_colour(headset):
    device, transport = headset
    device.set_static(*RED, zone='left')
    device.set_static(*BLUE, zone='right')
    assert transport.last == razer_ble.colour_command([RED, BLUE])


def test_writing_one_ear_leaves_the_other_alone(headset):
    device, transport = headset
    device.set_zone_colours([RED, BLUE])
    device.set_static(0, 255, 0, zone='right')
    assert transport.last == razer_ble.colour_command([RED, (0, 255, 0)])


def test_both_ears_can_be_set_in_a_single_write(headset):
    device, transport = headset
    device.set_zone_colours([RED, BLUE])
    assert len(transport.writes) == 1
    assert transport.last == razer_ble.colour_command([RED, BLUE])


def test_one_colour_through_set_zone_colours_covers_both_ears(headset):
    device, transport = headset
    device.set_zone_colours([RED])
    assert transport.last == razer_ble.colour_command([RED, RED])
    # ... and the stored state has to agree, or the next per-ear write would
    # read back a black ear that is actually red.
    device.set_static(*BLUE, zone='right')
    assert transport.last == razer_ble.colour_command([RED, BLUE])


def test_too_many_colours_is_a_device_error_not_a_traceback(headset):
    device, _ = headset
    with pytest.raises(DeviceError):
        device.set_zone_colours([RED, BLUE, RED])


def test_off_clears_both_ears_and_the_stored_colours(headset, persistence):
    device, transport = headset
    device.set_zone_colours([RED, BLUE])
    device.set_none()
    assert transport.last == razer_ble.off_command()
    assert persistence.get(device.serial, AGGREGATE_ZONE, 'effect') == 'none'


def test_one_ear_can_be_switched_off_on_its_own(headset):
    device, transport = headset
    device.set_zone_colours([RED, BLUE])
    device.set_none(zone='left')
    assert transport.last == razer_ble.colour_command([(0, 0, 0), BLUE])


def test_the_zones_are_the_two_ears_plus_the_whole_headset(headset):
    device, _ = headset
    capabilities = device.capabilities()
    assert set(capabilities['zones']) == {AGGREGATE_ZONE, 'left', 'right'}
    assert capabilities['zone_order'] == ['left', 'right']
    assert capabilities['zone_colours'] is True
    assert capabilities['matrix_dimensions'] == [1, 2]
    assert capabilities['protocol'] == 'razer-ble'


def test_the_address_stands_in_for_a_serial(headset):
    device, _ = headset
    assert device.serial == 'BLE445ECD583460'


def test_a_matrix_frame_paints_the_ears(headset):
    device, transport = headset
    device.set_key_row(bytes((0, 0, 1)) + bytes(RED) + bytes(BLUE))
    device.set_custom()
    assert transport.last == razer_ble.colour_command([RED, BLUE])


def test_a_frame_can_paint_a_single_ear(headset):
    device, transport = headset
    device.set_zone_colours([RED, BLUE])
    device.set_key_row(bytes((0, 1, 1)) + bytes((0, 255, 0)))
    assert transport.last == razer_ble.colour_command([RED, (0, 255, 0)])


def test_restore_replays_the_last_colours(headset):
    device, transport = headset
    device.set_zone_colours([RED, BLUE])
    transport.writes.clear()
    device.restore()
    assert transport.last == razer_ble.colour_command([RED, BLUE])


def test_the_headset_reports_no_firmware_and_has_no_recipes(headset):
    device, _ = headset
    for call in (lambda: device.firmware_version,
                 lambda: device.run('set_static_effect')):
        with pytest.raises(NotSupported):
            call()


def test_brightness_is_a_percentage_over_a_byte(headset):
    device, transport = headset
    device.set_brightness(50)
    # Synapse's slider sent 0x7f at 50, and 50% of 255 rounds to 0x80.
    assert transport.last == razer_ble.brightness_command(0x80)
    assert device.get_brightness() == 100.0     # what the fake answers


def test_brightness_is_clamped_to_the_slider_range(headset):
    device, transport = headset
    device.set_brightness(400)
    assert transport.last == razer_ble.brightness_command(0xFF)
    device.set_brightness(-5)
    assert transport.last == razer_ble.brightness_command(0x00)


def test_brightness_does_not_disturb_the_held_colour(headset):
    device, transport = headset
    device.set_zone_colours([RED, BLUE])
    device.set_brightness(50)
    assert transport.held == razer_ble.colour_command([RED, BLUE])


def test_the_battery_level_is_read_from_the_device(headset):
    device, transport = headset
    assert device.get_battery_level() == 87
    assert razer_ble.OP_BATTERY in transport.reads
    assert device.is_charging() is False


def test_a_device_that_will_not_answer_is_reported_as_such(headset):
    device, transport = headset
    transport.answers.clear()
    with pytest.raises(DeviceError):
        device.get_battery_level()


def test_brightness_and_battery_are_advertised_as_capabilities(headset):
    device, _ = headset
    capabilities = device.capabilities()
    assert capabilities['battery'] is True
    assert capabilities['readback'] is True
    assert capabilities['zones']['backlight']['brightness'] is True
    # Brightness is device-wide, so an ear must not claim its own.
    assert capabilities['zones']['left']['brightness'] is False


def test_only_the_effects_the_hardware_has_are_claimed(headset):
    device, _ = headset
    assert device.available_effects() == ['none', 'static']
    assert not device.has_effect('spectrum')


# -- discovery --------------------------------------------------------------

def test_the_headset_is_marked_as_a_bluetooth_device():
    meta = get_database().get(0x1532, KITTY_V2_BT)
    assert meta.is_bluetooth
    assert meta.transport == 'ble'


def test_every_other_device_is_reached_over_hid():
    bluetooth = [entry.name for entry in get_database() if entry.is_bluetooth]
    assert bluetooth == ['Razer Kraken Kitty V2 BT']


def test_the_manager_finds_the_headset_by_advertisement(persistence, fake_radio):
    manager = DeviceManager(backend=FakeHidBackend(), persistence=persistence)
    devices = manager.scan()
    assert len(devices) == 1
    assert isinstance(devices[0], BleDevice)
    assert devices[0].serial == 'BLE445ECD583460'
    devices[0].set_static(*RED)
    assert fake_radio[0].last == razer_ble.colour_command([RED, RED])
    manager.close()


def test_a_nameless_advertisement_is_still_matched(persistence, monkeypatch, fake_radio):
    """The name and the manufacturer data arrive in separate packets.

    A sweep that hears only the second one still knows what the device is.
    """
    import openrazer_win.ble as ble
    monkeypatch.setattr(ble, 'scan', lambda *a, **k: [
        Advertiser(ADDRESS, '', -55, KITTY_V2_BT, CLASSIC_ADDRESS)])
    manager = DeviceManager(backend=FakeHidBackend(), persistence=persistence)
    devices = manager.scan()
    assert len(devices) == 1
    assert isinstance(devices[0], BleDevice)
    manager.close()


def test_a_nameless_advertisement_with_no_product_id_is_ignored(
        persistence, monkeypatch, fake_radio):
    import openrazer_win.ble as ble
    monkeypatch.setattr(ble, 'scan', lambda *a, **k: [Advertiser(ADDRESS, '', -55)])
    manager = DeviceManager(backend=FakeHidBackend(), persistence=persistence)
    assert manager.scan() == []
    manager.close()


def test_a_usb_product_id_advertised_over_bluetooth_is_ignored(
        persistence, monkeypatch, fake_radio):
    """Only devices the database marks Bluetooth-only are built this way."""
    import openrazer_win.ble as ble
    monkeypatch.setattr(ble, 'scan',
                        lambda *a, **k: [Advertiser(ADDRESS, '', -55, 0x0078)])
    manager = DeviceManager(backend=FakeHidBackend(), persistence=persistence)
    assert manager.scan() == []
    manager.close()


def test_the_name_alone_is_enough_to_match(persistence, monkeypatch, fake_radio):
    import openrazer_win.ble as ble
    monkeypatch.setattr(ble, 'scan',
                        lambda *a, **k: [Advertiser(ADDRESS, 'Razer Stereo', -55)])
    manager = DeviceManager(backend=FakeHidBackend(), persistence=persistence)
    assert len(manager.scan()) == 1
    manager.close()


def test_an_unknown_advertiser_is_ignored(persistence, monkeypatch, fake_radio):
    import openrazer_win.ble as ble
    monkeypatch.setattr(ble, 'scan',
                        lambda *a, **k: [Advertiser(1, 'Some Speaker', -40, 0x9999)])
    manager = DeviceManager(backend=FakeHidBackend(), persistence=persistence)
    assert manager.scan() == []
    manager.close()


def test_the_radio_is_not_swept_on_every_rescan(persistence, fake_radio):
    sweeps = []

    import openrazer_win.ble as ble

    def counting_scan(*args, **kwargs):
        sweeps.append(1)
        return [ADVERT]

    ble.scan = counting_scan
    try:
        manager = DeviceManager(backend=FakeHidBackend(), persistence=persistence,
                                bluetooth_interval=3600)
        manager.scan()
        manager.scan()
        manager.scan()
        # A sweep takes seconds; the hot-plug poll runs every few seconds.
        assert len(sweeps) == 1
        assert len(manager.devices) == 1
        manager.close()
    finally:
        del ble.scan


def test_a_missed_advertisement_does_not_drop_the_device(persistence, fake_radio):
    manager = DeviceManager(backend=FakeHidBackend(), persistence=persistence,
                            bluetooth_interval=3600)
    assert len(manager.scan()) == 1
    # The radio is not swept again, so the headset must survive the rescan.
    assert len(manager.scan()) == 1
    manager.close()


def test_a_silent_radio_drops_the_device(persistence, monkeypatch, fake_radio):
    manager = DeviceManager(backend=FakeHidBackend(), persistence=persistence,
                            bluetooth_interval=0)
    assert len(manager.scan()) == 1
    import openrazer_win.ble as ble
    monkeypatch.setattr(ble, 'scan', lambda *a, **k: [])
    assert manager.scan() == []
    manager.close()


def test_a_broken_radio_does_not_break_scanning(persistence, monkeypatch, fake_radio):
    import openrazer_win.ble as ble

    def explode(*args, **kwargs):
        raise OSError('the radio is off')

    monkeypatch.setattr(ble, 'scan', explode)
    fake = FakeHidBackend()
    manager = DeviceManager(backend=fake, persistence=persistence)
    assert manager.scan() == []
    manager.close()


def test_bluetooth_discovery_can_be_switched_off(persistence, fake_radio):
    manager = DeviceManager(backend=FakeHidBackend(), persistence=persistence,
                            bluetooth=False)
    assert manager.scan() == []
    manager.close()


def test_the_hid_scan_never_builds_a_bluetooth_device(persistence):
    """The headset has no USB data interface, so a HID match would be bogus."""
    from openrazer_win.hid.fake import FakeRazerDevice

    fake = FakeRazerDevice(KITTY_V2_BT, 'Razer Kraken Kitty V2 BT', interface=3,
                           kraken=True)
    manager = DeviceManager(backend=FakeHidBackend([fake]), persistence=persistence,
                            bluetooth=False)
    assert manager.scan() == []
    assert manager.unsupported == []
    manager.close()


# -- the client and the CLI -------------------------------------------------

@pytest.fixture
def headset_daemon(tmp_path, monkeypatch, fake_radio):
    yield from _serve(tmp_path, monkeypatch, fake_radio, effects=False)


@pytest.fixture
def headset_daemon_with_effects(tmp_path, monkeypatch, fake_radio):
    yield from _serve(tmp_path, monkeypatch, fake_radio, effects=True)


def _serve(tmp_path, monkeypatch, fake_radio, effects: bool):
    from openrazer_win.core.persistence import Persistence
    from openrazer_win.core.profiles import ProfileStore
    from openrazer_win.daemon.server import DaemonServer, DaemonService

    service = DaemonService(backend=FakeHidBackend(),
                            persistence=Persistence(str(tmp_path / 'p.json')),
                            enable_effects=effects,
                            profiles=ProfileStore(str(tmp_path / 'profiles.json')))
    service.start()
    server = DaemonServer(service, port=0)
    path = str(tmp_path / 'daemon.json')
    server.endpoint.write(path)
    monkeypatch.setattr('openrazer_win.daemon.protocol.endpoint_path', lambda: path)
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={'poll_interval': 0.05}, daemon=True)
    thread.start()
    try:
        yield fake_radio[0]
    finally:
        server.shutdown()
        server.server_close()
        service.stop()
        thread.join(timeout=2)


def test_the_client_lists_the_ears_as_colour_zones(headset_daemon):
    from openrazer_win.client import DeviceManager as ClientManager

    with ClientManager() as manager:
        device = manager.devices[0]
        assert device.colour_zones() == ['left', 'right']
        device.set_colour_zones([RED, BLUE])
    assert headset_daemon.last == razer_ble.colour_command([RED, BLUE])


def test_the_client_sends_both_ears_in_one_write(headset_daemon):
    from openrazer_win.client import DeviceManager as ClientManager

    with ClientManager() as manager:
        manager.devices[0].set_colour_zones([RED, BLUE])
    # Two writes would light one ear before the other, visibly.
    assert len(headset_daemon.colour_writes) == 1


def test_zones_command_paints_each_ear(headset_daemon, capsys):
    from openrazer_win.cli import main

    assert main(['zones', 'red', 'blue']) == 0
    assert headset_daemon.last == razer_ble.colour_command([RED, BLUE])
    output = capsys.readouterr().out
    assert 'left=#ff0000' in output
    assert 'right=#0000ff' in output


def test_zones_command_with_one_colour_covers_both(headset_daemon):
    from openrazer_win.cli import main

    assert main(['zones', '#ff8800']) == 0
    assert headset_daemon.last == razer_ble.colour_command([(255, 136, 0)] * 2)


def test_zones_command_with_no_colours_lists_them(headset_daemon, capsys):
    from openrazer_win.cli import main

    assert main(['zones']) == 0
    assert 'left, right' in capsys.readouterr().out


def test_zones_command_rejects_the_wrong_count(headset_daemon, capsys):
    from openrazer_win.cli import main

    assert main(['zones', 'red', 'blue', 'green']) == 1
    assert '2 zones' in capsys.readouterr().err


def test_the_effect_command_defaults_to_both_ears(headset_daemon, capsys):
    from openrazer_win.cli import main

    assert main(['effect', 'static', 'red']) == 0
    assert headset_daemon.last == razer_ble.colour_command([RED, RED])
    assert 'static on backlight' in capsys.readouterr().out


def test_info_names_the_bluetooth_address_not_a_hid_interface(headset_daemon, capsys):
    from openrazer_win.cli import main

    assert main(['info']) == 0
    output = capsys.readouterr().out
    assert 'bluetooth   445ECD583460' in output
    assert 'interface' not in output


def test_the_effect_command_can_name_one_ear(headset_daemon, capsys):
    from openrazer_win.cli import main

    assert main(['effect', 'static', 'red', '--zone', 'left']) == 0
    assert headset_daemon.last == razer_ble.colour_command([RED, (0, 0, 0)])
    assert 'static on left' in capsys.readouterr().out


def test_spectrum_falls_back_to_the_host_renderer(headset_daemon_with_effects, capsys):
    """The headset only knows a static colour; Synapse fakes the rest too."""
    from openrazer_win.cli import main
    from openrazer_win.client.rpc import RpcClient

    assert main(['effect', 'spectrum']) == 0
    output = capsys.readouterr().out
    assert 'spectrum' in output
    assert 'host-rendered' in output

    client = RpcClient()
    try:
        active = client.call('effect.status')['active']
        assert active == {'BLE445ECD583460': 'spectrum_soft'}
    finally:
        main(['effect', 'none'])
        client.close()


def test_the_host_renderer_actually_writes_frames(headset_daemon_with_effects):
    import time

    from openrazer_win.cli import main

    transport = headset_daemon_with_effects
    assert main(['effect', 'spectrum']) == 0
    try:
        deadline = time.monotonic() + 3.0
        while len(transport.colour_writes) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
        frames = transport.colour_writes
        assert len(frames) >= 3, 'no frames reached the device'
        # Each frame is a full colour command, and the colour moves.
        assert all(frame[:3] == bytes((0xC4, 0x00, 0x06)) for frame in frames)
        assert len(set(frames)) > 1
    finally:
        main(['effect', 'none'])


# -- the traps that broke 1.2.0 ---------------------------------------------

def test_a_connected_device_survives_a_silent_sweep(persistence, monkeypatch,
                                                    fake_radio):
    """A connected BLE device stops advertising, so silence proves nothing.

    Dropping it on silence was self-perpetuating: whatever held the link kept
    it up, so the device never advertised again and was never rediscovered,
    while its lighting was still being written to.
    """
    import openrazer_win.ble as ble

    manager = DeviceManager(backend=FakeHidBackend(), persistence=persistence,
                            bluetooth_interval=0)
    assert len(manager.scan()) == 1
    fake_radio[0].connected = True
    monkeypatch.setattr(ble, 'scan', lambda *a, **k: [])
    assert len(manager.scan()) == 1, 'a connected device must not be dropped'
    manager.close()


def test_a_disconnected_silent_device_is_still_dropped(persistence, monkeypatch,
                                                       fake_radio):
    import openrazer_win.ble as ble

    manager = DeviceManager(backend=FakeHidBackend(), persistence=persistence,
                            bluetooth_interval=0)
    assert len(manager.scan()) == 1
    fake_radio[0].connected = False
    monkeypatch.setattr(ble, 'scan', lambda *a, **k: [])
    assert manager.scan() == []
    manager.close()


def test_a_ripple_is_refused_where_it_cannot_spread(headset):
    """Two ears is a blink, not a ripple -- and it cost the user their lighting."""
    from openrazer_win.effects.engine import EffectEngine

    device, _ = headset
    engine = EffectEngine(manager=None)
    for effect in ('ripple', 'ripple_random'):
        with pytest.raises(NotSupported):
            engine.set_effect(device, effect, {})
    engine.stop()


def test_a_clock_driven_effect_is_still_allowed_on_two_zones(headset):
    from openrazer_win.effects.engine import EffectEngine

    device, transport = headset
    engine = EffectEngine(manager=None)
    try:
        started = engine.set_effect(device, 'spectrum_soft', {})
        assert started['effect'] == 'spectrum_soft'
        deadline = time.monotonic() + 3.0
        while not transport.writes and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        engine.stop()
    assert transport.writes, 'the renderer should have written a frame'


def test_a_vanished_device_loses_its_effect(tmp_path, monkeypatch, fake_radio):
    """An effect thread must not outlive the device it is writing to."""
    import openrazer_win.ble as ble
    from openrazer_win.core.persistence import Persistence
    from openrazer_win.daemon.server import DaemonService

    service = DaemonService(backend=FakeHidBackend(),
                            persistence=Persistence(str(tmp_path / 'p.json')),
                            enable_effects=True)
    service.manager.bluetooth_interval = 0
    service.start()
    device = service.manager.devices[0]
    service.effects.set_effect(device, 'spectrum_soft', {})
    assert service.effects.active_serials() == [device.serial]

    fake_radio[0].connected = False
    monkeypatch.setattr(ble, 'scan', lambda *a, **k: [])
    service.poll_hotplug()

    assert service.manager.devices == []
    assert service.effects.active_serials() == [], \
        'the effect kept running on a device that is gone'
    service.stop()


def test_an_idle_link_is_given_up(monkeypatch):
    """Holding an idle link would keep the device from ever advertising."""
    import openrazer_win.ble.transport as transport_module

    monkeypatch.setattr(transport_module, 'require_available', lambda: None)

    closed = []

    class FakeGattDevice:
        connection_status = 1

        def close(self):
            closed.append(True)

    transport = transport_module.BleTransport(ADDRESS, idle_disconnect=0.15)
    sent = []

    async def fake_write_once(payload):
        sent.append(bytes(payload))

    monkeypatch.setattr(transport, '_write_once', fake_write_once)
    transport._device = FakeGattDevice()
    transport._characteristic_cache = object()

    transport.write(b'\xc4\x00\x06' + bytes(6))
    assert sent, 'the write should have gone out'
    assert transport.is_connected()

    deadline = time.monotonic() + 3.0
    while transport.is_connected() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not transport.is_connected(), 'the idle link was never dropped'
    assert closed == [True]


def test_a_link_that_is_being_used_is_not_given_up(monkeypatch):
    import openrazer_win.ble.transport as transport_module

    monkeypatch.setattr(transport_module, 'require_available', lambda: None)

    class FakeGattDevice:
        connection_status = 1

        def close(self):
            pass

    transport = transport_module.BleTransport(ADDRESS, idle_disconnect=0.3)

    async def fake_write_once(payload):
        pass

    monkeypatch.setattr(transport, '_write_once', fake_write_once)
    transport._device = FakeGattDevice()
    transport._characteristic_cache = object()

    # Frames at 20 fps, the way a host-rendered effect writes.
    for _ in range(12):
        transport.write(b'\xc4\x00\x06' + bytes(6))
        time.sleep(0.05)
    assert transport.is_connected(), 'a link in active use must be kept'
    transport.close()


def test_nothing_is_reported_connected_before_a_write(monkeypatch):
    import openrazer_win.ble.transport as transport_module

    monkeypatch.setattr(transport_module, 'require_available', lambda: None)
    assert transport_module.BleTransport(ADDRESS).is_connected() is False


def test_a_cold_resolve_is_retried_before_giving_up(monkeypatch):
    """The first GATT enumeration of an idle device often comes back empty.

    Windows answers from a cache that can be both successful and empty, which
    is indistinguishable from a device that lacks the characteristic -- so the
    first answer is not taken as final, and the retry does not trust the cache.
    """
    import openrazer_win.ble.transport as transport_module

    monkeypatch.setattr(transport_module, 'require_available', lambda: None)
    monkeypatch.setattr(transport_module, 'RESOLVE_BACKOFF', 0.01)

    transport = transport_module.BleTransport(ADDRESS)
    asked = []
    sentinel = (object(), object())

    async def fake_resolve_once(uncached):
        asked.append(uncached)
        if len(asked) < 2:
            raise transport_module.BleUnavailable('no GATT services yet')
        return sentinel

    monkeypatch.setattr(transport, '_resolve_once', fake_resolve_once)
    assert transport_module._loop().submit(transport._resolve()) == sentinel
    assert asked == [False, True], 'the retry must not trust the cache'


def test_a_resolve_that_never_works_reports_the_last_reason(monkeypatch):
    import openrazer_win.ble.transport as transport_module

    monkeypatch.setattr(transport_module, 'require_available', lambda: None)
    monkeypatch.setattr(transport_module, 'RESOLVE_BACKOFF', 0.01)

    transport = transport_module.BleTransport(ADDRESS)
    attempts = []

    async def fake_resolve_once(uncached):
        attempts.append(uncached)
        raise transport_module.BleUnavailable('the radio is off')

    monkeypatch.setattr(transport, '_resolve_once', fake_resolve_once)
    with pytest.raises(transport_module.BleUnavailable, match='the radio is off'):
        transport_module._loop().submit(transport._resolve())
    assert len(attempts) == transport_module.RESOLVE_ATTEMPTS


def test_a_static_colour_is_held_not_just_written(headset):
    """The device reverts to its saved colour the moment the link drops."""
    device, transport = headset
    device.set_zone_colours([RED, BLUE])
    assert transport.held == razer_ble.colour_command([RED, BLUE])


def test_switching_the_lighting_off_stops_holding_it(headset):
    device, transport = headset
    device.set_zone_colours([RED, BLUE])
    device.set_none()
    assert transport.held is None, 'nothing to hold once it is dark'


def test_an_animation_frame_is_not_held(headset):
    """The next frame is along in a moment; holding one would fight it."""
    device, transport = headset
    device.set_key_row(bytes((0, 0, 1)) + bytes(RED) + bytes(BLUE))
    assert transport.held is None


def test_a_held_colour_is_re_asserted_and_keeps_the_link(monkeypatch):
    import openrazer_win.ble.transport as transport_module

    monkeypatch.setattr(transport_module, 'require_available', lambda: None)

    class FakeGattDevice:
        connection_status = 1

        def close(self):
            pass

    transport = transport_module.BleTransport(
        ADDRESS, idle_disconnect=0.1, keepalive=0.1)
    sent = []

    async def fake_write_once(payload):
        sent.append(bytes(payload))

    monkeypatch.setattr(transport, '_write_once', fake_write_once)
    transport._device = FakeGattDevice()
    transport._characteristic_cache = object()

    payload = razer_ble.colour_command([RED, BLUE])
    transport.write(payload, hold=True)

    deadline = time.monotonic() + 3.0
    while len(sent) < 4 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(sent) >= 4, 'the colour was not re-asserted'
    assert set(sent) == {payload}, 'the held colour must be what goes out'
    # Idle disconnect must not fire while a colour is being held.
    assert transport.is_connected()

    transport.release()
    deadline = time.monotonic() + 3.0
    while transport.is_connected() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not transport.is_connected(), 'a released link should be given up'


def test_taking_over_the_lighting_is_announced(headset):
    """Synapse sends this before every effect, so this port does too."""
    device, transport = headset
    device.enter_driver_mode()
    assert transport.last == razer_ble.takeover_command()
    assert transport.last == bytes((0xC0, 0x00, 0x01, 0x05))


def test_a_device_that_refuses_the_takeover_still_works(headset, monkeypatch):
    device, transport = headset

    def refuse(payload, hold=False):
        raise BleUnavailable('nope')

    monkeypatch.setattr(transport, 'write', refuse)
    device.enter_driver_mode()          # must not raise


def test_a_lost_request_is_asked_again(monkeypatch):
    """Requests are Write Commands: unacknowledged, so one can vanish."""
    import asyncio

    import openrazer_win.ble.transport as transport_module

    monkeypatch.setattr(transport_module, 'require_available', lambda: None)
    monkeypatch.setattr(transport_module, 'REPLY_TIMEOUT', 0.05)

    transport = transport_module.BleTransport(ADDRESS)
    attempts = []

    async def flaky(opcode, payload):
        attempts.append(opcode)
        if len(attempts) < 3:
            raise asyncio.TimeoutError()
        return bytes((0xFF,))

    monkeypatch.setattr(transport, '_request_once', flaky)
    assert transport_module._loop().submit(
        transport._request(0x41, b'')) == bytes((0xFF,))
    assert len(attempts) == 3


def test_a_device_that_never_answers_is_reported_once(monkeypatch):
    import asyncio

    import openrazer_win.ble.transport as transport_module

    monkeypatch.setattr(transport_module, 'require_available', lambda: None)
    transport = transport_module.BleTransport(ADDRESS)
    attempts = []

    async def silent(opcode, payload):
        attempts.append(opcode)
        raise asyncio.TimeoutError()

    monkeypatch.setattr(transport, '_request_once', silent)
    with pytest.raises(transport_module.BleUnavailable, match='did not answer'):
        transport_module._loop().submit(transport._request(0x41, b''))
    assert len(attempts) == transport_module.REQUEST_ATTEMPTS


def test_brightness_is_remembered_by_the_device_not_by_us(headset, persistence):
    """Brightness survives a disconnect; the live colour does not.

    Confirmed on the hardware by reading it back over a fresh connection. The
    device is therefore the authority on brightness, and restoring lighting
    must not fight it.
    """
    device, transport = headset
    device.set_brightness(30)
    transport.answers[razer_ble.read_opcode_for(razer_ble.OP_BRIGHTNESS)] = \
        bytes((razer_ble.percentage_to_level(30),))
    device.set_zone_colours([RED, BLUE])
    transport.writes.clear()

    device.restore()
    # Only the colour is replayed: the device kept the brightness itself.
    assert transport.colour_writes
    assert not any(w[:1] == bytes((razer_ble.OP_BRIGHTNESS,))
                   for w in transport.writes)


def test_profiles_round_trip_through_the_daemon(headset_daemon, capsys, tmp_path,
                                                monkeypatch):
    from openrazer_win.cli import main

    assert main(['profile']) == 0
    assert 'no saved profiles' in capsys.readouterr().out

    assert main(['zones', 'red', 'blue']) == 0
    assert main(['profile', 'save', 'evening']) == 0
    assert "'evening' saved" in capsys.readouterr().out

    assert main(['zones', 'green', 'green']) == 0
    capsys.readouterr()

    assert main(['profile', 'load', 'evening']) == 0
    assert "'evening' applied" in capsys.readouterr().out
    assert headset_daemon.colour_writes[-1] == razer_ble.colour_command(
        [RED, BLUE])

    assert main(['profile']) == 0
    assert 'evening' in capsys.readouterr().out

    assert main(['profile', 'delete', 'evening']) == 0
    assert main(['profile']) == 0
    assert 'no saved profiles' in capsys.readouterr().out


def test_loading_a_profile_that_is_not_there_fails_cleanly(headset_daemon, capsys):
    from openrazer_win.cli import main

    assert main(['profile', 'load', 'nope']) == 1
    assert 'nope' in capsys.readouterr().err


def test_save_without_a_name_is_refused(headset_daemon, capsys):
    from openrazer_win.cli import main

    assert main(['profile', 'save']) == 2
    assert 'needs a profile name' in capsys.readouterr().err


def test_the_stored_colour_reads_as_what_it_is(headset, persistence):
    """A saved profile should not show the default palette for a lit device."""
    device, _ = headset
    device.set_zone_colours([RED, BLUE])
    assert persistence.get(device.serial, AGGREGATE_ZONE, 'colors')[:6] == [
        255, 0, 0, 0, 0, 255]
