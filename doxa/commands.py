# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.commands -- the slash-command registry: ONE list, several surfaces.

DOXA's slash commands are data here, exactly like ``doxa.operators``' tool
definitions are data there, and for the same reason: every surface that
shows a command (the prompt input's autocomplete, the Ctrl+P palette,
``/help``) reads this tuple, so no surface can drift from another and
adding a command is one row rather than four edits.

What a row is NOT: a handler. Handlers live on the pane that owns the
widgets they touch (``SessionPane._command_handlers``), and a closure test
asserts that the set of interactive rows here and the set of handlers there
are the same set -- the same registry-closure discipline
``tests/test_operators.py`` applies to the tool registry.

Rows also carry their functional ``group``. Ordering is a property of the
registry, not of any surface: :func:`ordered` (group order, then
alphabetical inside a group) is what the palette, the prompt's
autocomplete and ``/help`` all iterate. Three surfaces with three sort
orders is three chances to be inconsistent; there is one.

``passthrough`` rows are real commands that DOXA deliberately does NOT
intercept: ``/compact`` is the CLI's own prompt-text convention (PHASE0
§2/§6 -- sending the literal string is what triggers compaction and fires
the PreCompact hook the deriver hangs off). Listing it here means
autocomplete and ``/help`` can show it honestly; intercepting it would
break the very mechanism it names.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass


# Functional groups, in the order every surface shows them. Groups that
# have no rows yet are still listed here: it is where the commands that
# belong to them will land, and declaring the slot now is what keeps a
# later addition from inventing a sixth ordering. An empty group renders
# nowhere -- a header with nothing under it is a placeholder row, and this
# house does not ship those.
#
# "Plugins" (below /reload-plugins's own group, since that IS what governs
# it) holds rows :func:`ordered` folds in dynamically -- see
# :func:`_plugin_rows` -- so it is empty, and therefore invisible, on
# every install with adoption off or nothing adopted, exactly like every
# other empty group here.
GROUPS: tuple[str, ...] = (
    "Session",
    "Memory",
    "Panes & tabs",
    "Tools & config",
    "Plugins",
    "Maintenance",
)


@dataclass(frozen=True)
class SlashCommand:
    """One command, as every surface sees it."""

    name: str
    """The literal token typed at the start of the prompt, e.g. ``/peers``."""

    summary: str
    """One line. Shown in the palette's help column, the autocomplete
    dropdown and ``/help``."""

    usage: str = ""
    """Full call form when the command takes arguments (``/msg <session>
    <text>``). Empty means the name is the whole usage."""

    palette: str = ""
    """Display name on the Ctrl+P palette. Empty means the command is not
    offered there (debug/verbose commands stay in the prompt)."""

    palette_prefill: bool = False
    """Palette entry PREFILLS the prompt instead of running the command --
    for commands whose arguments the user has to supply."""

    passthrough: bool = False
    """Listed, never intercepted: the text goes to the model/CLI verbatim."""

    binding: str = ""
    """Key binding that reaches the same place, if one exists, spelled the
    way TEXTUAL spells it ("ctrl+comma", not "ctrl+,") -- /help renders the
    pretty form, and matching the app's BINDINGS verbatim is what lets
    /help tell a bound command apart from a bare hotkey."""

    group: str = "Session"
    """Functional group -- one of :data:`GROUPS`. Ordering lives HERE, on
    the definition, so every surface derives the same sequence from it."""

    def call_form(self) -> str:
        return self.usage or self.name


