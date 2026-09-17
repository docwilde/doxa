# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.vendors -- DOXA sessions driven by a third-party chat-completions
API: DeepSeek (``--engine deepseek``) and Z.ai's GLM (``--engine glm``).

WHY THESE EXIST. ``docs/plans/emergent-organization.md`` asks whether
coordinating structure emerges among peer agents, and its central control
is that **N copies of one model are not N independent agents** -- they
share training, phrasing and failure modes, so self-organisation among
them cannot be told apart from a shared prior. Cells C and D of that
design need a randomised mix of vendors, and this module is what makes
that mix possible.

That purpose dictates the ONE design rule here: **capability parity, not
feature parity**. The paper has to be able to say the agents differed in
model and in nothing else, and it reads
:class:`~doxa.engines.EngineCapabilities` to say it. So both vendors run
through ONE engine class with ONE capability map
(:data:`VENDOR_CAPABILITIES`) -- the parity is structural, not asserted --
and every field in that map is False only where the API genuinely cannot
do the thing. A field that is False "for now" would silently invalidate a
study.

MEASURED SURFACE. Everything below was RUN against both live APIs on
2026-09-17, not read from a vendor doc. Where the two disagree, the
measurement wins and the disagreement is recorded, because that is the
part worth more than a clean report.

**What both do, identically**

* OpenAI-shaped ``POST /chat/completions``. ``stream: true`` gives SSE
  ``data: {...}`` lines terminated by ``data: [DONE]``.
* ``stream_options: {"include_usage": true}`` puts a ``usage`` block on
  the FINAL chunk -- the same chunk that carries ``finish_reason``, not a
  separate trailing one. Hence ``token_usage=True``.
* Reasoning reaches the client as ``delta.reasoning_content`` (streamed)
  or ``message.reasoning_content`` (not). Hence ``reasoning=True``.
* Assistant prose reaches the client as ``delta.content`` fragments.
  Hence ``streaming_text=True``.
* Function calling works, in the OpenAI ``tools`` / ``tool_calls`` shape,
  WITH reasoning enabled. This is what makes ``mcp_tools`` and
  ``tool_gate`` True here and False on Codex: DOXA executes every call
  itself, in this process, through :class:`doxa.gate.ToolGate`.
* Every response and every stream chunk names the model that ANSWERED, in
  its own ``model`` field. Hence ``resolved_model=True``.
* **``max_tokens`` is never sent, by either.** A reasoning model spends a
  token cap on its hidden reasoning FIRST. Measured: ``max_tokens: 24``
  with a thinking prompt returned ``content: ""``, ``finish_reason:
  "length"`` and ``completion_tokens_details.reasoning_tokens: 24`` on
  DeepSeek, and the same empty-or-truncated shape on GLM. The key is built
  in one place (:func:`request_body`) and the suite asserts it is simply
  never present.
* Neither ``/models`` nor any response carries a context-window SIZE. See
  ``context_window=False`` below.

**Where they differ, measured**

* ``reasoning_effort`` placement. DeepSeek nests it inside ``thinking``;
  GLM puts it at the request root. Measured surprise: each vendor also
  ACCEPTS the other's placement with a 200 and real reasoning, so the
  difference is not enforced -- but whether the nested value is honoured
  by GLM is unobservable from the response, so each vendor is sent its
  own documented shape and :class:`VendorSpec` carries which.
