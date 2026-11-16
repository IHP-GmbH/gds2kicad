# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for io_pads/generate_io_pad.py and io_pads/kicad_pcb_to_iopads.py."""
import itertools
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from _vendor.round_trip_names import (FEATURES, FEATURES_VERSION, REQUIRED_FEATURES,
                                      ROUND_TRIP_VERSIONS, round_trip_states)

PROJECT_DIR = Path(__file__).resolve().parents[1]
IOPADS_DIR = PROJECT_DIR / "io_pads"
GEN_SCRIPT = IOPADS_DIR / "generate_io_pad.py"
EXTRACT_SCRIPT = IOPADS_DIR / "kicad_pcb_to_iopads.py"
_OPTIONAL = [f for f in FEATURES if f not in REQUIRED_FEATURES]
#: Every round_trip shape the reader takes: each numbered version, and version 6
#: with each set of the features it may leave out. Each is a minimal round_trip
#: record and, since the keys match, also _report's arguments.
SHAPES = [{"version": v} for v in ROUND_TRIP_VERSIONS if v != FEATURES_VERSION] + [
    {"version": FEATURES_VERSION, "features": [*REQUIRED_FEATURES, *extra]}
    for n in range(len(_OPTIONAL) + 1) for extra in itertools.combinations(_OPTIONAL, n)]
SHEET_SHAPES = [s for s in SHAPES if round_trip_states(s).sheet]
NO_SHEET_SHAPES = [s for s in SHAPES if not round_trip_states(s).sheet]


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def test_generate_wire_bond_100x100(tmp_path):
    out_dir = tmp_path / "io_pads"
    res = _run([sys.executable, str(GEN_SCRIPT),
                "--io-class", "wire_bond", "--size", "100x100",
                "--out-dir", str(out_dir)])
    assert res.returncode == 0, res.stderr

    sym = out_dir / "kicad_symbols" / "IOPad_WireBond_100x100.kicad_sym"
    fp = out_dir / "kicad_footprints" / "IOPad_WireBond_100x100.kicad_mod"
    assert sym.exists()
    assert fp.exists()

    sym_text = sym.read_text()
    assert '(symbol "IOPad_WireBond_100x100"' in sym_text
    assert sym_text.count("(pin passive line") == 1

    fp_text = fp.read_text()
    assert '(property "IO_CLASS" "wire_bond")' in fp_text
    assert '(property "IO_PAD_SIZE_UM" "100x100")' in fp_text
    assert '(pad "1" smd rect (at 0 0)' in fp_text
    assert "(size 0.100000 0.100000)" in fp_text


def test_generate_wire_bond_square_shorthand(tmp_path):
    out_dir = tmp_path / "io_pads"
    res = _run([sys.executable, str(GEN_SCRIPT),
                "--io-class", "wire_bond", "--size", "150",
                "--out-dir", str(out_dir)])
    assert res.returncode == 0, res.stderr
    fp = out_dir / "kicad_footprints" / "IOPad_WireBond_150x150.kicad_mod"
    assert fp.exists()
    assert '(property "IO_PAD_SIZE_UM" "150x150")' in fp.read_text()


def test_generate_reserved_class_rejected(tmp_path):
    res = _run([sys.executable, str(GEN_SCRIPT),
                "--io-class", "flipped_bump", "--size", "80x80",
                "--out-dir", str(tmp_path)])
    assert res.returncode != 0
    assert "reserved" in res.stderr.lower()


