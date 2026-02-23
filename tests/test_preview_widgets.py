"""Tests for preview_widgets module."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_import_preview_widgets():
    """Module loads and exports both widget classes."""
    from preview_widgets import SymbolPreviewWidget, LayoutPreviewWidget

    assert SymbolPreviewWidget is not None
    assert LayoutPreviewWidget is not None


def test_unified_gui_uses_shared_widgets():
    """unified_gui.py imports from preview_widgets, not defining its own."""
    import importlib
    src = (Path(__file__).parent.parent / "unified_gui.py").read_text()
    assert "from preview_widgets import" in src
    assert "class SymbolPreviewWidget" not in src
    assert "class LayoutPreviewWidget" not in src
