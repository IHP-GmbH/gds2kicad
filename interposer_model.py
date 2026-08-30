# SPDX-License-Identifier: GPL-3.0-or-later
"""Interposer intermediate representation (IR).

A tool-agnostic, DBU-based model of a stripped interposer GDS: which copper shapes
exist, what role each plays (cu-pillar, bond-pad, wire, fill, plane, frame,
via-land), which net each belongs to, and where the board outline is. Parsing GDS
and emitting KiCad are deliberately separated by this model: ``build_model`` fills
it from a GDS, and ``kicad_project_writer`` reads it to emit a project. The model is
JSON round-trippable so it can be dumped (``--dump-model``), diffed and golden-tested
without any KiCad knowledge.

Coordinates stay in integer DBU here; the DBU->mm + Y-flip transform is applied only
at emit time, exactly as the footprint writer does.
"""

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import List, Optional, Tuple

try:
    import klayout.db as db
except ImportError:  # pragma: no cover - import guard mirrors the rest of the tool
    import sys
    print("Error: KLayout Python module not found.", file=sys.stderr)
    sys.exit(1)

from pin_extractor import single_top_cell
from kicad_netlist_to_chiplet import classify_net
from interposer_profile import InterposerProfile


class CopperRole(str, Enum):
    """What a TopMetal2 shape is, physically. str-valued for clean JSON."""
    CU_PILLAR = "cu_pillar"     # chiplet attach pad (round); may carry a net label
    BOND_PAD = "bond_pad"       # external port (large square/octagon)
    VIA_LAND = "via_land"       # small square landing under a pillar
    WIRE = "wire"               # routing PATH connecting pads
    FILL = "fill"               # dummy/fill metal, electrically isolated
    FRAME = "frame"             # seal ring / border
    PLANE = "plane"             # large copper pour
    MISC = "misc"               # unclassified


PAD_ROLES = (CopperRole.CU_PILLAR, CopperRole.BOND_PAD)


@dataclass
class LayerMap:
    """One GDS metal <-> KiCad copper-layer binding (from the template board)."""
    kicad_layer: str            # e.g. "F.Cu"
    gds_layer: Tuple[int, int]  # e.g. (134, 0)
    gds_name: str               # e.g. "TopMetal2"


