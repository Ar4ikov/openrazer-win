"""Report builders -- a direct port of ``driver/razerchromacommon.c``.

Each function returns an unsent :class:`~openrazer_win.protocol.report.RazerReport`.
Names match the C originals with the ``razer_chroma_`` prefix dropped, so a
recipe extracted from the kernel driver can look the builder up by name.
"""
from __future__ import annotations

from typing import Sequence

from .report import (
    ARGUMENT_SIZE, RGB, MatrixEffect, RazerReport, VarStore, clamp,
    get_razer_report,
)

# Two canned reports the Orochi 2011 needs verbatim; the device predates the
# structured protocol, so the driver ships the full 90-byte blobs.
_OROCHI2011_LED = bytes((
    0x01, 0x00, 0x00, 0x06, 0x48, 0x00, 0x00, 0x00, 0x01, 0xFF, 0x03, 0x05, 0x06,
    0x06, 0x10, 0x10, 0x10, 0x10, 0x24, 0x24, 0x4c, 0x4c, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x01, 0x01, 0x01, 0x01, 0x02, 0x02, 0x01, 0x01, 0x03, 0x03, 0x04,
    0x01, 0x04, 0x04, 0x01, 0x01, 0x05, 0x05, 0x01, 0x01, 0x06, 0x31, 0x88, 0x00,
    0x07, 0x31, 0x87, 0x00, 0x08, 0x08, 0x01, 0x01, 0x09, 0x09, 0x01, 0x01, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x44, 0x01))

_OROCHI2011_DPI = bytes((
    0x01, 0x00, 0x00, 0x05, 0x05, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x4c, 0x4c,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01))


def _as_rgb(value) -> RGB:
    if isinstance(value, RGB):
        return value
    if value is None:
        return RGB(0, 0, 0)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return RGB.from_bytes(bytes(value))
    return RGB(*tuple(value)[:3])


# ---------------------------------------------------------------------------
# Standard device functions
# ---------------------------------------------------------------------------

def standard_set_device_mode(mode: int, param: int = 0x00) -> RazerReport:
    report = get_razer_report(0x00, 0x04, 0x02)
    if mode not in (0x00, 0x03):  # 0x02 is a factory test mode; never send it
        mode = 0x00
    report.set_argument(0, mode)
    report.set_argument(1, 0x00)
    return report


def standard_get_device_mode() -> RazerReport:
    return get_razer_report(0x00, 0x84, 0x02)


def standard_get_serial() -> RazerReport:
    return get_razer_report(0x00, 0x82, 0x16)


def standard_get_firmware_version() -> RazerReport:
    return get_razer_report(0x00, 0x81, 0x02)


# ---------------------------------------------------------------------------
# Standard LED functions
# ---------------------------------------------------------------------------

def standard_set_led_state(variable_storage: int, led_id: int, led_state: int) -> RazerReport:
    report = get_razer_report(0x03, 0x00, 0x03)
    report.set_argument(0, variable_storage)
    report.set_argument(1, led_id)
    report.set_argument(2, clamp(led_state, 0x00, 0x01))
    return report


def standard_get_led_state(variable_storage: int, led_id: int) -> RazerReport:
    report = get_razer_report(0x03, 0x80, 0x03)
    report.set_argument(0, variable_storage)
    report.set_argument(1, led_id)
    return report


def standard_set_led_blinking(variable_storage: int, led_id: int) -> RazerReport:
    report = get_razer_report(0x03, 0x04, 0x04)
    report.set_argument(0, variable_storage)
    report.set_argument(1, led_id)
    report.set_argument(2, 0x05)
    report.set_argument(3, 0x05)
    return report


def standard_set_led_rgb(variable_storage: int, led_id: int, rgb1) -> RazerReport:
    rgb = _as_rgb(rgb1)
    report = get_razer_report(0x03, 0x01, 0x05)
    report.set_argument(0, variable_storage)
    report.set_argument(1, led_id)
    report.set_argument(2, rgb.r)
    report.set_argument(3, rgb.g)
    report.set_argument(4, rgb.b)
    return report


