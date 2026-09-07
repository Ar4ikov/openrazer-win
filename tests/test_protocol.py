"""The 90-byte report and the builders that produce it.

The expected byte strings here were derived from the field layout in
``driver/razercommon.h`` and the builders in ``driver/razerchromacommon.c``, so
a change that breaks wire compatibility with the Linux driver fails here.
"""
from __future__ import annotations

import pytest

from openrazer_win.protocol import chroma
from openrazer_win.protocol.report import (
    ARGUMENT_SIZE, REPORT_SIZE, RGB, Led, RazerReport, Status, VarStore,
    calculate_crc, clamp,
)


def test_report_is_exactly_ninety_bytes():
    assert len(RazerReport().pack()) == REPORT_SIZE


def test_header_layout_matches_the_kernel_struct():
    report = RazerReport(command_class=0x03, command_id=0x0A, data_size=0x04)
    report.transaction_id = 0x3F
    report.remaining_packets = 0x0102
    raw = report.pack()
    assert raw[0] == 0x00           # status
    assert raw[1] == 0x3F           # transaction id
    assert raw[2:4] == b'\x01\x02'  # remaining packets, big endian
    assert raw[4] == 0x00           # protocol type
    assert raw[5] == 0x04           # data size
    assert raw[6] == 0x03           # command class
    assert raw[7] == 0x0A           # command id


def test_crc_is_the_xor_of_bytes_two_through_eightyseven():
    report = chroma.standard_matrix_effect_static(RGB(255, 0, 0))
    report.transaction_id = 0xFF
    raw = report.pack()
    expected = 0
    for byte in raw[2:88]:
        expected ^= byte
    assert raw[88] == expected
    assert calculate_crc(raw) == expected


def test_static_effect_bytes_are_stable():
    report = chroma.standard_matrix_effect_static(RGB(255, 0, 0))
    report.transaction_id = 0xFF
    raw = report.pack()
    assert raw[:12] == bytes((0x00, 0xFF, 0x00, 0x00, 0x00, 0x04, 0x03, 0x0A,
                              0x06, 0xFF, 0x00, 0x00))
    assert raw[88] == 0xF4


def test_unpack_round_trips():
    report = chroma.extended_matrix_effect_breathing_dual(
        VarStore.VARSTORE, Led.BACKLIGHT, RGB(1, 2, 3), RGB(4, 5, 6))
    report.transaction_id = 0x1F
    raw = report.pack()
    assert RazerReport.unpack(raw).pack() == raw


def test_reports_that_differ_are_not_equal():
    assert chroma.standard_matrix_effect_spectrum() != chroma.standard_matrix_effect_none()


def test_arguments_are_bounded():
    report = RazerReport()
    report.set_argument(ARGUMENT_SIZE + 10, 0xFF)
    report.set_arguments(ARGUMENT_SIZE - 1, b'\x01\x02\x03\x04')
    raw = report.pack()
    assert len(raw) == REPORT_SIZE
    assert raw[8 + ARGUMENT_SIZE - 1] == 0x01


def test_matches_only_accepts_the_matching_reply():
    request = chroma.standard_get_serial()
    reply = RazerReport.unpack(request.pack())
    assert request.matches(reply)
    reply.command_class = 0x99
    assert not request.matches(reply)


@pytest.mark.parametrize('value, low, high, expected', [
    (5, 1, 4, 4), (0, 1, 4, 1), (3, 1, 4, 3),
])
def test_clamp(value, low, high, expected):
    assert clamp(value, low, high) == expected


def test_status_names_cover_the_documented_codes():
    assert Status.name(Status.SUCCESSFUL) == 'successful'
    assert Status.name(Status.NOT_SUPPORTED) == 'not supported'
    assert 'unknown' in Status.name(0x42)


# -- builder shapes ---------------------------------------------------------

def test_standard_matrix_effects_use_class_three_command_ten():
    for builder in (chroma.standard_matrix_effect_none,
                    chroma.standard_matrix_effect_spectrum):
        report = builder()
        assert (report.command_class, report.command_id) == (0x03, 0x0A)


