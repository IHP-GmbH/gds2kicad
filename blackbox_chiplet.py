#!/usr/bin/env python3
"""
Black-box chiplet GDS generator.

Synthesize a minimal "black-box" chiplet GDS from a simple pad list: die
outline + metal pads + pad-name labels, stamped on the ADK canonical generic
layers. The result flows unchanged through the gds_to_kicad footprint converter
(which auto-detects these layers) and the chiplet-studio viewer.

Use for chiplets from commercial / closed PDK nodes where only the pad map is
known (name + location + size) and there is no full GDS or .lyp.

Canonical layers (adk/config/chiplet_pads.json, with hardcoded fallback):
  pad_drawing 205/0    pad metal
  pad_text    205/25   pad-name labels
  outline     206/0    die mechanical outline
A <stem>.boundaries.json manifest is written beside the GDS carrying the die
outline as the chiplet boundary (ADK assembly metadata, outside any fab-layer
namespace) so the ADK assembly DRC can check the standalone die.

Input spec (JSON):
  {
    "chiplet_name": "ACME_PHY",
    "die": {"width_um": 2000, "height_um": 1500},   # or {"bbox_um": [x0,y0,x1,y1]}
    "pads": [{"name": "VDD", "x_um": -800, "y_um": 600, "w_um": 60, "h_um": 60}, ...]
  }
Input spec (CSV): columns name,x_um,y_um,w_um,h_um (die derived from pads).
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

try:
    import klayout.db as db
except ImportError:
    print("Error: KLayout Python module not found.", file=sys.stderr)
    sys.exit(1)

# Hardcoded fallbacks (match adk/config/chiplet_pads.json + layers.json) so the
# generator runs even when the ADK repo is not reachable.
_FALLBACK = {
    "pad_drawing": (205, 0),
    "pad_text": (205, 25),
    "outline": (206, 0),
}


def _adk_root(explicit: Optional[str] = None) -> Optional[Path]:
    if explicit:
        return Path(explicit)
    env = os.environ.get("ADK_ROOT")
    if env:
        return Path(env)
    cand = Path(__file__).resolve().parent.parent / "adk"
    return cand if cand.is_dir() else None


def load_canonical_layers(adk_root: Optional[str] = None) -> Dict[str, Tuple[int, int]]:
    """Resolve the canonical generic layers from the ADK config, falling back
    to hardcoded numbers if the ADK is unavailable."""
    layers = dict(_FALLBACK)
    root = _adk_root(adk_root)
    if root is None:
        print(f"Warning: ADK not found; using hardcoded canonical layers {_FALLBACK}",
              file=sys.stderr)
        return layers
    try:
        pads = json.loads((root / "config" / "chiplet_pads.json").read_text())["layers"]
        for key in ("pad_drawing", "pad_text", "outline"):
            if key in pads:
                layers[key] = (pads[key]["gds_layer"], pads[key]["gds_datatype"])
    except (OSError, KeyError, json.JSONDecodeError) as e:
        print(f"Warning: could not read ADK layer config ({e}); using hardcoded fallback",
              file=sys.stderr)
    return layers


def load_spec(path: str) -> Dict:
    """Load a chiplet pad spec from JSON or CSV."""
    p = Path(path)
    if p.suffix.lower() == ".csv":
        pads = []
        with p.open() as f:
            for row in csv.DictReader(f):
                pads.append({
                    "name": row["name"],
                    "x_um": float(row["x_um"]),
                    "y_um": float(row["y_um"]),
                    "w_um": float(row["w_um"]),
                    "h_um": float(row["h_um"]),
                })
        return {"chiplet_name": p.stem, "pads": pads}
    return json.loads(p.read_text())


def _die_bbox_um(spec: Dict, margin_um: float = 50.0) -> Tuple[float, float, float, float]:
    """Resolve the die bounding box in um from the spec, or derive it from the
    pad extents plus a margin."""
    die = spec.get("die") or {}
    if "bbox_um" in die:
        x0, y0, x1, y1 = die["bbox_um"]
        return (float(x0), float(y0), float(x1), float(y1))
    if "width_um" in die and "height_um" in die:
        w = float(die["width_um"])
        h = float(die["height_um"])
        return (-w / 2.0, -h / 2.0, w / 2.0, h / 2.0)
    xs0 = [p["x_um"] - p["w_um"] / 2.0 for p in spec["pads"]]
    ys0 = [p["y_um"] - p["h_um"] / 2.0 for p in spec["pads"]]
    xs1 = [p["x_um"] + p["w_um"] / 2.0 for p in spec["pads"]]
    ys1 = [p["y_um"] + p["h_um"] / 2.0 for p in spec["pads"]]
    return (min(xs0) - margin_um, min(ys0) - margin_um,
            max(xs1) + margin_um, max(ys1) + margin_um)


def _write_blackbox_manifest(out_gds: str, die_name: str, dbu: float,
                             outline_box) -> Path:
    """Write a one-entry boundary manifest beside the die GDS: its mechanical
    outline as the chiplet boundary, in die-local DBU with identity transform.
    Lets the ADK assembly DRC check the standalone die without putting the
    boundary on any fabrication layer."""
    out = Path(out_gds)
    b = outline_box  # db.Box in DBU
    poly_dbu = [[b.left, b.bottom], [b.right, b.bottom],
                [b.right, b.top], [b.left, b.top]]
    poly_um = [[round(x * dbu, 6), round(y * dbu, 6)] for x, y in poly_dbu]
    manifest = {
        "schema": "adk-boundary-manifest",
        "version": "1.0.0",
        "generator": "blackbox_chiplet.py",
        "assembly_gds": out.name,
        "dbu_um": dbu,
        "top_cell": die_name,
        "boundaries": [{
            "instance": die_name,
            "source_die": die_name,
            "class": "chiplet",
            "transform": {"origin_um": [0.0, 0.0], "rotation_deg": 0.0,
                          "mirror_x": False, "magnification": 1.0},
            "polygon_dbu": poly_dbu,
            "polygon_um": poly_um,
        }],
    }
    mpath = out.with_name(out.stem + ".boundaries.json")
    mpath.write_text(json.dumps(manifest, indent=2))
    return mpath


def generate_blackbox_gds(spec: Dict, out_gds: str,
                          layers: Dict[str, Tuple[int, int]],
                          write_manifest: bool = True) -> int:
    """Write the die + pads + names GDS on the canonical layers, plus a
    <stem>.boundaries.json manifest carrying the die outline as the chiplet
    boundary (ADK assembly metadata, not a fabrication layer).

    Returns the number of pads stamped.
    """
    def _um(v: float) -> int:
        return int(round(v * 1000.0))  # um -> dbu (1 dbu = 1 nm)

    ly = db.Layout()
    ly.dbu = 0.001
    top = ly.create_cell(spec.get("chiplet_name", "BLACKBOX"))

    pad_l = ly.layer(*layers["pad_drawing"])
    txt_l = ly.layer(*layers["pad_text"])
    out_l = ly.layer(*layers["outline"])

    for pad in spec["pads"]:
        x, y = float(pad["x_um"]), float(pad["y_um"])
        w, h = float(pad["w_um"]), float(pad["h_um"])
        box = db.Box(_um(x - w / 2.0), _um(y - h / 2.0),
                     _um(x + w / 2.0), _um(y + h / 2.0))
        top.shapes(pad_l).insert(box)
        top.shapes(txt_l).insert(
            db.Text(str(pad["name"]), db.Trans(db.Point(_um(x), _um(y)))))

    x0, y0, x1, y1 = _die_bbox_um(spec)
    outline = db.Box(_um(x0), _um(y0), _um(x1), _um(y1))
    top.shapes(out_l).insert(outline)

    ly.write(out_gds)
    if write_manifest:
        _write_blackbox_manifest(out_gds, top.name, ly.dbu, outline)
    return len(spec["pads"])


def main():
    ap = argparse.ArgumentParser(
        description="Generate a black-box chiplet GDS (outline + pads + names) "
                    "from a pad list, on the ADK canonical generic layers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  %(prog)s spec.json -o ACME_PHY.gds\n"
               "  %(prog)s pads.csv -o chip.gds --no-manifest\n")
    ap.add_argument("spec", help="Pad spec: JSON or CSV (see module docstring).")
    ap.add_argument("-o", "--output", required=True, help="Output GDS path.")
    ap.add_argument("--no-manifest", action="store_true",
                    help="Do not write the <stem>.boundaries.json boundary manifest.")
    ap.add_argument("--adk-root", default=None,
                    help="ADK repo root (default: $ADK_ROOT or ../adk).")
    args = ap.parse_args()

    spec = load_spec(args.spec)
    if not spec.get("pads"):
        ap.error("spec has no pads")
    layers = load_canonical_layers(args.adk_root)
    n = generate_blackbox_gds(spec, args.output, layers,
                              write_manifest=not args.no_manifest)

    pd, pt, ol = layers["pad_drawing"], layers["pad_text"], layers["outline"]
    msg = (f"Wrote {args.output}: {n} pads on {pd[0]}/{pd[1]}, names on "
           f"{pt[0]}/{pt[1]}, outline on {ol[0]}/{ol[1]}")
    if not args.no_manifest:
        msg += f" + boundary manifest {Path(args.output).stem}.boundaries.json"
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
