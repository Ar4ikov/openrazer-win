#!/usr/bin/env python3
"""Extract the keyboard driver's key handling into ``keys.json``.

In driver mode a Razer keyboard stops handling its own Fn layer and macro keys
and leaves both to the host.  On Linux the kernel driver takes them over:

* ``razer_raw_event_standard()`` reads the vendor report -- report id 0x04,
  one byte per held key -- and turns M1-M5 into F13-F17, the media codes into
  media keys, and notes whether Fn is held.
* ``razer_event()`` then swaps keys pressed with Fn held, through one of
  several ``razer_key_translation`` tables chosen by product id.

There is no kernel driver here, so the daemon does the same job in user space
(``openrazer_win.keys``).  Like every other per-device behaviour in this port
the tables are generated from the C rather than written by hand, so the next
upstream sync picks up new keyboards and changed layouts.

Usage::

    python tools/transpile_keys.py /path/to/openrazer openrazer_win/devices/data/keys.json
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from transpile_recipes import (  # noqa: E402
    _match_brace, load_constants, parse_block, strip_comments,
)

TABLE_RE = re.compile(
    r'static\s+const\s+struct\s+razer_key_translation\s+(\w+)\s*\[\]\s*=\s*\{(.*?)\};',
    re.S)
ENTRY_RE = re.compile(r'\{\s*(\w+)\s*,\s*(\w+)\s*\}')
ALIAS_RE = re.compile(r'^#define\s+(RAZER_\w+_KEY|RAZER_BRIGHTNESS_\w+)\s+(\w+)', re.M)
FIND_RE = re.compile(r'find_translation\(\s*(\w+)\s*,')
RAW_CASE_RE = re.compile(r'case\s+(0x[0-9A-Fa-f]+)\s*:\s*cur_value\s*=\s*(\w+)\s*;')


def function_body(src: str, name: str) -> str:
    match = re.search(r'static\s+\w+\s+' + re.escape(name) + r'\s*\([^)]*\)\s*\{', src)
    if match is None:
        raise SystemExit('cannot find {0}() in the keyboard driver'.format(name))
    brace = match.end() - 1
    return src[brace + 1:_match_brace(src, brace)]


def extract(upstream: str) -> dict:
    driver_dir = os.path.join(upstream, 'driver')
    consts, _pids = load_constants(driver_dir)
    raw_src = open(os.path.join(driver_dir, 'razerkbd_driver.c'), encoding='utf-8').read()
    src = strip_comments(raw_src)

    # The driver names its own pseudo-keys after evdev macro codes; keep the
    # meaningful name, which is what the port acts on.
    aliases = {alias: alias for alias, _code in ALIAS_RE.findall(raw_src)}

    tables = {}
    for name, body in TABLE_RE.findall(src):
        entries = []
        for source, target in ENTRY_RE.findall(body):
            if source == '0':
                continue
            entries.append([source, aliases.get(target, target)])
        tables[name] = entries

    # razer_event(): which table each keyboard uses.
    event = parse_block(function_body(src, 'razer_event'))
    switch = next(node for node in event if node.kind == 'switch' and 'usb_pid' in node.text)
    by_pid: dict = {}
    default = None
    for labels, body in switch.cases:
        text = ' '.join(node.text for node in body)
        found = FIND_RE.search(text)
        if found is None:
            continue
        for label in labels:
            if label == 'default':
                default = found.group(1)
            elif label in consts:
                by_pid['{0:04x}'.format(consts[label])] = found.group(1)

    blade = re.search(r'static bool is_blade_laptop\(.*?\n\}', src, re.S)
    blade_pids = sorted({'{0:04x}'.format(consts[name])
                         for name in re.findall(r'USB_DEVICE_ID_\w+', blade.group(0))
                         if name in consts}) if blade else []

    # razer_raw_event_standard(): vendor report code -> the key it becomes.
    raw = function_body(src, 'razer_raw_event_standard')
    vendor = {'{0:02x}'.format(int(code, 16)): target
              for code, target in RAW_CASE_RE.findall(raw)}
    if not re.search(r'case\s+0x01\s*:.*?found_fn\s*=', raw, re.S):
        raise SystemExit('the Fn marker in razer_raw_event_standard() has moved')

    return {
        'schema': 1,
        'source': 'openrazer/openrazer driver/razerkbd_driver.c',
        #: The vendor report's id, and the code that means "Fn is held".
        'report_id': 0x04,
        'fn_code': 0x01,
        'vendor_codes': vendor,
        'tables': tables,
        'default_table': default,
        'tables_by_pid': by_pid,
        #: Laptops handle all of this in their own firmware.
        'no_translation_pids': blade_pids,
    }


def main(argv: list) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    data = extract(argv[1])
    with open(argv[2], 'w', encoding='utf-8', newline='\n') as handle:
        json.dump(data, handle, indent=1, sort_keys=True)
        handle.write('\n')
    print('wrote {0}: {1} tables, {2} keyboards with their own, {3} vendor codes'.format(
        argv[2], len(data['tables']), len(data['tables_by_pid']), len(data['vendor_codes'])))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
