# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded JSONL sidecar for Rust DOXA to call external LORE.

This process is deliberately separate from the native runtime. LORE owns the
scrubber and context store; DOXA owns the wire, deadlines, and fallback path.
Only an explicit allow-list of operations is exposed. Never echo input or
exception text in an error response, stderr, or a process argument.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

MAX_FRAME_BYTES = 1024 * 1024
PROTOCOL_VERSION = 1
_OPS = ("scrub", "snapshot", "pending", "sync_state", "refresh_interval", "transcript_identity")
_READ_OPS = ("consult", "beliefs", "evidence")
_REVIEW_OP = "pending_review_v1"
_RESOLVE_OP = "resolve_reviewed_v1"
_INDEX_OP = "index_transcript_v1"
_SESSION_SEARCH_OP = "session_search_v1"
_MEMORY_USAGE_OP = "memory_usage_v1"
_BELIEF_REVIEW_OP = "belief_review_v1"
_BELIEF_ACTION_OP = "belief_action_v1"
_PENDING_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z", re.ASCII)
_SESSION_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z-]{0,127}\Z", re.ASCII)
_MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
# A raw ASCII control byte can expand to six JSON bytes (\\u00XX). Reserve
# reply metadata too; review must return the exact bytes for SHA/inode checks.
_MAX_REVIEW_RAW_BYTES = (MAX_FRAME_BYTES - 512) // 6
_MAX_MEMORY_SOURCE_BYTES = 1024 * 1024


class PendingReviewError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


class BeliefActionError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code

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


def _memory_usage_ops() -> tuple[Any, Any, Any, Any, Any] | None:
    """LORE owns project identity, memory paths, and canonical entry rendering."""
    try:
        from . import _lore_bootstrap  # noqa: F401 -- choose LORE and root first
        from lore_core.config import project_slug
        from lore_core.memory import memory_cap, memory_path, read_entries, render_entries
        return project_slug, memory_path, read_entries, render_entries, memory_cap
    except Exception:  # noqa: BLE001 -- optional on older LORE builds
        return None


def _memory_usage(cwd: str, ops: tuple[Any, Any, Any, Any, Any]) -> dict[str, int]:
    """Exact Unicode chars and LORE caps for curated entries, no content."""
    if not isinstance(cwd, str) or not cwd or len(cwd) > 4096 or "\x00" in cwd:
        raise ValueError("invalid memory usage input")
    slug = ops[0](cwd)
    result = {}
    for scope in ("project", "user"):
        cap = ops[4](scope)
        if type(cap) is not int or not 0 < cap <= _MAX_MEMORY_SOURCE_BYTES:
            raise ValueError("invalid memory cap")
        path = ops[1](scope, slug)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            size = 0
        if size < 0 or size > _MAX_MEMORY_SOURCE_BYTES:
            raise ValueError("memory source too large")
        chars = len(ops[3](ops[2](path)))
        if chars > _MAX_MEMORY_SOURCE_BYTES:
            raise ValueError("memory content too large")
        result[f"{scope}_chars"] = chars
        result[f"{scope}_cap_chars"] = cap
    return result


def _read_ops() -> tuple[Any, Any] | None:
    """LORE's own database and FTS query boundary; no store files are opened here."""
    try:
        from lore_core.store import db_connect, fts_expr
        return db_connect, fts_expr
    except Exception:  # noqa: BLE001 -- older LORE may not expose FTS
        return None