def test_extract_io_pads_from_pcb_fixture(tmp_path):
    pcb = tmp_path / "mini.kicad_pcb"
    pcb.write_text("""
(kicad_pcb
  (footprint "io_pads:IOPad_WireBond_100x100"
    (layer "F.Cu")
    (at 5.0 -3.5 0)
    (property "Reference" "J1")
    (property "IO_CLASS" "wire_bond")
    (property "IO_PAD_SIZE_UM" "100x100")
    (fp_text reference "J1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.1 0.1) (layers "F.Cu" "F.Mask")
      (net 7 "VDD_EXT")
    )
  )
  (footprint "Connectors:RegularConnector"
    (layer "F.Cu")
    (at 1.0 1.0 0)
    (property "Reference" "X1")
    (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu"))
  )
  (footprint "io_pads:IOPad_WireBond_150x150"
    (layer "F.Cu")
    (at -2.0 4.0 0)
    (fp_text reference "J2" (at 0 0) (layer "F.SilkS"))
    (property "IO_CLASS" "wire_bond")
    (property "IO_PAD_SIZE_UM" "150x150")
    (pad "1" smd rect (at 0 0) (size 0.15 0.15) (layers "F.Cu" "F.Mask")
      (net 8 "OUT0")
    )
  )
)
""")
    out = tmp_path / "io_pads.json"
    res = _run([sys.executable, str(EXTRACT_SCRIPT), str(pcb), "-o", str(out)])
    assert res.returncode == 0, res.stderr

    data = json.loads(out.read_text())
    assert {k: v for k, v in data.items() if k != "io_pads"} == {
        "schema": "chiplet-io-pad-manifest",
        "version": "1.0.0",
        "frame": "board_absolute_ynegated_um",
        "generator": "gds_to_kicad kicad_pcb_to_iopads.py",
        "board": "mini.kicad_pcb",
    }
    pads = data["io_pads"]
    assert len(pads) == 2

    by_ref = {p["ref"]: p for p in pads}
    j1 = by_ref["J1"]
    assert j1["io_class"] == "wire_bond"
    assert j1["x_um"] == 5000.0
    assert j1["y_um"] == 3500.0  # KiCad Y-down -3.5 mm -> GDS Y-up +3500 um
    assert j1["size_x_um"] == 100.0
    assert j1["size_y_um"] == 100.0
    assert j1["net"] == "VDD_EXT"
    assert j1["layer"] == "TopMetal2"
    assert j1["source_layer"] == "F.Cu"

    j2 = by_ref["J2"]
    assert j2["x_um"] == -2000.0
    assert j2["y_um"] == -4000.0
    assert j2["size_x_um"] == 150.0
    assert j2["net"] == "OUT0"


def test_extract_skips_pcb_without_io_pads(tmp_path):
    pcb = tmp_path / "no_io.kicad_pcb"
    pcb.write_text('(kicad_pcb (footprint "x:y" (layer "F.Cu") (at 0 0 0)))')
    out = tmp_path / "io_pads.json"
    res = _run([sys.executable, str(EXTRACT_SCRIPT), str(pcb), "-o", str(out)])
    assert res.returncode == 0
    data = json.loads(out.read_text())
    assert data["io_pads"] == []
    assert (data["schema"], data["frame"]) == ("chiplet-io-pad-manifest",
                                              "board_absolute_ynegated_um")


def _pad_footprint(io_class="wire_bond", size="80x80", pad=True, at="1.0 0"):
    """One carrier J1 with the given IO_CLASS, IO_PAD_SIZE_UM (None for none)
    and, when ``pad``, one 50 x 60 um pad."""
    size_prop = f'(property "IO_PAD_SIZE_UM" "{size}")' if size is not None else ""
    pad_line = '(pad "1" smd rect (at 0 0) (size 0.05 0.06) (layers "F.Cu"))' if pad else ""
    return f"""
  (footprint "io_pads:Pad"
    (layer "F.Cu")
    (at {at} 0)
    (property "Reference" "J1")
    (property "IO_CLASS" "{io_class}")
    {size_prop}
    {pad_line}
  )"""


def test_an_unknown_io_class_is_refused_and_nothing_written(tmp_path):
    res, pads = _extract(tmp_path, _pad_footprint(io_class="wirebond"))
    assert res.returncode == 1
    assert "carrier J1: IO_CLASS 'wirebond' is not one of wire_bond" in res.stderr
    assert not (tmp_path / "io_pads.json").exists()


@pytest.mark.parametrize("size", [None, "0x80", "nanx80", "abc"])
def test_a_carrier_with_no_size_is_refused_not_dropped(tmp_path, size):
    res, pads = _extract(tmp_path, _pad_footprint(size=size, pad=False))
    assert res.returncode == 1
    assert "carrier J1: no pad size" in res.stderr
    assert not (tmp_path / "io_pads.json").exists()


