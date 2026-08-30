# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for net_extractor.py

Real KLayout layouts are built in-test (dbu=0.001), copper on the pad layer
(134/0) and net labels on the label layer (134/25). The CopperShape spine is built
the same way build_model does (interposer_model._shape_records), then extract_nets
is exercised directly; pad naming is checked end to end through build_model.
"""

import math
import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

import klayout.db as db

import interposer_model
import net_extractor
from interposer_model import build_model, CopperRole
from interposer_profile import InterposerProfile
from pin_extractor import single_top_cell


def round_poly(cx, cy, r, n):
    pts = [db.Point(int(round(cx + r * math.cos(2 * math.pi * i / n))),
                    int(round(cy + r * math.sin(2 * math.pi * i / n))))
           for i in range(n)]
    return db.Polygon(pts)


def write_gds(tmp_path, name, build):
    """Run ``build(layout, top)`` on a fresh dbu=0.001 layout and write a GDS."""
    layout = db.Layout()
    layout.dbu = 0.001
    top = layout.create_cell(name)
    build(layout, top)
    path = str(tmp_path / f"{name}.gds")
    layout.write(path)
    return path


def extract(path, profile=None):
    """Load a GDS and run net_extractor.extract_nets on its copper spine."""
    profile = profile or InterposerProfile()
    layout = db.Layout()
    layout.read(path)
    top = single_top_cell(layout, path)
    top.flatten(-1, True)
    copper = interposer_model._shape_records(top, layout.layer(*profile.pad_layer))
    names, isolated, warnings = net_extractor.extract_nets(
        layout, top, copper, profile)
    return copper, names, isolated, warnings


class TestSharedNet:
    def test_bridged_boxes_share_named_net(self, tmp_path):
        def build(layout, top):
            pad = layout.layer(134, 0)
            lbl = layout.layer(134, 25)
            top.shapes(pad).insert(db.Box(0, 0, 120000, 120000))
            top.shapes(pad).insert(db.Box(300000, 0, 420000, 120000))
            # Path touching both boxes bridges them into one geometric net.
            top.shapes(pad).insert(
                db.Path([db.Point(60000, 60000), db.Point(360000, 60000)], 5000))
            top.shapes(lbl).insert(db.Text("SIG", db.Trans(db.Point(60000, 60000))))
        path = write_gds(tmp_path, "bridge", build)

        copper, names, isolated, warnings = extract(path)
        # All three shapes probe onto the same "SIG" net.
        assert set(names.values()) == {"SIG"}

        model = build_model(path)
        sig = [n for n in model.nets if n.name == "SIG"]
        assert len(sig) == 1
        assert len(sig[0].shape_indices) == 3
        # The far bond-pad inherits the name from the shared net.
        bond_pads = [p for p in model.pads if p.role == CopperRole.BOND_PAD]
        assert len(bond_pads) == 2
        assert all(p.name == "SIG" for p in bond_pads)


class TestIsolatedPad:
    def test_unlabeled_isolated_box_has_no_net(self, tmp_path):
        def build(layout, top):
            top.shapes(layout.layer(134, 0)).insert(db.Box(0, 0, 120000, 120000))
        path = write_gds(tmp_path, "iso", build)

        copper, names, isolated, warnings = extract(path)
        assert names[0] == ""

        model = build_model(path)
        assert len(model.nets) == 0
        pad = model.pads[0]
        assert pad.name == ""
        assert pad.is_connected is False
        assert pad.net_id == -1


class TestOffPadLabelRecovery:
    def test_label_within_tolerance_recovered(self, tmp_path):
        def build(layout, top):
            # Pillar centred at (500000, 500000); text 25 um to the right (outside
            # the 45 um pad), within the default 30 um recovery window.
            top.shapes(layout.layer(134, 0)).insert(round_poly(500000, 500000, 22500, 64))
            top.shapes(layout.layer(134, 25)).insert(
                db.Text("PLR", db.Trans(db.Point(525000, 500000))))
        path = write_gds(tmp_path, "recov", build)

        copper, names, isolated, warnings = extract(path)
        assert names[0] == "PLR"

    def test_label_beyond_tolerance_not_recovered(self, tmp_path):
        def build(layout, top):
            # Text 40 um away: beyond the 30 um recovery window.
            top.shapes(layout.layer(134, 0)).insert(round_poly(500000, 500000, 22500, 64))
            top.shapes(layout.layer(134, 25)).insert(
                db.Text("PLR", db.Trans(db.Point(540000, 500000))))
        path = write_gds(tmp_path, "norecov", build)

        copper, names, isolated, warnings = extract(path)
        assert names[0] == ""


class TestConnectivityWarnings:
    def test_comma_label_warns(self, tmp_path):
        def build(layout, top):
            top.shapes(layout.layer(134, 0)).insert(db.Box(0, 0, 120000, 120000))
            top.shapes(layout.layer(134, 25)).insert(
                db.Text("A,B", db.Trans(db.Point(60000, 60000))))
        path = write_gds(tmp_path, "comma", build)

        copper, names, isolated, warnings = extract(path)
        assert warnings  # a SHORT/OPEN note fires

    def test_split_signal_warns(self, tmp_path):
        def build(layout, top):
            pad = layout.layer(134, 0)
            lbl = layout.layer(134, 25)
            # Same label on two non-touching boxes -> signal split across two nets.
            top.shapes(pad).insert(db.Box(0, 0, 120000, 120000))
            top.shapes(pad).insert(db.Box(500000, 0, 620000, 120000))
            top.shapes(lbl).insert(db.Text("SIG", db.Trans(db.Point(60000, 60000))))
            top.shapes(lbl).insert(db.Text("SIG", db.Trans(db.Point(560000, 60000))))
        path = write_gds(tmp_path, "split", build)

        copper, names, isolated, warnings = extract(path)
        assert warnings
