# SPDX-License-Identifier: GPL-3.0-or-later
"""Emit a KiCad 9 project (.kicad_pcb + .kicad_sch + .kicad_pro) from an
InterposerModel.

Format and idioms follow the ecosystem's proven generator
(design-projects/tv1/kicad/build_design.py): KiCad 9 tokens (pcb 20251027, sch
20251012, pro meta v3, generator_version 9.99), one shared net-code map feeding
every file, deterministic uuid5 so reruns are byte-stable, and the load-bearing
asymmetry that a footprint property carries a uuid while a schematic symbol
property must not.

Choices for this tool (from the design decisions):
  * Schematic = bond-pads only. Each bond-pad is a one-pin symbol carrying its net
    name as a global label, or a no-connect when it has no net. Cu-pillars get PCB
    footprints (with nets) but no symbol; KiCad shows them "not in schematic".
  * PCB copper = hybrid. Routing wires (GDS PATHs, and thin rectangle stubs) become
    editable copper tracks on their net; the seal-ring frame becomes the Edge.Cuts
    outline; planes, via-lands, fill and misc copper are reproduced as graphic
    copper polygons on F.Cu (faithful geometry; a base the user re-pours as zones).
  * The layer stack is cloned verbatim from the interposer template board.
"""

import json
import os
import uuid
from pathlib import Path
from typing import List, Optional

import sexpr_io as sx
from _paths import atomic_write
from sexpr import sanitize_sexpr_token
from interposer_model import CopperRole, InterposerModel, PAD_ROLES
from layer_stack import load_layer_stack

NS = uuid.UUID("b7d3a1e2-4c5f-5a6b-8c9d-0e1f2a3b4c5d")   # fixed: stable output
GENERATOR = "gds_to_kicad_project"

PCB_VERSION = "20251027"
SCH_VERSION = "20251012"
GENERATOR_VERSION = "9.99"

_PRO_RELPATHS = (
    "design-projects/example/kicad/two_die_interposer.kicad_pro",
)


def uid(*parts) -> str:
    """A uuid that depends only on what it names, so reruns are byte-stable."""
    return str(uuid.uuid5(NS, "|".join(str(p) for p in parts)))


def _q(value) -> sx.Quoted:
    return sx.Quoted(sanitize_sexpr_token(value))


# ---------------------------------------------------------------- geometry

class _Tx:
    """GDS DBU -> KiCad mm, Y negated (GDS y-up -> KiCad y-down), optional X flip."""

    def __init__(self, dbu_to_mm: float, flip: bool):
        self.s = dbu_to_mm
        self.mx = -1.0 if flip else 1.0

    def pt(self, x_dbu, y_dbu):
        return (round(self.mx * x_dbu * self.s, 6), round(-y_dbu * self.s, 6))

    def length(self, v_dbu):
        return round(v_dbu * self.s, 6)


def _poly_points(shape, tx) -> List[tuple]:
    if shape.polygon_points_dbu:
        pts = shape.polygon_points_dbu
    else:
        l, b, r, t = shape.bbox_dbu
        pts = [[l, b], [r, b], [r, t], [l, t]]
    return [tx.pt(x, y) for x, y in pts]


# ------------------------------------------------------------------- nets

def _netcode(model: InterposerModel) -> dict:
    """name -> board net index (0 reserved for the no-net)."""
    return {n.name: i + 1 for i, n in enumerate(sorted(model.nets, key=lambda x: x.name))}


# -------------------------------------------------------------- footprints

def _field(name, value, at, layer, hide, key, size=0.2, thick=0.05):
    node = ["property", _q(name), _q(value),
            ["at", sx.num(at[0]), sx.num(at[1]), "0"],
            ["layer", _q(layer)]]
    if hide:
        node.append(["hide", "yes"])
    node.append(["uuid", _q(uid("prop", key, name))])
    node.append(["effects", ["font", ["size", sx.num(size), sx.num(size)],
                             ["thickness", sx.num(thick)]]])
    return node


