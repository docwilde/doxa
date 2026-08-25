# SPDX-License-Identifier: AGPL-3.0-only
"""v0.37.0: lore_core is a declared dependency, and DOXA says which one it loaded.

The defect this file exists to prevent from returning: ``lore_core`` was
imported by ``doxa.engine``, ``doxa.peers``, ``doxa.operators`` and
``doxa.transcript`` while being declared nowhere. It resolved only because
``doxa/_lore_bootstrap.py`` reached into a LORE Claude Code plugin checkout
on the machine. On a bare clone there is no such checkout, and 41 of the 52
test modules failed at COLLECTION -- the suite could not even report the
problem as a failure, because it never got as far as running.

So the first test here reads ``pyproject.toml`` and asserts the
requirement is written down. Everything after it is about the consequence:
there are now two places a ``lore_core`` can come from, the plugin
checkout still wins when there is one (it and DOXA share a mutable SQLite
store, and the plugin is the busier writer -- see
``doxa._lore_bootstrap``'s docstring), and therefore ``/about`` has to
NAME the source. A version number alone stopped being enough to identify
what is running the moment there were two carriers.
"""

from __future__ import annotations

import sys
import tomllib
import types
from pathlib import Path

import pytest

from doxa import _lore_bootstrap
from doxa import version as version_mod

REPO_ROOT = Path(__file__).resolve().parent.parent


def _requirements() -> list[str]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)
    return list(data["project"]["dependencies"])


def _fake_checkout(root: Path) -> Path:
    """A directory shaped like a LORE plugin checkout: a ``lore_core``
    package inside it. Nothing imports from it -- the bootstrap only ever
    tests for the directory."""
    package = root / "lore_core"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    return root


# -- the declaration itself ----------------------------------------------


def test_lore_core_is_a_declared_dependency():
    """The whole point. Written down in pyproject, so `uv sync` on a bare
    clone installs it and every module that imports it collects."""
    names = [req.split("@")[0].split("[")[0].strip().lower() for req in _requirements()]
    assert "lore-core" in names, (
        "lore_core is imported by doxa.engine/peers/operators/transcript but is "
        "not in pyproject's dependencies -- a bare clone will fail at collection"
    )


def test_the_lore_dependency_is_pinned_to_an_immutable_ref():
    """Neither project is on PyPI, so this is a git URL. A git URL with no
    ``@rev`` -- or one naming a BRANCH -- re-resolves to whatever that
    branch is on the day someone syncs, which is not a dependency, it is a
    subscription."""
    requirement = next(r for r in _requirements() if r.lower().startswith("lore-core"))
    assert "git+https://github.com/docwilde/LORE" in requirement
    rev = requirement.split("git+", 1)[1].rpartition("@")[2].strip()
    assert rev, "the lore-core git dependency names no revision"
    assert rev not in ("main", "master", "HEAD"), (
        f"lore-core is pinned to the moving ref {rev!r}"
    )


def test_lore_core_is_installed_as_a_distribution():
    """The bare-clone property, asserted in-process: the environment has a
    ``lore-core`` distribution, whether or not this machine has the LORE
    plugin. This is what makes `import lore_core` work with an empty
    sys.path shim."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        assert version("lore-core")
    except PackageNotFoundError:  # pragma: no cover -- a broken environment
        pytest.fail("lore-core is not installed; run `uv sync`")


# -- precedence ----------------------------------------------------------


def test_a_plugin_checkout_wins_over_the_installed_package(monkeypatch, tmp_path):
    """The deliberate choice. DOXA and the LORE plugin share one
    ``state.db``; the plugin writes to it from a hook on every Claude Code
    session, so it is the copy whose schema the file on disk has."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delenv("DOXA_LORE_SOURCE", raising=False)
    checkout = _fake_checkout(tmp_path / "plugin")
    monkeypatch.setenv("DOXA_LORE_CORE_PATH", str(checkout))

    assert _lore_bootstrap.plugin_checkout() == checkout
    _lore_bootstrap.ensure_importable()
    assert sys.path[0] == str(checkout), "the plugin checkout is not searched first"


def test_no_plugin_checkout_leaves_sys_path_alone(monkeypatch, tmp_path):
    """The bare-clone path through the same function. The installed
    distribution needs no help -- it is already importable -- so a machine
    without the plugin must come out of here untouched rather than with a
    nonexistent directory on sys.path."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("DOXA_LORE_CORE_PATH", str(tmp_path / "nowhere"))
    before = list(sys.path)
    assert _lore_bootstrap.plugin_checkout() is None
    _lore_bootstrap.ensure_importable()
    assert sys.path == before


def test_doxa_lore_source_package_refuses_a_checkout_that_is_right_there(
    monkeypatch, tmp_path,
):
    """The escape hatch, and the reason it exists: reproducing a bug
    against the pinned dependency without moving the plugin out of the
    way."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    checkout = _fake_checkout(tmp_path / "plugin")
    monkeypatch.setenv("DOXA_LORE_CORE_PATH", str(checkout))
    monkeypatch.setenv("DOXA_LORE_SOURCE", "package")

    assert _lore_bootstrap.plugin_checkout() is None
    before = list(sys.path)
    _lore_bootstrap.ensure_importable()
    assert sys.path == before


