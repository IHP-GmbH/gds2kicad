#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Extract I/O pad locations from a KiCad PCB file into an io pad manifest.

Walks all footprints in a .kicad_pcb file, filters those whose
property IO_CLASS is set, and emits the io pad manifest of chiplet-spec's
io_pads.schema.json, in the board frame:

  {
    "schema": "chiplet-io-pad-manifest",
    "version": "1.0.0",
    "frame": "board_absolute_ynegated_um",
    "generator": "gds_to_kicad kicad_pcb_to_iopads.py",
    "board": "design.kicad_pcb",
    "io_pads": [
      {"ref": "J1", "io_class": "wire_bond",
       "x_um": 5000.0, "y_um": 3500.0,
       "size_x_um": 100.0, "size_y_um": 100.0,
       "net": "VDD_EXT", "layer": "TopMetal2",
       "source_layer": "F.Cu"},
      ...
    ]
  }

hyp_to_gds.py --io-pads reads only this frame and refuses a file without
the envelope. Every entry is what the chiplet_kicad_plugin's io_pads writer
writes for the same carrier: ``layer`` is the PDK layer the pads are drawn
on, the KiCad layer goes in ``source_layer``, and positions and sizes are
rounded to the nanometer. An IO_CLASS outside the schema's three, or a
carrier whose size neither IO_PAD_SIZE_UM nor its first pad gives above zero
on both axes, is refused rather than written or dropped; the file is not
written then.

Carrier ids
-----------
``ref`` is the io_pad id. interposer-pnr ``export`` draws every carrier pad,
on either path, from the ``chiplet_carrier`` library with its id in an
``IO_PAD_ID`` property; on a board drawn against a sheet the Reference is the
sheet's designator instead. So a board with a carrier from that library or
carrying ``IO_PAD_ID`` names every carrier by it, and one without it, or two
carrying one id, is refused by reference rather than read back under its
Reference. On any other (hand-drawn) board the Reference is the id. The
chiplet_kicad_plugin's io_pads writer reads it the same way. On such a
board a carrier with an empty Reference, and two carriers sharing one, are
refused too, by their KiCad positions: the schema takes no empty ref and
its consumers key pads by ref (the plugin's writer does not refuse them
yet). A carrier without a Reference is named by its position in every
refusal.

