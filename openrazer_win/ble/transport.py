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

#: GattCommunicationStatus.SUCCESS
STATUS_SUCCESS = 0


#: How long to wait for one WinRT call before deciding the radio has hung.
CALL_TIMEOUT = 30.0

#: How long a connection may sit unused before it is given up.  It has to be
#: long enough to span the gap between frames of a host-rendered effect, and
#: short enough that a device at rest goes back to advertising promptly --
#: because a connected device does not advertise at all.
IDLE_DISCONNECT = 5.0

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

    The connection is held open between writes, then dropped once it has been
    idle for :data:`IDLE_DISCONNECT`.  Both halves of that matter:

    * Re-resolving it costs about 90 ms -- most of it GATT service discovery --
      which no host-rendered effect survives.  Held open, a write without
      response costs about 1 ms, so streaming frames works.
    * A connected device stops advertising, verified on the hardware.  Holding
      an idle link therefore makes the device invisible to discovery and to
      every other program on the machine, so an idle link is given up.
    """

    def __init__(self, address: int,
                 characteristic_uuid: str = razer_ble.WRITE_CHARACTERISTIC,
                 idle_disconnect: float = IDLE_DISCONNECT):
        require_available()
        self.address = address
        self.characteristic_uuid = characteristic_uuid.lower()
        self.idle_disconnect = idle_disconnect
        self._lock = threading.RLock()
        self._device = None
        self._characteristic_cache = None
        self._last_write = 0.0
        self._idle_thread: Optional[threading.Thread] = None

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

        for service in discovered:
            characteristics = await service.get_characteristics_async()
            if characteristics.status != STATUS_SUCCESS:
                continue
            for characteristic in characteristics.characteristics:
                if str(characteristic.uuid).lower() == self.characteristic_uuid:
                    return device, characteristic
        device.close()
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
            self._device, self._characteristic_cache = await self._resolve()
        return self._device, self._characteristic_cache

    def _forget(self) -> None:
        """Drop the cached connection so the next call resolves it again."""
        device, self._device = self._device, None
        self._characteristic_cache = None
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

    # -- keeping the link no longer than needed ----------------------------
    def _touch(self) -> None:
        """Note that the link was just used, and watch for it going idle."""
        self._last_write = time.monotonic()
        if self._idle_thread is None or not self._idle_thread.is_alive():
            self._idle_thread = threading.Thread(
                target=self._watch_idle, daemon=True,
                name='openrazer-win-ble-idle-{0:012x}'.format(self.address))
            self._idle_thread.start()

    def _watch_idle(self) -> None:
        while True:
            with self._lock:
                if self._characteristic_cache is None:
                    return              # somebody else dropped it already
                idle = time.monotonic() - self._last_write
                if idle >= self.idle_disconnect:
                    logger.debug('dropping idle link to %012X', self.address)
                    self._forget()
                    return
            time.sleep(max(self.idle_disconnect - idle, 0.1))

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
    def write(self, payload: bytes) -> None:
        with self._lock:
            _loop().submit(self._write(payload))
            self._touch()

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
            self._forget()
