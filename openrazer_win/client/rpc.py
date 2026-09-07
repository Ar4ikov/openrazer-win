"""JSON-RPC client for the openrazer-win daemon."""
from __future__ import annotations

import itertools
import socket
import threading
from typing import Any, Optional

from ..daemon import protocol
from ..daemon.protocol import Endpoint, RpcError

DEFAULT_TIMEOUT = 15.0


class DaemonUnavailable(ConnectionError):
    """The daemon is not running, or its endpoint file is unusable."""


class RpcClient:
    """A thread-safe connection to the daemon."""

    def __init__(self, endpoint: Optional[Endpoint] = None,
                 timeout: float = DEFAULT_TIMEOUT):
        if endpoint is None:
            try:
                endpoint = Endpoint.read()
            except FileNotFoundError as error:
                raise DaemonUnavailable(
                    'the openrazer-win daemon is not running '
                    '(no endpoint file at {0})'.format(protocol.endpoint_path())
                ) from error
            except (OSError, ValueError, KeyError) as error:
                raise DaemonUnavailable(
                    'unreadable endpoint file: {0}'.format(error)) from error
        self.endpoint = endpoint
        self.timeout = timeout
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._socket: Optional[socket.socket] = None
        self._stream = None

    # -- connection --------------------------------------------------------
    def connect(self) -> None:
        with self._lock:
            self._connect_locked()

    def _connect_locked(self) -> None:
        if self._socket is not None:
            return
        try:
            connection = socket.create_connection(
                (self.endpoint.host, self.endpoint.port), timeout=self.timeout)
        except OSError as error:
            # An endpoint file with nothing listening behind it means the
            # daemon died without cleaning up, which is worth saying plainly
            # rather than surfacing a raw connection error.
            raise DaemonUnavailable(
                'the openrazer-win daemon is not running: nothing is listening on '
                '{0}:{1}, so its endpoint file is stale ({2})'.format(
                    self.endpoint.host, self.endpoint.port, error)) from error
        self._socket = connection
        self._stream = connection.makefile('rwb')

    def close(self) -> None:
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.close()
                except OSError:
                    pass
                self._stream = None
            if self._socket is not None:
                try:
                    self._socket.close()
                except OSError:
                    pass
                self._socket = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- requests ----------------------------------------------------------
    def call(self, rpc_method: str, /, **params) -> Any:
        """Invoke `rpc_method`.

        The method name is positional-only so that RPC parameters called
        ``method`` -- which ``device.call`` takes -- cannot collide with it.
        """
        request = {
            'jsonrpc': '2.0',
            'id': next(self._ids),
            'method': rpc_method,
            'params': params,
            'token': self.endpoint.token,
        }
        with self._lock:
            self._connect_locked()
            try:
                self._stream.write(protocol.encode(request))
                self._stream.flush()
                message = protocol.read_message(self._stream)
            except (OSError, ValueError) as error:
                self._reset_locked()
                raise DaemonUnavailable(
                    'lost connection to the daemon: {0}'.format(error)) from error
        if message is None:
            self.close()
            raise DaemonUnavailable('the daemon closed the connection')
        if 'error' in message:
            error = message['error']
            raise RpcError(error.get('code', protocol.INTERNAL_ERROR),
                           error.get('message', 'unknown error'),
                           error.get('data'))
        return message.get('result')

    def _reset_locked(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                pass
            self._stream = None
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None


def is_daemon_running() -> bool:
    """Cheap liveness probe used by the CLI and the tray icon."""
    try:
        client = RpcClient(timeout=2.0)
        client.call('daemon.version')
        client.close()
    except (DaemonUnavailable, RpcError, OSError):
        return False
    return True
