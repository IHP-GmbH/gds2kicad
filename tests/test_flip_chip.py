"""Tests for flip-chip mirror-X footprint generation."""

import re
import subprocess
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
    """Create a GDS with two pads at known positive-X positions."""
    layout = db.Layout()
    layout.dbu = 0.001  # 1 nm
    cell = layout.create_cell("FLIP_TEST")
    pl = layout.layer(*pad_layer)

    # Pad A: 50x50 um at center (100, 75)
    size = 50000  # 50 um in dbu
    cell.shapes(pl).insert(db.Box(75000, 50000, 125000, 100000))

    # Pad B: 50x50 um at center (300, 75)
    cell.shapes(pl).insert(db.Box(275000, 50000, 325000, 100000))

    layout.write(str(path))
    return path


def _parse_pad_centers(content: str):
    """Extract (x, y) centers from smd pad lines in a .kicad_mod."""
    pattern = r'\(pad\s+"[^"]*"\s+smd\s+\w+\s+\(at\s+([-\d.]+)\s+([-\d.]+)\)'
    return [(float(m.group(1)), float(m.group(2))) for m in re.finditer(pattern, content)]


def _parse_courtyard(content: str):
    """Extract fp_rect start/end from .kicad_mod."""
    pattern = r'\(fp_rect\s+\(start\s+([-\d.]+)\s+([-\d.]+)\)\s+\(end\s+([-\d.]+)\s+([-\d.]+)\)'
    m = re.search(pattern, content)
    if m:
        return tuple(float(m.group(i)) for i in range(1, 5))
    return None


class TestFlipChipFootprint:
    def test_face_up_x_positive(self, tmp_path, interposer_lyp):
        gds_path = _create_test_gds(tmp_path / "test.gds")
        output = str(tmp_path / "face_up.kicad_mod")

        converter = GDSToKiCad(interposer_lyp, "TopMetal2.drawing")
        converter.convert(str(gds_path), output, flip_chip=False)

        centers = _parse_pad_centers(Path(output).read_text())
        assert len(centers) == 2
        assert all(x > 0 for x, _ in centers)

    def test_flip_chip_negates_x(self, tmp_path, interposer_lyp):
        gds_path = _create_test_gds(tmp_path / "test.gds")
        out_up = str(tmp_path / "face_up.kicad_mod")
        out_flip = str(tmp_path / "flip.kicad_mod")

        converter = GDSToKiCad(interposer_lyp, "TopMetal2.drawing")
        converter.convert(str(gds_path), out_up, flip_chip=False)
        converter.convert(str(gds_path), out_flip, flip_chip=True)

        centers_up = _parse_pad_centers(Path(out_up).read_text())
        centers_flip = _parse_pad_centers(Path(out_flip).read_text())

        assert len(centers_up) == len(centers_flip) == 2
        for (xu, yu), (xf, yf) in zip(
            sorted(centers_up, key=lambda c: abs(c[0])),
            sorted(centers_flip, key=lambda c: abs(c[0])),
        ):
            assert xf == pytest.approx(-xu, abs=1e-5)
            assert yf == pytest.approx(yu, abs=1e-5)

    def test_flip_chip_courtyard_bounds(self, tmp_path, interposer_lyp):
        gds_path = _create_test_gds(tmp_path / "test.gds")
        out_up = str(tmp_path / "face_up.kicad_mod")
        out_flip = str(tmp_path / "flip.kicad_mod")

        converter = GDSToKiCad(interposer_lyp, "TopMetal2.drawing")
        converter.convert(str(gds_path), out_up, flip_chip=False)
        converter.convert(str(gds_path), out_flip, flip_chip=True)

        crt_up = _parse_courtyard(Path(out_up).read_text())
        crt_flip = _parse_courtyard(Path(out_flip).read_text())
        assert crt_up is not None and crt_flip is not None

        # After mirror: min_x_flip == -max_x_up, max_x_flip == -min_x_up
        assert crt_flip[0] == pytest.approx(-crt_up[2], abs=1e-5)
        assert crt_flip[2] == pytest.approx(-crt_up[0], abs=1e-5)
        # Y unchanged
        assert crt_flip[1] == pytest.approx(crt_up[1], abs=1e-5)
        assert crt_flip[3] == pytest.approx(crt_up[3], abs=1e-5)

    def test_orientation_property_face_up(self, tmp_path, interposer_lyp):
        gds_path = _create_test_gds(tmp_path / "test.gds")
        output = str(tmp_path / "face_up.kicad_mod")

        converter = GDSToKiCad(interposer_lyp, "TopMetal2.drawing")
        converter.convert(str(gds_path), output, flip_chip=False)

        content = Path(output).read_text()
        assert '(property "ORIENTATION" "face_up")' in content

    def test_orientation_property_flip_chip(self, tmp_path, interposer_lyp):
        gds_path = _create_test_gds(tmp_path / "test.gds")
        output = str(tmp_path / "flip.kicad_mod")

        converter = GDSToKiCad(interposer_lyp, "TopMetal2.drawing")
        converter.convert(str(gds_path), output, flip_chip=True)

        content = Path(output).read_text()
        assert '(property "ORIENTATION" "flip_chip")' in content


class TestFlipChipCLI:
    def test_cli_flip_chip_flag(self, tmp_path, interposer_lyp):
        gds_path = _create_test_gds(tmp_path / "test.gds")
        output = str(tmp_path / "cli_flip.kicad_mod")
        lyp = str(Path(__file__).parent.parent / "pdks" / "interposer.lyp")

        result = subprocess.run(
            [sys.executable, "gds_to_kicad.py",
             str(gds_path), "--lyp-file", lyp,
             "--layer", "TopMetal2.drawing",
             "--flip-chip", "-o", output],
            cwd=str(Path(__file__).parent.parent),
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        content = Path(output).read_text()
        assert '(property "ORIENTATION" "flip_chip")' in content

    def test_cli_default_face_up(self, tmp_path, interposer_lyp):
        gds_path = _create_test_gds(tmp_path / "test.gds")
        output = str(tmp_path / "cli_up.kicad_mod")
        lyp = str(Path(__file__).parent.parent / "pdks" / "interposer.lyp")

        result = subprocess.run(
            [sys.executable, "gds_to_kicad.py",
             str(gds_path), "--lyp-file", lyp,
             "--layer", "TopMetal2.drawing",
             "-o", output],
            cwd=str(Path(__file__).parent.parent),
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        content = Path(output).read_text()
        assert '(property "ORIENTATION" "face_up")' in content
