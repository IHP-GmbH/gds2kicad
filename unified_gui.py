#!/usr/bin/env python3
"""
Unified GDS to KiCad Workflow GUI

Single application with 5 tabs:
  1. Extract Pins - load GDS/LYP, prepare stripped GDS, extract pin list
  2. Pin List Editor - review/edit pin names, types, sides
  3. Symbol Designer - generate .kicad_sym from pin list
  4. Footprint Generator - generate .kicad_mod from extraction data
  5. History - conversion registry
"""

import sys
import json
import uuid
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit, QFileDialog,
    QGroupBox, QMessageBox, QTabWidget, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QComboBox, QSplitter, QSlider,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QColor

from theme import COLORS, STYLESHEET, FilterableComboBox
from lyp_parser import LYPParser
from pin_extractor import PinExtractor
from pin_list import PinList, PinEntry, VALID_PIN_TYPES, VALID_PIN_SIDES
from pad_review import PadReview
from kicad_sym_writer import (
    KiCadSymWriter, SymbolDefinition, SymbolPin, PinSide, PinType,
    PIN_SPACING, PIN_LENGTH,
)
from symbol_layout import (
    create_default_layout, create_layout_from_pin_list, classify_pin, get_pin_type,
)
from preview_widgets import SymbolPreviewWidget, LayoutPreviewWidget


# Style override for QComboBox embedded in QTableWidget cells
TABLE_COMBO_STYLE = "QComboBox { min-width: 0px; padding: 2px 4px; }"


# =============================================================================
# Conversion Registry
# =============================================================================
class ConversionRegistry:
    """Manages JSON database of conversion records."""

    def __init__(self, registry_path: str):
        self.registry_path = Path(registry_path)
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.data: Dict = {"conversions": []}
        self.load()

    def load(self):
        if self.registry_path.exists():
            try:
                with open(self.registry_path, 'r') as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, IOError):
                self.data = {"conversions": []}

    def save(self):
        with open(self.registry_path, 'w') as f:
            json.dump(self.data, f, indent=2)

    def add_entry(self, entry_type: str, **kwargs) -> str:
        entry_id = str(uuid.uuid4())[:8]
        entry = {
            "id": entry_id,
            "timestamp": datetime.now().isoformat(),
            "type": entry_type,
            **kwargs,
        }
        self.data["conversions"].append(entry)
        self.save()
        return entry_id

    def get_entries(self) -> List[Dict]:
        return self.data["conversions"]

    def delete_entry(self, entry_id: str) -> bool:
        for i, entry in enumerate(self.data["conversions"]):
            if entry["id"] == entry_id:
                del self.data["conversions"][i]
                self.save()
                return True
        return False