@pytest.mark.parametrize("size", ["0x80", "80", "80X80", "80x80x80", "abc"])
def test_a_size_property_the_plugin_ignores_falls_back_to_the_pad(tmp_path, size):
    """Only "<x>x<y>" with both sides above zero is read, as the plugin's
    io_pad_size_um reads it; anything else takes the first pad's size."""
    res, pads = _extract(tmp_path, _pad_footprint(size=size))
    assert res.returncode == 0, res.stderr
    assert (pads[0]["size_x_um"], pads[0]["size_y_um"]) == (50.0, 60.0)


@pytest.mark.parametrize("io_class", ["wire_bond", "flipped_bump", "tsv_bump"])
def test_each_schema_io_class_is_written(tmp_path, io_class):
    res, pads = _extract(tmp_path, _pad_footprint(io_class=io_class))
    assert res.returncode == 0, res.stderr
    assert pads[0]["io_class"] == io_class


def _unreferenced(footprint):
    return footprint.replace('(property "Reference" "J1")', '(property "Reference" "")')


def test_a_carrier_with_no_reference_is_refused_by_its_kicad_position(tmp_path):
    res, _ = _extract(tmp_path, _unreferenced(_pad_footprint(at="1.0 2.5")))
    assert res.returncode == 1
    assert "the carrier at (1.0, 2.5) mm has no Reference" in res.stderr
    assert not (tmp_path / "io_pads.json").exists()


def test_a_carrier_with_no_reference_is_named_by_position_in_any_refusal(tmp_path):
    res, _ = _extract(tmp_path, _unreferenced(_pad_footprint(io_class="wirebond", at="1.0 2.5")))
    assert res.returncode == 1
    assert "carrier at (1.0, 2.5) mm: IO_CLASS 'wirebond'" in res.stderr


def test_two_carriers_sharing_a_reference_are_refused_by_position(tmp_path):
    res, _ = _extract(tmp_path, _pad_footprint(), _pad_footprint(at="2.0 0"),
                      _carrier("J2", 3.0, lib="io_pads:Pad"))
    assert res.returncode == 1
    assert ("the carriers at (1.0, 0) mm and (2.0, 0) mm share the reference 'J1'"
            in res.stderr)
    assert "'J2'" not in res.stderr
    assert not (tmp_path / "io_pads.json").exists()


def test_a_shared_reference_is_refused_before_a_carrier_net(tmp_path):
    res, _ = _extract(tmp_path, _carrier("J1", 1.0, lib="io_pads:Pad", nets=["VDD", "VSS"]),
                      _carrier("J1", 2.0, lib="io_pads:Pad"))
    assert res.returncode == 1
    assert "share the reference 'J1'" in res.stderr
    assert "different nets" not in res.stderr


def test_export_carriers_sharing_a_reference_keep_their_io_pad_ids(tmp_path):
    """A board drawn by export names carriers by IO_PAD_ID, so a Reference two
    of them share (an unannotated draft) is not refused here."""
    res, pads = _extract(tmp_path, _carrier("J1", 1.0, io_pad_id="A", linked=False),
                         _carrier("J1", 2.0, io_pad_id="B", linked=False))
    assert res.returncode == 0, res.stderr
    assert [p["ref"] for p in pads] == ["A", "B"]


def test_the_io_classes_are_the_reference_reader_s():
    """The extractor keeps its own copy, to stay standard-library only."""
    sys.path.insert(0, str(IOPADS_DIR))
    try:
        import generate_io_pad
        import kicad_pcb_to_iopads
    finally:
        sys.path.remove(str(IOPADS_DIR))
    from _vendor import chiplet_format_io as cfio
    assert kicad_pcb_to_iopads.IO_CLASSES == tuple(cfio.IO_CLASS_INTERFACE_TYPES)
    assert (generate_io_pad.SUPPORTED_IO_CLASSES | generate_io_pad.RESERVED_IO_CLASSES
            == set(cfio.IO_CLASS_INTERFACE_TYPES))


