"""Tests for polygon pad support across the pipeline."""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import klayout.db as db

from pin_extractor import PadInfo, PinExtractor
from pin_list import PinList, PinEntry
from pad_review import PadReview
from lyp_parser import LYPParser


@pytest.fixture
def interposer_lyp():
    lyp_path = Path(__file__).parent.parent / "pdks" / "interposer.lyp"
    if not lyp_path.exists():
        pytest.skip("interposer.lyp not found")
    return LYPParser(str(lyp_path))


def _create_polygon_gds(path, pad_layer=(134, 0), text_layer=(134, 25)):
    """Create a GDS with a mix of box and polygon pads."""
    layout = db.Layout()
    cell = layout.create_cell("POLY_TEST")

    pl = layout.layer(*pad_layer)
    tl = layout.layer(*text_layer)

    # Box pad at (0,0)-(100000, 100000)
    cell.shapes(pl).insert(db.Box(0, 0, 100000, 100000))
    cell.shapes(tl).insert(db.Text("RECT_PAD", db.Trans(db.Point(50000, 50000))))

    # L-shaped polygon pad
    poly = db.Polygon([
        db.Point(200000, 0),
        db.Point(300000, 0),
        db.Point(300000, 50000),
        db.Point(250000, 50000),
        db.Point(250000, 100000),
        db.Point(200000, 100000),
    ])
    cell.shapes(pl).insert(poly)
    cell.shapes(tl).insert(db.Text("POLY_PAD", db.Trans(db.Point(250000, 50000))))

    layout.write(str(path))
    return path


class TestPolygonExtraction:
    def test_polygon_preserves_vertices(self, tmp_path, interposer_lyp):
        """PadInfo.from_polygon should store all hull vertices."""
        gds_path = _create_polygon_gds(tmp_path / "poly.gds")

        extractor = PinExtractor(interposer_lyp)
        layout = db.Layout()
        layout.read(str(gds_path))
        cell = layout.top_cell()

        pads = extractor.extract_pads(layout, cell, (134, 0))
        assert len(pads) == 2

        # Find the polygon pad
        poly_pad = next(p for p in pads if p.is_polygon)
        assert poly_pad.polygon_points is not None
        assert len(poly_pad.polygon_points) == 6

        # Find the box pad
        box_pad = next(p for p in pads if not p.is_polygon)
        assert box_pad.polygon_points is None
        assert box_pad.is_polygon is False

    def test_polygon_bbox_is_correct(self, tmp_path, interposer_lyp):
        """Polygon bbox should span the full extent."""
        gds_path = _create_polygon_gds(tmp_path / "poly.gds")

        extractor = PinExtractor(interposer_lyp)
        layout = db.Layout()
        layout.read(str(gds_path))
        cell = layout.top_cell()

        pads = extractor.extract_pads(layout, cell, (134, 0))
        poly_pad = next(p for p in pads if p.is_polygon)

        left, bottom, right, top = poly_pad.bbox
        assert left == 200000
        assert bottom == 0
        assert right == 300000
        assert top == 100000


class TestPolygonFootprintOutput:
    def test_polygon_produces_smd_custom(self, tmp_path, interposer_lyp):
        """Polygon pads should produce 'smd custom' + 'gr_poly' in footprint."""
        gds_path = _create_polygon_gds(tmp_path / "poly.gds")
        output = str(tmp_path / "poly.kicad_mod")

        from gds_to_kicad import GDSToKiCad
        converter = GDSToKiCad(interposer_lyp, "TopMetal2.drawing",
                                auto_detect_text=True)
        converter.convert(str(gds_path), output)

        content = Path(output).read_text()
        assert "smd custom" in content
        assert "gr_poly" in content
        assert "(primitives" in content

    def test_rectangles_still_produce_smd_rect(self, tmp_path, interposer_lyp):
        """Box pads should still produce 'smd rect'."""
        gds_path = _create_polygon_gds(tmp_path / "poly.gds")
        output = str(tmp_path / "poly.kicad_mod")

        from gds_to_kicad import GDSToKiCad
        converter = GDSToKiCad(interposer_lyp, "TopMetal2.drawing",
                                auto_detect_text=True)
        converter.convert(str(gds_path), output)

        content = Path(output).read_text()
        assert "smd rect" in content


class TestPolygonPinList:
    def test_pin_list_json_roundtrip(self, tmp_path):
        """Pin list with polygon points should survive JSON save/load."""
        pl = PinList(pins=[
            PinEntry(name="RECT", pad_index=0,
                     center_x_dbu=50000, center_y_dbu=50000,
                     width_dbu=100000, height_dbu=100000),
            PinEntry(name="POLY", pad_index=1,
                     center_x_dbu=250000, center_y_dbu=50000,
                     width_dbu=100000, height_dbu=100000,
                     polygon_points_dbu=[
                         [200000.0, 0.0], [300000.0, 0.0],
                         [300000.0, 50000.0], [250000.0, 50000.0],
                         [250000.0, 100000.0], [200000.0, 100000.0],
                     ]),
        ])

        json_path = str(tmp_path / "poly_pins.json")
        pl.save(json_path)

        loaded = PinList.load(json_path)
        assert len(loaded.pins) == 2

        rect_pin = loaded.pins[0]
        assert rect_pin.polygon_points_dbu is None

        poly_pin = loaded.pins[1]
        assert poly_pin.polygon_points_dbu is not None
        assert len(poly_pin.polygon_points_dbu) == 6
        assert poly_pin.polygon_points_dbu[0] == [200000.0, 0.0]

    def test_backward_compatible_load(self, tmp_path):
        """Loading a pin list without polygon_points_dbu should work."""
        data = {
            "version": 1,
            "chiplet_name": "OLD_FORMAT",
            "pins": [
                {"name": "VDD", "type": "power_in", "side": "top",
                 "pad_index": 0, "center_x_dbu": 0, "center_y_dbu": 0,
                 "width_dbu": 100, "height_dbu": 100},
            ]
        }
        json_path = str(tmp_path / "old_format.json")
        with open(json_path, 'w') as f:
            json.dump(data, f)

        loaded = PinList.load(json_path)
        assert loaded.pins[0].polygon_points_dbu is None


class TestPolygonPadReview:
    def test_pad_review_preserves_polygon_vertices(self, tmp_path):
        """read_edited_pads should return polygon_points for polygon shapes."""
        gds_path = _create_polygon_gds(tmp_path / "review.gds")

        pads = PadReview.read_edited_pads(
            str(gds_path), pad_layer=(134, 0)
        )
        assert len(pads) == 2

        # Find the polygon pad (one has polygon_points, one doesn't)
        poly_pads = [p for p in pads if p.get("is_polygon")]
        rect_pads = [p for p in pads if not p.get("is_polygon")]

        assert len(poly_pads) == 1
        assert len(rect_pads) == 1

        assert poly_pads[0]["polygon_points"] is not None
        assert len(poly_pads[0]["polygon_points"]) == 6
        assert rect_pads[0]["polygon_points"] is None
