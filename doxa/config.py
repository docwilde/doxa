# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.config -- the settings file, and the precedence rule around it.

One rule, everywhere: **environment > config file > default.**

An env var is a deliberate act with a narrower scope than a file (a shell,
a launcher, a systemd unit, a test), so it must beat the file it can't see.
The config file (``$DOXA_HOME/config.toml``, default ``~/.doxa/config.toml``)
is where the settings modal writes what the user picked. A default is what
DOXA does when neither says otherwise.

The knobs here are exactly the knobs that already DO something -- each row
names the code that reads it. This module does not invent settings; it
gives the existing env knobs a persistent home and one lookup function
(:func:`raw`) that every reader now calls instead of ``os.environ.get``.
That single substitution is what makes the file effective without any
consumer growing settings logic of its own.

Nothing here is a credential store: the settings are model names, seconds,
thresholds and display toggles. Values are written back as TOML by a
deliberately small writer -- scalars, arrays, and tables, nested as
deeply as the value requires (``[projects]`` is the one DOXA writes
today) -- rather than a dependency, because the file has to stay
hand-editable and boring. It is not a general TOML serializer: a shape it
does not recognize (a TOML date/time is the one real gap) is refused
loudly (see :func:`_toml_value`) rather than flattened into a string
nothing can read back, which is how a hand-edited table used to be
destroyed by an unrelated settings-modal save.
"""

from __future__ import annotations

import os
import re
import tempfile
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import fcntl


@dataclass(frozen=True)
class Setting:
    """One knob: where it is stored, what overrides it, what reads it."""

    key: str
    """Key in config.toml. Empty for a read-only, display-only row."""

    env: str
    """Environment variable that overrides the file."""

    label: str
    """Row label in the settings modal."""

    help: str
    """One line: what it does, and what reads it."""

    kind: str = "str"
    """str | number | bool | bool_on | choice | strftime -- drives
    validation, not widgets. ``bool_on`` is ``bool`` for a knob that
    defaults ON: see the note on :func:`_coerce` for why it needs a
    different STORAGE representation, not just a different default."""

    choices: tuple[str, ...] = ()
    """The allowed values, when they are a fixed literal. Empty for a row
    whose options come from :attr:`choices_source` instead."""

    choices_source: "Callable[[], tuple[str, ...]] | None" = None
    """A callable returning the allowed values, for a row whose options are
    a REGISTRY rather than a literal.

    A CALLABLE, not a tuple, and that is forced rather than preferred: the
    one row that needs this (``engine``) takes its options from
    :func:`doxa.engines.available`, which registers ``doxa.codex`` and
    ``doxa.vendors`` on first lookup -- ~65 ms cold, most of it the
    ``lore_core`` import ``doxa.vendors`` pulls in. Evaluating that at
    import of THIS module would put it on every launch, including ``doxa
    --version``, ``doxa doctor`` and ``doxa launcher install``, none of
    which ever open a settings modal. Deferring it to :meth:`options` puts
    it where the answer is actually wanted (2.7 ms once the TUI is up,
    measured, because the app has already loaded what it drags in).

    The tuple field stays for every other choice row: an effort level or an
    image protocol is a closed literal with no registry behind it, and
    turning those into callables would buy nothing and read worse."""

    default: str = ""
    read_only: bool = False

    category: str = "Session"
    """Which tab of the settings modal this row lives on."""

    note: str = ""
    """Extra line under the help, for rows that need a caveat."""

    def options(self) -> "tuple[str, ...]":
        """The allowed values for this row, whichever way they are
        declared. THE one reader -- the placeholder the modal renders and
        the save-time check in :func:`_coerce` both come through here, so a
        registry-driven row cannot be validated against one list and
        displayed as another."""
        if self.choices_source is not None:
            return tuple(self.choices_source())
        return self.choices

    def placeholder(self) -> str:
        options = self.options()
        if options:
            return " | ".join(c for c in options if c)
        if self.kind in ("bool", "bool_on"):
            return "1 = on, empty = off" if self.kind == "bool" else "1 = on, 0 = off (empty = on)"
        if self.kind == "strftime":
            return "e.g. %a %H:%M (empty = built-in format)"
        return self.default or "(default)"


def _engine_choices() -> "tuple[str, ...]":
    """The ``engine`` row's options, read from the engine REGISTRY.

    ``doxa.engines.available()`` is already the one list ``doxa --engine
    <id>`` is validated against and the one an unknown id is refused with;
    this makes it the one the settings modal offers too. Before this the
    row carried the literal ``("", "claude", "codex")`` and the two vendor
    engines that shipped in 1.10.0 were unreachable from inside the app --
    a hardcoded pair beside a real registry, which is the drift that
    produces a fifth engine nobody can select either.

    Imported at the point of call, not at module scope: see
    :attr:`Setting.choices_source` for the measured cost, and note that
    ``doxa.engines`` imports ``doxa.config`` nowhere, so this direction is
    the only one there is.

    The leading ``""`` is the row's "unset" value, the same first element
    every other choice row here carries -- it means "no engine pinned", and
    :func:`engine` resolves that to ``doxa.engines.DEFAULT_ENGINE_ID``."""
    from . import engines as engines_mod

    return ("", *engines_mod.available())


# The knobs, in the order the modal shows them. Every row is load-bearing:
# there are no placeholder settings here, because a settings menu that
# lists something inert teaches the user that the menu lies.
SETTINGS: tuple[Setting, ...] = (
    Setting(
        key="engine", env="DOXA_ENGINE", label="engine", category="Session",
        kind="choice", choices_source=_engine_choices, default="claude",
        help="Which engine drives NEW sessions (doxa.engines -- `doxa "
             "--engine <id>` is the flag layer, `/engine` the in-app one)",
        note="Not every session surface exists on every engine, and the "
             "ones that do not are HIDDEN rather than shown inert -- no "
             "permission-mode chip where there are no modes, no ctx chip "
             "where no window size is reported, no cost chip where no "
             "dollar figure is. `/engine` prints what each one can and "
             "cannot do, read off doxa.engines.EngineCapabilities itself "
             "rather than described here, where it would go stale. An "
             "engine is chosen at CONNECT, so a change here reaches NEW "
             "sessions and tabs and never the running one.",
    ),
    Setting(
        key="model", env="DOXA_MODEL", label="model", category="Session",
        help="Model preference for the active session's engine, used by "
             "new sessions of that engine (/model switches the live session). "
             "DOXA_MODEL overrides every engine.",
    ),
    Setting(
        key="effort", env="DOXA_EFFORT", label="effort", category="Session",
        kind="choice", choices=("", "low", "medium", "high", "xhigh", "max"),
        help="Effort level for NEW sessions (doxa.engine.effort_level)",
        note="ClaudeAgentOptions.effort is a connect-time option -- the SDK "
             "has no live setter, so a running session keeps its own.",
    ),
    Setting(
        key="allow_bypass", env="DOXA_ALLOW_BYPASS",
        label="allow bypass", category="Session",
        kind="bool", default="",
        help="Let NEW sessions reach bypassPermissions at all "
             "(spawns their CLI with --allow-dangerously-skip-permissions)",
        note="OFF by default, and the default is the point. The claude CLI "
             "arms this capability at LAUNCH, not at runtime: a session "
             "started without the flag cannot enter bypassPermissions, and "
             "no setting can retrofit one that is already running. While "
             "this is off, the mode is absent from the Shift+Tab cycle, the "
             "chip's picker and /mode's list rather than being offered and "
             "refused. Turning it on puts every session spawned afterwards "
             "one keystroke away from running tools unapproved, in every "
             "repository you open.",
    ),
    Setting(
        key="adopt_plugins", env="DOXA_ADOPT_PLUGINS",
        label="adopt claude plugins", category="Session",
        kind="bool", default="",
        help="Load the commands, skills and agents from your OWN "
             "installed Claude Code plugins into NEW sessions "
             "(doxa.claude_plugins.adopt) -- never their hooks or MCP "
             "servers, and never the LORE plugin",
        note="OFF by default: isolation (doxa.cli_isolation, item AA) stays "
             "the resting posture, and adopting your plugins is a choice "
             "you make, not something a fresh install does for you. Turning "
             "this on does not undo the isolation fix -- hooks and MCP "
             "servers stay refused unconditionally (see docs/plans/"
             "plugins.md), only commands/skills/agents from plugins your "
             "own ~/.claude/settings.json already has enabled are staged "
             "into a sanitized copy and loaded via --plugin-dir, one "
             "session-scoped flag per adopted plugin. /plugins previews "
             "what this would adopt before you turn it on; /reload-plugins "
             "re-scans for NEW sessions without restarting doxa.",
    ),
    Setting(
        key="auto_diff", env="DOXA_AUTO_DIFF",
        label="auto-open the live diff", category="Session",
        kind="bool", default="",
        help="Open the live diff beside a session the FIRST time it edits "
             "the worktree, once per session (doxa.diff.auto_open_enabled / "
             "PaneRuntimeMixin._maybe_auto_open_diff)",
        note="OFF by default, and the default is the argument: opening the "
             "diff splits the group the session is in, halving the width of "
             "the transcript you are reading, and a surface that rearranges "
             "the screen mid-turn without being asked is worse than one you "
             "have to know about. ONCE per session, so closing it is final "
             "-- it never re-opens behind you. It never takes the keyboard "
             "(the prompt keeps focus), and on a window too narrow to split "
             "it REFUSES and says so rather than making an unusable sliver. "
             "The `diff N files +A −R` status chip is on either way and is "
             "how you see there are changes at all; F2 and /diff open the "
             "pane by hand at any time.",
    ),
    Setting(
        key="spawn_sessions", env="DOXA_SPAWN_SESSIONS",
        label="let sessions spawn sessions", category="Session",
        kind="bool", default="",
        help="Offer the model the spawn_session tool, which starts a "
             "SECOND daemon-backed DOXA session in this repo and hands it "
             "a task (doxa.session_ops)",
        note="OFF by default, and this row is the ONLY place it can be "
             "turned on: it is read through config.raw, so ~/.doxa/"
             "config.toml or DOXA_SPAWN_SESSIONS in your own shell are the "
             "two doors, and nothing inside a repository you open is one "
             "of them -- a repo that could arm this would be arbitrary "
             "code execution on `doxa new` against an untrusted clone. "
             "Turning it on costs real money and real machine: each "
             "spawned session is another claude process (~294 MB "
             "measured), another linked worktree (~18 MB measured for "
             "this repo), and its own token spend, additive to the "
             "session that asked for it. Every call still stops and asks "
             "you, showing the exact task text the child will be given, "
             "in every permission mode except bypassPermissions; the "
             "depth/live-count/rate caps in doxa.session_ops are enforced "
             "regardless of mode and cannot be raised from here.",
    ),
    Setting(
        key="agent_peer_send", env="DOXA_AGENT_PEER_SEND",
        label="let the model message other sessions", category="Session",
        kind="bool", default="",
        help="Offer the model the peer_send tool, which delivers a message "
             "straight into another live DOXA session (doxa.operators)",
        note="OFF by default, and this is the knob that retires DOXA's "
             "oldest statement about itself: until now the model had NO "
             "send tool, and every peer message crossed because a human "
             "typed /msg. Turning this on lets a model reach another "
             "session's context on its own initiative -- possibly in a "
             "different repository, since addressing is not scope-limited. "
             "Nothing about it is silent: every send is charged against a "
             "rate limit priced in DELIVERIES (a broadcast to 31 peers "
             "costs 31), every send is appended with its full body to "
             "$DOXA_HOME/peers/messages.jsonl, and both directions flash a "
             "light on the status bar. Read through config.raw, so this "
             "file or the environment are the two doors and nothing inside "
             "a repository you open is one of them. peer_list and "
             "peer_history stay available either way: seeing who is "
             "running changes nothing outside this process.",
    ),
    Setting(
        key="peer_inbound_turns", env="DOXA_PEER_INBOUND_TURNS",
        label="let an arriving message start a turn", category="Session",
        kind="bool", default="",
        help="An incoming peer message starts a turn when this session is "
             "idle, and queues behind the running one when it is not "
             "(doxa.engine.SessionEngine._on_peer_frame)",
        note="OFF by default, and deliberately NOT part of the row above: "
             "accepting messages and being woken by them are different "
             "grants, and a session may reasonably want the first without "
             "the second. With this off, an arriving message renders "
             "immediately and the model sees it on the next turn you "
             "start -- the behaviour DOXA has always had. With it on, a "
             "peer can spend this session's budget while you are not "
             "watching; a turn it started says so in its own first line "
             "and carries a peer- turn id into the ledger, so the spend "
             "has a traceable cause. A BROADCAST never starts a turn at "
             "any setting.",
    ),
    Setting(
        key="session_budget_usd", env="DOXA_SESSION_BUDGET_USD",
        label="session spend ceiling ($)", category="Session",
        kind="number", default="",
        help="Stop STARTING turns once this session has spent this many "
             "dollars (doxa.budget.session_ceiling / doxa.engine."
             "SessionEngine._budget_refusal)",
        note="OFF by default -- unset, and nothing about any session "
             "changes -- and it is the row the two above make necessary. "
             "With peer_send and inbound turns armed, sessions address "
             "each other across repositories and wake each other, and "
             "until this row existed nothing anywhere bounded what that "
             "cost. It bounds STARTING a turn, not a turn in flight: the "
             "only dollar figure that exists arrives with the message "
             "that ENDS a turn, so a session may exceed this by the price "
             "of the one turn that crosses it, and DOXA will not "
             "multiply tokens by a price sheet to pretend otherwise (see "
             "doxa.vendors). A session at its ceiling is STOPPED, not "
             "dead: it says so in the transcript, every command still "
             "answers, and raising this number here lets the next prompt "
             "through with no restart. A turn an arriving PEER message "
             "started is refused exactly like a typed one -- that path is "
             "the reason this exists. Read through config.raw, so this "
             "file and the environment are the two doors and nothing "
             "inside a repository you open is one of them. Enforceable "
             "only on an engine that reports cost: codex and both API "
             "vendors report none, their spend reads as $0.00, and the "
             "row says so rather than appearing to work. doxa.fleet's "
             "--run-budget sets a run-wide total and derives this per "
             "session, because thirty-two individually reasonable limits "
             "multiply into one unreasonable one.",
    ),
    Setting(
        key="permission_mode", env="DOXA_PERMISSION_MODE",
        label="permission mode", category="Session",
        kind="choice", choices=("", "default", "acceptEdits", "plan"),
        help="Permission mode NEW sessions connect in "
             "(doxa.engine.permission_mode_default); Shift+Tab cycles the "
             "running session, /mode sets it",
        note="Only default, acceptEdits and plan can be persisted -- "
             "doxa.engine.PERSISTABLE_MODES, and NARROWER than what the "
             "hotkey reaches. Shift+Tab can put the running session into "
             "auto or bypassPermissions, where DOXA stops asking you about "
             "tool calls; that is visible (a red chip, a transcript line) "
             "and lasts one session. A stored one would be silent and "
             "would apply to every future session, in repositories you "
             "have not read yet. dontAsk needs /mode and a confirmation.",
    ),
    Setting(
        key="linger_secs", env="DOXA_LINGER_SECS", label="linger secs",
        category="Session", kind="number", default="120",
        help="Seconds a daemon outlives its last client before finalizing "
             "(doxa.cli --linger default)",
    ),
    Setting(
        key="worktree_per_session", env="DOXA_WORKTREE",
        label="worktree per session", category="Session",
        kind="bool_on", default="1",
        help="Give each session its own git worktree (isolated edits, own "
             "branch doxa/<id>) instead of sharing the launch directory "
             "(doxa.worktrees.create)",
        note="Off returns to today's behavior exactly: every session runs "
             "directly in the launch directory. A clean, unmerged worktree "
             "is removed with its branch when the session ends; anything "
             "committed or dirty is kept for you to merge by hand -- never "
             "auto-merged.",
    ),
    Setting(
        key="restore_tabs", env="DOXA_RESTORE_TABS",
        label="restore tabs", category="Session",
        kind="bool_on", default="1",
        help="Reattach this repo's whole saved tab set -- order, pinned "
             "names, active tab, AND each tab's conversation -- on plain "
             "`doxa`, instead of the single most-recent session "
             "(doxa.tabsets)",
        note="`doxa new` always starts exactly one fresh tab and never "
             "restores; `doxa attach <prefix>` stays the single-session "
             "path either way. Off returns to today's single-most-recent "
             "spawn-or-attach exactly -- the record is still WRITTEN "
             "(so turning this back on later has something to restore "
             "from), just never read on launch. A tab whose session has "
             "ENDED comes back read-only over its transcript, marked as "
             "such; splits are not restored because DOXA has none.",
    ),
    Setting(
        key="resume_restored", env="DOXA_RESUME_RESTORED",
        label="resume restored tabs", category="Session",
        kind="bool_on", default="1",
        help="A restored tab whose session ENDED comes back as a LIVE "
             "session continuing that conversation, instead of a "
             "read-only transcript (v0.56.0)",
        note="Its own switch rather than a clause of `restore_tabs`, "
             "because it is the one part of restore that starts a "
             "PROCESS: one `claude` per resumed tab, spawned with "
             "--resume. It spends no tokens doing so -- the CLI loads "
             "that conversation from its own store and DOXA sends "
             "nothing until you type -- but a machine that comes back "
             "to six restored tabs starts six processes, and that is a "
             "choice worth being able to decline. Off is exactly "
             "v0.32.0's behaviour: read-only over the transcript, "
             "marked. A conversation the CLI has no history for (every "
             "session DOXA recorded before v0.56.0, when its id and the "
             "CLI's were still two different id spaces) falls back to "
             "read-only either way, and the tab says so.",
    ),
    Setting(
        key="mesh_open_browser", env="DOXA_MESH_OPEN_BROWSER",
        label="open the mesh graph in a browser", category="Session",
        kind="bool", default="",
        help="`/mesh` opens the graph in this machine's browser as well "
             "as printing its URL. OFF by default",
        note="Off because DOXA runs in terminals that have no browser to "
             "open: over SSH, in a container, on a headless box. "
             "`webbrowser.open` there either does nothing, prints a "
             "launcher's error over the TUI's own screen, or opens a "
             "text browser on top of it -- and the URL is printed either "
             "way, so nothing is lost by the default. On a desktop this "
             "saves a copy-paste.",
    ),
    # -- remote authorization (R1, docs/plans/remote.md) -----------------
    #
    # No transport ships yet -- these four rows are the allow/deny
    # decision a future bridge process will ask doxa.remote_policy to
    # make, given exactly the same "OFF and empty until told otherwise"
    # posture allow_bypass above already established for the local
    # cycler. Read together, not each in isolation:
    #
    #   remote_enabled          is a remote listener allowed to exist AT
    #                           ALL. Off means the other three rows are
    #                           moot -- there is nothing for them to gate.
    #   remote_allowed_logins   DOXA's OWN allow-list, defence in depth
    #                           on top of the tailnet's. Deliberately NOT
    #                           a default-allow when remote_enabled is on
    #                           and this is empty: an empty list refuses
    #                           EVERYONE, the same direction every other
    #                           allow-list in security software fails in.
    #   remote_allow_shell      the `!` shell escape (v0.36.0), OFF over
    #                           the remote surface unless this says so --
    #                           see doxa.shell's own "only a keystroke
    #                           reaches this" invariant, which a network
    #                           request is not.
    #   remote_allow_bypass     may a REMOTE request raise the permission
    #                           mode to bypassPermissions. Independent of,
    #                           and narrower than, allow_bypass above:
    #                           that row decides whether the mode is
    #                           reachable from THIS keyboard at all; this
    #                           one decides whether a request arriving
    #                           over the network may ask for it. Both
    #                           must be on for a remote bypass request to
    #                           succeed -- doxa.remote_policy checks only
    #                           its own row, and doxa.engine's own arming
    #                           check still applies on top of it.
    Setting(
        key="remote_enabled", env="DOXA_REMOTE_ENABLED",
        label="remote listening", category="Remote",
        kind="bool", default="",
        help="Start the private remote peer bridge with this daemon "
             "(doxa.remote_policy.remote_enabled)",
        note="OFF by default. When on, the first live daemon starts a "
             "machine-wide bridge with a 0600 Unix socket in its runtime "
             "directory for `tailscale serve` to proxy to; it does not "
             "expose a TCP port. Turning it on grants "
             "nothing by itself: remote_allowed_logins below still "
             "refuses every identity while it is empty.",
    ),
    Setting(
        key="remote_allowed_logins", env="DOXA_REMOTE_ALLOWED_LOGINS",
        label="remote allowed logins", category="Remote",
        kind="str", default="",
        help="Comma-separated Tailscale logins (the Tailscale-User-Login "
             "header a `tailscale serve` loopback listener attaches) "
             "allowed to drive this session remotely "
             "(doxa.remote_policy.allowed_logins)",
        note="Empty means what an empty allow-list means everywhere else "
             "in DOXA: refuse everyone, not allow everyone -- turning "
             "remote_enabled on with this row still empty leaves the "
             "surface enabled and unreachable by anybody. This is DOXA's "
             "OWN list, kept even though the tailnet already answers "
             "identity, because a security boundary that trusts a single "
             "layer is one misconfiguration away from trusting nobody's "
             "check at all (docs/plans/remote.md's defence-in-depth "
             "rule). Compared case-insensitively; entries are matched "
             "verbatim otherwise, so a typo'd login is a silent refusal, "
             "same as an absent one.",
    ),
    Setting(
        key="remote_peers", env="DOXA_REMOTE_PEERS",
        label="remote peers", category="Remote",
        kind="str", default="",
        help="Other machines' peer bridges, comma-separated as "
             "label=host[:port] -- e.g. "
             "workstation=ws.tail1234.ts.net:47600 "
             "(doxa.peernet.endpoints)",
        note="EMPTY by default: DOXA looks for peers on this machine only, "
             "through the 0700 registry directory, exactly as it always "
             "has. An entry here is a hostname and a name to show -- it "
             "is NOT a credential and there is nowhere in DOXA to put "
             "one (docs/plans/remote.md: 'no new credential store'). What "
             "makes a listed endpoint trustworthy is the private network "
             "it is on. A peer fetched from one is marked with the label "
             "you gave it everywhere a local peer would appear, and the "
             "label is stamped from the endpoint DOXA dialled, never from "
             "anything the other machine claimed about itself.",
    ),
    Setting(
        key="remote_bind", env="DOXA_REMOTE_BIND",
        label="remote bind address", category="Remote",
        kind="str", default="127.0.0.1",
        help="Legacy TCP test-adapter address (doxa.peernet.bind_host)",
        note="The daemon's production bridge ignores this setting and uses "
             "a private Unix socket. TCP cannot distinguish tailscaled from "
             "another local process that forges Tailscale-User-Login, so "
             "the default verifier refuses TCP requests.",
    ),
    Setting(
        key="remote_port", env="DOXA_REMOTE_PORT",
        label="remote bind port", category="Remote",
        kind="number", default="47600",
        help="Legacy TCP test-adapter port (doxa.peernet.bind_port)",
        note="The daemon's production bridge ignores this setting. Configure "
             "the externally served Tailscale port with `tailscale serve`; "
             "the DOXA backend is its private Unix socket.",
    ),
    Setting(
        key="remote_proxy_uid", env="DOXA_REMOTE_PROXY_UID",
        label="remote proxy uid", category="Remote",
        kind="number", default="0",
        help="Unix UID of the local Tailscale Serve proxy allowed to pass "
             "Tailscale identity headers (doxa.peernet.proxy_uid)",
        note="The remote bridge uses a private Unix socket, not a loopback "
             "TCP port: any local process can forge an HTTP header on TCP. "
             "On Linux the bridge reads SO_PEERCRED and accepts a header "
             "only when the peer has this UID. Tailscaled normally runs as "
             "root (0), or another service UID distinct from the DOXA user. "
             "DOXA refuses an unprivileged UID equal to its own because any "
             "same-user process could then forge a header. Point Tailscale "
             "Serve at the bridge with `tailscale serve --bg "
             "unix:/absolute/path/to/peernet.sock`; a malformed or unsafe "
             "value refuses every request.",
    ),
    Setting(
        key="remote_allow_shell", env="DOXA_REMOTE_ALLOW_SHELL",
        label="remote allow shell", category="Remote",
        kind="bool", default="",
        help="Let a remote driver run `!` shell commands "
             "(doxa.remote_policy.remote_allow_shell)",
        note="OFF by default. doxa.shell's own security section is built "
             "on exactly one guarantee -- 'the only thing that can reach "
             "it is a keystroke the user typed into the prompt' -- and a "
             "request arriving over a network, however authenticated, is "
             "not that. This row is the explicit, opt-in exception "
             "docs/plans/remote.md calls for, not a default DOXA chose "
             "for you.",
    ),
    Setting(
        key="remote_allow_bypass", env="DOXA_REMOTE_ALLOW_BYPASS",
        label="remote allow bypass", category="Remote",
        kind="bool", default="",
        help="Let a remote driver raise the permission mode to "
             "bypassPermissions (doxa.remote_policy.remote_allow_bypass)",
        note="OFF by default, and independent of allow_bypass above: "
             "that row arms THIS session's CLI to reach bypassPermissions "
             "at all (a launch-time flag); this one decides whether a "
             "request that arrived over the network may ask for it. Both "
             "gates must be open for a remote bypass request to succeed. "
             "A mode that stops asking, requested from a phone that might "
             "be unlocked on a table, is a different risk from the same "
             "mode requested at the keyboard, and the conservative "
             "reading -- refuse unless told otherwise -- is the one this "
             "row encodes.",
    ),
    Setting(
        key="lore", env="DOXA_LORE", label="memory",
        category="Memory", kind="bool_on", default="1",
        help="Does a session have memory at all -- the LORE snapshot in "
             "its system prompt, the per-turn refresh, the lore_* tools, "
             "and every write back into the store "
             "(doxa.engine.lore_enabled_default)",
        note="ON by default, and the only switch in this file that "
             "REMOVES a capability rather than granting one. OFF means "
             "genuinely off: no snapshot is built, the lore_* operators "
             "are ABSENT from the model's tool list rather than present "
             "and refusing, and nothing is written -- no beliefs, no "
             "staged proposals, no session index. The transcript is "
             "still written (it is DOXA's own record; /resume and the "
             "transcript pane read it) and lore_core is still used to "
             "scrub secrets out of every line. This row is the DEFAULT: "
             "a session can be started with memory off individually "
             "(`doxa.daemon --no-lore`), which is what doxa.fleet uses "
             "to run memory-on and memory-off agents in one experiment.",
    ),
    Setting(
        key="derive_secs", env="DOXA_DERIVE_SECS", label="derive secs",
        category="Memory", kind="number", default="900",
        help="Streaming-deriver debounce interval, seconds; 0 or off "
             "disables it (doxa.engine.derive_interval)",
    ),
    Setting(
        key="consult_floor", env="DOXA_CONSULT_FLOOR", label="consult floor",
        category="Memory", kind="number", default="1.0",
        help="bm25 relevance floor for the act-time belief consult; 0 "
             "disables it (doxa.engine.consult_floor)",
    ),
    Setting(
        key="graph_context", env="DOXA_GRAPH_CONTEXT",
        label="graph context", category="Memory",
        kind="bool", default="",
        help="Add LORE's graph-backed context block (beliefs reached by a "
             "relation, ranked confidence-first) to the act-time consult "
             "(doxa.engine.graph_context_enabled)",
        note="OFF by default. Calls LORE's OWN builder "
             "(lore_core.graph.context_candidates/render_context_block, "
             "LORE 0.44.0+) rather than a second ranking implementation, "
             "as a SEPARATE stage from the consult floor above -- the two "
             "toggle independently. Unlike the consult note (silent unless "
             "something clears the relevance floor), this block falls back "
             "to the best-supported beliefs in scope when nothing matches "
             "the prompt, so once on it rides EVERY turn, budgeted under "
             "its own char cap but real, recurring cost -- see the "
             "graph_context_chars row in /context.",
    ),
    Setting(
        key="graph_view", env="DOXA_GRAPH_VIEW",
        label="belief graph view", category="Memory",
        kind="choice", choices=("", "browser", "ascii"), default="browser",
        help="How the beliefs picker's 'g graph' row action shows a "
             "belief's neighbourhood (doxa.beliefgraph.graph_view_mode): "
             "'browser' writes LORE's pan/zoom mermaid page under "
             "$DOXA_HOME/graphs and opens it; 'ascii' inserts LORE's own "
             "edge block as rows beneath the belief, in the TUI. Empty = "
             "browser.",
        note="Per BELIEF, never whole-graph, and that is measured rather "
             "than chosen: a whole-graph view filtered to asserted "
             "relations fragmented 104 beliefs into 44 clusters, which "
             "mermaid stacks vertically -- 1188x13814 pixels, fitting on "
             "screen at 5%. A k-hop neighbourhood is connected by "
             "construction. 'ascii' is the answer for a headless or "
             "SSH session; 'browser' prints the file's path into the "
             "transcript either way, so one still ends up with something "
             "to scp when no browser opens.",
    ),
    Setting(
        key="lore_root", env="LORE_ROOT", label="lore store", category="Memory",
        help="Where the belief store and session index live (lore_core.ROOT)",
        note="Shared with the Claude Code LORE plugin -- one store, two "
             "carriers. Set LORE_ROOT to point elsewhere; a private store "
             "would fork your memory into two divergent halves. /setup "
             "makes and stickies this choice -- read_only here because "
             "this row is /setup's, not the settings modal's, to edit.",
        read_only=True,
    ),
    Setting(
        key="nerd_font", env="DOXA_NERD_FONT", label="nerd font",
        category="Appearance", kind="bool",
        help="Use the nerd-font branch glyph instead of the branch sign in "
             "the status line (doxa.app.git_branch_symbol)",
    ),
    Setting(
        key="ctx_absolute", env="DOXA_CTX_ABSOLUTE", label="ctx: absolute tokens",
        category="Appearance", kind="bool",
        help="Print used/total tokens beside the ctx% chip "
             "(doxa.ui.labels.ctx_chip)",
        note="Off, the numbers are still one hover away -- the ctx chip's "
             "tooltip carries them either way, and /usage prints them in "
             "full. On, they are dropped again on a terminal narrower than "
             "100 columns rather than pushing other chips off the bar. A "
             "context limit the CLI never reported reads `?`; DOXA does not "
             "guess a window size.",
    ),
    Setting(
        key="image_mode", env="DOXA_IMAGE_MODE", label="image mode",
        category="Appearance", kind="choice",
        choices=("", "probe", "kgp", "sixel", "halfblock", "text"),
        help="Empty = text fallback (fast startup); probe = detect terminal "
             "support; or force a rung of the image ladder",
    ),
    Setting(
        key="boot_banner", env="DOXA_BOOT_BANNER", label="boot banner",
        category="Appearance", kind="bool_on", default="1",
        help="Draw the DOXA mark above the session's opening identity "
             "block (doxa.banner.enabled)",
        note="A plain on/off knob since v0.70.0 -- the drawn ring-and-"
             "triangle mark is the only form there is now, on every "
             "terminal; v0.58.0-0.65.0 also drew a raster logo.png on "
             "kitty-graphics/sixel terminals ('auto'/'image'), which "
             "read better than a half-block downscale but not better "
             "than the drawn mark, so it is gone rather than kept as a "
             "second look. A config.toml still holding 'auto', 'blocks' "
             "or 'image' from before this collapse keeps meaning on -- "
             "only an explicit off (or the pre-v0.49.0 0/false/no) turns "
             "the banner off. /img still shows the raster logo on "
             "request, in every tier this terminal answers for.",
    ),
    Setting(
        key="key_notice", env="DOXA_KEY_NOTICE",
        label="unreachable key notice", category="Appearance",
        kind="bool_on", default="1",
        help="One line at session start naming bound keys THIS terminal "
             "can't deliver and the slash command that reaches them "
             "instead (doxa.keyboard.notice_enabled / "
             "doxa.ui.labels.unreachable_notice)",
        note="Empty exactly when there is nothing to say: a "
             "kitty-protocol terminal (nothing lost), or one whose "
             "protocol was never measured -- UNKNOWN is not LEGACY, so an "
             "unmeasured terminal stays silent rather than guessing "
             "(doxa/keyboard.py). Off returns to plain silence; /help and "
             "/doctor still report the same keys either way.",
    ),
    Setting(
        key="context_grid", env="DOXA_CONTEXT_GRID", label="context grid style",
        category="Appearance", kind="choice", choices=("", "glyphs", "ascii"),
        help="Cell style for /context's 10x20 usage grid "
             "(doxa.ui.labels.context_grid_mode): 'glyphs' draws the "
             "draughts glyphs (⛀⛁⛶, Claude Code's own look); 'ascii' draws "
             "bracket cells ([#]/[ ]) for a terminal font that tofu's the "
             "Miscellaneous Symbols block. Empty = glyphs.",
        note="DOXA cannot probe a terminal's own font coverage -- nothing "
             "in a terminal reports that -- so this is a manual switch, "
             "not detection: see tofu on the grid once, flip it here. "
             "Both styles read the identical measured cells and the "
             "identical per-category colors; only the two characters "
             "change.",
    ),
    Setting(
        key="show_reasoning", env="DOXA_SHOW_REASONING", label="show reasoning",
        category="Appearance", kind="bool_on", default="1",
        help="Stream the model's summarized reasoning into a collapsed "
             "'Reasoning' section per turn (doxa.engine._build_options / "
             "doxa.app.ReasoningSection)",
        note="On: requests thinking={type: adaptive, display: summarized} "
             "at connect. Off: DOXA asks for nothing extra and leaves the "
             "model's own default alone -- it does NOT force thinking off, "
             "because some models (Claude Fable 5, Claude Mythos 5, Claude "
             "Mythos Preview) reject an explicit disable outright. On "
             "those models thinking runs (and is billed) regardless of "
             "this toggle; off only stops DOXA from asking to see it.",
    ),
    Setting(
        key="background", env="DOXA_BACKGROUND", label="background",
        category="Appearance", kind="choice",
        choices=("", "opaque", "transparent"), default="opaque",
        help="Paint the app's own background (opaque), or leave it "
             "unpainted so the terminal's own background shows through "
             "(transparent) (doxa.app.DoxaApp.get_theme_variable_defaults)",
        note="DOXA can only stop PAINTING its background -- making the "
             "terminal WINDOW itself see-through is your terminal "
             "emulator's job (kitty's background_opacity, WezTerm's "
             "window_background_opacity, etc.). On an opaque terminal "
             "this setting changes nothing visible. Validated against "
             "dark terminal backgrounds, same as the rest of DOXA's "
             "palette -- a light terminal background will render body "
             "text at very low contrast.",
    ),
    Setting(
        key="sidebar", env="DOXA_SIDEBAR", label="session sidebar",
        category="Appearance", kind="bool_on", default="",
        help="Show the collapsible session rail down the left of the "
             "window (F3, /sidebar — doxa.ui.sidebar.SessionSidebar)",
        note="THREE states, which is why this row is bool_on and not "
             "bool: empty means AUTO -- the rail appears once there is "
             "something for it to say (any collection, or a second "
             "session) and stays hidden before that, the hide-at-zero "
             "discipline the context chip and the group tab strips "
             "already follow. 1 pins it open, 0 pins it shut, and F3 "
             "writes one of those two, so the first toggle ends the "
             "guessing for good. The rail REFUSES to open on a window "
             "too narrow to hold it and the panes both "
             "(doxa.layout.sidebar_refusal): it says so rather than "
             "squeezing a pane below its floor.",
    ),
    Setting(
        key="sidebar_width", env="DOXA_SIDEBAR_WIDTH",
        label="session sidebar: width", category="Appearance",
        kind="number", default="25",
        help="Columns the session rail occupies "
             "(doxa.layout.SIDEBAR_WIDTH; clamped to 22–41). Drag the "
             "rail's right edge, or Alt+Shift+←/→, to change it",
        note="Clamped, never rejected: 25 is derived as the rail's own "
             "chrome (9 columns) plus half the tab-label cap the strip "
             "writes at, 22 is the width below which a row cannot show "
             "the label floor the tab strip keeps legible, and 41 is the "
             "width at which the whole capped label fits and wider buys "
             "nothing. All three moved by two in v1.5.0: "
             "doxa.layout.SIDEBAR_CHROME was re-measured against the "
             "rail's DEEPEST row -- a tab row under a pane entry under a "
             "heading -- which v1.2.0 added without re-pricing. A drag "
             "and the keys write this row, and both refuse at the same "
             "floor opening the rail refuses at.",
    ),
    Setting(
        key="clock_show", env="DOXA_CLOCK_SHOW", label="clock: show",
        category="Appearance", kind="bool_on", default="1",
        help="Show the fixed-width clock at the right edge of the tab "
             "bar (doxa.clock.ClockConfig)",
        note="The one bool setting in this app that defaults ON -- an "
             "empty field here still means the clock shows; type 0 to "
             "turn it off.",
    ),
    Setting(
        key="clock_date", env="DOXA_CLOCK_DATE", label="clock: show date",
        category="Appearance", kind="bool",
        help="Prefix the clock with %Y-%m-%d (doxa.clock.builtin_format)",
    ),
    Setting(
        key="clock_hour", env="DOXA_CLOCK_HOUR", label="clock: hour format",
        category="Appearance", kind="choice", choices=("", "12", "24"),
        default="24",
        help="12- or 24-hour clock (doxa.clock.builtin_format)",
    ),
    Setting(
        key="clock_seconds", env="DOXA_CLOCK_SECONDS",
        label="clock: show seconds", category="Appearance", kind="bool",
        help="Show :SS; also switches the clock's one timer from minute- "
             "to second-aligned (doxa.clock.seconds_until_boundary)",
    ),
    Setting(
        key="clock_tz", env="DOXA_CLOCK_TZ", label="clock: timezone",
        category="Appearance",
        help="IANA zone name, e.g. Europe/Berlin; empty = system local "
             "(doxa.clock.resolve_tz)",
        note="An unresolvable name falls back to system local time, "
             "visibly (the clock's tooltip says so) rather than silently.",
    ),
    Setting(
        key="clock_format", env="DOXA_CLOCK_FORMAT",
        label="clock: custom format", category="Appearance",
        kind="strftime",
        help="strftime format overriding the toggles above "
             "(doxa.clock.render)",
        note="Validated on save (a value strftime rejects is not stored); "
             "a value that becomes invalid later (a hand-edited file, an "
             "env var) falls back to the built-in format at render time, "
             "visibly, rather than crashing the clock.",
    ),
    Setting(
        key="notify", env="DOXA_NOTIFY", label="notify",
        category="Notifications", kind="choice",
        choices=("auto", "always", "off"), default="auto",
        help="When to send desktop notifications: auto (only while the "
             "terminal window is unfocused), always, or off (doxa.notify)",
    ),
    Setting(
        key="notify_update", env="DOXA_NOTIFY_UPDATE",
        label="notify: update available", category="Notifications",
        kind="bool_on", default="1",
        help="Notify when /update has something to pull "
             "(doxa.notify.notify_update_available)",
    ),
    Setting(
        key="notify_lore", env="DOXA_NOTIFY_LORE",
        label="notify: lore review", category="Notifications",
        kind="bool_on", default="1",
        help="Notify when LORE stages memory proposals; off also silences "
             "lore_core's own in-process notification (LORE_NOTIFY) -- see "
             "doxa.notify.sync_lore_notify_env",
        note="This is lore_core's OWN banner, which knows nothing about "
             "window focus. 'notify: proposals staged' below is DOXA's "
             "focus-gated replacement for it, and while that one is on "
             "this one is held silent so a single staged batch produces a "
             "single notification.",
    ),
    Setting(
        key="notify_staged", env="DOXA_NOTIFY_STAGED",
        label="notify: proposals staged", category="Notifications",
        kind="bool_on", default="1",
        help="Notify when the streaming background reviewer stages memory "
             "proposals (doxa.notify.notify_staged)",
        note="Fires off the streaming deriver (derive_secs), names the tab "
             "and quotes the first proposal, and is gated like every other "
             "trigger above -- so it stays quiet while you are looking at "
             "DOXA. Turn it off and 'notify: lore review' decides on its "
             "own again (doxa.notify.sync_lore_notify_env).",
    ),
    Setting(
        key="notify_needs_input", env="DOXA_NOTIFY_NEEDS_INPUT",
        label="notify: needs input", category="Notifications",
        kind="bool", default="",
        help="Notify when a session is waiting on you",
        note="OFF by default -- the ONLY notification-worthy turn outcome "
             "(a plain finished response is not one; see the module "
             "docstring of doxa.notify) is still opt-in, on the owner's "
             "own call. Fires on an AskUserQuestion or a permission "
             "request the CLI would have prompted on (doxa.engine's "
             "can_use_tool callback) -- while it's attached, gated like "
             "every other trigger above; a fully detached session (nobody "
             "attached at all) always notifies once this is on, since "
             "there is no window to blink instead.",
    ),
    Setting(
        key="", env="DOXA_HOME", label="doxa home", category="Paths",
        help="Durable DOXA state: this config, the window layout",
        read_only=True,
    ),
    Setting(
        key="", env="DOXA_RUNTIME_DIR", label="runtime dir", category="Paths",
        help="Ephemeral endpoints: daemon sockets and the peer registry "
             "(doxa.peers.runtime_dir)",
        note="Deliberately NOT under ~/.doxa: home directories can be NFS "
             "(AF_UNIX misbehaves) and stale sockets must not outlive a "
             "reboot.",
        read_only=True,
    ),
)

SETTINGS_BY_KEY: dict[str, Setting] = {s.key: s for s in SETTINGS if s.key}
SETTINGS_BY_ENV: dict[str, Setting] = {s.env: s for s in SETTINGS}


# Where DOXA's durable state lives. Deliberately ~/.doxa (DOXA_HOME
# overrides), mirroring ~/.claude -- and deliberately NOT the runtime dir:
#
#   ~/.doxa        durable state: config.toml, window layout, anything that
#                  must survive a reboot.
#   runtime dir    ephemeral endpoints: the daemon sockets and the peer
#                  registry ($DOXA_RUNTIME_DIR -> $XDG_RUNTIME_DIR/doxa ->
#                  ~/.local/share/doxa). Sockets stay there because a home
#                  directory can be NFS (AF_UNIX misbehaves there) and
#                  because stale socket files must not survive a reboot,
#                  which the runtime dir's tmpfs semantics guarantee.
#
# The LORE store is neither: it stays lore_core's own (~/.claude/lore,
# LORE_ROOT-overridable) because sharing one store with the Claude Code
# plugin is a product property -- a private DOXA store would silently fork
# the user's memory and beliefs into two divergent halves.
_MIGRATED = False


def doxa_home() -> Path:
    base = os.environ.get("DOXA_HOME", "").strip()
    return Path(base) if base else Path.home() / ".doxa"


def config_dir() -> Path:
    return doxa_home()


def config_path() -> Path:
    return doxa_home() / "config.toml"


def legacy_config_path() -> Path:
    """Where an early build wrote it (XDG). Migrated once, then forgotten."""
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return (Path(base) if base else Path.home() / ".config") / "doxa" / "config.toml"


def migrate_legacy() -> "Path | None":
    """Move a pre-~/.doxa config into place, once per process. Returns the
    destination when something was actually moved, else None -- so the
    caller can say so out loud rather than silently relocating a file."""
    global _MIGRATED
    if _MIGRATED:
        return None
    _MIGRATED = True
    destination = config_path()
    legacy = legacy_config_path()
    if destination.exists() or not legacy.exists() or legacy == destination:
        return None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(destination.parent, 0o700)
        os.replace(legacy, destination)
    except OSError:
        return None
    invalidate()
    return destination


# Cache key: (path, mtime, size) -- same discipline as doxa.identity. The
# file is read on every knob lookup (per turn, per status refresh), so
# re-parsing it each time would be a small tax on a hot path. A save moves
# the mtime; save() also invalidates directly for same-tick rewrites.
_CACHE: "tuple[tuple[str, float, int], dict[str, Any]] | None" = None


def invalidate() -> None:
    global _CACHE
    _CACHE = None


def load() -> dict[str, Any]:
    """The config file as a flat dict, or ``{}``.

    Never raises: a missing file, an unreadable one and malformed TOML all
    mean "no stored settings" to every caller -- a broken config must cost
    the user their customizations, never their session."""
    global _CACHE
    path = config_path()
    try:
        stat = path.stat()
    except OSError:
        _CACHE = None
        return {}
    key = (str(path), stat.st_mtime, stat.st_size)
    if _CACHE is not None and _CACHE[0] == key:
        return _CACHE[1]
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return {}
    data = data if isinstance(data, dict) else {}
    _CACHE = (key, data)
    return data


def raw(env_name: str) -> str:
    """The effective value of one knob, as a string: env, then file, then
    "" (which every existing reader already treats as "use the default").

    This is the single substitution that makes the config file real -- the
    readers in engine/app/images call THIS instead of os.environ.get, and
    keep their own parsing and their own defaults."""
    value = os.environ.get(env_name, "")
    if value.strip():
        return value
    setting = SETTINGS_BY_ENV.get(env_name)
    if setting is None or not setting.key:
        return ""
    stored = load().get(setting.key)
    if stored is None or stored == "":
        return ""
    if isinstance(stored, bool):
        return "1" if stored else ""
    return str(stored)


def effective(env_name: str) -> str:
    """:func:`raw` with the declared default filled in -- for display."""
    value = raw(env_name)
    if value.strip():
        return value
    setting = SETTINGS_BY_ENV.get(env_name)
    return setting.default if setting else ""


def provenance(env_name: str) -> tuple[str, str]:
    """``(source, value)`` for one knob -- where the effective value came
    from, resolved through the same precedence every reader uses.

    ``source`` is "env", "config" or "default". The settings modal shows
    this next to every row: a value the user cannot change from the UI must
    be visibly EXPLAINED, not mysteriously ignored."""
    env_value = os.environ.get(env_name, "")
    if env_value.strip():
        return "env", env_value
    setting = SETTINGS_BY_ENV.get(env_name)
    if setting is not None and setting.key:
        stored = load().get(setting.key)
        if stored is not None and stored != "":
            if isinstance(stored, bool):
                return "config", "true" if stored else "false"
            return "config", str(stored)
    return "default", (setting.default if setting else "")


def source_label(env_name: str) -> str:
    """The human form of :func:`provenance`'s source, naming the env var
    that is winning when one is."""
    source, _value = provenance(env_name)
    if source == "env":
        return f"env {env_name} — overrides config"
    return source


def overridden_by_env(env_name: str) -> bool:
    """True when the environment is what is winning -- the settings modal
    says so out loud, because an edit that silently does nothing is the
    worst thing a settings menu can do."""
    return bool(os.environ.get(env_name, "").strip())


def linger_secs() -> float:
    """The daemon linger knob, parsed. Garbage falls back to the default
    rather than crashing the CLI on a typo in a config file."""
    from .defaults import DEFAULT_LINGER_SECS

    value = raw("DOXA_LINGER_SECS").strip()
    if not value:
        return DEFAULT_LINGER_SECS
    try:
        parsed = float(value)
    except ValueError:
        return DEFAULT_LINGER_SECS
    return parsed if parsed >= 0 else DEFAULT_LINGER_SECS


def mesh_open_browser() -> bool:
    """``DOXA_MESH_OPEN_BROWSER`` / the config file's
    ``mesh_open_browser`` row, default OFF: does ``/mesh`` try to open the
    graph in a browser, or only print its URL?

    Read per call, like every other env-driven knob here, so the settings
    modal's toggle takes effect on the next ``/mesh`` without a
    restart."""
    return raw("DOXA_MESH_OPEN_BROWSER").strip().lower() not in (
        "", "0", "false", "no", "off",
    )


def background_mode() -> str:
    """``"opaque"`` or ``"transparent"`` -- an unset or unrecognized value
    (a typo'd env var, a hand-edited config, a future value an older DOXA
    doesn't know) falls back to ``"opaque"`` rather than crashing the app:
    the SAME rule :func:`_coerce`'s ``choices`` check already applies at
    save time, applied again here for values that reached the file some
    other way."""
    value = raw("DOXA_BACKGROUND").strip()
    return value if value in ("opaque", "transparent") else "opaque"


def stored_model(engine_id: str = "claude") -> "str | None":
    """The file preference for one engine, without the global env override.

    The original top-level ``model`` key remains Claude's preference, so
    existing config files and the Claude settings row keep their meaning.
    Other engines have independent entries in ``[models]``. A malformed
    hand-edited entry is ignored instead of reaching an unrelated engine.
    """
    stored = load()
    if engine_id == "claude":
        value = stored.get("model")
    else:
        models = stored.get("models")
        value = models.get(engine_id) if isinstance(models, dict) else None
    if not isinstance(value, str):
        return None
    return value.strip() or None


def model_provenance(engine_id: str = "claude") -> tuple[str, str]:
    """The effective model and its source for an engine's settings row."""
    override = os.environ.get("DOXA_MODEL", "").strip()
    if override:
        return "env", override
    stored = stored_model(engine_id)
    return ("config", stored) if stored else ("default", "")


def model(engine_id: str = "claude") -> "str | None":
    """Model for a new session of ``engine_id``, or the engine default.

    ``DOXA_MODEL`` is an explicit process-wide override. The CLI's
    ``--model`` takes precedence over this function's answer.
    """
    _source, value = model_provenance(engine_id)
    return value or None


def engine() -> str:
    """WHICH engine drives a new session (v1.4.0) -- ``"claude"`` unless
    told otherwise.

    Same flag > env > file > default precedence as every other row here;
    the flag layer is ``doxa --engine``. NOT validated at this layer, on
    purpose: :func:`doxa.engines.get` is the one place an unknown id is
    refused, and it refuses by LISTING the real ones. A second validator
    here would either duplicate that list or diverge from it."""
    value = raw("DOXA_ENGINE").strip().lower()
    return value or "claude"


#: The session rail has not been decided either way -- see
#: :func:`sidebar_mode`.
SIDEBAR_AUTO = ""
SIDEBAR_ON = "on"
SIDEBAR_OFF = "off"


def sidebar_mode() -> str:
    """``""`` (auto), ``"on"`` or ``"off"`` for the session rail.

    THREE states, and the third one is the feature. Auto is what a fresh
    install gets, and it means hide-at-zero: the rail appears once there
    is something for it to say -- a collection exists, or a second session
    is open -- and stays out of the way before that, because a rail
    listing one session under no heading is chrome that answers nothing.
    The same discipline :data:`doxa.ui.labels.CTX_ABSOLUTE_MIN_COLS`,
    :data:`doxa.diff.SIDE_BY_SIDE_MIN_COLS` and the group tab strips
    already follow.

    ``F3`` writes ``"1"`` or ``"0"``, never the empty string, so the
    first deliberate toggle takes the decision away from the heuristic
    for good -- a user who closed the rail must not have it reappear
    because they opened a second tab.

    Reachable as three states only because the row is declared
    ``bool_on``: :func:`_coerce` stores that kind as the STRING "0"/"1"
    rather than a Python bool, and :func:`raw` collapses a bool ``False``
    to ``""`` -- which would make "explicitly off" and "never touched"
    the same answer. See the note on that function."""
    value = raw("DOXA_SIDEBAR").strip().lower()
    if not value:
        return SIDEBAR_AUTO
    return SIDEBAR_OFF if value in ("0", "false", "no", "off") else SIDEBAR_ON


#: The config table that overrides a project's assigned rail colour --
#: ``[projects]`` in ``~/.doxa/config.toml``, keyed by ``repo_root``::
#:
#:     [projects]
#:     "/home/me/src/doxa" = "teal"
#:
#: A TABLE and not a flat ``DOXA_*`` knob, because there is one entry per
#: repo and a settings-modal row cannot hold a map. It is therefore not in
#: :data:`SETTINGS` and has no env override: env beats file for a single
#: value, and "which colour is repo X" is not a value a shell exports.
#: The file stays the trusted non-repo-local source, which is the rule
#: docs/plugin-api.md already states for everything DOXA reads that a
#: checked-out repo must not be able to set.
PROJECTS_KEY = "projects"


def project_colour(repo_root: Any) -> "str | None":
    """The palette NAME configured for one project, or ``None``.

    A NAME, never a hex -- :func:`doxa.triage.colour_for` refuses
    anything that is not in :data:`doxa.triage.PALETTE`, so a hand-typed
    ``#3a3a3a`` (unreadable on half the terminals in the world) falls
    back to the assigned colour rather than being honoured. This function
    does not validate; it reads. Never raises, the posture every reader
    in this module takes: a broken config costs the user a colour, never
    a session."""
    root = str(repo_root or "").strip()
    if not root:
        return None
    table = load().get(PROJECTS_KEY)
    if not isinstance(table, dict):
        return None
    value = table.get(root)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def sidebar_width() -> int:
    """The rail's width in columns, clamped to what a rail can be. A
    garbage value in a hand-edited config falls back to the default
    rather than crashing the window -- :func:`linger_secs`' own posture,
    one knob over."""
    from .layout import SIDEBAR_WIDTH, clamp_sidebar_width

    value = raw("DOXA_SIDEBAR_WIDTH").strip()
    return clamp_sidebar_width(value) if value else SIDEBAR_WIDTH


# -- writing ---------------------------------------------------------------


_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")

# TOML basic strings escape these two-character sequences; every OTHER
# control character (0x00-0x1F, 0x7F) that has no short form falls
# through to \\uXXXX below. Escaping only backslash and quote -- the
# previous behaviour -- writes a LITERAL newline or tab into the file: the
# next tomllib.load raises on it, turning one settings-modal save into a
# config the reader can no longer parse.
_STRING_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}


