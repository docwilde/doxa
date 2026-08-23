"""/doctor and `doxa doctor` -- read-only health checks.

Every check is a pure function returning a Check (pass/fail/unknown +
fix); this file drives each branch directly through monkeypatch rather
than depending on the real machine's python version, claude CLI, or
network reachability -- the same discipline test_update.py uses for git.

Two integration points get their own tests: doxa.peers.count_stale (the
READ-ONLY twin of sweep_stale -- it must never delete what it counts),
and `doxa.cli.main(["doctor"])`'s exit code (0 all-pass, 1 anything
failing) -- what scripts/install.sh's `doxa doctor || true` actually runs.
"""

from __future__ import annotations

import json
import subprocess
import tomllib

import pytest

from doxa import cli as cli_mod
from doxa import config as config_mod
from doxa import doctor as doctor_mod
from doxa import peers as peers_mod


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "runtime"))
    config_mod.invalidate()
    yield
    config_mod.invalidate()


# -- python version ----------------------------------------------------


def test_python_check_passes_when_current_meets_the_minimum(monkeypatch):
    monkeypatch.setattr(doctor_mod, "_min_python", lambda: "3.11")
    check = doctor_mod._python_check()
    assert check.status == doctor_mod.STATUS_PASS
    assert check.fix == ""


def test_python_check_fails_and_names_the_fix_when_too_old(monkeypatch):
    monkeypatch.setattr(doctor_mod, "_min_python", lambda: "99.0")
    check = doctor_mod._python_check()
    assert check.status == doctor_mod.STATUS_FAIL
    assert "uv python install 99.0" in check.fix


def test_min_python_reads_the_real_checkouts_pyproject():
    # This IS the checkout -- pyproject.toml really says >=3.11.
    assert doctor_mod._min_python() == "3.11"


# -- DOXA version --------------------------------------------------------


def test_doxa_version_check_always_passes():
    check = doctor_mod._doxa_version_check()
    assert check.status == doctor_mod.STATUS_PASS
    assert "DOXA" in check.detail


# -- claude CLI ------------------------------------------------------------


def test_claude_cli_check_fails_when_not_installed(monkeypatch):
    from doxa import auth as auth_mod

    monkeypatch.setattr(auth_mod.AuthProvider, "installed", lambda self: False)
    check = doctor_mod._claude_cli_check()
    assert check.status == doctor_mod.STATUS_FAIL
    assert "not found on PATH" in check.detail
    assert "docs.claude.com" in check.fix


def test_claude_cli_check_fails_when_installed_but_not_authenticated(monkeypatch):
    from doxa import auth as auth_mod

    monkeypatch.setattr(auth_mod.AuthProvider, "installed", lambda self: True)
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stdout="", stderr=""),
    )
    check = doctor_mod._claude_cli_check()
    assert check.status == doctor_mod.STATUS_FAIL
    assert check.fix == "claude auth login"


def test_claude_cli_check_passes_when_authenticated(monkeypatch):
    from doxa import auth as auth_mod

    monkeypatch.setattr(auth_mod.AuthProvider, "installed", lambda self: True)
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **k: subprocess.CompletedProcess(
            cmd, 0, stdout="2.1.228 (Claude Code)" if "--version" in cmd else "",
        ),
    )
    check = doctor_mod._claude_cli_check()
    assert check.status == doctor_mod.STATUS_PASS
    assert "2.1.228" in check.detail
    assert check.fix == ""


# -- engine CLI isolation (item AA) --------------------------------------


def test_cli_isolation_check_fails_when_not_provisioned():
    check = doctor_mod._cli_isolation_check()
    assert check.status == doctor_mod.STATUS_FAIL
    assert "not provisioned" in check.detail
    assert "/setup" in check.fix


def test_cli_isolation_check_fails_when_settings_carry_plugins_or_hooks():
    from doxa import cli_isolation as iso_mod

    path = iso_mod.ensure_cli_config_dir()
    (path / iso_mod.SETTINGS_NAME).write_text(
        json.dumps({"enabledPlugins": {"lore": True}})
    )
    check = doctor_mod._cli_isolation_check()
    assert check.status == doctor_mod.STATUS_FAIL
    assert "enabledPlugins" in check.detail


