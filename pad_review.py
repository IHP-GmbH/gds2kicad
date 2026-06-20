# SPDX-License-Identifier: GPL-3.0-or-later
"""
Pad Review GDS Generator

Creates a stripped-down GDS file containing only pad layer shapes and text
labels for human review. The user edits this in KLayout to remove non-pad
structures (routing, fills, guard rings), then the reviewed GDS is read
back for footprint generation.
"""

import subprocess
import shutil
import sys
from typing import List, Optional, Tuple

try:
    import klayout.db as db
except ImportError:
    print("Error: KLayout Python module not found.", file=sys.stderr)
    sys.exit(1)

from pin_list import PinList, PinEntry


class PadReview:
    """Generates and reads back pad review GDS files for human curation."""

    @staticmethod
    def generate(source_gds: str, output_gds: str,
                 pad_layer: Tuple[int, int],
                 text_layer: Optional[Tuple[int, int]] = None,
                 text_layers: Optional[List[Tuple[int, int]]] = None,
                 pin_list: Optional[PinList] = None,
                 flatten: bool = True):
        """Create a pad review GDS with only pad layer shapes and text labels.

        Copies all shapes from the pad layer in the source GDS into a new GDS.
        Text labels are either:
        - Copied from text_layer/text_layers (if provided and no pin_list)
        - Generated from pin_list at pad centers (if pin_list provided)

        Args:
            source_gds: Path to original GDS file
            output_gds: Path for the pad review GDS output
            pad_layer: (layer_num, datatype) for pad shapes
            text_layer: (layer_num, datatype) for a single text layer (optional)
            text_layers: list of (layer_num, datatype) for multiple text layers
            pin_list: PinList to use for text labels at pad centers
            flatten: Whether to flatten cell hierarchy before extraction
        """
        # Load source
        src_layout = db.Layout()
        src_layout.read(source_gds)
        src_top = src_layout.top_cell()
        if not src_top:
            raise ValueError(f"No top cell found in {source_gds}")
        if flatten:
            src_top.flatten(1)

        # Create output layout
        out_layout = db.Layout()
        out_cell = out_layout.create_cell(src_top.name)

        # Copy pad layer shapes
        out_pad_layer = out_layout.layer(*pad_layer)
        src_pad_idx = src_layout.layer(*pad_layer)
        src_shapes = src_top.shapes(src_pad_idx)

        pad_count = 0
        for shape in src_shapes.each():
            if shape.is_box():
                out_cell.shapes(out_pad_layer).insert(shape.box)
                pad_count += 1
            elif shape.is_polygon():
                out_cell.shapes(out_pad_layer).insert(shape.polygon)
                pad_count += 1

        # Merge text_layer into text_layers list
        all_text_layers = list(text_layers or [])
        if text_layer is not None and text_layer not in all_text_layers:
            all_text_layers.append(text_layer)

        # Text labels
        if pin_list is not None:
            # Generate text from pin list at pad centers
            text_target = all_text_layers[0] if all_text_layers else pad_layer
            out_text_layer = out_layout.layer(*text_target)

            for pin in pin_list.pins:
                if pin.name.strip():
                    cx = int(pin.center_x_dbu)
                    cy = int(pin.center_y_dbu)
                    out_cell.shapes(out_text_layer).insert(
                        db.Text(pin.name, db.Trans(db.Point(cx, cy)))
                    )
        elif all_text_layers:
            # Copy existing text shapes from all text layers
            for tl in all_text_layers:
                out_tl = out_layout.layer(*tl)
                src_tl_idx = src_layout.layer(*tl)
                for shape in src_top.shapes(src_tl_idx).each():
                    if shape.is_text():
                        out_cell.shapes(out_tl).insert(shape.text)

        out_layout.write(output_gds)
        return pad_count

    @staticmethod
    def read_edited_pads(edited_gds: str, pad_layer: Tuple[int, int],
                         pin_list: Optional[PinList] = None,
                         flatten: bool = True) -> List[dict]:
        """Read pads from a user-edited pad review GDS.

        Matches remaining pad shapes to pin_list entries by spatial proximity
        (nearest-neighbor). Returns pad info dicts with names from pin_list.

        Args:
            edited_gds: Path to user-edited GDS file
            pad_layer: (layer_num, datatype) for pad shapes
            pin_list: PinList for naming pads by proximity matching
            flatten: Whether to flatten cell hierarchy

        Returns:
            List of dicts with keys: index, name, center_x, center_y,
            width, height, bbox
        """
        layout = db.Layout()
        layout.read(edited_gds)
        top_cell = layout.top_cell()
        if not top_cell:
            raise ValueError(f"No top cell found in {edited_gds}")
        if flatten:
            top_cell.flatten(1)

        # Extract pad shapes
        layer_idx = layout.layer(*pad_layer)
        shapes = top_cell.shapes(layer_idx)

        pads = []
        idx = 0
        for shape in shapes.each():
            box = None
            is_polygon = False
            polygon_points = None
            if shape.is_box():
                box = shape.box
            elif shape.is_polygon():
                poly = shape.polygon
                box = poly.bbox()
                is_polygon = True
                polygon_points = [(int(p.x), int(p.y))
                                  for p in poly.each_point_hull()]

            if box is not None:
                pads.append({
                    "index": idx,
                    "name": None,
                    "center_x": (box.left + box.right) / 2.0,
                    "center_y": (box.bottom + box.top) / 2.0,
                    "width": box.right - box.left,
                    "height": box.top - box.bottom,
                    "bbox": (box.left, box.bottom, box.right, box.top),
                    "is_polygon": is_polygon,
                    "polygon_points": polygon_points,
                })
                idx += 1

        # Match pads to pin_list by spatial proximity
        if pin_list is not None and pads:
            _match_pads_to_pin_list(pads, pin_list)

        return pads

    @staticmethod
    def open_in_klayout(gds_path: str, lyp_path: Optional[str] = None):
        """Launch KLayout in edit mode to review the pad review GDS.

        Args:
            gds_path: Path to GDS file to open
            lyp_path: Optional LYP file for layer coloring

        Returns:
            subprocess.Popen object (non-blocking)
        """
        klayout_bin = shutil.which("klayout")
        if not klayout_bin:
            raise FileNotFoundError(
                "klayout not found in PATH. Install KLayout or add it to PATH."
            )

        cmd = [klayout_bin, "-e"]
        if lyp_path:
            cmd.extend(["-l", lyp_path])
        cmd.append(gds_path)

        return subprocess.Popen(cmd)