def test_an_empty_io_class_is_not_a_carrier(tmp_path):
    res, pads = _extract(tmp_path, _pad_footprint(io_class=""))
    assert res.returncode == 0, res.stderr
    assert pads == []


def test_positions_and_sizes_are_written_to_the_nanometer(tmp_path):
    res, pads = _extract(tmp_path, _pad_footprint(size="80.00004x79.9996", at="1.005 -0.0000001"))
    assert res.returncode == 0, res.stderr
    pad = pads[0]
    assert (pad["x_um"], pad["y_um"]) == (1005.0, 0.0)
    assert (pad["size_x_um"], pad["size_y_um"]) == (80.0, 80.0)


def _carrier(ref, x, lib="chiplet_carrier:EXT_PAD_80um", io_pad_id=None, nets=None,
             linked=None):
    """A carrier footprint with one pad per entry of ``nets`` ('' for an
    unnetted pad); one pad on N_<ref> by default. ``linked`` gives it a
    symbol ``path``; by default a carrier with an ``io_pad_id`` whose
    reference differs from it (a sheet designator) is linked."""
    ident = f'(property "IO_PAD_ID" "{io_pad_id}" (at 0 0 0) (hide yes))' if io_pad_id else ""
    if linked is None:
        linked = bool(io_pad_id) and io_pad_id != ref
    if linked:
        ident += f' (path "/{ref}-{x}")'

    def pad(number, net):
        netted = f' (net 1 "{net}")' if net else ""
        return f'\n    (pad "{number}" smd rect (at 0 0) (size 0.08 0.08) (layers "F.Cu"){netted})'

    pad_lines = "".join(pad(n, net) for n, net in
                        enumerate([f"N_{ref}"] if nets is None else nets, 1))
    return f"""
  (footprint "{lib}"
    (layer "F.Cu")
    (at {x} 0 0)
    (property "Reference" "{ref}")
    {ident}
    (property "IO_CLASS" "wire_bond")
    (property "IO_PAD_SIZE_UM" "80x80"){pad_lines}
  )"""


def _pad_record(io_pad_id):
    """A carrier pad's record in an export report, stating ``io_pad_id``."""
    return {"properties": {"io_pad_id": io_pad_id}} if io_pad_id else {}


def _property(footprint, name):
    """The value of the ``name`` property of ``footprint``, ``""`` for none."""
    found = re.search(rf'\(property "{name}" "([^"]*)"', footprint)
    return found.group(1) if found else ""


def _report(version=3, features=None, net_names=None, sheet_nets=None, reserved=None,
            io_pads=None, **edit):
    """An export report whose round_trip names the given nets, states the given
    reserved nets (each one array of one via) when it carries reserved copper,
    and has an empty ``widths`` block when it states widths. ``features`` is
    version 6's list. ``io_pads`` are the board references of its carrier pads,
    each recorded with no IO_PAD_ID; None leaves them for :func:`_extract` to
    fill with the board's own. A field the record cannot carry is a mistake in
    the test."""
    round_trip = {"version": version}
    if io_pads is not None:
        round_trip["io_pads"] = {ref: _pad_record("") for ref in io_pads}
    if features is not None:
        round_trip["features"] = features
    states = round_trip_states(round_trip)
    assert states.sheet or not (net_names or sheet_nets), round_trip
    assert states.reserved or reserved is None, round_trip
    if states.sheet:
        round_trip.update(net_names=net_names or {}, sheet_nets=sheet_nets or {})
    if states.widths:
        round_trip["widths"] = {}
    if states.reserved:
        tile = {"at_mm": [0, 0], "size_mm": 0.1, "drill_mm": 0.05, "layers": ["F.Cu", "B.Cu"]}
        round_trip["reserved"] = {
            net: {"owner": "pdn", "copper_sha256": "0" * 64,
                  "arrays": {"A1": {"cuts": 1, "tiles": [tile]}}}
            for net in (["N_RES"] if reserved is None else reserved)}
    return {"format": "interposer_pnr.export", "version": 1, "round_trip": round_trip, **edit}