def test_cli_isolation_check_reports_auth_and_skills(monkeypatch):
    from doxa import cli_isolation as iso_mod

    path = iso_mod.ensure_cli_config_dir()
    skills = iso_mod.isolated_skills_path()
    (skills / "a-skill").mkdir(parents=True)
    (skills / "a-skill" / "SKILL.md").write_text("body")
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **k: subprocess.CompletedProcess(
            cmd, 0, stdout='{"loggedIn": true}',
        ),
    )
    check = doctor_mod._cli_isolation_check()
    assert check.status == doctor_mod.STATUS_PASS
    assert "1 learned skill" in check.detail
    assert "authenticates" in check.detail
    assert check.fix == ""


def test_cli_isolation_check_fails_when_spawned_session_not_authenticated(monkeypatch):
    from doxa import cli_isolation as iso_mod

    iso_mod.ensure_cli_config_dir()
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout='{"loggedIn": false}'),
    )
    check = doctor_mod._cli_isolation_check()
    assert check.status == doctor_mod.STATUS_FAIL
    assert "NOT authenticated" in check.detail
    assert check.fix


# -- LORE store ----------------------------------------------------------


def test_lore_store_check_reports_the_real_belief_count():
    check = doctor_mod._lore_store_check()
    assert check.status == doctor_mod.STATUS_PASS
    assert "active belief" in check.detail


def test_lore_store_check_fails_when_the_store_cannot_open(monkeypatch):
    import lore_core

    def _boom():
        raise RuntimeError("db is locked")

    monkeypatch.setattr(lore_core.store, "db_connect", _boom)
    check = doctor_mod._lore_store_check()
    assert check.status == doctor_mod.STATUS_FAIL
    assert "db is locked" in check.detail
    assert check.fix == "run /setup to choose or create a LORE store"


# -- config file -----------------------------------------------------------


def test_config_check_passes_when_absent():
    check = doctor_mod._config_check()
    assert check.status == doctor_mod.STATUS_PASS
    assert "absent" in check.detail


def test_config_check_passes_when_valid():
    config_mod.save({"model": "claude-opus-4-5"})
    check = doctor_mod._config_check()
    assert check.status == doctor_mod.STATUS_PASS
    assert "parses cleanly" in check.detail


