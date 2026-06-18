#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
GDSII to KiCad Schematic Symbol Converter

Converts GDSII files to KiCad symbol format (.kicad_sym).
Extracts pad geometries and text labels to create symbols with named pins.
"""

import argparse
import sys
from pathlib import Path

try:
    import klayout.db as db
except ImportError:
    print("Error: KLayout Python module not found.", file=sys.stderr)
    print("Set PYTHONPATH to include KLayout's python directory.", file=sys.stderr)
    sys.exit(1)

from lyp_parser import LYPParser
from pin_extractor import PinExtractor
from kicad_sym_writer import KiCadSymWriter, SymbolDefinition, PinSide
from symbol_layout import create_default_layout, create_layout_from_pin_list
from pin_list import PinList
from gds_to_kicad import parse_layer_spec, resolve_pad_layer, DEFAULT_GENERIC_LYP


def generate_test_gds():
    """Generate a test GDS file with pads and text labels for development"""
    print("Generating test GDS file...")

    layout = db.Layout()
    top_cell = layout.create_cell("TEST_SYMBOL")

    # Layers matching SG13G2 conventions
    topmetal2_drawing = layout.layer(134, 0)   # TopMetal2.drawing
    topmetal2_text = layout.layer(134, 25)     # TopMetal2.text
    text_drawing = layout.layer(63, 0)         # TEXT.drawing

    # Create a 4x5 grid of pads (20 pads total), 80um x 80um, 200um pitch
    pad_names = [
        "VDD", "GND", "CLK", "RST",
        "VDDA", "VSSA", "DATA_IN", "DATA_OUT",
        "ADDR0", "ADDR1", "ADDR2", "ADDR3",
        "CS", "WE", "OE", "IRQ",
        "VCC", "VSS", "SDA", "SCL",
    ]

    pad_size = 80000   # 80 um in DBU (nm)
    pitch = 200000     # 200 um pitch

    for i, name in enumerate(pad_names):
        col = i % 4
        row = i // 4
        x0 = col * pitch
        y0 = row * pitch
        x1 = x0 + pad_size
        y1 = y0 + pad_size

        # Create pad geometry on TopMetal2.drawing
        top_cell.shapes(topmetal2_drawing).insert(db.Box(x0, y0, x1, y1))

        # Create text label at pad center on TopMetal2.text
        cx = (x0 + x1) // 2
        cy = (y0 + y1) // 2
        top_cell.shapes(topmetal2_text).insert(
            db.Text(name, db.Trans(db.Point(cx, cy)))
        )

    # Also add some texts on TEXT.drawing layer (for testing multi-layer)
    top_cell.shapes(text_drawing).insert(
        db.Text("TEST_ANNOTATION", db.Trans(db.Point(300000, 500000)))
    )

    output_file = "tests/test_symbol.gds"
    Path("tests").mkdir(exist_ok=True)
    layout.write(output_file)
    print(f"Generated: {output_file} ({len(pad_names)} pads)")
    return output_file


def scan_layers(gds_path: str, lyp_parser: LYPParser = None):
    """Scan GDS file and show which layers contain pads and text labels"""
    print(f"Scanning: {gds_path}")
    print("=" * 72)

    result = PinExtractor.scan_gds_layers(gds_path, lyp_parser)

    pad_cands = result['pad_candidates']
    text_cands = result['text_candidates']

    print(f"\nPad candidates (layers with box/polygon shapes):")
    print(f"  {'Layer':<40} {'Boxes':>7} {'Polys':>7} {'Total':>7}")
    print(f"  {'-'*40} {'-'*7} {'-'*7} {'-'*7}")
    for c in pad_cands[:15]:
        name = c['name'] or f"{c['layer_num']}/{c['datatype']}"
        ld = f"({c['layer_num']}/{c['datatype']})"
        label = f"{name} {ld}" if c['name'] else ld
        print(f"  {label:<40} {c['boxes']:>7} {c['polygons']:>7} {c['boxes']+c['polygons']:>7}")

    print(f"\nText candidates (layers with text labels):")
    print(f"  {'Layer':<40} {'Texts':>7}")
    print(f"  {'-'*40} {'-'*7}")
    for c in text_cands[:15]:
        name = c['name'] or f"{c['layer_num']}/{c['datatype']}"
        ld = f"({c['layer_num']}/{c['datatype']})"
        label = f"{name} {ld}" if c['name'] else ld
        print(f"  {label:<40} {c['texts']:>7}")

    print(f"\n{'='*72}")
    suggested_pad = result['suggested_pad_layer']
    suggested_text = result['suggested_text_layers']

    if suggested_pad:
        print(f"Suggested pad layer:   {suggested_pad}")
    else:
        print("Suggested pad layer:   (none found)")

    if suggested_text:
        print(f"Suggested text layers: {', '.join(suggested_text)}")
    else:
        print("Suggested text layers: (none found)")

    print(f"\nUsage:")
    if suggested_pad:
        cmd = f"  python3 gds_to_kicad_symbol.py {gds_path}"
        if lyp_parser:
            cmd += f" --lyp-file {lyp_parser.lyp_path}"
        cmd += f" --pad-layer {suggested_pad}"
        print(cmd)

    return result


def list_layers(lyp_parser: LYPParser):
    """List all layers from the LYP file"""
    print(f"Layers in {lyp_parser.lyp_path}:")
    print("-" * 60)
    for name in lyp_parser.get_layer_names():
        layer, datatype = lyp_parser.get_layer(name)
        print(f"  {name:<45} ({layer}/{datatype})")
    print("-" * 60)
    print(f"Total: {len(lyp_parser.get_layer_names())} layers")


def list_text_layers(lyp_parser: LYPParser, gds_path: str, pad_layer_name: str):
    """List text layers that contain actual text for a given pad layer"""
    layout = db.Layout()
    layout.read(gds_path)
    top_cell = layout.top_cell()
    if not top_cell:
        print("Error: No top cell found", file=sys.stderr)
        return

    top_cell.flatten(1)

    extractor = PinExtractor(lyp_parser)

    # Show candidate text layers from LYP conventions
    candidates = lyp_parser.find_text_layers_for(pad_layer_name)
    print(f"\nCandidate text layers for '{pad_layer_name}':")
    print("-" * 60)

    for name in candidates:
        layer_info = lyp_parser.get_layer(name)
        if not layer_info:
            continue

        layer_index = layout.layer(*layer_info)
        shapes = top_cell.shapes(layer_index)

        text_count = 0
        for shape in shapes.each():
            if shape.is_text():
                text_count += 1

        status = f"{text_count} texts" if text_count > 0 else "empty"
        marker = " <-- " if text_count > 0 else "     "
        print(f"  {name:<35} ({layer_info[0]}/{layer_info[1]})  {marker}{status}")

    print("-" * 60)

    # Also show auto-detected result
    detected = extractor.auto_detect_text_layers(layout, top_cell, pad_layer_name)
    if detected:
        names = [n for n, _ in detected]
        print(f"Auto-detected: {', '.join(names)}")
    else:
        print("Auto-detected: none (no text found on candidate layers)")


def _resolve_pad_and_text(args, lyp_parser):
    """Resolve pad + text layers for extraction, black-box aware.

    Returns (pad_layer, pad_name, text_layer, use_name):
      pad_layer  -- raw (layer, datatype) tuple, or None if a name was given
                    that the LYP cannot resolve;
      pad_name   -- resolved name or "N/D" string (metadata / messages);
      text_layer -- raw (layer, datatype) tuple for pin names, or None;
      use_name   -- True if pad_name is a real LYP layer name (use the
                    name-based extractor, preserving multi text-layer + name
                    auto-detect); False for the raw black-box path.
    """
    pad_layer, pad_name, auto_text = resolve_pad_layer(args, lyp_parser, args.input)

    if getattr(args, 'text_layer_number', None):
        text_layer = parse_layer_spec(args.text_layer_number)
    elif args.text_layer:
        text_layer = None
        for nm in args.text_layer:
            t = lyp_parser.get_layer(nm)
            if t:
                text_layer = t
                break
    else:
        text_layer = auto_text

    use_name = bool(pad_name) and lyp_parser.get_layer(pad_name) is not None
    return pad_layer, pad_name, text_layer, use_name


def _pad_resolution_error(lyp_file, pad_name):
    if pad_name:
        print(f"Error: pad layer '{pad_name}' not found in LYP ({lyp_file}). "
              f"Use --pad-layer-number N/D for a raw layer.", file=sys.stderr)
    else:
        print("Error: could not determine a pad layer. Pass --pad-layer NAME, "
              "--pad-layer-number N/D, or ensure the GDS has pad geometry to "
              "auto-detect.", file=sys.stderr)


def extract_pins(args):
    """Extract pin list from GDS and write to JSON."""
    lyp_parser = LYPParser(args.lyp_file)
    print(f"Loaded {lyp_parser}")

    extractor = PinExtractor(lyp_parser)

    pad_layer, pad_name, text_layer, use_name = _resolve_pad_and_text(args, lyp_parser)
    if pad_layer is None:
        _pad_resolution_error(args.lyp_file, pad_name)
        return False

    max_dist = float(args.max_text_distance) if args.max_text_distance else None

    if use_name:
        text_layers = args.text_layer if args.text_layer else None
        pads, cell_name = extractor.extract_named_pads(
            args.input, pad_name, text_layer_names=text_layers,
            max_distance=max_dist)
        meta_pad_layer = pad_name
        meta_text_layers = text_layers
    else:
        pads, cell_name = extractor.extract_named_pads_raw(
            args.input, pad_layer, text_layer=text_layer, max_distance=max_dist)
        meta_pad_layer = pad_name or f"{pad_layer[0]}/{pad_layer[1]}"
        meta_text_layers = ([f"{text_layer[0]}/{text_layer[1]}"]
                            if text_layer else None)

    print(f"\nTop cell: {cell_name}")
    print(f"Extracted {len(pads)} pads")

    pin_list = PinList.from_extracted_pads(
        pads,
        chiplet_name=cell_name,
        gds_source=Path(args.input).name,
        lyp_file=Path(args.lyp_file).name,
        pad_layer=meta_pad_layer,
        text_layers=meta_text_layers,
    )

    # Validate and warn
    warnings = pin_list.validate()
    for w in warnings:
        print(f"Warning: {w}", file=sys.stderr)

    pin_list.save(args.extract_pins)
    print(f"\nPin list saved: {args.extract_pins}")
    print(f"  {len(pin_list)} pins")
    print(f"  Review and edit the JSON, then use --from-pin-list to generate symbol")

    return True


def convert_from_pin_list(args):
    """Generate symbol from a user-reviewed pin list JSON."""
    pin_list = PinList.load(args.from_pin_list)
    print(f"Loaded pin list: {len(pin_list)} pins")
    print(f"  Chiplet: {pin_list.metadata.get('chiplet_name', 'unknown')}")

    warnings = pin_list.validate()
    for w in warnings:
        print(f"Warning: {w}", file=sys.stderr)

    symbol_name = args.symbol_name or pin_list.metadata.get('chiplet_name', 'SYMBOL')
    footprint_ref = args.footprint_ref or ""

    symbol = create_layout_from_pin_list(pin_list, symbol_name, footprint_ref)

    counts = symbol.pin_count_per_side()
    print(f"\nSymbol layout: {symbol.body_width:.2f} x {symbol.body_height:.2f} mm")
    print(f"  Left:   {counts[PinSide.LEFT]} pins")
    print(f"  Right:  {counts[PinSide.RIGHT]} pins")
    print(f"  Top:    {counts[PinSide.TOP]} pins")
    print(f"  Bottom: {counts[PinSide.BOTTOM]} pins")

    if args.output:
        output_path = args.output
    else:
        output_dir = Path("generated_kicad_symbol_files")
        output_dir.mkdir(exist_ok=True)
        output_path = str(output_dir / f"{symbol_name}.kicad_sym")

    writer = KiCadSymWriter()
    writer.write_symbol_library([symbol], output_path)
    print(f"\nOutput: {output_path}")

    return True


def convert(args):
    """Main conversion pipeline"""
    lyp_parser = LYPParser(args.lyp_file)
    print(f"Loaded {lyp_parser}")

    extractor = PinExtractor(lyp_parser)

    pad_layer, pad_name, text_layer, use_name = _resolve_pad_and_text(args, lyp_parser)
    if pad_layer is None:
        _pad_resolution_error(args.lyp_file, pad_name)
        return False

    max_dist = float(args.max_text_distance) if args.max_text_distance else None

    if use_name:
        text_layers = args.text_layer if args.text_layer else None
        pads, cell_name = extractor.extract_named_pads(
            args.input, pad_name, text_layer_names=text_layers,
            max_distance=max_dist)
    else:
        pads, cell_name = extractor.extract_named_pads_raw(
            args.input, pad_layer, text_layer=text_layer, max_distance=max_dist)

    print(f"\nTop cell: {cell_name}")
    print(f"Extracted {len(pads)} pads")

    # Determine symbol name
    symbol_name = args.symbol_name or cell_name

    # Build footprint reference
    footprint_ref = args.footprint_ref or ""

    # Create default layout
    symbol = create_default_layout(pads, symbol_name, footprint_ref)

    # Print layout summary
    counts = symbol.pin_count_per_side()
    print(f"\nSymbol layout: {symbol.body_width:.2f} x {symbol.body_height:.2f} mm")
    print(f"  Left:   {counts[PinSide.LEFT]} pins")
    print(f"  Right:  {counts[PinSide.RIGHT]} pins")
    print(f"  Top:    {counts[PinSide.TOP]} pins")
    print(f"  Bottom: {counts[PinSide.BOTTOM]} pins")

    # Determine output path
    if args.output:
        output_path = args.output
    else:
        output_dir = Path("generated_kicad_symbol_files")
        output_dir.mkdir(exist_ok=True)
        output_path = str(output_dir / f"{Path(args.input).stem}.kicad_sym")

    # Write symbol
    writer = KiCadSymWriter()
    writer.write_symbol_library([symbol], output_path)
    print(f"\nOutput: {output_path}")

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Convert GDSII files to KiCad schematic symbols (.kicad_sym)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s input.gds --scan-layers                          # scan without LYP
  %(prog)s input.gds --lyp-file pdk.lyp --scan-layers       # scan with layer names
  %(prog)s input.gds --lyp-file pdk.lyp --pad-layer TopMetal2.drawing
  %(prog)s input.gds --lyp-file sg13g2.lyp --pad-layer TopMetal2.drawing --text-layer TopMetal2.text
  %(prog)s --lyp-file pdk.lyp --list-layers
  %(prog)s input.gds --lyp-file pdk.lyp --pad-layer TopMetal2.drawing --list-text-layers
  %(prog)s --generate-test-gds

Pin list workflow (human-in-the-loop):
  %(prog)s input.gds --lyp-file pdk.lyp --pad-layer TopMetal2.drawing --extract-pins pins.json
  # Edit pins.json (rename, retype, reside)
  %(prog)s --from-pin-list pins.json -o output.kicad_sym
        """
    )

    parser.add_argument('input', nargs='?', help='Input GDSII file')
    parser.add_argument('-o', '--output', help='Output .kicad_sym file')
    parser.add_argument('--lyp-file', default=str(DEFAULT_GENERIC_LYP),
                        help='KLayout .lyp file with layer definitions '
                             '(default: bundled generic pads-only lyp)')
    parser.add_argument('--pad-layer', help='Layer name for pads (e.g., TopMetal2.drawing)')
    parser.add_argument('--pad-layer-number', metavar='N/D',
                        help='Raw pad layer number N/D (black-box; bypasses the LYP)')
    parser.add_argument('--text-layer', action='append',
                        help='Text layer name(s) for pin names (auto-detected if omitted)')
    parser.add_argument('--text-layer-number', metavar='N/D',
                        help='Raw text layer number N/D for pin names (black-box)')
    parser.add_argument('--symbol-name', help='Override symbol name (defaults to cell name)')
    parser.add_argument('--footprint-ref',
                        help='KiCad footprint reference (e.g., "MyLib:Footprint")')
    parser.add_argument('--max-text-distance', type=float,
                        help='Maximum distance in DBU for text-to-pad association')
    parser.add_argument('--list-layers', action='store_true',
                        help='List all layers in the LYP file and exit')
    parser.add_argument('--list-text-layers', action='store_true',
                        help='List text layers with content for the specified pad layer')
    parser.add_argument('--scan-layers', action='store_true',
                        help='Scan GDS and suggest best pad/text layers')
    parser.add_argument('--generate-test-gds', action='store_true',
                        help='Generate a test GDS file for development')

    # Pin list workflow flags
    parser.add_argument('--extract-pins', metavar='OUTPUT_JSON',
                        help='Extract pin list from GDS and write to JSON file')
    parser.add_argument('--from-pin-list', metavar='PIN_LIST_JSON',
                        help='Generate symbol from a pin list JSON (no GDS needed)')

    args = parser.parse_args()

    if args.generate_test_gds:
        generate_test_gds()
        return 0

    if args.scan_layers:
        if not args.input:
            parser.error("Input GDS file required with --scan-layers")
        lyp = LYPParser(args.lyp_file) if args.lyp_file else None
        scan_layers(args.input, lyp)
        return 0

    if args.list_layers:
        if not args.lyp_file:
            parser.error("--lyp-file is required with --list-layers")
        lyp = LYPParser(args.lyp_file)
        list_layers(lyp)
        return 0

    if args.list_text_layers:
        if not args.lyp_file or not args.input or not args.pad_layer:
            parser.error("--lyp-file, input GDS, and --pad-layer required with --list-text-layers")
        lyp = LYPParser(args.lyp_file)
        list_text_layers(lyp, args.input, args.pad_layer)
        return 0

    # Pin list extraction mode
    if args.extract_pins:
        if not args.input:
            parser.error("Input GDS file required with --extract-pins")
        success = extract_pins(args)
        return 0 if success else 1

    # Symbol from pin list mode
    if args.from_pin_list:
        success = convert_from_pin_list(args)
        return 0 if success else 1

    # Conversion mode
    if not args.input:
        parser.error("Input GDS file required")

    success = convert(args)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
