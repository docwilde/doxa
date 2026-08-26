# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.claude_plugins -- discovering, and selectively adopting, the
operator's OWN Claude Code plugins and skills without reopening item AA's
duplicate-snapshot defect (doxa.cli_isolation's module docstring).

Every test builds a throwaway ``~/.claude``-shaped tree under ``tmp_path``
(``installed_plugins.json`` + ``settings.json`` + a fake plugin cache
directory per entry) rather than reading the real machine's install --
the real layout was measured by hand once (see docs/plans/plugins.md) and
these tests pin the SHAPE that measurement found, not today's live
values, which would make the suite flaky against whatever plugins happen
to be installed on whoever runs it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from doxa import claude_plugins as cp_mod
from doxa import cli_isolation as iso_mod
from doxa import config as config_mod


@pytest.fixture(autouse=True)
def _isolated_dirs(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "real-claude"))
    (tmp_path / "real-claude").mkdir(parents=True, exist_ok=True)
    config_mod.invalidate()
    yield
    config_mod.invalidate()


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _make_plugin(
    root, name, *, commands=0, skills=0, agents=0, hooks_file=False,
    hooks_key=False, mcp_file=False, mcp_key=False, extra_script=False,
    description="",
):
    """Build one fake plugin install directory under ``root/name`` and
    return its path."""
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"name": name, "description": description}
    if hooks_key:
        manifest["hooks"] = {"SessionStart": [{"hooks": [{"type": "command", "command": "x"}]}]}
    if mcp_key:
        manifest["mcpServers"] = {"srv": {"command": "x"}}
    _write_json(plugin_dir / ".claude-plugin" / "plugin.json", manifest)
    for i in range(commands):
        (plugin_dir / "commands").mkdir(exist_ok=True)
        (plugin_dir / "commands" / f"cmd{i}.md").write_text(f"do thing {i}")
    for i in range(skills):
        skill_dir = plugin_dir / "skills" / f"skill{i}"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(f"---\nname: skill{i}\n---\nbody")
    for i in range(agents):
        (plugin_dir / "agents").mkdir(exist_ok=True)
        (plugin_dir / "agents" / f"agent{i}.md").write_text(f"agent {i}")
    if hooks_file:
        (plugin_dir / "hooks").mkdir(exist_ok=True)
        _write_json(plugin_dir / "hooks" / "hooks.json", {"hooks": {}})
    if mcp_file:
        _write_json(plugin_dir / ".mcp.json", {"mcpServers": {"srv": {"command": "x"}}})
    if extra_script:
        (plugin_dir / "scripts").mkdir(exist_ok=True)
        (plugin_dir / "scripts" / "helper.sh").write_text("#!/bin/sh\necho hi\n")
    return plugin_dir


def _install(base, scope_key, install_path, *, version="1.0.0", enabled=True):
    installed_path = base / "plugins" / "installed_plugins.json"
    data = json.loads(installed_path.read_text()) if installed_path.exists() else {
        "version": 2, "plugins": {},
    }
    data["plugins"][scope_key] = [
        {"scope": "user", "installPath": str(install_path), "version": version}
    ]
    _write_json(installed_path, data)

    settings_path = base / "settings.json"
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    settings.setdefault("enabledPlugins", {})[scope_key] = enabled
    _write_json(settings_path, settings)


# -- discovery -----------------------------------------------------------


def test_discover_returns_nothing_with_no_installed_plugins_json(tmp_path):
    assert cp_mod.discover(base=tmp_path / "real-claude") == []


def test_discover_reads_installpath_directly_not_the_newest_cache_version(tmp_path):
    """The audit's own instruction: pick deliberately, do not glob-and-hope
    across a versioned cache that can hold several orphaned versions."""
    base = tmp_path / "real-claude"
    cache = tmp_path / "cache"
    # Two versions of the "same" plugin sitting in the cache side by side,
    # mirroring the real machine (lore 0.32.0..0.39.0 all present at once,
    # most orphaned). installed_plugins.json names the OLD one -- discovery
    # must use exactly that path, never "whichever looks newest" in cache/.
    old = _make_plugin(cache, "caveman-old", commands=1)
    _make_plugin(cache, "caveman-new", commands=2)
    _install(base, "caveman@caveman", old, version="0.1.0")

    [found] = cp_mod.discover(base=base)
    assert found.install_path == old
    assert found.n_commands == 1


