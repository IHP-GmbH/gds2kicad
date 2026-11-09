# SPDX-License-Identifier: GPL-3.0-or-later
"""Every fixture is the one fixtures/PROVENANCE records.

A file at the top of fixtures/ has its own line, `name sha256 <hex>`, with a colon
after it if prose follows. A folder has a section headed `name/:` that runs to the next
blank line, with one `  file sha256 <hex>` line per file in it; a name recorded twice or a
malformed digest line fails. The record and the folder have to agree both ways: a file changed,
added or left without a line fails, and so does a line for a file that is gone. A
fixture added without a record fails here too, so none goes unchecked. A sheet and its
netlist are bound together this way: a redrawn sheet needs its netlist exported again.
"""
import hashlib
import re
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
RECORD = FIXTURES / "PROVENANCE"

_TOP = re.compile(r"^(\S+) sha256 ([0-9a-f]{64}):?$")
_HEAD = re.compile(r"^(\S+)/:")
_ENTRY = re.compile(r"^  (\S+) sha256 ([0-9a-f]{64})$")


def _recorded():
    files, folders, section = {}, {}, None
    for line in RECORD.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            section = None
        elif m := _HEAD.match(line):
            assert m.group(1) not in folders, f"second section for {m.group(1)}/"
            section = folders[m.group(1)] = {}
        elif m := _TOP.match(line):
            assert m.group(1) not in files, f"second line for {m.group(1)}"
            files[m.group(1)] = m.group(2)
        elif section is not None and " sha256 " in line:
            m = _ENTRY.match(line)
            assert m, f"malformed digest line: {line!r}"
            assert m.group(1) not in section, f"second line for {m.group(1)}"
            section[m.group(1)] = m.group(2)
    return files, folders


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_every_fixture_has_a_record():
    files, folders = _recorded()
    assert {p.name for p in FIXTURES.iterdir() if p.is_file() and p != RECORD} == set(files)
    assert {p.name for p in FIXTURES.iterdir() if p.is_dir()} == set(folders)


def test_each_top_level_fixture_is_the_one_recorded():
    files, _ = _recorded()
    assert files == {p.name: _sha(p) for p in FIXTURES.iterdir() if p.is_file() and p != RECORD}


@pytest.mark.parametrize("folder", sorted(p.name for p in FIXTURES.iterdir() if p.is_dir()))
def test_each_fixture_folder_is_the_one_recorded(folder):
    _, folders = _recorded()
    assert folders.get(folder) == {p.name: _sha(p) for p in (FIXTURES / folder).iterdir()}
