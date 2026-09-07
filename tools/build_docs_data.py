"""Regenerate ``docs/devices.json``, the table the documentation site renders.

Keeps the published device list in step with the bundled database without
shipping the full record (methods, recipes) to every page load.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from openrazer_win._version import UPSTREAM_VERSION  # noqa: E402
from openrazer_win.devices import get_database  # noqa: E402

OUT_PATH = os.path.join(ROOT, 'docs', 'devices.json')


def main() -> int:
    rows = []
    for entry in get_database():
        rows.append({
            'n': entry.name,
            'p': '{0:04x}'.format(entry.pid),
            't': entry.type,
            'm': list(entry.matrix_dims) if entry.matrix_dims else None,
            'd': entry.dpi_max,
            'b': 'get_battery' in entry.methods,
            'i': entry.image or '',
        })
    rows.sort(key=lambda row: (row['t'], row['n']))

    payload = {
        'generated_from': 'openrazer {0}'.format(UPSTREAM_VERSION),
        'count': len(rows),
        'devices': rows,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, separators=(',', ':'), ensure_ascii=False)
        handle.write('\n')
    print('wrote {0}: {1} devices'.format(OUT_PATH, len(rows)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