def _session_search(cwd: str, query: str, ops: tuple[Any, Any],
                    ext: tuple[Any, Any, Any, Any], scrub: Any) -> list[dict[str, str]]:
    """Serve existing LORE FTS rows without indexing or reading transcripts."""
    if (not isinstance(cwd, str) or not cwd or len(cwd) > 4096 or "\x00" in cwd
            or not isinstance(query, str) or not query.strip()
            or len(query.encode("utf-8")) > 200
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in query)):
        raise ValueError("invalid session search input")
    exprs = list(dict.fromkeys(expr for expr in
        (ops[1](query), ops[1](query, " OR ")) if expr))
    slug = ext[0](cwd)
    conn = ops[0]()
    try:
        for scope in (slug, None):
            for expr in exprs:
                sql = ("SELECT m.session_id, m.project, "
                       "snippet(msg, 4, '[', ']', '…', 16) "
                       "FROM msg m WHERE msg MATCH ?")
                params: list[Any] = [expr]
                if scope:
                    sql += " AND m.project = ?"
                    params.append(scope)
                sql += " ORDER BY bm25(msg) LIMIT 20"
                rows = conn.execute(sql, params).fetchall()
                if rows:
                    hits: list[dict[str, str]] = []
                    seen: set[tuple[str, str]] = set()
                    for session_id, project, snippet in rows:
                        if (not isinstance(session_id, str) or _SESSION_ID.fullmatch(session_id) is None
                                or not isinstance(project, str) or not project
                                or len(project.encode("utf-8")) > 255 or "/" in project
                                or "\\" in project or any(ord(ch) < 32 for ch in project)
                                or (project, session_id) in seen):
                            continue
                        safe = scrub(str(snippet or ""))
                        if not isinstance(safe, str):
                            raise TypeError("invalid scrub result")
                        hits.append({"session_id": session_id, "project": project,
                                     "snippet": " ".join(safe.split())[:280]})
                        seen.add((project, session_id))
                    return hits
    finally:
        conn.close()
    return []


def _belief_action_ops() -> tuple[Any, Any, Any, Any, Any] | None:
    """LORE's canonical mutation paths, never a DOXA-authored store update."""
    try:
        from lore_core.config import project_slug
        from lore_core.store import db_connect
        from lore_core.beliefs import belief_retract, outcome_counts, record_outcome
        if not all(callable(op) for op in (project_slug, db_connect, belief_retract,
                                            outcome_counts, record_outcome)):
            return None
        return project_slug, db_connect, belief_retract, outcome_counts, record_outcome
    except Exception:  # noqa: BLE001 -- old LORE versions are read only
        return None


def _belief_identity(req: dict[str, Any], ops: tuple[Any, Any, Any, Any, Any],
                     *, require_expected: bool) -> tuple[int, str]:
    cwd, bid = req.get("cwd"), req.get("belief_id")
    if (not isinstance(cwd, str) or not cwd or len(cwd) > 4096 or "\x00" in cwd
            or type(bid) is not int or not 0 < bid <= 2**63 - 1):
        raise BeliefActionError("invalid_request")
    slug = ops[0](cwd)
    if not isinstance(slug, str) or not slug:
        raise BeliefActionError("invalid_request")
    if require_expected:
        expected = req.get("expected")
        if (type(expected) is not dict or set(expected) != {"uid", "subject", "claim_sha256"}
                or not all(isinstance(expected[k], str) for k in expected)
                or not expected["uid"] or len(expected["uid"]) > 128
                or not expected["subject"] or len(expected["subject"]) > 4096
                or re.fullmatch(r"[0-9a-f]{64}", expected["claim_sha256"]) is None):
            raise BeliefActionError("invalid_request")
    return bid, slug


def _belief_checked_row(conn: Any, bid: int, slug: str) -> tuple[str, str, str, str]:
    row = conn.execute(
        "SELECT uid, subject, claim, status FROM beliefs WHERE id = ?", (bid,)
    ).fetchone()
    if row is None:
        raise BeliefActionError("belief_unavailable")
    uid, subject, claim, status = row
    if (not isinstance(uid, str) or not uid or len(uid) > 128
            or not isinstance(subject, str) or not isinstance(claim, str)):
        raise BeliefActionError("belief_unavailable")
    if subject not in ("user", "user-model", f"project:{slug}"):
        raise BeliefActionError("belief_unavailable")
    if status != "active":
        raise BeliefActionError("belief_changed")
    return uid, subject, claim, status


