"""The daemon, its RPC surface, and the client library that talks to it."""
from __future__ import annotations

import threading

import pytest

from openrazer_win.client import DeviceManager
from openrazer_win.client.rpc import DaemonUnavailable, RpcClient
from openrazer_win.daemon.demo import build_demo_backend
from openrazer_win.daemon.protocol import (
    METHOD_NOT_FOUND, NOT_SUPPORTED, UNAUTHORIZED, Endpoint, RpcError,
)
from openrazer_win.daemon.server import DaemonServer, DaemonService


@pytest.fixture
def daemon(tmp_path):
    """A live daemon on a random loopback port, backed by emulated devices."""
    from openrazer_win.core.persistence import Persistence
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
def client(daemon):
    connection = RpcClient(daemon.endpoint, timeout=5)
    yield connection
    connection.close()


# -- transport --------------------------------------------------------------

def test_version_reports_the_emulator_backend(client):
    info = client.call('daemon.version')
    assert info['backend'] == 'fake'
    assert info['devices'] == 3


def test_devices_are_listed_with_serial_and_firmware(client):
    devices = client.call('devices.list')
    assert len(devices) == 3
    assert all(device['serial'].startswith('DEMO') for device in devices)
    assert {device['type'] for device in devices} == {'keyboard', 'mouse', 'mousemat'}


def test_a_wrong_token_is_rejected(daemon):
    bad = Endpoint(host=daemon.endpoint.host, port=daemon.endpoint.port,
                   token='not-the-token', pid=0, version='0')
    connection = RpcClient(bad, timeout=5)
    with pytest.raises(RpcError) as raised:
        connection.call('daemon.version')
    assert raised.value.code == UNAUTHORIZED
    connection.close()


def test_unknown_methods_are_refused(client):
    with pytest.raises(RpcError) as raised:
        client.call('daemon.rm_rf')
    assert raised.value.code == METHOD_NOT_FOUND


def test_device_call_is_an_allowlist_not_getattr(client):
    serial = client.call('devices.list')[0]['serial']
    with pytest.raises(RpcError) as raised:
        client.call('device.call', serial=serial, method='close', args=[], kwargs={})
    assert raised.value.code == METHOD_NOT_FOUND


def test_unsupported_operations_report_not_supported(client):
    mouse = next(d for d in client.call('devices.list') if d['type'] == 'mouse')
    with pytest.raises(RpcError) as raised:
        client.call('device.call', serial=mouse['serial'],
                    method='set_wheel', args=[1], kwargs={'zone': 'logo'})
    assert raised.value.code == NOT_SUPPORTED


def test_a_bad_request_does_not_kill_the_connection(client):
    with pytest.raises(RpcError):
        client.call('nope')
    assert client.call('daemon.version')['devices'] == 3


def test_missing_endpoint_file_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr('openrazer_win.daemon.protocol.endpoint_path',
                        lambda: str(tmp_path / 'absent.json'))
    with pytest.raises(DaemonUnavailable, match='not running'):
        RpcClient()


def test_endpoint_file_round_trips(tmp_path):
    path = str(tmp_path / 'daemon.json')
    endpoint = Endpoint('127.0.0.1', 5000, 'token', 42, '1.0.0')
    endpoint.write(path)
    assert Endpoint.read(path) == endpoint
    Endpoint.remove(path)
    with pytest.raises(FileNotFoundError):
        Endpoint.read(path)


# -- client library ---------------------------------------------------------

@pytest.fixture
def manager(daemon):
    instance = DeviceManager(endpoint=daemon.endpoint)
    yield instance
    instance.close()


def test_client_sees_every_device(manager):
    assert len(manager.devices) == 3
    assert manager.by_name('Viper')


def test_client_applies_an_effect(manager):
    keyboard = next(d for d in manager.devices if d.type == 'keyboard')
    keyboard.fx.static(255, 0, 0)
    keyboard.fx.spectrum()
    keyboard.fx.wave(1)


def test_client_brightness_property(manager):
    keyboard = next(d for d in manager.devices if d.type == 'keyboard')
    keyboard.brightness = 40
    assert keyboard.brightness == pytest.approx(40, abs=0.5)


def test_client_dpi_property(manager):
    mouse = next(d for d in manager.devices if d.type == 'mouse')
    mouse.dpi = (2400, 2400)
    assert mouse.dpi == (2400, 2400)
    mouse.dpi = 800
    assert mouse.dpi == (800, 800)


def test_client_poll_rate_property(manager):
    mouse = next(d for d in manager.devices if d.type == 'mouse')
    mouse.poll_rate = 500
    assert mouse.poll_rate == 500
    assert 1000 in mouse.supported_poll_rates


def test_client_zone_selection_follows_capabilities(manager):
    mouse = next(d for d in manager.devices if d.type == 'mouse')
    assert mouse.primary_zone() == 'logo'
    assert mouse.zone_for_effect('static') == 'logo'
    assert mouse.zone_with_brightness() == 'logo'


def test_advanced_matrix_draws_a_frame(manager):
    keyboard = next(d for d in manager.devices if d.type == 'keyboard')
    matrix = keyboard.fx.advanced
    matrix[0, 0] = (255, 0, 0)
    assert matrix[0, 0] == (255, 0, 0)
    matrix.draw()


def test_software_effects_need_the_engine(manager):
    keyboard = next(d for d in manager.devices if d.type == 'keyboard')
    with pytest.raises(RpcError) as raised:
        keyboard.fx.ripple()
    assert raised.value.code == NOT_SUPPORTED


def test_direct_mode_bypasses_the_daemon():
    from openrazer_win.hid.fake import FakeHidBackend, FakeRazerDevice
    backend = FakeHidBackend([FakeRazerDevice(0x0203, 'Razer BlackWidow Chroma',
                                              interface=1)])
    with DeviceManager(direct=True, hid_backend=backend) as manager:
        assert len(manager.devices) == 1
        manager.devices[0].fx.static(0, 255, 0)


def test_the_listening_socket_cannot_be_hijacked(daemon):
    """A second bind on the daemon's port must fail.

    Windows honours SO_REUSEADDR on listening sockets, so leaving it on would
    let any process on the machine bind the same port and intercept device
    commands. The server asks for exclusive use instead.
    """
    import socket as socket_module

    assert daemon.allow_reuse_address is False
    with socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM) as thief:
        thief.setsockopt(socket_module.SOL_SOCKET, socket_module.SO_REUSEADDR, 1)
        with pytest.raises(OSError):
            thief.bind((daemon.endpoint.host, daemon.endpoint.port))


def test_the_daemon_refuses_non_loopback_binds():
    """The only supported bind address is loopback; main() enforces it."""
    from openrazer_win.daemon.main import main

    with pytest.raises(SystemExit):
        main(['--host', '0.0.0.0'])
