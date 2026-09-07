"""Print every connected device and what it can do."""
from openrazer_win.client import DeviceManager

with DeviceManager() as manager:
    if not manager.devices:
        raise SystemExit('No supported Razer devices found.')

    for device in manager.devices:
        print('{0}  ({1})'.format(device.name, device.type))
        print('  serial    {0}'.format(device.serial))
        print('  firmware  {0}'.format(device.firmware_version))

        for zone in device.zones:
            effects = device.fx_for(zone).effects
            print('  zone {0:<12} {1}'.format(zone, ', '.join(effects)))

        if device.has('dpi'):
            print('  dpi       {0} (max {1})'.format(device.dpi, device.max_dpi))
        if device.has('poll_rate'):
            print('  poll rate {0} Hz'.format(device.poll_rate))
        if device.has('battery'):
            print('  battery   {0}%{1}'.format(
                device.battery_level, ' (charging)' if device.is_charging else ''))
        print()
