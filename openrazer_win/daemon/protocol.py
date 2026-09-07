"""The daemon's wire protocol and the endpoint file clients discover it with.

Linux OpenRazer exposes the daemon on the D-Bus session bus.  Windows has no
equivalent, so the port uses newline-delimited JSON-RPC 2.0 over a loopback TCP
socket.  The port number and a random bearer token live in a small JSON file
under ``%LOCALAPPDATA%``, readable only by the user who started the daemon --
the same trust boundary a session bus gives you.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
from dataclasses import dataclass
from typing import Any, Optional

from ..core.persistence import default_config_dir

#: Every message is one JSON object terminated by a newline.
ENCODING = 'utf-8'
TERMINATOR = b'\n'

#: Refuse anything larger; a well-formed request is a few hundred bytes.
MAX_MESSAGE_SIZE = 1 << 20


def endpoint_path() -> str:
    return os.path.join(default_config_dir(), 'daemon.json')


@dataclass(frozen=True)
class Endpoint:
    """Where the daemon is listening, and the token needed to talk to it."""

    host: str
    port: int
    token: str
    pid: int
    version: str

    def to_dict(self) -> dict:
        return {'host': self.host, 'port': self.port, 'token': self.token,
                'pid': self.pid, 'version': self.version}

    @classmethod
    def from_dict(cls, data: dict) -> 'Endpoint':
        return cls(host=data.get('host', '127.0.0.1'), port=int(data['port']),
                   token=data['token'], pid=int(data.get('pid', 0)),
                   version=data.get('version', ''))

    def write(self, path: Optional[str] = None) -> str:
        path = path or endpoint_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Create with owner-only permissions before anything is written to it.
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, 'w', encoding=ENCODING) as handle:
            json.dump(self.to_dict(), handle)
        return path

    @classmethod
    def read(cls, path: Optional[str] = None) -> 'Endpoint':
        path = path or endpoint_path()
        with open(path, encoding=ENCODING) as handle:
            return cls.from_dict(json.load(handle))

    @staticmethod
    def remove(path: Optional[str] = None) -> None:
        try:
            os.remove(path or endpoint_path())
        except OSError:
            pass


def new_token() -> str:
    return secrets.token_urlsafe(32)


class RpcError(Exception):
    """A JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self) -> dict:
        error: dict = {'code': self.code, 'message': self.message}
        if self.data is not None:
            error['data'] = self.data
        return error


# JSON-RPC 2.0 reserved codes, plus this daemon's own range.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNAUTHORIZED = -32000
DEVICE_NOT_FOUND = -32001
NOT_SUPPORTED = -32002
DEVICE_ERROR = -32003


def encode(message: dict) -> bytes:
    return json.dumps(message, separators=(',', ':')).encode(ENCODING) + TERMINATOR


def read_message(stream) -> Optional[dict]:
    """Read one newline-delimited JSON object, or ``None`` at end of stream."""
    line = stream.readline(MAX_MESSAGE_SIZE + 1)
    if not line:
        return None
    if len(line) > MAX_MESSAGE_SIZE:
        raise RpcError(INVALID_REQUEST, 'message too large')
    try:
        message = json.loads(line.decode(ENCODING))
    except (ValueError, UnicodeDecodeError) as error:
        raise RpcError(PARSE_ERROR, 'malformed JSON: {0}'.format(error)) from error
    if not isinstance(message, dict):
        raise RpcError(INVALID_REQUEST, 'expected a JSON object')
    return message


def find_free_port(host: str = '127.0.0.1') -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return probe.getsockname()[1]