def standard_get_led_rgb(variable_storage: int, led_id: int) -> RazerReport:
    report = get_razer_report(0x03, 0x81, 0x05)
    report.set_argument(0, variable_storage)
    report.set_argument(1, led_id)
    return report


def standard_set_led_effect(variable_storage: int, led_id: int, led_effect: int) -> RazerReport:
    report = get_razer_report(0x03, 0x02, 0x03)
    report.set_argument(0, variable_storage)
    report.set_argument(1, led_id)
    report.set_argument(2, clamp(led_effect, 0x00, 0x05))
    return report


def standard_get_led_effect(variable_storage: int, led_id: int) -> RazerReport:
    report = get_razer_report(0x03, 0x82, 0x03)
    report.set_argument(0, variable_storage)
    report.set_argument(1, led_id)
    return report


def standard_set_led_brightness(variable_storage: int, led_id: int, brightness: int) -> RazerReport:
    report = get_razer_report(0x03, 0x03, 0x03)
    report.set_argument(0, variable_storage)
    report.set_argument(1, led_id)
    report.set_argument(2, brightness)
    return report


def standard_get_led_brightness(variable_storage: int, led_id: int) -> RazerReport:
    report = get_razer_report(0x03, 0x83, 0x03)
    report.set_argument(0, variable_storage)
    report.set_argument(1, led_id)
    return report


# ---------------------------------------------------------------------------
# Standard matrix effects (command class 0x03, command 0x0A)
# ---------------------------------------------------------------------------

def _standard_matrix_effect_base(arg_size: int, effect_id: int) -> RazerReport:
    report = get_razer_report(0x03, 0x0A, arg_size)
    report.set_argument(0, effect_id)
    return report


def standard_matrix_effect_none() -> RazerReport:
    return _standard_matrix_effect_base(0x01, MatrixEffect.OFF)


def standard_matrix_effect_wave(wave_direction: int) -> RazerReport:
    report = _standard_matrix_effect_base(0x02, MatrixEffect.WAVE)
    report.set_argument(1, clamp(wave_direction, 0x01, 0x02))
    return report


def standard_matrix_effect_spectrum() -> RazerReport:
    return _standard_matrix_effect_base(0x01, MatrixEffect.SPECTRUM)


def standard_matrix_effect_reactive(speed: int, rgb1) -> RazerReport:
    rgb = _as_rgb(rgb1)
    report = _standard_matrix_effect_base(0x05, MatrixEffect.REACTIVE)
    report.set_argument(1, clamp(speed, 0x01, 0x04))
    report.set_argument(2, rgb.r)
    report.set_argument(3, rgb.g)
    report.set_argument(4, rgb.b)
    return report


def standard_matrix_effect_static(rgb1) -> RazerReport:
    rgb = _as_rgb(rgb1)
    report = _standard_matrix_effect_base(0x04, MatrixEffect.STATIC)
    report.set_argument(1, rgb.r)
    report.set_argument(2, rgb.g)
    report.set_argument(3, rgb.b)
    return report


def standard_matrix_effect_starlight_single(speed: int, rgb1) -> RazerReport:
    rgb = _as_rgb(rgb1)
    report = _standard_matrix_effect_base(0x01, MatrixEffect.STARLIGHT)
    report.set_argument(1, 0x01)
    report.set_argument(2, clamp(speed, 0x01, 0x03))
    report.set_argument(3, rgb.r)
    report.set_argument(4, rgb.g)
    report.set_argument(5, rgb.b)
    return report


