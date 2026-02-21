# Developer Guide

This document provides comprehensive technical documentation for developers who want to understand, modify, or extend the GDSII to KiCad footprint converter.

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Code Structure](#code-structure)
- [Development Environment](#development-environment)
- [KLayout API Usage](#klayout-api-usage)
- [Common Development Tasks](#common-development-tasks)
- [Testing Strategy](#testing-strategy)
- [Known Issues](#known-issues)
- [Future Enhancements](#future-enhancements)

---

## Architecture Overview

### High-Level Design

The converter follows a pipeline architecture with clear separation of concerns:

```
Input (GDS) → Layer Mapping → Geometry Extraction → Text Association → Output (KiCad)
```

### Component Responsibilities

| Component | Responsibility | Input | Output |
|-----------|---------------|-------|--------|
| `LayerMap` | Parse and provide layer definitions | CSV file | Layer lookup dictionary |
| `GDSToKiCad` | Orchestrate conversion pipeline | GDS file, LayerMap | KiCad footprint file |
| `_extract_pads()` | Extract pad geometries | Layout, Cell | List of Box objects |
| `_extract_text()` | Extract text labels | Layout, Cell | List of (string, Point) tuples |
| `_associate_text_with_pads()` | Match text to pads | Pads, Texts | Dictionary {pad_index: name} |
| `_generate_kicad_footprint()` | Generate output file | Name, Pads, Texts | KiCad .kicad_mod file |

### Design Decisions

**Why CSV instead of JSON for layer mapping?**
- CSV is the native format provided by the PDK
- Simpler to maintain and edit manually
- No need for additional schema complexity

**Why nearest-neighbor for text association?**
- Simple and predictable behavior
- Sufficient for typical IC layouts where text is placed near pads
- Easy to debug and understand
- Future enhancement: spatial indexing for very large designs (1000+ pads)

**Why flatten to top cell?**
- KiCad footprints represent a single physical component
- Hierarchical GDS structures represent design organization, not assembly
- Simplifies conversion logic significantly

---

## Code Structure

### Class: LayerMap

**Purpose:** Parse `layer_table.csv` and provide layer number lookups by name and purpose.

**File:** `gds_to_kicad.py` lines 24-60

**Key Methods:**

```python
def __init__(self, csv_path: str = "layer_table.csv")
    """Load layer definitions from CSV file"""

def get_layer(self, name: str, purpose: str = "drawing") -> Optional[Tuple[int, int]]
    """Return (layer_number, datatype) for given layer name and purpose"""
```

**Internal Structure:**

```python
self.layers = {
    "TopMetal2:drawing": (134, 0),
    "TopMetal2:text": (134, 25),
    "TEXT:drawing": (63, 0),
    # ... 293 more layers
}
```

**CSV Format:**

```
LayerName,Purpose,LayerNumber,Datatype,Description
TopMetal2,drawing,134,0,Defines 2-nd thick TopMetal layer
TopMetal2,text,134,25,Text layer for TopMetal2
```

### Class: GDSToKiCad

**Purpose:** Main converter class orchestrating the conversion pipeline.

**File:** `gds_to_kicad.py` lines 63-228

**Constructor:**

```python
def __init__(self, layer_map: LayerMap):
    self.layer_map = layer_map
    self.topmetal2_layer = layer_map.get_layer("TopMetal2", "drawing")
    self.text_layer = layer_map.get_layer("TEXT", "drawing")
    self.topmetal2_text_layer = layer_map.get_layer("TopMetal2", "text")
```

**Method: convert()**

Main entry point for conversion.

```python
def convert(self, gds_path: str, output_path: str) -> bool:
    layout = db.Layout()
    layout.read(gds_path)                    # Load GDS file
    top_cell = layout.top_cell()             # Get top-level cell

    pads = self._extract_pads(layout, top_cell)
    texts = self._extract_text(layout, top_cell)

    self._generate_kicad_footprint(top_cell.name, pads, texts, output_path)
    return True
```

**Method: _extract_pads()**

Extracts rectangular geometries from TopMetal2 layer.

```python
def _extract_pads(self, layout: db.Layout, cell: db.Cell) -> List[db.Box]:
    pads = []
    layer_index = layout.layer(*self.topmetal2_layer)
    shapes = cell.shapes(layer_index)

    for shape in shapes.each():
        if shape.is_box():
            pads.append(shape.box)           # PROPERTY, not method
        elif shape.is_polygon():
            pads.append(shape.polygon.bbox())  # bbox() IS a method

    return pads
```

**Important:** Polygons are converted to bounding boxes. This means non-rectangular pads will be approximated. See [Future Enhancements](#future-enhancements) for polygon support.

**Method: _extract_text()**

Extracts text labels from TEXT and TopMetal2:text layers.

```python
def _extract_text(self, layout: db.Layout, cell: db.Cell) -> List[Tuple[str, db.Point]]:
    texts = []

    # Try TEXT layer (63/0)
    if self.text_layer:
        layer_index = layout.layer(*self.text_layer)
        shapes = cell.shapes(layer_index)
        for shape in shapes.each():
            if shape.is_text():
                text = shape.text            # PROPERTY, not method
                texts.append((text.string, text.trans.disp))

    # Try TopMetal2:text layer (134/25) - primary source for pin names
    if self.topmetal2_text_layer:
        layer_index = layout.layer(*self.topmetal2_text_layer)
        shapes = cell.shapes(layer_index)
        for shape in shapes.each():
            if shape.is_text():
                text = shape.text
                texts.append((text.string, text.trans.disp))

    return texts
```

**Note:** TopMetal2:text (134/25) is the primary source for pin names in real IC designs. TEXT layer (63/0) may be empty or contain only annotation text.

**Method: _associate_text_with_pads()**

Matches text labels to nearest pad using Euclidean distance.

```python
def _associate_text_with_pads(self, pads: List[db.Box],
                              texts: List[Tuple[str, db.Point]]) -> Dict[int, str]:
    pad_names = {}

    for text_str, text_pos in texts:
        min_dist = float('inf')
        closest_pad_idx = -1

        for idx, pad in enumerate(pads):
            # Calculate pad center
            pad_center_x = (pad.left + pad.right) / 2
            pad_center_y = (pad.bottom + pad.top) / 2

            # Euclidean distance
            dx = text_pos.x - pad_center_x
            dy = text_pos.y - pad_center_y
            dist = (dx * dx + dy * dy) ** 0.5

            if dist < min_dist:
                min_dist = dist
                closest_pad_idx = idx

        if closest_pad_idx >= 0:
            pad_names[closest_pad_idx] = text_str

    return pad_names  # {0: "VDD", 5: "GND", 12: "OUT", ...}
```

**Complexity:** O(n * m) where n = number of texts, m = number of pads.

**Limitation:** If multiple texts are equidistant from a pad, the last one processed wins.

**Method: _generate_kicad_footprint()**

Writes KiCad S-expression format file.

```python
def _generate_kicad_footprint(self, name: str, pads: List[db.Box],
                              texts: List[Tuple[str, db.Point]], output_path: str):
    pad_names = self._associate_text_with_pads(pads, texts)

    # Coordinate conversion: GDS database units (nm) to millimeters
    DBU_TO_MM = 1e-6

    with open(output_path, 'w') as f:
        # Header
        f.write(f'(footprint "{name}"\n')
        f.write('  (layer "F.Cu")\n')
        f.write('  (descr "Auto-generated from GDSII")\n')
        f.write('  (attr smd)\n\n')

        # Reference and value
        f.write('  (fp_text reference "REF**" (at 0 0) (layer "F.SilkS")\n')
        f.write('    (effects (font (size 1 1) (thickness 0.15)))\n')
        f.write('  )\n')
        f.write(f'  (fp_text value "{name}" (at 0 -2) (layer "F.Fab")\n')
        f.write('    (effects (font (size 1 1) (thickness 0.15)))\n')
        f.write('  )\n\n')

        # Pads
        for idx, pad in enumerate(pads):
            pad_name = pad_names.get(idx, str(idx + 1))  # Default to number

            center_x = ((pad.left + pad.right) / 2) * DBU_TO_MM
            center_y = ((pad.bottom + pad.top) / 2) * DBU_TO_MM
            width = (pad.right - pad.left) * DBU_TO_MM
            height = (pad.top - pad.bottom) * DBU_TO_MM

            f.write(f'  (pad "{pad_name}" smd rect (at {center_x:.6f} {center_y:.6f})\n')
            f.write(f'    (size {width:.6f} {height:.6f})\n')
            f.write('    (layers "F.Cu" "F.Paste" "F.Mask")\n')
            f.write('  )\n')

        f.write(')\n')
```

**Output Format:** KiCad 6+ S-expression (Lisp-like syntax)

**Layers Used:**
- `F.Cu` - Front copper layer (pads)
- `F.Paste` - Solder paste stencil
- `F.Mask` - Solder mask openings
- `F.SilkS` - Silkscreen (reference designator)
- `F.Fab` - Fabrication layer (value)

### Function: generate_test_gds()

**Purpose:** Create a minimal GDSII file for testing without external dependencies.

**File:** `gds_to_kicad.py` lines 231-255

```python
def generate_test_gds():
    layout = db.Layout()
    top_cell = layout.create_cell("TEST_FOOTPRINT")

    # Define layers
    topmetal2 = layout.layer(134, 0)
    text_layer = layout.layer(63, 0)

    # Create 3 rectangular pads (100um x 100um)
    top_cell.shapes(topmetal2).insert(db.Box(0, 0, 100000, 100000))
    top_cell.shapes(topmetal2).insert(db.Box(200000, 0, 300000, 100000))
    top_cell.shapes(topmetal2).insert(db.Box(0, 200000, 100000, 300000))

    # Add text labels at pad centers
    top_cell.shapes(text_layer).insert(db.Text("VDD", db.Trans(db.Point(50000, 50000))))
    top_cell.shapes(text_layer).insert(db.Text("GND", db.Trans(db.Point(250000, 50000))))
    top_cell.shapes(text_layer).insert(db.Text("OUT", db.Trans(db.Point(50000, 250000))))

    layout.write("test_footprint.gds")
```

**Coordinates:** All in database units (nanometers)
- `db.Box(0, 0, 100000, 100000)` = 0-100um in x, 0-100um in y
- `db.Point(50000, 50000)` = center at 50um, 50um

**Usage:** `./gds_to_kicad.py --generate-test-gds`

---

## Development Environment

### Prerequisites

1. **KLayout** (>= 0.28)
   - Required for `klayout.db` Python module
   - Installation: `sudo apt install klayout` (Debian/Ubuntu)
   - Verify: `klayout -v`

2. **Python** (>= 3.6)
   - Standard library only (no pip dependencies)
   - Type hints supported but not enforced

3. **PYTHONPATH Configuration**

   Add to `~/.bashrc` or `~/.zshrc`:

   ```bash
   export PYTHONPATH=$PYTHONPATH:/usr/share/klayout/python
   ```

   Verify:

   ```bash
   python3 -c "import klayout.db; print('Success')"
   ```

### Project Setup

```bash
git clone <repository-url>  # When published
cd gds_kicad
chmod +x gds_to_kicad.py

# Generate test file
./gds_to_kicad.py --generate-test-gds

# Test conversion
./gds_to_kicad.py test_footprint.gds

# Verify output
ls -lh test_footprint.kicad_mod
```

### Git Workflow

This project uses local git for version control:

```bash
git status                    # Check working tree
git add <files>              # Stage changes
git commit -m "Description"  # Commit changes
git log --oneline            # View history
```

**Branch Strategy:** Currently single-branch (`main`/`master`). Create feature branches for experimental work.

**Commit Messages:** Clear, descriptive, imperative mood. No attribution metadata.

---

## KLayout API Usage

### Critical Concepts

**Properties vs Methods**

KLayout's Python API uses properties (not methods) for accessing shape data:

```python
# CORRECT
box = shape.box              # Property access
text = shape.text            # Property access
polygon = shape.polygon      # Property access

# INCORRECT (will raise TypeError)
box = shape.box()            # Error: 'Box' object is not callable
text = shape.text()          # Error: 'Text' object is not callable
```

**Exception:** Bounding box calculation IS a method:

```python
bbox = shape.polygon.bbox()  # Correct - bbox() is a method
```

### Common KLayout Objects

**db.Layout**

Container for entire GDS database.

```python
layout = db.Layout()
layout.read("input.gds")     # Load from file
layout.write("output.gds")   # Save to file
top = layout.top_cell()      # Get top-level cell
layer_idx = layout.layer(134, 0)  # Get layer index
```

**db.Cell**

Represents a GDS cell (structure).

```python
cell = layout.top_cell()
name = cell.name             # Cell name (string)
shapes = cell.shapes(layer_idx)  # Get shapes on layer
```

**db.Box**

Rectangular geometry.

```python
box = db.Box(x1, y1, x2, y2)  # Constructor
box.left                      # Min X coordinate
box.right                     # Max X coordinate
box.bottom                    # Min Y coordinate
box.top                       # Max Y coordinate
box.width()                   # Width (method)
box.height()                  # Height (method)
```

**db.Polygon**

Arbitrary polygon geometry.

```python
poly = shape.polygon          # Get polygon from shape
bbox = poly.bbox()           # Get bounding box (db.Box)
num_points = poly.num_points()  # Number of vertices
```

**db.Text**

Text label.

```python
text = shape.text            # Get text from shape
text.string                  # Text content (string)
text.trans                   # Transformation (db.Trans)
text.trans.disp              # Position (db.Point)
```

**db.Point**

2D coordinate.

```python
point = db.Point(x, y)
point.x                      # X coordinate
point.y                      # Y coordinate
```

**db.Trans**

Transformation (translation, rotation, mirroring).

```python
trans = db.Trans(point)      # Translation only
trans = db.Trans.R90         # 90-degree rotation
trans.disp                   # Displacement (db.Point)
```

### Iteration Patterns

**Iterate over shapes:**

```python
shapes = cell.shapes(layer_index)
for shape in shapes.each():
    if shape.is_box():
        process_box(shape.box)
    elif shape.is_polygon():
        process_polygon(shape.polygon)
    elif shape.is_text():
        process_text(shape.text)
```

**Shape type checking:**

```python
shape.is_box()               # True if box
shape.is_polygon()           # True if polygon
shape.is_path()              # True if path
shape.is_text()              # True if text
```

### Coordinate System

**Database Units (DBU):**

- GDS uses integer database units
- IHP SG13G2 PDK: 1 DBU = 1 nanometer
- Access via `layout.dbu` (in microns, e.g., 0.001)

**Conversion to millimeters:**

```python
DBU_TO_MM = 1e-6             # 1 nm = 1e-6 mm
width_mm = (box.right - box.left) * DBU_TO_MM
```

**Typical pad sizes:**
- Small signal pad: 50-80 um (50000-80000 DBU)
- Power pad: 100-200 um (100000-200000 DBU)

---

## Common Development Tasks

### Adding Support for a New Layer

**Example:** Extract pads from TopMetal1 instead of TopMetal2.

1. **Verify layer exists in `layer_table.csv`:**

   ```bash
   grep "TopMetal1" layer_table.csv
   ```

   Expected output:
   ```
   TopMetal1,drawing,130,0,Defines 1-st thick TopMetal layer
   ```

2. **Add layer to `GDSToKiCad.__init__()`:**

   ```python
   self.topmetal1_layer = layer_map.get_layer("TopMetal1", "drawing")
   ```

3. **Add command-line option:**

   ```python
   parser.add_argument('--metal-layer', default='TopMetal2',
                      choices=['TopMetal1', 'TopMetal2'],
                      help='Metal layer to extract pads from')
   ```

4. **Update `_extract_pads()` to use selected layer:**

   ```python
   if args.metal_layer == 'TopMetal1':
       layer = self.topmetal1_layer
   else:
       layer = self.topmetal2_layer
   ```

5. **Test with both layers:**

   ```bash
   ./gds_to_kicad.py input.gds --metal-layer TopMetal1
   ./gds_to_kicad.py input.gds --metal-layer TopMetal2
   ```

### Improving Text Association Algorithm

**Current Issue:** Simple nearest-neighbor can fail if text is far from pad.

**Enhancement:** Add distance threshold.

```python
def _associate_text_with_pads(self, pads: List[db.Box],
                              texts: List[Tuple[str, db.Point]],
                              max_distance: float = 500000) -> Dict[int, str]:
    """
    Associate text labels with pads using nearest neighbor with distance limit.

    Args:
        pads: List of pad geometries
        texts: List of (text_string, position) tuples
        max_distance: Maximum distance in DBU (default 500um)

    Returns:
        Dictionary mapping pad index to name
    """
    pad_names = {}

    for text_str, text_pos in texts:
        min_dist = float('inf')
        closest_pad_idx = -1

        for idx, pad in enumerate(pads):
            pad_center_x = (pad.left + pad.right) / 2
            pad_center_y = (pad.bottom + pad.top) / 2

            dx = text_pos.x - pad_center_x
            dy = text_pos.y - pad_center_y
            dist = (dx * dx + dy * dy) ** 0.5

            if dist < min_dist:
                min_dist = dist
                closest_pad_idx = idx

        # Only associate if within threshold
        if closest_pad_idx >= 0 and min_dist <= max_distance:
            pad_names[closest_pad_idx] = text_str

    return pad_names
```

**Testing:**

```bash
./gds_to_kicad.py input.gds  # Default threshold
./gds_to_kicad.py input.gds --text-distance 1000000  # 1mm threshold
```

### Adding Polygon Pad Support

**Current:** Polygons converted to bounding boxes.

**Goal:** Preserve polygon shapes in KiCad.

**Challenge:** KiCad pads support custom shapes, but require different syntax.

**Implementation Outline:**

1. **Detect polygon vs rectangle:**

   ```python
   def is_rectangular_polygon(poly: db.Polygon) -> bool:
       return poly.num_points() == 4 or poly.num_points() == 5
   ```

2. **Add custom pad shape generation:**

   ```python
   def generate_custom_pad_shape(poly: db.Polygon) -> str:
       points = []
       for i in range(poly.num_points()):
           pt = poly.point(i)
           x = pt.x * DBU_TO_MM
           y = pt.y * DBU_TO_MM
           points.append(f"(xy {x:.6f} {y:.6f})")

       return "(primitives\n" + \
              "  (gr_poly\n" + \
              "    (pts\n" + \
              f"      {' '.join(points)}\n" + \
              "    )\n" + \
              "    (width 0)\n" + \
              "  )\n" + \
              ")"
   ```

3. **Update `_generate_kicad_footprint()` to use custom shapes when needed.**

**Reference:** [KiCad Custom Pad Shapes](https://dev-docs.kicad.org/en/file-formats/sexpr-intro/)

### Debugging KLayout API Issues

**Problem:** Code raises `TypeError: 'X' object is not callable`

**Solution:**

1. Check if you're calling a property as a method:

   ```python
   # Wrong
   box = shape.box()

   # Correct
   box = shape.box
   ```

2. Use `dir()` to inspect object:

   ```python
   shape = next(shapes.each())
   print(dir(shape))  # List all attributes and methods
   ```

3. Check KLayout documentation:

   ```bash
   # Open local documentation
   firefox KLayout_with_python.html

   # Or online
   firefox https://www.klayout.de/doc/code/class_Shape.html
   ```

**Problem:** No shapes found on expected layer

**Solution:**

1. Verify layer exists in GDS:

   ```bash
   # Open in KLayout GUI
   klayout input.gds

   # Check layer panel (F4) for layer 134/0
   ```

2. Print available layers:

   ```python
   layout = db.Layout()
   layout.read("input.gds")

   for layer_info in layout.layer_infos():
       print(f"Layer {layer_info.layer}/{layer_info.datatype}: {layer_info.name}")
   ```

3. Check if layer index is valid:

   ```python
   layer_index = layout.layer(134, 0)
   if not layout.is_valid_layer(layer_index):
       print("Layer index invalid!")
   ```

---

## Testing Strategy

### Unit Testing Philosophy

Current implementation uses manual testing with test GDS generation. Future development should consider:

1. **Automated Unit Tests**
   - Test `LayerMap` parsing with fixture CSV
   - Test coordinate conversion math
   - Test text association algorithm with known inputs

2. **Integration Tests**
   - Generate test GDS with known geometry
   - Convert to KiCad
   - Parse output and verify pad count, names, positions

3. **Regression Tests**
   - Keep known-good GDS/KiCad pairs
   - Verify new changes don't break existing conversions

### Testing Workflow

**Quick Test (30 seconds):**

```bash
# Generate test file
./gds_to_kicad.py --generate-test-gds

# Convert it
./gds_to_kicad.py test_footprint.gds

# Verify output
grep -c "^  (pad" test_footprint.kicad_mod  # Should be 3

# Visual verification in KiCad
pcbnew test_footprint.kicad_mod
```

**Full Test (5 minutes):**

```bash
# Convert real GDS file (not in repo)
./gds_to_kicad.py your_chip.gds

# Open in KiCad Footprint Editor
pcbnew your_chip.kicad_mod

# Check:
# - All pads visible on F.Cu layer
# - Named pads have correct labels
# - Pad dimensions reasonable (typically 50-200um)
# - No overlapping pads (DRC check)

# Run DRC
# In KiCad: Inspect → Show Footprint Checker
```

**Regression Test:**

```bash
# Before making changes
./gds_to_kicad.py test_footprint.gds
cp test_footprint.kicad_mod test_footprint.kicad_mod.baseline

# Make code changes

# After changes
./gds_to_kicad.py test_footprint.gds
diff test_footprint.kicad_mod test_footprint.kicad_mod.baseline

# Should see no differences (or only expected changes)
```

### Test Coverage

| Component | Current Test | Needed Test |
|-----------|-------------|-------------|
| LayerMap parsing | Manual | Automated unit test |
| GDS reading | Manual | Automated with fixture GDS |
| Pad extraction | Manual | Automated geometry verification |
| Text extraction | Manual | Automated text position verification |
| Text association | Manual | Automated with known cases |
| Coordinate conversion | Manual | Automated unit test |
| KiCad output | Manual KiCad check | Automated S-expression parsing |

---

## Known Issues

### KLayout API Gotchas

**Issue:** `TypeError: 'Box' object is not callable`

**Cause:** Calling a property as if it were a method.

**Solution:**

```python
# Wrong
box = shape.box()

# Correct
box = shape.box
```

**Issue:** Polygon bounding box method name

**Cause:** Polygon objects use `bbox()` not `box()`.

**Solution:**

```python
# Wrong
box = shape.polygon.box()

# Correct
box = shape.polygon.bbox()
```

### Text Association Limitations

**Issue:** Text far from pad gets incorrectly associated.

**Current Status:** No distance threshold.

**Workaround:** Manually rename pads in KiCad after import.

**Future Fix:** Add `--max-text-distance` option (see [Common Development Tasks](#improving-text-association-algorithm)).

**Issue:** Multiple texts equally close to same pad.

**Current Status:** Last text processed wins.

**Workaround:** Edit GDS to move text labels closer to intended pads.

**Future Fix:** Use spatial containment (text inside pad bounding box) as first check.

### Coordinate Conversion Edge Cases

**Issue:** Pads appear too small or too large in KiCad.

**Cause:** Incorrect DBU assumption.

**Current Status:** Hardcoded `DBU_TO_MM = 1e-6` (assumes 1 DBU = 1nm).

**Solution:** Check actual DBU from layout:

```python
layout = db.Layout()
layout.read("input.gds")
print(f"Database unit: {layout.dbu} microns")

# If layout.dbu = 0.001 (1nm), then DBU_TO_MM = 1e-6 is correct
# If layout.dbu = 0.01 (10nm), then DBU_TO_MM = 1e-5 is correct
```

**Future Fix:** Auto-detect DBU from layout:

```python
DBU_TO_MM = layout.dbu * 1e-3  # Convert microns to mm
```

### KiCad Import Issues

**Issue:** Footprint doesn't open in KiCad.

**Possible Causes:**
1. Invalid S-expression syntax (missing parentheses, quotes)
2. Special characters in pad names (spaces, quotes, parentheses)
3. File encoding (must be UTF-8)

**Debugging:**

```bash
# Check syntax with Lisp parser
python3 << 'EOF'
import re
with open('output.kicad_mod', 'r') as f:
    content = f.read()
    opens = content.count('(')
    closes = content.count(')')
    print(f"Open parens: {opens}, Close parens: {closes}")
    if opens != closes:
        print("ERROR: Unbalanced parentheses!")
EOF

# Check for problematic characters in pad names
grep -o 'pad "[^"]*"' output.kicad_mod | sort -u
```

**Issue:** Pads have wrong origin.

**Cause:** KiCad uses footprint-relative coordinates, GDS uses absolute.

**Current Status:** Uses GDS coordinates directly.

**Future Enhancement:** Add `--center-at-origin` option to translate all coordinates so pad centroid is at (0, 0).

---

## Future Enhancements

### High Priority

**1. Auto-detect Database Units**

```python
def convert(self, gds_path: str, output_path: str):
    layout = db.Layout()
    layout.read(gds_path)

    # Auto-detect DBU
    dbu_to_mm = layout.dbu * 1e-3  # Convert microns to mm
```

**2. Add Text Association Distance Threshold**

```python
parser.add_argument('--max-text-distance', type=float, default=0.5,
                   help='Maximum distance (mm) to associate text with pad')
```

**3. Polygon Pad Support**

- Preserve non-rectangular pad shapes
- Use KiCad custom pad shape syntax
- Handle circular pads (check if polygon approximates circle)

### Medium Priority

**4. Add Fab Outline Generation**

Extract die boundary from specific layer and add to F.Fab:

```python
def _extract_outline(self, layout: db.Layout, cell: db.Cell) -> Optional[db.Polygon]:
    """Extract die outline from boundary layer"""
    boundary_layer = self.layer_map.get_layer("DIE_FRAME", "drawing")
    # ... implementation
```

**5. Multi-layer Support**

Allow extracting pads from multiple metal layers:

```bash
./gds_to_kicad.py input.gds --layers TopMetal1,TopMetal2
```

**6. Footprint Origin Control**

```bash
./gds_to_kicad.py input.gds --center-at-origin
./gds_to_kicad.py input.gds --origin-at-pad VDD
```

### Low Priority

**7. Silkscreen Generation**

Extract text from specific layer for silkscreen:

```python
def _generate_silkscreen(self, layout: db.Layout, cell: db.Cell):
    """Generate silkscreen text from PLACE layer"""
```

**8. Hierarchical Cell Conversion**

Generate multiple footprints from hierarchical GDS:

```bash
./gds_to_kicad.py input.gds --all-cells
# Creates: cell1.kicad_mod, cell2.kicad_mod, ...
```

**9. GUI Wrapper**

Create simple GUI for non-command-line users:

```python
# Using tkinter (built-in)
import tkinter as tk
from tkinter import filedialog

class ConverterGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("GDS to KiCad Converter")
        # ... GUI implementation
```

**10. Configuration File Support**

```yaml
# gds_convert.yaml
input: my_chip.gds
output: my_chip.kicad_mod
layers:
  pads: TopMetal2
  text: [TopMetal2:text, TEXT]
options:
  max_text_distance: 0.5
  center_at_origin: true
```

```bash
./gds_to_kicad.py --config gds_convert.yaml
```

---

## Reference Materials

### External Documentation

- [KLayout Python API](https://www.klayout.de/doc/code/index.html)
- [KiCad File Formats](https://dev-docs.kicad.org/en/file-formats/)
- [IHP Open PDK](https://github.com/IHP-GmbH/IHP-Open-PDK)
- [GDSII Format Specification](http://www.artwork.com/gdsii/gdsii/)

### Local Files

- `NOTES.md` - Project context and development guidelines
- `README.md` - User documentation
- `TESTING.md` - Testing procedures and verification checklist
- `KLayout_with_python.html` - Offline KLayout API reference
- `layer_table.csv` - IHP SG13G2 PDK layer definitions (296 layers)

### Code Comments

Key sections with inline documentation:

- `gds_to_kicad.py:24-60` - LayerMap class
- `gds_to_kicad.py:63-85` - GDSToKiCad initialization
- `gds_to_kicad.py:113-127` - Pad extraction logic
- `gds_to_kicad.py:129-151` - Text extraction logic
- `gds_to_kicad.py:153-180` - Text association algorithm
- `gds_to_kicad.py:182-228` - KiCad generation

---

## Version History

**Version 1.0** (2025-10-07)
- Initial implementation
- TopMetal2 pad extraction
- TEXT and TopMetal2:text support
- Nearest-neighbor text association
- KiCad 6+ S-expression output
- Test GDS generator

**Future Versions**

See [Future Enhancements](#future-enhancements) for planned features.

---

## Contributing

This project uses local git version control. When ready for public release, contribution guidelines will be added.

**Current Development:**
- Single developer with AI assistance
- Local git commits only
- No pull request workflow yet

**Future Workflow:**
- Fork and pull request model
- Code review required for major changes
- Automated testing via CI/CD
- Semantic versioning for releases

---

## Contact

For questions or issues, contact the project maintainer or file an issue in the repository (when published).

**Maintainer:** Mauricio-xx
**Email:** montanares@ihp-microelectronics.com

---

**Document Version:** 1.0
**Last Updated:** 2025-10-08
**Status:** Production Ready
