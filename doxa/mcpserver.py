# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.mcpserver -- DOXA's operator registry as a stdio MCP server process.

``python -m doxa.mcpserver`` speaks MCP over stdin/stdout so that an engine
DOXA does not host in-process can still reach the LORE tools. Today that is
exactly one engine: :mod:`doxa.codex`. ``codex exec`` runs the model in its
own process and offers no in-process tool surface, so the only way DOXA's
operators reach a Codex turn is as an EXTERNAL stdio MCP server the CLI
spawns -- this one.

MEASURED SURFACE (mcp 2.0.0, codex-cli 0.144.4, on the machine this was
built on -- every claim below was run, not read):

* ``mcp`` 2.0.0's low-level server takes its handlers as constructor
  callbacks (``Server(name, on_list_tools=..., on_call_tool=...)``), not as
  decorators; ``mcp.server.stdio.stdio_server()`` yields the read/write
  pair ``Server.run`` wants. There is no ``FastMCP`` in this version --
  the class is ``mcp.server.MCPServer`` -- and it is not used here because
  the tool list has to be COMPUTED per request (see the disable rule
  below), which is the low-level server's shape, not the decorated one's.
* ``codex mcp add NAME --env K=V -- CMD ARGS`` writes exactly
  ``[mcp_servers.NAME] command="CMD" args=["ARGS"]`` plus
  ``[mcp_servers.NAME.env] K = "V"`` -- run against a throwaway
  ``CODEX_HOME`` and read back off the generated ``config.toml``. Those
  are the three keys :meth:`doxa.codex.CodexEngine._mcp_overrides` writes
  as ``-c`` overrides, so the spelling is the CLI's own, not a guess.
* ``default_tools_approval_mode`` is a real key on that table and its
  three values are ``prompt`` / ``writes`` / ``approve`` (the serde
  variant list, read out of the shipped binary). Codex cancels an MCP tool
  call with ``error: {"message": "user cancelled MCP tool call"}`` on
  anything but ``approve`` in a non-interactive ``exec`` run, which is the
  finding :mod:`doxa.codex`'s header paid a live probe for.

WHAT THIS PROCESS IS, AND WHAT IT IS NOT. It is the SAME containment the
vendor engines run, in a second process: one :class:`doxa.gate.ToolGate`,
built the way ``doxa.vendors.ChatApiEngine.start`` builds its own, with a
:class:`doxa.gate.OperatorContext` carrying only values the HOST resolved.
Every ``tools/call`` goes through ``gate.execute`` and nothing else, so the
allowed-set check, the graceful-degradation contract (``execute`` never
raises), the two-strikes tracker and the op_ctx sidecar all apply with no
per-tool wiring. It is NOT a second registry and it does not reimplement
gating -- ``tools/list`` is a projection of ``operators.configured_names``
and ``tools/call`` is one line of dispatch.

What the gate can govern here and what it cannot. It governs every call to
a DOXA operator, because every one of them arrives through this process.
It does NOT govern Codex's OWN tools (its shell, its file edits, its
apply_patch): those never leave the CLI, DOXA never sees them, and
``sandbox_mode`` + ``approval_policy`` are the only controls over them.
``tool_gate=True`` for Codex therefore means "DOXA's tools are contained",
which is the same thing it means for DeepSeek and GLM.

