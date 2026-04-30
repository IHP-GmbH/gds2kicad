#!/usr/bin/env python3
"""Extract I/O pad locations from a KiCad PCB file into a sidecar JSON.

Walks all footprints in a .kicad_pcb file, filters those whose
property IO_CLASS is set, and emits a JSON list:

  {
    "io_pads": [
      {"ref": "J1", "io_class": "wire_bond",
       "x_um": 5000.0, "y_um": 3500.0,
       "size_x_um": 100.0, "size_y_um": 100.0,
       "net": "VDD_EXT", "layer": "F.Cu"},
      ...
    ]
  }

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
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


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
    if tokens[pos] == '(':
        pos += 1
        out: List[Any] = []
        while tokens[pos] != ')':
            v, pos = parse(tokens, pos)
            out.append(v)
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


def parse_size(size_str: str) -> Tuple[float, float]:
    if 'x' in size_str.lower():
        a, b = size_str.lower().split('x', 1)
        return float(a), float(b)
    v = float(size_str)
    return v, v


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


def _pad_net(pad: List[Any]) -> str:
    for psub in pad[1:]:
        if isinstance(psub, list) and psub and psub[0] == 'net' and len(psub) >= 3:
            return str(psub[2])
    return ''


def extract_io_pads(pcb_path: Path) -> List[Dict[str, Any]]:
    text = pcb_path.read_text()
    tokens = tokenize(text)
    root, _ = parse(tokens, 0)

    out: List[Dict[str, Any]] = []
    for fp in find_all(root, 'footprint'):
        ref = ''
        io_class = None
        io_size_str = None
        x_mm, y_mm = 0.0, 0.0
        layer = ''

        for sub in _direct_children(fp):
            head = sub[0]
            if head == 'at' and len(sub) >= 3:
                x_mm = float(sub[1])
                y_mm = float(sub[2])
            elif head == 'layer' and len(sub) >= 2:
                layer = str(sub[1])
            elif head == 'property' and len(sub) >= 3:
                key, val = str(sub[1]), str(sub[2])
                if key == 'IO_CLASS':
                    io_class = val
                elif key == 'IO_PAD_SIZE_UM':
                    io_size_str = val
                elif key == 'Reference':
                    ref = val
            elif head == 'fp_text' and len(sub) >= 3 and sub[1] == 'reference':
                if not ref:
                    ref = str(sub[2])

        if io_class is None:
            continue

        if io_size_str:
            size_x_um, size_y_um = parse_size(io_size_str)
        else:
            pad = _first_pad(fp)
            if not pad:
                continue
            size_x_um, size_y_um = _pad_size_um(pad)
            if size_x_um == 0.0 or size_y_um == 0.0:
                continue

        net_name = _pad_net(_first_pad(fp))

        out.append({
            'ref': ref,
            'io_class': io_class,
            'x_um': x_mm * 1000.0,
            'y_um': -y_mm * 1000.0,
            'size_x_um': size_x_um,
            'size_y_um': size_y_um,
            'net': net_name,
            'layer': layer,
        })
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Extract I/O pad locations from a KiCad PCB into a sidecar JSON.",
    )
    ap.add_argument('pcb', help='Input .kicad_pcb file')
    ap.add_argument('-o', '--output', default='io_pads.json',
                    help='Output JSON path (default: io_pads.json)')
    args = ap.parse_args(argv)

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f'Error: file not found: {pcb_path}', file=sys.stderr)
        return 1

    pads = extract_io_pads(pcb_path)
    payload = {'io_pads': pads}
    Path(args.output).write_text(json.dumps(payload, indent=2) + '\n')
    print(f'Wrote {len(pads)} io_pads to {args.output}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