REGISTRY: tuple[SlashCommand, ...] = (
    SlashCommand(
        name="/peers",
        group="Panes & tabs",
        summary="Live sessions in this project right now",
        palette="Peers: list",
    ),
    SlashCommand(
        name="/split",
        group="Panes & tabs",
        summary="Split this pane — a second session STACKED BELOW it "
                "(Alt+S)",
        palette="Split: stacked",
    ),
    SlashCommand(
        name="/vsplit",
        group="Panes & tabs",
        summary="Split this pane — a second session SIDE BY SIDE with it "
                "(Alt+D)",
        palette="Split: side by side",
    ),
    SlashCommand(
        name="/diff",
        group="Panes & tabs",
        summary="Live diff of this session's worktree, SIDE BY SIDE with "
                "it — reject a hunk to revert it and tell the agent "
                "(Alt+G)",
        palette="Diff: live, beside this session",
    ),
    SlashCommand(
        name="/msg",
        group="Panes & tabs",
        summary="Send a message to one same-project peer session",
        usage="/msg <session_prefix> <text>",
        palette="Peers: message",
        palette_prefill=True,
    ),
    SlashCommand(
        name="/img",
        group="Tools & config",
        summary="What this terminal can draw, measured and shown in every "
                "tier; with a path, render that file",
        usage="/img [path]",
        palette="Image support",
    ),
    SlashCommand(
        name="/login",
        group="Tools & config",
        summary="Sign in through a provider's own auth CLI (default: claude)",
        usage="/login [provider]",
        palette="Auth: login",
    ),
    SlashCommand(
        name="/logout",
        group="Tools & config",
        summary="Sign out through a provider's own auth CLI (default: claude)",
        usage="/logout [provider]",
        palette="Auth: logout",
    ),
    SlashCommand(
        name="/settings",
        group="Tools & config",
        summary="Open the settings modal (env > config file > default)",
        palette="Settings",
        binding="ctrl+comma",
    ),
    SlashCommand(
        name="/setup",
        group="Tools & config",
        summary="Check state, fix findings one at a time (auto-runs once, first launch)",
        palette="Setup",
    ),
    SlashCommand(
        name="/doctor",
        group="Tools & config",
        summary="Read-only health checks: pass/fail and the fix command for each",
        palette="Doctor",
    ),
    SlashCommand(
        name="/plugins",
        group="Tools & config",
        # docs/plans/plugins.md. Deliberately not "/plugin" (singular) --
        # the CLI's own `claude plugin|plugins` subcommand already owns
        # that name, and this is a different thing: what DOXA discovered
        # in the OPERATOR'S OWN ~/.claude, not a package manager.
        summary="Your Claude Code plugins/skills: discovered, adopted or refused, and why",
        palette="Plugins: list",
    ),
    SlashCommand(
        name="/reload-plugins",
        group="Tools & config",
        summary="Re-scan Claude Code plugins/skills now (new sessions/tabs only)",
        palette="Plugins: reload",
    ),
    SlashCommand(
        name="/model",
        group="Session",
        summary="Switch the model for the rest of this session (no reconnect)",
        usage="/model [name]",
        palette="Model: switch",
    ),
    SlashCommand(
        name="/branch",
        group="Session",
        summary="List local branches (current base marked), or switch this session's base",
        usage="/branch [name]",
        palette="Branch: switch",
    ),
    SlashCommand(
        name="/mode",
        group="Session",
        # The keycap named here is Shift+Tab, not the Ctrl+Tab the request
        # asked for: doxa.keyboard.unreachable_under_legacy("ctrl+tab") is
        # True, so a legacy-encoding terminal cannot send it at all. BOTH
        # are bound (see DoxaApp.BINDINGS) and /help marks whichever this
        # terminal was measured unable to send; this row names the one that
        # is deliverable everywhere.
        binding="shift+tab",
        summary="Permission mode: what still stops and asks you before a tool runs",
        # `/mode [name]`, not the six spelled out. /help pads its whole
        # command column to the widest call form, and enumerating them
        # here made that column 63 characters -- every other row in the
        # file indented past the point of being scannable to describe one
        # command. Bare `/mode` lists all six with what each one does,
        # which is the surface that has room for them.
        usage="/mode [name]",
        palette="Permission mode",
    ),
    SlashCommand(
        name="/effort",
        group="Session",
        summary="Effort level for NEW sessions (the SDK sets it at connect only)",
        usage="/effort [low|medium|high|xhigh|max]",
    ),
    SlashCommand(
        name="/usage",
        group="Session",
        summary="Session tokens, turns, cost, and subscription headroom",
        palette="Usage",
    ),
    SlashCommand(
        name="/context",
        group="Session",
        # The sibling of /usage, and deliberately a different question:
        # /usage is what this session has SPENT (cumulative tokens, turns,
        # cost), /context is what it is CARRYING right now. Every number
        # behind this row is the claude CLI's own measurement of its own
        # window -- see labels.context_breakdown_text on why nothing here
        # is ever estimated. (The status bar's own ctx chip, item X, is the
        # same measurement at a glance: labels.ctx_text builds its words.)
        summary="What is occupying the context window right now, by component",
        palette="Context breakdown",
    ),
    SlashCommand(
        name="/clear",
        group="Session",
        summary="Fresh session in THIS tab: finalize, rotate transcript, reset",
        palette="Clear session",
    ),
    SlashCommand(
        name="/detach",
        group="Panes & tabs",
        summary="Close this tab but LEAVE its session running (reattach later)",
        palette="Detach tab",
    ),
    SlashCommand(
        name="/attach",
        group="Panes & tabs",
        # "in a new tab" is in the one-liner for the same reason /resume's
        # is: attach does not take over the pane it was typed in.
        summary="Reattach a live detached session in a new tab (bare: pick one)",
        usage="/attach [session prefix]",
        palette="Attach a detached session",
        # NOT prefilled: bare /attach already offers a picker when there is
        # more than one candidate, same reasoning as bare /resume.
    ),
    SlashCommand(
        name="/sessions",
        group="Session",
        summary="Every live session: name, age, attached — and how to kill one",
        usage="/sessions [kill <prefix> | kill-detached]",
        palette="Sessions: list",
    ),
    SlashCommand(
        name="/rename",
        group="Panes & tabs",
        summary="Name this tab (pins the label); empty restores the automatic one",
        usage="/rename [name]",
        palette="Rename tab",
        palette_prefill=True,
    ),
    SlashCommand(
        name="/dir",
        group="Session",
        summary="This session's own working directory — where its tool calls actually run",
        palette="Dir: show",
    ),
    SlashCommand(
        name="/cd",
        group="Panes & tabs",
        # NOT "changes this session's directory" -- it can't (see the
        # handler's own docstring): a running CLI subprocess keeps the
        # cwd it was spawned with. This opens the target in a NEW tab,
        # same as /resume and the repo-name chip's own directory picker,
        # and says so plainly rather than pretending to move THIS one.
        summary="Open a directory in a NEW tab (cannot move this running session)",
        usage="/cd <path>",
        palette="Cd: open a directory",
        palette_prefill=True,
    ),
    SlashCommand(
        name="/beliefs",
        group="Memory",
        # v0.69.0 retired item V's standalone browser tab: the chip
        # picker now carries everything it did -- confirmed/contradicted/
        # stale/retract inline on each row, and Right on a highlighted
        # row expands its evidence trail in place (Left folds it away).
        # This command opens that SAME picker, not a second surface.
        summary="Browse active beliefs — confirmed/contradicted/stale/retract inline",
        palette="Beliefs: browse",
    ),
    SlashCommand(
        name="/pending",
        group="Memory",
        # Approve and reject are inline on each row too (v0.67.0,
        # ``a``/``r``), or one selection away in the row's own action
        # sub-menu -- reviewing no longer hands off to a second surface.
        summary="Staged proposals — approve or reject inline, right here",
        palette="Pending proposals",
    ),
    SlashCommand(
        name="/search",
        group="Memory",
        summary="Search every past session (live results as you type)",
        usage="/search <terms>",
        palette="Search past sessions",
        palette_prefill=True,
        binding="ctrl+r",
    ),
    SlashCommand(
        name="/resume",
        group="Session",
        # "in a new tab" is in the one-liner because it is the surprising
        # half: resume does not take over the pane it was typed in, and a
        # user who expects it to would otherwise find out by watching a
        # tab they did not ask for appear.
        summary="Reopen a past conversation in a new tab, its history and all",
        usage="/resume [session-id]",
        palette="Resume a conversation",
        # NOT prefilled from the palette: bare /resume already offers the
        # recent conversations to pick from, so prefilling would leave the
        # user staring at a prompt asking for a uuid they do not have.
    ),
    SlashCommand(
        name="/compact",
        group="Maintenance",
        summary="Ask the CLI to compact the transcript (runs LORE's review first)",
        passthrough=True,
    ),
    SlashCommand(
        name="/update",
        group="Maintenance",
        summary="Fast-forward this DOXA checkout from origin (never merges)",
        usage="/update [--restart]",
        palette="Update DOXA",
    ),
    SlashCommand(
        name="/help",
        group="Maintenance",
        summary="Every command and key binding, generated from this registry",
        palette="Help",
    ),
    SlashCommand(
        name="/about",
        group="Maintenance",
        summary="Version, dependencies, platform and config path — what a bug report needs",
        palette="About DOXA",
    ),
)


