# SPDX-License-Identifier: GPL-3.0-or-later
"""kicad_sch_to_cosim_setup: edited setup sheets read back as a setup form.

Hermetic: the sheets are drawn by chiplet_to_kicad_sch.py --setup from the
projection of test_cosim_setup_sheets, and each netlist is written as KiCad
writes one (its own properties included), from what the projection states and
the fields each primitive is drawn with; a SPICE card carries its raw model's
text as KiCad's number formatting writes it (``_to_spice``).
The test at the end exports real netlists and needs kicad-cli; set
GDS_TO_KICAD_REQUIRE_KICAD_CLI=1 to make a missing one a failure.
"""

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kicad_sch_to_cosim_setup as r  # noqa: E402
from tests import test_chiplet_to_kicad_sch as gt  # noqa: E402
from tests import test_cosim_setup_sheets as st  # noqa: E402

TOOL = "Eeschema 9.0.2"
# Properties KiCad adds to a component of its own; none is an attribute.
KICAD_OWN = (("Sheetname", "Root"), ("exclude_from_bom", ""), ("exclude_from_board", ""),
             ("dnp", ""), ("ki_keywords", "setup"))


# A raw model's text KiCad writes as it is, a number it reformats, and the SPICE
# prefix of a written exponent from the lowest exponent it covers.
_PLAIN = re.compile(r"[0-9.]*\Z")
_SI_NUMBER = re.compile(r"([+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+))(?:[eE]([+-]?[0-9]+))?\Z")
_PREFIXES = [(-15, "f"), (-12, "p"), (-9, "n"), (-6, "u"), (-3, "m"), (0, ""), (3, "k"),
             (6, "Meg"), (9, "G"), (12, "T")]


def _to_spice(text):
    """The card text KiCad writes for a raw SPICE model's text: SIM_VALUE::ToSpice
    in the KiCad-ADK-MOD fork (eeschema/sim/sim_model_raw_spice.cpp:112,
    sim_value.cpp:371-418). Digits and points go as they are; a number with a
    sign or an exponent goes to six significant digits ({:g}) under the SPICE
    prefix of its written exponent, none outside f to T; text that is not a
    number goes as it is. A unit prefix is not emulated: the generator draws
    none."""
    if _PLAIN.match(text):
        return text
    m = _SI_NUMBER.match(text)
    if m is None:
        assert not re.match(r"[-+.0-9]", text), f"{text!r} is not emulated"
        return text
    exponent = int(m.group(2) or 0)
    low, prefix = next(((lo, p) for lo, p in _PREFIXES if lo <= exponent < lo + 3), (0, ""))
    return "%g%s" % (float(m.group(1)) * 10 ** (exponent - low), prefix)


def _model(props):
    """The model text of a drawn primitive's Sim.Params."""
    return {m.group(1): m.group(2) if m.group(2) is not None else m.group(3)
            for m in r._PARAM.finditer(props["Sim.Params"])}["model"]


def _q(text):
    return json.dumps(text)


def _comp(ref, part, props, value="x"):
    fields = "".join(f"(property (name {_q(n)}) (value {_q(v)}))" for n, v in props)
    return (f"(comp (ref {_q(ref)}) (value {_q(value)}) (footprint \"\") (description \"\")"
            f" (libsource (lib \"chiplet_cosim\") (part {_q(part)}) (description \"\"))"
            f" {fields} (sheetpath (names \"/\") (tstamps \"/\")) (tstamps \"00\"))\n")


