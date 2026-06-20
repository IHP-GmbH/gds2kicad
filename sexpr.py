# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared helpers for emitting KiCad S-expression output.

The footprint converter (gds_to_kicad.py) and the symbol writer
(kicad_sym_writer.py) both interpolate untrusted strings -- GDS text labels,
cell names, user-supplied descriptions -- into quoted S-expression tokens.
Routing every such string through one sanitizer keeps footprint and symbol
output consistent and prevents a stray quote / paren / newline from corrupting
the generated .kicad_mod / .kicad_sym.
"""


def sanitize_sexpr_token(value) -> str:
    """Make a string safe to embed inside a quoted KiCad S-expression token.

    Lossy substitution rather than backslash-escaping, so the result is robust
    regardless of how strictly a consumer treats escapes:
      - double-quote -> ' (would close the quoted string early)
      - backslash    -> / (the S-expression escape character)
      - CR/LF/tab    -> space (a newline could split the token across lines)
    Leading/trailing whitespace is stripped. Parentheses are intentionally NOT
    touched: inside a quoted token they are literal data (they do not affect
    S-expression nesting), so substituting them would corrupt legitimate
    content like a Description reading "(20 pads)". The quote/backslash
    substitutions are the long-standing behavior of the symbol writer.
    """
    return (str(value)
            .replace('"', "'")
            .replace('\\', '/')
            .replace('\r', ' ')
            .replace('\n', ' ')
            .replace('\t', ' ')
            .strip())
