#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Draw a co-simulation setup on KiCad sheets, from its projection.

The co-simulation flow projects a cosim.yaml into the tool-neutral setup form
``chiplet_cosim.cosim_setup`` 1.0 (``tool.name`` chiplet-cosim). This module
draws that projection for chiplet_to_kicad_sch.py ``--setup``:

  * the system sheet's setup symbols (simulation, models, supply, crossing,
    bench), in a band below the dies of the system sheet;
  * every other sheet of the projection as a flat sheet of its own, beside the
    system sheet and netlisted on its own: ``<stem>.die.<die>.kicad_sch``,
    ``<stem>.bench.kicad_sch`` and ``<stem>.channel.<net>.kicad_sch``. None is
    a hierarchical subsheet, whose sheet path would prefix every net name.

With ``--setup-only`` no system sheet is drawn, and the system setup symbols go
on a flat sheet of their own too, ``<stem>.system.kicad_sch``.

Beside the sheets go the symbol library ``chiplet_cosim.kicad_sym`` and a
project ``sym-lib-table`` naming it ``chiplet_cosim`` (``${KIPRJMOD}``), so a
person can place a setup symbol of any kind whose pins the spec fixes, also one
the projection does not use. Each library symbol carries its kind and one empty
field per required attribute, for the person to fill in, and is the very symbol
a sheet embeds for that kind, so KiCad sees no difference from the library. A
bench or block symbol takes its pins from its row, so the library has none:
to place one, copy an existing one. A ``sym-lib-table`` already there is
refused unless it is the one this generator writes.

A setup symbol is a symbol with a ``chiplet_cosim_setup`` field holding its
kind, one shown field per attribute, and the pins the symbol spec gives its
kind in the symbol spec ``chiplet_cosim.cosim_setup_symbols`` 1.0, the co-simulation
flow's file, which the caller passes in (``--setup-spec``); none is bundled. It is
excluded from simulation and from the BOM, and stays on the board so that
KiCad's netlist lists it. Each pin is wired to a global label carrying its
net's exact name, or carries a no-connect flag when the projection leaves it
open.

A bench or channel sheet draws each element row as a two-pin primitive whose
Reference is the element's SPICE name, so the tool netlists it under that name,
pin 1 on the first node and pin 2 on the second. Every primitive is a raw SPICE
model (Sim.Device SPICE, Sim.Params type and model, Sim.Pins 1=1 2=2) whose
model is the card's value text. KiCad writes that text into the card as drawn
only when it is digits and points; any other number (an exponent, a sign, a
unit prefix), and a value it infers or reads from a typed model, goes through
its number formatting, which keeps six significant digits. So an R, C or L
model is the value's exact plain decimal (``2.322105e-14`` as
``0.00000000000002322105``, ``1e6`` as ``1000000``) and its Value shows the
projection's text; a source's model is not a number to KiCad and is also its
Value. A value typed in e-notation with more than six significant digits comes
out rounded, which the reader refuses; the title block of a bench or channel
sheet says to type a plain decimal, or six digits at most. Only DC and pulse
sources are drawn (``dc <v>``, ``pulse(<args>)``), the forms the
shipped benches use, a pulse with two to seven arguments (V1 V2 TD TR TF PW
PER); a sin or pwl source, or a DC value with a function, is refused until
KiCad's reading of it is checked. A reference without a trailing digit is one
KiCad reports as not annotated, and annotating renames it; the sheet's title
block says so.

