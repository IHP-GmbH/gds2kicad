"""
Symbol Layout Engine

Creates default pin arrangements for KiCad schematic symbols based on
extracted pad information. Power pins are placed on top/bottom, signal
pins are distributed left/right alphabetically.
"""

import re
from typing import List

from pin_extractor import PadInfo
from kicad_sym_writer import (
    PinSide, PinType, SymbolPin, SymbolDefinition, PIN_SPACING,
)

# Regex patterns for power pin detection
POWER_HIGH_PATTERNS = [
    re.compile(r'^V(DD|CC|DDA|CCA|DDI|CCI)', re.IGNORECASE),
    re.compile(r'^AVDD', re.IGNORECASE),
    re.compile(r'^DVDD', re.IGNORECASE),
    re.compile(r'^VIN', re.IGNORECASE),
]

POWER_LOW_PATTERNS = [
    re.compile(r'^(GND|VSS|VSSA|VSSI|GNDA|DGND|AGND)', re.IGNORECASE),
]


def classify_pin(name: str) -> PinSide:
    """Classify a pin name into a default side placement.

    Power high pins (VDD, VCC, etc.) go to TOP.
    Power low pins (GND, VSS, etc.) go to BOTTOM.
    Everything else is a signal pin (LEFT/RIGHT, decided later).
    """
    for pattern in POWER_HIGH_PATTERNS:
        if pattern.match(name):
            return PinSide.TOP

    for pattern in POWER_LOW_PATTERNS:
        if pattern.match(name):
            return PinSide.BOTTOM

    return PinSide.LEFT  # placeholder, will be split L/R later


def get_pin_type(name: str, side: PinSide) -> PinType:
    """Assign a default electrical type based on pin classification."""
    if side == PinSide.TOP or side == PinSide.BOTTOM:
        return PinType.POWER_IN
    return PinType.PASSIVE


def create_default_layout(pads: List[PadInfo], symbol_name: str,
                          footprint_ref: str = "") -> SymbolDefinition:
    """Create a default symbol layout from extracted pad information.

    Strategy:
    - VDD/VCC/VDDA -> top side
    - GND/VSS/VSSA -> bottom side
    - Remaining signal pins split evenly left/right, sorted alphabetically
    - Body size proportional to max pins on any side

    Args:
        pads: List of PadInfo objects (with .name populated where possible)
        symbol_name: Name for the symbol
        footprint_ref: KiCad footprint reference string

    Returns:
        SymbolDefinition ready for KiCadSymWriter
    """
    top_pins = []
    bottom_pins = []
    signal_pins = []

    for pad in pads:
        # Use name if available, else sequential number
        pin_name = pad.name if pad.name else str(pad.index + 1)
        pin_number = pin_name  # number == name for symbol-footprint matching

        side = classify_pin(pin_name)
        pin_type = get_pin_type(pin_name, side)

        pin = SymbolPin(
            name=pin_name,
            number=pin_number,
            side=side,
            pin_type=pin_type,
        )

        if side == PinSide.TOP:
            top_pins.append(pin)
        elif side == PinSide.BOTTOM:
            bottom_pins.append(pin)
        else:
            signal_pins.append(pin)

    # Sort signal pins alphabetically and split left/right
    signal_pins.sort(key=lambda p: p.name.lower())
    mid = (len(signal_pins) + 1) // 2  # left side gets the extra pin if odd

    left_pins = signal_pins[:mid]
    right_pins = signal_pins[mid:]

    for pin in left_pins:
        pin.side = PinSide.LEFT
    for pin in right_pins:
        pin.side = PinSide.RIGHT

    # Sort power pins alphabetically within their side
    top_pins.sort(key=lambda p: p.name.lower())
    bottom_pins.sort(key=lambda p: p.name.lower())

    # Assign position indices
    for idx, pin in enumerate(left_pins):
        pin.position_index = idx
    for idx, pin in enumerate(right_pins):
        pin.position_index = idx
    for idx, pin in enumerate(top_pins):
        pin.position_index = idx
    for idx, pin in enumerate(bottom_pins):
        pin.position_index = idx

    # Calculate body size
    all_pins = left_pins + right_pins + top_pins + bottom_pins
    counts = {
        PinSide.LEFT: len(left_pins),
        PinSide.RIGHT: len(right_pins),
        PinSide.TOP: len(top_pins),
        PinSide.BOTTOM: len(bottom_pins),
    }

    max_vertical = max(counts[PinSide.LEFT], counts[PinSide.RIGHT], 1)
    max_horizontal = max(counts[PinSide.TOP], counts[PinSide.BOTTOM], 1)

    # Body height: enough room for vertical pins + margins
    body_height = (max_vertical + 1) * PIN_SPACING
    # Body width: enough room for horizontal pins + margins, minimum reasonable
    body_width = max((max_horizontal + 1) * PIN_SPACING, body_height)

    # Snap to grid
    body_width = _snap_to_grid(body_width)
    body_height = _snap_to_grid(body_height)

    # Minimum size
    body_width = max(body_width, 5.08)
    body_height = max(body_height, 5.08)

    return SymbolDefinition(
        name=symbol_name,
        pins=all_pins,
        body_width=body_width,
        body_height=body_height,
        footprint_ref=footprint_ref,
        description=f"Auto-generated from GDS ({len(pads)} pads)",
    )


