"""Tests for dynamic DBU detection in gds_to_kicad.py"""

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


def _create_gds_with_dbu(path, dbu_um=0.001, pad_layer=(134, 0)):
    """Create a GDS file with a specific dbu value.

    Args:
        path: Output path
        dbu_um: Database unit in microns (default 0.001 = 1nm)
        pad_layer: Layer tuple
    """
    layout = db.Layout()
    layout.dbu = dbu_um
    cell = layout.create_cell("DBU_TEST")

    pl = layout.layer(*pad_layer)

    # Create a pad that is 100um x 100um regardless of dbu.
    # In DBU: size = 100um / dbu_um
    size_dbu = int(100.0 / dbu_um)
    cell.shapes(pl).insert(db.Box(0, 0, size_dbu, size_dbu))

    # Second pad offset
    offset_dbu = int(200.0 / dbu_um)
    cell.shapes(pl).insert(db.Box(offset_dbu, 0, offset_dbu + size_dbu, size_dbu))

    layout.write(str(path))
    return path


class TestDBUDetection:
    def test_default_dbu(self, interposer_lyp):
        """Default dbu from KLayout is 0.001 um."""
        layout = db.Layout()
        assert layout.dbu == 0.001

        converter = GDSToKiCad(interposer_lyp, "TopMetal2.drawing")
        dbu_to_mm = converter._get_dbu_to_mm(layout)
        assert dbu_to_mm == pytest.approx(1e-6)

    def test_custom_dbu_gds(self, tmp_path, interposer_lyp):
        """GDS with non-default dbu produces correct footprint coordinates."""
        # sky130 uses dbu=0.005 (5nm)
        gds_path = _create_gds_with_dbu(tmp_path / "sky130.gds", dbu_um=0.005)
        output = str(tmp_path / "sky130.kicad_mod")

        converter = GDSToKiCad(interposer_lyp, "TopMetal2.drawing")
        converter.convert(str(gds_path), output)

        content = Path(output).read_text()

        # 100um pad = 0.1mm, center at 50um = 0.05mm
        assert "0.050000" in content  # center_x of first pad
        assert "0.100000" in content  # width/height of pad

    def test_explicit_dbu_override(self, tmp_path, interposer_lyp):
        """Explicit --dbu override takes precedence over layout.dbu."""
        # Create GDS with default dbu (0.001)
        gds_path = _create_gds_with_dbu(tmp_path / "override.gds", dbu_um=0.001)
        output = str(tmp_path / "override.kicad_mod")

        # Override dbu to 0.005 -- coordinates will be 5x larger
        converter = GDSToKiCad(interposer_lyp, "TopMetal2.drawing", dbu=0.005)
        converter.convert(str(gds_path), output)

        content = Path(output).read_text()
        # With override, 100000 DBU * 0.005um * 1e-3 = 0.5mm
        assert "0.500000" in content

    def test_resolve_dbu_prefers_override(self, interposer_lyp):
        """_resolve_dbu returns override when set."""
        converter = GDSToKiCad(interposer_lyp, "TopMetal2.drawing", dbu=0.01)
        layout = db.Layout()
        layout.dbu = 0.001  # layout says 1nm
        assert converter._resolve_dbu(layout) == 0.01

    def test_resolve_dbu_reads_layout(self, interposer_lyp):
        """_resolve_dbu reads layout.dbu when no override."""
        converter = GDSToKiCad(interposer_lyp, "TopMetal2.drawing")
        layout = db.Layout()
        layout.dbu = 0.005
        assert converter._resolve_dbu(layout) == 0.005

    def test_bounding_box_uses_detected_dbu(self, tmp_path, interposer_lyp, capsys):
        """Bounding box display uses detected dbu, not hardcoded."""
        gds_path = _create_gds_with_dbu(tmp_path / "bbox.gds", dbu_um=0.005)
        output = str(tmp_path / "bbox.kicad_mod")

        converter = GDSToKiCad(interposer_lyp, "TopMetal2.drawing")
        converter.convert(str(gds_path), output)

        captured = capsys.readouterr()
        # Should show "DBU: 0.005 um"
        assert "DBU: 0.005 um" in captured.out
        # Bounding box should show 100um pad size
        assert "100.000" in captured.out
