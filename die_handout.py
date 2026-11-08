# SPDX-License-Identifier: GPL-3.0-or-later
"""The schematic-first handout drawn from die folders, with no .chiplet.

``chiplet_to_kicad_sch.py --handout DIR --dies FOLDER... --ext-pad-um N``
writes into ``DIR``:

- ``chiplet_dies.kicad_sym``: one symbol per top cell, sorted by it, drawn
  with the .chiplet handout's own shape. Pin numbers come from the vendored
  ``pad_numbers``. A port's pads share one pin, named by the port. Power and
  ground pins sit on the left, every other pin on the right. There is no
  system here, so a link pin is not told apart from any other signal. A die
  whose pads repeat a number sets ``duplicate_pin_numbers_are_jumpers``, the
  die tying those pads inside, when every repeated number is one power or
  ground port; any other repeat is refused, ``undeclared_tie``.
- ``chiplet_system.kicad_sym``: ``GND`` and ``SUPPLY`` power symbols (Value
  names the rail; ``SUPPLY`` carries an empty ``Volts``), and ``EXT_PAD``, one
  passive pin with an empty ``Edge`` and the sized footprint of ``N`` um.
- ``sym-lib-table`` naming ``chiplet_system`` and then ``chiplet_dies`` by
  ``${KIPRJMOD}``, a project, and an empty sheet.

A die folder is read the way the classic import reads it, so the handout
draws no die that import would refuse. The checks mirror three functions of
the chiplet-spec system description: ``source.inventory.read_die_folder``
(die block, process and technology, inventory path, pad rows and their ports,
supply domains), ``classic_import._bound`` and ``classic_import._agree`` (the
contract and the inventory name the same pads on the same ports). They are a
copy to keep in step with those until a schema-free die-folder reader lands in
``chiplet_format_io`` and both sides call it. Schema admission is not run: the
vendored copy holds no schema, and ``bind`` checks digests a die folder need
not carry. Each folder lies inside ``DIR``,
and a symbol's ``Die`` is that folder relative to ``DIR``, the path the import
resolves against the project file. One top cell is one folder.

Every refusal is a record (code, message, and ``folder`` where one is meant);
nothing is written unless the whole handout can be. A rerun writes the same
bytes, and a handout file that differs from what would be written is never
overwritten.
"""

import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import chiplet_to_kicad_sch as c
import sexpr_io as sx
from _vendor.chiplet_format_io import (
    ChipletFormatError, _parse_contract_version, check_contract_version)
from _vendor.chiplet_format_io.die_contract import (
    ACCEPTED_DIE_CONTRACT_VERSIONS, BoundContract, DieContractError, check_terminals,
    inventory_identities)
from _vendor.chiplet_format_io.footprint_ids import (
    FootprintIdError, die_footprint, ext_pad_footprint)
from _vendor.chiplet_format_io.pad_numbers import pad_numbers
from _vendor.chiplet_format_io.sheet_names import SHEET_NAME_WORDS, is_sheet_name
from _vendor.chiplet_format_io.supply_domains import SupplyDomainError, read_supply_domains
from _vendor.chiplet_format_io.technology import TECHNOLOGY_OF_PDK

SYSTEM_LIB = c.SYSTEM_LIB
SYSTEM_LIB_FILE = f"{SYSTEM_LIB}.kicad_sym"
_SYSTEM_ROW = (SYSTEM_LIB, SYSTEM_LIB_FILE, "chiplet system symbols")
_LIBS = (_SYSTEM_ROW, c._HANDOUT_ROW)
CONTRACT = "die-contract.json"

_DIRECTION_TYPES = {"input": "input", "output": "output", "bidir": "bidirectional",
                    "analog": "passive", "none": "passive", None: "passive"}
_ROLE_TYPES = {"power": "power_in", "ground": "power_in", "no_connect": "no_connect"}


class _Refused(Exception):
    """One folder cannot be drawn. ``code`` is the refusal's code."""

    def __init__(self, code: str, message: str, **where):
        super().__init__(message)
        self.code, self.where = code, where


