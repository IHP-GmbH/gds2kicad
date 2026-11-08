# SPDX-License-Identifier: GPL-3.0-or-later
"""kicad_sch_to_connectivity: a kicad-cli netlist of the system sheet read back
as the connectivity form. Host tests read hand-built netlists; the tests at the
end need kicad-cli and run the generator, KiCad and this reader end to end."""
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
import kicad_sch_to_connectivity as k  # noqa: E402
import sexpr_io as sx  # noqa: E402
from tests import test_chiplet_to_kicad_sch as gt  # noqa: E402
from tests.sheet_drawing import draw_from_library  # noqa: E402

DIGEST = "ab" * 32
LIBPARTS = {"DIE_A": ["A0", "A1", "VDD1"], "DIE_B": ["B0", "VDD"]}
COMPS = {"U1": ("U1", "DIE_A"), "U2": ("U2", "DIE_B")}
NETS = {
    "sig0": [("U2", "B0"), ("U1", "A0")],
    "VDD": [("U1", "VDD1"), ("U2", "VDD")],
    "unconnected-(U1-A1-PadA1)": [("U1", "A1")],
}


def _netlist(comps=None, libparts=None, nets=None, sheets=("/",), digest=DIGEST,
             source="/w/sys.kicad_sch", props=None, lib="chiplet", libs=None):
    """``libs`` names a part's library when it is not ``lib``."""
    libs = libs or {}
    comps = COMPS if comps is None else comps
    props = props or {}
    libparts = LIBPARTS if libparts is None else libparts
    nets = NETS if nets is None else nets
    sheet_text = "".join(
        f'(sheet (number "{i}") (name "{name}") (tstamps "{name}")'
        f' (title_block (title "sys") (company) (rev) (date) (source "sys.kicad_sch")'
        + (f' (comment (number "1") (value "{g.BASE_DIGEST_PREFIX}{digest}"))'
           if digest is not None and name == "/" else "")
        + ' (comment (number "2") (value "generated"))))'
        for i, name in enumerate(sheets, 1))
    comp_text = "".join(
        f'(comp (ref "{ref}") (value "{part}")'
        + (f' (fields (field (name "{g.IDENTITY_PROPERTY}") "{ident}"))' if ident else "")
        + f' (libsource (lib "{libs.get(part, lib)}") (part "{part}") (description ""))'
        + (f' (property (name "{g.IDENTITY_PROPERTY}") (value "{ident}"))' if ident else "")
        + "".join(f' (property (name "{name}") (value "{value}"))'
                  for name, value in props.get(ref, {}).items())
        + ' (property (name "Sheetname") (value "")) (sheetpath (names "/") (tstamps "/")))\n'
        for ref, (ident, part) in comps.items())
    lib_text = "".join(
        f'(libpart (lib "{libs.get(part, lib)}") (part "{part}") (pins '
        + "".join(f'(pin (num "{n}") (name "{n}") (type "passive"))' for n in pins)
        + "))\n" for part, pins in libparts.items())
    net_text = "".join(
        f'(net (code "{i}") (name "{name}") (class "Default") '
        + "".join(f'(node (ref "{r}") (pin "{p}") (pintype "passive"))' for r, p in eps)
        + ")\n" for i, (name, eps) in enumerate(nets.items(), 1))
    return (f'(export (version "E")\n (design (source "{source}") (date "now")'
            f' (tool "Eeschema 9.0.2") {sheet_text})\n'
            f' (components {comp_text}) (libparts {lib_text}) (nets {net_text}))\n')


def _erc(violations=(), source="sys.kicad_sch"):
    # the shape kicad-cli 9.99 writes: no severity or ignored-check fields
    return {"$schema": "https://schemas.kicad.org/erc.v1.json", "source": source,
            "kicad_version": "9.99.0", "coordinate_units": "mm", "date": "now",
            "sheets": [{"path": "/", "uuid_path": "/", "violations": [
                {"type": t, "description": f"{t} here", "severity": "warning",
                 "items": [{"description": "Global Label 'A'", "uuid": "0"}]}
                for t in violations]}]}


