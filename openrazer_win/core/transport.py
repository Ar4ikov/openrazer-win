"""Talking to a Razer device over Windows HID.

The Linux driver reaches the device with ``usb_control_msg`` addressed to a
specific USB interface.  Windows exposes each interface (and each top-level
collection within it) as its own HID device path, so the equivalent is to open
the collection whose path carries the matching ``&mi_XX`` and whose report
descriptor declares a 90-byte feature report.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from ..hid.base import HidDeviceInfo, HidError
from ..protocol import blackshark, blackshark_v3x
from ..protocol.report import REPORT_SIZE, RazerReport, RazerReportError, Status

logger = logging.getLogger(__name__)

#: HID feature buffers carry a leading report-id byte.
FEATURE_BUFFER_SIZE = REPORT_SIZE + 1

#: The ARGB accessory protocol uses a single 320-byte report instead.
ARGB_REPORT_SIZE = 320
ARGB_BUFFER_SIZE = ARGB_REPORT_SIZE + 1
ARGB_FEATURE_REPORT_ID = 0x5B

#: The Kraken family writes 37-byte output reports rather than feature reports,
#: on interface 3.
KRAKEN_REPORT_SIZE = 37
KRAKEN_INTERFACE = 3

#: ``razer_send_payload`` retries five times, 10 ms apart.
MAX_ATTEMPTS = 5
RETRY_DELAY = 0.010

#: Windows' HID stack adds its own scheduling latency, so never sleep for less
#: than this between SET_REPORT and GET_REPORT even when the driver says 600us.
MIN_WAIT = 0.001


class DeviceNotFound(Exception):
    """No HID collection on this machine matches the request."""


def is_control_interface(info: HidDeviceInfo) -> bool:
    """True when this collection declares the 90-byte Razer feature report."""
    return info.feature_length == FEATURE_BUFFER_SIZE


def is_argb_interface(info: HidDeviceInfo) -> bool:
    return info.feature_length == ARGB_BUFFER_SIZE


def is_kraken_interface(info: HidDeviceInfo) -> bool:
    """The collection that accepts the Kraken's 37-byte output report."""
    return info.output_length == KRAKEN_REPORT_SIZE


def select_kraken_interface(candidates: list):
    """Pick the collection to write Kraken RAM through.

    The kernel addresses interface 3 explicitly, so prefer it, but fall back to
    any collection declaring an output report of the right size -- collection
    numbering varies between the Kraken generations.
    """
    usable = [info for info in candidates if is_kraken_interface(info)]
    if not usable:
        return None
    exact = [info for info in usable if info.interface == KRAKEN_INTERFACE]
    pool = exact or usable
    return sorted(pool, key=lambda i: (i.interface or 0, i.collection or 0))[0]


def is_blackshark_interface(info: HidDeviceInfo) -> bool:
    """A vendor collection that takes the BlackShark's 64-byte reports."""
    return (info.usage_page >= 0xFF00
            and info.output_length >= blackshark.REPORT_SIZE
            and info.input_length >= blackshark.REPORT_SIZE)


def select_blackshark_interface(candidates: list):
    """Pick the collection to talk to a BlackShark V2 HyperSpeed through."""
    usable = [info for info in candidates if is_blackshark_interface(info)]
    if not usable:
        return None

    def preference(info: HidDeviceInfo) -> tuple:
        pages = (blackshark.USAGE_PAGE, blackshark.OTHER_USAGE_PAGE)
        return (pages.index(info.usage_page) if info.usage_page in pages else len(pages),
                info.interface != blackshark.INTERFACE, info.collection or 0)
    return sorted(usable, key=preference)[0]


def is_blackshark_v3x_interface(info: HidDeviceInfo) -> bool:
    """The vendor collection that declares the BlackShark V3 X's feature report."""
    return (info.usage_page >= 0xFF00
            and info.feature_length == blackshark_v3x.REPORT_SIZE)


def select_blackshark_v3x_interface(candidates: list):
    """Pick the collection to talk to a BlackShark V3 X through."""
    usable = [info for info in candidates if is_blackshark_v3x_interface(info)]
    if not usable:
        return None
    return sorted(usable, key=lambda i: (i.interface or 0, i.collection or 0))[0]


