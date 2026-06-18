# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the black-box chiplet generator and its round-trip through the
footprint converter.
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import klayout.db as db

from blackbox_chiplet import (_adk_root, generate_blackbox_gds,
                              load_canonical_layers, load_spec)
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
    assert "exchange0" not in L  # boundary lives in the manifest, not a fab layer


def test_generate_stamps_canonical_layers(tmp_path):
    gds = tmp_path / "acme.gds"
    n = generate_blackbox_gds(SPEC, str(gds), load_canonical_layers())
    assert n == 4
    assert _count_on(gds, 205, 0) == 4    # pads
    assert _count_on(gds, 205, 25) == 4   # pad-name labels
    assert _count_on(gds, 206, 0) == 1    # die outline
    assert _count_on(gds, 190, 0) == 0    # never stamps the exchange0 fab layer


def test_generate_writes_boundary_manifest(tmp_path):
    """The die outline is exported as the chiplet boundary in a sidecar
    manifest (not a fab layer), with die-local DBU coordinates."""
    gds = tmp_path / "acme.gds"
    generate_blackbox_gds(SPEC, str(gds), load_canonical_layers())
    manifest = json.loads((tmp_path / "acme.boundaries.json").read_text())
    assert manifest["schema"] == "adk-boundary-manifest"
    assert len(manifest["boundaries"]) == 1
    b = manifest["boundaries"][0]
    assert b["source_die"] == "ACME_PHY"
    # 1000x800 um die centered at origin -> [-500,-400]..[500,400] um in DBU.
    xs = [p[0] for p in b["polygon_dbu"]]
    ys = [p[1] for p in b["polygon_dbu"]]
    assert min(xs) == -500000 and max(xs) == 500000
    assert min(ys) == -400000 and max(ys) == 400000


def test_blackbox_manifest_version_pinned(tmp_path):
    """The sidecar must carry the exact schema id and version the adk readers
    exact-match (adk/docs/boundary_manifest.md), plus every field the schema
    marks required -- bumping the producer without the readers (or dropping a
    field) breaks assembly DRC loudly downstream."""
    gds = tmp_path / "acme.gds"
    generate_blackbox_gds(SPEC, str(gds), load_canonical_layers())
    m = json.loads((tmp_path / "acme.boundaries.json").read_text())
    assert m["schema"] == "adk-boundary-manifest"
    assert m["version"] == "1.0.0"
    assert m["generator"] == "blackbox_chiplet.py"
    for field in ("assembly_gds", "dbu_um", "top_cell", "boundaries"):
        assert field in m, f"required manifest field missing: {field}"
    b = m["boundaries"][0]
    for field in ("instance", "source_die", "class", "polygon_dbu"):
        assert field in b, f"required boundary field missing: {field}"


def test_no_manifest_flag_suppresses_sidecar(tmp_path):
    gds = tmp_path / "acme.gds"
    generate_blackbox_gds(SPEC, str(gds), load_canonical_layers(),
                          write_manifest=False)
    assert not (tmp_path / "acme.boundaries.json").exists()
    assert _count_on(gds, 190, 0) == 0


# --- ADK discovery (ecosystem convention: env -> sibling walk) ---------------

def _fake_adk(parent, dirname):
    root = parent / dirname
    (root / "config").mkdir(parents=True)
    (root / "config" / "chiplet_pads.json").write_text("{}")
    return root


def test_adk_root_walk_accepts_repo_alias(tmp_path, monkeypatch):
    """A sibling checkout under the GitHub repository name (ADK) must resolve,
    not only the canonical ecosystem dirname (adk)."""
    monkeypatch.delenv("ADK_ROOT", raising=False)
    alias = _fake_adk(tmp_path, "ADK")
    start = tmp_path / "tools" / "gds-to-kicad" / "blackbox_chiplet.py"
    start.parent.mkdir(parents=True)
    assert _adk_root(start=start) == alias


def test_adk_root_walk_prefers_canonical_over_alias(tmp_path, monkeypatch):
    monkeypatch.delenv("ADK_ROOT", raising=False)
    canonical = _fake_adk(tmp_path, "adk")
    _fake_adk(tmp_path, "ADK")
    start = tmp_path / "gds_to_kicad" / "blackbox_chiplet.py"
    start.parent.mkdir(parents=True)
    assert _adk_root(start=start) == canonical


def test_adk_root_invalid_env_falls_through_to_walk(tmp_path, monkeypatch):
    """A set-but-invalid $ADK_ROOT (marker file missing) must fall through to
    the sibling walk instead of being returned blindly."""
    monkeypatch.setenv("ADK_ROOT", str(tmp_path / "nonexistent"))
    canonical = _fake_adk(tmp_path, "adk")
    start = tmp_path / "gds_to_kicad" / "blackbox_chiplet.py"
    start.parent.mkdir(parents=True)
    assert _adk_root(start=start) == canonical


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
