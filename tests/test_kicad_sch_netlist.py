# SPDX-License-Identifier: GPL-3.0-or-later
"""kicad_sch_netlist: the native netlister against kicad-cli's own netlist and
ERC report of the same sheet, and the reader's ``--native`` read.

Each case under tests/data/native_netlist holds a sheet, its project, and the
sheet.net and erc.json that kicad-cli 9.99.0 wrote for it (``sch export
netlist --format kicadsexpr`` and ``sch erc --format json --severity-all``);
the sheet's generator line was renamed afterwards, which neither reads. None
of these tests runs kicad-cli."""
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kicad_rules as r  # noqa: E402
import kicad_sch_netlist as n  # noqa: E402
import kicad_sch_to_connectivity as k  # noqa: E402
import sexpr_io as sx  # noqa: E402
from chiplet_to_kicad_sch import _report_ref, _sha256  # noqa: E402

CASES = Path(__file__).parent / "data" / "native_netlist"
SCRIPT = Path(__file__).resolve().parent.parent / "kicad_sch_to_connectivity.py"
# What the reader reads of a component's properties (sheet_check.netlist_property).
READ_PROPERTIES = ("CHIPLET_COMPONENT", "IO_PAD_ID", "chiplet_cosim_setup")
STUB_LIBRARY = '(kicad_symbol_lib (version 20241209) (generator "tests"))\n'


def _sheet(case):
    return CASES / case / f"ext_{case}.kicad_sch"


def _value(node, tag, default=None):
    found = sx.find(node, tag) if node is not None else None
    if found is None or len(found) < 2 or isinstance(found[1], list):
        return default
    return str(found[1])


def _reference(case):
    """The parts of kicad-cli's netlist and ERC report the model carries."""
    tree = sx.parse((CASES / case / "sheet.net").read_text(encoding="utf-8"))
    design = sx.find(tree, "design")
    sheets = sx.find_all(design, "sheet")
    root = next(s for s in sheets if _value(s, "name") == "/")
    comment1 = next(_value(c, "value", "") for c in
                    sx.find_all(sx.find(root, "title_block"), "comment")
                    if _value(c, "number") == "1")
    components = []
    for comp in sx.find_all(sx.find(tree, "components"), "comp"):
        source = sx.find(comp, "libsource")
        components.append({
            "ref": _value(comp, "ref"),
            "libsource": {"lib": _value(source, "lib"), "part": _value(source, "part")},
            "value": _value(comp, "value"), "footprint": _value(comp, "footprint", ""),
            "fields": {_value(f, "name"): str(f[-1]) if len(f) > 2 else ""
                       for f in sx.find_all(sx.find(comp, "fields") or [], "field")},
            "property": {_value(p, "name"): _value(p, "value", "")
                         for p in sx.find_all(comp, "property")},
            "sheetpath": _value(sx.find(comp, "sheetpath"), "names"),
            "tstamps": [str(t) for t in sx.find(comp, "tstamps")[1:]]})
    libparts = [{"lib": _value(lp, "lib"), "part": _value(lp, "part"),
                 "pins": [_value(p, "num") for p in sx.find_all(sx.find(lp, "pins"), "pin")]}
                for lp in sx.find_all(sx.find(tree, "libparts"), "libpart")]
    nets = [{"name": _value(net, "name"),
             "nodes": [{"ref": _value(d, "ref"), "pin": _value(d, "pin"),
                        "no_connect": _value(d, "pintype", "").endswith("+no_connect")}
                       for d in sx.find_all(net, "node")]}
            for net in sx.find_all(sx.find(tree, "nets"), "net")]
    erc = json.loads((CASES / case / "erc.json").read_text(encoding="utf-8"))
    violations = [{"type": v["type"], "description": v["description"],
                   "items": [i["description"] for i in v["items"]]}
                  for s in erc["sheets"] for v in s["violations"]
                  if v["type"] == "multiple_net_names"]
    return {"design": {"source": os.path.basename(_value(design, "source")),
                       "sheets": [_value(s, "name") for s in sheets],
                       "title_block_comment_1": comment1},
            "components": components, "libparts": libparts, "nets": nets,
            "violations": violations}


def _canon(model):
    """A model as what it says, not the order it says it in."""
    comps = {c["ref"]: (c["libsource"], c["value"], c["footprint"], c["fields"],
                        {k: v for k, v in c["property"].items() if k in READ_PROPERTIES},
                        c["sheetpath"], tuple(c["tstamps"]))
             for c in model["components"]}
    assert len(comps) == len(model["components"])
    libparts = {(lp["lib"], lp["part"]): tuple(lp["pins"]) for lp in model["libparts"]}
    assert len(libparts) == len(model["libparts"])
    nets = {net["name"]: frozenset((d["ref"], d["pin"], d["no_connect"]) for d in net["nodes"])
            for net in model["nets"]}
    assert len(nets) == len(model["nets"])
    design = dict(model["design"], sheets=sorted(model["design"]["sheets"]))
    violations = Counter((v["type"], v["description"], tuple(v["items"]))
                         for v in model["violations"])
    return design, comps, libparts, nets, violations


