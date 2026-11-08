# SPDX-License-Identifier: GPL-3.0-or-later
"""Classic connectivity forms from hand-written KiCad inputs, without KiCad."""
import hashlib
import json
from pathlib import Path

import pytest

import chiplet_to_kicad_sch as g
import die_handout as dh
import kicad_sch_to_connectivity as k
import sexpr_io as sx
from tests import test_kicad_sch_to_connectivity as bt
from tests.sheet_drawing import draw_from_library
from tests import test_schematic_handout_dies as ht

# A sheet drawn by draw_units_golden and its netlist from the tools image's kicad-cli.
UNITS_GOLDEN = Path(__file__).resolve().parent / "fixtures" / "v8_units"

UUIDS = {"U1": "00000001-0000-4000-8000-000000000001",
         "U2": "00000002-0000-4000-8000-000000000001"}
# The libpart pins of bt's blank netlist, cached on the sheet unless a test caches its own.
DIE_PINS = {"chiplet_dies:DIE_A": ("A0", "A1", "VDD1"), "chiplet_dies:DIE_B": ("B0", "VDD")}


def _files(tmp_path, sheet=None, edit=None, erc=None):
    net, schematic, lib = bt._blank_files(tmp_path)
    tree = sx.parse(net.read_text(encoding="utf-8"))
    for comp in sx.find_all(sx.find(tree, "components"), "comp"):
        comp.append(["tstamps", sx.Quoted(UUIDS[k._value(comp, "ref")])])
    if edit:
        edit(tree)
    net.write_text(sx.dumps(tree) + "\n", encoding="utf-8")
    root = sx.parse(sheet) if sheet is not None else ["kicad_sch", ["lib_symbols"]]
    known = {k._value(p, "uuid") for p in sx.find_all(root, "symbol")}
    for ref, uuid in UUIDS.items():
        if uuid not in known:
            root.append(["symbol", ["uuid", sx.Quoted(uuid)],
                         ["lib_id", sx.Quoted(f"chiplet_dies:{'DIE_A' if ref == 'U1' else 'DIE_B'}")]])
    cache = sx.find(root, "lib_symbols")
    cached = {str(s[1]) for s in sx.find_all(cache, "symbol")}
    for name, pins in DIE_PINS.items():
        if name not in cached:
            cache.append(_part(name, (1, 1, None, [_pin(n, 0, -2.54 * i)
                                                   for i, n in enumerate(pins)])))
    schematic.write_text(sx.dumps(root), encoding="utf-8")
    system = schematic.with_name("chiplet_system.kicad_sym")
    system.write_text("(kicad_symbol_lib)\n", encoding="utf-8")
    erc_path = None
    if erc is not None:
        erc_path = schematic.with_name("erc.json")
        erc_path.write_text(json.dumps(erc), encoding="utf-8")
    return net, schematic, lib, system, erc_path


def _form(tmp_path, **kwargs):
    net, sch, lib, system, erc = _files(tmp_path, **kwargs)
    form = k.read_form(net, sch, None, sch.with_name("connectivity.json"), erc=erc,
                       library=lib, system_library=system)
    return form, (net, sch, lib, system, erc)


def _rail(name="VDD", cell="SUPPLY", volts="1.2", lib="chiplet_system"):
    props = [["property", sx.Quoted("Value"), sx.Quoted(name)]]
    if volts is not None:
        props.append(["property", sx.Quoted("Volts"), sx.Quoted(volts)])
    return ["symbol", ["lib_id", sx.Quoted(f"{lib}:{cell}")], *props]


def _power_part(name, pin_type="power_in"):
    return ["symbol", sx.Quoted(name), ["power"],
            ["symbol", sx.Quoted(name.split(":")[-1] + "_1_1"), ["pin", pin_type]]]


def _sheet(*symbols, cache=()):
    known = {str(s[1]) for s in cache}
    defaults = [_power_part(name) for name in k._RAILS if name not in known]
    return sx.dumps(["kicad_sch", ["lib_symbols", *defaults, *cache], *symbols])


def _comp(tree, ref="U1"):
    return next(c for c in sx.find_all(sx.find(tree, "components"), "comp")
                if k._value(c, "ref") == ref)