def _plugin_rows() -> "list[SlashCommand]":
    """Adopted Claude Code plugin commands (``doxa.claude_plugins``),
    folded into the SAME registry every surface below reads.

    THE REPORTED DEFECT, closed here: v0.74.0 adopted plugin commands all
    the way to the underlying ``claude`` CLI (one ``--plugin-dir`` per
    plugin) -- and a plugin's own command genuinely runs when typed, e.g.
    ``/caveman:caveman ultra`` (measured against a real adopted plugin,
    isolated CLI, this exact stream-json path) -- but neither the prompt's
    autocomplete dropdown nor the Ctrl+P palette had ever HEARD of one,
    because both read only :data:`REGISTRY` and this module never learned
    plugins existed. A command that reaches the CLI but not DOXA's own "/"
    surfaces exists on one surface and not the other, which is exactly
    what docs/plans/plugin-api.md's "no second ordering" design was meant
    to rule out -- this row is the fold-in that design already implies,
    for the plugin system that document does not itself cover.

    Every row is PASSTHROUGH: DOXA never runs one of these (there is no
    pane handler, and there must not be -- the underlying CLI is what
    expands a plugin command, the exact mechanism ``/compact`` already
    rides, see ``doxa.session.pane.on_prompt_submitted``), so
    :func:`interactive`/:func:`interactive_names` -- and therefore the
    pane's own handler dict and its closure test -- read :data:`REGISTRY`
    directly and never see these at all. ``palette_prefill=True`` for the
    same reason: the palette's OTHER path (``DoxaApp._cmd_run_slash``)
    calls a pane handler these rows do not have; prefilling and letting
    the user press Enter routes through the passthrough path instead,
    identically to how the dropdown's own ``PromptInput.complete()``
    already treats a ``usage``-bearing row.

    Computed fresh on every call rather than cached -- discovery measured
    under 1ms/call on a real 5-plugin install, and a cache here would go
    stale exactly when ``/reload-plugins`` changes what is adopted. Never
    raises: a broken plugin scan must cost this one row, not every other
    command surface in the app."""
    try:
        from . import claude_plugins as claude_plugins_mod
    except Exception:  # noqa: BLE001 -- see docstring
        return []
    try:
        adopted = claude_plugins_mod.adopted_commands()
    except Exception:  # noqa: BLE001 -- discovery reads files DOXA does
        # not own; a malformed one must not cost the whole registry.
        return []
    rows = []
    for plugin, command in adopted:
        name = f"/{command.invocable}"
        usage = f"{name} {command.argument_hint}" if command.argument_hint else ""
        rows.append(SlashCommand(
            name=name,
            group="Plugins",
            summary=command.summary or f"{plugin.plugin} plugin command",
            usage=usage,
            palette=f"Plugin: {name}",
            palette_prefill=True,
            passthrough=True,
        ))
    return rows


