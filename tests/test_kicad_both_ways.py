# SPDX-License-Identifier: GPL-3.0-or-later
"""kicad_both_ways: a sheet read with kicad-cli and with the native netlister.

All but the kicad_cli tests stand a script in for kicad-cli that writes the
fixture's own sheet.net and erc.json (see test_kicad_sch_netlist) and leaves
the project lock file as kicad-cli 9.99 does."""
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kicad_both_ways as b  # noqa: E402
import kicad_sch_netlist as n  # noqa: E402
from tests import test_kicad_sch_netlist as nt  # noqa: E402

FAKE_KICAD_CLI = """\
import pathlib, shutil, sys
argv = sys.argv[1:]
out, sheet = pathlib.Path(argv[argv.index("--output") + 1]), pathlib.Path(argv[-1])
source = sheet.parent / ("sheet.net" if "netlist" in argv else "erc.json")
if sheet.with_suffix(".kicad_pro").exists():
    sheet.with_name("~" + sheet.stem + ".kicad_pro.lck").write_text("{{}}")
act = {act!r}
if act == "none":
    pass
elif act == "exit":
    out.write_text("(export")
    sys.exit(3)
elif act == "copy" or "erc" in argv:
    shutil.copy(source, out)
elif act == "rename":
    out.write_text(source.read_text().replace('(name "STK")', '(name "STK2")'))
else:
    out.write_text("(not a netlist")
"""


def _fake_kicad_cli(tmp_path, act):
    """A kicad-cli that writes the fixture's own netlist and ERC report
    (``copy``), renames net STK in the netlist (``rename``), writes a netlist
    that is not one (``garbage``), writes nothing (``none``), writes part of one
    and exits 3 (``exit``), or has no shebang, so it cannot be run
    (``unrunnable``)."""
    exe = tmp_path / f"kicad-cli-{act}"
    shebang = "" if act == "unrunnable" else f"#!{sys.executable}\n"
    exe.write_text(shebang + FAKE_KICAD_CLI.format(act=act), encoding="utf-8")
    exe.chmod(0o755)
    return str(exe)


def _both_ways(tmp_path, case, kicad_cli):
    sheet, library = nt._copy(tmp_path, case)
    return b.read_both_ways(sheet, library=library, mode="blank", kicad_cli=kicad_cli)


def test_differences_name_every_leaf_where_two_forms_part():
    form = {"symbols": [{"ref": "U1", "pins": ["1", "2"]}], "nets": [{"name": "A"}],
            "refusals": [{"code": "local_label", "message": "m"}]}
    other = json.loads(json.dumps(form))
    other["symbols"][0]["pins"][1] = "3"
    other["nets"][0]["name"] = "B"
    other["refusals"][0]["message"] = "n"
    assert b._differences(form, form) == []
    assert b._differences(form, other) == [
        {"at": "symbols[0].pins[1]", "kicad_cli": "2", "native": "3"},
        {"at": "nets[0].name", "kicad_cli": "A", "native": "B"},
        {"at": "refusals[0].message", "kicad_cli": "m", "native": "n"}]


def test_a_place_one_form_lacks_has_only_the_other_side():
    assert b._differences({"nets": [1], "a": 1}, {"nets": [1, 2], "b": None}) == [
        {"at": "nets[1]", "native": 2}, {"at": "a", "kicad_cli": 1},
        {"at": "b", "native": None}]


def test_a_leaf_of_another_type_is_one_difference():
    assert b._differences({"pins": ["1"]}, {"pins": {"1": None}}) == [
        {"at": "pins", "kicad_cli": ["1"], "native": {"1": None}}]


@pytest.mark.parametrize("case", ["stacked", "subsheet_local"])
def test_a_sheet_read_alike_both_ways_matches_though_front_ends_differ(tmp_path, case):
    result = _both_ways(tmp_path, case, _fake_kicad_cli(tmp_path, "copy"))
    assert result == {"word": "matches", "kicad_cli_version": "Eeschema 9.99.0-unknown",
                      "netlister": n.NETLISTER, "differences": []}
    assert json.loads(json.dumps(result)) == result


