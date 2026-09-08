"""The openrazer-win daemon.

Owns the HID handles, serialises access to them, watches for hot-plug, and
serves an explicitly whitelisted RPC surface to local clients.
"""
from __future__ import annotations

import logging
import os
import socket
import socketserver
import sys
import threading
import time
from typing import Any, Callable, Optional

from .._version import __version__
from ..core.device import DeviceError, RazerDevice
from ..core.manager import DeviceManager
from ..core.persistence import Persistence
from ..devices.recipes import NotSupported, RecipeError
from ..effects.engine import EffectEngine
from . import protocol
from .protocol import (
    DEVICE_ERROR, DEVICE_NOT_FOUND, INTERNAL_ERROR, INVALID_PARAMS, INVALID_REQUEST,
    METHOD_NOT_FOUND, NOT_SUPPORTED, UNAUTHORIZED, Endpoint, RpcError,
)

logger = logging.getLogger(__name__)

#: How often to rescan the HID bus for plug/unplug events.
HOTPLUG_INTERVAL = 3.0

#: Device methods clients may invoke.  Anything not listed here is refused --
#: the RPC surface is a deliberate allowlist, not ``getattr`` on the device.
DEVICE_METHODS = {
    # lighting
    'set_none', 'set_on', 'set_static', 'set_spectrum', 'set_wave', 'set_wheel',
    'set_reactive', 'trigger_reactive', 'set_blinking', 'set_custom',
    'set_breath_random', 'set_breath_single', 'set_breath_dual', 'set_breath_triple',
    'set_starlight_random', 'set_starlight_single', 'set_starlight_dual',
    'set_brightness', 'get_brightness', 'set_key_row', 'set_custom_frame',
    'set_zone_colours',
    # mouse
    'get_dpi', 'set_dpi', 'get_dpi_stages', 'set_dpi_stages',
    'get_poll_rate', 'set_poll_rate', 'supported_poll_rates',
    'get_scroll_mode', 'set_scroll_mode', 'get_scroll_acceleration',
    'set_scroll_acceleration', 'get_scroll_smart_reel', 'set_scroll_smart_reel',
    # power
    'get_battery_level', 'is_charging', 'get_idle_time', 'set_idle_time',
    'get_low_battery_threshold', 'set_low_battery_threshold',
    # keyboard
    'get_game_mode', 'set_game_mode', 'get_macro_mode', 'set_macro_mode',
    'set_macro_effect', 'set_fn_toggle', 'get_keyboard_layout',
    # misc
    'get_device_mode', 'set_device_mode', 'restore', 'available_effects',
}


def _device_summary(device: RazerDevice) -> dict:
    return {
        'serial': device.serial,
        'name': device.name,
        'type': device.type,
        'vid': device.vid,
        'pid': device.pid,
        'image': device.info.image,
        'firmware': _safe(device, 'firmware_version'),
    }


def _safe(device: RazerDevice, attribute: str) -> Any:
    try:
        return getattr(device, attribute)
    except Exception:  # noqa: BLE001 - a missing optional field must not 500
        return None


