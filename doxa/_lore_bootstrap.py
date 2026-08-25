"""Which ``lore_core`` this process gets, and where it came from.

DOXA declares ``lore-core`` as a real dependency (see ``pyproject.toml``),
so a bare clone installs it like anything else and ``import lore_core``
resolves with nothing on ``sys.path`` but the environment. That is the
floor, and until v0.37.0 there was no floor at all: without the LORE
Claude Code plugin on disk, 41 of 52 test modules failed at collection.

The plugin checkout still WINS when one is present, and that is a
deliberate choice rather than the old behaviour left in place:

* DOXA and the plugin share one mutable store -- the same
  ``~/.claude/lore`` directory, the same ``state.db``. The plugin is the
  busier writer of the two (a hook fires on every Claude Code session
  start, end and compaction), so it is the copy whose schema the file on
  disk actually has. Making the installed wheel win would mean pointing
  two different ``lore_core`` versions at one SQLite file and hoping the
  older one reads what the newer one migrated.
* A user running the plugin from a checkout they are editing has been
  seeing their edits in DOXA since the shim was written. Reproducibility
  is worth a lot, but not the surprise of a terminal that silently stops
  reflecting the memory system the rest of the machine is running.

The cost is that what DOXA loaded is no longer obvious from
``pyproject.toml`` alone, so DOXA SAYS which one it loaded: ``/about``
carries a ``lore from`` row naming the source and its location, and
:func:`resolved_source` is what fills it. That row is the whole reason
this precedence is safe to keep -- a user chasing a LORE-behaviour
difference reads it instead of guessing.

Two escape hatches, both env vars, both read per call:

``DOXA_LORE_CORE_PATH``
    The directory CONTAINING ``lore_core`` (a plugin checkout root).
    Overrides the default location -- a machine where the plugin lives
    somewhere else, or a test pointing at a throwaway checkout.

``DOXA_LORE_SOURCE``
    ``auto`` (default) prefers a plugin checkout, ``package`` refuses to
    look at one and takes the installed distribution, ``plugin`` is
    ``auto`` stated out loud. Set ``package`` to reproduce a bug against
    the pinned dependency without moving the plugin out of the way.

This is also the ONE place a ``/setup``-stickied LORE store choice
(``config.toml``'s ``lore_root`` key, see ``doxa/setup.py`` and
``doxa.config.save_lore_root``) gets exported to ``LORE_ROOT`` -- lore_core
reads that environment variable once, at ITS OWN import time, and nothing
below this module in the import graph may import it first.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_DEFAULT_LORE_CORE_PARENT = Path.home() / ".claude" / "plugins" / "marketplaces" / "lore"

#: Values of ``DOXA_LORE_SOURCE``. ``auto`` is the default.
SOURCE_AUTO = "auto"
SOURCE_PLUGIN = "plugin"
SOURCE_PACKAGE = "package"


def _lore_core_parent() -> Path:
    override = os.environ.get("DOXA_LORE_CORE_PATH", "").strip()
    return Path(override) if override else _DEFAULT_LORE_CORE_PARENT


def _preference() -> str:
    """``DOXA_LORE_SOURCE``, normalized. Anything unrecognized reads as
    ``auto`` -- a typo in an env var must not be the thing that decides a
    memory system is unavailable."""
    value = os.environ.get("DOXA_LORE_SOURCE", "").strip().lower()
    return value if value in (SOURCE_PLUGIN, SOURCE_PACKAGE) else SOURCE_AUTO


def plugin_checkout() -> "Path | None":
    """The plugin checkout DOXA would load from, or None when there isn't
    one (or ``DOXA_LORE_SOURCE=package`` says not to look)."""
    if _preference() == SOURCE_PACKAGE:
        return None
    parent = _lore_core_parent()
    return parent if (parent / "lore_core").is_dir() else None


def ensure_importable() -> None:
    """Idempotent: safe to call from every module that needs lore_core.

    Only ever PREPENDS a plugin checkout. The installed distribution needs
    no help -- it is already on ``sys.path`` -- so a machine without the
    plugin passes through here untouched, which is exactly the bare-clone
    case."""
    parent = plugin_checkout()
    if parent is None:
        return
    p = str(parent)
    if p not in sys.path:
        sys.path.insert(0, p)


def resolved_source() -> "tuple[str, str] | None":
    """``(kind, location)`` for the ``lore_core`` this process actually
    imported, or None when it could not import one at all.

    MEASURED, not assumed: read off ``lore_core.__file__`` after the
    import, so a copy that arrived some way this module did not arrange
    (a ``PYTHONPATH`` entry, an editable install, a vendored tree) is
    reported as what it is rather than as what the precedence rules would
    have predicted. ``kind`` is ``"plugin"`` when the imported package
    sits inside the checkout :func:`plugin_checkout` names, and
    ``"package"`` otherwise."""
    ensure_importable()
    try:
        import lore_core
    except Exception:  # noqa: BLE001 -- no lore_core is a row, not a crash
        return None
    origin = getattr(lore_core, "__file__", None)
    if not origin:
        return None
    parent = Path(origin).resolve().parent.parent
    checkout = plugin_checkout()
    if checkout is not None and parent == checkout.resolve():
        return (SOURCE_PLUGIN, str(parent))
    return (SOURCE_PACKAGE, str(parent))


def export_sticky_lore_root() -> None:
    """If a previous ``/setup`` stickied a store choice and nothing has
    already set ``LORE_ROOT``, export it now -- before ``lore_core`` is
    importable at all is exactly early enough, and idempotent (an env var
    already present, from a real environment or a test's conftest, always
    wins and is never touched)."""
    if os.environ.get("LORE_ROOT", "").strip():
        return
    from . import config as config_mod

    stored = config_mod.load().get("lore_root")
    if stored:
        os.environ["LORE_ROOT"] = str(stored)


ensure_importable()
export_sticky_lore_root()
