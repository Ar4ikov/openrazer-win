# Contributing

## Setting up

```bash
git clone https://github.com/Ar4ikov/openrazer-win
cd openrazer-win
pip install -e ".[dev]"
```

## Before opening a pull request

```bash
pytest                              # 160 tests, no hardware required
ruff check .
python tools/simulate_devices.py    # every capability of all 267 devices
```

Everything runs against the built-in emulator, so all three pass on any machine — Windows or not.

## Reporting an unsupported device

Run `openrazer-win doctor` and paste the output. It shows the product id, the HID collections
Windows exposes, and which one carries the control interface. If the device is listed as
`NOT IN DATABASE`, it is newer than the bundled data.

The device database and the per-device quirks both come from upstream OpenRazer, so support for a
new device usually starts [there](https://github.com/openrazer/openrazer). Once it lands upstream,
re-syncing here is mechanical:

```bash
git clone --depth 1 https://github.com/openrazer/openrazer /tmp/openrazer
python tools/extract_device_db.py  /tmp/openrazer openrazer_win/devices/data/devices.json
python tools/transpile_recipes.py  /tmp/openrazer openrazer_win/devices/data/recipes.json
python tools/build_docs_data.py
pytest && python tools/simulate_devices.py
```

## Where things live

| Path | What it holds |
|---|---|
| `openrazer_win/protocol/` | The 90-byte report and its builders — a direct port of the C |
| `openrazer_win/hid/` | The ctypes Windows backend, and the emulator used by the tests |
| `openrazer_win/devices/` | Generated data plus the recipe interpreter |
| `openrazer_win/core/` | Transport, device API, discovery, persistence |
| `openrazer_win/daemon/` | The JSON-RPC service |
| `openrazer_win/client/` | The public Python API |
| `openrazer_win/effects/` | Frame buffer, keyboard hook, host-rendered effects |
| `tools/` | The transpiler, the database extractor, the fleet simulator |

## Style

- Python 3.9 compatible; the code reads as 3.9 on purpose (`typing.Optional`, explicit
  `str.format` indices).
- `ruff check .` must pass.
- Comments explain *why*, not *what* — especially where the code mirrors a specific quirk in the
  upstream driver.
- Do not hand-edit `openrazer_win/devices/data/*.json`. Change the generator instead, so the next
  upstream sync does not throw the fix away.
