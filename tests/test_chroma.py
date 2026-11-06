"""Games lighting devices through Razer's Chroma SDK, drawn by the daemon."""
from __future__ import annotations

import json
import time
import urllib.request

import pytest

from openrazer_win.chroma import ChromaBridge, ChromaError, ChromaServer, bridge as bridges
from openrazer_win.chroma import grid
from openrazer_win.core.ble_device import BleDevice
from openrazer_win.core.persistence import Persistence
from openrazer_win.daemon.demo import build_demo_backend
from openrazer_win.daemon.server import DaemonService
from openrazer_win.devices import get_database
from openrazer_win.integrations import IntegrationSettings

from .conftest import KITTY_V2_BT, FakeBleTransport, make_device

RED, BLUE, GREEN = 0x0000FF, 0xFF0000, 0x00FF00       # COLORREF: red in the low byte


def keyboard_grid(colour=RED):
    return [[colour] * 22 for _ in range(6)]


# -- what an effect comes to -------------------------------------------------

def test_a_colourref_has_red_in_the_low_byte():
    assert grid.colour_of(0x00D47800) == (0x00, 0x78, 0xD4)


def test_a_static_effect_fills_the_grid():
    look = grid.parse('mouse', {'effect': 'CHROMA_STATIC', 'param': {'color': BLUE}})
    assert len(look) == 9 and len(look[0]) == 7
    assert look[4][3] == (0, 0, 255)


def test_keys_are_drawn_over_the_colours_under_them():
    keys = [[0] * 22 for _ in range(6)]
    keys[3][2] = grid.KEY_FLAG | GREEN                    # A
    look = grid.parse('keyboard', {'effect': 'CHROMA_CUSTOM_KEY',
                                   'param': {'color': keyboard_grid(), 'key': keys}})
    assert look[3][2] == (0, 255, 0) and look[3][3] == (255, 0, 0)


def test_the_extended_keyboard_grid_holds_the_ordinary_one_inside_it():
    extended = [[0] * 24 for _ in range(8)]
    extended[1][1] = RED                                  # Esc's corner
    look = grid.parse('keyboard', {'effect': 'CHROMA_CUSTOM2', 'param': {'color': extended}})
    assert (len(look), len(look[0])) == (6, 22)
    assert look[0][0] == (255, 0, 0)


def test_a_strip_of_leds_is_one_row():
    look = grid.parse('headset', {'effect': 'CHROMA_CUSTOM', 'param': [RED, BLUE, 0, 0, 0]})
    assert look == [[(255, 0, 0), (0, 0, 255), (0, 0, 0), (0, 0, 0), (0, 0, 0)]]


@pytest.mark.parametrize('kind, body', [
    ('keyboard', {'effect': 'CHROMA_WAVE'}),
    ('keyboard', {'effect': 'CHROMA_CUSTOM', 'param': 'red'}),
    ('keyboard', {'effect': 'CHROMA_CUSTOM', 'param': [[0] * 99]}),
    ('toaster', {'effect': 'CHROMA_NONE'}),
])
def test_what_cannot_be_drawn_is_refused(kind, body):
    with pytest.raises(ChromaError):
        grid.parse(kind, body)


def test_leds_are_spread_over_however_many_a_device_has():
    assert grid.resample([1, 2, 3], 5) == [1, 1, 2, 3, 3]
    assert grid.resample([1, 2, 3], 1) == [1]


# -- on the devices ----------------------------------------------------------

class Effects:
    def __init__(self):
        self.cleared, self.restored = [], []

    def clear_serial(self, serial):
        self.cleared.append(serial)

    def restore(self, device):
        self.restored.append(device.serial)


@pytest.fixture
def keyboard(persistence):
    device, _fake = make_device(0x0203, persistence)
    return device


def test_a_game_draws_the_keyboard_key_by_key(keyboard, persistence):
    frames = []
    keyboard.set_key_row = lambda payload: frames.append(bytes(payload))
    bridge = ChromaBridge(lambda: [keyboard], persistence=persistence)
    session = bridge.open({'title': 'A game'})
    looks = keyboard_grid(0)
    looks[3][2] = GREEN
    bridge.show(session, 'keyboard', {'effect': 'CHROMA_CUSTOM', 'param': looks})
    bridge.render()
    assert frames, 'nothing was drawn'
    payload = frames[-1]
    row3 = payload[3 * (3 + 22 * 3):4 * (3 + 22 * 3)]
    assert row3[0] == 3 and row3[3 + 2 * 3:3 + 3 * 3] == bytes((0, 255, 0))
    assert bridge.drawing(keyboard)


def test_an_unchanged_frame_is_not_sent_again(keyboard, persistence):
    frames = []
    keyboard.set_key_row = lambda payload: frames.append(payload)
    bridge = ChromaBridge(lambda: [keyboard], persistence=persistence)
    session = bridge.open({})
    bridge.show(session, 'keyboard', {'effect': 'CHROMA_STATIC', 'param': {'color': RED}})
    bridge.render()
    bridge.render()
    assert len(frames) == 1