def _netlist_text(sheet, source, drawn=None, tool=TOOL, extra_comps="", extra_nets=(),
                  open_pins=True):
    """A kicadsexpr netlist of one projection sheet, as KiCad writes it; ``drawn``:
    each primitive's fields on the sheet, by reference."""
    comps, parts = extra_comps, ""
    for row in sheet["symbols"]:
        props = [(r.SETUP_MARKER, row["kind"])] + sorted(row["attributes"].items())
        comps += _comp(row["ref"], row["ref"], props + [("Sheetfile", source), *KICAD_OWN])
        pins = "".join(f"(pin (num {_q(p)}) (name {_q(p)}) (type \"passive\"))"
                       for p in row["pins"])
        parts += f"(libpart (lib \"chiplet_cosim\") (part {_q(row['ref'])}) (pins {pins}))\n"
    for row in sheet.get("elements", []):
        props = drawn[row["name"]]
        comps += _comp(row["name"], row["kind"],
                       [(n, props[n]) for n in ("Sim.Device", "Sim.Pins", "Sim.Params")]
                       + [("Sheetfile", source)], value=props["Value"])
    nets, free = st._expected_nets(sheet)
    body = list(extra_nets)
    for name, ends in sorted(nets.items()):
        body.append((name, sorted(ends)))
    for ref, pin in sorted(free) if open_pins else ():
        body.append((f"unconnected-({ref}-{pin}-Pad{pin})", [(ref, pin)]))
    text = "".join(
        f"(net (code \"{i}\") (name {_q(name)}) (class \"Default\") "
        + "".join(f"(node (ref {_q(a)}) (pin {_q(b)}) (pintype \"passive\"))" for a, b in ends)
        + ")\n" for i, (name, ends) in enumerate(body, 1))
    return (f"(export (version \"E\") (design (source {_q(source)}) (tool {_q(tool)})"
            f" (sheet (number \"1\") (name \"/\") (tstamps \"/\")))\n"
            f"(components {comps}) (libparts {parts}) (nets {text}))\n")


def _spice_text(sheet, drawn):
    """A SPICE netlist of one projection sheet, each card as KiCad writes the raw
    model drawn for it."""
    cards = "".join(f"{row['name']} {row['nodes'][0]} {row['nodes'][1]} "
                    f"{_to_spice(_model(drawn[row['name']]))}\n" for row in sheet["elements"])
    return f".title KiCad schematic\n{cards}.end\n"


def _drawn(tmp_path, alone=False):
    """The generator's report and every drawn sheet with its netlist (and SPICE);
    ``alone``: drawn with --setup-only."""
    setup = ["--setup", str(st._write_projection(tmp_path)), "--setup-spec", str(st.SPEC)]
    code, out, _ = (gt._cli(tmp_path, "--setup-only", *setup, pins={}) if alone
                    else gt._cli(tmp_path, *setup))
    assert code == 0
    files = st.FILES_ALONE if alone else st.FILES
    sheets = {}
    for sheet in st.PROJECTION["sheets"]:
        ident = (sheet["role"], sheet.get("die") or sheet.get("net"))
        sch = out.parent / files[ident]
        drawn = {ref: props for ref, (props, _) in st._symbols(sch).items()}
        net = sch.with_suffix(".net")
        net.write_text(_netlist_text(sheet, sch.name, drawn), encoding="utf-8")
        spice = None
        if sheet["role"] in r.SPICE_ROLES:
            spice = sch.with_suffix(".cir")
            spice.write_text(_spice_text(sheet, drawn), encoding="utf-8")
        sheets[ident] = (sch, net, spice)
    return out.parent / "schematic.json", sheets


def _read(tmp_path, report, sheets):
    return r.read_form(report, tmp_path / "form" / "cosim.setup.json", list(sheets))


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ------------------------------------------------------------------- the form

def test_every_sheet_reads_back_as_the_projection_drew_it(tmp_path):
    report, sheets = _drawn(tmp_path)
    form = _read(tmp_path, report, sheets.values())
    assert set(form) == {"format", "version", "tool", "sheets"}
    assert (form["format"], form["version"]) == ("chiplet_cosim.cosim_setup", "1.0")
    assert form["tool"] == {"name": "kicad", "version": TOOL}
    want = {(s["role"], s.get("die") or s.get("net")): s for s in st.PROJECTION["sheets"]}
    assert [(s["role"], s.get("die") or s.get("net")) for s in form["sheets"]] == list(want)
    for got in form["sheets"]:
        ident = (got["role"], got.get("die") or got.get("net"))
        sch, _, spice = sheets[ident]
        assert got["file"] == f"../pnr/{sch.name}" and got["sha256"] == _sha(sch)
        if spice is None:
            assert "netlist" not in got
        else:
            assert got["netlist"] == {"file": f"../pnr/{spice.name}", "sha256": _sha(spice)}
        assert got["symbols"] == sorted(want[ident]["symbols"], key=lambda s: s["ref"])


