#!/usr/bin/env python3
"""Draw the control panel's icon and write it as a multi-size .ico.

The same drawing as ``icon_rgba`` in gui/src/shell.rs -- a dark disc, a green
ring, a bright core -- so the executable, the window and the tray agree.
Standard library only: each size is a PNG, which .ico files may contain.

    python tools/make_icon.py gui/assets/openrazer.ico
"""
import struct
import sys
import zlib


def rgba(size: int) -> bytes:
    out = bytearray()
    s = float(size)
    c = (s - 1) / 2

    def mix(a, b, t):
        return [a[i] + (b[i] - a[i]) * t for i in range(3)]

    for y in range(size):
        for x in range(size):
            d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5

            def inside(radius, d=d):
                return min(max(radius - d + 0.5, 0.0), 1.0)

            colour = [18.0, 22.0, 28.0]
            colour = mix(colour, [68.0, 214.0, 44.0], inside(s * 0.44) * (1 - inside(s * 0.31)))
            colour = mix(colour, [184.0, 255.0, 168.0], inside(s * 0.15))
            out += bytes(int(v) for v in colour) + bytes((int(inside(s * 0.48) * 255),))
    return bytes(out)


def png(size: int) -> bytes:
    raw = rgba(size)
    rows = b''.join(b'\x00' + raw[y * size * 4:(y + 1) * size * 4] for y in range(size))

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))

    header = struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header)
            + chunk(b'IDAT', zlib.compress(rows, 9)) + chunk(b'IEND', b''))


def main(path: str) -> None:
    sizes = (16, 24, 32, 48, 64, 128, 256)
    images = [png(size) for size in sizes]
    offset = 6 + 16 * len(sizes)
    directory = struct.pack('<HHH', 0, 1, len(sizes))
    for size, image in zip(sizes, images):
        directory += struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32,
                                 len(image), offset)
        offset += len(image)
    with open(path, 'wb') as handle:
        handle.write(directory + b''.join(images))


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'gui/assets/openrazer.ico')
