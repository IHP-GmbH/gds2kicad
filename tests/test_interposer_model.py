# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for interposer_model.py

A small synthetic interposer GDS (two cu-pillars, a bond-pad with a net label, a
wire path) is built in-test at dbu=0.001 and parsed with build_model.
"""

import math
import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import klayout.db as db

from interposer_model import build_model, InterposerModel, CopperRole


def round_poly(cx, cy, r, n):
    pts = [db.Point(int(round(cx + r * math.cos(2 * math.pi * i / n))),
                    int(round(cy + r * math.sin(2 * math.pi * i / n))))
           for i in range(n)]
    return db.Polygon(pts)


@pytest.fixture
def model_gds(tmp_path):
    """Two pillars, one labelled bond-pad, one wire path."""
    layout = db.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TEST_IP")
    pad = layout.layer(134, 0)
    lbl = layout.layer(134, 25)

    # Bond-pad with a net label.
    top.shapes(pad).insert(db.Box(0, 0, 120000, 120000))
    top.shapes(lbl).insert(db.Text("NET1", db.Trans(db.Point(60000, 60000))))
    # Two cu-pillars (45 um round, 64 hull vertices), unlabelled.
    top.shapes(pad).insert(round_poly(300000, 300000, 22500, 64))
    top.shapes(pad).insert(round_poly(500000, 300000, 22500, 64))
    # A wire (standalone path).
    top.shapes(pad).insert(
        db.Path([db.Point(100000, 500000), db.Point(300000, 500000)], 5000))

    path = str(tmp_path / "model.gds")
    layout.write(path)
    return path


@pytest.fixture
def model(model_gds):
    return build_model(model_gds)


class TestBuildModel:
    def test_role_counts_and_pads(self, model):
        assert model.role_counts() == {"cu_pillar": 2, "bond_pad": 1, "wire": 1}
        # Pads are the pad-role shapes: 2 pillars + 1 bond-pad.
        assert len(model.pads) == 3

    def test_cell_name(self, model):
        assert model.name == "TEST_IP"


class TestPadDict:
    def test_pad_dict_keys(self, model):
        expected = {"index", "name", "center_x", "center_y", "width", "height",
                    "bbox", "is_polygon", "polygon_points"}
        for pad in model.pads:
            assert set(pad.to_pad_dict().keys()) == expected

    def test_connected_pad(self, model):
        bond = next(p for p in model.pads if p.role == CopperRole.BOND_PAD)
        assert bond.name == "NET1"
        assert bond.is_connected is True
        assert bond.to_pad_dict()["name"] == "NET1"

    def test_not_connect_pad(self, model):
        pillar = next(p for p in model.pads if p.role == CopperRole.CU_PILLAR)
        assert pillar.is_connected is False
        assert pillar.net_id == -1
        # A not-connect pad reports name None in the shared pad dict.
        assert pillar.to_pad_dict()["name"] is None


class TestJsonRoundtrip:
    def test_roundtrip(self, model):
        reparsed = InterposerModel.from_json(model.to_json())
        assert reparsed.role_counts() == model.role_counts()
        assert len(reparsed.pads) == len(model.pads)
        assert len(reparsed.nets) == len(model.nets)
        assert reparsed.outline.points_dbu == model.outline.points_dbu


class TestSummary:
    def test_summary_mentions_cell(self, model):
        s = model.summary()
        assert isinstance(s, str) and s
        assert "TEST_IP" in s
