# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for symbol_layout.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from pin_extractor import PadInfo
from kicad_sym_writer import PinSide, PinType, PIN_SPACING
from symbol_layout import classify_pin, get_pin_type, create_default_layout


class TestClassifyPin:
    def test_power_high_vdd(self):
        assert classify_pin("VDD") == PinSide.TOP

    def test_power_high_vcc(self):
        assert classify_pin("VCC") == PinSide.TOP

    def test_power_high_vdda(self):
        assert classify_pin("VDDA") == PinSide.TOP

    def test_power_high_avdd(self):
        assert classify_pin("AVDD") == PinSide.TOP

    def test_power_high_dvdd(self):
        assert classify_pin("DVDD") == PinSide.TOP

    def test_power_low_gnd(self):
        assert classify_pin("GND") == PinSide.BOTTOM

    def test_power_low_vss(self):
        assert classify_pin("VSS") == PinSide.BOTTOM

    def test_power_low_vssa(self):
        assert classify_pin("VSSA") == PinSide.BOTTOM

    def test_power_low_agnd(self):
        assert classify_pin("AGND") == PinSide.BOTTOM

    def test_signal_pin(self):
        assert classify_pin("CLK") == PinSide.LEFT

    def test_signal_data(self):
        assert classify_pin("DATA_IN") == PinSide.LEFT

    def test_case_insensitive(self):
        assert classify_pin("vdd") == PinSide.TOP
        assert classify_pin("gnd") == PinSide.BOTTOM

    def test_numbered_pin(self):
        """Numbered-only pins go to signal side"""
        assert classify_pin("42") == PinSide.LEFT


class TestGetPinType:
    def test_power_pins(self):
        assert get_pin_type("VDD", PinSide.TOP) == PinType.POWER_IN
        assert get_pin_type("GND", PinSide.BOTTOM) == PinType.POWER_IN

    def test_signal_pins(self):
        assert get_pin_type("CLK", PinSide.LEFT) == PinType.PASSIVE


class TestCreateDefaultLayout:
    def _make_pads(self, names):
        """Helper to create PadInfo list with given names"""
        pads = []
        for i, name in enumerate(names):
            pads.append(PadInfo(
                index=i,
                bbox=(0, 0, 100000, 100000),
                center_x=50000,
                center_y=50000,
                width=100000,
                height=100000,
                name=name,
            ))
        return pads

    def test_basic_layout(self):
        pads = self._make_pads(["VDD", "GND", "CLK", "DATA"])
        sym = create_default_layout(pads, "TEST")

        assert sym.name == "TEST"
        assert len(sym.pins) == 4

        counts = sym.pin_count_per_side()
        assert counts[PinSide.TOP] == 1     # VDD
        assert counts[PinSide.BOTTOM] == 1  # GND
        assert counts[PinSide.LEFT] == 1    # CLK
        assert counts[PinSide.RIGHT] == 1   # DATA

    def test_signal_split_alphabetical(self):
        """Signal pins should be split left/right in alphabetical order"""
        pads = self._make_pads(["D", "C", "B", "A"])
        sym = create_default_layout(pads, "TEST")

        left_pins = [p for p in sym.pins if p.side == PinSide.LEFT]
        right_pins = [p for p in sym.pins if p.side == PinSide.RIGHT]

        left_names = [p.name for p in left_pins]
        right_names = [p.name for p in right_pins]

        # First half alphabetically on left
        assert left_names == ["A", "B"]
        # Second half on right
        assert right_names == ["C", "D"]

    def test_odd_signal_count(self):
        """With odd signal count, left gets the extra pin"""
        pads = self._make_pads(["A", "B", "C"])
        sym = create_default_layout(pads, "TEST")

        counts = sym.pin_count_per_side()
        assert counts[PinSide.LEFT] == 2
        assert counts[PinSide.RIGHT] == 1

    def test_all_power_pins(self):
        pads = self._make_pads(["VDD", "VCC", "GND", "VSS"])
        sym = create_default_layout(pads, "POWER")

        counts = sym.pin_count_per_side()
        assert counts[PinSide.TOP] == 2
        assert counts[PinSide.BOTTOM] == 2
        assert counts[PinSide.LEFT] == 0
        assert counts[PinSide.RIGHT] == 0

    def test_body_size_scales_with_pins(self):
        """More pins should produce a larger body"""
        small_pads = self._make_pads(["A", "B"])
        big_pads = self._make_pads([f"SIG_{i}" for i in range(20)])

        small_sym = create_default_layout(small_pads, "SMALL")
        big_sym = create_default_layout(big_pads, "BIG")

        assert big_sym.body_height > small_sym.body_height

    def test_unnamed_pads_use_numbers(self):
        """Pads without names should get sequential numbers"""
        pads = [
            PadInfo(0, (0, 0, 100, 100), 50, 50, 100, 100, name=None),
            PadInfo(1, (0, 0, 100, 100), 50, 50, 100, 100, name=None),
        ]
        sym = create_default_layout(pads, "TEST")
        pin_names = {p.name for p in sym.pins}
        assert "1" in pin_names
        assert "2" in pin_names

    def test_footprint_ref(self):
        pads = self._make_pads(["A"])
        sym = create_default_layout(pads, "TEST", footprint_ref="Lib:Foot")
        assert sym.footprint_ref == "Lib:Foot"

    def test_minimum_body_size(self):
        """Even a single-pin symbol should have a minimum body size"""
        pads = self._make_pads(["A"])
        sym = create_default_layout(pads, "TINY")
        assert sym.body_width >= 5.08
        assert sym.body_height >= 5.08

    def test_position_indices_are_sequential(self):
        pads = self._make_pads(["A", "B", "C", "D", "E", "F"])
        sym = create_default_layout(pads, "TEST")

        for side in PinSide:
            side_pins = sorted(
                [p for p in sym.pins if p.side == side],
                key=lambda p: p.position_index
            )
            for i, pin in enumerate(side_pins):
                assert pin.position_index == i
