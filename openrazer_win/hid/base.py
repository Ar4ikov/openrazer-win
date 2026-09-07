"""Backend-independent description of a HID interface."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Protocol

#: ``&mi_02`` in a Windows device path -- the USB interface number.  The kernel
#: driver addresses control transfers by interface, so this is how we find the
#: right collection to talk to.
_INTERFACE_RE = re.compile(r'&mi_([0-9a-f]{2})', re.IGNORECASE)
_COLLECTION_RE = re.compile(r'&col([0-9a-f]{2})', re.IGNORECASE)


@dataclass(frozen=True)
class HidDeviceInfo:
    """One HID top-level collection exposed by the operating system."""

    path: str
    vendor_id: int
    product_id: int
    interface: Optional[int] = None
    collection: Optional[int] = None
    usage_page: int = 0
    usage: int = 0
    feature_length: int = 0
    input_length: int = 0
    output_length: int = 0
    version: int = 0
    manufacturer: str = ''
    product: str = ''
    serial: str = ''

    @staticmethod
    def parse_interface(path: str) -> Optional[int]:
        match = _INTERFACE_RE.search(path)
        return int(match.group(1), 16) if match else None

    @staticmethod
    def parse_collection(path: str) -> Optional[int]:
        match = _COLLECTION_RE.search(path)
        return int(match.group(1), 16) if match else None

    def describe(self) -> str:
        return ('{0:04x}:{1:04x} mi={2} col={3} usage={4:04x}:{5:04x} '
                'feature={6}').format(
            self.vendor_id, self.product_id,
            '--' if self.interface is None else '{0:02d}'.format(self.interface),
            '--' if self.collection is None else '{0:02d}'.format(self.collection),
            self.usage_page, self.usage, self.feature_length)


class HidError(IOError):
    """A HID transfer failed."""


class HidHandle(Protocol):
    """An open handle able to exchange feature reports."""

    info: HidDeviceInfo

    def send_feature_report(self, data: bytes) -> None: ...

    def get_feature_report(self, length: int, report_id: int = 0x00) -> bytes: ...

    def send_output_report(self, data: bytes) -> None: ...

    def close(self) -> None: ...


class HidBackend(Protocol):
    """Enumerates HID collections and opens them."""

    name: str

    def enumerate(self, vendor_id: Optional[int] = None,
                  product_id: Optional[int] = None) -> list: ...

    def open(self, info: HidDeviceInfo) -> HidHandle: ...
