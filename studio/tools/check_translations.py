"""Check that every string Studio shows has a Russian and a Chinese translation.

Strings are written in English in the code, inside ``t("...")`` and
``tf("...")``; ``core/src/ru.rs`` pairs each with its Russian and
``core/src/zh.rs`` with its Simplified Chinese. This lists any
string with no pair, and any pair whose string is no longer used, and exits
non-zero if there are either -- so a new string cannot ship in English only.

    python studio/tools/check_translations.py
"""
from __future__ import annotations

import pathlib
import re
import sys

STUDIO = pathlib.Path(__file__).resolve().parent.parent
CALL = re.compile(r'\b(?:t|tf)\(\s*"((?:[^"\\]|\\.)*)"')
PAIR = re.compile(r'^\s*\("((?:[^"\\]|\\.)*)",', re.M)
#: Key names the macro editor passes through ``t`` by variable, not literally.
KEY_NAMES = ("Mute", "Volume down", "Volume up", "Next track", "Previous track",
             "Play/Pause", "Right Ctrl", "Right Shift", "Right Alt")


def used() -> list:
    found, seen = [], set()
    for crate in ("app", "core", "setup"):
        for path in sorted((STUDIO / crate / "src").rglob("*.rs")):
            if path.name in ("ru.rs", "zh.rs"):
                continue
            for match in CALL.finditer(path.read_text(encoding="utf-8")):
                if match.group(1) not in seen:
                    seen.add(match.group(1))
                    found.append(match.group(1))
    for name in KEY_NAMES:
        if name not in seen:
            seen.add(name)
            found.append(name)
    return found


def main() -> int:
    strings = used()
    failed = False
    for name, language in (("ru.rs", "Russian"), ("zh.rs", "Chinese")):
        translated = set(PAIR.findall((STUDIO / "core" / "src" / name).read_text(encoding="utf-8")))
        missing = [s for s in strings if s not in translated]
        # Printed key names are listed so they stay untranslated on purpose.
        unused = sorted(s for s in translated - set(strings)
                        if s not in ("Enter", "Esc", "Tab", "Backspace", "Caps Lock", "Insert", "End",
                                     "Page Up", "Page Down", "Print Screen"))
        for text in missing:
            print("no {0} for: {1}".format(language, text))
        for text in unused:
            print("{0} translated but not used: {1}".format(language, text))
        print("{0}: {1} strings, {2} missing, {3} unused".format(language, len(strings), len(missing), len(unused)))
        failed = failed or bool(missing or unused)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
