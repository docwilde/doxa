# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.claude_plugins -- discovering, and selectively adopting, the
operator's OWN Claude Code plugins and skills into the engine's isolated
CLI, without reopening item AA's defect (see :mod:`doxa.cli_isolation`'s
module docstring for the measured problem this module is careful not to
bring back).

THE SHAPE OF A PLUGIN, measured against this machine's real
``~/.claude/plugins`` (5 installed plugins: github, warp, codex, caveman,
lore): ``plugins/installed_plugins.json`` names one ``installPath`` per
``<plugin>@<marketplace>`` scope key, resolved UNDER ``plugins/cache`` --
and that cache is versioned by path and can hold SEVERAL versions of the
same plugin at once (``lore/lore/0.32.0`` through ``0.39.0`` on this
machine), most marked ``.orphaned_at`` by the CLI's own housekeeping.
:func:`discover` reads ``installPath`` directly rather than globbing the
cache for the newest directory -- the orphaned versions are not what the
CLI itself would load, and guessing would be exactly the kind of invented
scoping ``cli_isolation``'s own skills note already refused to do.

A plugin directory can carry, in any combination: ``commands/*.md`` (slash
commands), ``skills/*/SKILL.md`` (the same shape ``cli_isolation`` already
carries for the user's own learned skills), ``agents/*.md`` (Task-tool
subagent definitions), a ``hooks/hooks.json`` file OR a top-level
``"hooks"`` key in ``.claude-plugin/plugin.json`` (both shapes are real:
LORE uses the file, caveman inlines the key), and a ``.mcp.json`` /
``mcp.json`` declaring an MCP server to start.

THE ADOPT/REFUSE LINE, decided per capability rather than per plugin:

* **commands, skills, agents -- ADOPTED.** All three are inert until
  something explicit invokes them: a command is typed, a skill is read
  when the model reaches for it, an agent spawns only through a Task-tool
  call the running permission mode still gates. None of the three executes
  anything at session start. Skills specifically are the SAME risk class
  ``cli_isolation.ensure_skills_link`` already carries wholesale for the
  user's own ``~/.claude/skills`` -- this module only widens the source to
  include the skills a PLUGIN bundles (``~/.claude/plugins/cache/.../
  skills/``), which that symlink never reached.
* **hooks -- REFUSED, unconditionally.** This is item AA's actual defect:
  a plugin's ``hooks.json`` fires on ``SessionStart``/``UserPromptSubmit``/
  ``PreCompact`` with no invocation from the user or the model at all.
  LORE's own hooks are the measured case -- a SECOND memory snapshot on
  top of the one ``doxa.engine._build_options`` already injects, silently.
  Adopting hooks would reopen exactly that.
