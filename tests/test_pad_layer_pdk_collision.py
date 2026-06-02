"""Guard: the canonical black-box pad layers must not collide with the
reference interposer PDK (IHP SG13G2).

The black-box generator stamps synthetic pad/outline geometry on the ADK
canonical layers (adk/config/chiplet_pads.json: 205/0, 205/25, 206/0). Those
numbers are deliberately chosen in a band IHP leaves free -- IHP SG13G2 uses
the Exchange family 190-194 and layer 257, leaving 195-256 unassigned. When a
black-box chiplet is placed onto an IHP interposer its pads end up in the
assembled GDS; if these layers ever aliased a real IHP fab layer the synthetic
pads would overlap genuine interposer geometry. This test pins the choice
against the IHP layer set, so a config edit or a PDK revision that breaks it is
caught.

Unlike the boundary (pure assembly metadata, moved to the manifest), the pads
are real geometry that must live on some GDS layer -- the fix is a collision-free
number, not a sidecar.

Skipped when the IHP PDK is not reachable (PDK_ROOT unset / sg13g2.lyp missing).
"""
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from blackbox_chiplet import load_canonical_layers


def _ihp_sg13g2_used_layers():
    """Return the set of (layer, datatype) IHP SG13G2 assigns, parsed from the
    KLayout layer-properties file -- the comprehensive set, which includes the
    Exchange family 190-194 and 257 that the streamout .map omits."""
    root = os.environ.get("PDK_ROOT")
    if not root:
        pytest.skip("PDK_ROOT unset; cannot verify against the IHP layer set")
    lyp = (Path(root) / "ihp-sg13g2" / "libs.tech" / "klayout" / "tech"
           / "sg13g2.lyp")
    if not lyp.exists():
        pytest.skip(f"IHP sg13g2.lyp not found: {lyp}")
    used = set()
    for m in re.finditer(r"<source>(\d+)/(\d+)", lyp.read_text()):
        used.add((int(m.group(1)), int(m.group(2))))
    return used


def test_canonical_pad_layers_are_free_in_ihp_sg13g2():
    used = _ihp_sg13g2_used_layers()
    # Parser sanity: a real IHP layer (Exchange0) must be present, else an empty
    # parse would make the collision check pass vacuously.
    assert (190, 0) in used, "parser sanity: IHP Exchange0 190/0 should be present"

    canonical = load_canonical_layers()
    collisions = {name: ld for name, ld in canonical.items() if ld in used}
    assert not collisions, (
        "black-box pad layers collide with IHP SG13G2 fab layers: "
        + ", ".join(f"{n}={l}/{d}" for n, (l, d) in collisions.items())
        + " -- pick layers free in the interposer PDK "
        "(see adk/config/chiplet_pads.json)"
    )
