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


def test_belief_graph_uses_scoped_canonical_relations_and_scrubs(tmp_path):
    path = tmp_path / "graph.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE beliefs(id INTEGER PRIMARY KEY, subject TEXT, status TEXT)")
    conn.executemany("INSERT INTO beliefs VALUES(?,?,?)", [(1,"user","active"), (2,"project:current","active"), (3,"project:other","active")])
    conn.commit(); conn.close()
    def relations(conn, bid):
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM beliefs")
        return "  relations:\n  --depends_on--> [2] derived SECRET\n  --depends_on--> [3] OUTSIDE"
    ops = (lambda cwd: "current", lambda: sqlite3.connect(path), relations)
    req = {"cwd":"/repo", "belief_id":1, "browser":False}
    result = lore_bridge._belief_graph(req, ops, lambda text: text.replace("SECRET", "[redacted]"))
    assert result["id"] == 1 and result["html"] is None
    assert result["lines"] == ["  --depends_on--> [2] derived [redacted]"]
    with pytest.raises(lore_bridge.BeliefActionError, match="belief_unavailable"):
        lore_bridge._belief_graph({**req,"belief_id":3}, ops, lambda text: text)
    with pytest.raises(lore_bridge.BeliefActionError, match="invalid_request"):
        lore_bridge._belief_graph({**req,"browser":"false"}, ops, lambda text: text)


def test_belief_graph_no_relations_does_not_import_browser_renderer(tmp_path):
    path = tmp_path / "graph.db"; conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE beliefs(id INTEGER PRIMARY KEY, subject TEXT, status TEXT)")
    conn.execute("INSERT INTO beliefs VALUES(1,'user','active')"); conn.commit(); conn.close()
    result = lore_bridge._belief_graph({"cwd":"/repo","belief_id":1,"browser":True},
        (lambda cwd:"current", lambda:sqlite3.connect(path), lambda *_:""), lambda text:text)
    assert result["html"] is None and "No relations" in result["lines"][0]


def test_indexed_session_search_is_project_first_bounded_and_scrubbed():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE msg USING fts5(session_id UNINDEXED, project UNINDEXED, "
                 "ts UNINDEXED, role UNINDEXED, content)")
    conn.executemany("INSERT INTO msg VALUES (?, ?, '', 'user', ?)", [
        ("other-1", "other", "rare token"),
        ("local-1", "project", "rare SECRET token"),
        ("local-1", "project", "another rare token"),
    ])
    conn.execute("PRAGMA query_only=ON")
    hits = lore_bridge._session_search(
        "/repo", "rare token", (lambda: conn, lambda q, sep=" ": sep.join(q.split())),
        (lambda cwd: "project", None, None, None),
        lambda text: text.replace("SECRET", "[redacted]"),
    )
    assert [hit["session_id"] for hit in hits] == ["local-1", "local-1"]
    assert "SECRET" not in str(hits)
    assert any("[redacted]" in hit["snippet"] for hit in hits)
    with pytest.raises(ValueError):
        lore_bridge._session_search("/repo", "x" * 201, (lambda: None, lambda q: q),
                                    (lambda cwd: "project", None, None, None), lambda x: x)


def test_native_reviewed_resolve_rejects_one_exact_snapshot(tmp_path):
    from doxa.native_lore import executable
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
    env = dict(os.environ, HOME=str(tmp_path), DOXA_LORE_RS=executable(), LORE_ROOT=str(root),
               LORE_SKILLS_DIR=str(tmp_path / "skills"),
               LORE_PROJECTS_DIR=str(tmp_path / "projects"))
    result = subprocess.run([sys.executable, "-m", "doxa.lore_bridge"],
                            input=b"".join(map(lore_bridge._frame, requests)),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=Path(__file__).resolve().parent.parent, env=env,
                            timeout=10, check=True)
    frames = [json.loads(line) for line in result.stdout.splitlines()]
    assert "resolve_reviewed_v1" in frames[0]["capabilities"]
    assert frames[1]["value"]["sha256"] == expected["sha256"]
    assert frames[2]["error"] == "review_changed"
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
    monkeypatch.setattr(lore_bridge, "_belief_action_ops", lambda: None)
    monkeypatch.setattr(lore_bridge, "_belief_graph_ops", lambda: None)
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
    monkeypatch.setattr(lore_bridge, "_memory_usage_ops", lambda: None)
    monkeypatch.setattr(lore_bridge, "_memory_manage_ops", lambda: None)
    monkeypatch.setattr(lore_bridge, "_memory_entries_ops", lambda: None)
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