def test_config_check_fails_and_names_the_fix_when_malformed():
    path = config_mod.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not [ valid toml\n", encoding="utf-8")
    check = doctor_mod._config_check()
    assert check.status == doctor_mod.STATUS_FAIL
    assert str(path) in check.fix
    assert "reset to defaults" in check.fix


# -- daemon/registry health -------------------------------------------------


def test_registry_check_reports_zero_when_empty():
    check = doctor_mod._registry_check()
    assert check.status == doctor_mod.STATUS_PASS
    assert "0 live" in check.detail


def test_count_stale_never_deletes_what_it_counts(tmp_path):
    """The read-only twin of sweep_stale: same classification, zero
    unlinks -- proved by writing a provably-dead entry and checking the
    file is STILL there afterwards."""
    peers_mod.registry_dir()
    entry_path = peers_mod.registry_dir() / "dead.json"
    entry_path.write_text(
        json.dumps({
            "pid": 999999999,  # not a real pid
            "socket_path": str(tmp_path / "nonexistent.sock"),
            "heartbeat_at": "2000-01-01T00:00:00.000000Z",
        }),
        encoding="utf-8",
    )
    assert peers_mod.count_stale() == 1
    assert entry_path.exists()  # NOT swept -- count_stale is read-only
    assert peers_mod.count_stale() == 1  # idempotent, still there


def test_registry_check_surfaces_stale_count_without_sweeping(tmp_path):
    peers_mod.registry_dir()
    entry_path = peers_mod.registry_dir() / "dead.json"
    entry_path.write_text(
        json.dumps({
            "pid": 999999999,
            "socket_path": str(tmp_path / "nonexistent.sock"),
            "heartbeat_at": "2000-01-01T00:00:00.000000Z",
        }),
        encoding="utf-8",
    )
    check = doctor_mod._registry_check()
    assert check.status == doctor_mod.STATUS_PASS  # report only, never a fail
    assert "1 stale presence file" in check.detail
    assert "report only" in check.detail
    assert entry_path.exists()


# -- terminal capabilities --------------------------------------------------


def test_image_protocol_check_reports_the_detected_mode(monkeypatch):
    from doxa import images as images_mod

    monkeypatch.setattr(images_mod, "detect_mode", lambda: "halfblock")
    check = doctor_mod._image_protocol_check()
    assert check.status == doctor_mod.STATUS_PASS
    assert "halfblock" in check.detail


def test_keyboard_enhancement_check_is_honestly_unknown():
    check = doctor_mod._keyboard_enhancement_check()
    assert check.status == doctor_mod.STATUS_UNKNOWN
    assert "isn't measured yet" in check.detail


def test_mcp_check_reports_nothing_configured():
    check = doctor_mod._mcp_check()
    assert check.status == doctor_mod.STATUS_PASS
    assert "no external MCP servers" in check.detail


# -- run_checks / report / any_failing --------------------------------------


def test_run_checks_returns_one_per_check():
    checks = doctor_mod.run_checks()
    assert [c.id for c in checks] == [
        "python", "doxa-version", "claude-cli", "cli-isolation", "lore-store",
        "config", "registry", "image-protocol", "keyboard-enhancement", "mcp",
    ]


def test_report_renders_glyphs_and_fix_lines():
    checks = [
        doctor_mod.Check("a", "A", doctor_mod.STATUS_PASS, "fine"),
        doctor_mod.Check("b", "B", doctor_mod.STATUS_FAIL, "broken", fix="do X"),
        doctor_mod.Check("c", "C", doctor_mod.STATUS_UNKNOWN, "dunno"),
    ]
    text = doctor_mod.report(checks)
    assert "✓ A -- fine" in text
    assert "✗ B -- broken" in text
    assert "    fix: do X" in text
    assert "? C -- dunno" in text
    assert "1 check(s) failing" in text


def test_report_says_all_pass_when_nothing_failed():
    checks = [doctor_mod.Check("a", "A", doctor_mod.STATUS_PASS, "fine")]
    assert "all checks pass" in doctor_mod.report(checks)


def test_any_failing():
    passing = [doctor_mod.Check("a", "A", doctor_mod.STATUS_PASS, "fine")]
    failing = [doctor_mod.Check("a", "A", doctor_mod.STATUS_FAIL, "broken", fix="x")]
    assert doctor_mod.any_failing(passing) is False
    assert doctor_mod.any_failing(failing) is True


# -- `doxa doctor` (the CLI subcommand) --------------------------------


def test_cli_doctor_exits_zero_when_everything_passes(monkeypatch, capsys):
    monkeypatch.setattr(
        doctor_mod, "run_checks",
        lambda: [doctor_mod.Check("a", "A", doctor_mod.STATUS_PASS, "fine")],
    )
    code = cli_mod.main(["doctor"])
    assert code == 0
    out = capsys.readouterr().out
    assert "all checks pass" in out


def test_cli_doctor_exits_one_when_something_fails(monkeypatch, capsys):
    monkeypatch.setattr(
        doctor_mod, "run_checks",
        lambda: [doctor_mod.Check("a", "A", doctor_mod.STATUS_FAIL, "broken", fix="do X")],
    )
    code = cli_mod.main(["doctor"])
    assert code == 1
    out = capsys.readouterr().out
    assert "do X" in out


def test_cli_doctor_never_touches_peers_or_daemons(monkeypatch, capsys):
    """doxa doctor must not spawn anything -- it is the read-only path."""
    def _boom(*a, **k):
        raise AssertionError("doxa doctor must never spawn a daemon")

    monkeypatch.setattr("doxa.daemon.spawn_daemon", _boom)
    code = cli_mod.main(["doctor"])
    assert code in (0, 1)
