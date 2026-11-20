# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for extract_padmap.py cmd_annotate: side, type, role and direction
set on a pin list from the plan that placed its pads."""
import sys
import json
from pathlib import Path
from argparse import Namespace

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

import extract_padmap


PADS = ["VDD", "IOVDD", "VSS", "IOVSS", "IN0", "VDDX", "OUT0", "VINN"]


def _annotate(tmp_path, pins):
    plan = tmp_path / "die.plan.json"
    plan.write_text(json.dumps({
        "chiplet_name": "DIE", "die": {"width_um": 1000.0, "height_um": 1000.0},
        "ring": {"inset_um": 60.0, "pitch_um": 100.0, "per_side": 2}, "pads": PADS}))
    path = tmp_path / "die.pins.json"
    path.write_text(json.dumps({"version": "1.0", "pins": pins}, indent=2) + "\n")
    extract_padmap.cmd_annotate(Namespace(plan=str(plan), pins=str(path)))
    return {p["name"]: p for p in json.loads(path.read_text())["pins"]}


def test_supply_prefixes_get_a_role_and_direction(tmp_path):
    pins = _annotate(tmp_path, [{"name": n, "type": "passive", "side": "left"} for n in PADS])
    for name in ("VDD", "IOVDD", "VDDX"):
        assert (pins[name]["type"], pins[name]["role"], pins[name]["direction"]) == (
            "power_in", "POWER", "INOUT"), name
    for name in ("VSS", "IOVSS"):
        assert (pins[name]["type"], pins[name]["role"], pins[name]["direction"]) == (
            "power_in", "GROUND", "INOUT"), name


def test_the_other_pins_keep_no_role(tmp_path):
    """Only the prefix rule decides: a pin it does not name a supply has no
    role or direction, even one a footprint pin list carried."""
    source = [{"name": n, "type": "passive", "side": "left"} for n in PADS]
    source[PADS.index("IN0")].update(role="SIGNAL", direction="INPUT")
    pins = _annotate(tmp_path, source)
    for name in ("IN0", "OUT0", "VINN"):
        assert pins[name]["type"] == "passive"
        assert "role" not in pins[name] and "direction" not in pins[name], name


def test_a_pin_keeps_its_keys_and_their_order(tmp_path):
    """Annotate edits the pins in place: the keys a pin had keep their order,
    and role and direction follow them."""
    pins = _annotate(tmp_path, [{"name": n, "type": "passive", "side": "left", "pad_index": i}
                                for i, n in enumerate(PADS)])
    assert list(pins["VSS"]) == ["name", "type", "side", "pad_index", "role", "direction"]
    assert list(pins["IN0"]) == ["name", "type", "side", "pad_index"]
