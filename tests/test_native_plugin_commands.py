# SPDX-License-Identifier: AGPL-3.0-only
"""Fresh native palette inventory reads only disposable Claude plugin files."""
import json
import os
from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).resolve().parents[1] / "rust/doxa-claude/claude_sidecar.py"


def installed(tmp_path):
    base = tmp_path / "claude"
    (base / "plugins").mkdir(parents=True)
    plugins = {}
    for name in ("fixture@test", "lore@lore", "disabled@test"):
        source = tmp_path / name
        (source / "commands").mkdir(parents=True)
        (source / ".claude-plugin").mkdir()
        (source / ".claude-plugin/plugin.json").write_text(json.dumps({"name":name.split("@")[0]}))
        (source / "commands/task.md").write_text("---\ndescription: fixture command\nargument-hint: '[value]'\n---\nbody\n")
        plugins[name] = [{"scope":"user","installPath":str(source),"version":"1"}]
    (base / "plugins/installed_plugins.json").write_text(json.dumps({"version":2,"plugins":plugins}))
    (base / "settings.json").write_text(json.dumps({"enabledPlugins":{
        "fixture@test":True,"lore@lore":True,"disabled@test":False}}))
    return base


def inventory(tmp_path, base, enabled):
    env = {"PATH":os.environ.get("PATH",""), "HOME":str(tmp_path),
           "DOXA_HOME":str(tmp_path / "doxa"),"CLAUDE_CONFIG_DIR":str(base),
           "DOXA_ADOPT_PLUGINS":"1" if enabled else "0",
           "LORE_ROOT":str(tmp_path / "lore"),
           "PYTHONPATH":str(SCRIPT.parents[2])}
    return subprocess.run([sys.executable,str(SCRIPT),"--plugin-commands"],
        env=env,text=True,capture_output=True,timeout=15)


def test_native_plugin_commands_follow_canonical_adoption_without_staging(tmp_path):
    base = installed(tmp_path)
    source = tmp_path / "fixture@test/commands"
    (source / "unsafe name.md").write_text("---\ndescription: must not be offered\n---\n")
    (source / "ansi.md").write_text("---\ndescription: unsafe\x1b[31m text\n---\n")
    original = {str(file):file.read_bytes() for file in tmp_path.rglob("*") if file.is_file()}
    off = inventory(tmp_path,base,False)
    assert off.returncode == 0 and json.loads(off.stdout) == []
    on = inventory(tmp_path,base,True)
    assert on.returncode == 0, on.stdout
    assert json.loads(on.stdout) == [{"name":"/fixture:task","summary":"fixture command",
        "usage":"/fixture:task [value]","plugin":"fixture"}]
    assert {str(file):file.read_bytes() for file in tmp_path.rglob("*") if file.is_file()} == original
    assert not (tmp_path / "doxa").exists()
    assert not (tmp_path / "lore").exists()
    (source / "task.md").write_text("---\ndescription: changed command\n---\nbody\n")
    changed = inventory(tmp_path,base,True)
    assert json.loads(changed.stdout)[0]["summary"] == "changed command"
    assert json.loads(changed.stdout)[0]["usage"] == ""


def test_native_plugin_command_inventory_refuses_oversized_roster_generically(tmp_path):
    base = installed(tmp_path)
    source = tmp_path / "fixture@test/commands"
    for index in range(101):
        (source / f"extra-{index}.md").write_text("---\ndescription: private fixture text\n---\n")
    result = inventory(tmp_path,base,True)
    assert result.returncode == 1
    assert result.stdout == "Plugin command inventory failed; verify the installed DOXA Python/LORE dependencies.\n"
    assert "private fixture text" not in result.stdout + result.stderr
    assert not (tmp_path / "doxa").exists()
