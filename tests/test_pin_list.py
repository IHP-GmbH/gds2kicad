# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for pin_list module."""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import klayout.db as db

from pin_list import PinList, PinEntry, VALID_PIN_TYPES, VALID_PIN_SIDES
from pin_extractor import PadInfo


class TestPinEntry:
    def test_defaults(self):
        entry = PinEntry(name="SIG")
        assert entry.type == "passive"
        assert entry.side == "left"
        assert entry.pad_index == 0

    def test_to_dict(self):
        entry = PinEntry(name="VDD", type="power_in", side="top", pad_index=3,
                         center_x_dbu=100.0, center_y_dbu=200.0,
                         width_dbu=80000, height_dbu=80000)
        d = entry.to_dict()
        assert d["name"] == "VDD"
        assert d["type"] == "power_in"
        assert d["center_x_dbu"] == 100.0
        assert d["width_dbu"] == 80000

    def test_from_dict(self):
        d = {"name": "GND", "type": "power_in", "side": "bottom", "pad_index": 5}
        entry = PinEntry.from_dict(d)
        assert entry.name == "GND"
        assert entry.side == "bottom"
        assert entry.pad_index == 5
        # Defaults for missing keys
        assert entry.center_x_dbu == 0.0

    def test_from_dict_minimal(self):
        d = {"name": "X"}
        entry = PinEntry.from_dict(d)
        assert entry.type == "passive"
        assert entry.side == "left"


class TestPinListCreation:
    def test_empty(self):
        pl = PinList()
        assert len(pl) == 0
        assert pl.metadata["version"] == 1

    def test_with_entries(self):
        entries = [
            PinEntry(name="VDD", type="power_in", side="top"),
            PinEntry(name="GND", type="power_in", side="bottom"),
            PinEntry(name="SIG", type="passive", side="left"),
        ]
        pl = PinList(pins=entries)
        assert len(pl) == 3

    def test_from_extracted_pads(self):
        pads = [
            PadInfo(index=0, bbox=(0, 0, 80000, 80000),
                    center_x=40000, center_y=40000,
                    width=80000, height=80000, name="VDD"),
            PadInfo(index=1, bbox=(200000, 0, 280000, 80000),
                    center_x=240000, center_y=40000,
                    width=80000, height=80000, name="GND"),
            PadInfo(index=2, bbox=(400000, 0, 480000, 80000),
                    center_x=440000, center_y=40000,
                    width=80000, height=80000, name="DATA"),
        ]

        pl = PinList.from_extracted_pads(
            pads, chiplet_name="TEST",
            gds_source="test.gds", lyp_file="test.lyp",
            pad_layer="TopMetal2.drawing",
            text_layers=["TopMetal2.text"],
        )

        assert len(pl) == 3
        assert pl.metadata["chiplet_name"] == "TEST"
        assert pl.metadata["gds_source"] == "test.gds"
        assert pl.metadata["pad_layer"] == "TopMetal2.drawing"

        # VDD should be classified as power_in, top
        vdd = pl.pins[0]
        assert vdd.name == "VDD"
        assert vdd.type == "power_in"
        assert vdd.side == "top"

        # GND should be power_in, bottom
        gnd = pl.pins[1]
        assert gnd.name == "GND"
        assert gnd.type == "power_in"
        assert gnd.side == "bottom"

        # DATA should be passive, left (default signal)
        data = pl.pins[2]
        assert data.name == "DATA"
        assert data.type == "passive"
        assert data.side == "left"

        # Geometry preserved
        assert vdd.center_x_dbu == 40000
        assert vdd.width_dbu == 80000

    def test_from_extracted_pads_unnamed(self):
        """Pads without text labels get sequential numbers."""
        pads = [
            PadInfo(index=0, bbox=(0, 0, 100, 100),
                    center_x=50, center_y=50, width=100, height=100),
            PadInfo(index=1, bbox=(200, 0, 300, 100),
                    center_x=250, center_y=50, width=100, height=100),
        ]

        pl = PinList.from_extracted_pads(pads, chiplet_name="UNNAMED")
        assert pl.pins[0].name == "1"
        assert pl.pins[1].name == "2"