def test_extended_matrix_effects_use_class_fifteen():
    report = chroma.extended_matrix_effect_static(
        VarStore.VARSTORE, Led.BACKLIGHT, RGB(9, 8, 7))
    assert (report.command_class, report.command_id) == (0x0F, 0x02)
    assert report.arguments[0] == VarStore.VARSTORE
    assert report.arguments[1] == Led.BACKLIGHT
    assert report.arguments[2] == 0x01           # effect id: static
    assert bytes(report.arguments[6:9]) == bytes((9, 8, 7))


def test_mouse_extended_matrix_effects_use_class_three_command_thirteen():
    report = chroma.mouse_extended_matrix_effect_static(
        VarStore.VARSTORE, Led.LOGO, RGB(1, 2, 3))
    assert (report.command_class, report.command_id) == (0x03, 0x0D)
    assert bytes(report.arguments[3:6]) == bytes((1, 2, 3))


def test_dpi_is_encoded_big_endian():
    report = chroma.misc_set_dpi_xy(VarStore.VARSTORE, 1800, 3200)
    assert report.arguments[1:3] == bytearray((1800 >> 8, 1800 & 0xFF))
    assert report.arguments[3:5] == bytearray((3200 >> 8, 3200 & 0xFF))


def test_dpi_is_clamped_to_the_supported_range():
    report = chroma.misc_set_dpi_xy(VarStore.VARSTORE, 10, 90000)
    assert (report.arguments[1] << 8) | report.arguments[2] == 100
    assert (report.arguments[3] << 8) | report.arguments[4] == 45000


def test_dpi_stages_pack_seven_bytes_per_stage():
    report = chroma.misc_set_dpi_stages(
        VarStore.VARSTORE, 2, 1, [800, 800, 1600, 1600])
    assert report.arguments[0] == VarStore.VARSTORE
    assert report.arguments[1] == 1     # active stage
    assert report.arguments[2] == 2     # stage count
    assert report.arguments[3] == 0     # stage index
    assert (report.arguments[4] << 8) | report.arguments[5] == 800
    assert report.arguments[10] == 1
    assert (report.arguments[11] << 8) | report.arguments[12] == 1600


@pytest.mark.parametrize('rate, code', [(1000, 0x01), (500, 0x02), (125, 0x08),
                                        (333, 0x02)])
def test_polling_rate_codes(rate, code):
    assert chroma.misc_set_polling_rate(rate).arguments[0] == code


@pytest.mark.parametrize('rate, code', [(8000, 0x01), (4000, 0x02), (2000, 0x04),
                                        (1000, 0x08), (500, 0x10), (250, 0x20),
                                        (125, 0x40)])
def test_extended_polling_rate_codes(rate, code):
    assert chroma.misc_set_polling_rate2(rate).arguments[1] == code


def test_custom_frame_row_is_placed_after_the_header():
    colours = bytes(range(9))
    report = chroma.standard_matrix_set_custom_frame(0, 0, 2, colours)
    assert report.arguments[0] == 0xFF
    assert bytes(report.arguments[1:4]) == bytes((0, 0, 2))
    assert bytes(report.arguments[4:13]) == colours


def test_oversized_custom_frame_row_is_dropped_not_truncated_into_the_crc():
    report = chroma.standard_matrix_set_custom_frame(0, 0, 40, bytes(123))
    assert bytes(report.arguments[4:]) == bytes(ARGUMENT_SIZE - 4)


def test_device_mode_refuses_the_factory_test_mode():
    assert chroma.standard_set_device_mode(0x02).arguments[0] == 0x00
    assert chroma.standard_set_device_mode(0x03).arguments[0] == 0x03


def test_every_builder_referenced_by_name_exists():
    for name in ('razer_chroma_standard_matrix_effect_static',
                 'razer_chroma_extended_matrix_effect_static',
                 'razer_chroma_mouse_extended_matrix_effect_none',
                 'razer_chroma_misc_set_dpi_xy',
                 'razer_chroma_extended_matrix_set_custom_frame2',
                 'razer_naga_trinity_effect_static',
                 'razer_basilisk_mobile_effect_static'):
        assert chroma.get_builder(name) is not None


def test_unknown_builder_name_raises():
    with pytest.raises(KeyError):
        chroma.get_builder('razer_chroma_not_a_real_function')


def test_rgb_helpers():
    assert RGB(300, -1, 7) == (300 & 0xFF, -1 & 0xFF, 7)
    assert RGB.from_bytes(b'\x01\x02') == (1, 2, 0)
