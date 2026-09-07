# Security

## Scope

`openrazer-win` runs entirely on the local machine and makes no network requests. Its trust
boundary is the daemon:

- The daemon binds **127.0.0.1 only** and refuses any other bind address.
- Each run generates a random bearer token, written to `%LOCALAPPDATA%\openrazer-win\daemon.json`
  with owner-only permissions. Every request must carry it.
- The RPC surface is a **fixed allowlist** of device methods, not `getattr` on the device object.
- Recipe expressions are evaluated by a restricted AST walker that permits only arithmetic,
  comparisons and reads of the request buffer and device response. `eval` is never called.

Anything that can read the endpoint file can already act as the user, so the token defends against
other users on the machine and against local network clients, not against the user themselves.

## Reporting a vulnerability

Open a [security advisory](https://github.com/Ar4ikov/openrazer-win/security/advisories/new), or an
issue if the problem is not sensitive. Please include what you ran, what you expected and what
happened.

## Hardware safety

The port sends the same commands the Linux kernel driver sends, built by the same logic, so it
carries the same risk profile as upstream OpenRazer: it writes lighting, DPI and polling settings,
and never touches firmware.
