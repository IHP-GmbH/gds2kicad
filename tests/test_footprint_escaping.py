# SPDX-License-Identifier: GPL-3.0-or-later
"""The footprint writer must not be corrupted by hostile GDS-derived names.

GDS cell names and text labels are third-party data (closed-PDK black-box
chiplets are an explicit use case) and can contain S-expression metacharacters.
Before the fix, pad names were interpolated into the .kicad_mod unescaped, so a
quote/paren produced a structurally invalid footprint KiCad could not load.
"""
import sys
from pathlib import Path

import klayout.db as db

sys.path.insert(0, str(Path(__file__).parent.parent))

from gds_to_kicad import GDSToKiCad, DEFAULT_GENERIC_LYP  # noqa: E402
from lyp_parser import LYPParser  # noqa: E402


def _balanced(content):
    # Quotes balanced per line (no token left open) and parens balanced overall.
    if content.count("(") != content.count(")"):
        return False
    return all(line.count('"') % 2 == 0 for line in content.splitlines())


def test_pad_name_with_quote_and_parens_stays_valid(tmp_path):
    lyp = LYPParser(str(DEFAULT_GENERIC_LYP))
    conv = GDSToKiCad(lyp, "pad.drawing", pad_layer=(205, 0))
    out = tmp_path / "die.kicad_mod"
    pad_dicts = [{"bbox": db.Box(0, 0, 1000, 1000),
                  "is_polygon": False, "polygon_points": None}]
    conv._generate_kicad_footprint(
        'DIE"X(', pad_dicts, str(out), "src.gds",
        pad_names={0: 'VDD"(evil)'}, dbu_to_mm=1e-6)
    content = out.read_text(encoding="utf-8")
    # the raw hostile substrings must not appear verbatim
    assert 'VDD"(evil)' not in content
    assert 'DIE"X(' not in content
    assert _balanced(content)