def test_reads_that_disagree_differ_by_place(tmp_path):
    result = _both_ways(tmp_path, "stacked", _fake_kicad_cli(tmp_path, "rename"))
    assert (result["word"], result["differences"]) == (
        "differs", [{"at": "nets[0].name", "kicad_cli": "STK2", "native": "STK"}])


def test_a_sheet_the_native_read_refuses_is_refused_with_its_reason(tmp_path):
    result = _both_ways(tmp_path, "k_bus", "no-such-kicad-cli")
    assert result == {"word": "refused", "kicad_cli_version": None, "netlister": n.NETLISTER,
                      "reason": "ext_k_bus.kicad_sch: bus at (50.8, 76.2) mm is not read by "
                                f"the native netlister{nt.REMEDY}"}


@pytest.mark.parametrize("act, word, reason", [
    (None, "unavailable", "no-such-kicad-cli is not on PATH"),
    ("none", "failed", "kicad-cli wrote no netlist"),
    ("exit", "failed", "kicad-cli exited with status 3 while writing the netlist"),
    ("unrunnable", "failed", "kicad-cli could not be started: {exe}: Exec format error"),
    ("garbage", "failed",
     "kicadsexpr netlist sheet.net is unreadable: unbalanced parenthesis")])
def test_without_two_reads_the_word_says_why_and_names_no_temporary_file(tmp_path, act, word,
                                                                         reason):
    exe = _fake_kicad_cli(tmp_path, act) if act else "no-such-kicad-cli"
    result = _both_ways(tmp_path, "stacked", exe)
    assert result == {"word": word, "kicad_cli_version": None, "netlister": n.NETLISTER,
                      "reason": reason.format(exe=exe)}


@pytest.mark.parametrize("act", ["copy", "none"])
def test_the_project_lock_kicad_cli_leaves_is_removed_and_nothing_else(tmp_path, act):
    sheet, library = nt._copy(tmp_path, "stacked")
    other = sheet.with_name("~other.kicad_pro.lck")
    other.write_text("{}", encoding="utf-8")
    before = sorted(tmp_path.rglob("*"))
    b.read_both_ways(sheet, library=library, mode="blank",
                     kicad_cli=_fake_kicad_cli(tmp_path, act))
    assert sorted(p for p in tmp_path.rglob("*") if "kicad-cli-" not in p.name) == before


def test_a_project_lock_there_before_the_read_stays(tmp_path):
    sheet, library = nt._copy(tmp_path, "stacked")
    lock = sheet.with_name(f"~{sheet.stem}.kicad_pro.lck")
    lock.write_text('{"hostname": "elsewhere"}', encoding="utf-8")
    b.read_both_ways(sheet, library=library, mode="blank",
                     kicad_cli=_fake_kicad_cli(tmp_path, "copy"))
    assert lock.is_file()


@pytest.mark.kicad_cli
@pytest.mark.parametrize("case", ["stacked", "subsheet_local"])
def test_kicad_cli_and_the_native_read_match_and_leave_the_sheet_folder_alone(tmp_path, case):
    if shutil.which("kicad-cli") is None:
        if os.environ.get("GDS_TO_KICAD_REQUIRE_KICAD_CLI") == "1":
            pytest.fail("GDS_TO_KICAD_REQUIRE_KICAD_CLI=1 but kicad-cli is not on PATH")
        pytest.skip("kicad-cli not on PATH (GDS_TO_KICAD_REQUIRE_KICAD_CLI=1 requires it)")
    sheet, library = nt._copy(tmp_path, case)
    before = sorted(tmp_path.rglob("*"))
    result = b.read_both_ways(sheet, library=library, mode="blank")
    assert (result["word"], result["differences"]) == ("matches", [])
    assert sorted(tmp_path.rglob("*")) == before
