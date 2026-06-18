# GDS to KiCad

A toolkit for turning a chiplet's GDSII layout into the KiCad artifacts you need to design with it: footprints (`.kicad_mod`), schematic symbols (`.kicad_sym`), pin lists, netlists, and the I/O-pad parts for an interposer. It pulls bond-pad geometry and pin-name text straight out of the GDS so the KiCad parts match the silicon.

## PDK-agnostic

The converter works with **any** technology, not just one PDK. You tell it which layers carry the pads and pin names by pointing it at that technology's KLayout `.lyp` layer-properties file (`--lyp-file`), and it reads the layer numbers from there. IHP SG13G2 is a convenient example, not a requirement.

`pdks/` ships four ready-to-use `.lyp` files:

- `generic.lyp` — a minimal pads-only vocabulary (pad metal `205/0`, pad text `205/25`, outline `206/0`). This is the default when you don't pass `--lyp-file`, meant for a closed chiplet GDS that ships no PDK file.
- `sg13g2.lyp` — the full IHP SG13G2 layer set.
- `sky130.lyp` — the full SkyWater sky130 layer set.
- `interposer.lyp` — upper-metal/bump layers derived from SG13G2, used as the test fixture.

For any other process, hand it your own `.lyp`. You can also skip layer names entirely and give raw GDS layer numbers as `N/D` (e.g. `134/0`), or let the tool auto-detect the densest pad layer — so a GDS with no usable `.lyp` at all still converts.

## Install

You need Python 3 and the KLayout Python module (the converters read GDS through it). The supported install is via pip:

```bash
pip install klayout
pip install -r requirements.txt   # PyQt6, for the GUIs
```

A system KLayout install with its Python bindings on `PYTHONPATH` also works.

## Quickstart: GDS to footprint

```bash
python3 gds_to_kicad.py input.gds -o output.kicad_mod
```

With the default `generic.lyp`, the densest pad layer is auto-detected and pin names are read from any text it finds nearby. To be explicit about a real PDK:

```bash
python3 gds_to_kicad.py input.gds --lyp-file pdks/sg13g2.lyp \
    --layer TopMetal2.drawing --text-layer TopMetal2.text -o output.kicad_mod
```

Or bypass layer names and give raw `N/D` numbers — useful for a black-box GDS with no named layers:

```bash
python3 gds_to_kicad.py input.gds --pad-layer-number 134/0 --text-layer-number 134/25
```

Useful helpers before a real run: `--list-layers` prints every layer the `.lyp` parsed; `--scan-layers` inspects the GDS and suggests the best pad/text layers.

Without `-o`, the footprint lands in `generated_kicad_footprint_files/<stem>.kicad_mod`; with `--design-dir DIR` it goes to `DIR/<design>.pretty/`. Rectangular pads are emitted as `smd rect`; non-rectangular pads become true `smd custom` pads with a `gr_poly` primitive, so the real shape is preserved. `--flip-chip` mirrors X for a face-down die.

For a curated run, generate a pad-review GDS (`--generate-pad-review out.gds`), strip routing and fills down to the real bond pads in KLayout, then rebuild from it (`--from-pad-review edited.gds --pin-list pins.json`).

## The rest of the suite

**GDS to symbol** — `gds_to_kicad_symbol.py` reads the same pad/text geometry and writes a KiCad 6+ `.kicad_sym`, auto-arranging pins (power top/bottom, signals left/right). Same layer model: `--lyp-file` + `--pad-layer`/`--text-layer`, or raw `--pad-layer-number`/`--text-layer-number`. `--extract-pins out.json` dumps an editable pin list; `--from-pin-list pins.json` regenerates the symbol from it, no GDS needed.

```bash
python3 gds_to_kicad_symbol.py input.gds --lyp-file pdks/sg13g2.lyp \
    --pad-layer TopMetal2.drawing -o out.kicad_sym
```

**Black-box chiplet** — when you only have a pad map (no GDS, no PDK), `blackbox_chiplet.py` synthesizes a minimal chiplet GDS from a pad spec: die outline plus pad boxes and name labels on the canonical generic layers (`205/0`, `205/25`, `206/0`), which the converters above auto-detect cleanly. The spec is JSON or CSV (chosen by suffix); each pad gives a name, center, and size.

```bash
python3 blackbox_chiplet.py pads.json -o chiplet.gds
```

It also writes a sidecar `<stem>.boundaries.json` manifest carrying the die outline as the chiplet boundary (suppress with `--no-manifest`). `--adk-root PATH` points at an ADK checkout for canonical layer numbers; without one it falls back to the same hardcoded numbers and warns.

**Netlist to chiplet** — `kicad_netlist_to_chiplet.py` converts a KiCad S-expression netlist (`.net`) into chiplet-flow YAML and/or CSV, or injects a netlist section into an existing `.chiplet` file. Nets are classified power/ground/signal by name. Pass at least one of `--yaml`, `--csv`, `--inject`, or `--summary`. I/O-pad nets are flagged `external` by footprint library (`--io-pad-lib`, default `io_pads`) or ref prefix (`--external-ref-prefix`).

**Footprint to pin list** — `footprint_to_pinlist.py` extracts pad name/center/size from one or more `.kicad_mod` files into a PinList JSON. Single-file or batch (`--output-dir`); `--dbu` sets the unit (default `0.001`, IHP).

**I/O pads** (`io_pads/`) — `generate_io_pad.py` emits a parametric symbol+footprint for an external interposer pad, tagged with `IO_CLASS` and `IO_PAD_SIZE_UM` properties (`--io-class wire_bond --size 100x100`; only `wire_bond` is implemented today). `kicad_pcb_to_iopads.py` walks a routed `.kicad_pcb`, keeps footprints carrying `IO_CLASS`, and writes a sidecar `io_pads.json` of pad locations/sizes/nets (mm→um, Y negated for the GDS Y-up convention).

**Unified GUI** — `unified_gui.py` is a PyQt6 front-end over the same engine, with tabs for pin extraction, pin-list editing, symbol design with a live preview, footprint generation, and a conversion history. Two focused GUIs also exist: `gds_to_kicad_gui.py` and `gds_to_kicad_symbol_gui.py`. Run headless with `QT_QPA_PLATFORM=offscreen`.

```bash
python3 unified_gui.py
```

## Project

Part of [IHP-GmbH/gds2kicad](https://github.com/IHP-GmbH/gds2kicad). Licensed under GPL-3.0-or-later (see `LICENSE`). For build/test workflow and internals, see `docs/DEVELOPER_GUIDE.md`.