def _json(folder: Path, rel: str):
    try:
        return json.loads((folder / rel).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        # ValueError covers bad UTF-8, bad JSON and a NUL in the path.
        raise _Refused("die_folder", f"{rel} cannot be read: {exc}")


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _technology(data: dict) -> None:
    """A process the import can join to a technology (``inventory._technology``)."""
    process = data.get("process")
    if not isinstance(process, dict):
        raise _Refused("die_folder", f"{CONTRACT} states no process block")
    pdk, stated = process.get("pdk"), process.get("technology")
    mapped = TECHNOLOGY_OF_PDK.get(pdk)
    if stated is not None and mapped is not None and stated != mapped:
        raise _Refused("die_folder", f"{CONTRACT} attests process {pdk!r}, which maps "
                                     f"to {mapped!r}, and states technology {stated!r}")
    technology = stated if stated is not None else mapped
    if not isinstance(technology, str) or not technology:
        raise _Refused("die_folder",
                       f"process {pdk!r} maps to no technology "
                       f"({', '.join(sorted(TECHNOLOGY_OF_PDK))} do) and {CONTRACT} "
                       "states no process.technology")


def _supply(data: dict) -> None:
    """Supply domains the import reads (``inventory._supply``)."""
    stamped = _parse_contract_version(data.get("version"))
    if data.get("supply_domains") is not None and stamped is not None and stamped < (1, 2):
        raise _Refused("die_folder", f"supply_domains is a die contract 1.2 key, and "
                                     f"{CONTRACT} is {data.get('version')!r}")
    try:
        read_supply_domains(data, CONTRACT,
                            above_floor=stamped is not None and stamped > (1, 2))
    except SupplyDomainError as exc:
        raise _Refused("die_folder", str(exc))


def _ports(rel: str, document: dict) -> None:
    """Every pad row serves a port, with an ordinal, and a port is stated one
    way (``pins.load_pins_json`` and ``inventory._ports``)."""
    dbu = document.get("dbu_um")
    if not _number(dbu) or dbu <= 0:
        raise _Refused("die_folder", f"{rel} has no usable dbu_um")
    rows = document["pins"]
    any_port = any(isinstance(r, dict) and isinstance(r.get("port"), str) and r.get("port")
                   for r in rows)
    stated: Dict[str, tuple] = {}
    ordinals: Dict[str, set] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) \
                or not row.get("name"):
            raise _Refused("die_folder", f"{rel} has a pad row with no name")
        name, port, ordinal = row["name"], row.get("port"), row.get("ordinal")
        if not isinstance(port, str) or not port:
            what = f"pad {name!r} serves no port" if any_port else "the die states no ports"
            raise _Refused("unported_pads",
                           f"{rel}: {what}; a source wires the ports its dies state, so "
                           "put the pad on a port in the inventory, or regenerate the die")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int):
            raise _Refused("die_folder", f"{rel}: pad {name!r} carries no ordinal")
        facts = (str(row.get("direction", "")).upper(), str(row.get("role", "")).upper(),
                 str(row.get("net", "")))
        if stated.setdefault(port, facts) != facts:
            raise _Refused("die_folder",
                           f"{rel}: port {port!r} is stated as {stated[port]} and as {facts}")
        if ordinal in ordinals.setdefault(port, set()):
            raise _Refused("die_folder", f"{rel}: port {port!r} repeats an ordinal")
        ordinals[port].add(ordinal)


def _bound(folder: Path) -> BoundContract:
    """The contract bound to the inventory it names, once every check the
    import's folder reader makes holds (``inventory.read_die_folder``, then
    ``classic_import._bound``)."""
    data = _json(folder, CONTRACT)
    if not isinstance(data, dict) or not isinstance(data.get("die"), dict):
        raise _Refused("die_folder", f"{CONTRACT} is not a die contract (no die block)")
    try:
        check_contract_version(data.get("version"), ACCEPTED_DIE_CONTRACT_VERSIONS,
                               name=CONTRACT)
    except ChipletFormatError as exc:
        raise _Refused("die_folder", str(exc))
    die = data["die"]
    if not isinstance(die.get("top_cell"), str) or not die["top_cell"]:
        raise _Refused("die_folder", f"{CONTRACT} states no die.top_cell")
    box = die.get("bbox_um")
    if not (isinstance(box, list) and len(box) == 4 and all(map(_number, box))
            and box[2] > box[0] and box[3] > box[1]):
        raise _Refused("die_folder", "die.bbox_um is not [x0, y0, x1, y1] with extent")
    _technology(data)
    binding = data.get("inventory")
    rel = binding.get("path") if isinstance(binding, dict) else None
    if not isinstance(rel, str) or not rel or Path(rel).is_absolute() \
            or ".." in Path(rel).parts:
        raise _Refused("die_folder", "inventory.path is not a path inside the folder")
    document = _json(folder, rel)
    if not isinstance(document, dict) or not isinstance(document.get("pins"), list):
        raise _Refused("die_folder", f"{rel} states no pins list")
    _supply(data)
    _ports(rel, document)
    try:
        return BoundContract(data, folder / CONTRACT, document, folder / rel)
    except (KeyError, TypeError):
        raise _Refused("die_folder", f"{CONTRACT} is not a die contract")


