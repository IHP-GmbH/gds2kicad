# SPDX-License-Identifier: GPL-3.0-or-later
"""
KiCad Symbol File Writer

Generates KiCad 6+ compatible .kicad_sym files (S-expression format,
version 20211014). Handles pin positioning, body rectangle, and
reference/value fields.
"""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional

from sexpr import sanitize_sexpr_token
from _paths import atomic_write


class PinSide(Enum):
    LEFT = "left"
    RIGHT = "right"
    TOP = "top"
    BOTTOM = "bottom"


class PinType(Enum):
    PASSIVE = "passive"
    INPUT = "input"
    OUTPUT = "output"
    BIDIRECTIONAL = "bidirectional"
    TRI_STATE = "tri_state"
    POWER_IN = "power_in"
    POWER_OUT = "power_out"
    UNSPECIFIED = "unspecified"


# Pin angles for KiCad S-expression (direction pin points INTO the symbol body)
PIN_ANGLES = {
    PinSide.LEFT: 0,       # pin stub points right
    PinSide.RIGHT: 180,    # pin stub points left
    PinSide.TOP: 270,      # pin stub points down
    PinSide.BOTTOM: 90,    # pin stub points up
}

# Standard KiCad grid spacing for pins
PIN_SPACING = 2.54      # mm -- left/right pin vertical spacing
TB_PIN_SPACING = 5.08   # mm -- top/bottom pin horizontal spacing (wider for vertical text)
PIN_LENGTH = 2.54       # mm


@dataclass
class SymbolPin:
    """A pin in a KiCad schematic symbol"""
    name: str
    number: str  # must match footprint pad name/number
    side: PinSide = PinSide.LEFT
    pin_type: PinType = PinType.PASSIVE
    position_index: int = 0  # index along its side (0 = first pin)
    side_pin_count: int = 1  # total pins on this side (for centering)

    def get_coordinates(self, body_width: float, body_height: float) -> tuple:
        """Calculate pin endpoint coordinates given body dimensions.

        Pins are centered on each side. Returns (x, y) for the pin
        endpoint (where the wire connects). The body is centered at origin.
        """
        half_w = body_width / 2.0
        half_h = body_height / 2.0
        n = max(self.side_pin_count, 1)

        if self.side == PinSide.LEFT:
            x = -(half_w + PIN_LENGTH)
            span = (n - 1) * PIN_SPACING
            y = span / 2.0 - self.position_index * PIN_SPACING
        elif self.side == PinSide.RIGHT:
            x = half_w + PIN_LENGTH
            span = (n - 1) * PIN_SPACING
            y = span / 2.0 - self.position_index * PIN_SPACING
        elif self.side == PinSide.TOP:
            span = (n - 1) * TB_PIN_SPACING
            x = -span / 2.0 + self.position_index * TB_PIN_SPACING
            y = half_h + PIN_LENGTH
        elif self.side == PinSide.BOTTOM:
            span = (n - 1) * TB_PIN_SPACING
            x = -span / 2.0 + self.position_index * TB_PIN_SPACING
            y = -(half_h + PIN_LENGTH)

        return (x, y)

    @property
    def angle(self) -> int:
        return PIN_ANGLES[self.side]


@dataclass
class SymbolDefinition:
    """Complete KiCad symbol definition"""
    name: str
    pins: List[SymbolPin] = field(default_factory=list)
    body_width: float = 10.16   # mm (default 4 pin-spacings)
    body_height: float = 10.16  # mm
    footprint_ref: str = ""     # e.g. "MyLib:Footprint"
    description: str = ""
    reference_prefix: str = "U"  # KiCad ref prefix (U=IC, J=connector, etc.)

    def pin_count_per_side(self) -> dict:
        """Count pins on each side"""
        counts = {side: 0 for side in PinSide}
        for pin in self.pins:
            counts[pin.side] += 1
        return counts