def test_classic_head_and_verbatim_symbol_data(tmp_path):
    def edit(tree):
        comp = _comp(tree)
        sx.find(comp, "value")[1] = sx.Quoted("~")
        comp.append(["fields", ["field", ["name", sx.Quoted("Empty")]],
                     ["field", ["name", sx.Quoted("Die")], sx.Quoted("dies/A")],
                     ["field", ["name", sx.Quoted("Raw")], sx.Quoted(r"x{slash}y\nz")]])
        sx.find(sx.find(tree, "nets"), "net")[2][1] = sx.Quoted("a{slash}b")
    form, (net, sch, lib, system, _) = _form(tmp_path, edit=edit)
    assert (form["version"], form["sheet"]) == ("1.3", "classic")
    assert form["base_sha256"] is None and form["generator_report"] is None
    assert form["library"] == {"path": lib.name, "sha256": bt._sha(lib)}
    assert form["system_library"] == {"path": system.name, "sha256": bt._sha(system)}
    assert form["front_end"]["reader"] == f"{k.READER} 1.3"
    assert form["front_end"]["inputs"] == [
        {"path": p.name, "sha256": bt._sha(p)} for p in (net, sch)]
    assert form["symbols"][0] == {
        "ref": "U1", "identity": None, "part": {"lib": "chiplet_dies", "name": "DIE_A"},
        "pins": ["A0", "A1", "VDD1"], "key": "/" + UUIDS["U1"], "value": "~",
        "footprint": "", "fields": {"Empty": "", "Die": "dies/A", "Raw": r"x{slash}y\nz"},
        "units": [{"name": "A", "pins": ["A0", "A1", "VDD1"]}], "jumpers": False}
    assert "a{slash}b" in {n["name"] for n in form["nets"]}
    assert form["rails"] == [] and form["open"] == [] and form["refusals"] == []


def test_nonempty_footprint_fields_and_value_are_verbatim(tmp_path):
    def edit(tree):
        comp = _comp(tree)
        sx.find(comp, "value")[1] = sx.Quoted("a{slash}b")
        comp.append(["footprint", sx.Quoted("lib:a{slash}b")])
        comp.append(["fields", ["field", ["name", sx.Quoted("Edge")], sx.Quoted("west")]])
    sym = _form(tmp_path, edit=edit)[0]["symbols"][0]
    assert (sym["value"], sym["footprint"], sym["fields"]) == (
        "a{slash}b", "lib:a{slash}b", {"Edge": "west"})


def test_only_explicit_no_connect_flags_are_open_sorted_and_unique(tmp_path):
    def edit(tree):
        for net in sx.find_all(sx.find(tree, "nets"), "net"):
            for node in sx.find_all(net, "node"):
                ep = k._value(node, "ref"), k._value(node, "pin")
                sx.find(node, "pintype")[1] = sx.Quoted(
                    "no_connect" if ep == ("U1", "A1") else "passive+no_connect")
        net = sx.find(sx.find(tree, "nets"), "net")
        net.append(sx.find(net, "node"))
    form, _ = _form(tmp_path, edit=edit)
    assert form["open"] == [["U1", "A0"], ["U1", "VDD1"], ["U2", "B0"], ["U2", "VDD"]]
    assert {n["name"] for n in form["nets"]} == set(bt.NETS)


def test_rails_are_unique_sorted_and_ground_has_no_volts(tmp_path):
    sheet = _sheet(_rail(), _rail(), _rail("sig0", "GND", "ignored"))
    form, _ = _form(tmp_path, sheet=sheet)
    assert form["rails"] == [{"name": "VDD", "class": "power", "volts": "1.2"},
                             {"name": "sig0", "class": "ground", "volts": None}]
    assert not form["refusals"]


@pytest.mark.parametrize("volts", [None, "", "3.3", "001.20", "0.0001", "1" + "0" * 400])
def test_valid_supply_volts_matches_form_reader(tmp_path, volts):
    form, _ = _form(tmp_path, sheet=_sheet(_rail(volts=volts)))
    assert form["rails"] == [{"name": "VDD", "class": "power", "volts": volts or None}]
    assert not form["refusals"]


@pytest.mark.parametrize("volts", ["3V3", "3.3 V", "0", "0.00", "-1", "+1", "1e2", ".5", "1.",
                                    " 1.2", "1.2\n", "NaN", "Infinity"])
def test_invalid_supply_is_refused_and_omitted(tmp_path, volts):
    form, _ = _form(tmp_path, sheet=_sheet(_rail(volts=volts)))
    assert bt._codes(form) == [("bad_volts", None, None, "VDD")]
    assert "write 3.3, not 3V3 or 3.3 V" in form["refusals"][0]["message"]
    assert form["rails"] == []


@pytest.mark.parametrize("other", [_rail(volts="3.3"), _rail(volts=None),
                                  _rail(cell="GND", volts=None)])
@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_rails_are_omitted_in_either_order(tmp_path, other, reverse):
    rails = [_rail(), other]
    form, _ = _form(tmp_path, sheet=_sheet(*(rails[::-1] if reverse else rails)))
    assert bt._codes(form) == [("rail_volts_conflict", None, None, "VDD")]
    assert form["rails"] == []


def test_invalid_occurrence_cannot_be_hidden_by_a_valid_one(tmp_path):
    form, _ = _form(tmp_path, sheet=_sheet(_rail(volts="3V3"), _rail()))
    assert {r["code"] for r in form["refusals"]} == {"bad_volts"}
    assert form["rails"] == []


