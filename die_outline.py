# SPDX-License-Identifier: GPL-3.0-or-later
"""The die outline read rule, shared by every path that touches a die outline.

The footprint converter, the pad-review conversion and the pad-review
generator all call this one function, so they cannot disagree about what the
die outline is. The rule matches how a consumer reads the outline from the die
GDS: hierarchical, property-blind, merged, exactly one rectangle, and a named
refusal for everything else. There is never a bounding-box fallback.
"""

import sys
from typing import Tuple

try:
    import klayout.db as db
except ImportError:
    print("Error: KLayout Python module not found.", file=sys.stderr)
    sys.exit(1)


# The outline box that --generate-pad-review writes carries this GDS property,
# so the read-back can tell it from a pad without being told the outline layer.
# The read rule below ignores properties, so the tag never changes the outline.
REVIEW_OUTLINE_PROPERTY = (1, "gds2kicad:die-outline")


def is_review_outline(shape: "db.Shape") -> bool:
    """True when `shape` is the die outline box a pad review was written with."""
    key, value = REVIEW_OUTLINE_PROPERTY
    return shape.property(key) == value


def read_die_outline(layout: "db.Layout", cell: "db.Cell",
                     layer_spec: Tuple[int, int]) -> "db.Box":
    """Return the die outline box on `layer_spec` (layer, datatype) in `cell`.

    Every way this can be wrong is a refusal, raised before anything is
    written. The caller has asserted the outline exists by naming its layer,
    so "no outline found" is a contradiction of the command line, not a
    condition to recover from.

    The outline must merge to exactly ONE rectangle. Two rectangles cannot be
    ordered into a single die box without guessing which one is the die, and
    a non-rectangular merged shape has no unambiguous width and height at all;
    the .chiplet dimensions this eventually feeds are one width and one
    height, so anything else has to stop here rather than be reduced to a
    bounding box that silently invents the missing information.
    """
    layer, datatype = layer_spec
    spec = "%d/%d" % (layer, datatype)

    # find_layer, not layer: layout.layer() CREATES the layer when it is
    # absent, so it would report an empty shape set for a layer that was
    # never in the file and an empty one that was, indistinguishably.
    layer_index = layout.find_layer(layer, datatype)
    if layer_index is None:
        raise ValueError(
            "die outline layer %s is not present in the layout. "
            "--die-outline-layer names the layer the die outline is drawn "
            "on (the generic black-box writes 206/0); pass the layer that "
            "this GDS actually uses, or omit the option to keep the legacy "
            "pad-hull courtyard." % spec)

    # Hierarchical and property-blind, the same read a consumer does on the
    # die GDS. Property-blind matters on real dies: every IHP stdcell's
    # prBoundary box carries a GDS property ('oaBoundary:pr'), and a merge
    # keeps shapes with different properties apart, so a Region built from
    # cell.shapes() would split one die outline into several pieces.
    region = db.Region(cell.begin_shapes_rec(layer_index))
    # Merge before the emptiness test: a region backed by the shape iterator
    # is not "empty" while the layer holds only texts, and that is the empty
    # case a GDS read actually produces.
    region.merge()
    if region.is_empty():
        raise ValueError(
            "die outline layer %s exists but carries no shape in cell '%s'"
            % (spec, cell.name))

    polygons = list(region.each())
    if len(polygons) != 1:
        raise ValueError(
            "die outline layer %s merges to %d separate shapes in cell "
            "'%s'; the die outline must be one rectangle, so which of them "
            "is the die is not decidable here"
            % (spec, len(polygons), cell.name))

    poly = polygons[0]
    if not poly.is_box():
        raise ValueError(
            "die outline layer %s merges to a non-rectangular shape (%d "
            "hull points) in cell '%s'; the die box this feeds is one width "
            "and one height, which a non-rectangle does not define"
            % (spec, poly.num_points(), cell.name))

    box = poly.bbox()
    if box.width() <= 0 or box.height() <= 0:
        raise ValueError(
            "die outline layer %s gives a degenerate box %d x %d DBU in "
            "cell '%s'" % (spec, box.width(), box.height(), cell.name))

    return box
