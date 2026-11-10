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
clone root. You need KLayout (reads the GDS) and PyQt6 (for the GUI).

```sh
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt    # PyQt6, PyYAML
pip install klayout                # GDS reader
```

A system KLayout install with its Python bindings on `PYTHONPATH` works too. If
`import klayout.db` fails, the converters exit with a message saying so.
`pip install -e .` installs the same modules with all three dependencies,
klayout included (`pyproject.toml`).

Python 3.11 is the version CI runs. On a slim Linux box or container, PyQt6 also
needs `libgl1 libegl1 libxkbcommon0 libdbus-1-3 libfontconfig1 libglib2.0-0`, or
the GUI dies on a Qt platform-plugin error. To run the tests you additionally
need `pip install pytest`.

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
| `chiplet_to_kicad_sch.py` | `.chiplet` system to a KiCad schematic, one symbol per die and per carrier pad, or to a symbol library and blank sheet |
| `kicad_sch_to_connectivity.py` | Netlist of an edited system schematic to a connectivity form |
| `kicad_sch_to_cosim_setup.py` | Netlists of edited co-simulation setup sheets to a setup form |
| `extract_padmap.py` | Plan a die's pad map, or extract it from a finished die GDS |
| `io_pads/generate_io_pad.py` | Parametric interposer I/O pad symbol and footprint |
| `io_pads/kicad_pcb_to_iopads.py` | Routed `.kicad_pcb` to `io_pads.json` |

Every tool accepts `--help`.

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
| `sg13g2.lyp` | Full IHP SG13G2 layer set, a byte copy of IHP-Open-PDK's, pinned to its source commit by `tests/test_sg13g2_lyp_source.py` |
| `sky130.lyp` | Full SkyWater sky130 layer set, a byte copy of open_pdks' `sky130/klayout/sky130.lyp` (Apache-2.0) |
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

Die thickness is assembly metadata, not GDS geometry, so it is only written when
you pass it: `--die-thickness-um 300` emits a `DIE_THICKNESS_UM` property that
chiplet-export reads into hyp-to-gds `--die-thicknesses` for the assembly z-stack.
Omit it and the property is left out (the export defaults it to 0.0).

By default the courtyard is the pad bounding box, which for an inset pad ring is
smaller than the die. Name the layer that carries the die outline, for example
`--die-outline-layer 206/0` for a generic black-box die or `189/4` (prBoundary)
for an SG13G2 die built with LibreLane, and the courtyard becomes that outline.
The layer is also recorded as a `DIE_OUTLINE_LAYER` property (`"206/0"`), meant
for downstream tools that size the die from its GDS rather than from KiCad
geometry. The outline must merge to exactly one rectangle containing every pad;
anything else, no pads at all, or a `--dbu` that differs from the file's is an
error and no file is written. Omit the option and neither the
courtyard nor the properties change.

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

To keep the die outline on this path, pass the same `--die-outline-layer` to
both steps. `--generate-pad-review` then copies the outline into `review.gds`
(leave it in place while editing), and `--from-pad-review` reads it back from
there, since that is the file the footprint's `GDS_FILE` names. Name the pad
layer explicitly on both steps (`--pad-layer-number` or `--layer`): the review
now holds two layers, and auto-detection may rank the outline first. Reading the
outline box as a pad is refused, with or without `--die-outline-layer`, rather
than turned into a single die-sized pad.

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

Converts a KiCad S-expression netlist to YAML and/or CSV, or
injects a `netlist:` section straight into a `.chiplet` file. Nets are classified
power, ground or signal by name. Footprints from the `io_pads` library, or any
ref prefix you pass with `--external-ref-prefix J`, are flagged `external: true`.
Pass at least one of `--yaml`, `--csv`, `--inject` or `--summary`.

Going the other way, `footprint_to_pinlist.py` pulls pad geometry out of one or
more `.kicad_mod` files into a pin list JSON.

### System schematic from a chiplet