def _files(tmp_path, netlist=None, erc=None):
    sheet = tmp_path / "kicad" / "sys.kicad_sch"
    sheet.parent.mkdir(parents=True, exist_ok=True)
    sheet.write_text("(kicad_sch)\n", encoding="utf-8")
    net = sheet.with_suffix(".net")
    net.write_text(_netlist() if netlist is None else netlist, encoding="utf-8")
    report = tmp_path / "pnr" / "schematic.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text('{"word": "written"}\n', encoding="utf-8")
    erc_path = None
    if erc is not None:
        erc_path = sheet.with_name("erc.json")
        erc_path.write_text(json.dumps(erc), encoding="utf-8")
    return net, sheet, report, erc_path


def _form(tmp_path, netlist=None, erc=None):
    net, sheet, report, erc_path = _files(tmp_path, netlist, erc)
    return k.read_form(net, sheet, report, sheet.with_name("connectivity.json"), erc=erc_path)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ------------------------------------------------------------------- the form

def test_form_of_an_unedited_netlist(tmp_path):
    form = _form(tmp_path)
    assert form["format"] == "chiplet_system.connectivity" and form["version"] == "1.0"
    assert form["base_sha256"] == DIGEST
    assert form["symbols"] == [
        {"ref": "U1", "identity": "U1", "pins": ["A0", "A1", "VDD1"]},
        {"ref": "U2", "identity": "U2", "pins": ["B0", "VDD"]},
    ]
    assert form["nets"] == [
        {"name": "VDD", "auto_named": False, "endpoints": [["U1", "VDD1"], ["U2", "VDD"]]},
        {"name": "sig0", "auto_named": False, "endpoints": [["U1", "A0"], ["U2", "B0"]]},
        {"name": "unconnected-(U1-A1-PadA1)", "auto_named": True, "endpoints": [["U1", "A1"]]},
    ]
    assert form["refusals"] == []


def test_front_end_and_inputs_are_named_by_path_and_digest(tmp_path):
    net, sheet, report, erc = _files(tmp_path, erc=_erc())
    form = k.read_form(net, sheet, report, sheet.with_name("connectivity.json"), erc=erc)
    assert form["generator_report"] == {"path": "../pnr/schematic.json", "sha256": _sha(report)}
    assert form["front_end"] == {
        "tool": "kicad", "tool_version": "Eeschema 9.0.2",
        "reader": "kicad_sch_to_connectivity 1.1",
        "inputs": [{"path": "sys.net", "sha256": _sha(net)},
                   {"path": "sys.kicad_sch", "sha256": _sha(sheet)},
                   {"path": "erc.json", "sha256": _sha(erc)}]}
    no_erc = k.read_form(net, sheet, report, sheet.with_name("connectivity.json"))
    assert [i["path"] for i in no_erc["front_end"]["inputs"]] == ["sys.net", "sys.kicad_sch"]


def test_auto_names_are_kicads_own(tmp_path):
    nets = {"Net-(U1-A0)": [("U1", "A0"), ("U2", "B0")], "Net_A": [("U1", "A1")]}
    form = _form(tmp_path, _netlist(nets=nets))
    assert {n["name"]: n["auto_named"] for n in form["nets"]} == {
        "Net-(U1-A0)": True, "Net_A": False}


def test_symbol_without_identity_is_listed_with_null(tmp_path):
    comps = dict(COMPS, U3=(None, "DIE_B"))
    form = _form(tmp_path, _netlist(comps=comps))
    assert {"ref": "U3", "identity": None, "pins": ["B0", "VDD"]} in form["symbols"]


def test_endpoints_stay_raw_and_unique(tmp_path):
    nets = {"sig0": [("U2", "B0"), ("U9", "X"), ("U2", "B0")]}
    form = _form(tmp_path, _netlist(nets=nets))
    # U9 is no symbol: resolving refs is the import's job, not the reader's
    assert form["nets"] == [{"name": "sig0", "auto_named": False,
                             "endpoints": [["U2", "B0"], ["U9", "X"]]}]
    assert form["refusals"] == []


def test_a_net_without_nodes_is_left_out(tmp_path):
    form = _form(tmp_path, _netlist(nets=dict(NETS, dangling=[])))
    assert "dangling" not in {n["name"] for n in form["nets"]}


