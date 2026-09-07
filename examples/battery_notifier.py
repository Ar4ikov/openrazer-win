"""Watch battery level on wireless devices and colour the logo accordingly.

Green above 50%, amber above 20%, red below -- a visible low-battery warning
that does not need any notification API.
"""
import time

from openrazer_win.client import DeviceManager

INTERVAL = 60.0


def colour_for(level: float) -> tuple:
    if level > 50:
        return (0, 255, 0)
    if level > 20:
        return (255, 140, 0)
    return (255, 0, 0)


def main() -> int:
    with DeviceManager() as manager:
        wireless = [d for d in manager.devices if d.has('battery')]
        if not wireless:
            print('No battery-powered devices connected.')
            return 1

        print('Watching: {0}'.format(', '.join(d.name for d in wireless)))
        try:
            while True:
                for device in wireless:
                    level = device.battery_level
                    charging = device.is_charging
                    print('{0}: {1}%{2}'.format(
                        device.name, level, ' (charging)' if charging else ''))
                    if not charging:
                        device.fx.static(*colour_for(level))
                time.sleep(INTERVAL)
        except KeyboardInterrupt:
            print('\nStopped.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
