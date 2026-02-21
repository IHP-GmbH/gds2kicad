"""Tests for pin_extractor.py"""

import sys
import os
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import klayout.db as db

from pin_extractor import PadInfo, TextLabel, PinExtractor
from lyp_parser import LYPParser


@pytest.fixture
def test_gds_path(tmp_path):
    """Create a test GDS with known pads and text labels"""
    layout = db.Layout()
    top_cell = layout.create_cell("TEST_CHIP")

    # Layers: TopMetal2.drawing (134/0), TopMetal2.text (134/25)
    pad_layer = layout.layer(134, 0)
    text_layer = layout.layer(134, 25)

    # Create 4 pads at known positions
    #   VDD at (0, 0) - (100000, 100000)
    #   GND at (200000, 0) - (300000, 100000)
    #   SIG_A at (0, 200000) - (100000, 300000)
    #   SIG_B at (200000, 200000) - (300000, 300000)
    top_cell.shapes(pad_layer).insert(db.Box(0, 0, 100000, 100000))
    top_cell.shapes(pad_layer).insert(db.Box(200000, 0, 300000, 100000))
    top_cell.shapes(pad_layer).insert(db.Box(0, 200000, 100000, 300000))
    top_cell.shapes(pad_layer).insert(db.Box(200000, 200000, 300000, 300000))

    # Text labels at pad centers
    top_cell.shapes(text_layer).insert(db.Text("VDD", db.Trans(db.Point(50000, 50000))))
    top_cell.shapes(text_layer).insert(db.Text("GND", db.Trans(db.Point(250000, 50000))))
    top_cell.shapes(text_layer).insert(db.Text("SIG_A", db.Trans(db.Point(50000, 250000))))
    top_cell.shapes(text_layer).insert(db.Text("SIG_B", db.Trans(db.Point(250000, 250000))))

    gds_path = str(tmp_path / "test.gds")
    layout.write(gds_path)
    return gds_path


@pytest.fixture
def interposer_lyp():
    """Load interposer LYP file"""
    lyp_path = Path(__file__).parent.parent / "pdks" / "interposer.lyp"
    if not lyp_path.exists():
        pytest.skip("interposer.lyp not found")
    return LYPParser(str(lyp_path))


@pytest.fixture
def sg13g2_lyp():
    """Load SG13G2 LYP file"""
    lyp_path = Path(__file__).parent.parent / "pdks" / "sg13g2.lyp"
    if not lyp_path.exists():
        pytest.skip("sg13g2.lyp not found")
    return LYPParser(str(lyp_path))


class TestPadInfo:
    def test_from_box(self):
        box = db.Box(0, 0, 100000, 100000)
        pad = PadInfo.from_box(0, box)
        assert pad.index == 0
        assert pad.center_x == 50000.0
        assert pad.center_y == 50000.0
        assert pad.width == 100000
        assert pad.height == 100000
        assert pad.name is None

    def test_from_box_offset(self):
        box = db.Box(200000, 300000, 400000, 500000)
        pad = PadInfo.from_box(5, box)
        assert pad.center_x == 300000.0
        assert pad.center_y == 400000.0
        assert pad.width == 200000
        assert pad.height == 200000