CLAIMS = ["stacked", "net_names", "power_hidden", "power_glabel", "subsheet_local", "unit_fields",
          "label_tolerance", "pro_settings", "rot_mirror", "instance_missing", "instance_single",
          "instance_carried", "instance_agreed", "instance_first_sets", "instance_path_case",
          "instance_path_twice", "instance_value", "instance_without_unit",
          "fields_after_instances", "field_name_case", "cache_slash", "title_last"]


@pytest.mark.parametrize("case", CLAIMS)
def test_model_is_kicad_clis_netlist_and_erc(case):
    assert _canon(n.netlist(str(_sheet(case)))) == _canon(_reference(case))


def test_model_names_the_netlister_version_and_every_file_read():
    root = _sheet("subsheet_local")
    model = n.netlist(str(root))
    assert model["netlister"] == n.NETLISTER == f"gds_to_kicad kicad_sch_netlist {n.VERSION}"
    assert model["schematic_version"] == "20250114"
    child = root.with_name("ext_subsheet_local_child.kicad_sch")
    assert model["inputs"] == [str(root), str(child)]
    assert model["design"]["sheets"] == ["/", "/sub/"]


def test_a_project_set_to_ignore_drops_multiple_net_names(tmp_path):
    sheet = tmp_path / "ext_power_glabel.kicad_sch"
    shutil.copy(_sheet("power_glabel"), sheet)
    assert [v["type"] for v in n.netlist(str(sheet))["violations"]] == ["multiple_net_names"]
    project = tmp_path / "quiet.kicad_pro"
    project.write_text(json.dumps({"erc": {"rule_severities": {"multiple_net_names": "ignore"}}}),
                       encoding="utf-8")
    assert n.netlist(str(sheet), str(project))["violations"] == []


REMEDY = (". Remove it from the sheet, or read the sheet without --native (that read needs "
          "kicad-cli).")
CONSTRUCTS = {"k_bus": ("bus", (508000, 762000), "bus at (50.8, 76.2) mm"),
              "k_rotation_label45": ("rotation_label", (558800, 508000),
                                     "rotation not a multiple of 90 at (55.88, 50.8) mm")}


@pytest.mark.parametrize("case", sorted(CONSTRUCTS))
def test_a_construct_it_does_not_read_is_refused_by_name_and_place(case):
    construct, at, text = CONSTRUCTS[case]
    with pytest.raises(n.KicadConstruct) as raised:
        n.netlist(str(_sheet(case)))
    exc = raised.value
    assert (exc.construct, exc.path, exc.at) == (construct, str(_sheet(case)), at)
    assert exc.refusal() == {"code": "kicad_construct", "message":
                             f"ext_{case}.kicad_sch: {text} is not read by the native "
                             f"netlister{REMEDY}"}


# ------------------------------------------------------------------- the reader

def _copy(tmp_path, case):
    shutil.copytree(CASES / case, tmp_path / case)
    library = tmp_path / "chiplet_dies.kicad_sym"
    library.write_text(STUB_LIBRARY, encoding="utf-8")
    return tmp_path / case / _sheet(case).name, library


def _run(argv, cwd):
    return subprocess.run([sys.executable, str(SCRIPT), *argv], cwd=cwd,
                          capture_output=True, text=True, timeout=120)


def test_native_cli_is_the_in_process_read(tmp_path):
    sheet, library = _copy(tmp_path, "subsheet_local")
    before = sorted(p for p in tmp_path.rglob("*"))
    done = _run([str(sheet), "form.json", "--blank", "--library", str(library), "--native"],
                tmp_path)
    assert done.returncode == 0, done.stderr
    assert sorted(p for p in tmp_path.rglob("*")) == sorted(before + [tmp_path / "form.json"])
    written = json.loads((tmp_path / "form.json").read_text(encoding="utf-8"))
    form = k.read_form_native(sheet, None, tmp_path / "form.json", library, mode="blank")
    assert json.loads(json.dumps(form)) == written


def test_native_front_end_names_the_netlister_and_each_sheet(tmp_path):
    sheet, library = _copy(tmp_path, "subsheet_local")
    out = tmp_path / "form.json"
    front = k.read_form_native(sheet, None, out, library, mode="blank")["front_end"]
    child = sheet.with_name("ext_subsheet_local_child.kicad_sch")
    assert front == {"tool": "kicad", "tool_version": n.NETLISTER,
                     "schematic_version": "20250114", "netlister": n.NETLISTER,
                     "reader": "kicad_sch_to_connectivity 1.1",
                     "inputs": [{"path": _report_ref(p, tmp_path), "sha256": _sha256(p)}
                                for p in (sheet, child)]}


def test_native_read_keeps_the_hierarchy_refusal(tmp_path):
    sheet, library = _copy(tmp_path, "subsheet_local")
    form = k.read_form_native(sheet, None, tmp_path / "form.json", library, mode="blank")
    assert [r["message"] for r in form["refusals"] if r["code"] == "hierarchical_sheet"] == [
        "sub-sheet /sub/: only the root sheet is read"]


