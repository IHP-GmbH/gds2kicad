# SPDX-License-Identifier: GPL-3.0-or-later
"""chiplet_to_kicad_sch --handout: the symbol library, project and empty sheet
a student draws a system on, from a .chiplet whose die-to-die links are open.

Hermetic: every input is written into tmp_path."""

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chiplet_to_kicad_sch as g  # noqa: E402
import sexpr_io as sx  # noqa: E402
from tests import test_chiplet_to_kicad_sch as gt  # noqa: E402

# The links (sig0, sig1) are left for the student; the rail and the die's own
# probe net are given.
GIVEN = copy.deepcopy(gt.CHIPLET)
GIVEN["netlist"]["nets"] = [n for n in GIVEN["netlist"]["nets"]
                            if n["name"] in ("VDD", "probe")]
FILES = {"chiplet_dies.kicad_sym", "sym-lib-table", "design.kicad_pro", "design.kicad_sch"}


def _handout(tmp_path, chiplet=None, pins=None, out="handout", **kw):
    cpath, pins = gt._inputs(tmp_path, chiplet=GIVEN if chiplet is None else chiplet,
                             pins=pins)
    return g.handout(cpath, pins, tmp_path / out, **kw), tmp_path / out


def _symbols(out):
    tree = sx.parse((out / g.HANDOUT_LIB_FILE).read_text(encoding="utf-8"))
    return {str(s[1]): s for s in sx.find_all(tree, "symbol")}


def _pins(symbol):
    return {str(sx.find(p, "number")[1]): p for p in sx.find_all_rec(symbol, "pin")}


def test_writes_the_four_files_and_reruns_are_byte_equal(tmp_path):
    files, out = _handout(tmp_path / "a")
    again, out_b = _handout(tmp_path / "b")
    assert set(files) == FILES == {p.name for p in out.iterdir()}
    assert files == again
    for name in FILES:
        assert (out / name).read_bytes() == (out_b / name).read_bytes()
    assert g.handout(tmp_path / "a" / "design.chiplet",
                     {k: str(tmp_path / "a" / f"{k}.pins.json") for k in gt.PINS},
                     out) == files


def test_one_symbol_per_top_cell_named_by_it_all_passive_no_identity(tmp_path):
    _files, out = _handout(tmp_path)
    syms = _symbols(out)
    assert set(syms) == {"DIE_A", "DIE_B"}
    text = (out / g.HANDOUT_LIB_FILE).read_text(encoding="utf-8")
    assert g.IDENTITY_PROPERTY not in text and "chiplet:" not in text
    for name, sym in syms.items():
        props = {str(p[1]): str(p[2]) for p in sx.find_all(sym, "property")}
        assert props["Reference"] == "U" and props["Value"] == name
        for num, pin in _pins(sym).items():
            assert pin[1] == "passive"
    a = _pins(syms["DIE_A"])
    assert set(a) == {"A0", "A1", "VDD1", "VDD2", "NC1"}
    assert str(sx.find(a["A0"], "name")[1]) == "a0"


def test_link_pins_sit_right_and_the_rest_left(tmp_path):
    _files, out = _handout(tmp_path)
    syms = _symbols(out)
    x = {(cell, num): float(sx.find(pin, "at")[1])
         for cell, sym in syms.items() for num, pin in _pins(sym).items()}
    right = {k for k, v in x.items() if v > 0}
    assert right == {("DIE_A", "A0"), ("DIE_A", "A1"), ("DIE_B", "B0"), ("DIE_B", "B1"),
                     ("DIE_B", "SPARE")}


def test_the_symbol_does_not_depend_on_the_die_id(tmp_path):
    renamed = copy.deepcopy(GIVEN)
    renamed["components"][1]["id"] = "U1_LONG_NAME"
    for net in renamed["netlist"]["nets"]:
        for c in net["connections"]:
            if c["component"] == "U1":
                c["component"] = "U1_LONG_NAME"
    pins = {"U1_LONG_NAME": gt.PINS["U1"], "U2": gt.PINS["U2"]}
    _files, out = _handout(tmp_path / "a")
    _files, out_b = _handout(tmp_path / "b", chiplet=renamed, pins=pins)
    assert ((out / g.HANDOUT_LIB_FILE).read_bytes()
            == (out_b / g.HANDOUT_LIB_FILE).read_bytes())