def _belief_review(req: dict[str, Any], ops: tuple[Any, Any, Any, Any, Any],
                   scrub: Any) -> dict[str, Any]:
    bid, slug = _belief_identity(req, ops, require_expected=False)
    conn = ops[1]()
    try:
        uid, subject, claim, _ = _belief_checked_row(conn, bid, slug)
    finally:
        conn.close()
    safe_claim = scrub(claim)
    # An action requires a complete human review. If scrubbing hid part of the
    # claim, keep the secret hidden and refuse to authorize a mutation from an
    # incomplete display.
    if (not isinstance(safe_claim, str) or safe_claim != claim
            or len(safe_claim.encode("utf-8")) > 16384):
        raise BeliefActionError("belief_incomplete")
    return {"id": bid, "uid": uid, "subject": subject, "claim": safe_claim,
            "claim_sha256": hashlib.sha256(claim.encode("utf-8")).hexdigest()}


def _belief_action(req: dict[str, Any], ops: tuple[Any, Any, Any, Any, Any]) -> dict[str, Any]:
    bid, slug = _belief_identity(req, ops, require_expected=True)
    action, note = req.get("action"), req.get("note", "")
    if (action not in ("confirmed", "contradicted", "stale", "retract")
            or not isinstance(note, str) or not note.strip()
            or len(note.encode("utf-8")) > 300 or "\x00" in note):
        raise BeliefActionError("invalid_request")
    conn = ops[1]()
    try:
        # Lock before checking identity so another writer cannot change the row
        # between the review comparison and LORE's canonical mutation call.
        conn.execute("BEGIN IMMEDIATE")
        uid, subject, claim, _ = _belief_checked_row(conn, bid, slug)
        expected = req["expected"]
        if (uid != expected["uid"] or subject != expected["subject"]
                or hashlib.sha256(claim.encode("utf-8")).hexdigest() != expected["claim_sha256"]):
            raise BeliefActionError("belief_changed")
        if action == "retract":
            if not ops[2](conn, bid, note):
                raise BeliefActionError("belief_changed")
        else:
            ops[4](conn, bid, action, "user", note=note)
        counts = ops[3](conn, bid)
        status_row = conn.execute("SELECT status FROM beliefs WHERE id = ?", (bid,)).fetchone()
        if (status_row is None or status_row[0] not in ("active", "dormant", "retracted")
                or len(counts) != 3 or any(type(n) is not int or n < 0 for n in counts)):
            raise BeliefActionError("belief_changed")
        conn.commit()
        return {"status": status_row[0], "retired": status_row[0] != "active",
                "confirmed": counts[0],
                "contradicted": counts[1], "stale": counts[2]}
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _index_ops() -> tuple[Any, Any] | None:
    """Require LORE's descriptor-based indexer; never pass a checked path to reopen."""
    try:
        from lore_core.store import db_connect, index_live_fd
        return db_connect, index_live_fd
    except Exception:  # noqa: BLE001 -- optional on older LORE builds
        return None


