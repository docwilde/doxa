# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded native-host access to DOXA's existing LORE operator registry.

The engine binds one identity before listing tools. Tool arguments never carry
that identity, and ToolGate injects the canonical OperatorContext itself.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path

from .identity import valid_session_id

MAX_FRAME_BYTES = 64 * 1024
LORE_TOOLS = frozenset(("lore_belief_search", "lore_belief_show",
                       "lore_belief_neighbours", "lore_memory_list",
                       "lore_session_search", "lore_remember"))


class AgentOperators:
    def __init__(self) -> None:
        self.identity = None
        self.server = None

    def bind(self, identity: object) -> None:
        if not isinstance(identity, dict) or set(identity) != {
            "session_id", "cwd", "source_engine", "spawn_depth", "lore"
        }:
            raise ValueError("invalid native identity")
        cwd = identity["cwd"]
        if (not isinstance(cwd, str) or not cwd or len(cwd) > 4096
                or any(ord(char) < 32 for char in cwd)
                or not Path(cwd).is_absolute() or not Path(cwd).is_dir()
                or not valid_session_id(identity["session_id"])
                or identity["source_engine"] not in ("codex", "deepseek", "glm")
                or type(identity["spawn_depth"]) is not int
                or not 0 <= identity["spawn_depth"] <= 128
                or identity["lore"] is not True):
            raise ValueError("invalid native identity")
        if self.identity is not None:
            if identity != self.identity:
                raise ValueError("native identity changed")
            return
        from .mcpserver import Identity, OperatorSurface

        self.server = OperatorSurface(Identity(**identity))
        self.server.gate.allowed = set(LORE_TOOLS)
        self.identity = dict(identity)

    def catalog(self, identity: object) -> list[dict]:
        self.bind(identity)
        return [tool for tool in self.server.tools() if tool["name"] in LORE_TOOLS]

    def status(self, identity: object) -> dict:
        self.bind(identity)
        count = None
        try:
            with contextlib.closing(self.server.ctx["belief_store"]()) as conn:
                count = conn.execute("SELECT count(*) FROM beliefs WHERE status = 'active'").fetchone()[0]
        except Exception:
            pass  # an unavailable store is unknown, never a fabricated zero
        return {"belief_count": count, "disabled_tools": self.server.gate.disabled_tools()}

    async def call(self, identity: object, name: object, arguments: object) -> dict:
        self.bind(identity)
        if name not in LORE_TOOLS or not isinstance(arguments, dict):
            raise ValueError("unavailable native operator")
        # Configuredness and the canonical two-strikes tracker are authoritative.
        if name not in {tool["name"] for tool in self.server.tools()}:
            return {"error": "LORE operator is unavailable in this session"}
        return await self.server.call(name, arguments)


def _write(frame: dict) -> None:
    encoded = (json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    if len(encoded) > MAX_FRAME_BYTES:
        encoded = (json.dumps({"type":"reply", "id":frame.get("id"),
                               "ok":False, "error":"output_too_large"}) + "\n").encode()
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def serve() -> None:
    operators = AgentOperators()
    _write({"type":"hello", "proto":1,
            "capabilities":["agent_catalog_v1", "agent_tool_v1", "agent_status_v1"]})
    for raw in iter(lambda: sys.stdin.buffer.readline(MAX_FRAME_BYTES + 1), b""):
        if len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
            return
        rid = None
        try:
            request = json.loads(raw)
            if not isinstance(request, dict) or type(request.get("id")) is not int:
                continue
            rid = request["id"]
            if not 0 <= rid < 2**64:
                continue
            with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                if request.get("op") == "agent_catalog_v1":
                    result = operators.catalog(request.get("identity"))
                elif request.get("op") == "agent_status_v1":
                    result = operators.status(request.get("identity"))
                elif request.get("op") == "agent_tool_v1":
                    result = asyncio.run(operators.call(request.get("identity"),
                        request.get("name"), request.get("arguments")))
                else:
                    raise ValueError("unsupported operation")
                from lore_core.scrub import scrub_secrets
                # Results and errors cross the same secret boundary as native
                # transcripts, while JSON shape and numeric identities survive.
                def clean(value):
                    if isinstance(value, str): return scrub_secrets(value)
                    if isinstance(value, dict): return {key:clean(item) for key,item in value.items()}
                    if isinstance(value, list): return [clean(item) for item in value]
                    return value
                result = clean(result)
            reply = {"type":"reply", "id":rid, "ok":True, "value":result}
        except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
            if rid is None: continue
            reply = {"type":"reply", "id":rid, "ok":False, "error":"invalid_request"}
        except Exception:
            reply = {"type":"reply", "id":rid, "ok":False, "error":"operation_failed"}
        _write(reply)


if __name__ == "__main__":
    from .native_lore import executable
    os.execv(executable(), [executable(), "agent-bridge"])
