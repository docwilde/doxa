"""doxa.identity -- who this session bills as, at the best precision available.

Two sources, deliberately ranked:

* ``ClaudeSDKClient.get_server_info()["account"]`` -- what the CLI reports
  at connect (measured live: ``email``, ``organization``,
  ``subscriptionType``, ``apiProvider``; there is no organization_type
  field). Authoritative about the SESSION, coarse about the PLAN:
  ``subscriptionType`` is a display string ("Claude Max") that cannot tell
  a Max 5x from a Max 20x, and a coarse string is exactly the weak link
  behind a plan once rendering as something it wasn't.
* ``~/.claude.json``'s ``oauthAccount`` -- the local config the Claude Code
  CLI itself maintains. It carries ``organizationRateLimitTier``
  ("default_claude_max_20x"), which IS the precise plan, alongside
  ``organizationType`` / ``organizationRole`` / ``organizationName``.

The precise LOCAL field wins when present and parseable; the SDK string is
the fallback; with neither, DOXA shows no plan at all rather than a guess
(the never-invent-a-field posture the identity block already follows).

Read-only throughout: this module opens the CLI's config file and never
writes it, never copies it, and never carries a credential -- ``oauthAccount``
holds profile metadata, and the only fields anything here surfaces are the
plan tier, the organization name and the role. Token material lives in
``~/.claude/.credentials.json``, which this module does not touch.

Path resolution mirrors the CLI's own (``CLAUDE_CONFIG_DIR`` overrides the
home directory, an existing ``.config.json`` in that directory wins over
``.claude.json``), so pointing DOXA at a throwaway config dir -- what the
test suite does -- points it at the same file the CLI would use there.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# Cache key: (path, mtime, size). The file is small (tens of KB) but it is
# read on every status refresh, i.e. on every turn-done and every peer
# event -- re-parsing it each time would be the JSON-pretty-print mistake
# the tool chips exist to avoid. A `claude auth login` that rewrites the
# file moves the mtime, so the cache self-invalidates; invalidate() forces
# it for the case where it doesn't (same-second rewrite of the same size).
_CACHE: "tuple[tuple[str, float, int], dict[str, Any]] | None" = None


def claude_config_path() -> Path:
    """The CLI's global config file: ``$CLAUDE_CONFIG_DIR/.config.json`` if
    that legacy name exists, else ``.claude.json`` under CLAUDE_CONFIG_DIR
    or the home directory -- the same resolution order the CLI uses."""
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home())
    legacy = base / ".config.json"
    if legacy.exists():
        return legacy
    return base / ".claude.json"


def invalidate() -> None:
    """Drop the cached read -- called after an interactive auth flow, where
    the config may have been rewritten within the same mtime granularity."""
    global _CACHE
    _CACHE = None


def local_account() -> dict[str, Any]:
    """The CLI config's ``oauthAccount`` block, or ``{}``.

    Never raises: a missing file, an unreadable one, malformed JSON and a
    config without an oauthAccount all mean the same thing to every caller
    -- no local precision available, fall back to what the SDK said."""
    global _CACHE
    path = claude_config_path()
    try:
        stat = path.stat()
    except OSError:
        _CACHE = None
        return {}
    key = (str(path), stat.st_mtime, stat.st_size)
    if _CACHE is not None and _CACHE[0] == key:
        return _CACHE[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}
    account = data.get("oauthAccount") if isinstance(data, dict) else None
    account = dict(account) if isinstance(account, dict) else {}
    _CACHE = (key, account)
    return account


# The precise-tier mapping, pinned as a table for the values that actually
# occur (measured: default_claude_max_20x). Anything outside the table is
# derived by the same rule the table follows -- strip the deployment prefix
# ("default_"/"custom_"), strip the vendor word, spell the rest with
# spaces -- so a tier Anthropic adds tomorrow renders sensibly instead of
# vanishing, and the derivation stays visible next to its examples.
RATE_LIMIT_TIERS: dict[str, str] = {
    "default_claude_max_20x": "max 20x",
    "default_claude_max_5x": "max 5x",
    "default_claude_pro": "pro",
    "default_claude_team": "team",
    "default_claude_enterprise": "enterprise",
}

_TIER_PREFIXES = ("default_", "custom_")


def parse_rate_limit_tier(raw: "str | None") -> "str | None":
    """``organizationRateLimitTier`` -> the compact plan label.

    ``"default_claude_max_20x"`` -> ``"max 20x"``; ``"default_claude_pro"``
    -> ``"pro"``. Empty/None (or a value that reduces to nothing) is
    unparseable and returns None, which is what sends the caller down the
    SDK fallback."""
    if not raw or not str(raw).strip():
        return None
    key = str(raw).strip().lower()
    known = RATE_LIMIT_TIERS.get(key)
    if known:
        return known
    for prefix in _TIER_PREFIXES:
        key = key.removeprefix(prefix)
    key = key.removeprefix("claude_")
    label = key.replace("_", " ").strip()
    return label or None


def tier_short(
    subscription_type: "str | None", rate_limit_tier: "str | None" = None
) -> "str | None":
    """Compact plan label for the status line, precise-first.

    1. ``rate_limit_tier`` (the local ``organizationRateLimitTier``) parsed
       to its compact form -- "max 20x", "pro".
    2. else the SDK's ``subscriptionType``: 'Claude Max' -> 'max',
       'Claude Pro' -> 'pro'; any other non-empty string lowercased and
       passed through as-is (an unknown plan is reported, never renamed).
    3. else None -- API-key auth or an unreported plan, where the caller
       shows the plain $ figure and no plan line."""
    precise = parse_rate_limit_tier(rate_limit_tier)
    if precise:
        return precise
    if not subscription_type or not str(subscription_type).strip():
        return None
    tier = str(subscription_type).strip().lower()
    return tier.removeprefix("claude").strip() or tier


def account_tier(
    sdk_account: "dict[str, Any] | None", local: "dict[str, Any] | None" = None
) -> "str | None":
    """:func:`tier_short` over both sources -- the one call sites use."""
    sdk_account = sdk_account or {}
    local = local_account() if local is None else local
    return tier_short(
        sdk_account.get("subscriptionType"),
        local.get("organizationRateLimitTier"),
    )


def organization(
    sdk_account: "dict[str, Any] | None", local: "dict[str, Any] | None" = None
) -> "str | None":
    """The organization NAME -- informative, never rendered as the plan.
    (A user seeing "team subscription" where a Max plan was expected is
    exactly what conflating these two produces.)"""
    sdk_account = sdk_account or {}
    local = local_account() if local is None else local
    for value in (sdk_account.get("organization"), local.get("organizationName")):
        if value and str(value).strip():
            return str(value).strip()
    return None