def test_setup_symbols_and_their_endpoints_are_left_out(tmp_path):
    """A bench symbol wired to a die net and to a net of its own, and a pinless
    simulation block: the form equals the form of the sheet without them."""
    comps = dict(COMPS, BENCH1=(None, "BENCH"), SIM1=(None, "SIM"))
    libparts = dict(LIBPARTS, BENCH=["p", "n"], SIM=[])
    nets = dict(NETS, sig0=NETS["sig0"] + [("BENCH1", "p")], bench_n=[("BENCH1", "n")])
    props = {"BENCH1": {k.SETUP_MARKER: "bench"}, "SIM1": {k.SETUP_MARKER: "simulation"}}
    with_setup = _form(tmp_path / "a", _netlist(comps, libparts, nets, props=props))
    without = _form(tmp_path / "b")
    for key in ("symbols", "nets", "refusals", "base_sha256"):
        assert with_setup[key] == without[key]


def test_a_net_on_one_die_and_the_bench_reads_as_the_die_pin_alone(tmp_path):
    """Dropping the bench leaves its net one endpoint: the same net KiCad gives
    the labelled die pin on the sheet without the bench."""
    comps = dict(COMPS, BENCH1=(None, "BENCH"))
    libparts = dict(LIBPARTS, BENCH=["p"])
    base = {n: eps for n, eps in NETS.items() if n != "unconnected-(U1-A1-PadA1)"}
    props = {"BENCH1": {k.SETUP_MARKER: "bench"}}
    with_bench = _form(tmp_path / "a", _netlist(
        comps, libparts, dict(base, sense=[("BENCH1", "p"), ("U1", "A1")]), props=props))
    without = _form(tmp_path / "b", _netlist(nets=dict(base, sense=[("U1", "A1")])))
    for key in ("symbols", "nets", "refusals", "base_sha256"):
        assert with_bench[key] == without[key]
    assert {"name": "sense", "auto_named": False,
            "endpoints": [["U1", "A1"]]} in with_bench["nets"]


def test_an_empty_setup_marker_is_an_absent_one(tmp_path):
    comps = dict(COMPS, U3=(None, "DIE_B"))
    form = _form(tmp_path, _netlist(comps=comps, props={"U3": {k.SETUP_MARKER: ""}}))
    assert {"ref": "U3", "identity": None, "pins": ["B0", "VDD"]} in form["symbols"]


def test_cli_writes_strict_json_byte_stable(tmp_path):
    net, sheet, report, _ = _files(tmp_path)
    out = tmp_path / "form" / "connectivity.json"
    argv = [str(net), str(sheet), str(report), str(out), "--no-erc"]
    assert k.main(argv) == k.EXIT_WRITTEN
    first = out.read_bytes()
    assert json.loads(first)["front_end"]["inputs"][0]["path"] == "../kicad/sys.net"
    assert k.main(argv) == k.EXIT_WRITTEN and out.read_bytes() == first


# ---------------------------------------------------------------- refusals

def _codes(form):
    return [(r["code"], r.get("component"), r.get("terminal"), r.get("net"))
            for r in form["refusals"]]


def test_hierarchical_sheet_is_refused(tmp_path):
    form = _form(tmp_path, _netlist(sheets=("/", "/sub/")))
    assert _codes(form) == [("hierarchical_sheet", None, None, None)]


def test_local_label_is_refused(tmp_path):
    form = _form(tmp_path, _netlist(nets=dict(NETS, **{"/sig1": [("U1", "A1")]})))
    assert ("local_label", None, None, "/sig1") in _codes(form)
    assert "/sig1" in {n["name"] for n in form["nets"]}


def test_node_on_a_pin_its_symbol_lacks_is_refused(tmp_path):
    nets = dict(NETS, sig0=[("U1", "A0"), ("U2", "B9")])
    form = _form(tmp_path, _netlist(nets=nets))
    assert _codes(form) == [("symbol_pin_mismatch", "U2", "B9", "sig0")]


def test_symbol_listing_a_pin_twice_is_refused(tmp_path):
    # component is the ref even when the identity differs: resolving it is the import's job
    form = _form(tmp_path, _netlist(comps=dict(COMPS, U2=("U7", "DIE_B")),
                                    libparts=dict(LIBPARTS, DIE_B=["B0", "VDD", "B0"])))
    assert _codes(form) == [("symbol_pin_mismatch", "U2", "B0", None)]
    assert form["symbols"][1]["pins"] == ["B0", "VDD"]


def test_multiple_net_names_comes_from_erc(tmp_path):
    form = _form(tmp_path, erc=_erc(["multiple_net_names", "pin_not_connected"]))
    assert _codes(form) == [("multiple_net_names", None, None, None)]


