# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for --die-outline-layer: the courtyard as the die outline.

Why this option exists. The courtyard is not decoration: chiplet-export reads
KiCad's courtyard cache bbox and writes it into the .chiplet as the die
``dimensions``, so the courtyard IS the die size every downstream consumer
believes. Drawing it from the pad bounding box makes that size the extent of the
pad ring, which for an inset ring is smaller than the die. This file pins both
behaviours: the legacy one, so it cannot drift, and the new one, so it stays
exact.

Scope note: these build geometry with klayout.db, so they execute native
geometry code on synthetic layouts. They open no real die, run no KiCad and no
pcbnew, and perform no export.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import klayout.db as db

from lyp_parser import LYPParser
from gds_to_kicad import GDSToKiCad

PAD_LAYER = (205, 0)
OUTLINE_LAYER = (206, 0)


@pytest.fixture
def generic_lyp():
    lyp_path = Path(__file__).parent.parent / "pdks" / "generic.lyp"
    if not lyp_path.exists():
        lyp_path = Path(__file__).parent.parent / "pdks" / "interposer.lyp"
    if not lyp_path.exists():
        pytest.skip("no lyp available")
    return LYPParser(str(lyp_path))


def _um(value):
    """um -> dbu, at the 1 nm dbu these layouts use."""
    return int(round(value * 1000))


def _write_gds(path, *, pads, outline=None, outline_shapes=None,
               cell_name="CF_TEST"):
    """Synthetic die: pad boxes on 205/0, optional outline on 206/0.

    pads: list of (x0, y0, x1, y1) in um.
    outline: a single (x0, y0, x1, y1) in um, written as one box.
    outline_shapes: explicit list of shapes, for the ambiguous/non-rectangular
        cases; overrides `outline`.
    """
    layout = db.Layout()
    layout.dbu = 0.001
    cell = layout.create_cell(cell_name)

    pad_index = layout.layer(*PAD_LAYER)
    for x0, y0, x1, y1 in pads:
        cell.shapes(pad_index).insert(
            db.Box(_um(x0), _um(y0), _um(x1), _um(y1)))

    if outline_shapes is not None:
        outline_index = layout.layer(*OUTLINE_LAYER)
        for shape in outline_shapes:
            cell.shapes(outline_index).insert(shape)
    elif outline is not None:
        x0, y0, x1, y1 = outline
        outline_index = layout.layer(*OUTLINE_LAYER)
        cell.shapes(outline_index).insert(
            db.Box(_um(x0), _um(y0), _um(x1), _um(y1)))

    layout.write(str(path))
    return path


def _courtyard(content):
    """(start_x, start_y, end_x, end_y, stroke) from a .kicad_mod, in mm."""
    match = re.search(
        r'\(fp_rect\s+\(start\s+([-\d.]+)\s+([-\d.]+)\)\s+'
        r'\(end\s+([-\d.]+)\s+([-\d.]+)\)\s*\n\s*'
        r'\(stroke\s+\(width\s+([\d.]+)\)',
        content)
    assert match is not None, "no courtyard fp_rect in the footprint"
    return tuple(float(match.group(i)) for i in range(1, 6))


