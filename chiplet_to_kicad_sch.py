#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Draw a chiplet system as a KiCad schematic, straight from its .chiplet netlist.

One symbol per die, one pin per terminal of that die's pin list (a name the
list repeats, one pad of a rail, is one terminal), and a global label carrying
the exact net name on every connected pin; the remaining terminals get a
no-connect flag, except the open pins that link dies, which are left bare to
be wired. The sheet is laid out for reading and wiring on paper: dies
in one row, carriers below, every pin on the left or the right, and the pins
that link dies on facing columns in port order (``_columns``).

The sheet is a view of the system description:
nets come only from the .chiplet, terminals only from the per-die pin lists the
flow already binds (the ``--pins`` lists the board export is given), so a
schematic net, a .chiplet net and a board net name the same (component,
terminal) pairs.

A carrier, a component that is not a die but lists ``io_pads``, gets no symbol
of its own: each of its pads is a ``chiplet_system:EXT_PAD`` symbol, the one
designer sheets use, with Reference the pad id, Footprint the pad's
``ext_pad_footprint`` (its .chiplet size, square and a whole number of um) and
CHIPLET_COMPONENT the carrier id, so the board draws each pad as the footprint
the export gives it. The carrier's terminal is the pad id, and the pad sits on
the net the netlist puts it on, else on its ``io_pads`` net, else on none.
The read-back folds a carrier's pads back into one carrier (sheet_check.py).
Its pin list is derived from the .chiplet and written next to the sheet
(``<id>.carrier_pins.json``), so every drawn component has one. A die symbol
names its footprint by the die's top cell (``die_footprint``).

What a later import needs is written now, and reruns are byte-stable:
  * each symbol carries a hidden CHIPLET_COMPONENT property holding its
    component id (also its Reference) and a uuid5 derived from that id;
  * a terminal is the pin number, a net is the label text;
  * the sha256 of the base .chiplet is title-block comment 1.

Anything that cannot be drawn verbatim is refused by name and no schematic is
written: a connection to a component that is neither a die nor a carrier, a pin
the component does not have, a pad whose netlist net and ``io_pads`` net
disagree, a pad without a usable size (``io_pad_size``), a pad id that is also
a component id or another pad's (``reference_taken``), a die that states no
top cell, a name KiCad would not carry through its netlist unchanged. Every
refusal is collected, not just the first.

``--setup`` takes a co-simulation setup projection and also draws the setup:
its system-sheet symbols below the dies, and its die, bench and channel sheets
as flat sheets beside the system sheet (cosim_setup_sheets.py), with the symbol
kinds of ``--setup-spec``, the co-simulation flow's symbol spec, and writes the setup
symbol library with a ``sym-lib-table`` naming it beside them (refused when
another ``sym-lib-table`` is there). The report then lists every setup sheet by
role, path and sha256, and the library and the table by path and sha256. Setup
symbols are no components: the check, like the connectivity reader, leaves them
out.

``--setup-only`` draws the setup alone, for a system whose dies have no pin
lists: no system sheet is written, the system setup symbols go on a sheet of
their own (``<stem>.system.kicad_sch``, beside the others), and the .chiplet
only names the system. It takes no ``--pins`` and no ``--check``, and the
report's ``schematic`` is null.

``--handout DIR`` draws no sheet. It writes what a student needs to draw the
system on a blank sheet: ``chiplet_dies.kicad_sym``, one symbol per top cell
with the sheet's shape (link pins on the right, every pin passive, no
identity property), a ``sym-lib-table`` naming it by ``${KIPRJMOD}``, and a
project with an empty sheet, both named by ``--name`` or the .chiplet's stem.
No file carries a digest, a time or a host path, so reruns are byte-equal.

``--handout DIR --dies FOLDER --ext-pad-um N`` draws that handout from die
folders instead of a .chiplet; ``die_handout`` holds that path.

``--check`` exports the written sheet's netlist with kicad-cli and compares it
with the .chiplet (sheet_check.py): same named nets, same (component, terminal)
endpoints, every other terminal unconnected, identities and base digest intact,
every carrier pad folded.

Usage:
    chiplet_to_kicad_sch.py --chiplet design.chiplet \\
        --pins U1=pins/U1.json --pins U2=pins/U2.json \\
        --out pnr/sys.draft.kicad_sch --report pnr/schematic.json \\
        [--setup cosim.setup.json --setup-spec cosim_setup_symbols.json] [--check]
    chiplet_to_kicad_sch.py --chiplet design.chiplet --setup-only \\
        --setup cosim.setup.json --setup-spec cosim_setup_symbols.json \\
        --out pnr/sys.draft.kicad_sch --report pnr/schematic.json

    chiplet_to_kicad_sch.py --chiplet given.chiplet \\
        --pins U1=pins/U1.json --pins U2=pins/U2.json --handout sheet_first/
    chiplet_to_kicad_sch.py --handout sheet_first/ \\
        --dies sheet_first/dies/DRIVER --dies sheet_first/dies/ECHO --ext-pad-um 80

Exit status: 0 written (and, with --check, the check did not find a
difference), 3 refused, 4 written but the check found a difference or could
not run. The report is written in every one of those cases. ``--handout``
writes no report: 0 written, 3 refused, with the refusals on stderr.
"""

import argparse
import hashlib
import json
import math
import os
import re
import sys
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import sexpr_io as sx
from _paths import atomic_write
from sexpr import sanitize_sexpr_token
from _vendor.chiplet_format_io.footprint_ids import (
    FootprintIdError, die_footprint, ext_pad_footprint)
from _vendor.chiplet_format_io.sheet_names import SHEET_NAME_WORDS, is_sheet_name

NS = uuid.UUID("5e0c9a41-7b2d-5f3e-8a61-c4d2b9e07f13")   # fixed: stable output
GENERATOR = "chiplet_to_kicad_sch"

# KiCad 9.0 schematic format. Newer KiCad (the 9.99 fork in the tools image)
# reads it; a 9.0 install could not open a 9.99-versioned file. The one
# exception is a handout die with tied rails: its symbol states
# duplicate_pin_numbers_are_jumpers, which 9.0 cannot parse, so that library
# and any sheet caching the symbol open only in the tools image's KiCad.
SCH_VERSION = "20250114"
GENERATOR_VERSION = "9.0"

REPORT_FORMAT = "gds2kicad.system-schematic"
# Quoted "MAJOR.MINOR", the version policy of the .chiplet format family.
REPORT_VERSION = "1.3"

IDENTITY_PROPERTY = "CHIPLET_COMPONENT"
# A co-simulation setup symbol carries this field, its kind as the value. It
# is no component: connectivity reads leave it and its pins out.
SETUP_MARKER = "chiplet_cosim_setup"
BASE_DIGEST_PREFIX = "chiplet sha256 "
LIB = "chiplet"
# The system symbols designer sheets draw with; the draft sheet draws a carrier's
# pads with their EXT_PAD too.
SYSTEM_LIB = "chiplet_system"
EXT_PAD = "EXT_PAD"
# The handout: a symbol library the student places dies from on a blank sheet.
HANDOUT_LIB = "chiplet_dies"
HANDOUT_LIB_FILE = f"{HANDOUT_LIB}.kicad_sym"
LIB_TABLE = "sym-lib-table"
SYM_LIB_VERSION = "20241209"
PRO_VERSION = 3

EXIT_WRITTEN = 0
EXIT_REFUSED = 3
EXIT_CHECK = 4

# Names that are identities (component id, terminal, net) must reach KiCad's
# netlist unchanged. KiCad gives meaning to braces and brackets (bus and text
# markup), '~' (overbar), '/' (sheet paths), whitespace and quotes, so anything
# outside a plain set is refused rather than rewritten. A terminal or net name
# follows chiplet_format_io.sheet_names (is_sheet_name: plain, with [N]
# subscripts), the rule chiplet-spec's sheet imports read back with. A
# component id, top cell or project name is that rule without subscripts
# (_is_ident).
#: How a refusal states the rule of _is_ident.
IDENT_RULE = "plain [A-Za-z0-9_.+-] name"

# KiCad pin electrical types; the pin list's "type" must be one of them.
PIN_TYPES = ("input", "output", "bidirectional", "tri_state", "passive", "free",
             "unspecified", "power_in", "power_out", "open_collector",
             "open_emitter", "no_connect")
PIN_SIDES = ("left", "right", "top", "bottom")

# A carrier pad's pin type, from its io_class (a closed vocabulary of the
# .chiplet format). Every class is a bare pad, so passive; a class outside the
# table is refused rather than given a default.
IO_CLASS_PIN_TYPES = {"wire_bond": "passive", "flipped_bump": "passive",
                      "tsv_bump": "passive"}
CARRIER_PINS_FORMAT = "gds2kicad.carrier-pins"
CARRIER_PINS_VERSION = "1.0"

# Layout, in mm. Every coordinate lands on the 1.27 mm schematic grid.
GRID = 2.54
CHAR_W = 1.27              # generous width of one 1.27 mm glyph
NAME_OFFSET = 1.016        # pin name inset from the body edge
WIRE = 2.54                # pin end to label anchor
MARGIN = 25.4
GAP = 12.7
TITLE_BLOCK_H = 38.1
LABEL_ROOM = 4             # glyphs beyond the port name kept free for a label
POWER_TYPES = ("power_in", "power_out")
_FONT = ["effects", ["font", ["size", "1.27", "1.27"]]]
_PAPERS = (("A4", 297, 210), ("A3", 420, 297), ("A2", 594, 420),
           ("A1", 841, 594), ("A0", 1189, 841))


def uid(*parts) -> str:
    """A uuid that depends only on what it names, so reruns are byte-stable."""
    return str(uuid.uuid5(NS, "|".join(str(p) for p in parts)))


def _q(value) -> sx.Quoted:
    return sx.Quoted(sanitize_sexpr_token(value))


def _up(value: float, step: float = GRID) -> float:
    return math.ceil(round(value / step, 6)) * step


class SchematicRefused(Exception):
    """The inputs cannot be drawn verbatim. ``refusals`` lists every reason."""

    def __init__(self, refusals: List[dict]):
        self.refusals = refusals
        super().__init__("; ".join(r["message"] for r in refusals))


def _refusal(code: str, message: str, **where) -> dict:
    return {"code": code, "message": message, **where}


# ------------------------------------------------------------------- model

@dataclass(frozen=True)
class Terminal:
    name: str       # pin number, and the .chiplet connection pin
    port: str       # pin name shown on the body
    type: str
    side: str
    footprint: str = ""     # a carrier pad's EXT_PAD footprint; a die pin has none


@dataclass
class Die:
    id: str
    top_cell: str
    terminals: List[Terminal]
    kind: str = "die"           # the .chiplet type; a carrier's is not "die"
    footprint: str = ""         # a die's ``die_footprint``; a carrier's pads hold theirs


@dataclass
class System:
    name: str
    dies: List[Die]             # every drawn component, carriers included
    nets: List[Tuple[str, List[Tuple[str, str]]]]   # .chiplet order
    carrier_pins: Dict[str, list] = field(default_factory=dict)  # id -> pin list

    def references(self) -> List[str]:
        """Every Reference the sheet and its read-back use: each component's
        id, and each carrier pad's id, the Reference of its EXT_PAD."""
        return [d.id for d in self.dies] + [t.name for d in self.dies if d.kind != "die"
                                            for t in d.terminals]