def _agree(bound: BoundContract):
    """The pad numbers, once the contract and the inventory name the same
    pads and put each on the same port (``classic_import._agree``)."""
    numbered = pad_numbers(bound)
    held = inventory_identities(bound.inventory, bound.kind)
    contract = {(pin.name, pin.pad_index) for pin in numbered}
    if contract != set(held):
        missing = sorted([("contract", *p) for p in contract - set(held)]
                         + [("inventory", *p) for p in set(held) - contract])
        shown = ", ".join(f"{side} ({name}, {index})" for side, name, index in missing[:3])
        raise _Refused("contract_pads_differ",
                       f"the contract and the inventory do not name the same pads ({shown})")
    check_terminals(bound, require_equality=True)
    for pin in numbered:
        row = held[(pin.name, pin.pad_index)]
        if (pin.port, pin.ordinal) != (row.get("port"), row.get("ordinal")):
            raise _Refused("contract_pads_differ",
                           "the contract and the inventory disagree on which port a pad "
                           "serves")
    return numbered


def _pin(number: str, terms: List[dict], port: str) -> c.Terminal:
    """One symbol pin for every terminal of the pad name ``number``."""
    for value, what in ((number, "pad"), (port, "port")):
        if not is_sheet_name(value):
            raise _Refused("unmappable_name",
                           f"{what} name {value!r} is not a {SHEET_NAME_WORDS}; KiCad "
                           "would not carry it verbatim",
                           field="terminal", value=value)
    for t in terms:
        for key in ("role", "direction"):
            if not isinstance(t.get(key), (str, type(None))):
                raise _Refused("not_expressible",
                               f"pad {number} states {key} {t.get(key)!r}, which is no word")
    roles = {t.get("role") for t in terms}
    directions = {t.get("direction") for t in terms}
    if len(roles) != 1 or len(directions) != 1:
        raise _Refused("not_expressible",
                       f"pad {number} is stated with more than one role or direction")
    role, direction = roles.pop(), directions.pop()
    if role == "signal":
        kind = _DIRECTION_TYPES.get(direction)
    else:
        kind = _ROLE_TYPES.get(role)
    if kind is None:
        raise _Refused("not_expressible",
                       f"pad {number} has role {role!r} and direction {direction!r}, "
                       "which this handout draws no pin for")
    return c.Terminal(number, port, kind, "")


def _jumpers(groups: Dict[str, list], terms: Dict[tuple, dict]) -> bool:
    """Whether the symbol states ``duplicate_pin_numbers_are_jumpers``: set when
    a number repeats, and only if every repeated number is a power or ground
    port (``pad_numbers`` and ``_pin`` give each one port and one role)."""
    repeated = {number: pins for number, pins in groups.items() if len(pins) > 1}
    for number, pins in repeated.items():
        role = terms[(pins[0].name, pins[0].pad_index)].get("role")
        if role not in ("power", "ground"):
            raise _Refused("undeclared_tie",
                           f"pad {number} repeats on {len(pins)} pads of role {role!r}, "
                           "not one power or ground port, so the die does not say they "
                           "are tied; give each pad its own name, or put them on one "
                           "power or ground port", value=number)
    return bool(repeated)


def _path_rule(die: str) -> Optional[str]:
    """Why the import's path rule (``document.check_relative_path``) refuses
    ``die``, a path that already stays inside the handout, or None."""
    if "${" in die:
        return "it holds '${'"
    if die.startswith(("/", "\\")) or re.match(r"[A-Za-z]:[\\/]", die):
        return "it reads as absolute"
    if "\\" in die:
        return "it holds a backslash"
    return None