def ordered() -> list[SlashCommand]:
    """The registry in DISPLAY order: group order (:data:`GROUPS`), then
    alphabetical by name inside each group -- :data:`REGISTRY`'s built-in
    rows plus whatever :func:`_plugin_rows` currently contributes. The one
    sequence every surface iterates -- palette, autocomplete dropdown,
    generated ``/help``: a plugin command folded in here needs no second
    wiring anywhere else, which is the whole point of there being one
    registry rather than three."""
    index = {group: position for position, group in enumerate(GROUPS)}
    all_rows: "tuple[SlashCommand, ...]" = REGISTRY + tuple(_plugin_rows())
    return sorted(
        all_rows, key=lambda cmd: (index.get(cmd.group, len(GROUPS)), cmd.name)
    )


def grouped() -> list[tuple[str, list[SlashCommand]]]:
    """``[(group, [commands])]`` in display order, EMPTY GROUPS OMITTED --
    a header with nothing under it is a placeholder row."""
    buckets: dict[str, list[SlashCommand]] = {}
    for cmd in ordered():
        buckets.setdefault(cmd.group, []).append(cmd)
    return [(group, buckets[group]) for group in GROUPS if buckets.get(group)]


def names() -> list[str]:
    """Registry names in DISPLAY order (see :func:`ordered`)."""
    return [cmd.name for cmd in ordered()]