def _is_ident(value) -> bool:
    return is_sheet_name(value) and "[" not in value


def load_chiplet(path) -> Tuple[dict, str]:
    """Read a .chiplet; returns (document, sha256 of its bytes)."""
    try:
        import yaml
    except ImportError as exc:                       # pragma: no cover
        raise SystemExit("chiplet_to_kicad_sch needs PyYAML to read a .chiplet") from exc
    raw = Path(path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        doc = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise SchematicRefused([_refusal(
            "malformed_chiplet", f"{path} is not a readable .chiplet: {exc}")])
    if not isinstance(doc, dict):
        raise SchematicRefused([_refusal(
            "malformed_chiplet", f"{path} does not hold a .chiplet mapping")])
    return doc, digest


def load_pin_list(path) -> list:
    """The ``pins`` array of a pin list (pins.json), unvalidated."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SchematicRefused([_refusal(
            "malformed_pin_list", f"cannot read pin list {path}: {exc}")])
    pins = doc.get("pins") if isinstance(doc, dict) else None
    if not isinstance(pins, list):
        raise SchematicRefused([_refusal(
            "malformed_pin_list", f"pin list {path} has no 'pins' array")])
    return pins


def _terminals(inst: str, pins: list, refusals: List[dict],
               at: Tuple[str, str] = ("center_x_dbu", "center_y_dbu"),
               repeats: bool = True):
    """(terminals, terminal -> order key, names refused); bad pins become
    refusals, and a net reaching a refused pin is not refused a second time.
    ``at`` names the keys holding a pin's position.

    A name the list repeats is one pad of a rail: it is one terminal, drawn
    once, and the pin list keeps how many pads it has. Its entries must agree
    on the pin type, or it is refused. ``repeats`` False refuses any repeat."""
    out, seen, refused = [], {}, set()
    for i, pin in enumerate(pins):
        name = pin.get("name") if isinstance(pin, dict) else None
        if not is_sheet_name(name):
            refusals.append(_refusal(
                "unmappable_name",
                f"pin list of {inst}: pin #{i} name {name!r} is not a {SHEET_NAME_WORDS}; "
                f"KiCad would not carry it verbatim",
                component=inst, field="terminal", value=name))
            if isinstance(name, str):
                refused.add(name)
            continue
        if name in refused:
            continue
        if name in seen and not repeats:
            refusals.append(_refusal(
                "duplicate_terminal", f"pin list of {inst} lists {name} twice",
                component=inst, terminal=name))
            continue
        ptype = pin.get("type", "passive")
        if name in seen:
            if ptype != seen[name]:
                refusals.append(_refusal(
                    "conflicting_terminal",
                    f"pin list of {inst} lists {name} as {seen[name]!r} and as "
                    f"{ptype!r}; the pads of one terminal take one pin type",
                    component=inst, terminal=name, value=ptype))
                refused.add(name)
                out = [(t, k) for t, k in out if t.name != name]
            continue
        seen[name] = ptype
        if ptype not in PIN_TYPES:
            refusals.append(_refusal(
                "unknown_pin_type",
                f"pin list of {inst}: {name} has type {ptype!r}, not a KiCad "
                f"pin type ({', '.join(PIN_TYPES)})",
                component=inst, terminal=name, value=ptype))
            refused.add(name)
            continue
        side = pin.get("side", "left")
        if side not in PIN_SIDES:
            refusals.append(_refusal(
                "unknown_pin_side",
                f"pin list of {inst}: {name} has side {side!r}, not one of "
                f"{', '.join(PIN_SIDES)}",
                component=inst, terminal=name, value=side))
            refused.add(name)
            continue
        port = pin.get("port")
        port = port if isinstance(port, str) and port.strip() else name
        out.append((Terminal(name, port, ptype, side), _order_key(pin, side, i, at)))
    return [t for t, _ in out], {t.name: k for t, k in out}, refused


def _order_key(pin: dict, side: str, index: int, at: Tuple[str, str]):
    """The order terminals are listed in, the symbol's own and the pin list's:
    by side, where the pad sits along it (left and right top to bottom, top
    and bottom left to right). List order breaks ties and stands in for a pad
    with no centre. The drawing orders its columns itself."""
    x, y = pin.get(at[0]), pin.get(at[1])
    if not all(_is_num(v) for v in (x, y)):
        return (1, 0, index)
    return (0, -y if side in ("left", "right") else x, index)


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _xy(point) -> Optional[Tuple[float, float]]:
    if isinstance(point, dict) and _is_num(point.get("x")) and _is_num(point.get("y")):
        return float(point["x"]), float(point["y"])
    return None


def _carrier_pins(cid: str, comp: dict, pads: list, refusals: List[dict]):
    """A carrier's pin list, one pin per io pad, and the pad ids refused.

    The pin is named by the pad id and typed by its io_class. Its side is where
    the pad sits on the carrier: pads share the components' frame, where a
    component's position is its centre, so the offset from that centre, scaled
    by the carrier's size, picks the nearest edge. A carrier without a usable
    centre or size falls back to the pads' own bounding box. The pad's io_pads
    net rides along as ``net``; the caller resolves it against the netlist."""
    points = [_xy(p.get("position")) if isinstance(p, dict) else None for p in pads]
    known = [xy for xy in points if xy is not None]
    centre = _xy(comp.get("position")) if comp.get("anchor", "bbox_center") == "bbox_center" else None
    dims = comp.get("dimensions") if isinstance(comp.get("dimensions"), dict) else {}
    w, h = dims.get("width"), dims.get("height")
    if centre is None or not (_is_num(w) and _is_num(h) and w > 0 and h > 0):
        if known:
            xs, ys = [x for x, _ in known], [y for _, y in known]
            centre = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
            w, h = max(xs) - min(xs) or 1.0, max(ys) - min(ys) or 1.0
        else:
            centre, w, h = (0.0, 0.0), 1.0, 1.0

    pins, refused = [], set()
    for i, (pad, xy) in enumerate(zip(pads, points)):
        if not isinstance(pad, dict):
            refusals.append(_refusal(
                "malformed_chiplet", f"io pad #{i} of {cid} is not a mapping: {pad!r}",
                component=cid))
            continue
        name, io_class = pad.get("id"), pad.get("io_class")
        if io_class not in IO_CLASS_PIN_TYPES:
            refusals.append(_refusal(
                "unknown_io_class",
                f"io pad {name!r} of {cid} has io_class {io_class!r}, not one of "
                f"{', '.join(IO_CLASS_PIN_TYPES)}, so it has no pin type",
                component=cid, terminal=name, value=io_class))
            if isinstance(name, str):
                refused.add(name)
            continue
        side = "left"
        if xy is not None:
            dx, dy = xy[0] - centre[0], xy[1] - centre[1]
            if abs(dx) * h >= abs(dy) * w:
                side = "right" if dx > 0 else "left"
            else:
                side = "top" if dy > 0 else "bottom"
        pins.append({"name": name, "port": name, "type": IO_CLASS_PIN_TYPES[io_class],
                     "side": side, "io_class": io_class, "layer": pad.get("layer"),
                     "x_um": xy[0] if xy else None, "y_um": xy[1] if xy else None,
                     "net": pad.get("net")})
    return pins, refused


def _pad_footprints(cid: str, pads: list, refusals: List[dict]) -> Dict[str, str]:
    """Each io pad's EXT_PAD footprint by pad id, ``ext_pad_footprint`` of its size: the
    footprint the board export draws the pad with. A pad whose size is missing,
    not square or not a whole number of um is refused, since no footprint names
    it."""
    out = {}
    for pad in pads:
        if not isinstance(pad, dict) or not isinstance(pad.get("id"), str):
            continue        # refused with the pin list
        name, size = pad["id"], pad.get("size")
        x, y = (size.get("x"), size.get("y")) if isinstance(size, dict) else (None, None)
        if not (_is_num(x) and _is_num(y) and x == y and math.isfinite(x)
                and float(x) == int(x)):
            refusals.append(_refusal(
                "io_pad_size",
                f"io pad {name} of {cid} has size {size!r}; its EXT_PAD footprint "
                f"needs a square pad of a whole number of um", component=cid,
                terminal=name, value=size))
            continue
        try:
            out[name] = ext_pad_footprint(int(x))
        except FootprintIdError as exc:
            refusals.append(_refusal("io_pad_size", f"io pad {name} of {cid}: {exc}",
                                     component=cid, terminal=name, value=size))
    return out


def build_system(chiplet: dict, pin_lists: Dict[str, list], draw_pads: bool = True) -> System:
    """Resolve the .chiplet dies and nets against the pin lists, or raise
    SchematicRefused with every reason the result could not be drawn verbatim.
    ``draw_pads`` False is for a caller that draws no carrier pad (the handout): a
    pad then needs no footprint and no Reference of its own."""
    refusals: List[dict] = []
    components = chiplet.get("components") or []
    if not isinstance(components, list):
        raise SchematicRefused([_refusal(
            "malformed_chiplet", "'components' is not a list")])

    kinds: Dict[str, str] = {}
    dies: List[Die] = []
    refused_terms: Dict[str, set] = {}
    carriers: Dict[str, Optional[list]] = {}    # id -> pin list, None if refused
    for comp in components:
        cid = comp.get("id") if isinstance(comp, dict) else None
        if not isinstance(cid, str) or not cid:
            refusals.append(_refusal(
                "malformed_chiplet", f"a component has no id: {comp!r}"))
            continue
        if cid in kinds:
            refusals.append(_refusal(
                "duplicate_component", f"component id {cid} appears twice",
                component=cid))
            continue
        kinds[cid] = str(comp.get("type", ""))
        if kinds[cid] != "die":
            io_pads = comp.get("io_pads")
            if io_pads is None or io_pads == []:
                continue
            carriers[cid] = None
            if not isinstance(io_pads, list):
                refusals.append(_refusal(
                    "malformed_chiplet", f"io_pads of {cid} is not a list",
                    component=cid))
                continue
            if not _is_ident(cid):
                refusals.append(_refusal(
                    "unmappable_name",
                    f"carrier id {cid!r} is not a {IDENT_RULE}, so KiCad would not "
                    f"carry it verbatim as a Reference",
                    component=cid, field="component", value=cid))
                continue
            plist, bad = _carrier_pins(cid, comp, io_pads, refusals)
            terminals, order, refused_terms[cid] = _terminals(
                cid, plist, refusals, at=("x_um", "y_um"), repeats=False)
            refused_terms[cid] |= bad
            terminals.sort(key=lambda t: order[t.name])
            if draw_pads:
                footprints = _pad_footprints(cid, io_pads, refusals)
                terminals = [replace(t, footprint=footprints.get(t.name, ""))
                             for t in terminals]
            top_cell = comp.get("top_cell")
            dies.append(Die(cid, top_cell if isinstance(top_cell, str) and top_cell
                            else cid, terminals, kinds[cid]))
            carriers[cid] = plist
            continue
        if not _is_ident(cid):
            refusals.append(_refusal(
                "unmappable_name",
                f"die id {cid!r} is not a {IDENT_RULE}, so KiCad would not carry "
                f"it verbatim as a Reference",
                component=cid, field="component", value=cid))
            continue
        if cid in pin_lists and pin_lists[cid] is None:
            continue    # its pin list could not be read; already refused
        if cid not in pin_lists:
            refusals.append(_refusal(
                "no_pin_list",
                f"die {cid} has no pin list (--pins {cid}=...), so its "
                f"terminals are unknown and it cannot be drawn",
                component=cid))
            continue
        terminals, order, refused_terms[cid] = _terminals(cid, pin_lists[cid], refusals)
        terminals.sort(key=lambda t: order[t.name])
        cell = comp.get("top_cell")
        if not isinstance(cell, str) or not cell:
            refusals.append(_refusal(
                "unmappable_name", f"die {cid} states no top_cell, so its symbol cannot "
                f"name the footprint the board draws for it", component=cid,
                field="top_cell", value=cell))
            continue
        try:
            footprint = die_footprint(cell)
        except FootprintIdError as exc:
            refusals.append(_refusal(
                "unmappable_name", f"die {cid}: {exc}, so its symbol cannot name the "
                f"footprint the board draws for it", component=cid, field="top_cell",
                value=cell))
            continue
        dies.append(Die(cid, cell, terminals, footprint=footprint))

    for inst in sorted(pin_lists):
        if kinds.get(inst) != "die":
            what = f"a {kinds[inst]!r} component" if inst in kinds else "not a component"
            refusals.append(_refusal(
                "pin_list_not_a_die",
                f"--pins names {inst}, which is {what} of the .chiplet; only a "
                f"die takes a pin list, a carrier's pins are its io_pads",
                component=inst))

    terminals_of = {d.id: {t.name for t in d.terminals} for d in dies}
    netlist = chiplet.get("netlist") or {}
    raw_nets = netlist.get("nets") if isinstance(netlist, dict) else None
    if raw_nets is None:
        raw_nets = []
    if not isinstance(raw_nets, list):
        raise SchematicRefused(refusals + [_refusal(
            "malformed_chiplet", "'netlist.nets' is not a list")])

    nets: List[Tuple[str, List[Tuple[str, str]]]] = []
    net_names = set()
    bad_net_names = set()
    owner: Dict[Tuple[str, str], str] = {}
    for n in raw_nets:
        name = n.get("name") if isinstance(n, dict) else None
        if not is_sheet_name(name):
            refusals.append(_refusal(
                "unmappable_name",
                f"net name {name!r} is not a {SHEET_NAME_WORDS}; KiCad would not "
                f"carry it verbatim as a label",
                field="net", value=name))
            if isinstance(name, str):
                bad_net_names.add(name)
            continue
        if name in net_names:
            refusals.append(_refusal(
                "duplicate_net", f"net {name} appears twice", net=name))
            continue
        net_names.add(name)
        conns = n.get("connections") or []
        if not isinstance(conns, list) or not conns:
            refusals.append(_refusal(
                "empty_net",
                f"net {name} has no connections; a schematic cannot hold a "
                f"net with no pin, so it would be lost", net=name))
            continue
        endpoints: List[Tuple[str, str]] = []
        for c in conns:
            comp = c.get("component") if isinstance(c, dict) else None
            pin = c.get("pin") if isinstance(c, dict) else None
            if not isinstance(comp, str) or not isinstance(pin, str):
                refusals.append(_refusal(
                    "malformed_connection",
                    f"net {name} has a connection without component and pin: {c!r}",
                    net=name))
                continue
            if comp not in kinds:
                refusals.append(_refusal(
                    "unknown_component",
                    f"net {name} connects {comp}.{pin}, and {comp} is not a "
                    f"component of the .chiplet", net=name, component=comp,
                    terminal=pin))
                continue
            if kinds[comp] != "die" and comp not in carriers:
                refusals.append(_refusal(
                    "non_die_endpoint",
                    f"net {name} connects {comp}.{pin} on a {kinds[comp]!r} "
                    f"component with no io_pads; only die terminals and carrier "
                    f"pads are drawn", net=name, component=comp, terminal=pin))
                continue
            if comp not in terminals_of:
                continue    # the component itself was refused above
            if pin in refused_terms[comp]:
                continue    # the pin itself was refused above
            if pin not in terminals_of[comp]:
                refusals.append(_refusal(
                    "unknown_terminal",
                    f"net {name} connects {comp}.{pin}, and {pin} is not in "
                    f"the pin list of {comp}", net=name, component=comp,
                    terminal=pin))
                continue
            key = (comp, pin)
            if key in endpoints:
                refusals.append(_refusal(
                    "duplicate_endpoint",
                    f"net {name} lists {comp}.{pin} twice", net=name,
                    component=comp, terminal=pin))
                continue
            if key in owner:
                refusals.append(_refusal(
                    "terminal_in_two_nets",
                    f"{comp}.{pin} is on net {owner[key]} and on net {name}; "
                    f"KiCad would merge the two", net=name, component=comp,
                    terminal=pin, other_net=owner[key]))
                continue
            owner[key] = name
            endpoints.append(key)
        nets.append((name, endpoints))

    # A carrier pad the netlist does not connect sits on its io_pads net; one
    # it does connect must agree with that net. Which of the two fields wins
    # when they disagree is not settled by the format, so that is refused.
    index = {name: i for i, (name, _) in enumerate(nets)}
    for cid, plist in carriers.items():
        for pin in plist or []:
            pad, pad_net = pin["name"], pin["net"]
            key = (cid, pad)
            if pad in refused_terms[cid] or pad not in terminals_of[cid]:
                continue
            if pad_net is not None and pad_net != "":
                if not is_sheet_name(pad_net):
                    if pad_net not in bad_net_names:
                        refusals.append(_refusal(
                            "unmappable_name",
                            f"io pad {pad} of {cid} has net {pad_net!r}, not a "
                            f"{SHEET_NAME_WORDS}; KiCad would not carry it verbatim as "
                            f"a label", component=cid, terminal=pad,
                            field="net", value=pad_net))
                elif key in owner and owner[key] != pad_net:
                    refusals.append(_refusal(
                        "carrier_net_conflict",
                        f"io pad {pad} of {cid} is on net {owner[key]} in the "
                        f"netlist and on net {pad_net} in io_pads",
                        component=cid, terminal=pad, net=owner[key],
                        other_net=pad_net))
                elif key not in owner:
                    owner[key] = pad_net
                    if pad_net not in index:
                        index[pad_net] = len(nets)
                        nets.append((pad_net, []))
                    nets[index[pad_net]][1].append(key)
            pin["net"] = owner.get(key)

    # A pad is drawn as a symbol of its own, its Reference the pad id, so that
    # id may name no component and no other pad.
    named: Dict[str, str] = {cid: f"component {cid}" for cid in kinds}
    for d in dies:
        if d.kind == "die" or not draw_pads:
            continue
        for t in d.terminals:
            if t.name in named:
                refusals.append(_refusal(
                    "reference_taken",
                    f"io pad {t.name} of {d.id} is drawn as a symbol named by its id, "
                    f"which {named[t.name]} already names", component=d.id,
                    terminal=t.name))
            else:
                named[t.name] = f"io pad {t.name} of {d.id}"

    if refusals:
        raise SchematicRefused(refusals)
    return System(system_name(chiplet), dies, nets,
                  {cid: plist for cid, plist in carriers.items() if plist is not None})


def system_name(chiplet: dict) -> str:
    assembly = chiplet.get("assembly") if isinstance(chiplet.get("assembly"), dict) else {}
    return str(assembly.get("name") or "")


def expected_connectivity(system: System) -> Tuple[Dict[str, frozenset], frozenset]:
    """(net name -> {(component, terminal)}, terminals on no net)."""
    nets = {name: frozenset(eps) for name, eps in system.nets}
    used = set().union(*nets.values()) if nets else set()
    free = frozenset((d.id, t.name) for d in system.dies for t in d.terminals
                     if (d.id, t.name) not in used)
    return nets, free


# ------------------------------------------------------------------ layout

@dataclass
class _Shape:
    """One die symbol's geometry, symbol-local (KiCad library frame, y up). A
    carrier's pad column (``_pad_column``) uses ``pins`` and ``ext`` alone."""
    half_w: float
    half_h: float
    pin_len: float
    pins: Dict[str, Tuple[float, float, int]]     # terminal -> (x, y, angle)
    ext: Dict[str, float]                         # side -> envelope from origin


def _label_len(name: str) -> float:
    return len(name) * CHAR_W + 3.81


def _natural(text: str):
    """Sort key that reads digit runs as numbers: d2d_f2 before d2d_f10."""
    return tuple((int(p), "") if p.isdigit() else (-1, p)
                 for p in re.split(r"(\d+)", text))


def _columns(die: Die, inner: Optional[str], net_of: Dict[Tuple[str, str], str],
             nets: Dict[str, frozenset], die_ids: frozenset) -> Dict[str, List[Terminal]]:
    """The pins of each side of the symbol, top to bottom.

    Every pin sits on the left or the right, so every name reads horizontally
    on a printed sheet. A die's link pins face its neighbour on the ``inner``
    side: a pin on no net yet, or on a net that joins it to other dies and to
    nothing else. Power pins, carrier pads and nets of this die alone take the
    other side. A column runs in port order (``_port_order``), so two dies with
    the same port names line up row for row."""
    outer = "left" if inner == "right" else "right"
    cols: Dict[str, List[Terminal]] = {"left": [], "right": []}
    for t in die.terminals:
        net = net_of.get((die.id, t.name))
        comps = {c for c, _ in nets[net]} if net is not None else set()
        link = t.type not in POWER_TYPES and (
            net is None or (len(comps) > 1 and comps <= die_ids))
        cols[inner if link else outer].append(t)
    for items in cols.values():
        items.sort(key=_port_order)
    return cols


def _port_order(t: Terminal):
    """Port order, numbers read as numbers."""
    return _natural(t.port.casefold()), _natural(t.port), _natural(t.name)


def _shape(die: Die, cols: Dict[str, List[Terminal]],
           net_of: Dict[Tuple[str, str], str], inner: Optional[str] = None,
           rows: int = 0, reference: Optional[str] = None) -> _Shape:
    """The symbol for ``cols``. Pins fill each side from the top, and ``rows``
    sets the least height, so dies of one row put their n-th pins on one line.
    ``reference`` is the Reference the body makes room for, the die id unless
    given.
    The inner side keeps room for a label on every pin, the ones still open
    included, since that is where the nets get drawn."""
    longest = max((len(t.name) for t in die.terminals), default=1)
    pin_len = max(GRID, _up(longest * CHAR_W + 1.27))
    names = max((len(t.port) for items in cols.values() for t in items), default=0) * CHAR_W
    # Names run inward from each edge; the Reference and Value sit in the
    # middle, so the body also holds the longer of the two between them.
    ref = die.id if reference is None else reference
    centre = max(GRID, max(len(die.top_cell), len(ref)) * CHAR_W / 2 + GRID / 2)
    half_w = max(2 * GRID, _up(names + NAME_OFFSET + centre))
    n = max([rows] + [len(items) for items in cols.values()])
    half_h = max(2 * GRID, _up((n + 1) * GRID / 2))

    pins: Dict[str, Tuple[float, float, int]] = {}
    ext = {"top": half_h, "bottom": half_h}
    for side, items in cols.items():
        for i, t in enumerate(items):
            y = half_h - GRID * (1 + i)
            pins[t.name] = ((-half_w - pin_len, y, 0) if side == "left"
                            else (half_w + pin_len, y, 180))
        labels = [_label_len(net_of[(die.id, t.name)]) for t in items
                  if (die.id, t.name) in net_of]
        if side == inner:
            labels += [_label_len(t.port) + LABEL_ROOM * CHAR_W for t in items]
        reach = WIRE + max(labels) if labels else (GRID if items else 0)
        ext[side] = _up(half_w + (pin_len if items else 0) + reach)
    return _Shape(half_w, half_h, pin_len, pins, ext)


# A carrier's pads, one EXT_PAD each, stack this far apart: the body and the
# Reference above it. Each pad's symbol sits PAD_OFFSET right of its pin end.
PAD_PITCH = 3 * GRID
PAD_OFFSET = 2 * GRID


def _pad_column(die: Die, pads: List[Terminal],
                net_of: Dict[Tuple[str, str], str]) -> _Shape:
    """A carrier drawn as its pads: one EXT_PAD per terminal, stacked in the
    order of ``pads``, each pin on its left with the net's label beyond it.
    ``pins`` holds each pad's pin end and ``ext`` the column's envelope."""
    half_h = _up(max(len(pads), 1) * PAD_PITCH / 2)
    pins = {t.name: (-PAD_OFFSET, half_h - PAD_PITCH / 2 - PAD_PITCH * i, 0)
            for i, t in enumerate(pads)}
    labels = [_label_len(net_of[(die.id, t.name)]) for t in die.terminals
              if (die.id, t.name) in net_of]
    longest = max((len(t.name) for t in die.terminals), default=1)
    ext = {"top": half_h, "bottom": half_h,
           "left": _up(PAD_OFFSET + WIRE + (max(labels) if labels else GRID)),
           "right": _up(max(GRID, longest * CHAR_W / 2))}
    return _Shape(GRID, half_h, GRID, pins, ext)


def _place(dies: List[Die], shapes: Dict[str, _Shape], rows=None):
    """Symbol origins (sheet frame, y down), and the page size that holds them
    plus the title block. ``rows`` lists the dies of each row, top to bottom;
    without it, rows of about sqrt(n) dies."""
    if rows is None:
        cols = max(1, math.ceil(math.sqrt(len(dies))))
        rows = [dies[r:r + cols] for r in range(0, len(dies), cols)]
    origins: Dict[str, Tuple[float, float]] = {}
    y_cursor, page_w = MARGIN, 0.0
    for row in (r for r in rows if r):
        top = max(shapes[d.id].ext["top"] for d in row)
        bottom = max(shapes[d.id].ext["bottom"] for d in row)
        y = y_cursor + top
        x_cursor = MARGIN
        for d in row:
            s = shapes[d.id]
            x = x_cursor + s.ext["left"]
            origins[d.id] = (x, y)
            x_cursor = x + s.ext["right"] + GAP
        page_w = max(page_w, x_cursor - GAP + MARGIN)
        y_cursor = y + bottom + GAP
    page_h = y_cursor - GAP + MARGIN + TITLE_BLOCK_H
    return origins, page_w, page_h


def _paper(width: float, height: float) -> list:
    for name, w, h in _PAPERS:
        if width <= w and height <= h:
            return ["paper", _q(name)]
    return ["paper", _q("User"), sx.num(_up(width, 10)), sx.num(_up(height, 10))]


# --------------------------------------------------------------- schematic

def _symbol_names(dies: List[Die], shapes: Dict[str, _Shape]) -> Dict[str, str]:
    """Library symbol name per die: its top cell, shared by dies whose symbol
    is identical, suffixed with the die id when the same cell draws two ways."""
    names: Dict[str, str] = {}
    drawn: Dict[str, List[Tuple[str, tuple]]] = {}     # base -> [(name, body)]
    for d in dies:
        base = d.top_cell if _is_ident(d.top_cell) else d.id
        body = (tuple(d.terminals), shapes[d.id].pins, shapes[d.id].half_w,
                shapes[d.id].half_h)
        ways = drawn.setdefault(base, [])
        name = next((n for n, b in ways if b == body), None)
        if name is None:
            name = base if not ways else f"{base}_{d.id}"
            ways.append((name, body))
        names[d.id] = name
    return names


def _prop(name, value, at=(0, 0), hide=False, justify=None, show_name=False):
    node = ["property", _q(name), _q(value),
            ["at", sx.num(at[0]), sx.num(at[1]), "0"]]
    if show_name:
        node.append(["show_name", "yes"])
    if hide:
        node.append(["hide", "yes"])
    effects = list(_FONT)
    if justify:
        effects.append(["justify", justify])
    node.append(effects)
    return node


def _lib_symbol(name: str, die: Die, shape: _Shape, lib: Optional[str] = LIB,
                footprint: str = "", fields: Tuple[list, ...] = (),
                jumpers: bool = False) -> list:
    """One die's library symbol. ``fields`` are properties after KiCad's own.
    ``jumpers`` sets ``duplicate_pin_numbers_are_jumpers``, written only when
    set, since a KiCad before that token cannot read it."""
    pins = []
    for t in die.terminals:
        x, y, angle = shape.pins[t.name]
        pins.append(["pin", t.type, "line",
                     ["at", sx.num(x), sx.num(y), str(angle)],
                     ["length", sx.num(shape.pin_len)],
                     ["name", _q(t.port), _FONT],
                     ["number", _q(t.name), _FONT]])
    return ["symbol", _q(f"{lib}:{name}" if lib else name),
            ["pin_names", ["offset", sx.num(NAME_OFFSET)]],
            ["exclude_from_sim", "no"], ["in_bom", "yes"], ["on_board", "yes"],
            *([["duplicate_pin_numbers_are_jumpers", "yes"]] if jumpers else []),
            _prop("Reference", "U", (0, 1.27)),
            _prop("Value", name, (0, -1.27)),
            _prop("Footprint", footprint, hide=True),
            _prop("Datasheet", "", hide=True),
            _prop("Description", f"chiplet {die.kind}", hide=True),
            *fields,
            ["symbol", _q(f"{name}_0_1"),
             ["rectangle",
              ["start", sx.num(-shape.half_w), sx.num(shape.half_h)],
              ["end", sx.num(shape.half_w), sx.num(-shape.half_h)],
              ["stroke", ["width", "0.254"], ["type", "default"]],
              ["fill", ["type", "background"]]]],
            ["symbol", _q(f"{name}_1_1"), *pins],
            ["embedded_fonts", "no"]]


def ext_pad_symbol(footprint: str, name: str = EXT_PAD) -> list:
    """The system library's EXT_PAD: one passive pin "1", its sized
    ``footprint``, and an ``Edge`` left empty for the student. Designer sheets
    and the draft sheet draw an external pad with it; ``name`` is the library's
    own (``EXT_PAD``) or a sheet's cached ``chiplet_system:EXT_PAD``."""
    half = GRID
    return ["symbol", _q(name),
            ["pin_names", ["offset", sx.num(NAME_OFFSET)]],
            ["exclude_from_sim", "no"], ["in_bom", "yes"], ["on_board", "yes"],
            _prop("Reference", "J", (0, half + 1.27)),
            _prop("Value", EXT_PAD, (0, -half - 1.27)),
            _prop("Footprint", footprint, hide=True),
            _prop("Datasheet", "", hide=True),
            _prop("Edge", "", (0, -half - 3.81), show_name=True),
            ["symbol", _q(f"{EXT_PAD}_0_1"),
             ["rectangle", ["start", sx.num(-half), sx.num(half)],
              ["end", sx.num(half), sx.num(-half)],
              ["stroke", ["width", "0.254"], ["type", "default"]],
              ["fill", ["type", "background"]]]],
            ["symbol", _q(f"{EXT_PAD}_1_1"),
             ["pin", "passive", "line", ["at", sx.num(-2 * half), "0", "0"],
              ["length", sx.num(half)], ["name", _q("1"), _FONT],
              ["number", _q("1"), _FONT]]],
            ["embedded_fonts", "no"]]


def _pts(points) -> list:
    """A ``pts`` node through ``points``, each an (x, y)."""
    return ["pts", *(["xy", sx.num(x), sx.num(y)] for x, y in points)]


def _wire(a, b, *key) -> list:
    """A wire from point ``a`` to point ``b``; ``key`` names it in its uuid."""
    return ["wire", _pts((a, b)), ["stroke", ["width", "0"], ["type", "default"]],
            ["uuid", _q(uid("wire", *key))]]


# Label anchor offset from the pin end and its orientation, per side, sheet frame.
_LABEL = {"left": ((-WIRE, 0), "180", "right"),
          "right": ((WIRE, 0), "0", "left"),
          "top": ((0, -WIRE), "90", "left"),
          "bottom": ((0, WIRE), "270", "right")}


def _hookup(ex: float, ey: float, side: str, net: Optional[str], *key,
            flag: bool = True) -> list:
    """What goes on a pin end: a no-connect flag when ``net`` is None (nothing
    when ``flag`` is False), else a wire out to a global label carrying the
    net's name. ``key`` names the pin in the uuids."""
    if net is None:
        if not flag:
            return []
        return [["no_connect", ["at", sx.num(ex), sx.num(ey)],
                 ["uuid", _q(uid("nc", *key))]]]
    (dx, dy), angle, justify = _LABEL[side]
    lx, ly = ex + dx, ey + dy
    return [_wire((ex, ey), (lx, ly), *key),
            ["global_label", _q(net),
             ["shape", "passive"],
             ["at", sx.num(lx), sx.num(ly), angle],
             ["fields_autoplaced", "yes"],
             [*_FONT, ["justify", justify]],
             ["uuid", _q(uid("label", *key))],
             ["property", _q("Intersheetrefs"), sx.Quoted("${INTERSHEET_REFS}"),
              ["at", sx.num(lx), sx.num(ly), "0"],
              ["hide", "yes"],
              [*_FONT, ["justify", justify]]]]]


def schematic_node(system: System, base_sha256: str, project: str,
                   source_name: str, setup=None) -> list:
    """The sheet as a node. ``setup``, when given, draws a band below the dies:
    called as setup(sheet_uuid, project, top), it returns the band's library
    symbols, its items, the page width it needs and the page height."""
    nets, _free = expected_connectivity(system)
    net_of = {ep: name for name, eps in nets.items() for ep in eps}
    # The dies in one row in .chiplet order, the first facing right and the
    # others left; the carriers in a row below.
    row = [d for d in system.dies if d.kind == "die"]
    below = [d for d in system.dies if d.kind != "die"]
    inner = {d.id: "right" if i == 0 else "left" for i, d in enumerate(row)}
    die_ids = frozenset(inner)
    cols = {d.id: _columns(d, inner[d.id], net_of, nets, die_ids) for d in row}
    rows = max((len(items) for d in row for items in cols[d.id].values()), default=0)
    shapes = {d.id: _shape(d, cols[d.id], net_of, inner[d.id], rows) for d in row}
    # A carrier's pads read top to bottom in port order.
    stacks = {d.id: sorted(d.terminals, key=_port_order) for d in below}
    shapes.update({d.id: _pad_column(d, stacks[d.id], net_of) for d in below})
    origins, page_w, page_h = _place(system.dies, shapes, rows=[row, below])
    sym_names = _symbol_names(row, shapes)
    sheet = uid("sheet", system.name)
    band_libs, band_items = [], []
    if setup is not None:
        band_libs, band_items, band_w, page_h = setup(
            sheet, project, page_h - TITLE_BLOCK_H - MARGIN + GAP)
        page_w = max(page_w, band_w)

    lib_symbols, emitted = ["lib_symbols"], set()
    for d in row:
        if sym_names[d.id] not in emitted:
            emitted.add(sym_names[d.id])
            lib_symbols.append(_lib_symbol(sym_names[d.id], d, shapes[d.id]))
    pads = [(d, t) for d in below for t in stacks[d.id]]
    if pads:
        # The sheet caches the library symbol once, with the first pad's
        # footprint; each pad's own footprint is its instance's.
        lib_symbols.append(ext_pad_symbol(pads[0][1].footprint,
                                          name=f"{SYSTEM_LIB}:{EXT_PAD}"))
    lib_symbols += band_libs

    title = ["title_block", ["title", _q(system.name)],
             ["comment", "1", _q(BASE_DIGEST_PREFIX + base_sha256)],
             ["comment", "2", _q(f"generated from {source_name}; nets are global "
                                 f"labels, read back by the flow's edit")]]
    node = ["kicad_sch",
            ["version", SCH_VERSION],
            ["generator", _q(GENERATOR)],
            ["generator_version", _q(GENERATOR_VERSION)],
            ["uuid", _q(sheet)],
            _paper(page_w, page_h),
            title,
            lib_symbols]

    for d in row:
        s = shapes[d.id]
        ox, oy = origins[d.id]
        inst = ["symbol", ["lib_id", _q(f"{LIB}:{sym_names[d.id]}")],
                ["at", sx.num(ox), sx.num(oy), "0"],
                ["unit", "1"],
                ["exclude_from_sim", "no"], ["in_bom", "yes"],
                ["on_board", "yes"], ["dnp", "no"],
                ["uuid", _q(uid("symbol", d.id))],
                _prop("Reference", d.id, (ox, oy - 1.27)),
                _prop("Value", d.top_cell, (ox, oy + 1.27)),
                _prop("Footprint", d.footprint, (ox, oy), hide=True),
                _prop("Datasheet", "", (ox, oy), hide=True),
                _prop("Description", f"chiplet {d.kind}", (ox, oy), hide=True),
                _prop(IDENTITY_PROPERTY, d.id, (ox, oy), hide=True)]
        for t in d.terminals:
            inst.append(["pin", _q(t.name), ["uuid", _q(uid("pin", d.id, t.name))]])
        inst.append(["instances", ["project", _q(project),
                                   ["path", _q(f"/{sheet}"),
                                    ["reference", _q(d.id)], ["unit", "1"]]]])
        node.append(inst)

        # An open link pin is left bare: it is there to be wired, and KiCad's
        # list of unconnected pins is then the list still to wire.
        for side, items in cols[d.id].items():
            for t in items:
                px, py, _angle = s.pins[t.name]
                # library y up, sheet y down
                node += _hookup(ox + px, oy - py, side, net_of.get((d.id, t.name)),
                                d.id, t.name, flag=side != inner.get(d.id))

    for d, t in pads:
        ox, oy = origins[d.id]
        px, py, _angle = shapes[d.id].pins[t.name]
        node.append(_pad_instance(d.id, t.name, t.footprint,
                                  (ox + px + PAD_OFFSET, oy - py), sheet, project))
        node += _hookup(ox + px, oy - py, "left", net_of.get((d.id, t.name)),
                        d.id, t.name)

    node += band_items
    node.append(["sheet_instances", ["path", _q("/"), ["page", _q("1")]]])
    node.append(["embedded_fonts", "no"])
    return node


def _pad_instance(cid: str, pad: str, footprint: str, at: Tuple[float, float],
                  sheet: str, project: str) -> list:
    """Carrier ``cid``'s pad ``pad`` as an EXT_PAD, named by the pad id and
    carrying the carrier's identity, the footprint the board draws the pad
    with, and the uuids the carrier's pin had."""
    x, y = at
    half = GRID
    return ["symbol", ["lib_id", _q(f"{SYSTEM_LIB}:{EXT_PAD}")],
            ["at", sx.num(x), sx.num(y), "0"],
            ["unit", "1"],
            ["exclude_from_sim", "no"], ["in_bom", "yes"],
            ["on_board", "yes"], ["dnp", "no"],
            ["uuid", _q(uid("symbol", cid, pad))],
            _prop("Reference", pad, (x, y - half - 1.27)),
            _prop("Value", EXT_PAD, (x, y + half + 1.27), hide=True),
            _prop("Footprint", footprint, (x, y), hide=True),
            _prop("Datasheet", "", (x, y), hide=True),
            _prop("Edge", "", (x, y + half + 3.81), hide=True),
            _prop(IDENTITY_PROPERTY, cid, (x, y), hide=True),
            ["pin", _q("1"), ["uuid", _q(uid("pin", cid, pad))]],
            ["instances", ["project", _q(project),
                           ["path", _q(f"/{sheet}"),
                            ["reference", _q(pad)], ["unit", "1"]]]]]


def _load_pin_lists(pins: Dict[str, str]) -> Tuple[List[dict], Dict[str, Optional[list]]]:
    """(refusals, instance -> pin list); an unreadable list is None and refused."""
    refusals, pin_lists = [], {}
    for inst, path in sorted(pins.items()):
        try:
            pin_lists[inst] = load_pin_list(path)
        except SchematicRefused as exc:
            refusals += exc.refusals
            pin_lists[inst] = None
    return refusals, pin_lists


def generate(chiplet_path, pins: Dict[str, str], out_path, setup_path=None,
             spec_path=None, setup_only=False) -> dict:
    """Write the schematic for ``chiplet_path``; returns a summary. Raises
    SchematicRefused (nothing written) when it cannot be drawn verbatim. With
    ``setup_path``, a co-simulation setup projection, and ``spec_path``, the
    symbol spec it is drawn with, the system sheet also gets its setup symbols
    and every other setup sheet is written beside it (cosim_setup_sheets.py).
    ``setup_only`` writes no system sheet: the system setup symbols go on a
    setup sheet of their own, and the system has no dies."""
    if setup_only and (setup_path is None or pins):
        raise ValueError("setup_only takes a setup projection and no pin lists")
    chiplet, digest = load_chiplet(chiplet_path)
    refusals, pin_lists = _load_pin_lists(pins)
    system = None
    try:
        system = (System(system_name(chiplet), [], []) if setup_only
                  else build_system(chiplet, pin_lists))
    except SchematicRefused as exc:
        refusals += exc.refusals
    plan = None
    if setup_path is not None:
        import cosim_setup_sheets as cs     # it imports this module
        if spec_path is None:
            raise ValueError("a setup projection needs the symbol spec to draw it with")
        spec = cs.load_spec(spec_path)
        projection, errors = cs.load_projection(setup_path)
        refusals += errors
        if projection is not None:
            plan, errors = cs.plan(projection, system.references() if system else [], spec)
            refusals += errors
    out_path = Path(out_path)
    table = out_path.parent / cs.LIB_TABLE if setup_path is not None else None
    if table is not None and os.path.lexists(table) and (
            not table.is_file() or table.read_bytes() != cs.table_text().encode("utf-8")):
        refusals.append(_refusal(
            "library_table", f"{table} is there and is not the table this generator "
            f"writes; add {cs.LIB} (uri ${{KIPRJMOD}}/{cs.LIB_FILE}) to it by hand, or "
            f"move it away"))
    if refusals:
        raise SchematicRefused(refusals)

    stem = (out_path.name[:-len(".kicad_sch")] if out_path.name.endswith(".kicad_sch")
            else out_path.stem)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text, setup_sheets = None, []
    if setup_only:
        sheets = [("system", None, plan.system)] + plan.sheets
    else:
        band = None
        if plan is not None and plan.system:
            def band(sheet, project, top):
                return cs.draw(plan.system, sheet, project, "system", top)
        node = schematic_node(system, digest, stem, Path(chiplet_path).name, setup=band)
        text = sx.dumps(node) + "\n"
        sheets = plan.sheets if plan is not None else []
        if plan is not None:
            setup_sheets.append({"role": "system", "path": out_path,
                                 "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
    if plan is not None:
        for role, name, parts in sheets:
            path = out_path.parent / cs.sheet_file(stem, role, name, alone=setup_only)
            body = sx.dumps(cs.sheet_node(system.name, role, name, parts, path,
                                          Path(setup_path).name)) + "\n"
            with atomic_write(path) as f:
                f.write(body)
            setup_sheets.append({"role": role, **({cs.SHEET_KEY[role]: name} if name else {}),
                                 "path": path,
                                 "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()})
    setup_library = {}
    if plan is not None:
        for key, path, body in (("library", out_path.parent / cs.LIB_FILE, cs.library_text(spec)),
                                ("library_table", table, cs.table_text())):
            with atomic_write(path) as f:
                f.write(body)
            setup_library[key] = {"path": path,
                                  "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()}
    carrier_pins = {}
    for cid, plist in sorted(system.carrier_pins.items()):
        path = out_path.parent / f"{cid}.carrier_pins.json"
        body = json.dumps({"format": CARRIER_PINS_FORMAT, "version": CARRIER_PINS_VERSION,
                           "component": cid, "chiplet_sha256": digest, "pins": plist},
                          indent=2, sort_keys=True) + "\n"
        with atomic_write(path) as f:
            f.write(body)
        carrier_pins[cid] = (path, hashlib.sha256(body.encode("utf-8")).hexdigest())
    if text is not None:
        with atomic_write(out_path) as f:
            f.write(text)
    nets, free = expected_connectivity(system)
    return {"system": system, "chiplet_sha256": digest,
            "schematic_sha256": (hashlib.sha256(text.encode("utf-8")).hexdigest()
                                 if text is not None else None),
            "nets": nets, "unconnected": free, "carrier_pins": carrier_pins,
            "setup_sheets": setup_sheets, "setup_library": setup_library}


# ----------------------------------------------------------------- handout

def handout_symbols(system: System, chiplet: dict) -> Tuple[List[list], List[dict]]:
    """The handout's library symbols, one per top cell, and the refusals.

    The shape is the sheet's with the placement taken out: the link pins (on
    no net, or on a net that joins dies alone) sit on the right and every
    other pin on the left, whatever the die's neighbours, and every pin is
    passive. The student places a die by its top cell, so the symbol is named
    by it: a die without a plain top cell is refused, and so are two dies of
    one top cell whose symbols would differ. Carriers are not drawn."""
    nets, _free = expected_connectivity(system)
    net_of = {ep: name for name, eps in nets.items() for ep in eps}
    dies = [d for d in system.dies if d.kind == "die"]
    die_ids = frozenset(d.id for d in dies)
    cells = {c.get("id"): c.get("top_cell") for c in chiplet.get("components") or []
             if isinstance(c, dict)}
    refusals, symbols, drawn = [], [], {}
    for d in dies:
        cell = cells.get(d.id)
        if not _is_ident(cell):
            refusals.append(_refusal(
                "unmappable_name",
                f"die {d.id} has top cell {cell!r}, not a {IDENT_RULE}, so it "
                f"cannot name its library symbol",
                component=d.id, field="top_cell", value=cell))
            continue
        cols = _columns(d, "right", net_of, nets, die_ids)
        shape = _shape(d, cols, net_of, "right", reference="U?")
        flat = Die(d.id, cell, [Terminal(t.name, t.port, "passive", t.side)
                                for t in d.terminals], d.kind)
        body = (tuple(flat.terminals), shape.pins, shape.half_w, shape.half_h)
        if cell in drawn:
            if drawn[cell][1] != body:
                refusals.append(_refusal(
                    "top_cell_drawn_twice",
                    f"dies {drawn[cell][0]} and {d.id} share top cell {cell} but "
                    f"not one symbol (their pin lists, or which pins are wired, "
                    f"differ); one library symbol cannot place both",
                    component=d.id, other_component=drawn[cell][0], value=cell))
            continue
        drawn[cell] = (d.id, body)
        symbols.append(_lib_symbol(cell, flat, shape, lib=None))
    return symbols, refusals


def _table_text(*libs: Tuple[str, str, str]) -> str:
    """A ``sym-lib-table`` naming each ``(lib, file, descr)`` by ``${KIPRJMOD}``."""
    return sx.dumps(["sym_lib_table", ["version", "7"],
                     *(["lib", ["name", _q(lib)], ["type", _q("KiCad")],
                        ["uri", _q(f"${{KIPRJMOD}}/{lib_file}")], ["options", _q("")],
                        ["descr", _q(descr)]] for lib, lib_file, descr in libs)]) + "\n"


_HANDOUT_ROW = (HANDOUT_LIB, HANDOUT_LIB_FILE, "chiplet die symbols")


def _handout_table() -> str:
    return _table_text(_HANDOUT_ROW)


def symbol_library(symbols: List[list]) -> str:
    """A ``.kicad_sym`` library holding ``symbols``."""
    return sx.dumps(["kicad_symbol_lib", ["version", SYM_LIB_VERSION],
                     ["generator", _q(GENERATOR)],
                     ["generator_version", _q(GENERATOR_VERSION)], *symbols]) + "\n"


def blank_project(title: str, name: str) -> Dict[str, str]:
    """File name -> text: a project and its empty sheet, both named ``name``."""
    sheet = uid("handout", name)
    blank = sx.dumps(["kicad_sch",
                      ["version", SCH_VERSION],
                      ["generator", _q(GENERATOR)],
                      ["generator_version", _q(GENERATOR_VERSION)],
                      ["uuid", _q(sheet)],
                      ["paper", _q("A3")],
                      ["title_block", ["title", _q(title)]],
                      ["lib_symbols"],
                      ["sheet_instances", ["path", _q("/"), ["page", _q("1")]]],
                      ["embedded_fonts", "no"]]) + "\n"
    project = {"meta": {"filename": f"{name}.kicad_pro", "version": PRO_VERSION},
               "sheets": [[sheet, "Root"]], "boards": [], "text_variables": {},
               "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []}}
    return {f"{name}.kicad_pro": json.dumps(project, indent=2, sort_keys=True) + "\n",
            f"{name}.kicad_sch": blank}


def handout_files(system: System, symbols: List[list], name: str) -> Dict[str, str]:
    """File name -> text: the library, its table, a project and an empty sheet.
    Nothing depends on the host or the time, so reruns are byte-equal."""
    return {HANDOUT_LIB_FILE: symbol_library(symbols),
            LIB_TABLE: _handout_table(),
            **blank_project(system.name, name)}


def _differs(path: Path, text: str) -> bool:
    try:
        return os.path.lexists(path) and (
            not path.is_file() or path.read_bytes() != text.encode("utf-8"))
    except OSError:
        return True     # unreadable: not known to be the handout, so kept


def write_handout(out_dir, files: Dict[str, str], refusals: List[dict],
                  libs: Tuple[Tuple[str, str, str], ...]) -> dict:
    """Write ``files`` into ``out_dir``, or raise SchematicRefused with nothing
    written. ``files`` is empty when the handout cannot be drawn; the table of
    ``libs`` is still checked, so every refusal is listed at once.

    ``out_dir`` must be a directory or absent. Its ``sym-lib-table`` must be
    the one ``libs`` gives, and no other handout file in it may differ from
    what would be written (a sheet already drawn on is kept). A rerun on an
    untouched handout rewrites the same bytes. Returns file name -> sha256."""
    out_dir = Path(out_dir)
    if os.path.lexists(out_dir) and not out_dir.is_dir():
        raise SchematicRefused(refusals + [_refusal(
            "handout_dir", f"{out_dir} is there and is not a directory")])
    refusals = list(refusals)
    if _differs(out_dir / LIB_TABLE, _table_text(*libs)):
        named = " and ".join(f"{lib} (uri ${{KIPRJMOD}}/{lib_file})"
                             for lib, lib_file, _descr in libs)
        refusals.append(_refusal(
            "library_table", f"{out_dir / LIB_TABLE} is there and is not the table this "
            f"generator writes; add {named} to it by hand, or move it away"))
    for fname, text in files.items():
        if fname != LIB_TABLE and _differs(out_dir / fname, text):
            refusals.append(_refusal(
                "handout_exists", f"{out_dir / fname} is there and differs from the "
                f"handout (a sheet already drawn on?); it is not overwritten, move it "
                f"away to write a fresh handout", path=fname))
    if refusals:
        raise SchematicRefused(refusals)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SchematicRefused([_refusal(
            "handout_dir", f"cannot make the directory {out_dir}: {exc}")])
    for fname, text in files.items():
        with atomic_write(out_dir / fname) as f:
            f.write(text)
    return {fname: hashlib.sha256(text.encode("utf-8")).hexdigest()
            for fname, text in files.items()}


def handout(chiplet_path, pins: Dict[str, str], out_dir, name: Optional[str] = None) -> dict:
    """Write the schematic-first handout into ``out_dir``: the die symbol
    library, a ``sym-lib-table`` naming it by ``${KIPRJMOD}``, a project and an
    empty sheet, both named ``name`` (the .chiplet's stem unless given).
    No symbol carries an identity and no file a digest: on a blank sheet the
    designator is the identity. Returns file name -> sha256. Raises
    SchematicRefused, with nothing written, when a die cannot be drawn
    verbatim, or when ``out_dir`` holds a file of the handout that differs
    from what would be written (a sheet already drawn on, another
    ``sym-lib-table``), or is not a directory. A rerun on an untouched
    handout rewrites the same bytes."""
    chiplet, _digest = load_chiplet(chiplet_path)
    refusals, pin_lists = _load_pin_lists(pins)
    name = Path(chiplet_path).stem if name is None else name
    if not _is_ident(name):
        refusals.append(_refusal(
            "unmappable_name", f"project name {name!r} is not a {IDENT_RULE}",
            field="project", value=name))
    system, symbols, drawable = None, [], False
    try:
        system = build_system(chiplet, pin_lists, draw_pads=False)
    except SchematicRefused as exc:
        refusals += exc.refusals
    if system is not None:
        symbols, errors = handout_symbols(system, chiplet)
        refusals += errors
        drawable = not errors and _is_ident(name)
    files = handout_files(system, symbols, name) if drawable else {}
    return write_handout(out_dir, files, refusals, (_HANDOUT_ROW,))


# --------------------------------------------------------------------- cli

def _parse_pins(values: List[str]) -> Dict[str, str]:
    pins: Dict[str, str] = {}
    for value in values:
        inst, sep, path = value.partition("=")
        if not sep or not inst or not path:
            raise SystemExit(f"--pins expects INSTANCE=PATH, got {value!r}")
        if inst in pins:
            raise SystemExit(f"--pins names {inst} twice")
        pins[inst] = path
    return pins


def _report_ref(target, report_dir) -> str:
    """How the report names ``target``: relative to the report's directory
    when the two share a root below "/", else absolute. A host tree and its
    container mount then read the same; the digests carry the identity. The
    rule of the chiplet plugin's layout references; lexical, not resolved."""
    target = os.path.abspath(target)
    report_dir = os.path.abspath(report_dir)
    try:
        common = os.path.commonpath([target, report_dir])
    except ValueError:      # different drives
        return target
    if os.path.dirname(common) == common:
        return target
    return os.path.relpath(target, report_dir)


def _sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Draw a .chiplet system as a KiCad schematic: one symbol per "
                    "die, nets by name. Or write a handout for a blank sheet.")
    ap.add_argument("--chiplet", help="system description (.chiplet)")
    ap.add_argument("--pins", action="append", default=[], metavar="INSTANCE=PATH",
                    help="pin list of one die, once per die (the export's --pins)")
    ap.add_argument("--out", help="schematic to write (.kicad_sch)")
    ap.add_argument("--report", help="JSON report to write")
    ap.add_argument("--setup", metavar="PROJECTION",
                    help="co-simulation setup projection (chiplet_cosim.cosim_setup "
                         "1.0): draw its setup symbols and sheets too")
    ap.add_argument("--setup-spec", metavar="SPEC",
                    help="the co-simulation flow's symbol spec (chiplet_cosim.cosim_setup_symbols "
                         "1.0) to draw --setup with")
    ap.add_argument("--setup-only", action="store_true",
                    help="draw only the setup sheets, the system setup symbols on a "
                         "sheet of their own, for a system whose dies have no pin "
                         "lists; takes --setup, and no --pins or --check")
    ap.add_argument("--check", action="store_true",
                    help="export the netlist with kicad-cli and compare it")
    ap.add_argument("--kicad-cli", default="kicad-cli", help="kicad-cli executable")
    ap.add_argument("--handout", metavar="DIR",
                    help="write the schematic-first handout into DIR instead of a "
                         "sheet: the die symbol library, its sym-lib-table, a "
                         "project and an empty sheet; takes --pins, and no --out, "
                         "--report, --setup or --check")
    ap.add_argument("--name", help="with --handout, the project and sheet name "
                                   "(default: the .chiplet's stem, or with --dies "
                                   "the output directory's own name)")
    ap.add_argument("--dies", action="append", default=[], metavar="FOLDER",
                    help="with --handout, a die folder; repeat for each die")
    ap.add_argument("--ext-pad-um",
                    help="with --dies, the external pad edge in whole micrometres")
    args = ap.parse_args(argv)
    if args.dies or args.ext_pad_um is not None:
        import die_handout     # it draws with this module, so it is loaded late
        return die_handout.command(ap, args)
    if not args.chiplet:
        ap.error("the following arguments are required: --chiplet")
    pins = _parse_pins(args.pins)
    if not os.path.isfile(args.chiplet):
        ap.error(f"no such .chiplet: {args.chiplet}")
    if args.handout is not None:
        if args.out or args.report or args.setup or args.setup_spec or args.setup_only \
                or args.check:
            ap.error("--handout writes no sheet and no report: it takes --chiplet, "
                     "--pins and --name only")
        try:
            files = handout(args.chiplet, pins, args.handout, name=args.name)
        except SchematicRefused as exc:
            for r in exc.refusals:
                print(f"refused ({r['code']}): {r['message']}", file=sys.stderr)
            return EXIT_REFUSED
        for fname in files:
            print(os.path.join(args.handout, fname))
        return EXIT_WRITTEN
    if args.name is not None:
        ap.error("--name goes with --handout")
    if args.out is None or args.report is None:
        ap.error("--out and --report are required, unless --handout")
    if (args.setup is None) != (args.setup_spec is None):
        ap.error("--setup and --setup-spec go together")
    if args.setup_only and (args.setup is None or pins or args.check):
        ap.error("--setup-only draws no system sheet: it takes --setup and "
                 "--setup-spec, and no --pins or --check")
    if args.setup is not None:
        if not os.path.isfile(args.setup):
            ap.error(f"no such projection: {args.setup}")
        import cosim_setup_sheets as cs     # it imports this module
        try:
            cs.load_spec(args.setup_spec)
        except (OSError, ValueError) as exc:
            ap.error(f"unusable symbol spec: {exc}")

    here = os.path.dirname(os.path.abspath(args.report))
    report = {"format": REPORT_FORMAT, "version": REPORT_VERSION,
              "chiplet": _report_ref(args.chiplet, here),
              "chiplet_sha256": _sha256(args.chiplet),
              "pins": {k: {"path": _report_ref(v, here),
                           "sha256": _sha256(v) if os.path.isfile(v) else None}
                       for k, v in sorted(pins.items())}}
    code = EXIT_WRITTEN
    try:
        result = generate(args.chiplet, pins, args.out, setup_path=args.setup,
                          spec_path=args.setup_spec, setup_only=args.setup_only)
    except SchematicRefused as exc:
        report.update({"word": "refused", "schematic": None,
                       "refusals": exc.refusals})
        code = EXIT_REFUSED
        for r in exc.refusals:
            print(f"refused ({r['code']}): {r['message']}", file=sys.stderr)
    else:
        system = result["system"]
        for cid, (path, sha) in result["carrier_pins"].items():
            report["pins"][cid] = {"path": _report_ref(path, here), "sha256": sha}
        report.update({"word": "written", "schematic": None, "refusals": []})
        if not args.setup_only:
            report.update({
                "schematic": _report_ref(args.out, here),
                "schematic_sha256": result["schematic_sha256"],
                "components": [{"id": d.id, "type": d.kind, "top_cell": d.top_cell,
                                "terminals": len(d.terminals)} for d in system.dies],
                "nets": len(result["nets"]),
                "unconnected_terminals": len(result["unconnected"])})
        if args.setup is not None:
            report["setup"] = {
                "projection": {"path": _report_ref(args.setup, here),
                               "sha256": _sha256(args.setup)},
                "spec": {"path": _report_ref(args.setup_spec, here),
                         "sha256": _sha256(args.setup_spec)},
                "sheets": [{**{k: v for k, v in s.items() if k != "path"},
                            "file": _report_ref(s["path"], here)}
                           for s in result["setup_sheets"]],
                **{key: {"file": _report_ref(row["path"], here), "sha256": row["sha256"]}
                   for key, row in result["setup_library"].items()}}
        if args.check:
            import sheet_check      # it imports this module
            check = sheet_check.run_check(
                args.out, result["nets"], result["unconnected"], result["chiplet_sha256"],
                args.kicad_cli, carriers={d.id: {t.name for t in d.terminals}
                                          for d in system.dies if d.kind != "die"})
            report["check"] = check
            if check["word"] != "matches":
                code = EXIT_CHECK
                print(f"check {check['word']}: "
                      + "; ".join(check.get("differences") or
                                  [check.get("reason", "")]), file=sys.stderr)
        else:
            report["check"] = {"word": "not_run"}

    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    with atomic_write(args.report) as f:
        f.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