def standard_matrix_effect_starlight_dual(speed: int, rgb1, rgb2) -> RazerReport:
    first, second = _as_rgb(rgb1), _as_rgb(rgb2)
    report = _standard_matrix_effect_base(0x01, MatrixEffect.STARLIGHT)
    report.set_argument(1, 0x02)
    report.set_argument(2, clamp(speed, 0x01, 0x03))
    report.set_arguments(3, first)
    report.set_arguments(6, second)
    return report


def standard_matrix_effect_starlight_random(speed: int) -> RazerReport:
    report = _standard_matrix_effect_base(0x01, MatrixEffect.STARLIGHT)
    report.set_argument(1, 0x03)
    report.set_argument(2, clamp(speed, 0x01, 0x03))
    return report


def standard_matrix_effect_breathing_random() -> RazerReport:
    report = _standard_matrix_effect_base(0x08, MatrixEffect.BREATHING)
    report.set_argument(1, 0x03)
    return report


def standard_matrix_effect_breathing_single(rgb1) -> RazerReport:
    report = _standard_matrix_effect_base(0x08, MatrixEffect.BREATHING)
    report.set_argument(1, 0x01)
    report.set_arguments(2, _as_rgb(rgb1))
    return report


def standard_matrix_effect_breathing_dual(rgb1, rgb2) -> RazerReport:
    report = _standard_matrix_effect_base(0x08, MatrixEffect.BREATHING)
    report.set_argument(1, 0x02)
    report.set_arguments(2, _as_rgb(rgb1))
    report.set_arguments(5, _as_rgb(rgb2))
    return report


def standard_matrix_effect_custom_frame(variable_storage: int) -> RazerReport:
    report = _standard_matrix_effect_base(0x02, MatrixEffect.CUSTOMFRAME)
    report.set_argument(1, variable_storage)
    return report


def standard_matrix_set_custom_frame(row_index: int, start_col: int, stop_col: int,
                                     rgb_data: Sequence[int]) -> RazerReport:
    start_arg_offset = 4
    row_length = ((stop_col + 1) - start_col) * 3
    if row_length > ARGUMENT_SIZE - start_arg_offset:
        row_length = 0
    report = get_razer_report(0x03, 0x0B, 0x46)
    report.set_argument(0, 0xFF)
    report.set_argument(1, row_index)
    report.set_argument(2, start_col)
    report.set_argument(3, stop_col)
    report.set_arguments(start_arg_offset, bytes(rgb_data)[:row_length])
    return report


# ---------------------------------------------------------------------------
# Extended matrix effects (command class 0x0F)
# ---------------------------------------------------------------------------

def _extended_matrix_effect_base(arg_size: int, variable_storage: int, led_id: int,
                                 effect_id: int) -> RazerReport:
    report = get_razer_report(0x0F, 0x02, arg_size)
    report.set_argument(0, variable_storage)
    report.set_argument(1, led_id)
    report.set_argument(2, effect_id)
    return report


def extended_matrix_effect_none(variable_storage: int, led_id: int) -> RazerReport:
    return _extended_matrix_effect_base(0x06, variable_storage, led_id, 0x00)


def extended_matrix_effect_static(variable_storage: int, led_id: int, rgb) -> RazerReport:
    report = _extended_matrix_effect_base(0x09, variable_storage, led_id, 0x01)
    report.set_argument(5, 0x01)
    report.set_arguments(6, _as_rgb(rgb))
    return report


def extended_matrix_effect_wave(variable_storage: int, led_id: int, direction: int) -> RazerReport:
    report = _extended_matrix_effect_base(0x06, variable_storage, led_id, 0x04)
    report.set_argument(3, clamp(direction, 0x00, 0x02))
    report.set_argument(4, 0x28)
    return report


def extended_matrix_effect_starlight_random(variable_storage: int, led_id: int,
                                            speed: int) -> RazerReport:
    report = _extended_matrix_effect_base(0x06, variable_storage, led_id, 0x07)
    report.set_argument(4, clamp(speed, 0x01, 0x03))
    return report


