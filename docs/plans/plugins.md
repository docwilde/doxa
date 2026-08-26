# Claude Code plugins and skills, adopted without the duplicate snapshot — specification

Status: **implemented, v0.74.0**; command discoverability fixed, **v0.77.0**.
`doxa/claude_plugins.py`, the `adopt_plugins` setting (default OFF),
`/plugins` and `/reload-plugins`.

Not to be confused with [`docs/plans/plugin-api.md`](plugin-api.md), which
specifies a *different* thing — DOXA's own extension points, for Python code
that runs **inside DOXA's own process** and extends the TUI (a slash command,
a status chip, a transcript renderer). This document is about the **other**
plugin system: Claude Code's own, which lives entirely inside the `claude`
CLI process the engine spawns, and which DOXA neither writes nor loads code
from — it only decides, per session, which *pieces* of what the operator
already installed for their own interactive `claude` get carried into that
spawned process. Two systems, two documents, one name collision worth being
explicit about.

## Read this first: the thing being reopened

[`doxa/cli_isolation.py`](../../doxa/cli_isolation.py)'s module docstring is
required reading before this one — it is the audit that closed item AA, and
this feature deliberately reopens the door that audit closed, on purpose,
carefully. Its measurements, restated because they are the whole reason this
spec has an "adopt vs refuse" section instead of a single on/off switch:

- A bare, unisolated `claude -p` on the audit's machine loaded **5 plugins,
  16 hooks, 28 commands**, and started an external MCP server.
- Among them, the LORE plugin's own `hooks/hooks.json` (`SessionStart`,
  `UserPromptSubmit`, `PreCompact`) fired a **second, independent memory
  injection** into the same session — on top of, not instead of, the
  snapshot `doxa.engine._build_options` already appends to `system_prompt`.
  Silent duplication, not a crash, which is why it went unnoticed until the
  operator read a citation in the transcript that had no business being
  there.
- A fresh `CLAUDE_CONFIG_DIR` alone drops plugin/hook/command loading to
  zero, because `installed_plugins.json` and `plugins/cache` resolve UNDER
  `CLAUDE_CONFIG_DIR`, not hardcoded to `~/.claude`.
- `--bare` / `CLAUDE_CODE_SIMPLE=1` were rejected outright: they silently
  turn off OAuth/keychain auth, which would have been the "silent logout"
  defect AA also exists to forbid.
- `--safe-mode` kills plugin hooks but **not** project `CLAUDE.md`, contrary
  to its own `--help` text — measured, not assumed.

The fix `cli_isolation.py` shipped was total: the engine's spawned CLI gets
`CLAUDE_CONFIG_DIR=~/.doxa/claude-cli` (a directory DOXA owns outright, with
its own empty `settings.json` — no `hooks`, no `enabledPlugins`, no
`plugins` key), so the isolated CLI starts with **zero** of the operator's
plugins, hooks or commands. `LORE_SKIP=1` rides the same env dict as
belt-and-braces. One exception was carried through deliberately even then:
`~/.claude/skills` (the operator's own **learned** skills — approved
artifacts, not a foreign hook firing unasked) is symlinked into the isolated
directory, unconditionally, because closing that channel without an
explicit carry would make skills the operator already approved silently
vanish.

Measured before this feature existed, on the machine this spec was written
against: `~/.doxa/claude-cli/` carried a `skills` symlink (12 lore-learned
skills, unconditional) and nothing else — no `plugins/`, and nothing from
any INSTALLED plugin's own bundled skills or commands (a plugin's
`skills/*/SKILL.md` lives under `~/.claude/plugins/cache/...`, a path the
symlink above never reaches). So before this feature, a plugin's commands
and skills were absent from every DOXA session, full stop, regardless of
whether the operator had them enabled in their own interactive `claude`.

## What is discovered, and from where