def test_two_dies_of_one_cell_share_a_symbol(tmp_path):
    twin = copy.deepcopy(GIVEN)
    twin["components"][2]["top_cell"] = "DIE_A"
    twin["netlist"]["nets"] = [gt._net("VDD", ("U1", "VDD1"), ("U1", "VDD2"),
                                       ("U2", "VDD1"), ("U2", "VDD2"))]
    pins = {"U1": gt.PINS["U1"], "U2": gt.PINS["U1"]}
    _files, out = _handout(tmp_path, chiplet=twin, pins=pins)
    assert set(_symbols(out)) == {"DIE_A"}


def test_two_dies_of_one_cell_drawn_two_ways_are_refused(tmp_path):
    twin = copy.deepcopy(GIVEN)
    twin["components"][2]["top_cell"] = "DIE_A"
    with pytest.raises(g.SchematicRefused) as exc:
        _handout(tmp_path, chiplet=twin)
    assert [r["code"] for r in exc.value.refusals] == ["top_cell_drawn_twice"]
    assert not (tmp_path / "handout").exists()


@pytest.mark.parametrize("cell", [None, "", "DIE A", "lib:DIE"])
def test_a_die_without_a_plain_top_cell_is_refused(tmp_path, cell):
    bad = copy.deepcopy(GIVEN)
    if cell is None:
        del bad["components"][1]["top_cell"]
    else:
        bad["components"][1]["top_cell"] = cell
    with pytest.raises(g.SchematicRefused) as exc:
        _handout(tmp_path, chiplet=bad)
    assert [(r["code"], r.get("field")) for r in exc.value.refusals] == [
        ("unmappable_name", "top_cell")]


def test_sheet_and_project_carry_no_digest_and_agree(tmp_path):
    _files, out = _handout(tmp_path)
    sheet_text = (out / "design.kicad_sch").read_text(encoding="utf-8")
    assert g.BASE_DIGEST_PREFIX not in sheet_text and str(tmp_path) not in sheet_text
    sheet = sx.parse(sheet_text)
    assert sx.find(sheet, "lib_symbols") == ["lib_symbols"]
    assert not sx.find_all(sheet, "symbol")
    pro = json.loads((out / "design.kicad_pro").read_text(encoding="utf-8"))
    assert pro["meta"]["filename"] == "design.kicad_pro"
    assert pro["sheets"] == [[str(sx.find(sheet, "uuid")[1]), "Root"]]
    table = (out / "sym-lib-table").read_text(encoding="utf-8")
    assert '(name "chiplet_dies")' in table
    assert '"${KIPRJMOD}/chiplet_dies.kicad_sym"' in table


def test_name_overrides_the_stem(tmp_path):
    files, _out = _handout(tmp_path, name="sheet_first")
    assert {"sheet_first.kicad_pro", "sheet_first.kicad_sch"} <= set(files)


def test_a_foreign_library_table_is_refused(tmp_path):
    (tmp_path / "handout").mkdir(parents=True)
    (tmp_path / "handout" / "sym-lib-table").write_text("(sym_lib_table)\n")
    with pytest.raises(g.SchematicRefused) as exc:
        _handout(tmp_path)
    assert [r["code"] for r in exc.value.refusals] == ["library_table"]
    assert {p.name for p in (tmp_path / "handout").iterdir()} == {"sym-lib-table"}


def test_a_drawn_sheet_is_not_overwritten(tmp_path):
    _files, out = _handout(tmp_path)
    sheet = out / "design.kicad_sch"
    drawn = sheet.read_text(encoding="utf-8").replace(
        "(lib_symbols)", '(lib_symbols)\n\t(text "drawn" (at 0 0 0))')
    assert drawn != sheet.read_text(encoding="utf-8")
    sheet.write_text(drawn, encoding="utf-8")
    (out / "design.kicad_pro").write_text("{}\n", encoding="utf-8")
    with pytest.raises(g.SchematicRefused) as exc:
        _handout(tmp_path)
    assert sorted((r["code"], r["path"]) for r in exc.value.refusals) == [
        ("handout_exists", "design.kicad_pro"), ("handout_exists", "design.kicad_sch")]
    assert sheet.read_text(encoding="utf-8") == drawn