def test_a_setup_only_sheet_set_reads_back_as_the_projection_drew_it(tmp_path):
    report, sheets = _drawn(tmp_path, alone=True)
    form = _read(tmp_path, report, sheets.values())
    want = {(s["role"], s.get("die") or s.get("net")): s for s in st.PROJECTION["sheets"]}
    assert [(s["role"], s.get("die") or s.get("net"), s["file"]) for s in form["sheets"]] == [
        (*ident, f"../pnr/{name}") for ident, name in st.FILES_ALONE.items()]
    for got in form["sheets"]:
        ident = (got["role"], got.get("die") or got.get("net"))
        assert got["symbols"] == sorted(want[ident]["symbols"], key=lambda s: s["ref"])


def test_only_symbols_with_a_kind_are_read(tmp_path):
    sheet = {"role": "system", "symbols": [st._sym("SUP1", "supply", net="VDD", volts="1.2")]}
    extra = (_comp("U1", "die", [("CHIPLET_COMPONENT", "U1")])
             + _comp("SUP2", "supply", [(r.SETUP_MARKER, ""), ("net", "VSS")]))
    path = tmp_path / "sys.net"
    path.write_text(_netlist_text(sheet, "sys.kicad_sch", extra_comps=extra), encoding="utf-8")
    got = r.read_symbols(r._netlist(path, "sys.kicad_sch"))
    assert [s["ref"] for s in got] == ["SUP1"]
    assert got[0]["attributes"] == {"net": "VDD", "volts": "1.2"}


def test_every_pin_is_open_or_on_its_net_as_kicad_named_it(tmp_path):
    pins = ["IN", "OUT", "NC", "SHARED", "LOCAL", "NONE"]
    sheet = {"role": "system", "symbols": [st._sym("BENCH1", "bench", dict.fromkeys(pins))]}
    nets = [("sig0", [("BENCH1", "IN"), ("U1", "A0")]),
            ("Net-(BENCH1-OUT)", [("BENCH1", "OUT"), ("U1", "A1")]),
            ("unconnected-(BENCH1-NC-PadNC)", [("BENCH1", "NC")]),
            ("unconnected-(U1-A2-PadA2)", [("BENCH1", "SHARED"), ("U1", "A2")]),
            ("/loc", [("BENCH1", "LOCAL")])]
    path = tmp_path / "sys.net"
    path.write_text(_netlist_text(sheet, "sys.kicad_sch", extra_nets=nets, open_pins=False),
                    encoding="utf-8")
    got = r.read_symbols(r._netlist(path, "sys.kicad_sch"))[0]["pins"]
    assert got == {"IN": {"net": "sig0", "named": True},
                   "OUT": {"net": "Net-(BENCH1-OUT)", "named": False},
                   "NC": None,
                   "SHARED": {"net": "unconnected-(U1-A2-PadA2)", "named": False},
                   "LOCAL": {"net": "/loc", "named": True},
                   "NONE": None}


# --------------------------------------------------------------- input errors

BENCH, CHANNEL, DIE = ("bench", None), ("channel", "sig0"), ("die", "U1")


def _other(sheets, ident, slot, path):
    row = list(sheets[ident])
    row[slot] = path
    sheets[ident] = tuple(row)


def _edit_report(report, edit):
    doc = json.loads(report.read_text(encoding="utf-8"))
    edit(doc)
    report.write_text(json.dumps(doc), encoding="utf-8")


def _report_row(doc, role):
    return next(row for row in doc["setup"]["sheets"] if row["role"] == role)