def _convert(tmp_path, lyp, *, pads, outline=None, outline_shapes=None,
             die_outline_layer=None, flip_chip=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    gds = _write_gds(tmp_path / "die.gds", pads=pads, outline=outline,
                     outline_shapes=outline_shapes)
    out = tmp_path / "die.kicad_mod"
    converter = GDSToKiCad(lyp, pad_layer=PAD_LAYER,
                           die_outline_layer=die_outline_layer)
    converter.convert(str(gds), str(out), flip_chip=flip_chip)
    return out


# A 1 mm die with a 70 um pad ring inset 65 um from the edge: the case the
# option was written for. Pad hull spans 65..935, the die spans 0..1000.
ONE_MM_PADS = [
    (65.0, 65.0, 135.0, 135.0),
    (865.0, 65.0, 935.0, 135.0),
    (65.0, 865.0, 135.0, 935.0),
    (865.0, 865.0, 935.0, 935.0),
]
ONE_MM_OUTLINE = (0.0, 0.0, 1000.0, 1000.0)


class TestLegacyPathUnchanged:
    def test_omitted_option_keeps_the_pad_hull(self, tmp_path, generic_lyp):
        """No option: the courtyard is the pad bounding box, stroke 0.05."""
        out = _convert(tmp_path, generic_lyp, pads=ONE_MM_PADS,
                       outline=ONE_MM_OUTLINE)
        x0, y0, x1, y1, stroke = _courtyard(out.read_text())

        # 0.065..0.935 mm, Y negated. This is the pad hull, NOT the die.
        assert (x0, x1) == pytest.approx((0.065, 0.935), abs=1e-9)
        assert (y0, y1) == pytest.approx((-0.935, -0.065), abs=1e-9)
        assert stroke == 0.05

    def test_outline_present_but_unnamed_is_ignored(self, tmp_path, generic_lyp):
        """A 206/0 outline in the GDS changes nothing unless it is named.

        The option is the whole opt-in. An outline that starts being honoured
        because it happens to be in the file would silently move the die size of
        every existing project.
        """
        with_outline = _convert(tmp_path / "a", generic_lyp, pads=ONE_MM_PADS,
                                outline=ONE_MM_OUTLINE)
        without = _convert(tmp_path / "b", generic_lyp, pads=ONE_MM_PADS)
        assert _courtyard(with_outline.read_text()) == \
            _courtyard(without.read_text())


class TestOutlinePath:
    def test_courtyard_is_the_die_outline(self, tmp_path, generic_lyp):
        """The whole point: 1.000000 mm, not the 0.870 mm pad hull."""
        out = _convert(tmp_path, generic_lyp, pads=ONE_MM_PADS,
                       outline=ONE_MM_OUTLINE, die_outline_layer=OUTLINE_LAYER)
        x0, y0, x1, y1, stroke = _courtyard(out.read_text())

        assert (x0, x1) == pytest.approx((0.0, 1.0), abs=1e-9)
        assert (y0, y1) == pytest.approx((-1.0, 0.0), abs=1e-9)
        assert x1 - x0 == pytest.approx(1.0, abs=1e-9)
        assert y1 - y0 == pytest.approx(1.0, abs=1e-9)

    def test_stroke_is_five_micron(self, tmp_path, generic_lyp):
        """0.005, not 0.05.

        This is compatibility with the pinned KiCad runtime, not a claim about
        the courtyard cache in general: at this stroke the cache's outset and
        its bbox inset cancel, so the .chiplet dimensions come back equal to the
        rectangle drawn here. The derivation is in the comment where
        gds_to_kicad.py writes the stroke; this test does not observe a KiCad.
        """
        out = _convert(tmp_path, generic_lyp, pads=ONE_MM_PADS,
                       outline=ONE_MM_OUTLINE, die_outline_layer=OUTLINE_LAYER)
        assert _courtyard(out.read_text())[4] == 0.005

    def test_offset_die_keeps_its_origin(self, tmp_path, generic_lyp):
        """The outline is used as drawn; it is not re-centred on the pads."""
        out = _convert(tmp_path, generic_lyp,
                       pads=[(120.0, 120.0, 190.0, 190.0)],
                       outline=(100.0, 50.0, 400.0, 250.0),
                       die_outline_layer=OUTLINE_LAYER)
        x0, y0, x1, y1, _ = _courtyard(out.read_text())
        assert (x0, x1) == pytest.approx((0.1, 0.4), abs=1e-9)
        assert (y0, y1) == pytest.approx((-0.25, -0.05), abs=1e-9)


class TestFlipChipTransform:
    def test_outline_mirrors_with_the_pads(self, tmp_path, generic_lyp):
        """Same mx and same Y-down convention as the pad writer."""
        out = _convert(tmp_path, generic_lyp,
                       pads=[(120.0, 120.0, 190.0, 190.0)],
                       outline=(100.0, 50.0, 400.0, 250.0),
                       die_outline_layer=OUTLINE_LAYER, flip_chip=True)
        x0, y0, x1, y1, _ = _courtyard(out.read_text())

        # X mirrored: 100..400 -> -400..-100. Y negated as always.
        assert (x0, x1) == pytest.approx((-0.4, -0.1), abs=1e-9)
        assert (y0, y1) == pytest.approx((-0.25, -0.05), abs=1e-9)

    def test_mirrored_outline_still_brackets_every_pad(self, tmp_path,
                                                      generic_lyp):
        """Containment must survive the transform, not just hold before it."""
        out = _convert(tmp_path, generic_lyp, pads=ONE_MM_PADS,
                       outline=ONE_MM_OUTLINE, die_outline_layer=OUTLINE_LAYER,
                       flip_chip=True)
        content = out.read_text()
        x0, y0, x1, y1, _ = _courtyard(content)

        centres = [(float(m.group(1)), float(m.group(2))) for m in re.finditer(
            r'\(pad\s+"[^"]*"\s+smd\s+\w+\s+\(at\s+([-\d.]+)\s+([-\d.]+)\)',
            content)]
        assert centres, "no pads in the footprint"
        for cx, cy in centres:
            assert x0 - 1e-9 <= cx <= x1 + 1e-9
            assert y0 - 1e-9 <= cy <= y1 + 1e-9

    def test_face_up_and_flip_chip_agree_on_width(self, tmp_path, generic_lyp):
        face_up = _courtyard(_convert(
            tmp_path / "u", generic_lyp, pads=ONE_MM_PADS,
            outline=ONE_MM_OUTLINE, die_outline_layer=OUTLINE_LAYER).read_text())
        flipped = _courtyard(_convert(
            tmp_path / "f", generic_lyp, pads=ONE_MM_PADS,
            outline=ONE_MM_OUTLINE, die_outline_layer=OUTLINE_LAYER,
            flip_chip=True).read_text())
        assert face_up[2] - face_up[0] == pytest.approx(flipped[2] - flipped[0],
                                                        abs=1e-9)


class TestRefusals:
    """Every one of these must raise BEFORE the footprint is written.

    Naming the layer asserts the outline is there. Recovering quietly from a
    broken assertion would produce a file that reads exactly like a correct one
    and means a different die size, which is the failure this option exists to
    remove.
    """

    def _expect_refusal(self, tmp_path, lyp, match, **kwargs):
        gds = _write_gds(tmp_path / "die.gds", **kwargs)
        out = tmp_path / "die.kicad_mod"
        converter = GDSToKiCad(lyp, pad_layer=PAD_LAYER,
                               die_outline_layer=OUTLINE_LAYER)
        with pytest.raises(ValueError, match=match):
            converter.convert(str(gds), str(out))
        assert not out.exists(), "a refusal must not leave a footprint behind"

    def test_layer_absent(self, tmp_path, generic_lyp):
        self._expect_refusal(tmp_path, generic_lyp, "not present in the layout",
                             pads=ONE_MM_PADS)

    def test_layer_present_but_empty(self, generic_lyp):
        """Distinct from absent, and it must say so.

        Measured while writing this: an empty layer does NOT survive a GDS round
        trip, so this state cannot be reached through a file and the branch is
        defensive. It is still worth holding, because the layer can be present
        and empty in an in-memory layout, which is how the GUIs call this. The
        test therefore drives the extractor directly rather than pretending a
        written GDS can produce the condition.

        find_layer vs layer matters here for the OTHER case: layout.layer() would
        CREATE 206/0 on lookup and report it empty, making a layer that was never
        in the file indistinguishable from one that was there and empty. Both
        messages exist so those two stay apart.
        """
        layout = db.Layout()
        layout.dbu = 0.001
        cell = layout.create_cell("CF_TEST")
        layout.layer(*OUTLINE_LAYER)  # present, no shapes

        converter = GDSToKiCad(generic_lyp, pad_layer=PAD_LAYER,
                               die_outline_layer=OUTLINE_LAYER)
        with pytest.raises(ValueError, match="carries no shape"):
            converter._extract_die_outline(layout, cell)

    def test_absent_and_empty_are_different_messages(self, tmp_path,
                                                     generic_lyp):
        """A GDS with no 206/0 at all reports absent, not empty."""
        self._expect_refusal(tmp_path, generic_lyp, "not present in the layout",
                             pads=ONE_MM_PADS, outline_shapes=[])

    def test_two_disjoint_rectangles(self, tmp_path, generic_lyp):
        self._expect_refusal(
            tmp_path, generic_lyp, "merges to 2 separate shapes",
            pads=ONE_MM_PADS,
            outline_shapes=[db.Box(0, 0, _um(400), _um(400)),
                            db.Box(_um(600), 0, _um(1000), _um(400))])

    def test_non_rectangular_outline(self, tmp_path, generic_lyp):
        """An L shape has no single width and height."""
        el = db.Polygon([db.Point(0, 0), db.Point(_um(1000), 0),
                         db.Point(_um(1000), _um(400)),
                         db.Point(_um(400), _um(400)),
                         db.Point(_um(400), _um(1000)),
                         db.Point(0, _um(1000))])
        self._expect_refusal(tmp_path, generic_lyp, "non-rectangular",
                             pads=[(10.0, 10.0, 80.0, 80.0)],
                             outline_shapes=[el])

    def test_pad_outside_the_outline(self, tmp_path, generic_lyp):
        self._expect_refusal(
            tmp_path, generic_lyp, "fall outside the die outline",
            pads=ONE_MM_PADS + [(1010.0, 100.0, 1080.0, 170.0)],
            outline=ONE_MM_OUTLINE)

    def test_touching_pad_is_not_outside(self, tmp_path, generic_lyp):
        """A pad flush with the die edge is contained, not a refusal."""
        out = _convert(tmp_path, generic_lyp,
                       pads=[(0.0, 0.0, 70.0, 70.0)],
                       outline=ONE_MM_OUTLINE, die_outline_layer=OUTLINE_LAYER)
        assert out.exists()

    def test_two_abutting_rectangles_merge_to_one(self, tmp_path, generic_lyp):
        """Merge first, then judge: abutting halves are one rectangle."""
        out = _write_gds(tmp_path / "die.gds", pads=ONE_MM_PADS,
                         outline_shapes=[db.Box(0, 0, _um(500), _um(1000)),
                                         db.Box(_um(500), 0, _um(1000),
                                                _um(1000))])
        mod = tmp_path / "die.kicad_mod"
        GDSToKiCad(generic_lyp, pad_layer=PAD_LAYER,
                   die_outline_layer=OUTLINE_LAYER).convert(str(out), str(mod))
        x0, _, x1, _, _ = _courtyard(mod.read_text())
        assert x1 - x0 == pytest.approx(1.0, abs=1e-9)


class TestLegacyBytesUnchanged:
    """Whole-file comparison, not just the courtyard.

    The first version of these tests compared the fp_rect alone, which would
    have missed the option perturbing anything else in the footprint. The scope
    is exact: with the option omitted, the two runs differ in NO byte. The
    generator writes no timestamp and resolves GDS_FILE from the input path, so
    two runs over the same input are byte-reproducible and this comparison is
    meaningful rather than incidentally true.
    """

    def test_whole_footprint_is_byte_identical(self, tmp_path, generic_lyp):
        """Outline in the GDS, option omitted: not one byte moves."""
        with_outline = _convert(tmp_path / "a", generic_lyp, pads=ONE_MM_PADS,
                                outline=ONE_MM_OUTLINE)
        without = _convert(tmp_path / "b", generic_lyp, pads=ONE_MM_PADS)

        a = with_outline.read_bytes()
        b = without.read_bytes()
        # GDS_FILE is the absolute input path, which differs by the subdirectory
        # these two were written in. Normalise only that, and prove the
        # normalisation actually fired so this cannot pass vacuously.
        a_norm = a.replace(str(tmp_path / "a").encode(), b"<DIR>")
        b_norm = b.replace(str(tmp_path / "b").encode(), b"<DIR>")
        assert b"<DIR>" in a_norm and b"<DIR>" in b_norm
        assert a_norm == b_norm

    def test_option_omitted_matches_a_converter_built_without_it(
            self, tmp_path, generic_lyp):
        """Passing die_outline_layer=None is the same object as not passing it."""
        gds = _write_gds(tmp_path / "die.gds", pads=ONE_MM_PADS,
                         outline=ONE_MM_OUTLINE)
        explicit = tmp_path / "explicit.kicad_mod"
        implicit = tmp_path / "implicit.kicad_mod"

        GDSToKiCad(generic_lyp, pad_layer=PAD_LAYER,
                   die_outline_layer=None).convert(str(gds), str(explicit))
        GDSToKiCad(generic_lyp, pad_layer=PAD_LAYER).convert(str(gds),
                                                             str(implicit))
        assert explicit.read_bytes() == implicit.read_bytes()


class TestPadReviewPath:
    """--from-pad-review must honour the option too.

    It did not: the CLI built the converter without die_outline_layer and
    returned before the option was even parsed, and convert_from_pad_review
    ignored the constructor argument. So --die-outline-layer on that path exited
    0 and wrote a legacy pad-hull courtyard, which is precisely the outcome the
    option exists to make impossible: a file that reads the same in both worlds.
    """

    @staticmethod
    def _pin_list():
        from pin_list import PinList, PinEntry
        return PinList(
            pins=[PinEntry(name="P%d" % i, type="passive", side="left",
                           pad_index=i,
                           center_x_dbu=(x0 + x1) / 2 * 1000,
                           center_y_dbu=(y0 + y1) / 2 * 1000,
                           width_dbu=(x1 - x0) * 1000,
                           height_dbu=(y1 - y0) * 1000)
                  for i, (x0, y0, x1, y1) in enumerate(ONE_MM_PADS)],
            metadata={"chiplet_name": "CF_TEST"})

    def test_review_path_uses_the_outline(self, tmp_path, generic_lyp):
        gds = _write_gds(tmp_path / "review.gds", pads=ONE_MM_PADS,
                         outline=ONE_MM_OUTLINE)
        out = tmp_path / "review.kicad_mod"
        converter = GDSToKiCad(generic_lyp, pad_layer=PAD_LAYER,
                               die_outline_layer=OUTLINE_LAYER)
        assert converter.convert_from_pad_review(
            str(gds), self._pin_list(), str(out))

        x0, y0, x1, y1, stroke = _courtyard(out.read_text())
        assert x1 - x0 == pytest.approx(1.0, abs=1e-9)
        assert y1 - y0 == pytest.approx(1.0, abs=1e-9)
        assert stroke == 0.005

    def test_review_path_refuses_a_missing_outline(self, tmp_path, generic_lyp):
        """The bug in one assertion: this must not quietly emit a pad hull."""
        gds = _write_gds(tmp_path / "review.gds", pads=ONE_MM_PADS)
        out = tmp_path / "review.kicad_mod"
        converter = GDSToKiCad(generic_lyp, pad_layer=PAD_LAYER,
                               die_outline_layer=OUTLINE_LAYER)
        with pytest.raises(ValueError, match="not present in the layout"):
            converter.convert_from_pad_review(str(gds), self._pin_list(),
                                              str(out))
        assert not out.exists()

    def test_review_path_refuses_a_pad_outside(self, tmp_path, generic_lyp):
        gds = _write_gds(tmp_path / "review.gds",
                         pads=ONE_MM_PADS + [(1010.0, 100.0, 1080.0, 170.0)],
                         outline=ONE_MM_OUTLINE)
        out = tmp_path / "review.kicad_mod"
        converter = GDSToKiCad(generic_lyp, pad_layer=PAD_LAYER,
                               die_outline_layer=OUTLINE_LAYER)
        with pytest.raises(ValueError, match="fall outside the die outline"):
            converter.convert_from_pad_review(str(gds), self._pin_list(),
                                              str(out))
        assert not out.exists()

    def test_review_path_omitted_option_is_the_pad_hull(self, tmp_path,
                                                       generic_lyp):
        gds = _write_gds(tmp_path / "review.gds", pads=ONE_MM_PADS,
                         outline=ONE_MM_OUTLINE)
        out = tmp_path / "review.kicad_mod"
        GDSToKiCad(generic_lyp, pad_layer=PAD_LAYER).convert_from_pad_review(
            str(gds), self._pin_list(), str(out))
        x0, _, x1, _, stroke = _courtyard(out.read_text())
        assert x1 - x0 == pytest.approx(0.87, abs=1e-9)
        assert stroke == 0.05


class TestCLIDispatch:
    """Through main(), because the defect was in the dispatch, not the class.

    A test that only drove the constructor would have passed against the broken
    version: the CLI never handed the option to it.
    """

    SCRIPT = Path(__file__).resolve().parents[1] / "gds_to_kicad.py"

    def _run(self, args):
        import subprocess
        return subprocess.run([sys.executable, str(self.SCRIPT)] + args,
                              capture_output=True, text=True)

    def _pin_list_file(self, tmp_path):
        TestPadReviewPath._pin_list().save(str(tmp_path / "pins.json"))
        return tmp_path / "pins.json"

    def test_direct_path_accepts_the_option(self, tmp_path):
        gds = _write_gds(tmp_path / "die.gds", pads=ONE_MM_PADS,
                         outline=ONE_MM_OUTLINE)
        out = tmp_path / "die.kicad_mod"
        result = self._run([str(gds), "-o", str(out),
                            "--pad-layer-number", "205/0",
                            "--die-outline-layer", "206/0"])
        assert result.returncode == 0, result.stderr
        x0, _, x1, _, stroke = _courtyard(out.read_text())
        assert x1 - x0 == pytest.approx(1.0, abs=1e-9)
        assert stroke == 0.005

    def test_review_path_accepts_the_option(self, tmp_path):
        gds = _write_gds(tmp_path / "review.gds", pads=ONE_MM_PADS,
                         outline=ONE_MM_OUTLINE)
        out = tmp_path / "review.kicad_mod"
        result = self._run(["--from-pad-review", str(gds),
                            "--pin-list", str(self._pin_list_file(tmp_path)),
                            "-o", str(out),
                            "--pad-layer-number", "205/0",
                            "--die-outline-layer", "206/0"])
        assert result.returncode == 0, result.stderr
        x0, _, x1, _, stroke = _courtyard(out.read_text())
        assert x1 - x0 == pytest.approx(1.0, abs=1e-9), \
            "the pad-review dispatch ignored --die-outline-layer"
        assert stroke == 0.005

    def test_review_path_exits_nonzero_on_a_missing_outline(self, tmp_path):
        gds = _write_gds(tmp_path / "review.gds", pads=ONE_MM_PADS)
        out = tmp_path / "review.kicad_mod"
        result = self._run(["--from-pad-review", str(gds),
                            "--pin-list", str(self._pin_list_file(tmp_path)),
                            "-o", str(out),
                            "--pad-layer-number", "205/0",
                            "--die-outline-layer", "206/0"])
        assert result.returncode != 0
        assert not out.exists()
        assert "206/0" in (result.stderr + result.stdout)