def select_control_interface(candidates: list, preferred_index: Optional[int] = None):
    """Pick the collection to send control reports to.

    Prefers the interface number the kernel driver addresses; falls back to any
    collection that declares the right feature report length.
    """
    controls = [info for info in candidates if is_control_interface(info)]
    if not controls:
        return None
    if preferred_index is not None:
        exact = [info for info in controls if info.interface == preferred_index]
        if exact:
            # Lowest collection number first, mirroring enumeration order.
            return sorted(exact, key=lambda i: (i.collection or 0))[0]
    return sorted(controls, key=lambda i: (i.interface or 0, i.collection or 0))[0]


class Transport:
    """A serialised request/response channel to one device."""

    def __init__(self, backend, info: HidDeviceInfo, wait_us: int = 600,
                 argb_info: Optional[HidDeviceInfo] = None,
                 kraken_info: Optional[HidDeviceInfo] = None):
        self._backend = backend
        self.info = info
        self.argb_info = argb_info
        self.kraken_info = kraken_info
        self.wait = max(wait_us / 1_000_000.0, MIN_WAIT)
        self._lock = threading.RLock()
        self._handle = None
        self._argb_handle = None
        self._kraken_handle = None

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        with self._lock:
            if self._handle is None:
                self._handle = self._backend.open(self.info)

    def close(self) -> None:
        with self._lock:
            for attribute in ('_handle', '_argb_handle', '_kraken_handle'):
                handle = getattr(self, attribute)
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:  # pragma: no cover - best effort
                        logger.debug('error closing %s', attribute, exc_info=True)
                    setattr(self, attribute, None)

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- transfers ---------------------------------------------------------
    def send_payload(self, request: RazerReport,
                     want_response: bool = True) -> Optional[RazerReport]:
        """Send a report and read the reply, retrying like the kernel driver.

        ``busy`` acknowledges receipt, not completion: a Huntsman may still
        echo the request's arguments until the final reply is ready. Poll that
        reply without resending, before the next command can overwrite it.

        A device still busy after the polls has accepted the command all the
        same, so that counts as success, as it does in ``razer_send_payload``.
        """
        with self._lock:
            self.open()
            last_error: Optional[Exception] = None
            response: Optional[RazerReport] = None

            for attempt in range(MAX_ATTEMPTS, 0, -1):
                try:
                    self._handle.send_feature_report(b'\x00' + request.pack())
                    time.sleep(self.wait)
                    if not want_response:
                        return None
                    for poll in range(MAX_ATTEMPTS):
                        raw = self._handle.get_feature_report(FEATURE_BUFFER_SIZE)
                        response = RazerReport.unpack(raw[1:])
                        if not request.matches(response) or response.status != Status.BUSY:
                            break
                        if poll < MAX_ATTEMPTS - 1:
                            time.sleep(RETRY_DELAY)
                except HidError as error:
                    last_error = error
                    logger.debug('transfer failed (%d attempts left): %s', attempt - 1, error)
                    time.sleep(RETRY_DELAY)
                    continue

                if not request.matches(response):
                    last_error = RazerReportError(
                        'response does not match request: {0}'.format(response.describe()))
                    time.sleep(RETRY_DELAY)
                    continue

                if response.status == Status.SUCCESSFUL:
                    return response
                if response.status == Status.BUSY:
                    # Not an error in the kernel driver either: which devices
                    # stay busy over a command that worked is not known, and an
                    # error here would fail every one of them.
                    logger.debug('still busy after %d reads: %s', MAX_ATTEMPTS,
                                 response.describe())
                    return response

                last_error = RazerReportError(
                    'device replied {0}'.format(Status.name(response.status)),
                    status=response.status)
                if response.status == Status.NOT_SUPPORTED:
                    break  # retrying an unsupported command never helps
                time.sleep(RETRY_DELAY)

            if isinstance(last_error, RazerReportError):
                raise last_error
            raise RazerReportError(
                'no usable response from device: {0}'.format(last_error))

    def send_kraken(self, reports, settle: float = 0.0) -> None:
        """Write a sequence of Kraken RAM reports, in order.

        The controller commits each write slowly, so the caller supplies the
        settle time the driver uses -- 15 ms per byte written.
        """
        if self.kraken_info is None:
            raise DeviceNotFound('device has no Kraken control interface')
        with self._lock:
            if self._kraken_handle is None:
                self._kraken_handle = self._backend.open(self.kraken_info)
            for report in reports:
                self._kraken_handle.send_output_report(bytes(report))
                if settle:
                    time.sleep(settle)

    def send_argb(self, channel: int, colours: bytes) -> None:
        """Send an addressable-RGB frame (``razer_send_argb_msg``)."""
        if self.argb_info is None:
            raise DeviceNotFound('device has no ARGB interface')
        with self._lock:
            if self._argb_handle is None:
                self._argb_handle = self._backend.open(self.argb_info)
            led_count = len(colours) // 3
            report = bytearray(ARGB_REPORT_SIZE)
            report[0] = 0x04 if channel < 5 else 0x84
            report[1] = channel
            report[2] = channel
            report[3] = 0x00
            report[4] = max(led_count - 1, 0)
            report[5:5 + len(colours)] = colours[:ARGB_REPORT_SIZE - 5]
            self._argb_handle.send_feature_report(bytes((ARGB_FEATURE_REPORT_ID,)) + bytes(report))


