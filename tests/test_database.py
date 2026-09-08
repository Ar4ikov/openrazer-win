"""Integrity of the generated device database, and a full-fleet simulation."""
from __future__ import annotations

import pytest

from openrazer_win.core.persistence import Persistence
from openrazer_win.devices.database import DRIVER_FOR_TYPE

from .conftest import make_device

EXPECTED_MINIMUM_DEVICES = 250


def test_database_covers_the_upstream_fleet(database):
    assert len(database) >= EXPECTED_MINIMUM_DEVICES


def test_every_entry_is_well_formed(database):
    for entry in database:
        assert entry.vid == 0x1532, entry.name
        assert 0 < entry.pid <= 0xFFFF, entry.name
        assert entry.name.strip(), entry.class_name
        assert entry.type in DRIVER_FOR_TYPE, entry.name
        assert entry.driver in ('kbd', 'mouse', 'accessory'), entry.name
        if entry.matrix_dims:
            rows, columns = entry.matrix_dims
            assert rows >= 1 and columns >= 1, entry.name


def test_product_ids_are_unique(database):
    seen = set()
    for entry in database:
        key = (entry.vid, entry.pid)
        assert key not in seen, 'duplicate {0:04x}:{1:04x}'.format(*key)
        seen.add(key)


def test_lookup_by_product_id(database):
    viper = database.get(0x1532, 0x0078)
    assert viper is not None and viper.type == 'mouse'
    assert database.get(0x1532, 0xDEAD) is None


def test_search_by_name(database):
    assert any('blackwidow' in e.name.lower() for e in database.find('BlackWidow'))
    assert database.find('definitely not a razer product') == []


def test_mice_declare_a_maximum_dpi(database):
    missing = [e.name for e in database.by_type('mouse')
               if 'set_dpi_xy' in e.methods and not e.dpi_max]
    assert missing == [], 'mice with DPI control but no maximum: {0}'.format(missing)


def test_every_device_has_recipes_for_what_it_advertises(database, recipes, tmp_path):
    """The daemon's method list and the kernel recipes must agree.

    A device that advertises an effect the driver has no recipe for would fail
    at runtime with a confusing error, so catch it here instead.
    """
    persistence = Persistence(str(tmp_path / 'p.json'))
    problems = []
    for entry in database:
        if entry.is_bluetooth:
            continue        # no recipes at all; see tests/test_ble.py
        device, _fake = make_device(entry.pid, persistence)
        try:
            capabilities = device.capabilities()
            for zone, zone_caps in capabilities['zones'].items():
                for effect in zone_caps['effects']:
                    try:
                        device._effect_attribute(zone, effect)
                    except Exception as error:  # noqa: BLE001 - collected below
                        problems.append('{0} {1}/{2}: {3}'.format(
                            entry.name, zone, effect, error))
        finally:
            device.close()
    assert problems == [], '\n'.join(problems[:20])


@pytest.mark.slow
def test_full_fleet_simulation_passes(tmp_path):
    """Exercise every capability of every device against the emulator."""
    import sys
    import os
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools'))
    from simulate_devices import build_device, checks_for  # noqa: E402
    from openrazer_win.devices import get_database, get_recipes  # noqa: E402

    persistence = Persistence(str(tmp_path / 'p.json'))
    recipes = get_recipes()
    failures = []
    checked = 0
    for entry in get_database():
        device = build_device(entry, persistence, recipes)
        if device is None:
            continue        # not on the HID bus; see tests/test_ble.py
        for label, check in checks_for(device):
            checked += 1
            try:
                check()
            except Exception as error:  # noqa: BLE001 - collected below
                failures.append('{0} {1}: {2}'.format(entry.name, label, error))
        device.close()
    assert checked > 3000, 'expected the fleet to exercise thousands of calls'
    assert failures == [], '\n'.join(failures[:20])
