# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for pad_review module."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import klayout.db as db

from pad_review import PadReview
from pin_list import PinList, PinEntry


def _create_source_gds(path, pad_layer=(134, 0), text_layer=(134, 25),
                        extra_layer=None):
    """Create a source GDS with pads, text, and optionally extra shapes."""
    layout = db.Layout()
    top = layout.create_cell("SRC_CELL")

    pl = layout.layer(*pad_layer)
    tl = layout.layer(*text_layer)

    # 3 pads
    pads_data = [
        (0, 0, 80000, 80000, "VDD"),
        (200000, 0, 280000, 80000, "GND"),
        (400000, 0, 480000, 80000, "SIG"),
    ]
    for x0, y0, x1, y1, name in pads_data:
        top.shapes(pl).insert(db.Box(x0, y0, x1, y1))
        cx = (x0 + x1) // 2
        cy = (y0 + y1) // 2
        top.shapes(tl).insert(db.Text(name, db.Trans(db.Point(cx, cy))))

    # Extra non-pad shapes (routing, fills) on pad layer
    top.shapes(pl).insert(db.Box(0, 100000, 500000, 102000))  # thin routing
    top.shapes(pl).insert(db.Box(0, 200000, 500000, 210000))  # wide fill

    # Shapes on other layer (should not appear in pad review)
    if extra_layer:
        el = layout.layer(*extra_layer)
        top.shapes(el).insert(db.Box(0, 0, 1000000, 1000000))

    layout.write(str(path))
    return path


@pytest.fixture
def source_gds(tmp_path):
    return _create_source_gds(tmp_path / "source.gds")


@pytest.fixture
def pin_list_3():
    return PinList(pins=[
        PinEntry(name="VDD", type="power_in", side="top", pad_index=0,
                 center_x_dbu=40000, center_y_dbu=40000,
                 width_dbu=80000, height_dbu=80000),
        PinEntry(name="GND", type="power_in", side="bottom", pad_index=1,
                 center_x_dbu=240000, center_y_dbu=40000,
                 width_dbu=80000, height_dbu=80000),
        PinEntry(name="SIG", type="passive", side="left", pad_index=2,
                 center_x_dbu=440000, center_y_dbu=40000,
                 width_dbu=80000, height_dbu=80000),
    ])


class TestGenerate:
    def test_output_contains_only_pad_layer(self, source_gds, tmp_path):
        """Pad review GDS should contain only pad layer shapes."""
        out = str(tmp_path / "review.gds")
        count = PadReview.generate(
            str(source_gds), out,
            pad_layer=(134, 0),
            text_layer=(134, 25),
        )

        # 3 pads + 2 routing/fill = 5 shapes on pad layer
        assert count == 5

        # Verify output
        layout = db.Layout()
        layout.read(out)
        top = layout.top_cell()
        assert top is not None

        # Should have shapes on pad layer
        pl_idx = layout.layer(134, 0)
        shape_count = 0
        for _ in top.shapes(pl_idx).each():
            shape_count += 1
        assert shape_count == 5

    def test_text_from_source(self, source_gds, tmp_path):
        """Text should be copied from source when no pin_list."""
        out = str(tmp_path / "review.gds")
        PadReview.generate(
            str(source_gds), out,
            pad_layer=(134, 0),
            text_layer=(134, 25),
        )

        layout = db.Layout()
        layout.read(out)
        top = layout.top_cell()

        tl_idx = layout.layer(134, 25)
        texts = []
        for shape in top.shapes(tl_idx).each():
            if shape.is_text():
                texts.append(shape.text.string)
        assert set(texts) == {"VDD", "GND", "SIG"}

    def test_text_from_pin_list(self, source_gds, tmp_path, pin_list_3):
        """When pin_list provided, text labels should come from it."""
        # Rename a pin in the list
        pin_list_3.pins[2].name = "CUSTOM_NAME"

        out = str(tmp_path / "review.gds")
        PadReview.generate(
            str(source_gds), out,
            pad_layer=(134, 0),
            text_layer=(134, 25),
            pin_list=pin_list_3,
        )

        layout = db.Layout()
        layout.read(out)
        top = layout.top_cell()

        tl_idx = layout.layer(134, 25)
        texts = []
        for shape in top.shapes(tl_idx).each():
            if shape.is_text():
                texts.append(shape.text.string)
        assert "CUSTOM_NAME" in texts
        assert "SIG" not in texts  # original text replaced

    def test_no_text_layer(self, source_gds, tmp_path):
        """Generate without text layer should still work."""
        out = str(tmp_path / "review.gds")
        count = PadReview.generate(
            str(source_gds), out,
            pad_layer=(134, 0),
        )
        assert count == 5
        assert Path(out).exists()

    def test_preserves_cell_name(self, source_gds, tmp_path):
        out = str(tmp_path / "review.gds")
        PadReview.generate(str(source_gds), out, pad_layer=(134, 0))

        layout = db.Layout()
        layout.read(out)
        assert layout.top_cell().name == "SRC_CELL"

    def test_other_layers_excluded(self, tmp_path):
        """Shapes on non-pad layers should not appear in output."""
        src = _create_source_gds(
            tmp_path / "multi.gds",
            extra_layer=(50, 0),
        )
        out = str(tmp_path / "review.gds")
        PadReview.generate(str(src), out, pad_layer=(134, 0))

        layout = db.Layout()
        layout.read(out)
        top = layout.top_cell()

        # Check that layer 50/0 is not present
        for li in layout.layer_indices():
            info = layout.get_info(li)
            shapes = top.shapes(li)
            count = 0
            for _ in shapes.each():
                count += 1
            if info.layer == 50 and info.datatype == 0:
                assert count == 0, "Extra layer shapes should not be in output"