def extended_matrix_effect_starlight_single(variable_storage: int, led_id: int,
                                            speed: int, rgb1) -> RazerReport:
    report = _extended_matrix_effect_base(0x09, variable_storage, led_id, 0x07)
    report.set_argument(4, clamp(speed, 0x01, 0x03))
    report.set_argument(5, 0x01)
    report.set_arguments(6, _as_rgb(rgb1))
    return report


def extended_matrix_effect_starlight_dual(variable_storage: int, led_id: int, speed: int,
                                          rgb1, rgb2) -> RazerReport:
    report = _extended_matrix_effect_base(0x0C, variable_storage, led_id, 0x07)
    report.set_argument(4, clamp(speed, 0x01, 0x03))
    report.set_argument(5, 0x02)
    report.set_arguments(6, _as_rgb(rgb1))
    report.set_arguments(9, _as_rgb(rgb2))
    return report


def extended_matrix_effect_spectrum(variable_storage: int, led_id: int) -> RazerReport:
    return _extended_matrix_effect_base(0x06, variable_storage, led_id, 0x03)


def extended_matrix_effect_wheel(variable_storage: int, led_id: int, direction: int) -> RazerReport:
    report = _extended_matrix_effect_base(0x06, variable_storage, led_id, 0x0A)
    report.set_argument(3, clamp(direction, 0x01, 0x02))
    report.set_argument(4, 0x28)
    return report


def extended_matrix_effect_reactive(variable_storage: int, led_id: int, speed: int,
                                    rgb) -> RazerReport:
    report = _extended_matrix_effect_base(0x09, variable_storage, led_id, 0x05)
    report.set_argument(4, clamp(speed, 0x01, 0x04))
    report.set_argument(5, 0x01)
    report.set_arguments(6, _as_rgb(rgb))
    return report


def extended_matrix_effect_breathing_random(variable_storage: int, led_id: int) -> RazerReport:
    return _extended_matrix_effect_base(0x06, variable_storage, led_id, 0x02)


def extended_matrix_effect_breathing_single(variable_storage: int, led_id: int,
                                            rgb1) -> RazerReport:
    report = _extended_matrix_effect_base(0x09, variable_storage, led_id, 0x02)
    report.set_argument(3, 0x01)
    report.set_argument(5, 0x01)
    report.set_arguments(6, _as_rgb(rgb1))
    return report


def extended_matrix_effect_breathing_dual(variable_storage: int, led_id: int,
                                          rgb1, rgb2) -> RazerReport:
    report = _extended_matrix_effect_base(0x0C, variable_storage, led_id, 0x02)
    report.set_argument(3, 0x02)
    report.set_argument(5, 0x02)
    report.set_arguments(6, _as_rgb(rgb1))
    report.set_arguments(9, _as_rgb(rgb2))
    return report


def extended_matrix_effect_custom_frame() -> RazerReport:
    return _extended_matrix_effect_base(0x0C, 0x00, 0x00, 0x08)


def extended_matrix_brightness(variable_storage: int, led_id: int, brightness: int) -> RazerReport:
    report = get_razer_report(0x0F, 0x04, 0x03)
    report.set_argument(0, variable_storage)
    report.set_argument(1, led_id)
    report.set_argument(2, brightness)
    return report


def extended_matrix_get_brightness(variable_storage: int, led_id: int) -> RazerReport:
    report = get_razer_report(0x0F, 0x84, 0x03)
    report.set_argument(0, variable_storage)
    report.set_argument(1, led_id)
    return report


def extended_matrix_set_custom_frame2(row_index: int, start_col: int, stop_col: int,
                                      rgb_data: Sequence[int],
                                      packet_length: int = 0x47) -> RazerReport:
    start_arg_offset = 5
    row_length = ((stop_col + 1) - start_col) * 3
    if row_length > ARGUMENT_SIZE - start_arg_offset:
        row_length = 0
    data_length = packet_length if packet_length else row_length + 5
    report = get_razer_report(0x0F, 0x03, data_length)
    report.set_argument(2, row_index)
    report.set_argument(3, start_col)
    report.set_argument(4, stop_col)
    report.set_arguments(start_arg_offset, bytes(rgb_data)[:row_length])
    return report


