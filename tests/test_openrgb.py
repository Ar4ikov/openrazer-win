"""OpenRGB's SDK protocol, answered by the daemon for its devices."""
from __future__ import annotations

import socket
import struct
import time

import pytest

from openrazer_win._version import __version__
from openrazer_win.core.manager import DeviceManager
from openrazer_win.core.persistence import Persistence
from openrazer_win.daemon.demo import build_demo_backend
from openrazer_win.daemon.server import DaemonService
from openrazer_win.devices import get_database, get_recipes
from openrazer_win.hid.fake import FakeRazerDevice
from openrazer_win.integrations import IntegrationSettings
from openrazer_win.openrgb import OpenRGBBridge
from openrazer_win.openrgb import protocol as p
from openrazer_win.openrgb.controller import wave_byte, wave_percent

RED = (255, 0, 0)
BLUE = (0, 0, 255)
GREEN = (0, 255, 0)


# -- the wire format ------------------------------------------------------------

def test_a_packet_is_the_header_openrgb_reads_then_its_data():
    assert p.packet(3, p.UPDATE_LEDS, b'abc') == b'ORGB' + struct.pack('<III', 3, 1050, 3) + b'abc'
    assert p.HEADER.size == 16


def _sample() -> p.Controller:
    return p.Controller(
        type=p.KEYBOARD, name='Board', vendor='Razer', description='d', version='v',
        serial='S1', location='L',
        modes=[p.Mode('Direct', flags=p.HAS_PER_LED_COLOR, color_mode=p.COLORS_PER_LED),
               p.Mode('Static', flags=p.HAS_MODE_SPECIFIC_COLOR | p.HAS_BRIGHTNESS,
                      colors_min=1, colors_max=1, brightness_max=100, brightness=40,
                      color_mode=p.COLORS_MODE_SPECIFIC, colors=[RED]),
               p.Mode('Wave', flags=p.HAS_DIRECTION_LR | p.HAS_SPEED, speed_max=100, speed=33,
                      direction=p.DIRECTION_RIGHT)],
        active_mode=1,
        zones=[p.Zone('Keys', p.ZONE_MATRIX, 4, matrix=[[0, 1], [None, 3]]),
               p.Zone('Strip', p.ZONE_LINEAR, 2, segments=[p.Segment('Half', p.ZONE_LINEAR, 0, 1)],
                      flags=7)],
        leds=['a', 'b', 'c', 'd', 'e', 'f'],
        colors=[RED, GREEN, BLUE, RED, GREEN, BLUE], flags=0)


@pytest.mark.parametrize('version', range(0, p.VERSION + 1))
def test_a_description_reads_back_in_every_version(version):
    sent = _sample()
    data = p.controller_data(sent, version)
    assert struct.unpack('<I', data[:4])[0] == len(data)
    got = p.read_controller(data, version)
    assert (got.type, got.name, got.serial, got.location) == (p.KEYBOARD, 'Board', 'S1', 'L')
    assert got.vendor == ('Razer' if version >= 1 else '')
    assert [m.name for m in got.modes] == ['Direct', 'Static', 'Wave'] and got.active_mode == 1
    assert got.modes[1].colors == [RED]
    assert got.modes[1].brightness == (40 if version >= 3 else 0)
    assert (got.modes[2].speed, got.modes[2].direction) == (33, p.DIRECTION_RIGHT)
    assert got.zones[0].matrix == [[0, 1], [None, 3]] and got.zones[1].matrix is None
    assert got.zones[1].segments == (sent.zones[1].segments if version >= 4 else [])
    assert got.zones[1].flags == (7 if version >= 5 else 0)
    assert got.leds == sent.leds and got.colors == sent.colors


@pytest.mark.parametrize('version', range(0, p.VERSION + 1))
def test_a_mode_reads_back_in_every_version(version):
    mode = _sample().modes[1]
    index, got = p.read_mode(p.mode_data(1, mode, version), version)
    assert index == 1 and got.name == 'Static' and got.colors == [RED]
    assert got.brightness == (40 if version >= 3 else 0)


def test_colours_go_as_openrgb_has_them_red_in_the_low_byte():
    assert p.leds_data([(1, 2, 3)]) == struct.pack('<IH', 10, 1) + bytes((1, 2, 3, 0))
    assert p.read_leds(p.leds_data([RED, BLUE])) == [RED, BLUE]
    assert p.read_zone_leds(p.zone_leds_data(2, [GREEN])) == (2, [GREEN])
    assert p.read_single_led(p.single_led_data(5, BLUE)) == (5, BLUE)