def test_a_text_variable_in_comment_1_is_refused_where_the_form_reads_it(tmp_path):
    """KiCad expands it (comment_variable's sheet.net holds base-sha256:abc), the
    netlister does not; only a generated sheet's form reads comment 1."""
    sheet, library = _copy(tmp_path, "comment_variable")
    model = n.netlist(str(sheet))
    assert _reference("comment_variable")["design"]["title_block_comment_1"] == "base-sha256:abc"
    assert model["design"]["title_block_comment_1"] == "base-sha256:${X}"
    report = tmp_path / "report.json"
    report.write_text("{}", encoding="utf-8")
    form = k.read_form_native(sheet, report, tmp_path / "form.json", model=model)
    assert (form["symbols"], form["nets"]) == ([], [])
    assert form["refusals"] == [{"code": "kicad_construct", "message": (
        "ext_comment_variable.kicad_sch: text variable (${...}) in title block comment 1 is not "
        "read by the native netlister. Remove it from the sheet, or read the sheet without "
        "--native (that read needs kicad-cli).")}]
    assert form["front_end"]["schematic_version"] == "20250114"
    blank = k.read_form_native(sheet, None, tmp_path / "form.json", library, mode="blank")
    assert "kicad_construct" not in [r["code"] for r in blank["refusals"]]


def test_native_read_reports_multiple_net_names_from_the_model(tmp_path):
    sheet, library = _copy(tmp_path, "power_glabel")
    form = k.read_form_native(sheet, None, tmp_path / "form.json", library, mode="blank")
    (violation,) = _reference("power_glabel")["violations"]
    assert [r["message"] for r in form["refusals"] if r["code"] == "multiple_net_names"] == [
        f"ERC: {violation['description']} ({'; '.join(violation['items'])})"]


def test_native_cli_writes_a_construct_as_its_one_refusal(tmp_path):
    sheet, library = _copy(tmp_path, "k_bus")
    done = _run([str(sheet), "form.json", "--blank", "--library", str(library), "--native"],
                tmp_path)
    assert done.returncode == 0, done.stderr
    form = json.loads((tmp_path / "form.json").read_text(encoding="utf-8"))
    assert form["symbols"] == [] and form["nets"] == [] and form["base_sha256"] is None
    assert form["refusals"] == [{"code": "kicad_construct", "message":
                                 "ext_k_bus.kicad_sch: bus at (50.8, 76.2) mm is not read by "
                                 f"the native netlister{REMEDY}"}]
    assert [i["path"] for i in form["front_end"]["inputs"]] == [_report_ref(sheet, tmp_path)]
    assert form["front_end"]["schematic_version"] == "20250114"


UNITS = Path(__file__).parent / "fixtures" / "v8_units"


@pytest.mark.parametrize("mode", ["classic", "blank"])
def test_tstamps_order_and_the_root_sheetfile_do_not_reach_the_form(tmp_path, mode):
    """The two places the model may differ from kicad-cli's netlist and no
    refusal stands guard: the order of a reference's other placements in
    tstamps (KiCad's R-tree order) and the root Sheetfile (KiCad's loaded path).
    U1 is placed three times here, so its tstamps has two others to swap."""
    shutil.copytree(UNITS, tmp_path / "units")
    sheet = tmp_path / "units" / "units.kicad_sch"
    root = sx.parse(sheet.read_text(encoding="utf-8"))
    u1 = next(s for s in sx.find_all(root, "symbol") if any(
        _value(p, "reference") == "U1" for p in sx.find_all_rec(s, "path")))
    for k_ in (8, 9):
        copy = json.loads(json.dumps(u1))
        sx.find(copy, "uuid")[1] = sx.Quoted(_uuid(k_))
        root.append(copy)
    sheet.write_text(sx.dumps(root), encoding="utf-8")
    model = n.netlist(str(sheet))
    swapped = json.loads(json.dumps(model))
    for comp in swapped["components"]:
        *others, primary = comp["tstamps"]
        comp["tstamps"] = others[::-1] + [primary]
        comp["property"]["Sheetfile"] = str(sheet)
    assert [len(c["tstamps"]) for c in model["components"] if c["ref"] == "U1"] == [3]
    assert swapped["components"] != model["components"]
    files = {"library": tmp_path / "units" / "chiplet_dies.kicad_sym"}
    if mode == "classic":
        files["system_library"] = tmp_path / "units" / "chiplet_system.kicad_sym"
    forms = [k.read_form_native(sheet, None, tmp_path / "form.json", mode=mode, model=m, **files)
             for m in (model, swapped)]
    assert forms[0] == forms[1]


USAGE_MESSAGES = {
    "erc": "--native takes no --erc or --no-erc; the native read always checks "
           "multiple_net_names",
    "no_erc": "--native takes no --erc or --no-erc; the native read always checks "
              "multiple_net_names",
    "netlist": "with --native, give SCHEMATIC OUT_FORM and --blank or --classic, or "
               "SCHEMATIC GENERATOR_REPORT OUT_FORM"}
USAGE_ERRORS = {"erc": lambda net, sheet, rest: [sheet, *rest, "--erc", net],
                "no_erc": lambda net, sheet, rest: [sheet, *rest, "--no-erc"],
                "netlist": lambda net, sheet, rest: [net, sheet, *rest]}


@pytest.mark.parametrize("case", sorted(USAGE_ERRORS))
def test_native_cli_takes_no_netlist_and_no_erc_choice(tmp_path, case, capsys):
    sheet, library = _copy(tmp_path, "stacked")
    out = tmp_path / "form.json"
    argv = USAGE_ERRORS[case](str(sheet.with_name("sheet.net")), str(sheet),
                              [str(out), "--blank", "--library", str(library), "--native"])
    with pytest.raises(SystemExit) as raised:
        k.main(argv)
    assert raised.value.code == 2 and not out.exists()
    assert USAGE_MESSAGES[case] in capsys.readouterr().err