class ReportChannel:
    """Requests sent as output reports and answered with input reports."""

    def __init__(self, backend, info: HidDeviceInfo, timeout: float = 0.8):
        self._backend = backend
        self.info = info
        self.timeout = timeout
        self._lock = threading.Lock()
        self._writer = None
        self._reader = None

    def exchange(self, report: bytes, accept) -> Optional[bytes]:
        """Send `report` and return the first input report `accept` takes.

        ``None`` if none came in time.  Windows keeps input reports for every
        handle on a collection, so what Synapse asks for arrives here too, and
        anything left over from an earlier request is still queued: both are
        passed over rather than taken for the answer.
        """
        with self._lock:
            try:
                if self._reader is None:
                    # Before anything is sent: input reports are only queued
                    # for a handle that is already open.
                    self._reader = self._backend.open_input(self.info)
                if self._writer is None:
                    self._writer = self._backend.open(self.info)
                self._writer.send_output_report(report.ljust(self.info.output_length, b'\x00'))
                deadline = time.monotonic() + self.timeout
                while True:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return None
                    answer = self._reader.read(left)
                    if answer is not None and accept(answer):
                        return answer
            except (HidError, OSError):
                # The receiver was pulled out, say.  Start afresh next time.
                self._close()
                raise

    def _close(self) -> None:
        for attribute in ('_writer', '_reader'):
            handle = getattr(self, attribute)
            if handle is not None:
                try:
                    handle.close()
                except Exception:  # pragma: no cover - best effort
                    logger.debug('error closing %s', attribute, exc_info=True)
                setattr(self, attribute, None)

    def close(self) -> None:
        with self._lock:
            self._close()


class FeatureChannel:
    """Requests sent as feature reports and answered the same way, by report id.

    The 90-byte control report works like this too, but :class:`Transport` is
    built around that report's layout and status byte; a device with a layout
    of its own only needs the two calls and the pause between them.
    """

    def __init__(self, backend, info: HidDeviceInfo, wait: float = 0.05):
        self._backend = backend
        self.info = info
        #: How long the device takes to have its reply ready.
        self.wait = wait
        self._lock = threading.Lock()
        self._handle = None

    def exchange(self, report: bytes) -> bytes:
        """Send `report` and return what the device holds for its report id after."""
        with self._lock:
            try:
                if self._handle is None:
                    self._handle = self._backend.open(self.info)
                length = self.info.feature_length
                self._handle.send_feature_report(report.ljust(length, b'\x00'))
                time.sleep(self.wait)
                return self._handle.get_feature_report(length, report[0])
            except (HidError, OSError):
                # The receiver was pulled out, say.  Start afresh next time.
                self._close()
                raise

    def _close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except Exception:  # pragma: no cover - best effort
                logger.debug('error closing the feature channel', exc_info=True)
            self._handle = None

    def close(self) -> None:
        with self._lock:
            self._close()


def open_transport(backend, vendor_id: int, product_id: int,
                   preferred_index: Optional[int] = None,
                   wait_us: int = 600) -> Transport:
    """Find and open the control interface for one device."""
    candidates = backend.enumerate(vendor_id=vendor_id, product_id=product_id)
    info = select_control_interface(candidates, preferred_index)
    if info is None:
        raise DeviceNotFound(
            'no Razer control interface for {0:04x}:{1:04x} '
            '(found {2} HID collections, none with a 90-byte feature report)'.format(
                vendor_id, product_id, len(candidates)))
    argb = next((i for i in candidates if is_argb_interface(i)), None)
    return Transport(backend, info, wait_us=wait_us, argb_info=argb)
