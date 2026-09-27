# SPDX-License-Identifier: AGPL-3.0-only
"""Native one-shot inventory uses canonical sanitized adoption, no SDK/provider."""
import importlib.util
import json
from pathlib import Path

from doxa import claude_plugins, config


def test_native_inventory_reports_then_rebuilds_only_opted_in_sanitized_plugins(tmp_path, monkeypatch):
    home = tmp_path / "home"
    base = tmp_path / "claude"
    base.mkdir()
    monkeypatch.setenv("DOXA_HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base))
    monkeypatch.setenv("DOXA_ADOPT_PLUGINS", "0")
    config.invalidate()
    installs = {}
    originals = {}
    for name in ("fixture@test", "lore@lore", "disabled@test"):
        source = tmp_path / name
        (source / "commands").mkdir(parents=True)
        (source / "commands/task.md").write_text("first")
        (source / ".claude-plugin").mkdir()
        manifest = {"name": name.split("@")[0], "description": "fixture api_key=sk-" + "a" * 48 + "\x1b[31m", "hooks": {"SessionStart": []}, "mcpServers": {"bad": {"command": "never-run"}}}
        (source / ".claude-plugin/plugin.json").write_text(json.dumps(manifest))
        (source / "hooks").mkdir()
        (source / "hooks/hooks.json").write_text("{}")
        (source / ".mcp.json").write_text("{}")
        installs[name] = [{"scope": "user", "installPath": str(source), "version": "1"}]
        originals[name] = (source / ".claude-plugin/plugin.json").read_bytes()
    (base / "plugins").mkdir()
    (base / "plugins/installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": installs}))
    (base / "settings.json").write_text(json.dumps({"enabledPlugins": {"fixture@test": True, "lore@lore": True, "disabled@test": False}}))
    settings = (base / "settings.json").read_bytes()
    path = Path(__file__).parents[1] / "rust/doxa-claude/claude_sidecar.py"
    spec = importlib.util.spec_from_file_location("native_plugin_sidecar", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        report = module.plugin_inventory(False)
        assert "adoption: OFF" in report and "fixture@test" in report
        assert "sk-" + "a" * 48 not in report and "\x1b" not in report
        assert "re-staged 0" in module.plugin_inventory(True)
        assert not home.exists() or not list(home.rglob("task.md"))
        monkeypatch.setenv("DOXA_ADOPT_PLUGINS", "1")
        config.invalidate()
        report = module.plugin_inventory(True)
        assert "re-staged 1" in report and "NEW sessions" in report
        staged = claude_plugins.staged_plugin_dir(next(p for p in claude_plugins.discover() if p.scope_key == "fixture@test"))
        assert (staged / "commands/task.md").read_text() == "first"
        assert not (staged / "hooks").exists() and not (staged / ".mcp.json").exists()
        manifest = json.loads((staged / ".claude-plugin/plugin.json").read_text())
        assert "hooks" not in manifest and "mcpServers" not in manifest
        (tmp_path / "fixture@test/commands/task.md").write_text("changed")
        (staged / "hooks").mkdir()
        module.plugin_inventory(True)
        assert (staged / "commands/task.md").read_text() == "changed"
        assert not (staged / "hooks").exists()
        assert (base / "settings.json").read_bytes() == settings
        for name, original in originals.items():
            assert (tmp_path / name / ".claude-plugin/plugin.json").read_bytes() == original
    finally:
        config.invalidate()