# ------------------------------------------------- sheets written here, read by KiCad's rules
#
# These sheets have no kicad-cli output beside them: each test states what
# eeschema's source gives for its sheet, as the docstring says.

ROOT = "10000000-0000-4000-8000-000000000000"
LIB = """(lib_symbols
  (symbol "t:R" (property "Reference" "R") (property "Value" "R")
    (symbol "R_1_1" (pin passive line (at 0 0 0) (length 2.54) (name "~") (number "1"))))
  (symbol "t:DUAL" (property "Reference" "U") (property "Value" "DUAL")
    (symbol "DUAL_1_1" (pin passive line (at 0 0 0) (length 2.54) (name "A") (number "1")))
    (symbol "DUAL_2_1" (pin passive line (at 0 5.08 0) (length 2.54) (name "B") (number "2")))))
"""


def _write(path, body, version=20250114, uuid=ROOT, lib=LIB):
    path.write_text(f'(kicad_sch (version {version}) (generator "tests") (uuid "{uuid}")\n'
                    f'{lib}{body})\n', encoding="utf-8")
    return path


def _part(ref, x, y, uuid, lib_id="t:R", unit=1, fields="", path=f"/{ROOT}"):
    return (f'(symbol (lib_id "{lib_id}") (at {x} {y} 0) (unit {unit}) (uuid "{uuid}")\n'
            f'  (property "Reference" "{ref}") {fields}\n'
            f'  (instances (project "p" (path "{path}" (reference "{ref}") (unit {unit})))))\n')


def _uuid(n):
    return f"00000000-0000-4000-8000-{n:012d}"


def _nets(model):
    return {net["name"]: sorted((d["ref"], d["pin"]) for d in net["nodes"])
            for net in model["nets"]}


def test_a_file_older_than_the_format_floor_is_an_input_error(tmp_path):
    """KiCad migrates the root's symbol_instances and legacy power symbol
    values on load below 20230221 (eeschema_helpers.cpp:182-188)."""
    body = _part("R1", 10, 10, _uuid(1)) + '(global_label "G" (at 10 10 0))\n'
    with pytest.raises(n.InputError, match="version 20221001 is older than 20230221"):
        n.netlist(str(_write(tmp_path / "old.kicad_sch", body, version=20221001)))
    model = n.netlist(str(_write(tmp_path / "floor.kicad_sch", body, version=n.FORMAT_FLOOR)))
    assert _nets(model) == {"G": [("R1", "1")]}


def test_a_value_of_tilde_is_empty_in_files_before_20250318(tmp_path):
    """parseSchField reads "~" as empty before 20250318
    (sch_io_kicad_sexpr_parser.cpp:2302)."""
    body = _part("R1", 10, 10, _uuid(1), fields='(property "Datasheet" "~") '
                                                 '(property "Tag" "~")')
    for version, shown in ((20250114, ""), (20250318, "~")):
        (comp,) = n.netlist(str(_write(tmp_path / f"v{version}.kicad_sch", body,
                                       version=version)))["components"]
        assert (comp["fields"]["Datasheet"], comp["fields"]["Tag"]) == (shown, shown)


UNIT1_FIELDS = '(property "Value" "VAL_U1") (property "Tag" "TAG_U1") (property "Blank1" "")'
UNIT2_FIELDS = '(property "Value" "VAL_U2") (property "Tag" "TAG_U2") (property "Blank1" "B2")'


@pytest.mark.parametrize("unit1_first", [True, False])
def test_unit_fields_come_from_the_lowest_unit_below_the_primary(tmp_path, unit1_first):
    """The primary (lowest UUID) is unit 2, so minUnit starts at 2 and unit 1
    replaces every value whatever the walk order (netlist_exporter_xml.cpp:121-176)."""
    unit1 = _part("U1", 10, 10, _uuid(9), "t:DUAL", 1, UNIT1_FIELDS)
    unit2 = _part("U1", 30, 10, _uuid(1), "t:DUAL", 2, UNIT2_FIELDS)
    body = unit1 + unit2 if unit1_first else unit2 + unit1
    (comp,) = n.netlist(str(_write(tmp_path / "s.kicad_sch", body)))["components"]
    assert (comp["value"], comp["fields"]["Tag"], comp["fields"]["Blank1"]) == (
        "VAL_U1", "TAG_U1", "")
    assert comp["tstamps"] == [_uuid(9), _uuid(1)]


def test_units_that_disagree_with_the_primary_on_the_lowest_unit_are_refused(tmp_path):
    """The primary is unit 1: minUnit starts at 1, so the first non-blank value
    met in KiCad's walk wins, and that walk is R-tree order."""
    body = (_part("U1", 30, 10, _uuid(9), "t:DUAL", 2, UNIT2_FIELDS)
            + _part("U1", 10, 10, _uuid(1), "t:DUAL", 1, UNIT1_FIELDS))
    with pytest.raises(n.KicadConstruct) as raised:
        n.netlist(str(_write(tmp_path / "s.kicad_sch", body)))
    assert (raised.value.construct, raised.value.at) == ("unit_fields", (100000, 100000))
    assert raised.value.schematic_version == "20250114"
    assert str(raised.value) == (
        's.kicad_sch: symbol U1 at (10, 10) mm has units that give the field Value different '
        'values ("VAL_U1", "VAL_U2"). KiCad\'s netlist then depends on the order of the units in '
        "the file. Give every unit of U1 the same Value, or read the sheet without --native "
        "(that read needs kicad-cli).")


