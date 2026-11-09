#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read a sheet chiplet_to_kicad_sch.py drew back through KiCad, and compare it
with the .chiplet it was drawn from: that generator's ``--check``.

``run_check`` exports the sheet's netlist with kicad-cli, ``read_kicad_netlist``
reads what KiCad resolved, and ``compare`` lists every way it differs from the
description. A carrier's pads, EXT_PAD symbols carrying its identity, fold back
into the carrier (``pad_carriers``, ``fold_pads``); the connectivity reader
(kicad_sch_to_connectivity.py) folds them the same way.
"""

import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

import sexpr_io as sx
from chiplet_to_kicad_sch import (BASE_DIGEST_PREFIX, EXT_PAD, IDENTITY_PROPERTY,
                                  PAD_ID_PROPERTY, SETUP_MARKER, SYSTEM_LIB, _refusal)


_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
UNIT_SUFFIX = re.compile(r"_(\d+)_\d+$")


class InputError(Exception):
    """An input is missing or is not what it claims to be; no form is written."""


class PadSymbol(NamedTuple):
    ref: str
    lib: Optional[str]
    part: Optional[str]
    identity: Optional[str]
    io_pad_id: Optional[str] = None
    copies: int = 1


class Sheet(NamedTuple):
    placed: list
    by_uuid: dict
    cache: dict
    multi_unit: set


class Placements(NamedTuple):
    pairs: list
    multi_unit: bool
    shared: bool


def cache_key(symbol):
    return _value(symbol, "lib_name") or _value(symbol, "lib_id")


def read_schematic(path):
    try:
        tree = sx.parse(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise InputError(f"KiCad schematic {path} is unreadable: {exc}") from exc
    if not isinstance(tree, list) or sx.head(tree) != "kicad_sch":
        raise InputError(f"{path} is not a KiCad schematic")
    cache, multi = {}, set()
    for symbol in sx.find_all(sx.find(tree, "lib_symbols") or [], "symbol"):
        name = str(symbol[1])
        cache[name] = symbol
        for unit in sx.find_all(symbol, "symbol"):
            match = UNIT_SUFFIX.search(str(unit[1]))
            if match and int(match[1]) > 1:
                multi.add(name)
    placed = sx.find_all(tree, "symbol")
    return Sheet(placed, {_value(s, "uuid"): s for s in placed
                         if _value(s, "uuid") is not None}, cache, multi)


def component_placements(comp, sheet):
    """Resolve KiCad's merged component in every reader, including classic.
    Distinct units of one cached part are one symbol (primary UUID last).
    A repeated unit or mixed parts keep every placement and share a Reference.
    Missing, malformed and unresolved UUIDs retain classic's input errors."""
    ref = _value(comp, "ref")
    uuids = [str(stamp) for stamp in (sx.find(comp, "tstamps") or [])[1:]]
    if not uuids or any(_UUID.fullmatch(stamp) is None for stamp in uuids):
        raise InputError(f"symbol {ref} has no valid component UUID in tstamps; "
                         "export the schematic again with kicad-cli")
    if any(uuid not in sheet.by_uuid for uuid in uuids):
        raise InputError(f"symbol {ref} has a UUID absent from the root schematic; "
                         "export the schematic again with kicad-cli")
    placed = [sheet.by_uuid[u] for u in uuids]
    multi = any(cache_key(s) in sheet.multi_unit for s in placed)
    parts = {cache_key(s) for s in placed}
    units = [_value(s, "unit") or "1" for s in placed]
    shared = len(uuids) > 1 and (not multi or len(parts) > 1 or len(set(units)) < len(units))
    pairs = list(zip(uuids, placed)) if shared else [(uuids[-1], placed[-1])]
    return Placements(pairs, multi, shared)


def netlist_property(comp, name):
    for prop in sx.find_all(comp, "property"):
        if _value(prop, "name") == name:
            return _value(prop, "value")
    for field in sx.find_all(sx.find(comp, "fields") or [], "field"):
        if _value(field, "name") == name:
            return str(field[-1]) if len(field) > 2 and not isinstance(field[-1], list) else ""
    return None


def pad_id(symbol):
    """Stable pad id; an absent or empty field uses the older Reference."""
    return symbol.io_pad_id or symbol.ref


def netlist_symbols(components, sheet):
    """One named row per root component, with its shared placement count.
    A child's placed UUIDs live in its own file; hierarchy is refused by the
    connectivity reader before those components can be imported."""
    symbols = []
    for comp in root_components(components):
        if netlist_property(comp, SETUP_MARKER):
            continue
        source = sx.find(comp, "libsource") or []
        copies = len(component_placements(comp, sheet).pairs)
        symbols.append(PadSymbol(_value(comp, "ref"), _value(source, "lib"),
                                 _value(source, "part"), netlist_property(comp, IDENTITY_PROPERTY),
                                 netlist_property(comp, PAD_ID_PROPERTY), copies))
    return symbols


