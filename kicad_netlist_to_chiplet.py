#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Convert KiCad S-expression netlist (.net) to chiplet YAML and/or CSV formats.

Reads a KiCad netlist exported via kicad-cli, filters out unconnected nets,
classifies net types, and outputs:
  - YAML suitable for the netlist section of a .chiplet file
  - CSV compatible with Netlist::import_csv() in Chiplet Studio

Usage:
    ./kicad_netlist_to_chiplet.py demo.net --yaml out.yaml --csv out.csv --skip-unconnected
    ./kicad_netlist_to_chiplet.py demo.net --inject demo.chiplet --skip-unconnected
    ./kicad_netlist_to_chiplet.py demo.net --layer-map '{"U2":"TopMetal2"}' --yaml out.yaml
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field


# ── Net classification ──────────────────────────────────────────────

POWER_PATTERNS = re.compile(r'^(V(DD|CC|DDA|CCA|BAT)|DVDD|AVDD|\+\d+V)', re.IGNORECASE)
GROUND_PATTERNS = re.compile(r'^(GND|V(SS|SSA|SSE)|AGND|DGND|PGND|GNDA)', re.IGNORECASE)

VALID_NET_CLASSES = {"power", "ground", "signal", "diff_pair", "nc", "interface"}


def classify_net(net_name, pin_types):
    """Classify a net by name heuristics and pin types.

    Name patterns take absolute priority. Pin types from GDS-extracted symbols
    are unreliable (many analog signals get marked power_in), so they're only
    used as a secondary hint when the name also matches a power/ground pattern.
    """
    if GROUND_PATTERNS.match(net_name):
        return "ground"
    if POWER_PATTERNS.match(net_name):
        return "power"
    # Pin types alone are not enough -- GDS-extracted symbols often mark
    # all pins as power_in. Only classify by name patterns above.
    return "signal"


# ── S-expression tokenizer ─────────────────────────────────────────

def tokenize_sexpr(text):
    """Simple tokenizer for KiCad S-expression format."""
    tokens = []
    i = 0
    while i < len(text):
        c = text[i]
        if c in ' \t\n\r':
            i += 1
        elif c == '(':
            tokens.append('(')
            i += 1
        elif c == ')':
            tokens.append(')')
            i += 1
        elif c == '"':
            j = i + 1
            while j < len(text) and text[j] != '"':
                if text[j] == '\\':
                    j += 1
                j += 1
            tokens.append(text[i+1:j])
            i = j + 1
        else:
            j = i
            while j < len(text) and text[j] not in ' \t\n\r()\"':
                j += 1
            tokens.append(text[i:j])
            i = j
    return tokens


def parse_sexpr(tokens, pos=0):
    """Parse tokenized S-expression into nested lists."""
    if tokens[pos] == '(':
        lst = []
        pos += 1
        while pos < len(tokens) and tokens[pos] != ')':
            item, pos = parse_sexpr(tokens, pos)
            lst.append(item)
        return lst, pos + 1  # skip ')'
    else:
        return tokens[pos], pos + 1


def parse_all_sexpr(text):
    """Parse full S-expression text into a tree."""
    tokens = tokenize_sexpr(text)
    result, _ = parse_sexpr(tokens)
    return result


# ── Data model ──────────────────────────────────────────────────────

@dataclass
class NetConnection:
    component: str
    pin: str
    layer: str = ""

@dataclass
class Net:
    name: str
    net_class: str = "signal"
    connections: list = field(default_factory=list)
    external: bool = False  # True if the net touches an I/O pad component


# ── Netlist parser ──────────────────────────────────────────────────

def find_child(node, tag):
    """Find first child list starting with tag."""
    if isinstance(node, list):
        for child in node:
            if isinstance(child, list) and len(child) > 0 and child[0] == tag:
                return child
    return None


def find_all_children(node, tag):
    """Find all child lists starting with tag."""
    results = []
    if isinstance(node, list):
        for child in node:
            if isinstance(child, list) and len(child) > 0 and child[0] == tag:
                results.append(child)
    return results


def get_value(node, tag, default=""):
    """Get the string value of a simple (tag value) child."""
    child = find_child(node, tag)
    if child and len(child) > 1:
        return child[1]
    return default


def discover_io_pad_refs(tree, io_pad_libs=("io_pads",), extra_ref_prefixes=()):
    """
    Walk the (components) section of a KiCad netlist and return the set of
    refs (e.g. {"J1", "J2"}) that look like external I/O pads.

    A component is treated as an I/O pad when:
    - its footprint identifier starts with one of `io_pad_libs` followed by ':'
      (default "io_pads:..." -- matches the library produced by
      gds_to_kicad/io_pads/), OR
    - its ref starts with any of `extra_ref_prefixes` (e.g. "J" if you want
      every connector treated as external).
    """
    components_section = find_child(tree, "components")
    if components_section is None:
        return set()
    refs = set()
    lib_prefixes = tuple(f"{lib}:" for lib in io_pad_libs)
    for comp in find_all_children(components_section, "comp"):
        ref = get_value(comp, "ref")
        if not ref:
            continue
        if extra_ref_prefixes and ref.startswith(tuple(extra_ref_prefixes)):
            refs.add(ref)
            continue
        fp = get_value(comp, "footprint")
        if fp and fp.startswith(lib_prefixes):
            refs.add(ref)
    return refs