Discovery (`doxa.claude_plugins.discover`) reads the OPERATOR'S OWN, REAL
Claude Code config — `doxa.cli_isolation.user_config_base()`, the same split
`doxa.identity` already uses for the identity block and the subscription
chip: `$CLAUDE_CONFIG_DIR` when the operator's own environment sets it, else
`~/.claude`. It never reads the isolated directory, because the isolated
directory is deliberately empty of this by design.

Three files, read read-only:

| file | what it says |
|---|---|
| `plugins/installed_plugins.json` | every installed plugin, keyed `<plugin>@<marketplace>`, each entry naming an `installPath` |
| `settings.json` | `enabledPlugins`: which of those the operator has actually turned ON in their own CLI |
| `<installPath>/.claude-plugin/plugin.json` | the plugin's own manifest: description, and (sometimes) an inline `"hooks"` or `"mcpServers"` key |

**The cache is versioned by path and can hold several versions of the same
plugin.** Measured on the machine this spec was written against: LORE alone
had eight directories side by side under `plugins/cache/lore/lore/` —
`0.32.0` through `0.39.0` — most marked `.orphaned_at` by the CLI's own
housekeeping, one marked `.in_use` and matching exactly what
`installed_plugins.json` names as the current `installPath`
(`0.34.0` on that machine). Discovery reads `installPath` directly and never
globs the cache for "the newest version" — the orphaned entries are not what
the CLI itself would load, and guessing would be the same invented scoping
`cli_isolation`'s own skills note already refused to do for a different
question.

For each discovered plugin, discovery counts what it carries
(`commands/*.md`, `skills/*/SKILL.md`, `agents/*.md`) and detects the two
refused capabilities in **either** shape a real plugin uses (measured: LORE
ships a `hooks/hooks.json` file; caveman inlines a top-level `"hooks"` key in
`plugin.json` instead — both are real): a `hooks/hooks.json` file or a
`"hooks"` manifest key; a `.mcp.json`/`mcp.json` file or a `"mcpServers"`
manifest key (measured against the official `github` plugin, which declares
its MCP server via `.mcp.json` at the plugin root).

Discovery is unconditional and read-only — it runs whether or not the
opt-in setting below is on, because `/plugins` has to be able to preview
what turning it on *would* do.

## What is adopted, what is refused, and why — per capability, not per plugin

A plugin is not one indivisible unit here. Each capability is judged on
whether it can act **without an explicit invocation**:

| capability | verdict | why |
|---|---|---|
| commands (`commands/*.md`) | **adopted** | Inert until the user types the slash command. No code runs at load. |
| skills (`skills/*/SKILL.md`) | **adopted** | Same risk class `cli_isolation.ensure_skills_link` already carries wholesale for the operator's own learned skills — read by the model only when it reaches for that skill. This widens the SOURCE (a plugin's bundled skills, not reached by that symlink) without widening the RISK. |
| agents (`agents/*.md`, Task-tool subagent definitions) | **adopted** | A prompt template, reachable only through a Task-tool call the running permission mode still gates — no more privilege than the main session already has. |
| hooks (`hooks/hooks.json` or a manifest `"hooks"` key) | **refused, always** | This is item AA's actual defect. A hook fires on `SessionStart`/`UserPromptSubmit`/`PreCompact` with no invocation at all — LORE's own hooks are the measured case, a second memory snapshot on top of DOXA's own. Adopting hooks would reopen exactly that, for every plugin, not just LORE. |
| MCP servers (`.mcp.json`/`mcp.json` or a manifest `"mcpServers"` key) | **refused, always** | The other half of the SAME measured defect — "started an external MCP server… on top of… DOXA's own in-process LORE snapshot" is in `cli_isolation.py`'s own docstring. A process DOXA did not start, connected automatically at boot: the identical unrequested-execution shape hooks are refused for. |

Commands, skills and agents are additive and dormant; hooks and MCP servers
are the parts of a plugin that *do something on their own*. That is the
entire dividing line, and it holds regardless of which plugin is asking.