# =============================================================================
# Main Window
# =============================================================================
class UnifiedMainWindow(QMainWindow):

    DEFAULT_OUTPUT_DIR = Path(__file__).parent / "generated_kicad_symbol_files"
    REGISTRY_PATH = DEFAULT_OUTPUT_DIR / "unified_registry.json"

    def __init__(self):
        super().__init__()
        self.setWindowTitle("GDS to KiCad -- Unified Workflow")
        self.setMinimumSize(1200, 850)

        self.registry = ConversionRegistry(str(self.REGISTRY_PATH))
        self.lyp_parser: Optional[LYPParser] = None
        self.current_pin_list: Optional[PinList] = None
        self.current_symbol: Optional[SymbolDefinition] = None
        self.stripped_gds_path: Optional[str] = None
        self.pad_dicts: List[dict] = []

        self._setup_ui()
        self.setStyleSheet(STYLESHEET)

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(15, 15, 15, 15)

        title = QLabel("GDS to KiCad -- Unified Workflow")
        title.setFont(QFont("Monospace", 14, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(title)

        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)

        self._build_extract_tab()
        self._build_pin_editor_tab()
        self._build_symbol_tab()
        self._build_footprint_tab()
        self._build_history_tab()

        # Shared log at bottom
        log_group = QGroupBox("Log")
        log_layout = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(150)
        log_layout.addWidget(self.log_text)
        main_layout.addWidget(log_group)

        self.statusBar().showMessage("Ready")
        self.statusBar().setStyleSheet(f"color: {COLORS['text_secondary']};")
        self._log("Application started. Begin by extracting pins from a GDS file.")
        self._refresh_history()

    # =========================================================================
    # Logging
    # =========================================================================
    def _log(self, message: str, is_error: bool = False):
        timestamp = datetime.now().strftime("%H:%M:%S")
        color = COLORS['error'] if is_error else COLORS['text_primary']
        self.log_text.append(
            f'<span style="color: {COLORS["text_secondary"]}">[{timestamp}]</span> '
            f'<span style="color: {color}">{message}</span>'
        )

    # =========================================================================
    # Tab 1: Extract Pins
    # =========================================================================
    def _build_extract_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # Input group
        input_group = QGroupBox("Input Configuration")
        ig_layout = QVBoxLayout(input_group)

        # GDS File
        gds_row = QHBoxLayout()
        gds_label = QLabel("GDS File:")
        gds_label.setFixedWidth(110)
        self.gds_path_edit = QLineEdit()
        self.gds_path_edit.setPlaceholderText("Select a GDSII file...")
        gds_btn = QPushButton("Browse...")
        gds_btn.clicked.connect(self._select_gds_file)
        gds_row.addWidget(gds_label)
        gds_row.addWidget(self.gds_path_edit)
        gds_row.addWidget(gds_btn)
        ig_layout.addLayout(gds_row)

        # LYP File
        lyp_row = QHBoxLayout()
        lyp_label = QLabel("LYP File:")
        lyp_label.setFixedWidth(110)
        self.lyp_path_edit = QLineEdit()
        self.lyp_path_edit.setPlaceholderText("Select a KLayout .lyp file...")
        lyp_btn = QPushButton("Browse...")
        lyp_btn.clicked.connect(self._select_lyp_file)
        lyp_row.addWidget(lyp_label)
        lyp_row.addWidget(self.lyp_path_edit)
        lyp_row.addWidget(lyp_btn)
        ig_layout.addLayout(lyp_row)

        # Pad Layer
        pad_row = QHBoxLayout()
        pad_label = QLabel("Pad Layer:")
        pad_label.setFixedWidth(110)
        self.pad_layer_combo = FilterableComboBox()
        self.pad_layer_combo.setPlaceholderText("Select LYP file first...")
        self.pad_layer_combo.setEnabled(False)
        self.scan_btn = QPushButton("Scan GDS")
        self.scan_btn.setEnabled(False)
        self.scan_btn.clicked.connect(self._scan_gds_layers)
        pad_row.addWidget(pad_label)
        pad_row.addWidget(self.pad_layer_combo)
        pad_row.addWidget(self.scan_btn)
        ig_layout.addLayout(pad_row)

        # Text Layer
        text_row = QHBoxLayout()
        text_label = QLabel("Text Layer:")
        text_label.setFixedWidth(110)
        self.text_layer_combo = FilterableComboBox()
        self.text_layer_combo.setPlaceholderText("Auto-detect (or select)")
        self.text_layer_combo.setEnabled(False)
        text_row.addWidget(text_label)
        text_row.addWidget(self.text_layer_combo)
        text_row.addStretch()
        ig_layout.addLayout(text_row)

        layout.addWidget(input_group)

        # GDS Preparation
        prep_group = QGroupBox("Prepare GDS for Extraction")
        prep_layout = QVBoxLayout(prep_group)

        prep_btn_row = QHBoxLayout()
        gen_stripped_btn = QPushButton("Generate Stripped GDS")
        gen_stripped_btn.clicked.connect(self._generate_stripped_gds)
        open_stripped_btn = QPushButton("Open Stripped GDS in KLayout")
        open_stripped_btn.clicked.connect(self._open_stripped_in_klayout)
        open_full_btn = QPushButton("Open Full GDS in KLayout")
        open_full_btn.clicked.connect(self._open_full_in_klayout)
        prep_btn_row.addWidget(gen_stripped_btn)
        prep_btn_row.addWidget(open_stripped_btn)
        prep_btn_row.addWidget(open_full_btn)
        prep_layout.addLayout(prep_btn_row)

        self.stripped_gds_status = QLabel("No stripped GDS generated yet")
        self.stripped_gds_status.setFont(QFont("Monospace", 9))
        prep_layout.addWidget(self.stripped_gds_status)

        layout.addWidget(prep_group)

        # Action
        action_row = QHBoxLayout()
        self.extract_btn = QPushButton("Extract Pin List")
        self.extract_btn.setMinimumHeight(40)
        self.extract_btn.clicked.connect(self._extract_pin_list)
        action_row.addWidget(self.extract_btn)

        layout.addLayout(action_row)

        layout.addStretch()
        self.tabs.addTab(tab, "1. Extract Pins")

    # =========================================================================
    # Tab 2: Pin List Editor
    # =========================================================================
    def _build_pin_editor_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # Toolbar
        toolbar = QHBoxLayout()
        load_btn = QPushButton("Load JSON")
        load_btn.clicked.connect(self._load_pin_list_file)
        save_btn = QPushButton("Save JSON")
        save_btn.clicked.connect(self._save_pin_list_file)
        add_btn = QPushButton("Add Row")
        add_btn.clicked.connect(self._add_pin_row)
        del_btn = QPushButton("Delete Row")
        del_btn.clicked.connect(self._delete_pin_row)

        toolbar.addWidget(load_btn)
        toolbar.addWidget(save_btn)
        toolbar.addWidget(add_btn)
        toolbar.addWidget(del_btn)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        # Pin count and validation summary
        self.pin_editor_summary = QLabel("No pin list loaded")
        self.pin_editor_summary.setFont(QFont("Monospace", 10))
        layout.addWidget(self.pin_editor_summary)

        # Table
        self.pin_editor_table = QTableWidget()
        self.pin_editor_table.setColumnCount(7)
        self.pin_editor_table.setHorizontalHeaderLabels([
            "Name", "Type", "Side", "Pad Index", "Center X", "Center Y", "Size"
        ])
        self.pin_editor_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.pin_editor_table.horizontalHeader().setStretchLastSection(True)
        self.pin_editor_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        layout.addWidget(self.pin_editor_table)

        self.tabs.addTab(tab, "2. Pin List Editor")

    # =========================================================================
    # Tab 3: Symbol Designer
    # =========================================================================
    def _build_symbol_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: pin table
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)

        tbl_label = QLabel("Pin Configuration")
        tbl_label.setFont(QFont("Monospace", 11, QFont.Weight.Bold))
        left_layout.addWidget(tbl_label)

        self.sym_pin_table = QTableWidget()
        self.sym_pin_table.setColumnCount(4)
        self.sym_pin_table.setHorizontalHeaderLabels(["Name", "Number", "Side", "Type"])
        self.sym_pin_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.sym_pin_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.sym_pin_table.horizontalHeader().setStretchLastSection(True)
        self.sym_pin_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        left_layout.addWidget(self.sym_pin_table)

        # Side buttons
        btn_row = QHBoxLayout()
        for label, side in [("Left", PinSide.LEFT), ("Right", PinSide.RIGHT),
                            ("Top", PinSide.TOP), ("Bottom", PinSide.BOTTOM)]:
            btn = QPushButton(label)
            btn.clicked.connect(lambda checked, s=side: self._move_sym_pin_side(s))
            btn_row.addWidget(btn)
        left_layout.addLayout(btn_row)

        splitter.addWidget(left)

        # Right: preview
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        prev_label = QLabel("Live Preview")
        prev_label.setFont(QFont("Monospace", 11, QFont.Weight.Bold))
        right_layout.addWidget(prev_label)

        self.preview_widget = SymbolPreviewWidget()
        right_layout.addWidget(self.preview_widget)

        zoom_row = QHBoxLayout()
        zoom_row.addWidget(QLabel("Zoom:"))
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setRange(20, 500)
        self.zoom_slider.setValue(100)
        self.zoom_slider.valueChanged.connect(
            lambda v: self.preview_widget.set_zoom(v / 100.0)
        )
        zoom_row.addWidget(self.zoom_slider)
        self.zoom_label = QLabel("100%")
        self.zoom_slider.valueChanged.connect(
            lambda v: self.zoom_label.setText(f"{v}%")
        )
        zoom_row.addWidget(self.zoom_label)
        right_layout.addLayout(zoom_row)

        # Sync wheel zoom back to slider
        self.preview_widget.zoomChanged.connect(self._on_symbol_zoom_changed)

        # Cross-reference: pad layout thumbnail
        xref_group = QGroupBox("Pad Layout (ref.)")
        xref_layout = QVBoxLayout(xref_group)
        self.sym_xref_layout_preview = LayoutPreviewWidget()
        self.sym_xref_layout_preview.setMinimumSize(150, 100)
        self.sym_xref_layout_preview.setMaximumHeight(150)
        xref_layout.addWidget(self.sym_xref_layout_preview)
        self.sym_xref_pad_count = QLabel("Pads: --")
        self.sym_xref_pad_count.setFont(QFont("Monospace", 9))
        xref_layout.addWidget(self.sym_xref_pad_count)
        self.sym_xref_mismatch = QLabel("")
        self.sym_xref_mismatch.setFont(QFont("Monospace", 9))
        self.sym_xref_mismatch.setStyleSheet(f"color: {COLORS['warning']};")
        xref_layout.addWidget(self.sym_xref_mismatch)
        right_layout.addWidget(xref_group)

        splitter.addWidget(right)
        splitter.setSizes([500, 500])
        layout.addWidget(splitter)

        # Bottom actions
        action_row = QHBoxLayout()
        gen_sym_btn = QPushButton("Generate from Pin List")
        gen_sym_btn.setMinimumHeight(40)
        gen_sym_btn.clicked.connect(self._generate_symbol_from_pin_list)
        self.export_sym_btn = QPushButton("Export .kicad_sym")
        self.export_sym_btn.setMinimumHeight(40)
        self.export_sym_btn.setEnabled(False)
        self.export_sym_btn.clicked.connect(self._export_symbol)
        action_row.addWidget(gen_sym_btn)
        action_row.addWidget(self.export_sym_btn)
        layout.addLayout(action_row)

        self.tabs.addTab(tab, "3. Symbol Designer")

    # =========================================================================
    # Tab 4: Footprint Generator
    # =========================================================================
    def _build_footprint_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: layout preview
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)

        fp_label = QLabel("Pad Layout Preview")
        fp_label.setFont(QFont("Monospace", 11, QFont.Weight.Bold))
        left_layout.addWidget(fp_label)

        self.layout_preview = LayoutPreviewWidget()
        left_layout.addWidget(self.layout_preview)

        fp_zoom_row = QHBoxLayout()
        fp_zoom_row.addWidget(QLabel("Zoom:"))
        self.fp_zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.fp_zoom_slider.setRange(10, 1000)
        self.fp_zoom_slider.setValue(100)
        self.fp_zoom_slider.valueChanged.connect(
            lambda v: self.layout_preview.set_zoom(v / 100.0)
        )
        fp_zoom_row.addWidget(self.fp_zoom_slider)
        left_layout.addLayout(fp_zoom_row)

        # Sync wheel zoom back to slider
        self.layout_preview.zoomChanged.connect(self._on_fp_zoom_changed)

        splitter.addWidget(left)

        # Right: controls
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        ctrl_label = QLabel("Footprint Controls")
        ctrl_label.setFont(QFont("Monospace", 11, QFont.Weight.Bold))
        right_layout.addWidget(ctrl_label)

        # Pad count
        self.fp_pad_count_label = QLabel("Pads: --")
        self.fp_pad_count_label.setFont(QFont("Monospace", 10))
        right_layout.addWidget(self.fp_pad_count_label)

        # Cross-reference: symbol thumbnail
        fp_xref_group = QGroupBox("Symbol (ref.)")
        fp_xref_layout = QVBoxLayout(fp_xref_group)
        self.fp_xref_symbol_preview = SymbolPreviewWidget()
        self.fp_xref_symbol_preview.setMinimumSize(150, 100)
        self.fp_xref_symbol_preview.setMaximumHeight(150)
        fp_xref_layout.addWidget(self.fp_xref_symbol_preview)
        self.fp_xref_pin_count = QLabel("Pins: --")
        self.fp_xref_pin_count.setFont(QFont("Monospace", 9))
        fp_xref_layout.addWidget(self.fp_xref_pin_count)
        self.fp_xref_mismatch = QLabel("")
        self.fp_xref_mismatch.setFont(QFont("Monospace", 9))
        self.fp_xref_mismatch.setStyleSheet(f"color: {COLORS['warning']};")
        fp_xref_layout.addWidget(self.fp_xref_mismatch)
        right_layout.addWidget(fp_xref_group)

        right_layout.addStretch()

        gen_fp_btn = QPushButton("Generate .kicad_mod")
        gen_fp_btn.setMinimumHeight(40)
        gen_fp_btn.clicked.connect(self._generate_footprint)
        right_layout.addWidget(gen_fp_btn)

        splitter.addWidget(right)
        splitter.setSizes([600, 400])
        layout.addWidget(splitter)

        self.tabs.addTab(tab, "4. Footprint Generator")

    # =========================================================================
    # Tab 5: History
    # =========================================================================
    def _build_history_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        self.history_table = QTableWidget()
        self.history_table.setColumnCount(5)
        self.history_table.setHorizontalHeaderLabels([
            "Date", "Type", "Source", "Pins", "Output"
        ])
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.horizontalHeader().setStretchLastSection(True)
        self.history_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        layout.addWidget(self.history_table)

        btn_row = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_history)
        delete_btn = QPushButton("Delete Selected")
        delete_btn.clicked.connect(self._delete_history_entry)
        btn_row.addWidget(refresh_btn)
        btn_row.addWidget(delete_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.tabs.addTab(tab, "5. History")

    # =========================================================================
    # File Selectors (Tab 1)
    # =========================================================================
    def _select_gds_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select GDSII File",
            str(Path(__file__).parent / "gds_files"),
            "GDSII Files (*.gds *.GDS);;All Files (*)"
        )
        if path:
            self.gds_path_edit.setText(path)
            self._log(f"Selected GDS: {Path(path).name}")
            self.scan_btn.setEnabled(bool(self.lyp_parser))

    def _select_lyp_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Layer Properties File",
            str(Path(__file__).parent / "pdks"),
            "LYP Files (*.lyp);;All Files (*)"
        )
        if path:
            self.lyp_path_edit.setText(path)
            self._load_layers_from_lyp(path)

    def _load_layers_from_lyp(self, lyp_path: str):
        try:
            self.lyp_parser = LYPParser(lyp_path)
            display_names = self.lyp_parser.get_layer_display_names()

            self.pad_layer_combo.setEnabled(True)
            self.pad_layer_combo.setItems(display_names)

            self.text_layer_combo.setEnabled(True)
            text_items = ["(Auto-detect)"] + display_names
            self.text_layer_combo.setItems(text_items)
            self.text_layer_combo.setCurrentIndex(0)

            self._log(f"Loaded {len(display_names)} layers from {Path(lyp_path).name}")

            gds_path = self.gds_path_edit.text().strip()
            self.scan_btn.setEnabled(bool(gds_path and Path(gds_path).exists()))

            for i, name in enumerate(display_names):
                if 'TopMetal2.drawing' in name:
                    self.pad_layer_combo.setCurrentIndex(i)
                    break

        except Exception as e:
            self._log(f"Error loading LYP: {e}", is_error=True)
            self.lyp_parser = None

    # =========================================================================
    # Scan GDS (Tab 1)
    # =========================================================================
    def _scan_gds_layers(self):
        gds_path = self.gds_path_edit.text().strip()
        if not gds_path or not Path(gds_path).exists():
            self._log("Select a valid GDS file first", is_error=True)
            return

        self._log(f"Scanning {Path(gds_path).name}...")
        QApplication.processEvents()

        try:
            result = PinExtractor.scan_gds_layers(gds_path, self.lyp_parser)

            for c in result['pad_candidates'][:5]:
                name = c['name'] or f"{c['layer_num']}/{c['datatype']}"
                total = c['boxes'] + c['polygons']
                self._log(f"  Pad: {name} ({total} shapes)")

            for c in result['text_candidates'][:5]:
                name = c['name'] or f"{c['layer_num']}/{c['datatype']}"
                self._log(f"  Text: {name} ({c['texts']} texts)")

            # Auto-select
            suggested_pad = result['suggested_pad_layer']
            if suggested_pad:
                display_names = [self.pad_layer_combo.itemText(i)
                                 for i in range(self.pad_layer_combo.count())]
                for i, d in enumerate(display_names):
                    if d.startswith(suggested_pad + ' (') or d == suggested_pad:
                        self.pad_layer_combo.setCurrentIndex(i)
                        break
                self._log(f"Suggested pad layer: {suggested_pad}")

            suggested_text = result['suggested_text_layers']
            if suggested_text:
                text_items = [self.text_layer_combo.itemText(i)
                              for i in range(self.text_layer_combo.count())]
                for i, d in enumerate(text_items):
                    if d.startswith(suggested_text[0] + ' (') or d == suggested_text[0]:
                        self.text_layer_combo.setCurrentIndex(i)
                        break

        except Exception as e:
            self._log(f"Scan error: {e}", is_error=True)

    # =========================================================================
    # Extract Pin List (Tab 1 -> Tab 2)
    # =========================================================================
    def _extract_pin_list(self):
        gds_path = self.gds_path_edit.text().strip()
        lyp_path = self.lyp_path_edit.text().strip()

        if not gds_path or not Path(gds_path).exists():
            self._log("Invalid GDS path", is_error=True)
            return
        if not self.lyp_parser:
            self._log("Load a LYP file first", is_error=True)
            return

        pad_display = self.pad_layer_combo.getSelectedItem()
        pad_layer_name = pad_display.split(' (')[0] if ' (' in pad_display else pad_display
        if not pad_layer_name:
            self._log("Select a pad layer", is_error=True)
            return

        text_display = self.text_layer_combo.getSelectedItem()
        text_layer_names = None
        if text_display and text_display != "(Auto-detect)":
            text_name = text_display.split(' (')[0] if ' (' in text_display else text_display
            text_layer_names = [text_name]

        # Use stripped GDS if it exists and is newer than the original
        source_gds = gds_path
        if (self.stripped_gds_path
                and Path(self.stripped_gds_path).exists()
                and Path(self.stripped_gds_path).stat().st_mtime
                    >= Path(gds_path).stat().st_mtime):
            source_gds = self.stripped_gds_path
            self._log(f"Using stripped GDS: {Path(source_gds).name}")
        else:
            self._log(f"Extracting pins from {Path(source_gds).name}...")
        QApplication.processEvents()

        try:
            import io, contextlib

            extractor = PinExtractor(self.lyp_parser)

            f = io.StringIO()
            with contextlib.redirect_stdout(f):
                pads, cell_name = extractor.extract_named_pads(
                    source_gds, pad_layer_name,
                    text_layer_names=text_layer_names,
                )

            for line in f.getvalue().strip().split('\n'):
                if line.strip():
                    self._log(f"  {line.strip()}")

            pin_list = PinList.from_extracted_pads(
                pads,
                chiplet_name=cell_name,
                gds_source=Path(source_gds).name,
                lyp_file=Path(lyp_path).name,
                pad_layer=pad_layer_name,
                text_layers=text_layer_names,
            )

            self.current_pin_list = pin_list
            self._load_pin_list_into_editor()
            self._build_pad_dicts_from_pin_list()
            self._log(f"Extracted {len(pin_list)} pins. Switch to Pin List Editor to review.")

            self.tabs.setCurrentIndex(1)

        except Exception as e:
            self._log(f"Extraction error: {e}", is_error=True)

    # =========================================================================
    # Pin List Editor Operations (Tab 2)
    # =========================================================================
    def _load_pin_list_into_editor(self):
        if not self.current_pin_list:
            return

        pl = self.current_pin_list
        self.pin_editor_table.blockSignals(True)
        self.pin_editor_table.setRowCount(len(pl.pins))

        for row, pin in enumerate(pl.pins):
            # Name (editable)
            name_item = QTableWidgetItem(pin.name)
            self.pin_editor_table.setItem(row, 0, name_item)

            # Type combo
            type_combo = QComboBox()
            type_combo.setStyleSheet(TABLE_COMBO_STYLE)
            type_combo.addItems(VALID_PIN_TYPES)
            type_combo.setCurrentText(pin.type)
            self.pin_editor_table.setCellWidget(row, 1, type_combo)

            # Side combo
            side_combo = QComboBox()
            side_combo.setStyleSheet(TABLE_COMBO_STYLE)
            side_combo.addItems(VALID_PIN_SIDES)
            side_combo.setCurrentText(pin.side)
            self.pin_editor_table.setCellWidget(row, 2, side_combo)

            # Pad index (read-only)
            idx_item = QTableWidgetItem(str(pin.pad_index))
            idx_item.setFlags(idx_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.pin_editor_table.setItem(row, 3, idx_item)

            # Center X (read-only)
            cx_item = QTableWidgetItem(f"{pin.center_x_dbu:.0f}")
            cx_item.setFlags(cx_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.pin_editor_table.setItem(row, 4, cx_item)

            # Center Y (read-only)
            cy_item = QTableWidgetItem(f"{pin.center_y_dbu:.0f}")
            cy_item.setFlags(cy_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.pin_editor_table.setItem(row, 5, cy_item)

            # Size (read-only)
            size_str = f"{pin.width_dbu:.0f} x {pin.height_dbu:.0f}"
            size_item = QTableWidgetItem(size_str)
            size_item.setFlags(size_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.pin_editor_table.setItem(row, 6, size_item)

        self.pin_editor_table.blockSignals(False)
        self._update_pin_editor_summary()

    def _sync_editor_to_pin_list(self):
        """Read pin list data from editor table back into self.current_pin_list."""
        if not self.current_pin_list:
            self.current_pin_list = PinList()

        pins = []
        for row in range(self.pin_editor_table.rowCount()):
            name_item = self.pin_editor_table.item(row, 0)
            name = name_item.text() if name_item else ""

            type_w = self.pin_editor_table.cellWidget(row, 1)
            pin_type = type_w.currentText() if type_w else "passive"

            side_w = self.pin_editor_table.cellWidget(row, 2)
            side = side_w.currentText() if side_w else "left"

            idx_item = self.pin_editor_table.item(row, 3)
            pad_index = int(idx_item.text()) if idx_item and idx_item.text().isdigit() else row

            cx_item = self.pin_editor_table.item(row, 4)
            cx = float(cx_item.text()) if cx_item and cx_item.text() else 0.0

            cy_item = self.pin_editor_table.item(row, 5)
            cy = float(cy_item.text()) if cy_item and cy_item.text() else 0.0

            size_item = self.pin_editor_table.item(row, 6)
            w_dbu = h_dbu = 0.0
            if size_item and 'x' in size_item.text():
                parts = size_item.text().split('x')
                try:
                    w_dbu = float(parts[0].strip())
                    h_dbu = float(parts[1].strip())
                except ValueError:
                    pass

            pins.append(PinEntry(
                name=name, type=pin_type, side=side,
                pad_index=pad_index,
                center_x_dbu=cx, center_y_dbu=cy,
                width_dbu=w_dbu, height_dbu=h_dbu,
            ))

        self.current_pin_list.pins = pins

    def _update_pin_editor_summary(self):
        if not self.current_pin_list:
            self.pin_editor_summary.setText("No pin list loaded")
            return

        self._sync_editor_to_pin_list()
        pl = self.current_pin_list
        warnings = pl.validate()

        chiplet = pl.metadata.get("chiplet_name", "")
        text = f"Chiplet: {chiplet}  |  Pins: {len(pl)}"
        if warnings:
            text += f"  |  Warnings: {len(warnings)}"
            self.pin_editor_summary.setStyleSheet(f"color: {COLORS['warning']};")
        else:
            self.pin_editor_summary.setStyleSheet(f"color: {COLORS['success']};")

        self.pin_editor_summary.setText(text)

        # Highlight duplicate names in red
        name_counts = {}
        for row in range(self.pin_editor_table.rowCount()):
            item = self.pin_editor_table.item(row, 0)
            if item:
                name = item.text()
                name_counts[name] = name_counts.get(name, 0) + 1

        for row in range(self.pin_editor_table.rowCount()):
            item = self.pin_editor_table.item(row, 0)
            if item:
                if name_counts.get(item.text(), 0) > 1:
                    item.setBackground(QColor(COLORS['error']))
                else:
                    item.setBackground(QColor(COLORS['background']))

    def _load_pin_list_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Pin List",
            str(self.DEFAULT_OUTPUT_DIR),
            "JSON Files (*.json);;All Files (*)"
        )
        if path:
            try:
                self.current_pin_list = PinList.load(path)
                self._load_pin_list_into_editor()
                self._log(f"Loaded pin list: {path} ({len(self.current_pin_list)} pins)")
            except Exception as e:
                self._log(f"Error loading pin list: {e}", is_error=True)

    def _save_pin_list_file(self):
        self._sync_editor_to_pin_list()
        if not self.current_pin_list:
            self._log("No pin list to save", is_error=True)
            return

        chiplet = self.current_pin_list.metadata.get("chiplet_name", "pins")
        default_name = f"{chiplet}_pins.json"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Pin List",
            str(self.DEFAULT_OUTPUT_DIR / default_name),
            "JSON Files (*.json)"
        )
        if path:
            self.current_pin_list.save(path)
            self._log(f"Saved pin list: {path}")

            self.registry.add_entry(
                "pin_list",
                source=self.current_pin_list.metadata.get("gds_source", ""),
                output=Path(path).name,
                pin_count=len(self.current_pin_list),
            )
            self._refresh_history()

    def _add_pin_row(self):
        row = self.pin_editor_table.rowCount()
        self.pin_editor_table.insertRow(row)

        self.pin_editor_table.setItem(row, 0, QTableWidgetItem("NEW_PIN"))

        type_combo = QComboBox()
        type_combo.setStyleSheet(TABLE_COMBO_STYLE)
        type_combo.addItems(VALID_PIN_TYPES)
        self.pin_editor_table.setCellWidget(row, 1, type_combo)

        side_combo = QComboBox()
        side_combo.setStyleSheet(TABLE_COMBO_STYLE)
        side_combo.addItems(VALID_PIN_SIDES)
        self.pin_editor_table.setCellWidget(row, 2, side_combo)

        self.pin_editor_table.setItem(row, 3, QTableWidgetItem(str(row)))
        self.pin_editor_table.setItem(row, 4, QTableWidgetItem("0"))
        self.pin_editor_table.setItem(row, 5, QTableWidgetItem("0"))
        self.pin_editor_table.setItem(row, 6, QTableWidgetItem("0 x 0"))

        self._update_pin_editor_summary()

    def _delete_pin_row(self):
        selected = self.pin_editor_table.selectedItems()
        if selected:
            self.pin_editor_table.removeRow(selected[0].row())
            self._update_pin_editor_summary()

    # =========================================================================
    # Symbol Designer Operations (Tab 3)
    # =========================================================================
    def _generate_symbol_from_pin_list(self):
        self._sync_editor_to_pin_list()
        if not self.current_pin_list or not self.current_pin_list.pins:
            self._log("No pin list available. Extract or load one first.", is_error=True)
            return

        symbol_name = self.current_pin_list.metadata.get("chiplet_name", "SYMBOL")

        try:
            symbol = create_layout_from_pin_list(self.current_pin_list, symbol_name)
            self.current_symbol = symbol
            self._load_symbol_into_designer(symbol)
            self._log(f"Generated symbol '{symbol_name}' with {len(symbol.pins)} pins")
        except Exception as e:
            self._log(f"Symbol generation error: {e}", is_error=True)

    def _load_symbol_into_designer(self, symbol: SymbolDefinition):
        self.current_symbol = symbol
        self.preview_widget.set_symbol(symbol)
        self.export_sym_btn.setEnabled(True)
        self._update_cross_references()

        self.sym_pin_table.blockSignals(True)
        self.sym_pin_table.setRowCount(len(symbol.pins))

        side_options = [s.value for s in PinSide]
        type_options = [t.value for t in PinType]

        for row, pin in enumerate(symbol.pins):
            name_item = QTableWidgetItem(pin.name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.sym_pin_table.setItem(row, 0, name_item)

            num_item = QTableWidgetItem(pin.number)
            num_item.setFlags(num_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.sym_pin_table.setItem(row, 1, num_item)

            side_combo = QComboBox()
            side_combo.setStyleSheet(TABLE_COMBO_STYLE)
            side_combo.addItems(side_options)
            side_combo.setCurrentText(pin.side.value)
            side_combo.currentTextChanged.connect(self._on_sym_table_changed)
            self.sym_pin_table.setCellWidget(row, 2, side_combo)

            type_combo = QComboBox()
            type_combo.setStyleSheet(TABLE_COMBO_STYLE)
            type_combo.addItems(type_options)
            type_combo.setCurrentText(pin.pin_type.value)
            type_combo.currentTextChanged.connect(self._on_sym_table_changed)
            self.sym_pin_table.setCellWidget(row, 3, type_combo)

        self.sym_pin_table.blockSignals(False)

    def _on_sym_table_changed(self):
        if not self.current_symbol:
            return

        import math
        side_groups = {s: [] for s in PinSide}

        for row in range(self.sym_pin_table.rowCount()):
            name = self.sym_pin_table.item(row, 0).text()
            number = self.sym_pin_table.item(row, 1).text()
            side_w = self.sym_pin_table.cellWidget(row, 2)
            type_w = self.sym_pin_table.cellWidget(row, 3)

            side = PinSide(side_w.currentText())
            pin_type = PinType(type_w.currentText())

            pin = SymbolPin(name=name, number=number, side=side,
                            pin_type=pin_type,
                            position_index=len(side_groups[side]))
            side_groups[side].append(pin)

        all_pins = []
        for side_pins in side_groups.values():
            all_pins.extend(side_pins)

        counts = {s: len(pins) for s, pins in side_groups.items()}
        max_v = max(counts[PinSide.LEFT], counts[PinSide.RIGHT], 1)
        max_h = max(counts[PinSide.TOP], counts[PinSide.BOTTOM], 1)

        body_h = math.ceil((max_v + 1) * PIN_SPACING / PIN_SPACING) * PIN_SPACING
        body_w = max(math.ceil((max_h + 1) * PIN_SPACING / PIN_SPACING) * PIN_SPACING, body_h)
        body_w = max(body_w, 5.08)
        body_h = max(body_h, 5.08)

        self.current_symbol.pins = all_pins
        self.current_symbol.body_width = body_w
        self.current_symbol.body_height = body_h
        self.preview_widget.set_symbol(self.current_symbol)
        self._update_cross_references()

    def _move_sym_pin_side(self, target_side: PinSide):
        selected = self.sym_pin_table.selectedItems()
        if not selected:
            return
        row = selected[0].row()
        side_w = self.sym_pin_table.cellWidget(row, 2)
        if side_w:
            side_w.setCurrentText(target_side.value)

    def _export_symbol(self):
        if not self.current_symbol:
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Symbol",
            str(self.DEFAULT_OUTPUT_DIR / f"{self.current_symbol.name}.kicad_sym"),
            "KiCad Symbol (*.kicad_sym)"
        )
        if path:
            writer = KiCadSymWriter()
            writer.write_symbol_library([self.current_symbol], path)
            self._log(f"Exported symbol: {path}")

            self.registry.add_entry(
                "symbol",
                source=self.current_pin_list.metadata.get("gds_source", "") if self.current_pin_list else "",
                output=Path(path).name,
                pin_count=len(self.current_symbol.pins),
            )
            self._refresh_history()

    # =========================================================================
    # Zoom Sync (wheel -> slider)
    # =========================================================================
    def _on_symbol_zoom_changed(self, zoom: float):
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(int(zoom * 100))
        self.zoom_label.setText(f"{int(zoom * 100)}%")
        self.zoom_slider.blockSignals(False)

    def _on_fp_zoom_changed(self, zoom: float):
        self.fp_zoom_slider.blockSignals(True)
        self.fp_zoom_slider.setValue(int(zoom * 100))
        self.fp_zoom_slider.blockSignals(False)

    # =========================================================================
    # Cross-reference Thumbnails
    # =========================================================================
    def _update_cross_references(self):
        """Update cross-reference thumbnails and mismatch warnings."""
        pin_count = len(self.current_symbol.pins) if self.current_symbol else 0
        pad_count = len(self.pad_dicts)

        # Symbol tab: show pad layout thumbnail
        if self.pad_dicts:
            self.sym_xref_layout_preview.set_pads(self.pad_dicts)
            self.sym_xref_pad_count.setText(f"Pads: {pad_count}")
        else:
            self.sym_xref_pad_count.setText("Pads: --")

        # Footprint tab: show symbol thumbnail
        if self.current_symbol:
            self.fp_xref_symbol_preview.set_symbol(self.current_symbol)
            self.fp_xref_pin_count.setText(f"Pins: {pin_count}")
        else:
            self.fp_xref_pin_count.setText("Pins: --")

        # Mismatch warnings
        if pin_count > 0 and pad_count > 0 and pin_count != pad_count:
            msg = f"Mismatch: {pin_count} pins vs {pad_count} pads"
            self.sym_xref_mismatch.setText(msg)
            self.fp_xref_mismatch.setText(msg)
        else:
            self.sym_xref_mismatch.setText("")
            self.fp_xref_mismatch.setText("")

    # =========================================================================
    # GDS Preparation (Tab 1)
    # =========================================================================
    def _generate_stripped_gds(self):
        """Generate a stripped GDS with only pad + text layers."""
        gds_path = self.gds_path_edit.text().strip()
        if not gds_path or not Path(gds_path).exists():
            self._log("Select a GDS file first", is_error=True)
            return
        if not self.lyp_parser:
            self._log("Load a LYP file first", is_error=True)
            return

        pad_display = self.pad_layer_combo.getSelectedItem()
        pad_layer_name = pad_display.split(' (')[0] if ' (' in pad_display else pad_display
        pad_layer = self.lyp_parser.get_layer(pad_layer_name)
        if not pad_layer:
            self._log(f"Pad layer '{pad_layer_name}' not found", is_error=True)
            return

        # Resolve text layers
        text_layer = None
        text_layers = None
        text_display = self.text_layer_combo.getSelectedItem()
        if text_display and text_display != "(Auto-detect)":
            text_name = text_display.split(' (')[0] if ' (' in text_display else text_display
            text_layer = self.lyp_parser.get_layer(text_name)
        else:
            # Auto-detect: find all candidate text layers for this pad layer
            candidates = self.lyp_parser.find_text_layers_for(pad_layer_name)
            resolved = []
            for name in candidates:
                info = self.lyp_parser.get_layer(name)
                if info:
                    resolved.append(info)
                    self._log(f"  Auto-detected text layer: {name} ({info[0]}/{info[1]})")
            if resolved:
                text_layers = resolved

        self._sync_editor_to_pin_list()

        # Only pass pin_list if it actually has pins;
        # PinList.__bool__ returns False when empty, so check is not None first
        pin_list = self.current_pin_list
        if pin_list is None or not pin_list.pins:
            pin_list = None

        stem = Path(gds_path).stem
        output_path = str(self.DEFAULT_OUTPUT_DIR / f"{stem}_stripped.gds")

        try:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            count = PadReview.generate(
                gds_path, output_path,
                pad_layer=pad_layer,
                text_layer=text_layer,
                text_layers=text_layers,
                pin_list=pin_list,
            )
            self.stripped_gds_path = output_path
            self.stripped_gds_status.setText(
                f"Stripped GDS: {Path(output_path).name} ({count} pad shapes)"
            )
            self._log(f"Generated stripped GDS: {count} shapes -> {Path(output_path).name}")
            self._log("Edit in KLayout to remove non-pad structures, then Extract.")

        except Exception as e:
            self._log(f"Error generating stripped GDS: {e}", is_error=True)

    def _open_stripped_in_klayout(self):
        """Open the stripped GDS in KLayout for editing."""
        if not self.stripped_gds_path or not Path(self.stripped_gds_path).exists():
            self._log("Generate a stripped GDS first", is_error=True)
            return

        lyp_path = self.lyp_path_edit.text().strip()
        try:
            PadReview.open_in_klayout(
                self.stripped_gds_path,
                lyp_path=lyp_path if lyp_path else None,
            )
            self._log(f"Opened KLayout with {Path(self.stripped_gds_path).name}")
        except FileNotFoundError as e:
            self._log(str(e), is_error=True)

    def _open_full_in_klayout(self):
        """Open the original GDS in KLayout for reference."""
        gds_path = self.gds_path_edit.text().strip()
        if not gds_path or not Path(gds_path).exists():
            self._log("Select a GDS file first", is_error=True)
            return

        lyp_path = self.lyp_path_edit.text().strip()
        try:
            PadReview.open_in_klayout(
                gds_path,
                lyp_path=lyp_path if lyp_path else None,
            )
            self._log(f"Opened KLayout with {Path(gds_path).name}")
        except FileNotFoundError as e:
            self._log(str(e), is_error=True)

    # =========================================================================
    # Build pad dicts from pin list (replaces pad review refresh)
    # =========================================================================
    def _build_pad_dicts_from_pin_list(self):
        """Convert pin list entries to pad dicts for layout preview and cross-refs."""
        if not self.current_pin_list or not self.current_pin_list.pins:
            self.pad_dicts = []
            self.fp_pad_count_label.setText("Pads: --")
            return

        self.pad_dicts = []
        for i, pin in enumerate(self.current_pin_list.pins):
            cx = pin.center_x_dbu
            cy = pin.center_y_dbu
            w = pin.width_dbu
            h = pin.height_dbu
            half_w = w / 2.0
            half_h = h / 2.0

            self.pad_dicts.append({
                "index": i,
                "name": pin.name,
                "center_x": cx,
                "center_y": cy,
                "width": w,
                "height": h,
                "bbox": (cx - half_w, cy - half_h, cx + half_w, cy + half_h),
                "is_polygon": False,
                "polygon_points": None,
            })

        self.layout_preview.set_pads(self.pad_dicts)
        named = sum(1 for p in self.pad_dicts if p["name"])
        self.fp_pad_count_label.setText(f"Pads: {len(self.pad_dicts)} ({named} named)")
        self._update_cross_references()

    # =========================================================================
    # Footprint Generator (Tab 4)
    # =========================================================================
    def _generate_footprint(self):
        if not self.lyp_parser:
            self._log("Load LYP file first", is_error=True)
            return

        self._sync_editor_to_pin_list()
        if not self.current_pin_list:
            self._log("No pin list available", is_error=True)
            return

        # Determine source GDS for footprint generation
        gds_path = self.gds_path_edit.text().strip()
        source_gds = None
        if (self.stripped_gds_path
                and Path(self.stripped_gds_path).exists()):
            source_gds = self.stripped_gds_path
        elif gds_path and Path(gds_path).exists():
            source_gds = gds_path

        if not source_gds:
            self._log("No GDS file available for footprint generation", is_error=True)
            return

        pad_display = self.pad_layer_combo.getSelectedItem()
        pad_layer_name = pad_display.split(' (')[0] if ' (' in pad_display else pad_display

        chiplet = self.current_pin_list.metadata.get("chiplet_name", "footprint")
        default_name = f"{chiplet}.kicad_mod"

        path, _ = QFileDialog.getSaveFileName(
            self, "Save Footprint",
            str(self.DEFAULT_OUTPUT_DIR / default_name),
            "KiCad Footprint (*.kicad_mod)"
        )
        if not path:
            return

        try:
            from gds_to_kicad import GDSToKiCad
            converter = GDSToKiCad(self.lyp_parser, pad_layer_name)
            success = converter.convert_from_pad_review(
                source_gds, self.current_pin_list, path
            )

            if success:
                self._log(f"Generated footprint: {path}")
                self.registry.add_entry(
                    "footprint",
                    source=Path(source_gds).name,
                    output=Path(path).name,
                    pin_count=len(self.current_pin_list),
                )
                self._refresh_history()

        except Exception as e:
            self._log(f"Footprint generation error: {e}", is_error=True)

    # =========================================================================
    # History Operations (Tab 5)
    # =========================================================================
    def _refresh_history(self):
        self.history_table.setRowCount(0)
        for entry in reversed(self.registry.get_entries()):
            row = self.history_table.rowCount()
            self.history_table.insertRow(row)

            try:
                dt = datetime.fromisoformat(entry["timestamp"])
                date_str = dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, KeyError):
                date_str = "Unknown"

            self.history_table.setItem(row, 0, QTableWidgetItem(date_str))
            self.history_table.setItem(row, 1, QTableWidgetItem(entry.get("type", "")))
            self.history_table.setItem(row, 2, QTableWidgetItem(entry.get("source", "")))
            self.history_table.setItem(row, 3, QTableWidgetItem(str(entry.get("pin_count", ""))))
            self.history_table.setItem(row, 4, QTableWidgetItem(entry.get("output", "")))

            self.history_table.item(row, 0).setData(
                Qt.ItemDataRole.UserRole, entry.get("id")
            )

    def _delete_history_entry(self):
        selected = self.history_table.selectedItems()
        if not selected:
            return
        row = selected[0].row()
        entry_id = self.history_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        if entry_id:
            reply = QMessageBox.question(
                self, "Confirm Delete", "Delete this entry?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.registry.delete_entry(entry_id)
                self._refresh_history()


# =============================================================================
# Main
# =============================================================================
def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    window = UnifiedMainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
