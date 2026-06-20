#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
footprint_to_pinlist.py -- Extract PinList JSON from KiCad footprint (.kicad_mod)

Parses pad entries from a .kicad_mod file and generates a PinList JSON
compatible with bump_mirror.py and the gds_to_kicad ecosystem.

KiCad footprints use mm; PinList uses dbu (database units).
For IHP SG13G2 (dbu=0.001um): 1 dbu = 1 nm, so mm * 1e6 = dbu.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import List, Optional

from _paths import atomic_write


def _pad_block(content: str, start: int) -> str:
    """Return the balanced ``(pad ...)`` block starting at index ``start``.

    Walks parentheses from the opening '(' to its match, skipping quoted
    strings, so it works regardless of indentation or line breaks (compact /
    single-line .kicad_mod files included).
    """
    depth = 0
    in_str = False
    i = start
    while i < len(content):
        c = content[i]
        if in_str:
            if c == '\\':
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return content[start:i + 1]
        i += 1
    return content[start:]


def parse_pads_from_kicad_mod(filepath: str) -> List[dict]:
    """Parse pad entries from a .kicad_mod file.

    Returns list of dicts with: name, at_x_mm, at_y_mm, size_w_mm, size_h_mm
    """
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    pads = []
    at_pattern = re.compile(r'\(at\s+([-\d.]+)\s+([-\d.]+)')
    size_pattern = re.compile(r'\(size\s+([-\d.]+)\s+([-\d.]+)')

    # Find every (pad "name" occurrence anywhere in the file (not just at the
    # start of a line) and balance parens from there, so compact or
    # programmatically-generated footprints that put (pad ...) mid-line are not
    # silently skipped.
    for m in re.finditer(r'\(pad\s+"([^"]*)"', content):
        pad_name = m.group(1)
        block = _pad_block(content, m.start())
        at_match = at_pattern.search(block)
        size_match = size_pattern.search(block)
        if at_match:
            pads.append({
                'name': pad_name,
                'at_x_mm': float(at_match.group(1)),
                'at_y_mm': float(at_match.group(2)),
                'size_w_mm': float(size_match.group(1)) if size_match else 0.0,
                'size_h_mm': float(size_match.group(2)) if size_match else 0.0,
            })

    return pads


def read_orientation(filepath: str) -> str:
    """Return the footprint ORIENTATION property ('flip_chip'/'face_up'), or ''."""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    m = re.search(r'\(property\s+"ORIENTATION"\s+"([^"]*)"', content)
    return m.group(1) if m else ''


def mm_to_dbu(mm_val: float, dbu_um: float = 0.001) -> float:
    """Convert mm to database units. Default: IHP (1 dbu = 1 nm)."""
    um_val = mm_val * 1000.0
    return um_val / dbu_um


def pads_to_pinlist_json(pads: List[dict], chiplet_name: str,
                         footprint_source: str,
                         dbu_um: float = 0.001,
                         flip_chip: bool = False) -> dict:
    """Convert parsed pads to PinList JSON format.

    Maps KiCad (Y-down) coordinates back to GDS (Y-up) by negating Y. When the
    footprint was generated --flip-chip (ORIENTATION=flip_chip), gds_to_kicad
    also mirrored X (mx=-1); un-mirror it here so the recovered coordinates are
    in the original die frame rather than a mixed die/footprint frame.
    """
    mx = -1 if flip_chip else 1
    pins = []
    for i, pad in enumerate(pads):
        pins.append({
            'name': pad['name'],
            'type': 'passive',
            'side': 'left',
            'pad_index': i,
            'center_x_dbu': mm_to_dbu(mx * pad['at_x_mm'], dbu_um),
            'center_y_dbu': mm_to_dbu(-pad['at_y_mm'], dbu_um),  # KiCad y-down -> GDS y-up
            'width_dbu': mm_to_dbu(pad['size_w_mm'], dbu_um),
            'height_dbu': mm_to_dbu(pad['size_h_mm'], dbu_um),
        })

    return {
        'version': 1,
        'chiplet_name': chiplet_name,
        'source': footprint_source,
        'source_type': 'kicad_footprint',
        'dbu_um': dbu_um,
        'timestamp': '',
        'pins': pins,
    }


def extract_one(fp_path: Path, output: Optional[str], name: Optional[str],
                dbu: float) -> int:
    """Extract pin list from a single footprint."""
    if not fp_path.exists():
        print(f"Error: {fp_path} not found", file=sys.stderr)
        return 1

    chiplet_name = name or fp_path.stem
    pads = parse_pads_from_kicad_mod(str(fp_path))
    if not pads:
        print(f"Warning: No pads found in {fp_path}", file=sys.stderr)
        return 1

    flip_chip = read_orientation(str(fp_path)) == "flip_chip"
    data = pads_to_pinlist_json(pads, chiplet_name, str(fp_path), dbu,
                                flip_chip=flip_chip)
    out_path = output or f"{chiplet_name}_pins.json"
    with atomic_write(out_path) as f:
        json.dump(data, f, indent=2)

    print(f"Extracted {len(pads)} pins from {fp_path.name} -> {out_path}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description='Extract PinList JSON from KiCad footprint (.kicad_mod)',
    )
    parser.add_argument('footprint', nargs='+',
                        help='Path(s) to .kicad_mod file(s)')
    parser.add_argument('-o', '--output',
                        help='Output JSON path (single footprint only)')
    parser.add_argument('--output-dir',
                        help='Output directory for batch mode (default: cwd)')
    parser.add_argument('--name',
                        help='Chiplet name (single footprint only)')
    parser.add_argument('--dbu', type=float, default=0.001,
                        help='Database unit in um (default: 0.001 for IHP)')

    args = parser.parse_args(argv)

    if len(args.footprint) == 1:
        return extract_one(Path(args.footprint[0]), args.output, args.name,
                           args.dbu)

    # Batch mode
    if args.output:
        print("Warning: -o ignored in batch mode, use --output-dir",
              file=sys.stderr)

    out_dir = Path(args.output_dir) if args.output_dir else Path('.')
    out_dir.mkdir(parents=True, exist_ok=True)

    errors = 0
    for fp in args.footprint:
        fp_path = Path(fp)
        out_path = str(out_dir / f"{fp_path.stem}_pins.json")
        errors += extract_one(fp_path, out_path, None, args.dbu)

    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
