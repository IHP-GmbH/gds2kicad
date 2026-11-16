# SPDX-License-Identifier: GPL-3.0-or-later
"""cosim_setup_sheets: a co-simulation setup projection drawn on KiCad sheets.

Hermetic: the projection, the .chiplet and the pin lists are written into
tmp_path, the symbol spec is the copy in tests/fixtures (PROVENANCE names it), and the sheets are read back by geometry (pin ends, wires, labels,
no-connects). The tests at the end need kicad-cli; set
GDS_TO_KICAD_REQUIRE_KICAD_CLI=1 to make a missing one a failure.
"""

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chiplet_to_kicad_sch as g  # noqa: E402
import cosim_setup_sheets as cs  # noqa: E402
import kicad_sch_to_connectivity as k  # noqa: E402
import sexpr_io as sx  # noqa: E402
import sheet_check as sc  # noqa: E402
from tests import test_chiplet_to_kicad_sch as gt  # noqa: E402

SPEC = Path(__file__).resolve().parent / "fixtures" / "cosim_setup_symbols.json"


def _on(net):
    return {"net": net, "named": True}


def _sym(ref, kind, pins=None, /, **attributes):
    return {"ref": ref, "kind": kind, "attributes": attributes, "pins": pins or {}}


def _el(kind, name, a, b, value):
    return {"kind": kind, "name": name, "nodes": [a, b], "value": value}


def _src(dc=None, function=None, args=()):
    return {"dc": dc, "function": function, "args": list(args)}


def _card_text(value):
    """An element value as its SPICE card writes it."""
    if isinstance(value, str):
        return value
    if value["function"] is None:
        return f"dc {value['dc']}"
    return f"{value['function']}({' '.join(value['args'])})"


PULSE = ["0", "1", "5e-9", "1e-8", "1e-8", "1e-8", "3e-8"]
# The model of an R, C or L value that is not a plain decimal: its exact one.
PLAIN = {"5e-12": "0.000000000005", "2.322105e-14": "0.00000000000002322105"}
PROJECTION = {
    "format": "chiplet_cosim.cosim_setup", "version": "1.0",
    "tool": {"name": "chiplet-cosim", "version": "0.6"},
    "sheets": [
        {"role": "system", "symbols": [
            _sym("SIM1", "simulation", cosim_base_sha256="ab" * 32,
                 chiplet_base_sha256="cd" * 32, lock="cosim.lock", tran_step="1e-11",
                 tran_stop="4.5e-8", temp="27", stimulus_module="bench/loop_case.py",
                 stimulus_tests="the_loop_answers"),
            _sym("MOD1", "models", pdk="ihp-sg13g2", revision="bf07",
                 files="models/cornerMOSlv.lib@mos_tt stdcell.spice", order="0"),
            _sym("SUP1", "supply", net="VDD", volts="1.2"),
            _sym("CRS1", "crossing", net="sig0", kind="channel", channel="channel_x.spice",
                 subckt="ch_x", source="placeholder", origin="channel_origin.json",
                 edge="1e-9"),
            _sym("CRS2", "crossing", net="sig1", kind="ideal"),
            _sym("BENCH1", "bench", {"IN": _on("sig0"), "OUT": _on("probe"), "SPARE": None},
                 file="bench/divider.cir", subckt="divider", sides="IN=U2"),
        ]},
        {"role": "die", "die": "U1", "symbols": [
            _sym("DIE1", "die", die="U1", top_cell="core", treatment="modelled",
                 view="rtl", pdk="gf180mcuD", locals="tx_pad"),
            _sym("PRT1", "port", port="tx", role="signal", direction="output", order="0"),
            _sym("BLK1", "block", {"I": _on("tx_pad"), "ZN": _on("sig0"), "VDD": _on("VDD")},
                 name="drv", cell="inv_16"),
            _sym("CNV1", "conversion", {"net": _on("tx_pad")}, port="tx", supply="VDD",
                 low="0", high="3.3", rise="1e-9", fall="1e-9"),
            _sym("PRB1", "probe", net="tx_pad"),
        ]},
        {"role": "bench", "symbols": [
            _sym("SUB1", "subckt", role="bench", subckt="divider"),
            _sym("P1", "subckt_port", {"node": _on("IN")}, name="IN", order="0"),
            _sym("P2", "subckt_port", {"node": _on("OUT")}, name="OUT", order="1"),
        ], "elements": [
            _el("R", "R1", "IN", "OUT", "12800"),
            _el("R", "R2", "OUT", "0", "10000"),
            _el("C", "CINT", "OUT", "0", "5e-12"),
            _el("V", "VSEL", "SEL", "0", _src(dc="0.45")),
            _el("V", "VBK", "bk", "0", _src(function="pulse", args=PULSE)),
            _el("I", "IB", "IBIAS", "0", _src(dc="8e-5")),
        ]},
        {"role": "channel", "net": "sig0", "symbols": [
            _sym("SUB1", "subckt", role="channel", subckt="ch_x", net="sig0"),
            _sym("P1", "subckt_port", {"node": _on("a")}, name="a", order="0"),
            _sym("P2", "subckt_port", {"node": _on("b")}, name="b", order="1"),
        ], "elements": [
            _el("R", "R1", "a", "n1", "1.375"),
            _el("C", "CL1", "n1", "0", "2.322105e-14"),
            _el("R", "R2", "n1", "b", "1.375"),
        ]},
    ]}