class DaemonService:
    """Everything the RPC layer is allowed to touch."""

    def __init__(self, backend=None, persistence: Optional[Persistence] = None,
                 enable_effects: bool = True):
        self.persistence = persistence or Persistence()
        self.manager = DeviceManager(backend=backend, persistence=self.persistence)
        self.effects = EffectEngine(self.manager) if enable_effects else None
        self.started_at = time.time()
        self._lock = threading.RLock()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self.manager.scan()
        for device in self.manager.devices:
            try:
                device.enter_driver_mode()
                device.restore()
            except (DeviceError, RecipeError):
                logger.debug('could not initialise %s', device.name, exc_info=True)
        if self.effects is not None:
            self.effects.start()

    def stop(self) -> None:
        if self.effects is not None:
            self.effects.stop()
        self.manager.close()

    def poll_hotplug(self) -> None:
        known = {device.serial for device in self.manager.devices}
        self.manager.scan()
        for device in self.manager.devices:
            if device.serial not in known:
                try:
                    device.enter_driver_mode()
                    device.restore()
                except (DeviceError, RecipeError):
                    logger.debug('could not initialise %s', device.name, exc_info=True)

    # -- lookups -----------------------------------------------------------
    def _device(self, serial: str) -> RazerDevice:
        device = self.manager.by_serial(serial)
        if device is None:
            matches = self.manager.find(serial)
            if len(matches) == 1:
                return matches[0]
            raise RpcError(DEVICE_NOT_FOUND, 'no such device: {0}'.format(serial))
        return device

    # -- RPC methods -------------------------------------------------------
    def rpc_version(self) -> dict:
        from .._version import UPSTREAM_VERSION
        return {
            'version': __version__,
            'upstream': UPSTREAM_VERSION,
            'pid': os.getpid(),
            'uptime': round(time.time() - self.started_at, 1),
            'backend': self.manager.backend.name,
            'devices': len(self.manager.devices),
        }

    def rpc_list_devices(self) -> list:
        return [_device_summary(device) for device in self.manager.devices]

    def rpc_unsupported(self) -> list:
        return [{'pid': pid, 'product': name} for pid, name in self.manager.unsupported]

    def rpc_rescan(self) -> list:
        self.poll_hotplug()
        return self.rpc_list_devices()

    def rpc_capabilities(self, serial: str) -> dict:
        return self._device(serial).capabilities()

    def rpc_call(self, serial: str, method: str, args=None, kwargs=None) -> Any:
        if method not in DEVICE_METHODS:
            raise RpcError(METHOD_NOT_FOUND,
                           'method not exposed over RPC: {0}'.format(method))
        device = self._device(serial)
        target = getattr(device, method, None)
        if target is None:
            raise RpcError(METHOD_NOT_FOUND, 'unknown method: {0}'.format(method))
        args = list(args or [])
        kwargs = dict(kwargs or {})
        try:
            result = target(*args, **kwargs) if callable(target) else target
        except NotSupported as error:
            raise RpcError(NOT_SUPPORTED, str(error)) from error
        except (DeviceError, RecipeError) as error:
            raise RpcError(DEVICE_ERROR, str(error)) from error
        except TypeError as error:
            raise RpcError(INVALID_PARAMS, str(error)) from error
        self.persistence.save()
        return result

    def rpc_effect(self, serial: str, effect: str, options=None) -> dict:
        if self.effects is None:
            raise RpcError(NOT_SUPPORTED, 'software effects are disabled')
        device = self._device(serial)
        return self.effects.set_effect(device, effect, dict(options or {}))

    def rpc_effect_status(self) -> dict:
        if self.effects is None:
            return {'enabled': False, 'active': {}}
        return self.effects.status()

    def rpc_persistence(self) -> dict:
        return self.persistence.as_dict()


class RpcHandler(socketserver.StreamRequestHandler):
    """One client connection: authenticate once, then serve requests."""

    server: 'DaemonServer'

    def handle(self) -> None:
        authenticated = False
        while True:
            try:
                message = protocol.read_message(self.rfile)
            except RpcError as error:
                self._send_error(None, error)
                return
            except (OSError, ValueError):
                return
            if message is None:
                return

            request_id = message.get('id')
            try:
                if message.get('token') != self.server.endpoint.token:
                    raise RpcError(UNAUTHORIZED, 'invalid or missing token')
                authenticated = True
                result = self._dispatch(message)
            except RpcError as error:
                self._send_error(request_id, error)
                if not authenticated:
                    return
                continue
            except Exception as error:  # noqa: BLE001 - never kill the daemon
                logger.exception('unhandled error serving %s', message.get('method'))
                self._send_error(request_id, RpcError(INTERNAL_ERROR, str(error)))
                continue

            if request_id is not None:
                self.wfile.write(protocol.encode(
                    {'jsonrpc': '2.0', 'id': request_id, 'result': result}))

    def _dispatch(self, message: dict) -> Any:
        method = message.get('method')
        if not isinstance(method, str):
            raise RpcError(INVALID_REQUEST, 'missing method')
        params = message.get('params') or {}
        if not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, 'params must be an object')

        if method == 'daemon.shutdown':
            self.server.request_shutdown()
            return {'stopping': True}

        handler: Optional[Callable] = self.server.methods.get(method)
        if handler is None:
            raise RpcError(METHOD_NOT_FOUND, 'unknown method: {0}'.format(method))
        try:
            return handler(**params)
        except TypeError as error:
            raise RpcError(INVALID_PARAMS, str(error)) from error

    def _send_error(self, request_id, error: RpcError) -> None:
        try:
            self.wfile.write(protocol.encode(
                {'jsonrpc': '2.0', 'id': request_id, 'error': error.to_dict()}))
        except OSError:
            pass


