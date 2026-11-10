# SPDX-License-Identifier: GPL-3.0-or-later
"""die_handout: the handout drawn from die folders.

Hermetic: every die folder is written into tmp_path, inside the handout
directory, with a contract and the pins.json inventory it binds.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chiplet_to_kicad_sch as g  # noqa: E402
import die_handout as dh  # noqa: E402
import sexpr_io as sx  # noqa: E402

_INVENTORY_DIRECTION = {"input": "INPUT", "output": "OUTPUT", "bidir": "INOUT",
                        "analog": "ANALOG", "none": "NONE"}

# (pad name, port, ordinal, role, direction). Two VDD pads are one pin.
DRIVER = [
    ("CLK", "CLK", 0, "signal", "input"),
    ("OUT", "OUT", 0, "signal", "output"),
    ("IO", "IO", 0, "signal", "bidir"),
    ("ANA", "ANA", 0, "signal", "analog"),
    ("VDD", "VDD", 0, "power", "bidir"),
    ("VDD", "VDD", 1, "power", "bidir"),
    ("VSS", "VSS", 0, "ground", "bidir"),
]
ECHO = [("STATUS", "STATUS", 0, "signal", "output")]


def _folder(path, cell, pads, inventory=None):
    """A die folder whose inventory states ``inventory`` (``pads`` unless
    given) and whose contract states ``pads``. A port of None is a pad on no
    port; a direction of None is left out."""
    path.mkdir(parents=True, exist_ok=True)

    def rows(spec, contract):
        out = []
        for index, (name, port, ordinal, role, direction) in enumerate(spec):
            row = {"name": name, "pad_index": index, "role": role if contract else role.upper()}
            if port is not None:
                row.update(port=port, ordinal=ordinal)
            if direction is not None:
                row["direction"] = direction if contract else _INVENTORY_DIRECTION[direction]
            if not contract:
                row.update(center_x_dbu=100000.0 + 80000.0 * index, center_y_dbu=100000.0,
                           width_dbu=60000.0, height_dbu=60000.0)
            out.append(row)
        return out

    (path / "pins.json").write_text(json.dumps(
        {"dbu_um": 0.001, "pins": rows(pads if inventory is None else inventory, False)}),
        encoding="utf-8")
    contract = {
        "schema": "die-output-contract", "version": "1.1",
        "die": {"name": cell, "layout": "layout.gds", "top_cell": cell,
                "dbu_um": 0.001, "bbox_um": [0.0, 0.0, 1000.0, 1000.0],
                "frame": "die_local_gds_origin"},
        "process": {"pdk": "ihp-sg13g2", "pdk_version": "0"},
        "inventory": {"kind": "pins_json", "path": "pins.json", "sha256": "0" * 64},
        "terminals": rows(pads, True),
    }
    (path / "die-contract.json").write_text(json.dumps(contract), encoding="utf-8")
    return path


def _symbols(path):
    tree = sx.parse(path.read_text(encoding="utf-8"))
    return [(str(s[1]), s) for s in sx.find_all(tree, "symbol")]


def _props(symbol):
    return {str(p[1]): str(p[2]) for p in sx.find_all(symbol, "property")}


def _pins(symbol):
    found = {}
    for pin in sx.find_all_rec(symbol, "pin"):
        at = sx.find(pin, "at")
        found[str(sx.find(pin, "number")[1])] = {
            "type": str(pin[1]), "name": str(sx.find(pin, "name")[1]),
            "angle": str(at[3]), "x": float(at[1]), "y": float(at[2])}
    return found


def _refused(out, folders, um=80, name="sheet_first"):
    with pytest.raises(g.SchematicRefused) as exc:
        dh.die_handout(folders, out, um, name=name)
    assert not (out / g.LIB_TABLE).exists()
    return exc.value.refusals


@pytest.fixture
def out(tmp_path):
    return tmp_path / "sheet_first"


def test_one_symbol_per_cell_numbered_by_the_contract(out):
    driver = _folder(out / "dies" / "DRIVER", "DRIVER", DRIVER)
    echo = _folder(out / "dies" / "ECHO", "ECHO", ECHO)
    dh.die_handout([echo, driver], out, 80, name="sheet_first")
    symbols = _symbols(out / g.HANDOUT_LIB_FILE)
    assert [name for name, _s in symbols] == ["DRIVER", "ECHO"]
    symbol = symbols[0][1]
    pins = _pins(symbol)
    assert set(pins) == {"CLK", "OUT", "IO", "ANA", "VDD", "VSS"}
    assert {n: p["type"] for n, p in pins.items()} == {
        "CLK": "input", "OUT": "output", "IO": "bidirectional", "ANA": "passive",
        "VDD": "power_in", "VSS": "power_in"}
    assert all(pins[n]["angle"] == "0" and pins[n]["x"] < 0 for n in ("VDD", "VSS"))
    assert all(pins[n]["angle"] == "180" and pins[n]["x"] > 0
               for n in ("CLK", "OUT", "IO", "ANA"))
    assert pins["ANA"]["y"] > pins["CLK"]["y"] > pins["IO"]["y"] > pins["OUT"]["y"]
    assert _props(symbol) == {
        "Reference": "U", "Value": "DRIVER", "Footprint": "chiplet_dies:DRIVER",
        "Datasheet": "", "Description": "chiplet die", "Die": "dies/DRIVER",
        "Method": "", "Thickness_um": ""}


def test_a_signal_with_no_direction_is_passive(out):
    folder = _folder(out / "A", "A", [("X", "X", 0, "signal", None),
                                      ("Y", "Y", 0, "signal", "none")])
    dh.die_handout([folder], out, 80)
    pins = _pins(_symbols(out / g.HANDOUT_LIB_FILE)[0][1])
    assert pins["X"]["type"] == pins["Y"]["type"] == "passive"


def test_the_system_library_and_the_table(out):
    dh.die_handout([_folder(out / "ECHO", "ECHO", ECHO)], out, 80, name="sheet_first")
    symbols = _symbols(out / dh.SYSTEM_LIB_FILE)
    assert [name for name, _s in symbols] == ["GND", "SUPPLY", "EXT_PAD"]
    gnd, supply, pad = (s for _n, s in symbols)
    assert ["power"] in gnd and ["power"] in supply and ["power"] not in pad
    assert _props(gnd)["Value"] == "GND" and "Volts" not in _props(gnd)
    assert _props(supply)["Value"] == "SUPPLY" and _props(supply)["Volts"] == ""
    assert _props(gnd)["Reference"] == _props(supply)["Reference"] == "#PWR"
    assert _props(pad)["Reference"] == "J"
    assert _props(pad)["Footprint"] == "chiplet_carrier:EXT_PAD_80um"
    assert _props(pad)["Edge"] == ""
    assert {n: p["type"] for n, p in _pins(pad).items()} == {"1": "passive"}
    table = sx.parse((out / g.LIB_TABLE).read_text(encoding="utf-8"))
    libs = sx.find_all(table, "lib")
    assert [str(sx.find(lib, "uri")[1]) for lib in libs] == [
        "${KIPRJMOD}/chiplet_system.kicad_sym", "${KIPRJMOD}/chiplet_dies.kicad_sym"]
    sheet = sx.parse((out / "sheet_first.kicad_sch").read_text(encoding="utf-8"))
    assert str(sx.find(sx.find(sheet, "title_block"), "title")[1]) == "sheet_first"
    assert sx.find(sheet, "lib_symbols") == ["lib_symbols"]


def test_every_footprint_is_hidden(out):
    dh.die_handout([_folder(out / "ECHO", "ECHO", ECHO)], out, 80)
    for lib in (g.HANDOUT_LIB_FILE, dh.SYSTEM_LIB_FILE):
        for _name, symbol in _symbols(out / lib):
            node = next(p for p in sx.find_all(symbol, "property") if str(p[1]) == "Footprint")
            assert ["hide", "yes"] in node


def test_a_pad_on_no_port_is_refused(out):
    lone = _folder(out / "LONE", "LONE", ECHO + [("TP", None, None, "signal", None)])
    twice = _folder(out / "TWICE", "TWICE", ECHO + [("NC", None, None, "signal", None),
                                                    ("NC", None, None, "signal", None)])
    refusals = _refused(out, [lone, twice])
    assert [(r["code"], r["folder"]) for r in refusals] == [
        ("unported_pads", str(lone)), ("unported_pads", str(twice))]
    assert "'TP' serves no port" in refusals[0]["message"]


def _edit(folder, name, change):
    path = folder / name
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("name, change, said", [
    ("die-contract.json", lambda d: d["process"].update(pdk="example"), "maps to no technology"),
    ("die-contract.json", lambda d: d["die"].update(bbox_um=[0, 0, 0, 10]), "bbox_um"),
    ("die-contract.json", lambda d: d["die"].pop("top_cell"), "top_cell"),
    ("die-contract.json", lambda d: d.update(version="9.0"), "9.0"),
    ("pins.json", lambda d: d.update(dbu_um=0), "dbu_um"),
    ("pins.json", lambda d: d["pins"][1].pop("ordinal"), "ordinal"),
    ("pins.json", lambda d: d["pins"][5].update(ordinal=0), "repeats an ordinal"),
    ("pins.json", lambda d: d["pins"][5].update(direction="INPUT"), "is stated as"),
])
def test_a_folder_the_import_would_refuse_is_refused(out, name, change, said):
    folder = _folder(out / "A", "A", DRIVER)
    _edit(folder, name, change)
    refusals = _refused(out, [folder])
    assert [(r["code"], r["folder"]) for r in refusals] == [("die_folder", str(folder))]
    assert said in refusals[0]["message"]
    assert refusals[0]["message"].startswith(f"die folder {folder}: ")


def test_a_die_that_states_no_ports_is_refused(out):
    folder = _folder(out / "A", "A", [("X", None, None, "signal", "input")])
    refusals = _refused(out, [folder])
    assert [r["code"] for r in refusals] == ["unported_pads"]
    assert "the die states no ports" in refusals[0]["message"]


def test_a_contract_that_names_other_pads_is_refused(out):
    folder = _folder(out / "A", "A", ECHO, inventory=ECHO + [("X", "X", 0, "signal", "input")])
    refusals = _refused(out, [folder])
    assert [r["code"] for r in refusals] == ["contract_pads_differ"]


def test_two_folders_of_one_cell_are_refused(out):
    a = _folder(out / "a", "ECHO", ECHO)
    b = _folder(out / "b", "ECHO", ECHO)
    refusals = _refused(out, [a, b])
    assert [r["code"] for r in refusals] == ["top_cell_drawn_twice"]
    assert (refusals[0]["folder"], refusals[0]["other_folder"]) == (str(b), str(a))


def test_a_folder_outside_the_handout_is_refused(tmp_path, out):
    folder = _folder(tmp_path / "elsewhere" / "ECHO", "ECHO", ECHO)
    refusals = _refused(out, [folder])
    assert [(r["code"], r["folder"]) for r in refusals] == [("die_folder", str(folder))]
    assert "inside the handout directory" in refusals[0]["message"]


@pytest.mark.parametrize("rel, said", [("C:/ECHO", "absolute"), ("v${x}/ECHO", "'${'")])
def test_a_folder_the_path_rule_refuses_is_refused(out, rel, said):
    folder = _folder(out / rel, "ECHO", ECHO)
    refusals = _refused(out, [folder])
    assert [r["code"] for r in refusals] == ["die_folder"]
    assert said in refusals[0]["message"]
    assert "inside the handout" not in refusals[0]["message"]


def test_a_link_inside_the_handout_names_the_link(tmp_path, out):
    real = _folder(tmp_path / "elsewhere" / "ECHO", "ECHO", ECHO)
    out.mkdir(parents=True)
    (out / "ECHO").symlink_to(real, target_is_directory=True)
    dh.die_handout([out / "ECHO"], out, 80)
    (_name, symbol), = _symbols(out / g.HANDOUT_LIB_FILE)
    assert _props(symbol)["Die"] == "ECHO"


def test_one_folder_given_twice_is_named_as_such(out):
    folder = _folder(out / "ECHO", "ECHO", ECHO)
    refusals = _refused(out, [folder, str(folder) + "/"])
    assert [r["code"] for r in refusals] == ["die_folder_twice"]


def test_a_direction_that_is_no_word_is_refused(out):
    folder = _folder(out / "A", "A", DRIVER)
    _edit(folder, "die-contract.json",
          lambda d: d["terminals"][0].update(direction=["input"]))
    refusals = _refused(out, [folder])
    assert [r["code"] for r in refusals] == ["not_expressible"]
    assert "no word" in refusals[0]["message"]


def test_a_handout_path_that_cannot_be_read_is_refused(out):
    folder = _folder(out / "ECHO", "ECHO", ECHO)
    with pytest.raises(g.SchematicRefused) as exc:
        dh.die_handout([folder], str(out) + "\0", 80)
    assert [r["code"] for r in exc.value.refusals] == ["handout_dir"]


@pytest.mark.parametrize("cell", ["A B", "A:B", "via$1", "a/b", "A-B~"])
def test_a_cell_that_cannot_name_a_footprint_or_a_symbol_is_refused(out, cell):
    folder = _folder(out / "bad", cell, ECHO)
    refusals = _refused(out, [folder])
    assert [(r["code"], r["field"], r["value"]) for r in refusals] == [
        ("unmappable_name", "top_cell", cell)]


def test_a_pad_name_kicad_would_rewrite_is_refused(out):
    folder = _folder(out / "A", "A", [('A"', "A", 0, "signal", "input")])
    refusals = _refused(out, [folder])
    assert [(r["code"], r["field"]) for r in refusals] == [("unmappable_name", "terminal")]


@pytest.mark.parametrize("pad, port", [("IO_BOND_outputs[0].output_pad", "IO_BOND_outputs[0]"),
                                       ("x[03]", "x"), ("a[1].b[2]", "a[1]")])
def test_a_bus_pad_keeps_its_name(out, pad, port):
    pads = [(pad, port, 0, "signal", "output")]
    dh.die_handout([_folder(out / "A", "A", pads)], out, 80)
    assert set(_pins(_symbols(out / g.HANDOUT_LIB_FILE)[0][1])) == {pads[0][0]}


def test_a_pad_named_as_a_bus_range_is_refused(out):
    refusals = _refused(out, [_folder(out / "B", "B", [("D[0..3]", "D", 0, "signal", "input")])])
    assert [(r["code"], r["field"], r["value"]) for r in refusals] == [
        ("unmappable_name", "terminal", "D[0..3]")]


def test_an_unreadable_folder_is_a_refusal_not_a_traceback(out):
    missing = out / "gone"
    missing.mkdir(parents=True)
    latin = _folder(out / "latin", "LATIN", ECHO)
    (latin / "die-contract.json").write_bytes(b'{"die": "\xe9"}')
    deep = _folder(out / "deep", "DEEP", ECHO)
    (deep / "die-contract.json").write_text("[" * 100000 + "]" * 100000, encoding="utf-8")
    junk = _folder(out / "junk", "JUNK", ECHO)
    (junk / "die-contract.json").write_text("[]", encoding="utf-8")
    nul = str(out / "nul\0")
    refusals = _refused(out, [missing, latin, deep, junk, nul])
    assert [(r["code"], r["folder"]) for r in refusals] == [
        ("die_folder", str(f)) for f in (missing, latin, deep, junk, nul)]


@pytest.mark.parametrize("um", [True, False, 80.0, "80", 0, -1, None])
def test_a_bad_ext_pad_um_is_refused(out, um):
    refusals = _refused(out, [_folder(out / "ECHO", "ECHO", ECHO)], um=um)
    assert [r["code"] for r in refusals] == ["ext_pad_um"]


def test_reruns_are_byte_equal_and_a_drawn_sheet_is_kept(out):
    folder = _folder(out / "ECHO", "ECHO", ECHO)
    first = dh.die_handout([folder], out, 80)
    texts = {name: (out / name).read_bytes() for name in first}
    assert dh.die_handout([folder], out, 80) == first
    assert {name: (out / name).read_bytes() for name in first} == texts
    sheet = out / "sheet_first.kicad_sch"
    sheet.write_bytes(texts["sheet_first.kicad_sch"] + b"\n")
    refusals = [r["code"] for r in pytest.raises(g.SchematicRefused, dh.die_handout,
                                                 [folder], out, 80).value.refusals]
    assert refusals == ["handout_exists"]
    assert sheet.read_bytes() == texts["sheet_first.kicad_sch"] + b"\n"


def test_an_output_that_is_a_file_is_refused(tmp_path):
    target = tmp_path / "sheet_first"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(g.SchematicRefused) as exc:
        dh.die_handout([], target, 80, name="sheet_first")
    assert [r["code"] for r in exc.value.refusals] == ["handout_dir"]


def test_the_directory_name_is_the_default_project_name(out, monkeypatch):
    folder = _folder(out / "ECHO", "ECHO", ECHO)
    monkeypatch.chdir(out)
    dh.die_handout([Path("ECHO")], ".", 80)
    assert (out / "sheet_first.kicad_sch").is_file()
    assert _props(_symbols(out / g.HANDOUT_LIB_FILE)[0][1])["Die"] == "ECHO"
    assert folder.is_dir()


def test_cli_writes_and_refuses_the_flag_combinations(out, capsys):
    folder = _folder(out / "ECHO", "ECHO", ECHO)
    code = g.main(["--handout", str(out), "--dies", str(folder), "--ext-pad-um", "80"])
    assert code == g.EXIT_WRITTEN
    assert (out / dh.SYSTEM_LIB_FILE).is_file()
    for args in (["--ext-pad-um", "0"], ["--ext-pad-um", "-1"], ["--ext-pad-um", "80.0"],
                 ["--ext-pad-um", "abc"], ["--ext-pad-um", "080"], ["--ext-pad-um", " 80"],
                 ["--ext-pad-um", "8_0"], []):
        code = g.main(["--handout", str(out / "nope"), "--dies", str(folder), *args])
        err = capsys.readouterr().err
        assert code == g.EXIT_REFUSED
        assert "refused (ext_pad_um)" in err and "Traceback" not in err
    code = g.main(["--dies", str(folder), "--ext-pad-um", "80",
                   "--chiplet", str(out / "missing.chiplet")])
    err = capsys.readouterr().err
    assert code == g.EXIT_REFUSED
    assert "dies_with_chiplet" in err and "dies_without_handout" in err
    with pytest.raises(SystemExit):
        g.main(["--ext-pad-um", "80"])
    with pytest.raises(SystemExit):
        g.main(["--handout", str(out), "--dies", str(folder), "--ext-pad-um", "80",
                "--pins", "U1=pins.json"])


@pytest.mark.kicad_cli
def test_kicad_cli_loads_the_die_handout(out, tmp_path):
    """KiCad reads both libraries (one SVG per symbol) and the empty sheet
    (a netlist with no component)."""
    if shutil.which("kicad-cli") is None:
        if os.environ.get("GDS_TO_KICAD_REQUIRE_KICAD_CLI") == "1":
            pytest.fail("GDS_TO_KICAD_REQUIRE_KICAD_CLI=1 but kicad-cli is not on PATH")
        pytest.skip("kicad-cli not on PATH (GDS_TO_KICAD_REQUIRE_KICAD_CLI=1 requires it)")
    dh.die_handout([_folder(out / "DRIVER", "DRIVER", DRIVER),
                    _folder(out / "ECHO", "ECHO", ECHO)], out, 80)
    for lib, count in ((dh.SYSTEM_LIB_FILE, 3), (g.HANDOUT_LIB_FILE, 2)):
        svg = tmp_path / f"svg_{lib}"
        run = subprocess.run(["kicad-cli", "sym", "export", "svg", "--output", str(svg),
                              str(out / lib)], capture_output=True, timeout=300)
        assert run.returncode == 0, run.stderr
        assert len(list(svg.glob("*.svg"))) == count
    net = tmp_path / "blank.net"
    run = subprocess.run(["kicad-cli", "sch", "export", "netlist", "--format", "kicadsexpr",
                          "--output", str(net), str(out / "sheet_first.kicad_sch")],
                         capture_output=True, timeout=300)
    assert run.returncode == 0, run.stderr
    assert sx.find(sx.parse(net.read_text(encoding="utf-8")), "components") == ["components"]


def test_repeated_rail_pads_set_the_jumper_flag_and_others_do_not(out):
    driver = _folder(out / "dies" / "DRIVER", "DRIVER", DRIVER)
    echo = _folder(out / "dies" / "ECHO", "ECHO", ECHO)
    dh.die_handout([echo, driver], out, 80, name="sheet_first")
    flags = {name: sx.find(symbol, "duplicate_pin_numbers_are_jumpers")
             for name, symbol in _symbols(out / g.HANDOUT_LIB_FILE)}
    assert flags == {"DRIVER": ["duplicate_pin_numbers_are_jumpers", "yes"], "ECHO": None}


@pytest.mark.parametrize("role", ["no_connect", "signal"])
def test_a_repeat_that_is_no_rail_is_refused(out, role):
    pads = [("CLK", "CLK", 0, "signal", "input"),
            ("NC", "NC", 0, role, "input"), ("NC", "NC", 1, role, "input")]
    refusals = _refused(out, [_folder(out / "A", "A", pads)])
    assert [r["code"] for r in refusals] == ["undeclared_tie"]
    assert "pad NC repeats on 2 pads" in refusals[0]["message"]
