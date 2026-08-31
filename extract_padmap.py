#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Pad maps for a chiplet die, in the schema blackbox_chiplet.py consumes.

Two modes, one schema, one place for the naming rule.

  plan     Build the pad map of a die that does not exist yet, from a small
           plan file: die size, ring geometry and an ordered list of pad names.
           The ordering is the human decision (which net sits on the facing
           side); the arithmetic is here.

  extract  Read the pad map back out of a finished die GDS, pairing each pad
           box with its name text. Used in Phase E to prove the die that got
           built carries the pad map the assembly was designed against.

Why extract is a script and not a KLayout session: on the SG13G2 padring the
name texts sit at pad *corners* and on a different layer from the pads
(TopMetal1 text over a dfpad box), so a generic nearest-text pairing is not
safe. A script is also the provenance record a manual session is not.

The naming rule, enforced in both modes: every pad name is unique. A pad-to-net
join by name alone is impossible on a die that repeats names, and unique names
remove the ordinal binding from every downstream tool.

Output is the blackbox_chiplet.py spec:

    {"chiplet_name": ..., "die": {"bbox_um": [x0, y0, x1, y1]},
     "pads": [{"name":..., "x_um":..., "y_um":..., "w_um":..., "h_um":...}, ...]}

The die box is emitted as an explicit bbox with its lower-left at the origin,
matching the LibreLane die frame, so swapping the abstract for the real die
changes nothing downstream.
"""

import argparse
import json
import sys
from pathlib import Path

# Counter-clockwise from the east side. Each entry is (constant axis, how the
# running coordinate advances along that side).
_SIDES = ("E", "N", "W", "S")


def _fail(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def _check_unique(names):
    seen = {}
    for i, n in enumerate(names):
        if n in seen:
            _fail(f"pad name {n!r} repeats (positions {seen[n]} and {i}); "
                  f"every pad name must be unique, see CLAUDE.md")
        seen[n] = i


def _side_counts(per_side):
    """Normalise the per_side field to a count for each of E, N, W, S.

    A bare integer means the same count on all four sides, which is what a
    padring generator does. A mapping means the sides carry different counts,
    which is what an assembly needs: the die-to-die pads have to sit on the edge
    that faces the other die, and there are more of them than one edge of a 1.6
    or 2.0 mm die holds at the padring pitch.
    """
    if isinstance(per_side, dict):
        missing = [s for s in _SIDES if s not in per_side]
        if missing:
            _fail(f"ring.per_side is missing side(s) {missing}; give all of "
                  f"{list(_SIDES)} or a single integer for a uniform ring")
        extra = [k for k in per_side if k not in _SIDES]
        if extra:
            _fail(f"ring.per_side has unknown side(s) {extra}; sides are {list(_SIDES)}")
        return {s: int(per_side[s]) for s in _SIDES}
    return {s: int(per_side) for s in _SIDES}


def _ring_positions(width, height, inset, pitch, per_side, start_side, direction):
    """Pad centres around a peripheral ring, in die-local um.

    Walks the four sides in ring order starting at start_side, carrying the
    per-side count from _side_counts. Each side's pads are centred on that side
    and spaced by pitch.

    'ccw' walks E, N, W, S and runs each side in the counter-clockwise sense
    (east upward, north leftward, west downward, south rightward); 'cw' walks
    E, S, W, N and runs every side the other way. Reversing the side order
    without reversing the run within each side is not a clockwise walk, it is a
    walk that jumps back at every corner, so the two are flipped together.

    That the trip is continuous is the whole point: a contiguous group of names
    in the plan lands on contiguous pads, so "the die-to-die nets" is a slice of
    the name list rather than an index puzzle.
    """
    counts = _side_counts(per_side)
    order = list(_SIDES) if direction == "ccw" else [_SIDES[0]] + list(reversed(_SIDES[1:]))
    k = order.index(start_side)
    order = order[k:] + order[:k]

    # Counter-clockwise sense per side; clockwise is the reverse of each.
    ccw_reversed = {"E": False, "N": True, "W": True, "S": False}

    out, sides = [], []
    for side in order:
        n = counts[side]
        span = (n - 1) * pitch
        extent = height if side in ("E", "W") else width
        run = [(extent - span) / 2.0 + i * pitch for i in range(n)]
        if ccw_reversed[side] != (direction != "ccw"):
            run.reverse()
        if side in ("E", "W"):
            const = width - inset if side == "E" else inset
            out.extend((const, v) for v in run)
        else:
            const = height - inset if side == "N" else inset
            out.extend((v, const) for v in run)
        sides.extend([side] * n)
    return out, sides


def cmd_plan(args):
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    name = plan["chiplet_name"]
    die = plan["die"]
    ring = plan["ring"]
    pads = plan["pads"]

    width = float(die["width_um"])
    height = float(die["height_um"])
    per_side = _side_counts(ring["per_side"])
    pitch = float(ring["pitch_um"])
    pad = float(ring["pad_um"])
    inset = float(ring["inset_um"])

    total = sum(per_side.values())
    if len(pads) != total:
        _fail(f"{name}: {len(pads)} pad names for {total} ring positions "
              f"({', '.join(f'{s}={per_side[s]}' for s in _SIDES)}); the plan "
              f"must fill the ring exactly")
    _check_unique(pads)

    for side in _SIDES:
        extent = height if side in ("E", "W") else width
        span = (per_side[side] - 1) * pitch
        if span + pad + 2 * inset > extent:
            _fail(f"{name}: side {side} does not fit in {extent} um: "
                  f"{per_side[side]} pads at {pitch} um pitch span {span} um, "
                  f"plus a {pad} um pad and {inset} um of inset at each end")

    positions, _ = _ring_positions(width, height, inset, pitch, per_side,
                                   ring.get("start_side", "E"),
                                   ring.get("direction", "ccw"))
    spec = {
        "chiplet_name": name,
        "_provenance": {
            "generator": "extract_padmap.py plan",
            "plan": Path(args.plan).name,
            "ring": {"pitch_um": pitch, "pad_um": pad, "inset_um": inset,
                     "per_side": {s: per_side[s] for s in _SIDES},
                     "start_side": ring.get("start_side", "E"),
                     "direction": ring.get("direction", "ccw")},
        },
        "die": {"bbox_um": [0.0, 0.0, width, height]},
        "pads": [{"name": n, "x_um": round(x, 3), "y_um": round(y, 3),
                  "w_um": pad, "h_um": pad}
                 for n, (x, y) in zip(pads, positions)],
    }
    _write(spec, args.output)
    _report(spec, pitch)


_POWER_PREFIXES = ("VDD", "VSS", "IOVDD", "IOVSS")

# Die side to schematic-symbol side. The symbol side is a readability choice,
# not geometry, so it simply mirrors the physical side the pad sits on.
_SIDE_TO_SYMBOL = {"E": "right", "W": "left", "N": "top", "S": "bottom"}


def cmd_annotate(args):
    """Set side and type on a pin list, from the plan that placed the pads.

    footprint_to_pinlist.py can only recover name and geometry, because a
    footprint carries no such semantics; it defaults every pin to left/passive.
    Those two fields are the one human decision in the pin list, and the plan
    already encodes it: the ring walk knows which side each pad landed on, and
    the naming convention says which pads are supplies. Deriving them here keeps
    the pin list reproducible instead of hand-edited.
    """
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    ring = plan["ring"]
    names = plan["pads"]
    _, sides = _ring_positions(float(plan["die"]["width_um"]),
                               float(plan["die"]["height_um"]),
                               float(ring["inset_um"]), float(ring["pitch_um"]),
                               _side_counts(ring["per_side"]),
                               ring.get("start_side", "E"),
                               ring.get("direction", "ccw"))
    side_of = dict(zip(names, sides))

    pins_path = Path(args.pins)
    doc = json.loads(pins_path.read_text(encoding="utf-8"))
    unknown = [p["name"] for p in doc["pins"] if p["name"] not in side_of]
    if unknown:
        _fail(f"{pins_path}: pin(s) {unknown[:5]} are not in {args.plan}; the "
              f"pin list and the plan describe different dies")

    counts = {}
    for pin in doc["pins"]:
        pin["side"] = _SIDE_TO_SYMBOL[side_of[pin["name"]]]
        pin["type"] = ("power_in"
                       if pin["name"].startswith(_POWER_PREFIXES) else "passive")
        counts[(pin["side"], pin["type"])] = counts.get((pin["side"], pin["type"]), 0) + 1

    pins_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(f"{pins_path.name}: annotated {len(doc['pins'])} pins from "
          f"{Path(args.plan).name}", file=sys.stderr)
    for (side, kind), n in sorted(counts.items()):
        print(f"  {side:<7} {kind:<9} {n}", file=sys.stderr)


def cmd_extract(args):
    try:
        import klayout.db as db
    except ImportError:
        _fail("KLayout Python module not found; run inside the adk-tools image "
              "or a venv with klayout installed")

    pl, pd = _layer(args.pad_layer)
    tl, td = _layer(args.text_layer)

    layout = db.Layout()
    layout.read(args.gds)
    top = layout.top_cell() if not args.top_cell else layout.cell(args.top_cell)
    if top is None:
        _fail(f"cell {args.top_cell!r} not found in {args.gds}")

    pad_idx = layout.find_layer(pl, pd)
    txt_idx = layout.find_layer(tl, td)
    if pad_idx is None:
        _fail(f"pad layer {args.pad_layer} is absent from {args.gds}")
    if txt_idx is None:
        _fail(f"text layer {args.text_layer} is absent from {args.gds}")

    boxes = []
    it = top.begin_shapes_rec(pad_idx)
    while not it.at_end():
        boxes.append(it.shape().dbbox().transformed(it.dtrans()))
        it.next()
    if not boxes:
        _fail(f"no shapes on pad layer {args.pad_layer}")

    texts = []
    it = top.begin_shapes_rec(txt_idx)
    while not it.at_end():
        sh = it.shape()
        if sh.is_text():
            t = sh.dtext.transformed(it.dtrans())
            texts.append((t.string, t.x, t.y))
        it.next()

    # Pair each pad with the label whose anchor lies on or nearest to it. The
    # SG13G2 padring puts the text at a pad corner, so containment is checked
    # against the box grown by half a pad; ties break on distance to centre.
    pads, unlabelled = [], []
    for b in sorted(boxes, key=lambda b: (round(b.center().y, 3), round(b.center().x, 3))):
        cx, cy = b.center().x, b.center().y
        reach = max(b.width(), b.height())
        best, best_d = None, None
        for s, tx, ty in texts:
            d = max(abs(tx - cx), abs(ty - cy))
            if d <= reach and (best_d is None or d < best_d):
                best, best_d = s, d
        if best is None:
            unlabelled.append((round(cx, 3), round(cy, 3)))
            continue
        pads.append({"name": best, "x_um": round(cx, 3), "y_um": round(cy, 3),
                     "w_um": round(b.width(), 3), "h_um": round(b.height(), 3)})

    if unlabelled:
        _fail(f"{len(unlabelled)} pad(s) on {args.pad_layer} carry no label on "
              f"{args.text_layer}; first at {unlabelled[0]}. A pad map with an "
              f"anonymous pad is not usable downstream.")
    # Signal and control pads must stay unique (a name join is by name alone, so a
    # swap must not hide). Supply pads cannot: the built SG13G2 padring labels every
    # supply pad by its rail net (VSS x8, VDD x5, IOVDD/IOVSS x4 ...), so the plan's
    # unique VSS_0-style names are unrecoverable by extraction. Supplies bind
    # downstream by (rail net, pad_index), so a repeated rail name is expected here
    # and is not an error (handoff 7.3, supply identity by rail net).
    _check_unique([p["name"] for p in pads
                   if not p["name"].startswith(_POWER_PREFIXES)])

    bb = top.dbbox()
    # The die top-cell GDS bbox in native database units (die-local frame), same
    # convention as a pin list's center_x_dbu. This is the die outline whose centre
    # the bbox_center anchor's reference point sits at; it comes from the GDS top
    # cell, NOT from pad extents (pads sit inside the die edge, so pad extents would
    # undersize it). Carried downstream so pin-list mounting can place a bbox_center
    # die without guessing its size.
    bb_dbu = top.bbox()
    spec = {
        "chiplet_name": args.name or top.name,
        "_provenance": {
            "generator": "extract_padmap.py extract",
            "gds": Path(args.gds).name,
            "top_cell": top.name,
            "pad_layer": args.pad_layer,
            "text_layer": args.text_layer,
        },
        "die": {"bbox_um": [round(bb.left, 3), round(bb.bottom, 3),
                            round(bb.right, 3), round(bb.top, 3)]},
        "die_bbox_dbu": {"x_min": bb_dbu.left, "y_min": bb_dbu.bottom,
                         "x_max": bb_dbu.right, "y_max": bb_dbu.top},
        "pads": pads,
    }
    _write(spec, args.output)
    _report(spec, None)


def _layer(s):
    try:
        a, b = s.split("/")
        return int(a), int(b)
    except ValueError:
        _fail(f"layer must be given as layer/datatype, got {s!r}")


def _write(spec, output):
    text = json.dumps(spec, indent=2) + "\n"
    if output == "-":
        sys.stdout.write(text)
    else:
        Path(output).write_text(text, encoding="utf-8")


def _report(spec, pitch):
    """Print the numbers the attachment rules are actually checked against.

    The binding constraint is not the ring pitch, it is the closest pair of
    pads anywhere on the die, including two pads on perpendicular sides near a
    corner. Centre-to-centre feeds the pitch rule (IXN.e / Padc.e) and the gap
    between passivation openings feeds the spacing rule (IXN.b / Padc.b); the
    opening is smaller than the pad, so quoting the pad-edge gap here is the
    conservative reading.
    """
    pads = spec["pads"]
    bb = spec["die"]["bbox_um"]
    closest, best = None, None
    for i, a in enumerate(pads):
        for b in pads[i + 1:]:
            dx = abs(a["x_um"] - b["x_um"])
            dy = abs(a["y_um"] - b["y_um"])
            d = (dx * dx + dy * dy) ** 0.5
            if best is None or d < best:
                best, closest = d, (a, b)
    a, b = closest
    print(f"{spec['chiplet_name']}: {len(pads)} pads, die "
          f"{bb[2] - bb[0]:g} x {bb[3] - bb[1]:g} um", file=sys.stderr)
    if pitch is not None:
        print(f"  ring pitch: {pitch:g} um", file=sys.stderr)
    print(f"  closest pair: {a['name']} to {b['name']}, {best:.3f} um "
          f"centre to centre", file=sys.stderr)
    # opening, min pitch, min opening-to-opening space, per interconnect method
    for mid, opening, min_pitch, min_space in (
            ("cupillar_opt1", 35.0, 75.0, 40.0),
            ("cupillar_opt2", 40.0, 80.0, 40.0),
            ("cupillar_opt3", 45.0, 95.0, 50.0)):
        space = best - opening
        ok = "ok " if (best >= min_pitch and space >= min_space) else "FAIL"
        print(f"  {ok} {mid}: pitch {best:.3f} vs {min_pitch:g}, "
              f"opening space {space:.3f} vs {min_space:g}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="build a pad map from a die plan file")
    p.add_argument("plan")
    p.add_argument("-o", "--output", default="-")
    p.set_defaults(func=cmd_plan)

    a = sub.add_parser("annotate",
                       help="set side and type on a pin list, from its plan")
    a.add_argument("plan")
    a.add_argument("pins")
    a.set_defaults(func=cmd_annotate)

    e = sub.add_parser("extract", help="read a pad map out of a finished die GDS")
    e.add_argument("gds")
    e.add_argument("--pad-layer", default="41/0", help="default 41/0 (dfpad)")
    e.add_argument("--text-layer", default="126/25",
                   help="default 126/25 (TopMetal1 text, where the padring puts names)")
    e.add_argument("--top-cell", default=None)
    e.add_argument("--name", default=None)
    e.add_argument("-o", "--output", default="-")
    e.set_defaults(func=cmd_extract)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