def test_unused_rail_is_omitted(tmp_path):
    form, _ = _form(tmp_path, sheet=_sheet(_rail("unused")))
    assert bt._codes(form) == [("rail_unused", None, None, "unused")]
    assert form["rails"] == []


def test_foreign_rail_uses_cached_power_token_not_library_name(tmp_path):
    foreign = _power_part("other:SUPPLY")
    ordinary = ["symbol", sx.Quoted("power:DEVICE")]
    form, _ = _form(tmp_path, sheet=_sheet(_rail(lib="other"), _rail(cell="DEVICE", lib="power"),
                                        _rail(), cache=(foreign, ordinary)))
    assert bt._codes(form) == [("foreign_rail", None, None, "VDD")]
    assert "other:SUPPLY" in form["refusals"][0]["message"]
    assert "chiplet_system" in form["refusals"][0]["message"]
    assert form["rails"] == []


def test_only_root_placed_symbols_are_rails(tmp_path):
    cached = ["symbol", sx.Quoted("chiplet_system:SUPPLY"), ["power"]]
    form, _ = _form(tmp_path, sheet=_sheet(["sheet", _rail()], cache=(cached,)))
    assert form["rails"] == [] and not form["refusals"]


def test_multi_unit_is_decided_from_cache(tmp_path):
    cached = ["symbol", sx.Quoted("chiplet_dies:DIE_A"),
              ["symbol", sx.Quoted("DIE_A_2_1")]]
    form, _ = _form(tmp_path, sheet=_sheet(cache=(cached,)))
    assert bt._codes(form) == [("multi_unit", "U1", None, None)]
    assert form["symbols"][0]["key"] == "/" + UUIDS["U1"]


def test_existing_refusals_apply_to_classic(tmp_path):
    def edit(tree):
        design = sx.find(tree, "design")
        design.append(["sheet", ["name", sx.Quoted("/sub/")]])
        sx.find(_comp(tree, "U2"), "ref")[1] = sx.Quoted("U1")
        net = sx.find(sx.find(tree, "nets"), "net")
        sx.find(net, "name")[1] = sx.Quoted("/local")
        net.append(["node", ["ref", sx.Quoted("U1")], ["pin", sx.Quoted("unknown")]])
    form, _ = _form(tmp_path, edit=edit, erc=bt._erc(["multiple_net_names"]))
    assert {r["code"] for r in form["refusals"]} == {
        "hierarchical_sheet", "duplicate_reference", "local_label", "symbol_pin_mismatch",
        "multiple_net_names"}


@pytest.mark.parametrize("mode", ["generated", "blank"])
def test_other_modes_do_not_scan_rails_or_write_classic_keys(tmp_path, mode):
    library = None
    if mode == "blank":
        net, sch, library = bt._blank_files(tmp_path)
        report = None
    else:
        net, sch, report, _ = bt._files(tmp_path)
    sch.write_text(_sheet(_rail(volts="bad")), encoding="utf-8")
    form = k.read_form(net, sch, report, sch.with_name("form.json"), library=library)
    assert not {"rails", "open", "system_library"} & form.keys()
    assert all(not {"key", "value", "footprint", "fields"} & s.keys() for s in form["symbols"])
    assert form["front_end"]["reader"] == f"{k.READER} 1.1"
    assert form["refusals"] == []


def test_classic_cli_is_byte_stable_strict_json(tmp_path):
    net, sch, lib, system, _ = _files(tmp_path)
    out = tmp_path / "out" / "form.json"
    argv = [str(net), "--classic", str(sch), str(out), "--library", str(lib),
            "--system-library", str(system), "--no-erc"]
    assert k.main(argv) == 0
    first = out.read_bytes()
    assert json.loads(first)["system_library"]["path"] == "../kicad/chiplet_system.kicad_sym"
    assert k.main(argv) == 0 and out.read_bytes() == first


@pytest.mark.parametrize("flags", [[], ["--classic"], ["--library", "LIB"],
    ["--system-library", "SYS"], ["--classic", "--library", "LIB"],
    ["--classic", "--system-library", "SYS"],
    ["--blank", "--library", "LIB", "--system-library", "SYS"],
    ["--blank", "--classic", "--library", "LIB", "--system-library", "SYS"]])
def test_cli_refuses_flag_mixes(tmp_path, flags):
    with pytest.raises(SystemExit) as exc:
        k.main(["net", "sch", str(tmp_path / "form.json"), "--no-erc", *flags])
    assert exc.value.code == 2
    assert not (tmp_path / "form.json").exists()


