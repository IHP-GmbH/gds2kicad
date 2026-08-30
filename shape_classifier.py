# SPDX-License-Identifier: GPL-3.0-or-later
"""Classify TopMetal2 shapes into physical roles.

Everything on the stripped layer shares one GDS layer/datatype, so role
(cu-pillar / bond-pad / via-land / wire / fill / frame / plane) is decided from
geometry plus one electrical hint (isolation, from net extraction). Thresholds all
live in the InterposerProfile so no size is hard-coded. When the full GDS is
available its marker layers (41/35, 41/0) override the geometric guess.

Rules, first match wins, after a mass pre-pass that only calls a small isolated
shape "fill" when many identical ones exist (a lone 20 um square is not fill):
  marker hit -> that role; PATH -> wire; a side over frame_min -> frame; large
  near-square -> plane; fill; ~bondpad square -> bond_pad; ~pillar round -> cu_pillar;
  ~via_land square -> via_land; else misc.
"""

from typing import Dict, List, Optional

from interposer_model import CopperRole, CopperShape


def _near_square(s: CopperShape) -> bool:
    hi = max(s.width_dbu, s.height_dbu)
    lo = min(s.width_dbu, s.height_dbu)
    return hi > 0 and lo / hi >= 0.7


def _marker_bboxes(region) -> List[tuple]:
    if region is None:
        return []
    out = []
    for poly in region.each():
        b = poly.bbox()
        out.append((b.left, b.bottom, b.right, b.top))
    return out


def _in_any(bboxes: List[tuple], cx: int, cy: int) -> bool:
    for l, b, r, t in bboxes:
        if l <= cx <= r and b <= cy <= t:
            return True
    return False


def classify(copper: List[CopperShape], profile, dbu: float,
             isolated: Dict[int, bool],
             marker_regions: Optional[dict] = None) -> None:
    """Set ``.role`` on every CopperShape in place."""
    # microns per compare; profile sizes are in um.
    def um(v_dbu):
        return v_dbu * dbu

    pillar = profile.pillar_dia_um
    pillar_tol = profile.pillar_tol_um
    bond = profile.bondpad_size_um
    bond_tol = profile.bondpad_tol_um
    via = profile.via_land_um
    via_tol = profile.via_land_tol_um
    fill = profile.fill_size_um
    fill_tol = profile.fill_tol_um
    frame_min = profile.frame_min_len_um
    plane_min = profile.plane_min_side_um

    pillar_bboxes = _marker_bboxes((marker_regions or {}).get(CopperRole.CU_PILLAR))
    bond_bboxes = _marker_bboxes((marker_regions or {}).get(CopperRole.BOND_PAD))

    # Fill mass pre-pass: count isolated near-square shapes near the fill size.
    fill_candidates = 0
    for s in copper:
        if s.is_path or not _near_square(s):
            continue
        if isolated.get(s.source_index) and abs(um(max(s.width_dbu, s.height_dbu)) - fill) <= fill_tol:
            fill_candidates += 1
    fill_confirmed = fill_candidates >= profile.min_fill_repeat

    for s in copper:
        cx, cy = s.center_dbu
        # Authoritative marker override.
        if _in_any(pillar_bboxes, cx, cy):
            s.role = CopperRole.CU_PILLAR
            continue
        if _in_any(bond_bboxes, cx, cy):
            s.role = CopperRole.BOND_PAD
            continue

        if s.is_path:
            s.role = CopperRole.WIRE
            continue

        max_um = um(max(s.width_dbu, s.height_dbu))
        min_um = um(min(s.width_dbu, s.height_dbu))
        square = _near_square(s)

        if max_um > frame_min:
            s.role = CopperRole.FRAME
        elif not square:
            # An elongated box/polygon that is not a seal-ring edge is a routing
            # stub drawn as a rectangle (a wire without a PATH primitive).
            s.role = CopperRole.WIRE
        elif square and min_um >= plane_min:
            s.role = CopperRole.PLANE
        elif (fill_confirmed and isolated.get(s.source_index)
              and square and abs(max_um - fill) <= fill_tol):
            s.role = CopperRole.FILL
        elif square and abs(max_um - bond) <= bond_tol:
            s.role = CopperRole.BOND_PAD
        elif (square and abs(max_um - pillar) <= pillar_tol
              and s.n_vertices >= profile.pillar_min_vertices):
            s.role = CopperRole.CU_PILLAR
        elif square and abs(max_um - via) <= via_tol:
            s.role = CopperRole.VIA_LAND
        else:
            s.role = CopperRole.MISC
