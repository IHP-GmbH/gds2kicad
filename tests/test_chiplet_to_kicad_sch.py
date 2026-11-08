# SPDX-License-Identifier: GPL-3.0-or-later
"""chiplet_to_kicad_sch: the system schematic drawn from a .chiplet netlist.

Hermetic: every input is written into tmp_path. The sheet's connectivity is
read back two ways: geometrically here (pin ends, wires, labels, no-connects),
and by KiCad itself in the kicad_cli case (`pytest -m kicad_cli`). Set
GDS_TO_KICAD_REQUIRE_KICAD_CLI=1 to make a missing kicad-cli a failure there.
"""

import copy
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chiplet_to_kicad_sch as g  # noqa: E402
import sexpr_io as sx  # noqa: E402
import sheet_check as sc  # noqa: E402


def _pin(name, side, type_="input", port=None, x=0, y=0):
    d = {"name": name, "side": side, "type": type_, "pad_index": 0,
         "center_x_dbu": x, "center_y_dbu": y}
    if port:
        d["port"] = port
    return d


PINS = {
    "U1": [_pin("A0", "left", port="a0", y=10), _pin("A1", "right", "output", "a1"),
           _pin("VDD1", "top", "power_in", "VDD", x=-5),
           _pin("VDD2", "bottom", "power_in", "VDD"),
           _pin("NC1", "left", "passive", y=-10)],
    "U2": [_pin("B0", "left", port="b0", y=5), _pin("B1", "left", "output", "b1", y=-5),
           _pin("VDD", "top", "power_in"), _pin("SPARE", "right", "passive")],
}


def _net(name, *eps):
    return {"name": name, "connections": [
        {"component": c, "pin": p, "layer": "TopMetal2"} for c, p in eps]}


CHIPLET = {
    "format_version": "1.0",
    "assembly": {"name": "sys", "units": "um"},
    "components": [
        {"id": "interposer", "type": "interposer", "top_cell": "CARRIER"},
        {"id": "U1", "type": "die", "top_cell": "DIE_A"},
        {"id": "U2", "type": "die", "top_cell": "DIE_B"},
    ],
    "netlist": {"nets": [
        _net("sig0", ("U1", "A0"), ("U2", "B0")),
        _net("sig1", ("U1", "A1"), ("U2", "B1")),
        _net("VDD", ("U1", "VDD1"), ("U1", "VDD2"), ("U2", "VDD")),
        _net("probe", ("U1", "NC1")),
    ]},
}

EXPECTED_NETS = {
    "sig0": {("U1", "A0"), ("U2", "B0")},
    "sig1": {("U1", "A1"), ("U2", "B1")},
    "VDD": {("U1", "VDD1"), ("U1", "VDD2"), ("U2", "VDD")},
    "probe": {("U1", "NC1")},
}
EXPECTED_FREE = {("U2", "SPARE")}


def _inputs(tmp_path, chiplet=None, pins=None):
    chiplet = CHIPLET if chiplet is None else chiplet
    pins = PINS if pins is None else pins
    tmp_path.mkdir(parents=True, exist_ok=True)
    cpath = tmp_path / "design.chiplet"
    cpath.write_text(yaml.safe_dump(chiplet, sort_keys=False), encoding="utf-8")
    paths = {}
    for inst, plist in pins.items():
        p = tmp_path / f"{inst}.pins.json"
        p.write_text(json.dumps({"version": 1, "pins": plist}), encoding="utf-8")
        paths[inst] = str(p)
    return cpath, paths


def _generate(tmp_path, **kw):
    cpath, pins = _inputs(tmp_path, **kw)
    out = tmp_path / "pnr" / "sys.draft.kicad_sch"
    result = g.generate(cpath, pins, out)
    return cpath, out, result


# ------------------------------------------- reading the sheet back by geometry

def _key(x, y):
    return (round(float(x), 4), round(float(y), 4))


def _sheet_connectivity(sch_path):
    """Connectivity as the drawing states it: a pin end joins the wires that
    touch it, a wire end the label at it. Returns the form read_kicad_netlist
    returns, so the same comparison applies: a carrier's EXT_PADs folded back
    into the carrier."""
    tree = sx.parse(Path(sch_path).read_text(encoding="utf-8"))
    lib_pins = {}
    for sym in sx.find_all(sx.find(tree, "lib_symbols"), "symbol"):
        pins = {}
        for unit in sx.find_all(sym, "symbol"):
            for pin in sx.find_all(unit, "pin"):
                at = sx.find(pin, "at")
                pins[str(sx.find(pin, "number")[1])] = (float(at[1]), float(at[2]))
        lib_pins[str(sym[1])] = pins

    ends, identity, pads = {}, {}, {}
    for inst in sx.find_all(tree, "symbol"):
        lib_id = str(sx.find(inst, "lib_id")[1])
        at = sx.find(inst, "at")
        assert at[3] == "0" and sx.find(inst, "mirror") is None
        props = {str(p[1]): str(p[2]) for p in sx.find_all(inst, "property")}
        ref, ident = props["Reference"], props.get(g.IDENTITY_PROPERTY)
        if sc.is_pad(*lib_id.split(":", 1)) and ident is not None:
            pads[ref] = ident
            identity[ident] = ident
        else:
            identity[ref] = ident
        for number, (px, py) in lib_pins[lib_id].items():
            ends[_key(float(at[1]) + px, float(at[2]) - py)] = sc.fold_pads((ref, number), pads)

    parent = {}

    def find(k):
        parent.setdefault(k, k)
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for wire in sx.find_all(tree, "wire"):
        a, b = [_key(xy[1], xy[2]) for xy in sx.find_all(sx.find(wire, "pts"), "xy")]
        parent[find(a)] = find(b)
    labels = {}
    for lbl in sx.find_all(tree, "global_label"):
        at = sx.find(lbl, "at")
        labels.setdefault(find(_key(at[1], at[2])), set()).add(str(lbl[1]))
    no_connects = {_key(*sx.find(nc, "at")[1:3]) for nc in sx.find_all(tree, "no_connect")}

    nets, free = {}, set()
    for point, ep in ends.items():
        names = labels.get(find(point), set())
        assert len(names) <= 1, f"{ep} touches labels {names}"
        if names:
            assert point not in no_connects
            nets.setdefault(names.pop(), set()).add(ep)
        else:
            free.add(ep)
    base = None
    for c in sx.find_all(sx.find(tree, "title_block"), "comment"):
        if str(c[2]).startswith(g.BASE_DIGEST_PREFIX):
            base = str(c[2])[len(g.BASE_DIGEST_PREFIX):]
    return {"nets": {k: frozenset(v) for k, v in nets.items()},
            "unconnected": frozenset(free), "identity": identity, "base_sha256": base,
            "flagged": frozenset(ep for point, ep in ends.items() if point in no_connects)}


