#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The netlist kicad-cli would export from a schematic, without kicad-cli.

``netlist(schematic)`` reads a .kicad_sch and every sub-sheet file it places,
resolves its connectivity the way eeschema's connection graph does, and returns
what kicad_sch_to_connectivity.py reads of a kicadsexpr netlist and of an ERC
report: the design's sheets, the components, the library parts, the nets and
the multiple_net_names violations. The rules are those of the tools image's
KiCad 9.99 (connection_graph.cpp, netlist_exporter_xml.cpp, sch_pin.cpp);
each function names the one it follows. A project file beside the schematic
(or the one given) supplies what the netlist depends on: the
multiple_net_names severity and the unit reference separator and first id.

What it does not model it refuses: a bus, bus entry, sheet pin, hierarchical
label, netclass flag or directive label; a hidden power_in pin on a non-power
symbol; a local power symbol; a reference that is not annotated (it ends in
``?``); two references that differ only in case; a cached symbol that extends
another or has jumper_pin_groups; a symbol or a sheet excluded from the board;
a text variable or an escaped character in a symbol field, an escaped
character in a field name or in a symbol's lib_id or lib_name, or a text
variable in a label; a pin set to an alternate function; a label whose text is a bus; a
rule area excluded from the board; a label or symbol rotated by other than a
multiple of 90 degrees. The first one met, the root sheet first and each file
in file order, raises KicadConstruct, which names it and where it is. A root
file older than the format KiCad still migrates on load (FORMAT_FLOOR) is an
InputError.