FILES = {("system", None): "sys.draft.kicad_sch", ("die", "U1"): "sys.draft.die.U1.kicad_sch",
         ("bench", None): "sys.draft.bench.kicad_sch",
         ("channel", "sig0"): "sys.draft.channel.sig0.kicad_sch"}
FILES_ALONE = {**FILES, ("system", None): "sys.draft.system.kicad_sch"}
# Every kind whose pins the spec fixes, in spec order: no bench, no block.
LIBRARY = ["simulation", "models", "supply", "crossing", "die", "port", "conversion", "probe",
           "subckt", "subckt_port"]


def _write_projection(tmp_path, projection=None):
    path = tmp_path / "cosim.setup.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(PROJECTION if projection is None else projection),
                    encoding="utf-8")
    return path


def _generate(tmp_path, projection=None):
    cpath, pins = gt._inputs(tmp_path)
    out = tmp_path / "pnr" / "sys.draft.kicad_sch"
    result = g.generate(cpath, pins, out, setup_path=_write_projection(tmp_path, projection),
                        spec_path=SPEC)
    return out, result


def _generate_alone(tmp_path, projection=None):
    """Drawn with setup_only, from a .chiplet whose dies have no pin lists."""
    cpath, _ = gt._inputs(tmp_path, pins={})
    out = tmp_path / "pnr" / "sys.draft.kicad_sch"
    result = g.generate(cpath, {}, out, setup_path=_write_projection(tmp_path, projection),
                        spec_path=SPEC, setup_only=True)
    return out, result


def _sheets(out):
    return {ident: out.parent / name for ident, name in FILES.items()}


def _symbols(sch_path):
    """Reference -> {property: value} and the instance node, per symbol."""
    tree = sx.parse(Path(sch_path).read_text(encoding="utf-8"))
    out = {}
    for inst in sx.find_all(tree, "symbol"):
        props = {str(p[1]): str(p[2]) for p in sx.find_all(inst, "property")}
        out[props["Reference"]] = (props, inst)
    return out


def _flag(inst, tag):
    return str(sx.find(inst, tag)[1])


def _expected_nets(sheet):
    """Net -> {(reference, pin)} and the open pins, as the projection states them."""
    nets, free = {}, set()
    for row in sheet["symbols"]:
        for pin, at in row["pins"].items():
            if at is None:
                free.add((row["ref"], pin))
            else:
                nets.setdefault(at["net"], set()).add((row["ref"], pin))
    for row in sheet.get("elements", []):
        for pin, node in zip(("1", "2"), row["nodes"]):
            nets.setdefault(node, set()).add((row["name"], pin))
    return nets, free


# ------------------------------------------------------------------- written

def test_every_sheet_is_written_beside_the_system_sheet(tmp_path):
    out, result = _generate(tmp_path)
    got = {(s["role"], s.get("die") or s.get("net")): (s["path"].name, s["sha256"])
           for s in result["setup_sheets"]}
    assert {k: v[0] for k, v in got.items()} == FILES
    for (role, name), (file, sha) in got.items():
        assert hashlib.sha256((out.parent / file).read_bytes()).hexdigest() == sha
    assert got[("system", None)][1] == result["schematic_sha256"]


