# SPDX-License-Identifier: GPL-3.0-or-later
"""A draft sheet pairs with the board the draft export drew.

The board is a draft-path export of padring_die_cmos5l (fixtures/PROVENANCE,
draft_pair/). The sheet is drawn here from the same .chiplet and pin lists.
KiCad's own schematic parity finds nothing between them, and finds every
footprint extra against the same sheet with its symbols taken out, so the check
compares content. A run that never read the sheet is refused by the line
kicad-cli prints only when it did (_parity). The parity cases are opt-in
(`pytest -m kicad_cli`).

Every pin of the pair states both a type and a role, so the die symbols, the
sheet, its connection sheet and the handout are the same bytes, with no pin
type warning, when the type is taken out of every pin.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import chiplet_to_kicad_sch as g
import sexpr_io as sx
from kicad_sym_writer import KiCadSymWriter
from pin_list import PinList
from symbol_layout import create_layout_from_pin_list
from tests import test_kicad_sch_to_connectivity as bt

PAIR = Path(__file__).resolve().parent / "fixtures" / "draft_pair"
DIES = ("U1", "U2", "U3")


def _pair(tmp_path, check=True):
    """The sheet drawn (and checked by KiCad's netlist), beside a copy of the board."""
    sch = tmp_path / "board.kicad_sch"
    argv = ["--chiplet", str(PAIR / "design.chiplet"), "--out", str(sch),
            "--report", str(tmp_path / "schematic.json"), *(["--check"] if check else [])]
    for die in DIES:
        argv += ["--pins", f"{die}={PAIR / f'{die}.pins.json'}"]
    assert g.main(argv) == g.EXIT_WRITTEN
    board = shutil.copy(PAIR / "board.kicad_pcb", tmp_path / "board.kicad_pcb")
    return sch, Path(board)


def _parity(board):
    out = board.with_name("drc.json")
    run = subprocess.run(["kicad-cli", "pcb", "drc", "--schematic-parity", "--format", "json",
                          "--output", str(out), str(board)], capture_output=True, timeout=300)
    assert run.returncode == 0, run.stderr
    report = json.loads(out.read_text(encoding="utf-8"))
    assert Path(report["source"]).name == board.name
    # A sheet kicad-cli cannot read still exits 0 with an empty list; only
    # this line says the parity check ran.
    issues = report["schematic_parity"]
    assert f"Found {len(issues)} schematic parity issues" in run.stdout.decode(), run.stdout
    return issues


def _references(path, tag):
    return sorted(str(p[2]) for c in sx.find_all(sx.parse(path.read_text(encoding="utf-8")), tag)
                  for p in sx.find_all(c, "property") if str(p[1]) == "Reference")


def test_the_draft_sheet_draws_a_symbol_for_each_footprint_of_the_board(tmp_path):
    sch, board = _pair(tmp_path, check=False)
    assert _references(sch, "symbol") == _references(board, "footprint")


@pytest.mark.kicad_cli
def test_kicad_cli_parity_of_the_draft_sheet_and_its_board_is_empty(tmp_path):
    bt._need_kicad_cli()
    _, board = _pair(tmp_path)
    assert _parity(board) == []


@pytest.mark.kicad_cli
def test_kicad_cli_parity_against_a_sheet_without_symbols_finds_every_footprint(tmp_path):
    bt._need_kicad_cli()
    sch, board = _pair(tmp_path)

    def drop_symbols(tree):
        tree[:] = [c for c in tree if sx.head(c) != "symbol"]
    bt._edit(sch, drop_symbols)
    footprints = sx.find_all(sx.parse(board.read_text(encoding="utf-8")), "footprint")
    issues = _parity(board)
    assert {i["type"] for i in issues} == {"extra_footprint"}
    assert len(issues) == len(footprints) == 35


def _outputs(run, untyped):
    """({file: bytes} of the die symbols, the sheet with its connection sheet
    and the handout; the pin type warnings said) for the pair's pin lists,
    each pin's type taken out when ``untyped``. Every path is the same both
    ways, so only the pin lists can make the bytes differ."""
    shutil.rmtree(run, ignore_errors=True)
    run.mkdir()
    pins, said = {}, []
    for die in DIES:
        doc = json.loads((PAIR / f"{die}.pins.json").read_text(encoding="utf-8"))
        assert all("type" in p and "role" in p for p in doc["pins"])
        if untyped:
            for pin in doc["pins"]:
                del pin["type"]
        pins[die] = str(run / f"{die}.pins.json")
        Path(pins[die]).write_text(json.dumps(doc, indent=2), encoding="utf-8")
        pl = PinList.load(pins[die])
        said += pl.validate()
        KiCadSymWriter().write_symbol_library(
            [create_layout_from_pin_list(pl, die)], str(run / "sym" / f"{die}.kicad_sym"))
    sheet = run / "sheet"
    g.generate(PAIR / "design.chiplet", pins, sheet / "board.kicad_sch",
               svg_path=sheet / "board.svg", on_warn=said.append)
    g.handout(PAIR / "design.chiplet", pins, run / "handout", on_warn=said.append)
    files = {str(p.relative_to(run)): p.read_bytes() for p in sorted(run.rglob("*"))
             if p.is_file() and not p.name.endswith(".pins.json")}
    return files, said


def test_the_pair_draws_the_same_bytes_with_the_type_taken_out(tmp_path):
    typed, said_typed = _outputs(tmp_path / "run", untyped=False)
    untyped, said_untyped = _outputs(tmp_path / "run", untyped=True)
    assert {Path(f).parent.name for f in typed} >= {"sym", "sheet", "handout"}
    assert said_typed == said_untyped == []
    assert typed == untyped
