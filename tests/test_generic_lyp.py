# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the bundled generic pads-only LYP (default for black-box chiplets).

The committed pdks/generic.lyp is the offline default the footprint converter
falls back to when no PDK .lyp is available. Its layer numbers are the ADK
canonical vocabulary (adk/config/chiplet_pads.json).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lyp_parser import LYPParser
from gds_to_kicad import DEFAULT_GENERIC_LYP

GENERIC_LYP = Path(__file__).parent.parent / "pdks" / "generic.lyp"


def test_default_generic_lyp_path():
    assert DEFAULT_GENERIC_LYP == GENERIC_LYP
    assert DEFAULT_GENERIC_LYP.exists(), "pdks/generic.lyp must be committed"


def test_generic_lyp_layers():
    p = LYPParser(str(GENERIC_LYP))
    assert p.get_layer("pad.drawing") == (205, 0)
    assert p.get_layer("pad.text") == (205, 25)
    assert p.get_layer("outline.drawing") == (206, 0)


def test_generic_lyp_text_pairing():
    """The .text datatype (25) must let the auto-pairing find pad.text from
    pad.drawing -- this is what makes the black-box round-trip name its pads."""
    p = LYPParser(str(GENERIC_LYP))
    assert p.find_text_layers_for("pad.drawing") == ["pad.text"]


def test_generic_lyp_mirrors_canonical_vocabulary():
    """generic.lyp must mirror the ADK canonical numbers (the source of truth
    in adk/config/chiplet_pads.json). load_canonical_layers() reads that file
    (or its hardcoded fallback if the ADK is absent); the committed lyp must
    match. This drift guard replaced the generator's golden test."""
    from blackbox_chiplet import load_canonical_layers
    L = load_canonical_layers()
    p = LYPParser(str(GENERIC_LYP))
    assert p.get_layer("pad.drawing") == L["pad_drawing"]
    assert p.get_layer("pad.text") == L["pad_text"]
    assert p.get_layer("outline.drawing") == L["outline"]