Anything that cannot be drawn verbatim is refused by name, every reason
collected, and nothing is written: a projection of another format or tool, a
sheet role or identity that is wrong or drawn twice, a kind on the wrong sheet
or with pins its kind does not have, a reference that is illegal or taken on
its sheet, a net, pin or attribute KiCad would not carry unchanged, an empty
attribute (it would read back as a library field left unfilled), an element
row out of shape, and a source this generator does not draw.
"""

import json
import re
from collections import namedtuple
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import sexpr_io as sx
from chiplet_to_kicad_sch import (CHAR_W, GENERATOR, GENERATOR_VERSION, GRID, MARGIN,
                                  NAME_OFFSET, SCH_VERSION, SETUP_MARKER, WIRE, _FONT,
                                  _hookup, _is_ident, _label_len, _paper, _place, _prop, _q,
                                  _refusal, _up, uid)
from sexpr import sanitize_sexpr_token

SPEC_FORMAT = "chiplet_cosim.cosim_setup_symbols"
SPEC_VERSION = "1.0"
FORM_FORMAT = "chiplet_cosim.cosim_setup"
FORM_VERSION = "1.0"
PROJECTOR = "chiplet-cosim"
LIB = "chiplet_cosim"
LIB_FILE = f"{LIB}.kicad_sym"
LIB_TABLE = "sym-lib-table"
SYM_LIB_VERSION = "20241209"

# The spec's sheet for each sheet role of the form, and the key naming a sheet.
SPEC_SHEET = {"system": "system", "die": "die", "bench": "subckt", "channel": "subckt"}
SHEET_KEY = {"die": "die", "channel": "net"}
# Fields every KiCad symbol has, and the properties KiCad adds to a netlist
# component of its own (sheet, flags, ki_* from the library): an attribute
# cannot take their names, and the setup reader leaves them out.
KICAD_FIELDS = frozenset({"reference", "value", "footprint", "datasheet", "description",
                          "sheetname", "sheetfile", "exclude_from_bom", "exclude_from_board",
                          "exclude_from_sim", "dnp"})
ELEMENT_KINDS = ("V", "I", "R", "C", "L")
PULSE_PARAMS = ("y1", "y2", "td", "tr", "tf", "tw", "per")
RAW_PINS = "1=1 2=2"
ANNOTATION_NOTE = ("references are the SPICE element names: KiCad reports a name without "
                   "a trailing digit as not annotated, and annotating renames it, which "
                   "co-simulation reads as an edit")
VALUE_NOTE = ("type a value as a plain decimal, or with six significant digits at most: "
              "KiCad rounds a longer one in e-notation in the SPICE netlist, and the "
              "setup reader refuses it as value_rounded")
PIN_LEN = GRID

_REF_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")
_NUMBER_RE = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_Id = namedtuple("_Id", "id")


def load_spec(path) -> dict:
    """The symbol kinds of the spec at ``path``; ValueError or OSError when it is
    not that spec."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if (doc.get("format"), doc.get("version"), doc.get("marker")) != (
            SPEC_FORMAT, SPEC_VERSION, SETUP_MARKER):
        raise ValueError(f"{path} is not {SPEC_FORMAT} {SPEC_VERSION} with marker "
                         f"{SETUP_MARKER}")
    return doc["kinds"]


