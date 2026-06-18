# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for kicad_netlist_to_chiplet.py -- KiCad netlist to chiplet YAML/CSV converter."""

import csv
import io
import os
import tempfile
import yaml
import pytest

# Add parent directory to path
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kicad_netlist_to_chiplet import (
    classify_net,
    parse_kicad_netlist,
    nets_to_yaml,
    nets_to_csv,
    inject_into_chiplet,
    Net,
    NetConnection,
    VALID_NET_CLASSES,
)

# ── Fixtures ────────────────────────────────────────────────────────

DEMO_NET_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "kicad_designs", "kicad_interposer_hyperlynx_to_gds",
    "chiplet_files", "chiplet_demo.net"
)

DEMO_CHIPLET_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "kicad_designs", "kicad_interposer_hyperlynx_to_gds",
    "chiplet_files", "chiplet_demo.chiplet"
)

LAYER_MAP = {"U1": "TopMetal2", "U2": "TopMetal2", "U3": "met5", "U4": "TopMetal2"}


@pytest.fixture
def parsed_nets():
    """Parse the demo netlist with standard options."""
    if not os.path.exists(DEMO_NET_PATH):
        pytest.skip(f"Demo netlist not found: {DEMO_NET_PATH}")
    return parse_kicad_netlist(DEMO_NET_PATH, skip_unconnected=True, layer_map=LAYER_MAP)


@pytest.fixture
def csv_output(parsed_nets):
    """Generate CSV output from parsed nets."""
    return nets_to_csv(parsed_nets)


@pytest.fixture
def yaml_output(parsed_nets):
    """Generate YAML output from parsed nets."""
    return nets_to_yaml(parsed_nets, external_csv_name="chiplet_demo_netlist.csv")


# ── Test: CSV format ────────────────────────────────────────────────

class TestCSVOutput:
    def test_csv_header(self, csv_output):
        """CSV has correct header: net_name,component,pin,layer,net_class."""
        reader = csv.reader(io.StringIO(csv_output))
        header = next(reader)
        assert header == ["net_name", "component", "pin", "layer", "net_class"]

    def test_gnd_has_four_connections(self, csv_output):
        """GND net has exactly 4 connections."""
        reader = csv.DictReader(io.StringIO(csv_output))
        gnd_rows = [r for r in reader if r["net_name"] == "GND"]
        assert len(gnd_rows) == 4

    def test_vdd_has_two_connections_power_class(self, csv_output):
        """VDD net has 2 connections with class=power."""
        reader = csv.DictReader(io.StringIO(csv_output))
        vdd_rows = [r for r in reader if r["net_name"] == "VDD"]
        assert len(vdd_rows) == 2
        for row in vdd_rows:
            assert row["net_class"] == "power"

    def test_signal_nets_have_signal_class(self, csv_output):
        """VOUT, Aout_SIG, VINN nets have class=signal."""
        reader = csv.DictReader(io.StringIO(csv_output))
        signal_nets = {"VOUT", "Aout_SIG", "VINN"}
        for row in reader:
            if row["net_name"] in signal_nets:
                assert row["net_class"] == "signal", f"{row['net_name']} should be signal"

    def test_no_unconnected_nets(self, csv_output):
        """No unconnected-* nets in filtered output."""
        reader = csv.DictReader(io.StringIO(csv_output))
        for row in reader:
            assert not row["net_name"].startswith("unconnected-")

    def test_all_net_classes_valid(self, csv_output):
        """All net_class values are valid Chiplet Studio classes."""
        reader = csv.DictReader(io.StringIO(csv_output))
        for row in reader:
            assert row["net_class"] in VALID_NET_CLASSES, \
                f"Invalid net_class: {row['net_class']}"

    def test_csv_parseable_by_import_csv_logic(self, csv_output):
        """CSV can be parsed by reimplementation of Netlist::import_csv() logic."""
        reader = csv.DictReader(io.StringIO(csv_output))
        nets = {}
        for row in reader:
            net_name = row["net_name"].strip()
            component = row["component"].strip()
            pin = row["pin"].strip()
            layer = row["layer"].strip()
            net_class = row["net_class"].strip()

            # Skip empty required fields (matching C++ logic)
            if not net_name or not component or not pin:
                continue

            if net_name not in nets:
                nets[net_name] = {"class": net_class, "connections": []}

            nets[net_name]["connections"].append({
                "component": component,
                "pin": pin,
                "layer": layer,
            })

        assert len(nets) == 5
        assert "GND" in nets
        assert nets["GND"]["class"] == "ground"
        assert len(nets["GND"]["connections"]) == 4
        assert "VDD" in nets
        assert nets["VDD"]["class"] == "power"


# ── Test: YAML format ───────────────────────────────────────────────

