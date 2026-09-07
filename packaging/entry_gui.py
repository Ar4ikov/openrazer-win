"""Entry point for the frozen GUI executable.

With no arguments it opens the control panel.  With arguments it behaves like
``openrazer-win`` -- which matters because the panel offers to start the daemon
for you, and the only executable it can be sure exists is itself. Without this,
``openrazer-win-gui.exe daemon run`` opened a second control panel, which
offered to start the daemon again, and so on.
"""
import multiprocessing
import sys

if __name__ == '__main__':
    multiprocessing.freeze_support()
    if len(sys.argv) > 1:
        from openrazer_win.cli import main as cli_main
        sys.exit(cli_main())
    from openrazer_win.tray.gui import main as gui_main
    sys.exit(gui_main())