def parse_kicad_netlist(net_file_path, skip_unconnected=True, layer_map=None,
                         io_pad_libs=("io_pads",), extra_ref_prefixes=()):
    """
    Parse a KiCad S-expression netlist file.

    Returns a list of Net objects. Nets touching I/O pad components
    (see discover_io_pad_refs) are flagged with `external=True`.
    """
    with open(net_file_path, "r") as f:
        content = f.read()

    tree = parse_all_sexpr(content)

    io_pad_refs = discover_io_pad_refs(
        tree, io_pad_libs=io_pad_libs, extra_ref_prefixes=extra_ref_prefixes)

    # tree is ['export', ...]
    nets_section = find_child(tree, "nets")
    if nets_section is None:
        print("ERROR: No (nets ...) section found in netlist", file=sys.stderr)
        return []

    parsed_nets = []

    for net_node in find_all_children(nets_section, "net"):
        net_name = get_value(net_node, "name")
        net_class_raw = get_value(net_node, "class", "Default")

        # Get all nodes (connections)
        nodes = find_all_children(net_node, "node")

        # Skip unconnected nets (single-node or "unconnected-*" pattern)
        if skip_unconnected:
            if net_name.startswith("unconnected-"):
                continue
            if len(nodes) < 2:
                continue

        # Collect pin types for classification
        pin_types = []
        connections = []
        for node in nodes:
            ref = get_value(node, "ref")
            pin = get_value(node, "pin")
            pintype = get_value(node, "pintype")
            pin_types.append(pintype)

            layer = ""
            if layer_map and ref in layer_map:
                layer = layer_map[ref]

            connections.append(NetConnection(
                component=ref,
                pin=pin,
                layer=layer,
            ))

        # Classify net
        net_class = classify_net(net_name, pin_types)

        # External: any connection lands on an I/O pad component.
        external = any(c.component in io_pad_refs for c in connections)

        parsed_nets.append(Net(
            name=net_name,
            net_class=net_class,
            connections=connections,
            external=external,
        ))

    return parsed_nets


# ── Output formatters ───────────────────────────────────────────────

# A plain (unquoted) YAML scalar is safe only for identifier-like strings.
_YAML_PLAIN_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_.+/-]*$')
# Tokens a YAML loader would resolve to a bool/null instead of a string.
_YAML_RESERVED = {"true", "false", "yes", "no", "on", "off", "null", "none",
                  "y", "n", "~"}


def yaml_scalar(value):
    """Render a value as a YAML scalar that round-trips to the same string.

    KiCad net/component/pin names are arbitrary user strings; interpolating
    them raw into the .chiplet YAML lets a ':' / '#' / '{' / quote / leading
    indicator char break the parse or silently change the value's type. Plain
    identifier-like names (GND, VDD, U1, TopMetal2) are emitted bare so the
    output stays byte-identical to the historical format; anything else is
    emitted as a JSON double-quoted string, which is valid YAML and
    safe_loads back to the original string.
    """
    s = str(value)
    if _YAML_PLAIN_RE.match(s) and s.lower() not in _YAML_RESERVED:
        return s
    return json.dumps(s)  # JSON string escaping is a subset of YAML's


def nets_to_yaml(nets, external_csv_name=None):
    """Format nets as YAML for .chiplet netlist section."""
    lines = ["netlist:"]
    lines.append("  nets:")

    for net in nets:
        lines.append(f"    - name: {yaml_scalar(net.name)}")
        lines.append(f"      class: {yaml_scalar(net.net_class)}")
        if net.external:
            lines.append("      external: true")
        lines.append("      connections:")
        for conn in net.connections:
            parts = [f"component: {yaml_scalar(conn.component)}",
                     f"pin: {yaml_scalar(conn.pin)}"]
            if conn.layer:
                parts.append(f"layer: {yaml_scalar(conn.layer)}")
            line = ", ".join(parts)
            lines.append(f"        - {{{line}}}")

    if external_csv_name:
        lines.append(f"  external_netlist: {yaml_scalar(external_csv_name)}")

    return "\n".join(lines) + "\n"