def _expected():
    return ({k: frozenset(v) for k, v in EXPECTED_NETS.items()},
            frozenset(EXPECTED_FREE))


# ------------------------------------------------------------------- written

def test_sheet_connectivity_equals_the_description(tmp_path):
    cpath, out, result = _generate(tmp_path)
    digest = hashlib.sha256(cpath.read_bytes()).hexdigest()
    nets, free = _expected()
    assert result["nets"] == nets and result["unconnected"] == free
    got = _sheet_connectivity(out)
    assert sc.compare(nets, free, digest, got) == []


def test_identities_are_written(tmp_path):
    cpath, out, _ = _generate(tmp_path)
    tree = sx.parse(out.read_text(encoding="utf-8"))
    insts = sx.find_all(tree, "symbol")
    assert [str(sx.find(s, "lib_id")[1]) for s in insts] == ["chiplet:DIE_A", "chiplet:DIE_B"]
    for inst, cid in zip(insts, ("U1", "U2")):
        props = {str(p[1]): p for p in sx.find_all(inst, "property")}
        assert str(props["Reference"][2]) == cid
        ident = props[g.IDENTITY_PROPERTY]
        assert str(ident[2]) == cid and sx.find(ident, "hide") == ["hide", "yes"]
        assert str(sx.find(inst, "uuid")[1]) == g.uid("symbol", cid)
        ref = sx.find_all_rec(sx.find(inst, "instances"), "reference")
        assert [str(r[1]) for r in ref] == [cid]
        assert {str(p[1]) for p in sx.find_all(inst, "pin")} == {
            p["name"] for p in PINS[cid]}
    comments = {str(c[1]): str(c[2]) for c in
                sx.find_all(sx.find(tree, "title_block"), "comment")}
    assert comments["1"] == g.BASE_DIGEST_PREFIX + hashlib.sha256(
        cpath.read_bytes()).hexdigest()


def test_pin_types_and_ports_come_from_the_pin_list(tmp_path):
    _, out, _ = _generate(tmp_path)
    tree = sx.parse(out.read_text(encoding="utf-8"))
    lib = sx.find(tree, "lib_symbols")
    pins = {str(sx.find(p, "number")[1]): (p[1], str(sx.find(p, "name")[1]))
            for p in sx.find_all_rec(sx.find_all(lib, "symbol")[0], "pin")}
    assert pins == {"A0": ("input", "a0"), "A1": ("output", "a1"),
                    "VDD1": ("power_in", "VDD"), "VDD2": ("power_in", "VDD"),
                    "NC1": ("passive", "NC1")}


def test_a_repeated_pin_name_is_one_terminal(tmp_path):
    """A rail lists its name once per pad; the sheet draws it once."""
    pins = copy.deepcopy(PINS)
    pins["U2"] += [_pin("VDD", "left", "power_in", y=3), _pin("VDD", "bottom", "power_in")]
    cpath, out, result = _generate(tmp_path, pins=pins)
    u2 = [d for d in result["system"].dies if d.id == "U2"][0]
    assert [t.name for t in u2.terminals].count("VDD") == 1
    tree = sx.parse(out.read_text(encoding="utf-8"))
    assert list(_lib_pins(tree, 1)).count("VDD") == 1
    nets = {k: frozenset(v) for k, v in EXPECTED_NETS.items()}
    assert result["nets"] == nets
    digest = hashlib.sha256(cpath.read_bytes()).hexdigest()
    assert sc.compare(nets, frozenset(EXPECTED_FREE), digest, _sheet_connectivity(out)) == []


def _lib_pins(tree, index):
    sym = sx.find_all(sx.find(tree, "lib_symbols"), "symbol")[index]
    return {str(sx.find(p, "number")[1]): sx.find(p, "at")
            for p in sx.find_all_rec(sym, "pin")}


def _pin_ends(tree):
    """(component, terminal) -> where the pin end sits on the sheet."""
    lib = {str(sym[1]): {str(sx.find(p, "number")[1]): sx.find(p, "at")
                         for p in sx.find_all_rec(sym, "pin")}
           for sym in sx.find_all(sx.find(tree, "lib_symbols"), "symbol")}
    ends = {}
    for inst in sx.find_all(tree, "symbol"):
        ox, oy = (float(v) for v in sx.find(inst, "at")[1:3])
        ref = str(sx.find(sx.find_all_rec(inst, "path")[0], "reference")[1])
        for number, at in lib[str(sx.find(inst, "lib_id")[1])].items():
            ends[(ref, number)] = _key(ox + float(at[1]), oy - float(at[2]))
    return ends


def test_link_pins_face_the_other_die(tmp_path):
    _, out, _ = _generate(tmp_path)
    tree = sx.parse(out.read_text(encoding="utf-8"))
    u1, u2 = _lib_pins(tree, 0), _lib_pins(tree, 1)
    # U1 comes first, so its links (A0, A1) point right at U2 and its power
    # pins and its one-die net (NC1) sit on the left. U2's links and its open
    # pin face left, its power pin right.
    assert {k: v[3] for k, v in u1.items()} == {
        "A0": "180", "A1": "180", "VDD1": "0", "VDD2": "0", "NC1": "0"}
    assert {k: v[3] for k, v in u2.items()} == {
        "B0": "0", "B1": "0", "SPARE": "0", "VDD": "180"}
    # A column runs in port order from the top, whatever the pads' sides.
    assert float(u2["B0"][2]) > float(u2["B1"][2]) > float(u2["SPARE"][2])
    assert float(u1["A0"][2]) > float(u1["A1"][2])
    # Facing columns put the n-th links of the two dies on one line.
    ends = _pin_ends(tree)
    assert ends[("U1", "A0")][1] == ends[("U2", "B0")][1]
    assert ends[("U1", "A1")][1] == ends[("U2", "B1")][1]