def test_setup_only_writes_the_setup_sheets_and_no_system_sheet(tmp_path):
    out, result = _generate_alone(tmp_path)
    got = {(s["role"], s.get("die") or s.get("net")): s["path"].name
           for s in result["setup_sheets"]}
    assert got == FILES_ALONE and result["schematic_sha256"] is None
    assert sorted(p.name for p in out.parent.iterdir()) == sorted(
        [*FILES_ALONE.values(), cs.LIB_FILE, cs.LIB_TABLE])


def test_setup_only_puts_the_system_setup_symbols_on_a_sheet_of_their_own(tmp_path):
    out, _ = _generate_alone(tmp_path)
    path = out.parent / FILES_ALONE[("system", None)]
    sheet = PROJECTION["sheets"][0]
    assert sorted(_symbols(path)) == sorted(row["ref"] for row in sheet["symbols"])
    got = gt._sheet_connectivity(path)
    nets, free = _expected_nets(sheet)
    assert got["nets"] == {n: frozenset(eps) for n, eps in nets.items()}
    assert got["unconnected"] == frozenset(free)


def test_setup_only_draws_every_other_sheet_as_beside_a_system_sheet(tmp_path):
    out_a, _ = _generate(tmp_path / "a")
    out_b, _ = _generate_alone(tmp_path / "b")
    for (role, _), name in FILES.items():
        if role != "system":
            assert (out_a.parent / name).read_bytes() == (out_b.parent / name).read_bytes()


def test_reruns_are_byte_stable(tmp_path):
    out_a, _ = _generate(tmp_path / "a")
    out_b, _ = _generate(tmp_path / "b")
    for ident in FILES:
        assert _sheets(out_a)[ident].read_bytes() == _sheets(out_b)[ident].read_bytes()


def test_every_setup_symbol_carries_its_kind_and_attributes(tmp_path):
    out, _ = _generate(tmp_path)
    for sheet in PROJECTION["sheets"]:
        drawn = _symbols(_sheets(out)[(sheet["role"], sheet.get("die") or sheet.get("net"))])
        for row in sheet["symbols"]:
            props, inst = drawn[row["ref"]]
            assert props[g.SETUP_MARKER] == row["kind"]
            assert {n: props[n] for n in row["attributes"]} == row["attributes"]
            assert (_flag(inst, "exclude_from_sim"), _flag(inst, "in_bom"),
                    _flag(inst, "on_board")) == ("yes", "no", "yes")


def test_pins_sit_on_their_nets_and_open_pins_are_flagged(tmp_path):
    out, _ = _generate(tmp_path)
    for sheet in PROJECTION["sheets"][1:]:
        got = gt._sheet_connectivity(_sheets(out)[(sheet["role"],
                                                   sheet.get("die") or sheet.get("net"))])
        nets, free = _expected_nets(sheet)
        assert got["nets"] == {n: frozenset(eps) for n, eps in nets.items()}
        assert got["unconnected"] == frozenset(free)


def test_the_system_sheet_keeps_its_dies_and_adds_the_bench(tmp_path):
    out, result = _generate(tmp_path)
    got = gt._sheet_connectivity(out)
    nets, free = gt._expected()
    bench, open_pins = _expected_nets(PROJECTION["sheets"][0])
    for name, eps in bench.items():
        nets[name] = nets.get(name, frozenset()) | eps
    assert got["nets"] == nets
    assert got["unconnected"] == free | open_pins
    assert got["base_sha256"] == result["chiplet_sha256"]


def test_setup_symbols_sit_below_the_dies(tmp_path):
    out, _ = _generate(tmp_path)
    drawn = _symbols(out)
    dies = max(float(sx.find(inst, "at")[2]) for ref, (props, inst) in drawn.items()
               if g.SETUP_MARKER not in props)
    setup = min(float(sx.find(inst, "at")[2]) for ref, (props, inst) in drawn.items()
                if g.SETUP_MARKER in props)
    assert setup > dies


