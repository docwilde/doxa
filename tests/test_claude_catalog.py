# SPDX-License-Identifier: AGPL-3.0-only
"""The subscription catalogue is a dated, account-scoped Claude CLI cache."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from doxa import claude_catalog, identity


NOW = datetime(2026, 9, 23, 21, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolated_claude_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    identity.invalidate()
    yield
    identity.invalidate()


def _cli(
    monkeypatch, *, org="current-org", authenticated=True,
    auth_method=None, version="2.1.222",
):
    commands = []

    def status(cli):
        commands.append([cli, "auth", "status", "--json"])
        return {
            "loggedIn": authenticated,
            "authMethod": auth_method or ("claude.ai" if authenticated else "none"),
            "orgId": org if authenticated else None,
            "secret": "not-a-real-secret",
        }

    def output(args):
        commands.append(args)
        if args[1:] == ["--version"]:
            return f"{version} (Claude Code)"
        raise AssertionError(args)

    monkeypatch.setattr(claude_catalog, "_auth_status", status)
    monkeypatch.setattr(claude_catalog, "_cli_output", output)
    return commands


def _profile(base, org="current-org"):
    (base / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"organizationUuid": org},
    }))
    identity.invalidate()


def _cache(base, *, org="current-org", fetched=None, stale=None, models=None, **updates):
    fetched = fetched or NOW - timedelta(hours=2)
    stale = stale or NOW - timedelta(hours=1)
    models = models or [
        {"id": "claude-sonnet-5", "name": "Sonnet 5", "min_claude_code_version": None},
        {"id": "claude-fable-5-1", "name": "Fable 5.1", "min_claude_code_version": "2.1.251"},
    ]
    data = {
        "version": 2,
        "fetchedAt": int(fetched.timestamp() * 1000),
        "staleAt": int(stale.timestamp() * 1000),
        "organizationUuid": org,
        "resolution": "token_org",
        "catalog": {
            "surface": "ccd",
            "config": {"id": "ccd", "models": models},
            "state": {"id": "ccd"},
        },
        "accessToken": "not-a-real-secret",
    }
    data.update(updates)
    path = base / "cache" / "model-catalog" / "one.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def test_reads_only_current_org_models_compatible_with_installed_cli(tmp_path, monkeypatch):
    commands = _cli(monkeypatch)
    _cache(tmp_path)

    result = claude_catalog.read_cached_catalog(config_dir=tmp_path, now=NOW)

    assert result is not None
    assert [(m.id, m.display_name) for m in result.models] == [
        ("claude-sonnet-5", "Sonnet 5")
    ]
    assert result.is_stale is True
    assert result.fetched_at == NOW - timedelta(hours=2)
    assert "not-a-real-secret" not in repr(result)
    assert commands == [
        ["claude", "auth", "status", "--json"],
        ["claude", "--version"],
    ]


def test_fresh_cache_is_still_a_cache(tmp_path, monkeypatch):
    _cli(monkeypatch, version="2.1.280")
    _cache(tmp_path, fetched=NOW - timedelta(minutes=5), stale=NOW + timedelta(minutes=55))
    result = claude_catalog.read_cached_catalog(config_dir=tmp_path, now=NOW)
    assert result is not None
    assert result.is_stale is False
    assert [m.id for m in result.models] == ["claude-sonnet-5", "claude-fable-5-1"]


def test_other_account_and_logged_out_never_get_cached_models(tmp_path, monkeypatch):
    _cache(tmp_path, org="previous-org")
    _cli(monkeypatch, org="current-org")
    assert claude_catalog.read_cached_catalog(config_dir=tmp_path, now=NOW) is None
    _cli(monkeypatch, org="previous-org", authenticated=False)
    assert claude_catalog.read_cached_catalog(config_dir=tmp_path, now=NOW) is None


def test_logged_out_cli_can_show_only_its_matching_last_profile_snapshot(
    tmp_path, monkeypatch,
):
    _cache(tmp_path)
    _profile(tmp_path)
    _cli(monkeypatch, authenticated=False)

    result = claude_catalog.read_cached_catalog(config_dir=tmp_path, now=NOW)

    assert result is not None
    assert result.offline is True
    assert result.is_stale is True
    assert [model.id for model in result.models] == ["claude-sonnet-5"]
    assert "not-a-real-secret" not in repr(result)


def test_logged_out_snapshot_rejects_wrong_account_api_key_and_unknown_auth(
    tmp_path, monkeypatch,
):
    _cache(tmp_path)
    _profile(tmp_path, org="other-org")
    _cli(monkeypatch, authenticated=False)
    assert claude_catalog.read_cached_catalog(config_dir=tmp_path, now=NOW) is None

    _profile(tmp_path)
    _cli(monkeypatch, authenticated=False, auth_method="api_key")
    assert claude_catalog.read_cached_catalog(config_dir=tmp_path, now=NOW) is None
    _cli(monkeypatch, authenticated=False, auth_method="mystery")
    assert claude_catalog.read_cached_catalog(config_dir=tmp_path, now=NOW) is None
    _cli(monkeypatch, authenticated=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    assert claude_catalog.read_cached_catalog(config_dir=tmp_path, now=NOW) is None
    _cli(monkeypatch, authenticated=True)
    assert claude_catalog.read_cached_catalog(config_dir=tmp_path, now=NOW) is None


def test_cli_explicit_signed_out_status_is_parsed_despite_exit_code_one(monkeypatch):
    def run(args, **kwargs):
        assert args == ["claude", "auth", "status", "--json"]
        assert kwargs["timeout"] == 4
        return subprocess.CompletedProcess(args, 1, json.dumps({
            "loggedIn": False, "authMethod": "none", "apiProvider": "firstParty",
        }))

    monkeypatch.setattr(claude_catalog.subprocess, "run", run)
    assert claude_catalog._auth_status("claude") == {
        "loggedIn": False, "authMethod": "none", "apiProvider": "firstParty",
    }


def test_cli_error_is_not_mistaken_for_signed_out(monkeypatch):
    monkeypatch.setattr(
        claude_catalog.subprocess, "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 2, json.dumps({"loggedIn": False, "authMethod": "none"}),
        ),
    )
    assert claude_catalog._auth_status("claude") is None


def test_too_old_or_unrecognized_cache_uses_fallback(tmp_path, monkeypatch):
    _cli(monkeypatch)
    path = _cache(tmp_path, stale=NOW - timedelta(days=8))
    assert claude_catalog.read_cached_catalog(config_dir=tmp_path, now=NOW) is None
    _cache(tmp_path, version=3)
    assert claude_catalog.read_cached_catalog(config_dir=tmp_path, now=NOW) is None
    path.write_text("{broken")
    assert claude_catalog.read_cached_catalog(config_dir=tmp_path, now=NOW) is None


def test_bad_model_rows_never_enter_picker(tmp_path, monkeypatch):
    _cli(monkeypatch)
    _cache(tmp_path, models=[
        {"id": "claude-sonnet-5", "name": "Sonnet 5"},
        {"id": "claude-sonnet-5", "name": "duplicate"},
        {"id": "claude-bad\nrow", "name": "Bad"},
        {"id": "claude-future", "name": "Future", "min_claude_code_version": "2.1.300"},
        {"id": "claude-bad-min", "name": "Bad", "min_claude_code_version": "later"},
    ])
    result = claude_catalog.read_cached_catalog(config_dir=tmp_path, now=NOW)
    assert result is not None
    assert [m.id for m in result.models] == ["claude-sonnet-5"]


def test_preserves_account_specific_billing_notice(tmp_path, monkeypatch):
    _cli(monkeypatch)
    _cache(tmp_path, models=[{
        "id": "claude-fable-5",
        "name": "Fable 5",
        "notice": {"title": "Requires usage credits", "text": "Billed separately"},
    }])
    result = claude_catalog.read_cached_catalog(config_dir=tmp_path, now=NOW)
    assert result is not None
    assert result.models[0].notice == "Requires usage credits: Billed separately"