def root_components(components):
    return [c for c in components if _value(sx.find(c, "sheetpath") or [], "names") == "/"]


def reference_counts(symbols):
    counts = Counter()
    for symbol in symbols:
        counts[symbol.ref] += symbol.copies
    return counts


def duplicate_references(counts):
    """The symbol-level owner of the shared-reference refusal and its message."""
    return [_refusal("duplicate_reference", f"{counts[ref]} symbols are named {ref}; "
                     "annotate the sheet so every symbol has its own", component=ref)
            for ref in sorted(counts) if counts[ref] > 1]


def read_kicad_netlist(path, carriers: Optional[Dict[str, set]] = None, *, schematic=None) -> dict:
    """What KiCad resolved: named nets as {(reference, pin)}, pins on no net,
    the identity property per reference, the base digest from the title block,
    and the refusals of the pad fold. Reads the kicadsexpr netlist ``kicad-cli
    sch export netlist`` writes. A carrier's pads, EXT_PAD symbols carrying its
    identity, are folded back into the carrier (``pad_carriers`` with
    ``carriers``, then ``fold_pads``); a pad that does not fold stays a symbol.
    Shared References are refused from the same placement grouping as the
    connectivity reader. ``schematic`` defaults to the netlist's source path,
    relative to the netlist when needed; its placed UUIDs must resolve."""
    tree = sx.parse(Path(path).read_text(encoding="utf-8"))
    if schematic is None:
        source = _value(sx.find(tree, "design") or [], "source")
        if source is None:
            raise InputError(f"netlist {path} names no source schematic")
        schematic = Path(path).parent / source
    comps = sx.find_all(sx.find(tree, "components") or [], "comp")
    setup = {_value(c, "ref") for c in comps if netlist_property(c, SETUP_MARKER)}
    symbols = netlist_symbols(comps, read_schematic(schematic))
    pads, pad_refusals = pad_carriers(symbols, carriers)
    refusals = [*duplicate_references(reference_counts(symbols)), *pad_refusals]
    identity = {s.ref: s.identity for s in symbols if s.ref not in pads}

    nets: Dict[str, frozenset] = {}
    unconnected = set()
    for net in sx.find_all(sx.find(tree, "nets") or [], "net"):
        name = str(_value(net, "name"))
        every = frozenset(fold_pads((str(_value(n, "ref")), str(_value(n, "pin"))), pads)
                          for n in sx.find_all(net, "node"))
        nodes = frozenset(ep for ep in every if ep[0] not in setup)
        if every and not nodes:
            continue
        if name.startswith("unconnected-(") and len(nodes) == 1:
            unconnected |= nodes
        else:
            nets[name] = nodes

    base = None
    for atom in _atoms(sx.find(tree, "design") or []):
        if atom.startswith(BASE_DIGEST_PREFIX):
            base = atom[len(BASE_DIGEST_PREFIX):]
    return {"nets": nets, "unconnected": frozenset(unconnected),
            "identity": identity, "base_sha256": base, "refusals": refusals}


def is_pad(lib, part) -> bool:
    """Whether a netlist libsource is the system library's EXT_PAD."""
    return lib == SYSTEM_LIB and part == EXT_PAD


def pad_carriers(symbols: Iterable[PadSymbol], carriers: Optional[Dict[str, set]] = None
                 ) -> Tuple[Dict[str, Tuple[str, str]], List[dict]]:
    """Reference -> (carrier id, pad id), with refused pads left unfolded.

    A designer EXT_PAD has no carrier identity and never folds. If any carrier
    pad has a non-empty IO_PAD_ID, all must; otherwise the whole older sheet
    uses References. Missing fields are missing_identity (carrier, Reference).
    Unknown carriers and pad ids are unknown_pad, checked before duplicate_pad
    (one carrier and pad id, listing each Reference once; neither copy folds).
    Shared References decline to fold; the symbol-level duplicate_references
    owns that refusal, using the same placement counts in every reader.
    """
    symbols = list(symbols)
    counts = reference_counts(symbols)
    candidates = [s for s in symbols if is_pad(s.lib, s.part) and s.identity is not None]
    use_fields = any(s.io_pad_id for s in candidates)
    by_pad = defaultdict(set)
    for symbol in candidates:
        if not use_fields or symbol.io_pad_id:
            by_pad[symbol.identity, pad_id(symbol)].add(symbol.ref)
    pads, refusals, refused_pads = {}, [], set()
    for symbol in candidates:
        ref, carrier, pid = symbol.ref, symbol.identity, pad_id(symbol)
        if counts[ref] > 1:
            continue
        if carriers is not None and carrier not in carriers:
            refusals.append(_refusal(
                "unknown_pad", f"pad symbol {ref} names {carrier}, which is not a carrier "
                f"on this sheet", component=carrier, terminal=pid))
        elif use_fields and not symbol.io_pad_id:
            refusals.append(_refusal(
                "missing_identity", f"pad symbol {ref} of {carrier} has no {PAD_ID_PROPERTY}; "
                "restore its pad identity", component=carrier, terminal=ref))
        elif carriers is not None and pid not in carriers[carrier]:
            refusals.append(_refusal(
                "unknown_pad", f"pad symbol {ref} names {pid}, which is not one of "
                f"{carrier}'s io pads", component=carrier, terminal=pid))
        elif len(by_pad[carrier, pid]) > 1:
            if (carrier, pid) not in refused_pads:
                refs = ", ".join(sorted(by_pad[carrier, pid]))
                refusals.append(_refusal(
                    "duplicate_pad", f"carrier {carrier}'s io pad {pid} is named by "
                    f"pad symbols {refs}; give each pad its own {PAD_ID_PROPERTY}",
                    component=carrier, terminal=pid))
                refused_pads.add((carrier, pid))
        else:
            pads[ref] = (carrier, pid)
    return pads, refusals