def test_every_refusal_is_reported(tmp_path):
    netlist = _netlist(sheets=("/", "/sub/"),
                       nets=dict(NETS, **{"/x": [("U1", "A1")], "y": [("U1", "Q")]}))
    form = _form(tmp_path, netlist, erc=_erc(["multiple_net_names"]))
    assert sorted(r["code"] for r in form["refusals"]) == [
        "hierarchical_sheet", "local_label", "multiple_net_names", "symbol_pin_mismatch"]


# ------------------------------------------------------- a carrier's pads

PAD_LIBS = {"EXT_PAD": "chiplet_system"}
PAD_COMPS = dict(COMPS, J1=("ip", "EXT_PAD"), J2=("ip", "EXT_PAD"))
PAD_PARTS = dict(LIBPARTS, EXT_PAD=["1"])
PAD_NETS = {
    "sig0": [("U2", "B0"), ("U1", "A0"), ("J1", "1")],
    "VDD": [("U1", "VDD1"), ("U2", "VDD")],
    "unconnected-(U1-A1-PadA1)": [("U1", "A1")],
    "unconnected-(J2-Pad1)": [("J2", "1")],
}


def _pad_form(tmp_path, comps=PAD_COMPS, nets=PAD_NETS, pads=("J1", "J2")):
    """A sheet whose carrier ``ip`` the generator drew with ``pads``."""
    net, sheet, report, _ = _files(tmp_path, _netlist(comps=comps, libparts=PAD_PARTS,
                                                      nets=nets, libs=PAD_LIBS))
    pins = report.with_name("ip.carrier_pins.json")
    pins.write_text(json.dumps({"pins": [{"name": p} for p in pads]}), encoding="utf-8")
    report.write_text(json.dumps({
        "word": "written",
        "components": [{"id": "U1", "type": "die"}, {"id": "U2", "type": "die"},
                       {"id": "ip", "type": "interposer"}],
        "pins": {"ip": {"path": pins.name, "sha256": _sha(pins)}}}), encoding="utf-8")
    return k.read_form(net, sheet, report, sheet.with_name("connectivity.json"))


def test_a_carriers_pads_read_as_the_carrier(tmp_path):
    form = _pad_form(tmp_path)
    assert form["refusals"] == []
    assert form["symbols"] == [
        {"ref": "U1", "identity": "U1", "pins": ["A0", "A1", "VDD1"]},
        {"ref": "U2", "identity": "U2", "pins": ["B0", "VDD"]},
        {"ref": "ip", "identity": "ip", "pins": ["J1", "J2"]},
    ]
    nets = {n["name"]: n["endpoints"] for n in form["nets"]}
    assert nets["sig0"] == [["U1", "A0"], ["U2", "B0"], ["ip", "J1"]]
    assert nets["unconnected-(J2-Pad1)"] == [["ip", "J2"]]


def test_a_pad_the_generator_did_not_draw_is_refused_and_left_unfolded(tmp_path):
    form = _pad_form(tmp_path, comps=dict(PAD_COMPS, J3=("ip", "EXT_PAD")))
    assert _codes(form) == [("unknown_pad", "ip", "J3", None)]
    assert {"ref": "J3", "identity": "ip", "pins": ["1"]} in form["symbols"]


def test_a_pad_of_no_carrier_is_refused(tmp_path):
    form = _pad_form(tmp_path, comps=dict(PAD_COMPS, J2=("U1", "EXT_PAD")))
    assert _codes(form) == [("unknown_pad", "U1", "J2", None)]


def test_two_pads_under_one_reference_are_refused(tmp_path):
    netlist = _netlist(comps=PAD_COMPS, libparts=PAD_PARTS, nets=PAD_NETS, libs=PAD_LIBS)
    twice = netlist.replace("(comp (ref \"J2\")", "(comp (ref \"J1\")")
    net, sheet, report, _ = _files(tmp_path, twice)
    pins = report.with_name("ip.carrier_pins.json")
    pins.write_text(json.dumps({"pins": [{"name": "J1"}, {"name": "J2"}]}), encoding="utf-8")
    report.write_text(json.dumps({"components": [{"id": "ip", "type": "interposer"}],
                                  "pins": {"ip": {"path": pins.name, "sha256": _sha(pins)}}}),
                      encoding="utf-8")
    form = k.read_form(net, sheet, report, sheet.with_name("connectivity.json"))
    assert ("duplicate_reference", "J1", None, None) in _codes(form)


