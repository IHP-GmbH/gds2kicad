# SPDX-License-Identifier: GPL-3.0-or-later
"""Interposer classification profile.

Every geometric size that separates a cu-pillar from a bond-pad from fill metal
is design-specific (a 240tx pillar is 45 um; the interconnect_pdk manifest default
is 49 um). So the shape classifier reads its thresholds from an ``InterposerProfile``
instead of hard-coding them: defaults track the IHP intm4tm2 / interconnect_pdk
numbers, a ``--profile foo.json`` overlay retunes them per design, and
``calibrate_from_markers`` can auto-fit pillar/bond-pad sizes from the full GDS's
marker layers (41/35, 41/0) when that GDS is available.

Sizes are kept in microns (human-facing, matches the PDK datasheets); the
classifier converts to DBU with the layout's dbu at compare time.
"""

import json
from dataclasses import dataclass, field, asdict, fields
from typing import Optional, Tuple


def _as_layer(value) -> Tuple[int, int]:
    """Coerce a JSON layer spec to a (layer, datatype) int tuple.

    Accepts [layer, datatype], (layer, datatype) or the "layer/datatype" string
    form used elsewhere in the tool (parse_layer_spec)."""
    if isinstance(value, str):
        num, _, dt = value.partition("/")
        return (int(num), int(dt or 0))
    return (int(value[0]), int(value[1]))


@dataclass
class InterposerProfile:
    """Thresholds that drive shape classification and net extraction.

    Defaults come from the IHP intm4tm2 interposer (TopMetal2 = 134/0, its text on
    134/25, cu-pillar/bond-pad marker layers 41/35 and 41/0) and the
    interconnect_pdk ``cupillar_opt2`` method (49 um body). A per-design overlay is
    expected; treat these as a starting point, not gospel.
    """

    # Layers (the stripped GDS the tool consumes).
    pad_layer: Tuple[int, int] = (134, 0)        # TopMetal2 drawing
    label_layer: Tuple[int, int] = (134, 25)     # TopMetal2 text / net labels

    # Cu-pillar: near-circular polygon, ~pillar_dia_um across, many hull vertices.
    pillar_dia_um: float = 45.0
    pillar_tol_um: float = 8.0
    pillar_min_vertices: int = 16                # nv64 in practice; 8 = octagon bond-pad

    # Bond-pad: ~square (box or low-vertex octagon), ~bondpad_size_um on a side.
    bondpad_size_um: float = 120.0
    bondpad_tol_um: float = 15.0

    # Via-land: small square under a pillar (29.34 um in 240tx).
    via_land_um: float = 29.34
    via_land_tol_um: float = 4.0

    # Fill / dummy metal: a small shape repeated many times and isolated.
    fill_size_um: float = 20.0
    fill_tol_um: float = 3.0
    min_fill_repeat: int = 20                    # a lone 20 um shape is not fill

    # Frame / seal ring: a box with one side longer than this.
    frame_min_len_um: float = 1000.0

    # Plane: a large near-square copper region.
    plane_min_side_um: float = 300.0

    # Text label placed just off its pad: nearest-pad recovery window (um).
    label_tolerance_um: float = 30.0

    # Board outline source: "frame" (seal ring), "plane", or "bbox" (design extent).
    board_outline_src: str = "frame"

    # Keep dummy fill metal in the output (the user asked for fill on the PCB).
    keep_fill: bool = True

    # Full-GDS marker layers for optional authoritative classification / calibration.
    pillar_marker_layer: Tuple[int, int] = (41, 35)   # dfpad:pillar
    bondpad_marker_layer: Tuple[int, int] = (41, 0)   # dfpad

    # Free-text notes carried into the model metadata (design name, method, etc.).
    notes: dict = field(default_factory=dict)

    _LAYER_KEYS = ("pad_layer", "label_layer",
                   "pillar_marker_layer", "bondpad_marker_layer")

    # ---- JSON I/O ---------------------------------------------------------

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in self._LAYER_KEYS:
            d[k] = list(d[k])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "InterposerProfile":
        """Build a profile from a dict, applying only known keys over defaults."""
        known = {f.name for f in fields(cls)}
        clean = {}
        for k, v in d.items():
            if k not in known:
                continue
            clean[k] = _as_layer(v) if k in cls._LAYER_KEYS else v
        return cls(**clean)

    @classmethod
    def load(cls, path: str) -> "InterposerProfile":
        """Load a profile JSON as an overlay on the defaults."""
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    def merge(self, overlay: dict) -> "InterposerProfile":
        """Return a copy with ``overlay`` keys applied over this profile."""
        base = self.to_dict()
        base.update(overlay or {})
        return self.from_dict(base)

    # ---- Optional marker calibration -------------------------------------

    def calibrate_from_markers(self, layout, top_cell) -> "InterposerProfile":
        """Fit pillar/bond-pad sizes from full-GDS marker layers, in place.

        When the caller supplies the un-stripped GDS, layers 41/35 (pillar) and
        41/0 (bond-pad) give the true pad footprints. The median marker span sets
        ``pillar_dia_um`` / ``bondpad_size_um`` so the geometric thresholds need no
        manual tuning per design. Returns self (mutated) for chaining; silently
        does nothing for a layer that is absent or empty.
        """
        dbu = layout.dbu
        for marker, attr in ((self.pillar_marker_layer, "pillar_dia_um"),
                             (self.bondpad_marker_layer, "bondpad_size_um")):
            idx = layout.layer(*marker)
            spans = []
            for shape in top_cell.shapes(idx).each():
                if shape.is_box():
                    b = shape.box
                elif shape.is_polygon():
                    b = shape.polygon.bbox()
                else:
                    continue
                spans.append(((b.right - b.left) + (b.top - b.bottom)) / 2.0 * dbu)
            if spans:
                spans.sort()
                setattr(self, attr, round(spans[len(spans) // 2], 4))
        return self