class TestPinListIO:
    def test_save_and_load_roundtrip(self, tmp_path):
        entries = [
            PinEntry(name="VDD", type="power_in", side="top", pad_index=0,
                     center_x_dbu=40000, center_y_dbu=40000,
                     width_dbu=80000, height_dbu=80000),
            PinEntry(name="GND", type="power_in", side="bottom", pad_index=1),
            PinEntry(name="CLK", type="input", side="left", pad_index=2),
        ]
        original = PinList(
            pins=entries,
            metadata={
                "version": 1,
                "chiplet_name": "ROUNDTRIP_TEST",
                "gds_source": "test.gds",
                "lyp_file": "test.lyp",
                "pad_layer": "TopMetal2.drawing",
                "text_layers": ["TopMetal2.text"],
                "timestamp": "2026-02-20T14:30:00",
            }
        )

        json_path = str(tmp_path / "test_pins.json")
        original.save(json_path)

        # Verify file content
        with open(json_path) as f:
            data = json.load(f)
        assert data["version"] == 1
        assert data["chiplet_name"] == "ROUNDTRIP_TEST"
        assert len(data["pins"]) == 3

        # Load back
        loaded = PinList.load(json_path)
        assert len(loaded) == 3
        assert loaded.metadata["chiplet_name"] == "ROUNDTRIP_TEST"
        assert loaded.metadata["pad_layer"] == "TopMetal2.drawing"

        # Check pin data preserved
        assert loaded.pins[0].name == "VDD"
        assert loaded.pins[0].type == "power_in"
        assert loaded.pins[0].center_x_dbu == 40000
        assert loaded.pins[2].name == "CLK"
        assert loaded.pins[2].type == "input"

    def test_save_creates_parent_dirs(self, tmp_path):
        pl = PinList(pins=[PinEntry(name="X")])
        nested = str(tmp_path / "a" / "b" / "pins.json")
        pl.save(nested)
        assert Path(nested).exists()


class TestPinListValidation:
    def test_empty_list(self):
        pl = PinList()
        warnings = pl.validate()
        assert any("empty" in w.lower() for w in warnings)

    def test_valid_list(self):
        pl = PinList(pins=[
            PinEntry(name="VDD", type="power_in", side="top"),
            PinEntry(name="GND", type="power_in", side="bottom"),
        ])
        warnings = pl.validate()
        assert len(warnings) == 0

    def test_duplicate_names(self):
        pl = PinList(pins=[
            PinEntry(name="VDD"),
            PinEntry(name="VDD"),
            PinEntry(name="GND"),
        ])
        warnings = pl.validate()
        assert any("duplicate" in w.lower() for w in warnings)
        assert any("VDD" in w for w in warnings)

    def test_empty_names(self):
        pl = PinList(pins=[
            PinEntry(name="VDD"),
            PinEntry(name=""),
            PinEntry(name="  "),
        ])
        warnings = pl.validate()
        assert any("empty" in w.lower() for w in warnings)

    def test_invalid_type(self):
        pl = PinList(pins=[
            PinEntry(name="X", type="bogus"),
        ])
        warnings = pl.validate()
        assert any("invalid type" in w.lower() for w in warnings)

    def test_invalid_side(self):
        pl = PinList(pins=[
            PinEntry(name="X", side="diagonal"),
        ])
        warnings = pl.validate()
        assert any("invalid side" in w.lower() for w in warnings)


class TestPinListDeduplication:
    def test_no_duplicates(self):
        pl = PinList(pins=[
            PinEntry(name="A"),
            PinEntry(name="B"),
            PinEntry(name="C"),
        ])
        pl.deduplicate_names()
        names = [p.name for p in pl.pins]
        assert names == ["A", "B", "C"]

    def test_basic_dedup(self):
        pl = PinList(pins=[
            PinEntry(name="VDD"),
            PinEntry(name="VDD"),
            PinEntry(name="VDD"),
            PinEntry(name="GND"),
        ])
        pl.deduplicate_names()
        names = [p.name for p in pl.pins]
        assert names == ["VDD", "VDD_1", "VDD_2", "GND"]

    def test_dedup_preserves_unique(self):
        pl = PinList(pins=[
            PinEntry(name="A"),
            PinEntry(name="B"),
            PinEntry(name="A"),
        ])
        pl.deduplicate_names()
        names = [p.name for p in pl.pins]
        assert names[0] == "A"
        assert names[1] == "B"
        assert names[2] == "A_1"

    def test_dedup_clears_validation(self):
        pl = PinList(pins=[
            PinEntry(name="VDD", type="power_in", side="top"),
            PinEntry(name="VDD", type="power_in", side="top"),
        ])
        assert any("duplicate" in w.lower() for w in pl.validate())
        pl.deduplicate_names()
        warnings = pl.validate()
        assert not any("duplicate" in w.lower() for w in warnings)


class TestPinListHelpers:
    def test_get_unique_names(self):
        pl = PinList(pins=[
            PinEntry(name="B"),
            PinEntry(name="A"),
            PinEntry(name="B"),
            PinEntry(name="C"),
        ])
        unique = pl.get_unique_names()
        assert unique == ["A", "B", "C"]

    def test_get_unique_names_skips_empty(self):
        pl = PinList(pins=[
            PinEntry(name="A"),
            PinEntry(name=""),
            PinEntry(name="  "),
        ])
        unique = pl.get_unique_names()
        assert unique == ["A"]

    def test_repr(self):
        pl = PinList(
            pins=[PinEntry(name="X")],
            metadata={"version": 1, "chiplet_name": "TEST"},
        )
        r = repr(pl)
        assert "1 pins" in r
        assert "TEST" in r

    def test_len(self):
        pl = PinList(pins=[PinEntry(name="A"), PinEntry(name="B")])
        assert len(pl) == 2