def _open_transcript_fd(root: Path, slug: str, session_id: str) -> int:
    """Walk directories without following links and verify the opened inode."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    components = root.parts[1:]
    if root.anchor != "/" or not components or any(part in ("", ".", "..") for part in components):
        raise ValueError("unsafe transcript directory")
    directory_fd = os.open("/", directory_flags)
    try:
        for index, component in enumerate((*components, slug)):
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
            if index >= len(components) - 1:
                metadata = os.fstat(directory_fd)
                if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
                        or metadata.st_mode & 0o002):
                    raise ValueError("unsafe transcript directory")
        transcript_fd = os.open(f"{session_id}.jsonl", file_flags, dir_fd=directory_fd)
        try:
            metadata = os.fstat(transcript_fd)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                    or metadata.st_mode & 0o002 or metadata.st_nlink != 1
                    or metadata.st_size > _MAX_TRANSCRIPT_BYTES):
                raise ValueError("unsafe transcript file")
            return transcript_fd
        except BaseException:
            os.close(transcript_fd)
            raise
    finally:
        os.close(directory_fd)


def _index_transcript(cwd: str, session_id: str, ext: tuple[Any, Any, Any, Any],
                      ops: tuple[Any, Any]) -> dict[str, int]:
    if (not isinstance(cwd, str) or not cwd or len(cwd) > 4096 or "\x00" in cwd
            or not isinstance(session_id, str) or _SESSION_ID.fullmatch(session_id) is None):
        raise ValueError("invalid transcript identity")
    identity = _transcript_identity(cwd, ext)
    root = Path(identity["projects_dir"])
    slug = identity["slug"]
    if (not root.is_absolute() or not isinstance(slug, str) or not slug
            or slug in (".", "..") or "/" in slug or "\\" in slug):
        raise ValueError("invalid project identity")
    project = root / slug
    transcript = project / f"{session_id}.jsonl"
    # LORE derives the project location; the opened descriptor pins the inode
    # while the logical path remains the stable database cursor key.
    transcript_fd = _open_transcript_fd(root, slug, session_id)
    try:
        conn = ops[0]()
        try:
            indexed, consumed = ops[1](conn, transcript_fd, transcript)
        finally:
            conn.close()
    finally:
        os.close(transcript_fd)
    if (type(indexed) is not int or type(consumed) is not int
            or indexed < 0 or consumed < 0):
        raise TypeError("invalid index result")
    return {"indexed": indexed, "consumed": consumed}


def _pending_review_reader() -> tuple[Any, Any] | None:
    """Use LORE's one-descriptor bytes/inode snapshot when that API exists."""
    try:
        from lore_core.pending import ROOT, _pending_bytes_snapshot
        return ROOT, _pending_bytes_snapshot
    except Exception:  # noqa: BLE001 -- older LORE cannot promise this snapshot
        return None


def _pending_resolver() -> tuple[Any, Any, Any] | None:
    """Only new LORE builds own the claim, provenance, and archive transaction."""
    try:
        from lore_core.pending import PendingResolutionError, record_full_review, resolve_reviewed
        if not all(callable(op) for op in (record_full_review, resolve_reviewed)):
            return None
        return record_full_review, resolve_reviewed, PendingResolutionError
    except Exception:  # noqa: BLE001 -- older builds remain read only
        return None


def _pending_review(cwd: str, pid: str, ext: tuple[Any, Any, Any, Any],
                    reader: tuple[Any, Any], expected: Any = None) -> dict[str, Any]:
    if not isinstance(cwd, str) or not cwd or len(cwd) > 4096 or "\x00" in cwd:
        raise PendingReviewError("invalid_request")
    if not isinstance(pid, str) or _PENDING_ID.fullmatch(pid) is None:
        raise PendingReviewError("invalid_request")
    path = reader[0] / "pending" / f"{pid}.json"
    try:
        before = path.lstat()
    except OSError as exc:
        raise PendingReviewError("pending_unavailable") from exc
    if not stat.S_ISREG(before.st_mode):
        raise PendingReviewError("pending_unavailable")
    if before.st_size > _MAX_REVIEW_RAW_BYTES:
        raise PendingReviewError("pending_incomplete")
    snapshot = reader[1](pid)
    if snapshot is None or not isinstance(snapshot, tuple) or len(snapshot) != 2:
        raise PendingReviewError("pending_unavailable")
    data, inode = snapshot
    if not isinstance(data, bytes) or not isinstance(inode, int) or inode <= 0:
        raise PendingReviewError("pending_incomplete")
    # Refuse a replaced path. LORE's helper captures the bytes and inode from
    # one descriptor; this second stat only verifies the name still points to it.
    try:
        after = path.lstat()
    except OSError as exc:
        raise PendingReviewError("pending_changed") from exc
    if len(data) > _MAX_REVIEW_RAW_BYTES:
        raise PendingReviewError("pending_incomplete")
    if (not stat.S_ISREG(after.st_mode) or before.st_ino != inode or after.st_ino != inode
            or before.st_size != len(data) or after.st_size != len(data)
            or before.st_ctime_ns != after.st_ctime_ns):
        raise PendingReviewError("pending_changed")
    try:
        raw = data.decode("utf-8")
        item = json.loads(raw)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise PendingReviewError("pending_incomplete") from exc
    if not isinstance(item, dict):
        raise PendingReviewError("pending_incomplete")
    if item.get("scope") == "project" and item.get("project") != ext[0](cwd):
        raise PendingReviewError("pending_unavailable")
    digest = hashlib.sha256(data).hexdigest()
    if expected is not None:
        if (not isinstance(expected, dict) or set(expected) != {"sha256", "inode"}
                or not isinstance(expected["sha256"], str)
                or type(expected["inode"]) is not int or expected["inode"] <= 0):
            raise PendingReviewError("invalid_request")
        if expected["sha256"] != digest or expected["inode"] != inode:
            raise PendingReviewError("pending_changed")
    return {"pid": pid, "raw": raw, "sha256": digest, "inode": inode, "complete": True}


