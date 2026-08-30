# SPDX-License-Identifier: GPL-3.0-or-later
"""Net extraction for the stripped interposer GDS.

Turns TopMetal2 copper + its text labels into named nets using KLayout's own
connectivity engine (``db.LayoutToNetlist``), the same recipe the interposer LVS
deck uses (``connectivity.lvs``) collapsed to the single stripped layer:
self-connect the metal (merge-by-touch = geometric nets) and attach the text layer
so labels name the nets they sit on.

Per-shape net membership is recovered with ``probe_net`` (a point query), which is
robust where matching merged-region centroids back to originals is not. A label
placed just off its pad (exact containment misses it) is recovered by a
nearest-unnamed-shape fallback within the profile's tolerance. A shape is reported
isolated when it is the only shape on its net (a single-member cluster), which the
classifier uses to tell fill from a connected pad. Isolation is decided from
cluster membership counts, not from probe_net returning None, because whether a
lone shape probes to None or to an unnamed net varies between KLayout builds.
"""

import sys
from typing import Dict, List, Tuple

try:
    import klayout.db as db
except ImportError:  # pragma: no cover
    print("Error: KLayout Python module not found.", file=sys.stderr)
    sys.exit(1)


def _cluster_key(net):
    """A stable identity for a net across probe calls (cluster_id if exposed)."""
    cid = getattr(net, "cluster_id", None)
    if cid is not None:
        return ("cid", cid)
    return ("name", net.name, net.expanded_name())


def _extract_labels(top_cell, label_index) -> List[Tuple[str, int, int]]:
    """(string, x_dbu, y_dbu) for every text on the label layer."""
    out = []
    for shape in top_cell.shapes(label_index).each():
        if shape.is_text():
            t = shape.text
            out.append((t.string, int(t.trans.disp.x), int(t.trans.disp.y)))
    return out


def extract_nets(layout, top_cell, copper, profile
                 ) -> Tuple[Dict[int, str], Dict[int, bool], List[str]]:
    """Assign a net name to each CopperShape.

    Returns:
        names: source_index -> net name ("" for not-connect).
        isolated: source_index -> True when the shape is on an isolated
            single-shape net (probe_net None): a fill/floating shape.
        warnings: human-readable SHORT/OPEN connectivity notes.
    """
    pad_index = layout.layer(*profile.pad_layer)
    label_index = layout.layer(*profile.label_layer)

    l2n = db.LayoutToNetlist(db.RecursiveShapeIterator(layout, top_cell, []))
    rpad = l2n.make_polygon_layer(pad_index, "pad")
    rlbl = l2n.make_text_layer(label_index, "lbl")
    l2n.connect(rpad)                 # merge-by-touch nets (connect(topmetal2))
    l2n.connect(rpad, rlbl)           # attach labels (connect(topmetal2, ..._text))
    l2n.extract_netlist()

    names: Dict[int, str] = {}
    attached: set = set()
    # name -> set of distinct net cluster ids (to spot a signal split across nets)
    name_clusters: Dict[str, set] = {}
    # source_index -> cluster key; a lone shape (probe None) gets a unique key.
    cluster_of: Dict[int, object] = {}
    cluster_size: Dict[object, int] = {}

    for s in copper:
        x, y = s.interior_point_dbu()
        net = l2n.probe_net(rpad, db.Point(x, y))
        if net is None:
            names[s.source_index] = ""
            key = ("_lone", s.source_index)
        else:
            name = net.name or ""
            names[s.source_index] = name
            key = _cluster_key(net)
            if name:
                attached.add(name)
                name_clusters.setdefault(name, set()).add(key)
        cluster_of[s.source_index] = key
        cluster_size[key] = cluster_size.get(key, 0) + 1

    # Isolated = the only shape on its net (single-member cluster). Version-
    # independent, unlike relying on probe_net returning None.
    isolated: Dict[int, bool] = {
        i: cluster_size[cluster_of[i]] == 1 for i in cluster_of}

    warnings = _recover_orphan_labels(top_cell, label_index, copper, names,
                                      attached, profile, layout.dbu)
    warnings += _connectivity_warnings(name_clusters)
    return names, isolated, warnings


def _recover_orphan_labels(top_cell, label_index, copper, names, attached,
                           profile, dbu) -> List[str]:
    """Assign labels that did not land on their pad to the nearest unnamed shape."""
    tol_dbu = profile.label_tolerance_um / dbu
    warnings: List[str] = []
    for string, lx, ly in _extract_labels(top_cell, label_index):
        if not string or string in attached:
            continue
        best = None
        best_d2 = tol_dbu * tol_dbu
        for s in copper:
            if names.get(s.source_index):
                continue
            cx, cy = s.center_dbu
            d2 = (cx - lx) ** 2 + (cy - ly) ** 2
            if d2 <= best_d2:
                best_d2 = d2
                best = s
        if best is not None:
            names[best.source_index] = string
            attached.add(string)
            warnings.append(
                f"label '{string}' recovered onto shape #{best.source_index} "
                f"at {best_d2 ** 0.5 * dbu:.1f} um (was off its pad)")
        else:
            warnings.append(
                f"label '{string}' has no pad within "
                f"{profile.label_tolerance_um:.0f} um; dropped")
    return warnings


def _connectivity_warnings(name_clusters: Dict[str, set]) -> List[str]:
    """SHORT (two labels merged onto one net) and OPEN (a signal name split across
    several nets). Power/ground names legitimately span islands on one layer, so a
    split there is not flagged."""
    from kicad_netlist_to_chiplet import classify_net
    warnings: List[str] = []
    for name, clusters in name_clusters.items():
        if "," in name:
            warnings.append(f"SHORT: labels '{name}' land on a single net")
        if len(clusters) > 1 and classify_net(name, []) == "signal":
            warnings.append(
                f"OPEN: signal '{name}' is split across {len(clusters)} nets "
                f"(expected on this single layer if it routes through lower metal)")
    return warnings
