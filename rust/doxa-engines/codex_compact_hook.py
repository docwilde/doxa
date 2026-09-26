"""DOXA-owned Codex 0.156.1 PreCompact command; stdout is hook JSON only.

The outer pinned bootstrap catches syntax/import failures in this file. This
module returns blocking JSON for expected review failures before Codex's hook
deadline. Codex itself treats OS-level hook failure as fail-open; the app-server
controller must abort on hook/completed failure notifications.
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time

MAX_INPUT = 64 * 1024
MAX_ROLLOUT = 32 * 1024 * 1024
MAX_LINE = 1024 * 1024
REVIEW_TIMEOUT = 180


def safe_read(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or before.st_size > limit):
            raise ValueError("unsafe review source")
        with os.fdopen(fd, "rb", closefd=False) as file:
            data = file.read(limit + 1)
        after = os.fstat(fd)
        key = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if len(data) > limit or key(before) != key(after):
            raise ValueError("review source changed")
        current = os.stat(path, follow_symlinks=False)
        if key(after) != key(current):
            raise ValueError("review source replaced")
        return data, key(before)
    finally:
        os.close(fd)


def messages_from_rollout(data, scrub):
    rows = []
    for line in data.splitlines():
        if len(line) > MAX_LINE:
            raise ValueError("review line too large")
        if not line:
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError("invalid rollout record")
        if row.get("type") != "response_item":
            continue
        item = row.get("payload")
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        role = item.get("role")
        if role not in ("user", "assistant"):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            raise ValueError("invalid provider message")
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("input_text", "output_text"):
                text = block.get("text")
                if not isinstance(text, str):
                    raise ValueError("invalid provider text")
                texts.append(scrub(text))
        if texts:
            rows.append({"type": role, "message": {"role": role,
                         "content": [{"type": "text", "text": "\n".join(texts)}]}})
    if not rows:
        raise ValueError("no reviewable provider messages")
    return rows


def run_worker(job, lore_parent, timeout=REVIEW_TIMEOUT):
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", "import sys; from pathlib import Path; "
         "sys.path.insert(0, sys.argv[2]); from lore_core.deriver import worker_run; "
         "sys.exit(worker_run(Path(sys.argv[1])))", str(job), str(lore_parent)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        return process.wait(timeout=timeout) == 0
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        return False


def review(manifest_path, event, worker=run_worker):
    descriptor, _ = safe_read(manifest_path, MAX_INPUT)
    manifest = json.loads(descriptor)
    if (manifest.get("version") != "0.156.1" or event.get("hook_event_name") != "PreCompact"
            or event.get("trigger") not in ("auto", "manual")
            or not manifest.get("provider_thread")
            or event.get("session_id") != manifest["provider_thread"]):
        return False
    if os.environ.get("LORE_DISABLE_REVIEW", "").strip():
        return False
    source = Path(event.get("transcript_path") or "")
    if not source.is_absolute() or source.parent == source or source.resolve(strict=True) != source:
        return False
    # Codex supplies a rollout under its selected CODEX_HOME. Never let a
    # provider hook choose some other same-user JSONL file for review.
    root = Path(manifest["codex_home"]).resolve()
    if not source.is_relative_to(root / "sessions") and not source.is_relative_to(root / "archived_sessions"):
        return False
    data, before = safe_read(source, MAX_ROLLOUT)
    if not data:
        return False
    first = json.loads(data.splitlines()[0])
    if first.get("type") != "session_meta" or first.get("payload", {}).get("id") != manifest["provider_thread"]:
        return False
    # Isolated Python imports DOXA from this interpreter's installed package,
    # never from the provider cwd. Its canonical bootstrap selects the configured
    # plugin/package implementation and sticky LORE store before lore_core loads.
    from doxa import _lore_bootstrap
    _lore_bootstrap.ensure_importable()
    _lore_bootstrap.export_sticky_lore_root()
    from lore_core import deriver
    from lore_core.config import project_slug, stage_disabled
    from lore_core.scrub import scrub_secrets
    if stage_disabled("review"):
        return False
    rows = messages_from_rollout(data, scrub_secrets)
    workspace = Path(manifest_path).parent
    with tempfile.TemporaryDirectory(prefix="review-", dir=workspace) as directory:
        snapshot = Path(directory) / (manifest["doxa_session"] + ".jsonl")
        with snapshot.open("x", encoding="utf-8") as file:
            for row in rows:
                file.write(json.dumps(row, ensure_ascii=False) + "\n")
        snapshot.chmod(0o600)
        # This immutable copy is what the worker reads, never the live rollout.
        snapshot_bytes, _ = safe_read(snapshot, MAX_ROLLOUT)
        snapshot_hash = hashlib.sha256(snapshot_bytes).hexdigest()
        job = deriver.build_review_job(snapshot, project_slug(manifest["cwd"]),
                                       cwd_hint=manifest["cwd"], older=True)
        if job is None:
            approved = True  # the reviewer's explicit minimum-message rule
        else:
            job["source_engine"] = "codex"
            jobfile = Path(directory) / "job.json"
            jobfile.write_text(json.dumps(job), encoding="utf-8")
            jobfile.chmod(0o600)
            lore_parent = Path(deriver.__file__).resolve().parent.parent
            approved = worker(jobfile, lore_parent)
        # A reviewer cannot turn an outdated snapshot into authorization.
        current_data, current = safe_read(source, MAX_ROLLOUT)
        unchanged = current == before and hashlib.sha256(current_data).digest() == hashlib.sha256(data).digest()
        snapshot_now, _ = safe_read(snapshot, MAX_ROLLOUT)
        return bool(approved and unchanged and hashlib.sha256(snapshot_now).hexdigest() == snapshot_hash)


def main():
    result = False
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) <= MAX_INPUT:
            result = review(sys.argv[1], json.loads(raw))
    except BaseException:
        result = False
    return {"continue": result, "suppressOutput": True,
            **({} if result else {"stopReason": "DOXA LORE review did not complete; compaction blocked"})}
