#!/usr/bin/env python3
"""
GDSII to KiCad Footprint Converter

Converts GDSII files to KiCad footprint format (.kicad_mod).
Extracts pad geometries from specified layer defined in a KLayout .lyp file.
Supports optional text layer for pin name association.
"""

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Tuple, Optional

try:
    import klayout.db as db
except ImportError:
    print("Error: KLayout Python module not found.", file=sys.stderr)
    print("Please ensure KLayout is installed and PYTHONPATH is configured.", file=sys.stderr)
    print("See docs/README.md for setup instructions.", file=sys.stderr)
    sys.exit(1)

from lyp_parser import LYPParser
from pin_list import PinList
from pad_review import PadReview


class GDSToKiCad:
    """Main converter class"""

    def __init__(self, lyp_parser: LYPParser, layer_name: str,
                 text_layer_name: Optional[str] = None,
                 auto_detect_text: bool = False,
                 dbu: Optional[float] = None):
        self.lyp_parser = lyp_parser
        self.layer_name = layer_name
        self.text_layer_name = text_layer_name
        self.auto_detect_text = auto_detect_text
        self._dbu_override = dbu

        # Get layer from LYP
        self.pad_layer = lyp_parser.get_layer(layer_name)

        if not self.pad_layer:
            print(f"Error: Layer '{layer_name}' not found in LYP file", file=sys.stderr)
            print(f"Available layers:", file=sys.stderr)
            for name in lyp_parser.get_layer_names()[:10]:
                layer, dt = lyp_parser.get_layer(name)
                print(f"  {name} ({layer}/{dt})", file=sys.stderr)
            if len(lyp_parser.get_layer_names()) > 10:
                print(f"  ... and {len(lyp_parser.get_layer_names()) - 10} more", file=sys.stderr)
            sys.exit(1)

        print(f"Using layer: {layer_name} {self.pad_layer}")
        if text_layer_name:
            print(f"Using text layer: {text_layer_name}")

    def _resolve_dbu(self, layout: db.Layout) -> float:
        """Return the database unit in microns.

        Uses explicit override if set, otherwise reads from layout.dbu.
        """
        if self._dbu_override is not None:
            return self._dbu_override
        return layout.dbu

    def _get_dbu_to_mm(self, layout: db.Layout) -> float:
        """Compute conversion factor from database units to millimeters."""
        dbu_um = self._resolve_dbu(layout)
        return dbu_um * 1e-3

    def convert(self, gds_path: str, output_path: str):
        """Convert GDS file to KiCad footprint"""
        print(f"\nConverting {gds_path} -> {output_path}")

        # Load GDSII file
        layout = db.Layout()
        layout.read(gds_path)

        # Get top cell
        top_cell = layout.top_cell()
        if not top_cell:
            print("Error: No top cell found in GDS file", file=sys.stderr)
            return False

        # Flatten hierarchy to access all geometries
        top_cell.flatten(1)
        print(f"Top cell: {top_cell.name} (flattened)")

        # Detect DBU conversion factor
        dbu_to_mm = self._get_dbu_to_mm(layout)
        dbu_um = self._resolve_dbu(layout)
        print(f"DBU: {dbu_um} um ({dbu_to_mm:.3e} mm)")

        # Extract pads with optional text association
        pad_names = {}
        if self.text_layer_name or self.auto_detect_text:
            try:
                from pin_extractor import PinExtractor
                extractor = PinExtractor(self.lyp_parser)

                # Use PinExtractor on the already-loaded/flattened layout
                pads_raw = extractor.extract_pads(layout, top_cell, self.pad_layer)

                if self.text_layer_name:
                    # Explicit text layer
                    text_layer_info = self.lyp_parser.get_layer(self.text_layer_name)
                    if text_layer_info:
                        text_layers = [(self.text_layer_name, text_layer_info)]
                    else:
                        print(f"Warning: Text layer '{self.text_layer_name}' not found in LYP")
                        text_layers = []
                else:
                    # Auto-detect text layers
                    text_layers = extractor.auto_detect_text_layers(
                        layout, top_cell, self.layer_name
                    )
                    if text_layers:
                        names = [n for n, _ in text_layers]
                        print(f"Auto-detected text layers: {', '.join(names)}")
                    else:
                        print("No text layers auto-detected, using sequential numbering")

                if text_layers:
                    texts = extractor.extract_texts(layout, top_cell, text_layers)
                    pads_raw = extractor.associate_texts_with_pads(pads_raw, texts)

                    for pi in pads_raw:
                        if pi.name:
                            pad_names[pi.index] = pi.name

                    layer_names = [n for n, _ in text_layers]
                    print(f"Text layers: {', '.join(layer_names)} ({len(texts)} labels)")
                    print(f"Named pads: {len(pad_names)}/{len(pads_raw)}")

            except ImportError:
                print("Warning: pin_extractor module not available, using sequential numbering")

        pad_dicts = self._extract_pads(layout, top_cell)
        print(f"Found {len(pad_dicts)} pads")

        # Calculate and display bounding box info for coordinate alignment verification
        boxes = [pd["bbox"] for pd in pad_dicts]
        self._print_bounding_box_info(boxes, dbu_to_mm=dbu_to_mm)

        # Generate footprint
        self._generate_kicad_footprint(top_cell.name, pad_dicts, output_path, gds_path,
                                        pad_names=pad_names, dbu_to_mm=dbu_to_mm)

        return True

    def _extract_pads(self, layout: db.Layout, cell: db.Cell) -> List[dict]:
        """Extract pad geometries from specified metal layer.

        Returns list of dicts with keys: bbox (db.Box), is_polygon (bool),
        polygon_points (list of (x,y) tuples or None).
        """
        pads = []

        layer_index = layout.layer(*self.pad_layer)
        shapes = cell.shapes(layer_index)

        for shape in shapes.each():
            if shape.is_box():
                pads.append({
                    "bbox": shape.box,
                    "is_polygon": False,
                    "polygon_points": None,
                })
            elif shape.is_polygon():
                poly = shape.polygon
                points = [(int(p.x), int(p.y)) for p in poly.each_point_hull()]
                pads.append({
                    "bbox": poly.bbox(),
                    "is_polygon": True,
                    "polygon_points": points,
                })

        return pads

    def _print_bounding_box_info(self, pads: List[db.Box],
                                dbu_to_mm: Optional[float] = None):
        """Calculate and print bounding box info for coordinate alignment verification.

        This helps users understand the relationship between the GDS origin (0,0)
        and the KiCad footprint anchor, which is critical for HYP-to-GDS workflows.
        """
        if not pads:
            print("\nNo pads found - cannot calculate bounding box")
            return

        if dbu_to_mm is None:
            dbu_to_mm = 1e-6  # fallback: 1 DBU = 1nm
        DBU_TO_UM = dbu_to_mm * 1e3

        # Calculate GDS bounding box
        gds_min_x = min(pad.left for pad in pads) * DBU_TO_UM
        gds_max_x = max(pad.right for pad in pads) * DBU_TO_UM
        gds_min_y = min(pad.bottom for pad in pads) * DBU_TO_UM
        gds_max_y = max(pad.top for pad in pads) * DBU_TO_UM

        width = gds_max_x - gds_min_x
        height = gds_max_y - gds_min_y

        # KiCad coordinates (Y is negated)
        kicad_min_x = gds_min_x
        kicad_max_x = gds_max_x
        kicad_min_y = -gds_max_y  # Y negated, so min/max swap
        kicad_max_y = -gds_min_y

        print(f"\nGDS Bounding Box (original):")
        print(f"  Min: ({gds_min_x:.3f}, {gds_min_y:.3f}) um")
        print(f"  Max: ({gds_max_x:.3f}, {gds_max_y:.3f}) um")
        print(f"  Size: {width:.3f} x {height:.3f} um")
        print(f"\nKiCad Bounding Box (Y negated):")
        print(f"  Min: ({kicad_min_x:.3f}, {kicad_min_y:.3f}) um")
        print(f"  Max: ({kicad_max_x:.3f}, {kicad_max_y:.3f}) um")
        print(f"\nCoordinate transformation: GDS (Y-up) -> KiCad (Y-down)")
        print(f"KiCad anchor at (0,0) = GDS origin (0,0)")

    def _generate_kicad_footprint(self, name: str, pad_dicts: List[dict], output_path: str,
                                    gds_path: str, pad_names: Optional[Dict] = None,
                                    dbu_to_mm: Optional[float] = None):
        """Generate KiCad footprint file with named or numbered pads.

        pad_dicts: list of dicts with keys:
            bbox (db.Box), is_polygon (bool), polygon_points (list or None)
        """
        print(f"Generating KiCad footprint: {output_path}")

        if pad_names is None:
            pad_names = {}

        if dbu_to_mm is None:
            dbu_to_mm = 1e-6  # fallback: 1 DBU = 1nm
        DBU_TO_MM = dbu_to_mm

        # Source file names for traceability properties
        gds_filename = Path(gds_path).name
        lyp_filename = Path(self.lyp_parser.lyp_path).name
        layer_num, layer_dt = self.pad_layer

        with open(output_path, 'w') as f:
            # Header
            f.write(f'(footprint "{name}"\n')
            f.write('  (layer "F.Cu")\n')
            f.write('  (descr "Auto-generated from GDSII")\n')
            f.write('  (attr smd)\n\n')

            # Traceability properties
            f.write(f'  (property "GDS_FILE" "{gds_filename}")\n')
            f.write(f'  (property "LYP_FILE" "{lyp_filename}")\n')
            f.write(f'  (property "GDS_LAYER" "{self.layer_name} ({layer_num}/{layer_dt})")\n\n')

            # Reference and value text
            f.write('  (fp_text reference "REF**" (at 0 0) (layer "F.SilkS")\n')
            f.write('    (effects (font (size 0.1 0.1) (thickness 0.015)))\n')
            f.write('  )\n')
            f.write(f'  (fp_text value "{name}" (at 0 -2) (layer "F.Fab")\n')
            f.write('    (effects (font (size 0.1 0.1) (thickness 0.015)))\n')
            f.write('  )\n\n')

            # Generate pads (named if text layer provided, otherwise sequential)
            for idx, pd in enumerate(pad_dicts):
                # Use text label name if available, otherwise sequential number
                pad_name = pad_names.get(idx, str(idx + 1))
                pad = pd["bbox"]
                is_polygon = pd.get("is_polygon", False)
                polygon_points = pd.get("polygon_points")

                # Calculate pad center and size in mm
                # Note: Y is negated to convert from GDS (Y-up) to KiCad (Y-down) convention
                center_x = ((pad.left + pad.right) / 2) * DBU_TO_MM
                center_y = -((pad.bottom + pad.top) / 2) * DBU_TO_MM
                width = (pad.right - pad.left) * DBU_TO_MM
                height = (pad.top - pad.bottom) * DBU_TO_MM

                if is_polygon and polygon_points:
                    # Custom polygon pad
                    f.write(f'  (pad "{pad_name}" smd custom (at {center_x:.6f} {center_y:.6f})\n')
                    f.write(f'    (size {width:.6f} {height:.6f})\n')
                    f.write('    (layers "F.Cu" "F.Paste" "F.Mask")\n')
                    # Polygon vertices relative to pad center, Y negated
                    cx_dbu = (pad.left + pad.right) / 2.0
                    cy_dbu = (pad.bottom + pad.top) / 2.0
                    f.write('    (primitives\n')
                    f.write('      (gr_poly\n')
                    f.write('        (pts\n')
                    for px, py in polygon_points:
                        rx = (px - cx_dbu) * DBU_TO_MM
                        ry = -((py - cy_dbu) * DBU_TO_MM)
                        f.write(f'          (xy {rx:.6f} {ry:.6f})\n')
                    f.write('        )\n')
                    f.write('        (width 0) (fill yes)\n')
                    f.write('      )\n')
                    f.write('    )\n')
                    f.write('  )\n')
                else:
                    # Rectangular pad
                    f.write(f'  (pad "{pad_name}" smd rect (at {center_x:.6f} {center_y:.6f})\n')
                    f.write(f'    (size {width:.6f} {height:.6f})\n')
                    f.write('    (layers "F.Cu" "F.Paste" "F.Mask")\n')
                    f.write('  )\n')

            # Courtyard from pad bounding box (no margin -- exact chiplet boundary)
            if pad_dicts:
                min_x = min(pd["bbox"].left for pd in pad_dicts) * DBU_TO_MM
                max_x = max(pd["bbox"].right for pd in pad_dicts) * DBU_TO_MM
                min_y = -max(pd["bbox"].top for pd in pad_dicts) * DBU_TO_MM    # Y negated
                max_y = -min(pd["bbox"].bottom for pd in pad_dicts) * DBU_TO_MM
                f.write(f'\n  (fp_rect (start {min_x:.6f} {min_y:.6f}) (end {max_x:.6f} {max_y:.6f})\n')
                f.write('    (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd")\n')
                f.write('  )\n')

            # Footer
            f.write(')\n')

        print(f"Generated {len(pad_dicts)} pads")

    def convert_from_pad_review(self, edited_gds: str, pin_list: PinList,
                                output_path: str):
        """Generate footprint from a user-edited pad review GDS.

        Uses pin_list for pad naming instead of text extraction from the
        original GDS. The user has already cleaned the pad review GDS
        to contain only real bond pads.

        Args:
            edited_gds: Path to user-edited pad review GDS
            pin_list: PinList with authoritative pad names
            output_path: Output .kicad_mod path
        """
        print(f"\nGenerating footprint from pad review GDS: {edited_gds}")

        # Load layout to detect dbu
        tmp_layout = db.Layout()
        tmp_layout.read(edited_gds)
        dbu_to_mm = self._get_dbu_to_mm(tmp_layout)
        dbu_um = self._resolve_dbu(tmp_layout)
        print(f"DBU: {dbu_um} um ({dbu_to_mm:.3e} mm)")

        # Read pads from edited GDS, match names from pin list
        pad_dicts = PadReview.read_edited_pads(
            edited_gds, self.pad_layer, pin_list=pin_list
        )

        if not pad_dicts:
            print("Error: No pads found in edited GDS", file=sys.stderr)
            return False

        named = sum(1 for p in pad_dicts if p["name"])
        print(f"Found {len(pad_dicts)} pads ({named} named from pin list)")

        # Build pad_names dict and pad dicts for footprint gen
        pad_names = {}
        fp_pads = []
        for i, pd in enumerate(pad_dicts):
            if pd["name"]:
                pad_names[i] = pd["name"]
            left, bottom, right, top = pd["bbox"]
            box = db.Box(int(left), int(bottom), int(right), int(top))
            fp_pads.append({
                "bbox": box,
                "is_polygon": pd.get("is_polygon", False),
                "polygon_points": pd.get("polygon_points"),
            })

        boxes = [pd["bbox"] for pd in fp_pads]
        self._print_bounding_box_info(boxes, dbu_to_mm=dbu_to_mm)

        self._generate_kicad_footprint(
            pin_list.metadata.get("chiplet_name", Path(edited_gds).stem),
            fp_pads, output_path, edited_gds,
            pad_names=pad_names,
            dbu_to_mm=dbu_to_mm,
        )

        return True


