"""Temporary sys.path shim so ``import lore_core`` resolves in-process.

lore_core (0.32.0, 68 tests) ships inside the LORE Claude Code plugin at
``~/.claude/plugins/marketplaces/lore/lore_core/`` with no ``pyproject.toml``
of its own -- it is not installable as a normal uv/pip dependency yet. DOXA
is not allowed to modify that repo (read-only import, per the phase-1 task
brief), so rather than add a pyproject there this module inserts the
plugin's ``lore_core`` PARENT directory onto ``sys.path`` so a plain
``import lore_core`` finds the shipped package -- no vendoring, no copy.

TEMPORARY: delete this shim the day lore_core ships to PyPI (or is
vendored/pinned as a proper path/git dependency in pyproject.toml) and
depend on it like any other package.

``DOXA_LORE_CORE_PATH`` overrides the parent directory (used by the test
suite to point at a throwaway lore_core checkout, or by a machine where the
plugin lives somewhere other than the default marketplace path).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_DEFAULT_LORE_CORE_PARENT = Path.home() / ".claude" / "plugins" / "marketplaces" / "lore"


def _lore_core_parent() -> Path:
    override = os.environ.get("DOXA_LORE_CORE_PATH", "").strip()
    return Path(override) if override else _DEFAULT_LORE_CORE_PARENT


def ensure_importable() -> None:
    """Idempotent: safe to call from every module that needs lore_core."""
    parent = _lore_core_parent()
    if (parent / "lore_core").is_dir():
        p = str(parent)
        if p not in sys.path:
            sys.path.insert(0, p)


ensure_importable()
