# SPDX-License-Identifier: GPL-3.0-or-later
"""
Pin List Module

Defines a JSON-serializable pin list that serves as the single source of truth
for pin names, types, and sides in the human-in-the-loop workflow. Auto-generated
from GDS extraction, then reviewed/edited by the user before symbol/footprint
generation.
"""

import json
from collections import Counter
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from _paths import atomic_write
from _vendor.chiplet_format_io import ChipletFormatError
from _vendor.chiplet_format_io.pins import kicad_type


# Valid values for type and side fields. The types are KiCad's twelve
# (chiplet_format_io.pins.KICAD_PIN_TYPES, which kicad_sym_writer.PinType
# also spells), in the editor's combo order: the words after unspecified were
# appended so the combo keeps its first eight.
VALID_PIN_TYPES = [
    "passive", "input", "output", "bidirectional", "tri_state",
    "power_in", "power_out", "unspecified",
    "free", "open_collector", "open_emitter", "no_connect",
]

VALID_PIN_SIDES = ["left", "right", "top", "bottom"]

PIN_LIST_VERSION = 1


# The PinEntry fields that are bookkeeping of the read, never pins.json keys.
_READ_FIELDS = ("extra", "stores_type", "read_warnings")


@dataclass
class PinEntry:
    """A single pin in the pin list.

    ``type`` is the pin's KiCad type. A pin read from a file takes it from
    chiplet_format_io.pins.kicad_type: from role and direction when the pin
    has a role, else from the stored (deprecated) type, else passive. The
    last three fields only carry the read through to the next write:
    ``extra`` holds every source key that is not a field (role, direction,
    port, ordinal, ...) in source order, ``stores_type`` whether the source
    wrote ``type`` at all, and ``read_warnings`` what kicad_type said.
    """
    name: str
    type: str = "passive"
    side: str = "left"
    pad_index: int = 0
    center_x_dbu: float = 0.0
    center_y_dbu: float = 0.0
    width_dbu: float = 0.0
    height_dbu: float = 0.0
    polygon_points_dbu: Optional[List[List[float]]] = None
    extra: dict = field(default_factory=dict, compare=False, repr=False)
    stores_type: bool = field(default=True, compare=False, repr=False)
    read_warnings: tuple = field(default=(), compare=False, repr=False)

    def to_dict(self) -> dict:
        d = asdict(self)
        for key in _READ_FIELDS:
            del d[key]
        # A pin that had no type is not given one: its type is derived.
        if not self.stores_type:
            del d["type"]
        if d.get("polygon_points_dbu") is None:
            del d["polygon_points_dbu"]
        d.update(self.extra)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> 'PinEntry':
        """Read one pins.json pin. A role or direction outside the
        vocabulary, or a stored type KiCad does not have on a pin with no
        role, raises ChipletFormatError."""
        word, said = kicad_type(d)
        known = {f.name for f in fields(cls)} - set(_READ_FIELDS)
        return cls(
            name=d["name"],
            type=word,
            side=d.get("side", "left"),
            pad_index=d.get("pad_index", 0),
            center_x_dbu=d.get("center_x_dbu", 0.0),
            center_y_dbu=d.get("center_y_dbu", 0.0),
            width_dbu=d.get("width_dbu", 0.0),
            height_dbu=d.get("height_dbu", 0.0),
            polygon_points_dbu=d.get("polygon_points_dbu"),
            extra={k: v for k, v in d.items() if k not in known},
            stores_type="type" in d,
            read_warnings=said,
        )