def _match_pads_to_pin_list(pads: List[dict], pin_list: PinList):
    """Assign names to pads by nearest-neighbor matching to pin_list entries.

    Each pad is matched to the closest pin_list entry. If multiple pads
    compete for the same pin, the closest wins. Modifies pads in-place.
    """
    # Build list of pin centers from pin_list
    pin_centers = [
        (pin.center_x_dbu, pin.center_y_dbu, pin.name, i)
        for i, pin in enumerate(pin_list.pins)
    ]

    if not pin_centers:
        return

    # For each pad, find nearest pin_list entry
    # Track: pin_idx -> (distance, pad_idx)
    pin_to_pad = {}

    for pad in pads:
        px, py = pad["center_x"], pad["center_y"]
        min_dist = float('inf')
        best_pin_idx = -1

        for pin_x, pin_y, pin_name, pin_idx in pin_centers:
            dx = px - pin_x
            dy = py - pin_y
            dist = (dx * dx + dy * dy) ** 0.5
            if dist < min_dist:
                min_dist = dist
                best_pin_idx = pin_idx

        if best_pin_idx < 0:
            continue

        if best_pin_idx in pin_to_pad:
            existing_dist, existing_pad_idx = pin_to_pad[best_pin_idx]
            if min_dist < existing_dist:
                # This pad is closer, steal the match
                pads[existing_pad_idx]["name"] = None
                pads[existing_pad_idx]["pin_list_index"] = None
                pin_to_pad[best_pin_idx] = (min_dist, pad["index"])
                pad["name"] = pin_list.pins[best_pin_idx].name
                pad["pin_list_index"] = best_pin_idx
            # else: existing pad keeps the match
        else:
            pin_to_pad[best_pin_idx] = (min_dist, pad["index"])
            pad["name"] = pin_list.pins[best_pin_idx].name
            pad["pin_list_index"] = best_pin_idx

    # Reconciliation pass: a pad displaced by a closer competitor above was
    # reset to name=None and never re-evaluated. It may still legitimately own
    # an as-yet-unmatched pin. Build all (distance, pad-position, pin) pairs
    # between still-unnamed pads and still-free pins and assign greedily by
    # distance, so a contended cluster does not leave a pad unnamed while a
    # free pin exists. Uses list position (not pad["index"]) consistently.
    used_pins = set(pin_to_pad.keys())
    candidates = []
    for pad_pos, pad in enumerate(pads):
        if pad.get("name") is not None:
            continue
        for pin_x, pin_y, pin_name, pin_idx in pin_centers:
            if pin_idx in used_pins:
                continue
            dx = pad["center_x"] - pin_x
            dy = pad["center_y"] - pin_y
            candidates.append(((dx * dx + dy * dy) ** 0.5, pad_pos, pin_idx))
    candidates.sort(key=lambda c: c[0])
    assigned_pads = set()
    for dist, pad_pos, pin_idx in candidates:
        if pad_pos in assigned_pads or pin_idx in used_pins:
            continue
        pad = pads[pad_pos]
        pad["name"] = pin_list.pins[pin_idx].name
        pad["pin_list_index"] = pin_idx
        pin_to_pad[pin_idx] = (dist, pad["index"])
        used_pins.add(pin_idx)
        assigned_pads.add(pad_pos)

    # Report unmatched
    matched_pins = set(pin_to_pad.keys())
    unmatched_pins = [
        pin_list.pins[i].name
        for i in range(len(pin_list.pins))
        if i not in matched_pins
    ]
    unnamed_pads = [p["index"] for p in pads if p["name"] is None]

    if unmatched_pins:
        print(f"Warning: {len(unmatched_pins)} pin list entries not matched "
              f"to pads: {unmatched_pins[:10]}", file=sys.stderr)
    if unnamed_pads:
        print(f"Warning: {len(unnamed_pads)} pads without names "
              f"(indices: {unnamed_pads[:10]})", file=sys.stderr)
