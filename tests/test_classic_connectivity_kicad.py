# SPDX-License-Identifier: GPL-3.0-or-later
"""Classic connectivity from real KiCad exports (opt-in)."""
import copy

import pytest

import chiplet_to_kicad_sch as g
import die_handout as dh
import kicad_sch_to_connectivity as k
import sexpr_io as sx
from tests import test_classic_connectivity as ct
from tests import test_kicad_sch_to_connectivity as bt
from tests.sheet_drawing import draw_from_library
from tests import test_schematic_handout_dies as ht

pytestmark = pytest.mark.kicad_cli


def _read(sch, net, erc):
    out = sch.with_name("connectivity.json")
    form = k.read_form(net, sch, None, out, erc=erc,
                       library=sch.parent / g.HANDOUT_LIB_FILE,
                       system_library=sch.parent / dh.SYSTEM_LIB_FILE)
    k.write_form(form, out)
    return form


def test_classic_sheet_export_fields_rails_and_open(tmp_path):
    bt._need_kicad_cli()
    out = tmp_path / "classic"
    first = ht._folder(out / "dies" / "ALPHA", "ALPHA", [
        row for row in ht.DRIVER if row[1] in {"OUT", "IO", "VDD", "VSS"}])
    second = ht._folder(out / "dies" / "BETA", "BETA", [
        ("RX", "RX", 0, "signal", "input"), ("VDD", "VDD", 0, "power", "bidir"),
        ("VSS", "VSS", 0, "ground", "bidir")])
    dh.die_handout([first, second], out, 80, name="classic")
    placements = [("U1", "ALPHA", 101.6, 101.6), ("U2", "BETA", 254.0, 101.6),
                  ("J1", "chiplet_system:EXT_PAD", 76.2, 50.8),
                  ("J2", "chiplet_system:EXT_PAD", 152.4, 50.8),
                  # Each power symbol on the end of a wire from its pad's pin, the
                  # pin 5.08 left of the pad, and the rail's die pins by label.
                  ("#PWR01", "chiplet_system:SUPPLY", 66.04, 50.8),
                  ("#PWR02", "chiplet_system:GND", 142.24, 50.8)]
    nets = {"data": [("U1", "OUT"), ("U2", "RX")],
            "CORE": [("U1", "VDD"), ("U2", "VDD")],
            "RETURN": [("U1", "VSS"), ("U2", "VSS")]}
    wires = [(("J1", "1"), ("#PWR01", "1")), (("J2", "1"), ("#PWR02", "1"))]
    fields = {"U1": {"Method": "cupillar_opt1", "Thickness_um": "750"},
              "U2": {"Method": "cupillar_opt1", "Thickness_um": "250"},
              "J1": {"Value": ""}, "J2": {"Edge": "west"},
              "#PWR01": {"Value": "CORE", "Volts": "1.2"}, "#PWR02": {"Value": "RETURN"}}
    sch = draw_from_library(out, "classic", placements, nets, properties=fields,
                            libraries={dh.SYSTEM_LIB: out / dh.SYSTEM_LIB_FILE},
                            no_connect={("U1", "IO")}, wires=wires,
                            shown={"#PWR01": ("Value", "Volts"), "#PWR02": ("Value",)})
    net, erc = bt._export(sch)
    form = _read(sch, net, erc)
    assert form["refusals"] == []
    assert form["rails"] == [{"name": "CORE", "class": "power", "volts": "1.2"},
                             {"name": "RETURN", "class": "ground", "volts": None}]
    assert form["open"] == [["U1", "IO"]]
    assert form["system_library"] == {"path": dh.SYSTEM_LIB_FILE,
                                      "sha256": bt._sha(out / dh.SYSTEM_LIB_FILE)}
    sheet = sx.parse(sch.read_text(encoding="utf-8"))
    placed = {k._sheet_properties(s)["Reference"]: k._value(s, "uuid")
              for s in sx.find_all(sheet, "symbol")}
    symbols = {s["ref"]: s for s in form["symbols"]}
    assert set(symbols) == {"U1", "U2", "J1", "J2"}
    assert {r: s["key"] for r, s in symbols.items()} == {
        ref: "/" + placed[ref] for ref in symbols}
    for ref, folder in (("U1", "ALPHA"), ("U2", "BETA")):
        assert symbols[ref]["fields"]["Die"] == f"dies/{folder}"
        for name in ("Method", "Thickness_um"):
            assert symbols[ref]["fields"][name] == fields[ref][name]
    assert symbols["J1"]["value"] == "~"
    assert symbols["J2"]["fields"]["Edge"] == "west"
    assert bt._named(form) == {"data": set(nets["data"]),
                               "CORE": {*nets["CORE"], ("J1", "1")},
                               "RETURN": {*nets["RETURN"], ("J2", "1")}}
    # Stable path for loading with a downstream connectivity reader:
    # <tmp_path>/classic/connectivity.json, with its bound inputs beside it.
    exported = sx.parse(net.read_text(encoding="utf-8"))
    flagged = [n for n in sx.find_all_rec(sx.find(exported, "nets"), "node")
               if (k._value(n, "ref"), k._value(n, "pin")) == ("U1", "IO")]
    assert len(flagged) == 1 and k._value(flagged[0], "pintype").endswith("+no_connect")