### The mechanism: a sanitized copy, never the CLI's own plugin loader

Adoption does **not** ask the isolated CLI to load anything as a plugin —
that channel (`installed_plugins.json`, `enabledPlugins`) is exactly what
item AA closed, and using it would silently bring hooks back with
everything else. Instead, `doxa.claude_plugins.adopt`:

1. For each plugin that is enabled (in the operator's OWN
   `~/.claude/settings.json`), not blocklisted (see below), and carries at
   least one command/skill/agent — copies the whole plugin directory into a
   DOXA-owned staging directory under
   `~/.doxa/claude-cli/plugins-adopted/<plugin>@<marketplace>/`.
2. **Copy-then-exclude, not a narrow whitelist.** The copy skips the
   `hooks/` directory, any `hooks.json` file, and `.mcp.json`/`mcp.json` —
   and then strips the `"hooks"`/`"mcpServers"` keys from the copied
   manifest if present. Everything else is copied verbatim, including a
   `scripts/`/`bin/` directory a command's own text references via
   `${CLAUDE_PLUGIN_ROOT}` (measured against the real `codex` plugin, whose
   commands do exactly this). A narrower "only copy `commands/`, `skills/`,
   `agents/`" design was considered and rejected: it would have shipped
   commands that render correctly but fail the moment they run, because the
   script they instruct the model to invoke would not exist in the copy.
3. Rebuilds the staging directory from scratch on every call rather than
   diffing it — the same discipline `ensure_cli_config_dir` uses for its own
   `settings.json`: a hazard file left over from a plugin version bump must
   not survive into a later session.
4. Hands each staged path to the SDK as
   `{"type": "local", "path": ...}` — `ClaudeAgentOptions.plugins`, which
   the transport turns into one `--plugin-dir <path>` flag per entry
   (measured against `claude_agent_sdk`'s own subprocess transport:
   `cmd.extend(["--plugin-dir", plugin["path"]])`). Session-scoped, per the
   CLI's own `--plugin-dir` semantics ("for this session only") — nothing
   is installed, nothing is written to the CLI's own plugin state.

The isolated CLI's own `settings.json`
(`doxa.cli_isolation.OWNED_SETTINGS`) is untouched by any of this — it stays
the empty object it already was, and `doctor`'s isolation check keeps
grepping it for exactly `hooks`/`enabledPlugins`/`plugins` and keeps
expecting none of them. Adoption and isolation are orthogonal: the isolated
directory is still empty of the operator's plugin STATE; the spawned CLI
just additionally receives a handful of `--plugin-dir` flags pointing at
copies DOXA made and sanitized itself.

### LORE is blocked, unconditionally, regardless of the setting or the operator's own `enabledPlugins`

```python
BLOCKLIST: frozenset[tuple[str, str]] = frozenset({("lore", "lore")})
```

Two independent reasons, either one sufficient on its own:

1. **`lore_core` already runs in-process inside DOXA.** `doxa.engine`
   imports it directly, for both the native belief tools and the snapshot
   `_build_options` injects into `system_prompt`. Loading the Claude-Code-
   plugin FORM of the same project would be a second, out-of-band carrier
   into the identical belief store — its `/lore:*` commands read and stage
   against the same SQLite file `/beliefs`, `/pending` and `/search`
   already read, through a code path DOXA's own error surface does not own.
2. **Measured directly: every one of LORE's 15 command files would not
   run.** Each instructs the model to execute
   `python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" <subcommand>` — a `bin/`
   path this module's copy-then-exclude design does not special-case IN
   (it is plain, unreviewed executable code, and LORE is refused wholesale
   rather than left to fail at invocation with a missing-file error).
   Adopting LORE's commands verbatim would ship **broken** commands, not
   merely redundant ones.