* **GLM cannot turn thinking off.** ``thinking: {"type": "disabled"}``
  and ``reasoning_effort: "none"`` are BOTH refused with HTTP 400 code
  ``1210`` ("This model always engages in thinking and cannot be
  disabled; please use low, high, or max"). DeepSeek accepts
  ``{"type": "disabled"}`` and answers without reasoning. So GLM's effort
  allow-list is three values and DeepSeek's is four.
* **DeepSeek silently substitutes a legacy model name.** Asking for
  ``deepseek-chat`` -- the name every older integration uses, including
  this repo's sibling ``panel`` project -- returns HTTP 200 answered by
  ``deepseek-flash``, and the ONLY place that truth appears is the
  response's own ``model`` field. A name that was never a model
  (``totally-not-a-model-xyz``) is refused with a 400 that lists the real
  ones. GLM does not substitute at all: an unknown model is HTTP 400 code
  ``1211``. This is why ``resolved_model`` is not decoration here. An
  experiment that assigns models randomly per agent and then reports the
  REQUESTED name would be reporting a model that did not answer.
* Live model lists, as of the measurement, are much shorter and newer
  than any doc: DeepSeek offers ``deepseek-flash`` and ``deepseek-v4-pro``
  and NOTHING else; GLM offers ``glm-4.5`` through ``glm-5.3-flash``.
* DeepSeek's 401 body echoes a masked tail of the key it rejected
  (``"your api key: ****nope is invalid"``). Every vendor message that
  reaches a transcript, an event or an exception goes through
  :func:`_scrub` first, which runs ``scrub_secrets`` AND redacts the
  live key by value.
* DeepSeek has ``GET /user/balance`` (measured: a real USD figure). GLM
  has no balance, billing or quota endpoint at all. Neither is a
  per-session dollar figure -- see ``cost=False``.

NO KEY IS EVER STORED. The engine holds no credential attribute. The key
is read from the environment at the moment a request is built and dropped
when it returns, so it cannot appear in a ``repr``, a pickle, a traceback
frame that survives, or a transcript. :func:`credential` raises with the
NAME of the missing variable and never its value.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from . import _lore_bootstrap  # noqa: F401 -- sys.path shim, see that module

from lore_core import store as lore_store
from lore_core.config import PROJECTS_DIR, project_slug
from lore_core.scrub import scrub_secrets

from . import peers as peers_mod
from .engines import (
    DEEPSEEK_ENGINE_ID,
    GLM_ENGINE_ID,
    Engine,
    EngineCapabilities,
)
from .events import EngineEvent


# -- constants ---------------------------------------------------------

#: How long one turn may run, wall clock, across every request and tool
#: call it makes. A turn that never ends would hold the pane's exclusive
#: worker forever. Same number and same reason as
#: ``doxa.codex.TURN_TIMEOUT_SECS``.
TURN_TIMEOUT_SECS = 3600.0

#: How many model->tools->model round trips one turn may make before the
#: engine stops and says so. A model that calls a tool, reads the result
#: and calls it again is working; one that does that forever is a turn
#: that never ends, and the cap is what makes the difference visible
#: instead of terminal.
MAX_TOOL_STEPS = 24

#: Result text kept per tool chip. Matches ``doxa.codex.RESULT_SUMMARY_MAX``
#: so a chip reads the same whichever engine produced it.
RESULT_SUMMARY_MAX = 280

#: How much of an error body is kept for the message. Bounded because a
#: vendor that answers an error with an HTML page must not cost megabytes
#: for a line that gets truncated anyway.
ERROR_BODY_MAX = 800

#: Reasoning effort when nothing asks for one. ``"low"`` is the only value
#: BOTH vendors accept that is also cheap, and the fleet in
#: docs/plans/emergent-organization.md is quota-bound rather than
#: quality-bound. Overridable per session (``DOXA_VENDOR_EFFORT``), and an
#: unrecognised value falls back HERE rather than reaching the API.
DEFAULT_EFFORT = "low"

#: The env var that overrides :data:`DEFAULT_EFFORT` for every vendor.
EFFORT_ENV = "DOXA_VENDOR_EFFORT"

#: Sampling temperature. One number for both vendors, on purpose: a
#: capability-parity experiment must not have one arm sampled differently
#: from another. Same value ``panel_core.providers`` settled on.
TEMPERATURE = 0.2

PEER_TITLE_MAX = 72


# -- the capability map ------------------------------------------------

VENDOR_CAPABILITIES = EngineCapabilities(
    # TRUE. Both APIs take OpenAI `tools` and emit `tool_calls`, measured
    # with reasoning enabled. doxa.operators' registry projects straight
    # to that schema (its `parameters` already IS a JSON Schema), so
    # DOXA's LORE operators genuinely reach the model as callable tools.
    mcp_tools=True,
    # FALSE. `hooks` names three Claude Code hook events -- UserPromptSubmit,
    # PreToolUse, PreCompact -- and a chat-completions request has no hook
    # surface of any kind: there is no dispatcher to register with and no
    # compaction event to intercept. What DOXA USED those hooks for is not
    # lost (the LORE snapshot is rebuilt and sent as the system message on
    # EVERY turn, which is what the UserPromptSubmit refresh did), but the
    # field names the surface, and the surface is absent.
    hooks=False,
    # TRUE, and this is the field Codex could not have. Codex's MCP path
    # runs tools in a server PROCESS outside DOXA and therefore outside
    # ToolGate. Here the model only ever NAMES a call; this engine
    # executes it in-process through ToolGate.execute, so the allowed-set
    # refusal, the graceful-degradation contract, the two-strikes disable
    # and the tool_disabled event all apply unchanged.
    tool_gate=True,
    # FALSE. There is no tool-permission posture to cycle. Claude's modes
    # (default / acceptEdits / bypassPermissions / plan) configure a CLI
    # that owns the tool surface; here DOXA owns the whole tool surface
    # itself, so /mode would change nothing and says so instead.
    permission_modes=False,
    # FALSE. `--plugin-dir` is a Claude Code CLI flag. A JSON request body
    # has nowhere to put a plugin directory and nothing to load one with.
    plugins=False,
    # TRUE, and load-bearing rather than decorative -- see the module
    # docstring: DeepSeek answers a request for `deepseek-chat` with
    # `deepseek-flash` and only the response says so.
    resolved_model=True,
    # FALSE. Neither GET /models nor any response field carries a window
    # SIZE. The RESIDENT count is knowable (usage.prompt_tokens is exactly
    # what was in context for that call) but a percentage needs a limit,
    # and the only limits available are numbers DOXA would hardcode and
    # then be wrong about after a model revision. That is the substituted
    # 200000 doxa.ui.labels.ctx_absolute_text already refused once. The
    # ctx chip is omitted and /context says it cannot be asked.
    context_window=False,
    # TRUE. usage arrives on the final stream chunk with
    # stream_options.include_usage: prompt, completion, cached and
    # reasoning token counts, measured on both.
    token_usage=True,
    # FALSE, and deliberately not fudged. No response field from either
    # vendor carries a dollar figure; DOXA would have to multiply tokens
    # by a price sheet it maintains itself, and a hardcoded price that
    # drifts prints a CONFIDENT WRONG number, which is worse than no chip.
    # DeepSeek's GET /user/balance is a real reported dollar figure, but
    # it is per ACCOUNT, not per session -- and in the very experiment
    # this module exists for, 32 sessions share one key, so a balance
    # delta would attribute the whole fleet's spend to each of them.
    cost=False,
    # TRUE. delta.reasoning_content, measured streaming on both.
    reasoning=True,
    # TRUE. delta.content fragments, measured on both.
    streaming_text=True,
    # TRUE. The API is stateless -- DOXA owns the message list -- so the
    # next request simply carries the new model. Takes effect from the
    # next turn, which is what set_model's return string says.
    live_model_switch=True,
    # TRUE. The conversation is a message list this engine owns and writes
    # to disk beside the transcript after every turn, so a later process
    # replays it EXACTLY, tool calls and tool results included.
    resume=True,
    # FALSE. doxa.daemon's RPC surface is SessionEngine's; no daemon hosts
    # this engine, so a session lives in the TUI process and Ctrl+Q ends
    # it rather than detaching from it. Same as Codex.
    detachable=False,
    # TRUE. DOXA's own layer, with no model in it.
    peer_messaging=True,
    # FALSE. doxa.session_ops.spawn_session shells out to a `doxa` command
    # line that does not thread an engine id to the child, so a spawn from
    # here would silently start a CLAUDE child -- which is both a
    # cross-engine spawn (out of scope per docs/plans/engine-providers.md)
    # and a lie about what was spawned. The operator is not offered.
    spawn_sessions=False,
    # FALSE, same as Codex and for the same reason: the belief/pending
    # PICKERS are lore_core queries that happen to live on SessionEngine.
    # belief_count() below is honest and complete; the pickers are absent
    # and every call site already reaches them through getattr.
    lore_pickers=False,
)


# -- vendor specs ------------------------------------------------------


@dataclass(frozen=True)
class VendorSpec:
    """Everything that differs between two otherwise identical engines.

    Deliberately small. If this dataclass starts growing behaviour rather
    than facts, the two vendors have stopped being capability-equal and
    the experiment's control has quietly broken."""

    engine_id: str
    display_name: str
    #: The free string a peer's self-description carries. Displayed, never
    #: verified, decides nothing -- see doxa.peers.PeerInfo.
    provider_id: str
    chat_url: str
    models_url: str
    #: The environment variable holding the key. Its NAME appears in
    #: errors; its VALUE never does.
    env_var: str
    #: Models the live API actually offered at the measurement, newest
    #: last. Used for the "did you mean" in an unknown-model message, not
    #: to reject one -- the vendor is the authority on its own catalogue
    #: and this tuple goes stale by design.
    models: "tuple[str, ...]"
    default_model: str
    #: Reasoning-effort values the API accepts. An ALLOW-list: an operator
    #: string is validated here rather than discovered as a 400 mid-turn.
    efforts: "tuple[str, ...]"
    #: Where reasoning_effort goes: "thinking" nests it inside the
    #: thinking block (DeepSeek), "root" puts it at the body root (GLM).
    effort_placement: str
    #: Whether the API accepts thinking being turned off. GLM: no (1210).
    thinking_disableable: bool
    #: GET endpoint returning a dollar balance, or None. Present for
    #: completeness and NOT wired to `cost` -- see VENDOR_CAPABILITIES.
    balance_url: "str | None" = None


DEEPSEEK = VendorSpec(
    engine_id=DEEPSEEK_ENGINE_ID,
    display_name="DeepSeek",
    provider_id="deepseek",
    chat_url="https://api.deepseek.com/chat/completions",
    models_url="https://api.deepseek.com/models",
    env_var="DEEPSEEK_API_KEY",
    # Measured 2026-09-17 from GET /models. The `deepseek-chat` and
    # `deepseek-reasoner` names older integrations use are NOT in this
    # list; asking for the former is answered by deepseek-flash.
    models=("deepseek-flash", "deepseek-v4-pro"),
    default_model="deepseek-flash",
    efforts=("none", "low", "high", "max"),
    effort_placement="thinking",
    thinking_disableable=True,
    balance_url="https://api.deepseek.com/user/balance",
)

GLM = VendorSpec(
    engine_id=GLM_ENGINE_ID,
    display_name="GLM (Z.ai)",
    provider_id="zai",
    chat_url="https://api.z.ai/api/paas/v4/chat/completions",
    models_url="https://api.z.ai/api/paas/v4/models",
    env_var="ZAI_API_KEY",
    models=(
        "glm-4.5", "glm-4.5-air", "glm-4.6", "glm-4.7", "glm-5",
        "glm-5-turbo", "glm-5.1", "glm-5.2", "glm-5.3", "glm-5.3-flash",
    ),
    default_model="glm-5.3-flash",
    # Three, not four: "none" is refused with 1210 exactly like
    # {"type": "disabled"} is. Measured, not inferred from the doc.
    efforts=("low", "high", "max"),
    effort_placement="root",
    thinking_disableable=False,
    balance_url=None,
)

VENDORS: "dict[str, VendorSpec]" = {DEEPSEEK.engine_id: DEEPSEEK, GLM.engine_id: GLM}


# -- credentials and errors -------------------------------------------


class MissingCredential(RuntimeError):
    """The vendor's API key is not in the environment.

    Raised from :func:`credential`, carrying the NAME of the variable and
    never its value -- the point of the exception is to tell an operator
    which environment variable to set, and an exception that quoted the
    key would put it in a traceback, a log line and the transcript."""


class VendorApiError(RuntimeError):
    """The API answered, and the answer was a failure.

    ``status`` is the HTTP status; ``detail`` is the scrubbed body, capped
    at :data:`ERROR_BODY_MAX`; ``code`` is the vendor's own ``error.code``
    when it sent one -- Z.ai in particular distinguishes a transient rate
    limit (1302) from a permanent out-of-credit (1113) ONLY there, and
    both arrive as HTTP 429, so the status line alone can never tell them
    apart."""

    def __init__(self, status: int, detail: str, code: "str | None" = None) -> None:
        super().__init__(f"HTTP {status}: {detail}" if detail else f"HTTP {status}")
        self.status = status
        self.detail = detail
        self.code = code


#: Vendor error codes that must never be retried: retrying spends latency
#: for a result that is already known. Measured against a live z.ai key by
#: the sibling `panel` project -- 1113 is "insufficient balance or no
#: resource package", which answers a retry exactly as it answered the
#: first call.
TERMINAL_ERROR_CODES = frozenset({"1113"})


def credential(spec: VendorSpec, env: "dict[str, str] | None" = None) -> str:
    """This vendor's API key, from the environment.

    ``env`` is injectable so the suite can prove both the present and the
    absent case without touching the real process environment. The key is
    returned, never stored: :class:`ChatApiEngine` holds no credential
    attribute, so there is nothing for a repr or a pickle to leak."""
    source = os.environ if env is None else env
    value = str(source.get(spec.env_var) or "").strip()
    if not value:
        raise MissingCredential(
            f"{spec.display_name} needs an API key in ${spec.env_var}, and "
            f"that variable is unset or empty -- export it (never commit "
            f"it), or start this session on the claude engine"
        )
    return value


def auth_headers(api_key: str) -> dict:
    """Bearer-auth headers, the same shape for both vendors.

    Its own function so no call site interpolates the key into an f-string
    inline, where a copy/paste would leave it un-redacted next to a log
    line. Identical to ``panel_core.providers.auth_headers`` and for the
    same stated reason."""
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _scrub(text: str, api_key: "str | None" = None) -> str:
    """Everything a vendor said, made safe to show.

    TWO layers, and the second one is measured rather than defensive:
    ``scrub_secrets`` is LORE's pattern-based redactor, and the explicit
    key replacement covers the case it cannot match -- DeepSeek's 401 body
    quotes a masked TAIL of the key it rejected, and a vendor that one day
    quotes more of it must not be the first to find that out."""
    out = scrub_secrets(str(text or ""))
    if api_key:
        out = out.replace(api_key, "***")
        # The tail alone is what DeepSeek echoes; four characters is short
        # enough to collide with ordinary prose, so only a tail that is
        # actually adjacent to the word "key" is worth removing -- and
        # that is exactly the shape the measured message takes.
        tail = api_key[-4:]
        if len(tail) == 4 and f"****{tail}" in out:
            out = out.replace(f"****{tail}", "****")
    return out


def _truncate(text: str, limit: int = RESULT_SUMMARY_MAX) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def extract_error_code(body_text: "str | None") -> "str | None":
    """``error.code`` out of a vendor's JSON error envelope, or None.

    Both vendors shape a failure ``{"error": {"code": ..., "message":
    ...}}``. Never raises: a malformed or HTML error body degrades to
    "unknown code", which is what the caller already handles, rather than
    crashing the path that is trying to report a failure."""
    if not body_text:
        return None
    try:
        parsed = json.loads(body_text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    error = parsed.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return str(code) if code is not None else None


# -- the request body --------------------------------------------------


def request_body(
    spec: VendorSpec,
    messages: "list[dict]",
    model: str,
    effort: str,
    tools: "list[dict] | None" = None,
    stream: bool = True,
) -> dict:
    """The one place a request body is built, for both vendors.

    Three things this function exists to guarantee, each of which cost
    somebody a debugging round to learn:

    1. **``max_tokens`` is never present.** A reasoning model spends a
       token cap on hidden reasoning first; measured, a cap low enough to
       matter returns empty ``content`` with the entire budget consumed.
       There is no parameter to set it through, deliberately.
    2. **``reasoning_effort`` goes where the vendor puts it** --
       ``spec.effort_placement``, nested for DeepSeek and at the root for
       GLM. Getting this by analogy with the other vendor is the exact
       mistake one shared builder prevents.
    3. **``thinking`` is enabled unless the vendor allows otherwise.** GLM
       refuses ``{"type": "disabled"}`` with error 1210, so an effort of
       ``"none"`` is not even in its allow-list and this function never
       constructs that body for it."""
    body: dict = {
        "model": model,
        "messages": messages,
        "temperature": TEMPERATURE,
    }
    if effort == "none" and spec.thinking_disableable:
        body["thinking"] = {"type": "disabled"}
    elif spec.effort_placement == "thinking":
        body["thinking"] = {"type": "enabled", "reasoning_effort": effort}
    else:
        body["thinking"] = {"type": "enabled"}
        body["reasoning_effort"] = effort
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if stream:
        body["stream"] = True
        # Without this the final chunk carries no usage at all and
        # token_usage=True would be a claim about nothing. Measured: with
        # it, usage rides the SAME chunk as finish_reason.
        body["stream_options"] = {"include_usage": True}
    return body


def operator_tools(ctx: "dict | None" = None) -> "list[dict]":
    """DOXA's LORE operators as OpenAI function-tool definitions.

    The projection is trivial because ``Operator.parameters`` is ALREADY a
    JSON Schema -- the same object ``doxa.operators.to_sdk_tools`` hands
    the SDK. What matters is that the SURFACE matches: the same
    registries, the same ``include_write=True`` (``lore_remember`` only
    STAGES a proposal, so the review gate is what keeps the write path
    safe, not its absence), the same configuredness filter and the same
    rendered descriptions a Claude session offers. A narrower surface
    would be a capability difference the map does not record, which is the
    one failure mode ``mcp_tools=True`` must not hide.

    ``doxa.session_ops.SESSION_OPERATORS`` is the DELIBERATE exception and
    the map does record it: ``spawn_session`` shells out to a command line
    that threads no engine id, so a spawn from here would start a Claude
    child under a DeepSeek parent's name. It is not offered, and
    ``spawn_sessions=False`` says so.

    ``doxa.operators`` is imported HERE rather than at module scope for
    the reason ``doxa.engines``' docstring states: it pulls
    ``claude_agent_sdk`` and its 404 ms, and a module whose job is to run
    a session on something other than Claude must not force that load on
    every import."""
    from .operators import OPERATORS, WRITE_OPERATORS, configured_names

    allowed = configured_names(ctx) if ctx is not None else None
    return [
        {
            "type": "function",
            "function": {
                "name": op.name,
                "description": f"{op.description} [cost: {op.cost}]"
                + ("" if op.read_only else f" [write: {op.write_note}]"),
                "parameters": op.parameters,
            },
        }
        for op in list(OPERATORS.values()) + list(WRITE_OPERATORS.values())
        if allowed is None or op.name in allowed
    ]


# -- the transport -----------------------------------------------------


class StreamTransport(Protocol):
    """What this engine needs from an HTTP client, and nothing more.

    One method, yielding the payload of each SSE ``data:`` line with the
    ``[DONE]`` sentinel already consumed. Kept to exactly the shape a test
    can fake in ten lines, which is what lets the whole suite run with no
    network and no credentials -- the same discipline
    ``SessionEngine(client_factory=...)`` and ``CodexEngine(exec_factory=
    ...)`` established, and the same one ``panel_core.providers`` states
    for its own ``Transport``."""

    def stream(
        self, url: str, body: dict, headers: dict, timeout: float
    ) -> "AsyncIterator[str]": ...


class HttpStreamTransport:
    """The production transport: ``urllib.request`` on a worker thread.

    WHY A THREAD AND NOT AN ASYNC CLIENT. DOXA declares no HTTP dependency
    -- there is no httpx or aiohttp on the install path, and adding one to
    ship two engines would be a dependency for every user who runs
    neither. ``urllib`` is blocking, so it runs off the event loop and
    pushes finished lines back onto it through
    ``loop.call_soon_threadsafe``; the loop itself never waits on a
    socket.

    WHAT CANCELLATION CAN AND CANNOT DO. A Python thread blocked in
    ``recv`` cannot be cancelled, so the generator's ``finally`` sets a
    stop flag that the pump checks per line and closes the response on.
    The bound is therefore ONE more chunk, not zero -- stated rather than
    implied, because a comment claiming immediate teardown would be the
    kind of half-truth this module exists to avoid."""

    def __init__(self, opener: "Callable[..., Any] | None" = None) -> None:
        self._opener = opener or urllib.request.urlopen

    async def stream(
        self, url: str, body: dict, headers: dict, timeout: float
    ) -> "AsyncIterator[str]":
        loop = asyncio.get_running_loop()
        queue: "asyncio.Queue[Any]" = asyncio.Queue()
        stop = threading.Event()
        done = object()

        def push(item: Any) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                # The loop closed while the pump was mid-line. Nothing to
                # deliver to and nothing to do about it.
                stop.set()

        def pump() -> None:
            request = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers=dict(headers),
                method="POST",
            )
            try:
                response = self._opener(request, timeout=timeout)
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:ERROR_BODY_MAX]
                except Exception:  # noqa: BLE001 -- a body we cannot read is
                    # still a failure with a status, and the status is the
                    # part the caller needs.
                    detail = ""
                push(VendorApiError(exc.code, detail, extract_error_code(detail)))
                push(done)
                return
            except Exception as exc:  # noqa: BLE001 -- DNS, TLS, timeout,
                # connection reset: every one of them is "the request did
                # not happen", and the turn reports it rather than raising
                # out of a generator the pane is iterating.
                push(VendorApiError(0, f"{type(exc).__name__}: {exc}"))
                push(done)
                return
            try:
                for raw in response:
                    if stop.is_set():
                        break
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        # SSE comments (": keep-alive") and blank
                        # separators. Neither vendor sends an `event:`
                        # field; if one starts, it lands here and is
                        # ignored rather than parsed as a payload.
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    push(payload)
            except Exception as exc:  # noqa: BLE001 -- a stream that dies
                # mid-flight is a failed turn, not a crashed session.
                push(VendorApiError(0, f"{type(exc).__name__}: {exc}"))
            finally:
                try:
                    response.close()
                except Exception:  # noqa: BLE001
                    pass
                push(done)

        worker = loop.run_in_executor(None, pump)
        try:
            while True:
                item = await queue.get()
                if item is done:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            stop.set()
            # NOT awaited: the thread is bounded by `timeout` and by the
            # stop flag, and awaiting it here would make an abandoned turn
            # wait for the very read it is abandoning.
            worker.cancel()