class KiCadSymWriter:
    """Writes KiCad 6+ .kicad_sym library files"""

    VERSION = 20211014
    GENERATOR = "gds_to_kicad_symbol"

    def write_symbol_library(self, symbols: List[SymbolDefinition],
                             output_path: str):
        """Write a complete .kicad_sym library file.

        Args:
            symbols: List of symbol definitions to include
            output_path: Path for the output .kicad_sym file
        """
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        with atomic_write(output_path) as f:
            # Library header
            f.write(f'(kicad_symbol_lib\n')
            f.write(f'  (version {self.VERSION})\n')
            f.write(f'  (generator "{self.GENERATOR}")\n')

            for sym in symbols:
                self._write_symbol(f, sym)

            f.write(')\n')

    def _write_symbol(self, f, sym: SymbolDefinition):
        """Write a single symbol definition"""
        safe_name = self._sanitize_name(sym.name)

        f.write(f'\n  (symbol "{safe_name}"\n')
        f.write(f'    (pin_numbers hide)\n')

        # Properties
        self._write_property(f, "Reference", sym.reference_prefix, 0, 0,
                             y_offset=sym.body_height / 2 + 2.54)
        self._write_property(f, "Value", safe_name, 1, 0,
                             y_offset=-(sym.body_height / 2 + 2.54))
        self._write_property(f, "Footprint", sym.footprint_ref, 2, 0,
                             y_offset=-(sym.body_height / 2 + 5.08),
                             visible=False)
        if sym.description:
            self._write_property(f, "Description", sym.description, 3, 0,
                                 y_offset=-(sym.body_height / 2 + 7.62),
                                 visible=False)

        # Symbol unit (unit 0 = common to all units)
        f.write(f'    (symbol "{safe_name}_0_1"\n')

        # Body rectangle
        half_w = sym.body_width / 2.0
        half_h = sym.body_height / 2.0
        f.write(f'      (rectangle\n')
        f.write(f'        (start {-half_w:.4f} {half_h:.4f})\n')
        f.write(f'        (end {half_w:.4f} {-half_h:.4f})\n')
        f.write(f'        (stroke (width 0.254) (type default))\n')
        f.write(f'        (fill (type background))\n')
        f.write(f'      )\n')

        f.write(f'    )\n')  # end symbol_0_1

        # Pins (in unit 1)
        f.write(f'    (symbol "{safe_name}_1_1"\n')

        for pin in sym.pins:
            self._write_pin(f, pin, sym.body_width, sym.body_height)

        f.write(f'    )\n')  # end symbol_1_1
        f.write(f'  )\n')    # end symbol

    def _write_property(self, f, key: str, value: str, prop_id: int,
                        x_offset: float = 0, y_offset: float = 0,
                        visible: bool = True):
        """Write a symbol property"""
        hide_str = "" if visible else " hide"
        # The value can be untrusted (e.g. a user-supplied --description or a
        # footprint ref); sanitize it like names so a stray quote/paren cannot
        # corrupt the S-expression.
        value = sanitize_sexpr_token(value)
        f.write(f'    (property "{key}" "{value}"\n')
        f.write(f'      (at {x_offset:.4f} {y_offset:.4f} 0)\n')
        f.write(f'      (effects (font (size 1.27 1.27)){hide_str})\n')
        f.write(f'    )\n')

    def _write_pin(self, f, pin: SymbolPin, body_width: float,
                   body_height: float):
        """Write a single pin definition"""
        x, y = pin.get_coordinates(body_width, body_height)
        angle = pin.angle
        pin_type_str = pin.pin_type.value

        safe_name = self._sanitize_pin_name(pin.name)
        safe_number = self._sanitize_pin_name(pin.number)

        f.write(f'      (pin {pin_type_str} line\n')
        f.write(f'        (at {x:.4f} {y:.4f} {angle})\n')
        f.write(f'        (length {PIN_LENGTH:.4f})\n')
        f.write(f'        (name "{safe_name}"\n')
        f.write(f'          (effects (font (size 1.27 1.27)))\n')
        f.write(f'        )\n')
        f.write(f'        (number "{safe_number}"\n')
        f.write(f'          (effects (font (size 1.27 1.27)))\n')
        f.write(f'        )\n')
        f.write(f'      )\n')

    @staticmethod
    def _sanitize_name(name: str) -> str:
        """Sanitize a symbol name for KiCad S-expression"""
        return sanitize_sexpr_token(name)

    @staticmethod
    def _sanitize_pin_name(name: str) -> str:
        """Sanitize a pin name/number"""
        return sanitize_sexpr_token(name)
