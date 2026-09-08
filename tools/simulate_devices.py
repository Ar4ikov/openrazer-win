"""Exercise every supported device against the built-in emulator.

Walks all 260+ product ids in the bundled database, builds a fake device for
each, and calls every capability it advertises.  Nothing here needs hardware --
it verifies that the transpiled recipes actually execute, that no recipe asks
for a variable the device layer never binds, and that the capability report and
the recipe table agree with each other.

Usage::

    python tools/simulate_devices.py [--verbose] [--device NAME]
"""
from __future__ import annotations

import argparse
import collections
import os
import sys
import tempfile
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openrazer_win.core.device import RazerDevice  # noqa: E402
from openrazer_win.core.kraken import KrakenDevice  # noqa: E402
from openrazer_win.core.persistence import Persistence  # noqa: E402
from openrazer_win.core.transport import Transport  # noqa: E402
from openrazer_win.devices import get_database, get_recipes  # noqa: E402
from openrazer_win.hid.fake import FakeHidBackend, FakeRazerDevice  # noqa: E402

GREEN = (0, 255, 0)
BLUE = (0, 0, 255)
RED = (255, 0, 0)

#: effect name -> positional arguments to pass to ``set_<effect>``.
EFFECT_ARGS = {
    'none': (),
    'on': (),
    'spectrum': (),
    'custom': (),
    'static': GREEN,
    'blinking': GREEN,
    'wave': (1,),
    'wheel': (1,),
    'reactive': GREEN + (2,),
    'breath_random': (),
    'breath_single': GREEN,
    'breath_dual': GREEN + BLUE,
    'breath_triple': GREEN + BLUE + RED,
    'starlight_random': (2,),
    'starlight_single': GREEN + (2,),
    'starlight_dual': GREEN + BLUE + (2,),
}


def build_device(meta, persistence, recipes) -> Optional[RazerDevice]:
    """An emulator-backed device, or None for one that is not on the HID bus."""
    if meta.is_bluetooth:
        # The emulator speaks HID; a Bluetooth-only device has no USB data
        # interface at all, and is covered by tests/test_ble.py instead.
        return None

    backend = FakeHidBackend()
    if meta.pid in recipes.kraken_pids:
        # Krakens take output reports on their own collection instead.
        fake = FakeRazerDevice(meta.pid, meta.name, interface=3, kraken=True)
        backend.add(fake)
        transport = Transport(backend, fake.kraken_info, wait_us=0,
                              kraken_info=fake.kraken_info)
        return KrakenDevice(meta, transport, persistence, recipes)

    params = recipes.transport_params(meta.driver, meta.pid)
    fake = FakeRazerDevice(meta.pid, meta.name, interface=params.get('index') or 0,
                           argb=meta.driver == 'accessory')
    backend.add(fake)
    transport = Transport(backend, fake.info, wait_us=0, argb_info=fake.argb_info)
    return RazerDevice(meta, transport, persistence, recipes)


def checks_for(device: RazerDevice) -> list:
    """Build the list of ``(label, callable)`` probes for one device."""
    caps = device.capabilities()
    checks = [('serial', lambda: device.serial)]
    # Krakens report firmware over input reports the port does not read.
    if caps.get('readback', True):
        checks.append(('firmware', lambda: device.firmware_version))
    for zone, zone_caps in caps['zones'].items():
        for effect in zone_caps['effects']:
            args = EFFECT_ARGS[effect]
            checks.append((
                '{0}@{1}'.format(effect, zone),
                lambda e=effect, a=args, z=zone: getattr(device, 'set_' + e)(*a, zone=z),
            ))
        checks.append(('restore@{0}'.format(zone), device.restore))
        if zone_caps['brightness']:
            checks.append((
                'brightness@{0}'.format(zone),
                lambda z=zone: (device.set_brightness(50, z), device.get_brightness(z)),
            ))
    if caps['dpi']:
        checks.append(('dpi', lambda: (device.set_dpi(1600), device.get_dpi())))
    if caps['dpi_stages']:
        checks.append(('dpi_stages', lambda: (
            device.set_dpi_stages(1, [(800, 800), (1600, 1600)]),
            device.get_dpi_stages())))
    if caps['poll_rate']:
        checks.append(('poll_rate', lambda: (device.set_poll_rate(500),
                                             device.get_poll_rate())))
    if caps['battery']:
        checks.append(('battery', lambda: (device.get_battery_level(),
                                           device.is_charging())))
    if caps['idle_time']:
        checks.append(('idle_time', lambda: (device.set_idle_time(600),
                                             device.get_idle_time())))
    if caps['game_mode']:
        checks.append(('game_mode', lambda: (device.set_game_mode(True),
                                             device.get_game_mode())))
    if caps['macro_mode']:
        checks.append(('macro_mode', lambda: (device.set_macro_mode(True),
                                              device.get_macro_mode())))
    if caps['scroll_mode']:
        checks.append(('scroll_mode', lambda: (device.set_scroll_mode(1),
                                               device.get_scroll_mode())))
    if caps['custom_frame']:
        rows, columns = caps['matrix_dimensions'] or (1, 1)
        colours = bytes(GREEN) * columns
        checks.append(('custom_frame', lambda: device.set_key_row(
            bytes((0, 0, columns - 1)) + colours)))
    return checks


def run(selected: str = '', verbose: bool = False) -> int:
    database = get_database()
    recipes = get_recipes()
    persistence = Persistence(os.path.join(tempfile.mkdtemp(), 'persistence.json'))

    total = passed = simulated = 0
    failures: collections.Counter = collections.Counter()
    failed_devices: collections.Counter = collections.Counter()

    for meta in database:
        if selected and selected.lower() not in meta.name.lower():
            continue
        device = build_device(meta, persistence, recipes)
        if device is None:
            continue
        simulated += 1
        for label, check in checks_for(device):
            total += 1
            try:
                check()
                passed += 1
            except Exception as error:  # noqa: BLE001 - this is the report
                failures['{0}: {1}'.format(type(error).__name__, str(error)[:110])] += 1
                failed_devices[meta.name] += 1
                if verbose:
                    print('  FAIL {0:<40s} {1:<22s} {2}'.format(
                        meta.name, label, error))
        device.close()

    print('{0} checks across {1} devices: {2} passed, {3} failed ({4:.2f}%)'.format(
        total, simulated, passed, total - passed,
        100.0 * passed / total if total else 100.0))
    if failures:
        print('\nfailure modes:')
        for message, count in failures.most_common(30):
            print('  {0:5d}  {1}'.format(count, message))
        print('\nmost affected devices:')
        for name, count in failed_devices.most_common(10):
            print('  {0:5d}  {1}'.format(count, name))
    return 0 if not failures else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', default='', help='only simulate matching devices')
    parser.add_argument('--verbose', action='store_true', help='print each failure')
    args = parser.parse_args()
    return run(args.device, args.verbose)


if __name__ == '__main__':
    raise SystemExit(main())
