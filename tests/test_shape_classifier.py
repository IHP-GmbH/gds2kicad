# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for shape_classifier.py

CopperShape instances are built directly (no GDS needed); sizes are in DBU with
dbu=0.001 so 45 um == 45000 DBU. The isolation dict is supplied by hand, standing
in for what net extraction would report.
"""

import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

import klayout.db as db

import shape_classifier
from interposer_model import CopperShape, CopperRole
from interposer_profile import InterposerProfile

DBU = 0.001


def make_shape(index, w, h, n_vertices, is_path=False, is_polygon=False,
               cx=500000, cy=500000):
    """A CopperShape centred at (cx, cy), sizes in DBU."""
    l = cx - w // 2
    b = cy - h // 2
    r = cx + w // 2
    t = cy + h // 2
    return CopperShape(
        source_index=index, role=CopperRole.MISC, net_id=-1,
        is_polygon=is_polygon, is_path=is_path,
        bbox_dbu=(l, b, r, t), center_dbu=(cx, cy),
        width_dbu=w, height_dbu=h, n_vertices=n_vertices)


def classify_one(shape, isolated=False, marker_regions=None):
    shape_classifier.classify([shape], InterposerProfile(), DBU,
                              {shape.source_index: isolated}, marker_regions)
    return shape.role


class TestSingleRoles:
    def test_bond_pad(self):
        s = make_shape(0, 120000, 120000, 4)
        assert classify_one(s, isolated=False) == CopperRole.BOND_PAD

    def test_cu_pillar_round(self):
        # 45 um near-round polygon, many hull vertices.
        s = make_shape(0, 45000, 45000, 64, is_polygon=True)
        assert classify_one(s, isolated=False) == CopperRole.CU_PILLAR

    def test_octagon_below_min_vertices_not_pillar(self):
        # Same size but only 8 vertices: below pillar_min_vertices (16).
        s = make_shape(0, 45000, 45000, 8, is_polygon=True)
        role = classify_one(s, isolated=False)
        assert role != CopperRole.CU_PILLAR

    def test_path_is_wire(self):
        s = make_shape(0, 300000, 5000, 2, is_path=True)
        assert classify_one(s) == CopperRole.WIRE

    def test_elongated_box_is_wire(self):
        # Very elongated (not near-square) rectangle -> routing stub -> wire.
        s = make_shape(0, 9400, 314000, 4)
        assert classify_one(s) == CopperRole.WIRE

    def test_long_box_is_frame(self):
        # One side over frame_min_len (1000 um).
        s = make_shape(0, 1500000, 3500, 4)
        assert classify_one(s) == CopperRole.FRAME

    def test_large_square_is_plane(self):
        s = make_shape(0, 304000, 304000, 4)
        assert classify_one(s) == CopperRole.PLANE


class TestFillMassRule:
    def test_many_isolated_small_squares_are_fill(self):
        shapes = [make_shape(i, 20000, 20000, 4, cx=100000 * i, cy=0)
                  for i in range(25)]
        isolated = {s.source_index: True for s in shapes}
        shape_classifier.classify(shapes, InterposerProfile(), DBU, isolated)
        assert all(s.role == CopperRole.FILL for s in shapes)

    def test_lone_small_square_is_not_fill(self):
        # A single isolated 20 um box among non-fill shapes: fewer than
        # min_fill_repeat (20) candidates, so it is not fill.
        lone = make_shape(0, 20000, 20000, 4)
        others = [make_shape(1, 120000, 120000, 4, cx=900000),
                  make_shape(2, 120000, 120000, 4, cx=1200000)]
        shapes = [lone] + others
        isolated = {0: True, 1: False, 2: False}
        shape_classifier.classify(shapes, InterposerProfile(), DBU, isolated)
        assert lone.role != CopperRole.FILL


class TestMarkerOverride:
    def test_marker_forces_cu_pillar(self):
        # A 120 um box would normally be a bond-pad, but a pillar marker region
        # covering its centre forces CU_PILLAR regardless of size.
        s = make_shape(0, 120000, 120000, 4, cx=500000, cy=500000)
        region = db.Region(db.Box(490000, 490000, 510000, 510000))
        role = classify_one(s, isolated=False,
                            marker_regions={CopperRole.CU_PILLAR: region})
        assert role == CopperRole.CU_PILLAR
