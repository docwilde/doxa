# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded JSONL sidecar for Rust DOXA to call external LORE.

This process is deliberately separate from the native runtime. LORE owns the
scrubber and context store; DOXA owns the wire, deadlines, and fallback path.
Only an explicit allow-list of operations is exposed. Never echo input or
exception text in an error response, stderr, or a process argument.
"""

from __future__ import annotations

import json
import sys
from typing import Any

MAX_FRAME_BYTES = 1024 * 1024
PROTOCOL_VERSION = 1
_OPS = ("scrub", "snapshot")


def _frame(value: dict[str, Any]) -> bytes:
    encoded = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > MAX_FRAME_BYTES:
        raise ValueError("sidecar frame too large")
    return encoded


def _write(value: dict[str, Any]) -> None:
    try:
        sys.stdout.buffer.write(_frame(value))
    except ValueError:
        sys.stdout.buffer.write(_frame({"type": "reply", "id": value.get("id"), "ok": False, "error": "output_too_large"}))
    sys.stdout.buffer.flush()


def _lore() -> tuple[Any, Any] | None:
    try:
        from . import _lore_bootstrap  # noqa: F401 -- sets import path/store root first
        from lore_core.context import build_context
        from lore_core.scrub import scrub_secrets
        return scrub_secrets, build_context
    except Exception:  # noqa: BLE001 -- unavailable means no capability
        return None


def serve() -> None:
    lore = _lore()
    _write({"type": "hello", "proto": PROTOCOL_VERSION,
            "capabilities": list(_OPS) if lore is not None else []})
    while True:
        raw = sys.stdin.buffer.readline(MAX_FRAME_BYTES + 1)
        if not raw:
            return
        if len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
            return  # The stream is no longer framed; do not resynchronize blindly.
        try:
            req = json.loads(raw)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(req, dict):
            continue
        rid = req.get("id")
        if type(rid) is not int or not 0 <= rid <= 2**64 - 1:
            continue
        op = req.get("op")
        if lore is None:
            _write({"type": "reply", "id": rid, "ok": False, "error": "lore_unavailable"})
            continue
        scrub, snapshot = lore
        try:
            if op == "scrub" and isinstance(req.get("text"), str):
                result = scrub(req["text"])
            elif op == "snapshot" and isinstance(req.get("cwd"), str):
                cwd = req["cwd"]
                scope = req.get("scope", "all")
                if not cwd or len(cwd) > 4096 or "\x00" in cwd or scope not in ("all", "user", "project"):
                    raise ValueError("invalid snapshot input")
                result = snapshot(cwd, scope=scope)
            else:
                _write({"type": "reply", "id": rid, "ok": False, "error": "invalid_request"})
                continue
            if not isinstance(result, str):
                raise TypeError("invalid LORE result")
            _write({"type": "reply", "id": rid, "ok": True, "text": result})
        except Exception:  # noqa: BLE001 -- never print credentials from input or LORE
            _write({"type": "reply", "id": rid, "ok": False, "error": "operation_failed"})


if __name__ == "__main__":
    serve()
