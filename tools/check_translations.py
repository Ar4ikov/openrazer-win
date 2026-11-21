"""Every string OpenRazer Studio can show has a translation in every language.

The strings are written in English in the code and looked up in `ru.rs` and
`zh.rs`; a string missing from a table is shown in English, which is easy to
miss by eye.  This walks the sources for `t("...")` and `tf("...")` and checks
that each one is in every table, with the same number of `{}` placeholders.

Run from the repository root::

    python tools/check_translations.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STUDIO = ROOT / "studio"
TABLES = {
    "ru": STUDIO / "core" / "src" / "ru.rs",
    "zh": STUDIO / "core" / "src" / "zh.rs",
}

ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", '"': '"', "'": "'"}
CALL = re.compile(r'\btf?\(\s*(r#*)?"((?:[^"\\]|\\.)*)"')
PAIR = re.compile(r'^\s*\("((?:[^"\\]|\\.)*)"\s*,\s*"', re.M)


def unescape(literal: str) -> str:
    out = []
    index = 0
    while index < len(literal):
        char = literal[index]
        if char == "\\" and index + 1 < len(literal):
            nxt = literal[index + 1]
            if nxt == "u" and literal[index + 2] == "{":
                end = literal.index("}", index + 3)
                out.append(chr(int(literal[index + 3:end], 16)))
                index = end + 1
                continue
            out.append(ESCAPES.get(nxt, nxt))
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def table(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    pairs: dict[str, str] = {}
    for match in re.finditer(
        r'^\s*\("((?:[^"\\]|\\.)*)"\s*,\s*"((?:[^"\\]|\\.)*)"\),?\s*$', text, re.M
    ):
        pairs[unescape(match.group(1))] = unescape(match.group(2))
    return pairs


def used_strings() -> dict[str, list[str]]:
    used: dict[str, list[str]] = {}
    for path in sorted(STUDIO.rglob("*.rs")):
        if path.name in ("ru.rs", "zh.rs"):
            continue
        if "target" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for match in CALL.finditer(text):
            used.setdefault(unescape(match.group(2)), []).append(str(path.relative_to(ROOT)))
    return used


def main() -> int:
    used = used_strings()
    problems = 0

    for language, path in TABLES.items():
        if not path.exists():
            print(f"{language}: {path.relative_to(ROOT)} is missing")
            problems += 1
            continue
        pairs = table(path)
        missing = sorted(set(used) - set(pairs))
        for english in missing:
            print(f"{language}: no translation for {english!r} (used in {used[english][0]})")
            problems += len(missing) and 1
        stray = sorted(set(pairs) - set(used))
        for english in stray:
            print(f"{language}: {english!r} is translated but never used")
        for english, translated in pairs.items():
            if "{}" in english or "{}" in translated:
                if english.count("{}") != translated.count("{}"):
                    print(f"{language}: placeholders differ: {english!r} / {translated!r}")
                    problems += 1
        print(f"{language}: {len(pairs)} strings, {len(missing)} missing, {len(stray)} unused")

    print(f"{len(used)} strings used in the sources")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
