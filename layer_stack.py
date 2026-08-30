# SPDX-License-Identifier: GPL-3.0-or-later
"""KiCad layer stack for the interposer board.

The canonical stack is the interposer PDK's own template board
(``interposer/libs.tech/kicad/interposer_template.kicad_pcb``): F.Cu=TopMetal2,
In1.Cu=TopMetal1, In2.Cu=Metal5, B.Cu=Metal4. Rather than hard-code (which drifts
from the PDK), the project writer clones that board's ``(layers)`` and ``(setup)``
blocks verbatim. This module locates the template (explicit path, then env vars,
then a sibling-repo guess), parses it, and hands back those two nodes plus the
GDS<->KiCad layer map. A compact builtin stack is the fallback so the tool still
runs where the interposer repo is absent (standalone / CI).
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import sexpr_io
from interposer_model import LayerMap

# IHP intm4tm2 metal name -> GDS (layer, datatype), from intm4tm2.map.
GDS_NAME_TO_LAYER = {
    "TopMetal2": (134, 0),
    "TopMetal1": (126, 0),
    "Metal5": (67, 0),
    "Metal4": (50, 0),
}

_TEMPLATE_RELPATH = "libs.tech/kicad/interposer_template.kicad_pcb"

# Minimal but valid KiCad-9 layers+setup, used only when the template is absent.
_FALLBACK_PCB = """(kicad_pcb
\t(version 20251027)
\t(generator "pcbnew")
\t(generator_version "9.99")
\t(layers
\t\t(0 "F.Cu" signal "TopMetal2")
\t\t(4 "In1.Cu" signal "TopMetal1")
\t\t(6 "In2.Cu" signal "Metal5")
\t\t(2 "B.Cu" signal "Metal4")
\t\t(13 "F.Paste" user)
\t\t(15 "B.Paste" user)
\t\t(5 "F.SilkS" user "F.Silkscreen")
\t\t(7 "B.SilkS" user "B.Silkscreen")
\t\t(1 "F.Mask" user)
\t\t(3 "B.Mask" user)
\t\t(17 "Dwgs.User" user "User.Drawings")
\t\t(19 "Cmts.User" user "User.Comments")
\t\t(25 "Edge.Cuts" user)
\t\t(31 "F.CrtYd" user "F.Courtyard")
\t\t(29 "B.CrtYd" user "B.Courtyard")
\t\t(35 "F.Fab" user)
\t\t(33 "B.Fab" user)
\t)
\t(setup
\t\t(stackup
\t\t\t(layer "F.Cu" (type "copper") (thickness 0.035))
\t\t\t(layer "dielectric 1" (type "core") (thickness 0.2) (material "FR-4") (epsilon_r 4.5) (loss_tangent 0.02))
\t\t\t(layer "In1.Cu" (type "copper") (thickness 0.035))
\t\t\t(layer "dielectric 2" (type "core") (thickness 1) (material "FR-4") (epsilon_r 4.5) (loss_tangent 0.02))
\t\t\t(layer "In2.Cu" (type "copper") (thickness 0.035))
\t\t\t(layer "dielectric 3" (type "core") (thickness 0.2) (material "FR-4") (epsilon_r 4.5) (loss_tangent 0.02))
\t\t\t(layer "B.Cu" (type "copper") (thickness 0.035))
\t\t\t(copper_finish "None")
\t\t\t(dielectric_constraints no)
\t\t)
\t\t(pad_to_mask_clearance 0)
\t)
)
"""


@dataclass
class LayerStack:
    layers_node: list
    setup_node: list
    layer_map: List[LayerMap]
    source: str                    # template path or "builtin"
    copper_layers: List[str] = field(default_factory=list)

    def kicad_layer_for(self, gds_layer: Tuple[int, int]) -> Optional[str]:
        for lm in self.layer_map:
            if tuple(lm.gds_layer) == tuple(gds_layer):
                return lm.kicad_layer
        return None


def find_template_pcb(explicit: Optional[str] = None) -> Optional[str]:
    """Locate the interposer template board.

    Order: explicit arg, $INTERPOSER_KICAD_TEMPLATE, $INTERPOSER_ROOT/<relpath>,
    then a sibling-repo guess relative to this file (umbrella_root/interposer/...).
    """
    candidates = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("INTERPOSER_KICAD_TEMPLATE"):
        candidates.append(os.environ["INTERPOSER_KICAD_TEMPLATE"])
    if os.environ.get("INTERPOSER_ROOT"):
        candidates.append(os.path.join(os.environ["INTERPOSER_ROOT"], _TEMPLATE_RELPATH))
    umbrella = Path(__file__).resolve().parent.parent
    candidates.append(str(umbrella / "interposer" / _TEMPLATE_RELPATH))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def _layer_map_from_layers(layers_node) -> Tuple[List[LayerMap], List[str]]:
    """Copper signal layers with a known GDS name -> LayerMap list."""
    lm: List[LayerMap] = []
    copper: List[str] = []
    for entry in layers_node[1:]:
        if not isinstance(entry, list) or len(entry) < 3:
            continue
        kicad_name = str(entry[1])
        kind = str(entry[2])
        if kind == "signal":
            copper.append(kicad_name)
            gds_name = str(entry[3]) if len(entry) >= 4 else ""
            if gds_name in GDS_NAME_TO_LAYER:
                lm.append(LayerMap(kicad_layer=kicad_name,
                                   gds_layer=GDS_NAME_TO_LAYER[gds_name],
                                   gds_name=gds_name))
    return lm, copper


def load_layer_stack(template_pcb: Optional[str] = None) -> LayerStack:
    """Load the (layers) and (setup) blocks and the GDS<->KiCad layer map."""
    path = find_template_pcb(template_pcb)
    if path:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        source = path
    else:
        text = _FALLBACK_PCB
        source = "builtin"
    root = sexpr_io.parse(text)
    layers_node = sexpr_io.find(root, "layers")
    setup_node = sexpr_io.find(root, "setup")
    if layers_node is None or setup_node is None:
        raise ValueError(f"{source}: no (layers)/(setup) block found")
    layer_map, copper = _layer_map_from_layers(layers_node)
    return LayerStack(layers_node=layers_node, setup_node=setup_node,
                      layer_map=layer_map, source=source, copper_layers=copper)
