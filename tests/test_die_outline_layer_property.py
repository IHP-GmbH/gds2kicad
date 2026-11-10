# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the DIE_OUTLINE_LAYER footprint property.

With --die-outline-layer the footprint records WHICH GDS layer holds the die
outline, as a bare "L/D" string. A consumer that sizes the die reads that layer
from the die GDS itself, in the die-local frame, instead of trusting KiCad
geometry that can be edited or stroke-inflated. It follows the DIE_THICKNESS_UM
idiom: written only when known (here: when the option was given and the outline
on that layer passed every refusal), never defaulted, and placed on the same
footprint as GDS_FILE so a reader never finds one without the other.

Scope note: synthetic layouts built with klayout.db; no real die, no KiCad, no
pcbnew, no export.
"""

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import klayout.db as db

import sexpr_io as sx
from lyp_parser import LYPParser
from gds_to_kicad import GDSToKiCad
from pin_list import PinList, PinEntry

PAD_LAYER = (205, 0)
SCRIPT = Path(__file__).resolve().parents[1] / "gds_to_kicad.py"

# 1 mm die, pad ring inset 65 um; same geometry as the courtyard tests.
PADS_UM = [
    (65.0, 65.0, 135.0, 135.0),
    (865.0, 65.0, 935.0, 135.0),
    (65.0, 865.0, 135.0, 935.0),
    (865.0, 865.0, 935.0, 935.0),
]
OUTLINE_UM = (0.0, 0.0, 1000.0, 1000.0)


@pytest.fixture
def generic_lyp():
    lyp_path = Path(__file__).parent.parent / "pdks" / "generic.lyp"
    if not lyp_path.exists():
        pytest.skip("pdks/generic.lyp not available")
    return LYPParser(str(lyp_path))


def _um(value):
    return int(round(value * 1000))


def _write_gds(path, *, outline_layer=(206, 0), with_outline=True):
    """Synthetic die with pads on 205/0 and the outline on `outline_layer`."""
    layout = db.Layout()
    layout.dbu = 0.001
    cell = layout.create_cell("CF_TEST")
    pad_index = layout.layer(*PAD_LAYER)
    for x0, y0, x1, y1 in PADS_UM:
        cell.shapes(pad_index).insert(
            db.Box(_um(x0), _um(y0), _um(x1), _um(y1)))
    if with_outline:
        x0, y0, x1, y1 = OUTLINE_UM
        cell.shapes(layout.layer(*outline_layer)).insert(
            db.Box(_um(x0), _um(y0), _um(x1), _um(y1)))
    layout.write(str(path))
    return path


def _pin_list():
    return PinList(
        pins=[PinEntry(name="P%d" % i, type="passive", side="left",
                       pad_index=i,
                       center_x_dbu=(x0 + x1) / 2 * 1000,
                       center_y_dbu=(y0 + y1) / 2 * 1000,
                       width_dbu=(x1 - x0) * 1000,
                       height_dbu=(y1 - y0) * 1000)
              for i, (x0, y0, x1, y1) in enumerate(PADS_UM)],
        metadata={"chiplet_name": "CF_TEST"})


def _footprint_properties(path):
    """Top-level (property "K" "V") children of the footprint, as a dict.

    Parsed, not regex-matched, so a property that ended up nested in a pad or a
    graphic (where a field reader would never look) does not count.
    """
    tree = sx.parse(Path(path).read_text())
    root = tree[0] if tree and isinstance(tree[0], list) else tree
    assert sx.head(root) == "footprint"
    props = {}
    for child in sx.find_all(root, "property"):
        key, value = str(child[1]), str(child[2])
        assert key not in props, "property %s written twice" % key
        props[key] = value
    return props


def _convert(tmp_path, lyp, *, outline_layer=(206, 0), die_outline_layer=None,
             flip_chip=False, with_outline=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    gds = _write_gds(tmp_path / "die.gds", outline_layer=outline_layer,
                     with_outline=with_outline)
    out = tmp_path / "die.kicad_mod"
    GDSToKiCad(lyp, pad_layer=PAD_LAYER,
               die_outline_layer=die_outline_layer).convert(
                   str(gds), str(out), flip_chip=flip_chip)
    return out


def _write_librelane_like_gds(path, *, outline_layer=(189, 4)):
    """Die whose outline layer also holds property-tagged stdcell boundaries.

    Mirrors a real LibreLane SG13G2 die: one top-cell prBoundary box for the
    die, plus a child cell per stdcell whose own prBoundary box carries the GDS
    property 126 = 'oaBoundary:pr'. A property-aware merge keeps those apart
    from the die box; the outline read must not.
    """
    layout = db.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("CF_LIBRELANE")
    pad_index = layout.layer(*PAD_LAYER)
    for x0, y0, x1, y1 in PADS_UM:
        top.shapes(pad_index).insert(
            db.Box(_um(x0), _um(y0), _um(x1), _um(y1)))
    outline_index = layout.layer(*outline_layer)
    x0, y0, x1, y1 = OUTLINE_UM
    top.shapes(outline_index).insert(
        db.Box(_um(x0), _um(y0), _um(x1), _um(y1)))
    stdcell = layout.create_cell("sg13g2_inv_1")
    prop_id = layout.properties_id([[126, "oaBoundary:pr"]])
    stdcell.shapes(outline_index).insert(db.Box(0, 0, _um(2.0), _um(3.78)),
                                         prop_id)
    for i in range(3):
        top.insert(db.CellInstArray(
            stdcell.cell_index(),
            db.Trans(db.Point(_um(300.0 + 50.0 * i), _um(400.0)))))
    layout.write(str(path))
    return path


def _write_deep_outline_gds(path):
    """Outline only in a grandchild cell (seal ring inside a frame cell)."""
    layout = db.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("CF_DEEP")
    frame = layout.create_cell("frame")
    seal = layout.create_cell("sealring")
    pad_index = layout.layer(*PAD_LAYER)
    for x0, y0, x1, y1 in PADS_UM:
        top.shapes(pad_index).insert(
            db.Box(_um(x0), _um(y0), _um(x1), _um(y1)))
    x0, y0, x1, y1 = OUTLINE_UM
    seal.shapes(layout.layer(39, 4)).insert(
        db.Box(_um(x0), _um(y0), _um(x1), _um(y1)))
    frame.insert(db.CellInstArray(seal.cell_index(), db.Trans()))
    top.insert(db.CellInstArray(frame.cell_index(), db.Trans()))
    layout.write(str(path))
    return path


class TestReadRule:
    """The producer reads the outline the way a consumer reads the die GDS:
    hierarchically, merged, property-blind, exactly one rectangle."""

    def test_property_tagged_stdcell_boundaries_merge_into_the_die(
            self, tmp_path, generic_lyp):
        gds = _write_librelane_like_gds(tmp_path / "die.gds")
        out = tmp_path / "die.kicad_mod"
        GDSToKiCad(generic_lyp, pad_layer=PAD_LAYER,
                   die_outline_layer=(189, 4)).convert(str(gds), str(out))
        assert _footprint_properties(out)["DIE_OUTLINE_LAYER"] == "189/4"

    def test_outline_in_a_grandchild_is_found_without_flattening(
            self, tmp_path, generic_lyp):
        """Pins the read itself, independent of any flatten done upstream."""
        gds = _write_deep_outline_gds(tmp_path / "die.gds")
        layout = db.Layout()
        layout.read(str(gds))
        converter = GDSToKiCad(generic_lyp, pad_layer=PAD_LAYER,
                               die_outline_layer=(39, 4))
        box = converter._extract_die_outline(layout, layout.top_cell())
        assert (box.width(), box.height()) == (_um(1000.0), _um(1000.0))


class TestDirectPath:
    def test_written_with_the_outline(self, tmp_path, generic_lyp):
        out = _convert(tmp_path, generic_lyp, die_outline_layer=(206, 0))
        assert _footprint_properties(out)["DIE_OUTLINE_LAYER"] == "206/0"

    def test_value_is_the_layer_named_not_a_constant(self, tmp_path,
                                                     generic_lyp):
        """A LibreLane-built SG13G2 die carries its outline on 189/4."""
        out = _convert(tmp_path, generic_lyp, outline_layer=(189, 4),
                       die_outline_layer=(189, 4))
        assert _footprint_properties(out)["DIE_OUTLINE_LAYER"] == "189/4"

    def test_absent_without_the_option(self, tmp_path, generic_lyp):
        """Outline present in the GDS but not named: nothing is declared."""
        out = _convert(tmp_path, generic_lyp)
        assert "DIE_OUTLINE_LAYER" not in _footprint_properties(out)

    def test_refusal_writes_no_footprint(self, tmp_path, generic_lyp):
        """Named but missing: refused before any file exists to carry it."""
        with pytest.raises(ValueError, match="not present in the layout"):
            _convert(tmp_path, generic_lyp, die_outline_layer=(206, 0),
                     with_outline=False)
        assert not (tmp_path / "die.kicad_mod").exists()

    def test_flip_chip_does_not_change_the_value(self, tmp_path, generic_lyp):
        """The layer is a GDS fact; mirroring is a KiCad-side transform."""
        out = _convert(tmp_path, generic_lyp, die_outline_layer=(206, 0),
                       flip_chip=True)
        assert _footprint_properties(out)["DIE_OUTLINE_LAYER"] == "206/0"

    def test_gds_file_names_the_gds_the_outline_was_read_from(self, tmp_path,
                                                              generic_lyp):
        """A consumer reads DIE_OUTLINE_LAYER from the file GDS_FILE names."""
        out = _convert(tmp_path, generic_lyp, die_outline_layer=(206, 0))
        props = _footprint_properties(out)
        assert props["DIE_OUTLINE_LAYER"] == "206/0"
        assert Path(props["GDS_FILE"]) == (tmp_path / "die.gds").resolve()

    def test_dbu_override_differing_from_the_file_is_refused(self, tmp_path,
                                                              generic_lyp):
        """--dbu would scale the footprint, the consumer reads the file DBU."""
        gds = _write_gds(tmp_path / "die.gds")
        out = tmp_path / "die.kicad_mod"
        with pytest.raises(ValueError, match="must use the file's DBU"):
            GDSToKiCad(generic_lyp, pad_layer=PAD_LAYER, dbu=0.0005,
                       die_outline_layer=(206, 0)).convert(str(gds), str(out))
        assert not out.exists()

    def test_zero_pads_with_an_outline_is_refused(self, tmp_path, generic_lyp):
        """An outline-only footprint would look validated and hold no die."""
        gds = _write_gds(tmp_path / "die.gds")
        out = tmp_path / "die.kicad_mod"
        with pytest.raises(ValueError, match="no pads found"):
            GDSToKiCad(generic_lyp, pad_layer=(999, 0),
                       die_outline_layer=(206, 0)).convert(str(gds), str(out))
        assert not out.exists()

    def test_coexists_with_die_thickness(self, tmp_path, generic_lyp):
        tmp_path.mkdir(parents=True, exist_ok=True)
        gds = _write_gds(tmp_path / "die.gds")
        out = tmp_path / "die.kicad_mod"
        GDSToKiCad(generic_lyp, pad_layer=PAD_LAYER,
                   die_outline_layer=(206, 0)).convert(
                       str(gds), str(out), die_thickness_um=300.0)
        props = _footprint_properties(out)
        assert props["DIE_OUTLINE_LAYER"] == "206/0"
        assert props["DIE_THICKNESS_UM"] == "300"

    def test_value_is_bare_layer_slash_datatype(self, tmp_path, generic_lyp):
        """The reader splits on '/': no spaces, no layer name, no suffix."""
        out = _convert(tmp_path, generic_lyp, die_outline_layer=(206, 0))
        value = _footprint_properties(out)["DIE_OUTLINE_LAYER"]
        assert re.fullmatch(r"\d+/\d+", value)


class TestPadReviewPath:
    def test_written_on_the_review_path(self, tmp_path, generic_lyp):
        gds = _write_gds(tmp_path / "review.gds")
        out = tmp_path / "review.kicad_mod"
        converter = GDSToKiCad(generic_lyp, pad_layer=PAD_LAYER,
                               die_outline_layer=(206, 0))
        assert converter.convert_from_pad_review(str(gds), _pin_list(),
                                                 str(out))
        assert _footprint_properties(out)["DIE_OUTLINE_LAYER"] == "206/0"

    def test_absent_on_the_review_path_without_the_option(self, tmp_path,
                                                          generic_lyp):
        gds = _write_gds(tmp_path / "review.gds")
        out = tmp_path / "review.kicad_mod"
        GDSToKiCad(generic_lyp, pad_layer=PAD_LAYER).convert_from_pad_review(
            str(gds), _pin_list(), str(out))
        assert "DIE_OUTLINE_LAYER" not in _footprint_properties(out)

    def test_generated_review_gds_carries_the_outline(self, tmp_path,
                                                      generic_lyp):
        """The real sequence: generate the review GDS, then convert it."""
        from pad_review import PadReview
        die = _write_gds(tmp_path / "die.gds")
        review = tmp_path / "review.gds"
        PadReview.generate(str(die), str(review), pad_layer=PAD_LAYER,
                           die_outline_layer=(206, 0))
        out = tmp_path / "review.kicad_mod"
        GDSToKiCad(generic_lyp, pad_layer=PAD_LAYER,
                   die_outline_layer=(206, 0)).convert_from_pad_review(
                       str(review), _pin_list(), str(out))
        assert _footprint_properties(out)["DIE_OUTLINE_LAYER"] == "206/0"

    def test_review_gds_without_the_option_drops_the_outline(self, tmp_path,
                                                             generic_lyp):
        """Unchanged default: without the option, generate copies pads only."""
        from pad_review import PadReview
        die = _write_gds(tmp_path / "die.gds")
        review = tmp_path / "review.gds"
        PadReview.generate(str(die), str(review), pad_layer=PAD_LAYER)
        layout = db.Layout()
        layout.read(str(review))
        present = {(layout.get_info(i).layer, layout.get_info(i).datatype)
                   for i in layout.layer_indices()}
        assert present == {PAD_LAYER}

    def test_generate_writes_one_validated_box(self, tmp_path, generic_lyp):
        """A real die has one boundary per stdcell; the review gets one box."""
        from pad_review import PadReview
        die = _write_librelane_like_gds(tmp_path / "die.gds")
        review = tmp_path / "review.gds"
        PadReview.generate(str(die), str(review), pad_layer=PAD_LAYER,
                           die_outline_layer=(189, 4))
        layout = db.Layout()
        layout.read(str(review))
        shapes = list(layout.top_cell().shapes(
            layout.find_layer(189, 4)).each())
        assert len(shapes) == 1
        assert shapes[0].bbox() == db.Box(0, 0, _um(1000.0), _um(1000.0))

    def test_generate_refuses_an_absent_outline(self, tmp_path, generic_lyp):
        """Refused before the manual edit, not after it."""
        from pad_review import PadReview
        die = _write_gds(tmp_path / "die.gds", with_outline=False)
        review = tmp_path / "review.gds"
        with pytest.raises(ValueError, match="not present in the layout"):
            PadReview.generate(str(die), str(review), pad_layer=PAD_LAYER,
                               die_outline_layer=(206, 0))
        assert not review.exists()

    def test_text_only_outline_layer_is_reported_empty(self, tmp_path,
                                                       generic_lyp):
        """Present with only a label: 'no shape', not 'merges to 0 shapes'."""
        layout = db.Layout()
        layout.dbu = 0.001
        cell = layout.create_cell("CF_TEXT")
        cell.shapes(layout.layer(206, 0)).insert(db.Text("die", db.Trans()))
        gds = tmp_path / "text.gds"
        layout.write(str(gds))
        read = db.Layout()
        read.read(str(gds))
        converter = GDSToKiCad(generic_lyp, pad_layer=PAD_LAYER,
                               die_outline_layer=(206, 0))
        with pytest.raises(ValueError, match="carries no shape"):
            converter._extract_die_outline(read, read.top_cell())

    def test_outline_is_read_from_the_gds_file_named(self, tmp_path,
                                                     generic_lyp):
        """gds_property_path set: the outline is checked in THAT file."""
        review = _write_gds(tmp_path / "review.gds", with_outline=False)
        full = _write_gds(tmp_path / "full.gds")
        out = tmp_path / "review.kicad_mod"
        GDSToKiCad(generic_lyp, pad_layer=PAD_LAYER,
                   die_outline_layer=(206, 0)).convert_from_pad_review(
                       str(review), _pin_list(), str(out),
                       gds_property_path=str(full))
        props = _footprint_properties(out)
        assert props["DIE_OUTLINE_LAYER"] == "206/0"
        assert Path(props["GDS_FILE"]) == full.resolve()

    def test_outline_only_in_the_review_gds_is_not_enough(self, tmp_path,
                                                          generic_lyp):
        """The property must never vouch for a file that lacks the outline."""
        review = _write_gds(tmp_path / "review.gds")
        full = _write_gds(tmp_path / "full.gds", with_outline=False)
        out = tmp_path / "review.kicad_mod"
        with pytest.raises(ValueError, match="not present in the layout"):
            GDSToKiCad(generic_lyp, pad_layer=PAD_LAYER,
                       die_outline_layer=(206, 0)).convert_from_pad_review(
                           str(review), _pin_list(), str(out),
                           gds_property_path=str(full))
        assert not out.exists()

    def test_review_outline_is_never_read_as_a_pad(self, tmp_path,
                                                   generic_lyp):
        """The tagged outline box is refused as a pad whatever the flags."""
        from pad_review import PadReview
        die = _write_gds(tmp_path / "die.gds")
        review = tmp_path / "review.gds"
        PadReview.generate(str(die), str(review), pad_layer=PAD_LAYER,
                           die_outline_layer=(206, 0))
        assert len(PadReview.read_edited_pads(str(review), PAD_LAYER)) == 4
        with pytest.raises(ValueError, match="holds the die outline box"):
            PadReview.read_edited_pads(str(review), (206, 0))

    def test_missing_outline_in_the_review_says_how_to_fix_it(self, tmp_path,
                                                              generic_lyp):
        from pad_review import PadReview
        die = _write_gds(tmp_path / "die.gds")
        review = tmp_path / "review.gds"
        PadReview.generate(str(die), str(review), pad_layer=PAD_LAYER)
        out = tmp_path / "review.kicad_mod"
        with pytest.raises(ValueError) as info:
            GDSToKiCad(generic_lyp, pad_layer=PAD_LAYER,
                       die_outline_layer=(206, 0)).convert_from_pad_review(
                           str(review), _pin_list(), str(out))
        message = str(info.value)
        assert str(review) in message
        assert "regenerate it with --generate-pad-review" in message
        assert ".." not in message
        assert not out.exists()

    def test_generate_keeps_the_source_dbu(self, tmp_path, generic_lyp):
        """Shapes are copied in DBU, so the review must keep the die's grid."""
        from pad_review import PadReview
        die = _write_gds(tmp_path / "die.gds")
        layout = db.Layout()
        layout.read(str(die))
        layout.dbu = 0.0005
        layout.write(str(die))
        review = tmp_path / "review.gds"
        PadReview.generate(str(die), str(review), pad_layer=PAD_LAYER,
                           die_outline_layer=(206, 0))
        read = db.Layout()
        read.read(str(review))
        assert read.dbu == pytest.approx(0.0005)

    def test_dbu_mismatch_with_the_gds_file_is_refused(self, tmp_path,
                                                       generic_lyp):
        """An outline in one grid and pads in another would compare nonsense."""
        review = _write_gds(tmp_path / "review.gds", with_outline=False)
        full = _write_gds(tmp_path / "full.gds")
        layout = db.Layout()
        layout.read(str(full))
        layout.dbu = 0.0005
        layout.write(str(full))
        out = tmp_path / "review.kicad_mod"
        with pytest.raises(ValueError, match="must share one DBU"):
            GDSToKiCad(generic_lyp, pad_layer=PAD_LAYER,
                       die_outline_layer=(206, 0)).convert_from_pad_review(
                           str(review), _pin_list(), str(out),
                           gds_property_path=str(full))
        assert not out.exists()