def _die_field(folder: Path, out_dir: Path) -> str:
    """``folder`` relative to ``out_dir``, as the classic import reads ``Die``:
    the path as given when it lies inside, else the resolved one, so a link
    inside the handout stays the link."""
    try:
        rels = (os.path.relpath(os.path.abspath(folder), os.path.abspath(out_dir)),
                os.path.relpath(folder.resolve(), out_dir.resolve()))
    except (OSError, ValueError) as exc:     # ValueError: a NUL in the path
        raise _Refused("die_folder", f"cannot be read: {exc}")
    inside = [rel for rel in rels if rel != "." and ".." not in Path(rel).parts]
    if not inside:
        raise _Refused("die_folder",
                       f"not inside the handout directory {out_dir}; put the die "
                       "folders under it, since a placed die names its folder relative "
                       "to the .chiplet source beside the project")
    die = Path(inside[0]).as_posix()
    why = _path_rule(die)
    if why:
        raise _Refused("die_folder", f"its path {die!r} cannot be a Die field: {why}")
    return die


def _read(folder: Path, out_dir: Path) -> Tuple[str, str, list]:
    """``(cell, die, symbol)`` for one folder, or :class:`_Refused`."""
    die = _die_field(folder, out_dir)
    bound = _bound(folder)
    cell = bound.contract["die"].get("top_cell")
    try:
        footprint = die_footprint(cell)
    except FootprintIdError as exc:
        raise _Refused("unmappable_name", str(exc), field="top_cell", value=cell)
    if not c._is_ident(cell):
        raise _Refused("unmappable_name",
                       f"top cell {cell!r} is not a {c.IDENT_RULE}, so it cannot name "
                       "its library symbol", field="top_cell", value=cell)
    try:
        numbered = _agree(bound)
    except DieContractError as exc:
        raise _Refused("not_expressible", str(exc))
    terms = {(t.get("name"), t.get("pad_index")): t for t in bound.terminals}
    groups: Dict[str, list] = {}
    for pin in numbered:
        groups.setdefault(pin.number, []).append(pin)
    model = c.Die(cell, cell, [_pin(number, [terms[(p.name, p.pad_index)] for p in pins],
                                    pins[0].port)
                               for number, pins in groups.items()])
    jumpers = _jumpers(groups, terms)
    cols = c._columns(model, inner="right", net_of={}, nets={}, die_ids=frozenset())
    shape = c._shape(model, cols, {}, "right", reference="U?")
    below = -shape.half_h - c.GRID
    fields = (c._prop("Die", die, (0, below), hide=True),
              c._prop("Method", "", (0, below), show_name=True),
              c._prop("Thickness_um", "", (0, below - c.GRID), show_name=True))
    return cell, die, c._lib_symbol(cell, model, shape, lib=None, footprint=footprint,
                                    fields=fields, jumpers=jumpers)


_HALF = c.GRID / 2
#: The power symbols, drawn in KiCad's manner so a wire meets the pin end on:
#: the side the graphic and the rail name go to (1 up, -1 down), the
#: polylines from the pin at (0, 0), and the extra properties after KiCad's
#: own. ``SUPPLY`` is a stem up to a bar and carries ``Volts``, empty, for the
#: student to fill; ``GND`` is KiCad's own ground wedge and has no volts.
_POWER = {
    "GND": (-1, [[(0, 0), (0, -_HALF), (_HALF, -_HALF), (0, -c.GRID), (-_HALF, -_HALF),
                  (0, -_HALF)]], ()),
    "SUPPLY": (1, [[(0, 0), (0, c.GRID)], [(-_HALF, c.GRID), (_HALF, c.GRID)]],
               ("Volts",)),
}


def _power_symbol(name: str) -> list:
    """The power symbol ``name`` of :data:`_POWER`, its Value the rail, shown
    beyond the graphic, and each extra property beyond that, empty."""
    side, outline, extra = _POWER[name]
    value_y = side * (c.GRID + 1.27)
    props = [c._prop("Reference", "#PWR", (0, -side * c.GRID), hide=True),
             c._prop("Value", name, (0, value_y)),
             c._prop("Footprint", "", hide=True),
             c._prop("Datasheet", "", hide=True)]
    props += [c._prop(field, "", (0, value_y + side * c.GRID * n), show_name=True)
              for n, field in enumerate(extra, 1)]
    return ["symbol", c._q(name), ["power"],
            ["pin_names", ["offset", sx.num(0)]],
            ["exclude_from_sim", "no"], ["in_bom", "yes"], ["on_board", "yes"],
            *props,
            ["symbol", c._q(f"{name}_0_1"),
             *(["polyline", c._pts(line),
                ["stroke", ["width", "0"], ["type", "default"]], ["fill", ["type", "none"]]]
               for line in outline)],
            ["symbol", c._q(f"{name}_1_1"),
             ["pin", "power_in", "line", ["at", "0", "0", "90" if side > 0 else "270"],
              ["length", "0"], ["hide", "yes"], ["name", c._q(name), c._FONT],
              ["number", c._q("1"), c._FONT]]],
            ["embedded_fonts", "no"]]