def test_discover_reports_enabled_state_from_the_real_settings_json(tmp_path):
    base = tmp_path / "real-claude"
    cache = tmp_path / "cache"
    on = _make_plugin(cache, "on-plugin", commands=1)
    off = _make_plugin(cache, "off-plugin", commands=1)
    _install(base, "on@mp", on, enabled=True)
    _install(base, "off@mp", off, enabled=False)

    found = {p.scope_key: p for p in cp_mod.discover(base=base)}
    assert found["on@mp"].user_enabled is True
    assert found["off@mp"].user_enabled is False
    assert found["off@mp"].refused is True
    assert "disabled" in found["off@mp"].refusal_reason()


@pytest.mark.parametrize("shape", ["file", "key"])
def test_discover_detects_hooks_in_either_shape(tmp_path, shape):
    """LORE ships hooks/hooks.json; caveman inlines a top-level "hooks" key
    in plugin.json. Both are real, measured shapes -- see the module
    docstring."""
    base = tmp_path / "real-claude"
    cache = tmp_path / "cache"
    plugin = _make_plugin(
        cache, "hooked", commands=1,
        hooks_file=(shape == "file"), hooks_key=(shape == "key"),
    )
    _install(base, "hooked@mp", plugin)

    [found] = cp_mod.discover(base=base)
    assert found.has_hooks is True


@pytest.mark.parametrize("shape", ["file", "key"])
def test_discover_detects_mcp_in_either_shape(tmp_path, shape):
    base = tmp_path / "real-claude"
    cache = tmp_path / "cache"
    plugin = _make_plugin(
        cache, "mcpped", commands=1,
        mcp_file=(shape == "file"), mcp_key=(shape == "key"),
    )
    _install(base, "mcpped@mp", plugin)

    [found] = cp_mod.discover(base=base)
    assert found.has_mcp is True


def test_discover_counts_commands_skills_and_agents(tmp_path):
    base = tmp_path / "real-claude"
    cache = tmp_path / "cache"
    plugin = _make_plugin(cache, "full", commands=3, skills=2, agents=1)
    _install(base, "full@mp", plugin)

    [found] = cp_mod.discover(base=base)
    assert (found.n_commands, found.n_skills, found.n_agents) == (3, 2, 1)
    assert found.has_adoptable_content is True
    assert found.refused is False


def test_discover_refuses_a_plugin_with_nothing_but_hooks(tmp_path):
    base = tmp_path / "real-claude"
    cache = tmp_path / "cache"
    plugin = _make_plugin(cache, "hooks-only", hooks_file=True)
    _install(base, "hooksonly@mp", plugin)

    [found] = cp_mod.discover(base=base)
    assert found.has_adoptable_content is False
    assert found.refused is True
    assert "nothing" in found.refusal_reason() or "no commands" in found.refusal_reason()


# -- the LORE blocklist ----------------------------------------------------


def test_lore_is_blocked_even_when_enabled_and_full_of_adoptable_content(tmp_path):
    """The task's own requirement: LORE must never be adopted, full stop --
    lore_core already runs in-process. Enabled, with commands and skills,
    changes nothing."""
    base = tmp_path / "real-claude"
    cache = tmp_path / "cache"
    plugin = _make_plugin(cache, "lore", commands=15, skills=1)
    _install(base, "lore@lore", plugin, enabled=True)

    [found] = cp_mod.discover(base=base)
    assert found.blocked is True
    assert found.refused is True
    assert "lore_core" in found.refusal_reason()