```sh
python3 chiplet_to_kicad_sch.py --chiplet design.chiplet \
    --pins U1=u1_pins.json --pins U2=u2_pins.json \
    --out sys.kicad_sch --report schematic.json --check
```

Draws the system as one KiCad 9 sheet: a symbol per die (Reference is the
component id), a pin per name in that die's pin list (number is the pin name,
label is its `port`), and a global label with the exact net name on every
connected pin. A name the list repeats, one pad of a rail, is drawn as one pin.
Terminals on no net get a no-connect flag, except open pins that link dies,
which are left bare so KiCad's list of unconnected pins is the list still to
wire. Nets come only from the `.chiplet`,
so the sheet is a view of the description, not a second source of it.

The sheet is laid out to be read and wired on paper, not to mirror the pads.
The dies sit in one row in `.chiplet` order, and every pin is on the left or
the right of its symbol, so every name reads horizontally. A die's link pins
(open, or on a net that joins dies and nothing else) face its neighbour, the
first die's on its right and the others' on their left; power pins, carrier
nets and nets of one die take the other side. Each column runs in port order
with numbers read as numbers (`d2d_f2` before `d2d_f10`), so two dies with the
same port names line up row for row, and the link side keeps room for a label
on every pin, so a sheet with its links open and the wired one share a layout.

A carrier (a component that is not a die but lists `io_pads`, such as a
wire-bond interposer) is drawn as its pads, in a column below the dies in name
order: one `chiplet_system:EXT_PAD` symbol per pad, the symbol designer sheets
use, with Reference initially the pad id, hidden `IO_PAD_ID` the stable pad id,
`CHIPLET_COMPONENT` the carrier id and
Footprint `chiplet_carrier:EXT_PAD_<n>um` from the pad's `.chiplet` size. The
pad's net is the one the netlist connects it to, else its `io_pads` net, else
none. A die symbol's Footprint is `chiplet_dies:<top cell>`, so a board the
export drew pairs with the sheet symbol for symbol. The carrier's pin list keeps the edge
each pad sits on; it is derived from the `.chiplet` and written next to the
sheet as `<id>.carrier_pins.json`, and the report records it like a die's.
Reading the sheet back (`--check`, `kicad_sch_to_connectivity.py`) folds a
carrier's pads into one carrier symbol whose pins are the pad ids.
Read-back takes the pad id from `IO_PAD_ID`, so Annotate can change a pad's
Reference without changing its terminal. Older sheets where every pad has an
absent or empty field use Reference as before. If any carrier pad has the
field, all must; a missing field is refused as `missing_identity`, naming its
carrier and Reference. Two pads of one carrier with the same pad id are refused
as `duplicate_pad`, naming both References, and neither folds.
Shared References are refused as `duplicate_reference` in every sheet mode
and by `--check`. The placed UUIDs distinguish pasted copies from distinct
units of one library symbol; an unresolved UUID is an input error.
A generated die-library symbol missing `CHIPLET_COMPONENT` is refused as
`missing_identity`; parts from other libraries keep a null identity.

Each symbol carries a hidden `CHIPLET_COMPONENT` property with its component
id, and title-block comment 1 holds the sha256 of the `.chiplet` it was drawn
from; reruns on the same inputs are byte-identical. Anything that cannot be
drawn verbatim is refused by name and no sheet is written: a connection to a
component that is neither a die nor a carrier, a pin the component does not
have, a pad whose netlist net and `io_pads` net disagree, a pad `io_class`
outside the format's vocabulary, a pad whose size is missing, not square or not
a whole number of um, a pad id that is also a component id or another pad's, a
die that states no top cell, a die or carrier id outside `[A-Za-z0-9_.+-]`,
or a pin or net name outside that set plus numeric subscripts after the first
character (`IO[0].pad` is drawn; KiCad gives other characters meaning, reads
`B[0..3]` as a bus and a pin `[0]` as stacked pins). The report lists every refusal. `--check` exports the
sheet's netlist with `kicad-cli` and compares it with the `.chiplet`. Exit
status is 0 when written, 3 when refused, 4 when written but the check found a
difference or could not run.