Net names
---------
A carrier's net is the one net all its pads carry. A carrier whose pads carry
different nets (an unnetted pad's empty net counts as a net) is refused by
reference, with its io_pad id where that differs, naming the nets, so this
reader and the plugin's write the same net for every board they accept.

``net`` is the planner's net name. A board drawn against a sheet carries the
sheet's net names instead, so it needs ``--export-report``, the JSON
``export`` wrote beside it, to name them back. Whether a board follows a sheet
is the report's to say; without one, a board whose carrier footprint links a
sheet symbol (a non-empty ``path``) is refused, asking for the report. A
report written for another board (other carrier references, or another
IO_PAD_ID on one) is refused. How a report is read, matched to the board's
carriers and applied per net is interposer-pnr's
(``_vendor/round_trip_names.py``: read_export_report, read_board_carriers,
planner_net).

Coordinate convention
---------------------
KiCad PCB stores positions Y-down in millimeters. The output JSON is in
micrometers with Y NEGATED to match the GDS Y-up convention used by
hyp_to_gds.py downstream.

The script depends only on the Python standard library (no kicad-cli,
no klayout): KiCad PCB is plain text S-expression which we tokenize and
walk in-memory.
"""
import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _paths import atomic_write  # noqa: E402
from _vendor.round_trip_names import (  # noqa: E402
    BoardCarrier,
    CarrierIdError,
    ExportReport,
    RoundTripNamesError,
    planner_net,
    read_board_carriers,
    read_export_report,
)

#: The property export writes a carrier's io_pad id in.
IO_PAD_ID = "IO_PAD_ID"

#: The manifest's discriminator, the version written, and the board frame:
#: the one frame hyp_to_gds.py --io-pads reads.
IO_PADS_SCHEMA = "chiplet-io-pad-manifest"
IO_PADS_VERSION = "1.0.0"
IO_PADS_FRAME = "board_absolute_ynegated_um"
IO_PADS_GENERATOR = "gds_to_kicad kicad_pcb_to_iopads.py"

#: The io_pad usage classes, the schema's closed vocabulary (the keys of
#: chiplet_format_io's IO_CLASS_INTERFACE_TYPES, held equal by a test; not
#: imported, since the reference reader needs yaml).
IO_CLASSES = ("wire_bond", "flipped_bump", "tsv_bump")

#: The PDK layer the worker draws every io_pad on, as the plugin's writer
#: names it.
IO_PAD_LAYER = "TopMetal2"


class CarrierNetError(ValueError):
    """A carrier whose pads do not all carry one net."""


class CarrierPadError(ValueError):
    """A carrier with an IO_CLASS outside IO_CLASSES or no pad size."""


def tokenize(text: str) -> List[str]:
    """Tokenize an S-expression into a flat list of tokens.

    Quoted strings are kept intact (with their surrounding quotes) so
    the parser can distinguish them from symbols.
    """
    tokens: List[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c == '(':
            tokens.append('(')
            i += 1
        elif c == ')':
            tokens.append(')')
            i += 1
        elif c == '"':
            j = i + 1
            buf: List[str] = []
            while j < n and text[j] != '"':
                if text[j] == '\\' and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            tokens.append('"' + ''.join(buf) + '"')
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '()':
                j += 1
            tokens.append(text[i:j])
            i = j
    return tokens


def parse(tokens: List[str], pos: int = 0) -> Tuple[Any, int]:
    if pos >= len(tokens):
        raise ValueError("unbalanced or truncated S-expression")
    if tokens[pos] == '(':
        pos += 1
        out: List[Any] = []
        while pos < len(tokens) and tokens[pos] != ')':
            v, pos = parse(tokens, pos)
            out.append(v)
        if pos >= len(tokens):
            raise ValueError("unbalanced S-expression: missing ')'")
        return out, pos + 1
    tok = tokens[pos]
    if tok.startswith('"') and tok.endswith('"'):
        return tok[1:-1], pos + 1
    return tok, pos + 1


def find_all(node: Any, key: str) -> Iterable[List[Any]]:
    if isinstance(node, list) and node and node[0] == key:
        yield node
    if isinstance(node, list):
        for child in node:
            yield from find_all(child, key)


def _direct_children(fp: List[Any]) -> Iterable[List[Any]]:
    for sub in fp[2:]:
        if isinstance(sub, list) and sub:
            yield sub


def _first_pad(fp: List[Any]) -> List[Any]:
    for sub in _direct_children(fp):
        if sub[0] == 'pad':
            return sub
    return []


def _pad_size_um(pad: List[Any]) -> Tuple[float, float]:
    for psub in pad[1:]:
        if isinstance(psub, list) and psub and psub[0] == 'size' and len(psub) >= 3:
            return float(psub[1]) * 1000.0, float(psub[2]) * 1000.0
    return 0.0, 0.0


def _size_um(x: float, y: float) -> Optional[Tuple[float, float]]:
    """(x, y) to the nanometer, or None unless both are finite and above zero."""
    size = (round(x, 3), round(y, 3))
    return size if all(math.isfinite(v) and v > 0.0 for v in size) else None


def carrier_size_um(fp: List[Any], io_size_str: Optional[str]) -> Optional[Tuple[float, float]]:
    """A carrier's pad size in micrometers: IO_PAD_SIZE_UM as "<x>x<y>",
    else its first pad's; None when neither gives one _size_um takes. The
    plugin's io_pad_size_um rule, so both writers size a carrier alike."""
    parts = (io_size_str or '').split('x')
    if len(parts) == 2:
        try:
            size = _size_um(float(parts[0]), float(parts[1]))
        except ValueError:
            size = None
        if size is not None:
            return size
    pad = _first_pad(fp)
    return _size_um(*_pad_size_um(pad)) if pad else None


def _pad_net(pad: List[Any]) -> str:
    for psub in pad[1:]:
        if isinstance(psub, list) and psub and psub[0] == 'net' and len(psub) >= 3:
            return str(psub[2])
    return ''


def _pad_nets(fp: List[Any]) -> Set[str]:
    """The nets of the footprint's pads ('' for an unnetted pad)."""
    return {_pad_net(sub) for sub in _direct_children(fp) if sub[0] == 'pad'}


def carrier_net(label: str, nets: Set[str]) -> str:
    """The one net of the carrier ``label`` whose pads carry ``nets`` ('' for
    one with no pad or only unnetted ones); more than one is refused."""
    if len(nets) > 1:
        raise CarrierNetError(
            f"carrier {label}: its pads carry different nets "
            f"({', '.join(map(repr, sorted(nets)))}); a carrier's pads carry one net")
    return next(iter(nets), '')


def _carrier_label(ref: str, place: str) -> str:
    """How a refusal names a carrier: its Reference, else its position."""
    return ref or f"at ({place}) mm"


def check_carrier_ids(ids: Sequence[str], places: Sequence[str]) -> None:
    """Refuse (CarrierIdError) an empty id and an id carriers share, naming
    the carriers by ``places``, their KiCad positions in mm, in the order of
    ``ids``. Empty ids are refused first, so two are not read as sharing ''.

    Only a hand-drawn board's ids (its References) can fail here:
    read_board_carriers already refuses an empty or shared IO_PAD_ID on a
    board drawn by export, the rule this applies to the other branch.
    """
    assert len(ids) == len(places)
    for ident, place in zip(ids, places):
        if not ident:
            raise CarrierIdError(
                f"the carrier at ({place}) mm has no Reference; give each "
                f"carrier its own reference")
    counts = Counter(ids)
    shared = [f"the carriers at "
              f"{' and '.join(f'({p}) mm' for i, p in zip(ids, places) if i == ident)} "
              f"share the reference {ident!r}"
              for ident in sorted(i for i, n in counts.items() if n > 1)]
    if shared:
        raise CarrierIdError("; ".join(shared) + "; give each carrier its own reference")


def extract_io_pads(pcb_path: Path,
                    report: Optional[ExportReport] = None) -> List[Dict[str, Any]]:
    """The io_pads of the board at ``pcb_path``, named through ``report``
    (the shared read_export_report's) when it follows a sheet."""
    text = pcb_path.read_text(encoding="utf-8")
    tokens = tokenize(text)
    root, _ = parse(tokens, 0)

    out: List[Dict[str, Any]] = []
    carriers: List[BoardCarrier] = []
    # The nets of each carrier's pads and its KiCad position, one per entry of out.
    pad_nets: List[Set[str]] = []
    places: List[str] = []
    for fp in find_all(root, 'footprint'):
        ref = ''
        io_class = None
        io_pad_id = ''
        io_size_str = None
        x_mm, y_mm = 0.0, 0.0
        place = '0, 0'
        layer = ''
        linked = False

        for sub in _direct_children(fp):
            head = sub[0]
            if head == 'at' and len(sub) >= 3:
                x_mm = float(sub[1])
                y_mm = float(sub[2])
                place = f"{sub[1]}, {sub[2]}"
            elif head == 'layer' and len(sub) >= 2:
                layer = str(sub[1])
            elif head == 'path' and len(sub) >= 2:
                linked = bool(str(sub[1]))
            elif head == 'property' and len(sub) >= 3:
                key, val = str(sub[1]), str(sub[2])
                if key == 'IO_CLASS':
                    io_class = val
                elif key == 'IO_PAD_SIZE_UM':
                    io_size_str = val
                elif key == IO_PAD_ID:
                    io_pad_id = val
                elif key == 'Reference':
                    ref = val
            elif head == 'fp_text' and len(sub) >= 3 and sub[1] == 'reference':
                if not ref:
                    ref = str(sub[2])

        if not io_class:
            continue
        label = _carrier_label(ref, place)
        if io_class not in IO_CLASSES:
            raise CarrierPadError(
                f"carrier {label}: IO_CLASS {io_class!r} is not one of "
                f"{', '.join(IO_CLASSES)}")
        size = carrier_size_um(fp, io_size_str)
        if size is None:
            raise CarrierPadError(
                f"carrier {label}: no pad size; give IO_PAD_SIZE_UM as <x>x<y> in "
                f"micrometers, or a pad, with both sides above zero")
        lib_id = str(fp[1]) if len(fp) > 1 else ''
        lib = lib_id.partition(':')[0] if ':' in lib_id else ''
        carriers.append(BoardCarrier(ref, io_pad_id, lib, linked))

        pad_nets.append(_pad_nets(fp))
        places.append(place)
        out.append({
            'ref': ref,
            'io_class': io_class,
            # + 0.0 normalizes -0.0 (from negating a zero) to 0.0 in the JSON.
            'x_um': round(x_mm * 1000.0, 3) + 0.0,
            'y_um': round(-y_mm * 1000.0, 3) + 0.0,
            'size_x_um': size[0],
            'size_y_um': size[1],
            'net': '',
            'layer': IO_PAD_LAYER,
            'source_layer': layer,
        })
    ids, names = read_board_carriers(carriers, report)
    check_carrier_ids(ids, places)
    # Ids and the report are refused before a carrier's nets, as in the plugin.
    for entry, carrier, ident, nets in zip(out, carriers, ids, pad_nets):
        ref = carrier.reference
        label = ref if ident == ref else f"{ref} (io_pad {ident})"
        entry['net'] = carrier_net(label, nets)
        if names is not None:
            entry['net'] = planner_net(label, entry['net'], names)
        entry['ref'] = ident
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Extract I/O pad locations from a KiCad PCB into an io pad manifest.",
    )
    ap.add_argument('pcb', help='Input .kicad_pcb file')
    ap.add_argument('-o', '--output', default='io_pads.json',
                    help='Output JSON path (default: io_pads.json)')
    ap.add_argument('--export-report', metavar='JSON',
                    help='The report export --form wrote for this board; required '
                         'when the board was drawn against a sheet, to write planner '
                         'net names')
    args = ap.parse_args(argv)

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f'Error: file not found: {pcb_path}', file=sys.stderr)
        return 1

    report = None
    if args.export_report is not None:
        path = Path(args.export_report)
        try:
            report = read_export_report(json.loads(path.read_text(encoding="utf-8")),
                                        str(path))
        except (OSError, ValueError) as exc:
            print(f'Error: export report {path}: {exc}', file=sys.stderr)
            return 1
    try:
        pads = extract_io_pads(pcb_path, report)
    except (CarrierIdError, CarrierNetError, CarrierPadError) as exc:
        print(f'Error: {pcb_path}: {exc}', file=sys.stderr)
        return 1
    except RoundTripNamesError as exc:
        # With no report the only refusal is read_board_carriers' ask for one,
        # whose words cannot name this CLI's flag.
        hint = ' (--export-report)' if report is None else ''
        print(f'Error: {pcb_path}: {exc}{hint}', file=sys.stderr)
        return 1
    except (ValueError, OSError, UnicodeDecodeError) as exc:
        print(f'Error: could not parse {pcb_path}: {exc}', file=sys.stderr)
        return 1
    payload = {
        'schema': IO_PADS_SCHEMA,
        'version': IO_PADS_VERSION,
        'frame': IO_PADS_FRAME,
        'generator': IO_PADS_GENERATOR,
        'board': pcb_path.name,
        'io_pads': pads,
    }
    with atomic_write(args.output) as out:
        out.write(json.dumps(payload, indent=2) + '\n')
    print(f'Wrote {len(pads)} io_pads to {args.output}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
