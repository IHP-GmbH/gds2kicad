# SPDX-License-Identifier: GPL-3.0-or-later
"""footprint_to_pinlist: a footprint back to a geometry pin list.

The round trip a stand-in die takes: a pad map to a GDS (blackbox_chiplet),
the GDS to a footprint (gds_to_kicad.py), the footprint back to a pins.json.
Every pad must come back with its name, centre and size in the die frame,
flip-chip or not, and the file must carry what interposer-pnr's pin list
reader requires: a version it accepts, dbu_um, and a pad_index per pad.
"""

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from blackbox_chiplet import generate_blackbox_gds, load_canonical_layers  # noqa: E402
import footprint_to_pinlist as f2p  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONV = ROOT / "gds_to_kicad.py"
DBU_UM = 0.001

# Rails repeat their name once per pad, as a real die's do.
SPEC = {
    "chiplet_name": "RAIL_DIE",
    "die": {"width_um": 1000, "height_um": 800},
    "pads": [
        {"name": "VDD", "x_um": -300, "y_um": 300, "w_um": 60, "h_um": 60},
        {"name": "VDD", "x_um": 300, "y_um": -300, "w_um": 60, "h_um": 60},
        {"name": "GND", "x_um": -300, "y_um": -300, "w_um": 60, "h_um": 60},
        {"name": "GND", "x_um": 300, "y_um": 300, "w_um": 60, "h_um": 60},
        {"name": "D0", "x_um": -120.5, "y_um": 0, "w_um": 40, "h_um": 80},
        {"name": "D1", "x_um": 250, "y_um": 10.25, "w_um": 80, "h_um": 40},
    ],
}


def _pads_in_dbu(spec):
    return Counter((p["name"], round(p["x_um"] / DBU_UM), round(p["y_um"] / DBU_UM),
                    round(p["w_um"] / DBU_UM), round(p["h_um"] / DBU_UM))
                   for p in spec["pads"])


def _round_trip(tmp_path, *flags):
    gds = tmp_path / "die.gds"
    generate_blackbox_gds(SPEC, str(gds), load_canonical_layers(), write_manifest=False)
    fp = tmp_path / "die.kicad_mod"
    r = subprocess.run([sys.executable, str(CONV), str(gds), "-o", str(fp), *flags],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    out = tmp_path / "pins.json"
    assert f2p.main([str(fp), "-o", str(out), "--name", "RAIL_DIE"]) == 0
    return json.loads(out.read_text(encoding="utf-8"))


def _got_in_dbu(doc):
    return Counter((p["name"], round(p["center_x_dbu"]), round(p["center_y_dbu"]),
                    round(p["width_dbu"]), round(p["height_dbu"])) for p in doc["pins"])


@pytest.mark.parametrize("flags", [(), ("--flip-chip",)], ids=["face_up", "flip_chip"])
def test_every_pad_comes_back_in_the_die_frame(tmp_path, flags):
    doc = _round_trip(tmp_path, *flags)
    assert _got_in_dbu(doc) == _pads_in_dbu(SPEC)
    # The footprint keeps 1 nm, so nothing drifts by more than float noise.
    for p in doc["pins"]:
        for key in ("center_x_dbu", "center_y_dbu", "width_dbu", "height_dbu"):
            assert abs(p[key] - round(p[key])) < 1e-6, (p["name"], key, p[key])


def test_the_file_is_a_geometry_pin_list(tmp_path):
    """What interposer-pnr's reader refuses a pin list for, checked here."""
    doc = _round_trip(tmp_path)
    assert doc["version"] in (1, "1.0")
    assert doc["dbu_um"] == DBU_UM
    assert doc["source_type"] == "kicad_footprint"
    assert doc["chiplet_name"] == "RAIL_DIE"
    indices = [p["pad_index"] for p in doc["pins"]]
    assert all(type(i) is int for i in indices)
    assert sorted(indices) == list(range(len(SPEC["pads"])))
    for p in doc["pins"]:
        assert isinstance(p["name"], str) and p["name"]


def test_a_rail_keeps_one_pin_per_pad(tmp_path):
    doc = _round_trip(tmp_path)
    names = Counter(p["name"] for p in doc["pins"])
    assert names == Counter({"VDD": 2, "GND": 2, "D0": 1, "D1": 1})
    for rail in ("VDD", "GND"):
        assert len({p["pad_index"] for p in doc["pins"] if p["name"] == rail}) == 2


def test_orientation_is_read_from_the_footprint(tmp_path):
    gds = tmp_path / "die.gds"
    generate_blackbox_gds(SPEC, str(gds), load_canonical_layers(), write_manifest=False)
    for flags, want in (((), "face_up"), (("--flip-chip",), "flip_chip")):
        fp = tmp_path / f"{want}.kicad_mod"
        r = subprocess.run([sys.executable, str(CONV), str(gds), "-o", str(fp), *flags],
                           capture_output=True, text=True, cwd=ROOT)
        assert r.returncode == 0, r.stderr
        assert f2p.read_orientation(str(fp)) == want
