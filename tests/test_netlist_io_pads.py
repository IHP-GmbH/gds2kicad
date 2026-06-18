# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the I/O pad detection added to kicad_netlist_to_chiplet.py.

Two layers:
- Direct tests of `parse_kicad_netlist` and `discover_io_pad_refs` against
  hand-built `.net` fixtures.
- End-to-end (smoke) test that combines `kicad_pcb_to_iopads.py` (PR1)
  with `kicad_netlist_to_chiplet.py` (this PR) on parallel KiCad PCB +
  netlist fixtures, asserting both halves agree on the same set of
  external nets.
"""
import json
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from kicad_netlist_to_chiplet import (  # noqa: E402
    discover_io_pad_refs,
    parse_all_sexpr,
    parse_kicad_netlist,
    nets_to_yaml,
)

IOPADS_DIR = PROJECT_DIR / "io_pads"
EXTRACT_SCRIPT = IOPADS_DIR / "kicad_pcb_to_iopads.py"
NETLIST_SCRIPT = PROJECT_DIR / "kicad_netlist_to_chiplet.py"


def _write_minimal_net(path: Path, refs_with_libs: dict, nets: list) -> None:
    """Build a minimal KiCad-style .net file."""
    parts = ['(export (version "E")', '  (components']
    for ref, (lib, value) in refs_with_libs.items():
        parts.append(
            f'    (comp (ref "{ref}") (value "{value}") '
            f'(footprint "{lib}:{value}"))'
        )
    parts.append("  )")
    parts.append("  (nets")
    for net_idx, (net_name, conns) in enumerate(nets, start=1):
        parts.append(f'    (net (code "{net_idx}") (name "{net_name}")')
        for ref, pin in conns:
            parts.append(f'      (node (ref "{ref}") (pin "{pin}") '
                         f'(pintype "passive"))')
        parts.append("    )")
    parts.append("  )")
    parts.append(")")
    path.write_text("\n".join(parts))


def test_discover_io_pad_refs_by_library(tmp_path):
    netfile = tmp_path / "n.net"
    _write_minimal_net(
        netfile,
        {
            "U1": ("chiplets", "MyDie"),
            "J1": ("io_pads", "IOPad_WireBond_100x100"),
            "J2": ("io_pads", "IOPad_WireBond_100x100"),
        },
        nets=[("VDD", [("U1", "VDD"), ("J1", "1")])],
    )
    tree = parse_all_sexpr(netfile.read_text())
    refs = discover_io_pad_refs(tree)
    assert refs == {"J1", "J2"}


def test_discover_io_pad_refs_by_extra_prefix(tmp_path):
    netfile = tmp_path / "n.net"
    _write_minimal_net(
        netfile,
        {
            "U1": ("chiplets", "MyDie"),
            "J1": ("Connector", "Generic"),
        },
        nets=[("VDD", [("U1", "VDD"), ("J1", "1")])],
    )
    tree = parse_all_sexpr(netfile.read_text())
    assert discover_io_pad_refs(tree) == set()
    assert discover_io_pad_refs(tree, extra_ref_prefixes=("J",)) == {"J1"}


def test_parse_marks_external_nets(tmp_path):
    netfile = tmp_path / "n.net"
    _write_minimal_net(
        netfile,
        {
            "U1": ("chiplets", "MyDie"),
            "J1": ("io_pads", "IOPad_WireBond_100x100"),
            "J2": ("io_pads", "IOPad_WireBond_100x100"),
        },
        nets=[
            ("VDD_EXT", [("U1", "VDD"), ("J1", "1")]),
            ("OUT0",     [("U1", "OUT"), ("J2", "1")]),
            ("INTERNAL", [("U1", "A"),   ("U1", "B")]),
        ],
    )
    nets = parse_kicad_netlist(str(netfile), skip_unconnected=True)
    by_name = {n.name: n for n in nets}
    assert by_name["VDD_EXT"].external is True
    assert by_name["OUT0"].external is True
    assert by_name["INTERNAL"].external is False


def test_yaml_output_contains_external_flag(tmp_path):
    netfile = tmp_path / "n.net"
    _write_minimal_net(
        netfile,
        {"U1": ("chiplets", "MyDie"),
         "J1": ("io_pads", "IOPad_WireBond_100x100")},
        nets=[("VDD_EXT", [("U1", "VDD"), ("J1", "1")])],
    )
    nets = parse_kicad_netlist(str(netfile), skip_unconnected=True)
    yaml_text = nets_to_yaml(nets)
    assert "external: true" in yaml_text


def test_cli_io_pad_lib_override(tmp_path):
    netfile = tmp_path / "n.net"
    _write_minimal_net(
        netfile,
        {"U1": ("chiplets", "MyDie"),
         "J1": ("custom_pads", "MyIOPad")},
        nets=[("VDD_EXT", [("U1", "VDD"), ("J1", "1")])],
    )
    out_yaml = tmp_path / "out.yaml"
    res = subprocess.run(
        [sys.executable, str(NETLIST_SCRIPT), str(netfile),
         "--io-pad-lib", "custom_pads",
         "--yaml", str(out_yaml)],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    assert "external: true" in out_yaml.read_text()


def test_smoke_pcb_and_netlist_agree_on_external_set(tmp_path):
    """End-to-end: kicad_pcb_to_iopads JSON and kicad_netlist_to_chiplet YAML
    must reference the same set of refs/nets.

    We synthesize parallel .kicad_pcb and .net fixtures with three io_pads
    (J1..J3) plus a non-IO connector (X1). The JSON should list 3 pads and
    the YAML should mark the same 3 nets as external.
    """
    pcb = tmp_path / "design.kicad_pcb"
    pcb.write_text("""
