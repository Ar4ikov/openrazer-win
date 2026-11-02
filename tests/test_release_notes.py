"""The text of a GitHub release, which Studio reads for its Updates page."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tools'))

from release_notes import body, section  # noqa: E402

CHANGELOG = """\
# Changelog

## [Unreleased]

## [1.5.3] - 2026-11-03

### Added

- A thing.

## [1.5.2] - 2026-11-02

### Fixed

- An older thing.
"""


def test_a_section_stops_at_the_next_version():
    assert section(CHANGELOG, '1.5.3') == '### Added\n\n- A thing.'
    assert section(CHANGELOG, '1.5.2') == '### Fixed\n\n- An older thing.'
    assert section(CHANGELOG, '9.9.9') is None


def test_a_version_is_not_mistaken_for_a_longer_one():
    assert section(CHANGELOG, '1.5') is None


def test_the_notes_are_marked_out_for_studio_in_each_language():
    text = body('1.5.3', CHANGELOG, CHANGELOG.replace('A thing', 'Штука'))
    assert '<!-- notes:en -->\n### Added\n\n- A thing.\n<!-- /notes -->' in text
    assert '<!-- notes:ru -->\n### Added\n\n- Штука.\n<!-- /notes -->' in text


def test_no_translation_means_no_russian_part():
    text = body('1.5.3', CHANGELOG, '')
    assert 'notes:ru' not in text and '<details>' not in text


def test_a_version_missing_from_the_changelog_stops_the_release():
    with pytest.raises(ValueError):
        body('9.9.9', CHANGELOG)


def test_the_changelogs_cover_the_version_being_released():
    from openrazer_win import __version__
    root = os.path.join(os.path.dirname(__file__), '..')
    with open(os.path.join(root, 'CHANGELOG.md'), encoding='utf-8') as english:
        assert section(english.read(), __version__), 'CHANGELOG.md'
    with open(os.path.join(root, 'CHANGELOG.ru.md'), encoding='utf-8') as russian:
        assert section(russian.read(), __version__), 'CHANGELOG.ru.md'
