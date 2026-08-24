"""doxa.tabsets -- item D: persist and restore a repo's tab set.

**This item's original spec text did not survive to this session.** What
follows is RE-DERIVED from the item's name ("tab restore") plus the
surrounding codebase -- most tellingly, ``doxa/naming.py`` already
forward-referenced it verbatim: "a restart (item D's window restore)
reuses the [cached tab] name rather than spending a second call on the
same session." See CHANGELOG.md's 0.23.0 entry for the judgment calls this
re-derivation had to make.

The gap it closes: sessions are daemons that outlive the TUI (doxa/
daemon.py), but ``doxa`` only ever reattaches to the SINGLE most recent
live session in a repo's scope (doxa.cli, the ``args.command is None``
branch) -- a multi-tab working set is lost on every restart even though
every daemon behind it is still alive and attachable. This module is the
persistence half; doxa.app wires the record's changes in, and doxa.cli
resolves it against the live peer registry on launch.

**Record**: one small JSON file per scope under ``$DOXA_HOME/tabsets/``
(default ``~/.doxa/tabsets/``), named by a truncated sha256 of the scope
key -- the scope key is a filesystem path (``peers.main_repo_root_of``,
the SAME key ``doxa.cli``'s spawn-or-attach and ``doxa.peers``' discovery
already group daemons by), which is not itself a safe filename on every
platform. ``{"scope_key", "active_session_id", "tabs": [{"session_id",
"pinned_name"}, ...]}`` -- order in ``tabs`` IS the saved tab-bar order.
Writes are atomic (tmp file + ``os.replace``), 0600, same discipline as
``doxa.config``'s settings file and ``doxa.peers``' registry entries.
Never raises: a missing, unreadable or malformed record reads as "nothing
to restore" to every caller, exactly like a broken config.toml costs the
user their settings, never their session (doxa.config.load's own rule).

**Restore is a cross-check, not a replay**: :func:`resolve` reads the
saved record, then filters it against the LIVE daemon registry
(``doxa.peers.list_daemons``) for the same scope. A saved session id with
no live daemon behind it (finalized since, killed, machine rebooted) is
dropped SILENTLY and counted -- it must never spawn a replacement session
(that would not be the session the user left) and must never block
startup. The caller (doxa.cli) reports the (restored, skipped) counts so
a startup that quietly differs from what the user left is never silent
about it.

**Stopped vs. detached** (v0.17's ``detached_on_purpose`` / stop-path
distinction, carried into the record): a session the user explicitly
STOPPED (``doxa stop``, Ctrl+Q, the palette's "Quit: stop session") is
gone for good and must leave the set; a session merely DETACHED (Ctrl+W,
Ctrl+C once, "Quit: detach") keeps running and STAYS in the set even
though its tab closed -- doxa.app is the one place that knows which is
which at the moment it happens, so this module stays a plain record store
and never tries to infer that distinction from the outside.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import config as config_mod
from . import peers as peers_mod


def _bool(env_name: str, default: bool) -> bool:
    """Same four lines as doxa.clock._bool / doxa.worktrees._bool -- the
    house convention for a bool_on setting is its own tiny copy, not a
    shared import for one helper."""
    raw = config_mod.raw(env_name).strip()
    if not raw:
        return default
    return raw.lower() not in ("0", "false", "no", "off")


def enabled() -> bool:
    """Effective value of the ``restore_tabs`` setting -- DEFAULT ON, same
    default-on shape as ``worktree_per_session``. ``DOXA_RESTORE_TABS=0``
    (or the settings-modal equivalent) is the only way back to today's
    single-most-recent-session behavior."""
    return _bool("DOXA_RESTORE_TABS", True)


@dataclass(frozen=True)
class TabRecord:
    """One tab in the saved set: which session, and the name the user
    pinned on it (``None`` for an automatic label -- that is derived fresh
    at restore time from the live engine/GitLine, never stored)."""

    session_id: str
    pinned_name: "str | None" = None


@dataclass(frozen=True)
class TabSetRecord:
    scope_key: str
    tabs: tuple[TabRecord, ...]
    active_session_id: "str | None"


@dataclass(frozen=True)
class ResolvedRestore:
    """:func:`resolve`'s answer: the saved tabs that are still live, paired
    with the peer registry entry to reattach through, IN SAVED ORDER --
    never the order their daemons happen to answer in -- plus how many of
    the saved tabs were skipped (dead), and which live session (if any)
    was the saved active tab."""

    tabs: "list[tuple[TabRecord, peers_mod.PeerInfo]]"
    skipped: int
    active_session_id: "str | None"


def tabsets_dir() -> Path:
    """Create-and-return the per-scope-record directory, 0700 -- same
    same-user boundary as the peer registry and the settings file."""
    d = config_mod.doxa_home() / "tabsets"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _file_for(scope_key: str) -> Path:
    """A scope key is a filesystem path (a repo root, or a bare cwd
    outside a repo) -- not a safe filename on its own (slashes, length,
    platform quirks). A truncated sha256 sidesteps all of that; the
    record's own ``scope_key`` field keeps the mapping legible for anyone
    reading the directory by hand."""
    digest = hashlib.sha256(scope_key.encode("utf-8")).hexdigest()[:24]
    return tabsets_dir() / f"{digest}.json"


def save(
    scope_key: str, tabs: "list[TabRecord]", active_session_id: "str | None"
) -> None:
    """Atomic write (tmp + ``os.replace``), 0600. Never raises: a
    persistence failure costs the user a future restore, never the
    running session -- the same posture ``doxa.config.save`` and
    ``doxa.peers.PeerHost._write_entry`` already take on their own state
    files."""
    if not scope_key:
        return
    payload = {
        "scope_key": scope_key,
        "active_session_id": active_session_id or None,
        "tabs": [
            {"session_id": t.session_id, "pinned_name": t.pinned_name}
            for t in tabs
            if t.session_id
        ],
    }
    path = _file_for(scope_key)
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        return


def load(scope_key: str) -> "TabSetRecord | None":
    """The saved set for this scope, or ``None`` -- a missing file, a
    corrupt one, or one that resolves to zero usable tabs all read as
    "nothing to restore" alike, never a crash and never a distinction the
    caller has to make itself."""
    if not scope_key:
        return None
    path = _file_for(scope_key)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    raw_tabs = data.get("tabs")
    if not isinstance(raw_tabs, list):
        return None
    tabs: list[TabRecord] = []
    for entry in raw_tabs:
        if not isinstance(entry, dict):
            continue
        session_id = str(entry.get("session_id") or "").strip()
        if not session_id:
            continue
        pinned = entry.get("pinned_name")
        tabs.append(
            TabRecord(
                session_id=session_id,
                pinned_name=str(pinned) if pinned else None,
            )
        )
    if not tabs:
        return None
    active = data.get("active_session_id")
    return TabSetRecord(
        scope_key=str(data.get("scope_key") or scope_key),
        tabs=tuple(tabs),
        active_session_id=str(active) if active else None,
    )


def resolve(scope_key: str) -> "ResolvedRestore | None":
    """Cross-reference the saved record against the LIVE daemon registry
    for this scope. ``None`` means there is no saved record at all --
    every OTHER outcome (including "every saved tab is dead") is a real
    :class:`ResolvedRestore` so the caller can report the skip count
    rather than silently falling back to nothing.

    A session id the registry no longer knows about is dropped here,
    silently, and counted -- it must never spawn a replacement (that
    would not be the session the user left) and must never block
    startup on a daemon that is provably gone."""
    record = load(scope_key)
    if record is None:
        return None
    live_by_id = {p.session_id: p for p in peers_mod.list_daemons(scope_key=scope_key)}
    live: "list[tuple[TabRecord, peers_mod.PeerInfo]]" = []
    skipped = 0
    for tab in record.tabs:
        entry = live_by_id.get(tab.session_id)
        if entry is None:
            skipped += 1
            continue
        live.append((tab, entry))
    active = record.active_session_id
    if active is not None and active not in live_by_id:
        active = None  # the saved active tab is itself dead -- no forced pick
    return ResolvedRestore(tabs=live, skipped=skipped, active_session_id=active)


def clear(scope_key: str) -> None:
    """Drop the saved record entirely -- not currently wired to any UI
    action (there is no "forget this repo's tabs" command yet), kept for
    tests and any future explicit-control surface; never raises."""
    if not scope_key:
        return
    with contextlib.suppress(OSError):
        _file_for(scope_key).unlink()
