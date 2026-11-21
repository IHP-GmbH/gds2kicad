#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A sheet read both ways: kicad-cli's netlist and ERC report, and the native
netlister, each read as a connectivity form by kicad_sch_to_connectivity.py.

``read_both_ways`` says whether the two forms agree and, where they do not,
where they part. It decides nothing: a caller that needs the native read to be
kicad-cli's takes anything but ``matches`` as a failure.
"""
import os
import shutil
import subprocess
import tempfile

import kicad_sch_netlist as native
from kicad_sch_to_connectivity import (InputError, _mode, _native_model, _need_files,
                                       read_form, read_form_native)

_MISSING = object()


def read_both_ways(schematic, generator_report=None, library=None, *, system_library=None,
                   mode=None, kicad_cli="kicad-cli", timeout=300) -> dict:
    """Read ``schematic`` with kicad-cli and with the native netlister and compare
    the two forms, front_end aside. Returns run_check's shape: the word, the
    differences by place in the form, the kicad-cli version and the netlister.
    kicad-cli reads the sheet where it is and leaves the project's lock file
    (~NAME.kicad_pro.lck beside the sheet) behind; that one file is removed when
    it was not there before."""
    sources = {"generator_report": generator_report, "library": library,
               "system_library": system_library}
    keys = {"library": library, "system_library": system_library, "mode": mode}

    def word(name, **rest):
        return {"word": name, "kicad_cli_version": None, "netlister": native.NETLISTER, **rest}

    try:
        _need_files(schematic, *sources.values())
        model = _native_model(str(schematic), _mode(mode, sources))
    except native.KicadConstruct as exc:
        return word("refused", reason=exc.refusal()["message"])
    except InputError as exc:
        return word("failed", reason=str(exc))
    exe = shutil.which(kicad_cli)
    if exe is None:
        return word("unavailable", reason=f"{kicad_cli} is not on PATH")
    # Never written: the forms' paths are relative to the sheet, not to a temporary file.
    out_form = os.path.join(os.path.dirname(os.path.abspath(schematic)), "both_ways.json")
    try:
        own = read_form_native(schematic, generator_report, out_form, model=model, **keys)
    except InputError as exc:
        return word("failed", reason=str(exc))
    with tempfile.TemporaryDirectory() as tmp:
        netlist, erc = os.path.join(tmp, "sheet.net"), os.path.join(tmp, "erc.json")
        failed = _export(exe, schematic, netlist, erc, timeout)
        if failed:
            return word("failed", reason=failed)
        try:
            cli = read_form(netlist, schematic, generator_report, out_form, erc=erc, **keys)
        except InputError as exc:
            return word("failed", reason=str(exc).replace(tmp + os.sep, ""))
    differences = _differences({k: v for k, v in cli.items() if k != "front_end"},
                               {k: v for k, v in own.items() if k != "front_end"})
    return {**word("differs" if differences else "matches", differences=differences),
            "kicad_cli_version": cli["front_end"]["tool_version"]}


def _export(exe, schematic, netlist, erc, timeout):
    """Export ``schematic``'s netlist and ERC report; the reason it failed, or None."""
    sheet = os.path.abspath(schematic)
    stem = os.path.splitext(os.path.basename(sheet))[0]
    # The one file kicad-cli 9.99 leaves, and only when the project file exists.
    lock = os.path.join(os.path.dirname(sheet), f"~{stem}.kicad_pro.lck")
    ours = not os.path.lexists(lock)
    try:
        for argv, out, what in (
                (["sch", "export", "netlist", "--format", "kicadsexpr"], netlist, "netlist"),
                (["sch", "erc", "--format", "json", "--severity-all"], erc, "ERC report")):
            try:
                subprocess.run([exe, *argv, "--output", out, sheet],
                               capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                return f"kicad-cli ran over {timeout} s"
            if not os.path.isfile(out):
                return f"kicad-cli wrote no {what}"
        return None
    finally:
        if ours:
            try:
                os.remove(lock)
            except FileNotFoundError:
                pass


def _differences(cli, own, at=""):
    """Every leaf where two forms part, in document order; a side that lacks
    the place has no key."""
    if isinstance(cli, dict) and isinstance(own, dict):
        places = [(f"{at}.{key}" if at else str(key), cli.get(key, _MISSING),
                   own.get(key, _MISSING)) for key in [*cli, *(k for k in own if k not in cli)]]
    elif isinstance(cli, list) and isinstance(own, list):
        places = [(f"{at}[{i}]", cli[i] if i < len(cli) else _MISSING,
                   own[i] if i < len(own) else _MISSING) for i in range(max(len(cli), len(own)))]
    else:
        return [] if cli == own else [{"at": at, "kicad_cli": cli, "native": own}]
    found = []
    for place, a, b in places:
        if a is _MISSING or b is _MISSING:
            found.append({"at": place, **({"kicad_cli": a} if b is _MISSING else {"native": b})})
        else:
            found += _differences(a, b, place)
    return found