KiCad walks items in R-tree order and keeps drivers in pointer-ordered sets;
this reads them in file order, and sub-sheets depth first in file order rather
than by page number. Where that could change the netlist it refuses, as
KicadConstruct, instead of guessing: two weak nets that take one name (one
keeps it, the other takes ``_1``), and units of one symbol whose fields KiCad
would pick by walk order (_walk_free). KiCad gives a symbol with no instance
for a sheet path the reference and unit of the last sheet path it updated, in
page order; that is refused where another sheet path placing it gives another
(_instances_free), or where such a unit picks a field (_unit_seen). It also
refuses a net that takes, once named as an unconnected pin, the name of another
net: KiCad keeps the two apart under one name. The weak names of a shared
reference are left: their suffixes can follow file order, but the reader
refuses that form with duplicate_reference. What is left changes no form the
reader writes: the order of three or more units in ``tstamps`` (the reader
keys placements by UUID), and which secondary name a multiple_net_names
violation quotes when a net has three or more (the lowest is taken; KiCad's
own pick is pointer order). Both reads refuse such a sheet with that code;
only the quoted name can differ.
The root sheet's Sheetfile property is the file's basename, where KiCad
writes the path it loaded; the reader does not read it. Title block comment 1
is returned as written, where KiCad expands its text variables; the reader
refuses one where it reads the comment.
"""
import json
import math
import os
import re
from functools import cmp_to_key
from pathlib import Path
from typing import NamedTuple

import sexpr_io as sx
from kicad_rules import (ESCAPE_TOKEN, IU_PER_MM, blank_tilde, escape_net, expand_stack,
                         is_bus_label, iu, jumpers, properties, str_num_cmp, unit_letters,
                         unit_style)
from sheet_check import InputError, cache_key, schematic_tree

VERSION = "1.0"
NETLISTER = f"gds_to_kicad kicad_sch_netlist {VERSION}"
MNN = "multiple_net_names"
# Below this root version KiCad migrates the file on load: instance data from
# the root's symbol_instances (20221002) and legacy power symbol values
# (20230221), eeschema_helpers.cpp:182-188. Those files are not read.
FORMAT_FLOOR = 20230221

_DIGITS = "0123456789"
_MANDATORY = ("Reference", "Value", "Footprint", "Datasheet", "Description")
_LABELS = ("label", "global_label")
_CELL = 10 * IU_PER_MM      # the grid _connect files wires under

CONSTRUCT_WHAT = {
    "bus": "bus", "bus_entry": "bus entry", "sheet_pin": "sheet pin",
    "hierarchical_label": "hierarchical label", "netclass_flag": "netclass flag",
    "directive_label": "directive label",
    "hidden_power_pin": "hidden power_in pin on a non-power symbol",
    "power_local": "local power symbol",
    "ref_question": "reference that is not annotated (it ends in ?)",
    "ref_case": "references that differ only in case",
    "extends": "symbol that extends another symbol (extends)",
    "jumper_pin_groups": "jumper pin groups (jumper_pin_groups)",
    "excluded_symbol": "symbol excluded from the board",
    "excluded_sheet": "sheet excluded from the board",
    "excluded_rule_area": "rule area excluded from the board",
    "text_variable": "text variable (${...}) in a field the form keeps",
    "field_escape": "escaped character such as {slash} in a field the form keeps",
    "pin_alternate": "pin set to an alternate function (alternate)",
    "unit_fields": "units that disagree on a field",
    "weak_name": "two nets KiCad names alike",
    "lib_id_escape": "escaped character such as {slash} in a symbol's lib_id or lib_name",
    "instance_missing": "symbol with no instance for its sheet path",
    "instance_unit": "symbol whose sheet paths give it different units",
    "title_variable": "text variable (${...}) in title block comment 1",
    "rotation_label": "rotation not a multiple of 90",
    "rotation_symbol": "rotation not a multiple of 90",
}

# SCH_SYMBOL orientations as TRANSFORM(x1, y1, x2, y2) (sch_io_kicad_sexpr_parser.cpp).
_ROTATIONS = {0: (1, 0, 0, 1), 90: (0, 1, -1, 0), 180: (-1, 0, 0, -1), 270: (0, -1, 1, 0)}
_MIRRORS = {"x": (1, 0, 0, -1), "y": (-1, 0, 0, 1)}

# CONNECTION_SUBGRAPH::PRIORITY (connection_graph.h); sheet pins, hierarchical
# labels and local power pins are refused, so their ranks only bound the others.
_INVALID, _NONE, _PIN, _HIER_LABEL, _LOCAL_LABEL, _GLOBAL_POWER_PIN, _GLOBAL = -1, 0, 1, 3, 4, 6, 7

# ERC item texts of a pin's electrical type and shape (pin_type.cpp).
_PIN_TYPES = {
    "input": "Input", "output": "Output", "bidirectional": "Bidirectional",
    "tri_state": "Tri-state", "passive": "Passive", "free": "Free",
    "unspecified": "Unspecified", "power_in": "Power input", "power_out": "Power output",
    "open_collector": "Open collector", "open_emitter": "Open emitter",
    "no_connect": "Unconnected",
}
_PIN_SHAPES = {
    "line": "Line", "inverted": "Inverted", "clock": "Clock", "inverted_clock": "Inverted clock",
    "input_low": "Input low", "clock_low": "Clock low", "output_low": "Output low",
    "edge_clock_high": "Falling edge clock", "non_logic": "NonLogic",
}


class KicadConstruct(Exception):
    """A construct the native netlister does not read, and where it is: ``path``
    as opened, ``at`` in internal units of that file (None for the title
    block). ``told``, for one it reads
    but where KiCad's result depends on its walk order, is (subject, what KiCad
    does, what to do). ``schematic_version`` is the root file's (version), once
    netlist() has read it."""

    def __init__(self, construct, path, at, told=None):
        self.construct, self.path, self.at = construct, str(path), at
        self.schematic_version = ""
        subject, why, fix = told or (CONSTRUCT_WHAT[construct],
                                     "is not read by the native netlister.",
                                     "Remove it from the sheet")
        where = f" at ({_mm(at[0])}, {_mm(at[1])}) mm" if at is not None else ""
        super().__init__(f"{os.path.basename(self.path)}: {subject}{where} {why} {fix}, or read "
                         "the sheet without --native (that read needs kicad-cli).")

    def refusal(self) -> dict:
        return {"code": "kicad_construct", "message": str(self)}


# ------------------------------------------------------------------ KiCad's helpers

def _mm(units) -> str:
    text = f"{units / IU_PER_MM:.4f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def _ref_key(ref):
    """Equal for two references StrNumCmp(ignore case) holds equal."""
    return tuple(int(run) if run[0] in _DIGITS else run.upper()
                 for run in re.findall(r"[0-9]+|[^0-9]", ref))


def _segment_hit(p, a, b, dist=1):
    """TestSegmentHit (trigo.cpp): ``p`` within ``dist`` of the segment a-b."""
    (px, py), (ax, ay), (bx, by) = p, a, b
    xmin, xmax = sorted((ax, bx))
    ymin, ymax = sorted((ay, by))
    if ymin - py > dist or py - ymax > dist or xmin - px > dist or px - xmax > dist:
        return False
    if ax == bx and ymin < py < ymax:
        return abs(ax - px) <= dist
    if ay == by and xmin < px < xmax:
        return abs(ay - py) <= dist
    apx, apy, abx, aby = px - ax, py - ay, bx - ax, by - ay
    e = apx * abx + apy * aby
    f = abx * abx + aby * aby
    if e <= 0:
        squared = apx * apx + apy * apy
    elif e >= f:
        squared = (px - bx) ** 2 + (py - by) ** 2
    else:
        squared = math.floor(apx * apx + apy * apy - e * e / f + 0.5)
    return squared < (dist + 1) ** 2


def _text(node, tag):
    child = sx.find(node, tag) if node is not None else None
    return str(child[1]) if child is not None and len(child) > 1 else None


def _xy(node):
    return iu(node[1]), iu(node[2])


def _dict(value):
    return value if isinstance(value, dict) else {}


# ------------------------------------------------------------------ the project

class _Settings(NamedTuple):
    check_names: bool       # multiple_net_names not set to "ignore"
    separator: str          # subpart_id_separator, "" for none
    first_id: str           # subpart_first_id


def _settings(schematic, project):
    """What KiCad loads from the project (SCHEMATIC_SETTINGS, ERC_SETTINGS); its
    defaults without one, and for a value outside the range KiCad accepts."""
    if project is None:
        beside = os.path.splitext(schematic)[0] + ".kicad_pro"
        if not os.path.isfile(beside):
            return _Settings(True, "", "A")
        project = beside
    try:
        doc = _dict(json.loads(Path(project).read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise InputError(f"KiCad project {project} is unreadable: {exc}") from exc
    severities = _dict(_dict(doc.get("erc")).get("rule_severities"))
    sch = _dict(doc.get("schematic"))
    separator, first = sch.get("subpart_id_separator", 0), sch.get("subpart_first_id", 65)
    separator = separator if isinstance(separator, int) and 0 <= separator <= 126 else 0
    first = first if isinstance(first, int) and ord("1") <= first <= ord("z") else 65
    return _Settings(severities.get(MNN) != "ignore", chr(separator) if separator else "",
                     chr(first))


def _sub_reference(unit, settings):
    """SCHEMATIC_SETTINGS::SubReference(unit, separator)."""
    if unit < 1:
        return ""
    ident = str(unit) if settings.first_id in _DIGITS else unit_letters(unit, settings.first_id)
    return settings.separator + ident


# ------------------------------------------------------------------ sheets and items

class _Sheet(NamedTuple):
    """One SCH_SHEET_PATH: a file as placed at one point of the hierarchy."""
    path: str               # the file, as opened
    tree: list
    version: int
    uuids: str              # the instance path symbols name, "/<root uuid>/<sheet uuid>..."
    human: str              # "/" or "/<Sheetname>/..."
    fields: dict            # the sheet's fields, as each of its components lists them


class _LibPin(NamedTuple):
    number: str
    name: str
    type: str
    shape: str
    hidden: bool
    at: tuple               # library coordinates, y down
    unit: int
    style: int


class _Item:
    """A connectable item of one sheet: wire, junction, no_connect, label,
    global_label or pin. ``origin`` is the same for the item on every sheet
    path that places its file, as KiCad's SCH_ITEM is."""
    __slots__ = ("kind", "points", "text", "placed", "pin", "origin")

    def __init__(self, kind, points, origin, text=None, placed=None, pin=None):
        self.kind, self.points, self.text, self.placed, self.pin = kind, points, text, placed, pin
        self.origin = origin

    def propagates(self):
        """ConnectionPropagatesTo: a no_connect pin connects to nothing."""
        return not (self.kind == "pin" and self.pin.type == "no_connect")

    def is_driver(self):
        """SCH_CONNECTION::IsDriver: a label, a power pin, or a pin of a symbol
        annotated on this sheet path."""
        return self.kind in _LABELS or self.kind == "pin" and (
            self.placed.annotated or self.global_power())

    def global_power(self):
        """SCH_PIN::IsGlobalPower, and so IsPower, local power being refused."""
        return (self.kind == "pin" and self.pin.type == "power_in"
                and (self.pin.hidden or self.placed.power))

    def names_net(self):
        """A label or power pin: a driver whose name a net can take."""
        return self.kind in _LABELS or self.global_power()


