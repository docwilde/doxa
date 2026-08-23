"""Identity precision: the plan DOXA shows must be the plan the user has.

The SDK's connect-time account block reports ``subscriptionType`` -- a
coarse display string ("Claude Max") that cannot tell a Max 5x from a Max
20x. The Claude Code CLI's own local config carries the precise field
(``organizationRateLimitTier`` = "default_claude_max_20x"). These tests pin
the mapping table, the whole fallback chain, and the passthrough for a
string neither source has taught us about -- the coarse string is the weak
link, so the rule that ranks it last is the thing worth freezing.
"""

from __future__ import annotations

import json

import pytest

from doxa import identity


def test_rate_limit_tier_mapping_table():
    assert identity.parse_rate_limit_tier("default_claude_max_20x") == "max 20x"
    assert identity.parse_rate_limit_tier("default_claude_max_5x") == "max 5x"
    assert identity.parse_rate_limit_tier("default_claude_pro") == "pro"
    assert identity.parse_rate_limit_tier("default_claude_team") == "team"
    assert identity.parse_rate_limit_tier("default_claude_enterprise") == "enterprise"
    # Case and whitespace are the CLI's, not ours.
    assert identity.parse_rate_limit_tier("  DEFAULT_CLAUDE_MAX_20X ") == "max 20x"


def test_rate_limit_tier_derives_unknown_values_by_the_same_rule():
    """A tier the table hasn't met renders by the rule the table follows --
    prefix stripped, underscores spelled out -- rather than vanishing."""
    assert identity.parse_rate_limit_tier("default_claude_max_50x") == "max 50x"
    assert identity.parse_rate_limit_tier("custom_claude_ultra") == "ultra"


def test_rate_limit_tier_unparseable_is_none():
    assert identity.parse_rate_limit_tier(None) is None
    assert identity.parse_rate_limit_tier("") is None
    assert identity.parse_rate_limit_tier("   ") is None
    assert identity.parse_rate_limit_tier("default_claude_") is None


def test_tier_short_prefers_the_precise_local_field():
    """The whole point: local precision beats the SDK's coarse string."""
    assert identity.tier_short("Claude Max", "default_claude_max_20x") == "max 20x"
    assert identity.tier_short("Claude Pro", "default_claude_pro") == "pro"


def test_tier_short_falls_back_to_the_sdk_string():
    assert identity.tier_short("Claude Max", None) == "max"
    assert identity.tier_short("Claude Max", "") == "max"
    assert identity.tier_short("Claude Pro", "   ") == "pro"


def test_tier_short_passes_an_unknown_sdk_string_through():
    """An unreported plan is reported as-is, never renamed into a guess."""
    assert identity.tier_short("Team") == "team"
    assert identity.tier_short("Some Future Plan") == "some future plan"


def test_tier_short_none_when_neither_source_has_a_plan():
    assert identity.tier_short(None, None) is None
    assert identity.tier_short("", "") is None


# -- the local config read ------------------------------------------------


def _write_config(tmp_path, oauth_account: "dict | None") -> None:
    payload = {"numStartups": 3}
    if oauth_account is not None:
        payload["oauthAccount"] = oauth_account
    (tmp_path / ".claude.json").write_text(json.dumps(payload), encoding="utf-8")


def test_local_account_reads_the_cli_config(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    identity.invalidate()
    _write_config(tmp_path, {
        "emailAddress": "doc@example.org",
        "organizationName": "Doc's Org",
        "organizationRole": "admin",
        "organizationType": "claude_max",
        "organizationRateLimitTier": "default_claude_max_20x",
    })
    account = identity.local_account()
    assert account["organizationRateLimitTier"] == "default_claude_max_20x"
    assert identity.account_tier({"subscriptionType": "Claude Max"}) == "max 20x"
    assert identity.organization({}) == "Doc's Org"
    identity.invalidate()


def test_local_account_degrades_to_empty(monkeypatch, tmp_path):
    """Missing file, malformed JSON and a config with no oauthAccount are
    all the same thing to a caller: no local precision, use the SDK."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nope"))
    identity.invalidate()
    assert identity.local_account() == {}

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    identity.invalidate()
    (tmp_path / ".claude.json").write_text("{not json", encoding="utf-8")
    assert identity.local_account() == {}

    identity.invalidate()
    _write_config(tmp_path, None)
    assert identity.local_account() == {}
    # ...and with no local field the SDK string is what shows.
    assert identity.account_tier({"subscriptionType": "Claude Max"}) == "max"
    identity.invalidate()


def test_local_account_never_surfaces_credentials(monkeypatch, tmp_path):
    """The config path is read-only and the credentials file is a DIFFERENT
    file -- this pins that identity never opens it."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    identity.invalidate()
    _write_config(tmp_path, {"organizationRateLimitTier": "default_claude_pro"})
    before = (tmp_path / ".claude.json").stat().st_mtime
    identity.local_account()
    assert (tmp_path / ".claude.json").stat().st_mtime == before  # never written
    identity.invalidate()


def test_organization_prefers_the_sdk_name_then_the_local_one(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    identity.invalidate()
    _write_config(tmp_path, {"organizationName": "Local Org"})
    assert identity.organization({"organization": "SDK Org"}) == "SDK Org"
    assert identity.organization({}) == "Local Org"
    assert identity.organization({}, {}) is None
    identity.invalidate()
