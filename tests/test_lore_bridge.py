"""The Rust LORE sidecar wire must never echo untrusted exception text."""

import io
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import types
from pathlib import Path

import pytest

from doxa import lore_bridge


def test_reviewed_resolve_with_new_lore_source_rejects_one_exact_snapshot(tmp_path):
    source = os.environ.get("DOXA_TEST_LORE_API_PATH")
    if not source:
        pytest.skip("set DOXA_TEST_LORE_API_PATH to a checkout with the reviewed resolution API")
    lore_source = Path(source)
    if not (lore_source / "lore_core" / "pending.py").is_file():
        pytest.skip("local LORE API checkout unavailable")
    root = tmp_path / "lore"
    pending = root / "pending"
    pending.mkdir(parents=True)
    proposal = pending / "one.json"
    raw = b'{"kind":"memory","scope":"user","text":"reviewed"}\n'
    proposal.write_bytes(raw)
    expected = {"sha256": hashlib.sha256(raw).hexdigest(), "inode": proposal.stat().st_ino}
    requests = [
        {"id": 1, "op": "pending_review_v1", "cwd": str(tmp_path), "pid": "one"},
        {"id": 2, "op": "resolve_reviewed_v1", "cwd": str(tmp_path), "pid": "one",
         "decision": "approve", "expected": {**expected, "sha256": "0" * 64}},
        {"id": 3, "op": "resolve_reviewed_v1", "cwd": str(tmp_path), "pid": "one",
         "decision": "reject", "expected": expected},
    ]
    env = dict(os.environ, LORE_ROOT=str(root), DOXA_LORE_CORE_PATH=str(lore_source))
    result = subprocess.run([sys.executable, "-m", "doxa.lore_bridge"],
                            input=b"".join(map(lore_bridge._frame, requests)),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=Path(__file__).resolve().parent.parent, env=env,
                            timeout=10, check=True)
    frames = [json.loads(line) for line in result.stdout.splitlines()]
    assert "resolve_reviewed_v1" in frames[0]["capabilities"]
    assert frames[1]["value"]["sha256"] == expected["sha256"]
    assert frames[2]["error"] == "pending_changed"
    assert frames[3]["value"] == {"status": "rejected"}
    assert not proposal.exists()
    assert len(list((pending / "archive").glob("*.json"))) == 1


def test_old_lore_review_stays_read_only_without_atomic_resolver(monkeypatch):
    monkeypatch.setattr(lore_bridge, "_lore", lambda: (lambda text: text, lambda cwd, scope: ""))
    monkeypatch.setattr(lore_bridge, "_extensions", lambda: (
        lambda cwd: "project", lambda: None, lambda: [], (lambda text: text, lambda: None)))
    monkeypatch.setattr(lore_bridge, "_pending_review_reader", lambda: (Path("/unused"), lambda pid: None))
    monkeypatch.setattr(lore_bridge, "_pending_resolver", lambda: None)
    output = io.BytesIO()
    monkeypatch.setattr(lore_bridge.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO()))
    monkeypatch.setattr(lore_bridge.sys, "stdout", types.SimpleNamespace(buffer=output))
    lore_bridge.serve()
    capabilities = json.loads(output.getvalue())["capabilities"]
    assert "pending_review_v1" in capabilities
    assert "resolve_reviewed_v1" not in capabilities


def test_partial_archive_failure_is_returned_without_retry_or_item_text(monkeypatch):
    checked = {"pid": "one", "raw": '{"secret":"not for status"}',
               "sha256": "a" * 64, "inode": 7, "complete": True}
    monkeypatch.setattr(lore_bridge, "_pending_review", lambda *args: checked)
    calls = []

    class ResolutionError(Exception):
        code = "archive_failed"
        applied = True

    def resolve(*args):
        calls.append(("resolve", args))
        raise ResolutionError()

    result = lore_bridge._resolve_reviewed(
        {"cwd": "/repo", "pid": "one", "decision": "approve",
         "expected": {"sha256": "a" * 64, "inode": 7}}, (), (),
        (lambda *args: calls.append(("record", args)), resolve, ResolutionError))
    assert result == {"status": "refused", "error": "archive_failed", "applied": True}
    assert [name for name, _ in calls] == ["record", "resolve"]
    assert "not for status" not in str(result)