* **MCP servers -- REFUSED, unconditionally.** The other half of the same
  measured defect (``doxa.cli_isolation``'s docstring: "started an
  external MCP server -- on top of, not instead of, DOXA's own in-process
  LORE snapshot"). An MCP server is a process DOXA did not start and does
  not own, connected automatically at boot, exactly the unrequested-
  execution shape hooks are refused for.

THE MECHANISM: adoption never touches the CLI's own plugin-management
files (``installed_plugins.json``, ``enabledPlugins`` in ``settings.json``)
-- doing so would ask the CLI to load the plugin AS a plugin, hooks and
MCP servers included, which is the exact channel item AA closed. Instead,
each adoptable plugin is copied into a DOXA-owned, sanitized staging
directory under ``cli_isolation.cli_config_dir()`` (:func:`_copy_sanitized`
strips the ``hooks/`` directory, any ``hooks.json``, ``.mcp.json``/
``mcp.json``, and the ``"hooks"``/``"mcpServers"`` manifest keys -- a
copy-then-exclude approach, not a narrow whitelist, so a command's own
``${CLAUDE_PLUGIN_ROOT}/scripts/...`` reference still resolves inside the
staged copy), and the sanitized path is handed to the SDK as
``SdkPluginConfig(type="local", path=...)`` -- ``ClaudeAgentOptions.plugins``,
which the transport turns into one ``--plugin-dir`` flag per entry
(measured, ``claude_agent_sdk/_internal/transport/subprocess_cli.py``:
``cmd.extend(["--plugin-dir", plugin["path"]])``), SESSION-SCOPED and nothing
else. The isolated CLI's own ``settings.json``
(:data:`doxa.cli_isolation.OWNED_SETTINGS`) stays the empty object it
already was -- ``doctor``'s isolation check still greps that file for
exactly ``hooks``/``enabledPlugins``/``plugins`` and still expects none of
them, and this module never gives it a reason to find one.

LORE, SPECIFICALLY, IS BLOCKED (:data:`BLOCKLIST`), independent of the
opt-in setting and independent of whether the user's own
``~/.claude/settings.json`` has it enabled. Two reasons, not one:

1. ``lore_core`` already runs IN-PROCESS inside DOXA (``doxa.engine``
   imports it directly for the native belief tools and the snapshot
   ``_build_options`` injects). Loading the Claude-Code-plugin FORM of the
   same project would be a second, out-of-band carrier into the identical
   belief store -- its ``/lore:*`` commands read and stage against the
   same SQLite file DOXA's own ``/beliefs``, ``/pending`` and ``/search``
   already read, through a code path DOXA's error surface does not own.
2. Measured directly against this machine's cached copy: every one of
   LORE's 15 command files instructs the model to run
   ``python3 "${CLAUDE_PLUGIN_ROOT}/bin/lore.py" <subcommand>`` --
   a path this module deliberately never stages (``bin/`` is plain,
   unreviewed executable code the "copy-then-exclude" design above does
   not special-case out, and LORE'S specific commands are refused
   wholesale rather than left to fail at invocation with a missing-file
   error). Adopting LORE's commands verbatim would ship BROKEN commands,
   not merely redundant ones.

OPT-IN, DEFAULT OFF (:data:`config.SETTINGS`'s ``adopt_plugins`` row,
``DOXA_ADOPT_PLUGINS``): isolation is the resting posture; adopting the
operator's own plugins is a choice they make. :func:`discover` itself is
unconditional and read-only regardless of the setting -- ``/plugins`` has
to be able to preview what turning it on WOULD do, and a read-only scan of
JSON files the operator's own CLI already wrote costs nothing worth
gating.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import cli_isolation as cli_isolation_mod
from . import config as config_mod

# -- where the REAL (operator's) plugin state lives, under user_config_base()
INSTALLED_PLUGINS_REL = Path("plugins") / "installed_plugins.json"
SETTINGS_REL = Path("settings.json")
MANIFEST_REL = Path(".claude-plugin") / "plugin.json"

# -- what a staged copy never carries
HOOKS_DIRNAME = "hooks"
HOOKS_FILENAME = "hooks.json"
MCP_FILENAMES = (".mcp.json", "mcp.json")
_MANIFEST_HAZARD_KEYS = ("hooks", "mcpServers")

# Where sanitized staged copies live -- a subdirectory of the isolated CLI's
# OWN config dir, so it is cleaned up, permissioned and reasoned about
# alongside everything else DOXA owns there.
STAGED_SUBDIR = "plugins-adopted"

# Hard-refused regardless of the opt-in setting or the operator's own
# enabledPlugins -- see the module docstring's "LORE, SPECIFICALLY" section.
BLOCKLIST: "frozenset[tuple[str, str]]" = frozenset({("lore", "lore")})

BLOCKLIST_REASON = (
    "lore_core already runs in-process inside doxa (the native belief "
    "tools and the injected snapshot both import it directly) -- loading "
    "the Claude Code LORE plugin would be a second, out-of-band carrier "
    "into the SAME belief store, and every one of its commands shells out "
    "to a bin/ path this module does not stage, so they would not even "
    "run"
)


@dataclass(frozen=True)
class DiscoveredPlugin:
    """One entry from the operator's OWN ``installed_plugins.json`` --
    what it is, what it carries, and whether it may be adopted."""

    plugin: str
    marketplace: str
    scope_key: str
    """``<plugin>@<marketplace>``, the exact key ``installed_plugins.json``
    and ``settings.json``'s ``enabledPlugins`` both use."""
    version: str
    install_path: Path
    description: str
    user_enabled: bool
    """Whether the OPERATOR'S OWN ``~/.claude/settings.json`` has this
    plugin's ``enabledPlugins`` entry true. Adoption never enables a
    plugin the operator has not already turned on for their own CLI."""
    has_hooks: bool
    has_mcp: bool
    n_commands: int
    n_skills: int
    n_agents: int
    blocked: bool
    blocked_reason: str

    @property
    def has_adoptable_content(self) -> bool:
        return bool(self.n_commands or self.n_skills or self.n_agents)

    @property
    def refused(self) -> bool:
        """True when NOTHING from this plugin will be staged, regardless
        of the opt-in setting -- see :meth:`refusal_reason` for why."""
        return self.blocked or not self.user_enabled or not self.has_adoptable_content

    def refusal_reason(self) -> str:
        """Empty when adoptable. A refusal that does not say why is a bug
        report waiting to happen (the task's own words) -- this is the
        one place that sentence is written down as code."""
        if self.blocked:
            return self.blocked_reason
        if not self.user_enabled:
            return "disabled in your own ~/.claude/settings.json"
        if not self.has_adoptable_content:
            return "no commands, skills or agents to adopt (nothing but hooks/mcp, or empty)"
        return ""


def _load_json(path: Path) -> dict:
    """Never raises: a missing or malformed file reads as "nothing here",
    the same rule every other reader in this codebase applies to files it
    does not own."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _count_md(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    try:
        return sum(1 for p in directory.iterdir() if p.is_file() and p.suffix == ".md")
    except OSError:
        return 0


def _count_skill_dirs(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    try:
        return sum(1 for p in directory.iterdir() if p.is_dir() and (p / "SKILL.md").exists())
    except OSError:
        return 0


def _front_matter_field(md_path: Path, key: str) -> str:
    """One ``key: value`` line out of a command file's YAML front matter
    (between the opening and closing ``---`` fences) -- no YAML dependency,
    every real command file measured on this machine (LORE, caveman, codex)
    uses this exact flat shape for ``description``/``argument-hint``, and a
    hand-rolled parser that only ever reads a flat string field cannot
    silently misparse the nested cases a real YAML lib would have to
    handle. Empty when the file is missing, has no front matter, or the
    key is absent -- never a guess."""
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end == -1:
        return ""
    prefix = key + ":"
    for line in text[3:end].splitlines():
        line = line.strip()
        if line.lower().startswith(prefix.lower()):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    return ""


@dataclass(frozen=True)
class PluginCommand:
    """One command a plugin carries -- what to actually TYPE for it to
    work, not the bare name the plugin's own docs may advertise.

    Measured directly (this module's own docstring, and reconfirmed
    against a real adopted plugin for this fix): a plugin loaded via
    ``--plugin-dir`` registers its commands NAMESPACED,
    ``<plugin>:<command-stem>`` -- typing the bare
    ``/<command-stem>`` a marketplace install would answer to gets
    ``Unknown command: /<command-stem>`` back from the CLI instead, even
    when the name is unique across every loaded plugin. :attr:`invocable`
    is that namespaced form, with NO leading slash (every caller adds its
    own, the same convention :class:`doxa.commands.SlashCommand` uses)."""

    invocable: str
    """``<plugin>:<command-stem>`` -- what the underlying CLI actually
    matches. No leading slash."""
    summary: str
    """The command file's own ``description:`` front-matter field, or
    empty when it does not have one."""
    argument_hint: str
    """The command file's own ``argument-hint:`` front-matter field, or
    empty. Used to build a usage string so the prompt's autocomplete can
    tell a caller whether to leave room for arguments."""


def command_names(plugin: DiscoveredPlugin) -> "list[PluginCommand]":
    """Every command ``plugin`` carries, as the EXACT invocable spelling
    plus whatever the file itself says about it -- the whole fix for the
    reported defect starts here: a discoverable command whose name still
    fails when typed is not discoverable, it is a trap. Sorted by stem for
    a deterministic listing; empty for a plugin with no ``commands/``
    directory at all (skills and agents are adopted too but are not
    something a user TYPES, so they have no analog here)."""
    directory = plugin.install_path / "commands"
    if not directory.is_dir():
        return []
    try:
        paths = sorted(
            (p for p in directory.iterdir() if p.is_file() and p.suffix == ".md"),
            key=lambda p: p.stem,
        )
    except OSError:
        return []
    return [
        PluginCommand(
            invocable=f"{plugin.plugin}:{path.stem}",
            summary=_front_matter_field(path, "description"),
            argument_hint=_front_matter_field(path, "argument-hint"),
        )
        for path in paths
    ]


def adopted_commands(
    discovered: "list[DiscoveredPlugin] | None" = None,
) -> "list[tuple[DiscoveredPlugin, PluginCommand]]":
    """Every command that would actually run if typed into a session
    started RIGHT NOW -- the exact same eligibility :func:`adopt` applies
    (opted in, not blocklisted, enabled in the operator's own CLI, at
    least one command/skill/agent), computed read-only, with no staging
    copy: this is a PREVIEW surface (``/plugins``, the prompt's
    autocomplete, the Ctrl+P palette), not a second place adoption itself
    happens -- :func:`adopt` is still the only function that writes
    anything to disk or hands anything to the SDK.

    Empty whenever :func:`adoption_enabled` is False, matching
    :func:`adopt`'s own empty return in that state -- a command a live
    session cannot reach must not be offered as though it could, on any
    surface."""
    if not adoption_enabled():
        return []
    discovered = discover() if discovered is None else discovered
    out: "list[tuple[DiscoveredPlugin, PluginCommand]]" = []
    for plugin in discovered:
        if plugin.refused:
            continue
        for command in command_names(plugin):
            out.append((plugin, command))
    return out


def adopted_skill_summary(
    discovered: "list[DiscoveredPlugin] | None" = None,
) -> "tuple[int, int]":
    """``(total skills, plugins contributing at least one)`` across every
    plugin that WOULD be adopted right now -- the same eligibility
    :func:`adopt` applies, computed read-only with no staging copy, the
    same relationship :func:`adopted_commands` already has to :func:`adopt`.

    This is what ``/context`` (item K's grid redesign) reports as the
    "skills" per-source section: the CLI's own ``get_context_usage`` has no
    ``skills`` field to read (unlike ``mcpTools`` and ``agents``, which it
    does report), so a skill count is the one piece of that section DOXA
    measures itself -- directly off the manifest counts :func:`discover`
    already read from disk, never a guess.

    ``(0, 0)`` -- hide-at-zero -- whenever :func:`adoption_enabled` is
    False or no adopted plugin carries a skill, matching every other
    plugin-adoption reader's empty state in this module."""
    if not adoption_enabled():
        return (0, 0)
    discovered = discover() if discovered is None else discovered
    total = 0
    contributing = 0
    for plugin in discovered:
        if plugin.refused:
            continue
        if plugin.n_skills:
            total += plugin.n_skills
            contributing += 1
    return (total, contributing)


def _has_hooks(install_path: Path, manifest: dict) -> bool:
    if manifest.get("hooks"):
        return True
    return (install_path / HOOKS_DIRNAME / HOOKS_FILENAME).exists()


def _has_mcp(install_path: Path, manifest: dict) -> bool:
    if manifest.get("mcpServers"):
        return True
    return any((install_path / name).exists() for name in MCP_FILENAMES)


def discover(base: "Path | None" = None) -> "list[DiscoveredPlugin]":
    """Every plugin the operator's REAL Claude Code install knows about
    (``base`` defaults to :func:`doxa.cli_isolation.user_config_base` --
    the same split ``identity.py`` and the credential/skills sync already
    use: this reads the operator's OWN environment, never DOXA's isolated
    copy, because that is where a real install's plugin state lives).

    Read-only, and unconditional -- does not check the opt-in setting.
    Never raises: an absent or malformed ``installed_plugins.json`` reads
    as "no plugins installed", the same as Claude Code not being present
    at all."""
    base = base if base is not None else cli_isolation_mod.user_config_base()
    installed = _load_json(base / INSTALLED_PLUGINS_REL)
    plugins_map = installed.get("plugins")
    if not isinstance(plugins_map, dict) or not plugins_map:
        return []
    enabled_map = _load_json(base / SETTINGS_REL).get("enabledPlugins")
    enabled_map = enabled_map if isinstance(enabled_map, dict) else {}

    out: list[DiscoveredPlugin] = []
    for scope_key, entries in plugins_map.items():
        if not isinstance(entries, list) or not entries:
            continue
        # Measured: every entry on this machine carries scope "user" and a
        # single-element list. The FIRST entry is what installed_plugins.json
        # itself names as the install -- picking it directly is the
        # "deliberate, not glob-and-hope" choice the cache's multiple
        # orphaned versions (.orphaned_at) exist to warn against.
        entry = entries[0]
        if not isinstance(entry, dict):
            continue
        install_path_raw = str(entry.get("installPath") or "").strip()
        if not install_path_raw:
            continue
        install_path = Path(install_path_raw)
        plugin_name, _, marketplace = scope_key.partition("@")
        manifest = _load_json(install_path / MANIFEST_REL)
        blocked = (plugin_name, marketplace) in BLOCKLIST
        out.append(DiscoveredPlugin(
            plugin=plugin_name,
            marketplace=marketplace,
            scope_key=scope_key,
            version=str(entry.get("version") or ""),
            install_path=install_path,
            description=str(manifest.get("description") or ""),
            user_enabled=bool(enabled_map.get(scope_key)),
            has_hooks=_has_hooks(install_path, manifest),
            has_mcp=_has_mcp(install_path, manifest),
            n_commands=_count_md(install_path / "commands"),
            n_skills=_count_skill_dirs(install_path / "skills"),
            n_agents=_count_md(install_path / "agents"),
            blocked=blocked,
            blocked_reason=BLOCKLIST_REASON if blocked else "",
        ))
    return sorted(out, key=lambda p: p.scope_key)


def adoption_enabled() -> bool:
    """``DOXA_ADOPT_PLUGINS`` / the config file's ``adopt_plugins`` row --
    same off-by-default, explicit-truthy-string reading
    ``doxa.engine.bypass_arming_enabled`` uses for the other opt-in
    capability expansion in this codebase."""
    raw = config_mod.raw("DOXA_ADOPT_PLUGINS").strip()
    return bool(raw) and raw.lower() not in ("0", "false", "no", "off")


def staged_plugin_dir(discovered: DiscoveredPlugin) -> Path:
    """Where a plugin's sanitized copy lives -- one directory per scope
    key, so ``lore@lore`` and a hypothetical ``lore@other-marketplace``
    never collide."""
    safe = discovered.scope_key.replace("/", "_")
    return cli_isolation_mod.cli_config_dir() / STAGED_SUBDIR / safe


def _copy_sanitized(source: Path, dest: Path) -> None:
    """Rebuild ``dest`` as a full copy of ``source`` MINUS the two live
    channels this module refuses -- the ``hooks/`` directory and any
    ``hooks.json`` anywhere in the tree, plus ``.mcp.json``/``mcp.json``
    at any level. Copy-then-exclude, not a narrow whitelist: a command's
    own ``${CLAUDE_PLUGIN_ROOT}/scripts/...`` reference (real, measured
    against the codex plugin) still resolves inside the staged copy,
    which a whitelist of only ``commands/``/``skills/``/``agents/`` would
    have silently broken.

    Rebuilt from scratch on every call rather than diffed -- same
    discipline as :func:`doxa.cli_isolation.ensure_cli_config_dir`'s
    ``settings.json``: a stale hazard file surviving a plugin version
    bump is worse than a few hundred KB recopied on a session start."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns(HOOKS_FILENAME, *MCP_FILENAMES, ".git")
    for item in sorted(source.iterdir()):
        if item.name == HOOKS_DIRNAME and item.is_dir():
            continue
        if item.name in (HOOKS_FILENAME, *MCP_FILENAMES):
            continue
        target = dest / item.name
        if item.is_dir():
            shutil.copytree(item, target, ignore=ignore, symlinks=False)
        else:
            shutil.copy2(item, target)
    _strip_manifest_hazards(dest / MANIFEST_REL)


def _strip_manifest_hazards(manifest_path: Path) -> None:
    if not manifest_path.exists():
        return
    data = _load_json(manifest_path)
    if not data:
        return
    changed = False
    for key in _MANIFEST_HAZARD_KEYS:
        if key in data:
            del data[key]
            changed = True
    if not changed:
        return
    try:
        manifest_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass  # the unsanitized key staying in a copy nothing points a
        # --plugin-dir flag at costs nothing; the flag is what matters.


def adopt(discovered: "list[DiscoveredPlugin] | None" = None) -> "list[dict]":
    """Stage every adoptable plugin and return the ``SdkPluginConfig``-
    shaped dicts (``{"type": "local", "path": ...}``)
    ``SessionEngine._build_options`` passes through
    ``ClaudeAgentOptions.plugins`` -- one ``--plugin-dir`` flag per entry,
    session-scoped, never touching the CLI's own plugin-management files.

    Empty, with nothing staged or touched on disk, unless
    :func:`adoption_enabled` -- discovery stays unconditional
    (:func:`discover`) so ``/plugins`` can preview what turning the
    setting on WOULD do, but staging (disk writes) only happens once the
    operator has opted in.

    A staging failure for one plugin (permissions, a half-written cache
    entry) costs that plugin, never the session -- same posture
    ``cli_isolation.ensure_cli_config_dir`` takes for its own provisioning
    failure."""
    if not adoption_enabled():
        return []
    discovered = discover() if discovered is None else discovered
    out: list[dict] = []
    for plugin in discovered:
        if plugin.refused:
            continue
        dest = staged_plugin_dir(plugin)
        try:
            _copy_sanitized(plugin.install_path, dest)
        except OSError:
            continue
        out.append({"type": "local", "path": str(dest)})
    return out


def report(discovered: "list[DiscoveredPlugin] | None" = None) -> str:
    """``/plugins``'s whole output: what was discovered, what is enabled
    (in the operator's own CLI), what would be or is adopted, and what is
    refused and why -- a refusal with no reason is a bug report waiting
    to happen, so every refused row states one."""
    discovered = discover() if discovered is None else discovered
    on = adoption_enabled()
    base = cli_isolation_mod.user_config_base()
    lines = [
        "claude plugins",
        "",
        f"adoption: {'ON' if on else 'OFF'}"
        + (
            "" if on else
            " -- set 'adopt claude plugins' in /settings, or "
            "DOXA_ADOPT_PLUGINS=1, to opt in"
        ),
        f"scanned: {base}",
        "",
    ]
    if not discovered:
        lines.append("no Claude Code plugins installed there.")
    else:
        n_adopted = 0
        n_would_adopt = 0  # could_adopt, but the setting is off
        for plugin in discovered:
            will_adopt = on and not plugin.refused
            could_adopt = not plugin.refused
            if will_adopt:
                n_adopted += 1
                glyph = "✓"
            elif could_adopt:
                n_would_adopt += 1
                glyph = "○"  # would adopt if the setting were on
            else:
                glyph = "✗"
            version = plugin.version or "?"
            lines.append(f"{glyph} {plugin.scope_key}  v{version}")
            if plugin.description:
                lines.append(f"    {plugin.description}")
            bits = [
                f"{plugin.n_commands} command(s)",
                f"{plugin.n_skills} skill(s)",
                f"{plugin.n_agents} agent(s)",
            ]
            refused_bits = []
            if plugin.has_hooks:
                refused_bits.append("hooks")
            if plugin.has_mcp:
                refused_bits.append("MCP server(s)")
            detail = "adoptable: " + ", ".join(bits)
            if refused_bits:
                detail += "  ·  refused always: " + ", ".join(refused_bits)
            lines.append(f"    {detail}")
            if plugin.refused:
                lines.append(f"    refused: {plugin.refusal_reason()}")
            elif plugin.n_commands:
                # The reported defect, closed here: DOXA's own autocomplete
                # and Ctrl+P palette fold these rows in too (see
                # doxa.commands._plugin_rows) once adoption is actually ON,
                # but /plugins is the one place they are spelled out
                # regardless of that setting -- a plugin marked "would
                # adopt" (○) still deserves to say what typing its
                # commands would require, not just how many there are.
                # NAMESPACED, never the bare name a plugin's own docs
                # advertise for its marketplace-installed form -- measured
                # against a real adopted plugin: the bare form gets
                # "Unknown command" back from the CLI even when unique.
                for command in command_names(plugin):
                    text = f"    /{command.invocable}"
                    if command.argument_hint:
                        text += f" {command.argument_hint}"
                    if command.summary:
                        text += f"  -- {command.summary}"
                    lines.append(text)
            lines.append("")
        n_refused = len(discovered) - n_adopted - n_would_adopt
        tally = (
            f"{len(discovered)} plugin(s) discovered, {n_adopted} adopted, "
            f"{n_refused} refused"
        )
        if n_would_adopt:
            # Named separately from n_refused on purpose: these are not
            # blocked, disabled or empty -- they are otherwise-adoptable
            # and idle for exactly one reason, the setting above, and a
            # tally that folded them into "refused" would say the wrong
            # thing about why (the actual reported gap this report exists
            # to close: nothing adopted because the setting is OFF is not
            # the same finding as a plugin refused on its own merits).
            tally += (
                f", {n_would_adopt} more would adopt if 'adopt claude "
                "plugins' were on"
            )
        lines.append(tally + ".")
    lines.append("")
    lines.append(
        "hooks and MCP servers are refused for every plugin, always -- "
        "see docs/plans/plugins.md. Your own ~/.claude/skills is carried "
        "unconditionally regardless of this setting (doxa.cli_isolation)."
    )
    return "\n".join(lines)