def _pad_footprint(pad, tx, netcode, sheet, sym_uuid, lib):
    """One footprint holding one pad, placed at the pad centre on F.Cu."""
    x, y = tx.pt(*pad.center_dbu)
    ref = _pad_ref(pad)
    role = "cu pillar" if pad.role == CopperRole.CU_PILLAR else "bond pad"
    node = ["footprint", _q(f"{lib}:{ref}"),
            ["layer", _q("F.Cu")],
            ["uuid", _q(uid("fp", ref))],
            ["at", sx.num(x), sx.num(y)],
            ["descr", _q(role)]]
    node.append(_field("Reference", ref, (0, 0), "F.SilkS", False, ref))
    node.append(_field("Value", pad.name or "NC", (0, -0.3), "F.Fab", True, ref))
    node.append(_field("Footprint", f"{lib}:{ref}", (0, 0), "F.Fab", True, ref))
    node.append(_field("Datasheet", "", (0, 0), "F.Fab", True, ref))
    node += [["attr", "smd"],
             ["path", _q(f"/{sheet}/{sym_uuid}")]]

    w = tx.length(pad.width_dbu)
    h = tx.length(pad.height_dbu)
    code = netcode.get(pad.name, 0)
    net_child = ["net", str(code), _q(pad.name)] if code else None

    # rect for a box, circle for a round pillar, rect otherwise (base approximation).
    if pad.role == CopperRole.CU_PILLAR and pad.is_polygon:
        shape, size = "circle", (max(w, h), max(w, h))
    else:
        shape, size = "rect", (w, h)
    pad_node = ["pad", _q("1"), "smd", shape,
                ["at", "0", "0"],
                ["size", sx.num(size[0]), sx.num(size[1])],
                ["layers", _q("F.Cu")]]
    if net_child:
        pad_node.append(net_child)
    pad_node += [["pinfunction", _q("PAD")], ["pintype", _q("passive")],
                 ["uuid", _q(uid("pad", ref))]]
    node.append(pad_node)
    return node


def _pad_ref(pad) -> str:
    prefix = "P" if pad.role == CopperRole.CU_PILLAR else "BP"
    return f"{prefix}{pad.source_index}"


# ------------------------------------------------------------------ copper

def _copper_nodes(model, tx, netcode) -> list:
    """Hybrid copper: tracks for wires, graphic polygons for the rest."""
    out = []
    seg = 0
    keep_fill = model.profile.keep_fill
    for s in model.copper:
        if s.role in PAD_ROLES or s.role == CopperRole.FRAME:
            continue
        name = _shape_net_name(model, s)
        code = netcode.get(name, 0)

        if s.role == CopperRole.WIRE:
            for a, b in _wire_segments(s, tx):
                out.append(["segment", ["start", sx.num(a[0]), sx.num(a[1])],
                            ["end", sx.num(b[0]), sx.num(b[1])],
                            ["width", sx.num(_wire_width(s, tx))],
                            ["layer", _q("F.Cu")],
                            ["net", str(code)],
                            ["uuid", _q(uid("seg", seg))]])
                seg += 1
            continue

        if s.role == CopperRole.FILL and not keep_fill:
            continue

        # plane / via_land / fill / misc -> graphic copper polygon on F.Cu.
        pts = _poly_points(s, tx)
        poly = ["gr_poly",
                ["pts"] + [["xy", sx.num(x), sx.num(y)] for x, y in pts],
                ["stroke", ["width", "0"], ["type", "solid"]],
                ["fill", "yes"],
                ["layer", _q("F.Cu")],
                ["uuid", _q(uid("grpoly", s.source_index))]]
        out.append(poly)
    return out


def _shape_net_name(model, shape) -> str:
    if shape.net_id is None or shape.net_id < 0:
        return ""
    for n in model.nets:
        if n.id == shape.net_id:
            return n.name
    return ""