def test_memory_usage_sidecar_counts_only_curated_entries_for_each_scope(tmp_path):
    from lore_core.config import project_slug

    root = tmp_path / "lore"
    first = tmp_path / "project-one"
    second = tmp_path / "project-two"
    first.mkdir()
    second.mkdir()
    first_memory = root / "projects" / project_slug(str(first)) / "MEMORY.md"
    second_memory = root / "projects" / project_slug(str(second)) / "MEMORY.md"
    first_memory.parent.mkdir(parents=True)
    second_memory.parent.mkdir(parents=True)
    first_memory.write_text("- café 😊\n- second\n\nignored note\n", encoding="utf-8")
    second_memory.write_text("- βeta\n", encoding="utf-8")
    root.mkdir(exist_ok=True)
    (root / "USER.md").write_text("- user 🌍\n", encoding="utf-8")
    requests = [
        {"id": 1, "op": "memory_usage_v1", "cwd": str(first)},
        {"id": 2, "op": "memory_usage_v1", "cwd": str(second)},
        {"id": 3, "op": "memory_usage_v1", "cwd": ""},
    ]
    env = dict(os.environ, LORE_ROOT=str(root), DOXA_LORE_SOURCE="package",
               LORE_MEMORY_CAP="5000", LORE_USER_CAP="7000")
    result = subprocess.run([sys.executable, "-m", "doxa.lore_bridge"],
                            input=b"".join(map(lore_bridge._frame, requests)),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=Path(__file__).resolve().parent.parent, env=env,
                            timeout=10, check=True)
    frames = [json.loads(line) for line in result.stdout.splitlines()]
    assert "memory_usage_v1" in frames[0]["capabilities"]
    assert frames[1]["value"] == {"project_chars": len("- café 😊\n- second\n"),
                                  "user_chars": len("- user 🌍\n"),
                                  "project_cap_chars": 5000, "user_cap_chars": 7000}
    assert frames[2]["value"] == {"project_chars": len("- βeta\n"),
                                  "user_chars": len("- user 🌍\n"),
                                  "project_cap_chars": 5000, "user_cap_chars": 7000}
    assert frames[3] == {"type": "reply", "id": 3, "ok": False, "error": "invalid_request"}
    assert "café".encode() not in result.stdout and "🌍".encode() not in result.stdout


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
    with pytest.raises(sqlite3.OperationalError):
        lore_bridge._belief_display({"cwd": "/repo", "belief_id": 1},
                                    (lambda: connect(True), None), str)
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
    monkeypatch.setattr(lore_bridge, "_memory_usage_ops", lambda: None)
    monkeypatch.setattr(lore_bridge, "_memory_manage_ops", lambda: None)
    monkeypatch.setattr(lore_bridge, "_memory_entries_ops", lambda: None)
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
    monkeypatch.setattr(lore_bridge, "_memory_usage_ops", lambda: None)
    monkeypatch.setattr(lore_bridge, "_memory_manage_ops", lambda: None)
    monkeypatch.setattr(lore_bridge, "_memory_entries_ops", lambda: None)
    monkeypatch.setattr(lore_bridge, "_read_ops", lambda: (lambda: sqlite3.connect(db_path), lambda text, op: text))
    requests = [
        {"id": 1, "op": "consult", "prompt": "fact"},
        {"id": 2, "op": "beliefs", "offset": 0, "limit": 1},
        {"id": 3, "op": "evidence", "belief_id": 1, "limit": 2},
        {"id": 6, "op": "evidence", "belief_id": 1, "offset": 1, "limit": 1},
        {"id": 4, "op": "beliefs", "limit": 51},
        {"id": 5, "op": "consult", "prompt": "SECRET" * 9000},
        {"id": 7, "op": "beliefs_filtered_v1", "query": "FACT", "limit": 1},
        {"id": 8, "op": "beliefs_filtered_v1", "query": False, "limit": 1},
        {"id": 9, "op": "belief_display_v1", "cwd": "/repo", "belief_id": 1},
    ]
    output = io.BytesIO()
    monkeypatch.setattr(lore_bridge.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"".join(map(lore_bridge._frame, requests)))))
    monkeypatch.setattr(lore_bridge.sys, "stdout", types.SimpleNamespace(buffer=output))
    lore_bridge.serve()
    frames = [json.loads(line) for line in output.getvalue().splitlines()]
    assert frames[0]["capabilities"] == ["scrub", "snapshot", "consult", "beliefs", "evidence", "beliefs_filtered_v1", "belief_display_v1"]
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
    assert frames[7]["value"][0]["recency"] == "2026-01-01"
    assert frames[8]["error"] == "operation_failed"
    assert frames[9]["value"] == {"id": 1, "subject": "user", "claim": "[redacted] fact",
                                  "complete": False, "redacted": True}
    assert b"SECRET" not in output.getvalue()


