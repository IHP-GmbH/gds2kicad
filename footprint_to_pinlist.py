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


def parse_pads_from_kicad_mod(filepath: str) -> List[dict]:
    """Parse pad entries from a .kicad_mod file.

    Returns list of dicts with: name, at_x_mm, at_y_mm, size_w_mm, size_h_mm
    """
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    pads = []
    # Match pad blocks: (pad "name" smd rect ... )
    # We need to handle nested parens, so use a state machine approach
    pad_pattern = re.compile(r'\(pad\s+"([^"]*)"')
    at_pattern = re.compile(r'\(at\s+([-\d.]+)\s+([-\d.]+)')
    size_pattern = re.compile(r'\(size\s+([-\d.]+)\s+([-\d.]+)')

    # Split into pad blocks by finding each (pad ...) at the top level
    lines = content.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        pad_match = pad_pattern.match(line)
        if pad_match:
            pad_name = pad_match.group(1)
            # Collect lines until we close this pad block
            block = line
            depth = line.count('(') - line.count(')')
            while depth > 0 and i + 1 < len(lines):
                i += 1
                block += '\n' + lines[i]
                depth += lines[i].count('(') - lines[i].count(')')

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
        i += 1

    return pads


def mm_to_dbu(mm_val: float, dbu_um: float = 0.001) -> float:
    """Convert mm to database units. Default: IHP (1 dbu = 1 nm)."""
    um_val = mm_val * 1000.0
    return um_val / dbu_um


def pads_to_pinlist_json(pads: List[dict], chiplet_name: str,
                         footprint_source: str,
                         dbu_um: float = 0.001) -> dict:
    """Convert parsed pads to PinList JSON format."""
    pins = []
    for i, pad in enumerate(pads):
        pins.append({
            'name': pad['name'],
            'type': 'passive',
            'side': 'left',
            'pad_index': i,
            'center_x_dbu': mm_to_dbu(pad['at_x_mm'], dbu_um),
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

    data = pads_to_pinlist_json(pads, chiplet_name, str(fp_path), dbu)
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