# -- streaming accumulation -------------------------------------------


@dataclass
class _ToolCall:
    """One tool call being assembled out of stream deltas."""

    id: str = ""
    name: str = ""
    arguments: str = ""

    def parsed(self) -> dict:
        """The arguments as a dict, or ``{}``.

        A model that emits malformed JSON is the model's mistake and
        ``ToolGate.execute`` already turns a bad-arguments call into an
        ordinary recoverable error result, so this returns an empty dict
        rather than raising into the turn."""
        try:
            value = json.loads(self.arguments or "{}")
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}


class _ToolCallAccumulator:
    """Merge ``delta.tool_calls`` fragments into whole calls.

    BOTH SHAPES, because the two vendors measurably differ and the
    difference is invisible until a tool call actually happens:

    * DeepSeek fragments a call across many deltas -- the first carries
      ``id``, ``type`` and ``function.name`` with empty arguments, and
      every delta after it carries one more piece of
      ``function.arguments`` and nothing else.
    * GLM sends ONE delta carrying ``id``, ``function.name`` and the
      COMPLETE ``function.arguments`` JSON.

    Keyed on the delta's ``index``, which both vendors send, so parallel
    calls assemble independently rather than concatenating into one."""

    def __init__(self) -> None:
        self._calls: "dict[int, _ToolCall]" = {}
        self._order: "list[int]" = []

    def absorb(self, deltas: Any) -> None:
        if not isinstance(deltas, list):
            return
        for delta in deltas:
            if not isinstance(delta, dict):
                continue
            index = delta.get("index")
            index = index if isinstance(index, int) else 0
            if index not in self._calls:
                self._calls[index] = _ToolCall()
                self._order.append(index)
            call = self._calls[index]
            if isinstance(delta.get("id"), str) and delta["id"]:
                call.id = delta["id"]
            function = delta.get("function")
            if isinstance(function, dict):
                if isinstance(function.get("name"), str) and function["name"]:
                    call.name = function["name"]
                fragment = function.get("arguments")
                if isinstance(fragment, str):
                    call.arguments += fragment

    def finished(self) -> "list[_ToolCall]":
        """Every assembled call, in the order the stream opened them.

        A call with no name never happened as far as this engine is
        concerned -- it is a fragment the stream ended in the middle of,
        and executing an unnamed tool is not a recoverable error, it is a
        guess."""
        return [self._calls[i] for i in self._order if self._calls[i].name]


