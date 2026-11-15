# gds2kicad

gds2kicad turns a die or chiplet GDSII into the KiCad parts you design a board
or an interposer around (footprints, schematic symbols and pin lists), and
reads the routed KiCad design back into the chiplet assembly format. It is the
step of the IHP Open ADK flow between a die's layout and the KiCad system
design.

## Use

```sh
git clone https://github.com/IHP-GmbH/gds2kicad.git && cd gds2kicad
python3 -m venv venv && . venv/bin/activate && pip install -e .
python3 gds_to_kicad.py --generate-test-gds && python3 gds_to_kicad.py tests/test_footprint.gds -o demo.kicad_mod
```

The first command writes a small test GDS, the second turns it into a KiCad
footprint with three pads.

## Documentation

The full documentation is at
https://ihp-open-adk-docs.readthedocs.io/en/latest/tools/03_gds2kicad.html

## Release

Released as part of the IHP Open ADK. The release page lists the commit of every
repository in each release:
https://ihp-open-adk-docs.readthedocs.io/en/latest/install/00_release.html

## License

GPL-3.0-or-later. See `LICENSE`. The vendored copy in `_vendor/chiplet_format_io`
is Apache-2.0 (`LICENSES/Apache-2.0.txt`).