def test_belief_filter_uses_visible_unicode_text_and_actual_recency_before_pagination(tmp_path):
    db = tmp_path / "beliefs.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE beliefs(id INTEGER, subject TEXT, claim TEXT, confidence REAL, "
                 "status TEXT, updated TEXT, created TEXT)")
    conn.execute("CREATE TABLE belief_evidence(belief_id INTEGER)")
    conn.executemany("INSERT INTO beliefs VALUES(?,?,?,0.8,?,?,?)", [
        (90, "project:here", "Straße newest update", "active", "2026-01-01T22:00:00Z", "2025-01-01"),
        (2, "user", "STRASSE 100% older update", "active", "2026-01-02T00:00:00+03:00", None),
        (1, "user", "Straße created fallback", "active", "unknown", "2026-01-03T00:00:00Z"),
        (999, "user", "SECRET hidden", "active", None, None),
        (3, "user", "STRASSE inactive", "retracted", "2027-01-01", None),
    ])
    conn.commit(); conn.close()
    ops = (lambda: sqlite3.connect(db), None)
    scrub = lambda text: text.replace("SECRET", "[redacted]")
    rows = lore_bridge._beliefs(0, 50, ops, scrub)
    assert [row["id"] for row in rows] == [1, 90, 2, 999]
    assert rows[0]["updated"] is None
    assert rows[0]["recency"] == rows[0]["created"] == "2026-01-03T00:00:00Z"
    assert rows[-1]["recency"] is None
    assert [row["id"] for row in lore_bridge._beliefs(1, 1, ops, scrub, "strasse")] == [90]
    assert [row["id"] for row in lore_bridge._beliefs(0, 50, ops, scrub, "PROJECT:HERE")] == [90]
    assert [row["id"] for row in lore_bridge._beliefs(0, 50, ops, scrub, "%")] == [2]
    assert lore_bridge._beliefs(0, 50, ops, scrub, "SECRET") == []
    assert [row["id"] for row in lore_bridge._beliefs(0, 50, ops, scrub, "[REDACTED]")] == [999]
    for query in (None, "x" * 201, "line\nbreak"):
        with pytest.raises(ValueError):
            lore_bridge._beliefs(0, 50, ops, scrub, query)
    from contextlib import closing
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT count(*) FROM beliefs").fetchone()[0] == 5


