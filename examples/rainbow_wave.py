"""Sweep a rainbow across a keyboard's key matrix, one frame at a time.

Shows how to drive per-key lighting directly rather than asking the firmware
for its built-in spectrum effect.
"""
import colorsys
import time

from openrazer_win.client import DeviceManager

FPS = 30
SPEED = 0.35          # hue turns per second


def main() -> int:
    with DeviceManager() as manager:
        matrices = [d for d in manager.devices
                    if d.has('custom_frame') and (d.matrix_dimensions or (1, 1))[1] > 1]
        if not matrices:
            print('No device with an addressable matrix is connected.')
            return 1

        device = matrices[0]
        matrix = device.fx.advanced
        print('Animating {0} ({1}x{2}) -- Ctrl-C to stop'.format(
            device.name, matrix.rows, matrix.columns))

        phase = 0.0
        try:
            while True:
                for column in range(matrix.columns):
                    hue = (phase + column / matrix.columns) % 1.0
                    red, green, blue = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
                    colour = (int(red * 255), int(green * 255), int(blue * 255))
                    for row in range(matrix.rows):
                        matrix[row, column] = colour
                matrix.draw()
                phase = (phase + SPEED / FPS) % 1.0
                time.sleep(1.0 / FPS)
        except KeyboardInterrupt:
            device.fx.spectrum()
            print('\nBack to the hardware spectrum effect.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