@pytest.fixture(autouse=True)
def no_optional_read_store(monkeypatch):
    monkeypatch.setattr(lore_bridge, "_read_ops", lambda: None)
    monkeypatch.setattr(lore_bridge, "_index_ops", lambda: None)
    monkeypatch.setattr(lore_bridge, "_pending_review_reader", lambda: None)


def test_index_transcript_is_capability_gated_and_lore_scoped(monkeypatch, tmp_path):
    project = tmp_path / "mapped-project"
    project.mkdir()
    transcript = project / "session-1.jsonl"
    transcript.write_text('{"type":"user","message":{"content":"safe"}}\n')
    seen = []
    monkeypatch.setattr(lore_bridge, "_lore", lambda: (lambda text: text, lambda cwd, scope: ""))
    monkeypatch.setattr(lore_bridge, "_extensions", lambda: (
        lambda cwd: "mapped-project", lambda: None, lambda: [],
        (lambda text: text, lambda: None)))
    monkeypatch.setattr(lore_bridge, "_transcript_identity", lambda cwd, ext: {
        "projects_dir": str(tmp_path), "slug": ext[0](cwd)})
    connection = types.SimpleNamespace(close=lambda: seen.append("closed"))
    monkeypatch.setattr(lore_bridge, "_index_ops", lambda: (
        lambda: connection, lambda conn, fd, path:
        (seen.append((conn, path, os.fstat(fd).st_ino)) or (1, 1))))
    requests = [
        {"id": 1, "op": "index_transcript_v1", "cwd": "/repo", "session_id": "session-1"},
        {"id": 2, "op": "index_transcript_v1", "cwd": "/repo", "session_id": "../secret"},
    ]
    output = io.BytesIO()
    monkeypatch.setattr(lore_bridge.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(
        b"".join(map(lore_bridge._frame, requests)))))
    monkeypatch.setattr(lore_bridge.sys, "stdout", types.SimpleNamespace(buffer=output))
    lore_bridge.serve()
    frames = [json.loads(line) for line in output.getvalue().splitlines()]
    assert "index_transcript_v1" in frames[0]["capabilities"]
    assert frames[1]["value"] == {"indexed": 1, "consumed": 1}
    assert seen == [(connection, transcript, transcript.stat().st_ino), "closed"]
    assert frames[2]["error"] == "operation_failed"


def test_index_transcript_rejects_symlink_and_foreign_path(monkeypatch, tmp_path):
    project = tmp_path / "mapped-project"
    project.mkdir()
    outside = tmp_path / "other.jsonl"
    outside.write_text("private")
    (project / "session-1.jsonl").symlink_to(outside)
    monkeypatch.setattr(lore_bridge, "_transcript_identity", lambda cwd, ext: {
        "projects_dir": str(tmp_path), "slug": "mapped-project"})
    called = []
    with pytest.raises((ValueError, OSError)):
        lore_bridge._index_transcript("/repo", "session-1", (),
                                       (lambda: None, lambda conn, fd, path: called.append(path)))
    assert called == []


def test_index_transcript_rejects_world_writable_project_directory(monkeypatch, tmp_path):
    project = tmp_path / "mapped-project"
    project.mkdir()
    (project / "session-1.jsonl").write_text('{"type":"user"}\n')
    project.chmod(0o777)
    monkeypatch.setattr(lore_bridge, "_transcript_identity", lambda cwd, ext: {
        "projects_dir": str(tmp_path), "slug": "mapped-project"})
    called = []
    with pytest.raises(ValueError):
        lore_bridge._index_transcript("/repo", "session-1", (),
                                       (lambda: None, lambda conn, fd, path: called.append(path)))
    assert called == []


def test_index_transcript_reads_pinned_inode_after_path_swap(monkeypatch, tmp_path):
    project = tmp_path / "mapped-project"
    project.mkdir()
    transcript = project / "session-1.jsonl"
    transcript.write_text("safe original\n")
    outside = tmp_path / "outside.jsonl"
    outside.write_text("secret replacement\n")
    monkeypatch.setattr(lore_bridge, "_transcript_identity", lambda cwd, ext: {
        "projects_dir": str(tmp_path), "slug": "mapped-project"})
    seen = []

    def index(conn, fd, logical_path):
        transcript.rename(project / "moved.jsonl")
        transcript.symlink_to(outside)
        with os.fdopen(os.dup(fd), encoding="utf-8") as source:
            seen.append((source.read(), logical_path))
        return 1, 1

    assert lore_bridge._index_transcript("/repo", "session-1", (),
                                         (lambda: types.SimpleNamespace(close=lambda: None), index)) == {"indexed": 1, "consumed": 1}
    assert seen == [("safe original\n", transcript)]


