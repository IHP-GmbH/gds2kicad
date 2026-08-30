# SPDX-License-Identifier: GPL-3.0-or-later
"""End-to-end test on the real 240tx stripped interposer GDS.

Skipped when the design is not present (same policy as the other sibling-asset
tests). Asserts the model has the expected structure and that a full project
emits and re-parses; loads it in pcbnew only if pcbnew is importable."""

import os

import pytest

import interposer_model as im
import kicad_project_writer as kpw
import sexpr_io as sx
from interposer_profile import InterposerProfile

STRIPPED = ("/home/montanares/adk-work/heterogenic-designs/240tx/chiplets/"
            "Interposer_ANT_CuPi_stripped.gds")

pytestmark = pytest.mark.skipif(
    not os.path.isfile(STRIPPED),
    reason="240tx stripped GDS not present on this host")


@pytest.fixture(scope="module")
def model():
    return im.build_model(STRIPPED, profile=InterposerProfile())


def test_model_structure(model):
    rc = model.role_counts()
    # A real interposer top metal: many pillars and bond-pads, routing wires.
    assert rc.get("cu_pillar", 0) >= 20
    assert rc.get("bond_pad", 0) >= 20
    assert rc.get("wire", 0) >= 10
    assert len(model.nets) >= 10
    assert any(n.name == "GND" for n in model.nets)
    # Every pad is either named (on a net) or an explicit not-connect.
    assert model.pads
    assert all((p.name and p.is_connected) or (not p.name and not p.is_connected)
               for p in model.pads)


def test_emits_and_reparses(model, tmp_path):
    out = str(tmp_path / "proj")
    written = kpw.write_project(model, out, emit="all")
    assert len(written) == 3
    for path in written:
        assert os.path.getsize(path) > 0
        if path.endswith(".kicad_pcb") or path.endswith(".kicad_sch"):
            node = sx.parse(open(path).read())     # raises on malformed output
            assert node


def test_pcbnew_loads_if_available(model, tmp_path):
    pcbnew = pytest.importorskip("pcbnew")
    out = str(tmp_path / "proj")
    written = kpw.write_project(model, out, emit="pcb")
    board = pcbnew.LoadBoard(written[0])
    assert board is not None
    assert len(list(board.GetFootprints())) == len(model.pads)