@dataclass
class _Completion:
    """What one model call produced, once its stream has closed."""

    text: str = ""
    reasoning: str = ""
    tool_calls: "list[_ToolCall]" = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    #: The model that ANSWERED, off the response's own `model` field --
    #: not the one that was asked for. See the module docstring on
    #: DeepSeek's silent substitution of a legacy name.
    model: "str | None" = None
    finish_reason: "str | None" = None


class ChatApiEngine:
    """One session on a third-party chat-completions API. Satisfies
    :class:`doxa.engines.Engine`.

    ONE class for both vendors, and that is the capability-parity argument
    made structurally: there is no place for the two arms of the
    experiment to drift, because there is no second implementation for
    them to drift into. Everything that differs is a field on
    :class:`VendorSpec`.

    ``transport`` is injectable for exactly the reason
    ``SessionEngine.client_factory`` is: the suite drives every mapping,
    every failure path and every capability claim here without a network
    call and without a credential."""

    #: What this handle says about itself (doxa.engines.capabilities_of).
    #: Narrowed per instance in __init__ when something this map promises
    #: turns out to be unavailable in THIS process -- see there.
    engine_capabilities = VENDOR_CAPABILITIES

    #: The attach chip's predicate. False, truthfully: no daemon hosts
    #: this engine, so there is nothing to detach from.
    detachable = False

    def __init__(
        self,
        cwd: str,
        model: "str | None" = None,
        session_id: "str | None" = None,
        *,
        spec: "VendorSpec | None" = None,
        resume: "str | None" = None,
        spawn_depth: int = 0,
        parent_session_id: "str | None" = None,
        transport: "StreamTransport | None" = None,
        effort: "str | None" = None,
        **_ignored: Any,
    ) -> None:
        # **_ignored, deliberately, for the reason CodexEngine states:
        # EngineProvider.new_session takes DOXA's session vocabulary and a
        # provider ignores what its engine has no use for (daemon_socket,
        # allowed_tools, client_factory). Refusing them would make every
        # caller branch on which engine it is talking to, which is the
        # branch this seam exists to remove.
        self.spec = spec or DEEPSEEK
        self.cwd = str(cwd)
        self.model = model or self.spec.default_model
        self.session_id = session_id or str(uuid.uuid4())
        self.resume = resume or None
        self.spawn_depth = max(0, int(spawn_depth or 0))
        self.parent_session_id = parent_session_id or None
        self.slug = project_slug(self.cwd)
        # NO environment is captured here, and that is the guarantee, not
        # an omission: an injectable env dict would be a credential stored
        # on the handle, reachable from vars(), a repr or a pickle. The
        # key is read from os.environ at the moment a request is built and
        # dropped when it returns, and the suite proves it by looking.
        self._transport: StreamTransport = transport or HttpStreamTransport()

        wanted = str(effort or os.environ.get(EFFORT_ENV, "") or "").strip().lower()
        # An ALLOW-list, not a passthrough: an unrecognised effort reaching
        # the API is a 400 in the middle of a turn, and GLM refuses "none"
        # outright (error 1210). Falling back HERE is what keeps that a
        # configuration detail instead of a failed turn.
        self.effort = wanted if wanted in self.spec.efforts else DEFAULT_EFFORT

        # Status-bar parity with SessionEngine/EngineClient. Every one of
        # these is read UNGUARDED mid-render by doxa.session.chips, so they
        # exist from construction rather than from the first turn.
        self.total_cost_usd = 0.0
        self.last_ctx_percentage: "float | None" = None
        self.last_ctx_tokens: "int | None" = None
        self.last_ctx_max_tokens: "int | None" = None
        self.last_context_usage: "dict[str, Any] | None" = None
        self.permission_mode: str = "default"
        self.bypass_armed: bool = False
        self.account: dict = {}
        self.lore_root: "str | None" = lore_root_path()
        self.lore_snapshot_chars: "int | None" = None
        self.num_turns = 0
        self.usage_totals: "dict[str, int]" = {}
        #: The model that last ANSWERED, which is not always the one that
        #: was asked for. Published rather than folded into self.model so
        #: the two stay distinguishable: self.model is the request.
        self.resolved_model: "str | None" = None

        self.peer_host: "peers_mod.PeerHost | None" = None
        self.peer_error: "str | None" = None
        self._peer_queue: "asyncio.Queue[EngineEvent]" = asyncio.Queue()
        self._pending_peer_frames: list[dict] = []
        self._gate: Any = None
        self._tools: "list[dict]" = []
        self._finalized = False
        self._started = False

        transcript_dir = PROJECTS_DIR / self.slug
        transcript_dir.mkdir(parents=True, exist_ok=True)
        self.transcript_path = transcript_dir / f"{self.session_id}.jsonl"
        #: The conversation, verbatim, beside the transcript. The LORE
        #: transcript is LORE's shape and is what /search indexes; this is
        #: the exact `messages` array the API needs, which is what makes
        #: resume a replay rather than a reconstruction.
        self.messages_path = transcript_dir / f"{self.session_id}.messages.json"
        self.messages: "list[dict]" = (
            _load_messages(transcript_dir / f"{self.resume}.messages.json")
            if self.resume else []
        )

    # -- persistence ---------------------------------------------------

    def _persist(self, record: dict) -> None:
        """One LORE-transcript-shaped line, the same file shape and the
        same contract as SessionEngine's and CodexEngine's: every text
        field is already scrubbed by the time it arrives here."""
        try:
            with self.transcript_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            # A transcript that cannot be written must not take the turn
            # down: the session is still usable, it just will not be
            # indexed. Same posture SessionEngine takes for its review.
            pass

    def _persist_user_text(self, text: str) -> None:
        self._persist({
            "type": "user",
            "message": {"role": "user", "content": _scrub(text)},
            "cwd": self.cwd,
            "sessionId": self.session_id,
            "timestamp": _iso_now(),
        })

    def _persist_assistant_text(self, text: str) -> None:
        self._persist({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": _scrub(text)}],
            },
            "sessionId": self.session_id,
            "timestamp": _iso_now(),
        })

    def _save_messages(self) -> None:
        """Write the conversation so a later process can replay it.

        Whole-file, after every turn, rather than appended: the messages
        array is what the API takes and a half-written append is not a
        conversation. It is small (one turn's prose, not a transcript) and
        a turn already costs a network round trip, so the write is
        invisible beside it."""
        try:
            self.messages_path.write_text(
                json.dumps(self.messages, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    # -- lifecycle -----------------------------------------------------

    async def start(self) -> EngineEvent:
        """Check the credential, build the tool gate, join the peer
        registry, and say the session started.

        The credential is checked HERE and nowhere later, which is the
        point: a missing key has to fail as a session that could not
        start, naming the variable, rather than as a 401 three minutes
        into the first turn. The value is discarded immediately -- it is
        read again, from the environment, when a request is built."""
        credential(self.spec)  # raises MissingCredential, and nothing here
        # keeps what it returns.
        self._started = True

        # The tool surface. Imported lazily (it pulls claude_agent_sdk),
        # and a failure narrows this HANDLE's capability map rather than
        # leaving the class-level map promising a surface this session
        # does not have -- EngineCapabilities.without exists for exactly
        # this "the same engine in a narrower posture" case.
        try:
            from .gate import OperatorContext, ToolGate, repo_root_of

            self._gate = ToolGate(
                allowed=None,
                op_ctx=OperatorContext(
                    session_id=self.session_id,
                    cwd=self.cwd,
                    repo_root=repo_root_of(self.cwd),
                    belief_store=lore_store.db_connect,
                    spawn_depth=self.spawn_depth,
                    # No channel to ask a human on and no spawn from this
                    # engine -- see spawn_sessions=False.
                    spawn_confirm=None,
                ),
                on_disable=self._on_tool_disabled,
            )
            # The same ctx SessionEngine passes, naming the seams this
            # engine actually wired -- an operator whose is_configured says
            # no is simply not projected, and a tool the model cannot see
            # is a tool the model cannot call.
            self._tools = operator_tools({
                "belief_store": lore_store.db_connect,
                "lore_root": self.lore_root,
            })
        except Exception as exc:  # noqa: BLE001 -- an absent tool surface is
            # a narrower session, not a failed one, and it SAYS it is
            # narrower instead of offering tools that cannot run.
            self._gate = None
            self._tools = []
            self.engine_capabilities = VENDOR_CAPABILITIES.without(
                mcp_tools=False, tool_gate=False
            )
            self.peer_error = f"tools unavailable: {type(exc).__name__}"

        try:
            self.peer_host = peers_mod.PeerHost(
                session_id=self.session_id,
                cwd=self.cwd,
                on_message=self._on_peer_frame,
                on_peer_joined=self._on_peer_joined,
                on_peer_left=self._on_peer_left,
                daemon_socket=None,
                # A self-description, exactly as v1.0.2 defined it: shown,
                # never verified, and it decides nothing.
                provider=self.spec.provider_id,
                model=self.model,
                engine=self.spec.engine_id,
                parent_session_id=self.parent_session_id,
            )
            await self.peer_host.start()
        except Exception as exc:  # noqa: BLE001 -- peers are strictly additive
            self.peer_host = None
            self.peer_error = repr(exc)

        return EngineEvent("session_started", {
            "session_id": self.session_id, "model": self.model, "cwd": self.cwd,
        })

    async def finalize(self) -> EngineEvent:
        """End the session: drop out of the registry, index what was said.

        No deriver review, and that is a declared gap rather than an
        oversight -- the same one CodexEngine declares, for the same
        reason: ``SessionEngine._run_review_sync`` builds its job from a
        transcript whose shape it also wrote."""
        if self._finalized:
            return EngineEvent("session_done", {"already_finalized": True})
        self._finalized = True
        self._save_messages()
        if self.peer_host is not None:
            try:
                await self.peer_host.stop()
            except Exception:  # noqa: BLE001
                pass
            self.peer_host = None
        indexed = 0
        try:
            conn = lore_store.db_connect()
            added, _consumed = lore_store.index_live(conn, self.transcript_path)
            indexed = added
        except Exception:  # noqa: BLE001 -- an index failure never blocks quit
            pass
        return EngineEvent("session_done", {
            "indexed": indexed,
            "belief_count": self.belief_count(),
            "review": "skipped -- the LORE review is not wired for this engine",
        })

    # -- turns ---------------------------------------------------------

    def _system_message(self) -> dict:
        """The system prompt, rebuilt every turn around a fresh LORE
        snapshot.

        This is what ``hooks=False`` costs and what it does NOT cost. The
        UserPromptSubmit hook surface genuinely does not exist here, so
        the field is False; the thing DOXA used it for -- keeping the LORE
        snapshot current as a conversation runs -- is done by rebuilding
        this message on every turn instead, which is strictly fresher than
        the throttled refresh the hook performs."""
        from lore_core import context as lore_context

        snapshot = ""
        try:
            snapshot = lore_context.build_context(self.cwd) or ""
        except Exception:  # noqa: BLE001 -- a LORE store that cannot be read
            # is a session without memory, not a session that cannot run.
            snapshot = ""
        self.lore_snapshot_chars = len(snapshot)
        header = (
            f"You are a DOXA session running on {self.spec.display_name}. "
            f"The working directory is {self.cwd}."
        )
        return {
            "role": "system",
            "content": f"{header}\n\n{snapshot}" if snapshot else header,
        }

    async def send(self, prompt: str) -> AsyncIterator[EngineEvent]:
        """One turn: as many model calls as the model's tool use needs.

        The turn is a LOOP, not a request, and that is the whole
        difference between this engine and a chat client: the model
        answers, DOXA runs whatever tools it named through
        :class:`doxa.gate.ToolGate`, feeds the results back, and asks
        again -- up to :data:`MAX_TOOL_STEPS` times. Every await inside is
        measured against one wall-clock deadline built from
        :data:`TURN_TIMEOUT_SECS`, so a model that will not stop calling
        tools ends as a failed turn that says so rather than as a pane
        whose worker never comes back."""
        if self._pending_peer_frames:
            frames, self._pending_peer_frames = self._pending_peer_frames, []
            prompt_out = peers_mod.frame_for_model(frames) + "\n\n" + prompt
        else:
            prompt_out = prompt

        if self.num_turns == 0 and self.peer_host is not None:
            # First turn only: the peer rail's row for this session gets
            # its name from what the operator actually asked for.
            try:
                self.peer_host.set_title(_peer_title(prompt))
            except Exception:  # noqa: BLE001
                pass

        self._persist_user_text(prompt_out)
        self.messages.append({"role": "user", "content": prompt_out})
        # Claimed HERE, at the one moment the turn becomes a fact, so a
        # failing and a succeeding turn report the same number -- the
        # off-by-one CodexEngine fixed in v1.7.3, not repeated.
        self.num_turns += 1
        yield EngineEvent("turn_started", {
            "prompt": prompt, "peer_context": prompt_out is not prompt,
        })

        started = time.monotonic()
        deadline = started + TURN_TIMEOUT_SECS
        failure: "str | None" = None
        steps = 0

        while True:
            steps += 1
            if steps > MAX_TOOL_STEPS:
                failure = (
                    f"the turn made {MAX_TOOL_STEPS} tool round trips without "
                    "finishing and was stopped"
                )
                break
            budget = deadline - time.monotonic()
            if budget <= 0:
                failure = (
                    f"the turn ran past its {TURN_TIMEOUT_SECS:.0f}s limit "
                    "and was stopped"
                )
                break

            completion = _Completion()
            try:
                async for event in self._stream_once(completion, budget):
                    yield event
            except MissingCredential as exc:
                # The one error whose text is guaranteed key-free by
                # construction -- see credential().
                failure = str(exc)
                break
            except VendorApiError as exc:
                failure = self._vendor_failure(exc)
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- any other failure is
                # this turn's, not the session's.
                failure = f"{type(exc).__name__}: {_scrub(str(exc))}"
                break

            if completion.model:
                self.resolved_model = completion.model
            self._absorb_usage(completion.usage)

            assistant: dict = {"role": "assistant", "content": completion.text}
            if completion.tool_calls:
                assistant["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in completion.tool_calls
                ]
            self.messages.append(assistant)
            if completion.text:
                self._persist_assistant_text(completion.text)

            if not completion.tool_calls:
                break

            for call in completion.tool_calls:
                yield EngineEvent("tool_call", {
                    "id": call.id, "name": call.name, "input": call.parsed(),
                })
                result = await self._run_tool(call)
                summary, is_error = _tool_summary(result)
                yield EngineEvent("tool_result", {
                    "id": call.id,
                    "name": call.name,
                    "result_summary": summary,
                    "is_error": is_error,
                    "duration_ms": None,
                })
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })

        self._save_messages()
        if failure:
            # Readable AND marked, the same pair CodexEngine's
            # _turn_failure keeps together: is_error alone paints an error
            # beside a turn with no text in it, which sends an operator
            # looking in the wrong place.
            yield EngineEvent("text_delta", {
                "text": f"{self.spec.engine_id}: {failure}",
            })
        yield EngineEvent("turn_done", {
            "duration_ms": int((time.monotonic() - started) * 1000),
            # None, never 0.0 -- see cost=False. A renderer handed 0.0
            # prints "$0.0000", and that is a claim.
            "cost_usd": None,
            "session_cost_usd": None,
            "num_turns": self.num_turns,
            "is_error": bool(failure),
            **({"error": failure} if failure else {}),
            # Three Nones, and they are the point: an unreported window is
            # unknown, and every surface downstream already says so.
            "ctx_percentage": None,
            "ctx_tokens": None,
            "ctx_max_tokens": None,
            "model": self.resolved_model or self.model,
        })

    def _vendor_failure(self, exc: VendorApiError) -> str:
        """A vendor error as the sentence the block will show.

        Scrubbed against the live key by value, because DeepSeek's own 401
        body quotes part of the key it rejected -- see :func:`_scrub`. The
        key is fetched here and dropped at the end of the expression; it
        is never stored on the engine."""
        try:
            key = credential(self.spec)
        except MissingCredential:
            key = None
        detail = _truncate(_scrub(exc.detail, key), ERROR_BODY_MAX)
        if exc.code in TERMINAL_ERROR_CODES:
            return (
                f"{self.spec.display_name} refused the request permanently "
                f"(code {exc.code}) -- retrying will not help: {detail}"
            )
        if exc.status == 401:
            return (
                f"{self.spec.display_name} rejected the credential in "
                f"${self.spec.env_var} (HTTP 401)"
            )
        if exc.status == 0:
            return f"the request to {self.spec.display_name} did not complete: {detail}"
        return f"{self.spec.display_name} answered HTTP {exc.status}: {detail}"

    async def _stream_once(
        self, out: _Completion, budget: float
    ) -> AsyncIterator[EngineEvent]:
        """One model call. Yields deltas; fills ``out`` with the rest.

        An out-parameter rather than a return value because this is an
        async GENERATOR -- the events have to reach the pane as they
        arrive, and a generator's return value is not reachable from an
        ``async for``. ``out`` is constructed by the caller one line
        above, so the indirection stays local."""
        key = credential(self.spec)
        body = request_body(
            self.spec,
            [self._system_message()] + self.messages,
            self.model,
            self.effort,
            tools=self._tools or None,
        )
        tools = _ToolCallAccumulator()
        async for payload in self._transport.stream(
            self.spec.chat_url, body, auth_headers(key), budget
        ):
            try:
                chunk = json.loads(payload)
            except (json.JSONDecodeError, TypeError, ValueError):
                # A line that is not JSON is the protocol breaking, not a
                # frame this build has not learned. Dropped, because there
                # is nothing to map -- and the stream carries its own end
                # markers, so one bad line does not desynchronise it.
                continue
            if not isinstance(chunk, dict):
                continue
            if isinstance(chunk.get("model"), str) and chunk["model"]:
                out.model = chunk["model"]
            if isinstance(chunk.get("usage"), dict):
                out.usage = chunk["usage"]
            choices = chunk.get("choices")
            if not isinstance(choices, list) or not choices:
                # Measured: both vendors put usage on a chunk that also
                # carries a choice, but an OpenAI-compatible server is
                # allowed to send a usage-only chunk with choices: [], and
                # one that does must not be read as an empty delta.
                continue
            choice = choices[0] if isinstance(choices[0], dict) else {}
            if isinstance(choice.get("finish_reason"), str):
                out.finish_reason = choice["finish_reason"]
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                out.reasoning += reasoning
                yield EngineEvent("reasoning_delta", {"text": _scrub(reasoning)})
            content = delta.get("content")
            if isinstance(content, str) and content:
                out.text += content
                yield EngineEvent("text_delta", {"text": _scrub(content)})
            if delta.get("tool_calls"):
                tools.absorb(delta["tool_calls"])
        out.tool_calls = tools.finished()
        for index, call in enumerate(out.tool_calls):
            if not call.id:
                # Both vendors send one; a server that does not still has
                # to be answerable, and a tool result with no
                # tool_call_id is a message the API rejects.
                call.id = f"call_{index}"

    async def _run_tool(self, call: _ToolCall) -> dict:
        """Execute one named call through :class:`doxa.gate.ToolGate`.

        Never raises -- that is ToolGate's own contract (every failure is
        an ordinary ``{"error": ...}`` result the model reads and recovers
        from), and it is preserved here for the one case the gate cannot
        cover: a session whose tool surface failed to import at all, which
        must answer a call rather than crash the turn."""
        if self._gate is None:
            return {"error": "tools are not available in this session"}
        result = self._gate.execute(call.name, call.parsed())
        if hasattr(result, "__await__"):
            result = await result
        return result if isinstance(result, dict) else {"result": result}

    def _on_tool_disabled(self, name: str, reason: str) -> None:
        """The two-strikes tracker removed a tool. Out-of-band, because it
        fires from inside a tool execution rather than at a yield point."""
        self._peer_queue.put_nowait(EngineEvent("tool_disabled", {
            "name": name, "reason": _truncate(_scrub(reason)),
        }))

    def _absorb_usage(self, usage: Any) -> None:
        """Accumulate one call's ``usage`` into the session totals.

        Tokens only. Nothing here touches ``last_ctx_*``: ``prompt_tokens``
        is exactly what was resident in the window for that call, but the
        window's SIZE is unreported, and a percentage needs both. Reading
        one as the other is the fabricated figure ``context_window=False``
        exists to refuse."""
        if not isinstance(usage, dict):
            return
        details = usage.get("completion_tokens_details")
        reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
        prompt_details = usage.get("prompt_tokens_details")
        cached = prompt_details.get("cached_tokens") if isinstance(prompt_details, dict) else None
        # DeepSeek also reports the hit/miss split at the top level; GLM
        # reports neither. `cached_tokens` is the field BOTH send, so it is
        # the one the totals are keyed on.
        if usage.get("prompt_cache_hit_tokens") is not None:
            cached = usage.get("prompt_cache_hit_tokens")
        for value, target in (
            (usage.get("prompt_tokens"), "input_tokens"),
            (usage.get("completion_tokens"), "output_tokens"),
            (cached, "cache_read_input_tokens"),
            (reasoning, "reasoning_output_tokens"),
        ):
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                self.usage_totals[target] = self.usage_totals.get(target, 0) + value
        if self.peer_host is not None and self.usage_totals:
            try:
                self.peer_host.update_usage(sum(self.usage_totals.values()))
            except Exception:  # noqa: BLE001
                pass

    # -- the settable surface ------------------------------------------

    async def set_model(self, model: "str | None") -> str:
        """Takes effect on the NEXT request. The API is stateless and DOXA
        owns the message list, so there is nothing to migrate -- but it is
        still the next turn rather than this one, and the string says so
        instead of claiming a live switch mid-stream."""
        self.model = model or self.spec.default_model
        # The RESOLVED model belongs to the answer that produced it. A new
        # request may well be answered by something else (see the module
        # docstring on DeepSeek's substitution), so the old value must not
        # survive the switch and be read as this model's.
        self.resolved_model = None
        if self.peer_host is not None:
            try:
                self.peer_host.set_model(self.model)
            except Exception:  # noqa: BLE001
                pass
        return f"{self.model} (from the next turn)"

    async def set_permission_mode(self, mode: str) -> str:
        """Refused, by name. There is no tool-permission posture at a chat
        API: DOXA owns the whole tool surface itself, through ToolGate, so
        a mode chip would claim a setting this session does not have. See
        ``permission_modes=False``."""
        raise NotImplementedError(
            f"the {self.spec.engine_id} engine has no permission modes -- "
            "DOXA executes every tool call itself, through the same gate, "
            "and there is no second posture to switch to"
        )

    async def switch_branch(self, target: "str | None") -> dict:
        raise NotImplementedError(
            f"the {self.spec.engine_id} engine does not manage its own worktree"
        )

    async def answer_needs_input(self, req_id: str, answer: dict) -> bool:
        """Nothing ever asks. There is no can_use_tool callback and no
        AskUserQuestion in this protocol, so no needs_input event is ever
        emitted and there is nothing to answer. False rather than a raise,
        so a stale dialog from another engine's session cannot explode."""
        return False

    # -- what the surfaces read ----------------------------------------

    async def context_usage(self) -> "dict[str, Any] | None":
        """None, always, and honestly: no window size is reported anywhere
        by either vendor. ``/context`` prints its own "cannot be asked"
        line for exactly this, and the ctx chip is omitted rather than
        painting a percentage computed against a number DOXA invented."""
        return None

    def usage_summary(self) -> "dict[str, Any]":
        return {
            "session_id": self.session_id,
            "model": self.model,
            "resolved_model": self.resolved_model,
            "num_turns": self.num_turns,
            # None, never 0.0 -- /usage omits what is absent.
            "total_cost_usd": None,
            "ctx_percentage": None,
            "ctx_tokens": None,
            "ctx_max_tokens": None,
            **self.usage_totals,
        }

    def belief_count(self) -> int:
        """The same COUNT(*) SessionEngine runs. The belief store is the
        PROJECT's, not the engine's, so this tab's chip shows the real
        number rather than a zero that would read as "this session has no
        memory"."""
        try:
            conn = lore_store.db_connect()
            return conn.execute(
                "SELECT count(*) FROM beliefs WHERE status = 'active'"
            ).fetchone()[0]
        except Exception:  # noqa: BLE001
            return 0

    def disabled_tools(self) -> "list[str]":
        """Real, and that is the difference from Codex: the two-strikes
        tracker lives in ToolGate, and this engine routes every call
        through one."""
        return list(self._gate.disabled_tools()) if self._gate is not None else []

    # -- peers ---------------------------------------------------------

    def _on_peer_frame(self, frame: dict) -> None:
        self._pending_peer_frames.append(dict(frame))
        self._peer_queue.put_nowait(EngineEvent("peer_message", dict(frame)))

    def _on_peer_joined(self, info: "peers_mod.PeerInfo") -> None:
        self._peer_queue.put_nowait(EngineEvent("peer_joined", {
            "session_id": info.session_id, "title": info.title, "cwd": info.cwd,
        }))

    def _on_peer_left(self, session_id: str) -> None:
        self._peer_queue.put_nowait(EngineEvent("peer_left", {"session_id": session_id}))

    async def peer_events(self) -> AsyncIterator[EngineEvent]:
        while True:
            yield await self._peer_queue.get()

    def list_peers(self) -> list:
        return self.peer_host.list_peers() if self.peer_host is not None else []

    def peer_count(self) -> int:
        return len(self.list_peers())

    async def send_peer_message(self, target_prefix: str, text: str) -> Any:
        if self.peer_host is None:
            raise peers_mod.PeerSendError("peer layer is not running in this session")
        peer = peers_mod.resolve_peer(self.peer_host.list_peers(), target_prefix)
        await peers_mod.send_message(
            peer.socket_path,
            from_id=self.session_id,
            from_title=self.peer_host.title,
            body=text,
        )
        return peer