def test_a_packet_that_ends_early_is_an_error_not_a_crash():
    with pytest.raises(p.ProtocolError):
        p.read_leds(struct.pack('<IH', 10, 3) + bytes(4))


def test_a_wave_s_speed_is_even_to_the_eye_and_comes_back():
    assert wave_percent(255) == 0 and wave_percent(1) == 100
    assert wave_byte(wave_percent(40)) in (39, 40, 41)


# -- a client, for the tests ----------------------------------------------------

class Sdk:
    """Just enough of an OpenRGB client to talk to the daemon."""

    def __init__(self, port: int, version: int = p.VERSION, name: str = 'tests'):
        self.sock = socket.create_connection(('127.0.0.1', port), timeout=5)
        self.send(0, p.REQUEST_PROTOCOL_VERSION, p.u32(version))
        self.server_version = struct.unpack('<I', self.expect(p.REQUEST_PROTOCOL_VERSION)[1])[0]
        self.version = min(version, self.server_version)
        self.send(0, p.SET_CLIENT_NAME, name.encode() + b'\0')

    def send(self, device: int, packet_id: int, data: bytes = b'') -> None:
        self.sock.sendall(p.packet(device, packet_id, data))

    def _exactly(self, count: int) -> bytes:
        data = b''
        while len(data) < count:
            chunk = self.sock.recv(count - len(data))
            if not chunk:
                raise ConnectionError('closed')
            data += chunk
        return data

    def read(self):
        magic, device, packet_id, size = p.HEADER.unpack(self._exactly(16))
        assert magic == b'ORGB'
        return device, packet_id, self._exactly(size)

    def expect(self, packet_id: int):
        while True:
            device, got, data = self.read()
            if got == packet_id:
                return device, data

    def count(self) -> int:
        self.send(0, p.REQUEST_CONTROLLER_COUNT)
        return struct.unpack('<I', self.expect(p.REQUEST_CONTROLLER_COUNT)[1])[0]

    def controller(self, index: int) -> p.Controller:
        self.send(index, p.REQUEST_CONTROLLER_DATA, p.u32(self.version))
        return p.read_controller(self.expect(p.REQUEST_CONTROLLER_DATA)[1], self.version)

    def controllers(self) -> list:
        return [self.controller(index) for index in range(self.count())]

    def index(self, name: str) -> int:
        return [c.name for c in self.controllers()].index(name)

    def set_mode(self, index: int, name: str, **values) -> None:
        controller = self.controller(index)
        number = [m.name for m in controller.modes].index(name)
        mode = controller.modes[number]
        for key, value in values.items():
            setattr(mode, key, value)
        self.send(index, p.UPDATE_MODE, p.mode_data(number, mode, self.version))

    def close(self) -> None:
        self.sock.close()


