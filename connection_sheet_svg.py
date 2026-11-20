# SPDX-License-Identifier: GPL-3.0-or-later
"""The connection sheet: the system sheet drawn as an SVG, form
``connection-sheet/1``.

It draws the model chiplet_to_kicad_sch builds for the .kicad_sch
(``sheet_layout``): the same die symbols, pins, labels, no-connect marks and
carrier pads, at the same places, with the texts KiCad shows. The
co-simulation setup band is not drawn. The assembly review signs these bytes
raw, so they depend only on the design and this code:
  * no time, path, host, tool version or uuid; the .chiplet's sha256 is in the
    title block, as on the .kicad_sch;
  * every number is an integer in the sheet's unit, the mil (KiCad's 50 mil
    grid; every point of the layout is on it), formatted here (``_mil``);
  * text is <text> in the generic family monospace at one size, and a label
    box is sized from its character count, never from a font;
  * elements follow the model's order: the title block, each die (its body,
    then each pin with its label or flag), each carrier pad;
  * no script, no event attribute, nothing outside the file (no <image>, no
    href, no url()), UTF-8, LF, one final newline.

Each element carries its kind as its class; ``KINDS`` lists every kind drawn.
A label's box and text are the children of its labelled group. A text no SVG
can hold is refused (``refusals``) before anything is written.
"""

import re
from xml.sax.saxutils import escape

import chiplet_to_kicad_sch as g

FORM = "connection-sheet/1"
# Every kind of element the renderer draws, the class it carries.
KINDS = ("sheet", "title", "symbol", "reference", "value", "pin-left", "pin-right",
         "pin-name", "pin-number", "wire", "label-left", "label-right", "no-connect",
         "pad")

MIL_MM = 0.0254


def _mil(mm: float) -> int:
    """A length of the layout, mm, as whole mils. Every point of the layout is
    on the mil grid, so anything else is a defect here, not in the design."""
    mil = round(mm / MIL_MM, 6)
    if mil != int(mil):
        raise ValueError(f"{mm!r} mm is not a whole number of mils")
    return int(mil)


TEXT = _mil(g.FONT_SIZE)        # text size, as on the .kicad_sch
CHAR = _mil(g.CHAR_W)           # label box per character
LABEL_PAD = 25      # label text inset from the box end
LABEL_HALF = 50     # half the label box height
NC_HALF = 25        # half the no-connect cross
NUMBER_RISE = 15    # pin number baseline above the pin
SCALE = 5           # mil per px of the page's width and height: 200 px per inch

STYLE = """\
.sheet{fill:#ffffff;stroke:#840000;stroke-width:6}
.symbol,.pad{fill:#ffffc2;stroke:#840000;stroke-width:10}
.pin-left,.pin-right{stroke:#840000;stroke-width:6}
.wire{stroke:#008400;stroke-width:6}
.no-connect{fill:none;stroke:#0000c8;stroke-width:6}
.label-left rect,.label-right rect{fill:#ffffff;stroke:#840000;stroke-width:6}
text{fill:#000000;dominant-baseline:central}
.reference,.value,.pin-name{fill:#006464}
.pin-number{fill:#a90000;dominant-baseline:auto}
.reference,.value,.pin-number{text-anchor:middle}
.label-left text,.label-right text{fill:#840000}
.label-left text{text-anchor:end}
"""

# What XML 1.0 lets a document hold at all, even as a character reference.
_NOT_XML = re.compile("[^\t\n\r\x20-퟿-�\U00010000-\U0010ffff]")


def _shown(value) -> str:
    """A text as the .kicad_sch shows it: KiCad's own substitutions."""
    return g.sanitize_sexpr_token(value)


def refusals(system: "g.System") -> list:
    """The free texts the sheet draws that no SVG can hold, as refusals. The
    ids, names, nets and top cells are plain words already."""
    texts = [(system.name, "the assembly name", {"field": "assembly.name"})]
    texts += [(t.port, f"pin {t.name} of {d.id}: the port name",
               {"component": d.id, "terminal": t.name, "field": "port"})
              for d in system.dies if d.kind == "die" for t in d.terminals]
    out = []
    for value, what, where in texts:
        bad = _NOT_XML.search(_shown(value))
        if bad:
            out.append(g._refusal(
                "undrawable_text",
                f"{what} {value!r} holds {bad.group(0)!r}, a character an SVG file "
                f"cannot hold, so the connection sheet cannot be drawn. Rename it.", value=value, **where))
    return out


def _el(kind: str, tag: str, text=None, inner=None, **attrs) -> str:
    """One element of kind ``kind``; ``attrs`` are integers or words, in the
    order given."""
    if kind not in KINDS:
        raise ValueError(f"{kind} is not a kind the connection sheet draws")
    return _tag(tag, text, inner, **{"class": kind}, **attrs)


def _tag(tag: str, text=None, inner=None, **attrs) -> str:
    """``tag`` closed on itself, or holding ``text`` or the markup ``inner``;
    an underscore in an attribute name is its hyphen."""
    head = " ".join([tag] + [f'{k.replace("_", "-")}="{_value(v)}"' for k, v in attrs.items()])
    if text is not None:
        if _NOT_XML.search(text):
            raise ValueError(f"{text!r} is not XML text; refusals() lets none through")
        return f"<{head}>{escape(text)}</{tag}>"
    return f"<{head}/>" if inner is None else f"<{head}>{inner}</{tag}>"