def test_a_pad_sharing_its_reference_with_a_die_is_refused(tmp_path):
    """The die is not folded into the carrier along with the pad."""
    netlist = _netlist(comps=PAD_COMPS, libparts=PAD_PARTS, nets=PAD_NETS, libs=PAD_LIBS)
    net, sheet, report, _ = _files(tmp_path, netlist.replace('(comp (ref "U2")',
                                                             '(comp (ref "J1")'))
    pins = report.with_name("ip.carrier_pins.json")
    pins.write_text(json.dumps({"pins": [{"name": "J1"}, {"name": "J2"}]}), encoding="utf-8")
    report.write_text(json.dumps({"components": [{"id": "ip", "type": "interposer"}],
                                  "pins": {"ip": {"path": pins.name, "sha256": _sha(pins)}}}),
                      encoding="utf-8")
    form = k.read_form(net, sheet, report, sheet.with_name("connectivity.json"))
    assert ("duplicate_reference", "J1", None, None) in _codes(form)
    assert {"ref": "ip", "identity": "ip", "pins": ["J2"]} in form["symbols"]


def test_a_node_on_a_pads_other_pin_is_refused(tmp_path):
    form = _pad_form(tmp_path, nets=dict(PAD_NETS, x=[("J2", "2"), ("U1", "A1")]))
    assert ("symbol_pin_mismatch", "J2", "2", "x") in _codes(form)


def test_a_pin_list_other_than_the_reports_is_not_accepted(tmp_path):
    _pad_form(tmp_path)
    (tmp_path / "pnr" / "ip.carrier_pins.json").write_text('{"pins": []}', encoding="utf-8")
    net, sheet = tmp_path / "kicad" / "sys.net", tmp_path / "kicad" / "sys.kicad_sch"
    with pytest.raises(k.InputError):
        k.read_form(net, sheet, tmp_path / "pnr" / "schematic.json", tmp_path / "c.json")


def test_a_pin_list_without_pins_is_not_accepted(tmp_path):
    _pad_form(tmp_path)
    pins = tmp_path / "pnr" / "ip.carrier_pins.json"
    pins.write_text("[]", encoding="utf-8")
    report = tmp_path / "pnr" / "schematic.json"
    doc = json.loads(report.read_text(encoding="utf-8"))
    doc["pins"]["ip"]["sha256"] = _sha(pins)
    report.write_text(json.dumps(doc), encoding="utf-8")
    net, sheet = tmp_path / "kicad" / "sys.net", tmp_path / "kicad" / "sys.kicad_sch"
    with pytest.raises(k.InputError, match="no 'pins' array"):
        k.read_form(net, sheet, report, tmp_path / "c.json")


# ------------------------------------------------------------- bad inputs

ERC_NOT_ACCEPTED = {
    "other_sheet": _erc(source="other.kicad_sch"),
    "no_source": {k_: v for k_, v in _erc().items() if k_ != "source"},
    "not_a_report": {"violations": []},
}


@pytest.mark.parametrize("case", sorted(ERC_NOT_ACCEPTED))
def test_erc_report_of_another_sheet_is_not_accepted(tmp_path, case):
    with pytest.raises(k.InputError):
        _form(tmp_path, erc=ERC_NOT_ACCEPTED[case])


BAD_NETLISTS = {
    "no_digest": _netlist(digest=None),
    "empty_digest": _netlist(digest=""),
    "other_sheet": _netlist(source="/w/other.kicad_sch"),
    "not_a_netlist": "(kicad_sch (version 20250114))\n",
    "unparsable": "(export (version \"E\"\n",
}


@pytest.mark.parametrize("case", sorted(BAD_NETLISTS))
def test_cli_writes_no_form_from_a_bad_input(tmp_path, case, capsys):
    net, sheet, report, _ = _files(tmp_path, netlist=BAD_NETLISTS[case])
    out = tmp_path / "connectivity.json"
    assert k.main([str(net), str(sheet), str(report), str(out), "--no-erc"]) == k.EXIT_INPUT
    assert not out.exists()
    assert capsys.readouterr().err.startswith("kicad_sch_to_connectivity: ")


def test_cli_needs_a_missing_file_named(tmp_path):
    net, sheet, report, _ = _files(tmp_path)
    out = tmp_path / "connectivity.json"
    code = k.main([str(net), str(sheet), str(tmp_path / "nope.json"), str(out), "--no-erc"])
    assert code == k.EXIT_INPUT and not out.exists()