@dataclass
class CopperShape:
    """A single copper shape on the pad layer, with role and net once resolved."""
    source_index: int
    role: CopperRole
    net_id: int                       # index into InterposerModel.nets; -1 = none
    is_polygon: bool
    is_path: bool
    bbox_dbu: Tuple[int, int, int, int]     # (left, bottom, right, top)
    center_dbu: Tuple[int, int]
    width_dbu: int
    height_dbu: int
    n_vertices: int                   # hull vertex count (round pillar ~64, box 4)
    polygon_points_dbu: Optional[List[List[int]]] = None
    path_points_dbu: Optional[List[List[int]]] = None
    path_width_dbu: Optional[int] = None
    kicad_layer: str = "F.Cu"

    def interior_point_dbu(self) -> Tuple[int, int]:
        """A point guaranteed inside the shape, for a net probe.

        Box/polygon: the bbox centre (interior for the convex pillar/pad/plane
        shapes an interposer top metal carries). Path: the midpoint of its first
        spine segment, which lies on the centreline and so inside a >0-width path.
        """
        if self.is_path and self.path_points_dbu and len(self.path_points_dbu) >= 2:
            (x0, y0), (x1, y1) = self.path_points_dbu[0], self.path_points_dbu[1]
            return ((x0 + x1) // 2, (y0 + y1) // 2)
        return self.center_dbu


@dataclass
class Pad:
    """A cu-pillar or bond-pad: becomes a KiCad footprint pad (and, for bond-pads,
    a schematic symbol). ``name`` is the net name, "" for not-connect."""
    source_index: int
    role: CopperRole
    name: str
    net_id: int
    is_connected: bool
    center_dbu: Tuple[int, int]
    width_dbu: int
    height_dbu: int
    is_polygon: bool
    polygon_points_dbu: Optional[List[List[int]]] = None

    def to_pad_dict(self) -> dict:
        """The shared pad-dict contract (pad_review.read_edited_pads) so the
        existing preview/footprint code can consume a Pad unchanged."""
        cx, cy = self.center_dbu
        l = cx - self.width_dbu / 2.0
        r = cx + self.width_dbu / 2.0
        b = cy - self.height_dbu / 2.0
        t = cy + self.height_dbu / 2.0
        return {
            "index": self.source_index,
            "name": self.name or None,
            "center_x": float(cx),
            "center_y": float(cy),
            "width": float(self.width_dbu),
            "height": float(self.height_dbu),
            "bbox": (l, b, r, t),
            "is_polygon": self.is_polygon,
            "polygon_points": ([(int(x), int(y)) for x, y in self.polygon_points_dbu]
                               if self.polygon_points_dbu else None),
        }


@dataclass
class Net:
    """A logical KiCad net: all shapes sharing a name. Unnamed shapes are not-connect
    and never become a Net (their pads carry name=""/net_id=-1)."""
    id: int
    name: str
    net_class: str              # power | ground | signal (classify_net)
    is_connected: bool
    pad_indices: List[int] = field(default_factory=list)      # into model.pads
    shape_indices: List[int] = field(default_factory=list)    # into model.copper


@dataclass
class BoardOutline:
    """Edge.Cuts outline as a closed DBU polygon, plus where it came from."""
    points_dbu: List[List[int]]
    source: str                 # frame | plane | bbox


@dataclass
class InterposerModel:
    name: str
    dbu_um: float
    source_gds: str
    profile: InterposerProfile
    layer_map: List[LayerMap]
    nets: List[Net]
    pads: List[Pad]
    copper: List[CopperShape]
    outline: BoardOutline
    metadata: dict = field(default_factory=dict)

    def dbu_to_mm(self) -> float:
        return self.dbu_um * 1e-3

    # ---- summary / JSON ---------------------------------------------------

    def role_counts(self) -> dict:
        counts: dict = {}
        for s in self.copper:
            counts[s.role.value] = counts.get(s.role.value, 0) + 1
        return counts

    def summary(self) -> str:
        rc = self.role_counts()
        named = sum(1 for n in self.nets if n.is_connected)
        return (f"{self.name}: {len(self.copper)} shapes "
                f"({', '.join(f'{k}={v}' for k, v in sorted(rc.items()))}); "
                f"{len(self.pads)} pads; {len(self.nets)} nets ({named} named); "
                f"outline={self.outline.source}")

    def to_json(self) -> str:
        def enc(o):
            if isinstance(o, CopperRole):
                return o.value
            if isinstance(o, InterposerProfile):
                return o.to_dict()
            if isinstance(o, (LayerMap, CopperShape, Pad, Net, BoardOutline)):
                return asdict(o)
            if isinstance(o, tuple):
                return list(o)
            raise TypeError(type(o))
        payload = {
            "name": self.name,
            "dbu_um": self.dbu_um,
            "source_gds": self.source_gds,
            "profile": self.profile.to_dict(),
            "layer_map": [asdict(lm) for lm in self.layer_map],
            "nets": [asdict(n) for n in self.nets],
            "pads": [self._pad_json(p) for p in self.pads],
            "copper": [self._shape_json(s) for s in self.copper],
            "outline": asdict(self.outline),
            "metadata": self.metadata,
        }
        return json.dumps(payload, indent=2, default=enc)

    @staticmethod
    def _pad_json(p: Pad) -> dict:
        d = asdict(p)
        d["role"] = p.role.value
        return d

    @staticmethod
    def _shape_json(s: CopperShape) -> dict:
        d = asdict(s)
        d["role"] = s.role.value
        return d

    @classmethod
    def from_json(cls, text: str) -> "InterposerModel":
        d = json.loads(text)

        def layer(t):
            return (int(t[0]), int(t[1]))
        lm = [LayerMap(kicad_layer=x["kicad_layer"],
                       gds_layer=layer(x["gds_layer"]),
                       gds_name=x["gds_name"]) for x in d["layer_map"]]
        nets = [Net(id=n["id"], name=n["name"], net_class=n["net_class"],
                    is_connected=n["is_connected"],
                    pad_indices=list(n["pad_indices"]),
                    shape_indices=list(n["shape_indices"])) for n in d["nets"]]
        pads = [Pad(source_index=p["source_index"], role=CopperRole(p["role"]),
                    name=p["name"], net_id=p["net_id"],
                    is_connected=p["is_connected"],
                    center_dbu=tuple(p["center_dbu"]),
                    width_dbu=p["width_dbu"], height_dbu=p["height_dbu"],
                    is_polygon=p["is_polygon"],
                    polygon_points_dbu=p.get("polygon_points_dbu"))
                for p in d["pads"]]
        copper = [CopperShape(
            source_index=s["source_index"], role=CopperRole(s["role"]),
            net_id=s["net_id"], is_polygon=s["is_polygon"], is_path=s["is_path"],
            bbox_dbu=tuple(s["bbox_dbu"]), center_dbu=tuple(s["center_dbu"]),
            width_dbu=s["width_dbu"], height_dbu=s["height_dbu"],
            n_vertices=s["n_vertices"],
            polygon_points_dbu=s.get("polygon_points_dbu"),
            path_points_dbu=s.get("path_points_dbu"),
            path_width_dbu=s.get("path_width_dbu"),
            kicad_layer=s.get("kicad_layer", "F.Cu")) for s in d["copper"]]
        outline = BoardOutline(points_dbu=[list(pt) for pt in d["outline"]["points_dbu"]],
                               source=d["outline"]["source"])
        return cls(name=d["name"], dbu_um=d["dbu_um"], source_gds=d["source_gds"],
                   profile=InterposerProfile.from_dict(d["profile"]),
                   layer_map=lm, nets=nets, pads=pads, copper=copper,
                   outline=outline, metadata=d.get("metadata", {}))


# ---------------------------------------------------------------------------
# Build the model from a GDS
# ---------------------------------------------------------------------------

def _shape_records(top_cell, layer_index) -> List[CopperShape]:
    """One pass over the pad-layer shapes -> unclassified CopperShape spine."""
    out: List[CopperShape] = []
    idx = 0
    for shape in top_cell.shapes(layer_index).each():
        poly_pts = path_pts = path_w = None
        is_poly = is_path = False
        if shape.is_box():
            b = shape.box
            nv = 4
        elif shape.is_polygon():
            p = shape.polygon
            b = p.bbox()
            is_poly = True
            poly_pts = [[int(pt.x), int(pt.y)] for pt in p.each_point_hull()]
            nv = len(poly_pts)
        elif shape.is_path():
            pa = shape.path
            b = pa.bbox()
            is_path = True
            path_pts = [[int(pt.x), int(pt.y)] for pt in pa.each_point()]
            path_w = int(pa.width)
            nv = len(path_pts)
        else:
            continue
        out.append(CopperShape(
            source_index=idx, role=CopperRole.MISC, net_id=-1,
            is_polygon=is_poly, is_path=is_path,
            bbox_dbu=(b.left, b.bottom, b.right, b.top),
            center_dbu=((b.left + b.right) // 2, (b.bottom + b.top) // 2),
            width_dbu=b.right - b.left, height_dbu=b.top - b.bottom,
            n_vertices=nv, polygon_points_dbu=poly_pts,
            path_points_dbu=path_pts, path_width_dbu=path_w))
        idx += 1
    return out


def _board_outline(copper: List[CopperShape], top_cell, source: str) -> BoardOutline:
    """Pick the Edge.Cuts rectangle from the requested source, falling back to the
    design bounding box."""
    box = None
    if source == "frame":
        frames = [s for s in copper if s.role == CopperRole.FRAME]
        if frames:
            l = min(s.bbox_dbu[0] for s in frames)
            b = min(s.bbox_dbu[1] for s in frames)
            r = max(s.bbox_dbu[2] for s in frames)
            t = max(s.bbox_dbu[3] for s in frames)
            box = (l, b, r, t)
    elif source == "plane":
        planes = [s for s in copper if s.role == CopperRole.PLANE]
        if planes:
            s = max(planes, key=lambda p: p.width_dbu * p.height_dbu)
            box = s.bbox_dbu
    if box is None:
        # Union of the copper (not top_cell.bbox(), which would include the text
        # layer and inflate the outline past the actual metal).
        if copper:
            box = (min(s.bbox_dbu[0] for s in copper),
                   min(s.bbox_dbu[1] for s in copper),
                   max(s.bbox_dbu[2] for s in copper),
                   max(s.bbox_dbu[3] for s in copper))
        else:
            bb = top_cell.bbox()
            box = (bb.left, bb.bottom, bb.right, bb.top)
        source = "bbox"
    l, b, r, t = box
    return BoardOutline(points_dbu=[[l, b], [r, b], [r, t], [l, t]], source=source)


def build_model(source_gds: str, profile: Optional[InterposerProfile] = None,
                layer_map: Optional[List[LayerMap]] = None,
                full_gds: Optional[str] = None,
                pin_list=None) -> InterposerModel:
    """Parse a stripped interposer GDS into an InterposerModel.

    Args:
        source_gds: the stripped GDS (TopMetal2 copper + text only).
        profile: classification thresholds; defaults to InterposerProfile().
        layer_map: GDS<->KiCad layer bindings; defaults to a single F.Cu binding
            for the pad layer (the writer supplies the full stack).
        full_gds: optional un-stripped GDS; its marker layers auto-calibrate the
            pillar/bond-pad sizes and override borderline geometric guesses.
        pin_list: optional reviewed PinList to override pad names by proximity.
    """
    import shape_classifier
    import net_extractor

    profile = profile or InterposerProfile()

    layout = db.Layout()
    layout.read(source_gds)
    top = single_top_cell(layout, source_gds)
    top.flatten(-1, True)

    kicad_pad_layer = "F.Cu"
    if layer_map:
        for lm in layer_map:
            if tuple(lm.gds_layer) == tuple(profile.pad_layer):
                kicad_pad_layer = lm.kicad_layer
                break
    else:
        layer_map = [LayerMap("F.Cu", tuple(profile.pad_layer), "TopMetal2")]

    # Optional marker calibration + authoritative classification regions.
    marker_regions = None
    if full_gds:
        full = db.Layout()
        full.read(full_gds)
        ftop = single_top_cell(full, full_gds)
        ftop.flatten(-1, True)
        profile.calibrate_from_markers(full, ftop)
        marker_regions = {
            CopperRole.CU_PILLAR: db.Region(ftop.shapes(full.layer(*profile.pillar_marker_layer))),
            CopperRole.BOND_PAD: db.Region(ftop.shapes(full.layer(*profile.bondpad_marker_layer))),
        }

    pad_idx = layout.layer(*profile.pad_layer)
    copper = _shape_records(top, pad_idx)
    for s in copper:
        s.kicad_layer = kicad_pad_layer

    # Nets first: names + isolation feed the classifier's fill rule.
    names, isolated, net_warnings = net_extractor.extract_nets(
        layout, top, copper, profile)

    shape_classifier.classify(copper, profile, layout.dbu, isolated, marker_regions)

    # Optional human override of pad names by proximity (reuses pad_review logic).
    if pin_list is not None:
        _apply_pin_list_names(copper, names, pin_list)

    model = _assemble(source_gds, layout, top, profile, layer_map,
                      copper, names, net_warnings)
    return model


def _apply_pin_list_names(copper, names, pin_list):
    """Override names for pad-role shapes by nearest pin_list entry (dbu)."""
    pins = [(p.center_x_dbu, p.center_y_dbu, p.name) for p in pin_list.pins if p.name]
    if not pins:
        return
    for s in copper:
        if s.role not in PAD_ROLES:
            continue
        cx, cy = s.center_dbu
        best = min(pins, key=lambda q: (cx - q[0]) ** 2 + (cy - q[1]) ** 2)
        names[s.source_index] = best[2]


def _assemble(source_gds, layout, top, profile, layer_map, copper, names,
              net_warnings) -> InterposerModel:
    # Logical nets group shapes by non-empty name.
    net_by_name: dict = {}
    nets: List[Net] = []
    for name in names.values():
        if name and name not in net_by_name:
            n = Net(id=len(nets), name=name,
                    net_class=classify_net(name, []), is_connected=True)
            net_by_name[name] = n
            nets.append(n)

    pads: List[Pad] = []
    for s in copper:
        name = names.get(s.source_index, "")
        net = net_by_name.get(name)
        s.net_id = net.id if net else -1
        if net:
            net.shape_indices.append(s.source_index)
        if s.role in PAD_ROLES:
            pad = Pad(source_index=s.source_index, role=s.role, name=name or "",
                      net_id=s.net_id, is_connected=bool(net),
                      center_dbu=s.center_dbu, width_dbu=s.width_dbu,
                      height_dbu=s.height_dbu, is_polygon=s.is_polygon,
                      polygon_points_dbu=s.polygon_points_dbu)
            if net:
                net.pad_indices.append(len(pads))
            pads.append(pad)

    outline = _board_outline(copper, top, profile.board_outline_src)

    named = sum(1 for n in nets if n.is_connected)
    metadata = {
        "role_counts": {},
        "named_nets": named,
        "net_warnings": net_warnings,
        "flip_chip": False,
    }
    rc: dict = {}
    for s in copper:
        rc[s.role.value] = rc.get(s.role.value, 0) + 1
    metadata["role_counts"] = rc

    return InterposerModel(
        name=top.name, dbu_um=layout.dbu, source_gds=source_gds, profile=profile,
        layer_map=layer_map, nets=nets, pads=pads, copper=copper,
        outline=outline, metadata=metadata)