def test_open_link_pins_are_bare_and_other_open_pins_flagged(tmp_path):
    pins = copy.deepcopy(PINS)
    pins["U2"].append(_pin("VDDX", "right", "power_in"))
    _, out, _ = _generate(tmp_path, pins=pins)
    got = _sheet_connectivity(out)
    # SPARE is an open link pin of U2; VDDX an open power pin.
    assert got["unconnected"] == {("U2", "SPARE"), ("U2", "VDDX")}
    assert got["flagged"] == {("U2", "VDDX")}
    comments = {str(c[1]): str(c[2]) for c in
                sx.find_all(sx.find(sx.parse(out.read_text(encoding="utf-8")),
                                    "title_block"), "comment")}
    assert comments["2"] == ("generated from design.chiplet; nets are global labels, "
                             "read back by the flow's edit")


def test_ports_sort_with_numbers_read_as_numbers(tmp_path):
    pins = copy.deepcopy(PINS)
    pins["U2"] = [_pin(f"P{i}", "top", port=f"d2d_f{i}", x=-i) for i in (10, 2, 1)]
    chiplet = copy.deepcopy(CHIPLET)
    chiplet["netlist"]["nets"] = [_net("probe", ("U1", "NC1"))]
    _, out, _ = _generate(tmp_path, chiplet=chiplet, pins=pins)
    u2 = _lib_pins(sx.parse(out.read_text(encoding="utf-8")), 1)
    assert sorted(u2, key=lambda k: -float(u2[k][2])) == ["P1", "P2", "P10"]


def test_open_and_wired_sheets_share_one_layout(tmp_path):
    """The sheet a student wires and the answer's sheet put every symbol and
    every pin end in the same place: the inner side keeps room for the label."""
    _, wired, _ = _generate(tmp_path / "wired")
    chiplet = copy.deepcopy(CHIPLET)
    chiplet["netlist"]["nets"] = [n for n in chiplet["netlist"]["nets"]
                                  if not n["name"].startswith("sig")]
    _, open_, _ = _generate(tmp_path / "open", chiplet=chiplet)
    a = sx.parse(wired.read_text(encoding="utf-8"))
    b = sx.parse(open_.read_text(encoding="utf-8"))
    assert _pin_ends(a) == _pin_ends(b)
    assert sx.find(a, "paper") == sx.find(b, "paper")


def test_names_read_horizontally_and_clear_the_middle(tmp_path):
    pins = copy.deepcopy(PINS)
    for i in range(6):
        pins["U2"].append(_pin(f"T{i}", "top", port=f"long_top_port_{i}", x=i))
        pins["U2"].append(_pin(f"D{i}", "bottom", "power_in", f"long_bottom_port_{i}", x=i))
    _, out, _ = _generate(tmp_path, pins=pins)
    tree = sx.parse(out.read_text(encoding="utf-8"))
    die_b = sx.find_all(sx.find(tree, "lib_symbols"), "symbol")[1]
    rect = sx.find_all_rec(die_b, "rectangle")[0]
    half_w = float(sx.find(rect, "end")[1])
    for p in sx.find_all_rec(die_b, "pin"):
        assert sx.find(p, "at")[3] in ("0", "180")
        reach = len(str(sx.find(p, "name")[1])) * g.CHAR_W + g.NAME_OFFSET
        # The top cell name, centred in the body, clears every pin name.
        assert len("DIE_B") * g.CHAR_W / 2 < half_w - reach


def test_reference_and_value_sit_in_the_body(tmp_path):
    _, out, _ = _generate(tmp_path)
    tree = sx.parse(out.read_text(encoding="utf-8"))
    rects = {str(sym[1]): sx.find_all_rec(sym, "rectangle")[0]
             for sym in sx.find_all(sx.find(tree, "lib_symbols"), "symbol")}
    for inst in sx.find_all(tree, "symbol"):
        ox, oy = (float(v) for v in sx.find(inst, "at")[1:3])
        rect = rects[str(sx.find(inst, "lib_id")[1])]
        half_w, half_h = float(sx.find(rect, "end")[1]), float(sx.find(rect, "start")[2])
        props = {str(p[1]): sx.find(p, "at") for p in sx.find_all(inst, "property")}
        for name in ("Reference", "Value"):
            x, y = float(props[name][1]), float(props[name][2])
            assert abs(x - ox) < half_w and abs(y - oy) < half_h, name


def test_every_point_is_on_the_grid(tmp_path):
    _, out, _ = _generate(tmp_path)
    tree = sx.parse(out.read_text(encoding="utf-8"))
    points = [(xy[1], xy[2]) for xy in sx.find_all_rec(tree, "xy")]
    for tag in ("no_connect", "global_label"):
        points += [tuple(sx.find(n, "at")[1:3]) for n in sx.find_all(tree, tag)]
    for v in [float(c) for p in points for c in p]:
        assert abs(v / 1.27 - round(v / 1.27)) < 1e-6, v


def test_reruns_are_byte_stable(tmp_path):
    _, a, _ = _generate(tmp_path / "a")
    _, b, _ = _generate(tmp_path / "b")
    assert a.read_bytes() == b.read_bytes()


def test_same_cell_drawn_the_same_way_shares_one_symbol(tmp_path):
    # U1 faces right, U2 and U3 face left: U2 and U3 share a symbol.
    chiplet = copy.deepcopy(CHIPLET)
    chiplet["components"][2]["top_cell"] = "DIE_A"
    chiplet["components"].append({"id": "U3", "type": "die", "top_cell": "DIE_A"})
    pins = {"U1": PINS["U1"], "U2": PINS["U1"], "U3": PINS["U1"]}
    chiplet["netlist"]["nets"] = []
    _, out, _ = _generate(tmp_path, chiplet=chiplet, pins=pins)
    tree = sx.parse(out.read_text(encoding="utf-8"))
    assert len(sx.find_all(sx.find(tree, "lib_symbols"), "symbol")) == 2
    assert [str(sx.find(s, "lib_id")[1]) for s in sx.find_all(tree, "symbol")] == \
        ["chiplet:DIE_A", "chiplet:DIE_A_U2", "chiplet:DIE_A_U2"]