def _extract(tmp_path, *footprints, report=None):
    pcb = tmp_path / "sheet.kicad_pcb"
    pcb.write_text("(kicad_pcb" + "".join(footprints) + ")\n")
    out = tmp_path / "io_pads.json"
    cmd = [sys.executable, str(EXTRACT_SCRIPT), str(pcb), "-o", str(out)]
    if report is not None:
        pads = {_property(f, "Reference"): _pad_record(_property(f, "IO_PAD_ID"))
                for f in footprints if _property(f, "Reference")}
        round_trip = report.get("round_trip")
        if isinstance(round_trip, dict) and "io_pads" not in round_trip and pads:
            report = {**report, "round_trip": {**round_trip, "io_pads": pads}}
        (tmp_path / "export.json").write_text(json.dumps(report))
        cmd += ["--export-report", str(tmp_path / "export.json")]
    res = _run(cmd)
    return res, (json.loads(out.read_text())["io_pads"] if res.returncode == 0 else None)


def test_a_sheet_drawn_board_names_carriers_by_io_pad_id(tmp_path):
    res, pads = _extract(tmp_path, _carrier("J1", 1.0, io_pad_id="VDD_EXT"),
                         _carrier("J2", 2.0, io_pad_id="OUT0"),
                         report=_report(net_names={"VDD": "N_J1", "OUT": "N_J2"}))
    assert res.returncode == 0, res.stderr
    assert [(p["ref"], p["net"]) for p in pads] == [("VDD_EXT", "VDD"), ("OUT0", "OUT")]


def test_a_carrier_library_footprint_without_io_pad_id_is_refused(tmp_path):
    res, _ = _extract(tmp_path, _carrier("J1", 1.0))
    assert res.returncode == 1
    assert "carrier pad J1: no IO_PAD_ID" in res.stderr


def test_one_carrier_with_io_pad_id_makes_the_others_need_it(tmp_path):
    res, _ = _extract(tmp_path,
                      _carrier("J1", 1.0, lib="io_pads:Pad", io_pad_id="A", linked=False),
                      _carrier("J2", 2.0, lib="io_pads:Pad"))
    assert res.returncode == 1 and "carrier pad J2: no IO_PAD_ID" in res.stderr


def test_two_carriers_with_one_io_pad_id_are_refused(tmp_path):
    res, _ = _extract(tmp_path, _carrier("J1", 1.0, io_pad_id="A", linked=False),
                      _carrier("J2", 2.0, io_pad_id="A", linked=False))
    assert res.returncode == 1 and "J1 and J2 both carry IO_PAD_ID A" in res.stderr


def test_two_sheet_carriers_of_one_reference_are_refused_by_the_report(tmp_path):
    """The report keys each carrier pad by its Reference, so it can state one
    io_pad id for J1: a board with two is not the board it was written for."""
    res, _ = _extract(tmp_path, _carrier("J1", 1.0, io_pad_id="A"),
                      _carrier("J1", 2.0, io_pad_id="B"),
                      report=_report(net_names={"P": "N_J1"}))
    assert res.returncode == 1 and "written for another board" in res.stderr


def test_a_hand_drawn_board_keeps_references_as_ids(tmp_path):
    res, pads = _extract(tmp_path, _carrier("VDD_EXT", 1.0, lib="io_pads:Pad"))
    assert res.returncode == 0 and [p["ref"] for p in pads] == ["VDD_EXT"]


def test_a_hand_drawn_board_linked_to_its_own_sheet_needs_no_report(tmp_path):
    """Any board updated from a schematic links its footprints; one export did
    not draw names its nets itself."""
    res, pads = _extract(tmp_path, _carrier("VDD_EXT", 1.0, lib="io_pads:Pad", linked=True))
    assert res.returncode == 0, res.stderr
    assert [(p["ref"], p["net"]) for p in pads] == [("VDD_EXT", "N_VDD_EXT")]


def test_a_sheet_drawn_board_needs_its_export_report(tmp_path):
    res, _ = _extract(tmp_path, _carrier("J1", 1.0, io_pad_id="A"))
    assert res.returncode == 1
    assert "carrier pad J1 is linked to a sheet symbol" in res.stderr
    assert "--export-report" in res.stderr


