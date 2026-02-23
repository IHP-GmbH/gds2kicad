"""
Symbol Layout Engine

Creates default pin arrangements for KiCad schematic symbols based on
extracted pad information. Power pins are placed on top/bottom, signal
pins are distributed left/right alphabetically.

Body dimensions are calculated to prevent pin name overlap: the width
accommodates both the horizontal pin count and left/right text length,
and the height accommodates the vertical pin count and top/bottom text.
"""

import math
import re
from typing import Dict, List

from pin_extractor import PadInfo
from kicad_sym_writer import (
    PinSide, PinType, SymbolPin, SymbolDefinition, PIN_SPACING, TB_PIN_SPACING,
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

# Approximate width of one character at KiCad font size 1.27mm
_CHAR_WIDTH = 1.0  # mm (conservative to avoid overlap)
# Minimum gap between opposing pin name labels inside the body
_TEXT_GAP = 2.54  # mm


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


def calculate_body_size(side_groups: Dict[PinSide, List[SymbolPin]]):
    """Calculate body dimensions that prevent text overlap.

    The body must be large enough for:
    - Horizontal: all top/bottom pins at PIN_SPACING, AND left/right text
    - Vertical: all left/right pins at PIN_SPACING, AND top/bottom text

    Returns (body_width, body_height) snapped to the PIN_SPACING grid.
    """
    left_pins = side_groups.get(PinSide.LEFT, [])
    right_pins = side_groups.get(PinSide.RIGHT, [])
    top_pins = side_groups.get(PinSide.TOP, [])
    bottom_pins = side_groups.get(PinSide.BOTTOM, [])

    n_left = len(left_pins)
    n_right = len(right_pins)
    n_top = len(top_pins)
    n_bottom = len(bottom_pins)

    max_vertical = max(n_left, n_right, 1)
    max_horizontal = max(n_top, n_bottom, 1)

    # Longest pin name on each side
    max_left_len = max((len(p.name) for p in left_pins), default=0)
    max_right_len = max((len(p.name) for p in right_pins), default=0)
    max_top_len = max((len(p.name) for p in top_pins), default=0)
    max_bottom_len = max((len(p.name) for p in bottom_pins), default=0)

    # Width: enough for horizontal pin slots (wider spacing) AND left+right text
    width_for_pins = (max_horizontal + 1) * TB_PIN_SPACING
    width_for_text = (max_left_len + max_right_len) * _CHAR_WIDTH + _TEXT_GAP
    body_width = max(width_for_pins, width_for_text)

    # Height: enough for vertical pin slots AND top+bottom text
    height_for_pins = (max_vertical + 1) * PIN_SPACING
    height_for_text = (max_top_len + max_bottom_len) * _CHAR_WIDTH + _TEXT_GAP
    body_height = max(height_for_pins, height_for_text)

    # Snap to grid and enforce minimum
    body_width = _snap_to_grid(body_width)
    body_height = _snap_to_grid(body_height)
    body_width = max(body_width, 5.08)
    body_height = max(body_height, 5.08)

    return body_width, body_height


def _finalize_pins(side_groups: Dict[PinSide, List[SymbolPin]]):
    """Assign position indices, side_pin_count, and number=name on all pins.

    Returns a flat list of all pins (left, right, top, bottom).
    """
    all_pins = []
    for side, side_pins in side_groups.items():
        count = len(side_pins)
        for idx, pin in enumerate(side_pins):
            pin.position_index = idx
            pin.side_pin_count = count
            pin.number = pin.name
        all_pins.extend(side_pins)
    return all_pins


def create_default_layout(pads: List[PadInfo], symbol_name: str,
                          footprint_ref: str = "") -> SymbolDefinition:
    """Create a default symbol layout from extracted pad information.

    Strategy:
    - VDD/VCC/VDDA -> top side
    - GND/VSS/VSSA -> bottom side
    - Remaining signal pins split evenly left/right, sorted alphabetically
    - Body sized to prevent text overlap

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
        side = classify_pin(pin_name)
        pin_type = get_pin_type(pin_name, side)

        pin = SymbolPin(
            name=pin_name,
            number=pin_name,  # number == name for symbol-footprint matching
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

    side_groups = {
        PinSide.LEFT: left_pins,
        PinSide.RIGHT: right_pins,
        PinSide.TOP: top_pins,
        PinSide.BOTTOM: bottom_pins,
    }

    all_pins = _finalize_pins(side_groups)
    body_width, body_height = calculate_body_size(side_groups)

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

    all_pins = _finalize_pins(side_groups)
    body_width, body_height = calculate_body_size(side_groups)

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
    return math.ceil(value / grid) * grid