def test_primitives_are_raw_spice_models_named_for_spice(tmp_path):
    out, _ = _generate(tmp_path)
    for sheet in PROJECTION["sheets"]:
        if "elements" not in sheet:
            continue
        drawn = _symbols(_sheets(out)[(sheet["role"], sheet.get("net"))])
        for row in sheet["elements"]:
            props, inst = drawn[row["name"]]
            text = _card_text(row["value"])
            model = PLAIN.get(text, text)
            assert g.SETUP_MARKER not in props and _flag(inst, "exclude_from_sim") == "no"
            assert props["Value"] == text
            sim = {n: v for n, v in props.items() if n.startswith("Sim.")}
            assert sim == {"Sim.Device": "SPICE", "Sim.Pins": "1=1 2=2",
                           "Sim.Params": f'type="{row["kind"]}" model="{model}" lib=""'}


@pytest.mark.parametrize("role,name,value,model", [
    ("channel", "CL1", "1.161053e-14", "0.00000000000001161053"),
    ("bench", "R1", "1e6", "1000000"),
], ids=["exponent", "large"])
def test_an_rcl_model_is_the_exact_plain_decimal_of_its_value(tmp_path, role, name, value,
                                                                model):
    def edit(p):
        next(e for e in _sheet(p, role)["elements"] if e["name"] == name)["value"] = value
    out, _ = _generate(tmp_path, _mutated(edit))
    props = _symbols(_sheets(out)[(role, "sig0" if role == "channel" else None)])[name][0]
    assert (props["Value"], props["Sim.Params"]) == (
        value, f'type="{name[0]}" model="{model}" lib=""')


def test_bench_and_channel_sheets_say_annotating_renames(tmp_path):
    out, _ = _generate(tmp_path)
    for ident, path in _sheets(out).items():
        title = sx.find(sx.parse(path.read_text(encoding="utf-8")), "title_block")
        notes = [str(c[2]) for c in sx.find_all(title, "comment") if str(c[1]) == "3"]
        assert notes == ([cs.ANNOTATION_NOTE] if ident[0] in ("bench", "channel") else []), ident


def test_bench_and_channel_sheets_say_how_to_type_a_value(tmp_path):
    out, _ = _generate(tmp_path)
    for ident, path in _sheets(out).items():
        title = sx.find(sx.parse(path.read_text(encoding="utf-8")), "title_block")
        notes = [str(c[2]) for c in sx.find_all(title, "comment") if str(c[1]) == "4"]
        assert notes == ([cs.VALUE_NOTE] if ident[0] in ("bench", "channel") else []), ident


def test_a_short_pulse_is_drawn_as_given_never_padded(tmp_path):
    def short(p):
        _sheet(p, "bench")["elements"][4]["value"]["args"] = ["0", "1", "5e-9"]
    out, _ = _generate(tmp_path, _mutated(short))
    props = _symbols(_sheets(out)[("bench", None)])["VBK"][0]
    assert (props["Value"], props["Sim.Params"]) == (
        "pulse(0 1 5e-9)", 'type="V" model="pulse(0 1 5e-9)" lib=""')


def test_every_point_is_on_the_grid(tmp_path):
    out, _ = _generate(tmp_path)
    for path in _sheets(out).values():
        tree = sx.parse(path.read_text(encoding="utf-8"))
        points = [(xy[1], xy[2]) for xy in sx.find_all_rec(tree, "xy")]
        for tag in ("no_connect", "global_label", "symbol"):
            points += [tuple(sx.find(n, "at")[1:3]) for n in sx.find_all(tree, tag)]
        for v in [float(c) for p in points for c in p]:
            assert abs(v / 1.27 - round(v / 1.27)) < 1e-6, (path.name, v)


def test_the_spec_copy_is_the_one_the_readers_share():
    kinds = cs.load_spec(SPEC)
    assert {e["sheet"] for e in kinds.values()} == set(cs.SPEC_SHEET.values())
    assert k.SETUP_MARKER == g.SETUP_MARKER == "chiplet_cosim_setup"


# ------------------------------------------------------------------ library

