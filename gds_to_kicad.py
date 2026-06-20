#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
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
from sexpr import sanitize_sexpr_token

# Default LYP used when --lyp-file is omitted: the bundled generic pads-only
# vocabulary (pad.drawing 205/0, pad.text 205/25, outline.drawing 206/0). Lets
# the converter process a chiplet GDS that has no real PDK .lyp (commercial /
# closed-node "black-box" chiplets). Source of truth: adk/config/chiplet_pads.json.
DEFAULT_GENERIC_LYP = Path(__file__).resolve().parent / "pdks" / "generic.lyp"


class GDSToKiCad:
    """Main converter class"""

    def __init__(self, lyp_parser: LYPParser, layer_name: Optional[str] = None,
                 text_layer_name: Optional[str] = None,
                 auto_detect_text: bool = False,
                 dbu: Optional[float] = None,
                 pad_layer: Optional[Tuple[int, int]] = None,
                 text_layer: Optional[Tuple[int, int]] = None):
        self.lyp_parser = lyp_parser
        self.text_layer_name = text_layer_name
        self.auto_detect_text = auto_detect_text
        self._dbu_override = dbu
        # Optional raw (layer, datatype) text override (bypasses name lookup).
        self._text_layer_override = text_layer

        if pad_layer is not None:
            # Raw layer-number path: use the given (layer, datatype) directly,
            # bypassing the LYP name lookup. For chiplet GDS with no named entry.
            self.pad_layer = pad_layer
            self.layer_name = layer_name or f"{pad_layer[0]}/{pad_layer[1]}"
        else:
            # Name path: resolve the layer name against the LYP.
            self.layer_name = layer_name
            self.pad_layer = lyp_parser.get_layer(layer_name) if layer_name else None
            if not self.pad_layer:
                print(f"Error: Layer '{layer_name}' not found in LYP file", file=sys.stderr)
                print(f"Available layers:", file=sys.stderr)
                for name in lyp_parser.get_layer_names()[:10]:
                    layer, dt = lyp_parser.get_layer(name)
                    print(f"  {name} ({layer}/{dt})", file=sys.stderr)
                if len(lyp_parser.get_layer_names()) > 10:
                    print(f"  ... and {len(lyp_parser.get_layer_names()) - 10} more", file=sys.stderr)
                sys.exit(1)

        print(f"Using layer: {self.layer_name} {self.pad_layer}")
        if text_layer_name:
            print(f"Using text layer: {text_layer_name}")
        elif self._text_layer_override:
            print(f"Using text layer: {self._text_layer_override[0]}/{self._text_layer_override[1]}")

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

    def convert(self, gds_path: str, output_path: str, flip_chip: bool = False):
        """Convert GDS file to KiCad footprint.

        Args:
            flip_chip: If True, mirror pad X coordinates for flip-chip (face-down)
                       orientation. The footprint represents the die as seen from
                       the interposer looking up.
        """
        orientation = "flip-chip (mirror-X)" if flip_chip else "face-up"
        print(f"\nConverting {gds_path} -> {output_path} [{orientation}]")

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
        if self.text_layer_name or self.auto_detect_text or self._text_layer_override:
            try:
                from pin_extractor import PinExtractor
                extractor = PinExtractor(self.lyp_parser)

                # Use PinExtractor on the already-loaded/flattened layout
                pads_raw = extractor.extract_pads(layout, top_cell, self.pad_layer)

                if self._text_layer_override is not None:
                    # Raw text layer (layer, datatype) override
                    tl = self._text_layer_override
                    text_layers = [(f"{tl[0]}/{tl[1]}", tl)]
                elif self.text_layer_name:
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
                                        pad_names=pad_names, dbu_to_mm=dbu_to_mm,
                                        flip_chip=flip_chip)

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
                                    dbu_to_mm: Optional[float] = None,
                                    gds_property_path: Optional[str] = None,
                                    flip_chip: bool = False):
        """Generate KiCad footprint file with numbered pads.

        pad_dicts: list of dicts with keys:
            bbox (db.Box), is_polygon (bool), polygon_points (list or None)
        gds_property_path: if set, used for GDS_FILE property instead of gds_path
        flip_chip: if True, mirror X coordinates (die seen from interposer side)
        """
        print(f"Generating KiCad footprint: {output_path}")

        # The cell name comes from the GDS (untrusted); sanitize it like pad
        # names so a stray quote/paren cannot corrupt the S-expression.
        name = sanitize_sexpr_token(name)

        if pad_names is None:
            pad_names = {}

        if dbu_to_mm is None:
            dbu_to_mm = 1e-6  # fallback: 1 DBU = 1nm
        DBU_TO_MM = dbu_to_mm

        # Flip-chip: mirror factor for X axis (-1 for flip, +1 for normal)
        mx = -1 if flip_chip else 1

        # Source file paths for traceability properties (absolute)
        gds_property_source = gds_property_path if gds_property_path else gds_path
        gds_filename = str(Path(gds_property_source).resolve())
        lyp_filename = str(Path(self.lyp_parser.lyp_path).resolve())
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
            f.write(f'  (property "GDS_LAYER" "{self.layer_name} ({layer_num}/{layer_dt})")\n')
            orientation = "flip_chip" if flip_chip else "face_up"
            f.write(f'  (property "ORIENTATION" "{orientation}")\n\n')

            # Reference and value text
            f.write('  (fp_text reference "REF**" (at 0 0) (layer "F.SilkS")\n')
            f.write('    (effects (font (size 0.1 0.1) (thickness 0.015)))\n')
            f.write('  )\n')
            f.write(f'  (fp_text value "{name}" (at 0 -2) (layer "F.Fab")\n')
            f.write('    (effects (font (size 0.1 0.1) (thickness 0.015)))\n')
            f.write('  )\n\n')

            # Generate pads (named if text layer provided, otherwise sequential)
            for idx, pd in enumerate(pad_dicts):
                # Use text label name if available, otherwise sequential number.
                # Names come from GDS text labels (untrusted) -> sanitize.
                pad_name = sanitize_sexpr_token(pad_names.get(idx, str(idx + 1)))
                pad = pd["bbox"]
                is_polygon = pd.get("is_polygon", False)
                polygon_points = pd.get("polygon_points")

                # Calculate pad center and size in mm
                # Y negated: GDS (Y-up) -> KiCad (Y-down)
                # X negated when flip_chip: die face-down mirror
                center_x = mx * ((pad.left + pad.right) / 2) * DBU_TO_MM
                center_y = -((pad.bottom + pad.top) / 2) * DBU_TO_MM
                width = (pad.right - pad.left) * DBU_TO_MM
                height = (pad.top - pad.bottom) * DBU_TO_MM

                if is_polygon and polygon_points:
                    # Custom polygon pad
                    f.write(f'  (pad "{pad_name}" smd custom (at {center_x:.6f} {center_y:.6f})\n')
                    f.write(f'    (size {width:.6f} {height:.6f})\n')
                    f.write('    (layers "F.Cu" "F.Paste" "F.Mask")\n')
                    # Polygon vertices relative to pad center
                    cx_dbu = (pad.left + pad.right) / 2.0
                    cy_dbu = (pad.bottom + pad.top) / 2.0
                    f.write('    (primitives\n')
                    f.write('      (gr_poly\n')
                    f.write('        (pts\n')
                    for px, py in polygon_points:
                        rx = mx * ((px - cx_dbu) * DBU_TO_MM)
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

            # Courtyard from pad bounding box
            if pad_dicts:
                all_left = [pd["bbox"].left for pd in pad_dicts]
                all_right = [pd["bbox"].right for pd in pad_dicts]
                if flip_chip:
                    # After X-mirror, min/max swap
                    min_x = -max(all_right) * DBU_TO_MM
                    max_x = -min(all_left) * DBU_TO_MM
                else:
                    min_x = min(all_left) * DBU_TO_MM
                    max_x = max(all_right) * DBU_TO_MM
                min_y = -max(pd["bbox"].top for pd in pad_dicts) * DBU_TO_MM
                max_y = -min(pd["bbox"].bottom for pd in pad_dicts) * DBU_TO_MM
                f.write(f'\n  (fp_rect (start {min_x:.6f} {min_y:.6f}) (end {max_x:.6f} {max_y:.6f})\n')
                f.write('    (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd")\n')
                f.write('  )\n')

            # Footer
            f.write(')\n')

        print(f"Generated {len(pad_dicts)} pads")

    def convert_from_pad_review(self, edited_gds: str, pin_list: PinList,
                                output_path: str,
                                gds_property_path: Optional[str] = None,
                                flip_chip: bool = False):
        """Generate footprint from a user-edited pad review GDS.

        Uses pin_list for pad naming instead of text extraction from the
        original GDS. The user has already cleaned the pad review GDS
        to contain only real bond pads.

        Args:
            edited_gds: Path to user-edited pad review GDS
            pin_list: PinList with authoritative pad names
            output_path: Output .kicad_mod path
            gds_property_path: If set, used for GDS_FILE property instead of edited_gds
            flip_chip: If True, mirror X for flip-chip orientation
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
            gds_property_path=gds_property_path,
            flip_chip=flip_chip,
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


def resolve_footprint_output(output: Optional[str],
                             design_dir: Optional[str],
                             stem: str) -> str:
    """Resolve the .kicad_mod output path per the per-design file-layout convention.

    Priority: explicit ``output`` > ``design_dir`` (KiCad ``<design>.pretty/`` library)
    > the legacy ``generated_kicad_footprint_files/`` fallback. Creates the target
    directory as a side effect.
    """
    if output:
        return output
    if design_dir:
        ddir = Path(design_dir)
        pretty = ddir / ("%s.pretty" % ddir.name)
        pretty.mkdir(parents=True, exist_ok=True)
        return str(pretty / ("%s.kicad_mod" % stem))
    default_dir = Path("generated_kicad_footprint_files")
    default_dir.mkdir(exist_ok=True)
    return str(default_dir / ("%s.kicad_mod" % stem))


def parse_layer_spec(spec: str) -> Tuple[int, int]:
    """Parse a raw GDS layer spec 'N/D' (or bare 'N' -> (N, 0)) into a tuple."""
    parts = str(spec).split('/')
    try:
        layer = int(parts[0])
        datatype = int(parts[1]) if len(parts) > 1 and parts[1] != '' else 0
    except (ValueError, IndexError):
        raise argparse.ArgumentTypeError(
            f"Invalid layer spec '{spec}'; expected 'N/D' or 'N'")
    return (layer, datatype)


def resolve_pad_layer(args, lyp_parser, gds_path):
    """Resolve the pad layer for a conversion.

    Precedence:
      1. --pad-layer-number N/D  (raw number, bypasses the LYP)
      2. --layer NAME            (resolved against the LYP)
      3. densest-layer auto-detect via PinExtractor.scan_gds_layers

    Returns (pad_layer, layer_name, text_layer): pad_layer is a (layer,
    datatype) tuple or None; layer_name is a display/name string or None;
    text_layer is a raw (layer, datatype) tuple suggested for pad names, or
    None (only populated on the auto-detect path). When pad_layer is None but
    layer_name is set, the caller lets GDSToKiCad raise the detailed
    'layer not found' error. text_layer is ignored by the explicit paths.
    """
    # Accept the pad-name flag under either spelling: --layer (footprint
    # converter) or --pad-layer (symbol converter), so both reuse this resolver.
    name = getattr(args, 'layer', None) or getattr(args, 'pad_layer', None)

    if getattr(args, 'pad_layer_number', None):
        pad = parse_layer_spec(args.pad_layer_number)
        return pad, (name or f"{pad[0]}/{pad[1]}"), None

    if name:
        return lyp_parser.get_layer(name), name, None

    # Auto-detect: pick the densest pad layer plus a text layer for names.
    from pin_extractor import PinExtractor
    scan = PinExtractor.scan_gds_layers(gds_path, lyp_parser)
    suggested = scan.get('suggested_pad_layer')
    if not suggested:
        return None, None, None
    pad = lyp_parser.get_layer(suggested) if lyp_parser else None
    if pad is None:
        # suggested is a bare "N/D" string (layer not named in the LYP)
        pad = parse_layer_spec(suggested)

    # Text layer: prefer a LYP-named suggestion; else the densest text
    # candidate that is not the pad layer itself (commercial GDS with
    # arbitrary, unnamed layers still yields pad names this way).
    text_layer = None
    suggested_text = scan.get('suggested_text_layers') or []
    if suggested_text and lyp_parser:
        text_layer = lyp_parser.get_layer(suggested_text[0])
    if text_layer is None:
        for c in scan.get('text_candidates', []):
            cand = (c['layer_num'], c['datatype'])
            if cand != pad:
                text_layer = cand
                break

    msg = f"Auto-detected pad layer: {suggested} {pad}"
    if text_layer:
        msg += f", text layer {text_layer[0]}/{text_layer[1]}"
    print(msg)
    return pad, suggested, text_layer


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
    parser.add_argument('--design-dir', metavar='DIR',
                       help='Per-design directory; footprints are written to '
                            '<DIR>/<design>.pretty/ per the project file-layout '
                            'convention. Ignored when -o/--output is given.')
    parser.add_argument('--lyp-file', default=str(DEFAULT_GENERIC_LYP),
                       help='KLayout .lyp file with layer definitions (default: '
                            'bundled generic pads-only lyp, for a chiplet GDS with no PDK lyp)')
    parser.add_argument('--layer', help='Layer name to extract (e.g., TopMetal2.drawing)')
    parser.add_argument('--pad-layer-number', metavar='N/D',
                       help='Raw GDS pad layer as N/D (e.g., 134/0). Bypasses --layer '
                            'name lookup; use for a chiplet GDS with no named LYP entry. '
                            'If neither --layer nor this is given, the densest pad layer '
                            'is auto-detected.')
    parser.add_argument('--text-layer',
                       help='Text layer name for pin names (e.g., TopMetal2.text)')
    parser.add_argument('--text-layer-number', metavar='N/D',
                       help='Raw GDS text layer as N/D (e.g., 134/25) for pad names.')
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

    # Flip-chip orientation
    parser.add_argument('--flip-chip', action='store_true',
                       help='Mirror X coordinates for flip-chip (face-down) die orientation. '
                            'Generates footprint as seen from interposer side.')

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

        lyp_parser = LYPParser(args.lyp_file)
        pad_layer, layer_name, auto_text_layer = resolve_pad_layer(args, lyp_parser, args.input)
        if pad_layer is None:
            parser.error(f"Could not resolve a pad layer (layer '{layer_name}' not in "
                         f"LYP, or no pad geometry to auto-detect)")

        # Resolve text layer
        text_layer_info = None
        if args.text_layer_number:
            text_layer_info = parse_layer_spec(args.text_layer_number)
        elif args.text_layer:
            text_layer_info = lyp_parser.get_layer(args.text_layer)
        else:
            text_layer_info = auto_text_layer

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
        if not args.pin_list:
            parser.error("--pin-list is required with --from-pad-review")

        pin_list = PinList.load(args.pin_list)
        print(f"Loaded pin list: {len(pin_list)} pins")

        args.output = resolve_footprint_output(
            args.output, args.design_dir, Path(args.from_pad_review).stem)

        lyp_parser = LYPParser(args.lyp_file)
        pad_layer, layer_name, _ = resolve_pad_layer(args, lyp_parser, args.from_pad_review)
        if pad_layer is None:
            parser.error(f"Could not resolve a pad layer (layer '{layer_name}' not in "
                         f"LYP, or no pad geometry to auto-detect)")
        converter = GDSToKiCad(lyp_parser, layer_name, dbu=args.dbu, pad_layer=pad_layer)
        success = converter.convert_from_pad_review(
            args.from_pad_review, pin_list, args.output,
            flip_chip=getattr(args, 'flip_chip', False),
        )
        return 0 if success else 1

    # Validate input arguments for conversion
    if not args.input:
        parser.error("Input GDS file required (or use --generate-test-gds or --list-layers)")

    args.output = resolve_footprint_output(
        args.output, args.design_dir, Path(args.input).stem)

    # Load LYP file (defaults to the bundled generic pads-only lyp)
    lyp_parser = LYPParser(args.lyp_file)
    print(f"Loaded {lyp_parser}")

    # Resolve the pad layer: --pad-layer-number > --layer > densest auto-detect
    pad_layer, layer_name, auto_text_layer = resolve_pad_layer(args, lyp_parser, args.input)
    if pad_layer is None and layer_name is None:
        parser.error("Could not determine a pad layer. Pass --layer NAME, "
                     "--pad-layer-number N/D, or ensure the GDS has pad geometry "
                     "to auto-detect.")

    # Text layer precedence: --text-layer-number > --text-layer (name) >
    # auto-detected raw text layer (pad names are the point of black-box mode).
    text_layer_tuple = None
    if args.text_layer_number:
        text_layer_tuple = parse_layer_spec(args.text_layer_number)
    elif args.text_layer is None:
        text_layer_tuple = auto_text_layer

    converter = GDSToKiCad(lyp_parser, layer_name,
                            text_layer_name=args.text_layer,
                            auto_detect_text=args.auto_text,
                            dbu=args.dbu,
                            pad_layer=pad_layer,
                            text_layer=text_layer_tuple)
    success = converter.convert(args.input, args.output,
                                flip_chip=getattr(args, 'flip_chip', False))

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
