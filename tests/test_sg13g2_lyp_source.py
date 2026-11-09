# SPDX-License-Identifier: GPL-3.0-or-later
"""pdks/sg13g2.lyp is a byte copy of the IHP-Open-PDK layer sheet, taken at a
named commit. The copy is pinned here so it cannot go stale silently.

When the PDK is reachable ($PDK_ROOT set), its sheet must still be the pinned
one. A PDK that has moved fails this test on purpose: re-sync the copy from the
new commit and update both constants in one commit. Skipped only when PDK_ROOT
is unset.
"""
import hashlib
import os
from pathlib import Path

import pytest

SOURCE_REPO = "IHP-Open-PDK"
SOURCE_REV = "22f43352dd8219f9007eb659e422e0d5fe28c5fb"
SOURCE_PATH = "ihp-sg13g2/libs.tech/klayout/tech/sg13g2.lyp"
SOURCE_SHA256 = "c9fd698498f3f798c2d3dc40f1aa0e0fcf646bd0a6e9e7522a008200487d5222"

COPY = Path(__file__).parent.parent / "pdks" / "sg13g2.lyp"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_copy_is_the_pinned_source():
    assert _sha256(COPY) == SOURCE_SHA256, (
        f"{COPY.name} is not the {SOURCE_REPO} {SOURCE_PATH} of {SOURCE_REV[:12]}; "
        f"copy it from that commit, or re-pin both constants to the commit it came from")


def test_pdk_still_has_the_pinned_source():
    root = os.environ.get("PDK_ROOT")
    if not root:
        pytest.skip("PDK_ROOT unset: needs the PDK libs.tech")
    source = Path(root) / SOURCE_PATH
    assert source.is_file(), f"PDK_ROOT is set but {source} does not exist"
    assert _sha256(source) == SOURCE_SHA256, (
        f"{source} differs from the copy pinned at {SOURCE_REPO} {SOURCE_REV[:12]}; "
        f"re-sync pdks/sg13g2.lyp from the PDK commit in use and re-pin "
        f"SOURCE_REV and SOURCE_SHA256 together")