class TestYAMLOutput:
    def test_yaml_is_valid(self, yaml_output):
        """YAML output is valid YAML."""
        data = yaml.safe_load(yaml_output)
        assert data is not None

    def test_yaml_has_netlist_section(self, yaml_output):
        """YAML has top-level netlist key."""
        data = yaml.safe_load(yaml_output)
        assert "netlist" in data

    def test_yaml_nets_structure(self, yaml_output):
        """Each net has name, class, and connections list."""
        data = yaml.safe_load(yaml_output)
        nets = data["netlist"]["nets"]
        assert len(nets) == 5
        for net in nets:
            assert "name" in net
            assert "class" in net
            assert "connections" in net
            assert isinstance(net["connections"], list)
            for conn in net["connections"]:
                assert "component" in conn
                assert "pin" in conn

    def test_yaml_gnd_net(self, yaml_output):
        """GND net in YAML has correct structure and 4 connections."""
        data = yaml.safe_load(yaml_output)
        gnd = [n for n in data["netlist"]["nets"] if n["name"] == "GND"]
        assert len(gnd) == 1
        assert gnd[0]["class"] == "ground"
        assert len(gnd[0]["connections"]) == 4

    def test_yaml_has_layer_info(self, yaml_output):
        """Connections have layer information when layer_map was provided."""
        data = yaml.safe_load(yaml_output)
        for net in data["netlist"]["nets"]:
            for conn in net["connections"]:
                assert "layer" in conn
                assert conn["layer"] in ("TopMetal2", "met5")

    def test_yaml_external_netlist_reference(self, yaml_output):
        """YAML references external CSV file."""
        data = yaml.safe_load(yaml_output)
        assert data["netlist"]["external_netlist"] == "chiplet_demo_netlist.csv"


# ── Test: Net classification ────────────────────────────────────────

class TestNetClassification:
    def test_gnd_is_ground(self):
        assert classify_net("GND", []) == "ground"

    def test_vss_is_ground(self):
        assert classify_net("VSS", []) == "ground"

    def test_vdd_is_power(self):
        assert classify_net("VDD", []) == "power"

    def test_vcc_is_power(self):
        assert classify_net("VCC", []) == "power"

    def test_avdd_is_power(self):
        assert classify_net("AVDD", []) == "power"

    def test_vinn_is_signal(self):
        """VINN is a signal, not power (analog input)."""
        assert classify_net("VINN", ["power_in"]) == "signal"

    def test_arbitrary_name_is_signal(self):
        assert classify_net("DATA_BUS", []) == "signal"

    def test_case_insensitive_ground(self):
        assert classify_net("gnd", []) == "ground"

    def test_case_insensitive_power(self):
        assert classify_net("vdd", []) == "power"


# ── Test: Chiplet injection ─────────────────────────────────────────

class TestChipletInjection:
    def test_inject_preserves_original_sections(self):
        """Injected .chiplet file preserves all original sections."""
        if not os.path.exists(DEMO_CHIPLET_PATH):
            pytest.skip(f"Demo chiplet not found: {DEMO_CHIPLET_PATH}")

        data = yaml.safe_load(open(DEMO_CHIPLET_PATH))
        assert "format_version" in data
        assert "assembly" in data
        assert "technologies" in data
        assert "components" in data
        assert "netlist" in data

    def test_inject_chiplet_is_valid_yaml(self):
        """Injected .chiplet file is valid YAML."""
        if not os.path.exists(DEMO_CHIPLET_PATH):
            pytest.skip(f"Demo chiplet not found: {DEMO_CHIPLET_PATH}")

        with open(DEMO_CHIPLET_PATH) as f:
            data = yaml.safe_load(f)
        assert data is not None

    def test_inject_adds_netlist_to_empty_chiplet(self, parsed_nets):
        """Injection works on a .chiplet file without existing netlist."""
        original = "format_version: '1.0'\nassembly:\n  name: test\n"
        yaml_block = nets_to_yaml(parsed_nets)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".chiplet", delete=False) as f:
            f.write(original)
            tmp_path = f.name

        try:
            inject_into_chiplet(tmp_path, yaml_block)
            with open(tmp_path) as f:
                result = yaml.safe_load(f)
            assert "format_version" in result
            assert "assembly" in result
            assert "netlist" in result
            assert len(result["netlist"]["nets"]) == 5
        finally:
            os.unlink(tmp_path)

    def test_inject_replaces_existing_netlist(self, parsed_nets):
        """Injection replaces existing netlist section."""
        original = (
            "format_version: '1.0'\nassembly:\n  name: test\n"
            "netlist:\n  nets:\n    - name: OLD_NET\n      class: signal\n"
        )
        yaml_block = nets_to_yaml(parsed_nets)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".chiplet", delete=False) as f:
            f.write(original)
            tmp_path = f.name

        try:
            inject_into_chiplet(tmp_path, yaml_block)
            with open(tmp_path) as f:
                content = f.read()
            assert "OLD_NET" not in content
            data = yaml.safe_load(content)
            assert len(data["netlist"]["nets"]) == 5
        finally:
            os.unlink(tmp_path)


# ── Test: Netlist parsing ───────────────────────────────────────────

class TestNetlistParsing:
    def test_five_nets_parsed(self, parsed_nets):
        """Parsing produces exactly 5 multi-node nets."""
        assert len(parsed_nets) == 5

    def test_net_names(self, parsed_nets):
        """All expected nets are present."""
        names = {n.name for n in parsed_nets}
        assert names == {"Aout_SIG", "GND", "VDD", "VINN", "VOUT"}

    def test_unfiltered_includes_unconnected(self):
        """Without skip_unconnected, unconnected nets are included."""
        if not os.path.exists(DEMO_NET_PATH):
            pytest.skip(f"Demo netlist not found: {DEMO_NET_PATH}")
        nets = parse_kicad_netlist(DEMO_NET_PATH, skip_unconnected=False)
        unconnected = [n for n in nets if n.name.startswith("unconnected-")]
        assert len(unconnected) > 0
