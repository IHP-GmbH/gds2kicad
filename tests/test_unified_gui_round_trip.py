# SPDX-License-Identifier: GPL-3.0-or-later
"""The GUI keeps what a pin list states.

A project restore and the pin editor give back every key of a pin as it was
read (role, direction, port, ordinal, keys no reader knows, fractional centres,
pad polygons), never write a type into a pin that had none, and show a pin with
a role with the type its role and direction give, which the editor does not
change.

Headless: QT_QPA_PLATFORM=offscreen (CI sets it), and GDS_TO_KICAD_DATA_DIR is
set before unified_gui is imported, because DATA_DIR binds at class definition.
"""

import copy
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# A SIGNAL input with a key no reader knows added and a no-connect pad, both
# stating role, direction, port and ordinal, then a ground pad stating no type
# and no direction, with a fractional centre and a polygon, and a pad with no
# role that stores no_connect.
PINS = [
    {"name": "PAD_A", "type": "input", "side": "left", "pad_index": 0,
     "center_x_dbu": 100000.0, "center_y_dbu": 635000.0,
     "width_dbu": 60000.0, "height_dbu": 60000.0,
     "port": "in_a", "ordinal": 0, "net": "in_a", "direction": "INPUT",
     "role": "SIGNAL", "alias": "in_a", "electrode_dbu": None,
     "x_review_note": "kept as written"},
    {"name": "PAD_NC", "type": "no_connect", "side": "top", "pad_index": 2,
     "center_x_dbu": 500000.0, "center_y_dbu": 900000.0,
     "width_dbu": 60000.0, "height_dbu": 60000.0,
     "port": "PAD_NC", "ordinal": 0, "net": "NC", "direction": "NONE",
     "role": "NO_CONNECT", "alias": "PAD_NC", "electrode_dbu": None},
    {"name": "PAD_GND", "side": "bottom", "pad_index": 4,
     "center_x_dbu": 500000.5, "center_y_dbu": 100000.25,
     "width_dbu": 60000.0, "height_dbu": 60000.0,
     "polygon_points_dbu": [[470000.5, 70000.25], [530000.5, 70000.25],
                            [530000.5, 130000.25], [470000.5, 130000.25]],
     "port": "VSS", "ordinal": 0, "role": "GROUND"},
    {"name": "SPARE", "type": "no_connect", "side": "right", "pad_index": 5,
     "center_x_dbu": 900000.0, "center_y_dbu": 500000.0,
     "width_dbu": 60000.0, "height_dbu": 60000.0},
]
METADATA = {"version": "1.0", "chiplet_name": "fxdie", "gds_source": "layout.gds"}
SPARE_SAID = ('pin SPARE (pad_index 5): it has no role, so its KiCad type is the stored '
              'type "no_connect". The type field is deprecated from pins.json 1.1; write a '
              'role and a direction instead.')
BAD_ROLE_SAID = ('pin PAD_A (pad_index 0): role "signal" is not a pin-list role; write one '
                 'of SIGNAL, POWER, GROUND, NO_CONNECT, in capitals.')
# The tooltip of a disabled type combo, with and without a direction.
TIP = ("The type comes from role {role} and direction {direction}. To change it, save the "
       "pin list, edit the role and direction in that file and load it again.")
TIP_NO_DIRECTION = ("The type comes from role {role}. To change it, save the pin list, edit "
                    "the role and direction in that file and load it again.")