def test_units_that_agree_are_read_whichever_is_the_primary(tmp_path):
    """A field only one unit carries, and values all units share, do not
    depend on the walk."""
    body = (_part("U1", 30, 10, _uuid(9), "t:DUAL", 2,
                  '(property "Value" "V") (property "Only2" "O2")')
            + _part("U1", 10, 10, _uuid(1), "t:DUAL", 1, '(property "Value" "V")'))
    (comp,) = n.netlist(str(_write(tmp_path / "s.kicad_sch", body)))["components"]
    assert (comp["value"], comp["fields"]["Only2"]) == ("V", "O2")


SYMBOL_CONSTRUCTS = {
    "field_escape": ("field_escape", "t:R", '(property "Value" "a{slash}b")'),
    "field_name_escape": ("field_escape", "t:R", '(property "a{slash}b" "V")'),
    "lib_id_escape": ("lib_id_escape", "t:R{slash}S", ""),
    "lib_name_escape": ("lib_id_escape", "t:R", ""),
    "private_value_escape": ("field_escape", "t:R", '(property private "Value" "a{slash}b")'),
    "pin_alternate": ("pin_alternate", "t:R", f'(pin "1" (uuid "{_uuid(7)}") (alternate "ALT"))')}


@pytest.mark.parametrize("case", sorted(SYMBOL_CONSTRUCTS))
def test_an_escape_and_an_alternate_pin_are_refused(tmp_path, case):
    """KiCad unescapes field names and values (string_utils.cpp:308-388),
    reads {slash} in a lib_id or lib_name as / (sch_io_kicad_sexpr_parser.cpp:
    3135,3150) and shows a pin's alternate name and type (sch_pin.cpp:577). Only
    a user field can be private (parseSchField)."""
    construct, lib_id, fields = SYMBOL_CONSTRUCTS[case]
    body = _part("R1", 10, 10, _uuid(1), lib_id, fields=fields)
    if case == "lib_name_escape":
        body = body.replace("(at 10 10 0)", '(lib_name "R{slash}S") (at 10 10 0)')
    with pytest.raises(n.KicadConstruct) as raised:
        n.netlist(str(_write(tmp_path / "s.kicad_sch", body)))
    assert (raised.value.construct, raised.value.at) == (construct, (100000, 100000))


def test_an_instance_without_a_reference_is_refused(tmp_path):
    """KiCad writes it unannotated, R? (SCH_SYMBOL::GetRef, IsAnnotated)."""
    body = _part("R1", 10, 10, _uuid(1)).replace('(reference "R1")', '(reference "")')
    with pytest.raises(n.KicadConstruct) as raised:
        n.netlist(str(_write(tmp_path / "s.kicad_sch", body)))
    assert (raised.value.construct, raised.value.at) == ("ref_question", (100000, 100000))


def test_a_private_field_may_hold_an_escape(tmp_path):
    body = _part("R1", 10, 10, _uuid(1), fields='(property private "Note" "a{slash}b")')
    assert [c["ref"] for c in n.netlist(str(_write(tmp_path / "s.kicad_sch", body)))[
        "components"]] == ["R1"]


TWO_PINS_ONE_NUMBER = LIB.replace(
    '(number "1"))))\n  (symbol "t:DUAL"',
    '(number "1"))\n      (pin passive line (at 0 5.08 0) (length 2.54) (name "~") (number "1"))))'
    '\n  (symbol "t:DUAL"')


def test_two_weak_nets_of_one_name_are_refused(tmp_path):
    """Two pins numbered 1, not jumpers, each left open: two weak subgraphs
    named Net-(R1-Pad1), and which takes the suffix follows KiCad's subgraph
    order (connection_graph.cpp:1859-1925)."""
    assert TWO_PINS_ONE_NUMBER != LIB
    sheet = _write(tmp_path / "s.kicad_sch", _part("R1", 10, 10, _uuid(1)),
                   lib=TWO_PINS_ONE_NUMBER)
    with pytest.raises(n.KicadConstruct) as raised:
        n.netlist(str(sheet))
    assert raised.value.construct == "weak_name"
    assert raised.value.at in {(100000, 100000), (100000, 150800)}
    assert str(raised.value).startswith("s.kicad_sch: pin 1 of R1 at (10, ")
    assert ("is on one of two nets KiCad names alike (Net-(R1-Pad1)). KiCad's netlist then "
            "depends on the order of the items in the file. Connect the two nets, or read the "
            "sheet without --native (that read needs kicad-cli).") in str(raised.value)