def test_index_transcript_rejects_symlinked_project_directory(monkeypatch, tmp_path):
    real_project = tmp_path / "real"
    real_project.mkdir()
    (real_project / "session-1.jsonl").write_text("safe\n")
    (tmp_path / "mapped-project").symlink_to(real_project, target_is_directory=True)
    monkeypatch.setattr(lore_bridge, "_transcript_identity", lambda cwd, ext: {
        "projects_dir": str(tmp_path), "slug": "mapped-project"})
    with pytest.raises(OSError):
        lore_bridge._index_transcript("/repo", "session-1", (),
                                       (lambda: None, lambda conn, fd, path: (1, 1)))


def test_index_transcript_closes_connection_and_fd_on_index_failure(monkeypatch, tmp_path):
    project = tmp_path / "mapped-project"
    project.mkdir()
    (project / "session-1.jsonl").write_text("safe\n")
    monkeypatch.setattr(lore_bridge, "_transcript_identity", lambda cwd, ext: {
        "projects_dir": str(tmp_path), "slug": "mapped-project"})
    seen = []

    def fail(conn, fd, path):
        seen.append(fd)
        raise RuntimeError("index failed")

    conn = types.SimpleNamespace(close=lambda: seen.append("closed"))
    with pytest.raises(RuntimeError):
        lore_bridge._index_transcript("/repo", "session-1", (), (lambda: conn, fail))
    assert seen[1] == "closed"
    with pytest.raises(OSError):
        os.fstat(seen[0])


def test_pending_review_v1_uses_lore_snapshot_and_rejects_changed_proposal(monkeypatch, tmp_path):
    from lore_core import pending as pending_mod

    pending_dir = tmp_path / "pending"
    pending_dir.mkdir()
    proposal = pending_dir / "one.json"
    raw = b'{"scope":"project","project":"this","kind":"sync","op":{"payload":"all bytes"}}\n'
    proposal.write_bytes(raw)
    monkeypatch.setattr(pending_mod, "ROOT", tmp_path)
    monkeypatch.setattr(lore_bridge, "_pending_review_reader",
                        lambda: (tmp_path, pending_mod._pending_bytes_snapshot))
    monkeypatch.setattr(lore_bridge, "_lore", lambda: (lambda text: text, lambda cwd, scope: ""))
    monkeypatch.setattr(lore_bridge, "_extensions", lambda: (
        lambda cwd: "this", lambda: None, lambda: [], (lambda text: text, lambda: None)))
    requests = [
        {"id": 1, "op": "pending_review_v1", "cwd": "/repo", "pid": "one"},
        {"id": 2, "op": "pending_review_v1", "cwd": "/repo", "pid": "one",
         "expected": {"sha256": "0" * 64, "inode": 1}},
        {"id": 3, "op": "pending_review_v1", "cwd": "/repo", "pid": "../one"},
    ]
    output = io.BytesIO()
    monkeypatch.setattr(lore_bridge.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(
        b"".join(map(lore_bridge._frame, requests)))))
    monkeypatch.setattr(lore_bridge.sys, "stdout", types.SimpleNamespace(buffer=output))
    lore_bridge.serve()
    frames = [json.loads(line) for line in output.getvalue().splitlines()]
    assert "pending_review_v1" in frames[0]["capabilities"]
    review = frames[1]["value"]
    assert review == {"pid": "one", "raw": raw.decode(),
                      "sha256": hashlib.sha256(raw).hexdigest(),
                      "inode": proposal.stat().st_ino, "complete": True}
    assert frames[2]["error"] == "pending_changed"
    assert frames[3]["error"] == "invalid_request"
    proposal.write_bytes(raw + b" ")
    with pytest.raises(lore_bridge.PendingReviewError) as changed:
        lore_bridge._pending_review("/repo", "one", lore_bridge._extensions(),
                                    (tmp_path, pending_mod._pending_bytes_snapshot),
                                    {"sha256": review["sha256"], "inode": review["inode"]})
    assert changed.value.code == "pending_changed"


