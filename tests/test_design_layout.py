# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the per-design file-layout convention (--design-dir output routing)."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import klayout.db as db

from gds_to_kicad import resolve_footprint_output


@pytest.fixture
def interposer_lyp_path():
    lyp_path = Path(__file__).parent.parent / "pdks" / "interposer.lyp"
    if not lyp_path.exists():
        pytest.skip("interposer.lyp not found")
    return str(lyp_path)


def _create_test_gds(path, pad_layer=(134, 0)):
    """Create a minimal GDS with one pad on the given layer."""
    layout = db.Layout()
    layout.dbu = 0.001
    cell = layout.create_cell("LAYOUT_TEST")
    pl = layout.layer(*pad_layer)
    cell.shapes(pl).insert(db.Box(75000, 50000, 125000, 100000))
    layout.write(str(path))
    return path


class TestResolveFootprintOutput:
    def test_explicit_output_wins(self, tmp_path):
        explicit = str(tmp_path / "explicit.kicad_mod")
        design_dir = str(tmp_path / "design")
        out = resolve_footprint_output(explicit, design_dir, "U1")
        assert out == explicit
        # design-dir must be ignored: no .pretty created
        assert not (tmp_path / "design" / "design.pretty").exists()

    def test_design_dir_routes_to_pretty(self, tmp_path):
        design_dir = tmp_path / "my_design"
        out = resolve_footprint_output(None, str(design_dir), "U1")
        expected = design_dir / "my_design.pretty" / "U1.kicad_mod"
        assert out == str(expected)
        # the .pretty library directory is created as a side effect
        assert (design_dir / "my_design.pretty").is_dir()

    def test_default_fallback(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Pin the relative-path fallback independently of the ambient env.
        monkeypatch.delenv("GDS_TO_KICAD_DATA_DIR", raising=False)
        out = resolve_footprint_output(None, None, "chipX")
        # legacy behavior: relative dir under the current working directory
        assert out == str(Path("generated_kicad_footprint_files") / "chipX.kicad_mod")
        assert (tmp_path / "generated_kicad_footprint_files").is_dir()

    def test_data_dir_env_roots_the_fallback(self, tmp_path, monkeypatch):
        pinned = tmp_path / "pinned"
        monkeypatch.setenv("GDS_TO_KICAD_DATA_DIR", str(pinned))
        out = resolve_footprint_output(None, None, "chipX")
        expected = pinned / "generated_kicad_footprint_files" / "chipX.kicad_mod"
        assert out == str(expected)
        assert (pinned / "generated_kicad_footprint_files").is_dir()


class TestDesignDirCLI:
    def test_cli_design_dir(self, tmp_path, interposer_lyp_path):
        gds_path = _create_test_gds(tmp_path / "chip.gds")
        design_dir = tmp_path / "mydesign"

        result = subprocess.run(
            [sys.executable, "gds_to_kicad.py",
             str(gds_path), "--lyp-file", interposer_lyp_path,
             "--layer", "TopMetal2.drawing",
             "--design-dir", str(design_dir)],
            cwd=str(Path(__file__).parent.parent),
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        produced = design_dir / "mydesign.pretty" / "chip.kicad_mod"
        assert produced.is_file()

    def test_cli_output_overrides_design_dir(self, tmp_path, interposer_lyp_path):
        gds_path = _create_test_gds(tmp_path / "chip.gds")
        design_dir = tmp_path / "mydesign"
        explicit = tmp_path / "explicit.kicad_mod"

        result = subprocess.run(
            [sys.executable, "gds_to_kicad.py",
             str(gds_path), "--lyp-file", interposer_lyp_path,
             "--layer", "TopMetal2.drawing",
             "--design-dir", str(design_dir),
             "-o", str(explicit)],
            cwd=str(Path(__file__).parent.parent),
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        assert explicit.is_file()
        assert not (design_dir / "mydesign.pretty").exists()
