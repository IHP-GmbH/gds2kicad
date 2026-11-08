#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read edited co-simulation setup sheets back as a setup form.

The KiCad reader of the co-simulation setup. The sheets chiplet_to_kicad_sch.py
``--setup`` drew, edited by hand and each exported with ``kicad-cli sch export
netlist --format kicadsexpr``, become the tool-neutral form
``chiplet_cosim.cosim_setup`` 1.0 (``tool.name`` kicad), which the co-simulation
flow's back end reads to write a revision of cosim.yaml. This reader only reports what
KiCad resolved. Whether a kind, an attribute, a pin or a net name is right is
the back end's call, made once for every tool. The reader refuses only what a
KiCad sheet can carry that the form would pass on as a real value: an
attribute left empty, and a value KiCad rounded (both below).

  * sheets: one per ``--sheet``, its role (and die or net) from the generator
    report, which lists every setup sheet it drew by file name. The sheet is
    named by path and sha256, and a bench or channel sheet also names its
    SPICE netlist (``kicad-cli sch export netlist --format spice``), which
    this reader does not parse;
  * symbols: every component with a non-empty ``chiplet_cosim_setup`` field,
    its kind (that field), its attributes (every other property but those
    KiCad adds of its own: Sheetname, Sheetfile, the exclude and dnp flags,
    ki_*) and its pins, the pin numbers of its libpart;
  * a pin is open (null) when it is on no net, or alone on an
    ``unconnected-(`` net, as a no-connect flag leaves it. Otherwise it is on
    its net, name verbatim, ``named`` false when KiCad made the name up
    (``Net-(`` or ``unconnected-(``). A local label or a sheet path stays in
    the name, for the back end to refuse.

An attribute that is empty is refused as ``attribute_empty``, naming the symbol
and the attribute: it is a field of a symbol placed from the setup library that
nobody filled in, and the generator never draws one.

A bench or channel sheet's SPICE netlist is read only to check it: every R, C,
L, V or I card must carry the value its symbol's fields state, read as numbers
(``5n`` equals ``5e-9``). KiCad writes a raw SPICE model's text into the card
as drawn only when it is digits and points; any other number, and a value it
infers from Value or reads from a typed model, goes through its number
formatting, which keeps six significant digits. A card that lost a digit (a
value typed in e-notation with more than six), or that no longer carries an
edited Value, is refused as ``value_rounded``, naming the element and both
texts.

Paths are relative to the form's directory when the two share a root below
"/", absolute otherwise. ``tool.version`` is the netlist's tool line, which
every sheet's netlist must name, and share; a netlist must also name the sheet
it was exported from.

Usage:
    kicad_sch_to_cosim_setup.py GENERATOR_REPORT OUT_FORM \\
        --sheet SCHEMATIC NETLIST [SPICE] [--sheet ...]

Exit status: 0 the form is written; 2 an input is missing or is not what it
claims to be, and no form is written.
"""
import argparse
import json
import os
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

import sexpr_io as sx
from chiplet_to_kicad_sch import REPORT_FORMAT, SETUP_MARKER, _report_ref, _sha256
from cosim_setup_sheets import FORM_FORMAT, FORM_VERSION, SHEET_KEY, SPEC_SHEET, kicad_own
from kicad_sch_to_connectivity import (AUTO_PREFIXES, EXIT_INPUT, EXIT_WRITTEN, InputError,
                                       _value, write_form)

READER = "kicad_sch_to_cosim_setup"
TOOL = "kicad"
OPEN_PREFIX = "unconnected-("
SPICE_ROLES = ("bench", "channel")
SPICE_LETTERS = "RCLVI"
# Scale factors as ngspice reads them (any case, the longest first), and as KiCad
# reads a field, where M is mega and m milli.
SPICE_SCALES = (("meg", "1e6"), ("mil", "25.4e-6"), ("t", "1e12"), ("g", "1e9"),
                ("k", "1e3"), ("m", "1e-3"), ("u", "1e-6"), ("n", "1e-9"), ("p", "1e-12"),
                ("f", "1e-15"))
_NUMBER = re.compile(r"([+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)(\S*)\Z")
_PARAM = re.compile(r'(\w+)=(?:"((?:[^"\\]|\\.)*)"|(\S*))')


def _report_sheets(generator_report) -> Dict[str, dict]:
    """File name -> {role, die or net} of every setup sheet the report lists."""
    try:
        doc = json.loads(Path(generator_report).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise InputError(f"generator report {generator_report} is unreadable: {exc}") from exc
    doc = doc if isinstance(doc, dict) else {}
    if doc.get("format") != REPORT_FORMAT:
        raise InputError(f"{generator_report} is not a {REPORT_FORMAT} report")
    setup = doc.get("setup")
    rows = setup.get("sheets") if isinstance(setup, dict) else None
    if not isinstance(rows, list):
        raise InputError(f"{generator_report} lists no setup sheets; draw them with "
                         f"chiplet_to_kicad_sch.py --setup")
    out = {}
    for row in rows:
        row = row if isinstance(row, dict) else {}
        role = row.get("role")
        if role not in SPEC_SHEET:
            raise InputError(f"{generator_report} lists a setup sheet with role {role!r}")
        if not isinstance(row.get("file"), str):
            raise InputError(f"{generator_report} lists a {role} sheet without its file")
        key = SHEET_KEY.get(role)
        if key and not isinstance(row.get(key), str):
            raise InputError(f"{generator_report} lists a {role} sheet without its {key}")
        name = os.path.basename(row["file"])
        if name in out:
            raise InputError(f"{generator_report} lists {name} twice")
        out[name] = {"role": role, **({key: row[key]} if key else {})}
    return out


def _netlist(netlist, schematic):
    """The parsed kicadsexpr export of ``schematic``."""
    try:
        tree = sx.parse(Path(netlist).read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise InputError(f"netlist {netlist} is unreadable: {exc}") from exc
    if not isinstance(tree, list) or sx.head(tree) != "export":
        raise InputError(f"{netlist} is not a kicadsexpr netlist")
    design = sx.find(tree, "design") or []
    source = _value(design, "source")
    if not source:
        raise InputError(f"{netlist} names no source sheet")
    if os.path.basename(source) != os.path.basename(schematic):
        raise InputError(f"{netlist} was exported from {os.path.basename(source)}, "
                         f"not {os.path.basename(schematic)}")
    if not _value(design, "tool"):
        raise InputError(f"{netlist} names no tool")
    return tree


def _pin(nets_of, ref, num) -> Optional[dict]:
    at = nets_of.get((ref, num))
    if at is None:
        return None
    name, count = at
    if name.startswith(OPEN_PREFIX) and count == 1:
        return None
    return {"net": name, "named": not name.startswith(AUTO_PREFIXES)}


def read_symbols(tree) -> List[dict]:
    """The setup symbols of one sheet's netlist, by reference."""
    libpins = {}
    for part in sx.find_all(sx.find(tree, "libparts") or [], "libpart"):
        nums = [_value(p, "num") for p in sx.find_all(sx.find(part, "pins") or [], "pin")]
        libpins[(_value(part, "lib"), _value(part, "part"))] = sorted(set(nums))
    nets_of = {}
    for net in sx.find_all(sx.find(tree, "nets") or [], "net"):
        nodes = sx.find_all(net, "node")
        for node in nodes:
            nets_of[(_value(node, "ref"), _value(node, "pin"))] = (_value(net, "name"),
                                                                   len(nodes))
    symbols = []
    for comp in sx.find_all(sx.find(tree, "components") or [], "comp"):
        props = {}
        for prop in sx.find_all(comp, "property"):
            name = _value(prop, "name")
            props[name] = _value(prop, "value") or ""
        kind = props.pop(SETUP_MARKER, "")
        if not kind:
            continue
        ref = _value(comp, "ref")
        src = sx.find(comp, "libsource") or []
        nums = libpins.get((_value(src, "lib"), _value(src, "part")), [])
        symbols.append({"ref": ref, "kind": kind,
                        "attributes": {n: v for n, v in props.items() if not kicad_own(n)},
                        "pins": {num: _pin(nets_of, ref, num) for num in nums}})
    return sorted(symbols, key=lambda s: s["ref"])


def _numbers(text, kicad=False) -> List[Decimal]:
    """Every number in ``text``, scaled, as ngspice reads a card (``kicad``: as
    KiCad reads a field). A token that does not start as a number is a word."""
    out = []
    for token in re.split(r'[\s(),="]+', text):
        m = _NUMBER.match(token)
        if not m:
            continue
        out.append(Decimal(m.group(1)) * _scale(m.group(2), kicad))
    return out


def _scale(rest, kicad) -> Decimal:
    if kicad and rest.startswith("M"):
        return Decimal("1e6")
    scales = [(s, f) for s, f in SPICE_SCALES if len(s) == 1] if kicad else SPICE_SCALES
    rest = rest.replace("\u00b5", "u").replace("\u03bc", "u").casefold()
    for suffix, factor in scales:
        if rest.startswith(suffix):
            return Decimal(factor)
    return Decimal(1)


def _cards(text) -> Dict[str, str]:
    """R, C, L, V and I card name -> its value text, continuation lines joined."""
    lines = []
    for line in text.splitlines():
        if line.startswith("+") and lines:
            lines[-1] += " " + line[1:]
        else:
            lines.append(line)
    out = {}
    for line in lines:
        tokens = line.split()
        if tokens and tokens[0][0].upper() in SPICE_LETTERS:
            out[tokens[0]] = " ".join(tokens[3:])
    return out


def _stated(comp) -> List[tuple]:
    """(field, text, kicad notation) of every field of a primitive that states
    its value: a raw SPICE model's text, a typed model's parameters, and Value
    when it holds a number."""
    props = {_value(p, "name"): _value(p, "value") or "" for p in sx.find_all(comp, "property")}
    params = {m.group(1): m.group(2) if m.group(2) is not None else m.group(3)
              for m in _PARAM.finditer(props.get("Sim.Params", ""))}
    out = []
    if props.get("Sim.Device", "").casefold() == "spice":
        out.append(("Sim.Params model", params.get("model", ""), False))
    elif params:
        out.append(("Sim.Params", props["Sim.Params"], True))
    out.append(("Value", _value(comp, "value") or "", True))
    return [row for row in out if _numbers(row[1], kicad=row[2])]


def _check_values(tree, netlist, spice):
    """Refuse a card whose value is not the one its symbol's fields state."""
    comps = {_value(c, "ref"): c for c in sx.find_all(sx.find(tree, "components") or [], "comp")}
    text = Path(spice).read_text(encoding="utf-8", errors="replace")
    for name, card in sorted(_cards(text).items()):
        comp = comps.get(name)
        if comp is None:
            raise InputError(f"{spice}: card {name} has no symbol of that reference in "
                             f"{netlist}; export both from the same sheet, and give a "
                             f"primitive a reference that starts with its SPICE letter")
        for field_name, stated, kicad in _stated(comp):
            if _numbers(card) != _numbers(stated, kicad=kicad):
                raise InputError(f"value_rounded: {name} is {card!r} in {spice} but its "
                                 f"{field_name} is {stated!r}")


def _check_filled(symbols, netlist):
    """Refuse a setup symbol with an attribute left empty."""
    for symbol in symbols:
        for name, value in sorted(symbol["attributes"].items()):
            if value == "":
                raise InputError(f"attribute_empty: {symbol['ref']} ({symbol['kind']}) in "
                                 f"{netlist} has {name} empty; fill it in, or delete the "
                                 f"field")


def read_form(generator_report, out_form, sheets) -> dict:
    """The setup form of the given sheets, each (schematic, netlist, spice or
    None), as a dict ready to dump."""
    for path in [generator_report] + [p for sheet in sheets for p in sheet if p]:
        if not os.path.isfile(path):
            raise InputError(f"no such file: {path}")
    drawn = _report_sheets(generator_report)
    here = os.path.dirname(os.path.abspath(out_form))
    out, seen, tools = [], set(), set()
    for schematic, netlist, spice in sheets:
        name = os.path.basename(schematic)
        if name not in drawn:
            raise InputError(f"{name} is not a setup sheet of {generator_report}")
        if name in seen:
            raise InputError(f"{name} is given twice")
        seen.add(name)
        role = drawn[name]["role"]
        if (spice is not None) != (role in SPICE_ROLES):
            raise InputError(f"{name} is a {role} sheet: "
                             + ("give its SPICE netlist" if spice is None
                                else "only a bench or channel sheet has a SPICE netlist"))
        tree = _netlist(netlist, schematic)
        if spice is not None:
            _check_values(tree, netlist, spice)
        tools.add(_value(sx.find(tree, "design"), "tool"))
        sheet = dict(drawn[name], file=_report_ref(schematic, here), sha256=_sha256(schematic))
        if spice is not None:
            sheet["netlist"] = {"file": _report_ref(spice, here), "sha256": _sha256(spice)}
        sheet["symbols"] = read_symbols(tree)
        _check_filled(sheet["symbols"], netlist)
        out.append(sheet)
    if len(tools) != 1:
        raise InputError(f"the netlists name {len(tools)} tool versions "
                         f"({', '.join(sorted(map(repr, tools)))}); export every sheet "
                         f"with the same kicad-cli")
    return {"format": FORM_FORMAT, "version": FORM_VERSION,
            "tool": {"name": TOOL, "version": tools.pop()}, "sheets": out}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Read kicad-cli netlists of edited co-simulation setup sheets as a "
                    "setup form for the co-simulation flow.")
    ap.add_argument("generator_report", help="the report chiplet_to_kicad_sch --setup wrote")
    ap.add_argument("out_form", help="setup form to write (JSON)")
    ap.add_argument("--sheet", action="append", nargs="+", required=True,
                    metavar="PATH", help="SCHEMATIC NETLIST [SPICE] of one sheet: the "
                    "edited .kicad_sch, its kicadsexpr netlist and, for a bench or "
                    "channel sheet, its SPICE netlist; once per sheet")
    args = ap.parse_args(argv)
    for paths in args.sheet:
        if len(paths) not in (2, 3):
            ap.error(f"--sheet takes SCHEMATIC NETLIST [SPICE], not {' '.join(paths)}")
    try:
        form = read_form(args.generator_report, args.out_form,
                         [(p[0], p[1], p[2] if len(p) == 3 else None) for p in args.sheet])
    except InputError as exc:
        print(f"{READER}: {exc}", file=sys.stderr)
        return EXIT_INPUT
    write_form(form, args.out_form)
    print(f"{args.out_form}: {len(form['sheets'])} sheets, "
          f"{sum(len(s['symbols']) for s in form['sheets'])} setup symbols")
    return EXIT_WRITTEN


if __name__ == "__main__":
    sys.exit(main())