def _version(tree, path):
    text = _text(tree, "version") or "0"
    if not text.isdigit():
        raise InputError(f"{path}: not a readable schematic: its version is {text!r}, "
                         "not a date number such as 20230221")
    return int(text)


def _lib_pins(lib, version):
    pins = []
    for unit in sx.find_all(lib, "symbol"):
        number, style = unit_style(lib, unit)
        for pin in sx.find_all(unit, "pin"):
            at = sx.find(pin, "at") or ["at", "0", "0"]
            hidden = (_text(pin, "hide") == "yes"
                      or any(a == "hide" for a in pin if not isinstance(a, (list, sx.Quoted))))
            pins.append(_LibPin(blank_tilde(_text(pin, "number") or "", version),
                                blank_tilde(_text(pin, "name") or "", version), str(pin[1]),
                                str(pin[2]), hidden, (iu(at[1]), -iu(at[2])), number, style))
    return pins


def _properties(node, version):
    """``(fields, private)``: a symbol's or sheet's property names to text, as
    KiCad loads them from a file of ``version``, and the names of its private
    ones."""
    found = properties(node, version)
    return {name: value for name, value, _ in found}, {name for name, _, p in found if p}


class _Placed:
    """A placed symbol on one sheet path, with its pins there."""

    def __init__(self, node, sheet, cache, settings, refs):
        self.node, self.sheet = node, sheet
        at = sx.find(node, "at")
        self.at = _xy(at)
        rotation = int(float(at[3])) if len(at) > 3 else 0
        if rotation not in _ROTATIONS:
            raise KicadConstruct("rotation_symbol", sheet.path, self.at)
        if _text(node, "on_board") == "no":
            raise KicadConstruct("excluded_symbol", sheet.path, self.at)
        # KiCad reads {slash} in either as / (sch_io_kicad_sexpr_parser.cpp:3135,3150).
        if any(ESCAPE_TOKEN.search(_text(node, k) or "") for k in ("lib_id", "lib_name")):
            raise KicadConstruct("lib_id_escape", sheet.path, self.at)
        self.cache_name = cache_key(node)
        self.lib = cache.get(self.cache_name)
        power = sx.find(self.lib, "power") if self.lib is not None else None
        for construct, found in (
                ("extends", self.lib is not None and sx.find(self.lib, "extends") is not None),
                ("jumper_pin_groups",
                 self.lib is not None and sx.find(self.lib, "jumper_pin_groups") is not None),
                ("power_local", power is not None and len(power) > 1 and power[1] == "local")):
            if found:
                raise KicadConstruct(construct, sheet.path, self.at)
        self.power = power is not None
        self.uuid = (_text(node, "uuid") or "").lower()
        self.fields, private = _properties(node, sheet.version)
        self.user = {k: v for k, v in self.fields.items()
                     if k not in _MANDATORY and k not in private}
        instance = self._instance(sheet.uuids)
        # Without an instance for this path the symbol is not annotated here
        # and its pins drive no net, though it is still listed. Its reference
        # is then the Reference field, which the first instance KiCad loads
        # sets (SCH_SYMBOL::AddHierarchicalReference).
        self.instanced = instance is not None
        self.annotated = self.instanced and bool(_text(instance, "reference"))
        self.ref = (_text(instance or self._first(), "reference")
                    or self.fields.get("Reference") or "?")
        if self.ref.endswith("?"):
            raise KicadConstruct("ref_question", sheet.path, self.at)
        if refs.setdefault(self.ref.upper(), self.ref) != self.ref:
            raise KicadConstruct("ref_case", sheet.path, self.at)
        kept = {k: v for k, v in self.fields.items() if k not in private}
        if any("${" in v for v in kept.values()):
            raise KicadConstruct("text_variable", sheet.path, self.at)
        # KiCad unescapes field names and values (UnescapeString) and shows a
        # pin's alternate name and type (sch_pin.cpp:577); neither is modelled.
        if any(ESCAPE_TOKEN.search(t) for item in kept.items() for t in item):
            raise KicadConstruct("field_escape", sheet.path, self.at)
        if any(sx.find(pin, "alternate") is not None for pin in sx.find_all(node, "pin")):
            raise KicadConstruct("pin_alternate", sheet.path, self.at)
        self.unit = self.unit_on(sheet.uuids)
        self.lib_pins = _lib_pins(self.lib, sheet.version) if self.lib is not None else []
        self.units = max([1] + [unit_style(self.lib, u)[0]
                                for u in sx.find_all(self.lib or [], "symbol")])
        self.ref_unit = self.ref + (_sub_reference(self.unit, settings) if self.units > 1 else "")
        lib_fields = _properties(self.lib, sheet.version)[0] if self.lib is not None else {}
        self.virtual = lib_fields.get("Reference", "").startswith("#")
        self.jumpers = self.lib is not None and jumpers(self.lib)
        self.pins = self._pins(rotation)

    def _pins(self, rotation):
        """The pins of its unit and body style (SCH_SYMBOL::GetPins), each at
        its place on the sheet (SCH_PIN::GetPosition)."""
        x1, y1, x2, y2 = _ROTATIONS[rotation]
        mirror = _MIRRORS.get(_text(self.node, "mirror"))
        if mirror is not None:
            t1, u1, t2, u2 = mirror
            x1, y1, x2, y2 = (x1 * t1 + x2 * u1, y1 * t1 + y2 * u1,
                              x1 * t2 + x2 * u2, y1 * t2 + y2 * u2)
        style = int(_text(self.node, "body_style") or _text(self.node, "convert") or 1)
        pins = []
        for k, pin in enumerate(self.lib_pins):
            if (pin.style and style and pin.style != style) or pin.unit not in (0, self.unit):
                continue
            if pin.type == "power_in" and pin.hidden and not self.power:
                raise KicadConstruct("hidden_power_pin", self.sheet.path, self.at)
            px, py = pin.at
            at = (x1 * px + y1 * py + self.at[0], x2 * px + y2 * py + self.at[1])
            pins.append(_Item("pin", (at,), (id(self.node), k), placed=self, pin=pin))
        return pins

    def _paths(self):
        return [p for p in sx.find_all_rec(sx.find(self.node, "instances") or [], "path")
                if len(p) > 1]

    def _instance(self, uuids):
        return next((p for p in self._paths() if str(p[1]) == uuids), None)

    def _first(self):
        return next(iter(self._paths()), None)

    def unit_on(self, uuids):
        """SCH_SYMBOL::GetUnitSelection for the sheet path ``uuids``: without an
        instance for it, the unit the first instance loaded sets."""
        instance = self._instance(uuids) or self._first()
        return int(_text(instance, "unit") or _text(self.node, "unit") or 1)