def test_a_plugin_merely_named_lore_under_a_different_marketplace_is_not_blocked(tmp_path):
    """The blocklist is keyed on (plugin, marketplace) -- (lore, lore)
    specifically, matching installed_plugins.json's own scope-key shape.
    This pins that it is not a bare name match, so the behavior is
    understood exactly, not assumed."""
    base = tmp_path / "real-claude"
    cache = tmp_path / "cache"
    plugin = _make_plugin(cache, "lore-fork", commands=1)
    _install(base, "lore@someone-elses-marketplace", plugin, enabled=True)

    [found] = cp_mod.discover(base=base)
    assert found.blocked is False


# -- opt-in gating ---------------------------------------------------------


def test_adoption_is_off_by_default(tmp_path):
    assert cp_mod.adoption_enabled() is False


def test_adopt_stages_nothing_while_the_setting_is_off(tmp_path, monkeypatch):
    base = tmp_path / "real-claude"
    cache = tmp_path / "cache"
    plugin = _make_plugin(cache, "caveman", commands=2, skills=1)
    _install(base, "caveman@caveman", plugin)

    assert cp_mod.adopt() == []
    assert not (iso_mod.cli_config_dir() / cp_mod.STAGED_SUBDIR).exists()


def test_discover_is_unconditional_even_with_adoption_off(tmp_path):
    """/plugins has to be able to preview what turning the setting on
    would do -- discovery itself never checks the setting."""
    base = tmp_path / "real-claude"
    cache = tmp_path / "cache"
    plugin = _make_plugin(cache, "caveman", commands=2)
    _install(base, "caveman@caveman", plugin)

    assert cp_mod.adoption_enabled() is False
    assert len(cp_mod.discover(base=base)) == 1


# -- staging: the sanitized copy -------------------------------------------


def test_adopt_stages_commands_skills_and_agents_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DOXA_ADOPT_PLUGINS", "1")
    base = tmp_path / "real-claude"
    cache = tmp_path / "cache"
    plugin = _make_plugin(cache, "caveman", commands=2, skills=1, agents=1)
    _install(base, "caveman@caveman", plugin)

    result = cp_mod.adopt(cp_mod.discover(base=base))

    assert len(result) == 1
    assert result[0]["type"] == "local"
    staged = Path(result[0]["path"])
    assert (staged / "commands" / "cmd0.md").exists()
    assert (staged / "commands" / "cmd1.md").exists()
    assert (staged / "skills" / "skill0" / "SKILL.md").exists()
    assert (staged / "agents" / "agent0.md").exists()


def test_adopt_never_stages_hooks_directory_or_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DOXA_ADOPT_PLUGINS", "1")
    base = tmp_path / "real-claude"
    cache = tmp_path / "cache"
    plugin = _make_plugin(cache, "caveman", commands=1, hooks_file=True, hooks_key=True)
    _install(base, "caveman@caveman", plugin)

    [result] = cp_mod.adopt(cp_mod.discover(base=base))
    staged = Path(result["path"])

    assert not (staged / "hooks").exists()
    manifest = json.loads((staged / ".claude-plugin" / "plugin.json").read_text())
    assert "hooks" not in manifest


def test_adopt_never_stages_mcp_declarations(tmp_path, monkeypatch):
    monkeypatch.setenv("DOXA_ADOPT_PLUGINS", "1")
    base = tmp_path / "real-claude"
    cache = tmp_path / "cache"
    plugin = _make_plugin(cache, "github", commands=1, mcp_file=True, mcp_key=True)
    _install(base, "github@mp", plugin)

    [result] = cp_mod.adopt(cp_mod.discover(base=base))
    staged = Path(result["path"])

    assert not (staged / ".mcp.json").exists()
    manifest = json.loads((staged / ".claude-plugin" / "plugin.json").read_text())
    assert "mcpServers" not in manifest


def test_adopt_preserves_other_files_commands_reference(tmp_path, monkeypatch):
    """Copy-then-exclude, not a narrow commands/skills/agents whitelist --
    a command's own ${CLAUDE_PLUGIN_ROOT}/scripts/... reference (measured
    against the real codex plugin) must still resolve in the staged copy."""
    monkeypatch.setenv("DOXA_ADOPT_PLUGINS", "1")
    base = tmp_path / "real-claude"
    cache = tmp_path / "cache"
    plugin = _make_plugin(cache, "codex", commands=1, extra_script=True)
    _install(base, "codex@mp", plugin)

    [result] = cp_mod.adopt(cp_mod.discover(base=base))
    staged = Path(result["path"])

    assert (staged / "scripts" / "helper.sh").exists()