# -- the providers -----------------------------------------------------


class VendorEngineProvider:
    """One registry entry per :class:`VendorSpec`.

    Holds no state, opens no connection and reads no credential -- building
    one costs nothing, which is what lets ``doxa.engines`` register both
    vendors on first lookup without deciding whether either is usable."""

    def __init__(self, spec: VendorSpec) -> None:
        self.spec = spec

    def engine_id(self) -> str:
        return self.spec.engine_id

    def engine_display_name(self) -> str:
        return self.spec.display_name

    def supports(self) -> EngineCapabilities:
        """The SAME map for both vendors, by construction.

        This is the experiment's control, made structural: cells C and D
        of docs/plans/emergent-organization.md need agents that differ in
        model and in nothing else, and two providers that return one
        shared object cannot drift apart the way two hand-maintained maps
        would."""
        return VENDOR_CAPABILITIES

    def new_session(self, **kwargs: Any) -> Engine:
        # The spec is forced, not defaulted: a caller that passed its own
        # would silently get a session on the other vendor under this
        # provider's id, which is the one mix-up a randomised fleet would
        # never notice.
        kwargs.pop("spec", None)
        return ChatApiEngine(spec=self.spec, **kwargs)


class DeepSeekEngineProvider(VendorEngineProvider):
    def __init__(self) -> None:
        super().__init__(DEEPSEEK)


