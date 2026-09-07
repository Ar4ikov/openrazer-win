"""Finding the Razer devices attached to this machine."""
from __future__ import annotations

import logging
import threading
from typing import Optional

from ..devices.database import DeviceDatabase, DeviceInfo, get_database
from ..devices.recipes import RecipeTable, get_recipes
from ..hid import get_backend
from ..protocol.report import VENDOR_ID
from .device import RazerDevice
from .kraken import KrakenDevice
from .persistence import Persistence
from .transport import Transport, select_control_interface, select_kraken_interface

logger = logging.getLogger(__name__)


class DeviceManager:
    """Enumerates supported devices and keeps :class:`RazerDevice` objects alive.

    Rescanning is cheap and idempotent: devices that are still present keep
    their existing object (and therefore their open HID handle and cached
    serial), devices that vanished are closed.
    """

    def __init__(self, backend=None, persistence: Optional[Persistence] = None,
                 database: Optional[DeviceDatabase] = None,
                 recipes: Optional[RecipeTable] = None):
        self.backend = backend if backend is not None else get_backend()
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
                key = self._key(meta, infos)
                seen.add(key)
                if key in self._devices:
                    continue
                device = self._build(meta, infos)
                if device is not None:
                    self._devices[key] = device
                    logger.info('found %s (%04x:%04x)', meta.name, meta.vid, meta.pid)

            for key in list(self._devices):
                if key not in seen:
                    logger.info('device removed: %s', self._devices[key].name)
                    self._devices.pop(key).close()

            self._unsupported = unsupported
            return self.devices

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