With `--setup cosim.setup.json --setup-spec cosim_setup_symbols.json`, a
co-simulation setup projection (`chiplet_cosim.cosim_setup` 1.0) and the symbol
spec the co-simulation flow draws it with (`chiplet_cosim.cosim_setup_symbols` 1.0; pass
your own, the copy under `tests/fixtures/` is only for the tests), the setup is drawn too: its system-sheet
symbols (simulation, models, supplies, crossings, bench) in a band below the
dies, and its die, bench and channel sheets as flat sheets of their own beside
the system sheet (`sys.die.<die>.kicad_sch`, `sys.bench.kicad_sch`,
`sys.channel.<net>.kicad_sch`), each netlisted on its own. A setup symbol
carries a `chiplet_cosim_setup` field holding its kind and one field per
attribute, and is excluded from simulation and from the BOM. Bench and channel
elements are R, C and L primitives and DC or pulse V and I sources, each with
its SPICE name as Reference and drawn as a raw SPICE model. KiCad writes a raw
model's text into the card unchanged only when it is digits and points, and any
other number to six significant digits, so an R, C or L model is the value's
exact plain decimal (`2.322105e-14` as `0.00000000000002322105`) while its Value
shows the projection's text. A value typed in e-notation with more than six
significant digits is rounded by KiCad, and the reader refuses it
(`value_rounded`): type a plain decimal, or six digits at most. The title block
of each bench and channel sheet says so.
A reference without a trailing digit (`VSEL`) is one KiCad reports as not
annotated; annotating renames it, which co-simulation reads as an edit, and the
sheet's title block says so. The report names the projection and the spec by
sha256 and lists every setup sheet by role, path and sha256; the check leaves
setup symbols out.

Beside the sheets go the symbol library `chiplet_cosim.kicad_sym` and a project
`sym-lib-table` naming it `chiplet_cosim`, so a setup symbol of any kind whose
pins the spec fixes can be placed from KiCad's symbol chooser, also a kind the
case does not use (a probe on a die sheet that has none). A placed symbol
carries its kind and one empty field per required attribute, to fill in; an
optional attribute is a field to add by hand. Each library symbol is the one
the sheets embed for its kind. The bench and block kinds take their pins from
their row, so the library has no symbol for them:
to place one, copy an existing one. A `sym-lib-table` already in the output
directory is refused unless it is the one the generator writes; the report
lists the library and the table by path and sha256.

A system whose dies have no pin lists (a mixed co-simulation case) is drawn with
`--setup-only` instead of `--pins`: no system sheet is written, and the
system-sheet setup symbols go on a flat sheet of their own,
`sys.system.kicad_sch`, beside the others. The .chiplet only names the system;
`--check` does not apply, and the report's `schematic` is null.

`kicad_sch_to_cosim_setup.py` reads the edited setup sheets back. It takes the
generator report, the form to write, and `--sheet SCHEMATIC NETLIST [SPICE]` once
per sheet: the kicadsexpr netlist and, for a bench or channel sheet, the SPICE
netlist, both exported with kicad-cli. It writes `chiplet_cosim.cosim_setup` 1.0
with tool `kicad`: every setup symbol with its kind, attributes and pins, each
pin open or on its net as KiCad named it, and every sheet and SPICE netlist by
path and sha256. It reads the SPICE netlist only to check it: every R, C, L, V
or I card must carry, as numbers, the value its symbol's fields state, or the
sheet is refused as `value_rounded` (a standard part whose value KiCad rounded,
or a Value edited on a raw model that KiCad does not read). An attribute left
empty, as a field of a placed library symbol is until someone fills it in, is
refused as `attribute_empty`. Otherwise it judges nothing; the co-simulation
flow's back end does. Exit status
is 0 when written and 2 when an input is not what it claims, with no form
written.

Once the sheet has been edited, `kicad_sch_to_connectivity.py` reads it back:

