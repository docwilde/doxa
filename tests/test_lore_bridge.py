"""The Rust LORE sidecar wire must never echo untrusted exception text."""

import io
import json
import types

from doxa import lore_bridge


def test_sidecar_scrub_snapshot_and_generic_error(monkeypatch):
    def snapshot(cwd, scope="all"):
        if cwd == "/fail":
            raise RuntimeError("SECRET IN ERROR")
        return f"memory {scope}"

    monkeypatch.setattr(lore_bridge, "_lore", lambda: (lambda text: text.replace("SECRET", "[redacted]"), snapshot))
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