def _wire_width(shape, tx) -> float:
    if shape.is_path and shape.path_width_dbu:
        return tx.length(shape.path_width_dbu)
    return tx.length(min(shape.width_dbu, shape.height_dbu))


def _wire_segments(shape, tx) -> List[tuple]:
    """Wire -> list of (a, b) mm endpoints. A PATH becomes its spine; a thin
    rectangle becomes one track along its long axis."""
    if shape.is_path and shape.path_points_dbu and len(shape.path_points_dbu) >= 2:
        pts = [tx.pt(x, y) for x, y in shape.path_points_dbu]
        return [(a, b) for a, b in zip(pts, pts[1:]) if a != b]
    l, b, r, t = shape.bbox_dbu
    cx, cy = shape.center_dbu
    if shape.width_dbu >= shape.height_dbu:
        return [(tx.pt(l, cy), tx.pt(r, cy))]
    return [(tx.pt(cx, b), tx.pt(cx, t))]


# ------------------------------------------------------------------- board

def _pcb_node(model, tx, netcode, stack, sheet, sym_uuids, lib):
    thickness = 1.56
    node = ["kicad_pcb",
            ["version", PCB_VERSION],
            ["generator", _q(GENERATOR)],
            ["generator_version", _q(GENERATOR_VERSION)],
            ["general", ["thickness", sx.num(thickness)],
             ["legacy_teardrops", "no"]],
            ["paper", _q("A4")],
            stack.layers_node,
            stack.setup_node]

    node.append(["net", "0", _q("")])
    for name, code in sorted(netcode.items(), key=lambda kv: kv[1]):
        node.append(["net", str(code), _q(name)])

    # Footprints: every pad (cu-pillar + bond-pad).
    for pad in model.pads:
        node.append(_pad_footprint(pad, tx, netcode, sheet,
                                   sym_uuids.get(_pad_ref(pad), uid("sym", _pad_ref(pad))),
                                   lib))

    # Edge.Cuts outline.
    pts = [tx.pt(x, y) for x, y in model.outline.points_dbu]
    for i in range(len(pts)):
        a, b = pts[i], pts[(i + 1) % len(pts)]
        node.append(["gr_line", ["start", sx.num(a[0]), sx.num(a[1])],
                     ["end", sx.num(b[0]), sx.num(b[1])],
                     ["stroke", ["width", "0.005"], ["type", "default"]],
                     ["layer", _q("Edge.Cuts")],
                     ["uuid", _q(uid("edge", i))]])

    node += _copper_nodes(model, tx, netcode)
    node.append(["embedded_fonts", "no"])
    return node


# --------------------------------------------------------------- schematic

def _bondpad_lib_symbol(lib, name):
    """A minimal one-pin pad symbol embedded in the schematic's lib_symbols."""
    return ["symbol", _q(f"{lib}:{name}"),
            ["pin_numbers", ["hide", "yes"]],
            ["pin_names", ["offset", "0"], ["hide", "yes"]],
            ["exclude_from_sim", "no"], ["in_bom", "yes"], ["on_board", "yes"],
            ["property", _q("Reference"), _q("BP"),
             ["at", "0", "2.54", "0"],
             ["effects", ["font", ["size", "1.27", "1.27"]]]],
            ["property", _q("Value"), _q(name),
             ["at", "0", "-2.54", "0"],
             ["effects", ["font", ["size", "1.27", "1.27"]]]],
            ["symbol", _q(f"{name}_0_1"),
             ["rectangle", ["start", "-2.54", "1.27"], ["end", "2.54", "-1.27"],
              ["stroke", ["width", "0.254"], ["type", "default"]],
              ["fill", ["type", "background"]]]],
            ["symbol", _q(f"{name}_1_1"),
             ["pin", "passive", "line",
              ["at", "-5.08", "0", "0"], ["length", "2.54"],
              ["name", _q("PAD"), ["effects", ["font", ["size", "1.27", "1.27"]]]],
              ["number", _q("1"), ["effects", ["font", ["size", "1.27", "1.27"]]]]]]]