def test_same_cell_drawn_two_ways_gets_two_symbols(tmp_path):
    chiplet = copy.deepcopy(CHIPLET)
    chiplet["components"][2]["top_cell"] = "DIE_A"
    chiplet["netlist"]["nets"] = []
    _, out, _ = _generate(tmp_path, chiplet=chiplet)
    tree = sx.parse(out.read_text(encoding="utf-8"))
    assert [str(sx.find(s, "lib_id")[1]) for s in sx.find_all(tree, "symbol")] == \
        ["chiplet:DIE_A", "chiplet:DIE_A_U2"]
    got = _sheet_connectivity(out)
    assert got["nets"] == {} and len(got["unconnected"]) == 9


# ------------------------------------------------------------------- carrier

def _pad(id_, x, y, net=None, io_class="wire_bond"):
    d = {"id": id_, "io_class": io_class, "position": {"x": x, "y": y},
         "size": {"x": 80.0, "y": 80.0}, "layer": "TopMetal2"}
    if net is not None:
        d["net"] = net
    return d


# The interposer is 2000 x 1000 um centred at (1000, 500). One pad per edge,
# one the netlist connects too (and agrees), one on a net only the pads carry,
# one on no net.
CARRIER_CHIPLET = copy.deepcopy(CHIPLET)
CARRIER_CHIPLET["components"][0].update({
    "anchor": "bbox_center",
    "position": {"x": 1000.0, "y": 500.0, "z": 0.0},
    "dimensions": {"width": 2000.0, "height": 1000.0, "thickness": 13.83},
    "io_pads": [_pad("J_VDD", 100, 500, "VDD"), _pad("J_S0", 1900, 600, "sig0"),
                _pad("J_P", 1000, 950, "probe"), _pad("J_EXT", 1000, 50, "ext"),
                _pad("J_NC", 1900, 400)]})
CARRIER_CHIPLET["netlist"]["nets"][3]["connections"].append(
    {"component": "interposer", "pin": "J_P", "layer": "TopMetal2"})

CARRIER_NETS = {
    **EXPECTED_NETS,
    "sig0": EXPECTED_NETS["sig0"] | {("interposer", "J_S0")},
    "VDD": EXPECTED_NETS["VDD"] | {("interposer", "J_VDD")},
    "probe": EXPECTED_NETS["probe"] | {("interposer", "J_P")},
    "ext": {("interposer", "J_EXT")},
}
CARRIER_FREE = EXPECTED_FREE | {("interposer", "J_NC")}
CARRIER_SIDES = {"J_VDD": "left", "J_S0": "right", "J_P": "top", "J_EXT": "bottom",
                 "J_NC": "right"}


def _carrier_pin_file(out):
    return json.loads((out.parent / "interposer.carrier_pins.json").read_text(encoding="utf-8"))


def test_carrier_pads_sit_on_their_nets(tmp_path):
    cpath, out, result = _generate(tmp_path, chiplet=CARRIER_CHIPLET)
    nets = {k: frozenset(v) for k, v in CARRIER_NETS.items()}
    assert result["nets"] == nets and result["unconnected"] == CARRIER_FREE
    digest = hashlib.sha256(cpath.read_bytes()).hexdigest()
    assert sc.compare(nets, frozenset(CARRIER_FREE), digest, _sheet_connectivity(out)) == []


# A die's bus pads (example 10), and the net and carrier pad named after one.
BUS_NAMES = {"A0": "IO_BOND_bidirs[0].bidir_pad", "sig0": "U1_IO_BOND_bidirs[0].bidir_pad",
             "J_S0": "J_U1_IO_BOND_bidirs[0].bidir_pad"}


def _bus_named(data):
    """``data`` with every string BUS_NAMES names renamed, keys included."""
    if isinstance(data, dict):
        return {_bus_named(k): _bus_named(v) for k, v in data.items()}
    if isinstance(data, (list, tuple, set, frozenset)):
        return type(data)(_bus_named(v) for v in data)
    return BUS_NAMES.get(data, data) if isinstance(data, str) else data


def test_bus_pad_names_are_drawn_verbatim(tmp_path):
    """A pin, a net and a carrier pad named with brackets keep their names:
    nothing is escaped, and the sheet reads back as the description."""
    cpath, out, result = _generate(tmp_path, chiplet=_bus_named(CARRIER_CHIPLET),
                                   pins=_bus_named(PINS))
    nets = {k: frozenset(v) for k, v in _bus_named(CARRIER_NETS).items()}
    assert result["nets"] == nets and result["unconnected"] == CARRIER_FREE
    digest = hashlib.sha256(cpath.read_bytes()).hexdigest()
    assert sc.compare(nets, frozenset(CARRIER_FREE), digest, _sheet_connectivity(out)) == []
    text = out.read_text(encoding="utf-8")
    sig0 = BUS_NAMES["sig0"]
    assert text.count(f'(global_label "{sig0}"') == len(nets[sig0])
    assert [p["name"] for p in _carrier_pin_file(out)["pins"]].count(BUS_NAMES["J_S0"]) == 1