class TestReadEditedPads:
    def test_read_all_pads(self, source_gds, tmp_path):
        """Read back should find all shapes on pad layer."""
        pads = PadReview.read_edited_pads(
            str(source_gds), pad_layer=(134, 0)
        )
        # 3 pads + 2 routing shapes = 5
        assert len(pads) == 5

    def test_read_after_deletion(self, source_gds, tmp_path, pin_list_3):
        """After user deletes non-pad shapes, read back should have fewer."""
        # Generate pad review
        review = str(tmp_path / "review.gds")
        PadReview.generate(
            str(source_gds), review,
            pad_layer=(134, 0),
            text_layer=(134, 25),
            pin_list=pin_list_3,
        )

        # Simulate user editing: load, remove non-pad shapes, save
        layout = db.Layout()
        layout.read(review)
        top = layout.top_cell()
        pl_idx = layout.layer(134, 0)

        # Remove shapes that are clearly routing (aspect ratio check)
        to_remove = []
        for shape in top.shapes(pl_idx).each():
            if shape.is_box():
                box = shape.box
                w = box.right - box.left
                h = box.top - box.bottom
                # Routing is very thin or very wide relative to pads
                if w > 100000 or h < 10000:
                    to_remove.append(shape.box)

        for box in to_remove:
            # Can't easily delete individual shapes, so rebuild
            pass

        # Alternative: create a clean GDS with only the 3 pads
        clean = db.Layout()
        clean_cell = clean.create_cell("SRC_CELL")
        clean_pl = clean.layer(134, 0)
        clean_cell.shapes(clean_pl).insert(db.Box(0, 0, 80000, 80000))
        clean_cell.shapes(clean_pl).insert(db.Box(200000, 0, 280000, 80000))
        clean_cell.shapes(clean_pl).insert(db.Box(400000, 0, 480000, 80000))

        edited = str(tmp_path / "edited.gds")
        clean.write(edited)

        pads = PadReview.read_edited_pads(
            edited, pad_layer=(134, 0), pin_list=pin_list_3
        )
        assert len(pads) == 3

        # Names should be matched from pin_list
        names = {p["name"] for p in pads}
        assert names == {"VDD", "GND", "SIG"}

    def test_pad_geometry(self, tmp_path):
        """Read-back should return correct geometry."""
        layout = db.Layout()
        cell = layout.create_cell("GEO_TEST")
        pl = layout.layer(134, 0)
        cell.shapes(pl).insert(db.Box(1000, 2000, 5000, 6000))

        gds = str(tmp_path / "geo.gds")
        layout.write(gds)

        pads = PadReview.read_edited_pads(gds, pad_layer=(134, 0))
        assert len(pads) == 1
        p = pads[0]
        assert p["center_x"] == 3000.0
        assert p["center_y"] == 4000.0
        assert p["width"] == 4000
        assert p["height"] == 4000

    def test_pin_list_matching_accuracy(self, tmp_path):
        """Nearest-neighbor matching should assign correct names."""
        # Create GDS with 2 pads far apart
        layout = db.Layout()
        cell = layout.create_cell("MATCH_TEST")
        pl = layout.layer(134, 0)
        cell.shapes(pl).insert(db.Box(0, 0, 100, 100))       # pad near (50, 50)
        cell.shapes(pl).insert(db.Box(10000, 10000, 10100, 10100))  # pad near (10050, 10050)

        gds = str(tmp_path / "match.gds")
        layout.write(gds)

        pin_list = PinList(pins=[
            PinEntry(name="NEAR_ORIGIN", center_x_dbu=50, center_y_dbu=50),
            PinEntry(name="FAR_AWAY", center_x_dbu=10050, center_y_dbu=10050),
        ])

        pads = PadReview.read_edited_pads(
            gds, pad_layer=(134, 0), pin_list=pin_list
        )
        assert len(pads) == 2

        # Sort by center_x to get deterministic order
        pads_sorted = sorted(pads, key=lambda p: p["center_x"])
        assert pads_sorted[0]["name"] == "NEAR_ORIGIN"
        assert pads_sorted[1]["name"] == "FAR_AWAY"

    def test_no_pin_list(self, tmp_path):
        """Without pin_list, pads should have name=None."""
        layout = db.Layout()
        cell = layout.create_cell("NONAME")
        pl = layout.layer(134, 0)
        cell.shapes(pl).insert(db.Box(0, 0, 100, 100))

        gds = str(tmp_path / "noname.gds")
        layout.write(gds)

        pads = PadReview.read_edited_pads(gds, pad_layer=(134, 0))
        assert len(pads) == 1
        assert pads[0]["name"] is None

    def test_polygon_shapes(self, tmp_path):
        """Polygons should also be read as pads."""
        layout = db.Layout()
        cell = layout.create_cell("POLY_TEST")
        pl = layout.layer(134, 0)
        # Insert a polygon (triangle)
        poly = db.Polygon([
            db.Point(0, 0), db.Point(100, 0), db.Point(50, 100)
        ])
        cell.shapes(pl).insert(poly)

        gds = str(tmp_path / "poly.gds")
        layout.write(gds)

        pads = PadReview.read_edited_pads(gds, pad_layer=(134, 0))
        assert len(pads) == 1
        assert pads[0]["width"] == 100
        assert pads[0]["height"] == 100


class TestPadCountChange:
    def test_count_decreases_after_edit(self, tmp_path, pin_list_3):
        """Demonstrate that pad count changes when user removes shapes."""
        # Source has 5 shapes (3 pads + 2 routing)
        src = _create_source_gds(tmp_path / "src.gds")

        before = PadReview.read_edited_pads(
            str(src), pad_layer=(134, 0)
        )
        assert len(before) == 5

        # "Edited" version: only the 3 real pads
        layout = db.Layout()
        cell = layout.create_cell("SRC_CELL")
        pl = layout.layer(134, 0)
        cell.shapes(pl).insert(db.Box(0, 0, 80000, 80000))
        cell.shapes(pl).insert(db.Box(200000, 0, 280000, 80000))
        cell.shapes(pl).insert(db.Box(400000, 0, 480000, 80000))

        edited = str(tmp_path / "edited.gds")
        layout.write(edited)

        after = PadReview.read_edited_pads(
            edited, pad_layer=(134, 0), pin_list=pin_list_3
        )
        assert len(after) == 3
        assert len(after) < len(before)
