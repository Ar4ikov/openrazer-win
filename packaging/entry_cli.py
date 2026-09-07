"""Entry point for the frozen CLI executable."""
import multiprocessing
import sys

from openrazer_win.cli import main

if __name__ == '__main__':
    # A frozen binary that re-launches itself for the daemon needs this before
    # anything spawns a child process.
    multiprocessing.freeze_support()
    sys.exit(main())