def test_a_draft_board_reads_without_a_report(tmp_path):
    """export's draft path: chiplet_carrier pads, IO_PAD_ID the Reference, no
    sheet symbol linked, and the planner's nets already."""
    res, pads = _extract(tmp_path, _carrier("VDD_EXT", 1.0, io_pad_id="VDD_EXT"),
                         _carrier("OUT0", 2.0, io_pad_id="OUT0"))
    assert res.returncode == 0, res.stderr
    assert [(p["ref"], p["net"]) for p in pads] == [("VDD_EXT", "N_VDD_EXT"), ("OUT0", "N_OUT0")]


def test_a_draft_board_updated_from_its_sheet_reads_with_its_report(tmp_path):
    """KiCad links a draft board's pads to the draft sheet's symbols once it is
    updated from it; the draft's own report says its nets are the planner's."""
    board = (_carrier("VDD_EXT", 1.0, io_pad_id="VDD_EXT", linked=True),
             _carrier("OUT0", 2.0, io_pad_id="OUT0", linked=True))
    res, _ = _extract(tmp_path, *board)
    assert res.returncode == 1 and "--export-report" in res.stderr
    res, pads = _extract(tmp_path, *board, report=_report(version=2))
    assert res.returncode == 0, res.stderr
    assert [(p["ref"], p["net"]) for p in pads] == [("VDD_EXT", "N_VDD_EXT"), ("OUT0", "N_OUT0")]


def test_a_sheet_board_carrier_without_io_pad_id_is_refused_by_reference(tmp_path):
    res, _ = _extract(tmp_path, _carrier("J1", 1.0, io_pad_id="A"),
                      _carrier("J2", 2.0, lib="io_pads:Pad", linked=True),
                      report=_report(net_names={"P": "N_J1", "Q": "N_J2"}))
    assert res.returncode == 1 and "carrier pad J2: no IO_PAD_ID" in res.stderr


def test_a_report_of_another_board_is_refused(tmp_path):
    res, _ = _extract(tmp_path, _carrier("J1", 1.0, io_pad_id="A"),
                      report=_report(net_names={"P": "N_J1"}, io_pads=["J1", "J9"]))
    assert res.returncode == 1
    assert "was written for another board" in res.stderr and "J9" in res.stderr


def test_a_sheet_net_no_routed_net_binds_is_unbound(tmp_path):
    unnetted = _carrier("J3", 3.0, io_pad_id="C", nets=[""])
    res, pads = _extract(tmp_path, _carrier("J1", 1.0, io_pad_id="A"),
                         _carrier("J2", 2.0, io_pad_id="B"), unnetted,
                         report=_report(net_names={"P": "N_J1"},
                                        sheet_nets={"N_J2": ["J2", "1"]}))
    assert res.returncode == 0, res.stderr
    assert [p["net"] for p in pads] == ["P", "", ""]


def test_a_carrier_whose_pads_carry_one_net_reads_that_net(tmp_path):
    res, pads = _extract(tmp_path, _carrier("VDD_EXT", 1.0, lib="io_pads:Pad", nets=["VDD", "VDD"]))
    assert res.returncode == 0, res.stderr
    assert [p["net"] for p in pads] == ["VDD"]


def test_a_carrier_whose_first_pad_is_unnetted_is_refused(tmp_path):
    res, pads = _extract(tmp_path, _carrier("J1", 1.0, io_pad_id="A", nets=["", "N_J1"]),
                         report=_report(net_names={"P": "N_J1"}))
    assert res.returncode == 1 and pads is None
    assert ("carrier J1 (io_pad A): its pads carry different nets ('', 'N_J1'); "
            "a carrier's pads carry one net") in res.stderr


def test_a_carrier_whose_pads_carry_two_nets_is_refused(tmp_path):
    res, pads = _extract(tmp_path, _carrier("VDD_EXT", 1.0, lib="io_pads:Pad", nets=["VDD", "VSS"]))
    assert res.returncode == 1 and pads is None
    assert "carrier VDD_EXT: its pads carry different nets ('VDD', 'VSS')" in res.stderr