def test_pending_review_v1_never_sends_partial_or_other_project(monkeypatch, tmp_path):
    from lore_core import pending as pending_mod

    pending_dir = tmp_path / "pending"
    pending_dir.mkdir()
    (pending_dir / "large.json").write_bytes(b"x" * (lore_bridge._MAX_REVIEW_RAW_BYTES + 1))
    (pending_dir / "hidden.json").write_text('{"scope":"project","project":"other","secret":"never send"}')
    monkeypatch.setattr(pending_mod, "ROOT", tmp_path)
    reader = (tmp_path, pending_mod._pending_bytes_snapshot)
    ext = (lambda cwd: "this", None, None, None)
    with pytest.raises(lore_bridge.PendingReviewError) as large:
        lore_bridge._pending_review("/repo", "large", ext, reader)
    assert large.value.code == "pending_incomplete"
    with pytest.raises(lore_bridge.PendingReviewError) as hidden:
        lore_bridge._pending_review("/repo", "hidden", ext, reader)
    assert hidden.value.code == "pending_unavailable"


def test_pending_review_control_bytes_fit_reply_without_changing_raw(monkeypatch, tmp_path):
    pending_dir = tmp_path / "pending"
    pending_dir.mkdir()
    # JSON cannot carry literal control bytes. Backslashes in the raw file
    # need another round of escaping in the reply.
    raw = b'{"scope":"user","text":"' + b'\\n' * ((lore_bridge._MAX_REVIEW_RAW_BYTES - 28) // 2) + b'"}'
    (pending_dir / "one.json").write_bytes(raw)
    ext = (lambda cwd: "this", None, None, None)
    reader = (tmp_path, lambda pid: (raw, (pending_dir / "one.json").stat().st_ino))
    result = lore_bridge._pending_review("/repo", "one", ext, reader)
    assert result["raw"].encode() == raw
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    assert len(lore_bridge._frame({"type": "reply", "id": 2**64 - 1,
                                   "ok": True, "value": result})) <= lore_bridge.MAX_FRAME_BYTES

    expanded = b'{"scope":"user"}' + b'\n' * (lore_bridge.MAX_FRAME_BYTES // 2 - 16)
    assert len(expanded) <= lore_bridge.MAX_FRAME_BYTES // 2
    with pytest.raises(ValueError, match="frame too large"):
        lore_bridge._frame({"type": "reply", "id": 1, "ok": True,
                            "value": {"raw": expanded.decode()}})
    (pending_dir / "one.json").write_bytes(expanded)
    with pytest.raises(lore_bridge.PendingReviewError) as large:
        lore_bridge._pending_review("/repo", "one", ext, reader)
    assert large.value.code == "pending_incomplete"


def test_sidecar_scrub_snapshot_and_generic_error(monkeypatch):
    def snapshot(cwd, scope="all"):
        if cwd == "/fail":
            raise RuntimeError("SECRET IN ERROR")
        return f"memory {scope}"

    monkeypatch.setattr(lore_bridge, "_lore", lambda: (lambda text: text.replace("SECRET", "[redacted]"), snapshot))
    monkeypatch.setattr(lore_bridge, "_extensions", lambda: None)
    requests = [
        {"id": 1, "op": "scrub", "text": "SECRET"},
        {"id": 2, "op": "snapshot", "cwd": "/repo", "scope": "project"},
        {"id": 3, "op": "snapshot", "cwd": "/fail"},
        {"id": 4, "op": "unknown"},
    ]
    input_bytes = b"".join(lore_bridge._frame(req) for req in requests)
    output = io.BytesIO()
    monkeypatch.setattr(lore_bridge.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(input_bytes)))
    monkeypatch.setattr(lore_bridge.sys, "stdout", types.SimpleNamespace(buffer=output))
    lore_bridge.serve()
    frames = [json.loads(line) for line in output.getvalue().splitlines()]
    assert frames[0] == {"type": "hello", "proto": 1, "capabilities": ["scrub", "snapshot"]}
    assert frames[1]["text"] == "[redacted]"
    assert frames[2]["text"] == "memory project"
    assert frames[3] == {"type": "reply", "id": 3, "ok": False, "error": "operation_failed"}
    assert frames[4] == {"type": "reply", "id": 4, "ok": False, "error": "invalid_request"}
    assert b"SECRET IN ERROR" not in output.getvalue()


def test_oversize_request_closes_without_echo(monkeypatch):
    monkeypatch.setattr(lore_bridge, "_lore", lambda: (lambda text: text, lambda cwd, scope: ""))
    monkeypatch.setattr(lore_bridge, "_extensions", lambda: None)
    output = io.BytesIO()
    monkeypatch.setattr(lore_bridge.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"x" * (lore_bridge.MAX_FRAME_BYTES + 1))))
    monkeypatch.setattr(lore_bridge.sys, "stdout", types.SimpleNamespace(buffer=output))
    lore_bridge.serve()
    assert len(output.getvalue().splitlines()) == 1  # hello only


