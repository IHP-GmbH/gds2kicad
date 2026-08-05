# gds2kicad

Turn a die or chiplet GDSII into the KiCad parts you need to design a board or
an interposer around it: footprints, schematic symbols, pin lists, and the
netlist that flows back into the chiplet assembly format.

KiCad cannot read GDS. The usual fallback is redrawing bond pads by hand from a
datasheet and typing pin names into a symbol cell by cell, then doing it again
every time the layout changes. gds2kicad reads the pad geometry and the text
labels straight out of the layout, so the KiCad part matches the silicon.

## Status

> [!WARNING]
> gds2kicad is currently a preview release only!

**Contents:** [Install](#install) - [Quick start](#quick-start) -
[Tools](#tools) - [Selecting layers](#selecting-layers) -
[Workflows](#workflows) - [Where files are written](#where-files-are-written) -
[Troubleshooting](#troubleshooting) - [Documentation](#documentation)

## Install

There is nothing to build and nothing to install: run the scripts from the
clone root. You need KLayout (reads the GDS) and PyQt6 (for the GUIs).

```sh
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt    # PyQt6
pip install klayout                # GDS reader
```

A system KLayout install with its Python bindings on `PYTHONPATH` works too. If
`import klayout.db` fails, the converters exit with a message saying so.

Python 3.11 is the version CI runs. On a slim Linux box or container, PyQt6 also
needs `libgl1 libegl1 libxkbcommon0 libdbus-1-3 libfontconfig1 libglib2.0-0`, or
the GUIs die on a Qt platform-plugin error. To run the tests you additionally
need `pip install pytest PyYAML`.

## Quick start

No GDS at hand? Both converters can write themselves a small one, which is the
quickest way to check the install:

```sh
python3 gds_to_kicad.py --generate-test-gds     # writes tests/test_footprint.gds
python3 gds_to_kicad.py tests/test_footprint.gds -o demo.kicad_mod
```

That gives you 3 pads. They come out numbered, not named, because the fixture
carries no text labels; a real GDS does.

### With the GUI

The simplest way in, and it drives the same engine as the command line:

```sh
python3 unified_gui.py
```

One window, five tabs in order: **Extract Pins** from a GDS, **Pin List Editor**
to fix names, sides and types, **Symbol Designer** with a live preview,
**Footprint Generator**, and a **History** of past runs.

On a headless machine, set `QT_QPA_PLATFORM=offscreen`.

### From the command line

```sh
python3 gds_to_kicad.py die.gds -o die.kicad_mod          # footprint
python3 gds_to_kicad_symbol.py die.gds -o die.kicad_sym   # symbol
```

With no layer flags both tools auto-detect the densest pad layer and the text
layer that names the pads. That works on a clean chiplet GDS. On a real full die
it will happily return routing, fill and guard rings as "pads", so read
[Selecting layers](#selecting-layers) and
[Curating pads by hand](#curating-pads-by-hand) before trusting the result.

## Tools

| Tool | Does |
| --- | --- |
| `unified_gui.py` | GUI covering the whole flow: pins, symbol, footprint |
| `gds_to_kicad.py` | GDS to `.kicad_mod` footprint |
| `gds_to_kicad_symbol.py` | GDS to `.kicad_sym` symbol library |
| `blackbox_chiplet.py` | Pad map (JSON or CSV) to a stand-in GDS |
| `footprint_to_pinlist.py` | `.kicad_mod` to pin list JSON |
| `kicad_netlist_to_chiplet.py` | KiCad `.net` to chiplet YAML, CSV or `.chiplet` |
| `io_pads/generate_io_pad.py` | Parametric interposer I/O pad symbol and footprint |
| `io_pads/kicad_pcb_to_iopads.py` | Routed `.kicad_pcb` to `io_pads.json` |

`gds_to_kicad_gui.py` and `gds_to_kicad_symbol_gui.py` are single-purpose GUIs
for footprints and symbols. Every tool accepts `--help`.

## Selecting layers

There is no PDK baked in, no `--pdk` flag and no PDK discovery. You always say
explicitly where the pads are, in one of three ways:

| Precedence | How | Example |
| --- | --- | --- |
| 1 | Raw GDS number, ignores the `.lyp` | `--pad-layer-number 134/0` |
| 2 | Layer name from a KLayout `.lyp` | `--lyp-file pdks/sg13g2.lyp --layer TopMetal2.drawing` |
| 3 | Nothing, auto-detect the densest pad layer | (no flags) |

Pin names come from a separate text layer, selected the same way:
`--text-layer NAME`, `--text-layer-number N/D`, or auto-detect.

> **Two things that will bite you.** First, the flag spelling differs between
> the converters: `gds_to_kicad.py` uses `--layer`, `gds_to_kicad_symbol.py`
> uses `--pad-layer`. The `*-layer-number` flags are the same on both.
>
> Second, and this one costs people an afternoon: if you name the pad layer with
> `--layer` and say nothing about text, `gds_to_kicad.py` emits **numbered**
> pads instead of named ones, silently. Add `--text-layer NAME`, or
> `--auto-text` to let it find the text layer itself. The symbol tool does not
> have this problem: it derives `TopMetal2.text` from `TopMetal2.drawing` on its
> own, which is why it has no `--auto-text` flag. When both tools auto-detect
> the pad layer, both auto-detect the text layer too.

### Bundled layer files

`pdks/` ships four ready `.lyp` files:

| File | Contents |
| --- | --- |
| `generic.lyp` | **Default.** Pads-only vocabulary: pad metal `205/0`, pad text `205/25`, outline `206/0`. For closed chiplets that ship no layer file. |
| `sg13g2.lyp` | Full IHP SG13G2 layer set |
| `sky130.lyp` | Full SkyWater sky130 layer set |
| `interposer.lyp` | SG13G2 upper metals through Bump, for interposer work |

Any technology works: drop its KLayout `.lyp` into `pdks/`, or pass any path
with `--lyp-file`. IHP SG13G2 is what we test against most, not a requirement.

### Inspecting a GDS first

```sh
python3 gds_to_kicad.py die.gds --scan-layers                        # suggest pad/text candidates
python3 gds_to_kicad.py die.gds --list-layers --lyp-file pdks/sky130.lyp   # dump the parsed .lyp
```

## Workflows

### Die to footprint

```sh
python3 gds_to_kicad.py die.gds --lyp-file pdks/sg13g2.lyp \
    --layer TopMetal2.drawing --text-layer TopMetal2.text -o die.kicad_mod
```

Reads the top cell, flattens it, pulls box and polygon shapes off the pad layer,
and names each pad from the nearest text label. Rectangular pads become
`smd rect`; non-rectangular pads become `smd custom` with a `gr_poly` primitive,
so the real shape is preserved. It also writes a courtyard and traceability
properties recording the source GDS, the `.lyp`, the layer and the orientation
(`GDS_FILE`, `LYP_FILE`, `GDS_LAYER`, `ORIENTATION`).

For a die that mounts face-down on an interposer, add `--flip-chip` to mirror X
so the footprint reads as seen from the interposer side.

### Die to symbol

```sh
python3 gds_to_kicad_symbol.py die.gds --lyp-file pdks/sg13g2.lyp \
    --pad-layer TopMetal2.drawing -o die.kicad_sym
```

Same pad and text extraction, emitted as a KiCad 6+ `.kicad_sym`. Pins are
arranged automatically: power top and bottom, signals left and right. Extras:
`--symbol-name`, `--footprint-ref`, and `--max-text-distance` (a DBU cutoff for
matching text to pads).

There is a two-step path when the automatic result needs fixing. Dump an
editable pin list, correct names, sides and types, then rebuild from it with no
GDS involved:

```sh
python3 gds_to_kicad_symbol.py die.gds --extract-pins pins.json
python3 gds_to_kicad_symbol.py --from-pin-list pins.json -o die.kicad_sym
```

In that JSON each pin carries a `name`, a `side` (`left`, `right`, `top`,
`bottom`) and a `type` (`passive`, `input`, `output`, `bidirectional`,
`tri_state`, `power_in`, `power_out`, `unspecified`), plus its geometry in DBU.
The same file is what the Pin List Editor tab edits, and what `--pin-list`
expects in the pad-review workflow below.

### Curating pads by hand

This is the normal path for real silicon, not an advanced fallback. A full die
GDS is mostly routing, fill and guard rings, and auto-detect cannot tell those
from bond pads. Strip the GDS down to the pad layer plus labels, clean it up in
KLayout, then build from the result:

```sh
python3 gds_to_kicad.py die.gds --generate-pad-review review.gds
klayout -e -l pdks/sg13g2.lyp review.gds   # delete everything that is not a real pad, save
python3 gds_to_kicad.py --from-pad-review review.gds --pin-list pins.json -o die.kicad_mod
```

`--pin-list` is **required** here: it is the authoritative list of names, and
pads are matched to it by nearest neighbor, with warnings for anything left
unmatched. Produce it with `gds_to_kicad_symbol.py --extract-pins pins.json` or
with `footprint_to_pinlist.py`.

### A chiplet with no GDS

When you only have a pad map from a datasheet (names, centers, sizes) and no
layout and no layer file, synthesize a minimal stand-in GDS:

```sh
python3 blackbox_chiplet.py pads.json -o chiplet.gds
python3 blackbox_chiplet.py pads.csv  -o chiplet.gds --no-manifest
```

The spec is JSON or CSV, chosen by extension. CSV is a `name,x_um,y_um,w_um,h_um`
header with one pad per row; JSON adds an optional explicit `die` size and a
`chiplet_name`. Pad `x`/`y` are **center** coordinates. Without an explicit die,
the outline comes from the pad extents plus a margin.

Pads, labels and outline are stamped on the canonical generic layers
(`205/0`, `205/25`, `206/0`), so the output feeds the converters above with no
flags at all. Alongside the GDS it writes a `<stem>.boundaries.json` manifest
carrying the die outline as the chiplet boundary for assembly DRC; suppress it
with `--no-manifest`.

### Board back to chiplet netlist

```sh
python3 kicad_netlist_to_chiplet.py design.net --yaml nets.yaml --csv nets.csv
python3 kicad_netlist_to_chiplet.py design.net --inject board.chiplet --summary
```

Converts a KiCad S-expression netlist into chiplet-flow YAML and/or CSV, or
injects a `netlist:` section straight into a `.chiplet` file. Nets are classified
power, ground or signal by name. Footprints from the `io_pads` library, or any
ref prefix you pass with `--external-ref-prefix J`, are flagged `external: true`.
Pass at least one of `--yaml`, `--csv`, `--inject` or `--summary`.

Going the other way, `footprint_to_pinlist.py` pulls pad geometry out of one or
more `.kicad_mod` files into a pin list JSON.

### Interposer I/O pads

```sh
python3 io_pads/generate_io_pad.py --io-class wire_bond --size 100x100
python3 io_pads/kicad_pcb_to_iopads.py design.kicad_pcb -o io_pads.json
```

`generate_io_pad.py` emits a parametric KiCad symbol and footprint for an
external interposer pad, tagged with `IO_CLASS` and `IO_PAD_SIZE_UM` properties.
Only `wire_bond` is implemented today; `flipped_bump` and `tsv_bump` are
reserved and rejected.

After routing, `kicad_pcb_to_iopads.py` walks the `.kicad_pcb`, keeps the
footprints carrying an `IO_CLASS`, and writes pad locations, sizes and nets to
JSON (mm to um, Y negated for the GDS Y-up convention). Full library setup is in
[io_pads/README.md](io_pads/README.md).

## Where files are written

From the **command line**, generated files land in the current directory:
footprints under `generated_kicad_footprint_files/`, symbols under
`generated_kicad_symbol_files/`.

| To control it | Use |
| --- | --- |
| Exact output path | `-o FILE` |
| Per-design `<DIR>/<design>.pretty/` convention | `--design-dir DIR` (footprint tool only) |
| Different base directory | `GDS_TO_KICAD_DATA_DIR` environment variable |

The **GUIs** work differently: every export goes through a save dialog that
starts in the directory you launched the GUI from. Only the conversion history
registry is written under `generated_kicad_symbol_files/`.

One more: `io_pads/generate_io_pad.py` writes back into your clone
(`io_pads/kicad_symbols/`, `io_pads/kicad_footprints/`) unless you pass
`--out-dir`.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `No module named klayout` | `pip install klayout`, or put a system KLayout's bindings on `PYTHONPATH` |
| Qt "xcb platform plugin" error on Linux | Missing Qt system libraries, see [Install](#install); or use `QT_QPA_PLATFORM=offscreen` |
| Footprint pads come out numbered, not named | You passed `--layer` but no text layer; add `--auto-text` or `--text-layer` |
| Thousands of pads, huge output file | Auto-detect grabbed routing or fill; use [pad review](#curating-pads-by-hand) |
| `pdks/...lyp` not found | The examples use paths relative to the clone root; run from there or pass an absolute `--lyp-file` |

## Documentation

- [io_pads/README.md](io_pads/README.md), the I/O pad library workflow.
- `--help` is authoritative for flags; this README covers the common paths.

Tests (needs `pytest` and `PyYAML`, see [Install](#install)):

```sh
QT_QPA_PLATFORM=offscreen pytest tests -q
```

20 skips are expected, and `-ra` (on by default, see `pytest.ini`) prints the
reason for each one. Ten need an interposer `.lyp`, a `PDK_ROOT` or an `ADK_ROOT`
that a standalone clone does not have. The other ten, all in
`tests/test_netlist_converter.py`, look for a demo netlist and `.chiplet` under
`kicad_designs/kicad_interposer_hyperlynx_to_gds/chiplet_files/`, and that
directory is empty: those ten cannot run anywhere today, here or in CI. Either
vendor the two small fixtures into `tests/` or drop the tests; do not read their
skip as an environment problem.

Upstream: [github.com/IHP-GmbH/gds2kicad](https://github.com/IHP-GmbH/gds2kicad).

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