@pytest.mark.parametrize("bad", ["missing_library", "missing_system", "bad_sheet", "bad_uuid"])
def test_classic_input_errors_do_not_write_a_form(tmp_path, bad):
    net, sch, lib, system, _ = _files(tmp_path)
    if bad == "missing_library":
        lib.unlink()
    elif bad == "missing_system":
        system.unlink()
    elif bad == "bad_sheet":
        sch.write_text("(not_a_schematic)", encoding="utf-8")
    else:
        net.write_text(net.read_text().replace(UUIDS["U1"], "invalid"), encoding="utf-8")
    out = tmp_path / "form.json"
    assert k.main([str(net), str(sch), str(out), "--classic", "--library", str(lib),
                   "--system-library", str(system), "--no-erc"]) == k.EXIT_INPUT
    assert not out.exists()


def _placed(ref, uuid, part="DIE_A", properties=None, lib_name=None, unit="1"):
    symbol = ["symbol", ["uuid", sx.Quoted(uuid)],
              ["lib_id", sx.Quoted(f"chiplet_dies:{part}")], ["unit", unit],
              ["property", sx.Quoted("Reference"), sx.Quoted(ref)]]
    symbol += [["property", sx.Quoted(k), sx.Quoted(v)]
               for k, v in (properties or {}).items()]
    if lib_name is not None:
        symbol.append(["lib_name", sx.Quoted(lib_name)])
    return symbol


def _merge_references(tree):
    comps = sx.find(tree, "components")
    first, second = sx.find_all(comps, "comp")
    sx.find(first, "tstamps").extend(sx.find(second, "tstamps")[1:])
    comps.remove(second)


def test_shared_reference_is_two_records_with_each_placed_symbols_fields(tmp_path):
    props = [{"Value": "alpha{slash}value", "Footprint": "parts:alpha", "Die": "dies/A",
              "Method": "first", "Thickness_um": "750", "Empty": ""},
             {"Value": "~", "Footprint": "", "Die": "dies/B",
              "Method": "second", "Thickness_um": "250"}]
    sheet = _sheet(*(_placed("U1", uuid, properties=p) for uuid, p in zip(UUIDS.values(), props)))
    form, _ = _form(tmp_path, sheet=sheet, edit=_merge_references)
    assert bt._codes(form) == [("duplicate_reference", "U1", None, None)]
    symbols = form["symbols"]
    assert [s["ref"] for s in symbols] == ["U1", "U1"]
    assert [s["key"] for s in symbols] == ["/" + u for u in UUIDS.values()]
    for symbol, properties in zip(symbols, props):
        assert symbol["value"] == properties["Value"]
        assert symbol["footprint"] == properties["Footprint"]
        assert symbol["fields"] == {k: v for k, v in properties.items() if k != "Value"}


def test_multiple_units_use_only_the_primary_uuid_and_cache_refusal(tmp_path):
    cached = ["symbol", sx.Quoted("chiplet_dies:DIE_A"),
              ["symbol", sx.Quoted("DIE_A_2_1")]]
    sheet = _sheet(*(_placed("U1", uuid, unit=str(i))
                     for i, uuid in enumerate(UUIDS.values(), 1)), cache=(cached,))
    form, _ = _form(tmp_path, sheet=sheet, edit=_merge_references)
    assert bt._codes(form) == [("multi_unit", "U1", None, None)]
    assert [s["key"] for s in form["symbols"]] == ["/" + UUIDS["U2"]]


def test_twice_placed_subsheet_cannot_repeat_form_keys(tmp_path):
    def edit(tree):
        comps = sx.find(tree, "components")
        child = _comp(tree, "U2")
        sx.find(sx.find(child, "sheetpath"), "names")[1] = sx.Quoted("/left/")
        other = sx.parse(sx.dumps(child))
        sx.find(sx.find(other, "sheetpath"), "names")[1] = sx.Quoted("/right/")
        sx.find(other, "ref")[1] = sx.Quoted("U3")
        comps.append(other)
        sx.find(tree, "design").extend([
            ["sheet", ["name", sx.Quoted("/left/")]],
            ["sheet", ["name", sx.Quoted("/right/")]]])
    form, (_, sch, *_) = _form(tmp_path, edit=edit)
    assert [s["ref"] for s in form["symbols"]] == ["U1"]
    assert len([r for r in form["refusals"] if r["code"] == "hierarchical_sheet"]) == 2
    out = sch.with_name("connectivity.json")
    k.write_form(form, out)
    written = json.loads(out.read_text())
    keys = [symbol["key"] for symbol in written["symbols"]]
    assert keys == ["/" + UUIDS["U1"]] and len(keys) == len(set(keys))