def test_a_net_named_as_an_unconnected_pin_like_another_is_refused(tmp_path):
    """One pin 1 flagged, the other open: each is renamed unconnected-(R1-Pad1)
    after the weak names are settled, and KiCad keeps the two net codes apart
    (connection_graph.cpp:2413-2425), so its netlist holds two nets of that
    name."""
    sheet = _write(tmp_path / "s.kicad_sch",
                   _part("R1", 10, 10, _uuid(1)) + "(no_connect (at 10 10))\n",
                   lib=TWO_PINS_ONE_NUMBER)
    with pytest.raises(n.KicadConstruct) as raised:
        n.netlist(str(sheet))
    assert raised.value.construct == "weak_name"
    assert raised.value.at in {(100000, 100000), (100000, 150800)}
    assert ("is on one of two nets KiCad names alike (unconnected-(R1-Pad1)). KiCad's netlist "
            "then holds two nets of that name. Connect the two nets, or read the sheet without "
            "--native (that read needs kicad-cli).") in str(raised.value)


def _placed_twice(tmp_path, child, root="", names="ab"):
    """A root that places child.kicad_sch on sheet paths a and b (or ``names``)."""
    _write(tmp_path / "child.kicad_sch", child, uuid="20000000-0000-4000-8000-000000000000")
    sheets = "".join(f'(sheet (at {x} 50) (size 10 10) (uuid "{_uuid(x)}")\n'
                     f'  (property "Sheetname" "{name}")\n'
                     f'  (property "Sheetfile" "child.kicad_sch"))\n'
                     for x, name in zip((10, 40, 70), names))
    return _write(tmp_path / "root.kicad_sch", sheets + root)


def _instances(*paths):
    return "(instances " + "".join(
        f'(project "{project}" (path "{path}" (reference "{ref}") (unit {unit})))'
        for project, path, ref, unit in paths) + ")"


PATH_A, PATH_B = f"/{ROOT}/{_uuid(10)}", f"/{ROOT}/{_uuid(40)}"


def test_a_symbol_without_an_instance_on_one_path_is_refused_when_other_paths_differ(
        tmp_path):
    """Each sheet path, in page order, leaves its reference and unit on the
    symbol (sch_sheet_path.cpp:434-435), and a path without an instance shows
    those of the last (SCH_SYMBOL::GetRef): on c, U1 unit 1 or U2 unit 2."""
    child = (f'(symbol (lib_id "t:DUAL") (at 10 10 0) (unit 1) (uuid "{_uuid(1)}")\n'
             '  (property "Reference" "U1")\n'
             + _instances(("p", PATH_A, "U1", 1), ("p", PATH_B, "U2", 2)) + ")\n")
    with pytest.raises(n.KicadConstruct) as raised:
        n.netlist(str(_placed_twice(tmp_path, child, names="abc")))
    assert (raised.value.construct, raised.value.at) == ("instance_missing", (100000, 100000))
    assert str(raised.value) == (
        "child.kicad_sch: symbol U1 at (10, 10) mm has no instance for the sheet path /c/. "
        "KiCad's netlist then depends on the page order of the sheets that place it. Annotate "
        "the schematic in KiCad, or read the sheet without --native (that read needs kicad-cli).")


def test_units_seen_from_another_path_that_differ_by_path_are_refused_where_fields_differ(
        tmp_path):
    """Seen from the root, the sub-sheet's U1 has no instance, so KiCad reads its
    m_unit, which the last sheet path updated leaves at 1 or 2."""
    child = (f'(symbol (lib_id "t:DUAL") (at 10 10 0) (unit 1) (uuid "{_uuid(1)}")\n'
             '  (property "Reference" "U1") (property "Value" "VB")\n'
             + _instances(("p", PATH_A, "U1", 2), ("p", PATH_B, "U2", 1)) + ")\n")
    root = _part("U1", 10, 80, _uuid(2), "t:DUAL", 2, '(property "Value" "VA")')
    with pytest.raises(n.KicadConstruct) as raised:
        n.netlist(str(_placed_twice(tmp_path, child, root)))
    assert (raised.value.construct, raised.value.at) == ("instance_unit", (100000, 100000))
    assert str(raised.value) == (
        "child.kicad_sch: symbol U1 at (10, 10) mm takes different units on the sheet paths "
        "that place it, and its units give the field Value different values. KiCad's netlist "
        "then depends on the page order of those sheets. Give it one unit on every sheet path, "
        "or read the sheet without --native (that read needs kicad-cli).")
    agreed = (tmp_path / "child.kicad_sch").read_text(encoding="utf-8").replace('"VB"', '"VA"')
    (tmp_path / "child.kicad_sch").write_text(agreed, encoding="utf-8")
    (comp,) = [c for c in n.netlist(str(tmp_path / "root.kicad_sch"))["components"]
               if c["ref"] == "U1"]
    assert comp["value"] == "VA"


def test_two_weak_nets_of_a_shared_reference_are_read(tmp_path):
    """The reader refuses a shared reference (duplicate_reference), so its
    pins' names are left to it."""
    body = _part("R1", 10, 10, _uuid(1)) + _part("R1", 30, 10, _uuid(2))
    model = n.netlist(str(_write(tmp_path / "s.kicad_sch", body)))
    assert sorted(_nets(model)) == ["unconnected-(R1-Pad1)", "unconnected-(R1-Pad1)_1"]