def test_belief_display_is_global_active_read_only_full_bounded_and_scrubbed(tmp_path):
    db = tmp_path / "display.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE beliefs(id INTEGER, subject TEXT, claim TEXT, status TEXT)")
    full = "complete paragraph\n" + "ü" * 5000
    conn.executemany("INSERT INTO beliefs VALUES(?,?,?,?)", [
        (1, "project:another-checkout", full, "active"),
        (2, "user-model", "SECRET claim", "active"),
        (3, "user", "unsafe\x1b[31m claim", "active"),
        (4, "user", "ü" * 32769, "active"),
        (5, "user", "retired", "retracted"),
    ])
    conn.commit(); conn.close()
    opened = []
    def connect():
        conn = sqlite3.connect(db)
        opened.append(conn)
        return conn
    ops = (connect, None)
    scrub = lambda text: text.replace("SECRET", "[redacted]")
    req = {"cwd": "/current-checkout", "belief_id": 1}
    reply = lore_bridge._belief_display(req, ops, scrub)
    assert reply == {"id": 1, "subject": "project:another-checkout", "claim": full,
                     "complete": True, "redacted": False}
    assert "uid" not in reply and "claim_sha256" not in reply
    redacted = lore_bridge._belief_display({**req, "belief_id": 2}, ops, scrub)
    assert redacted["claim"] == "[redacted] claim"
    assert redacted["complete"] is False and redacted["redacted"] is True
    unsafe = lore_bridge._belief_display({**req, "belief_id": 3}, ops, scrub)
    assert unsafe["claim"] == "" and unsafe["complete"] is False and unsafe["redacted"] is True
    oversized = lore_bridge._belief_display({**req, "belief_id": 4}, ops, scrub)
    assert oversized["claim"] == "" and oversized["complete"] is False
    for bid in (5, 999):
        with pytest.raises(lore_bridge.BeliefActionError) as error:
            lore_bridge._belief_display({**req, "belief_id": bid}, ops, scrub)
        assert error.value.code == "belief_unavailable"
    for bid in (True, 0, -1, 2**63):
        with pytest.raises(lore_bridge.BeliefActionError) as error:
            lore_bridge._belief_display({**req, "belief_id": bid}, ops, scrub)
        assert error.value.code == "invalid_request"
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")
    assert len(lore_bridge._frame({"type": "reply", "id": 1, "ok": True, "value": reply})) < lore_bridge.MAX_FRAME_BYTES


def test_curated_memory_read_view_preserves_facts_and_canonical_provenance(tmp_path):
    from contextlib import nullcontext
    path = tmp_path / "MEMORY.md"
    path.write_text("- first fact\n- SECRET fact\n- [source: codex] is literal fact text\n")
    entries = ["first fact", "SECRET fact", "[source: codex] is literal fact text"]
    calls = []
    memory = types.SimpleNamespace(project_slug=lambda cwd: "canonical-slug",
        memory_path=lambda scope, slug: path, read_entries=lambda path: entries,
        render_entries=lambda entries: "".join(f"- {entry}\n" for entry in entries),
        memory_bucket=lambda scope, slug: f"{scope}:{slug}")
    def labels(bucket, actual):
        calls.append((bucket, actual))
        return ["claude", "codex", ""]
    ops = (memory, tmp_path, lambda *args, **kwargs: nullcontext(), labels)
    rows = lore_bridge._memory_entries({"cwd": "/repo", "scope": "project"}, ops,
                                      lambda text: text.replace("SECRET", "[redacted]"))
    assert rows == [
        {"text": "first fact", "source": "claude", "redacted": False},
        {"text": "[redacted] fact", "source": "codex", "redacted": True},
        {"text": "[source: codex] is literal fact text", "source": None, "redacted": False},
    ]
    assert calls == [("project:canonical-slug", entries)]
    assert path.read_text() == "- first fact\n- SECRET fact\n- [source: codex] is literal fact text\n"
    for bad_entries in (["x"] * 401, ["x" * 65536], ["control\x1b"]):
        memory.read_entries = lambda path, actual=bad_entries: actual
        with pytest.raises(lore_bridge.BeliefActionError) as error:
            lore_bridge._memory_entries({"cwd": "/repo", "scope": "project"}, ops, str)
        assert error.value.code == "memory_incomplete"


def test_curated_memory_entries_wire_reads_private_canonical_facts(tmp_path):
    root = tmp_path / "lore"
    root.mkdir()
    body = "- one complete fact\n- another individual fact\n"
    (root / "USER.md").write_text(body)
    frames = _memory_wire(tmp_path, [
        {"id": 1, "op": "memory_entries_v1", "cwd": str(tmp_path), "scope": "user"},
        {"id": 2, "op": "memory_entries_v1", "cwd": str(tmp_path), "scope": "all"},
    ])
    assert frames[0]["value"] == [
        {"text": "one complete fact", "source": None, "redacted": False},
        {"text": "another individual fact", "source": None, "redacted": False},
    ]
    assert frames[1]["ok"] is False
    assert (root / "USER.md").read_text() == body


