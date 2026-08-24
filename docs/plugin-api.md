# DOXA plugin API — specification

Status: **draft for review**. Nothing here is implemented yet. This spec is
written before the `app.py` split so the split can land directly in its final
shape rather than being redone once extension points appear.

## Why this exists

`doxa/app.py` is 5,737 lines, 36% of the package. Every feature shipped in
v0.11–v0.28 landed in it, and the last two rebases both conflicted there. The
file is not merely large — it hardcodes the four things a plugin would most
obviously want to add:

| what a plugin would add | where it is hardcoded today |
|---|---|
| a slash command | `SessionPane._command_handlers`, a literal dict, kept in sync with `doxa/commands.py` by a test |
| a status-line chip | `SessionPane._refresh_status`, 157 lines of literal chip construction |
| a transcript block | `SessionPane._handle_event`, an `if/elif` chain over six event types |
| a model backend | `doxa/providers.py`, which already has the right shape |

The split and the plugin API are the same work: each extension point is the
seam the split follows.

## Non-goals

- **Not a sandbox.** Plugins run in-process with the user's full filesystem and
  credential access. This spec makes loading *explicit and auditable*; it does
  not make a hostile plugin safe. See "Trust" below.
- **Not a stable public API yet.** Until DOXA reaches 1.0 the contract may
  break between minor versions. `API_VERSION` exists so breakage is loud.
- **Not a replacement for `doxa/commands.py`.** That registry already works and
  is test-pinned against the pane. Plugins extend it; they do not supersede it.

## Trust and loading

Two sources, both requiring explicit user action:

1. **Installed distributions** exposing the `doxa.plugins` entry-point group.
2. **Local files** in `~/.doxa/plugins/*.py`.

Discovery is not activation. A discovered plugin is inert until its name
appears in the `plugins` allowlist in `config.toml`, surfaced in the settings
modal as a per-plugin toggle. `doxa --no-plugins` disables all of them for a
run, and that flag is the documented first step of any bug report.

**A plugin is never loaded from the working repository.** Not from
`.doxa/plugins/`, not from a repo-local config file, not from anything a
`git clone` can deliver. DOXA opens sessions in arbitrary repositories; a
repo-supplied plugin would be arbitrary code execution on `doxa new` against
an untrusted clone. This constraint is not negotiable for convenience.

Plugin load happens once at app start, before the first pane is composed, so a
plugin cannot observe a partially built UI.

## The plugin object

A plugin module defines a module-level `PLUGIN`:

```python
from doxa.plugin import Plugin, API_VERSION

PLUGIN = Plugin(
    name="jira",
    version="1.0.0",
    api_version=API_VERSION,
    summary="Jira issue lookup as a slash command and a status chip.",
)
```

`Plugin` is a frozen dataclass, matching the house style of
`commands.SlashCommand` and `config.Setting` — every field carries its own
docstring, and the object describes rather than executes.

Contributions are registered by decorating functions in the same module. A
plugin that registers nothing is a configuration error, reported at load, not
a silent no-op.

### Version compatibility

`API_VERSION` is a single integer, bumped on any breaking change to the
protocols below. A plugin whose `api_version` does not match is **not loaded**,
and the mismatch is reported in the settings modal with both numbers. Silently
loading an incompatible plugin to "see if it works" is how a TUI ends up with
an unreproducible crash three tabs deep.

## Extension points

### 1. Slash commands

Reuses the existing `SlashCommand` dataclass verbatim — palette entry, usage
line, `/help` row and autocomplete all come free, because every surface already
reads that registry.

```python
@PLUGIN.command(SlashCommand(
    name="/jira",
    summary="Look up a Jira issue by key.",
    usage="/jira <ISSUE-KEY>",
    palette="Jira lookup",
    palette_prefill=True,
))
async def jira(ctx: PaneContext, args: str) -> None:
    ...
```

The existing test that asserts `_command_handlers.keys() == commands.interactive_names()`
must be extended to account for plugin-contributed names rather than deleted.
That test is the reason the registry and the executor have never drifted.

### 2. Status-line chips

The prize, and the reason `_refresh_status` needs breaking up. A chip is a
protocol, not a widget — DOXA owns rendering, the plugin owns content:

