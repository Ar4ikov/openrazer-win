"""Upstream OpenRazer as the generators read it: a checkout plus pending pull requests.

Support for a device can sit in an upstream pull request for months.  Rather
than port it by hand, the pull request's driver and daemon changes are kept in
``tools/upstream_patches/`` and applied to a scratch copy of the checkout
before a generator reads it.  The generated data is then exactly what the next
sync would produce once upstream merges the pull request.  When it does, the
patch applies in reverse instead: it is skipped with a note, and is due for
deletion.
"""
from __future__ import annotations

import contextlib
import glob
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Iterator

PATCH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'upstream_patches')

#: The parts of a checkout the generators read.
READ_DIRS = ('driver', 'daemon')


def pending_patches() -> list:
    return sorted(glob.glob(os.path.join(PATCH_DIR, '*.patch')))


def _touched(patch: str) -> list:
    with open(patch, encoding='utf-8') as handle:
        return [line.split()[3][len('b/'):] for line in handle
                if line.startswith('diff --git ')]


def _to_lf(path: str) -> None:
    """A Windows checkout with core.autocrlf has CRLF files; the patches are LF."""
    with open(path, 'rb') as handle:
        data = handle.read()
    with open(path, 'wb') as handle:
        handle.write(data.replace(b'\r\n', b'\n'))


def _git_apply(root: str, patch: str, *flags: str) -> bool:
    try:
        result = subprocess.run(['git', 'apply', *flags, patch], cwd=root,
                                capture_output=True, text=True)
    except FileNotFoundError:
        raise SystemExit('git is needed to apply {0}'.format(patch)) from None
    return result.returncode == 0


@contextlib.contextmanager
def checkout(upstream: str) -> Iterator[str]:
    """Yield the directory to read upstream from: a patched copy, if anything is pending."""
    patches = pending_patches()
    if not patches:
        yield upstream
        return
    with tempfile.TemporaryDirectory(prefix='openrazer-upstream-') as scratch:
        for sub in READ_DIRS:
            shutil.copytree(os.path.join(upstream, sub), os.path.join(scratch, sub),
                            ignore=shutil.ignore_patterns('__pycache__'))
        for patch in patches:
            name = os.path.basename(patch)
            for path in _touched(patch):
                _to_lf(os.path.join(scratch, path))
            if _git_apply(scratch, patch):
                print('applied pending upstream change {0}'.format(name), file=sys.stderr)
            elif _git_apply(scratch, patch, '--reverse', '--check'):
                print('upstream already carries {0}; delete it from {1}'.format(name, PATCH_DIR),
                      file=sys.stderr)
            else:
                raise SystemExit(
                    '{0} no longer applies to {1}: rebase it onto upstream master, or delete '
                    'it if upstream took the change in another form'.format(name, upstream))
        yield scratch