def fold_pads(endpoint: Tuple[str, str], pads: Dict[str, Tuple[str, str]]) -> Tuple[str, str]:
    """A node on a carrier's pad, (Reference, "1"), as the carrier's terminal,
    (carrier, pad id); ``pads`` maps each pad symbol's Reference to its
    (carrier id, pad id). Any other node is returned as it is."""
    ref, pin = endpoint
    return pads[ref] if ref in pads and pin == "1" else endpoint


def _value(node, tag):
    child = sx.find(node, tag)
    return child[1] if child is not None and len(child) > 1 else None


def _atoms(node):
    for child in node:
        if isinstance(child, list):
            yield from _atoms(child)
        else:
            yield str(child)


def compare(expected_nets: Dict[str, frozenset], expected_free: frozenset,
            base_sha256: str, got: dict) -> List[str]:
    """Every way the KiCad netlist differs from the description; empty if none."""
    diffs = [r["message"] for r in got.get("refusals", [])]
    ident = got["identity"]
    for ref, cid in sorted(ident.items()):
        if cid is None:
            diffs.append(f"symbol {ref} has no {IDENTITY_PROPERTY}")
        elif cid != ref:
            diffs.append(f"symbol {ref} carries {IDENTITY_PROPERTY}={cid}")

    def as_ids(eps):
        return frozenset((ident.get(r) or r, p) for r, p in eps)

    got_nets = {name: as_ids(eps) for name, eps in got["nets"].items()}
    for name in sorted(set(expected_nets) | set(got_nets)):
        want, have = expected_nets.get(name), got_nets.get(name)
        if have is None:
            diffs.append(f"net {name} is missing")
        elif want is None:
            diffs.append(f"net {name} is not in the description: "
                         + _eps(have))
        elif want != have:
            if want - have:
                diffs.append(f"net {name} lacks {_eps(want - have)}")
            if have - want:
                diffs.append(f"net {name} also has {_eps(have - want)}")
    have_free = as_ids(got["unconnected"])
    if expected_free - have_free:
        diffs.append("terminals not reported unconnected: "
                     + _eps(expected_free - have_free))
    if have_free - expected_free:
        diffs.append("terminals unconnected that the description connects: "
                     + _eps(have_free - expected_free))
    if got["base_sha256"] != base_sha256:
        diffs.append(f"base digest is {got['base_sha256']!r}, not {base_sha256}")
    return diffs


def _eps(eps) -> str:
    return ", ".join(f"{c}.{p}" for c, p in sorted(eps))


def run_check(sch_path, expected_nets, expected_free, base_sha256,
              kicad_cli: str = "kicad-cli", timeout: int = 300,
              carriers: Optional[Dict[str, set]] = None) -> dict:
    """Export the sheet's netlist with kicad-cli and compare it; ``carriers``
    are the drawn carriers' pad ids, by carrier id."""
    exe = shutil.which(kicad_cli)
    if exe is None:
        return {"word": "unavailable", "reason": f"{kicad_cli} is not on PATH"}
    with tempfile.TemporaryDirectory() as tmp:
        net_path = os.path.join(tmp, "check.net")
        try:
            proc = subprocess.run(
                [exe, "sch", "export", "netlist", "--format", "kicadsexpr",
                 "--output", net_path, str(sch_path)],
                capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"word": "failed", "reason": f"kicad-cli ran over {timeout} s"}
        if proc.returncode != 0 or not os.path.isfile(net_path):
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
            return {"word": "failed", "exit_code": proc.returncode,
                    "reason": "kicad-cli wrote no netlist", "output": tail}
        try:
            got = read_kicad_netlist(net_path, carriers, schematic=sch_path)
        except InputError as exc:
            return {"word": "failed", "reason": str(exc)}
    diffs = compare(expected_nets, expected_free, base_sha256, got)
    return {"word": "differs" if diffs else "matches", "differences": diffs}