def test_belief_actions_use_exact_review_and_canonical_lore_mutators(tmp_path):
    db = tmp_path / "beliefs.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE beliefs(id INTEGER PRIMARY KEY, uid TEXT, subject TEXT, claim TEXT, status TEXT)")
    conn.execute("INSERT INTO beliefs VALUES(1,'uid-one','project:my-project','Safe fact','active')")
    conn.commit()
    conn.close()
    calls = []

    def outcome(conn, bid, event, source, *, note):
        calls.append((bid, event, source, note))
        if event == "contradicted":
            conn.execute("UPDATE beliefs SET status='dormant' WHERE id=?", (bid,))

    def retract(conn, bid, reason):
        calls.append((bid, "retract", reason))
        conn.execute("UPDATE beliefs SET status='retracted' WHERE id=?", (bid,))
        return True

    ops = (lambda cwd: "my-project", lambda: sqlite3.connect(db), retract,
           lambda conn, bid: (0, 1, 0), outcome)
    review = lore_bridge._belief_review({"cwd": "/repo", "belief_id": 1}, ops,
                                        lambda text: text.replace("SECRET", "[redacted]"))
    assert review["claim"] == "Safe fact"
    assert review["claim_sha256"] == hashlib.sha256(b"Safe fact").hexdigest()
    expected = {key: review[key] for key in ("uid", "subject", "claim_sha256")}
    request = {"cwd": "/repo", "belief_id": 1, "expected": expected,
               "action": "contradicted", "note": "Observed failure"}
    result = lore_bridge._belief_action(request, ops)
    assert result == {"status": "dormant", "retired": True, "confirmed": 0,
                      "contradicted": 1, "stale": 0}
    assert calls == [(1, "contradicted", "user", "Observed failure")]
    with pytest.raises(lore_bridge.BeliefActionError) as changed:
        lore_bridge._belief_action(request, ops)
    assert changed.value.code == "belief_changed"
    assert len(calls) == 1


@pytest.mark.parametrize("action,status,counts", [
    ("confirmed", "active", (2, 1, 0)),
    ("retract", "retracted", (1, 1, 0)),
])
def test_belief_accept_and_reject_use_canonical_actions_and_keep_history(
        tmp_path, action, status, counts):
    db = tmp_path / "reviewed-beliefs.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE beliefs(id INTEGER PRIMARY KEY, uid TEXT, subject TEXT, "
                 "claim TEXT, status TEXT, confidence REAL, writer TEXT, origin TEXT)")
    original = (1, "reviewed-uid", "project:my-project", "Reviewed fact", "active",
                0.7, "original-writer", "derived")
    conn.execute("INSERT INTO beliefs VALUES(?,?,?,?,?,?,?,?)", original)
    conn.execute("CREATE TABLE outcomes(belief_id INTEGER, event TEXT, source TEXT, note TEXT)")
    conn.executemany("INSERT INTO outcomes VALUES(1,?,'audit','existing history')",
                     [("confirmed",), ("contradicted",)])
    conn.commit()
    conn.close()
    calls = []

    def outcome(conn, bid, event, source, *, note):
        calls.append(("outcome", bid, event, source, note))
        conn.execute("INSERT INTO outcomes VALUES(?,?,?,?)", (bid, event, source, note))

    def retract(conn, bid, reason):
        calls.append(("retract", bid, reason))
        return conn.execute("UPDATE beliefs SET status='retracted' WHERE id=?",
                            (bid,)).rowcount == 1

    def outcome_counts(conn, bid):
        return tuple(conn.execute(
            "SELECT count(*) FROM outcomes WHERE belief_id=? AND event=?", (bid, event)
        ).fetchone()[0] for event in ("confirmed", "contradicted", "stale"))

    ops = (lambda cwd: "my-project", lambda: sqlite3.connect(db), retract,
           outcome_counts, outcome)
    review = lore_bridge._belief_review({"cwd": "/repo", "belief_id": 1}, ops, str)
    expected = {key: review[key] for key in ("uid", "subject", "claim_sha256")}
    assert expected == {"uid": original[1], "subject": original[2],
                        "claim_sha256": hashlib.sha256(original[3].encode()).hexdigest()}
    request = {"cwd": "/repo", "belief_id": 1, "expected": expected,
               "action": action, "note": "User reviewed the complete claim"}
    result = lore_bridge._belief_action(request, ops)
    assert result == {"status": status, "retired": status != "active",
                      "confirmed": counts[0], "contradicted": counts[1], "stale": counts[2]}
    assert calls == ([("outcome", 1, "confirmed", "user", request["note"])]
                     if action == "confirmed" else [("retract", 1, request["note"])])
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT * FROM beliefs").fetchone() == original[:4] + (status,) + original[5:]
    assert conn.execute("SELECT event,source,note FROM outcomes ORDER BY rowid LIMIT 2").fetchall() == [
        ("confirmed", "audit", "existing history"),
        ("contradicted", "audit", "existing history"),
    ]
    # An old review cannot authorize a different claim or a retired row.
    if action == "confirmed":
        conn.execute("UPDATE beliefs SET claim='Changed after review' WHERE id=1")
        conn.commit()
    conn.close()
    with pytest.raises(lore_bridge.BeliefActionError) as changed:
        lore_bridge._belief_action(request, ops)
    assert changed.value.code == "belief_changed"
    assert len(calls) == 1


