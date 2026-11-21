# SPDX-License-Identifier: GPL-3.0-or-later
"""chiplet_to_kicad_sch --svg: the connection sheet, the system sheet drawn as
an SVG, form connection-sheet/1.

The assembly review signs these bytes raw, so they depend only on the design
and this code. Two goldens (tests/data/connection_sheet) are compared byte for
byte: the draft_pair system of tests/fixtures (three dies on a carrier with
wire-bond pads) and carrier_system/, the unit tests' carrier system
(test_chiplet_to_kicad_sch CARRIER_CHIPLET and PINS) frozen as files. Between
them they hold every element kind the renderer draws (the coverage case). Each
byte rule has a check, run on both goldens, and shown to fail on a planted SVG
that breaks that rule alone.

A change to a golden's bytes moves the assembly digest of every design whose
drawing it changes: it needs a revision of the connection-sheet/1 note before
the golden is rewritten. Rewrite one with the command of _render, --svg only.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chiplet_to_kicad_sch as g  # noqa: E402
import sexpr_io as sx  # noqa: E402
from tests import test_chiplet_to_kicad_sch as gt  # noqa: E402
from tests import test_cosim_setup_sheets as ct  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(__file__).resolve().parent / "data" / "connection_sheet"
PAIR = Path(__file__).resolve().parent / "fixtures" / "draft_pair"
SMALL = DATA / "carrier_system"
# name -> (.chiplet, {die: pin list})
SYSTEMS = {
    "draft_pair": (PAIR / "design.chiplet",
                   {d: PAIR / f"{d}.pins.json" for d in ("U1", "U2", "U3")}),
    "carrier_system": (SMALL / "design.chiplet",
                       {d: SMALL / f"{d}.pins.json" for d in ("U1", "U2")}),
}
NS = "{http://www.w3.org/2000/svg}"
MIL = 0.0254        # the sheet's unit, mm


def _golden(name):
    return DATA / f"{name}.connection-sheet.svg"


def _argv(tmp_path, name, chiplet=None, pins=None, out=False, extra=()):
    """(argv, svg path, .kicad_sch path, report path) of a run on system
    ``name`` (or the given inputs) with --svg, and --out too when ``out``."""
    cpath, pin_paths = SYSTEMS[name] if chiplet is None else (chiplet, pins)
    svg, sch, report = (tmp_path / "pnr" / "sys.connection-sheet.svg",
                        tmp_path / "pnr" / "sys.draft.kicad_sch",
                        tmp_path / "pnr" / "schematic.json")
    argv = ["--chiplet", str(cpath), "--svg", str(svg), "--report", str(report),
            *(["--out", str(sch)] if out else []), *extra]
    for inst, p in sorted(pin_paths.items()):
        argv += ["--pins", f"{inst}={p}"]
    return argv, svg, sch, report


def _render(tmp_path, name, chiplet=None, pins=None, out=False, extra=()):
    """Run the CLI in process; returns (exit, svg path, .kicad_sch path, report)."""
    argv, svg, sch, report = _argv(tmp_path, name, chiplet, pins, out, extra)
    code = g.main(argv)
    return code, svg, sch, json.loads(report.read_text(encoding="utf-8"))


# ----------------------------------------------------------------- the goldens

@pytest.mark.parametrize("name", sorted(SYSTEMS))
def test_the_sheet_is_the_golden_byte_for_byte(tmp_path, name):
    code, svg, _sch, report = _render(tmp_path, name)
    assert code == g.EXIT_WRITTEN and report["word"] == "written"
    assert svg.read_bytes() == _golden(name).read_bytes()


@pytest.mark.parametrize("name", sorted(SYSTEMS))
def test_another_folder_and_other_input_paths_draw_the_same_bytes(tmp_path, name, monkeypatch):
    """The inputs copied to other absolute paths under other names, the run in
    another folder: the same bytes."""
    cpath, pins = SYSTEMS[name]
    elsewhere = tmp_path / "elsewhere" / "deeper"
    elsewhere.mkdir(parents=True)
    copied = {d: Path(shutil.copy(p, elsewhere / f"other_{d}.json")) for d, p in pins.items()}
    chiplet = Path(shutil.copy(cpath, elsewhere / "renamed.chiplet"))
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.chdir(run)
    code, svg, _sch, _report = _render(run, name, chiplet=chiplet, pins=copied)
    assert code == g.EXIT_WRITTEN
    assert svg.read_bytes() == _golden(name).read_bytes()


@pytest.mark.parametrize("name", sorted(SYSTEMS))
def test_the_sheet_drawn_beside_the_kicad_sch_is_the_same(tmp_path, name):
    code, svg, sch, report = _render(tmp_path, name, out=True)
    assert code == g.EXIT_WRITTEN and sch.is_file()
    assert svg.read_bytes() == _golden(name).read_bytes()


def test_run_as_a_script_it_draws_the_same_bytes(tmp_path):
    """As packaging-flow runs it: the tool as a script, so the renderer loads
    its own copy of chiplet_to_kicad_sch beside __main__."""
    argv, svg, _sch, _report = _argv(tmp_path, "carrier_system")
    run = subprocess.run([sys.executable, str(ROOT / "chiplet_to_kicad_sch.py"), *argv],
                         cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert run.returncode == g.EXIT_WRITTEN, run.stderr
    assert svg.read_bytes() == _golden("carrier_system").read_bytes()


def test_the_setup_band_is_drawn_on_the_kicad_sch_and_not_on_the_svg(tmp_path):
    """With --setup the .kicad_sch carries the setup band; the SVG drawn beside
    it is byte for byte the one drawn without it."""
    cpath, pins = _refused_inputs(tmp_path, gt.CHIPLET, gt.PINS)
    setup = ["--setup", str(ct._write_projection(tmp_path / "in")),
             "--setup-spec", str(ct.SPEC)]
    code, svg, sch, _report = _render(tmp_path / "a", None, chiplet=cpath, pins=pins,
                                      out=True, extra=setup)
    assert code == g.EXIT_WRITTEN
    assert g.SETUP_MARKER in sch.read_text(encoding="utf-8")
    code, alone, _sch, _report = _render(tmp_path / "b", None, chiplet=cpath, pins=pins)
    assert code == g.EXIT_WRITTEN
    assert svg.read_bytes() == alone.read_bytes()
    assert g.SETUP_MARKER not in svg.read_text(encoding="utf-8")


def test_svg_without_out_writes_the_sheet_alone(tmp_path):
    code, svg, sch, report = _render(tmp_path, "carrier_system")
    assert code == g.EXIT_WRITTEN and report["word"] == "written"
    assert sorted(p.name for p in svg.parent.iterdir()) == sorted([svg.name, "schematic.json"])
    assert report["schematic"] is None and report["check"] == {"word": "not_run"}


# ----------------------------------------------------------------- refusals

def _refused_inputs(tmp_path, chiplet, pins):
    cpath, paths = gt._inputs(tmp_path / "in", chiplet=chiplet, pins=pins)
    return cpath, {k: Path(v) for k, v in paths.items()}


def _refusal_cases():
    cases = {f"system-{code}": gt._mutated(gt.REFUSALS[code]) for code in sorted(gt.REFUSALS)}
    for case in sorted(gt.CARRIER_REFUSALS):
        chiplet = gt.copy.deepcopy(gt.CARRIER_CHIPLET)
        gt.CARRIER_REFUSALS[case][1](chiplet)
        cases[f"carrier-{case}"] = (chiplet, gt.copy.deepcopy(gt.PINS))
    return cases


REFUSED = _refusal_cases()


@pytest.mark.parametrize("out", [False, True], ids=["svg-only", "with-out"])
@pytest.mark.parametrize("case", sorted(REFUSED))
def test_a_refused_system_writes_no_svg_and_exits_3(tmp_path, case, out):
    cpath, pins = _refused_inputs(tmp_path, *REFUSED[case])
    code, svg, sch, report = _render(tmp_path, None, chiplet=cpath, pins=pins, out=out)
    assert code == g.EXIT_REFUSED and report["word"] == "refused" and report["refusals"]
    assert not svg.exists() and not sch.exists()


def test_an_unreadable_pin_list_writes_no_svg_and_exits_3(tmp_path):
    cpath, pins = _refused_inputs(tmp_path, gt.CHIPLET, gt.PINS)
    pins["U2"].write_text("{not json", encoding="utf-8")
    code, svg, _sch, report = _render(tmp_path, None, chiplet=cpath, pins=pins)
    assert code == g.EXIT_REFUSED
    assert [r["code"] for r in report["refusals"]] == ["malformed_pin_list"]
    assert not svg.exists()


ALONE = "without --out, --svg draws the connection sheet alone: it takes no --setup or --check"
NO_SVG = "--setup-only draws no system sheet, so it takes no --svg"


@pytest.mark.parametrize("extra, message", [
    (["--check"], ALONE),
    (["--setup", "{projection}", "--setup-spec", "{spec}"], ALONE),
    (["--setup-only", "--setup", "{projection}", "--setup-spec", "{spec}"], NO_SVG),
], ids=["check", "setup", "setup-only"])
def test_svg_alone_takes_no_option_that_needs_the_kicad_sch(tmp_path, extra, message, capsys):
    projection = ct._write_projection(tmp_path / "in")
    extra = [a.format(projection=projection, spec=ct.SPEC) for a in extra]
    with pytest.raises(SystemExit) as exc:
        _render(tmp_path, "carrier_system", extra=extra)
    assert exc.value.code == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "pnr").exists()


@pytest.mark.parametrize("clash", ["out", "report"])
def test_the_svg_takes_no_path_of_another_output(tmp_path, clash, capsys):
    argv, svg, sch, report = _argv(tmp_path, "carrier_system", out=True)
    argv[argv.index("--svg") + 1] = str(sch if clash == "out" else report)
    with pytest.raises(SystemExit) as exc:
        g.main(argv)
    assert exc.value.code == 2
    assert "--svg names a file of its own, not the --out or the --report file" \
        in capsys.readouterr().err
    assert not (tmp_path / "pnr").exists()


def test_generate_draws_no_svg_with_setup_only_and_none_alone_with_setup(tmp_path):
    cpath, pins = _refused_inputs(tmp_path, gt.CHIPLET, gt.PINS)
    projection, svg = ct._write_projection(tmp_path / "in"), tmp_path / "pnr" / "s.svg"
    with pytest.raises(ValueError, match="no connection sheet"):
        g.generate(cpath, {}, tmp_path / "pnr" / "a.kicad_sch", setup_path=projection,
                   spec_path=ct.SPEC, setup_only=True, svg_path=svg)
    with pytest.raises(ValueError, match="only a connection sheet is drawn"):
        g.generate(cpath, pins, None, setup_path=projection, spec_path=ct.SPEC,
                   svg_path=svg)
    assert not (tmp_path / "pnr").exists()


def _with_port(port):
    pins = gt.copy.deepcopy(gt.PINS)
    pins["U1"][0]["port"] = port
    return pins


def _named(name):
    chiplet = gt.copy.deepcopy(gt.CHIPLET)
    chiplet["assembly"]["name"] = name
    return chiplet


# A text with a character XML does not allow: refused with --svg.
UNDRAWABLE = {
    "name-vt": (_named("a\x0bb"), gt.PINS, {"field": "assembly.name"}),
    "name-esc": (_named("a\x1bb"), gt.PINS, {"field": "assembly.name"}),
    "port-soh": (gt.CHIPLET, _with_port("\x01ab"), {"field": "port", "terminal": "A0"}),
    "port-nul": (gt.CHIPLET, _with_port("a\x000"), {"field": "port", "terminal": "A0"}),
    "port-ffff": (gt.CHIPLET, _with_port("a\uffff"), {"field": "port", "terminal": "A0"}),
    "port-surrogate": (gt.CHIPLET, _with_port("a\ud800"), {"field": "port", "terminal": "A0"}),
}


@pytest.mark.parametrize("out", [False, True], ids=["svg-only", "with-out"])
@pytest.mark.parametrize("case", sorted(UNDRAWABLE))
def test_a_text_no_svg_can_hold_is_refused_and_writes_nothing(tmp_path, case, out):
    chiplet, pins, where = UNDRAWABLE[case]
    cpath, paths = _refused_inputs(tmp_path, chiplet, pins)
    code, svg, sch, report = _render(tmp_path, None, chiplet=cpath, pins=paths, out=out)
    assert code == g.EXIT_REFUSED and report["word"] == "refused"
    assert [{k: r.get(k) for k in ("code", *where)} for r in report["refusals"]] == [
        {"code": "undrawable_text", **where}]
    assert not svg.exists() and not sch.exists()


def test_without_svg_such_a_text_still_draws_the_kicad_sch(tmp_path):
    code, out, _report = gt._cli(tmp_path, chiplet=_named("a\x0bb"), pins=_with_port("\x01ab"))
    assert code == g.EXIT_WRITTEN and out.is_file()


@pytest.mark.parametrize("port, shown", [("a\r0", "a 0"), ("x\ty\n", "x y"),
                                         ('"q" &', "'q' &"), ("p\\q", "p/q")])
def test_a_pin_name_is_drawn_as_kicad_shows_it(tmp_path, port, shown):
    """The .kicad_sch's substitutions (a quote, a backslash, a tab or line end)
    apply to the SVG too: the same text, and no CR in the file."""
    cpath, paths = _refused_inputs(tmp_path, gt.CHIPLET, _with_port(port))
    code, svg, sch, _report = _render(tmp_path, None, chiplet=cpath, pins=paths, out=True)
    assert code == g.EXIT_WRITTEN
    raw = svg.read_bytes()
    assert check_encoding(raw) == []
    names = [el.text for el in _svg_items(raw.decode("utf-8"))["pin-name"]]
    assert shown in names and port not in names
    assert f'(name "{shown}"' in sch.read_text(encoding="utf-8")


# ---------------------------------------- the sheet draws the .kicad_sch's model

def _mil(value):
    m = round(float(value) / MIL, 6)
    assert m == int(m), value
    return int(m)


def _svg_items(text):
    """class -> elements, every element of the drawing."""
    out = {}
    for el in ET.fromstring(text).iter():
        for kind in el.get("class", "").split():
            out.setdefault(kind, []).append(el)
    return out


def _ends(line):
    return {(int(line.get("x1")), int(line.get("y1"))), (int(line.get("x2")), int(line.get("y2")))}


def _centre(rect):
    return (int(rect.get("x")) + int(rect.get("width")) // 2,
            int(rect.get("y")) + int(rect.get("height")) // 2)


@pytest.mark.parametrize("name", sorted(SYSTEMS))
def test_the_svg_draws_what_the_kicad_sch_places_where_it_places_it(tmp_path, name):
    _code, svg, sch, _report = _render(tmp_path, name, out=True)
    tree = sx.parse(sch.read_text(encoding="utf-8"))
    items = _svg_items(svg.read_text(encoding="utf-8"))

    def at(node):
        a = sx.find(node, "at")
        return _mil(a[1]), _mil(a[2])

    def prop(node, key):
        return next((p for p in sx.find_all(node, "property") if str(p[1]) == key), None)

    def placed(els):
        return sorted((el.text, (int(el.get("x")), int(el.get("y")))) for el in els)

    dies = [s for s in sx.find_all(tree, "symbol")
            if str((prop(s, "Description") or [None] * 3)[2]) == "chiplet die"]
    pads = [s for s in sx.find_all(tree, "symbol") if g.EXT_PAD in str(sx.find(s, "lib_id")[1])]
    assert sorted(_centre(r) for r in items["symbol"]) == sorted(at(s) for s in dies)
    assert sorted(_centre(r) for r in items.get("pad", [])) == sorted(at(s) for s in pads)
    # The Reference and Value texts, where the .kicad_sch places them.
    assert placed(items["reference"]) == sorted(
        (str(prop(s, "Reference")[2]), at(prop(s, "Reference"))) for s in dies + pads)
    assert placed(items["value"]) == sorted(
        (str(prop(s, "Value")[2]), at(prop(s, "Value"))) for s in dies)
    wires = set().union(*(_ends(w) for w in items.get("wire", [])), set())
    assert wires == {(_mil(xy[1]), _mil(xy[2])) for w in sx.find_all(tree, "wire")
                     for xy in sx.find_all(sx.find(w, "pts"), "xy")}
    # Each label's text at its anchor: the box's end at the wire.
    boxes = []
    for kind in ("label-left", "label-right"):
        for el in items.get(kind, []):
            r = el.find(f"{NS}rect")
            x = int(r.get("x")) + (int(r.get("width")) if kind == "label-left" else 0)
            boxes.append((el.find(f"{NS}text").text, (x, _centre(r)[1])))
    assert sorted(boxes) == sorted((str(lb[1]), at(lb))
                                   for lb in sx.find_all(tree, "global_label"))
    flags = [[int(v) for v in p.get("d").split() if not v.isalpha()]
             for p in items.get("no-connect", [])]
    assert sorted(((a + c) // 2, (b + d) // 2) for a, b, c, d, *_ in flags) == sorted(
        at(nc) for nc in sx.find_all(tree, "no_connect"))
    # Every pin of every symbol, from its end to the body, and a die pin's
    # name and number: the library pins, placed by their instance.
    lib = {str(s[1]): s for s in sx.find_all(sx.find(tree, "lib_symbols"), "symbol")}
    lines, names, numbers = [], [], []
    for s in dies + pads:
        ox, oy = at(s)
        for unit in sx.find_all(lib[str(sx.find(s, "lib_id")[1])], "symbol"):
            for pin in sx.find_all(unit, "pin"):
                pa = sx.find(pin, "at")
                end = (ox + _mil(pa[1]), oy - _mil(pa[2]))
                reach = _mil(sx.find(pin, "length")[1]) * (1 if str(pa[3]) == "0" else -1)
                lines.append(sorted([end, (end[0] + reach, end[1])]))
                if s in dies:
                    names.append(str(sx.find(pin, "name")[1]))
                    numbers.append(str(sx.find(pin, "number")[1]))
    assert sorted(sorted(_ends(el)) for k in ("pin-left", "pin-right")
                  for el in items.get(k, [])) == sorted(lines)
    assert sorted(el.text for el in items["pin-name"]) == sorted(names)
    assert sorted(el.text for el in items["pin-number"]) == sorted(numbers)


# ------------------------------------------------------------ the byte rules
#
# Each check returns what breaks its rule, [] for none.

_TEXT_ATTRS = {"class", "data-form", "font-family", "text-anchor", "dominant-baseline",
               "xmlns"}
_NUMBER = re.compile(r"-?[0-9]+")
_NUMERIC_TOKEN = re.compile(r"[-+]?(?:[0-9]*\.[0-9]+|[0-9]+\.?)(?:[eE][-+]?[0-9]+)?")


def _root(text):
    return ET.fromstring(text)


def _attrs(text):
    for el in _root(text).iter():
        for key, value in el.attrib.items():
            yield el, key.rsplit("}", 1)[-1], key, value


def _style(text):
    """The style sheet, and every inline style attribute after it."""
    return "".join([el.text or "" for el in _root(text).iter(f"{NS}style")]
                   + [f";{v}" for _el, name, _k, v in _attrs(text) if name == "style"])


def _said(text):
    """Every text the file says outside its markup and style: text nodes,
    comments, processing instructions, and attribute values but the class."""
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
    root = ET.fromstring(text, parser=parser)
    out = []
    for el in root.iter():
        if el.tag is ET.Comment or el.tag is ET.ProcessingInstruction:
            out.append(el.text or "")
        elif el.tag != f"{NS}style":
            out.append(el.text or "")
        out += [v for k, v in el.attrib.items() if k != "class"]
    return out


def check_integers(text):
    """Every number in an attribute or the style sheet is an integer."""
    bad = []
    for _el, name, _key, value in _attrs(text):
        if name in _TEXT_ATTRS:
            continue
        bad += [f"{name}={value!r}" for tok in _NUMERIC_TOKEN.findall(value)
                if not _NUMBER.fullmatch(tok)]
    style = re.sub(r"#[0-9a-fA-F]{3,8}\b", "", _style(text))
    return bad + [f"style {tok!r}" for tok in _NUMERIC_TOKEN.findall(style)
                  if not _NUMBER.fullmatch(tok)]


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
# ISO date and time forms only: a bus slice such as D[15:00] is a net name.
_TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|T\d{2}:\d{2}|\b\d{2}:\d{2}:\d{2}\b")


def check_no_host_facts(text, paths=()):
    """No time, uuid, host path, input path, host name, tool name or version.
    Names are looked for in what the file says (``_said``), not in its class
    names or style sheet, and the host name only as a whole word."""
    said = _said(text)
    host = socket.gethostname()
    words = [str(p) for p in paths] + [g.GENERATOR, "gds2kicad", "gds_to_kicad",
                                        sys.version.split()[0], os.getcwd()]
    bad = (_UUID.findall(text) + [m.group(0) for m in _TIME.finditer(text)]
           + [w for w in words if w and any(w in t for t in said)])
    if host:
        whole = re.compile(rf"(?<![\w.-]){re.escape(host)}(?![\w.-])")
        bad += [host for t in said if whole.search(t)]
    return bad


def check_encoding(raw):
    """UTF-8, LF line ends, one final newline."""
    bad = []
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        bad.append(str(exc))
    if b"\r" in raw:
        bad.append("CR")
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        bad.append("not one final newline")
    return bad


def check_form(text):
    root = _root(text)
    return [] if root.tag == f"{NS}svg" and root.get("data-form") == "connection-sheet/1" \
        else [f"root {root.tag} data-form={root.get('data-form')!r}"]


def check_no_script(text):
    """No script element, no event attribute."""
    return ([el.tag for el in _root(text).iter() if el.tag.rsplit("}", 1)[-1] == "script"]
            + [key for _el, name, key, _v in _attrs(text) if name.lower().startswith("on")])


_URL = re.compile(r"url\(\s*(?![\s'\"]*#)[^)]*\)", re.I)


def check_no_outside_reference(text):
    """Only '#' fragments: no href elsewhere, no <image>, no @import, no url()
    but url(#...), and in the raw text no processing instruction but the XML
    declaration (an xml-stylesheet one loads CSS) and no DOCTYPE or ENTITY."""
    bad = [el.tag for el in _root(text).iter() if el.tag.rsplit("}", 1)[-1] == "image"]
    bad += [f"{name}={v!r}" for _el, name, _k, v in _attrs(text)
            if name == "href" and not v.startswith("#")]
    bad += [u for _el, _name, _k, v in _attrs(text) for u in _URL.findall(v)]
    style = _style(text)
    bad += _URL.findall(style) + re.findall(r"@import", style, re.I)
    declaration = 1 if text.startswith("<?xml ") else 0
    bad += re.findall(r"<\?[^\s?>]*", text)[declaration:]
    return bad + re.findall(r"<!(?:DOCTYPE|ENTITY)", text, re.I)


def check_generic_font(text):
    """The font is the generic family monospace, named once on the root, and
    nothing else names or loads a font."""
    fams = [v for _el, name, _k, v in _attrs(text) if name == "font-family"]
    bad = [] if fams == ["monospace"] and _root(text).get("font-family") == "monospace" \
        else [f"font-family {fams!r}"]
    style = _style(text)
    return bad + re.findall(r"font-family[^;}]*|@font-face|font:[^;}]*", style)


def check_text_is_text(text):
    """Text is <text>, at one fixed size (the root's): no foreignObject, no
    other size."""
    root = _root(text)
    bad = [el.tag for el in root.iter() if el.tag.rsplit("}", 1)[-1] in ("foreignObject", "tspan")]
    sizes = [v for _el, name, _k, v in _attrs(text) if name == "font-size"]
    if sizes != [root.get("font-size")] or not root.get("font-size"):
        bad.append(f"font-size {sizes!r}")
    return bad + re.findall(r"font-size[^;}]*", _style(text))


CHECKS = {"integers": check_integers, "host_facts": check_no_host_facts,
          "encoding": None, "form": check_form, "script": check_no_script,
          "outside_reference": check_no_outside_reference, "font": check_generic_font,
          "text": check_text_is_text}


def _run(rule, raw):
    return check_encoding(raw) if rule == "encoding" else CHECKS[rule](raw.decode("utf-8"))


@pytest.mark.parametrize("rule", sorted(CHECKS))
@pytest.mark.parametrize("name", sorted(SYSTEMS))
def test_the_golden_keeps_the_byte_rule(name, rule):
    assert _run(rule, _golden(name).read_bytes()) == []


@pytest.mark.parametrize("rule", sorted(CHECKS))
@pytest.mark.parametrize("name", sorted(SYSTEMS))
def test_a_fresh_sheet_keeps_the_byte_rule(tmp_path, name, rule):
    """The rules hold for what the code draws now, not only for the goldens."""
    _code, svg, _sch, _report = _render(tmp_path, name)
    assert _run(rule, svg.read_bytes()) == []


@pytest.mark.parametrize("name", sorted(SYSTEMS))
def test_a_fresh_sheet_names_no_path_of_its_run(tmp_path, name):
    cpath, pins = SYSTEMS[name]
    _code, svg, _sch, _report = _render(tmp_path, name)
    paths = [tmp_path, svg, cpath, cpath.parent, *pins.values()]
    assert check_no_host_facts(svg.read_text(encoding="utf-8"), paths) == []


def test_host_facts_are_words_the_file_says_not_its_markup(monkeypatch):
    """A host named like a class, and a bus slice in a net label, are no time
    and no host fact; the host's name said in a text is."""
    raw = _golden("carrier_system").read_text(encoding="utf-8")
    monkeypatch.setattr(socket, "gethostname", lambda: "wire")
    assert check_no_host_facts(raw) == []
    assert check_no_host_facts(raw.replace(
        "</svg>", '<g class="label-right"><text>D[15:00]</text></g>\n</svg>')) == []
    assert check_no_host_facts(raw.replace(
        "</svg>", '<text class="title">drawn on wire</text>\n</svg>')) == ["wire"]


def _plant(raw, old, new):
    assert raw.count(old) >= 1, old
    return raw.replace(old, new, 1)


# rule -> a golden with that rule, and only that rule, broken.
PLANTED = {
    "integers": lambda r: re.sub(rb'x="(\d+)"', rb'x="\1.5"', r, count=1),
    "host_facts": lambda r: _plant(r, b"</svg>", b"<!-- 2026-11-20 -->\n</svg>"),
    "encoding": lambda r: r.replace(b"\n", b"\r\n"),
    "form": lambda r: _plant(r, b'data-form="connection-sheet/1"', b'data-form="connection-sheet/2"'),
    "script": lambda r: _plant(r, b'<svg ', b'<svg onload="alert(1)" '),
    "outside_reference": lambda r: _plant(
        r, b"</svg>", b'<image href="https://example.org/a.png"/>\n</svg>'),
    # The usual way a standalone SVG loads outside CSS; the parser drops it.
    "outside_reference_stylesheet": lambda r: _plant(
        r, b"?>\n", b'?>\n<?xml-stylesheet href="https://example.org/a.css"?>\n'),
    "font": lambda r: _plant(r, b'font-family="monospace"', b'font-family="DejaVu Sans Mono"'),
    "font_inline_style": lambda r: _plant(
        r, b'class="sheet"', b'class="sheet" style="font-family:Arial"'),
    "text": lambda r: _plant(r, b"</svg>", b'<text font-size="12">x</text>\n</svg>'),
    "text_inline_style": lambda r: _plant(
        r, b'class="sheet"', b'class="sheet" style="font-size:80px"'),
}


def _rule(planted):
    """The rule a planted break breaks: its name up to a variant suffix."""
    return next(rule for rule in sorted(CHECKS, key=len, reverse=True)
                if planted == rule or planted.startswith(rule + "_"))


def test_every_rule_has_a_planted_break():
    assert sorted({_rule(p) for p in PLANTED}) == sorted(CHECKS)


@pytest.mark.parametrize("planted_case", sorted(PLANTED))
def test_each_check_fails_on_a_sheet_that_breaks_its_rule_alone(planted_case):
    rule = _rule(planted_case)
    planted = PLANTED[planted_case](_golden("carrier_system").read_bytes())
    assert _run(rule, planted) != []
    others = {other: _run(other, planted) for other in CHECKS if other != rule}
    assert others == {other: [] for other in others}


def test_a_script_element_and_an_outside_url_are_caught_too():
    raw = _golden("carrier_system").read_bytes().decode("utf-8")
    assert check_no_script(raw.replace("</svg>", "<script>x()</script>\n</svg>"))
    assert check_no_outside_reference(raw.replace("</style>", "@import 'a.css';</style>"))
    assert check_no_outside_reference(raw.replace("</style>", "@IMPORT 'a.css';</style>"))
    assert check_no_outside_reference(raw.replace(
        'class="sheet"', 'class="sheet" fill="url(https://example.org/p)"'))
    assert check_no_outside_reference(raw.replace(
        'class="sheet"', 'class="sheet" fill="URL(https://example.org/p)"'))
    assert check_no_outside_reference(raw.replace(
        'class="sheet"', 'class="sheet" style="fill:url(https://example.org/p)"'))
    assert check_no_outside_reference(raw.replace(
        "?>\n", '?>\n<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" '
        '"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">\n', 1))
    assert check_no_outside_reference(raw.replace(
        'class="sheet"', 'class="sheet" xlink:href="b.svg" xmlns:xlink="http://www.w3.org/1999/xlink"'))
    assert check_no_outside_reference(raw.replace(
        'class="sheet"', 'class="sheet" fill="url(#p)"')) == []


# ------------------------------------------------------------------ coverage

def _missing(kinds):
    """The kinds with no instance in either golden."""
    drawn = set()
    for name in SYSTEMS:
        drawn |= set(_svg_items(_golden(name).read_text(encoding="utf-8")))
    return [k for k in kinds if k not in drawn]


def test_every_kind_the_renderer_draws_is_in_a_golden():
    import connection_sheet_svg as cs
    assert _missing(cs.KINDS) == []


def test_the_goldens_draw_no_kind_the_renderer_does_not_list():
    import connection_sheet_svg as cs
    for name in SYSTEMS:
        assert set(_svg_items(_golden(name).read_text(encoding="utf-8"))) <= set(cs.KINDS)


def test_a_kind_added_with_no_instance_is_caught():
    import connection_sheet_svg as cs
    assert _missing(cs.KINDS + ("bus-entry",)) == ["bus-entry"]


def _unclassed(text):
    """The elements outside the kinds: not the root, the style sheet or a
    label's box and text, and with no class of KINDS."""
    import connection_sheet_svg as cs
    root = _root(text)
    known = {id(root)} | {id(el) for el in root.iter(f"{NS}style")}
    for group in root.iter(f"{NS}g"):
        if group.get("class") in ("label-left", "label-right"):
            known |= {id(child) for child in group}
    return [el.tag for el in root.iter() if id(el) not in known
            and el.get("class") not in cs.KINDS]


@pytest.mark.parametrize("name", sorted(SYSTEMS))
def test_every_element_of_a_golden_is_of_a_listed_kind(name):
    assert _unclassed(_golden(name).read_text(encoding="utf-8")) == []


def test_an_element_of_no_listed_kind_is_caught():
    raw = _golden("carrier_system").read_text(encoding="utf-8")
    assert _unclassed(raw.replace("</svg>", '<circle cx="0" cy="0" r="5"/>\n</svg>')) == [
        f"{NS}circle"]
