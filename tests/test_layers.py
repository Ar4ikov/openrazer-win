"""Per-key lighting from layers, and host-drawn effects that outlive the daemon."""
from __future__ import annotations

import threading

import pytest

from openrazer_win.core.manager import DeviceManager
from openrazer_win.daemon.demo import build_demo_backend
from openrazer_win.daemon.protocol import INVALID_PARAMS, RpcError
from openrazer_win.daemon.server import DaemonServer, DaemonService
from openrazer_win.effects.engine import EffectEngine, EffectThread
from openrazer_win.effects.frame import Frame
from openrazer_win.effects.layers import (
    LayerError, LayerStack, clean_layers, twinkle_phase,
)
from openrazer_win.hid.fake import FakeHidBackend, FakeRazerDevice

from .conftest import KEYBOARD_PID, make_device
from .test_effects import FakeKeySource, wait_for


def stack(*layers, rows=6, columns=22):
    return LayerStack(clean_layers(list(layers), rows, columns), rows, columns)


def layer(kind, cells, colours=((255, 0, 0),), **extra):
    return dict({'kind': kind, 'cells': [list(c) for c in cells],
                 'colours': [list(c) for c in colours]}, **extra)


# -- validation ---------------------------------------------------------------

def test_layers_are_validated_and_cells_outside_the_matrix_dropped():
    clean = clean_layers([layer('static', [(0, 0), (9, 9), (0, 0)], [(300, -5, 7)])], 6, 22)
    assert clean[0]['cells'] == [[0, 0]]
    assert clean[0]['colours'] == [[255, 0, 7]]
    assert clean[0]['speed'] == 1.0 and clean[0]['enabled'] is True


@pytest.mark.parametrize('bad', [
    'not a list',
    [{'kind': 'disco', 'cells': []}],
    [{'kind': 'static', 'cells': [['a', 'b']]}],
    [{'kind': 'static', 'cells': [], 'colours': [['x']]}],
    [{'kind': 'static', 'cells': []}] * 17,
])
def test_layers_that_cannot_be_drawn_are_refused(bad):
    with pytest.raises(LayerError):
        clean_layers(bad, 6, 22)


# -- drawing -------------------------------------------------------------------

def test_upper_layers_cover_lower_ones():
    frame = stack(layer('static', [(0, 0), (0, 1)], [(0, 0, 255)]),
                  layer('static', [(0, 1)], [(255, 0, 0)])).render(Frame(6, 22), 0.0)
    assert frame.get(0, 0) == (0, 0, 255)
    assert frame.get(0, 1) == (255, 0, 0)
    assert frame.get(0, 2) == (0, 0, 0)


def test_a_disabled_layer_is_not_drawn():
    frame = stack(layer('static', [(0, 0)], enabled=False)).render(Frame(6, 22), 0.0)
    assert frame.get(0, 0) == (0, 0, 0)


def test_a_gradient_runs_across_the_selection():
    frame = stack(layer('gradient', [(1, 2), (1, 7), (1, 12)],
                        [(255, 0, 0), (0, 0, 255)])).render(Frame(6, 22), 0.0)
    assert frame.get(1, 2) == (255, 0, 0)
    assert frame.get(1, 12) == (0, 0, 255)
    assert frame.get(1, 7) == (127, 0, 127)


def test_breathing_is_dark_then_bright():
    layers = stack(layer('breath', [(0, 0)], [(200, 100, 0)]))
    assert layers.render(Frame(6, 22), 0.0).get(0, 0) == (0, 0, 0)
    assert layers.render(Frame(6, 22), 2.0).get(0, 0) == (200, 100, 0)
    assert layers.animated and not layers.key_driven


def test_a_wave_moves_with_time():
    layers = stack(layer('wave', [(0, c) for c in range(22)]))
    assert layers.render(Frame(6, 22), 0.0).copy() != layers.render(Frame(6, 22), 0.5)


