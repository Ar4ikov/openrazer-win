"""Devices taken from upstream pull requests that are still open.

``tools/upstream_patches/`` holds those pull requests' driver and daemon
changes, and ``tools/upstream_source.py`` applies them to a scratch copy of the
upstream checkout before the generators read it.
"""
from __future__ import annotations

import os
import sys

import pytest

from .conftest import last_report, make_device

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tools'))
import upstream_source  # noqa: E402

PATCH = """diff --git a/driver/x.c b/driver/x.c
--- a/driver/x.c
+++ b/driver/x.c
@@ -1,3 +1,4 @@
 a
 b
+new
 c
"""


@pytest.fixture
def patches(tmp_path, monkeypatch):
    folder = tmp_path / 'patches'
    folder.mkdir()
    monkeypatch.setattr(upstream_source, 'PATCH_DIR', str(folder))
    return folder


def _add_patch(folder, name='one.patch'):
    with open(str(folder / name), 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(PATCH)


def _upstream(root, text: bytes) -> str:
    os.makedirs(str(root / 'driver'))
    os.makedirs(str(root / 'daemon'))
    with open(str(root / 'driver' / 'x.c'), 'wb') as handle:
        handle.write(text)
    return str(root)


def _read(root: str) -> bytes:
    """The file's lines; git writes them back with whatever core.autocrlf says."""
    with open(os.path.join(root, 'driver', 'x.c'), 'rb') as handle:
        return handle.read().replace(b'\r\n', b'\n')


def test_without_patches_the_checkout_is_read_in_place(tmp_path, patches):
    upstream = _upstream(tmp_path / 'up', b'a\nb\nc\n')
    with upstream_source.checkout(upstream) as checkout:
        assert checkout == upstream


def test_a_pending_patch_applies_to_a_windows_checkout(tmp_path, patches):
    _add_patch(patches)
    # git on Windows checks out with core.autocrlf, the patches are LF.
    upstream = _upstream(tmp_path / 'up', b'a\r\nb\r\nc\r\n')
    with upstream_source.checkout(upstream) as checkout:
        assert checkout != upstream
        assert _read(checkout) == b'a\nb\nnew\nc\n'
    assert _read(upstream) == b'a\nb\nc\n'          # the checkout itself is left alone


def test_a_patch_upstream_already_carries_is_skipped(tmp_path, patches, capsys):
    _add_patch(patches)
    upstream = _upstream(tmp_path / 'up', b'a\nb\nnew\nc\n')
    with upstream_source.checkout(upstream) as checkout:
        assert _read(checkout) == b'a\nb\nnew\nc\n'
    assert 'already carries one.patch' in capsys.readouterr().err


def test_a_patch_that_no_longer_applies_stops_the_sync(tmp_path, patches):
    _add_patch(patches)
    upstream = _upstream(tmp_path / 'up', b'x\ny\nz\n')
    with pytest.raises(SystemExit, match='no longer applies'):
        with upstream_source.checkout(upstream):
            pass


# -- what the shipped patches bring ------------------------------------------

@pytest.mark.parametrize('pid, transaction_id', [(0x0277, 0x1F), (0x027B, 0x9F)])
def test_the_pro_type_ultra_is_driven_on_its_third_interface(pid, transaction_id,
                                                             recipes, persistence):
    # openrazer-win#11: wired and wireless both carry the 91-byte feature
    # report on mi=02, which is the interface openrazer#2888 addresses.
    assert recipes.transport_params('kbd', pid)['index'] == 2
    device, fake = make_device(pid, persistence)
    try:
        device.set_static(255, 255, 255)
        report = last_report(fake, 0x0F, 0x02)
        assert report.transaction_id == transaction_id
        # Backlight LED, static, and the pull request's 0x01 in argument 4.
        assert list(report.arguments[:9]) == [0x01, 0x05, 0x01, 0x00, 0x01,
                                              0x01, 0xFF, 0xFF, 0xFF]
    finally:
        device.close()