def test_without_an_instance_here_the_first_instance_gives_unit_and_reference(tmp_path):
    """SCH_SYMBOL::AddHierarchicalReference sets the unit and the Reference
    field from the first instance loaded (sch_symbol.cpp:603-608)."""
    body = (_part("U1", 10, 10, _uuid(1), "t:DUAL", 2, path="/elsewhere")
            .replace('(property "Reference" "U1")', '(property "Reference" "U9")')
            .replace("(unit 2) (uuid", "(unit 1) (uuid")
            + '(global_label "G" (at 10 4.92 0))\n')
    model = n.netlist(str(_write(tmp_path / "s.kicad_sch", body)))
    assert [c["ref"] for c in model["components"]] == ["U1"]
    assert _nets(model) == {"G": [("U1", "2")]}


def test_a_local_label_of_another_net_on_the_sheet_merges_it_by_its_escaped_name(tmp_path):
    """processSubGraphs compares GetNameForDriver, the escaped text
    (connection_graph.cpp:2131)."""
    body = (_part("R1", 10, 10, _uuid(1)) + _part("R2", 10, 20, _uuid(2))
            + "(wire (pts (xy 10 10) (xy 30 10)))\n(wire (pts (xy 10 20) (xy 30 20)))\n"
            + '(label "X" (at 20 10 0)) (label "a/b" (at 25 10 0))\n'
            + '(label "Y" (at 20 20 0)) (label "a/b" (at 25 20 0))\n')
    model = n.netlist(str(_write(tmp_path / "s.kicad_sch", body)))
    assert _nets(model) == {"/X": [("R1", "1"), ("R2", "1")]}


def test_a_file_placed_twice_reports_its_name_conflict_once(tmp_path):
    """ercCheckMultipleDrivers skips a driver it has seen, and both sheet
    paths share the file's items (connection_graph.cpp:3250-3270)."""
    _write(tmp_path / "child.kicad_sch",
           _part("R1", 10, 10, _uuid(1)) + "(wire (pts (xy 10 10) (xy 30 10)))\n"
           + '(global_label "G1" (at 20 10 0)) (global_label "G2" (at 25 10 0))\n',
           uuid="20000000-0000-4000-8000-000000000000")
    sheets = "".join(f'(sheet (at {x} 50) (size 10 10) (uuid "{_uuid(x)}")\n'
                     f'  (property "Sheetname" "{name}")\n'
                     f'  (property "Sheetfile" "child.kicad_sch"))\n'
                     for x, name in ((10, "a"), (40, "b")))
    model = n.netlist(str(_write(tmp_path / "root.kicad_sch", sheets)))
    assert model["design"]["sheets"] == ["/", "/a/", "/b/"]
    assert [v["description"] for v in model["violations"]] == [
        "Both G1 and G2 are attached to the same items; G1 will be used in the netlist"]


@pytest.mark.parametrize("text, bus", [
    ("A[1..4]", True), ("A[4..1]", True), ("A[1..1]", False), ("x[3]", False),
    ("x[03]", False), ("{A B}", True), ("PFX{A B}", True), ("~{RESET}", False),
    ("a{slash}b", False), ("D_{0}[0..3]", True), ("A B[1..2]", False), ("plain", False)])
def test_bus_labels_are_those_kicad_reads_as_a_bus(text, bus):
    assert r.is_bus_label(text) is bus


LABEL_CONSTRUCTS = {
    "bus": '(label "D[0..7]" (at 20 30 0))',
    "text_variable": '(global_label "${NET}" (at 20 30 0))',
    "excluded_rule_area": "(rule_area (on_board no) (polyline (pts (xy 20 30) (xy 40 30) "
                          "(xy 40 50))))"}


@pytest.mark.parametrize("construct", sorted(LABEL_CONSTRUCTS))
def test_a_bus_label_a_variable_label_and_an_excluded_rule_area_are_refused(tmp_path, construct):
    sheet = _write(tmp_path / "s.kicad_sch", LABEL_CONSTRUCTS[construct] + "\n")
    with pytest.raises(n.KicadConstruct) as raised:
        n.netlist(str(sheet))
    assert (raised.value.construct, raised.value.at) == (construct, (200000, 300000))
    assert raised.value.schematic_version == "20250114"


def test_a_rule_area_on_the_board_is_read(tmp_path):
    body = (LABEL_CONSTRUCTS["excluded_rule_area"].replace("on_board no", "on_board yes")
            + _part("R1", 10, 10, _uuid(1)) + '(global_label "G" (at 10 10 0))\n')
    assert _nets(n.netlist(str(_write(tmp_path / "s.kicad_sch", body)))) == {"G": [("R1", "1")]}


@pytest.mark.parametrize("body", [
    "(wire (pts (xy 0 0)))", '(symbol (lib_id "t:R") (uuid "x"))', '(junction (at 1 mm))',
    "(label)"])
def test_a_node_without_what_kicad_requires_is_an_input_error(tmp_path, body):
    with pytest.raises(n.InputError, match="not a readable schematic"):
        n.netlist(str(_write(tmp_path / "s.kicad_sch", body + "\n")))


def test_a_jumper_flag_other_than_yes_or_no_is_an_input_error(tmp_path):
    lib = LIB.replace('(symbol "t:R" ', '(symbol "t:R" (duplicate_pin_numbers_are_jumpers maybe) ')
    with pytest.raises(n.InputError, match="neither yes nor no"):
        n.netlist(str(_write(tmp_path / "s.kicad_sch", _part("R1", 10, 10, _uuid(1)), lib=lib)))


# ------------------------------------------------------------------- both ways