def test_an_unrecognized_source_preference_reads_as_auto(monkeypatch, tmp_path):
    """A typo in an env var must not be what decides the memory system is
    unavailable."""
    checkout = _fake_checkout(tmp_path / "plugin")
    monkeypatch.setenv("DOXA_LORE_CORE_PATH", str(checkout))
    monkeypatch.setenv("DOXA_LORE_SOURCE", "pacakge")
    assert _lore_bootstrap.plugin_checkout() == checkout


# -- saying which one -----------------------------------------------------


def test_resolved_source_measures_the_module_that_was_actually_imported(
    monkeypatch, tmp_path,
):
    """Measured off ``lore_core.__file__``, not restated from the
    precedence rule -- so a copy that arrived some way the bootstrap did
    not arrange (PYTHONPATH, an editable install) is reported as what it
    is."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    checkout = _fake_checkout(tmp_path / "plugin")
    monkeypatch.setenv("DOXA_LORE_CORE_PATH", str(checkout))
    monkeypatch.delenv("DOXA_LORE_SOURCE", raising=False)

    fake = types.ModuleType("lore_core")
    fake.__file__ = str(checkout / "lore_core" / "__init__.py")
    monkeypatch.setitem(sys.modules, "lore_core", fake)

    assert _lore_bootstrap.resolved_source() == ("plugin", str(checkout))

    # Same module, but now nothing says a plugin checkout is in play: the
    # identical file reads as the installed package, because "plugin"
    # means "inside the checkout we would have loaded from", not "in a
    # directory whose name looks plugin-ish".
    monkeypatch.setenv("DOXA_LORE_SOURCE", "package")
    kind, location = _lore_bootstrap.resolved_source()
    assert kind == "package"
    assert location == str(checkout)


def test_resolved_source_is_none_when_there_is_no_lore_core(monkeypatch):
    monkeypatch.setitem(sys.modules, "lore_core", None)
    assert _lore_bootstrap.resolved_source() is None


def test_about_names_the_source_it_loaded(monkeypatch, tmp_path):
    """A user debugging a LORE-behaviour difference must not have to guess
    which copy DOXA is running."""
    rows = dict(version_mod.about_rows())
    assert "lore from" in rows, "/about does not say where lore_core came from"
    kind, _, location = rows["lore from"].partition("  ")
    assert kind in ("plugin", "package")
    assert location.strip(), "/about names a source with no location"


def test_about_source_row_follows_the_precedence(monkeypatch, tmp_path):
    """Not a restatement of the previous test: this one MOVES the source
    and checks the row moves with it."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    checkout = _fake_checkout(tmp_path / "plugin")
    monkeypatch.setenv("DOXA_LORE_CORE_PATH", str(checkout))
    monkeypatch.delenv("DOXA_LORE_SOURCE", raising=False)
    fake = types.ModuleType("lore_core")
    fake.__file__ = str(checkout / "lore_core" / "__init__.py")
    fake.ROOT = tmp_path / "store"
    monkeypatch.setitem(sys.modules, "lore_core", fake)

    assert dict(version_mod.about_rows())["lore from"] == f"plugin  {checkout}"

    monkeypatch.setenv("DOXA_LORE_SOURCE", "package")
    assert dict(version_mod.about_rows())["lore from"] == f"package  {checkout}"


# -- the version, across both carriers ------------------------------------


def test_the_version_comes_from_the_copy_that_was_loaded(monkeypatch, tmp_path):
    """LORE 0.35.1 and later carry ``__version__``, and it already resolves
    itself correctly for whichever carrier it arrived in -- so DOXA asks
    it rather than second-guessing it."""
    fake = types.ModuleType("lore_core")
    fake.__version__ = "1.2.3"
    monkeypatch.setitem(sys.modules, "lore_core", fake)
    monkeypatch.setenv("DOXA_LORE_CORE_PATH", str(tmp_path / "nowhere"))
    assert version_mod.lore_core_version() == "1.2.3"


def test_a_pre_0_35_1_plugin_still_reports_its_manifest_version(
    monkeypatch, tmp_path,
):
    """Every LORE before 0.35.1 shipped only inside the plugin and carried
    no version attribute at all. Those installs are still on machines, and
    the manifest beside the package is the only file that declares a
    version for them."""
    fake = types.ModuleType("lore_core")  # no __version__, like 0.34.0
    fake.__file__ = str(tmp_path / "lore_core" / "__init__.py")
    monkeypatch.setitem(sys.modules, "lore_core", fake)
    monkeypatch.setenv("DOXA_LORE_CORE_PATH", str(tmp_path))

    assert version_mod.lore_core_version() is None
    manifest = tmp_path / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"name": "lore", "version": "0.34.0"}', encoding="utf-8")
    assert version_mod.lore_core_version() == "0.34.0"