def test_belief_review_refuses_claim_if_scrub_hides_content(tmp_path):
    db = tmp_path / "beliefs.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE beliefs(id INTEGER PRIMARY KEY, uid TEXT, subject TEXT, claim TEXT, status TEXT)")
    conn.execute("INSERT INTO beliefs VALUES(1,'uid-one','user','SECRET fact','active')")
    conn.commit()
    conn.close()
    ops = (lambda cwd: "my-project", lambda: sqlite3.connect(db),
           lambda *args: None, lambda conn, bid: (0, 0, 0),
           lambda *args, **kwargs: None)
    with pytest.raises(lore_bridge.BeliefActionError) as error:
        lore_bridge._belief_review({"cwd": "/repo", "belief_id": 1}, ops,
                                   lambda text: text.replace("SECRET", "[redacted]"))
    assert error.value.code == "belief_incomplete"


def test_belief_review_and_action_refuse_foreign_missing_and_changed_rows(tmp_path):
    db = tmp_path / "beliefs.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE beliefs(id INTEGER PRIMARY KEY, uid TEXT, subject TEXT, claim TEXT, status TEXT)")
    conn.executemany("INSERT INTO beliefs VALUES(?,?,?,?,?)", [
        (1, "one", "project:other", "foreign", "active"),
        (2, "two", "user", "original", "active"),
    ])
    conn.commit()
    conn.close()
    calls = []
    ops = (lambda cwd: "my-project", lambda: sqlite3.connect(db),
           lambda *args: calls.append("retract"), lambda conn, bid: (0, 0, 0),
           lambda *args, **kwargs: calls.append("outcome"))
    for bid in (1, 99):
        with pytest.raises(lore_bridge.BeliefActionError) as error:
            lore_bridge._belief_review({"cwd": "/repo", "belief_id": bid}, ops, str)
        assert error.value.code == "belief_unavailable"
    review = lore_bridge._belief_review({"cwd": "/repo", "belief_id": 2}, ops, str)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE beliefs SET claim='changed' WHERE id=2")
    conn.commit()
    conn.close()
    with pytest.raises(lore_bridge.BeliefActionError) as error:
        lore_bridge._belief_action({"cwd": "/repo", "belief_id": 2,
            "expected": {key: review[key] for key in ("uid", "subject", "claim_sha256")},
            "action": "confirmed", "note": "checked"}, ops)
    assert error.value.code == "belief_changed"
    assert calls == []


def _memory_wire(tmp_path, requests, **extra):
    """Every write targets a disposable pinned LORE store, never live memory."""
    from doxa.native_lore import executable
    env = dict(os.environ, HOME=str(tmp_path), DOXA_LORE_RS=executable(), LORE_ROOT=str(tmp_path / "lore"),
               LORE_SKILLS_DIR=str(tmp_path / "skills"), LORE_PROJECTS_DIR=str(tmp_path / "projects"),
               LORE_WRITE_GATE="off")
    env.update(extra)
    result = subprocess.run([sys.executable, "-m", "doxa.lore_bridge"],
                            input=b"".join(map(lore_bridge._frame, requests)),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                            cwd=Path(__file__).resolve().parent.parent, timeout=10, check=True)
    frames = [json.loads(line) for line in result.stdout.splitlines()]
    assert "memory_action_v1" in frames[0]["capabilities"]
    return frames[1:]