def test_what_a_game_shows_is_not_kept_as_the_user_s_lighting(keyboard, persistence):
    keyboard.set_static(0, 0, 255)
    bridge = ChromaBridge(lambda: [keyboard], persistence=persistence)
    session = bridge.open({})
    bridge.show(session, 'keyboard', {'effect': 'CHROMA_STATIC', 'param': {'color': RED}})
    bridge.render()
    assert persistence.get(keyboard.serial, 'backlight', 'effect') == 'static'
    assert persistence.get(keyboard.serial, 'backlight', 'colors')[:3] == [0, 0, 255]


def test_the_user_s_lighting_comes_back_when_the_game_ends(keyboard, persistence):
    keyboard.set_static(0, 0, 255)
    effects = Effects()
    restored = []
    keyboard.restore = lambda: restored.append(True)
    bridge = ChromaBridge(lambda: [keyboard], effects=effects, persistence=persistence)
    session = bridge.open({})
    bridge.show(session, 'keyboard', {'effect': 'CHROMA_STATIC', 'param': {'color': RED}})
    bridge.render()
    assert effects.cleared == [keyboard.serial]
    bridge.close(session)
    bridge.render()
    assert restored and effects.restored == [keyboard.serial]
    assert not bridge.drawing(keyboard)
    keyboard.set_static(0, 255, 0)
    assert persistence.get(keyboard.serial, 'backlight', 'colors')[:3] == [0, 255, 0]


def test_a_game_that_goes_quiet_lets_go(keyboard, persistence):
    now = [100.0]
    keyboard.restore = lambda: None
    bridge = ChromaBridge(lambda: [keyboard], persistence=persistence, clock=lambda: now[0])
    session = bridge.open({})
    bridge.show(session, 'keyboard', {'effect': 'CHROMA_STATIC', 'param': {'color': RED}})
    bridge.render()
    now[0] += bridges.SESSION_TIMEOUT - 1
    bridge.heartbeat(session)
    now[0] += bridges.SESSION_TIMEOUT - 1
    bridge._expire()
    assert bridge.status()['apps'] == ['A Chroma app']
    now[0] += bridges.SESSION_TIMEOUT + 1
    bridge._expire()
    bridge.render()
    assert bridge.status()['apps'] == [] and not bridge.drawing(keyboard)


def test_the_latest_game_to_show_something_is_on_the_devices(keyboard, persistence):
    now = [0.0]
    bridge = ChromaBridge(lambda: [keyboard], persistence=persistence, clock=lambda: now[0])
    first, second = bridge.open({'title': 'One'}), bridge.open({'title': 'Two'})
    bridge.show(first, 'keyboard', {'effect': 'CHROMA_STATIC', 'param': {'color': RED}})
    now[0] += 1
    bridge.show(second, 'keyboard', {'effect': 'CHROMA_STATIC', 'param': {'color': BLUE}})
    assert bridge.status()['showing'] == 'Two'


def test_effects_made_ahead_are_shown_by_id(keyboard, persistence):
    bridge = ChromaBridge(lambda: [keyboard], persistence=persistence)
    session = bridge.open({})
    effect = bridge.create(session, 'keyboard', {'effect': 'CHROMA_STATIC', 'param': {'color': RED}})
    assert not bridge.status()['showing']
    bridge.apply(session, [effect])
    assert bridge.status()['showing'] == 'A Chroma app'
    bridge.delete(session, [effect])
    with pytest.raises(ChromaError):
        bridge.apply(session, [effect])


def test_the_headset_s_first_two_leds_are_its_ears(persistence):
    headset = BleDevice(get_database().get(0x1532, KITTY_V2_BT), FakeBleTransport(), persistence)
    bridge = ChromaBridge(lambda: [headset], persistence=persistence)
    session = bridge.open({})
    bridge.show(session, 'headset', {'effect': 'CHROMA_CUSTOM', 'param': [RED, BLUE, 0, 0, 0]})
    bridge.render()
    assert headset.transport.colour_writes[-1][-6:] == bytes((255, 0, 0, 0, 0, 255))


def test_a_mouse_without_a_grid_takes_its_parts_from_razer_s(persistence):
    mouse, _fake = make_device(0x0078, persistence)        # Viper: a logo
    shown = []
    mouse.set_static = lambda r, g, b, zone='backlight': shown.append((zone, (r, g, b)))
    look = [[0] * 7 for _ in range(9)]
    look[7][3] = GREEN
    bridge = ChromaBridge(lambda: [mouse], persistence=persistence)
    session = bridge.open({})
    bridge.show(session, 'mouse', {'effect': 'CHROMA_CUSTOM2', 'param': look})
    bridge.render()
    assert ('logo', (0, 255, 0)) in shown