def test_multi_unit_cache_lookup_uses_placed_lib_name(tmp_path):
    cached = ["symbol", sx.Quoted("saved_copy"), ["symbol", sx.Quoted("DIE_A_2_1")]]
    sheet = _sheet(_placed("U1", UUIDS["U1"], lib_name="saved_copy"), cache=(cached,))
    def edit(tree):
        source = sx.find(_comp(tree), "libsource")
        sx.find(source, "lib")[1] = sx.Quoted("")
        sx.find(source, "part")[1] = sx.Quoted("saved_copy")
        part = sx.find(sx.find(tree, "libparts"), "libpart")
        sx.find(part, "lib")[1] = sx.Quoted("")
        sx.find(part, "part")[1] = sx.Quoted("saved_copy")
    form, _ = _form(tmp_path, sheet=sheet, edit=edit)
    assert bt._codes(form) == [("multi_unit", "U1", None, None)]
    assert form["symbols"][0]["part"] == {"lib": "chiplet_dies", "name": "DIE_A"}


def test_foreign_power_cache_lookup_uses_placed_lib_name(tmp_path):
    foreign = _rail(lib="foreign")
    foreign.append(["lib_name", sx.Quoted("saved_power")])
    cached = _power_part("saved_power")
    form, _ = _form(tmp_path, sheet=_sheet(foreign, cache=(cached,)))
    assert bt._codes(form) == [("foreign_rail", None, None, "VDD")]


@pytest.mark.parametrize("value, escaped", [("power/domain", "power{slash}domain"),
    ("A\r\n/B", "A{slash}B"), ("A{slash}B", "A{slash}B"), ('A\\B \t"', 'A\\B \t"')])
def test_rail_uses_kicad_netname_escaping(tmp_path, value, escaped):
    def edit(tree):
        sx.find(sx.find(sx.find(tree, "nets"), "net"), "name")[1] = sx.Quoted(escaped)
    form, _ = _form(tmp_path, sheet=_sheet(_rail(value)), edit=edit)
    assert form["rails"] == [{"name": escaped, "class": "power", "volts": "1.2"}]
    assert not form["refusals"]


@pytest.mark.parametrize("rows, code", [
    ([_rail(volts="1.2"), _rail(volts="3.3"), _rail(volts="1.2")], "rail_volts_conflict"),
    ([_rail("unused", volts="bad"), _rail("unused", volts="1.2")], "bad_volts"),
    ([_rail("unused", volts="1.2"), _rail("unused", volts="3.3")], "rail_volts_conflict"),
    ([_rail("unused"), _rail("unused")], "rail_unused")])
def test_rail_is_decided_once_with_invalid_then_conflict_then_unused_priority(tmp_path, rows, code):
    form, _ = _form(tmp_path, sheet=_sheet(*rows))
    assert len(form["refusals"]) == 1
    assert form["refusals"][0]["code"] == code
    assert form["rails"] == []


def test_rail_diagnostics_are_sorted_by_name(tmp_path):
    form, _ = _form(tmp_path, sheet=_sheet(_rail("z", volts="bad"), _rail("a"), _rail("m")))
    assert [r["net"] for r in form["refusals"]] == ["a", "m", "z"]


@pytest.mark.parametrize("mode, supplied", [("classic", {}), ("blank", {"system_library": "x"}),
    ("generated", {"library": "x"}), ("unknown", {})])
def test_python_api_validates_mode_in_the_same_place_as_cli(tmp_path, mode, supplied):
    with pytest.raises(ValueError):
        k.read_form("net", "sch", None, tmp_path / "form.json", mode=mode, **supplied)


def test_power_flag_on_a_rail_is_not_a_foreign_rail(tmp_path):
    flag = _rail("PWR_FLAG", cell="PWR_FLAG", lib="power", volts=None)
    form, _ = _form(tmp_path, sheet=_sheet(_rail(), flag,
                                          cache=(_power_part("power:PWR_FLAG", "power_out"),)))
    assert form["refusals"] == []
    assert form["rails"] == [{"name": "VDD", "class": "power", "volts": "1.2"}]


def test_even_system_power_symbols_need_a_net_naming_pin(tmp_path):
    form, _ = _form(tmp_path, sheet=_sheet(_rail(), cache=(
        _power_part("chiplet_system:SUPPLY", "power_out"),)))
    assert form["refusals"] == [] and form["rails"] == []


def test_private_volts_is_read_from_its_actual_property_name(tmp_path):
    rail = _rail(volts=None)
    rail.append(["property", "private", sx.Quoted("Volts"), sx.Quoted("1.2")])
    form, _ = _form(tmp_path, sheet=_sheet(rail))
    assert form["refusals"] == []
    assert form["rails"] == [{"name": "VDD", "class": "power", "volts": "1.2"}]


def test_quoted_property_named_private_is_not_a_private_modifier():
    assert k._sheet_properties(["symbol", ["property", sx.Quoted("private"), sx.Quoted("text")]]) == {
        "private": "text"}