def test_a_carrier_named_by_its_own_io_pad_id_is_refused_by_reference_alone(tmp_path):
    res, _ = _extract(tmp_path, _carrier("A", 1.0, io_pad_id="A", nets=["N_A", ""], linked=True),
                      report=_report(net_names={"P": "N_A"}))
    assert res.returncode == 1
    assert "carrier A: its pads carry different nets ('', 'N_A')" in res.stderr


def test_a_carrier_with_no_pad_has_no_net(tmp_path):
    res, pads = _extract(tmp_path, _carrier("VDD_EXT", 1.0, lib="io_pads:Pad", nets=[]))
    assert res.returncode == 0, res.stderr
    assert [p["net"] for p in pads] == [""]


def test_a_missing_io_pad_id_is_refused_before_a_carrier_on_two_nets(tmp_path):
    res, _ = _extract(tmp_path, _carrier("J1", 1.0, io_pad_id="A"),
                      _carrier("J2", 2.0, nets=["N_J2", ""]), report=_report())
    assert res.returncode == 1
    assert "no IO_PAD_ID" in res.stderr


def test_a_missing_report_is_refused_before_a_carrier_on_two_nets(tmp_path):
    res, _ = _extract(tmp_path, _carrier("J1", 1.0, io_pad_id="A", nets=["N_J1", ""]))
    assert res.returncode == 1
    assert "linked to a sheet symbol" in res.stderr and "--export-report" in res.stderr


def test_a_net_the_report_does_not_name_is_refused(tmp_path):
    res, _ = _extract(tmp_path, _carrier("J1", 1.0, io_pad_id="A"),
                      _carrier("J2", 2.0, io_pad_id="B"), report=_report(net_names={"P": "N_J1"}))
    assert res.returncode == 1
    assert "carrier J2 (io_pad B): net N_J2 is neither a routed net nor a sheet net" in res.stderr


def test_a_v3_report_with_no_routed_net_still_refuses_an_unknown_net(tmp_path):
    res, _ = _extract(tmp_path, _carrier("J1", 1.0, io_pad_id="A"), report=_report())
    assert res.returncode == 1
    assert "carrier J1 (io_pad A): net N_J1 is neither a routed net nor a sheet net" in res.stderr


def test_the_shapes_follow_a_sheet_where_the_format_says():
    """The buckets the tests below loop over come from round_trip_states; this
    states them by hand, so a reader that misread a shape cannot move it."""
    def key(shape):
        return shape["version"], tuple(f for f in shape.get("features", ()) if f in _OPTIONAL)
    assert [key(s) for s in SHEET_SHAPES] == [
        (3, ()), (5, ()), (6, ("sheet",)), (6, ("sheet", "reserved"))]
    assert [key(s) for s in NO_SHEET_SHAPES] == [
        (1, ()), (2, ()), (4, ()), (6, ()), (6, ("reserved",))]


def test_a_report_that_follows_no_sheet_changes_no_byte_of_a_hand_drawn_board(tmp_path):
    pad = _carrier("VDD_EXT", 1.0, lib="io_pads:Pad")
    _extract(tmp_path, pad)
    alone = (tmp_path / "io_pads.json").read_bytes()
    for shape in NO_SHEET_SHAPES:
        res, _ = _extract(tmp_path, pad, report=_report(**shape))
        assert res.returncode == 0, (shape, res.stderr)
        assert (tmp_path / "io_pads.json").read_bytes() == alone, shape


def test_a_sheet_board_with_reserved_copper_names_its_carriers_too(tmp_path):
    # Reserved copper on a net the sheet names as well, numbered and by feature.
    for shape in [s for s in SHEET_SHAPES if round_trip_states(s).reserved]:
        res, pads = _extract(tmp_path, _carrier("J1", 1.0, io_pad_id="VDD_EXT"),
                             _carrier("J2", 2.0, io_pad_id="OUT0"),
                             report=_report(**shape, net_names={"VDD": "N_J1", "OUT": "N_J2"},
                                            reserved=["N_J1"]))
        assert res.returncode == 0, (shape, res.stderr)
        assert [(p["ref"], p["net"]) for p in pads] == [("VDD_EXT", "VDD"), ("OUT0", "OUT")], shape


