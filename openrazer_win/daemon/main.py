"""``python -m openrazer_win.daemon.main`` -- run the daemon in the foreground."""
from __future__ import annotations

import argparse

from .server import serve


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog='openrazer-win-daemon',
        description='Run the openrazer-win background service.')
    parser.add_argument('--host', default='127.0.0.1',
                        help='bind address (loopback only; default 127.0.0.1)')
    parser.add_argument('--port', type=int, default=0,
                        help='TCP port, or 0 to pick a free one')
    parser.add_argument('--no-effects', action='store_true',
                        help='disable host-rendered effects and the keyboard hook')
    parser.add_argument('--emulate', action='store_true',
                        help='run against emulated devices instead of real hardware')
    parser.add_argument('--verbose', '-v', action='store_true')
    parser.add_argument('--log-file', help='write logs here instead of the default location')
    args = parser.parse_args(argv)

    from . import logs
    logs.configure(args.verbose, args.log_file)

    if args.host != '127.0.0.1' and not args.host.startswith('127.'):
        parser.error('the daemon only binds to loopback addresses')

    backend = None
    if args.emulate:
        from .demo import build_demo_backend
        backend = build_demo_backend()

    serve(backend=backend, host=args.host, port=args.port,
          enable_effects=not args.no_effects)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
