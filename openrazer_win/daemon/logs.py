"""Where the daemon writes its log.

The daemon normally runs detached, with stdout and stderr going nowhere, and a
windowed PyInstaller build has no stderr at all -- ``sys.stderr`` is ``None``.
Without a file there is no way to find out why it failed to start, so it always
keeps a small rotating log next to its endpoint file.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from typing import Optional

from ..core.persistence import default_config_dir

LOG_FORMAT = '%(asctime)s  %(levelname)-7s %(name)s: %(message)s'

#: Small enough to stay diagnostic, bounded so it never fills a disk.
MAX_BYTES = 512 * 1024
BACKUP_COUNT = 2


def log_path() -> str:
    return os.path.join(default_config_dir(), 'daemon.log')


def configure(verbose: bool = False, log_file: Optional[str] = None) -> str:
    """Set up daemon logging.  Returns the file being written to."""
    level = logging.DEBUG if verbose else logging.INFO
    path = log_file or log_path()

    handlers: list = []
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding='utf-8'))
    except OSError:
        path = ''

    # A windowed build has no stderr; adding a handler on None would make every
    # log call raise inside logging itself.
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    formatter = logging.Formatter(LOG_FORMAT)
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(level)
    return path