def _sheet_items(sheet, settings, refs):
    """The connectable items and placed symbols of one sheet, in file order, and
    its sub-sheet nodes. The first construct met raises."""
    cache = {str(s[1]): s for s in sx.find_all(sx.find(sheet.tree, "lib_symbols") or [], "symbol")}
    items, placed, children = [], [], []
    for node in sheet.tree[1:]:
        tag = sx.head(node)
        if tag == "bus":
            raise KicadConstruct(tag, sheet.path, _xy(sx.find(sx.find(node, "pts"), "xy")))
        if tag in ("bus_entry", "hierarchical_label", "netclass_flag", "directive_label"):
            raise KicadConstruct(tag, sheet.path, _xy(sx.find(node, "at")))
        if tag == "rule_area" and _text(node, "on_board") == "no":
            # SCH_ITEM::ResolveExcludedFromBoard: the symbols inside are excluded.
            raise KicadConstruct("excluded_rule_area", sheet.path, _xy(sx.find_all_rec(
                sx.find(node, "polyline"), "xy")[0]))
        if tag == "wire":
            ends = sx.find_all(sx.find(node, "pts"), "xy")
            if len(ends) != 2:
                raise ValueError(f"a wire has {len(ends)} points")
            items.append(_Item(tag, tuple(_xy(p) for p in ends), id(node)))
        elif tag in ("junction", "no_connect"):
            items.append(_Item(tag, (_xy(sx.find(node, "at")),), id(node)))
        elif tag in _LABELS:
            at = sx.find(node, "at")
            text = str(node[1])
            if len(at) > 3 and float(at[3]) % 90:
                raise KicadConstruct("rotation_label", sheet.path, _xy(at))
            if "${" in text:
                raise KicadConstruct("text_variable", sheet.path, _xy(at))
            if is_bus_label(text):
                raise KicadConstruct("bus", sheet.path, _xy(at))
            items.append(_Item(tag, (_xy(at),), id(node), text=text))
        elif tag == "symbol":
            symbol = _Placed(node, sheet, cache, settings, refs)
            placed.append(symbol)
            items.extend(symbol.pins)
        elif tag == "sheet":
            if _text(node, "on_board") == "no":
                raise KicadConstruct("excluded_sheet", sheet.path, _xy(sx.find(node, "at")))
            pin = sx.find(node, "pin")
            if pin is not None:
                raise KicadConstruct("sheet_pin", sheet.path, _xy(sx.find(pin, "at")))
            children.append(node)
    return items, placed, children


def _hierarchy(schematic, tree, settings):
    """``(sheets, inputs)``: every sheet path depth first, each with its items and
    placed symbols, and every file read, root first."""
    sheets, inputs, refs = [], [schematic], {}
    trees = {os.path.abspath(schematic): tree}

    def visit(sheet, ancestors):
        try:
            items, placed, children = _sheet_items(sheet, settings, refs)
        except (TypeError, AttributeError, ValueError, IndexError) as exc:
            # A node that parses but lacks what KiCad requires of it.
            raise InputError(f"{sheet.path}: not a readable schematic: {exc}") from exc
        sheets.append((sheet, items, placed))
        for node in children:
            fields = _properties(node, sheet.version)[0]
            path = os.path.join(os.path.dirname(sheet.path), fields.get("Sheetfile", ""))
            key = os.path.abspath(path)
            if key in ancestors:
                raise InputError(f"{sheet.path}: sub-sheet {fields.get('Sheetname', '')} "
                                 f"places {os.path.basename(path)}, which already contains this "
                                 "sheet, so the sheets form a loop. Remove that sub-sheet.")
            if key not in trees:
                trees[key] = schematic_tree(path)
                inputs.append(path)
            visit(_Sheet(path, trees[key], _version(trees[key], path),
                         f"{sheet.uuids}/{_text(node, 'uuid')}",
                         f"{sheet.human}{fields.get('Sheetname', '')}/", fields),
                  ancestors | {key})

    visit(_Sheet(schematic, tree, _version(tree, schematic), f"/{_text(tree, 'uuid')}",
                 "/", {"Sheetname": "", "Sheetfile": os.path.basename(schematic)}),
          {os.path.abspath(schematic)})
    return sheets, inputs