def extended_matrix_set_custom_frame(row_index: int, start_col: int, stop_col: int,
                                     rgb_data: Sequence[int]) -> RazerReport:
    return extended_matrix_set_custom_frame2(row_index, start_col, stop_col, rgb_data, 0x47)


# ---------------------------------------------------------------------------
# Extended matrix effects for mice (command class 0x03, command 0x0D)
# ---------------------------------------------------------------------------

def _mouse_extended_matrix_effect_base(arg_size: int, variable_storage: int, led_id: int,
                                       effect_id: int) -> RazerReport:
    report = get_razer_report(0x03, 0x0D, arg_size)
    report.set_argument(0, variable_storage)
    report.set_argument(1, led_id)
    report.set_argument(2, effect_id)
    return report


def mouse_extended_matrix_effect_none(variable_storage: int, led_id: int) -> RazerReport:
    return _mouse_extended_matrix_effect_base(0x03, variable_storage, led_id, 0x00)


def mouse_extended_matrix_effect_static(variable_storage: int, led_id: int, rgb) -> RazerReport:
    report = _mouse_extended_matrix_effect_base(0x06, variable_storage, led_id, 0x06)
    report.set_arguments(3, _as_rgb(rgb))
    return report


def mouse_extended_matrix_effect_spectrum(variable_storage: int, led_id: int) -> RazerReport:
    return _mouse_extended_matrix_effect_base(0x03, variable_storage, led_id, 0x04)


def mouse_extended_matrix_effect_reactive(variable_storage: int, led_id: int, speed: int,
                                          rgb) -> RazerReport:
    report = _mouse_extended_matrix_effect_base(0x07, variable_storage, led_id, 0x02)
    report.set_argument(3, clamp(speed, 0x01, 0x04))
    report.set_arguments(4, _as_rgb(rgb))
    return report


def mouse_extended_matrix_effect_breathing_random(variable_storage: int, led_id: int) -> RazerReport:
    report = _mouse_extended_matrix_effect_base(0x0A, variable_storage, led_id, 0x03)
    report.set_argument(3, 0x03)
    return report


def mouse_extended_matrix_effect_breathing_single(variable_storage: int, led_id: int,
                                                  rgb1) -> RazerReport:
    report = _mouse_extended_matrix_effect_base(0x0A, variable_storage, led_id, 0x03)
    report.set_argument(3, 0x01)
    report.set_arguments(4, _as_rgb(rgb1))
    return report


def mouse_extended_matrix_effect_breathing_dual(variable_storage: int, led_id: int,
                                                rgb1, rgb2) -> RazerReport:
    report = _mouse_extended_matrix_effect_base(0x0A, variable_storage, led_id, 0x03)
    report.set_argument(3, 0x02)
    report.set_arguments(4, _as_rgb(rgb1))
    report.set_arguments(7, _as_rgb(rgb2))
    return report


# ---------------------------------------------------------------------------
# Misc functions
# ---------------------------------------------------------------------------

def misc_fn_key_toggle(state: int) -> RazerReport:
    report = get_razer_report(0x02, 0x06, 0x02)
    report.set_argument(0, 0x00)
    report.set_argument(1, clamp(state, 0x00, 0x01))
    return report


def misc_set_keyswitch_optimization_command1(optimization_mode: int) -> RazerReport:
    report = get_razer_report(0x02, 0x02, 0x04)
    if optimization_mode == 0x00:
        report.set_arguments(0, (0x00, 0x14, 0x00, 0x28, 0x00))
    return report


