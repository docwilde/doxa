# SPDX-License-Identifier: AGPL-3.0-only
"""Read Claude Code's local, account-scoped model catalogue.

Claude Code's documented selection surface is the interactive ``/model``
picker (https://code.claude.com/docs/en/model-config); it does not expose
a documented subscription model-list command.
Its own cache is useful as a *dated snapshot*, never as a live API response.
This module reads only model names and timestamps from that private cache;
it never reads, stores, or passes along a credential. Unknown cache formats,
accounts and CLI versions fail closed so the provider can use its fallback.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


_MAX_CACHE_BYTES = 2 * 1024 * 1024
_MAX_STALE_AGE = timedelta(days=7)
_VERSION_RE = re.compile(r"\b(\d+)\.(\d+)\.(\d+)\b")


@dataclass(frozen=True)
class ClaudeCatalogModel:
    id: str
    display_name: str
    notice: str | None = None


@dataclass(frozen=True)
class ClaudeCatalog:
    models: tuple[ClaudeCatalogModel, ...]
    fetched_at: datetime
    stale_at: datetime
    is_stale: bool


def _cli_output(args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=4, check=False
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _active_subscription_org(cli: str) -> str | None:
    output = _cli_output([cli, "auth", "status", "--json"])
    if output is None:
        return None
    try:
        status = json.loads(output)
    except ValueError:
        return None
    if not isinstance(status, dict):
        return None
    if status.get("loggedIn") is not True or status.get("authMethod") != "claude.ai":
        return None
    org = status.get("orgId")
    return org if isinstance(org, str) and org else None


def _cli_version(cli: str) -> tuple[int, int, int] | None:
    output = _cli_output([cli, "--version"])
    match = _VERSION_RE.search(output or "")
    return tuple(map(int, match.groups())) if match else None


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _model_rows(raw: Any, cli_version: tuple[int, int, int]) -> tuple[ClaudeCatalogModel, ...]:
    if not isinstance(raw, list):
        return ()
    rows: list[ClaudeCatalogModel] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        name = item.get("name")
        if not isinstance(model_id, str) or not isinstance(name, str):
            continue
        if (
            not model_id
            or len(model_id) > 128
            or not name.strip()
            or len(name) > 128
            or any(ord(ch) < 32 or ch.isspace() for ch in model_id)
            or any(ord(ch) < 32 for ch in name)
            or model_id in seen
        ):
            continue
        minimum = item.get("min_claude_code_version")
        if minimum is not None:
            if not isinstance(minimum, str):
                continue
            match = _VERSION_RE.fullmatch(minimum)
            if match is None or tuple(map(int, match.groups())) > cli_version:
                continue
        raw_notice = item.get("notice")
        notice = None
        if isinstance(raw_notice, dict):
            parts = (raw_notice.get("title"), raw_notice.get("text"))
            clean_parts = [
                part.strip()
                for part in parts
                if isinstance(part, str)
                and part.strip()
                and len(part) <= 300
                and not any(ord(ch) < 32 for ch in part)
            ]
            notice = ": ".join(clean_parts) or None
        rows.append(ClaudeCatalogModel(model_id, name.strip(), notice))
        seen.add(model_id)
    return tuple(rows)


def _read_one(
    path: Path, org: str, cli_version: tuple[int, int, int], now: datetime
) -> ClaudeCatalog | None:
    try:
        if path.is_symlink() or path.stat().st_size > _MAX_CACHE_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    if (
        data.get("version") != 2
        or data.get("resolution") != "token_org"
        or data.get("organizationUuid") != org
    ):
        return None
    fetched_at = _timestamp(data.get("fetchedAt"))
    stale_at = _timestamp(data.get("staleAt"))
    if (
        fetched_at is None
        or stale_at is None
        or fetched_at > now + timedelta(minutes=5)
        or stale_at <= fetched_at
        or now > stale_at + _MAX_STALE_AGE
    ):
        return None
    catalog = data.get("catalog")
    if not isinstance(catalog, dict) or catalog.get("surface") != "ccd":
        return None
    config = catalog.get("config")
    if not isinstance(config, dict) or config.get("id") != "ccd":
        return None
    models = _model_rows(config.get("models"), cli_version)
    if not models:
        return None
    return ClaudeCatalog(models, fetched_at, stale_at, now >= stale_at)


def read_cached_catalog(
    *, config_dir: Path | None = None, cli: str = "claude", now: datetime | None = None
) -> ClaudeCatalog | None:
    """Return the current subscription's dated CLI cache, if trustworthy.

    ``None`` asks the caller to use another source. A stale (but at most
    seven-day-old) cache is returned with ``is_stale=True`` so a picker can
    offer its last-seen models with an honest provenance note. This does not
    refresh the cache or contact Anthropic's model API.
    """
    org = _active_subscription_org(cli)
    version = _cli_version(cli) if org else None
    if org is None or version is None:
        return None
    base = config_dir or Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    cache_dir = base / "cache" / "model-catalog"
    try:
        paths = list(cache_dir.glob("*.json"))
    except OSError:
        return None
    instant = now or datetime.now(timezone.utc)
    candidates = (_read_one(path, org, version, instant) for path in paths)
    return max((item for item in candidates if item is not None),
               key=lambda item: item.fetched_at, default=None)