The blocklist is keyed on `(plugin, marketplace)`, matching
`installed_plugins.json`'s own `<plugin>@<marketplace>` scope-key shape —
`lore@lore` specifically, not a bare name match. A plugin someone else
publishes and merely calls "lore" under a different marketplace scope is a
different install action the operator would have taken deliberately, and is
not blocked by this rule; `/plugins`' output makes the actual reason for
every refusal explicit either way; this document names the deliberate
exception rather than trying to out-guess a hypothetical impostor.

## The setting: opt-in, default OFF

`adopt_plugins` / `DOXA_ADOPT_PLUGINS` (`doxa/config.py`, category
`Session`, next to `allow_bypass` — the other opt-in capability expansion in
this codebase, same shape). **Isolation stays the resting posture.**
Adopting the operator's own plugins is a choice they make, not something a
fresh install does for them — the same argument `allow_bypass`'s own note
makes for a different capability.

Off (the default): `/plugins` still discovers and previews, marking every
adoptable plugin `○` ("would adopt if this were on"); `adopt()` returns `[]`
and touches no disk; every session's `ClaudeAgentOptions.plugins` is `[]`,
byte-identical to every session before this feature shipped.

On: every session start stages (or re-stages) each adoptable plugin and
passes the resulting `--plugin-dir` list. Turning it off again does not
retroactively affect a session already running (see `/reload-plugins`
below) and does not delete anything already staged — a stale staging
directory costs nothing since nothing points a `--plugin-dir` flag at it
once adoption is off, and the next `adopt()` call rebuilds it from scratch
before ever pointing at it again.

## `/plugins`: what a user sees

Read-only, off the event loop (same discipline as `/doctor`): what
`discover()` found, whether the setting is on, and per plugin — adopted
(`✓`), would-adopt-if-on (`○`), or refused (`✗`) with the reason spelled
out. A refusal that does not say why is a bug report waiting to happen, so
every refused row states one: disabled in the operator's own
`~/.claude/settings.json`, blocklisted (LORE, with the reason above), or
nothing adoptable (hooks/MCP only). Every row also names its refused-always
capabilities (`hooks`, `MCP server(s)`) even for an otherwise-adopted
plugin, since "adopted" never means "adopted whole." A closing line always
restates that hooks and MCP servers are refused for every plugin,
unconditionally, and that the operator's own `~/.claude/skills` is carried
regardless of this setting — the two mechanisms are easy to conflate and the
report says so rather than leaving it implied.

## `/reload-plugins`: what a reload can and cannot do

Re-runs discovery and re-stages every adoptable plugin (if the setting is
on) without restarting DOXA — a fresh scan for a plugin installed, enabled,
upgraded or disabled since the process started, or simply for the setting
having just been turned on.

**What it cannot do, and says so in its own output:** a running session's
CLI was already spawned with whatever `--plugin-dir` flags its own
`_build_options()` resolved at `start()`. There is no SDK control request
that hands a live `claude` subprocess a new plugin directory — the same
category of limitation `/model`'s docstring already draws for a different
knob (`ClaudeAgentOptions.effort` has no live setter either). So
`/reload-plugins` only changes what the **next** session sees: a new tab
(Ctrl+T), `/clear`, or a fresh `doxa` launch. The command's own reply says
this in plain words rather than leaving a user to discover it by watching a
staged plugin do nothing in the tab they typed the command in.

## Typing an adopted command: the namespaced spelling, and where it shows up

**Reported, v0.77.0: "the discovered plugins' commands are not
available."** Measured against a real adopted plugin (isolated
`CLAUDE_CONFIG_DIR`, the exact stream-json path DOXA drives the CLI
through) before changing anything, to find which of four candidate causes
it actually was:

- the setting was off, so nothing was adopted — already true, and
  `/plugins` already named the setting; ruled out as the FULL story.
- the staged copy strips something a command needs — already false for
  everything but LORE (refused outright, see above); a staged `caveman`
  command ran fine once typed correctly.