def test_deep_json_request_does_not_stop_following_frames(monkeypatch):
    monkeypatch.setattr(lore_bridge, "_lore", lambda: (lambda text: text, lambda cwd, scope: ""))
    monkeypatch.setattr(lore_bridge, "_extensions", lambda: None)
    deep = b'{"id":1,"op":"scrub","text":"x","extra":' + b'[' * 10000 + b'0' + b']' * 10000 + b'}\n'
    output = io.BytesIO()
    monkeypatch.setattr(lore_bridge.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(
        deep + lore_bridge._frame({"id": 2, "op": "scrub", "text": "after"}))))
    monkeypatch.setattr(lore_bridge.sys, "stdout", types.SimpleNamespace(buffer=output))
    lore_bridge.serve()
    frames = [json.loads(line) for line in output.getvalue().splitlines()]
    assert frames[1] == {"type": "reply", "id": 2, "ok": True, "text": "after"}


def test_read_connections_close_on_success_and_query_failure():
    closed = []

    class Connection:
        def __init__(self, fail=False):
            self.fail = fail

        def execute(self, query, params=()):
            if self.fail:
                raise sqlite3.OperationalError("query failed")
            return types.SimpleNamespace(fetchone=lambda: None, fetchall=lambda: [])

        def close(self):
            closed.append(self)

    made = []

    def connect(fail=False):
        conn = Connection(fail)
        made.append(conn)
        return conn

    assert lore_bridge._consult("hello", (connect, lambda text, op: text), str) is None
    assert lore_bridge._beliefs(0, 1, (connect, None), str) == []
    assert lore_bridge._evidence(1, 0, 1, (connect, None), str) == []
    with pytest.raises(sqlite3.OperationalError):
        lore_bridge._beliefs(0, 1, (lambda: connect(True), None), str)
    assert closed == made


def test_unavailable_lore_refuses_scrubbing(monkeypatch):
    monkeypatch.setattr(lore_bridge, "_lore", lambda: None)
    output = io.BytesIO()
    monkeypatch.setattr(lore_bridge.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(
        lore_bridge._frame({"id": 1, "op": "scrub", "text": "SECRET"})
    )))
    monkeypatch.setattr(lore_bridge.sys, "stdout", types.SimpleNamespace(buffer=output))
    lore_bridge.serve()
    frames = [json.loads(line) for line in output.getvalue().splitlines()]
    assert frames[0]["capabilities"] == []
    assert frames[1] == {"type": "reply", "id": 1, "ok": False, "error": "lore_unavailable"}
    assert b"SECRET" not in output.getvalue()


