"""Entry point for the frozen GUI executable."""
import multiprocessing
import sys

from openrazer_win.tray.gui import main

if __name__ == '__main__':
    multiprocessing.freeze_support()
    sys.exit(main())
