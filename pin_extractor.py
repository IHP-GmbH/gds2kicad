"""
Pin Extraction Engine

Extracts pad geometries and text labels from GDSII files, then associates
text labels with pads using nearest-neighbor matching. Reusable by both
symbol and footprint converter projects.
"""

import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

try:
    import klayout.db as db
except ImportError:
    print("Error: KLayout Python module not found.", file=sys.stderr)
    print("Set PYTHONPATH to include KLayout's python directory.", file=sys.stderr)
    sys.exit(1)

from lyp_parser import LYPParser


@dataclass
class PadInfo:
    """Information about a single pad extracted from GDS"""
    index: int
    bbox: Tuple[int, int, int, int]  # (left, bottom, right, top) in DBU
    center_x: float  # in DBU
    center_y: float  # in DBU
    width: float     # in DBU
    height: float    # in DBU
    name: Optional[str] = None
    is_polygon: bool = False
    polygon_points: Optional[List[Tuple[int, int]]] = None

    @classmethod
    def from_box(cls, index: int, box) -> 'PadInfo':
        """Create PadInfo from a klayout Box object"""
        return cls(
            index=index,
            bbox=(box.left, box.bottom, box.right, box.top),
            center_x=(box.left + box.right) / 2.0,
            center_y=(box.bottom + box.top) / 2.0,
            width=box.right - box.left,
            height=box.top - box.bottom,
        )

    @classmethod
    def from_polygon(cls, index: int, polygon) -> 'PadInfo':
        """Create PadInfo from a klayout Polygon object, preserving vertices."""
        bbox = polygon.bbox()
        points = [(int(p.x), int(p.y)) for p in polygon.each_point_hull()]
        return cls(
            index=index,
            bbox=(bbox.left, bbox.bottom, bbox.right, bbox.top),
            center_x=(bbox.left + bbox.right) / 2.0,
            center_y=(bbox.bottom + bbox.top) / 2.0,
            width=bbox.right - bbox.left,
            height=bbox.top - bbox.bottom,
            is_polygon=True,
            polygon_points=points,
        )


@dataclass
class TextLabel:
    """A text label extracted from GDS"""
    string: str
    x: float  # position in DBU
    y: float  # position in DBU
    layer_name: str
    layer_info: Tuple[int, int]  # (layer_num, datatype)