def _value(v) -> str:
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        raise TypeError(f"attribute value {v!r} is neither an integer nor a word")
    return str(v) if isinstance(v, int) else escape(v, {'"': "&quot;"})


def _xy(point) -> tuple:
    return tuple(_mil(v) for v in point)


def _line(kind: str, a, b) -> str:
    return _el(kind, "line", x1=a[0], y1=a[1], x2=b[0], y2=b[1])


def _hookup(end, side: str, net, hookup: str) -> list:
    """The pin end ``end`` (mm)'s wire out to a label box, its no-connect
    cross, or nothing, as the layout decided."""
    x, y = _xy(end)
    if hookup == g.BARE:
        return []
    if hookup == g.NO_CONNECT:
        d = (f"M {x - NC_HALF} {y - NC_HALF} L {x + NC_HALF} {y + NC_HALF} "
             f"M {x - NC_HALF} {y + NC_HALF} L {x + NC_HALF} {y - NC_HALF}")
        return [_el("no-connect", "path", d=d)]
    lx, ly = _xy(g.label_at(end, side))
    text = _shown(net)
    width = len(text) * CHAR + 2 * LABEL_PAD
    box = lx if side == "right" else lx - width
    tx = lx + LABEL_PAD if side == "right" else lx - LABEL_PAD
    return [_line("wire", (x, y), (lx, ly)),
            _el(f"label-{side}", "g", inner=(
                _tag("rect", x=box, y=ly - LABEL_HALF, width=width, height=2 * LABEL_HALF)
                + _tag("text", text, x=tx, y=ly)))]


def _die(lay: "g.SheetLayout", d: "g.Die") -> list:
    b = lay.body(d)
    ox, oy = _xy(b.origin)
    hw, hh, pin_len = _mil(b.half_w), _mil(b.half_h), _mil(b.pin_len)
    ref_at, value_at = (_xy(p) for p in g.field_places(b.origin))
    out = [_el("symbol", "rect", x=ox - hw, y=oy - hh, width=2 * hw, height=2 * hh),
           _el("reference", "text", _shown(d.id), x=ref_at[0], y=ref_at[1]),
           _el("value", "text", _shown(d.top_cell), x=value_at[0], y=value_at[1])]
    for p in lay.die_pins(d):
        ex, ey = _xy(p.end)
        toward = pin_len if p.side == "left" else -pin_len    # pin end to body
        edge = ex + toward
        inset = _mil(g.NAME_OFFSET) if p.side == "left" else -_mil(g.NAME_OFFSET)
        out += [_line(f"pin-{p.side}", (ex, ey), (edge, ey)),
                _el("pin-name", "text", _shown(p.terminal.port), x=edge + inset, y=ey,
                    text_anchor="start" if p.side == "left" else "end"),
                _el("pin-number", "text", _shown(p.terminal.name), x=ex + toward // 2,
                    y=ey - NUMBER_RISE)]
        out += _hookup(p.end, p.side, p.net, p.hookup)
    return out


def _pad(p: "g.PadPlace") -> list:
    """A carrier pad as its EXT_PAD: the square body, its pad id above, its pin
    on the left. The pin's name and number, both "1", are not drawn."""
    x, y = _xy(p.at)
    ex, ey = _xy(p.end)
    half = _mil(g.PAD_HALF)
    ref_at, _value_at = (_xy(q) for q in g.field_places(p.at, g.PAD_HALF))
    return [_el("pad", "rect", x=x - half, y=y - half, width=2 * half, height=2 * half),
            _el("reference", "text", _shown(p.terminal.name), x=ref_at[0], y=ref_at[1]),
            _line("pin-left", (ex, ey), (x - half, ey))] + _hookup(p.end, "left", p.net,
                                                                  p.hookup)


def render(system: "g.System", base_sha256: str) -> str:
    """The connection sheet of ``system`` as SVG text, final newline included."""
    lay = g.sheet_layout(system)
    w, h = _mil(lay.page_w), _mil(lay.page_h)
    if w % SCALE or h % SCALE:
        raise ValueError(f"page {w} x {h} mil is not a whole number of px")
    margin = _mil(g.MARGIN)
    title_y = h - _mil(g.TITLE_BLOCK_H) + 2 * TEXT
    body = [f"<style>\n{STYLE}</style>",
            _el("sheet", "rect", x=0, y=0, width=w, height=h),
            _el("title", "text", _shown(system.name), x=margin, y=title_y),
            _el("title", "text", g.BASE_DIGEST_PREFIX + base_sha256, x=margin,
                y=title_y + 2 * TEXT)]
    for d in lay.row:
        body += _die(lay, d)
    for p in lay.pads():
        body += _pad(p)
    root = _tag("svg", inner="\n" + "\n".join(body) + "\n",
                xmlns="http://www.w3.org/2000/svg", data_form=FORM,
                width=w // SCALE, height=h // SCALE, viewBox=f"0 0 {w} {h}",
                font_family="monospace", font_size=TEXT)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + root + "\n"
