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

``passthrough`` rows are real commands that DOXA deliberately does NOT
intercept: ``/compact`` is the CLI's own prompt-text convention (PHASE0
§2/§6 -- sending the literal string is what triggers compaction and fires
the PreCompact hook the deriver hangs off). Listing it here means
autocomplete and ``/help`` can show it honestly; intercepting it would
break the very mechanism it names.
"""

from __future__ import annotations

from dataclasses import dataclass


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

    def call_form(self) -> str:
        return self.usage or self.name


REGISTRY: tuple[SlashCommand, ...] = (
    SlashCommand(
        name="/peers",
        summary="Live sessions in this project right now",
        palette="Peers: list",
    ),
    SlashCommand(
        name="/msg",
        summary="Send a message to one same-project peer session",
        usage="/msg <session_prefix> <text>",
        palette="Peers: message",
        palette_prefill=True,
    ),
    SlashCommand(
        name="/img",
        summary="Render an image file here (terminal image-support probe)",
        usage="/img <path>",
    ),
    SlashCommand(
        name="/login",
        summary="Sign in through a provider's own auth CLI (default: claude)",
        usage="/login [provider]",
        palette="Auth: login",
    ),
    SlashCommand(
        name="/logout",
        summary="Sign out through a provider's own auth CLI (default: claude)",
        usage="/logout [provider]",
        palette="Auth: logout",
    ),
    SlashCommand(
        name="/settings",
        summary="Open the settings modal (env > config file > default)",
        palette="Settings",
        binding="ctrl+comma",
    ),
    SlashCommand(
        name="/model",
        summary="Switch the model for the rest of this session (no reconnect)",
        usage="/model [name]",
        palette="Model: switch",
    ),
    SlashCommand(
        name="/effort",
        summary="Effort level for NEW sessions (the SDK sets it at connect only)",
        usage="/effort [low|medium|high|xhigh|max]",
    ),
    SlashCommand(
        name="/usage",
        summary="Session tokens, turns, cost, and subscription headroom",
        palette="Usage",
    ),
    SlashCommand(
        name="/clear",
        summary="Fresh session in THIS tab: finalize, rotate transcript, reset",
        palette="Clear session",
    ),
    SlashCommand(
        name="/compact",
        summary="Ask the CLI to compact the transcript (runs LORE's review first)",
        passthrough=True,
    ),
    SlashCommand(
        name="/help",
        summary="Every command and key binding, generated from this registry",
        palette="Help",
    ),
)


def names() -> list[str]:
    return [cmd.name for cmd in REGISTRY]


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
    the leading "/"), in registration order. The autocomplete overlay's
    coarse filter; the palette's own fuzzy matcher refines within it."""
    fragment = fragment.strip().lower()
    if not fragment.startswith("/"):
        return []
    return [cmd for cmd in REGISTRY if cmd.name.startswith(fragment)]
