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


def _structurally_balanced(content):
    """Parens balance when counted OUTSIDE quoted strings.

    A literal '(' inside a quoted token is fine and must NOT be substituted, so
    a naive raw count is the wrong invariant. What matters is that no hostile
    double-quote broke a token open (exposing a stray structural paren): walk
    the text, skip quoted regions, and require the structural depth to balance.
    """
    depth = 0
    in_str = False
    i = 0
    while i < len(content):
        c = content[i]
        if in_str:
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth < 0:
                return False
        i += 1
    return depth == 0 and not in_str


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
    # the hostile double-quote must be neutralized (no verbatim substring, and
    # no token left open) so the structure stays valid; legitimate parens in a
    # name are preserved, not mangled.
    assert 'VDD"(evil)' not in content
    assert 'DIE"X(' not in content
    assert all(line.count('"') % 2 == 0 for line in content.splitlines())
    assert _structurally_balanced(content)
