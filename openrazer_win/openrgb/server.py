"""OpenRGB's SDK server protocol, answered by the daemon.

OpenRGB, and the programs written against its SDK, connect to a server on
port 6742 and see the devices it has; OpenRGB's own GUI does so from its *SDK
Client* tab.  This server is one of those, with the daemon's devices in it.

It listens on the loopback address only.  Like OpenRGB's, it asks no
password: all a client can do is change the lights.
"""
from __future__ import annotations

import logging
import socket
import socketserver
import struct
import threading
from typing import Callable, Optional

from . import protocol as p
from .bridge import OpenRGBBridge

logger = logging.getLogger(__name__)


class _Client(socketserver.BaseRequestHandler):
    """One connection: packets in, answers out, until it closes."""

    server: 'OpenRGBServer'

    def setup(self) -> None:
        #: Until a client says otherwise, protocol 0, as OpenRGB assumes.
        self.version = 0
        self.name = ''
        self._sending = threading.Lock()
        self.server.joined(self)

    def finish(self) -> None:
        self.server.left(self)

    def _read(self, count: int) -> Optional[bytes]:
        data = b''
        while len(data) < count:
            try:
                chunk = self.request.recv(count - len(data))
            except OSError:
                return None
            if not chunk:
                return None
            data += chunk
        return data

    def send(self, data: bytes) -> None:
        """A whole packet in one go: never one client's answer inside another's."""
        with self._sending:
            try:
                self.request.sendall(data)
            except OSError:
                pass

    def handle(self) -> None:
        while True:
            header = self._read(p.HEADER.size)
            if header is None:
                return
            magic, device, packet_id, size = p.HEADER.unpack(header)
            if magic != p.MAGIC or size > p.MAX_PACKET:
                # Not OpenRGB -- an HTTP request from a web page, say: no
                # point reading on for a packet that will never start.
                logger.debug('closing a connection that does not speak OpenRGB')
                return
            data = self._read(size) if size else b''
            if data is None:
                return
            try:
                self.server.answer(self, device, packet_id, data)
            except p.ProtocolError as error:
                logger.debug('OpenRGB packet %d from %s: %s', packet_id, self.name, error)
            except Exception:  # noqa: BLE001 - one bad packet must not end the daemon
                logger.exception('could not answer OpenRGB packet %d', packet_id)


class OpenRGBServer(socketserver.ThreadingTCPServer):
    """OpenRGB's SDK server on the loopback address, in front of an :class:`OpenRGBBridge`."""

    daemon_threads = True
    # Not shared: if OpenRGB's own server already listens here, this one steps aside.
    allow_reuse_address = False

    def __init__(self, bridge: OpenRGBBridge, port: int = p.PORT,
                 rescan: Optional[Callable[[], object]] = None):
        self.bridge = bridge
        self.rescan = rescan
        self._clients: list = []
        self._lock = threading.Lock()
        self._rescanning = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        super().__init__(('127.0.0.1', port), _Client)

    def server_bind(self) -> None:
        # So that nothing else can take the port from under it, as Windows
        # otherwise allows.
        exclusive = getattr(socket, 'SO_EXCLUSIVEADDRUSE', None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()

    @property
    def port(self) -> int:
        return self.server_address[1]

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self.bridge.refresh()
        self.bridge.start()
        self._thread = threading.Thread(target=self.serve_forever, kwargs={'poll_interval': 0.3},
                                        name='openrgb-sdk', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            self.shutdown()     # waits for serve_forever, so only once it has run
        self.server_close()
        with self._lock:
            clients, self._clients = list(self._clients), []
        for client in clients:
            try:
                client.request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self.bridge.stop()

    # -- clients -----------------------------------------------------------
    def joined(self, client: _Client) -> None:
        with self._lock:
            self._clients.append(client)
        logger.info('an OpenRGB client connected')

    def left(self, client: _Client) -> None:
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)
        self.bridge.client_gone(client)
        logger.info('%s disconnected from OpenRGB', client.name or 'an OpenRGB client')

    def clients(self) -> list:
        """Who is connected, by the name each gave."""
        with self._lock:
            return [client.name or 'unnamed client' for client in self._clients]

    def broadcast(self, data: bytes) -> None:
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            client.send(data)

    def devices_changed(self) -> bool:
        """Tell every client when a device came or went, so it asks again."""
        changed = self.bridge.refresh()
        if changed:
            self.broadcast(p.packet(0, p.DEVICE_LIST_UPDATED))
        return changed

    def _rescan(self) -> None:
        if not self._rescanning.acquire(blocking=False):
            return      # one is under way, and will tell
        try:
            if self.rescan is not None:
                self.rescan()
            if not self.devices_changed():
                # Asked for: answered even when nothing came or went.
                self.broadcast(p.packet(0, p.DEVICE_LIST_UPDATED))
        except Exception:  # noqa: BLE001 - a failed scan is the scan's to report
            logger.debug('rescan for OpenRGB failed', exc_info=True)
        finally:
            self._rescanning.release()

    # -- packets -----------------------------------------------------------
    def answer(self, client: _Client, device: int, packet_id: int, data: bytes) -> None:
        bridge = self.bridge
        if packet_id == p.REQUEST_PROTOCOL_VERSION:
            asked = struct.unpack('<I', data[:4])[0] if len(data) >= 4 else 0
            client.version = min(asked, p.VERSION)
            client.send(p.packet(0, p.REQUEST_PROTOCOL_VERSION, p.u32(p.VERSION)))
        elif packet_id == p.SET_CLIENT_NAME:
            client.name = data.split(b'\0', 1)[0].decode('utf-8', 'replace')[:80]
            logger.info('%s is an OpenRGB client here', client.name or 'a program')
        elif packet_id == p.REQUEST_CONTROLLER_COUNT:
            client.send(p.packet(0, p.REQUEST_CONTROLLER_COUNT, p.u32(len(bridge.controllers()))))
        elif packet_id == p.REQUEST_CONTROLLER_DATA:
            # The version to describe it in comes with the request, from protocol 1 on.
            asked = struct.unpack('<I', data[:4])[0] if len(data) >= 4 else 0
            described = bridge.describe(device, min(asked, p.VERSION))
            if described is not None:
                client.send(p.packet(device, p.REQUEST_CONTROLLER_DATA, described))
        elif packet_id in (p.REQUEST_PROFILE_LIST, p.REQUEST_PLUGIN_LIST):
            # Profiles are Studio's, and there are no plugins: empty lists, not silence,
            # which some clients would wait on.
            client.send(p.packet(0, packet_id, p.EMPTY_LIST))
        elif packet_id == p.REQUEST_RESCAN_DEVICES:
            threading.Thread(target=self._rescan, name='openrgb-rescan', daemon=True).start()
        elif packet_id == p.UPDATE_LEDS:
            bridge.update_leds(client, device, p.read_leds(data))
        elif packet_id == p.UPDATE_ZONE_LEDS:
            zone, colours = p.read_zone_leds(data)
            bridge.update_leds(client, device, colours, zone=zone)
        elif packet_id == p.UPDATE_SINGLE_LED:
            led, colour = p.read_single_led(data)
            bridge.update_leds(client, device, [colour], led=led)
        elif packet_id == p.SET_CUSTOM_MODE:
            bridge.set_custom_mode(client, device)
        elif packet_id in (p.UPDATE_MODE, p.SAVE_MODE):
            index, mode = p.read_mode(data, client.version)
            bridge.update_mode(client, device, index, mode, client.version)
        # Anything else -- saving a profile, resizing a zone, what a newer
        # protocol added -- is let go: these zones are the size they are.
