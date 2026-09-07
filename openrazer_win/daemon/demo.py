"""Emulated hardware, so the daemon, CLI and tray can be tried without a device."""
from __future__ import annotations

from ..devices.database import get_database
from ..devices.recipes import get_recipes
from ..hid.fake import FakeHidBackend, FakeRazerDevice

#: A keyboard, a mouse and a mousepad -- enough to exercise every code path.
DEMO_DEVICES = (0x0203, 0x0078, 0x0C00)


def build_demo_backend(product_ids=DEMO_DEVICES) -> FakeHidBackend:
    database = get_database()
    recipes = get_recipes()
    backend = FakeHidBackend()
    for index, pid in enumerate(product_ids):
        meta = database.get(0x1532, pid)
        if meta is None:
            continue
        params = recipes.transport_params(meta.driver, meta.pid)
        backend.add(FakeRazerDevice(
            pid, meta.name, interface=params.get('index') or 0,
            serial='DEMO{0:012d}'.format(index + 1),
            argb=meta.driver == 'accessory'))
    return backend
