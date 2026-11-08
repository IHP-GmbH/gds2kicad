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
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import sexpr_io as sx
from chiplet_to_kicad_sch import (BASE_DIGEST_PREFIX, EXT_PAD, IDENTITY_PROPERTY,
                                  SETUP_MARKER, SYSTEM_LIB, _refusal)


def read_kicad_netlist(path, carriers: Optional[Dict[str, set]] = None) -> dict:
    """What KiCad resolved: named nets as {(reference, pin)}, pins on no net,
    the identity property per reference, the base digest from the title block,
    and the refusals of the pad fold. Reads the kicadsexpr netlist ``kicad-cli
    sch export netlist`` writes. A carrier's pads, EXT_PAD symbols carrying its
    identity, are folded back into the carrier (``pad_carriers`` with
    ``carriers``, then ``fold_pads``); a pad that does not fold stays a symbol."""
    tree = sx.parse(Path(path).read_text(encoding="utf-8"))
    symbols: List[Tuple[str, Optional[str], Optional[str], Optional[str]]] = []
    setup = set()
    comps = sx.find(tree, "components") or []
    for comp in sx.find_all(comps, "comp"):
        ref = str(_value(comp, "ref"))
        source = sx.find(comp, "libsource") or []
        ident = None
        for prop in sx.find_all(comp, "property"):
            if _value(prop, "name") == IDENTITY_PROPERTY:
                ident = _value(prop, "value")
            if _value(prop, "name") == SETUP_MARKER and _value(prop, "value"):
                setup.add(ref)
        if ref in setup:
            continue
        if ident is None:
            for field in sx.find_all(sx.find(comp, "fields") or [], "field"):
                if _value(field, "name") == IDENTITY_PROPERTY and len(field) > 2:
                    ident = str(field[-1]) if not isinstance(field[-1], list) else None
        symbols.append((ref, _value(source, "lib"), _value(source, "part"), ident))
    pads, refusals = pad_carriers(symbols, carriers)
    identity = {ref: ident for ref, _lib, _part, ident in symbols if ref not in pads}

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


def pad_carriers(symbols: Iterable[Tuple[str, Optional[str], Optional[str], Optional[str]]],
                 carriers: Optional[Dict[str, set]] = None
                 ) -> Tuple[Dict[str, str], List[dict]]:
    """``(pads, refusals)``: each EXT_PAD symbol that folds into a carrier, by
    Reference to the carrier id, and why the others do not.

    ``symbols`` are the sheet's symbols other than setup symbols, each
    ``(reference, lib, part, identity)``; an EXT_PAD with no identity is a
    designer's pad and not folded. A pad is refused when its Reference names
    another symbol too (``duplicate_reference``, naming the Reference), or,
    given ``carriers`` (carrier id to its pad ids), when its identity is not a
    carrier or its Reference not one of that carrier's pads (``unknown_pad``,
    naming the carrier and the pad). A refused pad is left unfolded."""
    symbols = list(symbols)
    count = Counter(ref for ref, *_ in symbols)
    pads: Dict[str, str] = {}
    refusals: List[dict] = []
    for ref, lib, part, ident in symbols:
        if not is_pad(lib, part) or ident is None:
            continue
        if count[ref] > 1:
            if not any(r["component"] == ref for r in refusals
                       if r["code"] == "duplicate_reference"):
                refusals.append(_refusal(
                    "duplicate_reference", f"{count[ref]} symbols are named {ref}; a "
                    f"carrier pad's Reference names it alone", component=ref))
        elif carriers is not None and ident not in carriers:
            refusals.append(_refusal(
                "unknown_pad", f"pad symbol {ref} names {ident}, which is not a carrier "
                f"on this sheet", component=ident, terminal=ref))
        elif carriers is not None and ref not in carriers[ident]:
            refusals.append(_refusal(
                "unknown_pad", f"pad symbol {ref} is not one of {ident}'s io pads",
                component=ident, terminal=ref))
        else:
            pads[ref] = ident
    return pads, refusals


def fold_pads(endpoint: Tuple[str, str], pads: Dict[str, str]) -> Tuple[str, str]:
    """A node on a carrier's pad, (pad id, "1"), as the carrier's terminal,
    (carrier, pad id); ``pads`` maps each pad symbol's Reference to its
    carrier. Any other node is returned as it is."""
    ref, pin = endpoint
    return (pads[ref], ref) if ref in pads and pin == "1" else endpoint


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
        got = read_kicad_netlist(net_path, carriers)
    diffs = compare(expected_nets, expected_free, base_sha256, got)
    return {"word": "differs" if diffs else "matches", "differences": diffs}