class PinList:
    """Pin list with metadata and JSON I/O.

    The pin list is the single source of truth for pin names in the
    human-in-the-loop workflow. It is auto-generated from GDS extraction,
    then edited by the user, and consumed by both symbol and footprint
    generators.
    """

    def __init__(self, pins: Optional[List[PinEntry]] = None,
                 metadata: Optional[dict] = None):
        self.pins: List[PinEntry] = pins or []
        self.metadata: dict = metadata or {
            "version": PIN_LIST_VERSION,
            "chiplet_name": "",
            "gds_source": "",
            "lyp_file": "",
            "pad_layer": "",
            "text_layers": [],
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }

    @classmethod
    def from_extracted_pads(cls, pads, chiplet_name: str,
                            gds_source: str = "",
                            lyp_file: str = "",
                            pad_layer: str = "",
                            text_layers: Optional[List[str]] = None) -> 'PinList':
        """Create a pin list from extracted PadInfo objects.

        Uses classify_pin() and get_pin_type() from symbol_layout for initial
        type/side assignment. The user must review and edit the result.

        Args:
            pads: List of PadInfo objects (from PinExtractor)
            chiplet_name: Name of the chiplet/cell
            gds_source: Source GDS filename
            lyp_file: LYP filename used
            pad_layer: Pad layer name
            text_layers: Text layer names used
        """
        # Import here to avoid circular dependency at module level
        from symbol_layout import classify_pin, get_pin_type

        entries = []
        for pad in pads:
            pin_name = pad.name if pad.name else str(pad.index + 1)
            side = classify_pin(pin_name)
            pin_type = get_pin_type(pin_name, side)

            poly_pts = None
            if getattr(pad, 'polygon_points', None):
                poly_pts = [[float(x), float(y)] for x, y in pad.polygon_points]

            entries.append(PinEntry(
                name=pin_name,
                type=pin_type.value,
                side=side.value,
                pad_index=pad.index,
                center_x_dbu=pad.center_x,
                center_y_dbu=pad.center_y,
                width_dbu=pad.width,
                height_dbu=pad.height,
                polygon_points_dbu=poly_pts,
            ))

        # Distribute signal pins equitably across all 4 sides.
        # Power pins already have top/bottom; signal pins all got "left"
        # from classify_pin(). Redistribute so total pins per side are
        # as balanced as possible.
        power_top = sum(1 for e in entries if e.side == "top")
        power_bottom = sum(1 for e in entries if e.side == "bottom")
        signal_entries = [e for e in entries if e.side == "left"]

        if signal_entries:
            counts = {
                "left": 0, "right": 0,
                "top": power_top, "bottom": power_bottom,
            }
            sides_order = ["left", "right", "top", "bottom"]
            for entry in signal_entries:
                min_side = min(sides_order, key=lambda s: counts[s])
                entry.side = min_side
                counts[min_side] += 1

        metadata = {
            "version": PIN_LIST_VERSION,
            "chiplet_name": chiplet_name,
            "gds_source": gds_source,
            "lyp_file": lyp_file,
            "pad_layer": pad_layer,
            "text_layers": text_layers or [],
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }

        return cls(pins=entries, metadata=metadata)

    @classmethod
    def load(cls, json_path: str) -> 'PinList':
        """Load a pin list from a JSON file.

        A pin kicad_type refuses raises ChipletFormatError naming the file.
        """
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        try:
            return cls.from_dict(data)
        except ChipletFormatError as exc:
            raise ChipletFormatError(f"pin list {json_path}: {exc}") from None

    @classmethod
    def from_dict(cls, data: dict) -> 'PinList':
        """A pin list from its JSON document (what load reads, or the copy a
        project file embeds). Raises ChipletFormatError as PinEntry.from_dict."""
        metadata = {k: v for k, v in data.items() if k != "pins"}
        pins = [PinEntry.from_dict(p) for p in data.get("pins", [])]

        return cls(pins=pins, metadata=metadata)

    def save(self, json_path: str):
        """Save the pin list to a JSON file."""
        data = dict(self.metadata)
        data["pins"] = [p.to_dict() for p in self.pins]

        Path(json_path).parent.mkdir(parents=True, exist_ok=True)
        with atomic_write(json_path) as f:
            json.dump(data, f, indent=2)

    def validate(self) -> List[str]:
        """Validate the pin list and return a list of warnings.

        Each pin's read warnings (kicad_type's, from load) come first, in pin
        order. Then it checks for:
        - Empty pin names
        - Duplicate pin names
        - Invalid type/side values
        - Empty pin list
        """
        warnings = [w for p in self.pins for w in p.read_warnings]

        if not self.pins:
            warnings.append("Pin list is empty")
            return warnings

        # Empty names
        empty_names = [i for i, p in enumerate(self.pins) if not p.name.strip()]
        if empty_names:
            warnings.append(
                f"Empty pin names at indices: {empty_names}"
            )

        # Duplicate names
        name_counts = Counter(p.name for p in self.pins if p.name.strip())
        duplicates = {name: count for name, count in name_counts.items()
                      if count > 1}
        if duplicates:
            dup_strs = [f"'{n}' x{c}" for n, c in duplicates.items()]
            warnings.append(f"Duplicate pin names: {', '.join(dup_strs)}")

        # Invalid types
        for i, p in enumerate(self.pins):
            if p.type not in VALID_PIN_TYPES:
                warnings.append(
                    f"Pin {i} '{p.name}': invalid type '{p.type}'"
                )

        # Invalid sides
        for i, p in enumerate(self.pins):
            if p.side not in VALID_PIN_SIDES:
                warnings.append(
                    f"Pin {i} '{p.name}': invalid side '{p.side}'"
                )

        return warnings

    def get_unique_names(self) -> List[str]:
        """Return sorted list of unique pin names."""
        return sorted(set(p.name for p in self.pins if p.name.strip()))

    def deduplicate_names(self):
        """Append _1, _2, ... suffixes to duplicate pin names.

        Modifies pins in-place. The first occurrence keeps its original name,
        subsequent occurrences get the lowest free suffix. The candidate is
        checked against every name already finalized, so a synthesized suffix
        can never collide with an existing name (e.g. [A, A, A_1] -> [A, A_1,
        A_1_1], not two A_1).
        """
        seen = set()
        for pin in self.pins:
            name = pin.name
            if name not in seen:
                seen.add(name)
                continue
            n = 1
            while f"{name}_{n}" in seen:
                n += 1
            pin.name = f"{name}_{n}"
            seen.add(pin.name)

    def __len__(self):
        return len(self.pins)

    def __repr__(self):
        return f"PinList({len(self.pins)} pins, chiplet='{self.metadata.get('chiplet_name', '')}')"