def create_layout_from_pin_list(pin_list, symbol_name: str,
                                 footprint_ref: str = "") -> SymbolDefinition:
    """Create a symbol layout directly from a user-reviewed pin list.

    Unlike create_default_layout(), this reads type and side directly from
    the pin list entries instead of running classify_pin(). The user has
    already set them during the review step.

    Signal pins (left/right) are sorted alphabetically within their side.
    Position indices are assigned sequentially per side.

    Args:
        pin_list: PinList object with user-reviewed pins
        symbol_name: Name for the symbol
        footprint_ref: KiCad footprint reference string

    Returns:
        SymbolDefinition ready for KiCadSymWriter
    """
    side_groups = {s: [] for s in PinSide}

    for entry in pin_list.pins:
        try:
            side = PinSide(entry.side)
        except ValueError:
            side = PinSide.LEFT

        try:
            pin_type = PinType(entry.type)
        except ValueError:
            pin_type = PinType.PASSIVE

        pin = SymbolPin(
            name=entry.name,
            number=entry.name,  # number == name for traceability
            side=side,
            pin_type=pin_type,
        )
        side_groups[side].append(pin)

    # Sort alphabetically within each side
    for side_pins in side_groups.values():
        side_pins.sort(key=lambda p: p.name.lower())

    # Assign position indices
    for side_pins in side_groups.values():
        for idx, pin in enumerate(side_pins):
            pin.position_index = idx

    all_pins = []
    for side_pins in side_groups.values():
        all_pins.extend(side_pins)

    # Calculate body size
    counts = {s: len(pins) for s, pins in side_groups.items()}
    max_vertical = max(counts[PinSide.LEFT], counts[PinSide.RIGHT], 1)
    max_horizontal = max(counts[PinSide.TOP], counts[PinSide.BOTTOM], 1)

    body_height = (max_vertical + 1) * PIN_SPACING
    body_width = max((max_horizontal + 1) * PIN_SPACING, body_height)

    body_width = _snap_to_grid(body_width)
    body_height = _snap_to_grid(body_height)
    body_width = max(body_width, 5.08)
    body_height = max(body_height, 5.08)

    return SymbolDefinition(
        name=symbol_name,
        pins=all_pins,
        body_width=body_width,
        body_height=body_height,
        footprint_ref=footprint_ref,
        description=f"Generated from pin list ({len(pin_list)} pins)",
    )


def _snap_to_grid(value: float, grid: float = PIN_SPACING) -> float:
    """Snap a value up to the nearest grid multiple"""
    import math
    return math.ceil(value / grid) * grid