def interactive() -> tuple[SlashCommand, ...]:
    """Rows DOXA handles itself -- the ones that need a handler."""
    return tuple(cmd for cmd in REGISTRY if not cmd.passthrough)


def interactive_names() -> list[str]:
    return [cmd.name for cmd in interactive()]


def find(name: str) -> "SlashCommand | None":
    for cmd in REGISTRY:
        if cmd.name == name:
            return cmd
    return None


def lookup(text: str) -> "SlashCommand | None":
    """The registry row for a prompt line, by its FIRST token -- the one
    place "is this a doxa command?" is decided. Anything else starting with
    "/" stays a prompt and reaches the model untouched."""
    token = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    return find(token)


def matches(fragment: str) -> list[SlashCommand]:
    """Registry rows whose name starts with ``fragment`` (which includes
    the leading "/"), in display order."""
    fragment = fragment.strip().lower()
    if not fragment.startswith("/"):
        return []
    return [cmd for cmd in ordered() if cmd.name.startswith(fragment)]


# -- THE GUARD (reported: "/lore:pending does nothing, no error") ---------
#
# ``lookup()`` above is deliberately permissive -- "anything else starting
# with '/' stays a prompt and reaches the model untouched" -- which is
# correct for ``/compact`` and every adopted plugin row (:func:`_plugin_rows`,
# neither of which ``lookup``/``find`` ever sees, by design: they have no
# DOXA handler and must reach the CLI verbatim). It is NOT correct for a
# line that looks like a slash command but answers to NOBODY: not a
# REGISTRY row, not an adopted plugin invocable, and -- silently, because
# the isolated CLI (``doxa.cli_isolation``) never receives a copy of the
# operator's own ``~/.claude/commands`` and the SDK's headless transport
# still runs the CLI's own local-command interception -- often not
# anything the CLI recognizes either. That line reaches ``_run_turn``
# today, the CLI's local-command parser finds nothing staged for it, and
# the whole turn answers with total silence: no DOXA error, no CLI text.
#
# ``doxa.session.pane.on_prompt_submitted`` calls :func:`unreachable_message`
# for exactly the lines that fall through both ``lookup()`` and the
# adopted-plugin fold-in, and mounts whatever it returns as a
# ``SystemBlock`` instead of starting that doomed turn.

_BLOCKED_PLUGIN_EQUIVALENTS: "dict[str, str]" = {
    # plugin name (the first element of a claude_plugins.BLOCKLIST tuple)
    # -> the DOXA-native surface that already does the same job. Deliberately
    # NOT derived from BLOCKLIST itself -- knowing a plugin is blocked says
    # nothing about what replaces it, that mapping is domain knowledge that
    # has to be written down somewhere, and here is the one place a reader
    # who wants to know "blocked commands, and their equivalents" looks.
    # A plugin blocklisted with no entry here still gets the blocked
    # message, just without the "use X instead" pointer.
    "lore": "/beliefs and /pending",
}