def _sch_node(model, netcode, sheet, sym_uuids, lib, project_name):
    symname = "BondPad"
    libid = f"{lib}:{symname}"
    node = ["kicad_sch",
            ["version", SCH_VERSION],
            ["generator", _q(GENERATOR)],
            ["generator_version", _q(GENERATOR_VERSION)],
            ["uuid", _q(sheet)],
            ["paper", _q("A2")],
            ["lib_symbols", _bondpad_lib_symbol(lib, symname)]]

    # Bond-pads only, on a 1.27 mm grid so every pin and label lands on grid.
    # Geometry (mm), all multiples of 1.27:
    #   symbol origin      (sx0, sy0)
    #   pin connection     (sx0 - PIN_DX, sy0)          local pin is at (-5.08, 0)
    #   label anchor       (sx0 - LABEL_DX, sy0)        pulled left, off the body
    #   a wire bridges the pin endpoint and the label so they read as separate.
    bondpads = [p for p in model.pads if p.role == CopperRole.BOND_PAD]
    ROWS = 12
    COL_PITCH, ROW_PITCH = 38.1, 12.7          # 30 x 1.27, 10 x 1.27
    X0, Y0 = 38.1, 25.4
    PIN_DX, LABEL_DX = 5.08, 12.7              # 4 x 1.27, 10 x 1.27
    REF_DY = 3.81                              # 3 x 1.27, reference sits above body
    for i, pad in enumerate(bondpads):
        ref = _pad_ref(pad)
        col, row = divmod(i, ROWS)
        sx0, sy0 = X0 + col * COL_PITCH, Y0 + row * ROW_PITCH
        sym_uuid = sym_uuids[ref]
        inst = ["symbol", ["lib_id", _q(libid)],
                ["at", sx.num(sx0), sx.num(sy0), "0"],
                ["unit", "1"], ["body_style", "1"],
                ["exclude_from_sim", "no"], ["in_bom", "yes"],
                ["on_board", "yes"], ["dnp", "no"],
                ["fields_autoplaced", "yes"],
                ["uuid", _q(sym_uuid)]]
        fpname = f"{lib}:{ref}"
        # Reference visible above the body; everything else hidden. The net name
        # is already shown by the global label, so a visible Value would duplicate
        # it and collide with the reference.
        for nm, val, hide, py in (("Reference", ref, False, sy0 - REF_DY),
                                  ("Value", pad.name or "NC", True, sy0 + REF_DY),
                                  ("Footprint", fpname, True, sy0 + REF_DY),
                                  ("Datasheet", "", True, sy0 + REF_DY),
                                  ("Description", "interposer bond pad", True,
                                   sy0 + REF_DY)):
            p = ["property", _q(nm), _q(val),
                 ["at", sx.num(sx0), sx.num(py), "0"]]
            if hide:
                p.append(["hide", "yes"])
            p.append(["effects", ["font", ["size", "1.27", "1.27"]]])
            inst.append(p)
        inst.append(["pin", _q("1"), ["uuid", _q(uid("schpin", ref))]])
        inst.append(["instances", ["project", _q(project_name),
                                   ["path", _q(f"/{sheet}"),
                                    ["reference", _q(ref)], ["unit", "1"]]]])
        node.append(inst)

        pin_x, pin_y = sx0 - PIN_DX, sy0
        if pad.name:
            lx = sx0 - LABEL_DX
            node.append(["wire",
                         ["pts", ["xy", sx.num(pin_x), sx.num(pin_y)],
                          ["xy", sx.num(lx), sx.num(pin_y)]],
                         ["stroke", ["width", "0"], ["type", "default"]],
                         ["uuid", _q(uid("wire", ref))]])
            node.append(["global_label", _q(pad.name),
                         ["shape", "passive"],
                         ["at", sx.num(lx), sx.num(pin_y), "180"],
                         ["fields_autoplaced", "yes"],
                         ["effects", ["font", ["size", "1.27", "1.27"]],
                          ["justify", "right"]],
                         ["uuid", _q(uid("lbl", ref))],
                         ["property", _q("Intersheetrefs"), _q("${INTERSHEET_REFS}"),
                          ["at", sx.num(lx), sx.num(pin_y), "180"],
                          ["hide", "yes"],
                          ["effects", ["font", ["size", "1.27", "1.27"]],
                           ["justify", "right"]]]])
        else:
            node.append(["no_connect", ["at", sx.num(pin_x), sx.num(pin_y)],
                         ["uuid", _q(uid("nc", ref))]])

    node.append(["sheet_instances",
                 ["path", _q("/"), ["page", _q("1")]]])
    node.append(["embedded_fonts", "no"])
    return node


