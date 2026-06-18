# Developer Guide

`gds2kicad` (the `gds_to_kicad` repo, [IHP-GmbH/gds2kicad](https://github.com/IHP-GmbH/gds2kicad), GPL-3.0-or-later) turns silicon-side artifacts into KiCad libraries and back: a die GDSII becomes a KiCad footprint (`.kicad_mod`) and symbol (`.kicad_sym`), and routed KiCad designs flow back into the chiplet assembly format. This guide is for people who want to work *on* the tools, not just run them, so it leans on how the pieces fit together rather than restating every flag (`--help` is authoritative for those).

## PDK-agnostic, by design

The single most important thing to understand: **the converter is not tied to any one process.** It reads layer name → (layer/datatype) mappings from a standard KLayout `.lyp` layer-properties file, which you point at with `--lyp-file`. Give it the `.lyp` for *your* technology and it works. IHP SG13G2 is a bundled example, not a requirement.

`pdks/` ships four ready `.lyp` files as examples and defaults:

- **`generic.lyp`**, the default. A hand-maintained, pads-only "black-box" vocabulary with just three entries: `outline.drawing` 206/0, `pad.drawing` 205/0, `pad.text` 205/25. It is what you fall back on when a chiplet GDS arrives with no PDK `.lyp` at all. Its numbers must mirror `adk/config/chiplet_pads.json`; a drift test (`tests/test_generic_lyp.py`) enforces that.
- **`interposer.lyp`**, upper-metal subset derived from SG13G2 (TopMetal2 = 134/0, Bump = 200/0, etc.). The most-used test fixture.
- **`sg13g2.lyp`**, the full IHP SG13G2 layer set.
- **`sky130.lyp`**, the full SkyWater sky130 set (note its source names carry a ` - L/D` suffix that the parser strips).

There is **no `--pdk` flag and no PDK auto-discovery.** You select a `.lyp` three ways: pass `--lyp-file <path>`, pick one in a GUI file dialog (which just opens in `pdks/` for convenience; it does not enumerate the directory), or omit the flag and get `generic.lyp`. No environment variable points at a PDK.

And you can bypass layer *names* entirely. The pad-layer resolver (`resolve_pad_layer` in `gds_to_kicad.py`, shared with the symbol tool) has a fixed precedence:

1. `--pad-layer-number N/D` (e.g. `134/0`), raw GDS layer, ignores the `.lyp` completely.
2. `--layer NAME`, looked up in the `.lyp`.
3. Neither given → **densest-pad-layer auto-detect** (`PinExtractor.scan_gds_layers`), which scores `.drawing` layers that have matching text layers, prefers higher metal numbers, and falls back to whichever layer has the most box/polygon shapes.

So a closed-PDK chiplet GDS with no usable `.lyp` still converts: run it against `generic.lyp` and let auto-detect find the pads, or hand it the raw `N/D` numbers. Same story for text/pin-name layers via `--text-layer` / `--text-layer-number` / `--auto-text`.

`lyp_parser.py` (`LYPParser`) does the parsing, `<name>`/`<source>` per `<properties>` entry, stripping KLayout's `@N` cellview suffix and the sky130 ` - L/D` suffix. It silently drops any entry whose source isn't `int/int`, and it `sys.exit(1)`s on a missing or unparseable file. `--list-layers` dumps the parsed table and is the quickest way to confirm a new `.lyp` loads.

## The suite

Footprint conversion is one tool among several. The pieces:

**`gds_to_kicad.py`**, GDS → `.kicad_mod` footprint. Flattens the top cell one level, extracts box and polygon shapes from the resolved pad layer, associates text labels as pad names, and emits pads, traceability properties (`GDS_FILE`, `LYP_FILE`, `GDS_LAYER`, `ORIENTATION`), and an `F.CrtYd` courtyard. **Polygon pads are fully supported**; non-rectangular pads come out as `(pad ... smd custom)` with a `gr_poly` primitive (the pad's `(size ...)` is the bbox, the true shape lives in the primitive). `--flip-chip` mirrors X for face-down dies. It also drives a human-in-the-loop pad-review workflow (below).

```
python3 gds_to_kicad.py die.gds -o die.kicad_mod --layer TopMetal2.drawing --lyp-file pdks/sg13g2.lyp
python3 gds_to_kicad.py blackbox.gds -o die.kicad_mod          # default generic.lyp + auto-detect
python3 gds_to_kicad.py blackbox.gds --pad-layer-number 134/0  # no .lyp names needed
```

**`gds_to_kicad_symbol.py`**, GDS → `.kicad_sym` symbol library (KiCad 6+ S-expression). `symbol_layout.py` auto-arranges pins (power top/bottom, signals left/right) and `kicad_sym_writer.py` emits the file. It uses `--pad-layer` / `--text-layer` (names) or `--pad-layer-number` / `--text-layer-number` (raw), shares the same `resolve_pad_layer` precedence, and adds `--symbol-name`, `--footprint-ref`, and `--max-text-distance` (a DBU cutoff for text-to-pad association; this flag lives **only** on the symbol tool, not the footprint tool). It supports the same two-step human-in-the-loop split: `--extract-pins OUT.json` writes an editable pin list, `--from-pin-list IN.json` rebuilds the symbol from it with no GDS needed.

```
python3 gds_to_kicad_symbol.py die.gds -o die.kicad_sym --pad-layer TopMetal2.drawing
python3 gds_to_kicad_symbol.py die.gds --extract-pins pins.json   # edit, then:
python3 gds_to_kicad_symbol.py --from-pin-list pins.json -o die.kicad_sym
```

**`blackbox_chiplet.py`**, black-box chiplet generator. A **standalone** tool (not a flag on the converters) for chiplets from commercial/closed nodes where you have only the pad map and no real GDS or `.lyp`. It synthesizes a minimal GDS from a pad spec, die outline plus metal pad boxes plus pad-name text, stamped on the ADK canonical generic layers (pad metal 205/0, pad text 205/25, outline 206/0) so the result auto-detects cleanly through the converters above. It also writes a `<stem>.boundaries.json` sidecar carrying the die outline as the chiplet boundary for ADK assembly DRC (it never stamps the boundary onto a fabrication layer).

```
python3 blackbox_chiplet.py pads.json -o chiplet.gds [--no-manifest] [--adk-root PATH]
python3 blackbox_chiplet.py pads.csv  -o chiplet.gds   # CSV: name,x_um,y_um,w_um,h_um
```

The spec is JSON or CSV (by suffix). Pad coordinates are **centers**; `w_um`/`h_um` are full extents. The `die` key is optional; without it the outline derives from pad extents plus a 50 µm margin. Canonical layer numbers come from `<ADK>/config/chiplet_pads.json` when an ADK checkout is found (via `--adk-root`, then `$ADK_ROOT`, then an upward sibling-dir walk for `adk`/`ADK`); otherwise it uses hardcoded fallbacks (same numbers) and warns. The boundary-manifest schema (`adk-boundary-manifest`, version `1.0.0`) is version-pinned and matched exactly by ADK readers; bump producer and readers together.

**`kicad_netlist_to_chiplet.py`**, KiCad netlist (`.net`, S-expression) → chiplet-flow YAML and/or CSV, or injected directly into a `.chiplet` file (`--inject`). It filters unconnected nets, classifies each net by name pattern (power/ground regexes; everything else is `signal`; pin types are deliberately ignored because GDS-extracted symbols tend to mark every pin `power_in`), and flags I/O-pad nets `external: true`. I/O pads are detected by footprint library prefix (`--io-pad-lib`, default `io_pads`) or ref-designator prefix (`--external-ref-prefix`). `--inject` does textual regex surgery on the `.chiplet`, not real YAML parsing, so keep `netlist:` as the last top-level block and back up first.

**`footprint_to_pinlist.py`**, `.kicad_mod` footprints → PinList JSON (pad name + center/size in DBU; Y-negated for GDS Y-up). Single or batch. Note it hardcodes pin `type=passive` and `side=left`.

**`io_pads/`**, interposer external-I/O-pad helpers. `generate_io_pad.py` emits a parametric KiCad symbol + footprint per pad class/size, tagging the footprint with `IO_CLASS` and `IO_PAD_SIZE_UM` properties. `kicad_pcb_to_iopads.py` walks a routed `.kicad_pcb`, keeps footprints carrying an `IO_CLASS` property, and writes a sidecar JSON of pad locations/sizes/nets (mm→µm, Y negated). Only `wire_bond` is implemented; `flipped_bump` and `tsv_bump` parse as choices but are rejected at generation time with exit code 2. (`kicad_pcb_to_iopads.py` mentions a downstream `hyp_to_gds.py`, which lives in a sibling repo, not here.)

```
python3 io_pads/generate_io_pad.py --io-class wire_bond --size 100x100
python3 io_pads/kicad_pcb_to_iopads.py design.kicad_pcb -o io_pads.json
```

**GUIs**, `unified_gui.py` is the main PyQt6 front end: a five-tab workflow (Extract Pins → Pin List Editor → Symbol Designer with a live painter preview → Footprint Generator with a flip-chip option → History). `gds_to_kicad_gui.py` and `gds_to_kicad_symbol_gui.py` are the standalone footprint and symbol GUIs. All three wrap the same engine classes and add a JSON conversion-history registry. The GUIs only do name-based layer resolution; they do not expose `--pad-layer-number`, `--design-dir`, `--dbu`, or the pad-review flags.

```
python3 unified_gui.py
```

## How a footprint conversion actually works

The footprint path is the one most people extend, so it's worth knowing the shape of it:

1. Read the GDS, flatten the top cell **one level** (`flatten(1)`, not a full recursive flatten; this is intentional and appears in every extraction path).
2. Resolve the pad layer by the precedence above; read box shapes (`shape.box`) and polygon shapes (`shape.polygon`, hull via `each_point_hull()`).
3. Read text shapes from the resolved/auto-detected text layer(s) and associate each to the nearest pad. The pad center for both rect and polygon pads is the bbox center.
4. Convert coordinates: GDS Y-up → KiCad Y-down by negating Y; flip-chip additionally mirrors X. DBU comes from `layout.dbu` unless `--dbu` overrides it.
5. Write the footprint. Output path resolves as `-o/--output` > `--design-dir/<dir>.pretty/<stem>.kicad_mod` > `generated_kicad_footprint_files/<stem>.kicad_mod`. The symbol tool mirrors this with `generated_kicad_symbol_files/`. The base directory is the CWD, overridable with `GDS_TO_KICAD_DATA_DIR` (the **only** env var the repo's own runtime reads; do not invent a `GDS_TO_KICAD_ROOT`; that's an ADK-side discovery variable, never referenced here).

The pad dict shape (`bbox` / `is_polygon` / `polygon_points`) is shared across `gds_to_kicad.py` (`_extract_pads` + `_generate_kicad_footprint`), `pin_extractor.py` (`PadInfo.from_polygon`), and `pad_review.py`. Change it in one place and you have to change all three.

### Pad review (human-in-the-loop)

Real dies carry routing, fill, and guard rings on the pad layer, not just bond pads. `pad_review.py` handles that. `--generate-pad-review OUT.gds` strips the source down to pad-layer shapes plus text labels; you open it in KLayout (`klayout -e -l <lyp> <gds>`), delete everything that isn't a real pad, then `--from-pad-review EDITED.gds --pin-list pins.json` reads the survivors back (preserving polygons) and assigns names by nearest-neighbor against the authoritative pin list.

## KLayout API notes that bite people

The geometry tooling is KLayout's `klayout.db` (imported as `db`; the tools `sys.exit` with a clear message if it's not on `PYTHONPATH`). Two things that cost time:

- **Shape accessors are properties, not methods.** `shape.box`, `shape.text`, `shape.polygon`; calling them (`shape.box()`) raises `TypeError: 'Box' object is not callable`. The exception is `polygon.bbox()`, which *is* a method.
- **Iterate with `for shape in cell.shapes(layer_idx).each():`** and branch on `shape.is_box()` / `is_polygon()` / `is_text()`.

## Environment and tests

Runtime deps: **KLayout** (the `klayout` Python module, `pip install klayout`, or a system install) for GDS reading, and **PyQt6 ≥ 6.6.0** for the GUIs. `PyYAML` is used on the netlist/test path. Minimal setup:

```
pip install -r requirements.txt pytest klayout PyYAML
```

There is a real pytest suite under `tests/` (~21 files, including `test_integration`, `test_polygon_pads`, `test_flip_chip`, `test_pin_extractor`, `test_dbu_detection`, `test_pad_review`, `test_blackbox_chiplet`, `test_symbol_blackbox`, `test_netlist_converter`, `test_io_pads`). Run it headless; the GUI imports need an offscreen Qt platform:

```
QT_QPA_PLATFORM=offscreen pytest tests -q
```

CI runs exactly this on every push and pull request (`.github/workflows/tests.yml`, Python 3.11 on ubuntu-latest, same offscreen Qt and the install line above). So a normal fork-and-PR workflow applies: branch, make the change, keep `pytest tests -q` green.

Two helpers worth knowing while developing: `--generate-test-gds` writes a small fixture (`tests/test_footprint.gds` for the footprint tool, `tests/test_symbol.gds` for the symbol tool), and `--scan-layers` reports pad/text-layer candidates for any GDS, useful even when you have no good `.lyp`.

## External references

- [KLayout Python API](https://www.klayout.de/doc/code/index.html)
- [KiCad file formats](https://dev-docs.kicad.org/en/file-formats/)
- [IHP Open PDK](https://github.com/IHP-GmbH/IHP-Open-PDK)