```python
class Chip(Protocol):
    def text(self, ctx: PaneContext) -> str: ...
    def tooltip(self, ctx: PaneContext) -> str: ...
    async def on_click(self, ctx: PaneContext) -> None: ...
```

Ordering is explicit (`order: int`), because a status line whose contents shift
between sessions is worse than one that omits something.

`text()` is called on **every status refresh**. The cost discipline already
documented on `open_beliefs_picker` applies with full force: a chip that does
I/O in `text()` will do it several times a second. The loader wraps `text()`
with a timing guard and disables — loudly — any chip that exceeds its budget.
Slow work belongs in `on_click`.

### 3. Transcript blocks

`_handle_event`'s `if/elif` over `turn_started`, `text_delta`,
`reasoning_delta`, `tool_call`, `tool_result`, `turn_done` becomes a dispatch
map. Plugins register a renderer for an event type, returning a Textual widget
mounted into the turn block.

Plugins may **add** event types. They may not replace the renderer for a
built-in type: a plugin that can silently redraw `tool_result` can lie to the
user about what a tool did.

### 4. Engine providers

`doxa/providers.py` already defines `ModelProvider(Protocol)` and
`ClaudeProvider`. This extension point is that protocol, widened to cover the
session lifecycle (spawn, send, interrupt, event stream) and registered by
name. This is the interface multi-provider engines (vault addendum 6) needs
regardless of whether plugins ever ship.

### 5. Lifecycle hooks

`turn_started`, `turn_done`, `needs_input`, `session_boot`, `session_stop`.
Hooks observe; they cannot veto. A hook that raises is logged and its plugin
is disabled for the rest of the run — one bad hook must not wedge every turn.

`doxa/notify.py` becomes the first in-tree consumer of this surface, which is
the honest test of whether the interface is real: if the built-in notifier
cannot be expressed as a hook, the hook design is wrong.

### 6. LORE access — read-only, by design

DOXA holds `lore_core` in-process, and the belief snapshot is injected into the
model's context. **A plugin that can write beliefs can steer the model** — not
for one turn, but persistently and across sessions, in a surface the user reads
as trustworthy precisely because LORE's premise is that everything steering the
agent is human-approved or outcome-calibrated.

Plugins therefore get **read access only**: query beliefs, read the snapshot,
read curated memory. There is no plugin-facing write API — not gated, not
behind a capability flag, not present. A plugin wanting to contribute a belief
does what everything else does: it stages a proposal for the user to approve
through the existing `pending`/`approve` path. Staging is the whole mechanism;
a plugin write API would be a second door into the room that door guards.

The same principle applies to how DOXA itself is packaged as a Claude Code
plugin, and to Claude Code plugins generally — see LORE issue on gating the
belief/memory write path.

### 7. Settings

Plugins contribute `config.Setting` rows, namespaced `plugin.<name>.<key>`,
appearing in the settings modal exactly like built-in knobs. No new storage
format, no second config file.

## Failure policy

A plugin failure degrades **that plugin**, never the app:

- raises at import → not loaded, reported in settings, DOXA starts normally
- raises in a hook or `text()` → disabled for the run, one system message
- exceeds its `text()` time budget → disabled for the run, one system message

Every one of these states is visible in the settings modal. A silently
disabled plugin is a support burden; a loudly disabled one is a bug report.

## Testing requirements

Same bar the v0.28.0 chip fixes established — assert the user-visible outcome,
not the structural one:

- a plugin-contributed chip **renders with non-zero height** and its text
  appears on screen. The invisible-button defect passed every structural
  assertion for a full release.
- a plugin-contributed command appears in `/help`, the palette *and* the
  autocomplete, and executes.
- a plugin raising at import, in a hook, and in `text()` each leave DOXA
  running and produce exactly one message.
- a repo-local plugin file is **not** loaded. This is a security assertion and
  should read like one.

## Open questions

1. **Per-repo enablement?** Useful ("this plugin only for work repos"), but the
   config must stay in `~/.doxa/`, never in the repo, or it reopens the
   execution hole above.
3. **Do plugins get their own tabs?** `SubagentTranscriptTab` is the existing
   precedent for a non-session tab. Generalizing it is plausible but adds a
   surface the review gate has not looked at yet.