def test_a_feature_the_reader_does_not_know_changes_no_name(tmp_path):
    """A version 6 feature only adds a block, so the carriers read the same
    with an unknown one listed, block and all."""
    features = [*REQUIRED_FEATURES, "sheet"]
    board = (_carrier("J1", 1.0, io_pad_id="VDD_EXT"), _carrier("J2", 2.0, io_pad_id="OUT0"))
    names = {"VDD": "N_J1", "OUT": "N_J2"}
    res, _ = _extract(tmp_path, *board, report=_report(
        FEATURES_VERSION, features=features, net_names=names))
    assert res.returncode == 0, res.stderr
    known = (tmp_path / "io_pads.json").read_bytes()
    unknown = _report(FEATURES_VERSION, features=[*features, "zz"], net_names=names)
    unknown["round_trip"]["zz"] = {"a": 1}   # by hand: _report builds only known blocks
    res, _ = _extract(tmp_path, *board, report=unknown)
    assert res.returncode == 0, res.stderr
    assert (tmp_path / "io_pads.json").read_bytes() == known


def test_the_report_must_describe_the_board(tmp_path):
    # The reader words these refusals; this checks only that they are made.
    for shape in NO_SHEET_SHAPES:
        res, _ = _extract(tmp_path, _carrier("J1", 1.0, io_pad_id="A"),
                          report=_report(**shape, io_pads=["A"]))
        assert res.returncode == 1, shape
        assert "written for another board" in res.stderr, shape
    for shape in SHEET_SHAPES:
        res, _ = _extract(tmp_path, _carrier("VDD_EXT", 1.0, lib="io_pads:Pad"),
                          report=_report(**shape))
        assert res.returncode == 1, shape
        assert "(VDD_EXT is not)" in res.stderr, shape


def test_a_report_the_reader_refuses_is_refused(tmp_path):
    pad = _carrier("J1", 1.0, io_pad_id="A")
    for report, sentence in [
            (_report(format="other"), "not an interposer_pnr.export report"),
            ({**_report(), "version": 2}, "not an interposer_pnr.export report"),
            (_report(net_names={"P": "N_J1", "Q": "N_J1"}), "maps both 'P' and 'Q'"),
            # Built by hand: _report takes only records the reader knows.
            ({**_report(), "round_trip": {"version": max(ROUND_TRIP_VERSIONS) + 1}},
             "this reader predates it"),
            (_report(version=5, reserved=[]), "states no reserved net")]:
        res, _ = _extract(tmp_path, pad, report=report)
        assert res.returncode == 1 and sentence in res.stderr, res.stderr
    pcb = tmp_path / "sheet.kicad_pcb"
    for path, sentence in [(tmp_path / "missing.json", "No such file"),
                           (pcb, "Expecting value")]:
        res = _run([sys.executable, str(EXTRACT_SCRIPT), str(pcb), "--export-report", str(path)])
        assert res.returncode == 1 and f"export report {path}: " in res.stderr, res.stderr
        assert sentence in res.stderr


def test_a_real_form_export_reads_back_the_planner_io_pads(tmp_path):
    """The board and report interposer-pnr export --form wrote (fixtures/PROVENANCE)
    give the ids and nets of the io_pads the planner's file states."""
    fixture = PROJECT_DIR / "tests" / "fixtures" / "form_export"
    out = tmp_path / "io_pads.json"
    res = _run([sys.executable, str(EXTRACT_SCRIPT), str(fixture / "draft.kicad_pcb"), "-o",
                str(out), "--export-report", str(fixture / "export.json")])
    assert res.returncode == 0, res.stderr
    chiplet = yaml.safe_load((fixture / "io_pads.chiplet").read_text())
    planner = [[pad["id"], pad["net"]] for c in chiplet["components"] for pad in c.get("io_pads", [])]
    written = json.loads(out.read_text())["io_pads"]
    assert planner and [[p["ref"], p["net"]] for p in written] == planner
