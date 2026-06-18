# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for io_pads/generate_io_pad.py and io_pads/kicad_pcb_to_iopads.py."""
import json
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
IOPADS_DIR = PROJECT_DIR / "io_pads"
GEN_SCRIPT = IOPADS_DIR / "generate_io_pad.py"
EXTRACT_SCRIPT = IOPADS_DIR / "kicad_pcb_to_iopads.py"


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def test_generate_wire_bond_100x100(tmp_path):
    out_dir = tmp_path / "io_pads"
    res = _run([sys.executable, str(GEN_SCRIPT),
                "--io-class", "wire_bond", "--size", "100x100",
                "--out-dir", str(out_dir)])
    assert res.returncode == 0, res.stderr

    sym = out_dir / "kicad_symbols" / "IOPad_WireBond_100x100.kicad_sym"
    fp = out_dir / "kicad_footprints" / "IOPad_WireBond_100x100.kicad_mod"
    assert sym.exists()
    assert fp.exists()

    sym_text = sym.read_text()
    assert '(symbol "IOPad_WireBond_100x100"' in sym_text
    assert sym_text.count("(pin passive line") == 1

    fp_text = fp.read_text()
    assert '(property "IO_CLASS" "wire_bond")' in fp_text
    assert '(property "IO_PAD_SIZE_UM" "100x100")' in fp_text
    assert '(pad "1" smd rect (at 0 0)' in fp_text
    assert "(size 0.100000 0.100000)" in fp_text


def test_generate_wire_bond_square_shorthand(tmp_path):
    out_dir = tmp_path / "io_pads"
    res = _run([sys.executable, str(GEN_SCRIPT),
                "--io-class", "wire_bond", "--size", "150",
                "--out-dir", str(out_dir)])
    assert res.returncode == 0, res.stderr
    fp = out_dir / "kicad_footprints" / "IOPad_WireBond_150x150.kicad_mod"
    assert fp.exists()
    assert '(property "IO_PAD_SIZE_UM" "150x150")' in fp.read_text()


def test_generate_reserved_class_rejected(tmp_path):
    res = _run([sys.executable, str(GEN_SCRIPT),
                "--io-class", "flipped_bump", "--size", "80x80",
                "--out-dir", str(tmp_path)])
    assert res.returncode != 0
    assert "reserved" in res.stderr.lower()


def test_extract_io_pads_from_pcb_fixture(tmp_path):
    pcb = tmp_path / "mini.kicad_pcb"
    pcb.write_text("""
(kicad_pcb
  (footprint "io_pads:IOPad_WireBond_100x100"
    (layer "F.Cu")
    (at 5.0 -3.5 0)
    (property "Reference" "J1")
    (property "IO_CLASS" "wire_bond")
    (property "IO_PAD_SIZE_UM" "100x100")
    (fp_text reference "J1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.1 0.1) (layers "F.Cu" "F.Mask")
      (net 7 "VDD_EXT")
    )
  )
  (footprint "Connectors:RegularConnector"
    (layer "F.Cu")
    (at 1.0 1.0 0)
    (property "Reference" "X1")
    (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu"))
  )
  (footprint "io_pads:IOPad_WireBond_150x150"
    (layer "F.Cu")
    (at -2.0 4.0 0)
    (fp_text reference "J2" (at 0 0) (layer "F.SilkS"))
    (property "IO_CLASS" "wire_bond")
    (property "IO_PAD_SIZE_UM" "150x150")
    (pad "1" smd rect (at 0 0) (size 0.15 0.15) (layers "F.Cu" "F.Mask")
      (net 8 "OUT0")
    )
  )
)
""")
    out = tmp_path / "io_pads.json"
    res = _run([sys.executable, str(EXTRACT_SCRIPT), str(pcb), "-o", str(out)])
    assert res.returncode == 0, res.stderr

    data = json.loads(out.read_text())
    pads = data["io_pads"]
    assert len(pads) == 2

    by_ref = {p["ref"]: p for p in pads}
    j1 = by_ref["J1"]
    assert j1["io_class"] == "wire_bond"
    assert j1["x_um"] == 5000.0
    assert j1["y_um"] == 3500.0  # KiCad Y-down -3.5 mm -> GDS Y-up +3500 um
    assert j1["size_x_um"] == 100.0
    assert j1["size_y_um"] == 100.0
    assert j1["net"] == "VDD_EXT"
    assert j1["layer"] == "F.Cu"

    j2 = by_ref["J2"]
    assert j2["x_um"] == -2000.0
    assert j2["y_um"] == -4000.0
    assert j2["size_x_um"] == 150.0
    assert j2["net"] == "OUT0"


def test_extract_skips_pcb_without_io_pads(tmp_path):
    pcb = tmp_path / "no_io.kicad_pcb"
    pcb.write_text('(kicad_pcb (footprint "x:y" (layer "F.Cu") (at 0 0 0)))')
    out = tmp_path / "io_pads.json"
    res = _run([sys.executable, str(EXTRACT_SCRIPT), str(pcb), "-o", str(out)])
    assert res.returncode == 0
    data = json.loads(out.read_text())
    assert data["io_pads"] == []
