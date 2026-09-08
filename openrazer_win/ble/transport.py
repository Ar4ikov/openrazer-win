"""Bluetooth LE transport, built on the Windows Runtime APIs.

This is the one part of the port that needs a package outside the standard
library: Windows exposes BLE GATT through WinRT, and reaching WinRT from
Python means PyWinRT.  It is an optional extra (``pip install
openrazer-win[ble]``) so the USB side keeps its no-dependency promise.

Devices are found by advertisement rather than by pairing.  A dual-mode
headset does not have to use its classic address on the LE side -- the Kraken
Kitty V2 BT advertises one byte away from it -- and Windows creates no device
node for an unpaired LE connection, so scanning is the only reliable way in.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import NamedTuple, Optional

from ..protocol import razer_ble

logger = logging.getLogger(__name__)

#: How long to listen for advertisements before giving up.
DEFAULT_SCAN_SECONDS = 6.0

#: GattCommunicationStatus.SUCCESS, and .ACCESS_DENIED -- which is what
#: Windows answers while another program holds the service open.
STATUS_SUCCESS = 0
STATUS_ACCESS_DENIED = 3

#: How long to wait for the device to answer a request.  It replies in about
#: 150 ms when it is going to reply at all.
REPLY_TIMEOUT = 1.5

#: How many times to ask.  Requests travel as ATT Write Commands, which carry
#: no acknowledgement of any kind, so one can be lost on the air and no answer
#: will ever come -- observed once in ordinary use.  Asking again is the only
#: remedy the protocol allows.
REQUEST_ATTEMPTS = 3


#: How long to wait for one WinRT call before deciding the radio has hung.
CALL_TIMEOUT = 30.0

#: How long a connection with nothing to hold may sit unused before it is
#: given up.  A connected device does not advertise at all, so a link that is
#: not being used for anything is worth releasing.
IDLE_DISCONNECT = 5.0

#: How often a held colour is re-asserted.  The device reverts to the colour
#: stored in it -- whatever vendor software last saved -- as soon as the link
#: drops, so the colour this port sets lasts exactly as long as the link does.
#: Re-sending also keeps Windows from tearing the link down as idle.
KEEPALIVE_INTERVAL = 2.0

#: How many times to try resolving the characteristic before giving up, and
#: how long to wait between tries.  A cold link commonly needs a second go.
RESOLVE_ATTEMPTS = 3
RESOLVE_BACKOFF = 0.4


class BleUnavailable(RuntimeError):
    """The Bluetooth support is not installed, or Windows refused it."""


class _EventLoopThread:
    """One event loop, on one thread, for every Bluetooth call.

    WinRT is asynchronous and this port is not, so the two have to be bridged.
    Doing it with ``asyncio.run`` per call would build and tear down a loop
    each time, and -- worse -- would leave a held GATT connection owned by a
    loop that no longer exists.  A single long-lived loop keeps every WinRT
    object on the thread that created it.
    """

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name='openrazer-win-ble', daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(self, coroutine, timeout: float = CALL_TIMEOUT):
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout)


_loop_thread: Optional[_EventLoopThread] = None
_loop_lock = threading.Lock()


def _loop() -> _EventLoopThread:
    global _loop_thread
    with _loop_lock:
        if _loop_thread is None:
            _loop_thread = _EventLoopThread()
        return _loop_thread


def is_available() -> bool:
    """Whether the optional Bluetooth dependency is importable."""
    try:
        import winrt.windows.devices.bluetooth  # noqa: F401
        import winrt.windows.devices.bluetooth.advertisement  # noqa: F401
        import winrt.windows.storage.streams  # noqa: F401
    except ImportError:
        return False
    return True


def require_available() -> None:
    if not is_available():
        raise BleUnavailable(
            'Bluetooth support needs the optional dependency.\n'
            '  pip install "openrazer-win[ble]"')


class Advertiser(NamedTuple):
    """One device heard advertising, assembled from all of its packets."""

    address: int
    name: str
    rssi: int
    product_id: Optional[int] = None
    classic_address: Optional[int] = None


def _read_buffer(buffer) -> bytes:
    """Copy a WinRT ``IBuffer`` out into bytes."""
    from winrt.windows.storage.streams import DataReader

    reader = DataReader.from_buffer(buffer)
    out = bytearray(reader.unconsumed_buffer_length)
    reader.read_bytes(out)
    return bytes(out)


async def _scan(seconds: float, company_id: Optional[int]) -> dict:
    from winrt.windows.devices.bluetooth.advertisement import (
        BluetoothLEAdvertisementWatcher, BluetoothLEScanningMode,
    )

    seen: dict = {}

    def on_received(_sender, args):
        # A device splits what it says across packets: this headset sends its
        # local name in one and its manufacturer data in another.  Filtering on
        # the company id per packet would throw the name away, so everything is
        # merged per address first and filtered at the end.
        entry = seen.setdefault(args.bluetooth_address,
                                {'name': '', 'rssi': 0, 'companies': set(),
                                 'product_id': None, 'classic_address': None})
        entry['rssi'] = args.raw_signal_strength_in_dbm
        if args.advertisement.local_name:
            entry['name'] = args.advertisement.local_name
        try:
            sections = list(args.advertisement.manufacturer_data)
        except Exception:  # noqa: BLE001 - a malformed advert must not stop us
            return
        for section in sections:
            entry['companies'].add(section.company_id)
            if section.company_id != razer_ble.COMPANY_ID:
                continue
            try:
                payload = _read_buffer(section.data)
            except Exception:  # noqa: BLE001 - as above
                continue
            product_id = razer_ble.product_id_from_advertisement(payload)
            if product_id is not None:
                entry['product_id'] = product_id
            classic = razer_ble.classic_address_from_advertisement(payload)
            if classic is not None:
                entry['classic_address'] = classic

    watcher = BluetoothLEAdvertisementWatcher()
    watcher.scanning_mode = BluetoothLEScanningMode.ACTIVE
    watcher.add_received(on_received)
    watcher.start()
    try:
        await asyncio.sleep(seconds)
    finally:
        watcher.stop()
    if company_id is not None:
        seen = {address: entry for address, entry in seen.items()
                if company_id in entry['companies']}
    return seen


def scan(seconds: float = DEFAULT_SCAN_SECONDS,
         company_id: Optional[int] = razer_ble.COMPANY_ID) -> list:
    """Advertising Razer devices as :class:`Advertiser` rows, strongest first."""
    require_available()
    seen = _loop().submit(_scan(seconds, company_id), timeout=seconds + CALL_TIMEOUT)
    return sorted((Advertiser(address, entry['name'], entry['rssi'],
                              entry['product_id'], entry['classic_address'])
                   for address, entry in seen.items()),
                  key=lambda row: -row.rssi)


class BleTransport:
    """A GATT connection to one device's lighting characteristic.

    The link is held open while there is a reason to hold it, and released
    when there is not.  Three facts, all confirmed on the hardware, decide
    that:

    * Re-resolving the characteristic costs about 90 ms -- most of it GATT
      service discovery -- which no host-rendered effect survives.  Held open,
      a write without response costs about 1 ms.
    * The device does not keep the colour it is told.  It shows it while the
      link is up and reverts to the colour stored in it -- whatever vendor
      software last saved -- as soon as the link goes.  So a colour set through
      this port lasts exactly as long as the link, which is why a held colour
      is re-asserted at :data:`KEEPALIVE_INTERVAL` rather than written once.
    * A connected device stops advertising altogether.  A link with nothing to
      hold therefore makes the device needlessly invisible to discovery and to
      every other program on the machine, so that one is given up.
    """

    def __init__(self, address: int,
                 characteristic_uuid: str = razer_ble.WRITE_CHARACTERISTIC,
                 notify_uuid: str = razer_ble.NOTIFY_CHARACTERISTIC,
                 idle_disconnect: float = IDLE_DISCONNECT,
                 keepalive: float = KEEPALIVE_INTERVAL):
        require_available()
        self.address = address
        self.characteristic_uuid = characteristic_uuid.lower()
        self.notify_uuid = notify_uuid.lower()
        self.idle_disconnect = idle_disconnect
        self.keepalive = keepalive
        self._lock = threading.RLock()
        self._device = None
        self._characteristic_cache = None
        self._last_write = 0.0
        self._held_payload: Optional[bytes] = None
        self._watchdog: Optional[threading.Thread] = None
        self._notify = None
        self._subscribed = False
        self._replies: dict = {}

    # -- plumbing ----------------------------------------------------------
    async def _resolve_once(self, uncached: bool):
        from winrt.windows.devices.bluetooth import (
            BluetoothCacheMode, BluetoothLEDevice,
        )

        device = await BluetoothLEDevice.from_bluetooth_address_async(self.address)
        if device is None:
            raise BleUnavailable(
                'no Bluetooth LE device at {0:012X}; is it powered on?'.format(
                    self.address))

        # Windows' service cache answers in milliseconds when it is warm, but
        # for a device that is not currently connected it can come back
        # successful and *empty* -- and an empty service list is
        # indistinguishable from a device that lacks the characteristic. So the
        # cache is tried first for speed, and a fruitless answer is retried
        # uncached rather than believed.
        services = await device.get_gatt_services_with_cache_mode_async(
            BluetoothCacheMode.UNCACHED if uncached else BluetoothCacheMode.CACHED)
        if services.status != STATUS_SUCCESS:
            device.close()
            raise BleUnavailable(
                'GATT service discovery failed (status {0})'.format(services.status))

        discovered = list(services.services)
        if not discovered:
            device.close()
            raise BleUnavailable(
                'Windows returned no GATT services for {0:012X}; the link is '
                'not up yet'.format(self.address))

        wanted = {self.characteristic_uuid, self.notify_uuid}
        resolved = {}
        refused = False
        for service in discovered:
            characteristics = await service.get_characteristics_async()
            if characteristics.status == STATUS_ACCESS_DENIED:
                refused = True
                continue
            if characteristics.status != STATUS_SUCCESS:
                continue
            for characteristic in characteristics.characteristics:
                uuid = str(characteristic.uuid).lower()
                if uuid in wanted:
                    resolved[uuid] = characteristic

        write = resolved.get(self.characteristic_uuid)
        if write is not None:
            return device, write, resolved.get(self.notify_uuid)

        device.close()
        if refused:
            # Exactly what Razer Synapse does to this device: it holds the
            # vendor service open and Windows refuses everyone else.  Saying so
            # beats reporting the hardware as the wrong model.
            raise BleUnavailable(
                'another program holds {0:012X} exclusively -- Razer Synapse '
                'does this; close it and retry'.format(self.address))
        raise BleUnavailable(
            'device {0:012X} advertises {1} GATT service(s) but not {2}'.format(
                self.address, len(discovered), self.characteristic_uuid))

    async def _resolve(self):
        """Resolve the characteristic, retrying a cold link a few times.

        The first attempt against a device that has been idle often comes back
        with nothing -- Windows answers from an empty cache, or the link is
        still being brought up -- so failing on it would report a working
        device as broken.  Only the first attempt trusts the cache.
        """
        last = None
        for attempt in range(RESOLVE_ATTEMPTS):
            try:
                return await self._resolve_once(uncached=attempt > 0)
            except BleUnavailable as error:
                last = error
                logger.debug('resolve attempt %d for %012X failed: %s',
                             attempt + 1, self.address, error)
                if attempt + 1 < RESOLVE_ATTEMPTS:
                    await asyncio.sleep(RESOLVE_BACKOFF)
        raise last

    async def _characteristic(self):
        if self._characteristic_cache is None:
            (self._device, self._characteristic_cache,
             self._notify) = await self._resolve()
            self._subscribed = False
        return self._device, self._characteristic_cache

    async def _subscribe(self) -> None:
        """Start listening for answers, once per connection.

        Nothing in the vendor service can be read directly, so a value is
        obtained by writing a request and waiting for the device to notify the
        answer back.  That means the subscription has to be in place before the
        first request, not after it.
        """
        if self._subscribed:
            return
        await self._characteristic()
        if self._notify is None:
            raise BleUnavailable(
                'device {0:012X} exposes no notify characteristic, so it '
                'cannot be asked anything'.format(self.address))

        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattClientCharacteristicConfigurationDescriptorValue as Descriptor,
        )

        loop = asyncio.get_running_loop()

        def on_value(_sender, args):
            try:
                payload = _read_buffer(args.characteristic_value)
                opcode, kind, body = razer_ble.parse_response(payload)
            except Exception:  # noqa: BLE001 - a stray notification is not fatal
                return
            loop.call_soon_threadsafe(self._deliver, opcode, kind, body)

        self._notify.add_value_changed(on_value)
        status = await self._notify.write_client_characteristic_configuration_descriptor_async(
            Descriptor.NOTIFY)
        if status != STATUS_SUCCESS:
            raise BleUnavailable(
                'could not subscribe to notifications (status {0})'.format(status))
        self._subscribed = True

    def _deliver(self, opcode: int, kind: int, body: bytes) -> None:
        """Hand a notification to whoever is waiting for that opcode."""
        if kind == razer_ble.PUSH:
            logger.debug('%012X pushed 0x%02x = %s', self.address, opcode,
                         body.hex(' '))
        waiter = self._replies.get(opcode)
        if waiter is not None and not waiter.done():
            waiter.set_result((kind, body))

    async def _request_once(self, opcode: int, payload: bytes) -> bytes:
        await self._subscribe()
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future = loop.create_future()
        self._replies[opcode] = waiter
        try:
            await self._write_once(razer_ble.message(opcode, payload))
            _kind, body = await asyncio.wait_for(waiter, REPLY_TIMEOUT)
            return body
        finally:
            self._replies.pop(opcode, None)

    async def _request(self, opcode: int, payload: bytes) -> bytes:
        for attempt in range(REQUEST_ATTEMPTS):
            try:
                return await self._request_once(opcode, payload)
            except asyncio.TimeoutError:
                logger.debug('%012X ignored 0x%02x (attempt %d)',
                             self.address, opcode, attempt + 1)
        raise BleUnavailable(
            'device {0:012X} did not answer 0x{1:02x} in {2} attempts'.format(
                self.address, opcode, REQUEST_ATTEMPTS))

    def _forget(self) -> None:
        """Drop the cached connection so the next call resolves it again."""
        device, self._device = self._device, None
        self._characteristic_cache = None
        self._notify = None
        self._subscribed = False
        self._replies.clear()
        if device is not None:
            try:
                device.close()
            except Exception:  # noqa: BLE001 - already gone is fine
                pass

    async def _write_once(self, payload: bytes) -> None:
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattWriteOption,
        )
        from winrt.windows.storage.streams import DataWriter

        _device, characteristic = await self._characteristic()
        writer = DataWriter()
        writer.write_bytes(payload)
        result = await characteristic.write_value_with_result_and_option_async(
            writer.detach_buffer(), GattWriteOption.WRITE_WITHOUT_RESPONSE)
        if result.status != STATUS_SUCCESS:
            raise BleUnavailable('write rejected (status {0})'.format(result.status))

    async def _write(self, payload: bytes) -> None:
        try:
            await self._write_once(payload)
        except Exception:
            # The link may simply have gone away between frames; rebuild it
            # once before giving up, so a suspended headset recovers on its own.
            self._forget()
            await self._write_once(payload)

    # -- holding the colour, or letting the link go ------------------------
    def _touch(self) -> None:
        """Note that the link was just used, and start watching over it."""
        self._last_write = time.monotonic()
        if self._watchdog is None or not self._watchdog.is_alive():
            self._watchdog = threading.Thread(
                target=self._watch, daemon=True,
                name='openrazer-win-ble-{0:012x}'.format(self.address))
            self._watchdog.start()

    def _watch(self) -> None:
        """Keep a held colour alive, or give up a link with nothing to hold.

        The device does not store a colour: it shows what it was last told for
        as long as the link is up, and reverts to its own default the moment
        that goes -- which is what made a colour vanish seconds after being
        set.  So while there is a colour to hold, it is re-asserted at
        :data:`KEEPALIVE_INTERVAL`, which both refreshes it and keeps Windows
        from tearing down a link it considers idle.  With nothing to hold, the
        link is dropped instead, so the device goes back to advertising.
        """
        while True:
            with self._lock:
                if self._characteristic_cache is None:
                    return              # somebody else dropped it already
                idle = time.monotonic() - self._last_write
                if self._held_payload is not None:
                    if idle < self.keepalive:
                        wait = self.keepalive - idle
                    else:
                        try:
                            _loop().submit(self._write(self._held_payload))
                        except Exception:  # noqa: BLE001 - reported by the next write
                            logger.debug('could not refresh %012X', self.address,
                                         exc_info=True)
                            return
                        self._last_write = time.monotonic()
                        wait = self.keepalive
                elif idle >= self.idle_disconnect:
                    logger.debug('dropping idle link to %012X', self.address)
                    self._forget()
                    return
                else:
                    wait = self.idle_disconnect - idle
            time.sleep(max(wait, 0.05))

    def is_connected(self) -> bool:
        """Whether a usable link is being held right now.

        Discovery needs this: a connected device stops advertising, so a sweep
        that hears nothing must not conclude the device has gone away.
        """
        with self._lock:
            if self._characteristic_cache is None:
                return False
            device = self._device
        if device is None:
            return False
        try:
            # BluetoothConnectionStatus.CONNECTED
            return int(device.connection_status) == 1
        except Exception:  # noqa: BLE001 - a dead object is not connected
            return False

    # -- public ------------------------------------------------------------
    def write(self, payload: bytes, hold: bool = False) -> None:
        """Send `payload`; with `hold`, keep re-sending it until told otherwise.

        Holding is what makes a colour stay put on hardware that does not
        store one.  A frame of an animation is not held -- the next frame is
        along in a moment anyway.
        """
        with self._lock:
            _loop().submit(self._write(payload))
            if hold:
                self._held_payload = bytes(payload)
            self._touch()

    def release(self) -> None:
        """Stop holding a colour, letting the link go once it falls idle."""
        with self._lock:
            self._held_payload = None

    def request(self, opcode: int, payload: bytes = b'') -> bytes:
        """Ask the device something and return the payload of its answer."""
        with self._lock:
            body = _loop().submit(self._request(opcode, bytes(payload)))
            self._touch()
            return body

    def read(self, opcode: int) -> bytes:
        """Read a value: a request with no payload."""
        return self.request(opcode)

    async def _describe(self) -> dict:
        device, characteristic = await self._characteristic()
        return {
            'name': device.name or '',
            'address': self.address,
            'characteristic': str(characteristic.uuid),
            'properties': int(characteristic.characteristic_properties),
        }

    def describe(self) -> dict:
        with self._lock:
            return _loop().submit(self._describe())

    def close(self) -> None:
        with self._lock:
            self._held_payload = None
            self._forget()
