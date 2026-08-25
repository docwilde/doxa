"""doxa.cli_isolation -- a CLI config directory DOXA owns, for the engine's
spawned ``claude`` process ONLY.

THE DEFECT (measured, see the audit note below): ``doxa.engine.SessionEngine``
never set ``ClaudeAgentOptions.env``, so the SDK's subprocess transport
inherited DOXA's own process environment verbatim
(``claude_agent_sdk/_internal/transport/subprocess_cli.py``: ``inherited_env
= {**os.environ, ...}``). With no ``CLAUDE_CONFIG_DIR`` of its own, the
spawned CLI read ``~/.claude`` -- the SAME config the operator's own
interactive ``claude`` sessions use, plugins and all. Measured live against
this machine's real ``~/.claude`` (5 plugins enabled, LORE among them): a
bare, otherwise-default ``claude -p`` call loaded 5 plugins, registered 16
plugin hooks and 28 plugin commands, and started an external MCP server --
on top of, not instead of, DOXA's own in-process LORE snapshot
(``ClaudeAgentOptions.system_prompt``'s ``append``). The LORE plugin's own
``SessionStart``/``UserPromptSubmit``/``PreCompact`` hooks
(``~/.claude/plugins/marketplaces/lore/hooks/hooks.json``) fire a SECOND,
independent memory injection into the same session, and its ``/lore:*``
slash commands become available to a model that has no business seeing
DOXA's own containment story from a foreign angle -- exactly the "94 staged
proposals, /lore:pending" citation the operator reported.

THE FIX: point every spawned engine CLI at a config directory DOXA owns
outright (:func:`cli_config_dir`, default ``~/.doxa/claude-cli/``,
respecting ``DOXA_HOME`` the same way every other durable-state path in this
app does) via ``CLAUDE_CONFIG_DIR`` in ``ClaudeAgentOptions.env`` -- NOT in
DOXA's own process environment, which stays untouched (see the identity.py
split note below). Measured: a fresh, empty ``CLAUDE_CONFIG_DIR`` alone --
before any extra flag -- drops plugin/hook/command loading straight to
zero (0 plugins, 0 hooks, 0 commands; ``installed_plugins.json`` and
``plugins/cache`` are resolved UNDER ``CLAUDE_CONFIG_DIR``, not hardcoded to
``~/.claude``). :func:`ensure_cli_config_dir` additionally writes a
DOXA-owned ``settings.json`` (:data:`OWNED_SETTINGS` -- empty: no ``hooks``,
no ``enabledPlugins``, no ``plugins``) so a bare directory and an explicit
"nothing is enabled here" read the same to anyone auditing the file later.
``LORE_SKIP=1`` rides the same env dict as belt-and-braces: even if a stray
config channel this module missed still fired a LORE plugin hook,
``lore_core.context``/``lore_core.deriver`` (the plugin's own dependency,
same package DOXA imports) self-suppress on that flag -- see
``lore_core/context.py``'s and ``lore_core/deriver.py``'s own
``os.environ.get("LORE_SKIP")`` checks, which is also the exact mechanism
``doxa.naming.generate_name``'s headless namer call already relies on.

REJECTED as the primary mechanism: ``--bare`` / ``CLAUDE_CODE_SIMPLE=1``.
Measured: it suppresses CLAUDE.md and plugins too, but ALSO hardcodes
"Anthropic auth is strictly ANTHROPIC_API_KEY... OAuth and keychain are
never read" -- setting it (as a flag OR as a bare env var) made an
already-authenticated ``claude auth status`` report ``loggedIn: false``.
Shipping that would be exactly the silent logout item AA forbids.
``--safe-mode`` was also measured: it zeroes plugin hooks too (confirmed via
``-d`` debug output: "Skipping plugin hooks - safe mode disables plugins"),
but does NOT stop a project's own ``CLAUDE.md`` from being read (measured:
a probe project's ``CLAUDE.md`` still surfaced verbatim under
``--safe-mode``, despite the CLI's own ``--help`` text claiming otherwise).
Since a project's OWN ``CLAUDE.md`` is ordinary Claude Code project context
-- not the foreign-plugin defect this module exists to close -- and the
dedicated ``CLAUDE_CONFIG_DIR`` already zeroes the actual defect (plugins/
hooks/commands) without it, ``--safe-mode`` is not added: one fewer flag
whose documented behaviour doesn't match its measured behaviour.

AUTH: a fresh ``CLAUDE_CONFIG_DIR`` is a LOGGED-OUT CLI (measured: no
``.credentials.json`` there, ``claude auth status`` reports
``loggedIn: false``). :func:`sync_credentials` closes that gap by copying
the user's own OAuth credentials file into the isolated directory,
READ-ONLY from the source's point of view (the source is never written) --
copying, not symlinking, so the isolated CLI's own token refresh never
touches the user's real file. Measured working end to end: copying
``~/.claude/.credentials.json`` into a fresh ``CLAUDE_CONFIG_DIR`` turned
``claude auth status`` there from ``loggedIn: false`` to
``loggedIn: true, subscriptionType: max``. Re-synced at every
:meth:`SessionEngine.start` (covers "at boot" for every session, since
each session's start IS its boot) and once more, forced, if the first
connect attempt fails at all (covers "on auth failure": a rotated token the
isolated copy hasn't seen yet). The mtime comparison in
:func:`sync_credentials` only copies FORWARD (source newer than the
isolated copy) unless ``force=True``, so the isolated CLI's own token
refresh (which writes back to ITS OWN copy, never the source) is not
clobbered by a later boot's opportunistic re-sync.

No credentials-path CLI flag was found (``claude --help`` documents none);
option (a) from the operator's preference order was not available to try.
No macOS Keychain path is handled here -- this machine's CLI stores OAuth
material as a plain file (``~/.claude/.credentials.json``, mode 600), which
is what :func:`sync_credentials` copies; a Keychain-backed installation
would need a different read path this module does not attempt, and that
gap is intentional to call out rather than silently no-op on.

SKILLS CARRY THROUGH, deliberately: learned skills
(``~/.claude/skills/<name>/SKILL.md``) are APPROVED artifacts -- a human
looked at them (``/lore:approve`` or the equivalent judge/update loop), not
a foreign hook firing unasked. Cutting them with the rest of the plugin
channel would be a regression the operator caught before this shipped:
skills reached DOXA sessions only because the (unisolated) spawned CLI
happened to read the user's real ``~/.claude``, and closing that channel
without an explicit carry would make them silently vanish.
:func:`ensure_skills_link` symlinks ``<cli_config_dir>/skills`` at the
user's real skills directory (same base as :func:`user_credentials_path`)
-- one store, no divergence: an approval recorded through either carrier
(the LORE plugin's own judge loop, or a future DOXA-side one) lands in both
immediately, because there is only one directory, not a copy of it. Ships
ALL skills in that directory, not a lore-tagged subset: on this measured
install every entry under ``~/.claude/skills`` already IS lore-learned, and
a user's own hand-written Claude Code skill vanishing inside DOXA is the
same class of surprise as a lore one vanishing -- the directory is the
scope, same as ``--add-dir``. Measured: the CLI resolves
``<CLAUDE_CONFIG_DIR>/skills`` as its "skill dir commands" source (debug
output: ``getSkills returning: N skill dir commands...``) and follows a
symlink there exactly like a real directory (a symlinked skills dir with
12 entries loaded all 12, and the CLI correctly answered a question about
one of them by name) -- no CLI flag or ``LORE_SKILLS_DIR``-style env
substitutes for this; ``LORE_SKILLS_DIR`` is ``lore_core``'s OWN knob for
where IT writes/reads a skill's source files, a different concern from
where the CLI ITSELF discovers skill-dir commands to advertise. Per-project
skill advertising (only the skills relevant to the tab's own repo) is NOT
attempted here: ``lore_core``'s skill usage records carry no project/cwd
stamp today (checked directly, see the module's own note on
``skill_usage.json`` in doxa/settings row and CHANGELOG for this item) --
building a cwd-based heuristic filter on top would be exactly the kind of
invented scoping the beliefs-chip work was told not to ship, so this
carries every skill, unfiltered, same as the CLI would have anyway.

THE SPLIT: ``doxa.identity`` deliberately keeps reading the REAL user
config (``CLAUDE_CONFIG_DIR`` or ``~/.claude``, read directly from THIS
process's own environment, which this module never modifies) for the
identity block and the subscription-usage chip -- that is what the CLI the
OPERATOR runs by hand is authenticated as, and it is the number ``/usage``
and the status line have always shown. The engine's SPAWNED CLI is the only
consumer of the isolated directory this module owns. Two consumers, one
credential (copied, not shared), two config directories.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from . import config as config_mod

DIR_NAME = "claude-cli"

# Exactly what the engine needs: nothing. No "hooks", no "enabledPlugins",
# no "plugins" key at all -- an absent key and an explicit empty one read
# the same to the CLI, but only the explicit form is auditable at a glance
# (doxa.doctor's isolation check greps for exactly these three keys).
OWNED_SETTINGS: dict = {}

CREDENTIALS_NAME = ".credentials.json"
SETTINGS_NAME = "settings.json"
SKILLS_NAME = "skills"


def cli_config_dir() -> Path:
    """Where the engine's isolated CLI config lives: ``$DOXA_HOME/claude-cli``
    (``DOXA_HOME`` default ``~/.doxa``) -- the same override every other
    piece of DOXA's durable state already honors, so a test pointing
    ``DOXA_HOME`` at a throwaway directory isolates this too, and so a real
    install keeps everything DOXA owns under one parent."""
    return config_mod.doxa_home() / DIR_NAME


def ensure_cli_config_dir() -> Path:
    """Create the directory and write the DOXA-owned ``settings.json`` if
    it is missing or has drifted from :data:`OWNED_SETTINGS`. DOXA is the
    ONLY writer of this file -- the isolated CLI itself writes plenty else
    into this directory as it runs (its own ``.claude.json``, session
    history, trust markers), which is expected and fine; this function only
    ever touches ``settings.json``, and only ever writes the one canonical
    form. Idempotent and cheap (a stat plus, at most, a few bytes) --
    called at every session start, not just first launch, since "single
    writer" means DOXA never lets anything else write this file, not that
    this function may only run once."""
    path = cli_config_dir()
    settings_path = path / SETTINGS_NAME
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
        current = None
        if settings_path.exists():
            try:
                current = json.loads(settings_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                current = None
        if current != OWNED_SETTINGS:
            tmp = settings_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(OWNED_SETTINGS, indent=2) + "\n", encoding="utf-8"
            )
            os.chmod(tmp, 0o600)
            os.replace(tmp, settings_path)
    except OSError:
        pass  # a provisioning failure costs isolation, not the session --
        # _build_options still passes CLAUDE_CONFIG_DIR either way, so a
        # read-only home directory degrades to "isolated, unauthenticated"
        # rather than "not isolated at all".
    return path


def user_config_base() -> Path:
    """The CLI's own config/credentials/skills directory -- ``$CLAUDE_CONFIG_DIR``
    when the operator's OWN environment sets it, else ``~/.claude``.

    Deliberately NOT the same as :func:`doxa.identity.claude_config_path`'s
    parent: measured live, when ``CLAUDE_CONFIG_DIR`` is unset the CLI's
    legacy top-level config file (``~/.claude.json``, what identity.py
    reads) sits directly in ``$HOME``, while credentials
    (``~/.claude/.credentials.json``) and skills (``~/.claude/skills/``)
    sit one level down, inside the ``~/.claude`` directory -- two different
    locations for two different files, an inconsistency an earlier version
    of this module got wrong (reused identity.py's resolution and silently
    found nothing to sync, since ``~/.credentials.json`` at the home root
    does not exist). Setting ``CLAUDE_CONFIG_DIR`` collapses that split
    (config, credentials AND skills all land directly under it, measured
    the same way DOXA's own isolated directory does), which is why this
    function only branches on whether it's set, not on which file it is
    resolving for."""
    base = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(base) if base else Path.home() / ".claude"


def user_credentials_path() -> Path:
    """The REAL user's OAuth credentials file -- see :func:`user_config_base`.
    This module never writes here; it is read exactly once per sync, to
    copy FROM."""
    return user_config_base() / CREDENTIALS_NAME


def isolated_credentials_path() -> Path:
    return cli_config_dir() / CREDENTIALS_NAME


def user_skills_path() -> Path:
    """The real user's learned-skills directory -- see
    :func:`user_config_base`."""
    return user_config_base() / SKILLS_NAME


def isolated_skills_path() -> Path:
    return cli_config_dir() / SKILLS_NAME


def ensure_skills_link() -> bool:
    """Symlink the isolated dir's ``skills`` at the user's real skills
    directory (see the module docstring's "SKILLS CARRY THROUGH" note).
    Idempotent: leaves a correct link alone, replaces a stale or wrong one,
    creates nothing when the user has no skills directory yet (a dangling
    symlink is worse than no symlink -- the CLI would report a broken
    path instead of simply finding zero skill-dir commands). Returns
    whether a real, valid link is in place after this call."""
    source = user_skills_path()
    if not source.is_dir():
        return False
    link = isolated_skills_path()
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(link.parent, 0o700)
        if link.is_symlink():
            if link.resolve() == source.resolve():
                return True
            link.unlink()
        elif link.exists():
            # Something real (not a symlink) already sits there -- never
            # delete a directory DOXA didn't create; leave it and report
            # "not linked" rather than destroy unknown content.
            return False
        link.symlink_to(source, target_is_directory=True)
    except OSError:
        return False
    return True


def sync_credentials(force: bool = False) -> bool:
    """Copy the user's OAuth credentials into the isolated directory.

    Copies only when the source is newer than the isolated copy (or the
    isolated copy doesn't exist yet), unless ``force`` -- so a later boot's
    opportunistic re-sync never clobbers a fresher token the ISOLATED CLI
    refreshed on its own (it writes that refresh back to its own copy only,
    never to the source). Returns whether a copy actually happened; never
    raises -- a sync DOXA cannot complete costs the isolated session its
    auth, never the host session."""
    source = user_credentials_path()
    dest = isolated_credentials_path()
    try:
        source_stat = source.stat()
    except OSError:
        return False  # nothing to copy from -- the real CLI has never
        # authenticated either, or stores credentials somewhere this
        # module doesn't read (e.g. a Keychain-backed install).
    if not force:
        try:
            if dest.stat().st_mtime >= source_stat.st_mtime:
                return False
        except OSError:
            pass  # no isolated copy yet: fall through and make one
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(dest.parent, 0o700)
        tmp = dest.with_suffix(".json.tmp")
        shutil.copyfile(source, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    except OSError:
        return False
    return True


def spawn_env() -> dict[str, str]:
    """The env overrides for EVERY spawned engine CLI
    (``ClaudeAgentOptions.env``): the isolated config dir, plus
    ``LORE_SKIP=1`` belt-and-braces (see module docstring). Provisions the
    directory and opportunistically syncs credentials as a side effect --
    cheap local file IO (a stat, maybe a few KB copied), not a subprocess,
    so doing it inline on every call (every session start) costs nothing a
    turn would notice."""
    ensure_cli_config_dir()
    sync_credentials()
    ensure_skills_link()
    return {
        "CLAUDE_CONFIG_DIR": str(cli_config_dir()),
        "LORE_SKIP": "1",
    }


def cli_session_file(session_id: str) -> "Path | None":
    """The isolated CLI's OWN transcript for ``session_id``, or ``None``
    when it has never heard of that id.

    The one question ``/resume`` has to answer before it offers anything:
    ``--resume`` reads the CLI's session store, which is a DIFFERENT store
    from the LORE transcript DOXA writes and ``/search`` indexes. Measured
    (v0.56.0, against a real ``claude`` under this module's own
    ``spawn_env``): resuming an id the store does not hold fails the turn
    outright with ``No conversation found with session ID: <id>``, and
    that is not an error worth discovering one prompt into a conversation
    the user thought they had reopened.

    Globbed across every project directory rather than encoding the cwd
    ourselves. The CLI's ``projects/`` subdirectory names are ITS
    encoding of a cwd, not ours, and a session id is a uuid: one glob
    answers "does the CLI know this conversation" exactly, with no second
    implementation of somebody else's path scheme to keep in step. Returns
    the path (callers only need its truthiness today, but the file is what
    the answer is ABOUT, and a bool would throw that away).

    Never raises: an unreadable config directory reads as "not
    resumable", which is the same answer the caller acts on anyway."""
    if not session_id:
        return None
    try:
        matches = sorted(
            (cli_config_dir() / "projects").glob(f"*/{session_id}.jsonl")
        )
    except OSError:
        return None
    for path in matches:
        try:
            if path.is_file() and path.stat().st_size > 0:
                return path
        except OSError:
            continue
    return None
