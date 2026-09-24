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
_OPS = ("scrub", "snapshot", "pending", "sync_state", "refresh_interval")

_PENDING_FIELDS = ("kind", "action", "scope", "project", "subject", "id",
                   "confidence", "session_id", "derived_by", "created", "writer",
                   "origin_project", "subject_unresolved", "to")
_PENDING_TEXT = ("text", "claim", "match", "path", "purpose", "name",
                 "description", "evidence", "reason", "writer_evidence")


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


def _extensions() -> tuple[Any, Any, Any, Any] | None:
    """Load optional LORE-backed readers without opening its store here."""
    try:
        from lore_core.config import project_slug
        from lore_core.context import refresh_interval
        from lore_core.pending import load_pending
        from lore_core.scrub import scrub_secrets
        from .lore_sync import read_state
        return project_slug, refresh_interval, load_pending, (scrub_secrets, read_state)
    except Exception:  # noqa: BLE001 -- older plugin builds may lack an API
        return None


def _pending(cwd: str, offset: int, limit: int, ext: tuple[Any, Any, Any, Any]) -> list[dict]:
    slug = ext[0](cwd)
    scrub = ext[3][0]
    records: list[dict] = []
    visible = 0
    for pid, item in ext[2]():
        if not isinstance(item, dict):
            continue
        if item.get("scope") == "project" and item.get("project") != slug:
            continue
        if visible < offset:
            visible += 1
            continue
        if len(records) >= limit:
            break
        record = {"pid": pid}
        # Keep pid as the stable routing identifier. Every other string is
        # display data and must pass through LORE's secret scrubber.
        record.update({key: scrub(item[key]) if isinstance(item[key], str) else item[key]
                       for key in _PENDING_FIELDS if item.get(key) is not None})
        record.update({key: scrub(str(item[key])) for key in _PENDING_TEXT if item.get(key)})
        records.append(record)
    return records


def serve() -> None:
    lore = _lore()
    ext = _extensions() if lore is not None else None
    _write({"type": "hello", "proto": PROTOCOL_VERSION,
            "capabilities": list(_OPS if ext is not None else _OPS[:2]) if lore is not None else []})
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
            elif op in ("pending", "sync_state", "refresh_interval") and ext is not None:
                cwd = req.get("cwd")
                if op == "pending":
                    offset, limit = req.get("offset", 0), req.get("limit", 50)
                    if (not isinstance(cwd, str) or not cwd or len(cwd) > 4096 or "\x00" in cwd
                            or type(offset) is not int or not 0 <= offset <= 10000
                            or type(limit) is not int or not 0 <= limit <= 50):
                        raise ValueError("invalid pending input")
                    result = _pending(cwd, offset, limit, ext)
                elif op == "sync_state":
                    state = ext[3][1]()
                    result = None if state is None else {
                        "last_pull_age_s": state.last_pull_age_s,
                        "unpushed": state.unpushed,
                        "conflicts": state.conflicts,
                        "unverified": state.unverified,
                    }
                else:
                    result = ext[1]()
                _write({"type": "reply", "id": rid, "ok": True, "value": result})
                continue
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