def test_reactive_lights_the_pressed_key_over_what_is_below():
    layers = stack(layer('static', [(2, 3), (2, 4)], [(10, 10, 10)]),
                   layer('reactive', [(2, 3), (2, 4)], [(250, 0, 0)]))
    assert layers.key_driven and not layers.animated
    frame = layers.render(Frame(6, 22), 0.0, [(0.0, (2, 3))])
    assert frame.get(2, 3) == (250, 0, 0)
    assert frame.get(2, 4) == (10, 10, 10)
    faded = layers.render(Frame(6, 22), 5.0, [(5.0, (2, 3))])
    assert faded.get(2, 3) == (10, 10, 10)


def test_a_ripple_spreads_from_the_key():
    layers = stack(layer('ripple', [(r, c) for r in range(6) for c in range(22)]))
    early = layers.render(Frame(6, 22), 0.0, [(0.05, (3, 10))]).copy()
    later = layers.render(Frame(6, 22), 0.0, [(0.4, (3, 10))])
    assert early.get(3, 11) != (0, 0, 0)
    assert later.get(3, 11) == (0, 0, 0)
    assert later.get(3, 20) != (0, 0, 0) or later.get(3, 0) != (0, 0, 0)


def test_the_twinkle_is_the_same_every_time():
    # The editor's preview computes the same number in Rust.
    assert twinkle_phase(0, 0) == pytest.approx(0.3262379, abs=1e-6)
    assert twinkle_phase(2, 5) == twinkle_phase(2, 5)
    assert 0.0 <= twinkle_phase(5, 21) < 1.0


# -- the effect thread -----------------------------------------------------------

@pytest.fixture
def keyboard(persistence):
    device, fake = make_device(KEYBOARD_PID, persistence)
    yield device, fake
    device.close()


def test_static_layers_are_pushed_once_and_left_alone(keyboard):
    device, fake = keyboard
    pushes = []
    device.set_key_row = lambda payload: pushes.append(bytes(payload))
    options = {'layers': clean_layers([layer('static', [(0, 1)])], 6, 22),
               'refresh_rate': 0.01}
    thread = EffectThread(device, 'layers', options, FakeKeySource())
    thread.start()
    try:
        assert wait_for(lambda: pushes)
        threading.Event().wait(0.15)
        assert len(pushes) == 1
    finally:
        thread.stop()
        thread.join(timeout=2)
    # Row 0 carries red at column 1.
    assert pushes[0][:3] == bytes((0, 0, 21)) and pushes[0][6:9] == bytes((255, 0, 0))


def test_a_key_driven_stack_listens_for_presses(keyboard):
    device, _fake = keyboard
    keys = FakeKeySource()
    options = {'layers': clean_layers([layer('reactive', [(3, 2)])], 6, 22),
               'refresh_rate': 0.01}
    thread = EffectThread(device, 'layers', options, keys)
    thread.start()
    try:
        assert wait_for(lambda: keys.subscribers)
    finally:
        thread.stop()
        thread.join(timeout=2)
    assert not keys.subscribers


# -- remembering -------------------------------------------------------------------

@pytest.fixture
def engine(persistence):
    backend = FakeHidBackend([FakeRazerDevice(KEYBOARD_PID, 'Razer BlackWidow Chroma',
                                              interface=1)])
    manager = DeviceManager(backend=backend, persistence=persistence)
    manager.scan()
    engine = EffectEngine(manager)
    engine.keys = FakeKeySource()
    yield engine, manager.devices[0], persistence
    engine.stop()
    manager.close()


def test_an_effect_is_remembered_and_forgotten(engine):
    engine, device, persistence = engine
    engine.set_effect(device, 'layers', {'layers': [layer('static', [(0, 1)])]})
    saved = persistence.get_device_value(device.serial, 'software')
    assert saved['effect'] == 'layers'
    assert saved['options']['layers'][0]['cells'] == [[0, 1]]
    engine.set_effect(device, 'none', {})
    assert persistence.get_device_value(device.serial, 'software') is None


def test_a_remembered_effect_comes_back(engine):
    engine, device, _persistence = engine
    engine.set_effect(device, 'wave_soft', {'refresh_rate': 0.05})
    engine.clear(device)                       # as when the device is unplugged
    assert engine.status()['active'] == {}
    assert engine.restore(device)
    assert engine.status()['active'] == {device.serial: 'wave_soft'}