def misc_set_keyswitch_optimization_command2(optimization_mode: int) -> RazerReport:
    report = get_razer_report(0x02, 0x15, 0x05)
    if optimization_mode == 0x00:
        report.set_arguments(0, (0x01, 0x00, 0x14, 0x00, 0x28, 0x00))
    elif optimization_mode == 0x01:
        report.set_argument(0, 0x01)
    return report


def misc_get_keyswitch_optimization() -> RazerReport:
    return get_razer_report(0x02, 0x82, 0x04)


def misc_set_blade_brightness(brightness: int) -> RazerReport:
    report = get_razer_report(0x0E, 0x04, 0x02)
    report.set_argument(0, 0x01)
    report.set_argument(1, brightness)
    return report


def misc_get_blade_brightness() -> RazerReport:
    report = get_razer_report(0x0E, 0x84, 0x02)
    report.set_argument(0, 0x01)
    return report


def misc_one_row_set_custom_frame(start_col: int, stop_col: int,
                                  rgb_data: Sequence[int]) -> RazerReport:
    start_arg_offset = 2
    row_length = ((stop_col + 1) - start_col) * 3
    if row_length > ARGUMENT_SIZE - start_arg_offset:
        row_length = 0
    report = get_razer_report(0x03, 0x0C, 0x32)
    report.set_argument(0, start_col)
    report.set_argument(1, stop_col)
    report.set_arguments(start_arg_offset, bytes(rgb_data)[:row_length])
    return report


def misc_matrix_reactive_trigger() -> RazerReport:
    report = _standard_matrix_effect_base(0x05, MatrixEffect.REACTIVE)
    for index in range(1, 5):
        report.set_argument(index, 0x00)
    return report


def misc_get_battery_level() -> RazerReport:
    return get_razer_report(0x07, 0x80, 0x02)


def misc_get_charging_status() -> RazerReport:
    return get_razer_report(0x07, 0x84, 0x02)


def misc_set_dock_charge_type(charge_type: int) -> RazerReport:
    report = get_razer_report(0x03, 0x10, 0x01)
    report.set_argument(0, clamp(charge_type, 0x00, 0x01))
    return report


def misc_get_polling_rate() -> RazerReport:
    return get_razer_report(0x00, 0x85, 0x01)


_POLL_RATE_CODES = {1000: 0x01, 500: 0x02, 125: 0x08}
_POLL_RATE2_CODES = {8000: 0x01, 4000: 0x02, 2000: 0x04, 1000: 0x08,
                     500: 0x10, 250: 0x20, 125: 0x40}


def misc_set_polling_rate(polling_rate: int) -> RazerReport:
    report = get_razer_report(0x00, 0x05, 0x01)
    report.set_argument(0, _POLL_RATE_CODES.get(polling_rate, 0x02))
    return report


def misc_get_polling_rate2() -> RazerReport:
    return get_razer_report(0x00, 0xC0, 0x01)


def misc_set_polling_rate2(polling_rate: int, argument: int = 0x00) -> RazerReport:
    report = get_razer_report(0x00, 0x40, 0x02)
    report.set_argument(0, argument)
    report.set_argument(1, _POLL_RATE2_CODES.get(polling_rate, 0x10))
    return report


def misc_get_dock_brightness() -> RazerReport:
    return get_razer_report(0x07, 0x82, 0x01)


def misc_set_dock_brightness(brightness: int) -> RazerReport:
    report = get_razer_report(0x07, 0x02, 0x01)
    report.set_argument(0, brightness)
    return report


def misc_set_dpi_xy(variable_storage: int, dpi_x: int, dpi_y: int) -> RazerReport:
    report = get_razer_report(0x04, 0x05, 0x07)
    dpi_x = clamp(dpi_x, 100, 45000)
    dpi_y = clamp(dpi_y, 100, 45000)
    report.set_argument(0, VarStore.VARSTORE)
    report.set_argument(1, (dpi_x >> 8) & 0xFF)
    report.set_argument(2, dpi_x & 0xFF)
    report.set_argument(3, (dpi_y >> 8) & 0xFF)
    report.set_argument(4, dpi_y & 0xFF)
    return report