@pytest.fixture(scope="module")
def ug(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("GDS_TO_KICAD_DATA_DIR", str(tmp_path_factory.mktemp("data")))
        import unified_gui
    return unified_gui


@pytest.fixture(scope="module")
def app(ug):
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(ug, app, tmp_path, monkeypatch):
    """A main window writing under tmp_path, its log kept as (message,
    is_error) pairs in ``window.logged``."""
    out = tmp_path / "generated"
    for attr, value in (("DATA_DIR", tmp_path), ("DEFAULT_OUTPUT_DIR", out),
                        ("REGISTRY_PATH", out / "unified_registry.json"),
                        ("INVOCATION_DIR", tmp_path)):
        monkeypatch.setattr(ug.UnifiedMainWindow, attr, value)
    win = ug.UnifiedMainWindow()
    win.logged = []
    monkeypatch.setattr(win, "_log", lambda message, is_error=False:
                        win.logged.append((message, is_error)))
    yield win
    win.close()
    win.deleteLater()


def _project(ug, tmp_path, pins=PINS):
    return {"format": ug.UnifiedMainWindow.PROJECT_FORMAT,
            "version": ug.UnifiedMainWindow.PROJECT_VERSION,
            "gds_path": str(tmp_path / "fxdie.gds"), "lyp_path": "",
            "stripped_gds_path": "", "pad_layer": "", "text_layer": "",
            "extraction_source": 0,
            "pin_list": {**METADATA, "pins": copy.deepcopy(pins)}}


def _restored(window, ug, tmp_path, pins=PINS):
    window._load_project(_project(ug, tmp_path, pins))
    return window._project_document()["pin_list"]


def _row(window, name):
    table = window.pin_editor_table
    return [r for r in range(table.rowCount()) if table.item(r, 0).text() == name][0]


def test_a_project_restore_keeps_every_key_of_a_pin(window, ug, tmp_path):
    """Role, direction, port, ordinal and an unknown key survive the restore
    and the editor, dict-equal."""
    window._load_project(_project(ug, tmp_path))
    pins = [p.to_dict() for p in window.current_pin_list.pins]
    assert [p.get("role") for p in pins] == ["SIGNAL", "NO_CONNECT", "GROUND", None]
    assert pins == PINS
    doc = window._project_document()["pin_list"]
    assert doc["pins"] == PINS
    assert {k: v for k, v in doc.items() if k != "pins"} == METADATA


def test_a_fractional_centre_is_kept(window, ug, tmp_path):
    pin = _restored(window, ug, tmp_path)["pins"][2]
    assert (pin["center_x_dbu"], pin["center_y_dbu"]) == (500000.5, 100000.25)


def test_a_pad_polygon_is_kept(window, ug, tmp_path):
    pin = _restored(window, ug, tmp_path)["pins"][2]
    assert pin["polygon_points_dbu"] == PINS[2]["polygon_points_dbu"]
    assert "type" not in pin


def test_no_connect_is_kept_by_the_editor(window, ug, tmp_path):
    doc = _restored(window, ug, tmp_path)
    assert [p.get("type") for p in doc["pins"]] == ["input", "no_connect", None, "no_connect"]
    combo = window.pin_editor_table.cellWidget(_row(window, "SPARE"), 1)
    assert combo.currentText() == "no_connect"


def test_the_type_combo_of_a_pin_with_a_role_is_disabled(window, ug, tmp_path):
    window._load_project(_project(ug, tmp_path))
    combos = {name: window.pin_editor_table.cellWidget(_row(window, name), 1)
              for name in ("PAD_A", "PAD_NC", "PAD_GND", "SPARE")}
    assert {name: (c.currentText(), c.isEnabled(), c.toolTip())
            for name, c in combos.items()} == {
        "PAD_A": ("input", False, TIP.format(role="SIGNAL", direction="INPUT")),
        "PAD_NC": ("no_connect", False, TIP.format(role="NO_CONNECT", direction="NONE")),
        "PAD_GND": ("power_in", False, TIP_NO_DIRECTION.format(role="GROUND")),
        "SPARE": ("no_connect", True, ""),
    }


def test_a_restore_logs_each_read_warning(window, ug, tmp_path):
    window._load_project(_project(ug, tmp_path))
    warned = [m for m in window.logged if m[0].startswith("Warning: pin ")]
    assert warned == [(f"Warning: {SPARE_SAID}", True)]
    assert "Warnings: 1" in window.pin_editor_summary.text()


def test_a_renamed_and_retyped_pin_warns_as_it_now_is(window, ug, tmp_path):
    """The editor's rename and new type are read again, so the warning count
    holds no warning naming the old pin or the old word."""
    window._load_project(_project(ug, tmp_path))
    row = _row(window, "SPARE")
    window.pin_editor_table.item(row, 0).setText("SPARE_B")
    window.pin_editor_table.cellWidget(row, 1).setCurrentText("input")
    window._sync_editor_to_pin_list()
    assert window.current_pin_list.validate() == [
        'pin SPARE_B (pad_index 5): it has no role, so its KiCad type is the stored type '
        '"input". The type field is deprecated from pins.json 1.1; write a role and a '
        'direction instead.']


def test_a_refused_restore_leaves_the_open_project(window, ug, tmp_path):
    before = _restored(window, ug, tmp_path)
    gds = window.gds_path_edit.text()
    bad = copy.deepcopy(PINS)
    bad[0]["role"] = "signal"
    other = _project(ug, tmp_path / "other", bad)
    window._load_project(other)
    assert (f"Error reading project: pin list in the project file: {BAD_ROLE_SAID}",
            True) in window.logged
    assert window.gds_path_edit.text() == gds
    assert window._project_document()["pin_list"] == before


def _open_dialog(ug, monkeypatch, path):
    monkeypatch.setattr(ug.QFileDialog, "getOpenFileName",
                        lambda *a, **k: (str(path), ""))


def test_loading_a_pin_list_logs_each_read_warning(window, ug, tmp_path, monkeypatch):
    path = tmp_path / "fxdie.pins.json"
    path.write_text(json.dumps({**METADATA, "pins": PINS}), encoding="utf-8")
    _open_dialog(ug, monkeypatch, path)
    window.gds_path_edit.setText(str(tmp_path / "fxdie.gds"))
    window._load_pin_list_file()
    warned = [m for m in window.logged if m[0].startswith("Warning: pin ")]
    assert warned == [(f"Warning: {SPARE_SAID}", True)]
    assert window._project_document()["pin_list"]["pins"] == PINS


def test_a_pin_list_kicad_type_refuses_is_logged_naming_it(window, ug, tmp_path,
                                                           monkeypatch):
    bad = copy.deepcopy(PINS)
    bad[0]["role"] = "signal"
    path = tmp_path / "bad.pins.json"
    path.write_text(json.dumps({**METADATA, "pins": bad}), encoding="utf-8")
    _open_dialog(ug, monkeypatch, path)
    window._load_pin_list_file()
    assert (f"Error loading pin list: pin list {path}: {BAD_ROLE_SAID}", True) \
        in window.logged
    assert window.current_pin_list is None


def test_the_tooltip_path_changes_the_type(window, ug, tmp_path, monkeypatch):
    """What the tooltip says works for a restored project: save the pin list,
    edit the role and direction in that file and load it again."""
    window._load_project(_project(ug, tmp_path))
    path = tmp_path / "saved.pins.json"
    monkeypatch.setattr(ug.QFileDialog, "getSaveFileName",
                        lambda *a, **k: (str(path), ""))
    window._save_pin_list_file()
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["pins"][0]["direction"] = "OUTPUT"
    path.write_text(json.dumps(doc), encoding="utf-8")
    _open_dialog(ug, monkeypatch, path)
    window._load_pin_list_file()
    combo = window.pin_editor_table.cellWidget(_row(window, "PAD_A"), 1)
    assert (combo.currentText(), combo.isEnabled(), combo.toolTip()) == (
        "output", False, TIP.format(role="SIGNAL", direction="OUTPUT"))


def test_a_renamed_extracted_pin_gains_no_warning(window, ug, tmp_path):
    """An extracted pin was never read, so a rename in the editor does not
    make it warn while its neighbours stay silent."""
    window.current_pin_list = ug.PinList(
        pins=[ug.PinEntry(name=n, type="input", side="left", pad_index=i)
              for i, n in enumerate(("A0", "A1"))],
        metadata=dict(METADATA))
    window._load_pin_list_into_editor()
    window.pin_editor_table.item(_row(window, "A0"), 0).setText("A0_B")
    window._sync_editor_to_pin_list()
    assert window.current_pin_list.validate() == []