- `--plugin-dir` reaches a spawned session but the CLI does not surface
  the command — false: the CLI's own `system.init` message lists it, in
  its `slash_commands` array.
- DOXA's own `/` autocomplete and Ctrl+P palette read one registry
  (`doxa.commands`) that never learned about adopted plugins — **true**,
  and compounded by a second, sharper problem the investigation surfaced:

**The CLI registers a plugin's commands NAMESPACED, not bare.** A plugin
loaded via `--plugin-dir` shows up in `slash_commands` as
`<plugin>:<command-stem>` — e.g. `caveman:caveman`, not `caveman`. Typing
the bare stem a plugin's own docs advertise (the form its
marketplace-installed self answers to) gets `Unknown command: /caveman`
back from the CLI, even when the name is unique across every loaded
plugin — measured directly, not assumed. So even a user who somehow knew
a command existed would type the wrong thing and read the CLI's refusal
as confirmation that adoption itself was broken.

Both are fixed together in `doxa.commands._plugin_rows`, which folds
`doxa.claude_plugins.adopted_commands()` — the exact eligibility
`adopt()` uses, computed read-only with no staging copy — into
`doxa.commands.ordered()`/`grouped()`/`names()` under a `Plugins` group,
using the CORRECT namespaced spelling as the row's `name`. Every surface
that already reads that registry (the prompt's autocomplete, the Ctrl+P
palette, `/help`) picks the rows up with no separate wiring — which is
exactly the guarantee docs/plans/plugin-api.md's own extension point 1
describes for DOXA's *native* plugin commands, now honored for an adopted
Claude-Code-plugin command too. Each row is `passthrough=True` and
`palette_prefill=True`: DOXA has no handler for a plugin command and must
never pretend to (`interactive()`/`interactive_names()` — and therefore
the pane's own handler dict and its closure test — read `REGISTRY`
directly and never see these rows at all), so completing one always lands
the text in the prompt for the user to submit, the same passthrough path
`/compact` already rides to the underlying CLI.

`/plugins`' own listing also states the exact spelling and description for
every adoptable command (`✓`/`○` rows), read from the command file's own
`description:`/`argument-hint:` front matter when present — a plugin
marked `○` ("would adopt if the setting were on") still says what typing
its commands would require, and the report's closing tally now counts
"would adopt if the setting were on" separately from "refused": an
otherwise-good plugin idle only because `adopt_plugins` is off is not the
same finding as one blocklisted, disabled, or carrying nothing adoptable,
and the tally no longer says so.

## Failure modes

- **A staging copy fails** (permissions, a half-written cache entry mid-
  upgrade): that plugin is skipped, silently from the session's point of
  view but visibly in `/plugins`' next report (it simply will not show as
  `✓`). Never fatal to the session — the same posture
  `cli_isolation.ensure_cli_config_dir` already takes for its own
  provisioning failure.
- **Claude Code is not present at all** (no `~/.claude/plugins` directory,
  or the operator has never installed a plugin): `discover()` returns `[]`,
  `/plugins` says so plainly, and nothing else changes. This is the
  ordinary case for anyone without Claude Code plugins, not an error.
- **A plugin is upgraded between two `/reload-plugins` calls**: the staged
  copy is rebuilt from scratch on the next `adopt()`, so a version bump is
  picked up cleanly rather than merged against a stale copy — see "rebuilds
  from scratch" above.
- **A malformed `installed_plugins.json` or manifest**: read as "nothing
  there" (same rule every other reader in this codebase applies to a file
  it does not own) rather than raising and taking a session down with it.
- **The isolation posture itself**: unaffected in every failure mode above.
  A broken discovery or a failed staging copy costs the ADOPTED plugin list,
  never the isolation the spawned CLI already has — `env=spawn_env()` is a
  separate keyword on the same `ClaudeAgentOptions` call and does not
  depend on this feature succeeding.