def test_split_fields_match_netlist_shape_and_leave_private_properties_out(tmp_path):
    placed = [_placed("U1", uuid, properties={"Value": "", "Footprint": "", "Method": "${METHOD}"})
              for uuid in UUIDS.values()]
    for symbol in placed:
        symbol.append(["property", "private", sx.Quoted("Hidden"), sx.Quoted("raw{slash}text")])
    form, _ = _form(tmp_path, sheet=_sheet(*placed), edit=_merge_references)
    assert bt._codes(form) == [("duplicate_reference", "U1", None, None)]
    for symbol in form["symbols"]:
        assert symbol["value"] == "~" and symbol["footprint"] == ""
        assert symbol["fields"] == {"Footprint": "", "Method": "${METHOD}"}


@pytest.mark.parametrize("same_part", [False, True], ids=["different_parts", "repeated_unit"])
def test_shared_references_are_not_lost_when_a_part_has_multiple_units(tmp_path, same_part):
    parts = ("DIE_A", "DIE_A" if same_part else "DIE_B")
    cache = [["symbol", sx.Quoted(f"chiplet_dies:{part}"),
              ["symbol", sx.Quoted(f"{part}_2_1")]] for part in set(parts)]
    placed = [_placed("U1", uuid, part=part, unit="1" if same_part else str(i))
              for i, (uuid, part) in enumerate(zip(UUIDS.values(), parts), 1)]
    def edit(tree):
        _merge_references(tree)
        nets = sx.find(tree, "nets")
        nums = ("A0", "A1") if same_part else ("A0", "B0")
        nets[:] = ["nets", ["net", ["name", sx.Quoted("signal")], *[
            ["node", ["ref", sx.Quoted("U1")], ["pin", sx.Quoted(num)]] for num in nums]]]
    form, _ = _form(tmp_path, sheet=_sheet(*placed, cache=cache), edit=edit)
    assert bt._codes(form) == [("multi_unit", "U1", None, None),
                               ("duplicate_reference", "U1", None, None)]
    assert [s["key"] for s in form["symbols"]] == ["/" + u for u in UUIDS.values()]
    assert len({s["key"] for s in form["symbols"]}) == 2
    assert [s["part"]["name"] for s in form["symbols"]] == list(parts)
    assert [s["pins"] for s in form["symbols"]] == [sorted(bt.LIBPARTS[part]) for part in parts]


def test_two_different_single_unit_parts_keep_their_own_pins(tmp_path):
    placed = [_placed("U1", uuid, part=part) for uuid, part in zip(UUIDS.values(), bt.LIBPARTS)]
    def edit(tree):
        _merge_references(tree)
        for node in sx.find_all_rec(sx.find(tree, "nets"), "node"):
            if k._value(node, "ref") == "U2":
                sx.find(node, "ref")[1] = sx.Quoted("U1")
    form, _ = _form(tmp_path, sheet=_sheet(*placed), edit=edit)
    assert bt._codes(form) == [("duplicate_reference", "U1", None, None)]
    assert [s["pins"] for s in form["symbols"]] == [sorted(p) for p in bt.LIBPARTS.values()]


def test_a_split_part_kicad_lists_no_libpart_for_takes_its_cached_pins(tmp_path):
    # KiCad lists a libpart only for the first symbol of a reference.
    first, second = bt.LIBPARTS
    placed = [_placed("U1", uuid, part=part) for uuid, part in zip(UUIDS.values(), bt.LIBPARTS)]
    # Drawn in two body styles, so the cache lists every pin twice.
    cache = [["symbol", sx.Quoted(f"chiplet_dies:{second}"), *[
        ["symbol", sx.Quoted(f"{second}_1_{style}"),
         *[["pin", "passive", "line", ["at", "0", sx.num(2.54 * i), "0"],
            ["number", sx.Quoted(num)]]
           for i, num in enumerate(bt.LIBPARTS[second])]] for style in (1, 2)]]]
    def edit(tree):
        _merge_references(tree)
        libparts = sx.find(tree, "libparts")
        libparts[:] = [lp for lp in libparts
                       if not (isinstance(lp, list) and k._value(lp, "part") == second)]
        for node in sx.find_all_rec(sx.find(tree, "nets"), "node"):
            if k._value(node, "ref") == "U2":
                sx.find(node, "ref")[1] = sx.Quoted("U1")
    form, _ = _form(tmp_path, sheet=_sheet(*placed, cache=cache), edit=edit)
    assert bt._codes(form) == [("duplicate_reference", "U1", None, None)]
    assert [s["pins"] for s in form["symbols"]] == [sorted(bt.LIBPARTS[first]),
                                                   sorted(bt.LIBPARTS[second])]


@pytest.mark.parametrize("name, volts, expected", [("unused", "bad", "bad_volts"),
    ("unused", "1.2", "foreign_rail"), ("VDD", "3.3", "foreign_rail")])
