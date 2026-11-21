#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""KiCad's own rules for the numbers and text of a schematic, as eeschema
applies them on load and in its netlist: internal units, stacked pin numbers,
unit letters, net-name escaping and bus labels, and the parts of a cached
library symbol both kicad_sch_to_connectivity.py and kicad_sch_netlist.py read.
Each function names the KiCad one it follows."""
import math
import re

import sexpr_io as sx
from sheet_check import InputError

IU_PER_MM = 10000           # schIUScale.IU_PER_MM
JUMPERS = "duplicate_pin_numbers_are_jumpers"
# A value "~" is an empty one in files older than this (sch_io_kicad_sexpr_parser.cpp).
TILDE_EMPTY_BEFORE = 20250318

_DIGITS = "0123456789"
_UNIT_STYLE = re.compile(r"_(\d+)_(\d+)$")
# UnescapeString's tokens (string_utils.cpp:308-388); one after $, ~, ^ or _ is markup.
_TOKENS = {"dblquote": '"', "quote": "'", "lt": "<", "gt": ">", "backslash": "\\",
           "slash": "/", "bar": "|", "comma": ",", "colon": ":", "space": " ",
           "dollar": "$", "tab": "\t", "return": "\n", "brace": "{"}
_TOKEN = re.compile(r"(?<![$~^_])\{(" + "|".join(_TOKENS) + r")\}")
# Any of those tokens, markup or not: a field value KiCad would unescape.
ESCAPE_TOKEN = re.compile(r"\{(?:" + "|".join(_TOKENS) + r")\}")
_MARKUP = "_^~"


def iu(text) -> int:
    """A coordinate in schematic internal units, rounded as KiROUND rounds."""
    value = float(text) * IU_PER_MM
    return int(math.copysign(math.floor(abs(value) + 0.5), value))


def _alnum_pin(number):
    """``(prefix, value)`` of a pin number with a numeric tail, value -1 when
    there is none (string_utils.cpp:1514-1537)."""
    head = len(number.rstrip(_DIGITS))
    return (number[:head], int(number[head:])) if head < len(number) else ("", -1)


def expand_stack(number):
    """The numbers a pin number stands for: ``[1-3,A5]`` is 1, 2, 3, A5; any
    other number, a malformed stack included, stands for itself
    (ExpandStackedPinNotation, string_utils.cpp:1542-1621)."""
    if not (number.startswith("[") and number.endswith("]")):
        return [number]
    out = []
    for part in (p.strip() for p in number[1:-1].split(",")):
        if not part:
            continue
        if "-" in part:
            first, _, last = part.partition("-")
            (prefix, lo), (other, hi) = _alnum_pin(first.strip()), _alnum_pin(last.strip())
            if prefix != other or lo == -1 or hi == -1 or lo > hi:
                return [number]
            out.extend(f"{prefix}{i}" for i in range(lo, hi + 1))
        else:
            out.append(part)
    return out or [number]


def unit_letters(unit, first="A"):
    """LIB_SYMBOL::LetterSubReference(unit, first): A..Z, then AA, AB, ..."""
    suffix = ""
    while True:
        u = (unit - 1) % 26
        suffix = chr(ord(first) + u) + suffix
        unit = (unit - u) // 26
        if unit <= 0:
            return suffix


def str_num_cmp(a, b, ignore_case=False) -> int:
    """StrNumCmp (string_utils.cpp): runs of digits compare as numbers."""
    i = j = 0
    while i < len(a) and j < len(b):
        c1, c2 = a[i], b[j]
        if c1 in _DIGITS and c2 in _DIGITS:
            n1 = n2 = 0
            while i < len(a) and a[i] in _DIGITS:
                n1, i = n1 * 10 + int(a[i]), i + 1
            while j < len(b) and b[j] in _DIGITS:
                n2, j = n2 * 10 + int(b[j]), j + 1
            if n1 != n2:
                return -1 if n1 < n2 else 1
            c1 = a[i] if i < len(a) else "\0"
            c2 = b[j] if j < len(b) else "\0"
        if ignore_case:
            c1, c2 = c1.upper(), c2.upper()
        if c1 != c2:
            return -1 if c1 < c2 else 1
        i, j = min(i + 1, len(a)), min(j + 1, len(b))
    return (i < len(a)) - (j < len(b))


def escape_net(text):
    """EscapeString(text, CTX_NETNAME): "/" becomes {slash}, CR and LF go."""
    return text.replace("/", "{slash}").replace("\n", "").replace("\r", "")


def _bus_vector(text):
    """NET_SETTINGS::ParseBusVector (net_settings.cpp:1052-1180): ``A[1..4]``."""
    i, n, nesting = 0, len(text), 0
    while i < n and text[i] != "[":
        if text[i] == "{":
            if not i or text[i - 1] not in _MARKUP:
                return False
            nesting += 1
        elif text[i] == "}":
            nesting -= 1
        if text[i] in " ]":
            return False
        i += 1
    i += 1
    begin = end = 0
    digits = ""
    while i < n:
        if text[i] == "." and i + 1 < n and text[i + 1] == ".":
            begin, i = int(digits or 0), i + 2
            break
        if text[i] not in _DIGITS:
            return False
        digits, i = digits + text[i], i + 1
    else:
        return False
    digits = ""
    if i >= n:
        return False
    while i < n:
        if text[i] == "]":
            end, i = int(digits or 0), i + 1
            break
        if text[i] not in _DIGITS:
            return False
        digits, i = digits + text[i], i + 1
    for c in text[i:]:
        if c != "}":
            return False
        nesting -= 1
    return nesting == 0 and begin != end


def _bus_group(text):
    """NET_SETTINGS::ParseBusGroup (net_settings.cpp:1183-1265): ``A{B C}``."""
    i, n, nesting = 0, len(text), 0
    while i < n:
        if text[i] == "{":
            if not i or text[i - 1] not in _MARKUP:
                break
            nesting += 1
        elif text[i] == "}":
            nesting -= 1
        if text[i] in " []":
            return False
        i += 1
    if nesting:
        return False
    for i in range(i + 1, n):
        if text[i] == "{":
            if text[i - 1] not in _MARKUP:
                return False
            nesting += 1
        elif text[i] == "}":
            if not nesting:
                return True
            nesting -= 1
    return False


def is_bus_label(text):
    """SCH_CONNECTION::IsBusLabel: a label text KiCad reads as a bus."""
    text = _TOKEN.sub(lambda m: _TOKENS[m[1]], text)
    return _bus_vector(text) or _bus_group(text)


def blank_tilde(text, version):
    """A value as KiCad loads it from a file of ``version``: "~" is empty
    before TILDE_EMPTY_BEFORE."""
    return "" if text == "~" and version is not None and version < TILDE_EMPTY_BEFORE else text


def properties(node, version=None):
    """``(name, value, private)`` of each property of a symbol or sheet, in file
    order. KiCad 9 writes a private one as ``(property private "Name" "Value")``
    (parseSchField); with ``version``, values load as a file of that version
    gives them."""
    out = []
    for prop in sx.find_all(node, "property"):
        private = len(prop) > 1 and prop[1] == "private" and not isinstance(prop[1], sx.Quoted)
        at = 2 if private else 1
        if len(prop) > at:
            value = str(prop[at + 1]) if len(prop) > at + 1 else ""
            out.append((str(prop[at]), blank_tilde(value, version), private))
    return out


def unit_style(cached, unit):
    """``(unit, body style)`` of a cached symbol's sub-symbol, named
    ``<name>_<unit>_<style>``."""
    match = _UNIT_STYLE.search(str(unit[1]))
    if match is None:
        raise InputError(f"cached symbol {cached[1]} has a unit named {unit[1]}, "
                         "which KiCad cannot read")
    return int(match[1]), int(match[2])


def jumpers(cached) -> bool:
    """A cached symbol's duplicate_pin_numbers_are_jumpers."""
    flag = sx.find(cached, JUMPERS)
    flag = str(flag[1]) if flag is not None and len(flag) > 1 else None
    if flag not in (None, "yes", "no"):
        raise InputError(f"cached symbol {cached[1]} states {JUMPERS} {flag!r}, "
                         "neither yes nor no")
    return flag == "yes"