def _edit_netlists(sheets, old, new, only=None):
    for ident, (_, net, _) in sheets.items():
        if only in (None, ident):
            text = net.read_text(encoding="utf-8")
            assert old in text
            net.write_text(text.replace(old, new), encoding="utf-8")


def _no_such_file(report, sheets):
    _other(sheets, BENCH, 1, report.parent / "missing.net")


def _report_unreadable(report, sheets):
    report.write_text("{", encoding="utf-8")


def _report_of_another_format(report, sheets):
    _edit_report(report, lambda doc: doc.update(format="gds2kicad.other"))


def _report_without_setup(report, sheets):
    _edit_report(report, lambda doc: doc.pop("setup"))


def _report_row_without_role(report, sheets):
    _edit_report(report, lambda doc: _report_row(doc, "system").pop("role"))


def _report_row_without_file(report, sheets):
    _edit_report(report, lambda doc: _report_row(doc, "die").pop("file"))


def _report_row_without_name(report, sheets):
    _edit_report(report, lambda doc: _report_row(doc, "channel").pop("net"))


def _report_file_twice(report, sheets):
    _edit_report(report, lambda doc: doc["setup"]["sheets"].append(
        dict(_report_row(doc, "die"), die="U2")))


def _sheet_not_drawn(report, sheets):
    stray = report.parent / "stray.kicad_sch"
    stray.write_text("", encoding="utf-8")
    _other(sheets, BENCH, 0, stray)


def _sheet_twice(report, sheets):
    sheets["again"] = sheets[BENCH]


def _netlist_of_another_sheet(report, sheets):
    _other(sheets, BENCH, 1, sheets[CHANNEL][1])


def _spice_on_a_die_sheet(report, sheets):
    _other(sheets, DIE, 2, sheets[BENCH][2])


def _no_spice_for_a_bench(report, sheets):
    _other(sheets, BENCH, 2, None)


def _two_tool_versions(report, sheets):
    _edit_netlists(sheets, TOOL, "Eeschema 8.0.8", only=BENCH)


def _netlist_without_tool(report, sheets):
    _edit_netlists(sheets, f"(tool {_q(TOOL)})", "")


def _netlist_without_source(report, sheets):
    _edit_netlists(sheets, '(source "sys.draft.bench.kicad_sch") ', "", only=BENCH)


def _netlist_unreadable(report, sheets):
    sheets[BENCH][1].write_bytes(b"\xff\xfe(export)")


def _not_a_netlist(report, sheets):
    sheets[BENCH][1].write_text("(kicad_sch (version 20250114))", encoding="utf-8")


def _edit_spice(sheets, ident, old, new):
    text = sheets[ident][2].read_text(encoding="utf-8")
    assert old in text
    sheets[ident][2].write_text(text.replace(old, new), encoding="utf-8")


def _card_rounded(report, sheets):
    _edit_spice(sheets, CHANNEL, "CL1 n1 0 0.00000000000002322105", "CL1 n1 0 23.2211f")


def _value_edited(report, sheets):
    _edit_netlists(sheets, '(value "12800")', '(value "15000")', only=BENCH)


def _typed_value_rounded(report, sheets):
    _edit_netlists(sheets, "(components ", "(components " + _comp(
        "R9", "R", [], value="1.234567k"), only=BENCH)
    _edit_spice(sheets, BENCH, ".end", "R9 IN 0 1.23457k\n.end")


def _typed_source_rounded(report, sheets):
    _edit_netlists(sheets, "(components ", "(components " + _comp(
        "V9", "V", [("Sim.Device", "V"), ("Sim.Type", "DC"), ("Sim.Params", "dc=1.234567")],
        value="VDC"), only=BENCH)
    _edit_spice(sheets, BENCH, ".end", "V9 SEL 0 DC 1.23457\n.end")


def _card_without_symbol(report, sheets):
    _edit_spice(sheets, BENCH, ".end", "R8 IN 0 1\n.end")


