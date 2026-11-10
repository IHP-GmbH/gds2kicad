# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for extract_padmap.py cmd_extract, focused on the die_bbox_dbu field.

die_bbox_dbu is the die-local GDS bbox in native database units, emitted so a
pin list can mount a bbox_center die without guessing its size. It must come
from the top-cell outline (not pad extents, which undersize the die), so the
test asserts the emitted value equals the read-back top-cell bbox and that it
covers the full pad span.
"""
import sys
import json
from pathlib import Path
from argparse import Namespace

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

import klayout.db as db

import extract_padmap


def _make_die_gds(path):
    """A minimal labeled die: 4 pads on TopMetal2 (134/0), labels on 134/25."""
    layout = db.Layout()
    top = layout.create_cell("TEST_DIE")
    pad = layout.layer(134, 0)
    txt = layout.layer(134, 25)
    # Pads span (0,0)..(300000,300000) dbu; centres on a 200 um grid.
    top.shapes(pad).insert(db.Box(0, 0, 100000, 100000))
    top.shapes(pad).insert(db.Box(200000, 0, 300000, 100000))
    top.shapes(pad).insert(db.Box(0, 200000, 100000, 300000))
    top.shapes(pad).insert(db.Box(200000, 200000, 300000, 300000))
    top.shapes(txt).insert(db.Text("VDD", db.Trans(db.Point(50000, 50000))))
    top.shapes(txt).insert(db.Text("GND", db.Trans(db.Point(250000, 50000))))
    top.shapes(txt).insert(db.Text("SIG_A", db.Trans(db.Point(50000, 250000))))
    top.shapes(txt).insert(db.Text("SIG_B", db.Trans(db.Point(250000, 250000))))
    layout.write(str(path))


def test_extract_emits_die_bbox_dbu(tmp_path):
    gds = tmp_path / "die.gds"
    _make_die_gds(gds)
    out = tmp_path / "padmap.json"
    args = Namespace(gds=str(gds), pad_layer="134/0", text_layer="134/25",
                     top_cell=None, name=None, output=str(out))
    extract_padmap.cmd_extract(args)

    spec = json.loads(out.read_text(encoding="utf-8"))
    assert "die_bbox_dbu" in spec, "extract must emit die_bbox_dbu"

    # Faithful to the emit path: the die-local GDS bbox in dbu is the top-cell
    # bbox, read back exactly as cmd_extract reads it.
    rl = db.Layout()
    rl.read(str(gds))
    bb = rl.top_cell().bbox()
    assert spec["die_bbox_dbu"] == {
        "x_min": bb.left, "y_min": bb.bottom,
        "x_max": bb.right, "y_max": bb.top,
    }


def test_die_bbox_dbu_from_outline_not_pad_extents(tmp_path):
    # The die outline must cover the full pad span; pad extents alone (which sit
    # inside the die edge) must never be what sizes die_bbox_dbu.
    gds = tmp_path / "die.gds"
    _make_die_gds(gds)
    out = tmp_path / "padmap.json"
    args = Namespace(gds=str(gds), pad_layer="134/0", text_layer="134/25",
                     top_cell=None, name=None, output=str(out))
    extract_padmap.cmd_extract(args)

    spec = json.loads(out.read_text(encoding="utf-8"))
    bbox = spec["die_bbox_dbu"]
    assert bbox["x_min"] <= 0 and bbox["y_min"] <= 0
    assert bbox["x_max"] >= 300000 and bbox["y_max"] >= 300000


def test_die_bbox_dbu_is_integer_dbu(tmp_path):
    # Native database units are integers; a float here means a um value leaked in.
    gds = tmp_path / "die.gds"
    _make_die_gds(gds)
    out = tmp_path / "padmap.json"
    args = Namespace(gds=str(gds), pad_layer="134/0", text_layer="134/25",
                     top_cell=None, name=None, output=str(out))
    extract_padmap.cmd_extract(args)

    spec = json.loads(out.read_text(encoding="utf-8"))
    for k in ("x_min", "y_min", "x_max", "y_max"):
        assert isinstance(spec["die_bbox_dbu"][k], int), f"{k} must be integer dbu"