def misc_get_dpi_xy(variable_storage: int) -> RazerReport:
    report = get_razer_report(0x04, 0x85, 0x07)
    report.set_argument(0, variable_storage)
    return report


def misc_set_dpi_xy_byte(dpi_x: int, dpi_y: int) -> RazerReport:
    report = get_razer_report(0x04, 0x01, 0x03)
    report.set_argument(0, dpi_x)
    report.set_argument(1, dpi_y)
    report.set_argument(2, 0x00)
    return report


def misc_get_dpi_xy_byte() -> RazerReport:
    return get_razer_report(0x04, 0x81, 0x03)


def misc_set_dpi_stages(variable_storage: int, count: int, active_stage: int,
                        dpi: Sequence[int]) -> RazerReport:
    """`dpi` is a flat sequence of x, y pairs -- two entries per stage."""
    report = get_razer_report(0x04, 0x06, 0x26)
    report.set_argument(0, variable_storage)
    report.set_argument(1, active_stage)
    report.set_argument(2, count)
    offset = 3
    for index in range(count):
        dpi_x = dpi[index * 2]
        dpi_y = dpi[index * 2 + 1]
        report.set_argument(offset, index)
        report.set_argument(offset + 1, (dpi_x >> 8) & 0xFF)
        report.set_argument(offset + 2, dpi_x & 0xFF)
        report.set_argument(offset + 3, (dpi_y >> 8) & 0xFF)
        report.set_argument(offset + 4, dpi_y & 0xFF)
        report.set_argument(offset + 5, 0x00)
        report.set_argument(offset + 6, 0x00)
        offset += 7
    return report


def misc_get_dpi_stages(variable_storage: int) -> RazerReport:
    report = get_razer_report(0x04, 0x86, 0x26)
    report.set_argument(0, variable_storage)
    return report


def misc_get_idle_time() -> RazerReport:
    return get_razer_report(0x07, 0x83, 0x02)


def misc_set_idle_time(idle_time: int) -> RazerReport:
    report = get_razer_report(0x07, 0x03, 0x02)
    idle_time = clamp(idle_time, 60, 900)
    report.set_argument(0, (idle_time >> 8) & 0xFF)
    report.set_argument(1, idle_time & 0xFF)
    return report


def misc_get_low_battery_threshold() -> RazerReport:
    return get_razer_report(0x07, 0x81, 0x01)


def misc_set_low_battery_threshold(battery_threshold: int) -> RazerReport:
    report = get_razer_report(0x07, 0x01, 0x01)
    report.set_argument(0, clamp(battery_threshold, 0x0C, 0x3F))
    return report


def misc_set_orochi2011_led(led_bitfield: int) -> RazerReport:
    report = RazerReport.unpack(_OROCHI2011_LED.ljust(90, b'\x00'))
    report.set_argument(1, led_bitfield)
    return report


def misc_set_orochi2011_poll_dpi(poll_rate: int, dpi_x: int, dpi_y: int) -> RazerReport:
    report = RazerReport.unpack(_OROCHI2011_DPI.ljust(90, b'\x00'))
    report.set_argument(1, _POLL_RATE_CODES.get(poll_rate, 0x02))
    report.set_argument(3, clamp(dpi_x, 0x15, 0x9C))
    report.set_argument(4, clamp(dpi_y, 0x15, 0x9C))
    return report


def basilisk_mobile_effect_static(rgb) -> RazerReport:
    colour = _as_rgb(rgb)
    report = get_razer_report(0x0F, 0x03, 0x0B)
    report.set_argument(4, 0x01)
    report.set_arguments(5, colour)
    report.set_arguments(8, colour)
    return report