def test_a_bad_stack_is_refused_and_nothing_is_remembered(engine):
    engine, device, persistence = engine
    with pytest.raises(ValueError):
        engine.set_effect(device, 'layers', {'layers': [{'kind': 'disco'}]})
    assert persistence.get_device_value(device.serial, 'software') is None


# -- through the daemon ---------------------------------------------------------------

@pytest.fixture
def service(tmp_path):
    from openrazer_win.core.persistence import Persistence
    from openrazer_win.core.profiles import ProfileStore
    service = DaemonService(backend=build_demo_backend(),
                            persistence=Persistence(str(tmp_path / 'p.json')),
                            profiles=ProfileStore(str(tmp_path / 'profiles.json')))
    service.effects.keys = FakeKeySource()
    service.start()
    yield service
    service.stop()


def keyboard_serial(service):
    return next(d.serial for d in service.manager.devices if d.type == 'keyboard')


def test_a_device_effect_on_the_backlight_stops_the_layers(service):
    serial = keyboard_serial(service)
    service.rpc_effect(serial, 'layers', {'layers': [layer('static', [(0, 1)])]})
    assert service.rpc_effect_status()['active'] == {serial: 'layers'}
    service.rpc_call(serial, 'set_static', [0, 255, 0], {'zone': 'backlight'})
    assert service.rpc_effect_status()['active'] == {}
    assert service.persistence.get_device_value(serial, 'software') is None


def test_a_bad_stack_is_an_invalid_request(service):
    with pytest.raises(RpcError) as raised:
        service.rpc_effect(keyboard_serial(service), 'layers', {'layers': 'nope'})
    assert raised.value.code == INVALID_PARAMS


def test_layers_come_back_with_the_device(service):
    serial = keyboard_serial(service)
    service.rpc_effect(serial, 'layers', {'layers': [layer('static', [(0, 1)])]})
    device = service.manager.by_serial(serial)
    service.effects.clear(device)
    service._initialise(device)
    assert service.rpc_effect_status()['active'] == {serial: 'layers'}


def test_profiles_carry_the_layers(service):
    serial = keyboard_serial(service)
    service.rpc_effect(serial, 'layers', {'layers': [layer('static', [(0, 1)])]})
    service.rpc_profile_save(serial, 'Layers')
    service.rpc_effect(serial, 'none', {})
    service.rpc_call(serial, 'set_spectrum', [], {'zone': 'backlight'})
    service.rpc_profile_save(serial, 'Plain')

    service.rpc_profile_load(serial, 'Layers')
    assert service.rpc_effect_status()['active'] == {serial: 'layers'}
    service.rpc_profile_load(serial, 'Plain')
    assert service.rpc_effect_status()['active'] == {}


def test_the_effect_status_shows_the_options(service):
    serial = keyboard_serial(service)
    service.rpc_effect(serial, 'layers', {'layers': [layer('static', [(0, 1)])]})
    assert service.rpc_effect_status()['options'][serial]['layers'][0]['kind'] == 'static'


def test_layers_over_rpc(tmp_path):
    from openrazer_win.client.rpc import RpcClient
    from openrazer_win.core.persistence import Persistence
    service = DaemonService(backend=build_demo_backend(),
                            persistence=Persistence(str(tmp_path / 'p.json')))
    service.effects.keys = FakeKeySource()
    service.start()
    server = DaemonServer(service, port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05},
                              daemon=True)
    thread.start()
    try:
        client = RpcClient(server.endpoint, timeout=5)
        serial = keyboard_serial(service)
        result = client.call('effect.set', serial=serial, effect='layers',
                             options={'layers': [layer('breath', [(1, 1)])]})
        assert result['effect'] == 'layers'
        saved = client.call('daemon.persistence')[serial]['software']
        assert saved['options']['layers'][0]['kind'] == 'breath'
        client.close()
    finally:
        server.shutdown()
        server.server_close()
        service.stop()
        thread.join(timeout=2)
