# SPDX-License-Identifier: GPL-3.0-or-later
"""Active coverage for the netlist output formatters (CSV / YAML / --inject).

The committed test_netlist_converter.py suite skips in its entirety when the
external demo netlist is absent (it is, in a clean checkout and in CI), so the
CSV/YAML/inject contract had no active coverage. These tests build Net objects
in memory so they always run, and pin the escaping/idempotency behavior the
audit flagged.
"""
import csv
import io
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from kicad_netlist_to_chiplet import (  # noqa: E402
    Net, NetConnection, nets_to_csv, nets_to_yaml, inject_into_chiplet,
    VALID_NET_CLASSES,
)


def _sample_nets():
    return [
        Net(name="VDD", net_class="power",
            connections=[NetConnection("U1", "VDD", "TopMetal2"),
                         NetConnection("J1", "1", "")], external=True),
        Net(name="GND", net_class="ground",
            connections=[NetConnection("U1", "GND", "")]),
        Net(name="DATA0", net_class="signal",
            connections=[NetConnection("U1", "D0", "")]),
    ]


def test_csv_header_and_net_classes():
    text = nets_to_csv(_sample_nets())
    assert "\r" not in text  # LF on disk, not CRLF
    rows = list(csv.DictReader(io.StringIO(text)))
    assert text.splitlines()[0] == "net_name,component,pin,layer,net_class"
    assert rows
    # Each row carries its net's class verbatim in the right column; assert
    # the exact mapping (not just membership, which would be tautological).
    by_net = {(r["net_name"], r["net_class"]) for r in rows}
    assert ("VDD", "power") in by_net
    assert ("GND", "ground") in by_net
    assert ("DATA0", "signal") in by_net
    assert all(cls in VALID_NET_CLASSES for _, cls in by_net)


def test_yaml_round_trips_via_safe_load():
    doc = yaml.safe_load(nets_to_yaml(_sample_nets()))
    by = {n["name"]: n for n in doc["netlist"]["nets"]}
    assert set(by) == {"VDD", "GND", "DATA0"}
    assert by["VDD"]["external"] is True
    assert by["VDD"]["connections"][0]["component"] == "U1"
    assert by["VDD"]["connections"][0]["layer"] == "TopMetal2"


def test_yaml_quotes_unsafe_names():
    # Names with YAML-significant content (':' and a leading '#') must survive
    # round-trip as literal strings rather than break the parse or change type.
    nets = [Net(name="NET: x", net_class="signal",
                connections=[NetConnection("#PWR01", "1", "")])]
    n = yaml.safe_load(nets_to_yaml(nets))["netlist"]["nets"][0]
    assert n["name"] == "NET: x"
    assert n["connections"][0]["component"] == "#PWR01"


def test_inject_is_idempotent(tmp_path):
    chip = tmp_path / "design.chiplet"
    chip.write_text("format_version: '1.0'\nnetlist:\n  nets: []\n",
                    encoding="utf-8")
    block = nets_to_yaml(_sample_nets())
    inject_into_chiplet(str(chip), block)
    first = chip.read_bytes()
    inject_into_chiplet(str(chip), block)
    assert chip.read_bytes() == first  # re-injecting the same block is stable
    doc = yaml.safe_load(chip.read_text(encoding="utf-8"))
    assert len(doc["netlist"]["nets"]) == 3  # exactly one netlist section


def test_inject_handles_blank_line_in_existing_block(tmp_path):
    # A blank line inside the existing netlist block must not orphan its tail
    # into the freshly written section (the old regex stopped at the blank).
    chip = tmp_path / "d.chiplet"
    chip.write_text(
        "netlist:\n  nets:\n    - name: OLD\n      class: signal\n"
        "      connections:\n        - {component: U9, pin: 1}\n\n"
        "    - name: OLD2\n      class: power\n      connections:\n"
        "        - {component: U9, pin: 2}\n",
        encoding="utf-8")
    inject_into_chiplet(str(chip), nets_to_yaml(_sample_nets()))
    names = [n["name"] for n in
             yaml.safe_load(chip.read_text(encoding="utf-8"))["netlist"]["nets"]]
    assert names == ["VDD", "GND", "DATA0"]  # OLD / OLD2 fully replaced
