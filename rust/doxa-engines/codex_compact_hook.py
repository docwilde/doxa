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

MAX_INPUT = 64 * 1024
MAX_ROLLOUT = 32 * 1024 * 1024
MAX_LINE = 1024 * 1024
REVIEW_TIMEOUT = 180
REVIEW_DEADLINE = 210  # includes imports/job construction; hook timeout is 240


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
        key = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if len(data) > limit or key(before) != key(after):
            raise ValueError("review source changed")
        current = os.stat(path, follow_symlinks=False)
        if key(after) != key(current):
            raise ValueError("review source replaced")
        return data, key(before)
    finally:
        os.close(fd)


def run_worker(metadata, timeout=REVIEW_TIMEOUT):
    source = globals().get("REVIEW_SUPERVISOR_SOURCE")
    if not isinstance(source, str) or not source:
        return False  # Only the binary's digest-verified supervisor can run.
    if not isinstance(metadata, dict):
        return False
    raw = json.dumps(metadata, ensure_ascii=False, allow_nan=False)
    if len(raw.encode("utf-8")) + 1 > 16 * 1024:
        return False
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", "import json,sys; namespace={'__name__':'doxa_review_supervisor'}; "
         "exec(compile(sys.argv[1],'<verified DOXA supervisor>','exec'),namespace); "
         "sys.exit(namespace['supervise'](json.loads(sys.argv[2]),'codex',float(sys.argv[3])))",
         source, raw, str(timeout)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        return process.wait(timeout=timeout + 5) == 0
    except subprocess.TimeoutExpired:
        return False
    finally:
        # EOF also happens on hook SIGKILL. The detached supervisor retains
        # and reaps its owned worker group, including a reviewer's CLI children.
        process.stdin.close()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def review(manifest_path, event, worker=run_worker):
    descriptor, _ = safe_read(manifest_path, MAX_INPUT)
    manifest = json.loads(descriptor)
    if (manifest.get("version") != "0.156.1" or event.get("hook_event_name") != "PreCompact"
            or event.get("trigger") not in ("auto", "manual")
            or not manifest.get("provider_thread")
            or event.get("session_id") != manifest["provider_thread"]):
        return False
    if manifest.get("lore_enabled") is False:
        return False  # Memory-off cannot complete the required review; block compaction.
    if (os.environ.get("LORE_DISABLE_REVIEW", "") not in ("", "0")
            or os.environ.get("LORE_SKIP", "")):
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
    cwd = manifest.get("cwd")
    session_id = manifest.get("doxa_session")
    if (not isinstance(cwd, str) or not Path(cwd).is_absolute()
            or not isinstance(session_id, str) or not session_id
            or len(session_id) > 128 or any(not (c.isascii() and (c.isalnum() or c in "-_")) for c in session_id)):
        return False
    # The native reviewer opens this owned rollout without following any path
    # symlinks, verifies the provider thread/cwd and this exact proof BEFORE
    # provider work, then verifies its frozen proof again before any effects.
    # Python carries metadata only; digest, scrubbing and derivation are native.
    metadata = {"cwd": cwd, "session_id": session_id,
                "provider_thread": manifest["provider_thread"],
                "transcript": str(source), "older": True,
                "expected_source": {"sha256": hashlib.sha256(data).hexdigest(),
                    "device": before[0], "inode": before[1], "size": before[2],
                    "ctime": before[4] // 1_000_000_000,
                    "ctime_nsec": before[4] % 1_000_000_000}}
    approved = worker(metadata)
    # A completed review of an outdated transcript cannot authorize compaction.
    current_data, current = safe_read(source, MAX_ROLLOUT)
    return bool(approved and current == before and
                hashlib.sha256(current_data).digest() == hashlib.sha256(data).digest())


def main():
    result = False
    def expired(_number, _frame):
        raise TimeoutError("bounded LORE review deadline")
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, REVIEW_DEADLINE)
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) <= MAX_INPUT:
            result = review(sys.argv[1], json.loads(raw))
    except BaseException:
        result = False
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    return {"continue": result, "suppressOutput": True,
            **({} if result else {"stopReason": "DOXA LORE review did not complete; compaction blocked"})}