def _library(path):
    """Symbol name -> its node, of a .kicad_sym file, and the file's version."""
    tree = sx.parse(Path(path).read_text(encoding="utf-8"))
    return {str(n[1]): n for n in sx.find_all(tree, "symbol")}, str(sx.find(tree, "version")[1])


def test_the_library_holds_every_kind_with_fixed_pins(tmp_path):
    out, _ = _generate(tmp_path)
    library, version = _library(out.parent / cs.LIB_FILE)
    kinds = cs.load_spec(SPEC)
    assert list(library) == LIBRARY and version == "20241209"
    for kind, node in library.items():
        props = {str(p[1]): str(p[2]) for p in sx.find_all(node, "property")}
        assert {n: v for n, v in props.items() if not cs.kicad_own(n)} == {
            g.SETUP_MARKER: kind, **dict.fromkeys(kinds[kind]["required"], "")}, kind
        pins = [str(sx.find(p, "number")[1]) for p in sx.find_all_rec(node, "pin")]
        assert pins == kinds[kind]["pins"], kind


def test_a_sheet_embeds_the_library_symbol_of_each_kind_it_draws(tmp_path):
    out, _ = _generate(tmp_path)
    library, _ = _library(out.parent / cs.LIB_FILE)
    seen = set()
    for path in _sheets(out).values():
        cache = sx.find(sx.parse(path.read_text(encoding="utf-8")), "lib_symbols")
        for node in sx.find_all(cache, "symbol"):
            lib, _, name = str(node[1]).partition(":")
            if lib == cs.LIB and name in library:
                assert node[2:] == library[name][2:], (path.name, name)
                seen.add(name)
    assert sorted(seen) == sorted(LIBRARY)


def test_the_symbol_table_names_the_library_beside_the_sheets(tmp_path):
    out, _ = _generate(tmp_path)
    tree = sx.parse((out.parent / cs.LIB_TABLE).read_text(encoding="utf-8"))
    rows = [{str(n[0]): str(n[1]) for n in lib[1:]} for lib in sx.find_all(tree, "lib")]
    assert [(r["name"], r["type"], r["uri"]) for r in rows] == [
        ("chiplet_cosim", "KiCad", "${KIPRJMOD}/chiplet_cosim.kicad_sym")]


def test_another_symbol_table_is_refused_and_left_as_it_was(tmp_path):
    table = tmp_path / "pnr" / cs.LIB_TABLE
    table.parent.mkdir(parents=True)
    table.write_text("(sym_lib_table (version 7))\n", encoding="utf-8")
    with pytest.raises(g.SchematicRefused) as exc:
        _generate(tmp_path)
    assert [r["code"] for r in exc.value.refusals] == ["library_table"]
    assert [p.name for p in table.parent.iterdir()] == [cs.LIB_TABLE]
    assert table.read_text(encoding="utf-8") == "(sym_lib_table (version 7))\n"


def test_a_rerun_takes_its_own_symbol_table(tmp_path):
    out, _ = _generate(tmp_path)
    first = {p.name: p.read_bytes() for p in out.parent.iterdir()}
    _generate(tmp_path)
    assert {p.name: p.read_bytes() for p in out.parent.iterdir()} == first


def test_check_leaves_setup_symbols_out(tmp_path):
    nets, free = gt._expected()
    text = gt._netlist_text(dict(nets, bench_only=frozenset()), free,
                            {"U1": "U1", "U2": "U2"}, "ab" * 32)
    bench = (f'(comp (ref "BENCH1") (value "bench") (libsource (lib "chiplet_cosim")'
             f' (part "bench_BENCH1") (description ""))'
             f' (property (name "{g.SETUP_MARKER}") (value "bench")))\n')
    text = text.replace("(components ", "(components " + bench)
    text = text.replace('(name "sig0") (class "Default") ',
                        '(name "sig0") (class "Default") (node (ref "BENCH1") (pin "IN"))')
    text = text.replace('(name "bench_only") (class "Default") ',
                        '(name "bench_only") (class "Default") (node (ref "BENCH1") (pin "OUT"))')
    path = tmp_path / "sys.net"
    path.write_text(text, encoding="utf-8")
    gt._check_sheet(path)
    got = sc.read_kicad_netlist(path)
    assert "BENCH1" not in got["identity"] and "bench_only" not in got["nets"]
    assert sc.compare(nets, free, "ab" * 32, got) == []