def _placed(net):
    """A probe placed from the setup library, its net field as given, as KiCad
    exports it."""
    return (_comp("SETUP1", "probe", [(r.SETUP_MARKER, "probe"), ("net", net),
                                      ("Sheetfile", "sys.draft.die.U1.kicad_sch")],
                  value="probe"), '(libpart (lib "chiplet_cosim") (part "probe") (pins ))\n')


def _attribute_left_empty(report, sheets):
    comp, part = _placed("")
    _edit_netlists(sheets, "(components ", "(components " + comp, only=DIE)
    _edit_netlists(sheets, "(libparts ", "(libparts " + part, only=DIE)


INPUT_ERRORS = [
    (_no_such_file, "no such file"),
    (_report_unreadable, "generator report .* is unreadable"),
    (_report_of_another_format, "is not a gds2kicad.system-schematic report"),
    (_report_without_setup, "lists no setup sheets"),
    (_report_row_without_role, "lists a setup sheet with role None"),
    (_report_row_without_file, "lists a die sheet without its file"),
    (_report_row_without_name, "lists a channel sheet without its net"),
    (_report_file_twice, "lists sys.draft.die.U1.kicad_sch twice"),
    (_sheet_not_drawn, "is not a setup sheet of"),
    (_sheet_twice, "is given twice"),
    (_netlist_of_another_sheet, "was exported from"),
    (_spice_on_a_die_sheet, "only a bench or channel sheet has a SPICE netlist"),
    (_no_spice_for_a_bench, "give its SPICE netlist"),
    (_two_tool_versions, "2 tool versions"),
    (_netlist_without_tool, "names no tool"),
    (_netlist_without_source, "names no source sheet"),
    (_netlist_unreadable, "netlist .* is unreadable"),
    (_not_a_netlist, "is not a kicadsexpr netlist"),
    (_card_rounded, "value_rounded: CL1 is '23.2211f' in .* its Sim.Params model is "
                    "'0.00000000000002322105'"),
    (_value_edited, "value_rounded: R1 is '12800' in .* its Value is '15000'"),
    (_typed_value_rounded, "value_rounded: R9 is '1.23457k' in .* its Value is '1.234567k'"),
    (_typed_source_rounded, "value_rounded: V9 is 'DC 1.23457' in .* its Sim.Params is "
                            "'dc=1.234567'"),
    (_card_without_symbol, "card R8 has no symbol"),
    (_attribute_left_empty, r"attribute_empty: SETUP1 \(probe\) in .* has net empty"),
]


@pytest.mark.parametrize("spoil,why", INPUT_ERRORS,
                         ids=[f.__name__[1:] for f, _ in INPUT_ERRORS])
def test_an_input_that_is_not_what_it_claims_is_refused(tmp_path, spoil, why):
    report, sheets = _drawn(tmp_path)
    spoil(report, sheets)
    with pytest.raises(r.InputError, match=why):
        _read(tmp_path, report, sheets.values())


def test_a_symbol_placed_from_the_library_reads_back_as_its_kind(tmp_path):
    report, sheets = _drawn(tmp_path)
    comp, part = _placed("tx_pad")
    _edit_netlists(sheets, "(components ", "(components " + comp, only=DIE)
    _edit_netlists(sheets, "(libparts ", "(libparts " + part, only=DIE)
    form = _read(tmp_path, report, sheets.values())
    die = next(s for s in form["sheets"] if s["role"] == "die")
    assert next(s for s in die["symbols"] if s["ref"] == "SETUP1") == {
        "ref": "SETUP1", "kind": "probe", "attributes": {"net": "tx_pad"}, "pins": {}}


def test_a_card_kicad_wrote_in_its_own_notation_is_read(tmp_path):
    report, sheets = _drawn(tmp_path)
    _edit_netlists(sheets, "(components ", "(components " + _comp(
        "V9", "V", [("Sim.Device", "V"), ("Sim.Type", "DC"), ("Sim.Params", "dc=8e-5")],
        value="VDC"), only=BENCH)
    _edit_spice(sheets, BENCH, ".end", "V9 SEL 0 DC 80u\n.end")
    assert len(_read(tmp_path, report, sheets.values())["sheets"]) == len(sheets)