def test_the_table_is_checked_behind_other_refusals(tmp_path):
    (tmp_path / "handout").mkdir(parents=True)
    (tmp_path / "handout" / "sym-lib-table").write_text("(sym_lib_table)\n")
    twin = copy.deepcopy(GIVEN)
    twin["components"][2]["top_cell"] = "DIE_A"
    with pytest.raises(g.SchematicRefused) as exc:
        _handout(tmp_path, chiplet=twin)
    assert sorted(r["code"] for r in exc.value.refusals) == [
        "library_table", "top_cell_drawn_twice"]


@pytest.mark.parametrize("out", ["handout", "handout/sub"])
def test_an_output_that_is_or_sits_in_a_file_is_refused(tmp_path, out):
    (tmp_path / "handout").write_text("x\n")
    with pytest.raises(g.SchematicRefused) as exc:
        _handout(tmp_path, out=out)
    assert [r["code"] for r in exc.value.refusals] == ["handout_dir"]
    assert (tmp_path / "handout").read_text() == "x\n"


def test_a_refused_symbol_does_not_report_the_existing_library(tmp_path):
    _handout(tmp_path)
    twin = copy.deepcopy(GIVEN)
    twin["components"][2]["top_cell"] = "DIE_A"
    with pytest.raises(g.SchematicRefused) as exc:
        _handout(tmp_path, chiplet=twin)
    assert [r["code"] for r in exc.value.refusals] == ["top_cell_drawn_twice"]


@pytest.mark.parametrize("name", ["../x", "a b", ""])
def test_a_project_name_that_is_not_plain_is_refused(tmp_path, name):
    with pytest.raises(g.SchematicRefused) as exc:
        _handout(tmp_path, name=name)
    assert [(r["code"], r.get("field")) for r in exc.value.refusals] == [
        ("unmappable_name", "project")]
    assert not (tmp_path / "handout").exists()


def test_cli(tmp_path, capsys):
    cpath, pins = gt._inputs(tmp_path, chiplet=GIVEN)
    argv = ["--chiplet", str(cpath)] + [a for k, v in pins.items()
                                        for a in ("--pins", f"{k}={v}")]
    assert g.main(argv + ["--handout", str(tmp_path / "h")]) == g.EXIT_WRITTEN
    assert {p.name for p in (tmp_path / "h").iterdir()} == FILES
    twin = copy.deepcopy(GIVEN)
    twin["components"][2]["top_cell"] = "DIE_A"
    cpath2, _ = gt._inputs(tmp_path / "t", chiplet=twin)
    assert g.main(["--chiplet", str(cpath2)] + argv[2:]
                  + ["--handout", str(tmp_path / "h2")]) == g.EXIT_REFUSED
    assert "top_cell_drawn_twice" in capsys.readouterr().err
    for extra in (["--out", "x.kicad_sch"], ["--check"]):
        with pytest.raises(SystemExit):
            g.main(argv + ["--handout", str(tmp_path / "h3")] + extra)
    with pytest.raises(SystemExit):
        g.main(argv + ["--name", "x", "--out", "a", "--report", "b"])


# ---------------------------------------------------------- KiCad itself

@pytest.mark.kicad_cli
def test_kicad_cli_loads_the_handout(tmp_path):
    """KiCad reads the library (one SVG per symbol) and the empty sheet of
    the project (a netlist with no component)."""
    import os
    import shutil
    import subprocess
    if shutil.which("kicad-cli") is None:
        if os.environ.get("GDS_TO_KICAD_REQUIRE_KICAD_CLI") == "1":
            pytest.fail("GDS_TO_KICAD_REQUIRE_KICAD_CLI=1 but kicad-cli is not on PATH")
        pytest.skip("kicad-cli not on PATH (GDS_TO_KICAD_REQUIRE_KICAD_CLI=1 requires it)")
    _files, out = _handout(tmp_path)
    svg = tmp_path / "svg"
    run = subprocess.run(["kicad-cli", "sym", "export", "svg", "--output", str(svg),
                          str(out / g.HANDOUT_LIB_FILE)], capture_output=True, timeout=300)
    assert run.returncode == 0, run.stderr
    assert len(list(svg.glob("*.svg"))) == 2
    net = tmp_path / "blank.net"
    run = subprocess.run(["kicad-cli", "sch", "export", "netlist", "--format", "kicadsexpr",
                          "--output", str(net), str(out / "design.kicad_sch")],
                         capture_output=True, timeout=300)
    assert run.returncode == 0, run.stderr
    assert sx.find(sx.parse(net.read_text(encoding="utf-8")), "components") == ["components"]