# ----------------------------------------------------------------------- cli

def test_cli_reports_every_setup_sheet(tmp_path):
    projection = _write_projection(tmp_path)
    code, out, report = gt._cli(tmp_path, "--setup", str(projection), "--setup-spec", str(SPEC))
    assert code == g.EXIT_WRITTEN and report["version"] == "1.3"
    assert report["setup"]["projection"] == {
        "path": "../cosim.setup.json",
        "sha256": hashlib.sha256(projection.read_bytes()).hexdigest()}
    spec = report["setup"]["spec"]
    assert spec["sha256"] == hashlib.sha256(SPEC.read_bytes()).hexdigest()
    assert os.path.samefile(os.path.join(out.parent, spec["path"]), SPEC)
    rows = report["setup"]["sheets"]
    assert [(r["role"], r.get("die") or r.get("net"), r["file"]) for r in rows] == [
        (role, name, file) for (role, name), file in FILES.items()]
    for row in rows:
        assert row["sha256"] == hashlib.sha256((out.parent / row["file"]).read_bytes()).hexdigest()


def test_cli_setup_only_reports_the_setup_sheets_and_no_schematic(tmp_path):
    projection = _write_projection(tmp_path)
    code, out, report = gt._cli(tmp_path, "--setup-only", "--setup", str(projection),
                                "--setup-spec", str(SPEC), pins={})
    assert (code, report["word"], report["schematic"]) == (g.EXIT_WRITTEN, "written", None)
    assert "components" not in report and not out.exists()
    rows = report["setup"]["sheets"]
    assert [(r["role"], r.get("die") or r.get("net"), r["file"]) for r in rows] == [
        (role, name, file) for (role, name), file in FILES_ALONE.items()]


@pytest.mark.parametrize("alone", [False, True], ids=["setup", "setup_only"])
def test_cli_reports_the_library_and_its_table(tmp_path, alone):
    setup = ["--setup", str(_write_projection(tmp_path)), "--setup-spec", str(SPEC)]
    code, out, report = (gt._cli(tmp_path, "--setup-only", *setup, pins={}) if alone
                         else gt._cli(tmp_path, *setup))
    assert code == g.EXIT_WRITTEN
    for key, name in (("library", cs.LIB_FILE), ("library_table", cs.LIB_TABLE)):
        assert report["setup"][key] == {
            "file": name, "sha256": hashlib.sha256((out.parent / name).read_bytes()).hexdigest()}


def test_cli_setup_only_takes_the_setup_and_no_pins_or_check(tmp_path):
    setup = ["--setup", str(_write_projection(tmp_path)), "--setup-spec", str(SPEC)]
    for extra, pins in ((["--setup-only"], {}), (["--setup-only", *setup], None),
                        (["--setup-only", "--check", *setup], {})):
        with pytest.raises(SystemExit) as exc:
            gt._cli(tmp_path, *extra, pins=pins)
        assert exc.value.code == 2, extra
        assert not (tmp_path / "pnr").exists()


def test_cli_without_setup_reports_none(tmp_path):
    code, _, report = gt._cli(tmp_path)
    assert code == g.EXIT_WRITTEN and "setup" not in report


def test_cli_takes_the_projection_and_a_usable_spec_together(tmp_path):
    projection = _write_projection(tmp_path)
    other = tmp_path / "other_spec.json"
    other.write_text(json.dumps(dict(json.loads(SPEC.read_text(encoding="utf-8")),
                                     version="2.0")), encoding="utf-8")
    for extra in (["--setup", str(projection)], ["--setup-spec", str(SPEC)],
                  ["--setup", str(projection), "--setup-spec", str(other)]):
        with pytest.raises(SystemExit) as exc:
            gt._cli(tmp_path, *extra)
        assert exc.value.code == 2, extra
        assert not (tmp_path / "pnr").exists()


# ------------------------------------------------------------------ refusals

def _mutated(mutate):
    projection = copy.deepcopy(PROJECTION)
    mutate(projection)
    return projection


def _sheet(p, role):
    return next(s for s in p["sheets"] if s["role"] == role)


def _row(p, role, ref):
    return next(r for r in _sheet(p, role)["symbols"] if r["ref"] == ref)


