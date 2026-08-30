# SPDX-License-Identifier: GPL-3.0-or-later
"""S-expression reader/writer for KiCad files.

Parse a .kicad_pcb / .kicad_sch / .kicad_mod / .kicad_sym into nested Python lists,
look things up, and write a node back out in KiCad's own indentation style. This is
what lets the project writer clone the layer stack out of an existing board and pull
pad/pin geometry straight from the library files instead of transcribing their
numbers into a second place that then drifts.

Atoms come back as plain strings; a quoted atom is wrapped in ``Quoted`` so the
writer puts the quotes back (KiCad distinguishes ``hide`` from ``"hide"``).

Ported from the TV1 design generator's ``sexpr.py`` (originally Apache-2.0;
re-licensed here under GPL-3.0-or-later, which Apache-2.0 permits).
"""


class Quoted(str):
    """A string atom that was quoted in the source and must be quoted again."""
    __slots__ = ()


_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}


def parse(text):
    """Parse a whole file and return its single top-level node."""
    nodes, _ = _parse_nodes(text, 0)
    if len(nodes) != 1:
        raise ValueError(f"expected one top-level node, found {len(nodes)}")
    return nodes[0]


def _parse_nodes(text, i):
    out = []
    n = len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c == "(":
            node, i = _parse_list(text, i + 1)
            out.append(node)
        elif c == ")":
            return out, i
        elif c == '"':
            atom_, i = _parse_quoted(text, i + 1)
            out.append(atom_)
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '()"':
                j += 1
            out.append(text[i:j])
            i = j
    return out, i


def _parse_list(text, i):
    out, i = _parse_nodes(text, i)
    if i >= len(text) or text[i] != ")":
        raise ValueError("unbalanced parenthesis")
    return out, i + 1


def _parse_quoted(text, i):
    buf = []
    n = len(text)
    while i < n and text[i] != '"':
        if text[i] == "\\" and i + 1 < n:
            buf.append(_ESCAPES.get(text[i + 1], text[i + 1]))
            i += 2
        else:
            buf.append(text[i])
            i += 1
    return Quoted("".join(buf)), i + 1


def head(node):
    return node[0] if isinstance(node, list) and node else None


def find(node, tag):
    """First child list whose head is ``tag``."""
    for child in node:
        if isinstance(child, list) and head(child) == tag:
            return child
    return None


def find_all(node, tag):
    return [c for c in node if isinstance(c, list) and head(c) == tag]


def find_all_rec(node, tag):
    out = []
    for child in node:
        if isinstance(child, list):
            if head(child) == tag:
                out.append(child)
            out.extend(find_all_rec(child, tag))
    return out


def atom(text):
    """Quote a string if KiCad would need it quoted."""
    if text == "" or any(c in text for c in ' ()"\t\n'):
        return Quoted(text)
    return text


def dumps(node, indent=0, tab="\t"):
    """Render a node the way KiCad writes it: one child per line, indented.

    Leading atoms stay on the head line (``(pad "1" smd rect``), which is both what
    KiCad emits and what keeps a diff against a KiCad-saved file readable.
    """
    pad = tab * indent
    if not isinstance(node, list):
        return pad + _atom_str(node)

    parts = [_atom_str(node[0])]
    i = 1
    while i < len(node) and not isinstance(node[i], list):
        parts.append(_atom_str(node[i]))
        i += 1
    line = pad + "(" + " ".join(parts)
    if i == len(node):
        return line + ")"
    body = "\n".join(dumps(child, indent + 1, tab) for child in node[i:])
    return line + "\n" + body + "\n" + pad + ")"


def _atom_str(a):
    if isinstance(a, Quoted):
        esc = str(a).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{esc}"'
    return str(a)


def num(x):
    """Format a coordinate the way KiCad does: shortest exact decimal."""
    s = f"{float(x):.6f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"
