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
    regardless of how strictly a consumer treats escapes, and so it keeps the
    parenthesis balance that downstream tooling (and our own tests) rely on:
      - double-quote -> ' (would close the quoted string early)
      - backslash    -> / (the S-expression escape character)
      - parentheses  -> [ ] (keep the token paren-balanced)
      - CR/LF/tab    -> space (a newline splits the token across lines)
    Leading/trailing whitespace is stripped. The quote/backslash substitutions
    are the long-standing behavior of the symbol writer; the rest closes the
    remaining S-expression metacharacter gaps.
    """
    return (str(value)
            .replace('"', "'")
            .replace('\\', '/')
            .replace('(', '[')
            .replace(')', ']')
            .replace('\r', ' ')
            .replace('\n', ' ')
            .replace('\t', ' ')
            .strip())
