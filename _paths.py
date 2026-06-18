# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared filesystem-path policy for the GDS->KiCad tools.

Single source of truth for where the GUIs write their generated files and the
conversion registry, so the rule (and the override env var) can never drift
between the footprint, symbol and unified entry points.
"""
import os
from pathlib import Path


def resolve_data_dir() -> Path:
    """Base directory for generated files and the conversion registry.

    Defaults to the current working directory so outputs land next to the
    files being converted. This is what makes the tools usable from the Docker
    image: the install dir (/opt/adk-tools/...) is read-only for the non-root
    user and $HOME is not mounted, so only the working tree (the mounted
    /work) persists. Set GDS_TO_KICAD_DATA_DIR to pin a fixed location.
    """
    override = os.environ.get("GDS_TO_KICAD_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path.cwd()
