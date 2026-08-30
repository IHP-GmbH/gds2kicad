# SPDX-License-Identifier: GPL-3.0-or-later
"""Emission tests for kicad_project_writer: the three KiCad files are well-formed,
internally consistent, and byte-stable across reruns."""

import json
import os

import klayout.db as db
import pytest

import interposer_model as im
import kicad_project_writer as kpw
import sexpr_io as sx
from interposer_profile import InterposerProfile


def _synth_gds(path):
    """Two bond-pads (one named via a bridged wire, one standalone) + a pillar."""
    ly = db.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("BOARD")
    L = ly.layer(134, 0)
    TX = ly.layer(134, 25)

    def box(cx, cy, w, h):
        top.shapes(L).insert(db.Box(cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2))

    # named net: two 120um bond-pads bridged by a wire, label on the left one
    box(0, 0, 120000, 120000)
    box(400000, 0, 120000, 120000)
    top.shapes(L).insert(db.Path([db.Point(0, 0), db.Point(400000, 0)], 9400))
    top.shapes(TX).insert(db.Text("VDD", db.Trans(db.Point(0, 0))))
    # standalone (not-connect) bond-pad
    box(0, 400000, 120000, 120000)
    # a round pillar (not in schematic)
    pts = []
    import math
    for k in range(64):
        a = 2 * math.pi * k / 64
        pts.append(db.Point(int(800000 + 22500 * math.cos(a)),
                            int(0 + 22500 * math.sin(a))))
    top.shapes(L).insert(db.Polygon(pts))
    ly.write(path)


@pytest.fixture
def model(tmp_path):
    gds = str(tmp_path / "synth.gds")
    _synth_gds(gds)
    return im.build_model(gds, profile=InterposerProfile())


def test_model_shape(model):
    rc = model.role_counts()
    assert rc.get("bond_pad") == 3
    assert rc.get("cu_pillar") == 1
    assert rc.get("wire") == 1
    assert any(n.name == "VDD" for n in model.nets)


def test_pcb_wellformed(model, tmp_path):
    out = str(tmp_path / "out")
    written = kpw.write_project(model, out, emit="pcb")
    assert len(written) == 1
    pcb = sx.parse(open(written[0]).read())
    assert sx.head(pcb) == "kicad_pcb"
    assert str(sx.find(pcb, "version")[1]) == kpw.PCB_VERSION
    # layer stack cloned (F.Cu present as TopMetal2)
    layers = sx.find(pcb, "layers")
    fcu = [row for row in layers[1:] if len(row) >= 2 and str(row[1]) == "F.Cu"]
    assert fcu and str(fcu[0][3]) == "TopMetal2"
    # net table: net 0 + one per model net
    nets = sx.find_all(pcb, "net")
    assert nets[0] == ["net", "0", ""]
    assert len(nets) == 1 + len(model.nets)
    net_names = {str(n[2]) for n in nets[1:]}
    # one footprint per pad; every footprint property has a uuid
    fps = sx.find_all(pcb, "footprint")
    assert len(fps) == len(model.pads)
    for fp in fps:
        for prop in sx.find_all(fp, "property"):
            assert sx.find(prop, "uuid") is not None
        pad = sx.find(fp, "pad")
        net_child = sx.find(pad, "net")
        if net_child is not None:
            assert str(net_child[2]) in net_names


def test_sch_wellformed(model, tmp_path):
    out = str(tmp_path / "out")
    written = kpw.write_project(model, out, emit="sch")
    sch = sx.parse(open(written[0]).read())
    assert sx.head(sch) == "kicad_sch"
    assert str(sx.find(sch, "version")[1]) == kpw.SCH_VERSION
    assert sx.find(sch, "lib_symbols") is not None
    bondpads = [p for p in model.pads if p.role == im.CopperRole.BOND_PAD]
    named = [p for p in bondpads if p.name]
    syms = [s for s in sx.find_all(sch, "symbol") if sx.find(s, "lib_id")]
    assert len(syms) == len(bondpads)
    assert len(sx.find_all(sch, "global_label")) == len(named)
    assert len(sx.find_all(sch, "no_connect")) == len(bondpads) - len(named)
    # a schematic symbol property must NOT carry a uuid
    for s in syms:
        for prop in sx.find_all(s, "property"):
            assert sx.find(prop, "uuid") is None


def test_pro_and_sheet_link(model, tmp_path):
    out = str(tmp_path / "out")
    kpw.write_project(model, out, emit="all")
    base = os.path.join(out, model.name)
    pro = json.loads(open(base + ".kicad_pro").read())
    assert pro["meta"]["version"] == 3
    assert pro["sheets"] and pro["sheets"][0][0]
    # the project's sheet uuid matches the schematic's uuid
    sch = sx.parse(open(base + ".kicad_sch").read())
    assert str(sx.find(sch, "uuid")[1]) == pro["sheets"][0][0]


def test_deterministic(model, tmp_path):
    a = str(tmp_path / "a")
    b = str(tmp_path / "b")
    kpw.write_project(model, a, emit="all")
    kpw.write_project(model, b, emit="all")
    for fn in os.listdir(a):
        assert open(os.path.join(a, fn)).read() == open(os.path.join(b, fn)).read()
