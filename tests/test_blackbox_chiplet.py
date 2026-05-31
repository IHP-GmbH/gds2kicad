"""Tests for the black-box chiplet generator and its round-trip through the
footprint converter.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import klayout.db as db

from blackbox_chiplet import generate_blackbox_gds, load_canonical_layers, load_spec
from lyp_parser import LYPParser
from pin_extractor import PinExtractor

CONV = Path(__file__).parent.parent / "gds_to_kicad.py"
GENERIC_LYP = Path(__file__).parent.parent / "pdks" / "generic.lyp"

SPEC = {
    "chiplet_name": "ACME_PHY",
    "die": {"width_um": 1000, "height_um": 800},
    "pads": [
        {"name": "VDD", "x_um": -300, "y_um": 200, "w_um": 60, "h_um": 60},
        {"name": "GND", "x_um": -100, "y_um": 200, "w_um": 60, "h_um": 60},
        {"name": "CLK", "x_um": 100, "y_um": 200, "w_um": 60, "h_um": 60},
        {"name": "DAT", "x_um": 300, "y_um": 200, "w_um": 60, "h_um": 60},
    ],
}


def _count_on(gds, layer, datatype):
    ly = db.Layout()
    ly.read(str(gds))
    top = ly.top_cell()
    n = 0
    for li in ly.layer_indices():
        info = ly.get_info(li)
        if info.layer == layer and info.datatype == datatype:
            n += sum(1 for _ in top.shapes(li).each())
    return n


def test_canonical_layers_from_adk():
    L = load_canonical_layers()
    assert L["pad_drawing"] == (205, 0)
    assert L["pad_text"] == (205, 25)
    assert L["outline"] == (206, 0)
    assert L["exchange0"] == (190, 0)


def test_generate_stamps_canonical_layers(tmp_path):
    gds = tmp_path / "acme.gds"
    n = generate_blackbox_gds(SPEC, str(gds), load_canonical_layers())
    assert n == 4
    assert _count_on(gds, 205, 0) == 4    # pads
    assert _count_on(gds, 205, 25) == 4   # pad-name labels
    assert _count_on(gds, 206, 0) == 1    # die outline
    assert _count_on(gds, 190, 0) == 1    # exchange0 mirror


def test_generate_no_exchange0(tmp_path):
    gds = tmp_path / "acme.gds"
    generate_blackbox_gds(SPEC, str(gds), load_canonical_layers(),
                          stamp_exchange0=False)
    assert _count_on(gds, 190, 0) == 0
    assert _count_on(gds, 206, 0) == 1


def test_csv_spec(tmp_path):
    csvp = tmp_path / "pads.csv"
    csvp.write_text("name,x_um,y_um,w_um,h_um\nA,0,0,50,50\nB,100,0,50,50\n")
    spec = load_spec(str(csvp))
    assert spec["chiplet_name"] == "pads"
    assert len(spec["pads"]) == 2
    gds = tmp_path / "pads.gds"
    assert generate_blackbox_gds(spec, str(gds), load_canonical_layers()) == 2
    assert _count_on(gds, 205, 0) == 2


def test_roundtrip_through_converter(tmp_path):
    """The generated GDS must auto-detect cleanly through the bare converter
    and reproduce the pad names -- the core black-box invariant."""
    gds = tmp_path / "acme.gds"
    generate_blackbox_gds(SPEC, str(gds), load_canonical_layers())

    # Auto-detect points at the canonical pad/text layers.
    scan = PinExtractor.scan_gds_layers(str(gds), LYPParser(str(GENERIC_LYP)))
    assert scan["suggested_pad_layer"] == "pad.drawing"
    assert "pad.text" in scan["suggested_text_layers"]

    # Bare converter (default generic lyp + auto-detect) -> named footprint.
    out = tmp_path / "acme.kicad_mod"
    r = subprocess.run([sys.executable, str(CONV), str(gds), "-o", str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    txt = out.read_text()
    assert txt.count("(pad ") == 4
    for name in ("VDD", "GND", "CLK", "DAT"):
        assert f'"{name}"' in txt
