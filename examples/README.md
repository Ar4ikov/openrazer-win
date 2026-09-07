# Examples

Each script runs against the daemon by default. Start it first:

```bash
openrazer-win daemon start
```

Or pass `--direct` where the script offers it, to open the hardware in-process.

| Script | What it shows |
|---|---|
| [`list_devices.py`](list_devices.py) | Discovery, capabilities and current settings |
| [`rainbow_wave.py`](rainbow_wave.py) | Driving a per-key matrix frame by frame |
| [`battery_notifier.py`](battery_notifier.py) | Polling battery level and reacting to it |
