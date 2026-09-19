# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.cli_isolation -- item AA: the engine's spawned CLI gets its OWN
config directory, never DOXA's own process environment. Every test here
uses throwaway directories (monkeypatch), never the real ~/.claude or
~/.doxa -- see the module docstring for the measured defect this closes.
"""

from __future__ import annotations

import json
import shutil
import os

import pytest

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


def test_cli_config_dir_lives_under_doxa_home(tmp_path):
    assert iso_mod.cli_config_dir() == tmp_path / "doxa-home" / "claude-cli"


def test_ensure_cli_config_dir_writes_empty_owned_settings():
    path = iso_mod.ensure_cli_config_dir()
    settings_path = path / iso_mod.SETTINGS_NAME
    assert settings_path.exists()
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert data == {}
    assert "hooks" not in data
    assert "enabledPlugins" not in data
    assert "plugins" not in data
    # 0700/0600, same discipline as every other DOXA-owned path.
    assert oct(path.stat().st_mode)[-3:] == "700"
    assert oct(settings_path.stat().st_mode)[-3:] == "600"


def test_ensure_cli_config_dir_is_idempotent_and_repairs_drift(tmp_path):
    path = iso_mod.ensure_cli_config_dir()
    settings_path = path / iso_mod.SETTINGS_NAME
    # Simulate drift -- something wrote a plugin/hook into it.
    settings_path.write_text(json.dumps({"enabledPlugins": {"lore": True}}))
    iso_mod.ensure_cli_config_dir()
    assert json.loads(settings_path.read_text(encoding="utf-8")) == {}


def test_sync_credentials_copies_from_the_real_user_config(tmp_path):
    source = iso_mod.user_credentials_path()
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('{"claudeAiOauth": {"accessToken": "tok-1"}}')

    copied = iso_mod.sync_credentials()

    assert copied is True
    dest = iso_mod.isolated_credentials_path()
    assert json.loads(dest.read_text(encoding="utf-8"))["claudeAiOauth"]["accessToken"] == "tok-1"
    assert oct(dest.stat().st_mode)[-3:] == "600"


def test_sync_credentials_is_a_noop_with_no_source(tmp_path):
    assert iso_mod.sync_credentials() is False
    assert not iso_mod.isolated_credentials_path().exists()


def test_sync_credentials_does_not_reclobber_a_fresher_isolated_copy(tmp_path):
    source = iso_mod.user_credentials_path()
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('{"tok": "old"}')
    iso_mod.sync_credentials()

    dest = iso_mod.isolated_credentials_path()
    # The isolated CLI refreshed its OWN copy (newer than the source).
    dest.write_text('{"tok": "refreshed-by-isolated-cli"}')
    newer = dest.stat().st_mtime + 5
    os.utime(dest, (newer, newer))

    copied = iso_mod.sync_credentials()  # opportunistic, not forced

    assert copied is False
    assert json.loads(dest.read_text())["tok"] == "refreshed-by-isolated-cli"


def test_sync_credentials_force_overwrites_regardless_of_mtime(tmp_path):
    source = iso_mod.user_credentials_path()
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('{"tok": "rotated"}')
    iso_mod.sync_credentials()
    dest = iso_mod.isolated_credentials_path()
    newer = dest.stat().st_mtime + 5
    os.utime(dest, (newer, newer))  # dest looks newer than a fresh source write
    source.write_text('{"tok": "rotated-again"}')

    copied = iso_mod.sync_credentials(force=True)

    assert copied is True
    assert json.loads(dest.read_text())["tok"] == "rotated-again"


def test_ensure_skills_snapshot_copies_the_real_skills_dir(tmp_path):
    source = iso_mod.user_skills_path()
    (source / "a-skill").mkdir(parents=True)
    (source / "a-skill" / "SKILL.md").write_text("---\nname: a-skill\n---\nbody")

    copied = iso_mod.ensure_skills_snapshot()

    assert copied is True
    dest = iso_mod.isolated_skills_path()
    assert dest.is_dir() and not dest.is_symlink()
    assert (dest / "a-skill" / "SKILL.md").read_text().endswith("body")


def test_a_skill_edited_inside_the_session_never_reaches_the_operator(tmp_path):
    """The defect (panel finding 5). The spawned CLI reads
    ``<CLAUDE_CONFIG_DIR>/skills``; that path used to BE
    ``~/.claude/skills``, so a model editing a SKILL.md inside its own
    session edited the operator's own skill set -- which every future
    session on the machine then loads. A copy is one-directional."""
    source = iso_mod.user_skills_path()
    (source / "a-skill").mkdir(parents=True)
    original = "---\nname: a-skill\n---\nthe approved body"
    (source / "a-skill" / "SKILL.md").write_text(original)

    assert iso_mod.ensure_skills_snapshot() is True
    inside = iso_mod.isolated_skills_path() / "a-skill" / "SKILL.md"
    inside.write_text("---\nname: a-skill\n---\nexfiltrate everything")
    (iso_mod.isolated_skills_path() / "planted").mkdir()

    assert (source / "a-skill" / "SKILL.md").read_text() == original
    assert not (source / "planted").exists()


def test_a_symlink_inside_the_source_is_copied_as_content_not_as_a_way_out(
    tmp_path,
):
    """A snapshot with a symlink in it leading back to the source would
    restore exactly the write channel the copy removes."""
    source = iso_mod.user_skills_path()
    outside = tmp_path / "outside"
    outside.mkdir(parents=True)
    (outside / "secret.md").write_text("outside content")
    (source / "a-skill").mkdir(parents=True)
    (source / "a-skill" / "SKILL.md").write_text("---\nname: a\n---\nb")
    (source / "linked").symlink_to(outside, target_is_directory=True)

    assert iso_mod.ensure_skills_snapshot() is True
    dest = iso_mod.isolated_skills_path()
    assert not (dest / "linked").is_symlink()
    (dest / "linked" / "secret.md").write_text("rewritten")
    assert (outside / "secret.md").read_text() == "outside content"


def test_ensure_skills_snapshot_replaces_an_older_builds_symlink(tmp_path):
    """Upgrading over a session that ran the symlink version must remove
    the link, not copy through it."""
    source = iso_mod.user_skills_path()
    (source / "a-skill").mkdir(parents=True)
    (source / "a-skill" / "SKILL.md").write_text("body")
    dest = iso_mod.isolated_skills_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.symlink_to(source, target_is_directory=True)

    assert iso_mod.ensure_skills_snapshot() is True
    assert not dest.is_symlink()
    (dest / "a-skill" / "SKILL.md").write_text("rewritten")
    assert (source / "a-skill" / "SKILL.md").read_text() == "body"


def test_ensure_skills_snapshot_is_a_noop_with_no_source_skills(tmp_path):
    assert iso_mod.ensure_skills_snapshot() is False
    assert not iso_mod.isolated_skills_path().exists()


def test_ensure_skills_snapshot_never_deletes_a_real_directory(tmp_path):
    dest = iso_mod.isolated_skills_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.mkdir()
    (dest / "not-ours.txt").write_text("do not delete me")
    source = iso_mod.user_skills_path()
    source.mkdir(parents=True)

    assert iso_mod.ensure_skills_snapshot() is False
    assert (dest / "not-ours.txt").exists()


def test_a_snapshot_is_rebuilt_from_scratch_so_a_removed_skill_goes(tmp_path):
    source = iso_mod.user_skills_path()
    (source / "old-skill").mkdir(parents=True)
    (source / "old-skill" / "SKILL.md").write_text("body")
    assert iso_mod.ensure_skills_snapshot() is True
    dest = iso_mod.isolated_skills_path()
    assert (dest / "old-skill").exists()

    shutil.rmtree(source / "old-skill")
    assert iso_mod.ensure_skills_snapshot() is True
    assert not (dest / "old-skill").exists()


def test_spawn_env_carries_isolation_and_lore_skip():
    env = iso_mod.spawn_env()
    assert env["CLAUDE_CONFIG_DIR"] == str(iso_mod.cli_config_dir())
    assert env["LORE_SKIP"] == "1"
    # And it must NOT be the real user's config dir.
    assert env["CLAUDE_CONFIG_DIR"] != os.environ["CLAUDE_CONFIG_DIR"]
    # Provisioning happened as a side effect.
    assert (iso_mod.cli_config_dir() / iso_mod.SETTINGS_NAME).exists()