def until(condition, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


@pytest.fixture
def service(tmp_path):
    service = DaemonService(backend=build_demo_backend(),
                            persistence=Persistence(str(tmp_path / 'p.json')),
                            integrations=IntegrationSettings(str(tmp_path / 'i.json')),
                            chroma_port=0, openrgb_port=0)
    service.start()
    yield service
    service.stop()


@pytest.fixture
def sdk(service):
    status = service.rpc_integration_set('openrgb', True)['openrgb']
    assert status['listening'] and status['enabled']
    client = Sdk(status['port'])
    yield client
    client.close()


def device(service, name):
    return service.manager.find(name)[0]


def fake(service, found):
    return next(d for d in service.manager.backend.devices if d.info.product_id == found.pid)


# -- the devices, as controllers ------------------------------------------------

def test_a_newer_client_is_told_protocol_5_and_uses_it(service):
    # OpenRGB 1.0 and later speak 6, and fall back to what the server has.
    sdk = Sdk(service.rpc_integration_set('openrgb', True)['openrgb']['port'], version=6)
    try:
        assert sdk.server_version == 5 and sdk.version == 5
        assert len(sdk.controllers()) == 4
    finally:
        sdk.close()


def test_a_keyboard_is_one_matrix_of_its_keys(sdk, service):
    board = sdk.controller(sdk.index('Razer BlackWidow Chroma'))
    keyboard = device(service, 'BlackWidow Chroma')
    assert (board.type, board.vendor, board.serial) == (p.KEYBOARD, 'Razer', keyboard.serial)
    assert board.version == 'openrazer-win {0}'.format(__version__)
    [zone] = board.zones
    assert (zone.name, zone.type, zone.count) == ('Keyboard', p.ZONE_MATRIX, 6 * 22)
    assert len(zone.matrix) == 6 and len(zone.matrix[0]) == 22 and zone.matrix[3][2] == 3 * 22 + 2
    assert len(board.leds) == len(board.colors) == 132 and board.leds[1] == 'Key: ESC'
    names = [mode.name for mode in board.modes]
    assert names[0] == 'Direct' and board.modes[0].color_mode == p.COLORS_PER_LED
    assert {'Off', 'Static', 'Breathing', 'Spectrum Cycle', 'Wave', 'Reactive'} <= set(names)
    # Nothing set yet: what Razer devices start in, and what the daemon assumes.
    assert names[board.active_mode] == 'Spectrum Cycle'


def test_a_mouse_without_a_frame_is_its_zones_each_with_a_colour(tmp_path):
    service = DaemonService(backend=build_demo_backend((0x0084,)),
                            persistence=Persistence(str(tmp_path / 'p.json')),
                            integrations=IntegrationSettings(str(tmp_path / 'i.json')),
                            chroma_port=0, openrgb_port=0)
    service.start()
    try:
        sdk = Sdk(service.rpc_integration_set('openrgb', True)['openrgb']['port'])
        [mouse] = sdk.controllers()
        assert mouse.type == p.MOUSE and mouse.name == 'Razer DeathAdder V2'
        assert [(z.name, z.type, z.count) for z in mouse.zones] == [
            ('Logo', p.ZONE_SINGLE, 1), ('Scroll Wheel', p.ZONE_SINGLE, 1)]
        assert mouse.leds == ['Logo', 'Scroll Wheel']
        assert mouse.modes[0].name == 'Direct'
        sdk.close()
    finally:
        service.stop()


def test_a_mousepad_is_a_strip(sdk):
    pad = sdk.controller(sdk.index('Razer Firefly (2013)'))
    assert pad.type == p.MOUSEMAT
    assert [(z.name, z.type, z.count) for z in pad.zones] == [('Mousemat', p.ZONE_LINEAR, 15)]


def test_a_headset_with_no_lights_is_not_listed(sdk):
    names = [c.name for c in sdk.controllers()]
    assert len(names) == 4 and not any('BlackShark' in name for name in names)


def test_profiles_and_plugins_are_empty_lists_not_silence(sdk):
    for packet_id in (p.REQUEST_PROFILE_LIST, p.REQUEST_PLUGIN_LIST):
        sdk.send(0, packet_id)
        assert sdk.expect(packet_id)[1] == p.EMPTY_LIST


def test_a_connection_that_is_not_openrgb_is_closed(service):
    port = service.rpc_integration_set('openrgb', True)['openrgb']['port']
    sock = socket.create_connection(('127.0.0.1', port), timeout=5)
    try:
        sock.sendall(b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n')
        assert sock.recv(64) == b''
    finally:
        sock.close()


# -- modes ----------------------------------------------------------------------

def test_a_mode_is_put_on_the_device_and_kept(sdk, service):
    index = sdk.index('Razer BlackWidow Chroma')
    keyboard = device(service, 'BlackWidow Chroma')
    sent = len(fake(service, keyboard).sent_reports)
    sdk.set_mode(index, 'Static', colors=[RED])
    # The effect is saved before its colours: wait for both.
    assert until(lambda: service.persistence.get(keyboard.serial, 'backlight', 'effect') == 'static'
                 and service.persistence.get(keyboard.serial, 'backlight', 'colors')[:3] == list(RED))
    assert len(fake(service, keyboard).sent_reports) > sent
    board = sdk.controller(index)
    assert board.modes[board.active_mode].name == 'Static'
    assert board.modes[board.active_mode].colors == [RED]


def test_a_wave_goes_the_way_it_is_asked_to(sdk, service):
    index = sdk.index('Razer BlackWidow Chroma')
    keyboard = device(service, 'BlackWidow Chroma')
    sdk.set_mode(index, 'Wave', direction=p.DIRECTION_RIGHT)
    assert until(lambda: service.persistence.get(keyboard.serial, 'backlight', 'effect') == 'wave'
                 and service.persistence.get(keyboard.serial, 'backlight', 'wave_dir') == 2)
    board = sdk.controller(index)
    assert board.modes[board.active_mode].direction == p.DIRECTION_RIGHT


def test_breathing_in_one_colour_or_two(sdk, service):
    index = sdk.index('Razer BlackWidow Chroma')
    keyboard = device(service, 'BlackWidow Chroma')

    def effect():
        return service.persistence.get(keyboard.serial, 'backlight', 'effect')

    sdk.set_mode(index, 'Breathing', colors=[RED, BLUE])
    assert until(lambda: effect() == 'breath_dual')
    sdk.set_mode(index, 'Breathing', colors=[GREEN, GREEN])
    assert until(lambda: effect() == 'breath_single')
    sdk.set_mode(index, 'Breathing', color_mode=p.COLORS_RANDOM)
    assert until(lambda: effect() == 'breath_random')


def test_a_device_without_one_colour_breathing_breathes_in_two():
    from openrazer_win.openrgb.controller import _choose
    options = ((3, 'breath_triple'), (2, 'breath_dual'), (1, 'breath_single'))
    assert _choose({'breath_dual'}, [RED], options) == ('breath_dual', [RED, RED])
    assert _choose({'breath_single', 'breath_dual'}, [RED, BLUE, GREEN], options) == (
        'breath_dual', [RED, BLUE])
    assert _choose({'breath_random'}, [RED], options) == (None, [])


def test_a_client_on_protocol_3_is_read_in_protocol_3(service):
    sdk = Sdk(service.rpc_integration_set('openrgb', True)['openrgb']['port'], version=3)
    try:
        assert sdk.version == 3
        index = sdk.index('Razer Huntsman V2 Tenkeyless')
        keyboard = device(service, 'Huntsman V2')
        sdk.set_mode(index, 'Static', colors=[BLUE], brightness=40)
        assert until(lambda: service.persistence.get(keyboard.serial, 'backlight', 'effect') == 'static'
                     and service.persistence.get(keyboard.serial, 'backlight', 'colors')[:3] == list(BLUE))
        assert until(lambda: service.persistence.get(keyboard.serial, 'backlight', 'brightness') == 40)
    finally:
        sdk.close()


def test_a_mode_puts_an_end_to_a_host_drawn_effect(sdk, service):
    keyboard = device(service, 'BlackWidow Chroma')
    service.rpc_effect(keyboard.serial, 'spectrum_soft', {})
    sdk.set_mode(sdk.index('Razer BlackWidow Chroma'), 'Static', colors=[RED])
    # Stopped first, then forgotten: wait for both, not the first alone.
    assert until(lambda: not service.effects.drawing(keyboard)
                 and service.effects.saved(keyboard.serial) is None)


# -- Direct ---------------------------------------------------------------------

def _frames(fake_device) -> bytes:
    return b''.join(fake_device.state.frames.values())


def test_direct_colours_reach_the_keyboard_and_are_not_kept(sdk, service):
    keyboard = device(service, 'BlackWidow Chroma')
    service.rpc_call(keyboard.serial, 'set_static', [0, 0, 255])
    index = sdk.index('Razer BlackWidow Chroma')
    colours = [(0, 0, 0)] * 132
    colours[1] = (250, 10, 20)      # Esc
    sdk.send(index, p.UPDATE_LEDS, p.leds_data(colours))
    assert until(lambda: bytes((250, 10, 20)) in _frames(fake(service, keyboard)))
    assert service.openrgb.drawing(keyboard)
    board = sdk.controller(index)
    assert board.modes[board.active_mode].name == 'Direct' and board.colors[1] == (250, 10, 20)
    # The user's own lighting is still what the daemon keeps.
    assert service.persistence.get(keyboard.serial, 'backlight', 'effect') == 'static'
    assert service.persistence.get(keyboard.serial, 'backlight', 'colors')[:3] == list(BLUE)


def test_a_zone_or_one_led_of_it_can_be_set(sdk, service):
    keyboard = device(service, 'BlackWidow Chroma')
    index = sdk.index('Razer BlackWidow Chroma')
    sdk.send(index, p.SET_CUSTOM_MODE)
    sdk.send(index, p.UPDATE_ZONE_LEDS, p.zone_leds_data(0, [(9, 8, 7)] * 132))
    sdk.send(index, p.UPDATE_SINGLE_LED, p.single_led_data(5, (1, 2, 3)))
    assert until(lambda: sdk.controller(index).colors[5] == (1, 2, 3))
    assert sdk.controller(index).colors[4] == (9, 8, 7)
    assert until(lambda: bytes((9, 8, 7, 1, 2, 3)) in _frames(fake(service, keyboard)))


def test_direct_on_a_mouse_s_zones_is_a_colour_each(tmp_path):
    persistence = Persistence(str(tmp_path / 'p.json'))
    manager = DeviceManager(backend=build_demo_backend((0x0084,)), persistence=persistence)
    manager.scan(sweep_bluetooth=False)
    [mouse] = manager.devices
    statics = []
    mouse.set_static = lambda r, g, b, zone: statics.append((zone, (r, g, b)))
    bridge = OpenRGBBridge(lambda: [mouse], lambda *a: None, persistence)
    bridge.update_leds('client', 0, [RED, BLUE])
    bridge.render()
    assert statics == [('logo', RED), ('scroll', BLUE)]
    bridge.update_leds('client', 0, [GREEN], led=1)
    bridge.render()
    assert statics[2:] == [('scroll', GREEN)]       # only what changed


def test_a_bluetooth_headset_s_ears_are_two_zones_lit_together(tmp_path):
    from openrazer_win.core.ble_device import BleDevice
    from openrazer_win.protocol import razer_ble

    from .conftest import KITTY_V2_BT, FakeBleTransport
    persistence = Persistence(str(tmp_path / 'p.json'))
    headset = BleDevice(get_database().get(0x1532, KITTY_V2_BT), FakeBleTransport(), persistence)
    bridge = OpenRGBBridge(lambda: [headset], lambda *a: None, persistence)
    controller = p.read_controller(bridge.describe(0, p.VERSION), p.VERSION)
    assert controller.type == p.HEADSET and controller.leds == ['Left Ear', 'Right Ear']
    assert [(z.type, z.count) for z in controller.zones] == [(p.ZONE_SINGLE, 1)] * 2
    bridge.update_leds('client', 0, [RED, BLUE])
    bridge.render()
    assert headset.transport.writes[-1] == razer_ble.colour_command([RED, BLUE])


def test_frames_are_coalesced_and_an_unchanged_one_is_not_sent(tmp_path):
    persistence = Persistence(str(tmp_path / 'p.json'))
    manager = DeviceManager(backend=build_demo_backend((0x0203,)), persistence=persistence)
    manager.scan(sweep_bluetooth=False)
    [keyboard] = manager.devices
    rows = []
    keyboard.set_key_row = lambda payload: rows.append(bytes(payload))
    bridge = OpenRGBBridge(lambda: [keyboard], lambda *a: None, persistence)
    for shade in range(60):            # a second of an effects plugin, between two frames
        bridge.update_leds('client', 0, [(shade, 0, 0)] * 132)
    bridge.render()
    bridge.render()
    assert len(rows) == 1 and rows[0][3:6] == bytes((59, 0, 0))


def test_the_device_gets_its_lighting_back_when_the_client_goes(sdk, service):
    keyboard = device(service, 'BlackWidow Chroma')
    service.rpc_call(keyboard.serial, 'set_static', [0, 0, 255])
    restored = []
    original = keyboard.restore
    keyboard.restore = lambda: (restored.append(True), original())
    index = sdk.index('Razer BlackWidow Chroma')
    sdk.send(index, p.UPDATE_LEDS, p.leds_data([RED] * 132))
    assert until(lambda: service.openrgb.drawing(keyboard))
    sdk.close()
    # Let go first, then given its lighting back.
    assert until(lambda: not service.openrgb.drawing(keyboard) and restored)
    assert service.persistence.get(keyboard.serial, 'backlight', 'colors')[:3] == list(BLUE)


def test_turning_it_off_gives_devices_back_and_closes_connections(sdk, service):
    keyboard = device(service, 'BlackWidow Chroma')
    sdk.send(sdk.index('Razer BlackWidow Chroma'), p.UPDATE_LEDS, p.leds_data([RED] * 132))
    assert until(lambda: service.openrgb.drawing(keyboard))
    status = service.rpc_integration_set('openrgb', False)['openrgb']
    assert not status['listening'] and not status['enabled']
    assert not service.openrgb.drawing(keyboard)
    sdk.sock.settimeout(3)
    assert sdk.sock.recv(16) == b''


# -- hot-plug -------------------------------------------------------------------

def _plug(service, pid: int, serial: str) -> FakeRazerDevice:
    meta = get_database().get(0x1532, pid)
    params = get_recipes().transport_params(meta.driver, meta.pid)
    return service.manager.backend.add(FakeRazerDevice(
        pid, meta.name, interface=params.get('index') or 0, serial=serial,
        argb=meta.driver == 'accessory'))


def test_clients_are_told_when_a_device_comes_or_goes(sdk, service):
    assert sdk.count() == 4
    plugged = _plug(service, 0x0C04, 'DEMO000000000099')       # a Firefly V2
    service.poll_hotplug()
    assert sdk.expect(p.DEVICE_LIST_UPDATED) == (0, b'')
    assert sdk.count() == 5 and 'Razer Firefly V2' in [c.name for c in sdk.controllers()]
    service.manager.backend.remove(plugged)
    service.poll_hotplug()
    sdk.expect(p.DEVICE_LIST_UPDATED)
    assert sdk.count() == 4


def test_a_device_drawn_on_that_goes_is_let_go(sdk, service):
    plugged = _plug(service, 0x0C04, 'DEMO000000000099')
    service.poll_hotplug()
    sdk.expect(p.DEVICE_LIST_UPDATED)
    pad = device(service, 'Firefly V2')
    sdk.send(sdk.index('Razer Firefly V2'), p.UPDATE_LEDS, p.leds_data([RED] * 19))
    assert until(lambda: service.openrgb.drawing(pad))
    service.manager.backend.remove(plugged)
    service.poll_hotplug()
    assert not service.openrgb.drawing(pad)


# -- the switch -------------------------------------------------------------------

def test_it_is_off_until_turned_on_on_openrgb_s_port(tmp_path):
    settings = IntegrationSettings(str(tmp_path / 'i.json'))
    assert settings.as_dict()['openrgb'] is False
    assert settings.port('openrgb', p.PORT) == 6742
    settings.set_port('openrgb', 16742)
    assert IntegrationSettings(str(tmp_path / 'i.json')).port('openrgb', p.PORT) == 16742
    for wrong in (0, 70000, '6742', True):
        with pytest.raises(ValueError):
            settings.set_port('openrgb', wrong)
    with pytest.raises(ValueError):
        settings.set_port('chroma', 1234)


def test_a_taken_port_is_reported_not_raised(tmp_path):
    blocker = socket.socket()
    blocker.bind(('127.0.0.1', 0))
    blocker.listen(1)
    try:
        service = DaemonService(backend=build_demo_backend(),
                                persistence=Persistence(str(tmp_path / 'p.json')),
                                integrations=IntegrationSettings(str(tmp_path / 'i.json')),
                                chroma_port=0, openrgb_port=blocker.getsockname()[1])
        status = service.rpc_integration_set('openrgb', True)['openrgb']
        assert not status['listening'] and "OpenRGB's own" in status['error']
        service.stop()
    finally:
        blocker.close()


def test_the_port_can_be_moved(service):
    first = service.rpc_integration_set('openrgb', True)['openrgb']['port']
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    status = service.rpc_integration_set('openrgb', True, port=port)['openrgb']
    assert status['listening'] and status['port'] == port != first
    assert service.integrations.port('openrgb', p.PORT) == port
    sdk = Sdk(port, name='OpenRGB')
    try:
        assert until(lambda: service.rpc_integration_status()['openrgb']['clients'] == ['OpenRGB'])
    finally:
        sdk.close()


def test_it_is_started_with_the_daemon_once_switched_on(tmp_path):
    settings = IntegrationSettings(str(tmp_path / 'i.json'))
    settings.set('openrgb', True)
    service = DaemonService(backend=build_demo_backend(),
                            persistence=Persistence(str(tmp_path / 'p.json')),
                            integrations=settings, chroma_port=0, openrgb_port=0)
    service.start()
    try:
        assert service.rpc_integration_status()['openrgb']['listening']
    finally:
        service.stop()
    assert service.openrgb_server is None
