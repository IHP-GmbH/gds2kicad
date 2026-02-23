"""Tests for kicad_sym_writer.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from kicad_sym_writer import (
    PinSide, PinType, SymbolPin, SymbolDefinition, KiCadSymWriter,
    PIN_SPACING, TB_PIN_SPACING, PIN_LENGTH,
)


class TestSymbolPin:
    def test_left_pin_coordinates(self):
        """Single left pin is centered vertically (y=0)"""
        pin = SymbolPin("A", "A", PinSide.LEFT, PinType.PASSIVE,
                         position_index=0, side_pin_count=1)
        x, y = pin.get_coordinates(10.16, 10.16)
        assert x == -(10.16 / 2 + PIN_LENGTH)
        assert y == 0.0  # centered

    def test_right_pin_coordinates(self):
        """Single right pin is centered vertically (y=0)"""
        pin = SymbolPin("B", "B", PinSide.RIGHT, PinType.PASSIVE,
                         position_index=0, side_pin_count=1)
        x, y = pin.get_coordinates(10.16, 10.16)
        assert x == (10.16 / 2 + PIN_LENGTH)
        assert y == 0.0  # centered

    def test_top_pin_coordinates(self):
        """Single top pin is centered horizontally (x=0)"""
        pin = SymbolPin("VDD", "VDD", PinSide.TOP, PinType.POWER_IN,
                         position_index=0, side_pin_count=1)
        x, y = pin.get_coordinates(10.16, 10.16)
        assert x == 0.0  # centered
        assert y == 10.16 / 2 + PIN_LENGTH

    def test_bottom_pin_coordinates(self):
        """Single bottom pin is centered horizontally (x=0)"""
        pin = SymbolPin("GND", "GND", PinSide.BOTTOM, PinType.POWER_IN,
                         position_index=0, side_pin_count=1)
        x, y = pin.get_coordinates(10.16, 10.16)
        assert x == 0.0  # centered
        assert y == -(10.16 / 2 + PIN_LENGTH)

    def test_pin_angle(self):
        assert SymbolPin("A", "A", PinSide.LEFT).angle == 0
        assert SymbolPin("A", "A", PinSide.RIGHT).angle == 180
        assert SymbolPin("A", "A", PinSide.TOP).angle == 270
        assert SymbolPin("A", "A", PinSide.BOTTOM).angle == 90

    def test_pin_spacing_along_side(self):
        """Consecutive pins should be spaced by PIN_SPACING"""
        pin0 = SymbolPin("A", "A", PinSide.LEFT, position_index=0, side_pin_count=2)
        pin1 = SymbolPin("B", "B", PinSide.LEFT, position_index=1, side_pin_count=2)
        _, y0 = pin0.get_coordinates(10.16, 10.16)
        _, y1 = pin1.get_coordinates(10.16, 10.16)
        assert abs(y0 - y1 - PIN_SPACING) < 0.001

    def test_multiple_pins_centered(self):
        """Three left pins should be centered around y=0"""
        pins = [SymbolPin("A", "A", PinSide.LEFT, position_index=i, side_pin_count=3)
                for i in range(3)]
        coords = [p.get_coordinates(10.16, 10.16) for p in pins]
        ys = [c[1] for c in coords]
        # 3 pins: y = +2.54, 0, -2.54
        assert abs(ys[0] - PIN_SPACING) < 0.001
        assert abs(ys[1]) < 0.001
        assert abs(ys[2] + PIN_SPACING) < 0.001

    def test_top_bottom_uses_wider_spacing(self):
        """Top/bottom pins use TB_PIN_SPACING (5.08mm) between pins"""
        pin0 = SymbolPin("V1", "V1", PinSide.TOP, position_index=0, side_pin_count=2)
        pin1 = SymbolPin("V2", "V2", PinSide.TOP, position_index=1, side_pin_count=2)
        x0, _ = pin0.get_coordinates(20.0, 10.0)
        x1, _ = pin1.get_coordinates(20.0, 10.0)
        assert abs(x1 - x0 - TB_PIN_SPACING) < 0.001


class TestSymbolDefinition:
    def test_pin_count_per_side(self):
        sym = SymbolDefinition(
            name="TEST",
            pins=[
                SymbolPin("A", "A", PinSide.LEFT),
                SymbolPin("B", "B", PinSide.LEFT),
                SymbolPin("C", "C", PinSide.RIGHT),
                SymbolPin("VDD", "VDD", PinSide.TOP),
            ]
        )
        counts = sym.pin_count_per_side()
        assert counts[PinSide.LEFT] == 2
        assert counts[PinSide.RIGHT] == 1
        assert counts[PinSide.TOP] == 1
        assert counts[PinSide.BOTTOM] == 0


class TestKiCadSymWriter:
    def test_write_basic_symbol(self, tmp_path):
        """Write a minimal symbol and verify output structure"""
        sym = SymbolDefinition(
            name="TEST_IC",
            pins=[
                SymbolPin("IN", "IN", PinSide.LEFT, PinType.INPUT, 0),
                SymbolPin("OUT", "OUT", PinSide.RIGHT, PinType.OUTPUT, 0),
                SymbolPin("VDD", "VDD", PinSide.TOP, PinType.POWER_IN, 0),
                SymbolPin("GND", "GND", PinSide.BOTTOM, PinType.POWER_IN, 0),
            ],
            body_width=10.16,
            body_height=10.16,
            footprint_ref="MyLib:TestIC",
        )

        output_path = str(tmp_path / "test.kicad_sym")
        writer = KiCadSymWriter()
        writer.write_symbol_library([sym], output_path)

        # Verify file exists
        assert Path(output_path).exists()

        content = Path(output_path).read_text()

        # Check structure
        assert "(kicad_symbol_lib" in content
        assert "(version 20211014)" in content
        assert '(symbol "TEST_IC"' in content

        # Check properties
        assert '(property "Reference" "U"' in content
        assert '(property "Value" "TEST_IC"' in content
        assert '(property "Footprint" "MyLib:TestIC"' in content

        # Check body rectangle
        assert "(rectangle" in content

        # Check all 4 pins
        assert content.count("(pin ") == 4
        assert '(name "IN"' in content
        assert '(name "OUT"' in content
        assert '(name "VDD"' in content
        assert '(name "GND"' in content

        # Check pin types
        assert "(pin input line" in content
        assert "(pin output line" in content
        assert "(pin power_in line" in content

        # Check balanced parentheses
        opens = content.count('(')
        closes = content.count(')')
        assert opens == closes

    def test_write_multiple_symbols(self, tmp_path):
        """Library can contain multiple symbols"""
        syms = [
            SymbolDefinition(name="SYM_A", pins=[
                SymbolPin("A1", "A1", PinSide.LEFT),
            ]),
            SymbolDefinition(name="SYM_B", pins=[
                SymbolPin("B1", "B1", PinSide.RIGHT),
            ]),
        ]

        output_path = str(tmp_path / "multi.kicad_sym")
        writer = KiCadSymWriter()
        writer.write_symbol_library(syms, output_path)

        content = Path(output_path).read_text()
        assert '(symbol "SYM_A"' in content
        assert '(symbol "SYM_B"' in content

    def test_empty_library(self, tmp_path):
        """An empty library should still be valid"""
        output_path = str(tmp_path / "empty.kicad_sym")
        writer = KiCadSymWriter()
        writer.write_symbol_library([], output_path)

        content = Path(output_path).read_text()
        assert "(kicad_symbol_lib" in content
        opens = content.count('(')
        closes = content.count(')')
        assert opens == closes

    def test_sanitize_special_characters(self, tmp_path):
        """Symbol and pin names with special chars should be escaped"""
        sym = SymbolDefinition(
            name='Chip "Special" / Name',
            pins=[
                SymbolPin('Pin "1"', 'Pin "1"', PinSide.LEFT),
            ],
        )

        output_path = str(tmp_path / "special.kicad_sym")
        writer = KiCadSymWriter()
        writer.write_symbol_library([sym], output_path)

        content = Path(output_path).read_text()
        # No unescaped double quotes inside S-expression strings
        assert 'Chip \'Special\' / Name' in content