```sh
kicad-cli sch export netlist --format kicadsexpr --output sys.net sys.kicad_sch
kicad-cli sch erc --format json --severity-all --output erc.json sys.kicad_sch
python3 kicad_sch_to_connectivity.py sys.net sys.kicad_sch schematic.json \
    connectivity.json --erc erc.json
```

It writes the connectivity form (`chiplet_system.connectivity` 1.0) a schematic
import turns into a `.chiplet` revision: each symbol with its
`CHIPLET_COMPONENT` identity and pin numbers, each net with its endpoints and
whether KiCad made its name up, the base digest from the title block, and the
report, netlist, sheet and ERC report by path and sha256. It only reports what
KiCad resolved; judging the edit is the import's job. What the form cannot
carry is listed as a refusal in it: a hierarchical sheet, a net named by a
local label, two names on one net (from the ERC report), or a node on a pin its
symbol does not have. A co-simulation setup symbol (a non-empty
`chiplet_cosim_setup` field) is left out with its pins, so setup symbols never
change the form. `--no-erc` skips the ERC report and with it the two-name
check. Exit status is 0 when the form is written, refusals included, and 2 when
an input is missing or is not what it claims to be.

#### Drawing the system on a blank sheet

For an exercise where the connections are drawn by hand, `--handout DIR`
writes, instead of a sheet, what is needed to draw one from scratch:

```sh
python3 chiplet_to_kicad_sch.py --chiplet given.chiplet \
    --pins U1=u1_pins.json --pins U2=u2_pins.json --handout sheet_first/
```

`DIR` gets `chiplet_dies.kicad_sym`, a `sym-lib-table` naming it
`chiplet_dies` through `${KIPRJMOD}`, and a project with an empty sheet, both
named after the `.chiplet` (or `--name`). The library has one symbol per top
cell, named by it, with Reference prefix `U`: the drawing of the sheet above
with the link pins always on the right and every pin passive. It carries no
`CHIPLET_COMPONENT` property, and no file carries a digest, a time or a host
path, so two runs give the same bytes. Carriers are not drawn. A die whose top
cell is not a plain name is refused, and so are two dies of one top cell that
would need two different symbols.

The same handout can be drawn from die folders, with no `.chiplet`
(`die_handout.py`). Each folder holds `die-contract.json` and the inventory it
binds, and lies inside the handout directory: a placed die's `Die` field is
that folder, relative to the folder of the `.chiplet` source, so keep that
source in the handout directory. A folder is read as the classic import
reads it, so a die whose contract and inventory name different pads, or with a
pad on no port, is refused rather than drawn. One symbol per top cell, and one
folder per top cell. There is no system netlist, so a link pin is not drawn
apart from any other signal: signals sit on the right, power and ground on the
left. `chiplet_system.kicad_sym` holds `GND`, `SUPPLY` and `EXT_PAD`. The table
names `chiplet_system` first, then `chiplet_dies`. `--ext-pad-um` is the
external pad's edge in whole micrometres. The project name is `--name`, or the
output directory's own name. A die whose repeated pad numbers are all one power
or ground port gets `duplicate_pin_numbers_are_jumpers` on its symbol; any other
repeat is refused as `undeclared_tie`. KiCad 9.0 cannot read that flag, so such
a handout opens only in the tools image's KiCad. A handout drawn from a
`.chiplet` never sets it, so a die whose rail pads repeat a number, drawn that
way, is refused at interposer-pnr `export --form`; that is expected, draw it
from its die folder instead.

```sh
python3 chiplet_to_kicad_sch.py --handout sheet_first/ \
    --dies sheet_first/dies/DRIVER --dies sheet_first/dies/ECHO --ext-pad-um 80
```

The drawn sheet is read back with `--blank --library sheet_first/chiplet_dies.kicad_sym`
in place of the generator report (`NETLIST SCHEMATIC OUT_FORM`). That writes
form 1.1 with `sheet` set to `blank`: no base digest and no report, the library
by path and sha256, and each symbol's `part` (library nickname and symbol name)
with its identity null. Two symbols under one reference, such as two left
unannotated, are refused as `duplicate_reference`. Without `--blank` the form
stays 1.0.