def test_carrier_pads_and_their_pin_list(tmp_path):
    """The carrier is the board, not a part: the sheet draws its pads, one
    EXT_PAD each, named by the pad id, carrying the carrier's identity and the
    footprint the board draws the pad with, the way a designer sheet does."""
    cpath, out, _ = _generate(tmp_path, chiplet=CARRIER_CHIPLET)
    tree = sx.parse(out.read_text(encoding="utf-8"))
    insts = sx.find_all(tree, "symbol")
    assert sorted({str(sx.find(s, "lib_id")[1]) for s in insts}) == [
        "chiplet:DIE_A", "chiplet:DIE_B", "chiplet_system:EXT_PAD"]
    pads = {}
    for inst in insts:
        if str(sx.find(inst, "lib_id")[1]) == "chiplet_system:EXT_PAD":
            props = {str(p[1]): str(p[2]) for p in sx.find_all(inst, "property")}
            pads[props["Reference"]] = (props[g.IDENTITY_PROPERTY], props["Footprint"],
                                        props["Value"], props["Edge"],
                                        [str(p[1]) for p in sx.find_all(inst, "pin")])
    assert pads == {pad: ("interposer", "chiplet_carrier:EXT_PAD_80um", "EXT_PAD", "", ["1"])
                    for pad in ("J_VDD", "J_S0", "J_P", "J_EXT", "J_NC")}

    doc = _carrier_pin_file(out)
    assert (doc["format"], doc["version"], doc["component"]) == (
        g.CARRIER_PINS_FORMAT, "1.0", "interposer")
    assert doc["chiplet_sha256"] == hashlib.sha256(cpath.read_bytes()).hexdigest()
    pins = doc["pins"]
    assert [p["name"] for p in pins] == ["J_VDD", "J_S0", "J_P", "J_EXT", "J_NC"]
    assert {p["name"]: p["side"] for p in pins} == CARRIER_SIDES
    assert {p["type"] for p in pins} == {"passive"}
    assert {p["name"]: p["net"] for p in pins} == {
        "J_VDD": "VDD", "J_S0": "sig0", "J_P": "probe", "J_EXT": "ext", "J_NC": None}

    lib = {str(s[1]): s for s in sx.find_all(sx.find(tree, "lib_symbols"), "symbol")}
    cached = copy.deepcopy(lib["chiplet_system:EXT_PAD"])
    cached[1] = sx.Quoted("EXT_PAD")
    assert sx.dumps(cached) == sx.dumps(g.ext_pad_symbol("chiplet_carrier:EXT_PAD_80um"))
    # The pads stack in name order, so they read top to bottom.
    at = {str(next(p for p in sx.find_all(i, "property") if str(p[1]) == "Reference")[2]):
          float(sx.find(i, "at")[2]) for i in insts
          if str(sx.find(i, "lib_id")[1]) == "chiplet_system:EXT_PAD"}
    assert sorted(at, key=at.get) == ["J_EXT", "J_NC", "J_P", "J_S0", "J_VDD"]


def test_carrier_without_a_centre_is_placed_by_its_pads(tmp_path):
    chiplet = copy.deepcopy(CARRIER_CHIPLET)
    for key in ("position", "dimensions"):
        del chiplet["components"][0][key]
    _, out, _ = _generate(tmp_path, chiplet=chiplet)
    assert {p["name"]: p["side"] for p in _carrier_pin_file(out)["pins"]} == CARRIER_SIDES


def test_no_io_pads_draws_no_carrier(tmp_path):
    chiplet = copy.deepcopy(CARRIER_CHIPLET)
    chiplet["components"][0]["io_pads"] = []
    chiplet["netlist"] = copy.deepcopy(CHIPLET["netlist"])
    _, out, result = _generate(tmp_path, chiplet=chiplet)
    assert [d.id for d in result["system"].dies] == ["U1", "U2"]
    assert result["carrier_pins"] == {}
    assert not (out.parent / "interposer.carrier_pins.json").exists()


def test_carrier_reruns_are_byte_stable(tmp_path):
    _, a, _ = _generate(tmp_path / "a", chiplet=CARRIER_CHIPLET)
    _, b, _ = _generate(tmp_path / "b", chiplet=CARRIER_CHIPLET)
    assert a.read_bytes() == b.read_bytes()
    name = "interposer.carrier_pins.json"
    assert (a.parent / name).read_bytes() == (b.parent / name).read_bytes()


def _pads(c):
    return c["components"][0]["io_pads"]


def _connect(net_index, pin):
    return lambda c: c["netlist"]["nets"][net_index]["connections"].append(
        {"component": "interposer", "pin": pin, "layer": "TopMetal2"})


CARRIER_REFUSALS = {
    "net_conflict": ("carrier_net_conflict", _connect(1, "J_VDD")),
    "unknown_io_class": ("unknown_io_class", lambda c: _pads(c)[4].__setitem__("io_class", "solder")),
    "no_io_class": ("unknown_io_class", lambda c: _pads(c)[4].pop("io_class")),
    "unknown_pad": ("unknown_terminal", _connect(0, "J_NOPE")),
    "pad_id": ("unmappable_name", lambda c: _pads(c)[4].__setitem__("id", "J NC")),
    "pad_net": ("unmappable_name", lambda c: _pads(c)[3].__setitem__("net", "e x")),
    "pad_net_range": ("unmappable_name", lambda c: _pads(c)[3].__setitem__("net", "e[0..1]")),
    "duplicate_pad": ("duplicate_terminal", lambda c: _pads(c).append(_pad("J_NC", 0, 0))),
    "io_pads_not_a_list": ("malformed_chiplet",
                           lambda c: c["components"][0].__setitem__("io_pads", {"J": 1})),
    # A pad is drawn with the EXT_PAD footprint of its size, and named by its id.
    "pad_size_missing": ("io_pad_size", lambda c: _pads(c)[4].pop("size")),
    "pad_size_not_square": ("io_pad_size",
                            lambda c: _pads(c)[4].__setitem__("size", {"x": 80.0, "y": 90.0})),
    "pad_size_fraction": ("io_pad_size",
                          lambda c: _pads(c)[4].__setitem__("size", {"x": 80.5, "y": 80.5})),
    "pad_size_zero": ("io_pad_size",
                      lambda c: _pads(c)[4].__setitem__("size", {"x": 0, "y": 0})),
    "pad_size_infinite": ("io_pad_size", lambda c: _pads(c)[4].__setitem__(
        "size", {"x": float("inf"), "y": float("inf")})),
    "pad_named_as_a_die": ("reference_taken", lambda c: _pads(c)[4].__setitem__("id", "U1")),
    "pad_named_as_an_undrawn_component": ("reference_taken", lambda c: (
        c["components"].append({"id": "X9", "type": "passive"}),
        _pads(c)[4].__setitem__("id", "X9"))),
    # A pad refused for its class is not refused again for its netlist connection.
    "refused_pad_connected": ("unknown_io_class", lambda c: _pads(c)[2].__setitem__("io_class", "x")),
}