(kicad_pcb
  (footprint "io_pads:IOPad_WireBond_100x100"
    (layer "F.Cu") (at 1.0 -1.0 0)
    (property "Reference" "J1")
    (property "IO_CLASS" "wire_bond")
    (property "IO_PAD_SIZE_UM" "100x100")
    (pad "1" smd rect (at 0 0) (size 0.1 0.1) (layers "F.Cu" "F.Mask")
      (net 1 "VDD_EXT")))
  (footprint "io_pads:IOPad_WireBond_100x100"
    (layer "F.Cu") (at 2.0 -1.0 0)
    (property "Reference" "J2")
    (property "IO_CLASS" "wire_bond")
    (property "IO_PAD_SIZE_UM" "100x100")
    (pad "1" smd rect (at 0 0) (size 0.1 0.1) (layers "F.Cu" "F.Mask")
      (net 2 "OUT0")))
  (footprint "io_pads:IOPad_WireBond_100x100"
    (layer "F.Cu") (at 3.0 -1.0 0)
    (property "Reference" "J3")
    (property "IO_CLASS" "wire_bond")
    (property "IO_PAD_SIZE_UM" "100x100")
    (pad "1" smd rect (at 0 0) (size 0.1 0.1) (layers "F.Cu" "F.Mask")
      (net 3 "GND_EXT")))
  (footprint "Connector:JST"
    (layer "F.Cu") (at 5.0 -1.0 0)
    (property "Reference" "X1")
    (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu"))))
""")

    netfile = tmp_path / "design.net"
    _write_minimal_net(
        netfile,
        {
            "U1": ("chiplets", "MyDie"),
            "J1": ("io_pads", "IOPad_WireBond_100x100"),
            "J2": ("io_pads", "IOPad_WireBond_100x100"),
            "J3": ("io_pads", "IOPad_WireBond_100x100"),
            "X1": ("Connector", "JST"),
        },
        nets=[
            ("VDD_EXT",  [("U1", "VDD"), ("J1", "1")]),
            ("OUT0",     [("U1", "OUT"), ("J2", "1")]),
            ("GND_EXT",  [("U1", "GND"), ("J3", "1")]),
            ("INTERNAL", [("U1", "A"),   ("U1", "B")]),
            ("X1_NET",   [("X1", "1"),   ("U1", "X")]),
        ],
    )

    # Path 1: PCB -> io_pads.json
    json_path = tmp_path / "io_pads.json"
    res = subprocess.run(
        [sys.executable, str(EXTRACT_SCRIPT), str(pcb), "-o", str(json_path)],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    pad_refs = {p["ref"] for p in json.loads(json_path.read_text())["io_pads"]}
    assert pad_refs == {"J1", "J2", "J3"}

    # Path 2: netlist -> YAML with external flags
    nets = parse_kicad_netlist(str(netfile), skip_unconnected=False)
    external_nets = {n.name for n in nets if n.external}
    assert external_nets == {"VDD_EXT", "OUT0", "GND_EXT"}

    # The two paths must agree: every ref discovered as an I/O pad in the
    # PCB must also be the destination of an external net in the netlist
    # (and vice-versa).
    netlist_io_refs = {
        c.component
        for n in nets if n.external
        for c in n.connections
        if c.component.startswith("J")
    }
    assert netlist_io_refs == pad_refs