# ------------------------------------------------------------------ connectivity

def _connect(items, placed):
    """The item groups of one sheet, in order of their first item
    (CONNECTION_GRAPH::updateItemConnectivity, buildConnectionGraph)."""
    parent = list(range(len(items)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def join(a, b):
        parent[root(a)] = root(b)

    at = {}
    for i, item in enumerate(items):
        for point in item.points:
            at.setdefault(point, []).append(i)
    # Each wire under every grid cell its box, grown by 1 IU, touches: a label
    # is tested only against the wires of its cell, in file order.
    grid = {}
    for w, item in enumerate(items):
        if item.kind == "wire":
            (ax, ay), (bx, by) = item.points
            for gx in range((min(ax, bx) - 1) // _CELL, (max(ax, bx) + 1) // _CELL + 1):
                for gy in range((min(ay, by) - 1) // _CELL, (max(ay, by) + 1) // _CELL + 1):
                    grid.setdefault((gx, gy), []).append(w)
    for i, item in enumerate(items):
        if item.kind not in _LABELS:
            continue
        point = item.points[0]
        hits = [w for w in grid.get((point[0] // _CELL, point[1] // _CELL), ())
                if _segment_hit(point, *items[w].points)]
        # SCH_LABEL_BASE::UpdateDanglingState: a label with no pin, label or
        # no_connect on its anchor joins the first wire within 1 IU of it.
        if hits and not any(j != i and items[j].kind in ("pin", "no_connect", *_LABELS)
                            for j in at[point]):
            join(i, hits[0])
        # Two or more wires under a label all meet it there.
        if len(hits) >= 2:
            at[point].extend(w for w in hits if w not in at[point])
    for members in at.values():
        linked = [i for i in members if items[i].propagates()]
        for a, b in zip(linked, linked[1:]):
            join(a, b)
    index = {id(item): i for i, item in enumerate(items)}
    for symbol in placed:
        if symbol.jumpers:
            by_number = {}
            for pin in symbol.pins:
                by_number.setdefault(pin.pin.number, []).append(index[id(pin)])
            for group in by_number.values():
                for a, b in zip(group, group[1:]):
                    join(a, b)
    groups = {}
    for i, item in enumerate(items):
        groups.setdefault(root(i), []).append(item)
    return list(groups.values())


def _pin_net_name(item, force_no_connect):
    """SCH_PIN::GetDefaultNetName (sch_pin.cpp)."""
    pin, symbol = item.pin, item.placed
    if item.global_power():
        return escape_net(symbol.fields.get("Value", "") if symbol.power else pin.name)
    unconnected = force_no_connect or pin.type == "no_connect"
    prefix = "unconnected-(" if unconnected else "Net-("
    pad = escape_net(expand_stack(pin.number)[0])
    if pin.name and pin.name != pin.number:
        multiple = any(o.pin.name == pin.name and o.pin.number != pin.number
                       and unconnected == (o.pin.type == "no_connect") for o in symbol.pins)
        tail = f"-Pad{pad}" if unconnected or multiple else ""
        return f"{prefix}{symbol.ref_unit}-{escape_net(pin.name)}{tail})"
    return f"{prefix}{symbol.ref}-Pad{pad})"


def _priority(item):
    """CONNECTION_SUBGRAPH::GetDriverPriority."""
    if item.kind == "global_label":
        return _GLOBAL
    if item.kind == "label":
        return _LOCAL_LABEL
    if item.global_power():
        return _GLOBAL_POWER_PIN
    return _NONE if item.placed.virtual else _PIN


class _Subgraph:
    """CONNECTION_SUBGRAPH: connected items of one sheet and the name they get.
    The connection is ``(path prefix, name, suffix)``."""

    def __init__(self, sheet, items):
        self.sheet, self.items = sheet, items
        self.no_connect = next((i for i in items if i.kind == "no_connect"
                                or (i.kind == "pin" and i.pin.type == "no_connect")), None)
        self.drivers = [i for i in items if i.is_driver()]
        self.driver = self.conn = None
        self.strong = self.local = self.multiple = self.absorbed = self.lone = False
        self.dirty = True

    @property
    def name(self):
        return "".join(self.conn)

    @property
    def short(self):
        """Name(true): without the sheet path."""
        return self.conn[1] + self.conn[2]

    def driver_name(self, item):
        """CONNECTION_SUBGRAPH::GetNameForDriver."""
        if item.kind == "pin":
            return _pin_net_name(item, self.no_connect is not None)
        return escape_net(item.text)

    def _before(self, a, b):
        # A pin of a power symbol first, as its library symbol says
        # (connection_graph.cpp:215-216).
        if a.kind == "pin" and b.kind == "pin" and a.placed.power != b.placed.power:
            return -1 if a.placed.power else 1
        na, nb = self.driver_name(a), self.driver_name(b)
        if ("-Pad" in na) != ("-Pad" in nb):
            return 1 if "-Pad" in na else -1
        return (na > nb) - (na < nb)

    def resolve(self):
        """CONNECTION_SUBGRAPH::ResolveDrivers; True when a driver is found."""
        best, candidates, strong = _INVALID, [], []
        for item in self.drivers:
            rank = _priority(item)
            if rank == _PIN and item.placed.ref.startswith("#"):
                continue
            if rank >= _HIER_LABEL:
                strong.append(item)
            if rank > best:
                best, candidates = rank, [item]
            elif candidates and rank == best:
                candidates.append(item)
        self.strong = self.strong or best >= _HIER_LABEL
        self.local = best < _GLOBAL_POWER_PIN
        self.driver = min(candidates, key=cmp_to_key(self._before)) if candidates else None
        self.multiple = self.multiple or len(strong) > 1
        if self.strong:
            self.drivers = strong
        if self.driver is not None:
            prefix = self.sheet.human if self.driver.kind == "label" else ""
            self.conn = (prefix, self.driver_name(self.driver), self.conn[2] if self.conn else "")
        return self.driver is not None

    def secondary_names(self):
        """The names its other power pins and labels would give it
        (processSubGraphs, add_connections_to_check)."""
        others = [i for i in self.items if i is not self.driver and i.names_net()]
        return [name for name in map(self.driver_name, others) if name != self.short]

    def answers_to(self, name):
        """Whether a same-sheet subgraph named ``name`` is this one: its own
        name, or that of another power pin or label it holds (connection_graph.cpp:2088-2135)."""
        if self.short == name:
            return True
        return self.multiple and any(
            d is not self.driver and (_pin_net_name(d, False) if d.kind == "pin"
                                      else self.driver_name(d)) == name
            for d in self.drivers if d.names_net())

    def absorb(self, other):
        """CONNECTION_SUBGRAPH::Absorb."""
        self.items = self.items + other.items
        self.drivers = self.drivers + [i for i in other.items if i.is_driver()]
        self.multiple = self.multiple or other.multiple
        other.absorbed, other.dirty, other.driver = True, False, None


def _suffixed(subgraph, names):
    """processSubGraphs' create_new_name, called until the name is free: the
    name a weak subgraph is filed under, and the suffix its connection keeps.
    The count starts at 1 for each subgraph (connection_graph.cpp:1862)."""
    suffix, key = 1, f"{subgraph.name}_1"
    while key in names:
        suffix += 1
        key = f"{subgraph.name}_{suffix - 1}_{suffix}"
    return key, f"_{suffix}"


def _name_nets(subgraphs, shared):
    """The driven subgraphs left once same-sheet names merge, weak duplicates
    take a suffix and global names propagate, each with its final name
    (CONNECTION_GRAPH::processSubGraphs and buildConnectionGraph)."""
    driven = [s for s in subgraphs if s.resolve()]
    _weak_names_free(driven, shared)
    _merge_and_suffix(driven)
    driven = [s for s in driven if not s.absorbed]
    _propagate_names(driven)
    _rename_lone_pins(driven)
    _lone_names_free(driven)
    return driven


def _shared_refs(sheets):
    """The references the reader refuses as shared (sheet_check.component_placements):
    placed more than once, other than as distinct units of one multi-unit symbol."""
    by_ref = {}
    for _, _, placed in sheets:
        for p in placed:
            by_ref.setdefault(p.ref, []).append(p)
    return {ref for ref, group in by_ref.items() if len(group) > 1 and (
        any(p.units == 1 for p in group) or len({p.cache_name for p in group}) > 1
        or len({p.unit for p in group}) < len(group))}


def _weak_names_free(driven, shared):
    """Raise when two weak subgraphs take one name: which keeps it and which
    takes the suffix follows KiCad's subgraph order (connection_graph.cpp:1859-1925),
    not the file. Pins of a shared reference are left: the reader refuses it."""
    weak = {}
    for s in driven:
        if not s.strong:
            weak.setdefault(s.name, []).append(s)
    for name, alike in weak.items():
        if len(alike) > 1 and not all(s.driver.placed.ref in shared for s in alike):
            pin = alike[0].driver
            raise KicadConstruct("weak_name", pin.placed.sheet.path, pin.points[0], (
                f"pin {pin.pin.number} of {pin.placed.ref}",
                f"is on one of two nets KiCad names alike ({name}). KiCad's netlist then "
                "depends on the order of the items in the file.",
                "Connect the two nets"))


def _merge_and_suffix(driven):
    """processSubGraphs (connection_graph.cpp:1841-2190), in one pass: a weak
    subgraph whose name another has takes a suffix (:1859-1925); a strong one
    absorbs each strong subgraph of its sheet that answers to one of its names,
    or to a name of what it absorbed (:2014-2165); each that absorbed resolves
    its drivers again (:2169-2187)."""
    names = {}
    for s in driven:
        names.setdefault(s.name, []).append(s)
    invalidated = []
    for s in driven:
        if s.absorbed:
            continue
        if not s.strong:
            if len(names.get(s.name, ())) > 1:
                key, suffix = _suffixed(s, names)
                names[s.name].remove(s)
                names.setdefault(key, []).append(s)
                s.conn = (s.conn[0], s.conn[1], suffix)
            continue
        candidates = [c for c in driven if c.sheet is s.sheet and c.strong and c is not s]
        checks = [s.short] + s.secondary_names()
        for test in checks:
            for c in candidates:
                if not c.absorbed and c.answers_to(test):
                    checks.extend(c.secondary_names())
                    s.absorb(c)
                    if s not in invalidated:
                        invalidated.append(s)
    for s in invalidated:
        if not s.absorbed:
            s.resolve()


def _propagate_names(driven):
    """buildConnectionGraph (connection_graph.cpp:2241-2330): a globally driven subgraph with more
    than one driver gives its name to every other globally driven subgraph that
    one of its other drivers names, on any sheet when that driver is global."""
    global_driven = [s for s in driven if not s.local]
    for s in driven:
        if not s.dirty or s.local or not s.multiple:
            continue
        for d in s.drivers:
            secondary = s.driver_name(d)
            if d is s.driver or secondary == s.name:
                continue
            anywhere = _priority(d) >= _GLOBAL_POWER_PIN
            for c in global_driven:
                if c is not s and (anywhere or c.sheet is s.sheet) and any(
                        c.driver_name(cd) == secondary for cd in c.drivers):
                    c.conn, c.dirty = s.conn, False


def _rename_lone_pins(driven):
    """buildConnectionGraph's updateItemConnectionsTask (connection_graph.cpp:2411-2423): a weakly
    driven subgraph whose one driver is a pin is named as an unconnected one."""
    for s in driven:
        if not s.strong and len(s.drivers) == 1 and s.driver.kind == "pin":
            s.conn = ("", _pin_net_name(s.driver, True), s.conn[2])
            s.lone = True


def _lone_names_free(driven):
    """Raise when a subgraph _rename_lone_pins named shares its name with
    another: KiCad keeps their net codes apart and writes two nets of that name
    (connection_graph.cpp:2413-2425, netlist_exporter_xml.cpp:910); _nets would
    write one."""
    by_name = {}
    for s in driven:
        by_name.setdefault(s.name, []).append(s)
    for name, alike in by_name.items():
        lone = [s for s in alike if s.lone]
        if lone and len(alike) > 1:
            pin = lone[0].driver
            raise KicadConstruct("weak_name", pin.placed.sheet.path, pin.points[0], (
                f"pin {pin.pin.number} of {pin.placed.ref}",
                f"is on one of two nets KiCad names alike ({name}). KiCad's netlist then "
                "holds two nets of that name.",
                "Connect the two nets"))


# ------------------------------------------------------------------ the netlist

def _nets(driven):
    """NETLIST_EXPORTER_XML::makeListOfNets, with each net's nodes."""
    groups = {}
    for s in driven:
        groups.setdefault(s.name, []).append(s)
    nets = []
    for name in sorted(groups, key=cmp_to_key(str_num_cmp)):
        marked = any(s.no_connect is not None and s.no_connect.kind == "no_connect"
                     for s in groups[name])
        nodes = sorted((i for s in groups[name] for i in s.items if i.kind == "pin"),
                       key=lambda i: (i.placed.ref, i.pin.number))
        nodes = [n for k, n in enumerate(nodes)
                 if not k or (n.placed.ref, n.pin.number) != (nodes[k - 1].placed.ref,
                                                              nodes[k - 1].pin.number)]
        if not nodes:
            continue
        first = nodes[0]
        stacked = all(n.placed.node is first.placed.node and n.points == first.points
                      and n.pin.name == first.pin.name for n in nodes)
        no_connect = marked and (len(nodes) == 1 or stacked)
        out = [{"ref": n.placed.ref, "pin": number, "pintype": n.pin.type,
                "no_connect": no_connect}
               for n in nodes if not n.placed.ref.startswith("#")
               for number in expand_stack(n.pin.number)]
        if out:
            nets.append({"name": name, "nodes": out})
    return nets


def _symbol_fields(primary, sheets):
    """``(value, footprint, fields)`` (NETLIST_EXPORTER_XML::addSymbolFields): a
    multi-unit symbol takes Value, Footprint, Datasheet and Description from the
    lowest unit that does not leave it blank, and a user field from the lowest
    unit that has it, blank or not. Each placement's unit is the one it has on
    the primary's sheet path. KiCad walks the placements in R-tree order from
    blank values, with minUnit starting at the primary's unit; where that order
    can change its pick, _walk_free refuses the symbol."""
    if primary.units == 1:
        group = [primary]
    else:
        seen = sorted(((_unit_seen(p, primary, sheets), p) for _, _, placed in sheets
                       for p in placed if p.ref.lower() == primary.ref.lower()),
                      key=lambda up: up[0] or 0)
        group = [p for _, p in seen]
        for name in _MANDATORY[1:]:
            _walk_free(primary, seen, name, [p.fields.get(name) or None for p in group])
        for name in sorted({k for p in group for k in p.user}):
            _walk_free(primary, seen, name, [p.user.get(name) for p in group])
    mandatory = dict.fromkeys(_MANDATORY[1:], "")
    user = {}
    for other in group:
        for name in mandatory:
            mandatory[name] = mandatory[name] or other.fields.get(name, "")
        for name, value in other.user.items():
            user.setdefault(name, value)
    value = mandatory.pop("Value")
    return value or "~", mandatory["Footprint"], {**user, **mandatory}


def _unit_seen(placed, primary, sheets):
    """SCH_SYMBOL::GetUnitSelection of ``placed`` for the primary's sheet path:
    its instance there, else m_unit, which UpdateAllScreenReferences leaves at
    the unit of the last sheet path placing it that has an instance
    (eeschema_helpers.cpp:211-214, sch_sheet_path.cpp:434-435). None where the
    sheet paths give it different units, which page order picks from."""
    if placed._instance(primary.sheet.uuids) is not None:
        return placed.unit_on(primary.sheet.uuids)
    units = {p.unit for _, _, others in sheets for p in others
             if p.node is placed.node and p.instanced}
    if len(units) > 1:
        return None
    return units.pop() if units else placed.unit


def _instances_free(sheets):
    """Raise for a placement with no instance for its sheet path where another
    sheet path placing the same symbol gives it another reference or unit:
    KiCad then takes those of the last path it updated, in page order
    (SCH_SYMBOL::GetRef and GetUnitSelection, sch_sheet_path.cpp:434-435)."""
    by_node = {}
    for _, _, placed in sheets:
        for p in placed:
            by_node.setdefault(id(p.node), []).append(p)
    for group in by_node.values():
        for p in group:
            if not p.instanced and any(q.instanced and (q.ref, q.unit) != (p.ref, p.unit)
                                       for q in group):
                raise KicadConstruct("instance_missing", p.sheet.path, p.at, (
                    f"symbol {p.ref}",
                    f"has no instance for the sheet path {p.sheet.human}. KiCad's netlist "
                    "then depends on the page order of the sheets that place it.",
                    "Annotate the schematic in KiCad"))


def _walk_free(primary, seen, name, given):
    """Raise unless every walk order gives KiCad's pick of the field ``name``
    (netlist_exporter_xml.cpp:121-176): the units agree on it, or the lowest
    unit is below the primary's and each of its placements gives one value.
    ``seen`` is, by ascending unit, each placement with its unit seen from the
    primary's sheet path (_unit_seen); ``given`` is its value, None where it
    leaves the field blank (Value, Footprint, Datasheet, Description) or lacks
    it."""
    values = {v for v in given if v is not None}
    if len(values) < 2:
        return
    for unit, placed in seen:
        if unit is None:
            raise KicadConstruct("instance_unit", placed.sheet.path, placed.at, (
                f"symbol {placed.ref}",
                "takes different units on the sheet paths that place it, and its units give the "
                f"field {name} different values. KiCad's netlist then depends on the page order "
                "of those sheets.",
                "Give it one unit on every sheet path"))
    units = [u for u, _ in seen]
    lowest = {v for u, v in zip(units, given) if u == units[0]}
    if units[0] < primary.unit_on(primary.sheet.uuids) and len(lowest) == 1 and None not in lowest:
        return
    shown = ", ".join(f'"{v}"' for v in sorted(values))
    raise KicadConstruct("unit_fields", primary.sheet.path, primary.at, (
        f"symbol {primary.ref}",
        f"has units that give the field {name} different values ({shown}). KiCad's netlist "
        "then depends on the order of the units in the file.",
        f"Give every unit of {primary.ref} the same {name}"))


def _split_lib_id(name):
    lib, sep, part = name.partition(":")
    return (lib, part) if sep else ("", name)


def _components(sheets):
    """NETLIST_EXPORTER_XML::makeSymbols and makeLibParts."""
    components, libparts, found = [], {}, set()
    for sheet, _, placed in sheets:
        groups = {}
        for symbol in placed:
            group = groups.setdefault(_ref_key(symbol.ref), [symbol, []])
            if group[0] is symbol:
                continue
            if group[0].uuid > symbol.uuid:
                group[1].append(group[0])
                group[0] = symbol
            else:
                group[1].append(symbol)
        ordered = sorted(groups.values(), key=cmp_to_key(
            lambda a, b: str_num_cmp(a[0].ref, b[0].ref, True)))
        for primary, extras in ordered:
            if primary.ref.startswith("#") or primary.lib is None:
                continue
            if primary.units > 1:
                if primary.ref in found:
                    continue
                found.add(primary.ref)
            lib_name = _text(primary.node, "lib_name")
            source = (("", lib_name) if lib_name
                      else _split_lib_id(_text(primary.node, "lib_id") or ""))
            value, footprint, fields = _symbol_fields(primary, sheets)
            components.append({
                "ref": primary.ref, "libsource": {"lib": source[0], "part": source[1]},
                "value": value, "footprint": footprint, "fields": fields,
                "property": {**primary.user, **sheet.fields}, "sheetpath": sheet.human,
                "tstamps": [s.uuid for s in extras] + [primary.uuid]})
            key = _split_lib_id(primary.cache_name)
            if key not in libparts:
                numbers = sorted((p.number for p in primary.lib_pins),
                                 key=cmp_to_key(lambda a, b: str_num_cmp(a, b, True)))
                numbers = [n for k, n in enumerate(numbers) if not k or n != numbers[k - 1]]
                libparts[key] = [n for number in numbers for n in expand_stack(number)]
    return components, [{"lib": lib, "part": part, "pins": pins}
                        for (lib, part), pins in libparts.items()]


def _describe(item):
    """The ERC item text of a driver (GetItemDescription)."""
    if item.kind == "global_label":
        return f"Global Label '{item.text}'"
    if item.kind == "label":
        return f"Label '{item.text}'"
    pin = item.pin
    what = ", ".join(t for t in (pin.name, _PIN_TYPES.get(pin.type, pin.type),
                                 _PIN_SHAPES.get(pin.shape, pin.shape)) if t)
    return (f"Symbol {item.placed.ref} {'Hidden pin' if pin.hidden else 'Pin'} "
            f"{pin.number} [{what}]")


def _violations(subgraphs):
    """CONNECTION_GRAPH::ercCheckMultipleDrivers over every subgraph, a driver
    reported once, though its file is placed on several sheet paths
    (connection_graph.cpp:3250-3270)."""
    out, seen = [], set()
    for s in subgraphs:
        if s.absorbed or s.driver is not None and s.driver.origin in seen:
            continue
        if s.driver is not None:
            seen.add(s.driver.origin)
        if not s.multiple:
            continue
        primary = s.driver_name(s.driver)
        others = [(s.driver_name(d), d) for d in s.drivers
                  if d is not s.driver and d.names_net()]
        others = [o for o in others if o[0] != primary]
        if others:
            secondary, item = min(others, key=lambda o: o[0])
            out.append({"type": MNN,
                        "description": f"Both {primary} and {secondary} are attached to the "
                                       f"same items; {primary} will be used in the netlist",
                        "items": [_describe(s.driver), _describe(item)]})
    return out


def netlist(schematic, project=None) -> dict:
    """The netlist and multiple_net_names violations kicad-cli gives for
    ``schematic``, under the settings of ``project`` (the .kicad_pro beside the
    schematic when None, KiCad's defaults without one). Raises KicadConstruct
    on a construct it does not read, InputError on a file it cannot read."""
    schematic = str(schematic)
    settings = _settings(schematic, None if project is None else str(project))
    tree = schematic_tree(schematic)
    version = _version(tree, schematic)
    if version < FORMAT_FLOOR:
        raise InputError(f"{schematic}: schematic version {version} is older than "
                         f"{FORMAT_FLOOR}, the oldest the native netlister reads; save it "
                         "again in KiCad")
    try:
        sheets, inputs = _hierarchy(schematic, tree, settings)
        _instances_free(sheets)
        subgraphs = [_Subgraph(sheet, group) for sheet, items, placed in sheets
                     for group in _connect(items, placed)]
        driven = _name_nets(subgraphs, _shared_refs(sheets))
        components, libparts = _components(sheets)
    except KicadConstruct as exc:
        exc.schematic_version = str(version)
        raise
    comment = next((c for c in sx.find_all(sx.find(tree, "title_block") or [], "comment")
                    if len(c) > 2 and str(c[1]) == "1"), None)
    return {
        "netlister": NETLISTER,
        "schematic_version": str(version),
        "inputs": inputs,
        "design": {"source": os.path.basename(schematic),
                   "sheets": [sheet.human for sheet, _, _ in sheets],
                   "title_block_comment_1": str(comment[2]) if comment is not None else ""},
        "components": components,
        "libparts": libparts,
        "nets": _nets(driven),
        "violations": _violations(subgraphs) if settings.check_names else [],
    }