@pytest.mark.parametrize("case", sorted(CARRIER_REFUSALS))
def test_carrier_refused_by_name_and_nothing_written(tmp_path, case):
    code, mutate = CARRIER_REFUSALS[case]
    chiplet = copy.deepcopy(CARRIER_CHIPLET)
    mutate(chiplet)
    with pytest.raises(g.SchematicRefused) as exc:
        _generate(tmp_path, chiplet=chiplet)
    assert [r["code"] for r in exc.value.refusals] == [code]
    assert all(r.get("component") == "interposer" for r in exc.value.refusals)
    assert not (tmp_path / "pnr").exists()


def test_a_handout_draws_no_pad_so_asks_nothing_of_one(tmp_path):
    """The handout draws no carrier, so a pad needs no size and may share a
    die's id."""
    chiplet = copy.deepcopy(CARRIER_CHIPLET)
    _pads(chiplet)[4].pop("size")
    _pads(chiplet)[3]["id"] = "U1"
    cpath, pins = _inputs(tmp_path, chiplet=chiplet)
    assert g.handout(cpath, pins, tmp_path / "handout")


def test_a_carrier_takes_no_pin_list(tmp_path):
    pins = {**PINS, "interposer": [_pin("J_VDD", "left")]}
    with pytest.raises(g.SchematicRefused) as exc:
        _generate(tmp_path, chiplet=CARRIER_CHIPLET, pins=pins)
    assert [r["code"] for r in exc.value.refusals] == ["pin_list_not_a_die"]


# ------------------------------------------------------------------ refusals

def _mutated(mutate):
    chiplet, pins = copy.deepcopy(CHIPLET), copy.deepcopy(PINS)
    mutate(chiplet, pins)
    return chiplet, pins


REFUSALS = {
    "non_die_endpoint": lambda c, p: c["netlist"]["nets"][0]["connections"].append(
        {"component": "interposer", "pin": "J_1"}),
    "unknown_component": lambda c, p: c["netlist"]["nets"][0]["connections"].append(
        {"component": "U9", "pin": "A0"}),
    "unknown_terminal": lambda c, p: c["netlist"]["nets"][0]["connections"].append(
        {"component": "U2", "pin": "NOPE"}),
    "no_pin_list": lambda c, p: p.pop("U2"),
    "pin_list_not_a_die": lambda c, p: p.__setitem__("interposer", []),
    "duplicate_net": lambda c, p: c["netlist"]["nets"].append(_net("sig0", ("U2", "SPARE"))),
    "terminal_in_two_nets": lambda c, p: c["netlist"]["nets"].append(
        _net("other", ("U2", "B0"))),
    "duplicate_endpoint": lambda c, p: c["netlist"]["nets"][0]["connections"].append(
        {"component": "U1", "pin": "A0"}),
    "empty_net": lambda c, p: c["netlist"]["nets"].append({"name": "lonely"}),
    "malformed_connection": lambda c, p: c["netlist"]["nets"][0]["connections"].append(
        {"component": "U1"}),
    "duplicate_component": lambda c, p: c["components"].append(
        {"id": "U1", "type": "die"}),
    "conflicting_terminal": lambda c, p: p["U1"].append(_pin("A0", "left", "output")),
    "unknown_pin_type": lambda c, p: p["U1"][0].__setitem__("type", "INOUT"),
    "unknown_pin_side": lambda c, p: p["U1"][0].__setitem__("side", "north"),
}


@pytest.mark.parametrize("code", sorted(REFUSALS))
def test_refused_by_name_and_nothing_written(tmp_path, code):
    chiplet, pins = _mutated(REFUSALS[code])
    with pytest.raises(g.SchematicRefused) as exc:
        _generate(tmp_path, chiplet=chiplet, pins=pins)
    assert [r["code"] for r in exc.value.refusals] == [code]
    assert not (tmp_path / "pnr").exists()


@pytest.mark.parametrize("name", ["x[3]", "x[03]", "a[1].b[2]"])
def test_subscripted_net_names_are_drawn(tmp_path, name):
    chiplet, pins = _mutated(lambda c, p: c["netlist"]["nets"][0].__setitem__("name", name))
    _, out, result = _generate(tmp_path, chiplet=chiplet, pins=pins)
    assert name in result["nets"]
    assert f'(global_label "{name}"' in out.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["a b", "bus[0..3]", "bus[a..b]", "[0..1]x", "[0]x", "a[x]", "a[",
                                  "a]", "a[]", "b{0}", "~{RST}", "x/y", "a\"b", "${VAR}",
                                  "", "Net-(U1-A0)"])
def test_net_names_kicad_would_rewrite_are_refused(tmp_path, name):
    chiplet, pins = _mutated(lambda c, p: c["netlist"]["nets"][0].__setitem__("name", name))
    with pytest.raises(g.SchematicRefused) as exc:
        _generate(tmp_path, chiplet=chiplet, pins=pins)
    assert [(r["code"], r["field"]) for r in exc.value.refusals] == [("unmappable_name", "net")]


@pytest.mark.parametrize("name", ["SPARE[0..1]", "[0]", "[0][1]", "[1-3]"])
def test_a_terminal_named_as_a_bus_or_stacked_pins_is_refused(tmp_path, name):
    chiplet, pins = _mutated(lambda c, p: p["U2"][3].__setitem__("name", name))
    with pytest.raises(g.SchematicRefused) as exc:
        _generate(tmp_path, chiplet=chiplet, pins=pins)
    assert [(r["code"], r["field"]) for r in exc.value.refusals] == [
        ("unmappable_name", "terminal")]


def test_a_die_symbol_names_the_footprint_the_board_draws_for_its_cell(tmp_path):
    """The board names a die footprint chiplet_dies:<top cell>; the symbol
    names the same, so KiCad pairs the two. The carrier itself is drawn by no
    symbol, only its pads."""
    _, out, _ = _generate(tmp_path, chiplet=CARRIER_CHIPLET, pins=PINS)
    tree = sx.parse(out.read_text(encoding="utf-8"))
    footprint = {}
    for sym in sx.find_all(tree, "symbol"):
        fields = {p[1]: p[2] for p in sx.find_all(sym, "property")}
        footprint[fields["Reference"]] = fields["Footprint"]
    assert {ref: footprint[ref] for ref in ("U1", "U2")} == {
        "U1": "chiplet_dies:DIE_A", "U2": "chiplet_dies:DIE_B"}
    assert "interposer" not in footprint