def test_adopt_never_stages_the_lore_plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("DOXA_ADOPT_PLUGINS", "1")
    base = tmp_path / "real-claude"
    cache = tmp_path / "cache"
    lore = _make_plugin(cache, "lore", commands=15)
    caveman = _make_plugin(cache, "caveman", commands=2)
    _install(base, "lore@lore", lore)
    _install(base, "caveman@caveman", caveman)

    result = cp_mod.adopt(cp_mod.discover(base=base))

    # Only caveman got staged -- lore is refused outright, enabled or not.
    assert len(result) == 1
    staged_names = [Path(r["path"]).name for r in result]
    assert not any(name.startswith("lore@") for name in staged_names)
    assert staged_names == ["caveman@caveman"]


def test_adopt_rebuilds_dropping_a_stale_hazard_from_a_previous_version(tmp_path, monkeypatch):
    """Rebuilt from scratch every call, same discipline as
    ensure_cli_config_dir's settings.json -- a hazard file left over from
    an older staged copy must not survive a rebuild."""
    monkeypatch.setenv("DOXA_ADOPT_PLUGINS", "1")
    base = tmp_path / "real-claude"
    cache = tmp_path / "cache"
    plugin = _make_plugin(cache, "caveman", commands=1)
    _install(base, "caveman@caveman", plugin)
    [discovered] = cp_mod.discover(base=base)

    dest = cp_mod.staged_plugin_dir(discovered)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "hooks").mkdir()
    (dest / "hooks" / "hooks.json").write_text("{}")

    cp_mod.adopt([discovered])

    assert not (dest / "hooks").exists()


# -- report() ----------------------------------------------------------


def test_report_says_adoption_is_off_by_default(tmp_path):
    text = cp_mod.report([])
    assert "adoption: OFF" in text
    assert "DOXA_ADOPT_PLUGINS" in text or "adopt claude plugins" in text


def test_report_lists_a_refused_plugin_with_its_reason(tmp_path):
    base = tmp_path / "real-claude"
    cache = tmp_path / "cache"
    plugin = _make_plugin(cache, "lore", commands=15)
    _install(base, "lore@lore", plugin, enabled=True)

    text = cp_mod.report(cp_mod.discover(base=base))

    assert "lore@lore" in text
    assert "lore_core" in text


def test_report_counts_adopted_only_when_the_setting_is_on(tmp_path, monkeypatch):
    base = tmp_path / "real-claude"
    cache = tmp_path / "cache"
    plugin = _make_plugin(cache, "caveman", commands=2)
    _install(base, "caveman@caveman", plugin)
    discovered = cp_mod.discover(base=base)

    off_report = cp_mod.report(discovered)
    assert "1 plugin(s) discovered, 0 adopted" in off_report

    monkeypatch.setenv("DOXA_ADOPT_PLUGINS", "1")
    on_report = cp_mod.report(discovered)
    assert "1 plugin(s) discovered, 1 adopted" in on_report


def test_report_would_adopt_tally_is_named_separately_from_refused(tmp_path):
    """Reported: the discovered plugins' commands are not available. One
    of the four candidate causes was "the setting is off, and /plugins
    does not say so plainly" -- it already named the setting, but its
    tally line folded an otherwise-good, merely-idle plugin into the same
    "refused" bucket a blocklisted/disabled one lands in, which answers
    the wrong question about WHY nothing was adopted."""
    base = tmp_path / "real-claude"
    cache = tmp_path / "cache"
    plugin = _make_plugin(cache, "caveman", commands=1)
    _install(base, "caveman@caveman", plugin)

    text = cp_mod.report(cp_mod.discover(base=base))
    assert "0 refused" in text
    assert "1 more would adopt if 'adopt claude plugins' were on" in text