class PinExtractor:
    """Extracts pads and text labels from GDSII files and associates them."""

    def __init__(self, lyp_parser: LYPParser):
        self.lyp_parser = lyp_parser

    def extract_pads(self, layout: db.Layout, cell: db.Cell,
                     pad_layer: Tuple[int, int]) -> List[PadInfo]:
        """Extract pad geometries from the specified layer.

        Args:
            layout: KLayout Layout object
            cell: KLayout Cell to extract from
            pad_layer: (layer_num, datatype) tuple

        Returns:
            List of PadInfo objects, one per pad
        """
        pads = []
        layer_index = layout.layer(*pad_layer)
        shapes = cell.shapes(layer_index)

        idx = 0
        for shape in shapes.each():
            if shape.is_box():
                pads.append(PadInfo.from_box(idx, shape.box))
                idx += 1
            elif shape.is_polygon():
                pads.append(PadInfo.from_polygon(idx, shape.polygon))
                idx += 1

        return pads

    def extract_texts(self, layout: db.Layout, cell: db.Cell,
                      text_layers: List[Tuple[str, Tuple[int, int]]]) -> List[TextLabel]:
        """Extract text labels from specified layers.

        Args:
            layout: KLayout Layout object
            cell: KLayout Cell to extract from
            text_layers: List of (layer_name, (layer_num, datatype)) tuples

        Returns:
            List of TextLabel objects
        """
        texts = []

        for layer_name, layer_info in text_layers:
            layer_index = layout.layer(*layer_info)
            shapes = cell.shapes(layer_index)

            for shape in shapes.each():
                if shape.is_text():
                    text = shape.text
                    pos = text.trans.disp
                    texts.append(TextLabel(
                        string=text.string,
                        x=pos.x,
                        y=pos.y,
                        layer_name=layer_name,
                        layer_info=layer_info,
                    ))

        return texts

    def associate_texts_with_pads(self, pads: List[PadInfo],
                                   texts: List[TextLabel],
                                   max_distance: Optional[float] = None) -> List[PadInfo]:
        """Associate text labels with nearest pads using Euclidean distance.

        Each text is matched to its closest pad. If two texts compete for the
        same pad, the closest one wins (with a warning). Pads without a text
        match retain name=None.

        Args:
            pads: List of PadInfo objects
            texts: List of TextLabel objects
            max_distance: Optional maximum distance in DBU. If set, texts
                         farther than this from any pad are ignored.

        Returns:
            The same list of PadInfo objects, with .name fields filled in
            where text associations were found.
        """
        if not pads or not texts:
            return pads

        # Track best text match per pad: pad_index -> (distance, text_string)
        pad_matches: dict = {}

        for text in texts:
            min_dist = float('inf')
            closest_pad_idx = -1

            for pad in pads:
                dx = text.x - pad.center_x
                dy = text.y - pad.center_y
                dist = (dx * dx + dy * dy) ** 0.5

                if dist < min_dist:
                    min_dist = dist
                    closest_pad_idx = pad.index

            if closest_pad_idx < 0:
                continue

            if max_distance is not None and min_dist > max_distance:
                continue

            # Check if this pad already has a closer match
            if closest_pad_idx in pad_matches:
                existing_dist, existing_text = pad_matches[closest_pad_idx]
                if min_dist < existing_dist:
                    print(f"Warning: Pad {closest_pad_idx} text conflict: "
                          f"'{existing_text}' (d={existing_dist:.0f}) replaced by "
                          f"'{text.string}' (d={min_dist:.0f})", file=sys.stderr)
                    pad_matches[closest_pad_idx] = (min_dist, text.string)
                else:
                    print(f"Warning: Pad {closest_pad_idx} text conflict: "
                          f"'{text.string}' (d={min_dist:.0f}) ignored, "
                          f"keeping '{existing_text}' (d={existing_dist:.0f})",
                          file=sys.stderr)
            else:
                pad_matches[closest_pad_idx] = (min_dist, text.string)

        # Apply matches
        for pad in pads:
            if pad.index in pad_matches:
                pad.name = pad_matches[pad.index][1]

        return pads

    def auto_detect_text_layers(self, layout: db.Layout, cell: db.Cell,
                                 pad_layer_name: str) -> List[Tuple[str, Tuple[int, int]]]:
        """Auto-detect text layers that contain text shapes related to a pad layer.

        Uses LYP naming conventions to find candidate text layers, then
        verifies they actually contain text shapes in the given cell.

        Args:
            layout: KLayout Layout object
            cell: KLayout Cell to check
            pad_layer_name: Name of the pad drawing layer (e.g. "TopMetal2.drawing")

        Returns:
            List of (layer_name, (layer_num, datatype)) tuples for layers
            that actually contain text.
        """
        candidates = self.lyp_parser.find_text_layers_for(pad_layer_name)
        result = []

        for name in candidates:
            layer_info = self.lyp_parser.get_layer(name)
            if layer_info is None:
                continue

            layer_index = layout.layer(*layer_info)
            shapes = cell.shapes(layer_index)

            has_text = False
            for shape in shapes.each():
                if shape.is_text():
                    has_text = True
                    break

            if has_text:
                result.append((name, layer_info))

        return result

    @staticmethod
    def scan_gds_layers(gds_path: str, lyp_parser: Optional['LYPParser'] = None,
                        flatten: bool = True) -> dict:
        """Scan a GDS file and report shape counts per layer.

        Returns a dict with:
          - 'pad_candidates': list of dicts sorted by box+polygon count desc
          - 'text_candidates': list of dicts sorted by text count desc
          - 'suggested_pad_layer': name of best pad layer candidate (or None)
          - 'suggested_text_layers': list of text layer names for the suggested pad layer

        Each candidate dict has keys:
          layer_num, datatype, name (from LYP or None), boxes, polygons, texts, total_shapes
        """
        layout = db.Layout()
        layout.read(gds_path)

        top_cell = layout.top_cell()
        if not top_cell:
            raise ValueError(f"No top cell found in {gds_path}")

        if flatten:
            top_cell.flatten(1)

        # Build reverse lookup from (layer_num, datatype) -> name if LYP provided
        layer_name_map = {}
        if lyp_parser:
            for name, info in lyp_parser.layers.items():
                layer_name_map[info] = name

        # Scan all layers present in the layout
        layer_stats = {}
        for layer_index in layout.layer_indices():
            info = layout.get_info(layer_index)
            layer_num = info.layer
            datatype = info.datatype
            key = (layer_num, datatype)

            shapes = top_cell.shapes(layer_index)
            boxes = 0
            polygons = 0
            texts = 0

            for shape in shapes.each():
                if shape.is_box():
                    boxes += 1
                elif shape.is_polygon():
                    polygons += 1
                elif shape.is_text():
                    texts += 1

            if boxes + polygons + texts == 0:
                continue

            layer_stats[key] = {
                'layer_num': layer_num,
                'datatype': datatype,
                'name': layer_name_map.get(key),
                'boxes': boxes,
                'polygons': polygons,
                'texts': texts,
                'total_shapes': boxes + polygons + texts,
            }

        # Separate into pad candidates (have box/polygon) and text candidates (have text)
        pad_candidates = sorted(
            [s for s in layer_stats.values() if s['boxes'] + s['polygons'] > 0],
            key=lambda s: s['boxes'] + s['polygons'],
            reverse=True,
        )
        text_candidates = sorted(
            [s for s in layer_stats.values() if s['texts'] > 0],
            key=lambda s: s['texts'],
            reverse=True,
        )

        # Suggest best pad layer using heuristics:
        # 1. Prefer .drawing layers that have a matching text layer with actual content
        # 2. Among those, prefer higher metal layers (bond pads are on top metal)
        # 3. Fallback: layer with most boxes/polygons
        suggested_pad = None
        suggested_text = []

        if lyp_parser:
            # Score each .drawing pad candidate by whether it has matching text
            scored = []
            for c in pad_candidates:
                if not c['name'] or not c['name'].endswith('.drawing'):
                    continue
                text_matches = lyp_parser.find_text_layers_for(c['name'])
                text_count = 0
                matching_text_layers = []
                for tname in text_matches:
                    tinfo = lyp_parser.get_layer(tname)
                    if tinfo and tinfo in layer_stats and layer_stats[tinfo]['texts'] > 0:
                        text_count += layer_stats[tinfo]['texts']
                        matching_text_layers.append(tname)
                scored.append((c, text_count, matching_text_layers))

            # Sort: text_count > 0 first, then by layer_num desc (higher metal = top metal)
            scored.sort(key=lambda x: (x[1] > 0, x[0]['layer_num']), reverse=True)

            if scored:
                best = scored[0]
                suggested_pad = best[0]['name']
                suggested_text = best[2]

        # Fallback: no LYP or no scored candidates
        if not suggested_pad and pad_candidates:
            # Pick the .drawing layer with most shapes, or any layer with most shapes
            for c in pad_candidates:
                if c['name'] and c['name'].endswith('.drawing'):
                    suggested_pad = c['name']
                    break
            if not suggested_pad:
                c = pad_candidates[0]
                suggested_pad = c['name'] or f"{c['layer_num']}/{c['datatype']}"

        return {
            'pad_candidates': pad_candidates,
            'text_candidates': text_candidates,
            'suggested_pad_layer': suggested_pad,
            'suggested_text_layers': suggested_text,
        }

    def extract_named_pads(self, gds_path: str, pad_layer_name: str,
                           text_layer_names: Optional[List[str]] = None,
                           max_distance: Optional[float] = None,
                           flatten: bool = True) -> Tuple[List[PadInfo], str]:
        """High-level convenience method: extract pads with names from a GDS file.

        Args:
            gds_path: Path to GDSII file
            pad_layer_name: Layer name for pads (e.g. "TopMetal2.drawing")
            text_layer_names: Optional list of text layer names. Auto-detected if None.
            max_distance: Optional max distance in DBU for text association.
            flatten: Whether to flatten the cell hierarchy (default True).

        Returns:
            Tuple of (list of PadInfo with names, cell_name string)
        """
        # Load GDS
        layout = db.Layout()
        layout.read(gds_path)

        top_cell = layout.top_cell()
        if not top_cell:
            raise ValueError(f"No top cell found in {gds_path}")

        if flatten:
            top_cell.flatten(1)

        # Resolve pad layer
        pad_layer = self.lyp_parser.get_layer(pad_layer_name)
        if not pad_layer:
            available = ', '.join(self.lyp_parser.get_layer_names()[:10])
            raise ValueError(
                f"Layer '{pad_layer_name}' not found in LYP. Available: {available}..."
            )

        # Extract pads
        pads = self.extract_pads(layout, top_cell, pad_layer)

        # Resolve text layers
        if text_layer_names:
            text_layers = []
            for name in text_layer_names:
                info = self.lyp_parser.get_layer(name)
                if info:
                    text_layers.append((name, info))
                else:
                    print(f"Warning: Text layer '{name}' not found in LYP",
                          file=sys.stderr)
        else:
            text_layers = self.auto_detect_text_layers(
                layout, top_cell, pad_layer_name
            )

        # Extract and associate texts
        if text_layers:
            texts = self.extract_texts(layout, top_cell, text_layers)
            pads = self.associate_texts_with_pads(pads, texts, max_distance)

            layer_names = [name for name, _ in text_layers]
            print(f"Text layers used: {', '.join(layer_names)}")
            print(f"Texts found: {len(texts)}")
            named_count = sum(1 for p in pads if p.name is not None)
            print(f"Pads with names: {named_count}/{len(pads)}")
        else:
            print("No text layers found, pads will be numbered sequentially")

        return pads, top_cell.name
