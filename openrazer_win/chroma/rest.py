"""Razer's Chroma SDK REST API, answered by the daemon.

The same API Synapse answers on the same port: a program opens a session with
``POST /razer/chromasdk``, is given a URI, and puts effects on kinds of device
under it.  Programs written against the REST API work unchanged, and this
port's stand-in for the SDK's DLL speaks it too.

It listens on the loopback address only.  Like Razer's, it asks no password:
all it can do is change the colours of the lights.
"""
from __future__ import annotations

import json
import logging
import re
import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .bridge import VERSION, ChromaBridge
from .grid import GRIDS, ChromaError

logger = logging.getLogger(__name__)

#: Where Razer's SDK listens, and so where programs look for it.
PORT = 54235

#: No effect is anywhere near this big.
MAX_BODY = 512 * 1024

#: What a request is about: ``/sessions/<n>/chromasdk/<what>``.
_SESSION_PATH = re.compile(r'^/sessions/(\d+)/chromasdk(?:/([a-z]+))?/?$')

#: Answers the SDK gives.  ``1168`` is ``ERROR_NOT_FOUND``, as in Razer's own.
SUCCESS = 0
NOT_FOUND = 1168
INVALID_PARAMETER = 87


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server: 'ChromaServer'

    def log_message(self, fmt, *args) -> None:  # pragma: no cover - quiet
        logger.debug('chroma: ' + fmt, *args)

    def do_OPTIONS(self) -> None:
        # What a page in a browser asks before it may send JSON here.
        self._reply(204, None)

    def do_GET(self) -> None:
        self._handle('GET')

    def do_POST(self) -> None:
        self._handle('POST')

    def do_PUT(self) -> None:
        self._handle('PUT')

    def do_DELETE(self) -> None:
        self._handle('DELETE')

    def _body(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length > MAX_BODY:
            raise ChromaError('the request is too large')
        raw = self.rfile.read(length) if length > 0 else b''
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError) as error:
            raise ChromaError('the request is not JSON') from error
        if not isinstance(data, dict):
            raise ChromaError('the request is not a JSON object')
        return data

    def _handle(self, method: str) -> None:
        bridge = self.server.bridge
        path = self.path.split('?', 1)[0]
        try:
            body = self._body()
            if path.rstrip('/') == '/razer/chromasdk':
                if method == 'GET':
                    return self._reply(200, VERSION)
                if method == 'POST':
                    number = bridge.open(body)
                    return self._reply(200, {'sessionid': number, 'uri': '{0}/sessions/{1}/chromasdk'.format(
                        self.server.base, number)})
                return self._reply(405, {'result': INVALID_PARAMETER})
            match = _SESSION_PATH.match(path)
            if match is None:
                return self._reply(404, {'result': NOT_FOUND, 'error': 'nothing here'})
            number, what = int(match.group(1)), match.group(2)
            if what is None:
                if method == 'DELETE':
                    bridge.close(number)
                    return self._reply(200, {'result': SUCCESS})
            elif what == 'heartbeat' and method in ('PUT', 'POST', 'GET'):
                return self._reply(200, {'tick': bridge.heartbeat(number)})
            elif what in GRIDS and method == 'PUT':
                bridge.show(number, what, body)
                return self._reply(200, {'result': SUCCESS})
            elif what in GRIDS and method == 'POST':
                return self._reply(200, {'id': bridge.create(number, what, body), 'result': SUCCESS})
            elif what == 'effect' and method in ('PUT', 'DELETE'):
                ids = body.get('ids') if isinstance(body.get('ids'), list) else [body.get('id')]
                if method == 'PUT':
                    bridge.apply(number, ids)
                else:
                    bridge.delete(number, ids)
                return self._reply(200, {'result': SUCCESS})
            return self._reply(405, {'result': INVALID_PARAMETER})
        except KeyError:
            return self._reply(404, {'result': NOT_FOUND, 'error': 'no such session'})
        except ChromaError as error:
            return self._reply(400, {'result': INVALID_PARAMETER, 'error': str(error)})

    def _reply(self, status: int, payload: Optional[dict]) -> None:
        data = b'' if payload is None else json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        if data:
            self.wfile.write(data)


class ChromaServer(ThreadingHTTPServer):
    """The REST API on the loopback address, feeding a :class:`ChromaBridge`."""

    daemon_threads = True
    # Not shared: if Synapse's SDK already listens here, this one steps aside.
    allow_reuse_address = False

    def __init__(self, bridge: ChromaBridge, port: int = PORT):
        self.bridge = bridge
        super().__init__(('127.0.0.1', port), _Handler)
        self.base = 'http://localhost:{0}'.format(self.server_address[1])
        self._thread: Optional[threading.Thread] = None

    def server_bind(self) -> None:
        # So that nothing else can take the port from under it, as Windows
        # otherwise allows.  And not HTTPServer's own, which looks the
        # machine's name up -- seconds, on some networks, for nothing.
        exclusive = getattr(socket, 'SO_EXCLUSIVEADDRUSE', None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def start(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, kwargs={'poll_interval': 0.3},
                                        name='chroma-rest', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