FAKE_KICAD_CLI = """\
import pathlib, shutil, sys
argv = sys.argv[1:]
out, sheet = pathlib.Path(argv[argv.index("--output") + 1]), pathlib.Path(argv[-1])
source = sheet.parent / ("sheet.net" if "netlist" in argv else "erc.json")
act = {act!r}
if act == "none":
    pass
elif act == "copy" or "erc" in argv:
    shutil.copy(source, out)
elif act == "rename":
    out.write_text(source.read_text().replace('(name "STK")', '(name "STK2")'))
else:
    out.write_text("(not a netlist")
"""


def _fake_kicad_cli(tmp_path, act):
    """A kicad-cli that writes the fixture's own netlist and ERC report
    (``copy``), renames net STK in the netlist (``rename``), writes a netlist
    that is not one (``garbage``) or writes nothing (``none``)."""
    exe = tmp_path / f"kicad-cli-{act}"
    exe.write_text(f"#!{sys.executable}\n" + FAKE_KICAD_CLI.format(act=act), encoding="utf-8")
    exe.chmod(0o755)
    return str(exe)


def _both_ways(tmp_path, case, kicad_cli):
    sheet, library = _copy(tmp_path, case)
    return k.read_both_ways(sheet, library=library, mode="blank", kicad_cli=kicad_cli)


def test_differences_name_every_leaf_where_two_forms_part():
    form = {"symbols": [{"ref": "U1", "pins": ["1", "2"]}], "nets": [{"name": "A"}],
            "refusals": [{"code": "local_label", "message": "m"}]}
    other = json.loads(json.dumps(form))
    other["symbols"][0]["pins"][1] = "3"
    other["nets"][0]["name"] = "B"
    other["refusals"][0]["message"] = "n"
    assert k._differences(form, form) == []
    assert k._differences(form, other) == [
        {"at": "symbols[0].pins[1]", "kicad_cli": "2", "native": "3"},
        {"at": "nets[0].name", "kicad_cli": "A", "native": "B"},
        {"at": "refusals[0].message", "kicad_cli": "m", "native": "n"}]


def test_a_place_one_form_lacks_has_only_the_other_side():
    assert k._differences({"nets": [1], "a": 1}, {"nets": [1, 2], "b": None}) == [
        {"at": "nets[1]", "native": 2}, {"at": "a", "kicad_cli": 1},
        {"at": "b", "native": None}]


@pytest.mark.parametrize("case", ["stacked", "subsheet_local"])
def test_a_sheet_read_alike_both_ways_matches_though_front_ends_differ(tmp_path, case):
    result = _both_ways(tmp_path, case, _fake_kicad_cli(tmp_path, "copy"))
    assert result == {"word": "matches", "kicad_cli_version": "Eeschema 9.99.0-unknown",
                      "netlister": n.NETLISTER, "differences": []}
    assert json.loads(json.dumps(result)) == result


def test_reads_that_disagree_differ_by_place(tmp_path):
    result = _both_ways(tmp_path, "stacked", _fake_kicad_cli(tmp_path, "rename"))
    assert (result["word"], result["differences"]) == (
        "differs", [{"at": "nets[0].name", "kicad_cli": "STK2", "native": "STK"}])


def test_a_sheet_the_native_read_refuses_is_refused_with_its_reason(tmp_path):
    result = _both_ways(tmp_path, "k_bus", "no-such-kicad-cli")
    assert result == {"word": "refused", "kicad_cli_version": None, "netlister": n.NETLISTER,
                      "reason": "ext_k_bus.kicad_sch: bus at (50.8, 76.2) mm is not read by "
                                f"the native netlister{REMEDY}"}


@pytest.mark.parametrize("act, word, reason", [
    (None, "unavailable", "no-such-kicad-cli is not on PATH"),
    ("none", "failed", "kicad-cli wrote no netlist"),
    ("garbage", "failed",
     "kicadsexpr netlist sheet.net is unreadable: unbalanced parenthesis")])
def test_without_two_reads_the_word_says_why_and_names_no_temporary_file(tmp_path, act, word,
                                                                         reason):
    exe = _fake_kicad_cli(tmp_path, act) if act else "no-such-kicad-cli"
    result = _both_ways(tmp_path, "stacked", exe)
    assert result == {"word": word, "kicad_cli_version": None, "netlister": n.NETLISTER,
                      "reason": reason}


@pytest.mark.kicad_cli
@pytest.mark.parametrize("case", ["stacked", "subsheet_local"])
def test_kicad_cli_and_the_native_read_match_and_leave_the_sheet_folder_alone(tmp_path, case):
    if shutil.which("kicad-cli") is None:
        if os.environ.get("GDS_TO_KICAD_REQUIRE_KICAD_CLI") == "1":
            pytest.fail("GDS_TO_KICAD_REQUIRE_KICAD_CLI=1 but kicad-cli is not on PATH")
        pytest.skip("kicad-cli not on PATH (GDS_TO_KICAD_REQUIRE_KICAD_CLI=1 requires it)")
    sheet, library = _copy(tmp_path, case)
    before = sorted(tmp_path.rglob("*"))
    result = k.read_both_ways(sheet, library=library, mode="blank")
    assert (result["word"], result["differences"]) == ("matches", [])
    assert sorted(tmp_path.rglob("*")) == before