THE DISABLE RULE, precisely. The two-strikes tracker lives on the gate, in
this process, and survives for the life of the server -- which is the life
of ONE ``codex exec`` turn, because Codex spawns the server per run. Two
hard failures inside a turn therefore disable the tool for the rest of
that turn: ``tools/list`` is recomputed on every request and omits the
disabled name, and a client that calls it anyway gets the gate's own
refusal result (``"<name> is disabled for the rest of this session after
repeated failures -- stop calling it"``). A ``notifications/tools/
list_changed`` is attempted after a call that disabled something, but it
is best-effort: mcp 2.0.0 derives the ``listChanged`` capability from
whether ``subscriptions/listen`` is served, and this server does not serve
it, so the REFUSAL is the guarantee and the list is the courtesy.

THE ENVIRONMENT CONTRACT -- separate variables, not one JSON blob, because
every value below has to survive being written into a TOML table by
``-c mcp_servers.doxa.env.<KEY>="<value>"`` and a JSON object is not a
TOML value:

===========================  ====================================
``DOXA_MCP_SESSION_ID``      DOXA's session id -- the OperatorContext's
                             ``session_id``. Empty means "no session",
                             and the peer tools answer accordingly.
``DOXA_MCP_CWD``             the session's working directory; the repo
                             root is derived from it (``repo_root_of``).
``DOXA_MCP_ENGINE``          engine id for memory proposal provenance.
``DOXA_MCP_SPAWN_DEPTH``     integer, default 0. On the sidecar, never
                             an argument -- a depth the model could
                             write is not a depth limit.
``DOXA_MCP_LORE``            ``0`` turns memory OFF: the ``lore_*``
                             tools are then ABSENT from ``tools/list``,
                             not present and refusing. Default on.
``DOXA_MCP_PEER_SEND``       ``1`` asks for the ``peer_send`` seam. It
                             is still only offered if
                             :mod:`doxa.peerdelivery` imports AND
                             exports the factory named below.
``DOXA_MCP_ENGINE_SOCKET``   the engine's control socket for this
                             session -- the seam ``peer_send`` is
                             performed through. A PATH, not a
                             credential; see the seam section below.
``DOXA_MCP_TURN_ID``         the turn this server was spawned for.
                             Rides the forwarded request so the
                             ledger row names the right turn.
===========================  ====================================

``--no-lore`` on the command line is the same switch as ``DOXA_MCP_LORE=0``
and wins over it, mirroring ``doxa.daemon --no-lore``.

Everything else this process needs (``LORE_ROOT``, ``LORE_PROJECTS_DIR``,
``DOXA_RUNTIME_DIR``, ``DOXA_HOME``, ``DOXA_AGENT_PEER_SEND`` ...) it reads
from its own environment in the ordinary way; :data:`doxa.codex.
MCP_ENV_PASSTHROUGH` is the list the engine forwards, and it is a list of
non-secret path/switch variables on purpose -- a ``-c`` override lands on
``codex exec``'s argv, which is world-readable in ``ps``.

THE peer_send SEAM, and how it reaches one limiter. ``peer_send``
delivers immediately and must be charged to the session's send-side rate
limiter, written to its ledger and shown on its status bar. A send
performed HERE could do none of those things: this process is spawned and
killed per ``codex exec`` turn, so its limiter would start empty every
turn, its ledger writer would race the engine's for the same lock, and
its lamps would light nothing. Reaching for ``doxa.peers.send_message``
would skip the limiter and the ledger outright, which is the defect
``doxa.peerdelivery`` exists to prevent.

So this process does not send. :func:`_resolve_delivery` asks
``doxa.peerdelivery.delivery_for(session_id, cwd)`` for an object with a
``tool_send(request) -> dict``, and that object
(:class:`doxa.peerdelivery.SidecarDelivery`) FORWARDS the operator's
already-validated request over ``DOXA_MCP_ENGINE_SOCKET`` to the engine,
which performs it through the one :class:`doxa.peerdelivery.PeerDelivery`
that also serves the human's ``/msg``. One limiter per session across
turns, one ledger writer, lamps and events immediate. When the engine did
not set that variable the factory returns None and ``peer_send`` is
simply not in ``tools/list`` -- ``operators._peer_send_configured`` sees
no ``peer_send`` key in the ctx and does not project it, so the model
cannot call a tool that would have nowhere to go.

That socket carries no credential and needs none. It is 0600 inside the
0700 runtime dir, so its boundary is the filesystem's -- the same
same-user boundary the peer sockets themselves rest on -- and a token
would have to ride ``-c mcp_servers.doxa.env.<KEY>``, which lands on
``codex exec``'s argv and is world-readable in ``ps``.

STDERR GOES NOWHERE USEFUL, AND THAT IS MEASURED. Nothing is written to
it on the normal path: the disable callback writes one line, a fatal
error writes one line, and ``DOXA_MCP_DEBUG=1`` adds request-level lines
for probing. No secret ever reaches it -- results are not logged, only
tool names and counts. But a live run with the debug switch on put NONE
of those lines in the 520 bytes ``codex exec`` wrote to its own stderr,
and left no exec log under ``~/.codex/log``: Codex captures an MCP
server's stderr and keeps it. So these lines are for someone driving this
server directly (the suite does exactly that), not for DOXA's turn. To
see them under a real ``codex exec``, point ``command`` at ``/bin/sh``
and ``args`` at ``["-c", "exec <python> -m doxa.mcpserver 2>>FILE"]`` --
that is how the ``tools/list`` line quoted in :mod:`doxa.codex`'s header
was captured.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable

from . import _lore_bootstrap  # noqa: F401 -- sys.path shim, see that module

from .peerdelivery import ENGINE_SOCKET_ENV, ENGINE_TURN_ENV

#: The MCP server's name. Codex namespaces the tools it exposes to the
#: model under it; the WIRE names inside this process stay the registry's
#: own (``lore_memory_list``), which is what ``doxa.gate`` keys on.
SERVER_NAME = "doxa"

ENV_SESSION_ID = "DOXA_MCP_SESSION_ID"
ENV_CWD = "DOXA_MCP_CWD"
ENV_ENGINE = "DOXA_MCP_ENGINE"
ENV_SPAWN_DEPTH = "DOXA_MCP_SPAWN_DEPTH"
ENV_LORE = "DOXA_MCP_LORE"
ENV_PEER_SEND = "DOXA_MCP_PEER_SEND"
ENV_DEBUG = "DOXA_MCP_DEBUG"

#: The two variables that name the engine behind this sidecar. Spelled in
#: :mod:`doxa.peerdelivery` and imported rather than repeated, because
#: that module is the one that READS them -- this process never does.
ENV_ENGINE_SOCKET = ENGINE_SOCKET_ENV
ENV_TURN_ID = ENGINE_TURN_ENV

#: The identity variables, in one tuple, so the engine that SETS them and
#: the server that READS them cannot drift -- doxa.codex imports this
#: rather than spelling the names a second time.
IDENTITY_ENV = (
    ENV_SESSION_ID, ENV_CWD, ENV_ENGINE, ENV_SPAWN_DEPTH, ENV_LORE, ENV_PEER_SEND,
    ENV_ENGINE_SOCKET, ENV_TURN_ID,
)

#: The seam ``doxa.peerdelivery`` exports for peer_send to be offered:
#: ``delivery_for(session_id, cwd)`` returning an object with an async
#: ``tool_send(request: dict) -> dict`` -- the callable
#: ``OperatorContext.peer_send`` takes -- or None when this process was
#: not told where its engine listens. One string, so the two modules
#: match by name rather than by a shape either could infer wrongly.
PEER_DELIVERY_FACTORY = "delivery_for"


def _log(message: str) -> None:
    """One line to stderr. Codex captures it; DOXA's own turn shows it only
    when the turn failed, so it must stay rare and must never carry a
    result."""
    try:
        print(f"[doxa.mcpserver] {message}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 -- a closed stderr is not a reason to
        # lose the turn the server is serving.
        pass


def _debug(message: str) -> None:
    if _truthy(os.environ.get(ENV_DEBUG), False):
        _log(message)


def _truthy(raw: "str | None", default: bool) -> bool:
    """The same off-switch spelling ``doxa.engine.lore_enabled_default``
    uses -- unset means the default, and only the four explicit negatives
    turn something off."""
    value = str(raw or "").strip().lower()
    if not value:
        return default
    return value not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class Identity:
    """Who this server is serving. Every field arrives from the ENGINE,
    never from the model: the session id names the peer registry row, the
    cwd decides the LORE project, and the spawn depth is a limit."""

    session_id: str
    cwd: str
    source_engine: str | None = None
    spawn_depth: int = 0
    lore: bool = True
    peer_send: bool = False


def identity_from_env(
    env: "dict[str, str] | None" = None, *, lore: "bool | None" = None
) -> Identity:
    """Read :class:`Identity` off the environment contract. ``lore=False``
    (the ``--no-lore`` flag) overrides ``DOXA_MCP_LORE``."""
    env = dict(os.environ) if env is None else env
    try:
        depth = max(0, int(str(env.get(ENV_SPAWN_DEPTH) or "0").strip() or 0))
    except ValueError:
        depth = 0
    return Identity(
        session_id=str(env.get(ENV_SESSION_ID) or "").strip(),
        cwd=str(env.get(ENV_CWD) or "").strip() or os.getcwd(),
        source_engine=str(env.get(ENV_ENGINE) or "").strip() or None,
        spawn_depth=depth,
        lore=_truthy(env.get(ENV_LORE), True) if lore is None else bool(lore),
        peer_send=_truthy(env.get(ENV_PEER_SEND), False),
    )


class OperatorSurface:
    """The registry, the gate and the configuredness ctx for one session.

    Built exactly the way ``doxa.vendors.ChatApiEngine.start`` builds its
    own -- ``ToolGate(allowed=None, op_ctx=OperatorContext(...))`` plus a
    ctx dict naming the seams this host actually wired -- because the whole
    point of this process is that a Codex session gets the SAME surface a
    DeepSeek one gets, through the same code."""

    def __init__(
        self,
        identity: Identity,
        *,
        delivery: "Callable[[dict], Any] | None" = None,
        on_disable: "Callable[[str, str], None] | None" = None,
    ) -> None:
        from .gate import OperatorContext, ToolGate, repo_root_of

        self.identity = identity
        self.disabled_seen: list[str] = []

        # The two LORE seams, named ONLY when this session has memory --
        # the same mechanism doxa.engine uses, and for the same reason:
        # absence is what makes every lore_* operator ABSENT from
        # tools/list rather than present and refusing.
        belief_store: "Callable[[], Any] | None" = None
        ctx: dict = {}
        if identity.lore:
            from lore_core import store as lore_store
            from lore_core.config import ROOT as LORE_STORE_ROOT

            belief_store = lore_store.db_connect
            ctx["belief_store"] = belief_store
            ctx["lore_root"] = str(LORE_STORE_ROOT)
        if delivery is not None:
            # Named for the same reason: operators._peer_send_configured
            # wants the SETTING and a real outbound path, and a host with
            # no path must not be offered the tool.
            ctx["peer_send"] = delivery
        self.ctx = ctx

        self.gate = ToolGate(
            allowed=None,
            op_ctx=OperatorContext(
                session_id=identity.session_id,
                cwd=identity.cwd,
                repo_root=repo_root_of(identity.cwd),
                belief_store=belief_store,
                source_engine=identity.source_engine,
                spawn_depth=identity.spawn_depth,
                # No human to ask and no spawn from this surface -- Codex
                # reports spawn_sessions=False and doxa.session_ops is not
                # composed in below.
                spawn_confirm=None,
                peer_send=delivery,
            ),
            on_disable=on_disable or self._note_disabled,
        )

    def tools(self) -> "list[dict]":
        """The configured operators as MCP tool definitions, recomputed on
        every ``tools/list``.

        ``OPERATORS + WRITE_OPERATORS``, the same pair
        ``doxa.vendors.operator_tools`` projects (``lore_remember`` only
        STAGES a proposal, so the review gate is what keeps the write path
        safe, not its absence), filtered by
        ``operators.configured_names(ctx)`` and then by the gate's disabled
        set. ``doxa.session_ops`` is deliberately NOT composed in: a spawn
        from here would thread no engine id."""
        from .operators import OPERATORS, WRITE_OPERATORS, configured_names

        allowed = configured_names(self.ctx)
        out: "list[dict]" = []
        for op in list(OPERATORS.values()) + list(WRITE_OPERATORS.values()):
            if op.name not in allowed or op.name in self.gate.disabled:
                continue
            out.append({
                "name": op.name,
                "description": f"{op.description} [cost: {op.cost}]"
                + ("" if op.read_only else f" [write: {op.write_note}]"),
                "inputSchema": op.parameters,
            })
        return out

    async def call(self, name: str, args: "dict | None") -> dict:
        """One ``tools/call``, through the gate and nothing else.

        Returns the SAME dict the vendor engine feeds back to its model
        (``doxa.vendors.ChatApiEngine._run_tool``), including its
        never-raises posture: ToolGate.execute turns every failure into an
        ordinary ``{"error": ...}`` result."""
        result = self.gate.execute(name, dict(args or {}))
        if hasattr(result, "__await__"):
            result = await result
        return result if isinstance(result, dict) else {"result": result}

    def _note_disabled(self, name: str, reason: str) -> None:
        """The two-strikes tracker removed a tool. One stderr line, no
        result text: the reason is the gate's own sentence and carries the
        tool name only."""
        self.disabled_seen.append(name)
        _log(f"tool disabled after repeated failures: {name} -- {reason}")


def _resolve_delivery(identity: Identity) -> "Callable[[dict], Any] | None":
    """The ``peer_send`` seam, or None -- see the module docstring.

    What comes back is the FORWARDING seam
    (:meth:`doxa.peerdelivery.SidecarDelivery.tool_send`), never a send
    performed in this process: the limiter, the ledger and the lamps all
    live in the engine, and a second copy of any of them is a session
    that cannot be bounded, recorded or watched. Deliberately NOT
    ``doxa.peers.send_message``, which would skip the first two outright.

    None whenever the seam cannot be built -- the setting is off, the
    factory is missing, or the engine named no control socket -- and a
    None seam is a tool that is ABSENT rather than one that refuses."""
    if not identity.peer_send:
        return None
    from . import peerdelivery

    factory = getattr(peerdelivery, PEER_DELIVERY_FACTORY, None)
    if not callable(factory):
        _log(
            f"peer_send not offered: doxa.peerdelivery exports no "
            f"{PEER_DELIVERY_FACTORY}(session_id, cwd)"
        )
        return None
    try:
        delivery = factory(identity.session_id, identity.cwd)
    except Exception as exc:  # noqa: BLE001 -- a seam that cannot be built
        # is a narrower surface, not a dead server.
        _log(f"peer_send not offered: {type(exc).__name__}: {exc}")
        return None
    if delivery is None:
        _debug(
            f"peer_send asked for, but no engine control socket was named "
            f"in {ENV_ENGINE_SOCKET}"
        )
        return None
    seam = getattr(delivery, "tool_send", None)
    if not callable(seam):
        _log(
            f"peer_send not offered: {PEER_DELIVERY_FACTORY} returned "
            f"{type(delivery).__name__}, which has no tool_send(request)"
        )
        return None
    return seam


def build_server(surface: OperatorSurface) -> Any:
    """The mcp ``Server`` for one surface. Handlers are constructor
    callbacks in mcp 2.0.0, not decorators."""
    import mcp.types as types
    from mcp.server.lowlevel import Server

    from . import __version__

    async def on_list_tools(_ctx: Any, _params: Any) -> Any:
        tools = surface.tools()
        _debug(f"tools/list -> {len(tools)}: {[t['name'] for t in tools]}")
        return types.ListToolsResult(tools=[
            types.Tool(
                name=tool["name"],
                description=tool["description"],
                inputSchema=tool["inputSchema"],
            )
            for tool in tools
        ])

    async def on_call_tool(ctx: Any, params: Any) -> Any:
        before = set(surface.gate.disabled)
        _debug(f"tools/call {params.name}")
        result = await surface.call(params.name, params.arguments)
        if set(surface.gate.disabled) - before:
            # Best effort, and the docstring says why: this server does not
            # serve subscriptions/listen, so the client may never have been
            # told the list can change. The gate's refusal is the guarantee.
            try:
                await ctx.session.send_tool_list_changed()
            except Exception:  # noqa: BLE001
                pass
        return types.CallToolResult(
            content=[types.TextContent(
                type="text", text=json.dumps(result, ensure_ascii=False)
            )],
            isError=isinstance(result, dict) and isinstance(result.get("error"), str),
        )

    return Server(
        SERVER_NAME,
        version=__version__,
        instructions=(
            "DOXA's own memory and peer tools. Read them before answering a "
            "question about this user, this project or what was decided "
            "earlier -- they are the session's durable memory."
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def serve(surface: OperatorSurface) -> None:
    """Run the server on this process's stdin/stdout until the client
    closes it."""
    from mcp.server.stdio import stdio_server

    server = build_server(surface)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


def _parse_args(argv: "list[str] | None") -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m doxa.mcpserver",
        description="DOXA's operator registry as a stdio MCP server.",
    )
    parser.add_argument(
        "--no-lore", dest="lore", action="store_false", default=None,
        help="run without memory: the lore_* tools are absent, not refusing "
             f"(same switch as {ENV_LORE}=0, and it wins over it)",
    )
    return parser.parse_args(argv)


def main(argv: "list[str] | None" = None) -> int:
    """``python -m doxa.mcpserver``. Returns a process exit code."""
    import anyio

    args = _parse_args(argv)
    identity = identity_from_env(lore=args.lore)
    surface = OperatorSurface(identity, delivery=_resolve_delivery(identity))
    _debug(
        f"serving session={identity.session_id!r} cwd={identity.cwd!r} "
        f"lore={identity.lore} peer_send={surface.ctx.get('peer_send') is not None}"
    )
    try:
        anyio.run(serve, surface)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 -- the client sees a dead server
        # either way; what it must not see is a traceback interleaved with
        # the JSON-RPC stream it is still reading.
        _log(f"fatal: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover -- exercised as a subprocess
    raise SystemExit(main())
