# SPDX-License-Identifier: AGPL-3.0-only
"""Native runtime declaration and explicit legacy Python interoperability seams.

Installed DOXA uses canonical Rust LORE. The Python package is development-only;
its bootstrap fixture functions remain available to tests when called explicitly.
About/version rows describe the selected native carrier and never substitute a
plugin checkout or Python package when native metadata is unavailable.
"""

from __future__ import annotations

import importlib
import sys
import tomllib
import types
from pathlib import Path


from doxa import _lore_bootstrap
from doxa import version as version_mod

REPO_ROOT = Path(__file__).resolve().parent.parent


def _manifest() -> dict:
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def _requirement_names(requirements: list[str]) -> list[str]:
    return [req.split("@")[0].split("[")[0].strip().lower() for req in requirements]


def _fake_checkout(root: Path) -> Path:
    """A directory shaped like a LORE plugin checkout: a ``lore_core``
    package inside it. Nothing imports from it -- the bootstrap only ever
    tests for the directory."""
    package = root / "lore_core"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    return root


# -- the declaration itself ----------------------------------------------


def test_python_lore_is_a_development_oracle_and_not_a_runtime_dependency():
    manifest = _manifest()
    assert "lore-core" not in _requirement_names(manifest["project"]["dependencies"])
    assert "lore-core" in _requirement_names(manifest["dependency-groups"]["dev"])


def test_the_development_oracle_is_pinned_to_an_immutable_ref():
    requirement = next(req for req in _manifest()["dependency-groups"]["dev"]
                       if req.lower().startswith("lore-core"))
    assert "git+https://github.com/docwilde/LORE" in requirement
    revision = requirement.split("git+", 1)[1].rpartition("@")[2].strip()
    assert revision and revision not in ("main", "master", "HEAD")


def test_runtime_bootstrap_does_not_implicitly_import_or_inject_python_lore(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "path", list(sys.path))
    checkout = _fake_checkout(tmp_path / "plugin")
    monkeypatch.setenv("DOXA_LORE_CORE_PATH", str(checkout))
    monkeypatch.delenv("DOXA_LORE_SOURCE", raising=False)
    monkeypatch.setitem(sys.modules, "lore_core", None)
    before = list(sys.path)
    importlib.reload(_lore_bootstrap)
    assert sys.path == before
    assert sys.modules["lore_core"] is None


# -- explicitly requested legacy compatibility bootstrap -----------------


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


def _native_info(root: Path, *, version: str = "0.61.0", source: str = "lore-rs") -> dict:
    return {"root":str(root / "store"), "projects_dir":str(root / "projects"),
            "version":version, "source":str(root / source), "disabled_stages":[]}


def test_about_names_the_native_source_it_loaded(monkeypatch, tmp_path):
    info = _native_info(tmp_path)
    monkeypatch.setattr(version_mod, "native_lore_info", lambda:info)
    rows = dict(version_mod.about_rows())
    assert rows["lore from"] == f"native  {info['source']}"
    assert rows["lore"] == f"0.61.0  {info['root']}"


def test_about_source_row_tracks_native_carrier_not_python_plugin_precedence(monkeypatch, tmp_path):
    info = _native_info(tmp_path)
    fake = types.ModuleType("lore_core")
    fake.__file__ = str(tmp_path / "plugin/lore_core/__init__.py")
    fake.__version__ = "9.9.9"
    monkeypatch.setitem(sys.modules, "lore_core", fake)
    monkeypatch.setattr(version_mod, "native_lore_info", lambda:info)
    for preference in ("plugin", "package"):
        monkeypatch.setenv("DOXA_LORE_SOURCE", preference)
        assert dict(version_mod.about_rows())["lore from"] == f"native  {info['source']}"
    info = _native_info(tmp_path, source="other-native-carrier")
    assert dict(version_mod.about_rows())["lore from"] == f"native  {info['source']}"


def test_the_version_comes_from_native_runtime_metadata(monkeypatch, tmp_path):
    info = _native_info(tmp_path, version="1.2.3")
    monkeypatch.setattr(version_mod, "native_lore_info", lambda:info)
    assert version_mod.lore_core_version() == "1.2.3"


def test_missing_native_metadata_does_not_fall_back_to_python_or_plugin_manifest(monkeypatch, tmp_path):
    fake = types.ModuleType("lore_core")
    fake.__version__ = "0.34.0"
    fake.__file__ = str(tmp_path / "lore_core/__init__.py")
    monkeypatch.setitem(sys.modules, "lore_core", fake)
    monkeypatch.setenv("DOXA_LORE_CORE_PATH", str(tmp_path))
    manifest = tmp_path / ".claude-plugin/plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"name":"lore","version":"0.34.0"}')
    monkeypatch.setattr(version_mod, "native_lore_info", lambda:None)
    assert version_mod.lore_core_version() is None
    rows = dict(version_mod.about_rows())
    assert "lore" not in rows and "lore from" not in rows