# -- command_names() / adopted_commands(): the actual invocable spelling --
#
# The measured cause of the reported defect: a plugin loaded via
# --plugin-dir registers its commands NAMESPACED (<plugin>:<command-stem>)
# in the underlying claude CLI -- typing the bare stem a plugin's own docs
# advertise for its marketplace-installed form gets "Unknown command"
# back, even when the name is unique. Measured directly against a real
# adopted plugin on the machine this fix was written on (caveman's own
# /caveman command): bare "/caveman" -> "Unknown command: /caveman";
# namespaced "/caveman:caveman" -> the plugin's own prompt actually runs.


def _write_command_md(plugin_dir: Path, stem: str, *, description="", argument_hint=""):
    commands_dir = plugin_dir / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    if description:
        lines.append(f"description: {description}")
    if argument_hint:
        lines.append(f'argument-hint: "{argument_hint}"')
    lines += ["---", "body text the model reads when this command runs"]
    (commands_dir / f"{stem}.md").write_text("\n".join(lines), encoding="utf-8")


def test_command_names_uses_the_namespaced_invocable_form(tmp_path):
    base = tmp_path / "real-claude"
    cache = tmp_path / "cache"
    plugin_dir = cache / "caveman"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    _write_json(plugin_dir / ".claude-plugin" / "plugin.json", {"name": "caveman"})
    _write_command_md(
        plugin_dir, "caveman",
        description="Switch caveman intensity level",
        argument_hint="[lite|full|ultra|wenyan]",
    )
    _install(base, "caveman@caveman", plugin_dir)
    [found] = cp_mod.discover(base=base)

    [cmd] = cp_mod.command_names(found)
    assert cmd.invocable == "caveman:caveman"
    assert cmd.summary == "Switch caveman intensity level"
    assert cmd.argument_hint == "[lite|full|ultra|wenyan]"


def test_command_names_tolerates_a_command_file_with_no_front_matter(tmp_path):
    """_make_plugin's own fixture files (used by every OTHER test in this
    module) carry no front matter at all -- the parser must read that as
    "no description", never raise."""
    base = tmp_path / "real-claude"
    cache = tmp_path / "cache"
    plugin = _make_plugin(cache, "caveman", commands=1)
    _install(base, "caveman@caveman", plugin)
    [found] = cp_mod.discover(base=base)

    [cmd] = cp_mod.command_names(found)
    assert cmd.invocable == "caveman:cmd0"
    assert cmd.summary == ""
    assert cmd.argument_hint == ""


def test_adopted_commands_empty_while_the_setting_is_off(tmp_path):
    base = tmp_path / "real-claude"
    cache = tmp_path / "cache"
    plugin = _make_plugin(cache, "caveman", commands=2)
    _install(base, "caveman@caveman", plugin)

    assert cp_mod.adopted_commands(cp_mod.discover(base=base)) == []


def test_adopted_commands_excludes_the_blocklisted_plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("DOXA_ADOPT_PLUGINS", "1")
    base = tmp_path / "real-claude"
    cache = tmp_path / "cache"
    caveman = _make_plugin(cache, "caveman", commands=2)
    lore = _make_plugin(cache, "lore", commands=15)
    _install(base, "caveman@caveman", caveman)
    _install(base, "lore@lore", lore)

    rows = cp_mod.adopted_commands(cp_mod.discover(base=base))
    invocables = sorted(command.invocable for _plugin, command in rows)
    assert invocables == ["caveman:cmd0", "caveman:cmd1"]


def test_report_lists_the_exact_invocable_spelling(tmp_path):
    """The discoverability half of the fix: /plugins has to print the
    spelling a user can actually type, not just a count -- "2 commands"
    tells nobody what to type."""
    base = tmp_path / "real-claude"
    cache = tmp_path / "cache"
    plugin_dir = cache / "caveman"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    _write_json(plugin_dir / ".claude-plugin" / "plugin.json", {"name": "caveman"})
    _write_command_md(plugin_dir, "caveman", description="Switch intensity")
    _install(base, "caveman@caveman", plugin_dir)

    text = cp_mod.report(cp_mod.discover(base=base))
    assert "/caveman:caveman" in text
    assert "Switch intensity" in text
