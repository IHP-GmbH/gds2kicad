# SPDX-License-Identifier: GPL-3.0-or-later
"""
Shared UI Theme

Nord Dark color scheme, stylesheet, and reusable widgets shared by the
three GUI applications (unified, footprint, symbol).
"""

from typing import List

from PyQt6.QtWidgets import QComboBox, QCompleter
from PyQt6.QtCore import Qt, QStringListModel


# =============================================================================
# Color Theme (Nord Dark)
# =============================================================================
COLORS = {
    'background': '#2e3440',
    'background_alt': '#3b4252',
    'text_primary': '#eceff4',
    'text_secondary': '#d8dee9',
    'accent': '#5e81ac',
    'button': '#4c566a',
    'button_hover': '#434c5e',
    'border': '#4c566a',
    'success': '#a3be8c',
    'error': '#bf616a',
    'warning': '#ebcb8b',
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
QSlider::groove:horizontal {{
    height: 6px;
    background: {COLORS['background_alt']};
    border-radius: 3px;
}}
QSlider::handle:horizontal {{
    background: {COLORS['accent']};
    width: 14px;
    margin: -4px 0;
    border-radius: 7px;
}}
QSplitter::handle {{
    background-color: {COLORS['accent']};
    width: 3px;
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

        self._completer = QCompleter()
        self._completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.setCompleter(self._completer)

        self._model = QStringListModel()
        self._completer.setModel(self._model)
        self._items: List[str] = []

    def setItems(self, items: List[str]):
        self._items = items
        self.clear()
        self.addItems(items)
        self._model.setStringList(items)

    def getSelectedItem(self) -> str:
        return self.currentText()
