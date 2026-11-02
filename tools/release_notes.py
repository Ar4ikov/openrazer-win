"""Write the text of a GitHub release from the changelogs.

    python tools/release_notes.py v1.5.3 > release-body.md

Studio shows this text on its *Updates* page, so it has to be what changed
rather than how to download: the version's section of ``CHANGELOG.md`` and of
``CHANGELOG.ru.md``, each between marks Studio looks for, followed by the
paragraphs for someone reading the release on GitHub. A version missing from
``CHANGELOG.md`` fails the release; one missing from the Russian changelog
only means Studio shows the English to everyone.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

DOWNLOAD = """\
**OpenRazer Studio** -- download `openrazer-studio-setup.exe` below and
run it. It installs for your account only (no administrator prompt),
brings the daemon with it, and repairs or removes itself from
*Settings › Apps*, the Start menu, or Studio's own settings. Studio 1.5.3 and
later offer each new release on their *Updates* page.

The classic control panel is still here: `openrazer-win-gui.exe`
runs as is, with the daemon inside it. `openrazer-win.exe` is the
command line, for scripts and terminals. Or install with pip:

```
pip install openrazer-win
```

`SHA256SUMS.txt` lists the checksum of every file; the executables
are not code-signed.

Device data generated from upstream OpenRazer. See the
[documentation](https://ar4ikov.github.io/openrazer-win/) for details.
"""


def section(changelog: str, version: str) -> str | None:
    """The body of ``## [version]`` in ``changelog``, without its heading."""
    start = re.search(rf'^## \[{re.escape(version)}\][^\n]*\n', changelog, re.M)
    if start is None:
        return None
    end = re.search(r'^## \[', changelog[start.end():], re.M)
    return changelog[start.end():start.end() + end.start() if end else None].strip()


def body(version: str, english: str, russian: str = '') -> str:
    """The release's text for ``version``, from the two changelogs."""
    notes = section(english, version)
    if notes is None:
        raise ValueError(f'CHANGELOG.md has no section for {version}')
    text = f'<!-- notes:en -->\n{notes}\n<!-- /notes -->\n\n'
    translated = section(russian, version)
    if translated:
        text += ('<details>\n<summary>По-русски</summary>\n\n'
                 f'<!-- notes:ru -->\n{translated}\n<!-- /notes -->\n\n</details>\n\n')
    return text + '---\n\n' + DOWNLOAD


def main(argv: list) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    version = argv[1].lstrip('v')
    russian = ROOT / 'CHANGELOG.ru.md'
    try:
        text = body(version, (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8'),
                    russian.read_text(encoding='utf-8') if russian.exists() else '')
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    sys.stdout.buffer.write(text.encode('utf-8'))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