def _memory_req(tmp_path, action, text="", entry="", body=""):
    return {"id": 1, "op": "memory_action_v1", "cwd": str(tmp_path), "scope": "user",
            "action": action, "text": text, "entry": entry,
            "expected": {"key": "user", "sha256": hashlib.sha256(body.encode()).hexdigest()}}


def test_curated_memory_actions_use_canonical_lore_and_refuse_stale_review(tmp_path):
    frames = _memory_wire(tmp_path, [
        _memory_req(tmp_path, "add", "old fact"),
        _memory_req(tmp_path, "replace", "new fact", "old fact", "- old fact\n"),
        _memory_req(tmp_path, "remove", entry="old fact", body="- old fact\n"),
        {"id": 4, "op": "memory_review_v1", "cwd": str(tmp_path), "scope": "user"},
        _memory_req(tmp_path, "remove", entry="new fact", body="- new fact\n"),
    ])
    assert frames[0]["value"]["status"] == "applied"
    assert frames[1]["value"]["status"] == "applied"
    assert frames[2]["error"] == "review_changed"
    assert frames[3]["value"]["entries"] == ["new fact"]
    assert frames[4]["value"]["status"] == "applied"
    assert (tmp_path / "lore" / "USER.md").read_text() == ""


def test_curated_memory_ambiguous_match_and_caps_do_not_change_store(tmp_path):
    root = tmp_path / "lore"
    root.mkdir()
    body = "- fact\n- longer fact\n"
    (root / "USER.md").write_text(body)
    frames = _memory_wire(tmp_path, [
        _memory_req(tmp_path, "remove", entry="fact", body=body),
        _memory_req(tmp_path, "add", "another fact over budget", body=body),
    ], LORE_USER_CAP="30")
    assert frames[0]["error"] == "review_changed"
    assert frames[1]["error"] == "over_cap"
    assert (root / "USER.md").read_text() == body


def test_native_carrier_authority_is_explicit_and_model_writes_only_stage(tmp_path):
    # Environment hints cannot manufacture or remove human authority. The
    # dedicated agent carrier always owns Model authority, independently.
    from tests.test_native_agent_tools import identity, wire
    root, frames = wire(tmp_path, [
        {"id":1,"op":"agent_catalog_v1","identity":identity(tmp_path)},
        {"id":2,"op":"agent_tool_v1","identity":identity(tmp_path),
         "name":"lore_remember","arguments":{"text":"staged fact","scope":"user"}},
    ])
    assert frames[2]["value"]["staged"]
    assert (root / "USER.md").read_text() == "- isolated fixture memory\n"
    pending = list((root / "pending").glob("*.json"))
    assert len(pending) == 1
    proposal = json.loads(pending[0].read_text())
    assert proposal["text"] == "staged fact" and proposal["writer"] == "model"
    assert proposal["source_engine"] == "codex"
    frames = _memory_wire(tmp_path, [_memory_req(tmp_path,"add","reviewed human fact",body="- isolated fixture memory\n")],
                          LORE_WRITE_GATE="on", AI_AGENT="claude-code_test_harness")
    assert frames[0]["value"] == {"status":"applied"}
    assert "reviewed human fact" in (root / "USER.md").read_text()
    assert len(list((root / "pending").glob("*.json"))) == 1


def test_curated_memory_secret_or_control_review_cannot_authorize_changes(tmp_path):
    root = tmp_path / "lore"
    root.mkdir()
    body = "- fact\x1b[31m\n"
    (root / "USER.md").write_text(body)
    frames = _memory_wire(tmp_path, [{"id": 1, "op": "memory_review_v1",
                                    "cwd": str(tmp_path), "scope": "user"}])
    assert frames[0]["error"] == "output_too_large"
    assert "fact" not in str(frames)
