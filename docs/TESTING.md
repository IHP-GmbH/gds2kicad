# Testing Instructions

## Quick Test (Completed ✅)

```bash
# Generate and convert test file
./gds_to_kicad.py --generate-test-gds
./gds_to_kicad.py test_footprint.gds
```

**Result:** Successfully converted 3 pads with VDD, GND, OUT labels

---

## Real File Test (To Do)

### Convert Real GDS File

```bash
./gds_to_kicad.py TxG3_240_480_TM2.gds
```

**Expected Output:**
- File: `TxG3_240_480_TM2.kicad_mod`
- ~857 pads extracted
- ~130 named pads (OutP, OutN, GND, DOUB_Vcc_2V5, etc.)

### Verify in KiCad

1. **Open KiCad Footprint Editor:**
   ```bash
   pcbnew
   # or
   kicad
   ```

2. **Import Footprint:**
   - File → Open → Select `TxG3_240_480_TM2.kicad_mod`
   - Or copy to your KiCad footprint library

3. **Visual Inspection:**
   - Check that pads are visible on F.Cu layer
   - Verify pad names in properties
   - Check pad dimensions make sense (should be in mm)
   - Ensure no overlapping pads

4. **DRC Check (Design Rule Check):**
   - In Footprint Editor: Inspect → Show Footprint Checker
   - Look for warnings/errors:
     - Pad on multiple nets
     - Missing or invalid pad names
     - Pad spacing issues
     - Invalid layer assignments

5. **Export to Library:**
   - If footprint looks good: File → Save As
   - Add to your project library

---

## Common Issues to Check

### Issue: Pads are too small/large
- Check DBU_TO_MM conversion (line 191 in gds_to_kicad.py)
- Verify: Should be 1e-6 for IHP PDK (1 DBU = 1nm)

### Issue: Some pads have no names (numbered instead)
- Normal behavior if text label is far from pad
- Check TopMetal2:text layer (134/25) in original GDS
- Adjust text association threshold if needed

### Issue: Footprint doesn't open in KiCad
- Validate S-expression syntax
- Check for special characters in pad names
- Verify file encoding (should be UTF-8)

### Issue: Wrong coordinate origin
- KiCad uses (0,0) as footprint center
- May need to adjust reference point in future version

---

## Validation Checklist

- [ ] Footprint opens in KiCad Footprint Editor
- [ ] All pads are visible on F.Cu layer
- [ ] Named pads match expected signals (VDD, GND, etc.)
- [ ] Pad dimensions are reasonable (typical: 50µm - 200µm)
- [ ] No DRC errors reported by KiCad
- [ ] Footprint can be added to schematic
- [ ] Can be placed on PCB layout

---

## Next Steps for Future Sessions

1. **Verify KiCad compatibility** - Full DRC check
2. **Add outline generation** - From die boundary layer
3. **Add courtyard layer** - For assembly clearance
4. **Improve text association** - Smarter algorithm
5. **Support polygon pads** - Not just bounding boxes
6. **Add command-line options:**
   - `--center-at-origin` - Recenter footprint
   - `--scale FACTOR` - Adjust scale if needed
   - `--layer LAYER` - Use different metal layer
   - `--add-outline` - Generate fab layer outline

---

## Notes

- Test files are in `.gitignore` (not version controlled)
- Real GDS file: `TxG3_240_480_TM2.gds` (78KB, 857 pads)
- Test GDS file: `test_footprint.gds` (428 bytes, 3 pads)
- Generated `.kicad_mod` files should be reviewed before production use

---

## Session Log

**Date:** 2025-10-07
**Status:** Initial implementation complete
**Test Results:**
- ✅ Test file conversion successful (3/3 pads)
- ⏳ Real file conversion pending KiCad verification
- ⏳ DRC validation pending

**Next Session Goals:**
1. Open TxG3_240_480_TM2.kicad_mod in KiCad
2. Run DRC and document any issues
3. Refine converter based on findings