def load_projection(path) -> Tuple[Optional[dict], List[dict]]:
    """(the projection, []) or (None, the refusal saying why it is unreadable)."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")), []
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return None, [_refusal("projection", f"projection {path} is unreadable: {exc}")]


def sheet_file(stem: str, role: str, name: Optional[str], alone=False) -> str:
    """The file of a setup sheet; ``alone``: no system sheet holds the system
    setup symbols, which then get a sheet of their own."""
    if role == "system" and not alone:
        return f"{stem}.kicad_sch"
    return f"{stem}.{role}.{name}.kicad_sch" if name else f"{stem}.{role}.kicad_sch"


# ------------------------------------------------------------------- model

@dataclass
class Part:
    """One symbol to draw: a setup symbol or a bench or channel primitive."""
    ref: str
    lib: str                                   # library symbol name in LIB
    value: str
    fields: List[Tuple[str, str]]              # shown below the body, with their names
    pins: List[Tuple[str, str, Optional[str]]]  # (pin, side, net); net None: open
    hidden: List[Tuple[str, str]] = field(default_factory=list)
    sim: bool = False                          # a primitive the SPICE export writes
    lib_fields: List[Tuple[str, str]] = field(default_factory=list)  # library symbol's


@dataclass
class SetupPlan:
    system: List[Part]
    sheets: List[Tuple[str, Optional[str], List[Part]]]   # role, die or net, parts


def kicad_own(name: str) -> bool:
    """A field or property name KiCad keeps for itself."""
    return name.casefold() in KICAD_FIELDS or name.casefold().startswith("ki_")


def _verbatim(value) -> bool:
    """Text a KiCad field carries to the netlist unchanged."""
    return (isinstance(value, str) and sanitize_sexpr_token(value) == value
            and "${" not in value)


def _number(value) -> bool:
    return isinstance(value, str) and bool(_NUMBER_RE.match(value))


def plan(doc, taken, spec) -> Tuple[Optional[SetupPlan], List[dict]]:
    """What to draw for projection ``doc`` with the kinds of ``spec``, or the
    refusals that stop it. ``taken`` are the References the system sheet
    already uses, which no system setup symbol may take."""
    refusals: List[dict] = []

    def refuse(code, message, **where):
        refusals.append(_refusal(code, message, **where))

    if (not isinstance(doc, dict) or doc.get("format") != FORM_FORMAT
            or doc.get("version") != FORM_VERSION):
        refuse("projection", f"the projection is not {FORM_FORMAT} {FORM_VERSION}")
        return None, refusals
    tool = doc.get("tool")
    if not isinstance(tool, dict) or tool.get("name") != PROJECTOR:
        refuse("projection", f"the setup form is not a projection: tool is "
               f"{tool.get('name') if isinstance(tool, dict) else tool!r}, not {PROJECTOR}")
    sheets = doc.get("sheets")
    if not isinstance(sheets, list):
        refuse("projection", "the projection has no list of sheets")
        return None, refusals

    out, seen = SetupPlan([], []), set()
    for index, sheet in enumerate(sheets):
        role = sheet.get("role") if isinstance(sheet, dict) else None
        if role not in SPEC_SHEET:
            refuse("sheet_role", f"sheet {index} has role {role!r}, not one of "
                   f"{', '.join(SPEC_SHEET)}", sheet=index)
            continue
        key = SHEET_KEY.get(role)
        name = sheet.get(key) if key else None
        if key and not _is_ident(name):
            refuse("sheet_role", f"sheet {index}: a {role} sheet names its {key}, "
                   f"and {name!r} is not a name KiCad carries unchanged", sheet=index)
            continue
        label = f"{role} {name}" if name else role
        if (role, name) in seen:
            refuse("sheet_role", f"the {label} sheet is drawn twice", sheet=label)
            continue
        seen.add((role, name))
        parts = _sheet_parts(sheet, role, label, spec,
                             taken if role == "system" else (), refuse)
        if role == "system":
            out.system = parts
        else:
            out.sheets.append((role, name, parts))
    return (None if refusals else out), refusals


def _sheet_parts(sheet, role, label, spec, taken, refuse) -> List[Part]:
    symbols, elements = sheet.get("symbols", []), sheet.get("elements", [])
    if not isinstance(symbols, list) or not isinstance(elements, list):
        refuse("projection", f"{label} sheet: symbols and elements must be lists",
               sheet=label)
        return []
    if elements and role not in ("bench", "channel"):
        refuse("element", f"{label} sheet: only a bench or channel sheet holds elements",
               sheet=label)
        elements = []
    refs = {str(r).casefold(): str(r) for r in taken}
    parts = []
    rows = [(_setup_part, row) for row in symbols] + [(_element_part, row) for row in elements]
    for make, row in rows:
        part = make(row, role, label, spec, refuse)
        if part is None:
            continue
        if not _REF_RE.match(part.ref):
            refuse("reference", f"{label} sheet: {part.ref!r} is not a reference KiCad "
                   f"keeps (a letter, then letters, digits or _)", sheet=label, ref=part.ref)
        elif part.ref.casefold() in refs:
            refuse("reference", f"{label} sheet: {part.ref} is taken by "
                   f"{refs[part.ref.casefold()]} (references fold case, as SPICE does)",
                   sheet=label, ref=part.ref)
        else:
            refs[part.ref.casefold()] = part.ref
            parts.append(part)
    return parts


def _setup_part(row, role, label, spec, refuse) -> Optional[Part]:
    if not isinstance(row, dict) or not isinstance(row.get("ref"), str):
        refuse("projection", f"{label} sheet: a symbol row without a reference",
               sheet=label)
        return None
    ref, kind = row["ref"], row.get("kind")
    what = f"{label} sheet, {ref}"
    entry = spec.get(kind) if isinstance(kind, str) else None
    if entry is None:
        refuse("kind_sheet", f"{what}: no symbol kind {kind!r}", sheet=label, ref=ref)
        return None
    if entry["sheet"] != SPEC_SHEET[role]:
        refuse("kind_sheet", f"{what}: a {kind} symbol belongs on a {entry['sheet']} "
               f"sheet", sheet=label, ref=ref)
        return None
    attributes, pins = row.get("attributes", {}), row.get("pins", {})
    if not isinstance(attributes, dict) or not isinstance(pins, dict):
        refuse("projection", f"{what}: attributes and pins must be objects",
               sheet=label, ref=ref)
        return None

    ok = True
    for name, value in attributes.items():
        if (not _is_ident(name) or kicad_own(name)
                or name == SETUP_MARKER or name.startswith("Sim.")):
            refuse("attribute", f"{what}: attribute {name!r} cannot be a field of its own",
                   sheet=label, ref=ref)
            ok = False
        elif not _verbatim(value):
            refuse("attribute", f"{what}: {name}={value!r} would not reach KiCad's "
                   f"netlist unchanged", sheet=label, ref=ref)
            ok = False
        elif value == "":
            refuse("attribute", f"{what}: {name} is empty, which reads back as a library "
                   f"field left unfilled", sheet=label, ref=ref)
            ok = False
    want = entry["pins"]
    if want != "per_row" and sorted(pins) != sorted(want):
        refuse("kind_pins", f"{what}: a {kind} symbol has pins {sorted(want) or 'none'}, "
               f"not {sorted(pins) or 'none'}", sheet=label, ref=ref)
        ok = False
    drawn = []
    for pin, at in pins.items():
        net = at.get("net") if isinstance(at, dict) else None
        # A setup sheet keeps the plain set: subscripted names are measured
        # through KiCad only, not through the simulators that read this sheet.
        if not _is_ident(pin):
            refuse("kind_pins", f"{what}: pin {pin!r} is not a name KiCad carries "
                   f"unchanged", sheet=label, ref=ref)
            ok = False
        elif at is not None and not _is_ident(net):
            refuse("net_name", f"{what}: pin {pin} is on {net!r}, not a name KiCad "
                   f"carries unchanged", sheet=label, ref=ref)
            ok = False
        else:
            drawn.append((pin, "left", net))
    if not ok:
        return None
    if want == "per_row":
        return Part(ref, f"{kind}_{ref}", kind,
                    [(SETUP_MARKER, kind)] + sorted(attributes.items()), drawn)
    return Part(ref, kind, kind, [(SETUP_MARKER, kind)] + sorted(attributes.items()), drawn,
                lib_fields=_library_fields(kind, entry))


def _library_fields(kind, entry) -> List[Tuple[str, str]]:
    """The fields of a kind's library symbol: its kind, and each required
    attribute empty."""
    return [(SETUP_MARKER, kind)] + [(name, "") for name in sorted(entry["required"])]


def _element_part(row, role, label, spec, refuse) -> Optional[Part]:
    row = row if isinstance(row, dict) else {}
    kind, name, nodes, value = (row.get(k) for k in ("kind", "name", "nodes", "value"))
    what = f"{label} sheet, element {name}"
    if kind not in ELEMENT_KINDS or not isinstance(name, str) or name[:1].upper() != kind:
        refuse("element", f"{what}: kind {kind!r} is not one of {', '.join(ELEMENT_KINDS)}"
               f" or the name does not start with it", sheet=label, ref=name)
        return None
    if not (isinstance(nodes, list) and len(nodes) == 2 and all(map(_is_ident, nodes))):
        refuse("element", f"{what}: nodes {nodes!r} are not two names KiCad carries "
               f"unchanged", sheet=label, ref=name)
        return None
    pins = [("1", "left", nodes[0]), ("2", "right", nodes[1])]
    if kind in ("R", "C", "L"):
        if not _number(value) or float(value) <= 0:
            refuse("element", f"{what}: value {value!r} is not a positive number",
                   sheet=label, ref=name)
            return None
        model, shown = format(Decimal(value), "f"), value
    else:
        model = shown = _source(value)
        if model is None:
            refuse("kicad_source_form", f"{what}: {value!r} is not a DC or a pulse "
                   f"source, the forms this generator draws", sheet=label, ref=name)
            return None
    return Part(name, kind, shown, [], pins, sim=True,
                hidden=[("Sim.Device", "SPICE"),
                        ("Sim.Params", f'type="{kind}" model="{model}" lib=""'),
                        ("Sim.Pins", RAW_PINS)])


def _source(value) -> Optional[str]:
    """The card text of a DC or a pulse source."""
    if not isinstance(value, dict) or set(value) != {"dc", "function", "args"}:
        return None
    dc, function, args = value["dc"], value["function"], value["args"]
    if function is None and _number(dc) and not args:
        return f"dc {dc}"
    if (function == "pulse" and dc is None and isinstance(args, list)
            and 2 <= len(args) <= len(PULSE_PARAMS) and all(map(_number, args))):
        return f"pulse({' '.join(args)})"
    return None


# ------------------------------------------------------------------ layout

@dataclass
class _PartShape:
    half_w: float
    half_h: float
    pins: Dict[str, Tuple[float, float, int]]     # pin -> (x, y, angle), y up
    ext: Dict[str, float]                         # side -> envelope from origin


def _part_shape(part: Part) -> _PartShape:
    sides = {s: [p for p in part.pins if p[1] == s] for s in ("left", "right")}
    names = 0 if part.sim else max((len(p[0]) for p in part.pins), default=0) * CHAR_W
    half_w = max(2 * GRID, _up(names + NAME_OFFSET + GRID))
    half_h = max(2 * GRID, _up((max(map(len, sides.values())) + 1) * GRID / 2))
    pins = {}
    for side, items in sides.items():
        slots = int(round(2 * half_h / GRID)) - 1
        first = (slots - len(items)) // 2
        for i, (pin, _, _) in enumerate(items):
            along = half_h - GRID * (1 + first + i)
            pins[pin] = ((-half_w - PIN_LEN, along, 0) if side == "left"
                         else (half_w + PIN_LEN, along, 180))
    text = max([len(part.value)] + [len(f"{n}: {v}") for n, v in part.fields]) * CHAR_W
    ext = {"top": _up(half_h + GRID),
           "bottom": _up(half_h + GRID * (2 + len(part.fields)))}
    for side, items in sides.items():
        labels = [_label_len(net) for _, _, net in items if net is not None]
        reach = (PIN_LEN + (WIRE + max(labels) if labels else GRID)) if items else 0
        ext[side] = _up(half_w + reach)
    ext["right"] = max(ext["right"], _up(text - half_w))
    return _PartShape(half_w, half_h, pins, ext)


def _lib_symbol(part: Part, shape: _PartShape, name: Optional[str] = None) -> list:
    pins = [["pin", "passive", "line",
             ["at", sx.num(x), sx.num(y), str(angle)],
             ["length", sx.num(PIN_LEN)],
             ["name", _q(pin), _FONT],
             ["number", _q(pin), _FONT]]
            for pin, (x, y, angle) in shape.pins.items()]
    names = ["pin_names", ["offset", sx.num(NAME_OFFSET)]]
    if part.sim:
        names.append(["hide", "yes"])
    fields = [_prop(n, v, (-shape.half_w, -shape.half_h - GRID * (2 + i)), justify="left",
                    show_name=True) for i, (n, v) in enumerate(part.lib_fields)]
    return ["symbol", _q(name or f"{LIB}:{part.lib}"),
            ["pin_numbers", ["hide", "yes"]], names,
            ["exclude_from_sim", "no" if part.sim else "yes"],
            ["in_bom", "no"], ["on_board", "yes"],
            _prop("Reference", part.lib if part.sim else "SETUP", (0, shape.half_h + 1.27)),
            _prop("Value", part.lib, (0, -shape.half_h - 1.27)),
            _prop("Footprint", "", hide=True),
            _prop("Datasheet", "", hide=True),
            _prop("Description", f"SPICE {part.lib}" if part.sim
                  else f"co-simulation setup: {part.value}", hide=True),
            *fields,
            ["symbol", _q(f"{part.lib}_0_1"),
             ["rectangle",
              ["start", sx.num(-shape.half_w), sx.num(shape.half_h)],
              ["end", sx.num(shape.half_w), sx.num(-shape.half_h)],
              ["stroke", ["width", "0.254"], ["type", "default"]],
              ["fill", ["type", "background"]]]],
            ["symbol", _q(f"{part.lib}_1_1"), *pins],
            ["embedded_fonts", "no"]]


def _hidden(name: str, value: str, at) -> list:
    """A hidden Sim.* field. Its text is built here from checked numbers and a
    kind letter, never taken from input, so it skips the sanitizer: the double
    quotes KiCad reads inside Sim.Params would not survive it."""
    node = _prop(name, "", at, hide=True)
    node[2] = sx.Quoted(value)
    return node


def _instance(part: Part, shape: _PartShape, ox: float, oy: float, sheet: str,
              project: str, key: str) -> list:
    left, below = ox - shape.half_w, oy + shape.half_h
    node = ["symbol", ["lib_id", _q(f"{LIB}:{part.lib}")],
            ["at", sx.num(ox), sx.num(oy), "0"],
            ["unit", "1"],
            ["exclude_from_sim", "no" if part.sim else "yes"], ["in_bom", "no"],
            ["on_board", "yes"], ["dnp", "no"],
            ["uuid", _q(uid("cosim", key, "symbol", part.ref))],
            _prop("Reference", part.ref, (ox, oy - shape.half_h - 1.27)),
            _prop("Value", part.value, (left, below + GRID), justify="left"),
            _prop("Footprint", "", (ox, oy), hide=True),
            _prop("Datasheet", "", (ox, oy), hide=True),
            _prop("Description", "", (ox, oy), hide=True)]
    node += [_prop(n, v, (left, below + GRID * (2 + i)), justify="left", show_name=True)
             for i, (n, v) in enumerate(part.fields)]
    node += [_hidden(n, v, (ox, oy)) for n, v in part.hidden]
    node += [["pin", _q(pin), ["uuid", _q(uid("cosim", key, "pin", part.ref, pin))]]
             for pin, _, _ in part.pins]
    node.append(["instances", ["project", _q(project),
                               ["path", _q(f"/{sheet}"),
                                ["reference", _q(part.ref)], ["unit", "1"]]]])
    items = [node]
    for pin, side, net in part.pins:
        px, py, _ = shape.pins[pin]
        items += _hookup(ox + px, oy - py, side, net, "cosim", key, part.ref, pin)
    return items


def draw(parts: List[Part], sheet: str, project: str, key: str, top: float):
    """Library symbols, items, page width and page height of ``parts`` drawn
    in rows from ``top`` down on the sheet with uuid ``sheet``."""
    shapes = {p.ref: _part_shape(p) for p in parts}
    origins, page_w, page_h = _place([_Id(p.ref) for p in parts], shapes)
    shift = top - MARGIN
    libs, emitted, items = [], set(), []
    for p in parts:
        if p.lib not in emitted:
            emitted.add(p.lib)
            libs.append(_lib_symbol(p, shapes[p.ref]))
        ox, oy = origins[p.ref]
        items += _instance(p, shapes[p.ref], ox, oy + shift, sheet, project, key)
    return libs, items, page_w, page_h + shift


def sheet_node(system_name: str, role: str, name: Optional[str], parts: List[Part],
               path, source_name: str) -> list:
    """A flat setup sheet of its own: a die, bench or channel sheet, or the
    system setup symbols when no system sheet is drawn."""
    key = f"{role} {name}" if name else role
    sheet = uid("cosim", "sheet", system_name, key)
    project = Path(path).name[:-len(".kicad_sch")]
    libs, items, page_w, page_h = draw(parts, sheet, project, key, MARGIN)
    title = ["title_block", ["title", _q(f"{system_name} {key}")],
             ["comment", "1", _q(f"{SETUP_MARKER} sheet: {role}"
                                 + (f" {SHEET_KEY[role]} {name}" if name else ""))],
             ["comment", "2", _q(f"generated from {source_name}; netlist this sheet "
                                 f"on its own")]]
    if role in ("bench", "channel"):
        title += [["comment", "3", _q(ANNOTATION_NOTE)], ["comment", "4", _q(VALUE_NOTE)]]
    return ["kicad_sch",
            ["version", SCH_VERSION],
            ["generator", _q(GENERATOR)],
            ["generator_version", _q(GENERATOR_VERSION)],
            ["uuid", _q(sheet)],
            _paper(page_w, page_h),
            title,
            ["lib_symbols", *libs],
            *items,
            ["sheet_instances", ["path", _q("/"), ["page", _q("1")]]],
            ["embedded_fonts", "no"]]


def library_text(spec) -> str:
    """The symbol library: one symbol per kind of ``spec`` whose pins it fixes,
    in spec order, as a sheet embeds it."""
    symbols = []
    for kind, entry in spec.items():
        if entry["pins"] == "per_row":
            continue
        part = Part(kind, kind, kind, [], [(pin, "left", None) for pin in entry["pins"]],
                    lib_fields=_library_fields(kind, entry))
        symbols.append(_lib_symbol(part, _part_shape(part), name=kind))
    return sx.dumps(["kicad_symbol_lib", ["version", SYM_LIB_VERSION],
                     ["generator", _q(GENERATOR)],
                     ["generator_version", _q(GENERATOR_VERSION)], *symbols]) + "\n"


def table_text() -> str:
    """The project symbol table naming the library beside the sheets."""
    return sx.dumps(["sym_lib_table", ["version", "7"],
                     ["lib", ["name", _q(LIB)], ["type", _q("KiCad")],
                      ["uri", _q(f"${{KIPRJMOD}}/{LIB_FILE}")], ["options", _q("")],
                      ["descr", _q("co-simulation setup symbols")]]]) + "\n"
