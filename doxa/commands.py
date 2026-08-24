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

from dataclasses import dataclass


# Functional groups, in the order every surface shows them. Groups that
# have no rows yet are still listed here: it is where the commands that
# belong to them will land, and declaring the slot now is what keeps a
# later addition from inventing a sixth ordering. An empty group renders
# nowhere -- a header with nothing under it is a placeholder row, and this
# house does not ship those.
GROUPS: tuple[str, ...] = (
    "Session",
    "Memory",
    "Panes & tabs",
    "Tools & config",
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
        summary="Render an image file here (terminal image-support probe)",
        usage="/img <path>",
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
        name="/pending",
        group="Memory",
        # READ-ONLY, deliberately, and the summary says so where a user
        # reads it. Approving or rejecting a staged proposal stays with
        # LORE's own `/lore:approve` / `/lore:reject`: the write path into
        # curated memory is under security review (docs/plugin-api.md §6)
        # and must not gain a second door here.
        summary="Staged memory proposals from the background reviewer (read-only)",
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


def ordered() -> list[SlashCommand]:
    """The registry in DISPLAY order: group order (:data:`GROUPS`), then
    alphabetical by name inside each group. The one sequence every surface
    iterates -- palette, autocomplete dropdown, generated /help."""
    index = {group: position for position, group in enumerate(GROUPS)}
    return sorted(
        REGISTRY, key=lambda cmd: (index.get(cmd.group, len(GROUPS)), cmd.name)
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