@pytest.mark.parametrize("cell", ["DIE A", "DIE/A", "DIE:A", "DIE%A"])
def test_a_die_cell_kicad_cannot_name_as_a_footprint_is_refused(tmp_path, cell):
    chiplet, pins = _mutated(lambda c, p: c["components"][1].__setitem__("top_cell", cell))
    with pytest.raises(g.SchematicRefused) as exc:
        _generate(tmp_path, chiplet=chiplet, pins=pins)
    assert [(r["code"], r["component"], r["field"], r["value"])
            for r in exc.value.refusals] == [("unmappable_name", "U1", "top_cell", cell)]


@pytest.mark.parametrize("cell", [None, ""])
def test_a_die_stating_no_top_cell_is_refused(tmp_path, cell):
    """The board names a die's footprint by its top cell; the sheet does not
    stand another name in for it."""
    def mutate(c, p):
        if cell is None:
            c["components"][1].pop("top_cell", None)
        else:
            c["components"][1]["top_cell"] = cell
    chiplet, pins = _mutated(mutate)
    with pytest.raises(g.SchematicRefused) as exc:
        _generate(tmp_path, chiplet=chiplet, pins=pins)
    assert [(r["code"], r["component"], r["field"]) for r in exc.value.refusals] == [
        ("unmappable_name", "U1", "top_cell")]
    assert "states no top_cell" in exc.value.refusals[0]["message"]


def test_a_subscripted_die_id_is_refused(tmp_path):
    """Only terminals and nets take subscripts; a component id stays plain."""
    def mutate(c, p):
        c["components"][1]["id"] = "U[1]"
        p["U[1]"] = p.pop("U1")
    chiplet, pins = _mutated(mutate)
    with pytest.raises(g.SchematicRefused) as exc:
        _generate(tmp_path, chiplet=chiplet, pins=pins)
    assert ("unmappable_name", "component", "U[1]") in [
        (r["code"], r.get("field"), r.get("value")) for r in exc.value.refusals]


def test_unmappable_terminal_and_die_id_are_refused(tmp_path):
    def mutate(c, p):
        p["U2"][3]["name"] = "SP ARE"
        c["components"][1]["id"] = "U 1"
        p["U 1"] = p.pop("U1")
    chiplet, pins = _mutated(mutate)
    with pytest.raises(g.SchematicRefused) as exc:
        _generate(tmp_path, chiplet=chiplet, pins=pins)
    fields = sorted(r["field"] for r in exc.value.refusals if r["code"] == "unmappable_name")
    assert fields == ["component", "terminal"]


def test_every_refusal_is_reported_not_just_the_first(tmp_path):
    def mutate(c, p):
        REFUSALS["non_die_endpoint"](c, p)
        REFUSALS["unknown_terminal"](c, p)
        REFUSALS["unknown_pin_side"](c, p)
    chiplet, pins = _mutated(mutate)
    with pytest.raises(g.SchematicRefused) as exc:
        _generate(tmp_path, chiplet=chiplet, pins=pins)
    assert sorted(r["code"] for r in exc.value.refusals) == [
        "non_die_endpoint", "unknown_pin_side", "unknown_terminal"]


def test_unreadable_pin_list_is_refused_once(tmp_path):
    cpath, pins = _inputs(tmp_path)
    Path(pins["U2"]).write_text("{not json", encoding="utf-8")
    with pytest.raises(g.SchematicRefused) as exc:
        g.generate(cpath, pins, tmp_path / "out.kicad_sch")
    assert [r["code"] for r in exc.value.refusals] == ["malformed_pin_list"]


# ----------------------------------------------------------------------- cli

def _cli(tmp_path, *extra, chiplet=None, pins=None):
    cpath, pin_paths = _inputs(tmp_path, chiplet=chiplet, pins=pins)
    out, report = tmp_path / "pnr" / "sys.draft.kicad_sch", tmp_path / "pnr" / "schematic.json"
    argv = ["--chiplet", str(cpath), "--out", str(out), "--report", str(report)]
    for inst, p in pin_paths.items():
        argv += ["--pins", f"{inst}={p}"]
    code = g.main(argv + list(extra))
    return code, out, json.loads(report.read_text(encoding="utf-8"))