def test_foreign_rail_priority_follows_bad_volts_and_precedes_conflicts_and_unused(
        tmp_path, name, volts, expected):
    rails = [_rail(name, volts=volts), _rail(name, volts="1.2"), _rail(name, lib="foreign")]
    form, _ = _form(tmp_path, sheet=_sheet(*rails, cache=(_power_part("foreign:SUPPLY"),)))
    assert bt._codes(form) == [(expected, None, None, name)]
    assert form["rails"] == []


def test_cli_does_not_relabel_a_reader_value_error_as_bad_usage(tmp_path, monkeypatch):
    net, sch, lib, system, _ = _files(tmp_path)
    def unreadable(*args, **kwargs):
        raise ValueError("reader error")
    monkeypatch.setattr(k, "read_form", unreadable)
    with pytest.raises(ValueError, match="reader error"):
        k.main([str(net), str(sch), str(tmp_path / "form.json"), "--classic", "--library", str(lib),
                "--system-library", str(system), "--no-erc"])


def _pin(number, x, y):
    return ["pin", "passive", "line", ["at", sx.num(x), sx.num(y), "0"],
            ["number", sx.Quoted(number)]]


def _part(name, *units, flag=None):
    """A cached symbol: each unit is (unit, body style, unit name or None, pins)."""
    body = [["symbol", sx.Quoted(f"{name.split(':')[-1]}_{unit}_{style}"),
             *([["unit_name", sx.Quoted(label)]] if label else []), *pins]
            for unit, style, label, pins in units]
    return ["symbol", sx.Quoted(name), *([[k._JUMPERS, flag]] if flag else []), *body]


def _die_a(*units, flag=None):
    placed = [_placed("U1", UUIDS["U1"])]
    return _sheet(*placed, cache=[_part("chiplet_dies:DIE_A", *units, flag=flag)])


def test_units_list_pins_by_position_x_then_y_with_the_file_y_negated():
    # x first; at one x the higher pin in the file (larger y) is the smaller internal y.
    part = _part("P", (1, 1, None, [_pin("A0", 2.54, 0), _pin("VDD1", -2.54, 0),
                                    _pin("A1", -2.54, 2.54)]))
    assert k._unit_pins(part) == ([{"name": "A", "pins": ["A1", "VDD1", "A0"]}], None)


def test_units_take_common_pins_names_stacks_and_each_number_once():
    part = _part("P", (0, 1, None, [_pin("G", 0, -5.08)]),
                 (1, 1, "Logic", [_pin("[1-3,B4]", -2.54, 0), _pin("2", -2.54, 2.54)]),
                 (1, 2, None, [_pin("9", 5.08, 0)]),
                 (3, 1, None, [_pin("", 0, 0)]))
    assert k._unit_pins(part) == ([
        {"name": "Logic", "pins": ["2", "1", "3", "B4", "G", "9"]},
        {"name": "B", "pins": ["G"]},
        {"name": "C", "pins": ["", "G"]}], None)


@pytest.mark.parametrize("number, expanded", [
    ("[1-3]", ["1", "2", "3"]), ("[A1-A2, 7]", ["A1", "A2", "7"]), ("[3-1]", ["[3-1]"]),
    ("[A1-B2]", ["[A1-B2]"]), ("[1-", ["[1-"]), ("[]", ["[]"]), ("[,]", ["[,]"]),
    ("12", ["12"])])
def test_stacked_numbers_expand_as_kicad_expands_them(number, expanded):
    assert k._expand(number) == expanded


def test_unit_letters_run_past_z():
    assert [k._unit_letter(u) for u in (1, 26, 27, 28, 52, 53)] == [
        "A", "Z", "AA", "AB", "AZ", "BA"]


def test_two_numbers_at_one_position_are_a_clash_and_one_number_is_not():
    clash = _part("P", (1, 1, None, [_pin("1", 0, 0), _pin("2", 0, 0)]))
    same = _part("P", (1, 1, None, [_pin("1", 0, 0)]), (1, 2, None, [_pin("1", 0, 0)]))
    assert k._unit_pins(clash)[1] == ("A", "1", "2")
    assert k._unit_pins(same) == ([{"name": "A", "pins": ["1"]}], None)


def test_classic_symbols_carry_units_and_jumpers(tmp_path):
    sheet = _die_a((1, 1, None, [_pin("VDD1", -2.54, 0), _pin("A0", 2.54, 0),
                                 _pin("A1", 2.54, -2.54)]), flag="yes")
    form, _ = _form(tmp_path, sheet=sheet)
    sym = next(s for s in form["symbols"] if s["ref"] == "U1")
    assert sym["units"] == [{"name": "A", "pins": ["VDD1", "A0", "A1"]}]
    assert sym["jumpers"] is True
    assert form["refusals"] == []


def test_a_symbol_without_the_flag_has_no_jumpers(tmp_path):
    form, _ = _form(tmp_path, sheet=_die_a((1, 1, None, [_pin("A0", 0, 0)])))
    assert next(s for s in form["symbols"] if s["ref"] == "U1")["jumpers"] is False


