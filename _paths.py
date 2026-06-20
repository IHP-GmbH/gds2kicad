# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared filesystem-path policy for the GDS->KiCad tools.

Single source of truth for where the GUIs write their generated files and the
conversion registry, so the rule (and the override env var) can never drift
between the footprint, symbol and unified entry points.
"""
import contextlib
import os
import tempfile
from pathlib import Path


@contextlib.contextmanager
def atomic_write(path, encoding="utf-8"):
    """Write text to ``path`` atomically and as UTF-8.

    Yields a file object; the content is written to a sibling temp file and
    os.replace()d onto ``path`` only after the block completes, so an error
    mid-write (encode error, ENOSPC, interrupt) leaves the previous file
    intact instead of a truncated/corrupt artifact at the canonical output
    path. Forcing UTF-8 also removes the locale-dependent UnicodeEncodeError
    on a non-ASCII pad/net name.
    """
    target = os.fspath(path)
    target_dir = os.path.dirname(os.path.abspath(target)) or "."
    fd, tmp = tempfile.mkstemp(dir=target_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            yield handle
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    else:
        os.replace(tmp, target)


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
