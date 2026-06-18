# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the black-box conversion paths: raw --pad-layer-number override,
default generic lyp (no --lyp-file), and densest-layer auto-detect.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import klayout.db as db

from gds_to_kicad import parse_layer_spec, DEFAULT_GENERIC_LYP

SCRIPT = Path(__file__).parent.parent / "gds_to_kicad.py"


def _pads_only_gds(path, pad_layer, text_layer, names=("VDD", "GND", "CLK", "DAT")):
    """Synthesize a closed-PDK style GDS: only pad boxes + pad-name texts."""
    ly = db.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("BLACKBOX")
    pl = ly.layer(*pad_layer)
    tl = ly.layer(*text_layer)
    for i, name in enumerate(names):
        x0 = (i % 5) * 200000
        y0 = (i // 5) * 200000
        x1, y1 = x0 + 80000, y0 + 80000
        top.shapes(pl).insert(db.Box(x0, y0, x1, y1))
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        top.shapes(tl).insert(db.Text(name, db.Trans(db.Point(cx, cy))))
    ly.write(str(path))
    return path, names


def _run(args):
    return subprocess.run([sys.executable, str(SCRIPT)] + args,
                          capture_output=True, text=True)


def _assert_named_footprint(mod_path, names):
    txt = Path(mod_path).read_text()
    assert txt.count("(pad ") == len(names)
    for n in names:
        assert f'"{n}"' in txt, f"pad name {n} missing from footprint"


def test_parse_layer_spec():
    assert parse_layer_spec("205/0") == (205, 0)
    assert parse_layer_spec("134/25") == (134, 25)
    assert parse_layer_spec("134") == (134, 0)
    with pytest.raises(Exception):
        parse_layer_spec("abc")


def test_default_lyp_is_generic():
    assert DEFAULT_GENERIC_LYP.name == "generic.lyp"
    assert DEFAULT_GENERIC_LYP.exists()


def test_pad_layer_number_no_lyp_file(tmp_path):
    """Raw pad + text layer numbers, NO --lyp-file (defaults to generic)."""
    gds, names = _pads_only_gds(tmp_path / "chip.gds", (134, 0), (134, 25))
    out = tmp_path / "chip.kicad_mod"
    r = _run([str(gds), "--pad-layer-number", "134/0",
              "--text-layer-number", "134/25", "-o", str(out)])
    assert r.returncode == 0, r.stderr
    _assert_named_footprint(out, names)


def test_auto_detect_densest_layer(tmp_path):
    """No --layer / --pad-layer-number: the densest layer is auto-detected,
    and its proximate text layer still names the pads."""
    gds, names = _pads_only_gds(tmp_path / "chip.gds", (88, 0), (88, 25))
    out = tmp_path / "chip.kicad_mod"
    r = _run([str(gds), "-o", str(out)])
    assert r.returncode == 0, r.stderr
    assert "Auto-detected pad layer: 88/0" in r.stdout
    _assert_named_footprint(out, names)