def test_pending_sync_and_refresh_are_bounded_and_scoped(monkeypatch):
    monkeypatch.setattr(lore_bridge, "_lore", lambda: (lambda text: text.replace("SECRET", "[redacted]"), lambda cwd, scope: "snapshot"))
    rows = [("one", {"scope": "project", "project": "this", "subject": "SECRET subject",
                      "confidence": 0.8, "subject_unresolved": False, "text": "SECRET here"}),
            ("hidden", {"scope": "project", "project": "other", "text": "private"}),
            ("two", {"scope": "user", "claim": "safe"})]
    state = types.SimpleNamespace(last_pull_age_s=2.5, unpushed=1, conflicts=0, unverified=0)
    monkeypatch.setattr(lore_bridge, "_extensions", lambda: (
        lambda cwd: "this", lambda: 30, lambda: rows,
        (lambda text: text.replace("SECRET", "[redacted]"), lambda: state)))
    requests = [
        {"id": 1, "op": "pending", "cwd": "/repo", "limit": 1},
        {"id": 2, "op": "pending", "cwd": "/repo", "offset": 1, "limit": 1},
        {"id": 3, "op": "sync_state"},
        {"id": 4, "op": "refresh_interval"},
        {"id": 5, "op": "pending", "cwd": "/repo", "limit": 51},
    ]
    output = io.BytesIO()
    monkeypatch.setattr(lore_bridge.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"".join(map(lore_bridge._frame, requests)))))
    monkeypatch.setattr(lore_bridge.sys, "stdout", types.SimpleNamespace(buffer=output))
    lore_bridge.serve()
    frames = [json.loads(line) for line in output.getvalue().splitlines()]
    assert frames[0]["capabilities"] == ["scrub", "snapshot", "pending", "sync_state", "refresh_interval", "transcript_identity"]
    assert frames[1]["value"] == [{"pid": "one", "scope": "project", "project": "this",
                                   "subject": "[redacted] subject", "confidence": 0.8,
                                   "subject_unresolved": False, "text": "[redacted] here"}]
    assert frames[2]["value"] == [{"pid": "two", "scope": "user", "claim": "safe"}]
    assert frames[3]["value"] == {"last_pull_age_s": 2.5, "unpushed": 1, "conflicts": 0, "unverified": 0}
    assert frames[4]["value"] == 30
    assert frames[5]["error"] == "operation_failed"
    assert b"SECRET" not in output.getvalue()


def test_transcript_identity_uses_lore_project_mapping(monkeypatch):
    monkeypatch.setattr(lore_bridge, "_lore", lambda: (lambda text: text, lambda cwd, scope: ""))
    monkeypatch.setattr(lore_bridge, "_extensions", lambda: (
        lambda cwd: "lore-project", lambda: 30, lambda: [], (lambda text: text, lambda: None)))
    monkeypatch.setattr(lore_bridge, "_transcript_identity", lambda cwd, ext: {
        "projects_dir": "/lore/projects", "slug": ext[0](cwd)})
    output = io.BytesIO()
    monkeypatch.setattr(lore_bridge.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(
        lore_bridge._frame({"id": 1, "op": "transcript_identity", "cwd": "/repo"}))))
    monkeypatch.setattr(lore_bridge.sys, "stdout", types.SimpleNamespace(buffer=output))
    lore_bridge.serve()
    frames = [json.loads(line) for line in output.getvalue().splitlines()]
    assert frames[1]["value"] == {"projects_dir": "/lore/projects", "slug": "lore-project"}


def test_pending_scrubs_nested_allowlisted_values_before_writing(monkeypatch):
    scrub = lambda text: text.replace("SECRET", "[redacted]")
    monkeypatch.setattr(lore_bridge, "_lore", lambda: (scrub, lambda cwd, scope: ""))
    monkeypatch.setattr(lore_bridge, "_extensions", lambda: (
        lambda cwd: "this", lambda: None,
        lambda: [("p1", {
            "scope": "user",
            "subject": {"SECRET key": ["SECRET value", {"nested": "SECRET again"}]},
            "confidence": 0.8,
            "subject_unresolved": False,
        })],
        (scrub, lambda: None),
    ))
    output = io.BytesIO()
    monkeypatch.setattr(lore_bridge.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(
        lore_bridge._frame({"id": 1, "op": "pending", "cwd": "/repo"})
    )))
    monkeypatch.setattr(lore_bridge.sys, "stdout", types.SimpleNamespace(buffer=output))

    lore_bridge.serve()

    frames = [json.loads(line) for line in output.getvalue().splitlines()]
    assert frames[1]["value"] == [{
        "pid": "p1", "scope": "user",
        "subject": {"[redacted] key": ["[redacted] value", {"nested": "[redacted] again"}]},
        "confidence": 0.8, "subject_unresolved": False,
    }]
    assert b"SECRET" not in output.getvalue()