# (a raw model's text, the card text KiCad writes for it)
CARDS = [
    ("0.00000000000002322105", "0.00000000000002322105"),
    ("1000000", "1000000"),
    ("2.322105e-14", "23.2211f"),
    ("1.161053e-14", "11.6105f"),
    ("4.702153e-12", "4.70215p"),
    ("5e-12", "5p"),
    ("1e6", "1Meg"),
    ("dc 8e-5", "dc 8e-5"),
    ("pulse(0 1 5e-9)", "pulse(0 1 5e-9)"),
]


@pytest.mark.parametrize("model,card", CARDS)
def test_the_stub_writes_a_raw_model_card_as_kicad_does(model, card):
    assert _to_spice(model) == card


# (a, read as KiCad reads a field, b, likewise, the same number list)
NUMBERS = [
    ("5n", False, "5e-9", False, True),
    ("DC 80u", False, "dc 8e-5", False, True),
    ("PULSE( 0 1 5n 10n )", False, "pulse(0 1 5e-9 1e-8)", False, True),
    ("10meg", False, "1e7", False, True),
    ("1.5M", True, "1.5meg", False, True),
    ("4.7\u00b5", True, "4.7u", False, True),
    ("y1=0 td=100ns", True, "0 1e-7", False, True),
    ("23.2211f", False, "2.322105e-14", False, False),
    ("10M", False, "10M", True, False),
    ("PULSE( 0 1 5n )", False, "pulse(0 1 5e-9 1e-8)", False, False),
]


@pytest.mark.parametrize("a,a_kicad,b,b_kicad,same", NUMBERS)
def test_numbers_are_read_as_ngspice_and_kicad_read_them(a, a_kicad, b, b_kicad, same):
    assert (r._numbers(a, kicad=a_kicad) == r._numbers(b, kicad=b_kicad)) == same


# ----------------------------------------------------------------------- cli

def _argv(report, out, sheets):
    argv = [str(report), str(out)]
    for row in sheets.values():
        argv += ["--sheet"] + [str(p) for p in row if p is not None]
    return argv


def test_cli_writes_the_form(tmp_path):
    report, sheets = _drawn(tmp_path)
    out = tmp_path / "form" / "cosim.setup.json"
    assert r.main(_argv(report, out, sheets)) == r.EXIT_WRITTEN
    assert json.loads(out.read_text(encoding="utf-8")) == _read(tmp_path, report, sheets.values())


def test_cli_writes_nothing_on_an_input_error(tmp_path):
    report, sheets = _drawn(tmp_path)
    out = tmp_path / "form" / "cosim.setup.json"
    _other(sheets, BENCH, 2, None)
    assert r.main(_argv(report, out, sheets)) == r.EXIT_INPUT
    assert not out.exists()
    with pytest.raises(SystemExit) as exc:
        r.main([str(report), str(out), "--sheet", str(sheets[("system", None)][0])])
    assert exc.value.code == 2 and not out.exists()


# ---------------------------------------------------------- KiCad itself

@pytest.mark.kicad_cli
def test_kicad_cli_reads_back_the_projection(tmp_path):
    exe = st._kicad_cli()
    report, sheets = _drawn(tmp_path)
    for ident, (sch, net, spice) in sheets.items():
        st._export(exe, sch, "kicadsexpr", net)
        if spice is not None:
            st._export(exe, sch, "spice", spice)
    form = _read(tmp_path, report, sheets.values())
    want = {(s["role"], s.get("die") or s.get("net")): s for s in st.PROJECTION["sheets"]}
    for got in form["sheets"]:
        ident = (got["role"], got.get("die") or got.get("net"))
        assert got["symbols"] == sorted(want[ident]["symbols"], key=lambda s: s["ref"]), ident
