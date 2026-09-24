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
_OPS = ("scrub", "snapshot", "pending", "sync_state", "refresh_interval", "transcript_identity")
_READ_OPS = ("consult", "beliefs", "evidence")

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


def _read_ops() -> tuple[Any, Any] | None:
    """LORE's own database and FTS query boundary; no store files are opened here."""
    try:
        from lore_core.store import db_connect, fts_expr
        return db_connect, fts_expr
    except Exception:  # noqa: BLE001 -- older LORE may not expose FTS
        return None


def _valid_page(req: dict[str, Any], maximum: int) -> tuple[int, int]:
    offset, limit = req.get("offset", 0), req.get("limit", maximum)
    if (type(offset) is not int or not 0 <= offset <= 10000
            or type(limit) is not int or not 0 <= limit <= maximum):
        raise ValueError("invalid page")
    return offset, limit


def _consult(prompt: str, read_ops: tuple[Any, Any], scrub: Any) -> dict | None:
    if not prompt or len(prompt) > 8192:
        raise ValueError("invalid consult prompt")
    expression = read_ops[1](prompt, " OR ")
    if not expression:
        return None
    conn = read_ops[0]()
    row = conn.execute(
        "SELECT b.id, b.claim, b.confidence, bm25(belief_fts) "
        "FROM beliefs b JOIN belief_fts f ON b.id = f.belief_id "
        "WHERE belief_fts MATCH ? AND b.status = 'active' "
        "ORDER BY bm25(belief_fts) LIMIT 1", (expression,),
    ).fetchone()
    if row is None:
        return None
    claim = scrub(str(row[1]))
    return {"id": int(row[0]), "claim": claim[:240], "claim_truncated": len(claim) > 240,
            "confidence": float(row[2]), "score": float(row[3]),
            "citation_status": "cite_only"}


def _beliefs(offset: int, limit: int, read_ops: tuple[Any, Any], scrub: Any) -> list[dict]:
    conn = read_ops[0]()
    rows = conn.execute(
        "SELECT b.id, b.subject, b.claim, b.confidence, "
        "(SELECT count(*) FROM belief_evidence e WHERE e.belief_id = b.id) "
        "FROM beliefs b WHERE b.status = 'active' "
        "ORDER BY b.updated DESC, b.id LIMIT ? OFFSET ?", (limit, offset),
    ).fetchall()
    result = []
    for row in rows:
        claim = scrub(str(row[2]))
        result.append({"id": int(row[0]), "subject": scrub(str(row[1])),
                       "claim": claim[:4096], "claim_truncated": len(claim) > 4096,
                       "confidence": float(row[3]), "evidence_count": int(row[4])})
    return result


def _evidence(belief_id: int, limit: int, read_ops: tuple[Any, Any], scrub: Any) -> list[dict]:
    conn = read_ops[0]()
    have_engine = any(row[1] == "source_engine" for row in conn.execute(
        "PRAGMA table_info(belief_evidence)").fetchall())
    rows = conn.execute(
        "SELECT session_id, project, note, created, "
        f"{'source_engine' if have_engine else 'NULL'} FROM belief_evidence "
        "WHERE belief_id = ? ORDER BY created, rowid LIMIT ?", (belief_id, limit + 1),
    ).fetchall()
    trail = []
    for row in rows[:limit]:
        note = scrub(str(row[2] or ""))
        trail.append({"session_id": scrub(str(row[0] or "")),
                      "project": scrub(str(row[1] or "")),
                      "note": note[:4096], "note_truncated": len(note) > 4096,
                      "created": scrub(str(row[3] or "")),
                      **({"source_engine": scrub(str(row[4]))} if row[4] else {})})
    if len(rows) > limit and trail:
        trail[-1]["trail_truncated"] = True
    return trail


def _scrub_pending_value(value: Any, scrub: Any) -> Any:
    """Preserve JSON structure while scrubbing every nested string."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("invalid pending key")
            safe_key = scrub(key)
            if safe_key in result:
                raise ValueError("pending keys collide after scrubbing")
            result[safe_key] = _scrub_pending_value(child, scrub)
        return result
    if isinstance(value, (list, tuple)):
        return [_scrub_pending_value(child, scrub) for child in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise TypeError("invalid pending value")


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
        record.update({key: _scrub_pending_value(item[key], scrub)
                       for key in _PENDING_FIELDS if item.get(key) is not None})
        record.update({key: scrub(str(item[key])) for key in _PENDING_TEXT if item.get(key)})
        records.append(record)
    return records


def _transcript_identity(cwd: str, ext: tuple[Any, Any, Any, Any]) -> dict[str, str]:
    from lore_core.config import PROJECTS_DIR
    return {"projects_dir": str(PROJECTS_DIR), "slug": ext[0](cwd)}


def serve() -> None:
    lore = _lore()
    ext = _extensions() if lore is not None else None
    read_ops = _read_ops() if lore is not None else None
    _write({"type": "hello", "proto": PROTOCOL_VERSION,
            "capabilities": (list(_OPS if ext is not None else _OPS[:2])
                             + (list(_READ_OPS) if read_ops is not None else [])) if lore is not None else []})
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
            elif op in ("pending", "sync_state", "refresh_interval", "transcript_identity") and ext is not None:
                cwd = req.get("cwd")
                if op == "transcript_identity":
                    if not isinstance(cwd, str) or not cwd or len(cwd) > 4096 or "\x00" in cwd:
                        raise ValueError("invalid transcript identity input")
                    result = _transcript_identity(cwd, ext)
                elif op == "pending":
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
            elif op in _READ_OPS and read_ops is not None:
                if op == "consult":
                    prompt = req.get("prompt")
                    if not isinstance(prompt, str):
                        raise ValueError("invalid consult input")
                    result = _consult(prompt, read_ops, scrub)
                elif op == "beliefs":
                    offset, limit = _valid_page(req, 50)
                    result = _beliefs(offset, limit, read_ops, scrub)
                else:
                    belief_id = req.get("belief_id")
                    if type(belief_id) is not int or not 0 < belief_id <= 2**63 - 1:
                        raise ValueError("invalid belief id")
                    _offset, limit = _valid_page(req, 50)
                    result = _evidence(belief_id, limit, read_ops, scrub)
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
