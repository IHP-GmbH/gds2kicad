# SPDX-License-Identifier: GPL-3.0-or-later
"""The vendored reader imports from a clone, and an install carries it.

Byte identity is checked against the ``_vendor/*.SOURCE`` files, each the
reference commit and each file's sha256, so the check needs no other checkout.
"""

import hashlib
import re
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
VENDORED = ROOT / "_vendor" / "chiplet_format_io"


def test_vendor_imports_from_the_clone():
    import _vendor.chiplet_format_io as reader
    from _vendor.chiplet_format_io.footprint_ids import die_footprint, ext_pad_footprint
    assert reader.__version__ == "1.21.0"
    assert die_footprint("DRIVER") == "chiplet_dies:DRIVER"
    assert ext_pad_footprint(80) == "chiplet_carrier:EXT_PAD_80um"


def test_every_vendored_package_is_installed():
    """An install carries the same packages a clone imports: ``packages`` in
    pyproject.toml names exactly the ``_vendor`` directories with an
    ``__init__.py``. Read as text, so no build, network or setuptools runs."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    listed = re.search(r"^packages = \[(.*?)\]", text, re.M | re.S)
    assert listed, "pyproject.toml lists no packages"
    declared = set(re.findall(r'"([^"]+)"', listed.group(1)))
    on_disk = {".".join(p.parent.relative_to(ROOT).parts)
               for p in (ROOT / "_vendor").rglob("__init__.py")
               if "__pycache__" not in p.parts}
    assert declared == on_disk


def test_every_module_is_installed():
    """``py-modules`` names exactly the modules at the top of the clone."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    listed = re.search(r"^py-modules = \[(.*?)\]", text, re.M | re.S)
    assert listed, "pyproject.toml lists no py-modules"
    assert set(re.findall(r'"([^"]+)"', listed.group(1))) == {
        p.stem for p in ROOT.glob("*.py")}


def _stated(source: str, header: str) -> dict:
    """The files ``_vendor/<source>`` names, each with its sha256, after its
    ``# <header> <commit> <path>`` line."""
    lines = (ROOT / "_vendor" / source).read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith(f"# {header} ")
    return dict(reversed(line.split("  ", 1)) for line in lines[1:])


def test_vendored_files_match_their_source():
    """The copy is byte for byte the files ``chiplet_format_io.SOURCE`` names,
    hashed at the reference commit it states, and nothing else."""
    stated = _stated("chiplet_format_io.SOURCE", "chiplet-spec")
    on_disk = {p.name for p in VENDORED.iterdir() if p.name != "__pycache__"}
    assert on_disk == set(stated)
    for name, digest in stated.items():
        assert hashlib.sha256((VENDORED / name).read_bytes()).hexdigest() == digest, name


def test_the_round_trip_reader_matches_its_source():
    """``_vendor/round_trip_names.py`` is byte for byte interposer-pnr's
    reader at the commit ``round_trip_names.SOURCE`` states."""
    stated = _stated("round_trip_names.SOURCE", "interposer-pnr")
    assert list(stated) == ["round_trip_names.py"]
    digest = hashlib.sha256((ROOT / "_vendor" / "round_trip_names.py").read_bytes()).hexdigest()
    assert digest == stated["round_trip_names.py"]
