# SPDX-License-Identifier: GPL-3.0-or-later
"""
KLayout Layer Properties (.lyp) File Parser

Parses LYP XML files to extract layer definitions (name -> layer/datatype mappings).
Provides text layer discovery for pin name extraction.
"""

import sys
import xml.etree.ElementTree as ET
from typing import Dict, List, Tuple, Optional


class LYPParser:
    """Parser for KLayout .lyp (layer properties) files"""

    def __init__(self, lyp_path: str):
        self.lyp_path = lyp_path
        self.layers: Dict[str, Tuple[int, int]] = {}  # name -> (layer, datatype)
        self._parse(lyp_path)

    def _parse(self, lyp_path: str):
        """Parse LYP XML file and extract layer definitions"""
        try:
            tree = ET.parse(lyp_path)
            root = tree.getroot()

            for props in root.findall('.//properties'):
                name_elem = props.find('name')
                source_elem = props.find('source')

                if name_elem is not None and source_elem is not None:
                    name = name_elem.text
                    source = source_elem.text

                    if name and source:
                        try:
                            # Remove @N suffix if present (KLayout cell view indicator)
                            if '@' in source:
                                source = source.split('@')[0]

                            parts = source.split('/')
                            if len(parts) >= 2:
                                layer_num = int(parts[0])
                                datatype = int(parts[1])

                                # Clean layer name (some LYPs include "name - layer/dt")
                                clean_name = name.split(' - ')[0] if ' - ' in name else name
                                self.layers[clean_name] = (layer_num, datatype)
                        except (ValueError, IndexError):
                            pass

        except FileNotFoundError:
            print(f"Error: Could not find LYP file: {lyp_path}", file=sys.stderr)
            sys.exit(1)
        except ET.ParseError as e:
            print(f"Error parsing LYP file: {e}", file=sys.stderr)
            sys.exit(1)

    def get_layer(self, name: str) -> Optional[Tuple[int, int]]:
        """Get layer number and datatype for given layer name"""
        return self.layers.get(name)

    def get_layer_names(self) -> List[str]:
        """Get list of all layer names, sorted alphabetically"""
        return sorted(self.layers.keys())

    def get_layer_display_names(self) -> List[str]:
        """Get list of layer names with layer/datatype info for display"""
        result = []
        for name in sorted(self.layers.keys()):
            layer, datatype = self.layers[name]
            result.append(f"{name} ({layer}/{datatype})")
        return result

    def find_text_layers_for(self, drawing_layer_name: str) -> List[str]:
        """Find candidate text layers for a given drawing layer.

        Given a drawing layer like "TopMetal2.drawing" or "met4.drawing",
        returns names of layers that likely contain text labels for its
        geometries. Searches by name suffix conventions used across PDKs:

          IHP SG13G2/Interposer: .text (dt 25), .pin (dt 2)
          sky130:                .label (dt 5), .pin (dt 16)
          General:               TEXT.drawing (63/0), text.drawing

        Returns list of layer names that exist in the LYP file.
        """
        candidates = []

        # Extract base name (e.g. "TopMetal2" from "TopMetal2.drawing")
        base_name = drawing_layer_name.split('.')[0] if '.' in drawing_layer_name else drawing_layer_name

        # Check all common text/label/pin suffixes (PDK-agnostic)
        for suffix in ['.text', '.label', '.pin']:
            name = f"{base_name}{suffix}"
            if name in self.layers:
                candidates.append(name)

        # Check for global text layers
        for global_name in ["TEXT.drawing", "text.drawing"]:
            if global_name in self.layers and global_name not in candidates:
                candidates.append(global_name)

        return candidates

    def __repr__(self):
        return f"LYPParser({len(self.layers)} layers from {self.lyp_path})"