_SUGGEST_CUTOFF = 0.72
"""``difflib.get_close_matches``'s cutoff for the "did you mean" hint below
-- high enough that "/lore:pending" (genuinely unrelated to any REGISTRY
name) suggests nothing, and "/setings"/"/pendign" (one dropped/swapped
letter from a real name) still match. Picked by measuring both directions
against this file's own REGISTRY, not guessed."""


def _blocked_plugin_names() -> "set[str]":
    """The plugin half of every :data:`claude_plugins.BLOCKLIST` entry --
    read from there, not re-typed here, so this guard can never name a
    plugin blocked that the blocklist itself does not."""
    try:
        from . import claude_plugins as claude_plugins_mod
    except Exception:  # noqa: BLE001 -- see _plugin_rows' docstring: a
        # broken import must cost this one check, not every "/" line typed.
        return set()
    return {plugin for plugin, _marketplace in claude_plugins_mod.BLOCKLIST}


def _blocked_plugin_message(name: str, plugin: str) -> str:
    try:
        from . import claude_plugins as claude_plugins_mod
        reason = claude_plugins_mod.BLOCKLIST_REASON.split(" -- ", 1)[0]
    except Exception:  # noqa: BLE001
        reason = "it duplicates a capability DOXA already runs natively"
    equivalent = _BLOCKED_PLUGIN_EQUIVALENTS.get(plugin)
    pointer = (
        f"; use DOXA's own {equivalent} instead" if equivalent
        else "; see /plugins for why"
    )
    return (
        f"unknown command: {name} -- the {plugin} plugin is blocked by "
        f"design ({reason}){pointer}"
    )


def _unknown_message(name: str) -> str:
    suggestion = difflib.get_close_matches(name, names(), n=1, cutoff=_SUGGEST_CUTOFF)
    if suggestion:
        return (
            f"unknown command: {name} -- did you mean {suggestion[0]}? not "
            "in DOXA's own commands or any adopted plugin, so nothing was "
            "sent to the CLI"
        )
    return (
        f"unknown command: {name} -- not in DOXA's own commands or any "
        "adopted plugin, so nothing was sent to the CLI; if this is a live "
        "Claude Code CLI or plugin command DOXA does not know about yet, "
        "it will not run from here either"
    )


def is_reachable(name: str) -> bool:
    """Whether ``name`` (a prompt's bare first token, leading "/" and all)
    is something THIS session can actually run right now: a REGISTRY row
    (interactive or passthrough) or a currently-adopted plugin command --
    the exact membership :func:`names` already computes for the
    autocomplete dropdown and the palette, reused here so "can this run"
    never drifts from "does the dropdown offer it"."""
    return name in names()


def unreachable_message(name: str) -> "str | None":
    """``None`` when :func:`is_reachable` -- the caller's cue to do nothing
    and let the line reach the CLI as it always has. Otherwise the text a
    caller mounts instead of starting a turn that would answer with
    silence, in three shapes:

    1. The token before ":" names a :data:`claude_plugins.BLOCKLIST` entry
       -- blocked BY DESIGN, not missing by accident, so say so, why in one
       clause (the blocklist's own reason), and point at the DOXA-native
       equivalent when :data:`_BLOCKED_PLUGIN_EQUIVALENTS` has one.
    2. A near miss of a REGISTRY/adopted-plugin name -- one suggestion,
       via :data:`_SUGGEST_CUTOFF`.
    3. Neither -- DOXA says exactly what it knows (not one of its own
       commands, not an adopted plugin) and nothing it does not: this may
       still be a genuine CLI-native command or a plugin DOXA has not
       adopted, and the message says that plainly rather than claiming
       certainty a client-side check cannot have."""
    if is_reachable(name):
        return None
    plugin = name[1:].partition(":")[0] if name.startswith("/") else ""
    if plugin and plugin in _blocked_plugin_names():
        return _blocked_plugin_message(name, plugin)
    return _unknown_message(name)