def test_cli_written(tmp_path):
    code, out, report = _cli(tmp_path)
    assert code == g.EXIT_WRITTEN and report["word"] == "written"
    assert report["format"] == "gds2kicad.system-schematic" and report["version"] == "1.3"
    assert report["schematic_sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert report["nets"] == 4 and report["unconnected_terminals"] == 1
    assert report["check"] == {"word": "not_run"} and report["refusals"] == []
    # Paths are relative to the report, so a container mount reads the same.
    assert report["chiplet"] == "../design.chiplet"
    assert report["schematic"] == "sys.draft.kicad_sch"
    assert report["pins"]["U1"]["path"] == "../U1.pins.json"


def test_cli_reports_the_carrier_and_its_pin_list(tmp_path):
    code, out, report = _cli(tmp_path, chiplet=CARRIER_CHIPLET)
    assert code == g.EXIT_WRITTEN and report["word"] == "written"
    pin_file = out.parent / "interposer.carrier_pins.json"
    assert report["pins"]["interposer"] == {
        "path": "interposer.carrier_pins.json",
        "sha256": hashlib.sha256(pin_file.read_bytes()).hexdigest()}
    assert report["components"][0] == {"id": "interposer", "type": "interposer",
                                       "top_cell": "CARRIER", "terminals": 5}
    assert [c["type"] for c in report["components"][1:]] == ["die", "die"]
    assert report["nets"] == 5 and report["unconnected_terminals"] == 2


def test_report_paths_outside_the_tree_stay_absolute():
    assert g._report_ref("/opt/adk/x.json", "/work/run/pnr") == "/opt/adk/x.json"
    assert g._report_ref("/work/run/design.chiplet", "/work/run/pnr") == "../design.chiplet"
    assert g._report_ref("/work/in/U1.json", "/work/run/pnr") == "../../in/U1.json"


def test_cli_refused_writes_the_report_only(tmp_path):
    chiplet, pins = _mutated(REFUSALS["non_die_endpoint"])
    code, out, report = _cli(tmp_path, chiplet=chiplet, pins=pins)
    assert code == g.EXIT_REFUSED and report["word"] == "refused"
    assert report["schematic"] is None and not out.exists()
    assert [r["code"] for r in report["refusals"]] == ["non_die_endpoint"]


def test_cli_check_without_kicad_cli_is_not_a_pass(tmp_path):
    code, out, report = _cli(tmp_path, "--check", "--kicad-cli", "no-such-kicad-cli")
    assert code == g.EXIT_CHECK and out.exists()
    assert report["word"] == "written" and report["check"]["word"] == "unavailable"


# ----------------------------------------------- reading a KiCad netlist back

def _netlist_text(nets, free, identity, digest):
    comps = "".join(
        f'(comp (ref "{ref}") (value "X") (footprint "")'
        f' (fields (field (name "Footprint")) (field (name "{g.IDENTITY_PROPERTY}") "{cid}"))'
        f' (libsource (lib "chiplet") (part "X") (description ""))'
        f' (property (name "{g.IDENTITY_PROPERTY}") (value "{cid}"))'
        f' (property (name "Sheetname") (value "Root"))'
        f' (sheetpath (names "/") (tstamps "/")) (tstamps "00"))\n'
        for ref, cid in identity.items())
    body, code = "", 1
    for name, eps in nets.items():
        nodes = "".join(f'(node (ref "{r}") (pin "{p}") (pintype "input"))' for r, p in sorted(eps))
        body += f'(net (code "{code}") (name "{name}") (class "Default") {nodes})\n'
        code += 1
    for r, p in sorted(free):
        body += (f'(net (code "{code}") (name "unconnected-({r}-{p}-Pad{p})") '
                 f'(class "Default") (node (ref "{r}") (pin "{p}") (pintype "passive+no_connect")))\n')
        code += 1
    return (f'(export (version "E") (design (source "sys.kicad_sch") (tool "Eeschema")'
            f' (sheet (number "1") (name "/") (tstamps "/") (title_block (title "sys")'
            f' (company) (rev) (date) (source "sys.kicad_sch")'
            f' (comment (number "1") (value "{g.BASE_DIGEST_PREFIX}{digest}")))))\n'
            f'(components {comps}) (nets {body}))\n')


def test_read_kicad_netlist_matches(tmp_path):
    nets, free = _expected()
    path = tmp_path / "sys.net"
    path.write_text(_netlist_text(nets, free, {"U1": "U1", "U2": "U2"}, "ab" * 32))
    got = sc.read_kicad_netlist(path)
    assert got["identity"] == {"U1": "U1", "U2": "U2"}
    assert got["base_sha256"] == "ab" * 32
    assert sc.compare(nets, free, "ab" * 32, got) == []


def test_the_check_names_every_pad_that_does_not_fold():
    """The check folds pads as the connectivity reader does, and says why a
    pad stayed a symbol of its own."""
    pad = (g.SYSTEM_LIB, g.EXT_PAD)
    pads, refusals = sc.pad_carriers(
        [("J_A", *pad, "ip"), ("J_A", "chiplet", "DIE_A", "U1"), ("J_B", *pad, "ip"),
         ("J_C", *pad, "ip")], {"ip": {"J_A", "J_C"}})
    assert pads == {"J_C": "ip"}
    assert [(r["code"], r["component"], r.get("terminal")) for r in refusals] == [
        ("duplicate_reference", "J_A", None), ("unknown_pad", "ip", "J_B")]
    got = {"nets": {}, "unconnected": frozenset(), "identity": {}, "base_sha256": "ab" * 32,
           "refusals": refusals}
    assert sc.compare({}, frozenset(), "ab" * 32, got) == [r["message"] for r in refusals]


def test_compare_names_every_difference(tmp_path):
    nets, free = _expected()
    edited = dict(nets)
    edited["sig0"] = frozenset({("U1", "A0")})
    edited["Net-(U2-B0)"] = frozenset({("U2", "B0")})
    del edited["probe"]
    path = tmp_path / "sys.net"
    path.write_text(_netlist_text(edited, free | {("U1", "NC1")},
                                  {"U1": "U1", "U2": "U7"}, "cd" * 32))
    diffs = sc.compare(nets, free, "ab" * 32, sc.read_kicad_netlist(path))
    assert diffs == [
        "symbol U2 carries CHIPLET_COMPONENT=U7",
        "net Net-(U2-B0) is not in the description: U7.B0",
        "net VDD lacks U2.VDD",
        "net VDD also has U7.VDD",
        "net probe is missing",
        "net sig0 lacks U2.B0",
        "net sig1 lacks U2.B1",
        "net sig1 also has U7.B1",
        "terminals not reported unconnected: U2.SPARE",
        "terminals unconnected that the description connects: U1.NC1, U7.SPARE",
        f"base digest is {'cd' * 32!r}, not {'ab' * 32}",
    ]


# ---------------------------------------------------------- KiCad itself

@pytest.mark.kicad_cli
@pytest.mark.parametrize("chiplet, pins", [
    (CHIPLET, PINS), (CARRIER_CHIPLET, PINS),
    (_bus_named(CARRIER_CHIPLET), _bus_named(PINS))], ids=["dies", "carrier", "bus_names"])
def test_kicad_cli_netlist_matches_the_description(tmp_path, chiplet, pins):
    if shutil.which("kicad-cli") is None:
        if os.environ.get("GDS_TO_KICAD_REQUIRE_KICAD_CLI") == "1":
            pytest.fail("GDS_TO_KICAD_REQUIRE_KICAD_CLI=1 but kicad-cli is not on PATH")
        pytest.skip("kicad-cli not on PATH (GDS_TO_KICAD_REQUIRE_KICAD_CLI=1 requires it)")
    cpath, out, result = _generate(tmp_path, chiplet=chiplet, pins=pins)
    check = sc.run_check(out, result["nets"], result["unconnected"], result["chiplet_sha256"])
    assert check == {"word": "matches", "differences": []}