# ----------------------------------------------------------------- project

def _find_reference_pro() -> Optional[str]:
    if os.environ.get("REFERENCE_KICAD_PRO"):
        p = os.environ["REFERENCE_KICAD_PRO"]
        if os.path.isfile(p):
            return p
    umbrella = Path(__file__).resolve().parent.parent
    for rel in _PRO_RELPATHS:
        cand = umbrella / rel
        if cand.is_file():
            return str(cand)
    return None


def _pro_dict(sheet, name) -> dict:
    ref = _find_reference_pro()
    if ref:
        pro = json.loads(Path(ref).read_text(encoding="utf-8"))
    else:
        pro = {"board": {"design_settings": {}},
               "net_settings": {"classes": [{"name": "Default"}]},
               "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},
               "pcbnew": {}, "sch": {}}
    pro["meta"] = {"filename": f"{name}.kicad_pro", "version": 3}
    pro["sheets"] = [[sheet, ""]]
    pro["boards"] = []
    # The reference project's `schematic` settings are design-specific and crash
    # eeschema's project loader (SCH_EDIT_FRAME::OpenProjectFiles) on some KiCad
    # builds; reset it and let KiCad backfill defaults. `net_settings` (the PDK
    # net classes) is kept, it loads fine. Only `schematic` is the offender.
    pro["schematic"] = {}
    pro["text_variables"] = {}
    pro.get("pcbnew", {}).pop("last_paths", None)
    return pro


# ------------------------------------------------------------------- entry

def write_project(model: InterposerModel, out_dir: str, emit: str = "all",
                  template_pcb: Optional[str] = None,
                  flip_chip: bool = False, lib: str = "interposer") -> List[str]:
    """Write the requested KiCad files for ``model`` into ``out_dir``.

    emit: "all" | "pcb" | "sch" | "pro". Returns the paths written.
    """
    os.makedirs(out_dir, exist_ok=True)
    name = model.name
    tx = _Tx(model.dbu_to_mm(), flip_chip)
    netcode = _netcode(model)
    stack = load_layer_stack(template_pcb)
    sheet = uid("sheet", name)

    sym_uuids = {}
    for pad in model.pads:
        sym_uuids[_pad_ref(pad)] = uid("sym", _pad_ref(pad))

    written = []
    want = {"all": {"pcb", "sch", "pro"}}.get(emit, {emit})

    if "pcb" in want:
        node = _pcb_node(model, tx, netcode, stack, sheet, sym_uuids, lib)
        path = os.path.join(out_dir, f"{name}.kicad_pcb")
        with atomic_write(path) as f:
            f.write(sx.dumps(node) + "\n")
        written.append(path)

    if "sch" in want:
        node = _sch_node(model, netcode, sheet, sym_uuids, lib, name)
        path = os.path.join(out_dir, f"{name}.kicad_sch")
        with atomic_write(path) as f:
            f.write(sx.dumps(node) + "\n")
        written.append(path)

    if "pro" in want:
        pro = _pro_dict(sheet, name)
        path = os.path.join(out_dir, f"{name}.kicad_pro")
        with atomic_write(path) as f:
            f.write(json.dumps(pro, indent=2, sort_keys=True) + "\n")
        written.append(path)

    return written
