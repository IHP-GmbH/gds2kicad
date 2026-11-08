#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read an edited system schematic back as a connectivity form.

The KiCad side of the schematic import. A sheet chiplet_to_kicad_sch.py drew,
edited by hand and exported with ``kicad-cli sch export netlist --format
kicadsexpr``, becomes the tool-neutral form ``chiplet_system.connectivity``
1.0, which the import reads to write a .chiplet revision. This reader only
reports what KiCad resolved. Whether a reference is a component, a pin is on
its die or a name is legal is the import's call, made once for every tool.

  * symbols: every component of the netlist, its CHIPLET_COMPONENT identity
    (null without one) and its terminals, the pin numbers of its libpart. A
    co-simulation setup symbol (one with a non-empty chiplet_cosim_setup
    field) is left out, and so are the endpoints of its pins, so a sheet with
    setup symbols reads as the same sheet without them;
  * nets: every net with a node, name verbatim, ``auto_named`` when KiCad made
    the name up (``Net-(`` or ``unconnected-(``), endpoints as
    [reference, pin number];
  * a carrier's external pads, one chiplet_system:EXT_PAD symbol per io pad
    whose CHIPLET_COMPONENT is the carrier, fold back into the carrier: one
    symbol, reference and identity the carrier id, pins the pad ids, and each
    node (pad, "1") as (carrier, pad). The carriers and their pads are the
    generator report's components that are not dies and their pin lists;
  * base_sha256: the base digest the generator wrote in title-block comment 1;
  * refusals, by code, for what the form cannot carry:
      hierarchical_sheet   the netlist spans more than the root sheet
      local_label          a net named by a local label (a "/" prefix)
      multiple_net_names   ERC found two names on one net and KiCad kept one
      symbol_pin_mismatch  a node on a pin its symbol does not have, or a
                           symbol listing a pin number twice
      unknown_pad          an EXT_PAD naming a carrier the generator did not
                           draw, or a reference that is not one of its pads;
                           the symbol is listed as it is, unfolded
      duplicate_reference  a carrier's EXT_PAD shares its reference with another
                           symbol

The form names the generator report, the netlist, the schematic and the ERC
report by path and sha256, each path relative to the form's directory when the
two share a root below "/" and absolute otherwise.

``--blank --library LIB`` reads a sheet the student drew on the blank sheet of
chiplet_to_kicad_sch's ``--handout``, from its library LIB, and takes no
generator report. It writes form 1.1 with ``sheet`` "blank": ``base_sha256``
and ``generator_report`` null, the library by path and sha256, each symbol's
``identity`` null and its ``part`` (library nickname and symbol name, from the
netlist's libsource) instead. One more refusal applies there, since the
designator is the only identity a blank sheet has:
      duplicate_reference  two symbols share a reference (two left
                           unannotated, both U?)
``--classic --library LIB --system-library SYSTEM_LIB`` writes form 1.3,
``sheet`` "classic". The form adds each component's UUID ``key``, verbatim
``value``, ``footprint`` and ``fields``, and explicitly flagged no-connect
pins as ``open``. Each symbol also carries ``units``, one ``{name, pins}`` per
unit of its cached library symbol in the order the netlist's ``(units ...)``
gives them, and ``jumpers``, that symbol's ``duplicate_pin_numbers_are_jumpers``.
Both are computed from the cache rather than copied from the netlist because
kicad-cli 9.0.x writes neither and its netlists are read too; the citations for
them are to the tools image's 9.99 fork, the others to KiCad 9.0.2. A symbol
with no cached library symbol is an input error, since KiCad always saves one.
Only root-sheet symbols are written; hierarchy stays refused.
When KiCad merges references, the form keeps each distinct placed symbol and
its own fields, part and pins, so the import can report the shared reference.
The cached library symbol determines ``multi_unit``. ``rails`` comes from
placed chiplet_system:GND and chiplet_system:SUPPLY symbols in the root
schematic, while netlist names and endpoints stay the connectivity authority.
Further refusals on classic sheets:
      multi_unit           a component has more than one symbol unit
      bad_volts            a supply Volts is not a plain decimal above 0
      rail_volts_conflict  one rail name has different classes or Volts
      rail_unused          a drawn rail name is not a netlist net
      foreign_rail         a net-naming power symbol is not from chiplet_system
      unit_pin_order       two pins of one unit sit at one position with
                           different numbers, so KiCad orders them by chance
A refused rail is omitted from ``rails``, and a symbol refused for
``unit_pin_order`` carries no ``units``, so the form remains loadable.
Without ``--blank`` or ``--classic`` the form is 1.0 and carries neither
``sheet`` nor ``part``. Generated and blank forms retain reader version 1.1
and their existing bytes; classic forms use reader version 1.3.

ERC is an input, not something this reader runs: pass the JSON report of
``kicad-cli sch erc --format json --severity-all`` with ``--erc``, or say
``--no-erc`` and the form lists no ERC report among its inputs. The report must
name the same sheet. It does not say which checks ran or at what severity, so
the check is only as good as the command that wrote it.

Usage:
    kicad_sch_to_connectivity.py NETLIST SCHEMATIC GENERATOR_REPORT OUT_FORM \\
        (--erc ERC_JSON | --no-erc)
    kicad_sch_to_connectivity.py NETLIST SCHEMATIC OUT_FORM --blank --library LIB \\
        (--erc ERC_JSON | --no-erc)
    kicad_sch_to_connectivity.py NETLIST SCHEMATIC OUT_FORM --classic \\
        --library LIB --system-library SYSTEM_LIB (--erc ERC_JSON | --no-erc)

Exit status: 0 the form is written, refusals included; 2 an input is missing
or is not what it claims to be, and no form is written.
"""
import argparse
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import List, NamedTuple, Optional

import sexpr_io as sx
from _paths import atomic_write
from chiplet_to_kicad_sch import (BASE_DIGEST_PREFIX, IDENTITY_PROPERTY, SETUP_MARKER,
                                  _report_ref, _sha256)
from sheet_check import fold_pads, pad_carriers

FORM_FORMAT = "chiplet_system.connectivity"
FORM_VERSION = "1.0"
BLANK_FORM_VERSION = "1.1"
CLASSIC_FORM_VERSION = "1.3"
READER = "kicad_sch_to_connectivity"
# Latest reader revision; older modes retain their own revision for byte stability.
READER_VERSION = "1.3"
_LEGACY_READER_VERSION = "1.1"

AUTO_PREFIXES = ("Net-(", "unconnected-(")
ERC_CHECK = "multiple_net_names"
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_UNIT_SUFFIX = re.compile(r"_(\d+)_\d+$")
_VOLTS = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_RAILS = {"chiplet_system:GND": "ground", "chiplet_system:SUPPLY": "power"}
_IU_PER_MM = 10000          # schIUScale.IU_PER_MM
_JUMPERS = "duplicate_pin_numbers_are_jumpers"

EXIT_WRITTEN = 0
EXIT_INPUT = 2


class InputError(Exception):
    """An input is missing or is not what it claims to be; no form is written."""


def _refusal(code: str, message: str, **where) -> dict:
    return {"code": code, "message": message, **where}


def _value(node, tag) -> Optional[str]:
    child = sx.find(node, tag)
    return str(child[1]) if child is not None and len(child) > 1 else None


def _property(comp, name) -> Optional[str]:
    for prop in sx.find_all(comp, "property"):
        if _value(prop, "name") == name:
            return _value(prop, "value")
    return None


def _identity(comp) -> Optional[str]:
    return _property(comp, IDENTITY_PROPERTY)


def _base_digest(design) -> str:
    root = next((s for s in sx.find_all(design, "sheet") if _value(s, "name") == "/"), None)
    block = sx.find(root, "title_block") if root is not None else None
    for comment in sx.find_all(block or [], "comment"):
        value = _value(comment, "value") or ""
        if _value(comment, "number") == "1" and value.startswith(BASE_DIGEST_PREFIX):
            if value[len(BASE_DIGEST_PREFIX):]:
                return value[len(BASE_DIGEST_PREFIX):]
    raise InputError("the root title block has no base digest in comment 1; "
                     "the sheet was not drawn by chiplet_to_kicad_sch")


def _erc_refusals(path, schematic) -> List[dict]:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise InputError(f"ERC report {path} is unreadable: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("sheets"), list):
        raise InputError(f"ERC report {path} is not a kicad-cli JSON report")
    source = doc.get("source")
    if not isinstance(source, str) or os.path.basename(source) != os.path.basename(schematic):
        raise InputError(f"ERC report {path} is for {source!r}, "
                         f"not {os.path.basename(schematic)}")
    out = []
    for sheet in doc["sheets"]:
        for v in (sheet.get("violations") or []) if isinstance(sheet, dict) else []:
            if isinstance(v, dict) and v.get("type") == ERC_CHECK:
                items = "; ".join(str(i.get("description")) for i in v.get("items") or []
                                  if isinstance(i, dict))
                out.append(_refusal(ERC_CHECK, f"ERC: {v.get('description')}"
                                    + (f" ({items})" if items else "")))
    return out


class _Mode(NamedTuple):
    version: str
    sheet: Optional[str]
    reader: str
    drawn: bool
    sources: tuple


_MODES = {
    "generated": _Mode(FORM_VERSION, None, _LEGACY_READER_VERSION, False, ("generator_report",)),
    "blank": _Mode(BLANK_FORM_VERSION, "blank", _LEGACY_READER_VERSION, True, ("library",)),
    "classic": _Mode(CLASSIC_FORM_VERSION, "classic", READER_VERSION, True,
                     ("library", "system_library")),
}


def _mode(name, sources):
    if name is None:
        name = "classic" if sources["system_library"] is not None else (
            "blank" if sources["library"] is not None else "generated")
    if name not in _MODES:
        raise ValueError(f"unknown sheet mode {name!r}")
    mode = _MODES[name]
    if {k for k, v in sources.items() if v is not None} != set(mode.sources):
        needed = ", ".join(k.replace("_", " ") for k in mode.sources)
        raise ValueError(f"{name} mode takes {needed} and no other library or generator report")
    return mode


def _parse(path, head, what):
    try:
        tree = sx.parse(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise InputError(f"{what} {path} is unreadable: {exc}") from exc
    if not isinstance(tree, list) or sx.head(tree) != head:
        raise InputError(f"{path} is not a {what}")
    return tree


def _sheet_properties(symbol, *, include_private=True):
    """A placed symbol's properties, name to raw text. KiCad 9 writes a private
    one as ``(property private "Name" "Value")``; ``include_private=False``
    leaves those out, as the netlist does (netlist_exporter_xml.cpp:165,203 at
    9.0.2)."""
    fields = {}
    for prop in sx.find_all(symbol, "property"):
        private = len(prop) > 1 and prop[1] == "private" and not isinstance(prop[1], sx.Quoted)
        offset = 2 if private else 1
        if len(prop) > offset and (include_private or not private):
            fields[str(prop[offset])] = str(prop[offset + 1]) if len(prop) > offset + 1 else ""
    return fields


def _netlist_fields(comp):
    return {_value(f, "name"): str(f[-1]) if len(f) > 2 else ""
            for f in sx.find_all(sx.find(comp, "fields") or [], "field")}


def _cache_key(symbol):
    return _value(symbol, "lib_name") or _value(symbol, "lib_id")


class _Sheet(NamedTuple):
    placed: list
    by_uuid: dict
    cache: dict
    multi_unit: set


def _iu(text) -> int:
    """A coordinate in schematic internal units, rounded as KiROUND rounds."""
    value = float(text) * _IU_PER_MM
    return int(math.copysign(math.floor(abs(value) + 0.5), value))


def _alnum_pin(number):
    """``(prefix, value)`` of a pin number with a numeric tail, value -1 when
    there is none (string_utils.cpp:1514-1537)."""
    head = len(number.rstrip("0123456789"))
    return (number[:head], int(number[head:])) if head < len(number) else ("", -1)


def _expand(number):
    """The numbers a pin number stands for: ``[1-3,A5]`` is 1, 2, 3, A5; any
    other number, a malformed stack included, stands for itself
    (ExpandStackedPinNotation, string_utils.cpp:1542-1621)."""
    if not (number.startswith("[") and number.endswith("]")):
        return [number]
    out = []
    for part in (p.strip() for p in number[1:-1].split(",")):
        if not part:
            continue
        if "-" in part:
            first, _, last = part.partition("-")
            (prefix, lo), (other, hi) = _alnum_pin(first.strip()), _alnum_pin(last.strip())
            if prefix != other or lo == -1 or hi == -1 or lo > hi:
                return [number]
            out.extend(f"{prefix}{i}" for i in range(lo, hi + 1))
        else:
            out.append(part)
    return out or [number]


def _unit_letter(unit):
    """LIB_SYMBOL::LetterSubReference(unit, 'A'): A..Z, then AA, AB, ..."""
    suffix = ""
    while True:
        u = (unit - 1) % 26
        suffix = chr(ord("A") + u) + suffix
        unit = (unit - u) // 26
        if unit <= 0:
            return suffix


def _unit_pins(cached):
    """``(units, clash)`` of a cached library symbol, as
    LIB_SYMBOL::GetUnitPinInfo builds the netlist's ``(units ...)``
    (lib_symbol.cpp:783-837): for each unit, its display name or letter, and
    the numbers of its pins and of the pins common to every unit, any body
    style, sorted by position, x then y in internal units (the file's y
    negated), stacks expanded, each number once.

    KiCad's sort is not stable, so two pins at one position with different
    numbers come out in either order; ``clash`` names the first such pair as
    ``(unit name, number, number)`` and is None otherwise."""
    if sx.find(cached, "extends") is not None:
        raise InputError(f"cached symbol {cached[1]} extends another; KiCad saves the "
                         "cache flattened, so save the schematic again in KiCad")
    count, names, pins = 1, {}, []
    for unit in sx.find_all(cached, "symbol"):
        match = _UNIT_SUFFIX.search(str(unit[1]))
        if match is None:
            raise InputError(f"cached symbol {cached[1]} has a unit named {unit[1]}, "
                             "which KiCad cannot read")
        index = int(match[1])
        count = max(count, index)
        label = sx.find(unit, "unit_name")
        if label is not None and len(label) > 1:
            names[index] = str(label[1])
        for pin in sx.find_all(unit, "pin"):
            at = sx.find(pin, "at") or ["at", "0", "0"]     # a pin's default position
            pins.append((index, (_iu(at[1]), -_iu(at[2])), _expand(_value(pin, "number") or "")))
    units, clash = [], None
    for index in range(1, count + 1):
        name = names.get(index, _unit_letter(index))
        drawn = sorted((p for p in pins if p[0] in (0, index)), key=lambda p: p[1])
        numbers = []
        for i, (_unit, at, expanded) in enumerate(drawn):
            if clash is None and i and drawn[i - 1][1] == at and drawn[i - 1][2] != expanded:
                clash = (name, drawn[i - 1][2][0], expanded[0])
            numbers.extend(n for n in expanded if n not in numbers)
        units.append({"name": name, "pins": numbers})
    return units, clash


def _jumpers(cached) -> bool:
    flag = _value(cached, _JUMPERS)
    if flag not in (None, "yes", "no"):
        raise InputError(f"cached symbol {cached[1]} states {_JUMPERS} {flag!r}, "
                         "neither yes nor no")
    return flag == "yes"


def _sheet_data(path):
    tree = _parse(path, "kicad_sch", "KiCad schematic")
    cache, multi = {}, set()
    for symbol in sx.find_all(sx.find(tree, "lib_symbols") or [], "symbol"):
        name = str(symbol[1])
        cache[name] = symbol
        for unit in sx.find_all(symbol, "symbol"):
            match = _UNIT_SUFFIX.search(str(unit[1]))
            if match and int(match[1]) > 1:
                multi.add(name)
    placed = sx.find_all(tree, "symbol")
    return _Sheet(placed, {_value(s, "uuid"): s for s in placed
                          if _value(s, "uuid") is not None}, cache, multi)


def _libpins(tree):
    return {(_value(part, "lib"), _value(part, "part")):
            [_value(p, "num") for p in sx.find_all(sx.find(part, "pins") or [], "pin")]
            for part in sx.find_all(sx.find(tree, "libparts") or [], "libpart")}


def _checked_symbols(records, drawn, multi=()):
    symbols, refusals = [], []
    for record in records:
        ref, nums = record["ref"], record["pins"]
        for num in sorted({n for n in nums if nums.count(n) > 1}):
            refusals.append(_refusal("symbol_pin_mismatch",
                                     f"symbol {ref} has pin {num} more than once",
                                     component=ref, terminal=num))
        symbols.append({**record, "pins": sorted(set(nums))})
    for ref in sorted(set(multi)):
        refusals.append(_refusal("multi_unit", f"symbol {ref} has multiple units; "
                                 "use a single-unit symbol", component=ref))
    if drawn:
        refs = [s["ref"] for s in symbols]
        for ref in sorted({r for r in refs if refs.count(r) > 1}):
            refusals.append(_refusal("duplicate_reference",
                                     f"{refs.count(ref)} symbols are named {ref}; "
                                     "annotate the sheet so every symbol has its own",
                                     component=ref))
    return symbols, refusals


def _setup_refs(components):
    return {_value(comp, "ref") for comp in components if _property(comp, SETUP_MARKER)}


def _carrier_pads(report) -> dict:
    """Each carrier the generator drew, by id, to its pad ids: the report's
    components that are not dies, each with the pin names of the carrier pin
    list the report names by path and sha256. A report without components
    (setup only) drew none."""
    doc = _json(report, "generator report")
    if not isinstance(doc, dict):
        raise InputError(f"generator report {report} is not a JSON object")
    carriers = {}
    for comp in doc.get("components") or []:
        if not isinstance(comp, dict) or comp.get("type") == "die":
            continue
        cid = comp.get("id")
        entry = (doc.get("pins") or {}).get(cid)
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise InputError(f"generator report {report} names no pin list for carrier {cid}")
        path = Path(report).parent / entry["path"]
        if not path.is_file() or _sha256(path) != entry.get("sha256"):
            raise InputError(f"carrier {cid}'s pin list {path} is missing or not the one "
                             f"generator report {report} names")
        pins = _json(path, f"carrier {cid}'s pin list")
        pins = pins.get("pins") if isinstance(pins, dict) else None
        if not isinstance(pins, list):
            raise InputError(f"carrier {cid}'s pin list {path} has no 'pins' array")
        carriers[cid] = {p.get("name") for p in pins if isinstance(p, dict)}
    return carriers


def _json(path, what):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise InputError(f"{what} {path} is unreadable: {exc}") from exc


def _fold(components, carriers):
    """``(pads, refusals)`` of ``pad_carriers`` over the sheet's symbols other
    than setup symbols."""
    symbols = []
    for comp in components:
        if not _property(comp, SETUP_MARKER):
            src = sx.find(comp, "libsource") or []
            symbols.append((_value(comp, "ref"), _value(src, "lib"), _value(src, "part"),
                            _identity(comp)))
    return pad_carriers(symbols, carriers)


def _symbols(components, libpins, drawn, pads):
    """Each component's record, a carrier's folded pads (``pads``, reference to
    carrier) read as the one carrier symbol."""
    records = []
    for comp in components:
        ref = _value(comp, "ref")
        if _property(comp, SETUP_MARKER) or ref in pads:
            continue
        src = sx.find(comp, "libsource") or []
        lib, part = _value(src, "lib"), _value(src, "part")
        record = {"ref": ref, "identity": None if drawn else _identity(comp),
                  "pins": libpins.get((lib, part), [])}
        if drawn:
            record["part"] = {"lib": lib, "name": part}
        records.append(record)
    for carrier in sorted(set(pads.values())):
        records.append({"ref": carrier, "identity": carrier,
                        "pins": sorted(r for r, c in pads.items() if c == carrier)})
    return _checked_symbols(records, drawn)


def _lib_id(placed):
    lib, _, part = (_value(placed, "lib_id") or "").partition(":")
    return lib, part


def _classic_record(comp, uuid, *, lib, name, pins, value, footprint, fields):
    return {"ref": _value(comp, "ref"), "identity": None,
            "part": {"lib": lib, "name": name}, "pins": pins,
            "key": "/" + uuid, "value": value, "footprint": footprint, "fields": fields}


def _netlist_record(comp, uuid, placed, libpins):
    """A component KiCad wrote for one placed symbol, as the netlist gives it."""
    source = sx.find(comp, "libsource") or []
    lib, part = _value(source, "lib"), _value(source, "part")
    pins = libpins.get((lib, part), [])
    if not lib:
        # A symbol stored under lib_name is exported with libsource lib "" and
        # the lib_name as part (netlist_exporter_xml.cpp:327-340 at 9.0.2).
        lib, part = _lib_id(placed)
    return _classic_record(comp, uuid, lib=lib, name=part, pins=pins,
                           value=_value(comp, "value") or "~",
                           footprint=_value(comp, "footprint") or "",
                           fields=_netlist_fields(comp))


def _split_record(comp, uuid, placed, sheet, libpins):
    """One placed symbol of a reference KiCad merged with others, read from the
    sheet in the netlist's shape: no Reference, Value or private fields, an
    empty value as ``~``. KiCad lists a libpart only for the main symbol of a
    reference, the one of the lowest UUID (netlist_exporter_base.cpp:122 at
    9.0.2), so pins missing there come from the cached symbol, each number once
    as a libpart lists them (netlist_exporter_xml.cpp:700-707). The sheet is refused for the shared reference,
    so text variables and escapes in the fields stay unresolved."""
    fields = _sheet_properties(placed, include_private=False)
    value = fields.pop("Value", "") or "~"
    fields.pop("Reference", None)
    alias = _value(placed, "lib_name")
    pins = libpins.get(("", alias) if alias else _lib_id(placed))
    if pins is None:
        cached = sheet.cache.get(_cache_key(placed), [])
        pins = list(dict.fromkeys(n for pin in sx.find_all_rec(cached, "pin")
                                  for n in _expand(_value(pin, "number") or "")))
    lib, name = _lib_id(placed)
    return _classic_record(comp, uuid, lib=lib, name=name, pins=pins, value=value,
                           footprint=fields.get("Footprint", ""), fields=fields)


def _placed_records(comp, sheet, libpins):
    ref = _value(comp, "ref")
    stamps = sx.find(comp, "tstamps") or []
    uuids = [str(stamp) for stamp in stamps[1:]]
    if not uuids or any(_UUID.fullmatch(stamp) is None for stamp in uuids):
        raise InputError(f"symbol {ref} has no valid component UUID in tstamps; "
                         "export the schematic again with kicad-cli")
    if any(uuid not in sheet.by_uuid for uuid in uuids):
        raise InputError(f"symbol {ref} has a UUID absent from the root schematic; "
                         "export the schematic again with kicad-cli")
    placed = [sheet.by_uuid[u] for u in uuids]
    multi = any(_cache_key(s) in sheet.multi_unit for s in placed)
    parts = {_cache_key(s) for s in placed}
    units = [_value(s, "unit") or "1" for s in placed]
    shared = len(uuids) > 1 and (not multi or len(parts) > 1 or len(set(units)) < len(units))
    # The units of one multi-unit symbol: KiCad 9 writes the primary UUID last
    # (netlist_exporter_xml.cpp:441-456 at 9.0.2).
    pairs = list(zip(uuids, placed)) if shared else [(uuids[-1], placed[-1])]
    records, refusals = [], []
    for uuid, symbol in pairs:
        row = (_split_record(comp, uuid, symbol, sheet, libpins) if shared
               else _netlist_record(comp, uuid, symbol, libpins))
        if not row["part"]["lib"] or not row["part"]["name"]:
            raise InputError(f"symbol {ref} has no qualified lib_id in the root schematic")
        if any(not name for name in row["fields"]):
            raise InputError(f"symbol {ref} has an unnamed field")
        cached = sheet.cache.get(_cache_key(symbol))
        if cached is None:
            raise InputError(f"symbol {ref} has no cached library symbol; "
                             "save the schematic again in KiCad")
        units, clash = _unit_pins(cached)
        row["jumpers"] = _jumpers(cached)
        if clash is None:
            row["units"] = units
        else:
            refusals.append(_refusal(
                "unit_pin_order", f"symbol {ref} has pins {clash[1]} and {clash[2]} of "
                f"unit {clash[0]} at one position, so KiCad lists them in either order; "
                "move one", component=ref))
        records.append(row)
    return records, multi, refusals


def _volts_ok(value):
    # Mirrors chiplet-spec connectivity._volts, including nonzero without float rounding.
    return _VOLTS.fullmatch(value) is not None and bool(value.strip("0."))


def _rail_name(value):
    # KiCad 9.0.2 sch_pin.cpp:1639 uses EscapeString(Value, CTX_NETNAME).
    # common/string_utils.cpp:179,188-194 replaces '/' and drops CR/LF only.
    # Text variables are not resolved here and may remain a rail_unused refusal.
    return value.replace("/", "{slash}").replace("\r", "").replace("\n", "")


class _RailRow(NamedTuple):
    net_class: Optional[str]
    volts: Optional[str]
    foreign_lib: Optional[str]


def _classic_rails(sheet, net_names):
    """One refusal per name: bad volts, foreign, conflicting, then unused."""
    groups = {}
    for symbol in sheet.placed:
        cached = sheet.cache.get(_cache_key(symbol), [])
        # KiCad 9.0.2 sch_pin.cpp:363-366, IsGlobalPower: only power_in
        # pins on power symbols name nets. A power_out PWR_FLAG is not a rail.
        if (sx.find(cached, "power") is None
                or not any(p[1] == "power_in" for p in sx.find_all_rec(cached, "pin"))):
            continue
        lib_id = _value(symbol, "lib_id")
        props = _sheet_properties(symbol)
        name = _rail_name(props.get("Value", ""))
        net_class = _RAILS.get(lib_id)
        volts = (props.get("Volts") or None) if net_class == "power" else None
        row = _RailRow(net_class, volts, lib_id if net_class is None else None)
        groups.setdefault(name, []).append(row)
    rails, refusals = [], []
    for name, rows in sorted(groups.items()):
        bad = sorted({r.volts for r in rows if r.volts is not None and not _volts_ok(r.volts)})
        foreign_libs = sorted({r.foreign_lib for r in rows if r.foreign_lib is not None})
        variants = {(r.net_class, r.volts) for r in rows}
        if bad:
            refusal = _refusal("bad_volts", f"rail {name} has invalid Volts {bad!r}; "
                               "write 3.3, not 3V3 or 3.3 V (a plain decimal above 0)", net=name)
        elif foreign_libs:
            refusal = _refusal("foreign_rail", f"power symbol {', '.join(foreign_libs)} "
                               "is not supported; use chiplet_system:GND or "
                               "chiplet_system:SUPPLY", net=name)
        elif len(variants) > 1:
            refusal = _refusal("rail_volts_conflict", f"rail {name} has different classes "
                               "or Volts; use one class and the same Volts on every occurrence",
                               net=name)
        elif name not in net_names:
            refusal = _refusal("rail_unused", f"rail {name} is not a net in the netlist; "
                               "connect it to a component pin", net=name)
        else:
            cls, volts = next(iter(variants))
            rails.append({"name": name, "class": cls, "volts": volts})
            continue
        refusals.append(refusal)
    return rails, refusals


def _classic_pass(components, libpins, schematic, nets, opened, setup):
    """Replace KiCad's merged symbol view with root-sheet records and metadata."""
    sheet = _sheet_data(schematic)
    records, multi, refused = [], [], []
    for comp in components:
        if (_value(sx.find(comp, "sheetpath") or [], "names") != "/"
                or _value(comp, "ref") in setup):
            continue
        placed, is_multi, unit_refusals = _placed_records(comp, sheet, libpins)
        records.extend(placed)
        refused.extend(unit_refusals)
        if is_multi:
            multi.append(_value(comp, "ref"))
    symbols, refusals = _checked_symbols(records, drawn=True, multi=multi)
    rails, rail_refusals = _classic_rails(sheet, {n["name"] for n in nets})
    return symbols, {"rails": rails, "open": [list(ep) for ep in sorted(opened)]}, [
        *refusals, *refused, *rail_refusals]


def _nets(tree, setup, pads):
    """Every net with a node, a node on a folded pad (``pads``) as the
    carrier's terminal."""
    nets, opened = [], set()
    for net in sx.find_all(sx.find(tree, "nets") or [], "net"):
        endpoints = set()
        for node in sx.find_all(net, "node"):
            ref, pin = fold_pads((_value(node, "ref"), _value(node, "pin")), pads)
            if ref in setup:
                continue
            endpoints.add((ref, pin))
            if (_value(node, "pintype") or "").endswith("+no_connect"):
                opened.add((ref, pin))
        if endpoints:
            name = _value(net, "name")
            nets.append({"name": name, "auto_named": name.startswith(AUTO_PREFIXES),
                         "endpoints": [list(ep) for ep in sorted(endpoints)]})
    return nets, opened


def _net_refusals(nets, symbols, drawn, pads):
    pins_of, refusals = {ref: {"1"} for ref in pads}, []
    for symbol in symbols:
        ref = symbol["ref"]
        pins_of[ref] = (pins_of.get(ref, set()) if drawn else set()) | set(symbol["pins"])
    for net in nets:
        name = net["name"]
        if name.startswith("/"):
            refusals.append(_refusal("local_label", f"net {name} is named by a local label; "
                                     "use a global label", net=name))
        for ref, pin in net["endpoints"]:
            if ref in pins_of and pin not in pins_of[ref]:
                refusals.append(_refusal("symbol_pin_mismatch", f"net {name} reaches pin {pin} "
                                         f"of {ref}, which its symbol does not have",
                                         component=ref, terminal=pin, net=name))
    return refusals


def read_form(netlist, schematic, generator_report, out_form, erc=None,
              library=None, *, system_library=None, mode=None) -> dict:
    """Read generated (1.0), blank (1.1), or classic (1.3) connectivity.

    Infer the mode from the libraries, or validate an explicit mode. Generated
    sheets take a generator report, blank sheets one library, classic sheets
    both libraries. Older modes preserve their output bytes and reader revision.
    """
    sources = {"generator_report": generator_report, "library": library,
               "system_library": system_library}
    mode = _mode(mode, sources)
    paths = (netlist, schematic, generator_report, library, system_library, erc)
    for path in (p for p in paths if p is not None):
        if not os.path.isfile(path):
            raise InputError(f"no such file: {path}")
    tree = _parse(netlist, "export", "kicadsexpr netlist")
    design = sx.find(tree, "design") or []
    source = _value(design, "source")
    if source is not None and os.path.basename(source) != os.path.basename(schematic):
        raise InputError(f"the netlist was exported from {os.path.basename(source)}, "
                         f"not {os.path.basename(schematic)}")
    base = None if mode.drawn else _base_digest(design)
    refusals: List[dict] = []

    for sheet in sx.find_all(design, "sheet"):
        name = _value(sheet, "name")
        if name != "/":
            refusals.append(_refusal("hierarchical_sheet",
                                     f"sub-sheet {name}: only the root sheet is read"))

    components = sx.find_all(sx.find(tree, "components") or [], "comp")
    libpins = _libpins(tree)
    setup = _setup_refs(components)
    pads = {}
    if mode.sheet is None:
        pads, pad_refusals = _fold(components, _carrier_pads(generator_report))
        refusals.extend(pad_refusals)
    nets, opened = _nets(tree, setup, pads)
    extra = {}
    if mode.sheet == "classic":
        symbols, extra, symbol_refusals = _classic_pass(
            components, libpins, schematic, nets, opened, setup)
    else:
        symbols, symbol_refusals = _symbols(components, libpins, mode.drawn, pads)
    refusals.extend(symbol_refusals)
    refusals.extend(_net_refusals(nets, symbols, mode.drawn, pads))

    if erc:
        refusals.extend(_erc_refusals(erc, schematic))

    here = os.path.dirname(os.path.abspath(out_form))
    inputs = [p for p in (netlist, schematic, erc) if p]
    head = {"format": FORM_FORMAT, "version": mode.version, "base_sha256": base,
            "generator_report": None}
    if mode.drawn:
        head["sheet"] = mode.sheet
    for name in mode.sources:
        path = sources[name]
        head[name] = {"path": _report_ref(path, here), "sha256": _sha256(path)}
    return {
        **head,
        **extra,
        "front_end": {"tool": "kicad", "tool_version": _value(design, "tool"),
                      "reader": f"{READER} {mode.reader}",
                      "inputs": [{"path": _report_ref(p, here), "sha256": _sha256(p)}
                                 for p in inputs]},
        "symbols": sorted(symbols, key=_drawn_key if mode.drawn else lambda s: s["ref"]),
        "nets": sorted(nets, key=lambda n: n["name"]),
        "refusals": refusals,
    }


def _drawn_key(symbol):
    """Symbols sharing a reference (refused) still sort the same every run."""
    part = symbol["part"]
    return symbol["ref"], part["lib"] or "", part["name"] or "", symbol["pins"], symbol.get("key", "")


def write_form(form: dict, out_form) -> None:
    Path(out_form).parent.mkdir(parents=True, exist_ok=True)
    with atomic_write(out_form) as fh:
        fh.write(json.dumps(form, indent=2, sort_keys=True, allow_nan=False) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Read a kicad-cli netlist of an edited system sheet as a "
                    "connectivity form for the schematic import.")
    ap.usage = ("%(prog)s NETLIST SCHEMATIC GENERATOR_REPORT OUT_FORM (--erc ERC | --no-erc)\n"
                "       %(prog)s NETLIST SCHEMATIC OUT_FORM --blank --library LIB "
                "(--erc ERC | --no-erc)\n"
                "       %(prog)s NETLIST SCHEMATIC OUT_FORM --classic --library LIB "
                "--system-library SYSTEM_LIB (--erc ERC | --no-erc)")
    ap.add_argument("paths", nargs="+", metavar="PATH",
                    help="NETLIST (kicadsexpr netlist of the edited sheet), SCHEMATIC "
                         "(the .kicad_sch it came from), GENERATOR_REPORT (the report "
                         "chiplet_to_kicad_sch wrote; generated sheets only), OUT_FORM "
                         "(connectivity form to write, JSON)")
    drawing = ap.add_argument_group("sheet mode and libraries")
    choice = drawing.add_mutually_exclusive_group()
    choice.add_argument("--blank", dest="mode", action="store_const", const="blank",
                        help="read a blank sheet as form 1.1 with --library")
    choice.add_argument("--classic", dest="mode", action="store_const", const="classic",
                        help="read a classic sheet as form 1.3, with both handout libraries")
    drawing.add_argument("--library", metavar="LIB",
                         help="with --blank or --classic, chiplet_dies.kicad_sym")
    drawing.add_argument("--system-library", metavar="SYSTEM_LIB",
                         help="with --classic, chiplet_system.kicad_sym")
    ap.set_defaults(mode="generated")
    erc = ap.add_mutually_exclusive_group(required=True)
    erc.add_argument("--erc", help="kicad-cli sch erc JSON report (--severity-all)")
    erc.add_argument("--no-erc", action="store_true",
                     help="read without ERC; multiple_net_names is not checked")
    # Intermixed, so options may sit between the paths.
    args = ap.parse_intermixed_args(argv)
    drawn = args.mode != "generated"
    if len(args.paths) != (3 if drawn else 4):
        ap.error("expected NETLIST SCHEMATIC OUT_FORM with --blank or --classic, else "
                 "NETLIST SCHEMATIC GENERATOR_REPORT OUT_FORM")
    if drawn:
        args.netlist, args.schematic, args.out_form = args.paths
        args.generator_report = None
    else:
        args.netlist, args.schematic, args.generator_report, args.out_form = args.paths
    try:
        _mode(args.mode, {"generator_report": args.generator_report, "library": args.library,
                          "system_library": args.system_library})
    except ValueError as exc:
        ap.error(str(exc))
    try:
        form = read_form(args.netlist, args.schematic, args.generator_report,
                         args.out_form, erc=args.erc, library=args.library,
                         mode=args.mode, system_library=args.system_library)
    except InputError as exc:
        print(f"{READER}: {exc}", file=sys.stderr)
        return EXIT_INPUT
    write_form(form, args.out_form)
    print(f"{args.out_form}: {len(form['symbols'])} symbols, {len(form['nets'])} nets, "
          f"{len(form['refusals'])} refusals")
    return EXIT_WRITTEN


if __name__ == "__main__":
    sys.exit(main())