For a classic sheet drawn from die folders, read both handout libraries:

```sh
python3 kicad_sch_to_connectivity.py sys.net sys.kicad_sch connectivity.json \
    --classic --library sheet_first/chiplet_dies.kicad_sym \
    --system-library sheet_first/chiplet_system.kicad_sym --erc erc.json
```

This writes form 1.2 with `sheet` set to `classic`, each symbol's UUID key,
value, footprint and all fields verbatim, no-connect flags as `open`, and
`rails` from the root sheet's `chiplet_system:GND` and `chiplet_system:SUPPLY`
symbols. A supply's `Volts` is empty or a plain decimal above zero. Multi-unit
symbols, net-naming power symbols from another library (a PWR_FLAG names no
net and is ignored), invalid or conflicting rail voltages, and
rails absent from the netlist are refused. Refused rails are omitted from the
rail list. A rail name follows KiCad net-name escaping. Symbols sharing a
reference retain separate UUID records and their own properties, so the import
can diagnose the duplicate. Hierarchical sheets are refused with only root
symbols written. The generated and blank modes keep their existing formats.

### Interposer I/O pads

```sh
python3 io_pads/generate_io_pad.py --io-class wire_bond --size 100x100
python3 io_pads/kicad_pcb_to_iopads.py design.kicad_pcb -o io_pads.json
python3 io_pads/kicad_pcb_to_iopads.py draft.kicad_pcb --export-report export.json -o io_pads.json
```

`generate_io_pad.py` emits a parametric KiCad symbol and footprint for an
external interposer pad, tagged with `IO_CLASS` and `IO_PAD_SIZE_UM` properties.
Only `wire_bond` is implemented today; `flipped_bump` and `tsv_bump` are
reserved and rejected.

After routing, `kicad_pcb_to_iopads.py` walks the `.kicad_pcb`, keeps the
footprints carrying an `IO_CLASS`, and writes pad locations, sizes and nets to
JSON (mm to um, Y negated for the GDS Y-up convention). Each pad's `ref` is its
io_pad id: on a board interposer-pnr `export` drew (carriers from
`chiplet_carrier` or carrying `IO_PAD_ID`) its `IO_PAD_ID`, which every carrier
there must carry, else the Reference. Each `net` is the planner's name: a board
drawn against a sheet needs `--export-report`, the report `export` wrote, to
map its sheet nets back, and a board whose carriers link sheet symbols is
refused without one. Full library setup is in
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

The **GUI** works differently: every export goes through a save dialog that
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

`-ra` (on by default, see `pyproject.toml`) prints the reason for each skip.
The ones to expect:

- two need the IHP PDK (`PDK_ROOT`): they compare the vendored `pdks/sg13g2.lyp`
  and the pad layers against the PDK's own layer sheet;
- twenty, all in `tests/test_netlist_converter.py`, look for a demo netlist and
  `.chiplet` under `kicad_designs/kicad_interposer_hyperlynx_to_gds/chiplet_files/`,
  and that directory is empty: those cannot run anywhere today, here or in CI.
  Either vendor the two small fixtures into `tests/` or drop the tests; do not
  read their skip as an environment problem.

The cases that run the real `kicad-cli` are deselected by default, so a bare
`pytest` starts no external tool. Run them with `pytest -m kicad_cli`; with
`GDS_TO_KICAD_REQUIRE_KICAD_CLI=1` a missing `kicad-cli` is a failure instead of
a skip.

Upstream: [github.com/IHP-GmbH/gds2kicad](https://github.com/IHP-GmbH/gds2kicad).

## License

GPL-3.0-or-later. See [LICENSE](LICENSE). The copy in `_vendor/chiplet_format_io` is Apache-2.0 (`LICENSES/Apache-2.0.txt`), inside this GPL-3.0-or-later tool; `_vendor/chiplet_format_io.SOURCE` names the chiplet-spec commit it was copied from and each file's sha256.