class DaemonServer(socketserver.ThreadingTCPServer):
    """Loopback-only threading server with a bearer token."""

    daemon_threads = True

    # Not SO_REUSEADDR: on Windows it lets another process bind a port that is
    # already being listened on, which would let anything on the machine
    # hijack the daemon's socket.  SO_EXCLUSIVEADDRUSE is the Windows way to
    # say "this port is mine".
    allow_reuse_address = False

    def server_bind(self) -> None:
        if sys.platform == 'win32':
            try:
                self.socket.setsockopt(socket.SOL_SOCKET,
                                       getattr(socket, 'SO_EXCLUSIVEADDRUSE', -5), 1)
            except OSError:
                logger.debug('SO_EXCLUSIVEADDRUSE unavailable', exc_info=True)
        super().server_bind()

    def __init__(self, service: DaemonService, host: str = '127.0.0.1', port: int = 0):
        super().__init__((host, port), RpcHandler)
        self.service = service
        actual_host, actual_port = self.server_address[:2]
        self.endpoint = Endpoint(host=actual_host, port=actual_port,
                                 token=protocol.new_token(), pid=os.getpid(),
                                 version=__version__)
        self.methods = {
            'daemon.version': service.rpc_version,
            'daemon.rescan': service.rpc_rescan,
            'daemon.persistence': service.rpc_persistence,
            'devices.list': service.rpc_list_devices,
            'devices.unsupported': service.rpc_unsupported,
            'device.capabilities': service.rpc_capabilities,
            'device.call': service.rpc_call,
            'effect.set': service.rpc_effect,
            'effect.status': service.rpc_effect_status,
        }
        self._shutdown_requested = threading.Event()

    def request_shutdown(self) -> None:
        self._shutdown_requested.set()
        threading.Thread(target=self.shutdown, daemon=True).start()

    @property
    def stopping(self) -> bool:
        return self._shutdown_requested.is_set()


def serve(backend=None, host: str = '127.0.0.1', port: int = 0,
          enable_effects: bool = True) -> None:
    """Run the daemon until it is asked to stop."""
    service = DaemonService(backend=backend, enable_effects=enable_effects)
    service.start()
    server = DaemonServer(service, host=host, port=port)
    endpoint_file = server.endpoint.write()
    logger.info('listening on %s:%d (endpoint file: %s)',
                server.endpoint.host, server.endpoint.port, endpoint_file)
    logger.info('%d device(s) ready', len(service.manager.devices))

    stop = threading.Event()

    def hotplug_loop() -> None:
        while not stop.wait(HOTPLUG_INTERVAL):
            try:
                service.poll_hotplug()
            except Exception:  # noqa: BLE001 - a bad scan must not kill the daemon
                logger.debug('hot-plug scan failed', exc_info=True)

    watcher = threading.Thread(target=hotplug_loop, name='hotplug', daemon=True)
    watcher.start()

    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        logger.info('interrupted, shutting down')
    finally:
        stop.set()
        server.server_close()
        service.stop()
        Endpoint.remove(endpoint_file)
        logger.info('stopped')
