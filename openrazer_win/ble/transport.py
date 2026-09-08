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
from typing import NamedTuple, Optional

from ..protocol import razer_ble

logger = logging.getLogger(__name__)

#: How long to listen for advertisements before giving up.
DEFAULT_SCAN_SECONDS = 6.0

#: GattCommunicationStatus.SUCCESS
STATUS_SUCCESS = 0


#: How long to wait for one WinRT call before deciding the radio has hung.
CALL_TIMEOUT = 30.0


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

    The connection is held open.  Re-resolving it costs about 90 ms -- most of
    it GATT service discovery -- which is far too slow for host-rendered
    effects, where a frame goes out several times a second.  Held open, a write
    without response costs a couple of milliseconds.  It is re-resolved once,
    transparently, if the device drops the link.
    """

    def __init__(self, address: int,
                 characteristic_uuid: str = razer_ble.WRITE_CHARACTERISTIC):
        require_available()
        self.address = address
        self.characteristic_uuid = characteristic_uuid.lower()
        self._lock = threading.RLock()
        self._device = None
        self._characteristic_cache = None

    # -- plumbing ----------------------------------------------------------
    async def _resolve(self):
        from winrt.windows.devices.bluetooth import BluetoothLEDevice

        device = await BluetoothLEDevice.from_bluetooth_address_async(self.address)
        if device is None:
            raise BleUnavailable(
                'no Bluetooth LE device at {0:012X}; is it powered on?'.format(
                    self.address))

        services = await device.get_gatt_services_async()
        if services.status != STATUS_SUCCESS:
            device.close()
            raise BleUnavailable(
                'GATT service discovery failed (status {0})'.format(services.status))

        for service in services.services:
            characteristics = await service.get_characteristics_async()
            if characteristics.status != STATUS_SUCCESS:
                continue
            for characteristic in characteristics.characteristics:
                if str(characteristic.uuid).lower() == self.characteristic_uuid:
                    return device, characteristic
        device.close()
        raise BleUnavailable(
            'device {0:012X} has no characteristic {1}'.format(
                self.address, self.characteristic_uuid))

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

    # -- public ------------------------------------------------------------
    def write(self, payload: bytes) -> None:
        with self._lock:
            _loop().submit(self._write(payload))

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
