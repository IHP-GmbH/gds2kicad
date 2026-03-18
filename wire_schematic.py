#!/usr/bin/env python3
"""
Add global labels to a KiCad schematic at pin endpoints.

Computes world coordinates from symbol positions + pin offsets
and injects global_label S-expressions into the schematic file.
"""

import argparse
import uuid
import re
import sys


# Pin angle -> label angle mapping (empirically verified from existing GND labels)
PIN_ANGLE_TO_LABEL_ANGLE = {
    0: 180,    # pin extends right -> label connects from right
    90: 0,     # pin extends up (down in schematic y) -> label connects from left
    180: 0,    # pin extends left -> label connects from left
    270: 90,   # pin extends down (up in schematic y) -> label connects from bottom
}

# Intersheet refs offset by label angle
INTERSHEETREFS_OFFSET = {
    0: (7.967, 0),
    90: (0, -7.967),
    180: (-7.967, 0),
    270: (0, 7.967),
}

# Net definitions: (net_name, component, pin_name, pin_rel_x, pin_rel_y, pin_angle)
# Symbol positions: U1=(213.36, 119.38), U2=(64.77, 45.72), U3=(73.66, 113.03), U4=(215.9, 50.8)
SYMBOL_POSITIONS = {
    "U1": (213.36, 119.38),
    "U2": (64.77, 45.72),
    "U3": (73.66, 113.03),
    "U4": (215.9, 50.8),
}

NETS_TO_ADD = [
    # VDD
    ("VDD", "U2", "vdd", -5.08, 11.43, 270),
    ("VDD", "U4", "Pin3", -12.7, 25.4, 270),
    # VOUT
    ("VOUT", "U2", "vout", 20.32, 0, 180),
    ("VOUT", "U1", "Pin14", 38.1, 13.97, 180),
    # Aout_SIG
    ("Aout_SIG", "U3", "Aout", -45.72, 15.24, 0),
    ("Aout_SIG", "U1", "Pin10", -38.1, 15.24, 0),
    # VINN
    ("VINN", "U2", "vinn", 0, 11.43, 270),
    ("VINN", "U4", "vinn", 27.94, 25.4, 270),
]


def make_global_label(net_name, world_x, world_y, label_angle):
    """Generate a KiCad global_label S-expression."""
    uid = str(uuid.uuid4())
    ox, oy = INTERSHEETREFS_OFFSET[label_angle]
    ref_x = world_x + ox
    ref_y = world_y + oy

    return f"""\t(global_label "{net_name}"
\t\t(shape bidirectional)
\t\t(at {world_x} {world_y} {label_angle})
\t\t(fields_autoplaced yes)
\t\t(effects
\t\t\t(font
\t\t\t\t(size 1.27 1.27)
\t\t\t)
\t\t\t(justify left)
\t\t)
\t\t(uuid "{uid}")
\t\t(property "Intersheetrefs" "${{INTERSHEET_REFS}}"
\t\t\t(at {ref_x} {ref_y} {label_angle})
\t\t\t(hide yes)
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t)
\t\t\t\t(justify left)
\t\t\t)
\t\t)
\t)"""


def compute_world_coords(component, pin_rel_x, pin_rel_y):
    """Compute world coordinates: world_x = sym_x + pin_x, world_y = sym_y - pin_y."""
    sym_x, sym_y = SYMBOL_POSITIONS[component]
    return round(sym_x + pin_rel_x, 2), round(sym_y - pin_rel_y, 2)


def add_labels_to_schematic(sch_path, dry_run=False):
    """Add global labels to the schematic file."""
    with open(sch_path, "r") as f:
        content = f.read()

    labels_block = []
    for net_name, comp, pin, px, py, pin_angle in NETS_TO_ADD:
        wx, wy = compute_world_coords(comp, px, py)
        label_angle = PIN_ANGLE_TO_LABEL_ANGLE[pin_angle]
        label = make_global_label(net_name, wx, wy, label_angle)
        labels_block.append(label)
        print(f"  {net_name}: {comp}/{pin} -> ({wx}, {wy}) angle={label_angle}")

    labels_text = "\n".join(labels_block) + "\n"

    # Insert before the first (symbol block (after existing global labels)
    # Find the first standalone symbol instance (not inside lib_symbols)
    match = re.search(r'^\t\(symbol\n\t\t\(lib_id ', content, re.MULTILINE)
    if not match:
        print("ERROR: Could not find symbol instance insertion point", file=sys.stderr)
        return False

    insert_pos = match.start()
    new_content = content[:insert_pos] + labels_text + content[insert_pos:]

    if dry_run:
        print(f"\nWould insert {len(labels_block)} labels at position {insert_pos}")
        return True

    with open(sch_path, "w") as f:
        f.write(new_content)

    print(f"\nInserted {len(labels_block)} global labels into {sch_path}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Add global labels to KiCad schematic")
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be added")
    args = parser.parse_args()

    print("Adding global labels:")
    success = add_labels_to_schematic(args.schematic, args.dry_run)
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
