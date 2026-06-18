#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
GDS to KiCad Footprint Converter - GUI Application

PyQt6-based graphical interface for converting GDSII files to KiCad footprints.
Uses KLayout .lyp files for layer definitions.
"""

import sys
import json
import shutil
import uuid
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit, QFileDialog,
    QGroupBox, QMessageBox, QTabWidget, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QComboBox, QCompleter
)
from PyQt6.QtCore import Qt, QStringListModel
from PyQt6.QtGui import QFont, QColor

# Import converter classes from CLI script
from gds_to_kicad import LYPParser, GDSToKiCad
from pin_extractor import PinExtractor
from _paths import resolve_data_dir


# =============================================================================
# Color Theme (Nord Dark)
# =============================================================================
COLORS = {
    'background': '#2e3440',      # Polar Night - darkest
    'background_alt': '#3b4252',  # Polar Night - medium
    'text_primary': '#eceff4',    # Snow Storm - white
    'text_secondary': '#d8dee9',  # Snow Storm - light gray
    'accent': '#5e81ac',          # Frost - blue
    'button': '#4c566a',          # Polar Night - lightest
    'button_hover': '#434c5e',    # Polar Night - medium dark
    'border': '#4c566a',          # Polar Night - lightest
    'success': '#a3be8c',         # Aurora - green
    'error': '#bf616a',           # Aurora - red
    'warning': '#ebcb8b',         # Aurora - yellow
}

STYLESHEET = f"""
QMainWindow {{
    background-color: {COLORS['background']};
}}
QWidget {{
    background-color: {COLORS['background']};
    color: {COLORS['text_primary']};
    font-family: 'Monospace', 'Courier New', monospace;
}}
QGroupBox {{
    background-color: {COLORS['background_alt']};
    border: 1px solid {COLORS['accent']};
    border-radius: 5px;
    margin-top: 10px;
    padding-top: 10px;
    font-weight: bold;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
    color: {COLORS['text_primary']};
}}
QLabel {{
    color: {COLORS['text_primary']};
    background-color: transparent;
}}
QPushButton {{
    background-color: {COLORS['button']};
    color: {COLORS['text_primary']};
    border: 1px solid {COLORS['border']};
    border-radius: 4px;
    padding: 8px 16px;
    min-width: 80px;
}}
QPushButton:hover {{
    background-color: {COLORS['button_hover']};
}}
QPushButton:pressed {{
    background-color: {COLORS['accent']};
}}
QPushButton:disabled {{
    background-color: #333333;
    color: #666666;
    border-color: #444444;
}}
QLineEdit {{
    background-color: {COLORS['background_alt']};
    color: {COLORS['text_primary']};
    border: 1px solid {COLORS['accent']};
    border-radius: 4px;
    padding: 6px;
    selection-background-color: {COLORS['button']};
}}
QLineEdit:focus {{
    border-color: {COLORS['border']};
}}
QComboBox {{
    background-color: {COLORS['background_alt']};
    color: {COLORS['text_primary']};
    border: 1px solid {COLORS['accent']};
    border-radius: 4px;
    padding: 6px;
    min-width: 200px;
}}
QComboBox:focus {{
    border-color: {COLORS['border']};
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QComboBox::down-arrow {{
    image: none;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 5px solid {COLORS['text_primary']};
    margin-right: 5px;
}}
QComboBox QAbstractItemView {{
    background-color: {COLORS['background_alt']};
    color: {COLORS['text_primary']};
    border: 1px solid {COLORS['accent']};
    selection-background-color: {COLORS['accent']};
}}
QTextEdit {{
    background-color: {COLORS['background']};
    color: {COLORS['text_primary']};
    border: 1px solid {COLORS['accent']};
    border-radius: 4px;
    font-family: 'Monospace', 'Courier New', monospace;
    font-size: 11px;
}}
QTabWidget::pane {{
    border: 1px solid {COLORS['accent']};
    border-radius: 4px;
    background-color: {COLORS['background_alt']};
}}
QTabBar::tab {{
    background-color: {COLORS['button']};
    color: {COLORS['text_primary']};
    padding: 8px 20px;
    margin-right: 2px;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
}}
QTabBar::tab:selected {{
    background-color: {COLORS['accent']};
}}
QTabBar::tab:hover:!selected {{
    background-color: {COLORS['button_hover']};
}}
QTableWidget {{
    background-color: {COLORS['background']};
    color: {COLORS['text_primary']};
    border: 1px solid {COLORS['accent']};
    gridline-color: {COLORS['button']};
    selection-background-color: {COLORS['accent']};
}}
QTableWidget::item {{
    padding: 5px;
}}
QHeaderView::section {{
    background-color: {COLORS['button']};
    color: {COLORS['text_primary']};
    padding: 5px;
    border: 1px solid {COLORS['background']};
    font-weight: bold;
}}
"""


# =============================================================================
# Filterable ComboBox
# =============================================================================
class FilterableComboBox(QComboBox):
    """ComboBox with text filtering/search capability."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)

        # Setup completer for filtering
        self._completer = QCompleter()
        self._completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.setCompleter(self._completer)

        # Model for completer
        self._model = QStringListModel()
        self._completer.setModel(self._model)

        # Store items for filtering
        self._items: List[str] = []

    def setItems(self, items: List[str]):
        """Set the items in the combobox."""
        self._items = items
        self.clear()
        self.addItems(items)
        self._model.setStringList(items)

    def getSelectedItem(self) -> str:
        """Get the currently selected/entered item."""
        return self.currentText()


# =============================================================================
# Conversion Registry (JSON Database)
# =============================================================================
class ConversionRegistry:
    """Manages JSON database of conversion records."""

    def __init__(self, registry_path: str):
        self.registry_path = Path(registry_path)
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.data: Dict = {"conversions": []}
        self.load()

    def load(self):
        """Load registry from JSON file."""
        if self.registry_path.exists():
            try:
                with open(self.registry_path, 'r') as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, IOError):
                self.data = {"conversions": []}

    def save(self):
        """Save registry to JSON file."""
        with open(self.registry_path, 'w') as f:
            json.dump(self.data, f, indent=2)

    def add_entry(self, gds_source: str, output_path: str, layer_name: str,
                  pad_count: int, lyp_file: str) -> str:
        """Add a conversion entry and return its ID."""
        entry_id = str(uuid.uuid4())[:8]
        entry = {
            "id": entry_id,
            "timestamp": datetime.now().isoformat(),
            "gds_source": str(gds_source),
            "output_path": str(output_path),
            "layer_name": layer_name,
            "lyp_file": lyp_file,
            "pad_count": pad_count,
            "copied_to_library": False,
            "library_path": None
        }
        self.data["conversions"].append(entry)
        self.save()
        return entry_id

    def update_library_status(self, entry_id: str, library_path: str):
        """Mark an entry as copied to library."""
        for entry in self.data["conversions"]:
            if entry["id"] == entry_id:
                entry["copied_to_library"] = True
                entry["library_path"] = str(library_path)
                self.save()
                break

    def get_entries(self) -> List[Dict]:
        """Get all conversion entries."""
        return self.data["conversions"]

    def get_latest_entry(self) -> Optional[Dict]:
        """Get the most recent conversion entry."""
        if self.data["conversions"]:
            return self.data["conversions"][-1]
        return None

    def delete_entry(self, entry_id: str) -> bool:
        """Delete a conversion entry by ID."""
        for i, entry in enumerate(self.data["conversions"]):
            if entry["id"] == entry_id:
                del self.data["conversions"][i]
                self.save()
                return True
        return False


# =============================================================================
# Main Window
# =============================================================================
class MainWindow(QMainWindow):
    """Main application window."""

    # Default paths -- writable base (CWD or $GDS_TO_KICAD_DATA_DIR), never the
    # read-only install dir. See _paths.resolve_data_dir().
    DATA_DIR = resolve_data_dir()
    DEFAULT_OUTPUT_DIR = DATA_DIR / "generated_kicad_footprint_files"
    LIBRARY_DIR = DATA_DIR / "kicad_interposer_lib" / "Interposer.pretty"
    REGISTRY_PATH = DATA_DIR / "kicad_interposer_lib" / "conversions_registry.json"

    def __init__(self):
        super().__init__()
        self.setWindowTitle("GDS to KiCad Footprint Converter")
        self.setMinimumSize(900, 700)

        # Initialize registry
        self.registry = ConversionRegistry(str(self.REGISTRY_PATH))

        # Track last conversion
        self.last_output_path: Optional[Path] = None
        self.last_entry_id: Optional[str] = None

        # Current LYP parser
        self.lyp_parser: Optional[LYPParser] = None

        # Setup UI
        self._setup_ui()
        self.setStyleSheet(STYLESHEET)

    def _setup_ui(self):
        """Setup the user interface."""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(15)
        main_layout.setContentsMargins(20, 20, 20, 20)

        # Title
        title_label = QLabel("GDS to KiCad Footprint Converter")
        title_label.setFont(QFont("Monospace", 16, QFont.Weight.Bold))
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(title_label)

        # Tab Widget
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)

        # ===== CONVERSION TAB =====
        conversion_tab = QWidget()
        conversion_layout = QVBoxLayout(conversion_tab)

        # Input Group
        input_group = QGroupBox("Input Configuration")
        input_layout = QVBoxLayout(input_group)

        # GDS File selector
        gds_layout = QHBoxLayout()
        gds_label = QLabel("GDS File:")
        gds_label.setFixedWidth(100)
        self.gds_path_edit = QLineEdit()
        self.gds_path_edit.setPlaceholderText("Select a GDSII file...")
        gds_browse_btn = QPushButton("Browse...")
        gds_browse_btn.clicked.connect(self._select_gds_file)
        gds_layout.addWidget(gds_label)
        gds_layout.addWidget(self.gds_path_edit)
        gds_layout.addWidget(gds_browse_btn)
        input_layout.addLayout(gds_layout)

        # LYP File selector (NEW)
        lyp_layout = QHBoxLayout()
        lyp_label = QLabel("LYP File:")
        lyp_label.setFixedWidth(100)
        self.lyp_path_edit = QLineEdit()
        self.lyp_path_edit.setPlaceholderText("Select a KLayout .lyp file for layer definitions...")
        lyp_browse_btn = QPushButton("Browse...")
        lyp_browse_btn.clicked.connect(self._select_lyp_file)
        lyp_layout.addWidget(lyp_label)
        lyp_layout.addWidget(self.lyp_path_edit)
        lyp_layout.addWidget(lyp_browse_btn)
        input_layout.addLayout(lyp_layout)

        # Layer selector (FilterableComboBox instead of QLineEdit)
        layer_layout = QHBoxLayout()
        layer_label = QLabel("Layer:")
        layer_label.setFixedWidth(100)
        self.layer_combo = FilterableComboBox()
        self.layer_combo.setPlaceholderText("Select LYP file first...")
        self.layer_combo.setEnabled(False)
        self.scan_btn = QPushButton("Scan GDS")
        self.scan_btn.setToolTip("Scan GDS to auto-detect pad and text layers")
        self.scan_btn.setEnabled(False)
        self.scan_btn.clicked.connect(self._scan_gds_layers)
        layer_layout.addWidget(layer_label)
        layer_layout.addWidget(self.layer_combo)
        layer_layout.addWidget(self.scan_btn)
        input_layout.addLayout(layer_layout)

        # Text Layer selector (for pin name extraction)
        text_layer_layout = QHBoxLayout()
        text_layer_label = QLabel("Text Layer:")
        text_layer_label.setFixedWidth(100)
        self.text_layer_combo = FilterableComboBox()
        self.text_layer_combo.setPlaceholderText("(Auto-detect or select)")
        self.text_layer_combo.setEnabled(False)
        text_layer_layout.addWidget(text_layer_label)
        text_layer_layout.addWidget(self.text_layer_combo)
        text_layer_layout.addStretch()
        input_layout.addLayout(text_layer_layout)

        # Output Directory selector
        output_layout = QHBoxLayout()
        output_label = QLabel("Output Dir:")
        output_label.setFixedWidth(100)
        self.output_dir_edit = QLineEdit()
        self.output_dir_edit.setText(str(self.DEFAULT_OUTPUT_DIR))
        output_browse_btn = QPushButton("Browse...")
        output_browse_btn.clicked.connect(self._select_output_dir)
        output_layout.addWidget(output_label)
        output_layout.addWidget(self.output_dir_edit)
        output_layout.addWidget(output_browse_btn)
        input_layout.addLayout(output_layout)

        # Library Directory selector
        library_layout = QHBoxLayout()
        library_label = QLabel("Library Dir:")
        library_label.setFixedWidth(100)
        self.library_dir_edit = QLineEdit()
        self.library_dir_edit.setText(str(self.LIBRARY_DIR))
        self.library_dir_edit.setPlaceholderText("KiCad footprint library directory...")
        library_browse_btn = QPushButton("Browse...")
        library_browse_btn.clicked.connect(self._select_library_dir)
        library_layout.addWidget(library_label)
        library_layout.addWidget(self.library_dir_edit)
        library_layout.addWidget(library_browse_btn)
        input_layout.addLayout(library_layout)

        conversion_layout.addWidget(input_group)

        # Action Buttons
        action_layout = QHBoxLayout()

        self.convert_btn = QPushButton("Convert to Footprint")
        self.convert_btn.setMinimumHeight(40)
        self.convert_btn.clicked.connect(self._convert_gds)

        self.copy_btn = QPushButton("Copy to Library")
        self.copy_btn.setEnabled(False)
        self.copy_btn.clicked.connect(self._copy_to_library)

        self.move_btn = QPushButton("Move to Library")
        self.move_btn.setEnabled(False)
        self.move_btn.clicked.connect(self._move_to_library)

        action_layout.addWidget(self.convert_btn)
        action_layout.addWidget(self.copy_btn)
        action_layout.addWidget(self.move_btn)

        conversion_layout.addLayout(action_layout)

        # Log/Status area
        log_group = QGroupBox("Log")
        log_layout = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(180)
        log_layout.addWidget(self.log_text)
        conversion_layout.addWidget(log_group)

        self.tabs.addTab(conversion_tab, "Conversion")

        # ===== HISTORY TAB =====
        history_tab = QWidget()
        history_layout = QVBoxLayout(history_tab)

        # History table
        self.history_table = QTableWidget()
        self.history_table.setColumnCount(6)
        self.history_table.setHorizontalHeaderLabels([
            "Date", "GDS File", "Layer", "Pads", "Output", "In Library"
        ])
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.horizontalHeader().setStretchLastSection(True)
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        history_layout.addWidget(self.history_table)

        # History buttons
        history_btn_layout = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_history)
        delete_btn = QPushButton("Delete Selected")
        delete_btn.clicked.connect(self._delete_history_entry)
        history_btn_layout.addWidget(refresh_btn)
        history_btn_layout.addWidget(delete_btn)
        history_btn_layout.addStretch()
        history_layout.addLayout(history_btn_layout)

        self.tabs.addTab(history_tab, "History")

        # Status bar
        self.statusBar().showMessage("Ready")
        self.statusBar().setStyleSheet(f"color: {COLORS['text_secondary']};")

        self._log("Application started. Select a GDS file and LYP file to begin.")
        self._refresh_history()

    def _log(self, message: str, is_error: bool = False):
        """Add message to log area."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        color = COLORS['error'] if is_error else COLORS['text_primary']
        self.log_text.append(f'<span style="color: {COLORS["text_secondary"]}">[{timestamp}]</span> '
                            f'<span style="color: {color}">{message}</span>')

    def _display_bounding_box_info(self, converter_output: str):
        """Parse and display bounding box info from converter output."""
        lines = converter_output.split('\n')
        bb_info = []

        in_bb_section = False
        for line in lines:
            if 'GDS Bounding Box' in line:
                in_bb_section = True
                bb_info.append(line.strip())
                continue
            if in_bb_section:
                if line.strip().startswith(('Min:', 'Max:', 'Size:')):
                    bb_info.append(line.strip())
                elif 'KiCad Bounding Box' in line:
                    bb_info.append(line.strip())
                elif 'Coordinate transformation' in line:
                    bb_info.append(line.strip())
                elif 'KiCad anchor' in line:
                    bb_info.append(line.strip())
                    in_bb_section = False
                elif line.strip() == '':
                    continue

        if bb_info:
            self._log("--- Coordinate Info ---")
            for info in bb_info:
                self._log(f"  {info}")
            self._log("-----------------------")

    def _select_gds_file(self):
        """Open file dialog to select GDS file."""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select GDSII File",
            str(Path.cwd()),
            "GDSII Files (*.gds *.GDS);;All Files (*)"
        )
        if file_path:
            self.gds_path_edit.setText(file_path)
            self._log(f"Selected GDS: {Path(file_path).name}")
            self.scan_btn.setEnabled(bool(self.lyp_parser))

    def _select_lyp_file(self):
        """Open file dialog to select LYP file and load layers."""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select KLayout Layer Properties File",
            str(Path.home()),
            "LYP Files (*.lyp);;All Files (*)"
        )
        if file_path:
            self.lyp_path_edit.setText(file_path)
            self._load_layers_from_lyp(file_path)

    def _load_layers_from_lyp(self, lyp_path: str):
        """Load layers from LYP file and populate the combo boxes."""
        try:
            self.lyp_parser = LYPParser(lyp_path)
            layer_display_names = self.lyp_parser.get_layer_display_names()

            self.layer_combo.setEnabled(True)
            self.layer_combo.setItems(layer_display_names)

            # Populate text layer combo
            self.text_layer_combo.setEnabled(True)
            text_items = ["(Auto-detect)", "(None - sequential numbering)"] + layer_display_names
            self.text_layer_combo.setItems(text_items)
            self.text_layer_combo.setCurrentIndex(0)

            self._log(f"Loaded {len(layer_display_names)} layers from {Path(lyp_path).name}")
            self.statusBar().showMessage(f"Loaded {len(layer_display_names)} layers")

            # Enable scan if GDS is already selected
            gds_path = self.gds_path_edit.text().strip()
            self.scan_btn.setEnabled(bool(gds_path and Path(gds_path).exists()))

            # Try to select a default layer if available
            for i, name in enumerate(layer_display_names):
                if 'TopMetal2.drawing' in name:
                    self.layer_combo.setCurrentIndex(i)
                    break

        except Exception as e:
            self._log(f"Error loading LYP file: {e}", is_error=True)
            self.lyp_parser = None
            self.layer_combo.setEnabled(False)
            self.layer_combo.clear()
            self.text_layer_combo.setEnabled(False)
            self.text_layer_combo.clear()

    def _select_output_dir(self):
        """Open directory dialog to select output directory."""
        dir_path = QFileDialog.getExistingDirectory(
            self,
            "Select Output Directory",
            str(self.DEFAULT_OUTPUT_DIR)
        )
        if dir_path:
            self.output_dir_edit.setText(dir_path)
            self._log(f"Output directory: {dir_path}")

    def _select_library_dir(self):
        """Open directory dialog to select KiCad library directory."""
        dir_path = QFileDialog.getExistingDirectory(
            self,
            "Select KiCad Library Directory",
            self.library_dir_edit.text() or str(self.LIBRARY_DIR)
        )
        if dir_path:
            self.library_dir_edit.setText(dir_path)
            self._log(f"Library directory: {dir_path}")

    def _refresh_history(self):
        """Load conversion history into table."""
        self.history_table.setRowCount(0)
        entries = self.registry.get_entries()

        for entry in reversed(entries):  # Show newest first
            row = self.history_table.rowCount()
            self.history_table.insertRow(row)

            # Parse timestamp
            try:
                dt = datetime.fromisoformat(entry["timestamp"])
                date_str = dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, KeyError):
                date_str = "Unknown"

            # Get filename only
            gds_name = Path(entry.get("gds_source", "")).name

            # Get layer name (handle both old 'metal_layer' and new 'layer_name')
            layer_name = entry.get("layer_name", entry.get("metal_layer", ""))

            # Set items
            self.history_table.setItem(row, 0, QTableWidgetItem(date_str))
            self.history_table.setItem(row, 1, QTableWidgetItem(gds_name))
            self.history_table.setItem(row, 2, QTableWidgetItem(layer_name))
            self.history_table.setItem(row, 3, QTableWidgetItem(str(entry.get("pad_count", 0))))
            self.history_table.setItem(row, 4, QTableWidgetItem(Path(entry.get("output_path", "")).name))

            # In Library indicator
            in_lib = entry.get("copied_to_library", False)
            lib_item = QTableWidgetItem("Yes" if in_lib else "No")
            if in_lib:
                lib_item.setForeground(QColor(COLORS['success']))
            else:
                lib_item.setForeground(QColor(COLORS['text_secondary']))
            self.history_table.setItem(row, 5, lib_item)

            # Store entry ID in first column for deletion
            self.history_table.item(row, 0).setData(Qt.ItemDataRole.UserRole, entry.get("id"))

    def _delete_history_entry(self):
        """Delete selected entry from history."""
        selected = self.history_table.selectedItems()
        if not selected:
            QMessageBox.warning(self, "No Selection", "Please select an entry to delete.")
            return

        row = selected[0].row()
        entry_id = self.history_table.item(row, 0).data(Qt.ItemDataRole.UserRole)

        if not entry_id:
            return

        reply = QMessageBox.question(
            self,
            "Confirm Delete",
            "Are you sure you want to delete this entry from history?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            if self.registry.delete_entry(entry_id):
                self._refresh_history()
                self._log(f"Deleted history entry: {entry_id}")
            else:
                self._log("Error: Could not delete entry", is_error=True)

    def _scan_gds_layers(self):
        """Scan GDS file and auto-select suggested pad/text layers."""
        gds_path = self.gds_path_edit.text().strip()
        if not gds_path or not Path(gds_path).exists():
            self._log("Error: Select a valid GDS file first", is_error=True)
            return

        self._log(f"Scanning {Path(gds_path).name}...")
        self.statusBar().showMessage("Scanning GDS layers...")
        QApplication.processEvents()

        try:
            result = PinExtractor.scan_gds_layers(gds_path, self.lyp_parser)

            # Log pad candidates
            pad_cands = result['pad_candidates'][:10]
            self._log(f"Found {len(result['pad_candidates'])} layers with pad shapes:")
            for c in pad_cands:
                name = c['name'] or f"{c['layer_num']}/{c['datatype']}"
                total = c['boxes'] + c['polygons']
                self._log(f"  {name} ({c['layer_num']}/{c['datatype']}): {total} shapes")

            # Log text candidates
            text_cands = result['text_candidates'][:10]
            self._log(f"Found {len(result['text_candidates'])} layers with text labels:")
            for c in text_cands:
                name = c['name'] or f"{c['layer_num']}/{c['datatype']}"
                self._log(f"  {name} ({c['layer_num']}/{c['datatype']}): {c['texts']} texts")

            # Auto-select suggested pad layer
            suggested_pad = result['suggested_pad_layer']
            if suggested_pad:
                for i in range(self.layer_combo.count()):
                    d = self.layer_combo.itemText(i)
                    if d.startswith(suggested_pad + ' (') or d == suggested_pad:
                        self.layer_combo.setCurrentIndex(i)
                        self._log(f"Suggested pad layer: {suggested_pad}")
                        break

            # Auto-select suggested text layer
            suggested_text = result['suggested_text_layers']
            if suggested_text:
                text_name = suggested_text[0]
                for i in range(self.text_layer_combo.count()):
                    d = self.text_layer_combo.itemText(i)
                    if d.startswith(text_name + ' (') or d == text_name:
                        self.text_layer_combo.setCurrentIndex(i)
                        self._log(f"Suggested text layer: {', '.join(suggested_text)}")
                        break
            else:
                self.text_layer_combo.setCurrentIndex(0)  # Auto-detect
                self._log("No matching text layer found, using auto-detect")

            self.statusBar().showMessage("Scan complete", 3000)

        except Exception as e:
            self._log(f"Scan error: {e}", is_error=True)
            self.statusBar().showMessage("Scan failed", 3000)

    def _convert_gds(self):
        """Perform GDS to KiCad conversion."""
        gds_path = self.gds_path_edit.text().strip()
        lyp_path = self.lyp_path_edit.text().strip()
        output_dir = self.output_dir_edit.text().strip()

        # Get layer name from combo box (extract just the name part)
        layer_display = self.layer_combo.getSelectedItem()
        if not layer_display:
            self._log("Error: Please select a layer", is_error=True)
            return

        # Extract layer name from display format "LayerName (layer/datatype)"
        layer_name = layer_display.split(' (')[0] if ' (' in layer_display else layer_display

        # Get text layer (optional)
        text_layer_display = self.text_layer_combo.getSelectedItem()
        text_layer_name = None
        auto_detect_text = False
        if text_layer_display == "(Auto-detect)":
            auto_detect_text = True
        elif text_layer_display and not text_layer_display.startswith("(None"):
            text_layer_name = text_layer_display.split(' (')[0] if ' (' in text_layer_display else text_layer_display

        # Validate inputs
        if not gds_path:
            self._log("Error: Please select a GDS file", is_error=True)
            return

        if not Path(gds_path).exists():
            self._log(f"Error: GDS file not found: {gds_path}", is_error=True)
            return

        if not lyp_path:
            self._log("Error: Please select an LYP file", is_error=True)
            return

        if not Path(lyp_path).exists():
            self._log(f"Error: LYP file not found: {lyp_path}", is_error=True)
            return

        if not output_dir:
            self._log("Error: Please select an output directory", is_error=True)
            return

        if not self.lyp_parser:
            self._log("Error: LYP file not loaded", is_error=True)
            return

        # Create output directory if needed
        output_dir_path = Path(output_dir)
        output_dir_path.mkdir(parents=True, exist_ok=True)

        # Generate output path
        gds_name = Path(gds_path).stem
        output_path = output_dir_path / f"{gds_name}.kicad_mod"

        self._log(f"Converting {gds_name}.gds...")
        self._log(f"Layer: {layer_name}")
        self.statusBar().showMessage("Converting...")
        QApplication.processEvents()

        try:
            # Create converter (with optional text layer for pin names)
            converter = GDSToKiCad(self.lyp_parser, layer_name,
                                    text_layer_name=text_layer_name,
                                    auto_detect_text=auto_detect_text)
            if text_layer_name:
                self._log(f"Text layer: {text_layer_name}")
            elif auto_detect_text:
                self._log("Text layer: auto-detect")

            # Redirect stdout to capture output
            import io
            import contextlib

            f = io.StringIO()
            with contextlib.redirect_stdout(f):
                success = converter.convert(gds_path, str(output_path))

            output = f.getvalue()

            if success:
                # Extract pad count from output
                pad_count = 0
                for line in output.split('\n'):
                    if 'Found' in line and 'pads' in line:
                        try:
                            pad_count = int(line.split()[1])
                        except (IndexError, ValueError):
                            pass

                self.last_output_path = output_path

                # Add to registry
                self.last_entry_id = self.registry.add_entry(
                    gds_path, str(output_path), layer_name, pad_count, lyp_path
                )

                self._log(f"Success! Generated {pad_count} pads")
                self._log(f"Output: {output_path}")

                # Extract and display bounding box info
                self._display_bounding_box_info(output)

                # Enable library buttons
                self.copy_btn.setEnabled(True)
                self.move_btn.setEnabled(True)

                self.statusBar().showMessage(f"Conversion complete: {pad_count} pads")
                self._refresh_history()
            else:
                self._log("Conversion failed!", is_error=True)
                self.statusBar().showMessage("Conversion failed")

        except Exception as e:
            self._log(f"Error: {str(e)}", is_error=True)
            self.statusBar().showMessage("Error during conversion")

    def _copy_to_library(self):
        """Copy the last converted footprint to KiCad library."""
        if not self.last_output_path or not self.last_output_path.exists():
            self._log("Error: No footprint to copy", is_error=True)
            return

        library_dir = Path(self.library_dir_edit.text().strip())
        if not library_dir:
            self._log("Error: Please specify a library directory", is_error=True)
            return

        library_dir.mkdir(parents=True, exist_ok=True)
        dest_path = library_dir / self.last_output_path.name

        try:
            shutil.copy2(self.last_output_path, dest_path)
            self._log(f"Copied to library: {dest_path.name}")

            if self.last_entry_id:
                self.registry.update_library_status(self.last_entry_id, str(dest_path))

            self.statusBar().showMessage(f"Copied to {library_dir}")
            self._refresh_history()

        except Exception as e:
            self._log(f"Error copying: {str(e)}", is_error=True)

    def _move_to_library(self):
        """Move the last converted footprint to KiCad library."""
        if not self.last_output_path or not self.last_output_path.exists():
            self._log("Error: No footprint to move", is_error=True)
            return

        library_dir = Path(self.library_dir_edit.text().strip())
        if not library_dir:
            self._log("Error: Please specify a library directory", is_error=True)
            return

        library_dir.mkdir(parents=True, exist_ok=True)
        dest_path = library_dir / self.last_output_path.name

        try:
            shutil.move(str(self.last_output_path), str(dest_path))
            self._log(f"Moved to library: {dest_path.name}")

            if self.last_entry_id:
                self.registry.update_library_status(self.last_entry_id, str(dest_path))

            self.statusBar().showMessage(f"Moved to {library_dir}")
            self._refresh_history()

            self.copy_btn.setEnabled(False)
            self.move_btn.setEnabled(False)
            self.last_output_path = None

        except Exception as e:
            self._log(f"Error moving: {str(e)}", is_error=True)


# =============================================================================
# Main Entry Point
# =============================================================================
def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