def test_a_device_of_a_kind_the_game_does_not_draw_is_left_alone(keyboard, persistence):
    bridge = ChromaBridge(lambda: [keyboard], persistence=persistence)
    session = bridge.open({})
    bridge.show(session, 'mouse', {'effect': 'CHROMA_STATIC', 'param': {'color': RED}})
    bridge.render()
    assert not bridge.drawing(keyboard)


# -- over Razer's REST API -----------------------------------------------------

def request(method, url, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=5) as answer:
        return json.loads(answer.read() or b'null')


@pytest.fixture
def rest(keyboard, persistence):
    bridge = ChromaBridge(lambda: [keyboard], persistence=persistence)
    server = ChromaServer(bridge, port=0)
    server.start()
    yield bridge, 'http://127.0.0.1:{0}'.format(server.server_address[1])
    server.stop()


def test_a_program_goes_through_the_whole_api(rest):
    bridge, base = rest
    assert 'version' in request('GET', base + '/razer/chromasdk')
    opened = request('POST', base + '/razer/chromasdk', {
        'title': 'Test', 'description': 'd', 'author': {'name': 'n', 'contact': 'c'},
        'device_supported': ['keyboard'], 'category': 'application'})
    uri = opened['uri'].replace('localhost', '127.0.0.1')
    assert request('PUT', uri + '/heartbeat')['tick'] == 1
    assert request('PUT', uri + '/keyboard', {'effect': 'CHROMA_STATIC',
                                              'param': {'color': RED}})['result'] == 0
    assert bridge.status()['showing'] == 'Test'
    made = request('POST', uri + '/keyboard', {'effect': 'CHROMA_NONE'})
    assert request('PUT', uri + '/effect', {'id': made['id']})['result'] == 0
    assert request('DELETE', uri + '/effect', {'ids': [made['id']]})['result'] == 0
    assert request('DELETE', uri)['result'] == 0
    assert bridge.status()['apps'] == []


def test_a_bad_effect_is_answered_not_dropped(rest):
    _bridge, base = rest
    uri = request('POST', base + '/razer/chromasdk', {'title': 'T'})['uri'].replace(
        'localhost', '127.0.0.1')
    with pytest.raises(urllib.error.HTTPError) as answer:
        request('PUT', uri + '/keyboard', {'effect': 'CHROMA_WAVE'})
    assert answer.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as answer:
        request('PUT', base + '/sessions/999/chromasdk/keyboard', {'effect': 'CHROMA_NONE'})
    assert answer.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as answer:
        request('PUT', uri + '/effect', ['not', 'an', 'object'])
    assert answer.value.code == 400


def test_a_browser_may_ask_first(rest):
    _bridge, base = rest
    req = urllib.request.Request(base + '/razer/chromasdk', method='OPTIONS')
    with urllib.request.urlopen(req, timeout=5) as answer:
        assert answer.status == 204
        assert answer.headers['Access-Control-Allow-Origin'] == '*'


# -- in the daemon -----------------------------------------------------------

def test_the_daemon_answers_once_switched_on(tmp_path):
    service = DaemonService(backend=build_demo_backend(),
                            persistence=Persistence(str(tmp_path / 'p.json')),
                            integrations=IntegrationSettings(str(tmp_path / 'i.json')),
                            chroma_port=0)
    service.start()
    try:
        assert not service.rpc_integration_status()['chroma']['listening']
        status = service.rpc_integration_set('chroma', True)['chroma']
        assert status['listening'] and status['enabled']
        base = 'http://127.0.0.1:{0}'.format(status['port'])
        uri = request('POST', base + '/razer/chromasdk', {'title': 'Game'})['uri'].replace(
            'localhost', '127.0.0.1')
        request('PUT', uri + '/keyboard', {'effect': 'CHROMA_STATIC', 'param': {'color': RED}})
        deadline = time.monotonic() + 3.0
        while not service.rpc_integration_status()['chroma']['drawing'] and time.monotonic() < deadline:
            time.sleep(0.02)
        assert service.rpc_integration_status()['chroma']['drawing']
        assert not service.rpc_integration_set('chroma', False)['chroma']['listening']
    finally:
        service.stop()


def test_a_taken_port_is_reported_not_raised(tmp_path):
    import socket
    blocker = socket.socket()
    blocker.bind(('127.0.0.1', 0))
    blocker.listen(1)
    try:
        service = DaemonService(backend=build_demo_backend(),
                                persistence=Persistence(str(tmp_path / 'p.json')),
                                integrations=IntegrationSettings(str(tmp_path / 'i.json')),
                                chroma_port=blocker.getsockname()[1])
        status = service.rpc_integration_set('chroma', True)['chroma']
        assert not status['listening'] and 'Synapse' in status['error']
        service.stop()
    finally:
        blocker.close()