@pytest.mark.parametrize("flags", [[], ["--no-erc", "--erc", "x.json"]], ids=["none", "both"])
def test_cli_wants_exactly_one_erc_choice(tmp_path, flags):
    net, sheet, report, _ = _files(tmp_path)
    with pytest.raises(SystemExit) as exc:
        k.main([str(net), str(sheet), str(report), str(tmp_path / "f.json"), *flags])
    assert exc.value.code == 2


# ------------------------------------------------------------ blank sheets

BLANK_COMPS = {"U1": (None, "DIE_A"), "U2": (None, "DIE_B")}


def _blank_files(tmp_path, netlist=None):
    net, sheet, _report, _ = _files(tmp_path, netlist if netlist is not None else _netlist(
        comps=BLANK_COMPS, digest=None, lib="chiplet_dies"))
    lib = tmp_path / "kicad" / "chiplet_dies.kicad_sym"
    lib.write_text("(kicad_symbol_lib)\n", encoding="utf-8")
    return net, sheet, lib


def _blank_form(tmp_path, netlist=None):
    net, sheet, lib = _blank_files(tmp_path, netlist)
    return k.read_form(net, sheet, None, sheet.with_name("connectivity.json"), library=lib)


def test_blank_form_names_the_library_and_each_part(tmp_path):
    form = _blank_form(tmp_path)
    net, sheet, lib = _blank_files(tmp_path)
    assert form["version"] == "1.1" and form["sheet"] == "blank"
    assert form["base_sha256"] is None and form["generator_report"] is None
    assert form["library"] == {"path": "chiplet_dies.kicad_sym", "sha256": _sha(lib)}
    assert form["symbols"] == [
        {"ref": "U1", "identity": None, "part": {"lib": "chiplet_dies", "name": "DIE_A"},
         "pins": ["A0", "A1", "VDD1"]},
        {"ref": "U2", "identity": None, "part": {"lib": "chiplet_dies", "name": "DIE_B"},
         "pins": ["B0", "VDD"]},
    ]
    assert form["nets"] == _form(tmp_path / "g")["nets"]
    assert form["refusals"] == []


def test_blank_form_ignores_an_identity_and_a_digest(tmp_path):
    form = _blank_form(tmp_path, _netlist(lib="chiplet_dies"))
    assert form["base_sha256"] is None
    assert {s["identity"] for s in form["symbols"]} == {None}


def test_two_symbols_under_one_reference_are_refused(tmp_path):
    netlist = _netlist(comps={"U1": (None, "DIE_A"), "U2": (None, "DIE_B")},
                       digest=None, lib="chiplet_dies").replace('(ref "U2")', '(ref "U1")')
    form = _blank_form(tmp_path, netlist)
    assert ("duplicate_reference", "U1", None, None) in _codes(form)
    assert [s["part"]["name"] for s in form["symbols"]] == ["DIE_A", "DIE_B"]


def test_generated_form_is_unchanged_by_the_blank_mode(tmp_path):
    form = _form(tmp_path)
    assert form["version"] == "1.0" and "sheet" not in form and "library" not in form
    assert all("part" not in s for s in form["symbols"])


def test_cli_takes_options_between_the_paths(tmp_path):
    net, sheet, report, _ = _files(tmp_path)
    out = tmp_path / "connectivity.json"
    assert k.main([str(net), str(sheet), "--no-erc", str(report), str(out)]) == k.EXIT_WRITTEN
    assert json.loads(out.read_text())["version"] == "1.0"


def test_cli_blank(tmp_path):
    net, sheet, lib = _blank_files(tmp_path)
    out = tmp_path / "form.json"
    argv = [str(net), str(sheet), str(out), "--no-erc"]
    assert k.main(argv + ["--blank", "--library", str(lib)]) == k.EXIT_WRITTEN
    first = out.read_bytes()
    assert json.loads(first)["sheet"] == "blank"
    assert k.main(argv + ["--blank", "--library", str(lib)]) == k.EXIT_WRITTEN
    assert out.read_bytes() == first
    for bad in (argv + ["--blank"], argv + ["--library", str(lib)],
                [str(net), str(sheet), str(lib), str(out), "--no-erc", "--blank",
                 "--library", str(lib)], argv):
        with pytest.raises(SystemExit) as exc:
            k.main(bad)
        assert exc.value.code == 2