def test_pending_rejects_keys_that_collapse_after_scrubbing():
    scrub = lambda text: "[redacted]" if text.startswith("SECRET") else text
    with pytest.raises(ValueError, match="collide"):
        lore_bridge._scrub_pending_value({"SECRET-one": 1, "SECRET-two": 2}, scrub)


def test_disabled_sync_returns_null(monkeypatch):
    monkeypatch.setattr(lore_bridge, "_lore", lambda: (lambda text: text, lambda cwd, scope: ""))
    monkeypatch.setattr(lore_bridge, "_extensions", lambda: (
        lambda cwd: "this", lambda: None, lambda: [], (lambda text: text, lambda: None)))
    output = io.BytesIO()
    monkeypatch.setattr(lore_bridge.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(
        lore_bridge._frame({"id": 1, "op": "sync_state"}) + lore_bridge._frame({"id": 2, "op": "refresh_interval"}))))
    monkeypatch.setattr(lore_bridge.sys, "stdout", types.SimpleNamespace(buffer=output))
    lore_bridge.serve()
    frames = [json.loads(line) for line in output.getvalue().splitlines()]
    assert frames[1]["value"] is None
    assert frames[2]["value"] is None


def test_consult_beliefs_and_evidence_are_bounded_scrubbed_and_cite_only(monkeypatch, tmp_path):
    db_path = tmp_path / "store.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE beliefs(id INTEGER, subject TEXT, claim TEXT, confidence REAL, status TEXT, updated TEXT)")
    conn.execute("CREATE TABLE belief_evidence(belief_id INTEGER, session_id TEXT, project TEXT, note TEXT, created TEXT, source_engine TEXT)")
    conn.execute("CREATE VIRTUAL TABLE belief_fts USING fts5(belief_id UNINDEXED, claim)")
    conn.execute("INSERT INTO beliefs VALUES(1,'user','SECRET fact',0.8,'active','2026-01-01')")
    conn.execute("INSERT INTO belief_fts VALUES(1,'SECRET fact')")
    for n in range(3):
        conn.execute("INSERT INTO belief_evidence VALUES(1,'SECRET session','project','SECRET note',?, 'claude')", (str(n),))
    conn.commit()
    conn.close()
    monkeypatch.setattr(lore_bridge, "_lore", lambda: (lambda text: text.replace("SECRET", "[redacted]"), lambda cwd, scope: ""))
    monkeypatch.setattr(lore_bridge, "_extensions", lambda: None)
    monkeypatch.setattr(lore_bridge, "_read_ops", lambda: (lambda: sqlite3.connect(db_path), lambda text, op: text))
    requests = [
        {"id": 1, "op": "consult", "prompt": "fact"},
        {"id": 2, "op": "beliefs", "offset": 0, "limit": 1},
        {"id": 3, "op": "evidence", "belief_id": 1, "limit": 2},
        {"id": 6, "op": "evidence", "belief_id": 1, "offset": 1, "limit": 1},
        {"id": 4, "op": "beliefs", "limit": 51},
        {"id": 5, "op": "consult", "prompt": "SECRET" * 9000},
    ]
    output = io.BytesIO()
    monkeypatch.setattr(lore_bridge.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"".join(map(lore_bridge._frame, requests)))))
    monkeypatch.setattr(lore_bridge.sys, "stdout", types.SimpleNamespace(buffer=output))
    lore_bridge.serve()
    frames = [json.loads(line) for line in output.getvalue().splitlines()]
    assert frames[0]["capabilities"] == ["scrub", "snapshot", "consult", "beliefs", "evidence"]
    assert frames[1]["value"]["citation_status"] == "cite_only"
    assert frames[1]["value"]["claim"] == "[redacted] fact"
    assert frames[2]["value"][0]["evidence_count"] == 3
    assert frames[2]["value"][0]["claim"] == "[redacted] fact"
    assert len(frames[3]["value"]) == 2
    assert frames[3]["value"][-1]["trail_truncated"] is True
    assert frames[3]["value"][0]["session_id"] == "[redacted] session"
    assert frames[4]["value"][0]["created"] == "1"
    assert frames[4]["value"][0]["trail_truncated"] is True
    assert frames[5]["error"] == frames[6]["error"] == "operation_failed"
    assert b"SECRET" not in output.getvalue()
