#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
GDS to KiCad Symbol Converter - GUI Application

PyQt6-based graphical interface for converting GDSII files to KiCad
schematic symbols (.kicad_sym). Includes a symbol designer with
interactive pin table and live preview.
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
    QHeaderView, QAbstractItemView, QComboBox, QSplitter,
    QSlider,
)
from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QFont, QColor, QPainter, QPen, QBrush

from lyp_parser import LYPParser
from pin_extractor import PinExtractor
from kicad_sym_writer import (
    KiCadSymWriter, SymbolDefinition, SymbolPin, PinSide, PinType,
    PIN_LENGTH,
)
from symbol_layout import create_default_layout, calculate_body_size
from _paths import resolve_data_dir
from theme import COLORS, STYLESHEET, FilterableComboBox


# =============================================================================
# Symbol Preview Widget
# =============================================================================
class SymbolPreviewWidget(QWidget):
    """Custom widget that renders a live preview of the symbol."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.symbol: Optional[SymbolDefinition] = None
        self._zoom = 1.0
        self.setMinimumSize(400, 400)

    def set_symbol(self, symbol: SymbolDefinition):
        self.symbol = symbol
        self.update()

    def set_zoom(self, zoom: float):
        self._zoom = max(0.2, min(5.0, zoom))
        self.update()

    def paintEvent(self, event):
        if not self.symbol:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Fill background
        painter.fillRect(self.rect(), QColor(COLORS['background']))

        # Set up coordinate transform: center in widget, scale, Y-up
        w = self.width()
        h = self.height()
        painter.translate(w / 2, h / 2)

        # Scale: convert mm to pixels (base ~15px per mm, adjusted by zoom)
        scale = 15.0 * self._zoom
        painter.scale(scale, -scale)  # -Y to flip to math orientation

        sym = self.symbol
        half_w = sym.body_width / 2.0
        half_h = sym.body_height / 2.0

        # Draw body rectangle
        pen = QPen(QColor(COLORS['accent']), 0.15)
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor('#3b4252')))
        painter.drawRect(QRectF(-half_w, -half_h, sym.body_width, sym.body_height))

        # Draw pins
        pin_pen = QPen(QColor(COLORS['text_primary']), 0.1)
        power_pen = QPen(QColor(COLORS['warning']), 0.1)
        text_color = QColor(COLORS['text_primary'])

        for pin in sym.pins:
            x, y = pin.get_coordinates(sym.body_width, sym.body_height)

            # Pin endpoint to body edge
            is_power = pin.pin_type == PinType.POWER_IN
            painter.setPen(power_pen if is_power else pin_pen)

            if pin.side == PinSide.LEFT:
                painter.drawLine(QRectF(x, y, PIN_LENGTH, 0).topLeft(),
                                 QRectF(x, y, PIN_LENGTH, 0).topRight())
            elif pin.side == PinSide.RIGHT:
                painter.drawLine(QRectF(x - PIN_LENGTH, y, PIN_LENGTH, 0).topLeft(),
                                 QRectF(x - PIN_LENGTH, y, PIN_LENGTH, 0).topRight())
            elif pin.side == PinSide.TOP:
                painter.drawLine(QRectF(x, y - PIN_LENGTH, 0, PIN_LENGTH).topLeft(),
                                 QRectF(x, y - PIN_LENGTH, 0, PIN_LENGTH).bottomLeft())
            elif pin.side == PinSide.BOTTOM:
                painter.drawLine(QRectF(x, y, 0, PIN_LENGTH).topLeft(),
                                 QRectF(x, y, 0, PIN_LENGTH).bottomLeft())

            # Draw pin dot at endpoint
            painter.setBrush(QBrush(QColor(COLORS['success']) if is_power
                                    else QColor(COLORS['text_primary'])))
            painter.drawEllipse(QRectF(x - 0.2, y - 0.2, 0.4, 0.4))

            # Draw pin name
            painter.save()
            painter.setPen(QPen(text_color, 0.05))
            font = painter.font()
            font.setPointSizeF(0.8)
            painter.setFont(font)

            # Flip Y back for text (text should read normally)
            painter.scale(1, -1)

            if pin.side == PinSide.LEFT:
                painter.drawText(QRectF(x + PIN_LENGTH + 0.3, -y - 0.5, 8, 1),
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                 pin.name)
            elif pin.side == PinSide.RIGHT:
                painter.drawText(QRectF(x - PIN_LENGTH - 8.3, -y - 0.5, 8, 1),
                                 Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                                 pin.name)
            elif pin.side == PinSide.TOP:
                painter.drawText(QRectF(x - 4, -y - PIN_LENGTH - 1.2, 8, 1),
                                 Qt.AlignmentFlag.AlignCenter,
                                 pin.name)
            elif pin.side == PinSide.BOTTOM:
                painter.drawText(QRectF(x - 4, -y + PIN_LENGTH + 0.2, 8, 1),
                                 Qt.AlignmentFlag.AlignCenter,
                                 pin.name)

            painter.restore()

        # Draw symbol name at center
        painter.save()
        painter.setPen(QPen(QColor(COLORS['text_secondary']), 0.05))
        font = painter.font()
        font.setPointSizeF(1.0)
        font.setBold(True)
        painter.setFont(font)
        painter.scale(1, -1)
        painter.drawText(QRectF(-half_w, -half_h, sym.body_width, sym.body_height),
                         Qt.AlignmentFlag.AlignCenter,
                         sym.name)
        painter.restore()

        painter.end()


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
        data = None
        if self.registry_path.exists():
            try:
                with open(self.registry_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError):
                data = None
        # Accept only the expected shape; a valid-JSON-but-wrong-schema file
        # would otherwise KeyError later in get_entries/add_entry.
        if isinstance(data, dict) and isinstance(data.get("conversions"), list):
            self.data = data
        else:
            self.data = {"conversions": []}

    def save(self):
        with open(self.registry_path, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, indent=2)

    def add_entry(self, gds_source: str, output_path: str, pad_layer: str,
                  pin_count: int, lyp_file: str, text_layers: str = "") -> str:
        entry_id = str(uuid.uuid4())[:8]
        entry = {
            "id": entry_id,
            "timestamp": datetime.now().isoformat(),
            "gds_source": str(gds_source),
            "output_path": str(output_path),
            "pad_layer": pad_layer,
            "text_layers": text_layers,
            "lyp_file": lyp_file,
            "pin_count": pin_count,
        }
        self.data["conversions"].append(entry)
        self.save()
        return entry_id

    def get_entries(self) -> List[Dict]:
        return self.data["conversions"]

    def delete_entry(self, entry_id: str) -> bool:
        for i, entry in enumerate(self.data["conversions"]):
            if entry.get("id") == entry_id:
                del self.data["conversions"][i]
                self.save()
                return True
        return False


# =============================================================================
# Main Window
# =============================================================================
class MainWindow(QMainWindow):

    # Writable base (CWD or $GDS_TO_KICAD_DATA_DIR), never the read-only
    # install dir. See _paths.resolve_data_dir().
    DATA_DIR = resolve_data_dir()
    DEFAULT_OUTPUT_DIR = DATA_DIR / "generated_kicad_symbol_files"
    REGISTRY_PATH = DATA_DIR / "generated_kicad_symbol_files" / "conversions_registry.json"

    def __init__(self):
        super().__init__()
        self.setWindowTitle("GDS to KiCad Symbol Converter")
        self.setMinimumSize(1100, 800)

        self.registry = ConversionRegistry(str(self.REGISTRY_PATH))
        self.lyp_parser: Optional[LYPParser] = None
        self.current_symbol: Optional[SymbolDefinition] = None

        self._setup_ui()
        self.setStyleSheet(STYLESHEET)

    def _setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(15)
        main_layout.setContentsMargins(20, 20, 20, 20)

        # Title
        title = QLabel("GDS to KiCad Symbol Converter")
        title.setFont(QFont("Monospace", 16, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(title)

        # Tabs
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)

        self._build_conversion_tab()
        self._build_designer_tab()
        self._build_history_tab()

        self.statusBar().showMessage("Ready")
        self.statusBar().setStyleSheet(f"color: {COLORS['text_secondary']};")
        self._log("Application started. Select a GDS file and LYP file to begin.")
        self._refresh_history()

    # =========================================================================
    # Conversion Tab
    # =========================================================================
    def _build_conversion_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # Input Group
        input_group = QGroupBox("Input Configuration")
        input_layout = QVBoxLayout(input_group)

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
        input_layout.addLayout(gds_row)

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
        input_layout.addLayout(lyp_row)

        # Pad Layer
        pad_row = QHBoxLayout()
        pad_label = QLabel("Pad Layer:")
        pad_label.setFixedWidth(110)
        self.pad_layer_combo = FilterableComboBox()
        self.pad_layer_combo.setPlaceholderText("Select LYP file first...")
        self.pad_layer_combo.setEnabled(False)
        self.scan_btn = QPushButton("Scan GDS")
        self.scan_btn.setToolTip("Scan GDS to auto-detect pad and text layers")
        self.scan_btn.setEnabled(False)
        self.scan_btn.clicked.connect(self._scan_gds_layers)
        pad_row.addWidget(pad_label)
        pad_row.addWidget(self.pad_layer_combo)
        pad_row.addWidget(self.scan_btn)
        input_layout.addLayout(pad_row)

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
        input_layout.addLayout(text_row)

        # Output Dir
        out_row = QHBoxLayout()
        out_label = QLabel("Output Dir:")
        out_label.setFixedWidth(110)
        self.output_dir_edit = QLineEdit()
        self.output_dir_edit.setText(str(self.DEFAULT_OUTPUT_DIR))
        out_btn = QPushButton("Browse...")
        out_btn.clicked.connect(self._select_output_dir)
        out_row.addWidget(out_label)
        out_row.addWidget(self.output_dir_edit)
        out_row.addWidget(out_btn)
        input_layout.addLayout(out_row)

        layout.addWidget(input_group)

        # Action Buttons
        action_row = QHBoxLayout()
        self.convert_btn = QPushButton("Convert to Symbol")
        self.convert_btn.setMinimumHeight(40)
        self.convert_btn.clicked.connect(self._convert_gds)
        self.open_designer_btn = QPushButton("Open in Designer")
        self.open_designer_btn.setEnabled(False)
        self.open_designer_btn.clicked.connect(self._open_in_designer)
        action_row.addWidget(self.convert_btn)
        action_row.addWidget(self.open_designer_btn)
        layout.addLayout(action_row)

        # Log
        log_group = QGroupBox("Log")
        log_layout = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(180)
        log_layout.addWidget(self.log_text)
        layout.addWidget(log_group)

        self.tabs.addTab(tab, "Conversion")

    # =========================================================================
    # Symbol Designer Tab
    # =========================================================================
    def _build_designer_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: Pin Table
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)

        table_label = QLabel("Pin Configuration")
        table_label.setFont(QFont("Monospace", 11, QFont.Weight.Bold))
        left_layout.addWidget(table_label)

        self.pin_table = QTableWidget()
        self.pin_table.setColumnCount(4)
        self.pin_table.setHorizontalHeaderLabels(["Name", "Number", "Side", "Type"])
        self.pin_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.pin_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.pin_table.horizontalHeader().setStretchLastSection(True)
        self.pin_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        left_layout.addWidget(self.pin_table)

        # Pin movement buttons
        btn_row = QHBoxLayout()
        sides = [("Left", PinSide.LEFT), ("Right", PinSide.RIGHT),
                 ("Top", PinSide.TOP), ("Bottom", PinSide.BOTTOM)]
        for label, side in sides:
            btn = QPushButton(label)
            btn.clicked.connect(lambda checked, s=side: self._move_pin_to_side(s))
            btn_row.addWidget(btn)
        left_layout.addLayout(btn_row)

        move_row = QHBoxLayout()
        up_btn = QPushButton("Move Up")
        up_btn.clicked.connect(lambda: self._move_pin_position(-1))
        down_btn = QPushButton("Move Down")
        down_btn.clicked.connect(lambda: self._move_pin_position(1))
        move_row.addWidget(up_btn)
        move_row.addWidget(down_btn)
        left_layout.addLayout(move_row)

        splitter.addWidget(left_panel)

        # Right: Preview
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)

        preview_label = QLabel("Live Preview")
        preview_label.setFont(QFont("Monospace", 11, QFont.Weight.Bold))
        right_layout.addWidget(preview_label)

        self.preview_widget = SymbolPreviewWidget()
        right_layout.addWidget(self.preview_widget)

        # Zoom control
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

        splitter.addWidget(right_panel)
        splitter.setSizes([500, 500])

        layout.addWidget(splitter)

        # Export button
        export_row = QHBoxLayout()
        self.export_btn = QPushButton("Export .kicad_sym")
        self.export_btn.setMinimumHeight(40)
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self._export_from_designer)
        export_row.addWidget(self.export_btn)
        layout.addLayout(export_row)

        self.tabs.addTab(tab, "Symbol Designer")

    # =========================================================================
    # History Tab
    # =========================================================================
    def _build_history_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        self.history_table = QTableWidget()
        self.history_table.setColumnCount(6)
        self.history_table.setHorizontalHeaderLabels([
            "Date", "GDS File", "Pad Layer", "Pins", "Text Layers", "Output"
        ])
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
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

        self.tabs.addTab(tab, "History")

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
    # File Selectors
    # =========================================================================
    def _select_gds_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select GDSII File",
            str(Path.cwd()),
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

    def _select_output_dir(self):
        path = QFileDialog.getExistingDirectory(
            self, "Select Output Directory",
            str(self.DEFAULT_OUTPUT_DIR)
        )
        if path:
            self.output_dir_edit.setText(path)

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

            # Enable scan if GDS is already selected
            gds_path = self.gds_path_edit.text().strip()
            self.scan_btn.setEnabled(bool(gds_path and Path(gds_path).exists()))

            # Try default selection
            for i, name in enumerate(display_names):
                if 'TopMetal2.drawing' in name:
                    self.pad_layer_combo.setCurrentIndex(i)
                    break

        except Exception as e:
            self._log(f"Error loading LYP: {e}", is_error=True)
            self.lyp_parser = None

    # =========================================================================
    # Layer Scanning
    # =========================================================================
    def _scan_gds_layers(self):
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
                display_names = [self.pad_layer_combo.itemText(i)
                                 for i in range(self.pad_layer_combo.count())]
                for i, d in enumerate(display_names):
                    if d.startswith(suggested_pad + ' (') or d == suggested_pad:
                        self.pad_layer_combo.setCurrentIndex(i)
                        self._log(f"Suggested pad layer: {suggested_pad}")
                        break

            # Auto-select suggested text layer
            suggested_text = result['suggested_text_layers']
            if suggested_text:
                text_name = suggested_text[0]
                text_items = [self.text_layer_combo.itemText(i)
                              for i in range(self.text_layer_combo.count())]
                for i, d in enumerate(text_items):
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

    # =========================================================================
    # Conversion
    # =========================================================================
    def _convert_gds(self):
        gds_path = self.gds_path_edit.text().strip()
        lyp_path = self.lyp_path_edit.text().strip()
        output_dir = self.output_dir_edit.text().strip()

        # Extract layer names
        pad_display = self.pad_layer_combo.getSelectedItem()
        pad_layer_name = pad_display.split(' (')[0] if ' (' in pad_display else pad_display

        text_display = self.text_layer_combo.getSelectedItem()
        text_layer_names = None
        if text_display and text_display != "(Auto-detect)":
            text_name = text_display.split(' (')[0] if ' (' in text_display else text_display
            text_layer_names = [text_name]

        # Validate
        if not gds_path or not Path(gds_path).exists():
            self._log("Error: Invalid GDS file path", is_error=True)
            return
        if not lyp_path or not Path(lyp_path).exists():
            self._log("Error: Invalid LYP file path", is_error=True)
            return
        if not pad_layer_name:
            self._log("Error: Select a pad layer", is_error=True)
            return
        if not self.lyp_parser:
            self._log("Error: LYP file not loaded", is_error=True)
            return

        output_dir_path = Path(output_dir)
        output_dir_path.mkdir(parents=True, exist_ok=True)

        gds_name = Path(gds_path).stem
        output_path = output_dir_path / f"{gds_name}.kicad_sym"

        self._log(f"Converting {gds_name}.gds...")
        self._log(f"Pad layer: {pad_layer_name}")
        self.statusBar().showMessage("Converting...")
        QApplication.processEvents()

        try:
            import io
            import contextlib

            extractor = PinExtractor(self.lyp_parser)

            f = io.StringIO()
            with contextlib.redirect_stdout(f):
                pads, cell_name = extractor.extract_named_pads(
                    gds_path, pad_layer_name,
                    text_layer_names=text_layer_names,
                )

            output = f.getvalue()
            for line in output.strip().split('\n'):
                if line.strip():
                    self._log(f"  {line.strip()}")

            symbol = create_default_layout(pads, cell_name)
            self.current_symbol = symbol

            writer = KiCadSymWriter()
            writer.write_symbol_library([symbol], str(output_path))

            self._log(f"Generated {len(pads)} pins")
            self._log(f"Output: {output_path}")

            counts = symbol.pin_count_per_side()
            self._log(f"Layout: L={counts[PinSide.LEFT]} R={counts[PinSide.RIGHT]} "
                       f"T={counts[PinSide.TOP]} B={counts[PinSide.BOTTOM]}")

            # Register
            text_layers_str = ", ".join(text_layer_names) if text_layer_names else "auto"
            self.registry.add_entry(
                gds_path, str(output_path), pad_layer_name,
                len(pads), lyp_path, text_layers_str
            )

            self.open_designer_btn.setEnabled(True)
            self.statusBar().showMessage(f"Done: {len(pads)} pins")
            self._refresh_history()

            # Update designer preview
            self._load_symbol_into_designer(symbol)

        except Exception as e:
            self._log(f"Error: {e}", is_error=True)
            self.statusBar().showMessage("Conversion failed")

    def _open_in_designer(self):
        if self.current_symbol:
            self._load_symbol_into_designer(self.current_symbol)
            self.tabs.setCurrentIndex(1)

    # =========================================================================
    # Designer Operations
    # =========================================================================
    def _load_symbol_into_designer(self, symbol: SymbolDefinition):
        self.current_symbol = symbol
        self.preview_widget.set_symbol(symbol)
        self.export_btn.setEnabled(True)

        # Populate pin table
        self.pin_table.blockSignals(True)
        self.pin_table.setRowCount(len(symbol.pins))

        side_options = [s.value for s in PinSide]
        type_options = [t.value for t in PinType]

        for row, pin in enumerate(symbol.pins):
            # Name (read-only)
            name_item = QTableWidgetItem(pin.name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.pin_table.setItem(row, 0, name_item)

            # Number (read-only)
            num_item = QTableWidgetItem(pin.number)
            num_item.setFlags(num_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.pin_table.setItem(row, 1, num_item)

            # Side combo
            side_combo = QComboBox()
            side_combo.addItems(side_options)
            side_combo.setCurrentText(pin.side.value)
            side_combo.currentTextChanged.connect(self._on_pin_table_changed)
            self.pin_table.setCellWidget(row, 2, side_combo)

            # Type combo
            type_combo = QComboBox()
            type_combo.addItems(type_options)
            type_combo.setCurrentText(pin.pin_type.value)
            type_combo.currentTextChanged.connect(self._on_pin_table_changed)
            self.pin_table.setCellWidget(row, 3, type_combo)

        self.pin_table.blockSignals(False)

    def _on_pin_table_changed(self):
        """Rebuild symbol from table and update preview"""
        if not self.current_symbol:
            return

        # Read pins from table, group by side
        side_groups = {s: [] for s in PinSide}

        for row in range(self.pin_table.rowCount()):
            name = self.pin_table.item(row, 0).text()
            number = self.pin_table.item(row, 1).text()
            side_widget = self.pin_table.cellWidget(row, 2)
            type_widget = self.pin_table.cellWidget(row, 3)

            side = PinSide(side_widget.currentText())
            pin_type = PinType(type_widget.currentText())

            pin = SymbolPin(
                name=name, number=number,
                side=side, pin_type=pin_type,
                position_index=len(side_groups[side]),
            )
            side_groups[side].append(pin)

        all_pins = []
        for side_pins in side_groups.values():
            count = len(side_pins)
            for idx, pin in enumerate(side_pins):
                pin.position_index = idx
                pin.side_pin_count = count
            all_pins.extend(side_pins)

        # Recalculate body size using the shared calculator
        body_w, body_h = calculate_body_size(side_groups)

        self.current_symbol.pins = all_pins
        self.current_symbol.body_width = body_w
        self.current_symbol.body_height = body_h

        self.preview_widget.set_symbol(self.current_symbol)

    def _move_pin_to_side(self, target_side: PinSide):
        selected = self.pin_table.selectedItems()
        if not selected:
            return
        row = selected[0].row()
        side_widget = self.pin_table.cellWidget(row, 2)
        if side_widget:
            side_widget.setCurrentText(target_side.value)

    def _move_pin_position(self, direction: int):
        """Move selected pin up or down within its side group"""
        selected = self.pin_table.selectedItems()
        if not selected:
            return

        row = selected[0].row()
        target_row = row + direction

        if target_row < 0 or target_row >= self.pin_table.rowCount():
            return

        # Swap the two rows
        self.pin_table.blockSignals(True)

        for col in range(2):
            item1 = self.pin_table.item(row, col).text()
            item2 = self.pin_table.item(target_row, col).text()
            self.pin_table.item(row, col).setText(item2)
            self.pin_table.item(target_row, col).setText(item1)

        for col in range(2, 4):
            w1 = self.pin_table.cellWidget(row, col)
            w2 = self.pin_table.cellWidget(target_row, col)
            v1 = w1.currentText()
            v2 = w2.currentText()
            w1.setCurrentText(v2)
            w2.setCurrentText(v1)

        self.pin_table.blockSignals(False)
        self.pin_table.selectRow(target_row)
        self._on_pin_table_changed()

    def _export_from_designer(self):
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
            self._log(f"Exported: {path}")
            self.statusBar().showMessage(f"Exported to {Path(path).name}")

    # =========================================================================
    # History
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

            gds_name = Path(entry.get("gds_source", "")).name
            out_name = Path(entry.get("output_path", "")).name

            self.history_table.setItem(row, 0, QTableWidgetItem(date_str))
            self.history_table.setItem(row, 1, QTableWidgetItem(gds_name))
            self.history_table.setItem(row, 2, QTableWidgetItem(entry.get("pad_layer", "")))
            self.history_table.setItem(row, 3, QTableWidgetItem(str(entry.get("pin_count", 0))))
            self.history_table.setItem(row, 4, QTableWidgetItem(entry.get("text_layers", "")))
            self.history_table.setItem(row, 5, QTableWidgetItem(out_name))

            self.history_table.item(row, 0).setData(
                Qt.ItemDataRole.UserRole, entry.get("id")
            )

    def _delete_history_entry(self):
        selected = self.history_table.selectedItems()
        if not selected:
            QMessageBox.warning(self, "No Selection", "Select an entry to delete.")
            return

        row = selected[0].row()
        entry_id = self.history_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        if not entry_id:
            return

        reply = QMessageBox.question(
            self, "Confirm Delete",
            "Delete this entry from history?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            if self.registry.delete_entry(entry_id):
                self._refresh_history()
                self._log(f"Deleted history entry: {entry_id}")


# =============================================================================
# Main
# =============================================================================
def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