# ---------------------------------------------------------- KiCad itself

def _need_kicad_cli():
    if shutil.which("kicad-cli") is None:
        if os.environ.get("GDS_TO_KICAD_REQUIRE_KICAD_CLI") == "1":
            pytest.fail("GDS_TO_KICAD_REQUIRE_KICAD_CLI=1 but kicad-cli is not on PATH")
        pytest.skip("kicad-cli not on PATH (GDS_TO_KICAD_REQUIRE_KICAD_CLI=1 requires it)")


def _export(sch):
    """Export the real netlist and ERC report, checking both commands."""
    net, erc = sch.with_suffix(".net"), sch.with_name("erc.json")
    for argv in (["sch", "export", "netlist", "--format", "kicadsexpr", "--output", net, sch],
                 ["sch", "erc", "--format", "json", "--severity-all", "--output", erc, sch]):
        run = subprocess.run(["kicad-cli", *map(str, argv)], capture_output=True, timeout=300)
        assert run.returncode == 0, run.stderr
    assert net.is_file() and erc.is_file()
    return net, erc


def _kicad_form(sch, report):
    """kicad-cli netlist and ERC of ``sch``, read back with --erc."""
    net, erc = _export(sch)
    return k.read_form(net, sch, report, sch.with_name("connectivity.json"), erc=erc)


def _drawn(tmp_path):
    code, out, report = gt._cli(tmp_path, chiplet=gt.CARRIER_CHIPLET)
    assert code == g.EXIT_WRITTEN
    return out, out.with_name("schematic.json"), report


def _edit(sch, edit):
    node = sx.parse(sch.read_text(encoding="utf-8"))
    edit(node)
    sch.write_text(sx.dumps(node) + "\n", encoding="utf-8")


def _labels(node, net):
    return [c for c in node if sx.head(c) == "global_label" and str(c[1]) == net]


def _named(form):
    return {n["name"]: {tuple(ep) for ep in n["endpoints"]}
            for n in form["nets"] if not n["auto_named"]}


@pytest.mark.kicad_cli
def test_kicad_cli_form_equals_the_description(tmp_path):
    _need_kicad_cli()
    sch, report_path, report = _drawn(tmp_path)
    form = _kicad_form(sch, report_path)
    assert form["refusals"] == []
    assert form["base_sha256"] == report["chiplet_sha256"]
    assert {s["ref"]: s["identity"] for s in form["symbols"]} == {
        "U1": "U1", "U2": "U2", "interposer": "interposer"}
    pins = {s["ref"]: s["pins"] for s in form["symbols"]}
    assert pins["U1"] == sorted(p["name"] for p in gt.PINS["U1"])
    assert pins["interposer"] == sorted(p["id"] for p in
                                        gt.CARRIER_CHIPLET["components"][0]["io_pads"])
    assert _named(form) == gt.CARRIER_NETS
    auto = [n["endpoints"] for n in form["nets"] if n["auto_named"]]
    assert all(len(eps) == 1 for eps in auto)
    assert {tuple(eps[0]) for eps in auto} == gt.CARRIER_FREE


