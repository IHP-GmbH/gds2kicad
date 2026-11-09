# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the DIE_THICKNESS_UM footprint property.

The die thickness is assembly metadata, not GDS geometry, so gds_to_kicad only
writes the property when it is told a value (--die-thickness-um). chiplet-export
reads DIE_THICKNESS_UM into hyp-to-gds --die-thicknesses; when the property is
absent the export keeps its 0.0 default, so an omitted thickness must leave the
property out entirely rather than emit a zero.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import klayout.db as db

from lyp_parser import LYPParser
from gds_to_kicad import GDSToKiCad


@pytest.fixture
def interposer_lyp():
    lyp_path = Path(__file__).parent.parent / "pdks" / "interposer.lyp"
    if not lyp_path.exists():
        pytest.skip("interposer.lyp not found")
    return LYPParser(str(lyp_path))


def _create_test_gds(path, pad_layer=(134, 0)):
    """A GDS with one 50x50 um pad, enough to emit a footprint."""
    layout = db.Layout()
    layout.dbu = 0.001  # 1 nm
    cell = layout.create_cell("THICK_TEST")
    pl = layout.layer(*pad_layer)
    cell.shapes(pl).insert(db.Box(75000, 50000, 125000, 100000))
    layout.write(str(path))
    return path


def _thickness_property(content: str):
    """Return the DIE_THICKNESS_UM property value, or None if absent."""
    m = re.search(r'\(property\s+"DIE_THICKNESS_UM"\s+"([^"]*)"\)', content)
    return m.group(1) if m else None


class TestDieThicknessProperty:
    def test_written_when_supplied(self, tmp_path, interposer_lyp):
        gds_path = _create_test_gds(tmp_path / "t.gds")
        output = str(tmp_path / "thick.kicad_mod")

        converter = GDSToKiCad(interposer_lyp, "TopMetal2.drawing")
        converter.convert(str(gds_path), output, die_thickness_um=725.0)

        assert _thickness_property(Path(output).read_text()) == "725"

    def test_absent_by_default(self, tmp_path, interposer_lyp):
        gds_path = _create_test_gds(tmp_path / "t.gds")
        output = str(tmp_path / "nothick.kicad_mod")

        converter = GDSToKiCad(interposer_lyp, "TopMetal2.drawing")
        converter.convert(str(gds_path), output)

        # Absent, not a zero: chiplet-export degrades a missing field to 0.0.
        assert _thickness_property(Path(output).read_text()) is None

    def test_fractional_value_preserved(self, tmp_path, interposer_lyp):
        gds_path = _create_test_gds(tmp_path / "t.gds")
        output = str(tmp_path / "frac.kicad_mod")

        converter = GDSToKiCad(interposer_lyp, "TopMetal2.drawing")
        converter.convert(str(gds_path), output, die_thickness_um=112.5)

        assert _thickness_property(Path(output).read_text()) == "112.5"

    def test_gate_colocated_with_gds_file(self, tmp_path, interposer_lyp):
        # chiplet-export only honors DIE_THICKNESS_UM on a footprint that also
        # carries GDS_FILE (its "die footprint" gate); a property on a footprint
        # without GDS_FILE is silently ignored. The legacy writer emits both in
        # the one die footprint, so the property is never stranded.
        gds_path = _create_test_gds(tmp_path / "t.gds")
        output = str(tmp_path / "gate.kicad_mod")

        converter = GDSToKiCad(interposer_lyp, "TopMetal2.drawing")
        converter.convert(str(gds_path), output, die_thickness_um=200.0)

        content = Path(output).read_text()
        assert '(property "DIE_THICKNESS_UM"' in content
        assert '(property "GDS_FILE"' in content

    def test_value_has_no_unit_suffix(self, tmp_path, interposer_lyp):
        # The reader is a raw float(text): "150" / "150.0" parse, "150um" does
        # not. Guard that the emitted value is bare-numeric, never suffixed.
        gds_path = _create_test_gds(tmp_path / "t.gds")
        output = str(tmp_path / "nosuffix.kicad_mod")

        converter = GDSToKiCad(interposer_lyp, "TopMetal2.drawing")
        converter.convert(str(gds_path), output, die_thickness_um=150.0)

        value = _thickness_property(Path(output).read_text())
        assert value is not None
        float(value)  # must parse as the reader does; raises if suffixed

    @pytest.mark.parametrize("bad", [0, 0.0, -1.0, -725])
    def test_nonpositive_raises(self, tmp_path, interposer_lyp, bad):
        gds_path = _create_test_gds(tmp_path / "t.gds")
        output = str(tmp_path / "bad.kicad_mod")

        converter = GDSToKiCad(interposer_lyp, "TopMetal2.drawing")
        with pytest.raises(ValueError, match="positive"):
            converter.convert(str(gds_path), output, die_thickness_um=bad)
