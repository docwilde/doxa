"""The Rust LORE sidecar wire must never echo untrusted exception text."""

import io
import json
import types

import pytest

from doxa import lore_bridge


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
    assert frames[0]["capabilities"] == ["scrub", "snapshot", "pending", "sync_state", "refresh_interval"]
    assert frames[1]["value"] == [{"pid": "one", "scope": "project", "project": "this",
                                   "subject": "[redacted] subject", "confidence": 0.8,
                                   "subject_unresolved": False, "text": "[redacted] here"}]
    assert frames[2]["value"] == [{"pid": "two", "scope": "user", "claim": "safe"}]
    assert frames[3]["value"] == {"last_pull_age_s": 2.5, "unpushed": 1, "conflicts": 0, "unverified": 0}
    assert frames[4]["value"] == 30
    assert frames[5]["error"] == "operation_failed"
    assert b"SECRET" not in output.getvalue()


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