class TestCLI:
    """Through main(), where the option is parsed and normalised."""

    @staticmethod
    def _run(args):
        return subprocess.run([sys.executable, str(SCRIPT)] + args,
                              capture_output=True, text=True)

    def test_direct_path(self, tmp_path):
        gds = _write_gds(tmp_path / "die.gds")
        out = tmp_path / "die.kicad_mod"
        result = self._run([str(gds), "-o", str(out),
                            "--pad-layer-number", "205/0",
                            "--die-outline-layer", "206/0"])
        assert result.returncode == 0, result.stderr
        assert _footprint_properties(out)["DIE_OUTLINE_LAYER"] == "206/0"

    def test_bare_layer_spec_is_normalised(self, tmp_path):
        """'206' means 206/0 to the parser, so the property must say 206/0."""
        gds = _write_gds(tmp_path / "die.gds")
        out = tmp_path / "die.kicad_mod"
        result = self._run([str(gds), "-o", str(out),
                            "--pad-layer-number", "205/0",
                            "--die-outline-layer", "206"])
        assert result.returncode == 0, result.stderr
        assert _footprint_properties(out)["DIE_OUTLINE_LAYER"] == "206/0"

    def test_generate_then_review(self, tmp_path):
        """The documented pad-review workflow, end to end through main()."""
        die = _write_gds(tmp_path / "die.gds")
        review = tmp_path / "review.gds"
        gen = self._run([str(die), "--generate-pad-review", str(review),
                         "--pad-layer-number", "205/0",
                         "--die-outline-layer", "206/0"])
        assert gen.returncode == 0, gen.stderr
        pins = tmp_path / "pins.json"
        _pin_list().save(str(pins))
        out = tmp_path / "review.kicad_mod"
        result = self._run(["--from-pad-review", str(review),
                            "--pin-list", str(pins), "-o", str(out),
                            "--pad-layer-number", "205/0",
                            "--die-outline-layer", "206/0"])
        assert result.returncode == 0, result.stderr
        assert _footprint_properties(out)["DIE_OUTLINE_LAYER"] == "206/0"

    def test_auto_detect_never_takes_the_outline_as_pads(self, tmp_path):
        """generic.lyp ranks outline.drawing 206/0 above pad.drawing 205/0, so
        on a review GDS that carries the outline, auto-detect picks it. That
        must refuse, not exit 0 with the die outline as the only pad."""
        die = _write_gds(tmp_path / "die.gds")
        review = tmp_path / "review.gds"
        gen = self._run([str(die), "--generate-pad-review", str(review),
                         "--pad-layer-number", "205/0",
                         "--die-outline-layer", "206/0"])
        assert gen.returncode == 0, gen.stderr
        pins = tmp_path / "pins.json"
        _pin_list().save(str(pins))
        out = tmp_path / "review.kicad_mod"
        result = self._run(["--from-pad-review", str(review),
                            "--pin-list", str(pins), "-o", str(out),
                            "--die-outline-layer", "206/0"])
        assert result.returncode != 0
        assert "die outline layer" in result.stderr
        assert not out.exists()

    def test_explicit_pad_layer_equal_to_the_outline_is_refused(self,
                                                                tmp_path):
        gds = _write_gds(tmp_path / "die.gds")
        out = tmp_path / "die.kicad_mod"
        result = self._run([str(gds), "-o", str(out),
                            "--pad-layer-number", "206/0",
                            "--die-outline-layer", "206/0"])
        assert result.returncode != 0
        assert "die outline layer" in result.stderr
        assert not out.exists()

    def test_review_outline_refused_as_pads_without_the_flag(self, tmp_path):
        """Step 2 run without --die-outline-layer and without a pad layer:
        auto-detect ranks 206/0 first, and the tagged box is still refused."""
        die = _write_gds(tmp_path / "die.gds")
        review = tmp_path / "review.gds"
        gen = self._run([str(die), "--generate-pad-review", str(review),
                         "--pad-layer-number", "205/0",
                         "--die-outline-layer", "206/0"])
        assert gen.returncode == 0, gen.stderr
        pins = tmp_path / "pins.json"
        _pin_list().save(str(pins))
        out = tmp_path / "review.kicad_mod"
        result = self._run(["--from-pad-review", str(review),
                            "--pin-list", str(pins), "-o", str(out)])
        assert result.returncode != 0
        assert "holds the die outline box" in result.stderr
        assert not out.exists()

    def test_generate_refuses_auto_detected_outline_as_pads(self, tmp_path):
        """No texts on the die, so auto-detect ranks the outline first."""
        die = _write_gds(tmp_path / "die.gds")
        review = tmp_path / "review.gds"
        result = self._run([str(die), "--generate-pad-review", str(review),
                            "--die-outline-layer", "206/0"])
        assert result.returncode == 2
        assert "also --die-outline-layer" in result.stderr
        assert not review.exists()

    def test_direct_path_on_a_review_refuses_the_outline_as_pads(self,
                                                                 tmp_path):
        die = _write_gds(tmp_path / "die.gds")
        review = tmp_path / "review.gds"
        gen = self._run([str(die), "--generate-pad-review", str(review),
                         "--pad-layer-number", "205/0",
                         "--die-outline-layer", "206/0"])
        assert gen.returncode == 0, gen.stderr
        out = tmp_path / "direct.kicad_mod"
        result = self._run([str(review), "-o", str(out)])
        assert result.returncode != 0
        assert "holds the die outline box" in result.stderr
        assert not out.exists()

    @pytest.mark.parametrize("spec", ["", "206/0/1", "206/", "/0", "-1/0",
                                      "99999999999999999999/0", "206/65536"])
    def test_bad_spec_is_a_usage_error(self, tmp_path, spec):
        gds = _write_gds(tmp_path / "die.gds")
        out = tmp_path / "die.kicad_mod"
        result = self._run([str(gds), "-o", str(out),
                            "--pad-layer-number", "205/0",
                            "--die-outline-layer=" + spec])
        assert result.returncode == 2, result.stderr
        assert "Traceback" not in result.stderr
        assert not out.exists()

    def test_malformed_spec_is_a_usage_error(self, tmp_path):
        gds = _write_gds(tmp_path / "die.gds")
        result = self._run([str(gds), "--generate-pad-review",
                            str(tmp_path / "review.gds"),
                            "--pad-layer-number", "205/0",
                            "--die-outline-layer", "foo"])
        assert result.returncode == 2
        assert "Traceback" not in result.stderr

    def test_review_path(self, tmp_path):
        gds = _write_gds(tmp_path / "review.gds")
        pins = tmp_path / "pins.json"
        _pin_list().save(str(pins))
        out = tmp_path / "review.kicad_mod"
        result = self._run(["--from-pad-review", str(gds),
                            "--pin-list", str(pins), "-o", str(out),
                            "--pad-layer-number", "205/0",
                            "--die-outline-layer", "206/0"])
        assert result.returncode == 0, result.stderr
        assert _footprint_properties(out)["DIE_OUTLINE_LAYER"] == "206/0"