def nets_to_csv(nets):
    """Format nets as CSV compatible with Netlist::import_csv()."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["net_name", "component", "pin", "layer", "net_class"])

    for net in nets:
        for conn in net.connections:
            writer.writerow([net.name, conn.component, conn.pin, conn.layer, net.net_class])

    return output.getvalue()


def inject_into_chiplet(chiplet_path, yaml_block):
    """Inject or replace netlist section in a .chiplet YAML file."""
    with open(chiplet_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Match the whole existing netlist block: the "netlist:" key plus every
    # following indented OR blank line, stopping at the next column-0 key.
    # The old "(?:  .*\n)*" stopped at the first blank line inside the block
    # and orphaned the tail into the freshly-written section.
    netlist_pattern = re.compile(r'^netlist:\n(?:[ \t].*\n|\n)*', re.MULTILINE)
    match = netlist_pattern.search(content)

    if match:
        # Replace existing netlist section
        new_content = content[:match.start()] + yaml_block
        # If there's content after the netlist section, preserve it
        remaining = content[match.end():]
        if remaining.strip():
            new_content += remaining
    else:
        # Append netlist section
        if not content.endswith("\n"):
            content += "\n"
        new_content = content + yaml_block

    # Atomic write: a mid-write failure (ENOSPC, interrupt, encode error) must
    # not truncate the user's hand-authored .chiplet. Write a sibling temp file
    # and os.replace() it onto the target.
    target_dir = os.path.dirname(os.path.abspath(chiplet_path))
    fd, tmp_path = tempfile.mkstemp(dir=target_dir, suffix=".chiplet.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_content)
        os.replace(tmp_path, chiplet_path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    print(f"Injected netlist section into {chiplet_path}")


# ── CLI ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Convert KiCad netlist to chiplet YAML/CSV format"
    )
    parser.add_argument("netlist", help="KiCad S-expression netlist file (.net)")
    parser.add_argument("--yaml", metavar="FILE", help="Output YAML file")
    parser.add_argument("--csv", metavar="FILE", help="Output CSV file")
    parser.add_argument("--inject", metavar="FILE",
                        help="Inject netlist into existing .chiplet file")
    parser.add_argument("--skip-unconnected", action="store_true",
                        help="Skip single-node and unconnected-* nets")
    parser.add_argument("--layer-map", metavar="JSON",
                        help='Component-to-layer mapping as JSON, e.g. \'{"U2":"TopMetal2"}\'')
    parser.add_argument("--external-csv", metavar="NAME",
                        help="CSV filename to reference in YAML external_netlist field")
    parser.add_argument("--io-pad-lib", action="append", default=[],
                        metavar="LIB",
                        help="Footprint library name whose components are I/O pads "
                             "(default: io_pads). Their nets are flagged "
                             "external: true. Repeat for multiple libs.")
    parser.add_argument("--external-ref-prefix", action="append", default=[],
                        metavar="PREFIX",
                        help="Additional ref-designator prefix to treat as I/O pad "
                             "(e.g. 'J' to flag every connector as external). "
                             "Repeat for multiple prefixes.")
    parser.add_argument("--summary", action="store_true",
                        help="Print summary of parsed nets")

    args = parser.parse_args()

    if not args.yaml and not args.csv and not args.inject and not args.summary:
        parser.error("Specify at least one output: --yaml, --csv, --inject, or --summary")

    layer_map = None
    if args.layer_map:
        layer_map = json.loads(args.layer_map)

    io_pad_libs = args.io_pad_lib or ["io_pads"]
    nets = parse_kicad_netlist(args.netlist, args.skip_unconnected, layer_map,
                                io_pad_libs=tuple(io_pad_libs),
                                extra_ref_prefixes=tuple(args.external_ref_prefix))

    if not nets:
        # No usable nets: fail loudly rather than writing empty YAML/CSV or
        # injecting an empty netlist: section into a .chiplet (and exiting 0,
        # which let automation treat the run as success).
        print("ERROR: No nets found in netlist", file=sys.stderr)
        return 1

    # Determine CSV filename for YAML reference
    csv_name = args.external_csv
    if not csv_name and args.csv:
        csv_name = args.csv.rsplit("/", 1)[-1]

    if args.yaml:
        yaml_text = nets_to_yaml(nets, csv_name)
        with open(args.yaml, "w") as f:
            f.write(yaml_text)
        print(f"Wrote YAML to {args.yaml}")

    if args.csv:
        csv_text = nets_to_csv(nets)
        with open(args.csv, "w") as f:
            f.write(csv_text)
        print(f"Wrote CSV to {args.csv}")

    if args.inject:
        yaml_text = nets_to_yaml(nets, csv_name)
        inject_into_chiplet(args.inject, yaml_text)

    if args.summary:
        print(f"\nNetlist summary ({len(nets)} nets):")
        class_counts = {}
        total_connections = 0
        for net in nets:
            class_counts[net.net_class] = class_counts.get(net.net_class, 0) + 1
            total_connections += len(net.connections)
            print(f"  {net.name} ({net.net_class}): {len(net.connections)} connections")
        print(f"\nBy class: {class_counts}")
        print(f"Total connections: {total_connections}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
