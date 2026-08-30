# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for interposer_profile.py"""

import json
import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

import klayout.db as db

from interposer_profile import InterposerProfile


class TestDefaults:
    def test_defaults(self):
        p = InterposerProfile()
        assert p.pad_layer == (134, 0)
        assert p.label_layer == (134, 25)
        assert p.keep_fill is True
        assert p.pillar_dia_um == 45.0
        assert p.bondpad_size_um == 120.0
        assert p.pillar_marker_layer == (41, 35)
        assert p.bondpad_marker_layer == (41, 0)


class TestFromDict:
    def test_known_keys_and_layer_coercion(self):
        p = InterposerProfile.from_dict({"pad_layer": "134/0", "pillar_dia_um": 49})
        assert p.pad_layer == (134, 0)
        assert p.pillar_dia_um == 49

    def test_unknown_key_ignored(self):
        p = InterposerProfile.from_dict({"not_a_field": 123, "bondpad_size_um": 88})
        assert p.bondpad_size_um == 88
        assert not hasattr(p, "not_a_field")

    def test_layer_from_list(self):
        p = InterposerProfile.from_dict({"label_layer": [134, 25]})
        assert p.label_layer == (134, 25)

    def test_to_dict_roundtrip(self):
        p = InterposerProfile(pillar_dia_um=49.0, bondpad_size_um=110.0)
        rt = InterposerProfile.from_dict(p.to_dict())
        assert rt == p
        # Layer keys serialise as lists.
        assert p.to_dict()["pad_layer"] == [134, 0]


class TestMerge:
    def test_merge_returns_copy(self):
        base = InterposerProfile()
        merged = base.merge({"pillar_dia_um": 60.0})
        assert merged.pillar_dia_um == 60.0
        # Original untouched.
        assert base.pillar_dia_um == 45.0
        assert merged is not base

    def test_merge_coerces_layer(self):
        merged = InterposerProfile().merge({"pad_layer": "8/0"})
        assert merged.pad_layer == (8, 0)


class TestLoad:
    def test_load_from_file(self, tmp_path):
        path = tmp_path / "profile.json"
        path.write_text(json.dumps({"pillar_dia_um": 42.5, "pad_layer": "10/2"}))
        p = InterposerProfile.load(str(path))
        assert p.pillar_dia_um == 42.5
        assert p.pad_layer == (10, 2)
        # Unspecified fields keep defaults.
        assert p.bondpad_size_um == 120.0


class TestCalibrate:
    def test_calibrate_from_markers(self):
        layout = db.Layout()
        layout.dbu = 0.001
        top = layout.create_cell("CAL")

        # Two 60um pillar markers on 41/35.
        for cx in (0, 200000):
            top.shapes(layout.layer(41, 35)).insert(
                db.Box(cx, 0, cx + 60000, 60000))
        # Two 100um bond-pad markers on 41/0.
        for cx in (0, 200000):
            top.shapes(layout.layer(41, 0)).insert(
                db.Box(cx, 300000, cx + 100000, 400000))

        p = InterposerProfile()
        ret = p.calibrate_from_markers(layout, top)
        assert ret is p  # mutates in place, returns self
        assert abs(p.pillar_dia_um - 60.0) < 1e-6
        assert abs(p.bondpad_size_um - 100.0) < 1e-6

    def test_calibrate_missing_layers_noop(self):
        layout = db.Layout()
        layout.dbu = 0.001
        top = layout.create_cell("EMPTY")
        p = InterposerProfile()
        p.calibrate_from_markers(layout, top)
        # No markers -> defaults preserved.
        assert p.pillar_dia_um == 45.0
        assert p.bondpad_size_um == 120.0
