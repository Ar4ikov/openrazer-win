"""Finding the Razer devices attached to this machine."""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from ..devices.database import DeviceDatabase, DeviceInfo, get_database
from ..devices.recipes import RecipeTable, get_recipes
from ..hid import get_backend
from ..protocol import razer_ble
from ..protocol.report import VENDOR_ID
from .device import RazerDevice
from .kraken import KrakenDevice
from .persistence import Persistence
from .transport import Transport, select_control_interface, select_kraken_interface

logger = logging.getLogger(__name__)

#: How a Bluetooth device is keyed in the device table.
BLE_PREFIX = 'ble:'

#: Bluetooth devices are found by listening for advertisements, which takes
#: seconds -- far too slow to repeat at the hot-plug interval, so the radio is
#: swept on its own much lazier schedule.
BLUETOOTH_INTERVAL = 60.0


class DeviceManager:
    """Enumerates supported devices and keeps :class:`RazerDevice` objects alive.

    Rescanning is cheap and idempotent: devices that are still present keep
    their existing object (and therefore their open HID handle and cached
    serial), devices that vanished are closed.
    """

    def __init__(self, backend=None, persistence: Optional[Persistence] = None,
                 database: Optional[DeviceDatabase] = None,
                 recipes: Optional[RecipeTable] = None,
                 bluetooth: bool = True,
                 bluetooth_interval: float = BLUETOOTH_INTERVAL):
        self.backend = backend if backend is not None else get_backend()
        #: Scanning for advertisements costs a few seconds, so it is opt-out
        #: and only happens when the optional dependency is installed.
        self.bluetooth = bluetooth
        self.bluetooth_interval = bluetooth_interval
        self._bluetooth_swept_at: Optional[float] = None
        self.persistence = persistence or Persistence()
        self.database = database or get_database()
        self.recipes = recipes or get_recipes()
        self._lock = threading.RLock()
        self._devices: dict = {}
        self._unsupported: list = []

    # -- enumeration -------------------------------------------------------
    def scan(self) -> list:
        """Rescan the HID bus.  Returns the current device list."""
        with self._lock:
            collections = self.backend.enumerate(vendor_id=VENDOR_ID)
            by_pid: dict = {}
            for info in collections:
                by_pid.setdefault(info.product_id, []).append(info)

            seen = set()
            unsupported = []
            for pid, infos in sorted(by_pid.items()):
                meta = self.database.get(VENDOR_ID, pid)
                if meta is None:
                    unsupported.append((pid, infos[0].product))
                    continue
                if meta.is_bluetooth:
                    continue      # reached over Bluetooth, not the HID bus
                key = self._key(meta, infos)
                seen.add(key)
                if key in self._devices:
                    continue
                device = self._build(meta, infos)
                if device is not None:
                    self._devices[key] = device
                    logger.info('found %s (%04x:%04x)', meta.name, meta.vid, meta.pid)

            swept = self._scan_bluetooth(seen) if self.bluetooth else False

            for key in list(self._devices):
                if key in seen:
                    continue
                if key.startswith(BLE_PREFIX) and self._keep_bluetooth(
                        self._devices[key], swept):
                    continue
                logger.info('device removed: %s', self._devices[key].name)
                self._devices.pop(key).close()

            self._unsupported = unsupported
            return self.devices

    @staticmethod
    def _keep_bluetooth(device, swept: bool) -> bool:
        """Whether to hold on to a Bluetooth device the sweep did not hear.

        Two reasons it might not have been heard, neither of them "it is
        gone".  The radio may not have been swept at all this time round.  Or
        -- and this one is not obvious -- the device may be *connected*: a BLE
        peripheral stops advertising while it has a link up, verified on the
        hardware.  Dropping it then was a trap of its own making: whatever held
        the link kept it alive, so the device never advertised again and was
        never rediscovered, while its lighting was still being driven.
        """
        if not swept:
            # An advertisement missed is not a device unplugged.
            return True
        if getattr(device, 'is_connected', None) is not None and device.is_connected():
            logger.debug('%s is silent but connected; keeping it', device.name)
            return True
        return False

    def _scan_bluetooth(self, seen: set) -> bool:
        """Add Razer devices that are only reachable over Bluetooth LE.

        These never appear on the HID bus -- a Bluetooth-only headset has no
        USB data mode at all -- so they are found by advertisement instead.

        Returns whether the radio was actually swept, which is what tells the
        caller that a Bluetooth device's absence means something.
        """
        from ..ble import is_available, scan

        if not is_available():
            return False
        now = time.monotonic()
        if (self._bluetooth_swept_at is not None
                and now - self._bluetooth_swept_at < self.bluetooth_interval):
            return False
        self._bluetooth_swept_at = now
        try:
            advertisers = scan()
        except Exception:  # noqa: BLE001 - a radio problem must not break scanning
            logger.debug('Bluetooth scan failed', exc_info=True)
            return False

        for advertiser in advertisers:
            meta = self._match_advertiser(advertiser)
            if meta is None:
                continue
            address = advertiser.address
            key = '{0}{1:012x}'.format(BLE_PREFIX, address)
            seen.add(key)
            if key in self._devices:
                continue
            device = self._build_ble(meta, address)
            if device is not None:
                self._devices[key] = device
                logger.info('found %s over Bluetooth (%012X)', meta.name, address)
        return True

    def _match_advertiser(self, advertiser) -> Optional[DeviceInfo]:
        """Map one advertising device onto a database entry.

        Razer puts the product id in its manufacturer data, so that is the
        first thing to go on.  The local name is a fallback for a device whose
        manufacturer data never arrives during the sweep -- the two travel in
        separate packets.
        """
        product_id = getattr(advertiser, 'product_id', None)
        if product_id is not None:
            meta = self.database.get(VENDOR_ID, product_id)
            if meta is not None and meta.is_bluetooth:
                return meta
        return self._match_advertised_name(advertiser.name)

    def _match_advertised_name(self, name: str) -> Optional[DeviceInfo]:
        """Map an advertised local name onto a database entry.

        The Kraken Kitty V2 BT advertises as "Razer Stereo", which no other
        supported device does.
        """
        if not name:
            return None
        for pid in razer_ble.ADVERTISED_NAMES.get(name, ()):
            meta = self.database.get(VENDOR_ID, pid)
            if meta is not None:
                return meta
        return None

    def _build_ble(self, meta: DeviceInfo, address: int):
        from ..ble import BleTransport
        from .ble_device import BleDevice

        try:
            transport = BleTransport(address)
        except Exception as error:  # noqa: BLE001 - reported, not fatal
            logger.warning('cannot open %s over Bluetooth: %s', meta.name, error)
            return None
        return BleDevice(meta, transport, self.persistence)

    @staticmethod
    def _key(meta: DeviceInfo, infos: list) -> str:
        return '{0:04x}:{1:04x}'.format(meta.vid, meta.pid)

    def _build(self, meta: DeviceInfo, infos: list) -> Optional[RazerDevice]:
        if meta.pid in self.recipes.kraken_pids:
            return self._build_kraken(meta, infos)

        params = self.recipes.transport_params(meta.driver, meta.pid)
        control = select_control_interface(infos, params.get('index'))
        if control is None:
            logger.warning(
                '%s is attached but exposes no 90-byte feature report; '
                'another program may hold it exclusively', meta.name)
            return None
        argb = next((i for i in infos if i.feature_length == 321), None)
        transport = Transport(self.backend, control,
                              wait_us=params.get('wait_us', 600), argb_info=argb)
        return RazerDevice(meta, transport, self.persistence, self.recipes)

    def _build_kraken(self, meta: DeviceInfo, infos: list) -> Optional[RazerDevice]:
        """Krakens write output reports instead of the 90-byte control report."""
        control = select_kraken_interface(infos)
        if control is None:
            logger.warning(
                '%s is attached but exposes no 37-byte output report; '
                'another program may hold it exclusively', meta.name)
            return None
        transport = Transport(self.backend, control, kraken_info=control)
        return KrakenDevice(meta, transport, self.persistence, self.recipes)

    # -- accessors ---------------------------------------------------------
    @property
    def devices(self) -> list:
        with self._lock:
            return list(self._devices.values())

    @property
    def unsupported(self) -> list:
        """``(product_id, product_string)`` for Razer hardware we don't know."""
        with self._lock:
            return list(self._unsupported)

    def by_serial(self, serial: str) -> Optional[RazerDevice]:
        for device in self.devices:
            if device.serial == serial:
                return device
        return None

    def by_pid(self, pid: int) -> Optional[RazerDevice]:
        for device in self.devices:
            if device.pid == pid:
                return device
        return None

    def find(self, needle: str) -> list:
        """Match a device by serial, hex product id, or name substring."""
        needle = needle.strip().lower()
        matches = []
        for device in self.devices:
            if needle in (device.serial.lower(), '{0:04x}'.format(device.pid)):
                return [device]
            if needle in device.name.lower():
                matches.append(device)
        return matches

    def close(self) -> None:
        with self._lock:
            for device in self._devices.values():
                device.close()
            self._devices.clear()
        self.persistence.save()

    def __enter__(self):
        self.scan()
        return self

    def __exit__(self, *exc):
        self.close()