def _set(target, key, value):
    target[key] = value


REFUSALS = {
    "projection": [
        lambda p: _set(p, "version", "2.0"),
        lambda p: _set(p["tool"], "name", "kicad"),
        lambda p: _set(p, "sheets", {}),
    ],
    "sheet_role": [
        lambda p: _set(_sheet(p, "bench"), "role", "board"),
        lambda p: p["sheets"].append(copy.deepcopy(_sheet(p, "die"))),
        lambda p: _sheet(p, "channel").pop("net"),
    ],
    "kind_sheet": [
        lambda p: _sheet(p, "system")["symbols"].append(_sym("PRB9", "probe", net="x")),
        lambda p: _set(_row(p, "system", "SUP1"), "kind", "meter"),
    ],
    "kind_pins": [
        lambda p: _set(_row(p, "system", "SUP1"), "pins", {"p": _on("VDD")}),
        lambda p: _set(_row(p, "die", "CNV1"), "pins", {}),
        lambda p: _set(_row(p, "die", "BLK1")["pins"], "a b", _on("VDD")),
    ],
    "reference": [
        lambda p: _set(_row(p, "system", "SUP1"), "ref", "SIM1"),
        lambda p: _set(_row(p, "system", "SUP1"), "ref", "u1"),
        lambda p: _set(_row(p, "die", "PRB1"), "ref", "1PRB"),
        lambda p: _set(_row(p, "bench", "P1"), "ref", "r1"),
    ],
    "net_name": [
        lambda p: _set(_row(p, "die", "BLK1")["pins"], "I", _on("tx pad")),
        lambda p: _set(_row(p, "system", "BENCH1")["pins"], "OUT", {"named": True}),
    ],
    "attribute": [
        lambda p: _set(_row(p, "system", "SUP1")["attributes"], "volts", "1.2\n"),
        lambda p: _set(_row(p, "system", "SUP1")["attributes"], "volts", '"1.2"'),
        lambda p: _set(_row(p, "system", "SUP1")["attributes"], "volts", "${V}"),
        lambda p: _set(_row(p, "system", "SUP1")["attributes"], "Value", "1"),
        lambda p: _set(_row(p, "system", "SUP1")["attributes"], "volts", 1.2),
        lambda p: _set(_row(p, "system", "SUP1")["attributes"], "Sheetfile", "x"),
        lambda p: _set(_row(p, "system", "SUP1")["attributes"], "ki_keywords", "x"),
        lambda p: _set(_row(p, "die", "PRB1")["attributes"], "path", ""),
    ],
    "element": [
        lambda p: _set(_sheet(p, "bench")["elements"][0], "name", "X1"),
        lambda p: _set(_sheet(p, "bench")["elements"][0], "nodes", ["IN", "OUT", "0"]),
        lambda p: _set(_sheet(p, "bench")["elements"][0], "value", "-1"),
        lambda p: _set(_sheet(p, "bench")["elements"][0], "value", "1k"),
        lambda p: _set(_sheet(p, "die"), "elements", [_el("R", "R9", "a", "b", "1")]),
    ],
    "kicad_source_form": [
        lambda p: _set(_sheet(p, "bench")["elements"][3], "value",
                       _src(function="sin", args=["0", "1", "1e6"])),
        lambda p: _set(_sheet(p, "bench")["elements"][4]["value"], "dc", "0"),
        lambda p: _set(_sheet(p, "bench")["elements"][3], "value", "0.45"),
        lambda p: _set(_sheet(p, "bench")["elements"][4]["value"], "args", PULSE + ["1"]),
    ],
}
CASES = [(code, i) for code, mutations in sorted(REFUSALS.items())
         for i in range(len(mutations))]


@pytest.mark.parametrize("code,index", CASES, ids=[f"{c}-{i}" for c, i in CASES])
def test_refused_by_name_and_nothing_written(tmp_path, code, index):
    with pytest.raises(g.SchematicRefused) as exc:
        _generate(tmp_path, _mutated(REFUSALS[code][index]))
    assert [r["code"] for r in exc.value.refusals] == [code]
    assert not (tmp_path / "pnr").exists()


