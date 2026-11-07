"""JSON-RPC client for the openrazer-win daemon."""
from __future__ import annotations

import itertools
import os
import socket
import sys
import threading
from typing import Any, Optional

from ..daemon import protocol
from ..daemon.protocol import Endpoint, RpcError

DEFAULT_TIMEOUT = 15.0

#: How long to wait for a loopback connect before asking whether the daemon is
#: even alive.  A live daemon accepts at once, from the kernel's backlog; but
#: Windows takes about 2 s to refuse a connection to a closed loopback port,
#: and a stale endpoint file at logon is exactly that.
LOOPBACK_CONNECT_TIMEOUT = 0.25

_LOOPBACK = ('::1', 'localhost')


def _is_loopback(host: str) -> bool:
    return host in _LOOPBACK or host.startswith('127.')


def process_alive(pid: int) -> bool:
    """Whether a process with this id is running.  Unknown is answered "yes".

    `os.kill(pid, 0)` is not an existence check on Windows -- there it ends the
    process -- so Windows asks for the exit code instead.  Any doubt means
    "alive": the caller uses this to give up on a daemon, and giving up on one
    that is running would get a second started beside it.
    """
    if pid <= 0:
        return True
    if sys.platform != 'win32':
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True       # PermissionError: it exists, it is not ours
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(0x1000, False, pid)     # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        # ERROR_INVALID_PARAMETER (87): no such process.  Anything else, such
        # as access denied, means there is one.
        return ctypes.get_last_error() != 87
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == 259                           # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


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
            connection = self._open_connection()
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

    def _open_connection(self) -> socket.socket:
        address = (self.endpoint.host, self.endpoint.port)
        if not _is_loopback(self.endpoint.host):
            return socket.create_connection(address, timeout=self.timeout)
        pid = self.endpoint.pid
        if not process_alive(pid):
            raise ConnectionRefusedError(
                'process {0} named in the endpoint file is gone'.format(pid))
        if self.timeout is None or self.timeout <= LOOPBACK_CONNECT_TIMEOUT:
            return socket.create_connection(address, timeout=self.timeout)
        try:
            connection = socket.create_connection(address, timeout=LOOPBACK_CONNECT_TIMEOUT)
        except socket.timeout:
            # Slow to accept: busy, or nothing there.  A daemon that may be
            # alive -- or whose id is not recorded -- gets the full time.
            if not process_alive(pid):
                raise
            return socket.create_connection(address, timeout=self.timeout)
        # The short timeout was for connecting only.
        connection.settimeout(self.timeout)
        return connection

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
