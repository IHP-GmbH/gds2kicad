"""Integration tests: full pipeline from GDS to .kicad_sym"""

import sys
import json
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import klayout.db as db

from lyp_parser import LYPParser
from pin_extractor import PinExtractor
from kicad_sym_writer import KiCadSymWriter, PinSide
from symbol_layout import create_default_layout, create_layout_from_pin_list
from pin_list import PinList, PinEntry
from pad_review import PadReview


def _create_test_gds(path, pad_names, pad_layer=(134, 0), text_layer=(134, 25)):
    """Create a GDS file with pads and labeled text"""
    layout = db.Layout()
    top_cell = layout.create_cell("INTEGRATION_TEST")

    pl = layout.layer(*pad_layer)
    tl = layout.layer(*text_layer)

    for i, name in enumerate(pad_names):
        x0 = (i % 5) * 200000
        y0 = (i // 5) * 200000
        x1 = x0 + 80000
        y1 = y0 + 80000
        top_cell.shapes(pl).insert(db.Box(x0, y0, x1, y1))
        cx = (x0 + x1) // 2
        cy = (y0 + y1) // 2
        top_cell.shapes(tl).insert(db.Text(name, db.Trans(db.Point(cx, cy))))

    layout.write(str(path))
    return path


@pytest.fixture
def interposer_lyp():
    lyp_path = Path(__file__).parent.parent / "pdks" / "interposer.lyp"
    if not lyp_path.exists():
        pytest.skip("interposer.lyp not found")
    return LYPParser(str(lyp_path))


class TestFullPipeline:
    def test_basic_conversion(self, tmp_path, interposer_lyp):
        """Full pipeline: GDS -> extract -> layout -> write"""
        pad_names = ["VDD", "GND", "CLK", "DATA", "RST"]
        gds_path = _create_test_gds(tmp_path / "basic.gds", pad_names)

        extractor = PinExtractor(interposer_lyp)
        pads, cell_name = extractor.extract_named_pads(
            str(gds_path), "TopMetal2.drawing"
        )

        assert len(pads) == 5
        assert cell_name == "INTEGRATION_TEST"
        assert all(p.name is not None for p in pads)

        sym = create_default_layout(pads, cell_name)
        assert len(sym.pins) == 5

        output_path = str(tmp_path / "basic.kicad_sym")
        writer = KiCadSymWriter()
        writer.write_symbol_library([sym], output_path)

        content = Path(output_path).read_text()
        assert "(kicad_symbol_lib" in content
        assert content.count("(pin ") == 5

        # Balanced parens
        assert content.count('(') == content.count(')')

    def test_many_pins(self, tmp_path, interposer_lyp):
        """Handle a symbol with many pins"""
        pad_names = [f"PIN_{i:03d}" for i in range(50)]
        gds_path = _create_test_gds(tmp_path / "many.gds", pad_names)

        extractor = PinExtractor(interposer_lyp)
        pads, cell_name = extractor.extract_named_pads(
            str(gds_path), "TopMetal2.drawing"
        )

        sym = create_default_layout(pads, cell_name)
        assert len(sym.pins) == 50

        output_path = str(tmp_path / "many.kicad_sym")
        writer = KiCadSymWriter()
        writer.write_symbol_library([sym], output_path)

        content = Path(output_path).read_text()
        assert content.count("(pin ") == 50
        assert content.count('(') == content.count(')')

    def test_power_pin_classification(self, tmp_path, interposer_lyp):
        """Power pins should end up on correct sides"""
        pad_names = ["VDD", "VDDA", "GND", "VSSA", "SIG1", "SIG2"]
        gds_path = _create_test_gds(tmp_path / "power.gds", pad_names)

        extractor = PinExtractor(interposer_lyp)
        pads, _ = extractor.extract_named_pads(
            str(gds_path), "TopMetal2.drawing"
        )

        sym = create_default_layout(pads, "POWER_TEST")
        counts = sym.pin_count_per_side()

        assert counts[PinSide.TOP] == 2      # VDD, VDDA
        assert counts[PinSide.BOTTOM] == 2   # GND, VSSA
        assert counts[PinSide.LEFT] + counts[PinSide.RIGHT] == 2  # SIG1, SIG2

    def test_pin_numbers_match_names(self, tmp_path, interposer_lyp):
        """Pin number must equal pin name for symbol-footprint traceability"""
        pad_names = ["VDD", "GND", "CLK"]
        gds_path = _create_test_gds(tmp_path / "match.gds", pad_names)

        extractor = PinExtractor(interposer_lyp)
        pads, _ = extractor.extract_named_pads(
            str(gds_path), "TopMetal2.drawing"
        )

        sym = create_default_layout(pads, "MATCH_TEST")

        for pin in sym.pins:
            assert pin.name == pin.number, \
                f"Pin name '{pin.name}' != number '{pin.number}'"

    def test_no_text_layers(self, tmp_path, interposer_lyp):
        """If no text layers contain data, pads get sequential numbers"""
        layout = db.Layout()
        top_cell = layout.create_cell("NO_TEXT")
        pl = layout.layer(134, 0)
        top_cell.shapes(pl).insert(db.Box(0, 0, 100000, 100000))
        top_cell.shapes(pl).insert(db.Box(200000, 0, 300000, 100000))

        gds_path = str(tmp_path / "notext.gds")
        layout.write(gds_path)

        extractor = PinExtractor(interposer_lyp)
        pads, _ = extractor.extract_named_pads(gds_path, "TopMetal2.drawing")

        assert len(pads) == 2
        # Without text, names are None -> layout uses sequential numbers
        sym = create_default_layout(pads, "NO_TEXT")
        pin_names = {p.name for p in sym.pins}
        assert "1" in pin_names
        assert "2" in pin_names


class TestCLI:
    def test_generate_test_gds(self):
        """CLI --generate-test-gds should work"""
        result = subprocess.run(
            [sys.executable, "gds_to_kicad_symbol.py", "--generate-test-gds"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        assert result.returncode == 0
        assert "Generated" in result.stdout

    def test_list_layers(self):
        """CLI --list-layers should work"""
        result = subprocess.run(
            [sys.executable, "gds_to_kicad_symbol.py",
             "--lyp-file", "pdks/interposer.lyp", "--list-layers"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        assert result.returncode == 0
        assert "TopMetal2.drawing" in result.stdout

    def test_full_conversion(self, tmp_path):
        """CLI full conversion pipeline"""
        # First generate test GDS
        subprocess.run(
            [sys.executable, "gds_to_kicad_symbol.py", "--generate-test-gds"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
        )

        output = str(tmp_path / "output.kicad_sym")
        result = subprocess.run(
            [sys.executable, "gds_to_kicad_symbol.py",
             "tests/test_symbol.gds",
             "--lyp-file", "pdks/sg13g2.lyp",
             "--pad-layer", "TopMetal2.drawing",
             "-o", output],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        assert result.returncode == 0
        assert Path(output).exists()

        content = Path(output).read_text()
        assert "(kicad_symbol_lib" in content
        assert content.count('(') == content.count(')')


class TestPinListWorkflow:
    """Integration tests for the human-in-the-loop pin list workflow."""

    def test_extract_to_pin_list_to_symbol(self, tmp_path, interposer_lyp):
        """Full workflow: GDS -> pin list -> (edit) -> symbol"""
        pad_names = ["VDD", "GND", "CLK", "DATA", "RST"]
        gds_path = _create_test_gds(tmp_path / "workflow.gds", pad_names)

        # Step 1: Extract named pads
        extractor = PinExtractor(interposer_lyp)
        pads, cell_name = extractor.extract_named_pads(
            str(gds_path), "TopMetal2.drawing"
        )
        assert len(pads) == 5

        # Step 2: Create pin list
        pin_list = PinList.from_extracted_pads(
            pads, chiplet_name=cell_name,
            gds_source="workflow.gds",
            pad_layer="TopMetal2.drawing",
        )
        assert len(pin_list) == 5

        # Step 3: Save and reload (simulates user editing)
        json_path = str(tmp_path / "pins.json")
        pin_list.save(json_path)
        pin_list_loaded = PinList.load(json_path)

        # Step 4: User renames a pin
        for pin in pin_list_loaded.pins:
            if pin.name == "DATA":
                pin.name = "SPI_MOSI"
                pin.type = "output"
                pin.side = "right"

        # Step 5: Generate symbol from edited pin list
        symbol = create_layout_from_pin_list(pin_list_loaded, cell_name)
        assert len(symbol.pins) == 5

        # Verify the rename was respected
        pin_names = {p.name for p in symbol.pins}
        assert "SPI_MOSI" in pin_names
        assert "DATA" not in pin_names

        # Verify edited type/side
        mosi_pin = next(p for p in symbol.pins if p.name == "SPI_MOSI")
        assert mosi_pin.side == PinSide.RIGHT
        assert mosi_pin.pin_type.value == "output"

        # Step 6: Write symbol
        output = str(tmp_path / "workflow.kicad_sym")
        writer = KiCadSymWriter()
        writer.write_symbol_library([symbol], output)

        content = Path(output).read_text()
        assert '"SPI_MOSI"' in content
        assert content.count("(pin ") == 5
        assert content.count('(') == content.count(')')

    def test_pin_list_preserves_power_classification(self, tmp_path, interposer_lyp):
        """Pin list from_extracted_pads should correctly classify power pins."""
        pad_names = ["VDD", "VDDA", "GND", "VSS", "SIG"]
        gds_path = _create_test_gds(tmp_path / "power.gds", pad_names)

        extractor = PinExtractor(interposer_lyp)
        pads, cell_name = extractor.extract_named_pads(
            str(gds_path), "TopMetal2.drawing"
        )

        pin_list = PinList.from_extracted_pads(pads, chiplet_name=cell_name)

        # Check types and sides
        pin_map = {p.name: p for p in pin_list.pins}
        assert pin_map["VDD"].type == "power_in"
        assert pin_map["VDD"].side == "top"
        assert pin_map["GND"].type == "power_in"
        assert pin_map["GND"].side == "bottom"
        assert pin_map["SIG"].type == "passive"

        # Generate symbol -- types/sides from pin list should be used
        symbol = create_layout_from_pin_list(pin_list, cell_name)
        sym_map = {p.name: p for p in symbol.pins}
        assert sym_map["VDD"].side == PinSide.TOP
        assert sym_map["GND"].side == PinSide.BOTTOM

    def test_deduplicate_then_generate(self, tmp_path, interposer_lyp):
        """Duplicated power net names should be deduplicated before symbol gen."""
        # Simulate repeated VDD labels (common in real GDS)
        pad_names = ["VDD", "VDD", "VDD", "GND", "SIG"]
        gds_path = _create_test_gds(tmp_path / "dup.gds", pad_names)

        extractor = PinExtractor(interposer_lyp)
        pads, cell_name = extractor.extract_named_pads(
            str(gds_path), "TopMetal2.drawing"
        )

        pin_list = PinList.from_extracted_pads(pads, chiplet_name=cell_name)

        # Validate finds duplicates
        warnings = pin_list.validate()
        assert any("duplicate" in w.lower() for w in warnings)

        # Deduplicate
        pin_list.deduplicate_names()
        warnings = pin_list.validate()
        assert not any("duplicate" in w.lower() for w in warnings)

        names = [p.name for p in pin_list.pins]
        assert "VDD" in names
        assert "VDD_1" in names
        assert "VDD_2" in names

        # Generate symbol
        symbol = create_layout_from_pin_list(pin_list, cell_name)
        assert len(symbol.pins) == 5
        sym_names = {p.name for p in symbol.pins}
        assert "VDD" in sym_names
        assert "VDD_1" in sym_names


class TestPadReviewWorkflow:
    """Integration tests for pad review GDS generation and read-back."""

    def test_full_pad_review_workflow(self, tmp_path, interposer_lyp):
        """GDS -> pin list -> pad review GDS -> (edit) -> footprint-ready pads"""
        pad_names = ["VDD", "GND", "CLK", "DATA"]
        gds_path = _create_test_gds(tmp_path / "review.gds", pad_names)

        # Step 1: Extract
        extractor = PinExtractor(interposer_lyp)
        pads, cell_name = extractor.extract_named_pads(
            str(gds_path), "TopMetal2.drawing"
        )

        # Step 2: Pin list
        pin_list = PinList.from_extracted_pads(pads, chiplet_name=cell_name)

        # Step 3: Generate pad review GDS
        inter_path = str(tmp_path / "pad_review.gds")
        pad_layer = interposer_lyp.get_layer("TopMetal2.drawing")
        text_layer = interposer_lyp.get_layer("TopMetal2.text")

        count = PadReview.generate(
            str(gds_path), inter_path,
            pad_layer=pad_layer,
            text_layer=text_layer,
            pin_list=pin_list,
        )
        assert count == 4  # 4 pads, no routing in test GDS

        # Step 4: Read back (no edits -- simulates user kept all pads)
        result_pads = PadReview.read_edited_pads(
            inter_path, pad_layer, pin_list=pin_list
        )
        assert len(result_pads) == 4

        # All pads should have names from pin list
        names = {p["name"] for p in result_pads}
        assert names == {"VDD", "GND", "CLK", "DATA"}

    def test_pad_review_with_deleted_pads(self, tmp_path, interposer_lyp):
        """After user deletes a pad from pad review, read-back reflects it."""
        pad_names = ["VDD", "GND", "CLK"]
        gds_path = _create_test_gds(tmp_path / "del.gds", pad_names)

        extractor = PinExtractor(interposer_lyp)
        pads, cell_name = extractor.extract_named_pads(
            str(gds_path), "TopMetal2.drawing"
        )
        pin_list = PinList.from_extracted_pads(pads, chiplet_name=cell_name)

        # Generate pad review
        inter_path = str(tmp_path / "review_del.gds")
        pad_layer = interposer_lyp.get_layer("TopMetal2.drawing")
        PadReview.generate(
            str(gds_path), inter_path,
            pad_layer=pad_layer, pin_list=pin_list,
        )

        # Simulate user deleting one pad: create edited GDS with 2 pads
        edited_layout = db.Layout()
        edited_cell = edited_layout.create_cell(cell_name)
        edited_pl = edited_layout.layer(*pad_layer)
        # Keep only VDD and GND pads (approximate positions)
        edited_cell.shapes(edited_pl).insert(db.Box(0, 0, 80000, 80000))
        edited_cell.shapes(edited_pl).insert(db.Box(200000, 0, 280000, 80000))

        edited_path = str(tmp_path / "edited.gds")
        edited_layout.write(edited_path)

        # Read back
        result = PadReview.read_edited_pads(
            edited_path, pad_layer, pin_list=pin_list
        )
        assert len(result) == 2
        names = {p["name"] for p in result}
        assert len(names) == 2  # should have 2 distinct names

    def test_pin_list_to_symbol_and_pad_review(self, tmp_path, interposer_lyp):
        """Verify symbol and footprint pads use same pin names from pin list."""
        pad_names = ["VDD", "GND", "IO_A", "IO_B"]
        gds_path = _create_test_gds(tmp_path / "cross.gds", pad_names)

        extractor = PinExtractor(interposer_lyp)
        pads, cell_name = extractor.extract_named_pads(
            str(gds_path), "TopMetal2.drawing"
        )

        pin_list = PinList.from_extracted_pads(pads, chiplet_name=cell_name)

        # Generate symbol
        symbol = create_layout_from_pin_list(pin_list, cell_name)
        sym_pin_names = {p.name for p in symbol.pins}

        # Generate pad review + read back
        inter_path = str(tmp_path / "cross_review.gds")
        pad_layer = interposer_lyp.get_layer("TopMetal2.drawing")
        PadReview.generate(
            str(gds_path), inter_path,
            pad_layer=pad_layer, pin_list=pin_list,
        )
        fp_pads = PadReview.read_edited_pads(
            inter_path, pad_layer, pin_list=pin_list
        )
        fp_pad_names = {p["name"] for p in fp_pads}

        # Symbol pin names and footprint pad names must match
        assert sym_pin_names == fp_pad_names


class TestCLIPinListWorkflow:
    """Test the new CLI flags for pin list extraction and generation."""

    def test_extract_pins_cli(self, tmp_path):
        """CLI --extract-pins should produce valid JSON."""
        # Generate test GDS first
        subprocess.run(
            [sys.executable, "gds_to_kicad_symbol.py", "--generate-test-gds"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
        )

        json_out = str(tmp_path / "extracted.json")
        result = subprocess.run(
            [sys.executable, "gds_to_kicad_symbol.py",
             "tests/test_symbol.gds",
             "--lyp-file", "pdks/sg13g2.lyp",
             "--pad-layer", "TopMetal2.drawing",
             "--extract-pins", json_out],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert Path(json_out).exists()

        # Validate JSON structure
        with open(json_out) as f:
            data = json.load(f)
        assert "pins" in data
        assert "version" in data
        assert len(data["pins"]) == 20  # test GDS has 20 pads
        assert all("name" in p for p in data["pins"])

    def test_from_pin_list_cli(self, tmp_path):
        """CLI --from-pin-list should generate .kicad_sym."""
        # Create a pin list JSON manually
        pin_list = PinList(
            pins=[
                PinEntry(name="VDD", type="power_in", side="top"),
                PinEntry(name="GND", type="power_in", side="bottom"),
                PinEntry(name="CLK", type="input", side="left"),
            ],
            metadata={
                "version": 1,
                "chiplet_name": "CLI_TEST",
                "gds_source": "",
                "lyp_file": "",
                "pad_layer": "",
                "text_layers": [],
                "timestamp": "2026-02-20T00:00:00",
            }
        )
        json_path = str(tmp_path / "cli_pins.json")
        pin_list.save(json_path)

        sym_out = str(tmp_path / "cli_test.kicad_sym")
        result = subprocess.run(
            [sys.executable, "gds_to_kicad_symbol.py",
             "--from-pin-list", json_path,
             "-o", sym_out],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        assert Path(sym_out).exists()

        content = Path(sym_out).read_text()
        assert "(kicad_symbol_lib" in content
        assert content.count("(pin ") == 3
        assert '"VDD"' in content
        assert '"GND"' in content
        assert '"CLK"' in content
        assert content.count('(') == content.count(')')

    def test_extract_then_generate_roundtrip(self, tmp_path):
        """Extract pins from GDS, then generate symbol from the JSON."""
        subprocess.run(
            [sys.executable, "gds_to_kicad_symbol.py", "--generate-test-gds"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
        )

        json_out = str(tmp_path / "roundtrip.json")
        subprocess.run(
            [sys.executable, "gds_to_kicad_symbol.py",
             "tests/test_symbol.gds",
             "--lyp-file", "pdks/sg13g2.lyp",
             "--pad-layer", "TopMetal2.drawing",
             "--extract-pins", json_out],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        assert Path(json_out).exists()

        sym_out = str(tmp_path / "roundtrip.kicad_sym")
        result = subprocess.run(
            [sys.executable, "gds_to_kicad_symbol.py",
             "--from-pin-list", json_out,
             "-o", sym_out],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        assert result.returncode == 0
        assert Path(sym_out).exists()

        content = Path(sym_out).read_text()
        assert content.count("(pin ") == 20