def test_every_refusal_is_reported_not_just_the_first(tmp_path):
    def two(p):
        REFUSALS["kind_sheet"][1](p)
        REFUSALS["element"][2](p)
    with pytest.raises(g.SchematicRefused) as exc:
        _generate(tmp_path, _mutated(two))
    assert sorted(r["code"] for r in exc.value.refusals) == ["element", "kind_sheet"]


def test_a_system_setup_symbol_may_not_take_a_carrier_pads_reference(tmp_path):
    """A carrier pad is drawn as an EXT_PAD named by its id."""
    chiplet = copy.deepcopy(gt.CARRIER_CHIPLET)
    gt._pads(chiplet)[4]["id"] = "SUP1"
    cpath, pins = gt._inputs(tmp_path, chiplet=chiplet)
    with pytest.raises(g.SchematicRefused) as exc:
        g.generate(cpath, pins, tmp_path / "pnr" / "sys.kicad_sch",
                   setup_path=_write_projection(tmp_path), spec_path=SPEC)
    assert [(r["code"], r.get("ref")) for r in exc.value.refusals] == [("reference", "SUP1")]


def test_an_unreadable_projection_is_refused(tmp_path):
    cpath, pins = gt._inputs(tmp_path)
    bad = tmp_path / "cosim.setup.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(g.SchematicRefused) as exc:
        g.generate(cpath, pins, tmp_path / "pnr" / "sys.kicad_sch", setup_path=bad,
                   spec_path=SPEC)
    assert [r["code"] for r in exc.value.refusals] == ["projection"]


# ---------------------------------------------------------- KiCad itself

def _kicad_cli():
    exe = shutil.which("kicad-cli")
    if exe is None:
        if os.environ.get("GDS_TO_KICAD_REQUIRE_KICAD_CLI") == "1":
            pytest.fail("GDS_TO_KICAD_REQUIRE_KICAD_CLI=1 but kicad-cli is not on PATH")
        pytest.skip("kicad-cli not on PATH (GDS_TO_KICAD_REQUIRE_KICAD_CLI=1 requires it)")
    return exe


def _export(exe, sch, fmt, out):
    subprocess.run([exe, "sch", "export", "netlist", "--format", fmt, "--output", str(out),
                    str(sch)], check=True, capture_output=True, timeout=300)
    return out.read_text(encoding="utf-8")


@pytest.mark.kicad_cli
def test_kicad_cli_system_sheet_check_matches_with_setup(tmp_path):
    _kicad_cli()
    out, result = _generate(tmp_path)
    check = sc.run_check(out, result["nets"], result["unconnected"], result["chiplet_sha256"])
    assert check == {"word": "matches", "differences": []}


@pytest.mark.kicad_cli
def test_kicad_cli_lists_every_setup_symbol_with_its_fields(tmp_path):
    exe = _kicad_cli()
    out, _ = _generate(tmp_path)
    for sheet in PROJECTION["sheets"]:
        path = _sheets(out)[(sheet["role"], sheet.get("die") or sheet.get("net"))]
        tree = sx.parse(_export(exe, path, "kicadsexpr", path.with_suffix(".net")))
        comps = {}
        for comp in sx.find_all(sx.find(tree, "components"), "comp"):
            comps[k._value(comp, "ref")] = {k._value(p, "name"): k._value(p, "value")
                                            for p in sx.find_all(comp, "property")}
        for row in sheet["symbols"]:
            assert comps[row["ref"]][g.SETUP_MARKER] == row["kind"]
            for name, value in row["attributes"].items():
                assert comps[row["ref"]][name] == value


@pytest.mark.kicad_cli
def test_kicad_cli_spice_holds_the_elements_under_their_names_only(tmp_path):
    exe = _kicad_cli()
    out, _ = _generate(tmp_path)
    for role, name in (("bench", None), ("channel", "sig0")):
        path = _sheets(out)[(role, name)]
        text = _export(exe, path, "spice", path.with_suffix(".cir"))
        cards = {line.split()[0] for line in text.splitlines()
                 if line.strip() and line[0].isalpha()}
        sheet = next(s for s in PROJECTION["sheets"] if s["role"] == role)
        assert cards == {e["name"] for e in sheet["elements"]}, text
