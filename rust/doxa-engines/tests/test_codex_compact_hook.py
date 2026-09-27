"""Pinned native PreCompact contracts; no LORE stores or provider calls."""
import hashlib
import importlib.util
import json
from pathlib import Path
from unittest import mock
import pytest

PATH = Path(__file__).parents[1] / "codex_compact_hook.py"
spec = importlib.util.spec_from_file_location("codex_compact_hook", PATH)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


def fixture(tmp_path, archived=False):
    home = tmp_path / "codex"
    sessions = home / ("archived_sessions" if archived else "sessions")
    sessions.mkdir(parents=True)
    source = sessions / "rollout.jsonl"
    rows = [{"type":"session_meta","payload":{"id":"provider-thread","cwd":str(tmp_path)}}]
    for role in ["user", "assistant"] * 3:
        rows.append({"type":"response_item","payload":{"type":"message","role":role,
                     "content":[{"type":"input_text" if role == "user" else "output_text","text":"fixture visible"}]}})
    rows.append({"type":"response_item","payload":{"type":"custom_tool_call","name":"apply_patch","input":"fixture patch"}})
    source.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"version":"0.156.1","provider_thread":"provider-thread",
                                   "doxa_session":"doxa-session","codex_home":str(home),"cwd":str(tmp_path)}))
    event = {"hook_event_name":"PreCompact","trigger":"auto","session_id":"provider-thread",
             "transcript_path":str(source)}
    return manifest, source, event


@pytest.mark.parametrize('archived', [False, True])
def test_review_carries_exact_raw_proof_without_importing_python_lore(tmp_path, monkeypatch, archived):
    monkeypatch.delenv("LORE_DISABLE_REVIEW", raising=False)
    monkeypatch.delenv("LORE_SKIP", raising=False)
    manifest, source, event = fixture(tmp_path, archived)
    observed = []
    def worker(metadata):
        observed.append(metadata)
        assert metadata['transcript'] == str(source)
        assert metadata['session_id'] == 'doxa-session'
        assert metadata['provider_thread'] == 'provider-thread'
        assert metadata['older'] is True
        assert 'agent' not in metadata and 'source_engine' not in metadata
        proof = metadata['expected_source']; st = source.stat()
        assert proof == {'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
                         'inode':st.st_ino,'device':st.st_dev,'size':st.st_size,
                         'ctime':st.st_ctime_ns//1_000_000_000,'ctime_nsec':st.st_ctime_ns%1_000_000_000}
        assert 'apply_patch' in source.read_text()
        return True
    import builtins
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        assert name != 'lore_core' and not name.startswith('lore_core.')
        assert name != 'doxa' and not name.startswith('doxa.')
        return original_import(name, *args, **kwargs)
    with mock.patch.object(builtins, '__import__', guarded_import):
        assert hook.review(manifest, event, worker=worker)
    assert len(observed) == 1
    assert not hook.review(manifest, event, worker=lambda *_: False)
    def changing_worker(_):
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
    rows = source.read_text().splitlines(); rows[0] = json.dumps({'type':'session_meta','payload':{'id':'different'}})
    source.write_text('\n'.join(rows))
    assert not hook.review(manifest, event, worker=lambda *_: True)


def test_worker_timeout_closes_supervisor_control_and_waits(monkeypatch):
    class Process:
        pid = 123
        def __init__(self):
            self.waits = 0
            self.stdin = mock.Mock()
        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1: raise hook.subprocess.TimeoutExpired("fixture", timeout)
            return -9
    process = Process(); calls = []
    monkeypatch.setattr(hook.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(hook.os, "killpg", lambda *a: calls.append(a))
    monkeypatch.setattr(hook, "REVIEW_SUPERVISOR_SOURCE", "verified source", raising=False)
    assert not hook.run_worker({'session_id':'fixture'}, timeout=0.01)
    assert calls == []
    process.stdin.close.assert_called_once()
    assert process.waits == 2


def test_worker_without_verified_supervisor_or_bounded_metadata_cannot_launch(monkeypatch):
    monkeypatch.delattr(hook, "REVIEW_SUPERVISOR_SOURCE", raising=False)
    with mock.patch.object(hook.subprocess, "Popen") as launch:
        assert not hook.run_worker({})
        monkeypatch.setattr(hook, "REVIEW_SUPERVISOR_SOURCE", "verified source", raising=False)
        assert not hook.run_worker({'transcript':'x'*16384})
        assert not hook.run_worker('fixture')
        launch.assert_not_called()


@pytest.mark.parametrize('variable,value', [('LORE_DISABLE_REVIEW','1'),('LORE_SKIP','1')])
def test_review_disabled_blocks_without_worker(tmp_path, monkeypatch, variable, value):
    manifest, source, event = fixture(tmp_path)
    monkeypatch.setenv(variable, value)
    worker = mock.Mock()
    assert not hook.review(manifest, event, worker=worker)
    worker.assert_not_called()


def test_explicit_review_zero_keeps_native_review_enabled(tmp_path, monkeypatch):
    manifest, source, event = fixture(tmp_path)
    monkeypatch.setenv('LORE_DISABLE_REVIEW','0');monkeypatch.delenv('LORE_SKIP',raising=False)
    assert hook.review(manifest,event,worker=lambda _:True)


def test_memory_off_blocks_without_worker(tmp_path):
    manifest, source, event = fixture(tmp_path)
    data = json.loads(manifest.read_text()); data['lore_enabled'] = False
    manifest.write_text(json.dumps(data))
    worker = mock.Mock()
    assert not hook.review(manifest, event, worker=worker)
    worker.assert_not_called()


def test_oversized_rollout_line_blocks_before_native_worker(tmp_path):
    manifest, source, event = fixture(tmp_path)
    with source.open('a') as output:
        output.write('x' * (hook.MAX_LINE + 1) + '\n')
    worker = mock.Mock()
    assert not hook.review(manifest, event, worker=worker)
    worker.assert_not_called()