def test_bus_pad_names_read_back_verbatim(tmp_path):
    """A die's bus pads, drawn from the handout and joined by labels named
    after them, come back from KiCad's netlist with every name as it was. The
    labels hold a subscript mid-name, trailing and repeated, on no bus; x[3]
    and x[03] stay two nets, since KiCad compares the names as text."""
    bt._need_kicad_cli()
    out = tmp_path / "classic"
    pad, rx = "IO_BOND_outputs[0].output_pad", "RX[0]"
    # label: (ALPHA pin, BETA pin), all plain, so only the label is bracketed
    labels = {"x[3]": ("D0", "Q0"), "x[03]": ("D1", "Q1"), "a[1].b[2]": ("D2", "Q2")}
    first = ht._folder(out / "dies" / "ALPHA", "ALPHA", [
        (pad, "IO_BOND_outputs[0]", 0, "signal", "output"),
        *[(d, d, 0, "signal", "output") for d, _ in labels.values()]])
    second = ht._folder(out / "dies" / "BETA", "BETA", [
        (rx, rx, 0, "signal", "input"),
        *[(q, q, 0, "signal", "input") for _, q in labels.values()]])
    dh.die_handout([first, second], out, 80, name="classic")
    nets = {f"U1_{pad}": [("U1", pad), ("U2", rx)],
            **{name: [("U1", d), ("U2", q)] for name, (d, q) in labels.items()}}
    sch = draw_from_library(out, "classic", [("U1", "ALPHA", 101.6, 101.6),
                                             ("U2", "BETA", 254.0, 101.6)], nets)
    form = _read(sch, *bt._export(sch))
    assert form["refusals"] == []
    assert bt._named(form) == {name: set(eps) for name, eps in nets.items()}


def _test_part(cell, pin_type="passive", multi=False):
    def unit(index, number):
        return ["symbol", sx.Quoted(f"{cell}_{index}_1"),
                ["pin", pin_type, "line", ["at", "-2.54", "0", "0"], ["length", "2.54"],
                 ["name", sx.Quoted(f"P{number}"), g._FONT],
                 ["number", sx.Quoted(number), g._FONT]]]
    return ["symbol", sx.Quoted(cell), ["in_bom", "yes"], ["on_board", "yes"],
            g._prop("Reference", "U"), g._prop("Value", cell),
            g._prop("Footprint", "", hide=True), g._prop("Datasheet", "", hide=True),
            unit(1, "1"), *([unit(2, "2")] if multi else []), ["embedded_fonts", "no"]]