## Testing bar

Same rule as every user-visible surface since v0.28.0: assert rendered text
and non-zero geometry, not that a query merely matched something.

- `/plugins` and `/reload-plugins` produce a `SystemBlock` with
  `region.height > 0` and text content, not merely a widget the query
  layer can find.
- Discovery reads `installPath` from a synthetic `installed_plugins.json`
  with TWO versions of the same plugin present, and resolves to the named
  one, never "the newest directory in cache/".
- Hooks and MCP servers are detected in BOTH real shapes (a `hooks.json`
  file and an inline manifest key; a `.mcp.json` file and an inline
  manifest key) and never appear in a staged copy, whichever shape they
  came in.
- A staged copy preserves a file a command references
  (`${CLAUDE_PLUGIN_ROOT}/scripts/...`) that is neither `commands/`,
  `skills/` nor `agents/` — the copy-then-exclude claim, not merely the
  whitelist a narrower design would have shipped.
- LORE is refused even when enabled, non-empty, and the setting is on —
  the blocklist beats every other signal.
- `adoption_enabled()` is `False` with nothing configured, and `adopt()`
  returns `[]` and writes nothing to disk in that state.
- `SessionEngine._build_options` wires `claude_plugins.adopt()`'s return
  value straight into `ClaudeAgentOptions.plugins` — a fake `adopt()`
  reaches the SDK options object, and the untouched default reaches `[]`.
- A stale hazard file placed directly in a previously-staged directory does
  not survive a rebuild.
- v0.77.0: `command_names()` reads the NAMESPACED invocable spelling
  (`<plugin>:<stem>`) plus `description:`/`argument-hint:` front matter,
  tolerating a command file with neither. `adopted_commands()` is empty
  with the setting off and excludes the blocklisted plugin with it on. A
  plugin row folded into `doxa.commands.ordered()`/`grouped()` carries
  `group="Plugins"`, `passthrough=True`, `palette_prefill=True`, and is
  absent from `interactive_names()`/`find()`/`lookup()` (REGISTRY-only,
  unaffected — the closure invariant survives the fold-in untouched).
  Driven end to end against a real `DoxaApp` pilot: the prompt's
  autocomplete dropdown lists the adopted row and paints a non-zero
  region; completing it prefills without submitting; the palette's own
  entry prefills too (never `_cmd_run_slash`, which has no handler for a
  plugin row); the exact namespaced text, submitted, reaches the fake
  engine untouched (`received_prompts`), the same passthrough path
  `/compact` already proves. `/plugins`' report states the exact
  spelling and description for an adoptable command, and its tally names
  "would adopt if the setting were on" separately from "refused".

## Open questions

1. **Per-plugin toggles.** The task asked for one setting, and that is what
   shipped — `/plugins` previews per-plugin state, but there is no
   per-plugin override yet. A user who wants `caveman` but not `codex` has
   to disable the one they do not want in their OWN `~/.claude/settings.json`
   (which also affects their interactive CLI) rather than in DOXA. Whether
   that coupling is acceptable long-term is worth revisiting once real usage
   says which way it cuts.
2. **The `skills` SDK field.** `ClaudeAgentOptions.skills` (`list[str] |
   "all" | None`) is a separate CONTEXT FILTER the SDK exposes — which
   discovered skills the model's listing actually shows, independent of
   which directories are on disk. This spec does not touch it (adoption
   answers "is it on disk at all", not "is it in the model's listing");
   whether DOXA should ever narrow that listing per project is a different,
   unopened question.
3. **Staged-copy cleanup.** A plugin adopted once and later disabled (in the
   operator's own settings, or by turning `adopt_plugins` off) leaves its
   staging directory on disk, inert. Harmless (nothing points a
   `--plugin-dir` flag at it), but `/doctor` could plausibly report and
   offer to prune orphaned staged copies the way it already does for
   worktrees; not built here.
