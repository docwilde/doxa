"""Pinned PreCompact review contract tests; no real LORE reviewers/providers."""
import importlib.util
import json
from pathlib import Path
import sys
import types
from unittest import mock
import pytest

PATH = Path(__file__).parents[1] / "codex_compact_hook.py"
spec = importlib.util.spec_from_file_location("codex_compact_hook", PATH)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


def fixture(tmp_path):
    home = tmp_path / "codex"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    source = sessions / "rollout.jsonl"
    rows = [{"type":"session_meta","payload":{"id":"provider-thread"}}]
    for role in ["user", "assistant"] * 3:
        rows.append({"type":"response_item","payload":{"type":"message","role":role,
                     "content":[{"type":"input_text" if role == "user" else "output_text","text":"SECRET visible"}]}})
    source.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"version":"0.156.1","provider_thread":"provider-thread",
                                   "doxa_session":"doxa-session","codex_home":str(home),"cwd":str(tmp_path)}))
    event = {"hook_event_name":"PreCompact","trigger":"auto","session_id":"provider-thread",
             "transcript_path":str(source)}
    return manifest, source, event


def fake_lore(observed):
    lore = types.ModuleType("lore_core")
    deriver = types.ModuleType("lore_core.deriver")
    deriver.__file__ = "/fixture/lore_core/deriver.py"
    def build(path, slug, **options):
        observed.append(path.read_text())
        return {"session_id":path.stem,"project":slug,"prompt":"fixture"}
    deriver.build_review_job = build
    lore.deriver = deriver
    config = types.ModuleType("lore_core.config")
    config.project_slug = lambda cwd: "project"
    config.stage_disabled = lambda stage: False
    scrub = types.ModuleType("lore_core.scrub")
    scrub.scrub_secrets = lambda text: text.replace("SECRET", "[redacted]")
    doxa = types.ModuleType("doxa")
    bootstrap = types.ModuleType("doxa._lore_bootstrap")
    bootstrap.ensure_importable = lambda: observed.append("bootstrap-source")
    bootstrap.export_sticky_lore_root = lambda: observed.append("bootstrap-store")
    doxa._lore_bootstrap = bootstrap
    return {"doxa":doxa,"doxa._lore_bootstrap":bootstrap,"lore_core":lore,"lore_core.deriver":deriver,"lore_core.config":config,"lore_core.scrub":scrub}


def test_review_scrubs_pinned_rollout_before_worker_and_refuses_change(tmp_path, monkeypatch):
    monkeypatch.delenv("LORE_DISABLE_REVIEW", raising=False)
    manifest, source, event = fixture(tmp_path)
    observed = []
    with mock.patch.dict(sys.modules, fake_lore(observed)):
        assert hook.review(manifest, event, worker=lambda *_: True)
        assert observed[:2] == ["bootstrap-source", "bootstrap-store"]
        assert "SECRET" not in observed[2]
        assert "[redacted]" in observed[2]
        assert not hook.review(manifest, event, worker=lambda *_: False)
        def changing_worker(*_):
            source.write_text(source.read_text() + '{}\n')
            return True
        assert not hook.review(manifest, event, worker=changing_worker)


def test_review_rejects_wrong_identity_unknown_version_and_outside_source(tmp_path):
    manifest, source, event = fixture(tmp_path)
    assert not hook.review(manifest, {**event,"session_id":"different"}, worker=lambda *_: True)
    data = json.loads(manifest.read_text()); data["version"] = "unknown"; manifest.write_text(json.dumps(data))
    assert not hook.review(manifest, event, worker=lambda *_: True)
    data["version"] = "0.156.1"; manifest.write_text(json.dumps(data))
    outside = tmp_path / "outside.jsonl"; outside.write_text(source.read_text())
    assert not hook.review(manifest, {**event,"transcript_path":str(outside)}, worker=lambda *_: True)
    link = source.parent / "link.jsonl"; link.symlink_to(source)
    assert not hook.review(manifest, {**event,"transcript_path":str(link)}, worker=lambda *_: True)


def test_worker_timeout_kills_and_reaps_process_group(monkeypatch):
    class Process:
        pid = 123
        def __init__(self): self.waits = 0
        def wait(self, timeout=None):
            self.waits += 1
            if timeout is not None: raise hook.subprocess.TimeoutExpired("fixture", timeout)
            return -9
    process = Process(); calls = []
    monkeypatch.setattr(hook.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(hook.os, "killpg", lambda *a: calls.append(a))
    assert not hook.run_worker(Path("fixture"), Path("fixture"), timeout=0.01)
    assert calls == [(123, hook.signal.SIGKILL)]
    assert process.waits == 2