class GLMEngineProvider(VendorEngineProvider):
    def __init__(self) -> None:
        super().__init__(GLM)


# -- small shared helpers ----------------------------------------------


def _tool_summary(result: Any) -> "tuple[str, bool]":
    """``(summary, is_error)`` for one tool result, for the chip.

    An ``{"error": ...}`` dict is ToolGate's ordinary refused/failed shape
    -- the model reads it and recovers -- so it marks the chip without
    ending anything."""
    is_error = isinstance(result, dict) and isinstance(result.get("error"), str)
    if is_error:
        return (_truncate(_scrub(result["error"])), True)
    try:
        text = json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(result)
    return (_truncate(_scrub(text)), False)


def _load_messages(path: Path) -> "list[dict]":
    """A saved conversation, or an empty one.

    A resume that cannot find its file starts fresh rather than failing:
    the session id still names the transcript, the registry row and the
    /search result, so the session is real -- it simply has no history to
    replay, which is what an empty list means everywhere else too."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    if not isinstance(loaded, list):
        return []
    return [m for m in loaded if isinstance(m, dict) and m.get("role")]


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _peer_title(prompt: str) -> str:
    """The peer registry's title for this session, from its first prompt:
    first line, internal whitespace collapsed, capped.

    The same rule ``doxa.engine._peer_title_from_prompt`` states, written
    again rather than imported -- importing it would pull
    ``claude_agent_sdk``'s 404 ms into a session that has no Claude in
    it."""
    lines = [line for line in str(prompt or "").strip().splitlines() if line.strip()]
    if not lines:
        return "session"
    return " ".join(lines[0].split())[:PEER_TITLE_MAX]


def lore_root_path() -> str:
    """Where LORE keeps its store, for the ``lore_root`` attribute the
    status surfaces read off any engine handle."""
    from lore_core.config import ROOT

    return str(ROOT)
