# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded native LORE carrier for retained Python SDK and MCP adapters.

No Python memory backend or fallback. The CLI owns canonical store operations;
this adapter owns one process group, finite JSONL frames and response deadlines.
"""
from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import threading
import time
import weakref
from typing import Any

MAX_FRAME_BYTES = 1024 * 1024
AGENT_FRAME_BYTES = 64 * 1024
LORE_TOOLS = frozenset(("lore_belief_search", "lore_belief_show", "lore_belief_neighbours",
                       "lore_memory_list", "lore_session_search", "lore_remember"))


_clients = weakref.WeakSet()


def _close_all() -> None:
    for client in tuple(_clients):
        client.close()


atexit.register(_close_all)


class NativeLoreError(RuntimeError):
    """Fixed diagnostics never include request payloads or backend exceptions."""


class NativeOperationError(NativeLoreError):
    pass


def executable() -> str:
    explicit = os.environ.get("DOXA_LORE_RS", "").strip()
    candidate = explicit or shutil.which("lore-rs")
    if not candidate:
        raise NativeLoreError("native_lore_unavailable")
    return candidate


class Carrier:
    def __init__(self, *, agent: bool = False, timeout: float = 3.0):
        if not 0 < timeout <= 300:
            raise NativeLoreError("invalid_timeout")
        self.timeout = timeout
        self.agent = agent
        self.limit = AGENT_FRAME_BYTES if agent else MAX_FRAME_BYTES
        self.process = None
        self.buffer = bytearray()
        self.next_id = 1
        self.lock = threading.RLock()
        self.capabilities = frozenset()
        _clients.add(self)

    def _start(self, deadline: float) -> None:
        if self.process is not None:
            return
        try:
            self.process = subprocess.Popen([executable(), "agent-bridge" if self.agent else "bridge"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                start_new_session=True)
            os.set_blocking(self.process.stdin.fileno(), False)
            os.set_blocking(self.process.stdout.fileno(), False)
            hello = self._receive(deadline)
            if hello.get("type") != "hello" or hello.get("proto") != 1:
                raise NativeLoreError("invalid_native_frame")
            caps = hello.get("capabilities")
            if not isinstance(caps, list) or len(caps) > 128 or any(not isinstance(cap, str) or len(cap) > 128 for cap in caps):
                raise NativeLoreError("invalid_native_frame")
            required = ("agent_catalog_v1", "agent_tool_v1", "agent_status_v1") if self.agent else ("scrub", "snapshot")
            if not all(name in caps for name in required):
                raise NativeLoreError("native_lore_unavailable")
            self.capabilities = frozenset(caps)
        except (OSError, ValueError, NativeLoreError):
            self.close()
            raise NativeLoreError("native_lore_unavailable") from None

    def _receive(self, deadline: float) -> dict:
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while True:
                newline = self.buffer.find(b"\n")
                if newline >= 0:
                    raw = bytes(self.buffer[:newline])
                    del self.buffer[:newline + 1]
                    try:
                        value = json.loads(raw)
                    except (ValueError, UnicodeError, RecursionError):
                        raise NativeLoreError("invalid_native_frame") from None
                    if not isinstance(value, dict):
                        raise NativeLoreError("invalid_native_frame")
                    return value
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise NativeLoreError("native_lore_timeout")
                chunk = os.read(self.process.stdout.fileno(), min(65536, self.limit + 1 - len(self.buffer)))
                if not chunk:
                    raise NativeLoreError("native_lore_closed")
                self.buffer.extend(chunk)
                if len(self.buffer) > self.limit:
                    raise NativeLoreError("native_frame_too_large")

    def request(self, op: str, **fields: Any) -> Any:
        if not self.lock.acquire(timeout=self.timeout):
            raise NativeLoreError("native_lore_timeout")
        try:
            deadline = time.monotonic() + self.timeout
            self._start(deadline)
            if op not in self.capabilities:
                raise NativeLoreError("native_operation_unavailable")
            rid = self.next_id
            if rid >= 2**64:
                raise NativeLoreError("native_identity_exhausted")
            self.next_id += 1
            try:
                raw = (json.dumps({**fields, "op":op, "id":rid}, ensure_ascii=False, allow_nan=False) + "\n").encode()
            except (ValueError, TypeError, RecursionError):
                raise NativeLoreError("invalid_native_request") from None
            if len(raw) > self.limit:
                raise NativeLoreError("native_frame_too_large")
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdin, selectors.EVENT_WRITE)
                offset = 0
                while offset < len(raw):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise NativeLoreError("native_lore_timeout")
                    try:
                        count = os.write(self.process.stdin.fileno(), raw[offset:])
                    except BlockingIOError:
                        continue
                    if not count:
                        raise NativeLoreError("native_lore_closed")
                    offset += count
            reply = self._receive(deadline)
            if reply.get("type") != "reply" or type(reply.get("id")) is not int or reply["id"] != rid or type(reply.get("ok")) is not bool:
                raise NativeLoreError("invalid_native_frame")
            if not reply["ok"]:
                # Backend codes are fixed, but never trust a future carrier to
                # put arbitrary source text into an exception shown by DOXA.
                code = reply.get("error")
                if code not in {"invalid_request", "operation_failed", "unsafe_path", "output_too_large", "timeout",
                    "review_changed", "over_cap", "untrusted_write", "unavailable_operation", "pending_changed",
                    "pending_incomplete", "pending_unavailable", "belief_changed", "belief_unavailable", "belief_incomplete",
                    "memory_incomplete", "memory_changed", "memory_ambiguous", "memory_over_cap", "memory_refused"}:
                    code = "native_operation_failed"
                raise NativeOperationError(code)
            if op in ("scrub", "snapshot"):
                value = reply.get("text")
                if not isinstance(value, str):
                    raise NativeLoreError("invalid_native_frame")
                return value
            if "value" not in reply:
                raise NativeLoreError("invalid_native_frame")
            return reply["value"]
        except (OSError, ValueError):
            self.close()
            raise NativeLoreError("native_lore_io") from None
        except NativeOperationError:
            raise
        except NativeLoreError:
            self.close()
            raise
        finally:
            self.lock.release()

    def __del__(self):
        if hasattr(self, "lock"):
            self.close()

    def close(self) -> None:
        # Never poll/reap the leader before signaling its owned process group.
        with self.lock:
            process, self.process = self.process, None
            self.buffer.clear()
            if process is None:
                return
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()


_default = Carrier()


def request(op: str, **fields: Any) -> Any:
    # This narrow configuration export never reads LORE memory itself.
    from ._lore_bootstrap import export_sticky_lore_root, export_shared_memory_caps
    export_sticky_lore_root()
    export_shared_memory_caps()
    return _default.request(op, **fields)


def request_rows(op: str, *, limit: int, offset: int = 0, **fields: Any) -> list[dict]:
    """A bounded caller window across canonical fifty-row native pages."""
    if (op not in ("beliefs", "pending") or type(limit) is not int or type(offset) is not int
            or not 0 <= limit <= 10000 or not 0 <= offset <= 10000):
        raise NativeLoreError("invalid_native_request")
    rows: list[dict] = []
    byte_count = 0
    remaining = min(limit, 10000 - offset)
    while remaining:
        count = min(50, remaining)
        page = request(op, offset=offset, limit=count, **fields)
        if (not isinstance(page, list) or len(page) > count
                or any(not isinstance(row, dict) for row in page)):
            raise NativeLoreError("invalid_native_frame")
        byte_count += len(json.dumps(page, ensure_ascii=False, allow_nan=False).encode())
        if byte_count > 16 * MAX_FRAME_BYTES:
            raise NativeLoreError("native_frame_too_large")
        rows.extend(page)
        if len(page) < count:
            break
        remaining -= len(page)
        offset += len(page)
    return rows


def capabilities() -> frozenset[str]:
    # This operation is configuration-only; never initializes a memory store.
    request("refresh_interval")
    return _default.capabilities


def scrub(text: str) -> str:
    return request("scrub", text=text)


class Agent:
    def __init__(self, *, session_id: str, cwd: str, engine: str, spawn_depth: int = 0):
        self.identity = {"session_id":session_id, "cwd":cwd, "source_engine":engine,
                         "spawn_depth":spawn_depth, "lore":True}
        self.carrier = Carrier(agent=True, timeout=5)
        self.catalog = None

    def tools(self) -> list[dict]:
        if self.catalog is None or self.carrier.process is None:
            from ._lore_bootstrap import export_sticky_lore_root, export_shared_memory_caps
            export_sticky_lore_root(); export_shared_memory_caps()
            rows = self.carrier.request("agent_catalog_v1", identity=self.identity)
            if (not isinstance(rows, list) or len(rows) > 6
                    or any(not isinstance(row, dict) or row.get("name") not in LORE_TOOLS
                        or not isinstance(row.get("description"), str) or len(row["description"]) > 8192
                        or any(ord(char) < 32 for char in row["description"])
                        or not isinstance(row.get("inputSchema"), dict) or row["inputSchema"].get("type") != "object" for row in rows)
                    or len({row["name"] for row in rows}) != len(rows)):
                raise NativeLoreError("invalid_native_catalog")
            self.catalog = rows
        return self.catalog

    def call(self, name: str, arguments: dict) -> Any:
        self.tools()  # Binding happens exactly once before any operator call.
        if name not in {row["name"] for row in self.catalog} or not isinstance(arguments, dict):
            return {"error":"native LORE operator is unavailable"}
        try:
            encoded = json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode()
        except (ValueError, TypeError, RecursionError):
            return {"error":f"bad arguments for {name}: expected finite JSON"}
        if len(encoded) > 32 * 1024:
            return {"error":f"bad arguments for {name}: arguments exceed limit"}
        try:
            return self.carrier.request("agent_tool_v1", identity=self.identity, name=name, arguments=arguments)
        except NativeOperationError as error:
            if str(error) == "invalid_request":
                return {"error":f"bad arguments for {name}: native validation refused arguments"}
            if str(error) in {"over_cap", "untrusted_write", "unsafe_path", "unavailable_operation"}:
                return {"error":f"{name}: request refused"}
            raise

    def status(self) -> dict:
        self.tools()
        result = self.carrier.request("agent_status_v1", identity=self.identity)
        if not isinstance(result, dict):
            raise NativeLoreError("invalid_native_status")
        count, disabled = result.get("belief_count"), result.get("disabled_tools")
        if (count is not None and (type(count) is not int or not 0 <= count < 2**64)
                or not isinstance(disabled, list) or len(disabled) > 6
                or any(not isinstance(name, str) or name not in LORE_TOOLS for name in disabled)
                or len(set(disabled)) != len(disabled)):
            raise NativeLoreError("invalid_native_status")
        return result


def runtime_config() -> dict:
    """Canonical path and stage metadata; construction never opens the store."""
    value = request("runtime_config_v1")
    if not isinstance(value, dict):
        raise NativeLoreError("invalid_native_frame")
    for key in ("root", "projects_dir"):
        raw = value.get(key)
        if not isinstance(raw, str) or len(raw) > 4096 or "\0" in raw or not Path(raw).is_absolute():
            raise NativeLoreError("invalid_native_frame")
    stages = value.get("disabled_stages")
    if not isinstance(stages, list) or len(stages) > 5 or any(
            stage not in ("inject", "index", "review", "beliefs", "skills") for stage in stages):
        raise NativeLoreError("invalid_native_frame")
    return value


def transcript_identity(cwd: str) -> tuple[Path, str]:
    """Use native Git/worktree identity rather than a Python Git subprocess."""
    value = request("transcript_identity", cwd=cwd)
    if not isinstance(value, dict):
        raise NativeLoreError("invalid_native_frame")
    root, slug = value.get("projects_dir"), value.get("slug")
    if (not isinstance(root, str) or len(root) > 4096 or "\0" in root or not Path(root).is_absolute()
            or not isinstance(slug, str) or not 0 < len(slug) <= 1020 or slug == "." or ".." in slug
            or any(char in slug for char in ("/", "\\", "\0"))):
        raise NativeLoreError("invalid_native_frame")
    return Path(root), slug


def stage_disabled(stage: str) -> bool:
    try:
        return stage in runtime_config()["disabled_stages"]
    except NativeLoreError:
        return True


def root_path() -> str:
    return runtime_config()["root"]
