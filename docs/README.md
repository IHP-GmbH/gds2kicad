# GDSII to KiCad Footprint Converter

Convert GDSII layout files to KiCad footprint format (`.kicad_mod`). This tool extracts pad geometries from the TopMetal2 layer and associates them with pin names from text layers, generating ready-to-use KiCad footprints for PCB design.

## Project Status

**Development/Testing Stage**

This tool is currently in active development and testing. While functional, it should be validated against KiCad's Design Rule Checker before use in production environments. Generated footprints may require manual verification.

## Features

- Extracts pad geometries from TopMetal2 layer (134/0)
- Reads pin names from TEXT (63/0) and TopMetal2:text (134/25) layers
- Automatic text-to-pad association using nearest neighbor algorithm
- Generates KiCad 6+ compatible `.kicad_mod` files
- Proper coordinate conversion from GDS database units to millimeters
- Test GDS file generator for development

## PDK Information

This tool is designed for the **IHP SG13G2 BiCMOS PDK** (130nm technology). Layer definitions are loaded from `layer_table.csv`, which contains the complete layer mapping for the process.

**Key Layers:**
- **TopMetal2** (134/0): Top-level metal layer for bonding pads
- **TopMetal2:text** (134/25): Text labels on TopMetal2
- **TEXT** (63/0): General text layer for annotations

For more information about the PDK, see the [IHP Open PDK](https://github.com/IHP-GmbH/IHP-Open-PDK) repository.

## Prerequisites

### 1. KLayout Installation

KLayout must be installed with Python bindings. Download from [klayout.de](https://www.klayout.de/).

**Ubuntu/Debian:**
```bash
sudo apt install klayout
```

### 2. Python Environment

Python 3.6+ required.

### 3. Configure PYTHONPATH

Expose KLayout's Python module to your system Python:

**Linux (add to `~/.bashrc` or `~/.zshrc`):**
```bash
export PYTHONPATH=$PYTHONPATH:/usr/share/klayout/python
```

**Verify installation:**
```bash
python3 -c "import klayout.db; print('KLayout module loaded successfully')"
```

## Installation

```bash
cd /path/to/gds_kicad
chmod +x gds_to_kicad.py
```

## Usage

### Basic Conversion

Convert a GDSII file to KiCad footprint:

```bash
./gds_to_kicad.py input.gds -o output.kicad_mod
```

If no output file is specified, it will use the input filename with `.kicad_mod` extension:

```bash
./gds_to_kicad.py input.gds
# Creates: input.kicad_mod
```

### Generate Test File

Create a test GDSII file for development:

```bash
./gds_to_kicad.py --generate-test-gds
# Creates: test_footprint.gds
```

Then convert it:

```bash
./gds_to_kicad.py test_footprint.gds
```

### Custom Layer Table

Specify a different layer mapping file:

```bash
./gds_to_kicad.py input.gds --layer-table custom_layers.csv
```

## Example Output

```bash
$ ./gds_to_kicad.py my_chip.gds

Loaded LayerMap(296 layers loaded)
Using TopMetal2 layer: (134, 0)
Using TEXT layer: (63, 0)
Using TopMetal2:text layer: (134, 25)

Converting my_chip.gds -> my_chip.kicad_mod
Top cell: MY_CHIP_DESIGN
Found 342 pads and 87 text labels
Generating KiCad footprint: my_chip.kicad_mod
Generated 342 pads, 65 named
```

The generated `.kicad_mod` file can be imported into KiCad's footprint library for verification and use.

## Output Format

Generated footprints use KiCad 6+ S-expression format:

```lisp
(footprint "CHIP_NAME"
  (layer "F.Cu")
  (descr "Auto-generated from GDSII")
  (attr smd)

  (fp_text reference "REF**" (at 0 0) (layer "F.SilkS")
    (effects (font (size 1 1) (thickness 0.15)))
  )
  (fp_text value "CHIP_NAME" (at 0 -2) (layer "F.Fab")
    (effects (font (size 1 1) (thickness 0.15)))
  )

  (pad "VDD" smd rect (at 1.250000 0.500000)
    (size 0.100000 0.100000)
    (layers "F.Cu" "F.Paste" "F.Mask")
  )

  (pad "GND" smd rect (at 2.500000 0.500000)
    (size 0.100000 0.100000)
    (layers "F.Cu" "F.Paste" "F.Mask")
  )

  (pad "OUT" smd rect (at 1.250000 1.750000)
    (size 0.080000 0.120000)
    (layers "F.Cu" "F.Paste" "F.Mask")
  )
)
```

## Layer Mapping

The tool reads layer definitions from `layer_table.csv` (CSV format):

```
LayerName,Purpose,LayerNumber,Datatype,Description
TopMetal2,drawing,134,0,Defines 2-nd thick TopMetal layer
TopMetal2,text,134,25,Text layer forTopMetal2
TEXT,drawing,63,0,Macro cell name, element text layer
...
```

## How It Works

1. **Parse layer table** - Load layer definitions from CSV
2. **Read GDSII** - Extract geometries from TopMetal2 layer
3. **Extract text** - Read pin names from TEXT and TopMetal2:text layers
4. **Associate** - Match text labels to nearest pads
5. **Convert** - Transform coordinates from database units (nm) to mm
6. **Generate** - Write KiCad footprint with proper formatting

## Coordinate System

- **GDS units**: Database units (typically 1 DBU = 1 nanometer)
- **KiCad units**: Millimeters
- **Conversion**: 1 DBU = 1e-6 mm

## Limitations

- Only supports rectangular pads (polygons are converted to bounding boxes)
- Text association uses simple nearest-neighbor algorithm
- Assumes standard IHP SG13G2 PDK layer numbering
- Does not preserve all GDSII hierarchy (flattens to top cell)

## Project Structure

```
gds_kicad/
├── gds_to_kicad.py         # Main converter script
├── layer_table.csv          # IHP SG13G2 layer definitions (359 layers)
├── NOTES.md                # Project context and instructions
├── KLayout_with_python.html # KLayout Python API reference
└── README.md                # This file
```

## Development

### Test GDS Generator

The `--generate-test-gds` option creates a simple test file with:
- 3 rectangular pads on TopMetal2
- 3 text labels: "VDD", "GND", "OUT"

Use this for testing modifications to the converter.

### Modifying Layer Mapping

Edit `layer_table.csv` to add or modify layer definitions. The format is:

```csv
LayerName,Purpose,LayerNumber,Datatype,Description
```

## Troubleshooting

**Error: `ModuleNotFoundError: No module named 'klayout'`**
- Ensure KLayout is installed
- Verify PYTHONPATH includes KLayout's Python directory
- Test: `python3 -c "import klayout.db"`

**No text labels found**
- Check that your GDS file has text on layers 63/0 or 134/25
- Use KLayout GUI to inspect layer contents
- Try `--generate-test-gds` to verify tool functionality

**Wrong coordinate scale**
- Verify DBU_TO_MM conversion factor in code (default: 1e-6)
- Check your GDS file's database unit setting

## License

See project repository for license information.

## Contributing

This project uses local git version control. Contact the repository owner for contribution guidelines.

## References

- [KLayout Python API](https://www.klayout.de/doc/code/index.html)
- [IHP Open PDK](https://github.com/IHP-GmbH/IHP-Open-PDK)
- [KiCad File Formats](https://dev-docs.kicad.org/en/file-formats/)
