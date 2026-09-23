# SPDX-License-Identifier: AGPL-3.0-only
"""Read Claude Code's local, account-scoped model catalogue.

Claude Code's documented selection surface is the interactive ``/model``
picker (https://code.claude.com/docs/en/model-config); it does not expose
a documented subscription model-list command.
Its own cache is useful as a *dated snapshot*, never as a live API response.
This module reads only model names and timestamps from that private cache;
it never reads, stores, or passes along a credential. An explicitly signed-out
CLI may show a dated snapshot only when its local profile names the same
organization. Unknown auth, accounts, cache formats and CLI versions fail
closed so the provider can use its fallback.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
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
    offline: bool = False


def _cli_output(args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=4, check=False
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _auth_status(cli: str) -> dict[str, Any] | None:
    """Ask Claude itself; signed-out status exits 1 but still emits JSON."""
    try:
        result = subprocess.run(
            [cli, "auth", "status", "--json"],
            capture_output=True, text=True, timeout=4, check=False,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return None
    try:
        status = json.loads(result.stdout)
    except ValueError:
        return None
    if not isinstance(status, dict):
        return None
    # A transport failure or a changed CLI contract is not a login state.
    expected_code = 0 if status.get("loggedIn") is True else 1
    if result.returncode != expected_code:
        return None
    return status


def _catalog_identity(cli: str) -> tuple[str, bool] | None:
    """(organization, offline), never an account guessed from credentials."""
    status = _auth_status(cli)
    if status is None:
        return None
    # An API key, auth token or alternate endpoint can change the model
    # universe independently of this Claude.ai subscription snapshot.
    if any(os.environ.get(name) for name in (
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    )):
        return None
    if status.get("loggedIn") is True and status.get("authMethod") == "claude.ai":
        org = status.get("orgId")
        return (org, False) if isinstance(org, str) and org else None
    if (
        status.get("loggedIn") is not False
        or status.get("authMethod") != "none"
        or status.get("orgId") not in (None, "")
    ):
        return None
    # The CLI is explicitly signed out. Its old profile is metadata, not
    # proof of current entitlement; it serves only to reject other accounts'
    # caches. The caller labels availability as unverified and sign-in needed.
    from . import identity

    org = identity.local_account().get("organizationUuid")
    return (org, True) if isinstance(org, str) and org else None


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
    path: Path, org: str, cli_version: tuple[int, int, int], now: datetime,
    offline: bool,
) -> ClaudeCatalog | None:
    try:
        if path.is_symlink() or path.stat().st_size > _MAX_CACHE_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("version") != 2:
        return None
    # Recent Claude Code stores its subscription catalogue as
    # <organization>-<account>-cc.json. The older ccd cache records the
    # organization inside the JSON instead. Never accept an unscoped cc file.
    legacy = data.get("resolution") == "token_org" and data.get("organizationUuid") == org
    current = (
        "organizationUuid" not in data
        and "resolution" not in data
        and path.name.startswith(f"{org}-")
        and path.name.endswith("-cc.json")
    )
    if not legacy and not current:
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
    surface = "ccd" if legacy else "cc"
    if not isinstance(catalog, dict) or catalog.get("surface") != surface:
        return None
    config = catalog.get("config")
    if not isinstance(config, dict) or config.get("id") != surface:
        return None
    models = _model_rows(config.get("models"), cli_version)
    if not models:
        return None
    return ClaudeCatalog(models, fetched_at, stale_at, now >= stale_at, offline)


def read_cached_catalog(
    *, config_dir: Path | None = None, cli: str = "claude", now: datetime | None = None
) -> ClaudeCatalog | None:
    """Return a dated CLI snapshot matched to the active or last local org.

    ``None`` asks the caller to use another source. A stale (but at most
    seven-day-old) cache is returned with ``is_stale=True`` so a picker can
    offer its last-seen models with an honest provenance note. ``offline``
    means Claude is explicitly signed out, so availability is unverified and
    sign-in is required before using a model. This does not refresh the cache
    or contact Anthropic's model API.
    """
    identity = _catalog_identity(cli)
    version = _cli_version(cli) if identity else None
    if identity is None or version is None:
        return None
    org, offline = identity
    base = config_dir or Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    cache_dir = base / "cache" / "model-catalog"
    try:
        paths = list(cache_dir.glob("*.json"))
    except OSError:
        return None
    instant = now or datetime.now(timezone.utc)
    candidates = (_read_one(path, org, version, instant, offline) for path in paths)
    return max((item for item in candidates if item is not None),
               key=lambda item: item.fetched_at, default=None)


async def warm_cli_catalog(*, cli: str = "claude", timeout: float = 8.0) -> bool:
    """Start Claude once without sending a prompt so it can refresh its cache.

    Stream input stays open during initialization. No model request is sent;
    the subprocess is stopped after a fixed window. This uses the operator's
    ordinary CLI profile, just as :func:`read_cached_catalog` does.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            cli, "--print", "--verbose", "--input-format", "stream-json",
            "--output-format", "stream-json", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    try:
        await asyncio.wait_for(process.wait(), timeout)
        return process.returncode == 0
    except asyncio.TimeoutError:
        # An idle stream-json session normally waits for input indefinitely.
        return True
    finally:
        if process.stdin is not None:
            process.stdin.close()
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 1.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