def test_multi_unit_tstamps_and_intrinsic_no_connect_pin(tmp_path):
    bt._need_kicad_cli()
    out = tmp_path / "units"
    die = ht._folder(out / "dies" / "A", "A", ht.ECHO)
    dh.die_handout([die], out, 80, name="units")
    lib = out / g.HANDOUT_LIB_FILE
    lib.write_text(g.symbol_library([_test_part("MULTI", multi=True),
                                     _test_part("NC", pin_type="no_connect"),
                                     _test_part("FLAG")]), encoding="utf-8")
    sch = draw_from_library(out, "units", [
        ("U1", "MULTI", 50.8, 50.8), ("U2", "NC", 152.4, 50.8),
        ("U3", "FLAG", 203.2, 50.8), ("U4", "MULTI", 254.0, 50.8)], {},
        no_connect={("U3", "1")})

    def add_second_unit(tree):
        first = next(s for s in sx.find_all(tree, "symbol")
                     if k._sheet_properties(s)["Reference"] == "U1")
        second = copy.deepcopy(first)
        sx.find(second, "uuid")[1] = sx.Quoted(g.uid("drawn", "U1", "unit2"))
        sx.find(second, "at")[2] = "101.6"
        sx.find(second, "unit")[1] = "2"
        path = sx.find(sx.find(sx.find(second, "instances"), "project"), "path")
        sx.find(path, "unit")[1] = "2"
        # Instances contain UUIDs only for the pins of the placed unit.
        for symbol, number in ((first, "1"), (second, "2")):
            symbol[:] = [c for c in symbol if sx.head(c) != "pin"]
            symbol.append(["pin", sx.Quoted(number),
                           ["uuid", sx.Quoted(g.uid("unit-pin", number))]])
        tree.append(second)
    bt._edit(sch, add_second_unit)
    net, erc = bt._export(sch)
    form = _read(sch, net, erc)
    assert {(r["code"], r.get("component")) for r in form["refusals"]} == {
        ("multi_unit", "U1"), ("multi_unit", "U4")}
    exported = sx.parse(net.read_text(encoding="utf-8"))
    comps = {k._value(c, "ref"): c for c in sx.find_all(sx.find(exported, "components"), "comp")}
    # KiCad 9 exports both placed UUIDs under tstamps. Later 9.x releases also
    # write a units block. multi_unit is decided from the cached symbol alone.
    assert len(sx.find(comps["U1"], "tstamps")[1:]) == 2
    assert len(sx.find(comps["U4"], "tstamps")[1:]) == 1
    nodes = {(k._value(n, "ref"), k._value(n, "pin")): k._value(n, "pintype")
             for n in sx.find_all_rec(sx.find(exported, "nets"), "node")}
    assert nodes["U2", "1"] == "no_connect"
    assert nodes["U3", "1"] == "passive+no_connect"
    assert form["open"] == [["U3", "1"]]


def test_same_reference_keeps_two_uuids_for_classic_import(tmp_path):
    bt._need_kicad_cli()
    out = tmp_path / "shared"
    folder = ht._folder(out / "dies" / "A", "A", ht.ECHO)
    dh.die_handout([folder], out, 80, name="shared")
    sch = draw_from_library(out, "shared", [("U1", "A", 50.8, 50.8),
                                           ("U2", "A", 152.4, 50.8)], {},
                            properties={"U1": {"Method": "first", "Thickness_um": "750"},
                                        "U2": {"Method": "second", "Thickness_um": "250"}})
    def share_reference(tree):
        symbol = next(s for s in sx.find_all(tree, "symbol")
                      if k._sheet_properties(s)["Reference"] == "U2")
        prop = next(p for p in sx.find_all(symbol, "property") if p[1] == "Reference")
        prop[2] = sx.Quoted("U1")
        path = sx.find(sx.find(sx.find(symbol, "instances"), "project"), "path")
        sx.find(path, "reference")[1] = sx.Quoted("U1")
    bt._edit(sch, share_reference)
    net, erc = bt._export(sch)
    form = _read(sch, net, erc)
    assert bt._codes(form) == [("duplicate_reference", "U1", None, None)]
    symbols = form["symbols"]
    assert len(symbols) == 2 and {s["ref"] for s in symbols} == {"U1"}
    assert {s["key"] for s in symbols} == {"/" + g.uid("drawn", ref) for ref in ("U1", "U2")}
    assert {s["fields"]["Method"] for s in symbols} == {"first", "second"}
    assert {s["fields"]["Thickness_um"] for s in symbols} == {"750", "250"}


def test_units_and_jumpers_equal_what_kicad_cli_writes_in_the_netlist(tmp_path):
    """The units golden's sheet, exported now: the reader's units and jumpers
    are the netlist's (units ...) and duplicate_pin_numbers_are_jumpers."""
    bt._need_kicad_cli()
    sch = ct.draw_units_golden(tmp_path / "units")
    net, _erc = bt._export(sch)
    read, form = ct.read_units_form(sch, net)
    assert form["refusals"] == []
    assert read == ct.netlist_units(net) == ct.netlist_units(ct.UNITS_GOLDEN / "units.net")