def test_a_unit_pin_clash_is_refused_and_states_no_units(tmp_path):
    form, _ = _form(tmp_path, sheet=_die_a((1, 1, None, [_pin("A0", 0, 0), _pin("A1", 0, 0)])))
    sym = next(s for s in form["symbols"] if s["ref"] == "U1")
    assert "units" not in sym and sym["jumpers"] is False
    assert bt._codes(form) == [("unit_pin_order", "U1", None, None)]


@pytest.mark.parametrize("flag", ["true", "1"])
def test_a_jumper_flag_that_is_neither_yes_nor_no_is_an_input_error(tmp_path, flag):
    with pytest.raises(k.InputError, match="neither yes nor no"):
        _form(tmp_path, sheet=_die_a((1, 1, None, [_pin("A0", 0, 0)]), flag=flag))


def test_a_cached_symbol_that_extends_another_is_an_input_error(tmp_path):
    part = _part("chiplet_dies:DIE_A")
    part.append(["extends", sx.Quoted("BASE")])
    with pytest.raises(k.InputError, match="extends another"):
        _form(tmp_path, sheet=_sheet(_placed("U1", UUIDS["U1"]), cache=[part]))


# A die whose rails repeat their pad numbers, so its symbol sets the jumper flag.
UNITS_DIE = [("OUT", "OUT", 0, "signal", "output"), ("IN", "IN", 0, "signal", "input"),
             ("VDD", "VDD", 0, "power", "bidir"), ("VDD", "VDD", 1, "power", "bidir"),
             ("VSS", "VSS", 0, "ground", "bidir"), ("VSS", "VSS", 1, "ground", "bidir"),
             ("VSS", "VSS", 2, "ground", "bidir")]


def draw_units_golden(out):
    """The units golden's sheet in ``out``: that die and an EXT_PAD, placed
    from the handout as a student would. Returns the schematic."""
    die = ht._folder(out / "dies" / "RAIL", "RAIL", UNITS_DIE)
    dh.die_handout([die], out, 80, name="units")
    return draw_from_library(
        out, "units", [("U1", "RAIL", 101.6, 101.6), ("J1", "chiplet_system:EXT_PAD", 50.8, 101.6)],
        {"SIG": [("U1", "OUT"), ("J1", "1")], "IN0": [("U1", "IN")]},
        libraries={dh.SYSTEM_LIB: out / dh.SYSTEM_LIB_FILE})


def netlist_units(net):
    """Reference to ``(units, jumpers)`` as a kicadsexpr netlist states them."""
    tree = sx.parse(Path(net).read_text(encoding="utf-8"))
    return {k._value(comp, "ref"): (
        [{"name": k._value(unit, "name"),
          "pins": [k._value(pin, "num") for pin in sx.find_all(sx.find(unit, "pins") or [], "pin")]}
         for unit in sx.find_all(sx.find(comp, "units") or [], "unit")],
        k._value(comp, k._JUMPERS) == "1")
        for comp in sx.find_all(sx.find(tree, "components"), "comp")}


def read_units_form(sch, net):
    form = k.read_form(net, sch, None, sch.with_name("connectivity.json"),
                       library=sch.parent / g.HANDOUT_LIB_FILE,
                       system_library=sch.parent / dh.SYSTEM_LIB_FILE)
    return {s["ref"]: (s["units"], s["jumpers"]) for s in form["symbols"]}, form


def test_the_golden_netlist_states_the_units_and_jumpers_the_reader_writes():
    sch = UNITS_GOLDEN / "units.kicad_sch"
    read, form = read_units_form(sch, UNITS_GOLDEN / "units.net")
    assert form["refusals"] == []
    assert read == netlist_units(UNITS_GOLDEN / "units.net")
    assert read["U1"][1] is True and read["J1"][1] is False
    assert read["U1"][0] == [{"name": "A", "pins": ["VDD", "VSS", "IN", "OUT"]}]


def test_the_golden_sheet_is_what_the_handout_draws_today(tmp_path):
    draw_units_golden(tmp_path)
    for name in ("units.kicad_sch", g.HANDOUT_LIB_FILE, dh.SYSTEM_LIB_FILE):
        assert (tmp_path / name).read_bytes() == (UNITS_GOLDEN / name).read_bytes(), name


def test_the_golden_files_are_the_ones_provenance_names():
    # These lines bind the sheet to its netlist: a redrawn sheet needs its netlist exported again.
    text = (UNITS_GOLDEN.parent / "PROVENANCE").read_text(encoding="utf-8")
    for name in ("units.kicad_sch", g.HANDOUT_LIB_FILE, dh.SYSTEM_LIB_FILE, "units.net"):
        digest = hashlib.sha256((UNITS_GOLDEN / name).read_bytes()).hexdigest()
        assert f"  {name} sha256 {digest}\n" in text, name