def die_handout(folders, out_dir, ext_pad_um, name=None) -> dict:
    """Write the handout for ``folders`` into ``out_dir``; see the module.

    ``ext_pad_um`` is the external pad edge, judged by ``ext_pad_footprint``
    alone. ``name`` is the project and the sheet, the output directory's own
    name unless given. Returns file name -> sha256, or raises
    ``SchematicRefused`` listing every refusal, with nothing written."""
    out_dir = Path(out_dir)
    refusals = []
    try:
        pad_footprint = ext_pad_footprint(ext_pad_um)
    except FootprintIdError as exc:
        pad_footprint = None
        refusals.append(c._refusal("ext_pad_um", str(exc), value=ext_pad_um))
    if name is None:
        try:
            name = out_dir.resolve().name
        except (OSError, ValueError) as exc:
            raise c.SchematicRefused(refusals + [c._refusal(
                "handout_dir", f"cannot read the directory {out_dir}: {exc}")])
    if not c._is_ident(name):
        refusals.append(c._refusal(
            "unmappable_name", f"project name {name!r} is not a {c.IDENT_RULE}",
            field="project", value=name))
    drawn: Dict[str, Tuple[str, str, list]] = {}
    for folder in folders:
        try:
            cell, die, symbol = _read(Path(folder), out_dir)
        except _Refused as exc:
            refusals.append(c._refusal(exc.code, f"die folder {folder}: {exc}",
                                       folder=str(folder), **exc.where))
            continue
        if cell in drawn and drawn[cell][1] == die:
            refusals.append(c._refusal(
                "die_folder_twice", f"die folder {folder} is {drawn[cell][0]} again",
                folder=str(folder), other_folder=drawn[cell][0]))
            continue
        if cell in drawn:
            refusals.append(c._refusal(
                "top_cell_drawn_twice",
                f"die folders {drawn[cell][0]} and {folder} share top cell {cell}; one "
                "library symbol names one folder, so hand out one of them",
                folder=str(folder), other_folder=drawn[cell][0], value=cell))
            continue
        drawn[cell] = (str(folder), die, symbol)
    files: Dict[str, str] = {}
    if not refusals:
        system = [*map(_power_symbol, _POWER), c.ext_pad_symbol(pad_footprint)]
        files = {SYSTEM_LIB_FILE: c.symbol_library(system),
                 c.HANDOUT_LIB_FILE: c.symbol_library(
                     [drawn[cell][2] for cell in sorted(drawn)]),
                 c.LIB_TABLE: c._table_text(*_LIBS),
                 **c.blank_project(name, name)}
    return c.write_handout(out_dir, files, refusals, _LIBS)


def _ext_pad_um(text):
    """The integer ``text`` spells in plain digits with no leading zero, or
    ``text`` itself, which ``ext_pad_footprint`` then refuses."""
    if isinstance(text, str) and text.isascii() and text.isdigit() \
            and str(int(text)) == text:
        return int(text)
    return text


def _print_refusals(refusals: List[dict]) -> int:
    for r in refusals:
        print(f"refused ({r['code']}): {r['message']}", file=sys.stderr)
    return c.EXIT_REFUSED


def command(ap, args) -> int:
    """``--dies`` is a handout, never a .chiplet drawing. Bad company is a
    refusal, printed like every other refusal, and nothing is written."""
    if not args.dies:
        ap.error("--ext-pad-um goes with --dies")
    if args.pins:
        ap.error("--dies takes no --pins")
    if args.out or args.report or args.setup or args.setup_spec or args.setup_only \
            or args.check:
        ap.error("--handout writes no sheet and no report")
    refusals = []
    if args.chiplet:
        refusals.append(c._refusal("dies_with_chiplet",
                                   "--dies reads die folders and takes no --chiplet"))
    if args.handout is None:
        refusals.append(c._refusal("dies_without_handout",
                                   "--dies writes the handout and takes --handout"))
    if refusals:
        return _print_refusals(refusals)
    try:
        files = die_handout(args.dies, args.handout, _ext_pad_um(args.ext_pad_um),
                            name=args.name)
    except c.SchematicRefused as exc:
        return _print_refusals(exc.refusals)
    for fname in files:
        print(os.path.join(args.handout, fname))
    return c.EXIT_WRITTEN