def naga_trinity_effect_static(rgb) -> RazerReport:
    colour = _as_rgb(rgb)
    report = get_razer_report(0x0F, 0x03, 0x0E)
    report.set_argument(4, 0x02)
    report.set_arguments(5, colour)
    report.set_arguments(8, colour)
    report.set_arguments(11, colour)
    return report


def misc_set_scroll_mode(scroll_mode: int) -> RazerReport:
    report = get_razer_report(0x02, 0x14, 0x02)
    report.set_argument(0, VarStore.VARSTORE)
    report.set_argument(1, scroll_mode)
    return report


def misc_get_scroll_mode() -> RazerReport:
    report = get_razer_report(0x02, 0x94, 0x02)
    report.set_argument(0, VarStore.VARSTORE)
    return report


def misc_set_scroll_acceleration(acceleration: bool) -> RazerReport:
    report = get_razer_report(0x02, 0x16, 0x02)
    report.set_argument(0, VarStore.VARSTORE)
    report.set_argument(1, 1 if acceleration else 0)
    return report


def misc_get_scroll_acceleration() -> RazerReport:
    report = get_razer_report(0x02, 0x96, 0x02)
    report.set_argument(0, VarStore.VARSTORE)
    return report


def misc_set_scroll_smart_reel(smart_reel: bool) -> RazerReport:
    report = get_razer_report(0x02, 0x17, 0x02)
    report.set_argument(0, VarStore.VARSTORE)
    report.set_argument(1, 1 if smart_reel else 0)
    return report


def misc_get_scroll_smart_reel() -> RazerReport:
    report = get_razer_report(0x02, 0x97, 0x02)
    report.set_argument(0, VarStore.VARSTORE)
    return report


def misc_set_hyperpolling_wireless_dongle_indicator_led_mode(mode: int) -> RazerReport:
    report = get_razer_report(0x07, 0x10, 0x01)
    if mode < 0x01 or mode > 0x03:
        mode = 0x01
    report.set_argument(0, mode)
    return report


def misc_set_hyperpolling_wireless_dongle_pair_step1(pid: int) -> RazerReport:
    report = get_razer_report(0x00, 0x46, 0x01)
    report.set_argument(0, 0x01)
    return report


def misc_set_hyperpolling_wireless_dongle_pair_step2(pid: int) -> RazerReport:
    report = get_razer_report(0x00, 0x41, 0x03)
    report.set_argument(0, 0x01)
    report.set_argument(1, (pid >> 8) & 0xFF)
    report.set_argument(2, pid & 0xFF)
    return report


def misc_set_hyperpolling_wireless_dongle_unpair(pid: int) -> RazerReport:
    report = get_razer_report(0x00, 0x42, 0x02)
    report.set_argument(0, (pid >> 8) & 0xFF)
    report.set_argument(1, pid & 0xFF)
    return report


# ---------------------------------------------------------------------------
# Name lookup used by the recipe interpreter
# ---------------------------------------------------------------------------

#: Maps the C function names used in the kernel driver to the builders above.
BUILDERS: dict = {}

_BUILDER_PREFIXES = ('standard_', 'extended_', 'mouse_extended_', 'misc_')


def _register_builders() -> None:
    for name, value in list(globals().items()):
        if name.startswith('_') or not callable(value):
            continue
        if name.startswith(_BUILDER_PREFIXES):
            BUILDERS['razer_chroma_' + name] = value
    BUILDERS['razer_naga_trinity_effect_static'] = naga_trinity_effect_static
    BUILDERS['razer_basilisk_mobile_effect_static'] = basilisk_mobile_effect_static
    BUILDERS['get_razer_report'] = get_razer_report


_register_builders()


def get_builder(name: str):
    """Look up a report builder by its kernel-driver name."""
    builder = BUILDERS.get(name)
    if builder is None:
        raise KeyError('unknown report builder: {0}'.format(name))
    return builder