def generate_test_gds():
    """Generate a test GDS file for development"""
    print("Generating test GDS file...")

    layout = db.Layout()
    top_cell = layout.create_cell("TEST_FOOTPRINT")

    # Create layers
    topmetal2 = layout.layer(134, 0)

    # Create sample pads (no text labels for 1:1 mapping)
    top_cell.shapes(topmetal2).insert(db.Box(0, 0, 100000, 100000))  # 100um x 100um
    top_cell.shapes(topmetal2).insert(db.Box(200000, 0, 300000, 100000))
    top_cell.shapes(topmetal2).insert(db.Box(0, 200000, 100000, 300000))

    # Save to tests directory
    output_file = "tests/test_footprint.gds"
    Path("tests").mkdir(exist_ok=True)
    layout.write(output_file)
    print(f"Generated: {output_file}")


def main():
    parser = argparse.ArgumentParser(
        description="Convert GDSII files to KiCad footprints using layer definitions from .lyp files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s input.gds --lyp-file pdk.lyp --scan-layers
  %(prog)s input.gds --lyp-file pdk.lyp --layer TopMetal2.drawing --auto-text
  %(prog)s input.gds --lyp-file pdk.lyp --layer TopMetal2.drawing --text-layer TopMetal2.text
  %(prog)s gds_files/chip.gds --lyp-file sg13g2.lyp --layer Metal5.drawing -o output.kicad_mod
  %(prog)s --lyp-file pdk.lyp --list-layers
  %(prog)s --generate-test-gds

Pad review workflow (human-in-the-loop):
  %(prog)s input.gds --lyp-file pdk.lyp --layer TopMetal2.drawing --generate-pad-review review.gds
  # Edit review.gds in KLayout, remove non-pad shapes
  %(prog)s --from-pad-review review.gds --pin-list pins.json --layer TopMetal2.drawing --lyp-file pdk.lyp -o output.kicad_mod
        """
    )

    parser.add_argument('input', nargs='?', help='Input GDSII file')
    parser.add_argument('-o', '--output', help='Output KiCad footprint file')
    parser.add_argument('--lyp-file', help='KLayout .lyp file with layer definitions')
    parser.add_argument('--layer', help='Layer name to extract (e.g., TopMetal2.drawing)')
    parser.add_argument('--text-layer',
                       help='Text layer name for pin names (e.g., TopMetal2.text)')
    parser.add_argument('--auto-text', action='store_true',
                       help='Auto-detect text layers for pin names')
    parser.add_argument('--dbu', type=float, default=None,
                       help='Override database unit in microns (default: read from GDS)')
    parser.add_argument('--scan-layers', action='store_true',
                       help='Scan GDS and suggest best pad/text layers')
    parser.add_argument('--list-layers', action='store_true',
                       help='List all layers in the LYP file and exit')
    parser.add_argument('--generate-test-gds', action='store_true',
                       help='Generate a test GDS file for development')

    # Pad review GDS workflow flags
    parser.add_argument('--generate-pad-review', metavar='OUTPUT_GDS',
                       help='Generate pad review GDS with only pad layer for editing')
    parser.add_argument('--from-pad-review', metavar='EDITED_GDS',
                       help='Generate footprint from user-edited pad review GDS')
    parser.add_argument('--pin-list', metavar='PIN_LIST_JSON',
                       help='Pin list JSON for pad naming (used with --from-pad-review)')

    args = parser.parse_args()

    # Handle test GDS generation
    if args.generate_test_gds:
        generate_test_gds()
        return 0

    # Handle list-layers
    if args.list_layers:
        if not args.lyp_file:
            parser.error("--lyp-file is required with --list-layers")
        lyp = LYPParser(args.lyp_file)
        print(f"Layers in {args.lyp_file}:")
        print("-" * 50)
        for name in lyp.get_layer_names():
            layer, datatype = lyp.get_layer(name)
            print(f"  {name:<40} ({layer}/{datatype})")
        print("-" * 50)
        print(f"Total: {len(lyp.get_layer_names())} layers")
        return 0

    # Handle scan-layers
    if args.scan_layers:
        if not args.input:
            parser.error("Input GDS file required with --scan-layers")
        from pin_extractor import PinExtractor
        lyp = LYPParser(args.lyp_file) if args.lyp_file else None
        result = PinExtractor.scan_gds_layers(args.input, lyp)

        print(f"Scanning: {args.input}")
        print("=" * 72)
        print(f"\nPad candidates (layers with box/polygon shapes):")
        print(f"  {'Layer':<40} {'Boxes':>7} {'Polys':>7} {'Total':>7}")
        print(f"  {'-'*40} {'-'*7} {'-'*7} {'-'*7}")
        for c in result['pad_candidates'][:15]:
            name = c['name'] or f"{c['layer_num']}/{c['datatype']}"
            ld = f"({c['layer_num']}/{c['datatype']})"
            label = f"{name} {ld}" if c['name'] else ld
            print(f"  {label:<40} {c['boxes']:>7} {c['polygons']:>7} {c['boxes']+c['polygons']:>7}")

        print(f"\nText candidates (layers with text labels):")
        print(f"  {'Layer':<40} {'Texts':>7}")
        print(f"  {'-'*40} {'-'*7}")
        for c in result['text_candidates'][:15]:
            name = c['name'] or f"{c['layer_num']}/{c['datatype']}"
            ld = f"({c['layer_num']}/{c['datatype']})"
            label = f"{name} {ld}" if c['name'] else ld
            print(f"  {label:<40} {c['texts']:>7}")

        print(f"\n{'='*72}")
        sp = result['suggested_pad_layer']
        st = result['suggested_text_layers']
        print(f"Suggested pad layer:   {sp or '(none found)'}")
        print(f"Suggested text layers: {', '.join(st) if st else '(none found)'}")
        return 0

    # Generate pad review GDS
    if args.generate_pad_review:
        if not args.input:
            parser.error("Input GDS file required with --generate-pad-review")
        if not args.lyp_file:
            parser.error("--lyp-file is required with --generate-pad-review")
        if not args.layer:
            parser.error("--layer is required with --generate-pad-review")

        lyp_parser = LYPParser(args.lyp_file)
        pad_layer = lyp_parser.get_layer(args.layer)
        if not pad_layer:
            parser.error(f"Layer '{args.layer}' not found in LYP file")

        # Resolve text layer
        text_layer_info = None
        if args.text_layer:
            text_layer_info = lyp_parser.get_layer(args.text_layer)

        # Load pin list if provided
        pl = PinList.load(args.pin_list) if args.pin_list else None

        count = PadReview.generate(
            args.input, args.generate_pad_review,
            pad_layer=pad_layer,
            text_layer=text_layer_info,
            pin_list=pl,
        )
        print(f"Generated pad review GDS: {args.generate_pad_review}")
        print(f"  {count} shapes on pad layer")
        print(f"  Edit in KLayout: klayout -e {args.generate_pad_review}")
        return 0

    # Footprint from pad review GDS
    if args.from_pad_review:
        if not args.lyp_file:
            parser.error("--lyp-file is required with --from-pad-review")
        if not args.layer:
            parser.error("--layer is required with --from-pad-review")
        if not args.pin_list:
            parser.error("--pin-list is required with --from-pad-review")

        pin_list = PinList.load(args.pin_list)
        print(f"Loaded pin list: {len(pin_list)} pins")

        if not args.output:
            output_dir = Path("generated_kicad_footprint_files")
            output_dir.mkdir(exist_ok=True)
            stem = Path(args.from_pad_review).stem
            args.output = str(output_dir / f"{stem}.kicad_mod")

        lyp_parser = LYPParser(args.lyp_file)
        converter = GDSToKiCad(lyp_parser, args.layer, dbu=args.dbu)
        success = converter.convert_from_pad_review(
            args.from_pad_review, pin_list, args.output
        )
        return 0 if success else 1

    # Validate input arguments for conversion
    if not args.input:
        parser.error("Input GDS file required (or use --generate-test-gds or --list-layers)")

    if not args.lyp_file:
        parser.error("--lyp-file is required for conversion")

    if not args.layer:
        parser.error("--layer is required for conversion")

    if not args.output:
        # Auto-generate output filename in generated_kicad_footprint_files/
        input_path = Path(args.input)
        output_dir = Path("generated_kicad_footprint_files")
        output_dir.mkdir(exist_ok=True)
        args.output = str(output_dir / input_path.with_suffix('.kicad_mod').name)

    # Load LYP file
    lyp_parser = LYPParser(args.lyp_file)
    print(f"Loaded {lyp_parser}")

    # Convert
    text_layer = getattr(args, 'text_layer', None)
    auto_detect = getattr(args, 'auto_text', False)
    converter = GDSToKiCad(lyp_parser, args.layer,
                            text_layer_name=text_layer,
                            auto_detect_text=auto_detect,
                            dbu=args.dbu)
    success = converter.convert(args.input, args.output)

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