def _toml_string(text: str) -> str:
    """``text`` as a quoted, escaped TOML basic string."""
    out: list[str] = []
    for ch in text:
        escape = _STRING_ESCAPES.get(ch)
        if escape is not None:
            out.append(escape)
        elif ch < " " or ch == "\x7f":  # other control chars: no short form
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _toml_key(key: str) -> str:
    """A TOML key, bare when that is legal and quoted otherwise.

    ``[projects]`` is keyed by filesystem paths, which contain ``/`` and
    start with it -- never a bare key -- so those always come back quoted.
    """
    if key and _BARE_KEY.fullmatch(key):
        return key
    return _toml_string(key)


def _toml_value(value: Any) -> str:
    """``value`` as a TOML literal: a scalar, an array, or an inline table
    (``{ k = v, ... }`` -- TOML's syntax for a table that is someone
    else's VALUE rather than its own ``[section]``; see
    :func:`_write_stored` for the top-level case). Recurses through
    lists and dicts, so an array may hold tables and a table entry may
    hold a nested table -- e.g. the ``customer`` extension a
    ``[projects]`` entry can carry alongside ``colour``.

    Raises :class:`ValueError` for a shape DOXA does not store today (a
    TOML date/time, decoded by ``tomllib`` into ``datetime.date`` et al.,
    is the one real gap). The previous version of this function had no
    such shape it refused: its ``str(value)`` fallback silently wrote
    *any* value as a quoted string, which is exactly how a ``[projects]``
    table came back unparseable after one unrelated save. Refusing
    loudly here is the same trade :func:`save` now makes for a malformed
    FILE -- lose the write, never the data.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        body = ", ".join(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items())
        return "{ " + body + " }"
    raise ValueError(
        f"config: cannot write a {type(value).__name__} value ({value!r}) "
        "to config.toml -- unsupported TOML shape"
    )


def _coerce(setting: Setting, value: str) -> "Any | None":
    """String from the modal -> the value stored in TOML. None means "drop
    this key" (an emptied field returns the knob to its default), which is
    how the modal expresses "unset" without a third state."""
    value = (value or "").strip()
    if not value:
        return None
    if setting.kind == "number":
        try:
            number = float(value)
        except ValueError:
            return None
        return int(number) if number.is_integer() else number
    if setting.kind in ("bool", "bool_on"):
        truthy = value.lower() not in ("0", "false", "no", "off")
        if setting.kind == "bool_on":
            # Stored as the STRING "0"/"1", never a Python bool. raw()
            # collapses an actual bool False to "" (so a bool row's
            # "unset" and "explicitly off" read the same) -- harmless for
            # every OTHER bool knob, whose default is off already, but it
            # would make an explicit "off" on a DEFAULT-ON knob (clock_show
            # is the one so far) indistinguishable from never having
            # touched it. A string survives raw() as itself.
            return "1" if truthy else "0"
        return truthy
    if setting.kind == "strftime":
        # Reject at save time what the render path would otherwise have
        # to fall back from silently -- doxa.clock.render carries the
        # same try/except as a second line of defense, for a value that
        # becomes invalid AFTER being saved (a hand-edited file, an env
        # var on a different platform's libc).
        import datetime as _dt

        try:
            text = _dt.datetime.now().strftime(value)
        except (ValueError, TypeError):
            return None
        if not text.strip():
            return None
        return value
    options = setting.options()
    if options and value not in options:
        return None
    return value


@contextmanager
def _write_lock():
    """Serialize the read-modify-replace sequence for ``config.toml``.

    Atomic replacement only protects readers from a partial file.  Two
    settings windows in separate processes could still both read the same
    old file and each atomically replace it, losing whichever change landed
    first.  The adjacent lock file survives replacement of config.toml, so
    the lock covers both the seed read and the final rename.
    """
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _write_stored(stored: dict[str, Any]) -> Path:
    """The shared tail of every writer: render ``stored`` as TOML and
    replace the file atomically, clamped to 0600 -- it is user
    configuration, not something a shared machine reads.

    A top-level value that is itself a table (``[projects]`` is the one
    DOXA writes) gets its own ``[section]`` block, emitted AFTER the flat
    keys, rather than being squeezed onto a ``key = value`` line the way
    every scalar and array is -- that squeeze is what used to turn a
    dict into ``projects = "{...}"``, a string the next load could not
    read back as a table at all."""
    lines = [
        "# DOXA settings. Precedence: environment > this file > default.",
        "# Written by the settings modal (Ctrl+, or /settings); safe to edit.",
        "",
    ]
    for setting in SETTINGS:
        if setting.key and setting.key in stored:
            lines.append(f"{setting.key} = {_toml_value(stored[setting.key])}")
    # Keys DOXA no longer knows about are preserved verbatim rather than
    # dropped: a config written by a newer version must survive an older
    # one. Split flat values from table values now so every scalar/array
    # line lands before any [section] block, matching the layout a human
    # would hand-write.
    unknown = sorted(k for k in stored if k not in SETTINGS_BY_KEY)
    table_keys = [k for k in unknown if isinstance(stored[k], dict)]
    for key in unknown:
        if key not in table_keys:
            lines.append(f"{_toml_key(key)} = {_toml_value(stored[key])}")
    for key in table_keys:
        table = stored[key]
        lines.append("")
        lines.append(f"[{_toml_key(key)}]")
        for subkey in sorted(table):
            lines.append(f"{_toml_key(subkey)} = {_toml_value(table[subkey])}")
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)  # DOXA's state home is the user's alone
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    invalidate()
    return path


class ConfigSaveRefused(RuntimeError):
    """A writer raised this instead of writing: ``config.toml`` exists but
    does not parse (or could not be read), and seeding the write from
    :func:`load`'s tolerant ``{}`` would silently delete every setting the
    file held, not just the ones this call is changing. ``load()`` keeps
    its "a broken config costs the user's customizations, never their
    session" contract for READERS; a writer cannot make that same trade,
    because a save that starts from nothing and replaces the file has
    every other setting left to lose. Fix or remove the file, then save
    again."""


def _seed_for_write() -> dict[str, Any]:
    """What :func:`save` and :func:`save_lore_root` copy their changes
    onto: the current file's contents, or ``{}`` when there is genuinely
    no file yet -- that case is fine, it is what a first save always
    sees. Raises :class:`ConfigSaveRefused` when the file EXISTS but a
    read or a parse failed, instead of returning ``{}`` and letting the
    caller silently full-replace it; see that class's docstring for why
    that distinction is the whole fix."""
    path = config_path()
    try:
        exists = path.exists()
    except OSError:
        exists = False
    if not exists:
        return {}
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except OSError as exc:
        raise ConfigSaveRefused(
            f"{path} exists but could not be read ({exc}) -- fix its "
            "permissions or remove the file, then save again."
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigSaveRefused(
            f"{path} exists but does not parse as TOML ({exc}) -- fix or "
            "remove the file, then save again."
        ) from exc
    return data if isinstance(data, dict) else {}


def save(values: dict[str, str], *, model_engine: str = "claude") -> Path:
    """Write the settings file from ``{key: string}`` (the modal's fields).

    Keys absent from ``values`` keep whatever the file already had; keys
    present but empty are REMOVED, which is what returns a knob to its
    default. Read-only rows are skipped even if present in ``values`` --
    the modal must never be the thing that writes a row it renders with no
    field (see :func:`save_lore_root` for the one row that DOES get
    written outside the modal). ``model_engine`` routes only the model
    field: Claude keeps the legacy top-level key; other engines write into
    ``[models]`` without touching Claude's preference.

    Raises :class:`ConfigSaveRefused` when the existing file is present
    but unreadable or malformed -- a missing file is NOT that case, and
    seeds an empty, fresh save exactly as before.
    """
    with _write_lock():
        stored = _seed_for_write()
        for setting in SETTINGS:
            if not setting.key or setting.read_only or setting.key not in values:
                continue
            coerced = _coerce(setting, values[setting.key])
            if setting.key == "model" and model_engine != "claude":
                models = stored.get("models")
                models = dict(models) if isinstance(models, dict) else {}
                if coerced is None:
                    models.pop(model_engine, None)
                else:
                    models[model_engine] = coerced
                if models:
                    stored["models"] = models
                else:
                    stored.pop("models", None)
                continue
            if coerced is None:
                stored.pop(setting.key, None)
            else:
                stored[setting.key] = coerced
        return _write_stored(stored)


def save_model(engine_id: str, value: str) -> Path:
    """Persist a successful ``/model`` switch for its active engine."""
    return save({"model": value}, model_engine=engine_id)


def save_lore_root(path: str) -> Path:
    """The one write ``/setup`` makes directly: the sticky LORE store
    choice (``doxa.setup``'s ladder). Deliberately bypasses :func:`save`'s
    read-only gate on the ``lore_root`` row -- that gate exists to keep
    this row OUT of the settings modal's editable fields (it is /setup's
    to decide, once, not a field to fat-finger), not to make it
    unwritable altogether.

    Shares :func:`save`'s refusal on a present-but-broken file (see
    :class:`ConfigSaveRefused`) -- this writer amplifies a malformed file
    into total data loss exactly the same way :func:`save` used to."""
    with _write_lock():
        stored = _seed_for_write()
        stored["lore_root"] = path
        return _write_stored(stored)