class TestPinExtractor:
    def test_extract_pads(self, test_gds_path, interposer_lyp):
        extractor = PinExtractor(interposer_lyp)
        layout = db.Layout()
        layout.read(test_gds_path)
        cell = layout.top_cell()

        pads = extractor.extract_pads(layout, cell, (134, 0))
        assert len(pads) == 4

        # Verify all pads have expected dimensions (100um x 100um)
        for pad in pads:
            assert pad.width == 100000
            assert pad.height == 100000

    def test_extract_texts(self, test_gds_path, interposer_lyp):
        extractor = PinExtractor(interposer_lyp)
        layout = db.Layout()
        layout.read(test_gds_path)
        cell = layout.top_cell()

        text_layers = [("TopMetal2.text", (134, 25))]
        texts = extractor.extract_texts(layout, cell, text_layers)
        assert len(texts) == 4

        text_strings = {t.string for t in texts}
        assert text_strings == {"VDD", "GND", "SIG_A", "SIG_B"}

    def test_associate_texts_with_pads(self, test_gds_path, interposer_lyp):
        extractor = PinExtractor(interposer_lyp)
        layout = db.Layout()
        layout.read(test_gds_path)
        cell = layout.top_cell()

        pads = extractor.extract_pads(layout, cell, (134, 0))
        text_layers = [("TopMetal2.text", (134, 25))]
        texts = extractor.extract_texts(layout, cell, text_layers)

        named_pads = extractor.associate_texts_with_pads(pads, texts)

        # All pads should have names (texts are at pad centers)
        assert all(p.name is not None for p in named_pads)
        names = {p.name for p in named_pads}
        assert names == {"VDD", "GND", "SIG_A", "SIG_B"}

    def test_associate_with_max_distance(self, test_gds_path, interposer_lyp):
        extractor = PinExtractor(interposer_lyp)
        layout = db.Layout()
        layout.read(test_gds_path)
        cell = layout.top_cell()

        pads = extractor.extract_pads(layout, cell, (134, 0))

        # Create a text far from any pad
        far_text = TextLabel("FAR_AWAY", 900000, 900000, "TopMetal2.text", (134, 25))
        texts = [far_text]

        # Without max_distance, it still matches
        named = extractor.associate_texts_with_pads(
            [PadInfo.from_box(p.index, db.Box(*p.bbox)) for p in pads],
            texts
        )
        assert any(p.name == "FAR_AWAY" for p in named)

        # With strict max_distance, it should NOT match
        named2 = extractor.associate_texts_with_pads(
            [PadInfo.from_box(p.index, db.Box(*p.bbox)) for p in pads],
            texts,
            max_distance=10000,
        )
        assert all(p.name is None for p in named2)

    def test_auto_detect_text_layers(self, test_gds_path, interposer_lyp):
        extractor = PinExtractor(interposer_lyp)
        layout = db.Layout()
        layout.read(test_gds_path)
        cell = layout.top_cell()

        detected = extractor.auto_detect_text_layers(layout, cell, "TopMetal2.drawing")
        # Should find TopMetal2.text since we put text there
        layer_names = [name for name, _ in detected]
        assert "TopMetal2.text" in layer_names

    def test_extract_named_pads_convenience(self, test_gds_path, interposer_lyp):
        extractor = PinExtractor(interposer_lyp)
        pads, cell_name = extractor.extract_named_pads(
            test_gds_path, "TopMetal2.drawing"
        )
        assert cell_name == "TEST_CHIP"
        assert len(pads) == 4
        names = {p.name for p in pads}
        assert names == {"VDD", "GND", "SIG_A", "SIG_B"}


class TestTextConflict:
    def test_closer_text_wins(self, interposer_lyp):
        """When two texts compete for the same pad, closest wins"""
        extractor = PinExtractor(interposer_lyp)

        pads = [PadInfo(0, (0, 0, 100000, 100000), 50000, 50000, 100000, 100000)]
        texts = [
            TextLabel("FAR_NAME", 50100, 50000, "t", (134, 25)),   # close
            TextLabel("CLOSE_NAME", 50000, 50000, "t", (134, 25)), # exact center
        ]

        named = extractor.associate_texts_with_pads(pads, texts)
        assert named[0].name == "CLOSE_NAME"

    def test_empty_pads(self, interposer_lyp):
        extractor = PinExtractor(interposer_lyp)
        result = extractor.associate_texts_with_pads([], [])
        assert result == []

    def test_no_texts(self, interposer_lyp):
        extractor = PinExtractor(interposer_lyp)
        pads = [PadInfo(0, (0, 0, 100, 100), 50, 50, 100, 100)]
        result = extractor.associate_texts_with_pads(pads, [])
        assert result[0].name is None