@pytest.mark.kicad_cli
def test_kicad_cli_power_symbols_add_no_nodes(tmp_path):
    """Every VDD label becomes a power symbol: the nets stay the same and no
    power reference reaches the form."""
    _need_kicad_cli()
    sch, report_path, _ = _drawn(tmp_path)

    def to_power(node):
        lib = sx.find(node, "lib_symbols")
        lib.append(["symbol", sx.Quoted("power:VDD"), ["power"],
                    ["pin_numbers", ["hide", "yes"]],
                    ["pin_names", ["offset", "0"], ["hide", "yes"]],
                    ["exclude_from_sim", "no"], ["in_bom", "yes"], ["on_board", "yes"],
                    g._prop("Reference", "#PWR", hide=True), g._prop("Value", "VDD"),
                    g._prop("Footprint", "", hide=True), g._prop("Datasheet", "", hide=True),
                    g._prop("Description", "", hide=True),
                    ["symbol", sx.Quoted("VDD_1_1"),
                     ["pin", "power_in", "line", ["at", "0", "0", "90"], ["length", "0"],
                      ["name", sx.Quoted("VDD"), g._FONT],
                      ["number", sx.Quoted("1"), g._FONT]]],
                    ["embedded_fonts", "no"]])
        sheet = str(sx.find(node, "uuid")[1])
        project = str(sx.find(sx.find(sx.find(node, "symbol"), "instances"), "project")[1])
        for i, label in enumerate(_labels(node, "VDD"), 1):
            at = sx.find(label, "at")
            ref = f"#PWR0{i}"
            node[node.index(label)] = [
                "symbol", ["lib_id", sx.Quoted("power:VDD")], ["at", at[1], at[2], "0"],
                ["unit", "1"], ["exclude_from_sim", "no"], ["in_bom", "yes"],
                ["on_board", "yes"], ["dnp", "no"], ["uuid", sx.Quoted(g.uid("pwr", ref))],
                g._prop("Reference", ref, hide=True), g._prop("Value", "VDD"),
                g._prop("Footprint", "", hide=True), g._prop("Datasheet", "", hide=True),
                g._prop("Description", "", hide=True),
                ["pin", sx.Quoted("1"), ["uuid", sx.Quoted(g.uid("pwr-pin", ref))]],
                ["instances", ["project", sx.Quoted(project),
                               ["path", sx.Quoted(f"/{sheet}"),
                                ["reference", sx.Quoted(ref)], ["unit", "1"]]]]]

    _edit(sch, to_power)
    assert "global_label \"VDD\"" not in sch.read_text(encoding="utf-8")
    form = _kicad_form(sch, report_path)
    assert form["refusals"] == []
    assert _named(form) == gt.CARRIER_NETS
    assert all(not s["ref"].startswith("#") for s in form["symbols"])


@pytest.mark.kicad_cli
def test_kicad_cli_local_label_is_refused(tmp_path):
    _need_kicad_cli()
    sch, report_path, _ = _drawn(tmp_path)

    def to_local(node):
        for label in _labels(node, "sig1"):
            label[0] = "label"
            label[:] = [c for c in label if sx.head(c) not in ("shape", "property")]

    _edit(sch, to_local)
    form = _kicad_form(sch, report_path)
    assert [(r["code"], r.get("net")) for r in form["refusals"]] == [("local_label", "/sig1")]


@pytest.mark.kicad_cli
def test_kicad_cli_two_names_on_one_net_are_refused(tmp_path):
    _need_kicad_cli()
    sch, report_path, _ = _drawn(tmp_path)

    def alias(node):
        label = _labels(node, "sig0")[0]
        twin = [c if sx.head(c) != "uuid" else ["uuid", sx.Quoted(g.uid("alias"))]
                for c in label]
        twin[1] = sx.Quoted("sig0_alias")
        node.insert(node.index(label) + 1, twin)

    _edit(sch, alias)
    form = _kicad_form(sch, report_path)
    assert "multiple_net_names" in {r["code"] for r in form["refusals"]}


@pytest.mark.kicad_cli
def test_kicad_cli_blank_sheet_reads_as_form_1_1(tmp_path):
    """The handout's empty sheet, with the dies placed from its library (no
    identity, passive pins) and the nets labelled, reads back as form 1.1 with
    every part named and the nets as drawn."""
    _need_kicad_cli()
    from tests import test_schematic_handout as ht
    _files_, handout = ht._handout(tmp_path)
    sch = draw_from_library(handout, "design",
                            [("U1", "DIE_A", 101.6, 101.6), ("U2", "DIE_B", 254.0, 101.6)],
                            gt.EXPECTED_NETS)
    net, erc = _export(sch)
    lib = handout / g.HANDOUT_LIB_FILE
    form = k.read_form(net, sch, None, sch.with_name("connectivity.json"), erc=erc,
                       library=lib)
    assert form["refusals"] == []
    assert (form["version"], form["sheet"], form["base_sha256"]) == ("1.1", "blank", None)
    assert form["library"]["sha256"] == _sha(lib)
    assert {s["ref"]: (s["identity"], s["part"]) for s in form["symbols"]} == {
        "U1": (None, {"lib": "chiplet_dies", "name": "DIE_A"}),
        "U2": (None, {"lib": "chiplet_dies", "name": "DIE_B"})}
    assert {s["ref"]: s["pins"] for s in form["symbols"]} == {
        ref: sorted({p["name"] for p in gt.PINS[ref]}) for ref in ("U1", "U2")}
    assert _named(form) == gt.EXPECTED_NETS