def _resolve_reviewed(req: dict[str, Any], ext: tuple[Any, Any, Any, Any],
                      reader: tuple[Any, Any], resolver: tuple[Any, Any, Any]) -> dict[str, Any]:
    decision = req.get("decision")
    if decision not in ("approve", "reject") or type(req.get("expected")) is not dict:
        raise PendingReviewError("invalid_request")
    expected = req["expected"]
    # This read checks project visibility and the exact already displayed
    # digest/inode; LORE then atomically claims and checks again before apply.
    review = _pending_review(req.get("cwd"), req.get("pid"), ext, reader, expected)
    try:
        if decision == "approve":
            resolver[0](review["pid"], review["sha256"], review["inode"])
        resolver[1](review["pid"], review["sha256"], review["inode"], decision)
    except resolver[2] as exc:
        return {"status": "refused", "error": exc.code, "applied": exc.applied}
    return {"status": "approved" if decision == "approve" else "rejected"}


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
    try:
        row = conn.execute(
            "SELECT b.id, b.claim, b.confidence, bm25(belief_fts) "
            "FROM beliefs b JOIN belief_fts f ON b.id = f.belief_id "
            "WHERE belief_fts MATCH ? AND b.status = 'active' "
            "ORDER BY bm25(belief_fts) LIMIT 1", (expression,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    claim = scrub(str(row[1]))
    return {"id": int(row[0]), "claim": claim[:240], "claim_truncated": len(claim) > 240,
            "confidence": float(row[2]), "score": float(row[3]),
            "citation_status": "cite_only"}


def _beliefs(offset: int, limit: int, read_ops: tuple[Any, Any], scrub: Any) -> list[dict]:
    conn = read_ops[0]()
    try:
        rows = conn.execute(
            "SELECT b.id, b.subject, b.claim, b.confidence, "
            "(SELECT count(*) FROM belief_evidence e WHERE e.belief_id = b.id) "
            "FROM beliefs b WHERE b.status = 'active' "
            "ORDER BY b.updated DESC, b.id LIMIT ? OFFSET ?", (limit, offset),
        ).fetchall()
    finally:
        conn.close()
    result = []
    for row in rows:
        claim = scrub(str(row[2]))
        result.append({"id": int(row[0]), "subject": scrub(str(row[1])),
                       "claim": claim[:4096], "claim_truncated": len(claim) > 4096,
                       "confidence": float(row[3]), "evidence_count": int(row[4])})
    return result


def _evidence(belief_id: int, offset: int, limit: int, read_ops: tuple[Any, Any], scrub: Any) -> list[dict]:
    conn = read_ops[0]()
    try:
        have_engine = any(row[1] == "source_engine" for row in conn.execute(
            "PRAGMA table_info(belief_evidence)").fetchall())
        rows = conn.execute(
            "SELECT session_id, project, note, created, "
            f"{'source_engine' if have_engine else 'NULL'} FROM belief_evidence "
            "WHERE belief_id = ? ORDER BY created, rowid LIMIT ? OFFSET ?",
            (belief_id, limit + 1, offset),
        ).fetchall()
    finally:
        conn.close()
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
    memory_ops = _memory_usage_ops() if lore is not None else None
    read_ops = _read_ops() if lore is not None else None
    belief_ops = _belief_action_ops() if lore is not None else None
    index_ops = _index_ops() if lore is not None and ext is not None else None
    review = _pending_review_reader() if lore is not None and ext is not None else None
    resolver = _pending_resolver() if review is not None else None
    _write({"type": "hello", "proto": PROTOCOL_VERSION,
            "capabilities": (list(_OPS if ext is not None else _OPS[:2])
                             + (list(_READ_OPS) if read_ops is not None else [])
                             + ([_INDEX_OP] if index_ops is not None else [])
                             + ([_SESSION_SEARCH_OP] if read_ops is not None and ext is not None else [])
                             + ([_MEMORY_USAGE_OP] if memory_ops is not None else [])
                             + ([_REVIEW_OP] if review is not None else [])
                             + ([_RESOLVE_OP] if resolver is not None else [])
                             + ([_BELIEF_REVIEW_OP, _BELIEF_ACTION_OP] if belief_ops is not None else [])) if lore is not None else []})
    while True:
        raw = sys.stdin.buffer.readline(MAX_FRAME_BYTES + 1)
        if not raw:
            return
        if len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
            return  # The stream is no longer framed; do not resynchronize blindly.
        try:
            req = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
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
            if op == _BELIEF_REVIEW_OP and belief_ops is not None:
                result = _belief_review(req, belief_ops, scrub)
                _write({"type": "reply", "id": rid, "ok": True, "value": result})
                continue
            if op == _BELIEF_ACTION_OP and belief_ops is not None:
                result = _belief_action(req, belief_ops)
                _write({"type": "reply", "id": rid, "ok": True, "value": result})
                continue
            if op == _MEMORY_USAGE_OP and memory_ops is not None:
                result = _memory_usage(req.get("cwd"), memory_ops)
                _write({"type": "reply", "id": rid, "ok": True, "value": result})
                continue
            if op == _INDEX_OP and ext is not None and index_ops is not None:
                result = _index_transcript(req.get("cwd"), req.get("session_id"), ext, index_ops)
                _write({"type": "reply", "id": rid, "ok": True, "value": result})
                continue
            if op == _SESSION_SEARCH_OP and ext is not None and read_ops is not None:
                result = _session_search(req.get("cwd"), req.get("query"), read_ops, ext, scrub)
                _write({"type": "reply", "id": rid, "ok": True, "value": result})
                continue
            if op == _REVIEW_OP and ext is not None and review is not None:
                result = _pending_review(req.get("cwd"), req.get("pid"), ext, review,
                                         req.get("expected"))
                _write({"type": "reply", "id": rid, "ok": True, "value": result})
                continue
            if op == _RESOLVE_OP and ext is not None and review is not None and resolver is not None:
                result = _resolve_reviewed(req, ext, review, resolver)
                _write({"type": "reply", "id": rid, "ok": True, "value": result})
                continue
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
                    offset, limit = _valid_page(req, 50)
                    result = _evidence(belief_id, offset, limit, read_ops, scrub)
                _write({"type": "reply", "id": rid, "ok": True, "value": result})
                continue
            else:
                _write({"type": "reply", "id": rid, "ok": False, "error": "invalid_request"})
                continue
            if not isinstance(result, str):
                raise TypeError("invalid LORE result")
            _write({"type": "reply", "id": rid, "ok": True, "text": result})
        except PendingReviewError as exc:
            _write({"type": "reply", "id": rid, "ok": False, "error": exc.code})
        except BeliefActionError as exc:
            _write({"type": "reply", "id": rid, "ok": False, "error": exc.code})
        except Exception:  # noqa: BLE001 -- never print credentials from input or LORE
            _write({"type": "reply", "id": rid, "ok": False, "error": "operation_failed"})


if __name__ == "__main__":
    serve()
