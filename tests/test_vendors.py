# SPDX-License-Identifier: AGPL-3.0-only
"""The third-party chat-completions engines: DeepSeek and GLM.

THE CHECK THIS FILE OWES. ``docs/plans/emergent-organization.md`` reads
``EngineCapabilities`` to assert that the agents in its mixed-vendor arms
differed in model and in nothing else. A capability map that lies would
not fail a run -- it would silently invalidate a study. So every test
below that touches a capability field tests the FIELD AGAINST THE
BEHAVIOUR, in both directions: a True field must be demonstrated by the
engine actually doing the thing through its transport, and a False field
must be demonstrated by the surface saying so rather than faking it.

NO NETWORK, NO CREDENTIALS. Every test here drives
``ChatApiEngine(transport=...)`` with a scripted SSE stream and an
injected environment, the same discipline ``SessionEngine(client_factory=
...)`` and ``CodexEngine(exec_factory=...)`` established. The one live
test at the bottom skips cleanly when the keys are absent.

THE SCRIPTS ARE THE MEASURED SHAPES. Every chunk builder below is a
transcription of what the live API actually sent on 2026-09-17 -- notably
the two DIFFERENT tool-call shapes (DeepSeek fragments a call across many
deltas; GLM sends one whole), and DeepSeek answering a request for
``deepseek-chat`` with ``deepseek-flash`` in the response's own ``model``
field. Scripting the documented shape instead of the measured one would
make this suite two readings of one doc that agree with each other.
"""

from __future__ import annotations

import json
import os

import pytest

from doxa import engines as engines_mod
from doxa import vendors as vendors_mod
from doxa.engines import (
    DEEPSEEK_ENGINE_ID,
    GLM_ENGINE_ID,
    Engine,
    EngineCapabilities,
    capabilities_of,
)
from doxa.vendors import (
    DEEPSEEK,
    GLM,
    VENDOR_CAPABILITIES,
    ChatApiEngine,
    DeepSeekEngineProvider,
    GLMEngineProvider,
    MissingCredential,
    VendorApiError,
    credential,
    request_body,
)

FAKE_ENV = {"DEEPSEEK_API_KEY": "ds-test-key-0001", "ZAI_API_KEY": "zai-test-key-0002"}


@pytest.fixture(autouse=True)
def fake_keys(monkeypatch, request):
    """Both vendors' keys, in the process environment, for every test here
    except the live one.

    monkeypatch rather than an injectable `env` argument on the engine, and
    that is the point rather than a convenience: an env dict the engine
    held would BE a credential stored on the handle, reachable from
    vars(), a repr or a pickle. The engine reads os.environ at request
    time and keeps nothing, so the test path and the production path are
    the same path -- and test_the_credential_is_never_an_attribute_of_the
    _engine below can prove it by looking."""
    if "live_smoke" in request.node.name:
        return
    for name, value in FAKE_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("DOXA_VENDOR_EFFORT", raising=False)
    # Explicitly OFF rather than inherited. Since docwilde/doxa#39 this
    # engine wires a peer_send seam, so the user's switch is the only
    # thing deciding whether the tool is projected -- a suite that read
    # the developer's own environment for it would pass or fail by
    # machine. tests/test_peer_delivery.py is where it is turned on.
    monkeypatch.delenv("DOXA_AGENT_PEER_SEND", raising=False)


# -- the stub transport ------------------------------------------------


class StubTransport:
    """Replays scripted SSE payloads and records what was sent.

    One script per model call, popped in order, so a tool-using turn (two
    calls: the one that names the tool, the one that reads its result) is
    written as two scripts. A script may be an exception instead of a list
    of chunks, which is how a vendor failure is driven."""

    def __init__(self, *scripts) -> None:
        self.scripts = list(scripts)
        self.requests: list[dict] = []

    async def stream(self, url, body, headers, timeout):
        self.requests.append(
            {"url": url, "body": body, "headers": headers, "timeout": timeout}
        )
        script = self.scripts.pop(0) if self.scripts else []
        if isinstance(script, BaseException):
            raise script
        for chunk in script:
            yield json.dumps(chunk)

    @property
    def last_body(self) -> dict:
        return self.requests[-1]["body"]


def _chunk(model: str, delta: dict, finish=None, usage=None) -> dict:
    out: dict = {
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        out["usage"] = usage
    return out


USAGE = {
    "prompt_tokens": 33,
    "completion_tokens": 29,
    "total_tokens": 62,
    "prompt_tokens_details": {"cached_tokens": 4},
    "completion_tokens_details": {"reasoning_tokens": 26},
}


def prose_script(model="deepseek-flash", text=("Hel", "lo"), reasoning=("think",)):
    """The measured shape of an ordinary answer: role first, then
    reasoning_content deltas, then content deltas, then a final chunk
    carrying finish_reason AND usage on the same chunk."""
    script = [_chunk(model, {"role": "assistant", "content": None, "reasoning_content": ""})]
    script += [_chunk(model, {"reasoning_content": r}) for r in reasoning]
    script += [_chunk(model, {"content": t}) for t in text]
    script.append(_chunk(model, {"content": ""}, finish="stop", usage=USAGE))
    return script


def deepseek_tool_script(name="lore_belief_search", args='{"query": "deploys"}'):
    """DeepSeek's measured tool shape: the first delta carries id/type/name
    with EMPTY arguments, and every delta after it carries one more
    fragment of the arguments JSON and nothing else."""
    model = "deepseek-flash"
    script = [_chunk(model, {"role": "assistant", "content": None})]
    script.append(_chunk(model, {"tool_calls": [{
        "index": 0, "id": "call_00_abc", "type": "function",
        "function": {"name": name, "arguments": ""},
    }]}))
    script += [
        _chunk(model, {"tool_calls": [{"index": 0, "function": {"arguments": piece}}]})
        for piece in args
    ]
    script.append(_chunk(model, {"content": ""}, finish="tool_calls", usage=USAGE))
    return script


def glm_tool_script(name="lore_belief_search", args='{"query": "deploys"}'):
    """GLM's measured tool shape: ONE delta carrying id, name and the
    complete arguments JSON."""
    model = "glm-5.3-flash"
    return [
        _chunk(model, {"tool_calls": [{
            "index": 0, "id": "call_47a5", "type": "function",
            "function": {"name": name, "arguments": args},
        }]}),
        _chunk(model, {"role": "assistant", "content": ""},
               finish="tool_calls", usage=USAGE),
    ]


def engine(tmp_path, spec=DEEPSEEK, transport=None, **kwargs) -> ChatApiEngine:
    return ChatApiEngine(
        cwd=str(tmp_path), spec=spec, transport=transport or StubTransport(), **kwargs,
    )


async def run_turn(eng: ChatApiEngine, prompt: str = "hi") -> list:
    return [event async for event in eng.send(prompt)]


def of_type(events, kind) -> list:
    return [e for e in events if e.type == kind]


# -- the registry ------------------------------------------------------


def test_both_vendors_are_registered_under_the_ids_the_flag_takes():
    assert engines_mod.is_known("deepseek")
    assert engines_mod.is_known("glm")
    assert engines_mod.get("deepseek").engine_id() == DEEPSEEK_ENGINE_ID
    assert engines_mod.get("GLM").engine_id() == GLM_ENGINE_ID


def test_an_unknown_engine_is_refused_the_way_get_codex_refuses():
    """Same refusal, same message shape: the id that was asked for, and
    the list of the ones that exist. Silently falling back to Claude would
    start a session on an engine nobody asked for -- and in a randomised
    fleet, one that the ledger would then record as the wrong vendor."""
    with pytest.raises(KeyError) as excinfo:
        engines_mod.get("deepsek")
    message = excinfo.value.args[0]
    assert "deepsek" in message
    for known in ("claude", "codex", "deepseek", "glm"):
        assert known in message


def test_the_two_providers_return_the_same_map_object():
    """Capability parity, made structural. Two hand-maintained maps could
    drift; one shared object cannot, and the experiment's control depends
    on it not drifting."""
    assert DeepSeekEngineProvider().supports() is GLMEngineProvider().supports()
    assert DeepSeekEngineProvider().supports() is VENDOR_CAPABILITIES


def test_a_provider_that_declares_nothing_gets_a_fully_false_map():
    """The conservative default, which is what makes an honest map
    possible at all: forgetting a field under-promises."""
    bare = EngineCapabilities()
    assert not any(
        getattr(bare, name) for name in EngineCapabilities.__dataclass_fields__
    )

    class Undeclared:
        """A provider that declares nothing at all."""

        def engine_id(self):
            return "undeclared"

        def engine_display_name(self):
            return "Undeclared"

        def supports(self):
            return EngineCapabilities()

        def new_session(self, **kwargs):
            raise NotImplementedError

    assert Undeclared().supports() == bare


def test_both_engines_satisfy_the_protocol(tmp_path):
    assert isinstance(engine(tmp_path, DEEPSEEK), Engine)
    assert isinstance(engine(tmp_path, GLM), Engine)


def test_a_handle_is_believed_about_itself(tmp_path):
    assert capabilities_of(engine(tmp_path)) is VENDOR_CAPABILITIES


# -- credentials -------------------------------------------------------


def test_a_missing_credential_names_the_variable_and_not_its_value():
    with pytest.raises(MissingCredential, match=r"DEEPSEEK_API_KEY"):
        credential(DEEPSEEK, {})
    with pytest.raises(MissingCredential, match=r"ZAI_API_KEY"):
        credential(GLM, {"ZAI_API_KEY": "   "})


async def test_a_missing_credential_fails_the_session_at_start_not_mid_turn(
    tmp_path, monkeypatch
):
    """A key that is absent has to fail as a session that could not start,
    naming the variable -- not as a 401 three minutes into the first
    turn."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    eng = ChatApiEngine(cwd=str(tmp_path), spec=DEEPSEEK, transport=StubTransport())
    with pytest.raises(MissingCredential, match=r"\$DEEPSEEK_API_KEY"):
        await eng.start()


async def test_the_credential_is_never_an_attribute_of_the_engine(tmp_path):
    """The strongest guarantee available: the key is read from the
    environment when a request is built and dropped when it returns, so
    there is nothing for a repr, a pickle or a surviving traceback frame
    to leak."""
    eng = engine(tmp_path, transport=StubTransport(prose_script()))
    await eng.start()
    await run_turn(eng)
    blob = repr(eng) + repr(vars(eng)) + repr(vars(eng.spec))
    for secret in FAKE_ENV.values():
        assert secret not in blob


async def test_a_vendor_error_body_quoting_the_key_is_scrubbed(tmp_path):
    """MEASURED, not hypothetical: DeepSeek's real 401 body reads
    "Authentication Fails, Your api key: ****nope is invalid". A failure
    message that passed a vendor body through verbatim would put key
    material into the transcript and the block."""
    key = FAKE_ENV["DEEPSEEK_API_KEY"]
    detail = json.dumps({"error": {
        "message": f"Authentication Fails, Your api key: {key} is invalid",
        "code": "invalid_request_error",
    }})
    transport = StubTransport(VendorApiError(401, detail, "invalid_request_error"))
    eng = engine(tmp_path, transport=transport)
    await eng.start()
    events = await run_turn(eng)
    blob = json.dumps([[e.type, e.data] for e in events])
    assert key not in blob
    assert "DEEPSEEK_API_KEY" in blob  # it says WHICH variable to fix
    assert of_type(events, "turn_done")[0].data["is_error"] is True


# -- the True fields, each demonstrated through the transport ----------


async def test_streaming_text_is_true_because_content_arrives_in_pieces(tmp_path):
    transport = StubTransport(prose_script(text=("Hel", "lo", " there")))
    eng = engine(tmp_path, transport=transport)
    await eng.start()
    events = await run_turn(eng)
    deltas = of_type(events, "text_delta")
    assert [e.data["text"] for e in deltas] == ["Hel", "lo", " there"]
    assert VENDOR_CAPABILITIES.streaming_text is True
    # And the request actually asked for a stream -- a True field that
    # came from a non-streaming body would be a claim about nothing.
    assert transport.last_body["stream"] is True


async def test_reasoning_is_true_because_reasoning_content_arrives(tmp_path):
    transport = StubTransport(prose_script(reasoning=("We ", "need")))
    eng = engine(tmp_path, transport=transport)
    await eng.start()
    events = await run_turn(eng)
    assert [e.data["text"] for e in of_type(events, "reasoning_delta")] == ["We ", "need"]
    assert VENDOR_CAPABILITIES.reasoning is True


async def test_token_usage_is_true_and_the_body_asked_for_it(tmp_path):
    """Both halves. Without stream_options.include_usage the final chunk
    carries no usage at all, so the capability would be a claim the
    request itself made impossible to honour."""
    transport = StubTransport(prose_script())
    eng = engine(tmp_path, transport=transport)
    await eng.start()
    await run_turn(eng)
    assert transport.last_body["stream_options"] == {"include_usage": True}
    assert eng.usage_totals == {
        "input_tokens": 33,
        "output_tokens": 29,
        "cache_read_input_tokens": 4,
        "reasoning_output_tokens": 26,
    }
    assert VENDOR_CAPABILITIES.token_usage is True


async def test_resolved_model_is_true_and_is_not_what_was_asked_for(tmp_path):
    """THE MEASURED CASE, replayed. DeepSeek answers a request for the
    legacy name `deepseek-chat` with `deepseek-flash`, HTTP 200, and the
    response's own `model` field is the only place that truth appears. An
    experiment that assigned models randomly and then recorded the
    REQUESTED name would be recording a model that did not answer."""
    transport = StubTransport(prose_script(model="deepseek-flash"))
    eng = engine(tmp_path, transport=transport, model="deepseek-chat")
    await eng.start()
    events = await run_turn(eng)
    assert transport.last_body["model"] == "deepseek-chat"   # what was asked
    assert eng.resolved_model == "deepseek-flash"            # what answered
    assert eng.model == "deepseek-chat"                      # kept distinct
    assert of_type(events, "turn_done")[0].data["model"] == "deepseek-flash"
    assert eng.usage_summary()["resolved_model"] == "deepseek-flash"
    assert VENDOR_CAPABILITIES.resolved_model is True


async def test_live_model_switch_is_true_and_takes_the_next_request(tmp_path):
    transport = StubTransport(prose_script(), prose_script(model="deepseek-v4-pro"))
    eng = engine(tmp_path, transport=transport)
    await eng.start()
    await run_turn(eng)
    assert await eng.set_model("deepseek-v4-pro") == "deepseek-v4-pro (from the next turn)"
    # The resolved model does NOT survive the switch: it belongs to the
    # answer that produced it, and the next answer may be something else.
    assert eng.resolved_model is None
    await run_turn(eng, "again")
    assert transport.requests[-1]["body"]["model"] == "deepseek-v4-pro"
    assert VENDOR_CAPABILITIES.live_model_switch is True


async def test_mcp_tools_is_true_because_the_operators_reach_the_model(tmp_path):
    """DOXA's LORE operators are offered as OpenAI function tools, and
    their schema is the SAME object to_sdk_tools hands the SDK."""
    transport = StubTransport(prose_script())
    eng = engine(tmp_path, transport=transport)
    await eng.start()
    await run_turn(eng)
    offered = {t["function"]["name"] for t in transport.last_body["tools"]}
    from doxa.operators import OPERATORS, WRITE_OPERATORS

    # The SAME surface a Claude session gets, including the write path --
    # lore_remember only STAGES a proposal for the review gate. A narrower
    # surface would be a capability difference the map does not record.
    #
    # peer_send is the ONE subtraction, and since docwilde/doxa#39 it is
    # the USER's switch rather than a missing seam: this engine now holds
    # a doxa.peerdelivery.PeerDelivery and names it in the ctx, so with
    # DOXA_AGENT_PEER_SEND set the tool is offered and really sends (see
    # tests/test_peer_delivery.py). The fixture above clears that
    # variable, so what this line measures is the default install.
    # Spelled as a set difference rather than a hardcoded list, so adding
    # a sixth read operator does not have to touch it.
    assert offered == (set(OPERATORS) | set(WRITE_OPERATORS)) - {"peer_send"}
    assert "lore_belief_search" in offered
    assert "lore_remember" in offered
    assert "peer_list" in offered, (
        "peer DISCOVERY is gated on neither the seam nor the switch -- it "
        "reads two files this process can open -- so a vendor session has "
        "it either way"
    )
    assert "peer_send" not in offered, (
        "with the switch off the send tool is not refused, it is not "
        "OFFERED: a tool the model cannot see is a tool it cannot call"
    )
    assert transport.last_body["tool_choice"] == "auto"
    assert VENDOR_CAPABILITIES.mcp_tools is True


async def test_spawn_session_is_not_offered_because_spawn_sessions_is_false(tmp_path):
    """The other direction of the same check. A tool the model cannot see
    is a tool the model cannot call, and offering one that would silently
    start a CLAUDE child from a DeepSeek parent is the mislabelled agent a
    randomised fleet would never notice."""
    transport = StubTransport(prose_script())
    eng = engine(tmp_path, transport=transport)
    await eng.start()
    await run_turn(eng)
    offered = {t["function"]["name"] for t in transport.last_body["tools"]}
    assert "spawn_session" not in offered
    assert VENDOR_CAPABILITIES.spawn_sessions is False


@pytest.mark.parametrize(
    "spec,script",
    [(DEEPSEEK, deepseek_tool_script()), (GLM, glm_tool_script())],
    ids=["deepseek-fragmented", "glm-whole"],
)
async def test_a_tool_call_is_assembled_executed_and_fed_back(tmp_path, spec, script):
    """BOTH measured wire shapes, through one accumulator: DeepSeek
    fragments a call across many deltas, GLM sends one whole. A shape that
    only handled the fragmented case would work on one vendor and silently
    call nothing on the other."""
    transport = StubTransport(script, prose_script(text=("done",)))
    eng = engine(tmp_path, spec=spec, transport=transport)
    await eng.start()
    events = await run_turn(eng, "search please")

    calls = of_type(events, "tool_call")
    assert len(calls) == 1
    assert calls[0].data["name"] == "lore_belief_search"
    assert calls[0].data["input"] == {"query": "deploys"}   # fragments rejoined
    results = of_type(events, "tool_result")
    assert len(results) == 1
    # A real measurement, not a None: the call is named, run and answered
    # inside one block, so there is nothing to stitch across frames.
    assert isinstance(results[0].data["duration_ms"], int)

    # The result was fed back, so a second request happened and it carries
    # the assistant's tool_calls plus a tool message answering them.
    assert len(transport.requests) == 2
    replayed = transport.requests[1]["body"]["messages"]
    assistant = [m for m in replayed if m.get("role") == "assistant"][-1]
    assert assistant["tool_calls"][0]["function"]["name"] == "lore_belief_search"
    tool_msg = [m for m in replayed if m.get("role") == "tool"][-1]
    assert tool_msg["tool_call_id"] == assistant["tool_calls"][0]["id"]


async def test_tool_gate_is_true_because_an_unknown_tool_degrades_gracefully(tmp_path):
    """ToolGate's contract, reached through this engine: an unknown name
    is an ordinary error result the model reads and recovers from, never
    an exception that ends the turn."""
    transport = StubTransport(
        deepseek_tool_script(name="not_a_real_tool"), prose_script(text=("ok",)),
    )
    eng = engine(tmp_path, transport=transport)
    await eng.start()
    events = await run_turn(eng)
    result = of_type(events, "tool_result")[0]
    assert result.data["is_error"] is True
    assert "unknown tool" in result.data["result_summary"]
    assert of_type(events, "turn_done")[0].data["is_error"] is False
    assert VENDOR_CAPABILITIES.tool_gate is True


async def test_the_two_strikes_tracker_really_disables_a_tool(tmp_path):
    """The rest of tool_gate=True: a SECOND hard failure removes the tool
    for the session and fires the tool_disabled event. Driven through a
    real ToolGate, with the operator itself made to fail, rather than by
    poking the gate's own state."""
    transport = StubTransport(
        deepseek_tool_script(), prose_script(text=("a",)),
        deepseek_tool_script(), prose_script(text=("b",)),
        prose_script(text=("c",)),
    )
    eng = engine(tmp_path, transport=transport)
    await eng.start()

    def explode(**_kwargs):
        raise RuntimeError("backend is down")

    from doxa.operators import OPERATORS

    original = OPERATORS["lore_belief_search"].fn
    object.__setattr__(OPERATORS["lore_belief_search"], "fn", explode)
    try:
        await run_turn(eng, "one")
        await run_turn(eng, "two")
    finally:
        object.__setattr__(OPERATORS["lore_belief_search"], "fn", original)

    assert eng.disabled_tools() == ["lore_belief_search"]
    disabled = [e for e in [eng._peer_queue.get_nowait() for _ in range(
        eng._peer_queue.qsize())] if e.type == "tool_disabled"]
    assert [e.data["name"] for e in disabled] == ["lore_belief_search"]


async def test_resume_is_true_and_replays_the_conversation_exactly(tmp_path):
    """Not a reconstruction from prose: the saved file IS the messages
    array the API takes, so a resumed session replays tool calls and tool
    results too."""
    transport = StubTransport(deepseek_tool_script(), prose_script(text=("first",)))
    first = engine(tmp_path, transport=transport)
    await first.start()
    await run_turn(first, "remember this")
    await first.finalize()

    resumed = engine(
        tmp_path, transport=StubTransport(prose_script(text=("second",))),
        session_id="fresh-id", resume=first.session_id,
    )
    assert [m["role"] for m in resumed.messages] == ["user", "assistant", "tool", "assistant"]
    assert resumed.messages[0]["content"] == "remember this"
    assert VENDOR_CAPABILITIES.resume is True


async def test_a_resume_with_no_saved_conversation_starts_empty(tmp_path):
    """An honest empty history, not a failure: the session id still names
    the transcript, the registry row and the /search result."""
    assert engine(tmp_path, resume="never-existed").messages == []


def test_peer_messaging_is_true_and_has_no_model_in_it():
    assert VENDOR_CAPABILITIES.peer_messaging is True


# -- the False fields, each demonstrated as a surface that says so ------


async def test_context_window_is_false_and_nothing_invents_a_percentage(tmp_path):
    """The window SIZE is unreported by both vendors, and prompt_tokens is
    a resident count, not a percentage. The surfaces say so rather than
    dividing by a number DOXA made up -- the substituted 200000
    doxa.ui.labels.ctx_absolute_text already refused once."""
    eng = engine(tmp_path, transport=StubTransport(prose_script()))
    await eng.start()
    events = await run_turn(eng)
    assert await eng.context_usage() is None
    assert eng.last_ctx_percentage is None
    assert eng.last_ctx_tokens is None
    done = of_type(events, "turn_done")[0].data
    assert done["ctx_percentage"] is None
    assert done["ctx_tokens"] is None
    assert done["ctx_max_tokens"] is None
    # ...and the token counts DID arrive, so this is a refusal to guess
    # rather than an absence of data.
    assert eng.usage_totals["input_tokens"] == 33
    assert VENDOR_CAPABILITIES.context_window is False


async def test_cost_is_false_and_nothing_paints_a_zero_dollar_figure(tmp_path):
    """No response field from either vendor carries dollars. 0.0 would
    read as "this session is free", which is a different claim from
    "nobody said" -- so the chip is omitted and the turn reports None."""
    eng = engine(tmp_path, transport=StubTransport(prose_script()))
    await eng.start()
    events = await run_turn(eng)
    assert eng.total_cost_usd == 0.0          # the attribute the chip reads
    assert eng.usage_summary()["total_cost_usd"] is None
    assert of_type(events, "turn_done")[0].data["cost_usd"] is None
    assert VENDOR_CAPABILITIES.cost is False


async def test_permission_modes_is_false_and_the_setter_refuses_by_name(tmp_path):
    eng = engine(tmp_path)
    with pytest.raises(NotImplementedError, match=r"no permission modes"):
        await eng.set_permission_mode("acceptEdits")
    assert VENDOR_CAPABILITIES.permission_modes is False


async def test_the_handle_is_not_detachable_but_the_engine_is(tmp_path):
    """Two different claims, and after issue #39 they differ.

    The MAP says the daemon can host this engine -- it takes an --engine
    now. The HANDLE's attribute is the attach chip's predicate and is about
    this object: a ChatApiEngine the TUI holds directly is one running
    in-process, and there is nothing to detach from it. (When a daemon
    hosts it, the TUI holds an EngineClient instead, which carries
    detachable = True.)"""
    assert engine(tmp_path).detachable is False
    assert VENDOR_CAPABILITIES.detachable is True


def test_lore_pickers_is_false_and_the_methods_are_genuinely_absent(tmp_path):
    """Reported rather than papered over with empty lists: the pickers are
    lore_core queries that happen to live on SessionEngine, and every call
    site already reaches them through getattr."""
    eng = engine(tmp_path)
    for name in ("list_beliefs", "list_pending", "approve_pending", "retract_belief"):
        assert not hasattr(eng, name)
    assert VENDOR_CAPABILITIES.lore_pickers is False


def test_plugins_and_hooks_are_false_because_neither_surface_exists():
    """`--plugin-dir` is a CLI flag and the three hook events are Claude
    Code's dispatcher. A JSON request body has neither."""
    assert VENDOR_CAPABILITIES.plugins is False
    assert VENDOR_CAPABILITIES.hooks is False


async def test_the_lore_snapshot_still_reaches_every_turn_without_hooks(tmp_path):
    """What hooks=False costs, and what it does NOT. The UserPromptSubmit
    surface is absent, so the field is False -- but the thing DOXA used it
    for is done by rebuilding the system message every turn, which is
    strictly fresher than the throttled refresh the hook performs."""
    transport = StubTransport(prose_script(), prose_script())
    eng = engine(tmp_path, transport=transport)
    await eng.start()
    await run_turn(eng, "one")
    await run_turn(eng, "two")
    for request in transport.requests:
        assert request["body"]["messages"][0]["role"] == "system"
    assert eng.lore_snapshot_chars is not None


async def test_needs_input_is_answered_false_rather_than_raising(tmp_path):
    """Nothing in this protocol ever asks, so nothing is ever answered --
    but a stale dialog from another engine's session must not explode."""
    assert await engine(tmp_path).answer_needs_input("req-1", {"decision": "allow"}) is False


# -- the request body --------------------------------------------------


@pytest.mark.parametrize("spec", [DEEPSEEK, GLM], ids=["deepseek", "glm"])
def test_max_tokens_is_never_in_a_request_body(spec):
    """MEASURED: a reasoning model spends a token cap on its hidden
    reasoning first. `max_tokens: 24` returned content "" with
    finish_reason "length" and all 24 tokens counted as reasoning, on BOTH
    vendors. There is no parameter to set it through, deliberately."""
    body = request_body(spec, [{"role": "user", "content": "x"}], "m", "low")
    assert "max_tokens" not in body
    assert "max_completion_tokens" not in body


def test_deepseek_nests_the_effort_and_glm_puts_it_at_the_root():
    """The mirror-image shapes, each vendor sent its own. Measured
    surprise: each also ACCEPTS the other's placement, but whether GLM
    HONOURS a nested value is unobservable from the response, so neither
    is sent the other's."""
    ds = request_body(DEEPSEEK, [], "deepseek-flash", "high")
    assert ds["thinking"] == {"type": "enabled", "reasoning_effort": "high"}
    assert "reasoning_effort" not in ds

    glm = request_body(GLM, [], "glm-5.3-flash", "high")
    assert glm["thinking"] == {"type": "enabled"}
    assert glm["reasoning_effort"] == "high"


def test_glm_is_never_sent_a_disabled_thinking_block():
    """MEASURED: GLM answers {"type": "disabled"} AND
    reasoning_effort "none" with HTTP 400 code 1210 ("This model always
    engages in thinking and cannot be disabled"). So "none" is not in its
    allow-list at all, and even if it were forced through, the body stays
    enabled."""
    assert "none" not in GLM.efforts
    assert "none" in DEEPSEEK.efforts
    assert request_body(GLM, [], "glm-5.3-flash", "none")["thinking"] == {"type": "enabled"}
    assert request_body(DEEPSEEK, [], "deepseek-flash", "none")["thinking"] == {"type": "disabled"}


def test_an_unrecognised_effort_falls_back_instead_of_reaching_the_api(tmp_path):
    """An allow-list, not a passthrough: an unknown effort reaching the
    API is a 400 in the middle of a turn."""
    assert engine(tmp_path, spec=GLM, effort="none").effort == vendors_mod.DEFAULT_EFFORT
    assert engine(tmp_path, spec=GLM, effort="max").effort == "max"
    assert engine(tmp_path, spec=DEEPSEEK, effort="none").effort == "none"
    assert engine(tmp_path, spec=DEEPSEEK, effort="nonsense").effort == vendors_mod.DEFAULT_EFFORT


def test_both_vendors_are_sampled_at_the_same_temperature():
    """A capability-parity experiment must not have one arm sampled
    differently from the other."""
    ds = request_body(DEEPSEEK, [], "deepseek-flash", "low")
    glm = request_body(GLM, [], "glm-5.3-flash", "low")
    assert ds["temperature"] == glm["temperature"] == vendors_mod.TEMPERATURE


# -- failure paths -----------------------------------------------------


async def test_a_turn_that_never_stops_calling_tools_is_stopped_and_says_so(tmp_path):
    """A model that calls a tool, reads the result and calls it again is
    working; one that does that forever is a turn that never ends."""
    transport = StubTransport(*[deepseek_tool_script() for _ in range(40)])
    eng = engine(tmp_path, transport=transport)
    await eng.start()
    events = await run_turn(eng)
    done = of_type(events, "turn_done")[0].data
    assert done["is_error"] is True
    assert "tool round trips" in done["error"]
    assert len(transport.requests) == vendors_mod.MAX_TOOL_STEPS


async def test_a_vendor_failure_ends_the_turn_readably_and_marked(tmp_path):
    """Both surfaces, together: is_error alone paints an error beside a
    turn with no text in it, which sends an operator looking in the wrong
    place."""
    transport = StubTransport(VendorApiError(429, '{"error":{"code":"1302"}}', "1302"))
    eng = engine(tmp_path, spec=GLM, transport=transport)
    await eng.start()
    events = await run_turn(eng)
    assert "429" in of_type(events, "text_delta")[-1].data["text"]
    assert of_type(events, "turn_done")[0].data["is_error"] is True


async def test_a_terminal_vendor_code_says_retrying_will_not_help(tmp_path):
    """1113 ("insufficient balance or no resource package") arrives as the
    same HTTP 429 a transient rate limit does, and only error.code tells
    them apart."""
    transport = StubTransport(VendorApiError(429, '{"error":{"code":"1113"}}', "1113"))
    eng = engine(tmp_path, spec=GLM, transport=transport)
    await eng.start()
    events = await run_turn(eng)
    assert "retrying will not help" in of_type(events, "turn_done")[0].data["error"]


async def test_the_turn_counter_agrees_between_a_failing_and_a_passing_turn(tmp_path):
    transport = StubTransport(prose_script(), VendorApiError(500, "boom"))
    eng = engine(tmp_path, transport=transport)
    await eng.start()
    first = await run_turn(eng)
    second = await run_turn(eng)
    assert of_type(first, "turn_done")[0].data["num_turns"] == 1
    assert of_type(second, "turn_done")[0].data["num_turns"] == 2


async def test_an_unparseable_stream_line_is_dropped_not_fatal(tmp_path):
    class Garbled(StubTransport):
        async def stream(self, url, body, headers, timeout):
            self.requests.append({"url": url, "body": body, "headers": headers,
                                  "timeout": timeout})
            yield "not json at all"
            for chunk in prose_script(text=("ok",)):
                yield json.dumps(chunk)

    eng = engine(tmp_path, transport=Garbled())
    await eng.start()
    events = await run_turn(eng)
    assert [e.data["text"] for e in of_type(events, "text_delta")] == ["ok"]
    assert of_type(events, "turn_done")[0].data["is_error"] is False


# -- the live smoke test, skipped without keys -------------------------


def _live_key(name: str) -> "str | None":
    return (os.environ.get(name) or "").strip() or None


@pytest.mark.parametrize(
    "spec", [DEEPSEEK, GLM], ids=["deepseek", "glm"],
)
async def test_live_smoke(tmp_path, spec):
    """The contract half: the same shapes the stub asserts, run against
    the real API.

    SKIPPED, NOT FAILED, WITHOUT A KEY -- the whole suite must pass with
    no network and no credentials, and a suite that fails on a missing
    optional service trains people to ignore red. Enable with:

        DEEPSEEK_API_KEY=... ZAI_API_KEY=... uv run pytest -q \\
            tests/test_vendors.py -k live_smoke

    The stub is written from the measured shapes by the same hand as the
    engine; two readings of one measurement that agree prove the reading
    is self-consistent, not that the API still behaves that way. This is
    the only place a vendor changing its stream can show up as red."""
    if not _live_key(spec.env_var):
        pytest.skip(f"no {spec.env_var} in the environment")

    eng = ChatApiEngine(cwd=str(tmp_path), spec=spec, model=spec.default_model)
    await eng.start()
    try:
        events = await run_turn(
            eng, "What is 17 times 23? Answer with the number and the word OK."
        )
    finally:
        await eng.finalize()

    done = of_type(events, "turn_done")[0].data
    assert done["is_error"] is False, done.get("error")
    assert "OK" in "".join(e.data["text"] for e in of_type(events, "text_delta"))
    # Every capability this engine declares True, observed live.
    assert eng.resolved_model, "resolved_model=True but no model was reported"
    assert eng.usage_totals.get("input_tokens"), "token_usage=True but no usage arrived"

    # reasoning=True, asserted against WHAT THE VENDOR ACTUALLY DID rather
    # than against what it says it does -- and that distinction is a live
    # finding, not caution. GLM refuses to have thinking turned off at all
    # (HTTP 400 code 1210: "This model always engages in thinking and
    # cannot be disabled"), and yet it measurably returns
    # `reasoning_tokens: 0` with no reasoning_content on some turns --
    # observed on a trivial prompt and on a tool-calling turn, on the same
    # model, minutes apart. Asserting "reasoning always arrives" would
    # therefore be a FLAKY test pinned to a vendor claim that is not true.
    # What IS invariant, and what the capability actually promises, is that
    # reasoning the vendor BILLED reaches the transcript.
    billed = eng.usage_totals.get("reasoning_output_tokens", 0)
    if billed:
        assert of_type(events, "reasoning_delta"), (
            f"{spec.engine_id} billed {billed} reasoning tokens and none of "
            "them reached the transcript -- reasoning=True is not honoured"
        )

    # ...and every one it declares False, still absent.
    assert await eng.context_usage() is None
    assert eng.total_cost_usd == 0.0


async def test_an_answer_truncated_before_any_text_says_so(tmp_path):
    """The measured trap, guarded from the other side. request_body never
    sends max_tokens -- but a vendor-side default can still truncate, and
    on a reasoning model the whole budget goes to hidden reasoning and
    `content` comes back EMPTY. Rendering that as "the model said nothing"
    is the reading that sends an operator looking in the wrong place."""
    script = [
        _chunk("deepseek-flash", {"reasoning_content": "thinking hard"}),
        _chunk("deepseek-flash", {"content": ""}, finish="length", usage=USAGE),
    ]
    eng = engine(tmp_path, transport=StubTransport(script))
    await eng.start()
    events = await run_turn(eng)
    done = of_type(events, "turn_done")[0].data
    assert done["is_error"] is True
    assert "cut off" in done["error"]


async def test_a_failed_turn_leaves_its_prompt_in_the_conversation(tmp_path):
    """A turn that failed still happened. The prompt stays -- dropping it
    would make the next turn's context differ from the transcript, and
    inventing an assistant reply to restore alternation would put words in
    the model's mouth. MEASURED that both vendors answer two consecutive
    user messages with HTTP 200; some OpenAI-compatible servers refuse
    it, which is why the next turn is driven here rather than assumed."""
    transport = StubTransport(VendorApiError(500, "boom"), prose_script(text=("ok",)))
    eng = engine(tmp_path, transport=transport)
    await eng.start()
    await run_turn(eng, "the one that failed")
    second = await run_turn(eng, "the one after it")

    assert of_type(second, "turn_done")[0].data["is_error"] is False
    replayed = [m["role"] for m in transport.requests[-1]["body"]["messages"]]
    assert replayed == ["system", "user", "user"]


def test_one_engine_that_cannot_import_leaves_the_others_registered(monkeypatch):
    """Each lazy provider is imported in its OWN try. A broken engine is
    absent -- which is the honest outcome, and what get() then says -- but
    it must not take the rest of the non-Claude half of the registry down
    with it."""
    import importlib

    real = importlib.import_module

    def broken(name, package=None):
        if name == ".codex":
            raise ImportError("pretend the codex module is broken")
        return real(name, package)

    monkeypatch.setattr(engines_mod, "_REGISTRY", {})
    monkeypatch.setattr(engines_mod, "_builtins_registered", False)
    monkeypatch.setattr(importlib, "import_module", broken)
    engines_mod.register(engines_mod.ClaudeEngineProvider())

    assert engines_mod.available() == ("claude", "deepseek", "glm")
    with pytest.raises(KeyError, match=r"unknown engine 'codex'"):
        engines_mod.get("codex")


# -- the production transport, without a socket ------------------------


class _FakeResponse:
    """What urllib hands back: an iterable of raw lines, and a close()."""

    def __init__(self, lines) -> None:
        self._lines = list(lines)
        self.closed = False

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        self.closed = True


async def test_the_http_transport_parses_sse_and_stops_at_done():
    """The real transport, driven through an injected opener rather than a
    socket. Covers the three things only it does: the ``data:`` prefix, the
    ``[DONE]`` sentinel, and the SSE comment lines a keep-alive sends."""
    from doxa.vendors import HttpStreamTransport

    seen = {}

    def opener(request, timeout=None):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data.decode())
        seen["timeout"] = timeout
        return _FakeResponse([
            b": keep-alive\n",
            b'data: {"a": 1}\n',
            b"\n",
            b'data: {"b": 2}\n',
            b"data: [DONE]\n",
            b'data: {"never": "read"}\n',
        ])

    transport = HttpStreamTransport(opener=opener)
    payloads = [
        p async for p in transport.stream(
            "https://example.invalid/chat", {"model": "m"}, {"Authorization": "Bearer x"}, 12.0
        )
    ]
    assert payloads == ['{"a": 1}', '{"b": 2}']
    assert seen["url"] == "https://example.invalid/chat"
    assert seen["body"] == {"model": "m"}
    assert seen["timeout"] == 12.0


async def test_the_http_transport_turns_an_http_error_into_a_vendor_error():
    """MEASURED shapes: DeepSeek's 401 body and Z.ai's coded 429. The
    error CODE is read from the body, never from the status line -- 1302
    (transient) and 1113 (permanent) arrive as the same HTTP 429 and are
    otherwise indistinguishable."""
    import urllib.error
    import io

    from doxa.vendors import HttpStreamTransport

    body = b'{"error":{"code":"1113","message":"Insufficient balance"}}'

    def opener(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 429, "Too Many Requests", {}, io.BytesIO(body)
        )

    transport = HttpStreamTransport(opener=opener)
    with pytest.raises(VendorApiError) as excinfo:
        async for _ in transport.stream("https://example.invalid/chat", {}, {}, 1.0):
            pass
    assert excinfo.value.status == 429
    assert excinfo.value.code == "1113"
    assert "Insufficient balance" in excinfo.value.detail


async def test_the_http_transport_reports_a_connection_failure_as_status_zero():
    """DNS, TLS, timeout, reset: every one of them is "the request did not
    happen", and the turn reports it rather than raising out of a
    generator the pane is iterating."""
    from doxa.vendors import HttpStreamTransport

    def opener(request, timeout=None):
        raise OSError("name or service not known")

    with pytest.raises(VendorApiError) as excinfo:
        async for _ in HttpStreamTransport(opener=opener).stream(
            "https://example.invalid/chat", {}, {}, 1.0
        ):
            pass
    assert excinfo.value.status == 0
    assert "OSError" in excinfo.value.detail


# -- the peer layer, which is what the experiment actually rides on -----


async def test_a_peer_message_reaches_the_next_turn_and_is_marked_untrusted(tmp_path):
    """``peer_messaging=True`` is the field the mixed-vendor experiment
    depends on most: an agent that cannot hear its peers cannot
    self-organise with them. A frame delivered out-of-band rides the NEXT
    prompt, wrapped in doxa.peers' own untrusted-peer marker -- the same
    rendering a Claude session gets, so the two arms read identical text."""
    from doxa import peers as peers_mod

    transport = StubTransport(prose_script())
    eng = engine(tmp_path, transport=transport)
    await eng.start()
    eng._on_peer_frame({
        "from_title": "peer-two", "from_id": "abcdef0123",
        "sent_at": "2026-09-17T00:00:00Z", "body": "take the parser, I have the lexer",
    })

    # Out-of-band first: the rail learns immediately, without waiting for
    # a turn generator's yield point.
    assert eng._peer_queue.get_nowait().type == "peer_message"

    events = await run_turn(eng, "what now?")
    sent = transport.last_body["messages"][-1]["content"]
    assert peers_mod.PEER_UNTRUSTED_INTRO in sent
    assert "take the parser, I have the lexer" in sent
    assert sent.endswith("what now?")
    assert of_type(events, "turn_started")[0].data["peer_context"] is True
    # ...and it is consumed, not replayed onto every later turn.
    assert eng._pending_peer_frames == []


async def test_send_peer_message_refuses_clearly_when_the_layer_is_down(tmp_path):
    from doxa import peers as peers_mod

    eng = engine(tmp_path)
    with pytest.raises(peers_mod.PeerSendError, match=r"peer layer is not running"):
        await eng.send_peer_message("abc", "hello")
    assert eng.list_peers() == []
    assert eng.peer_count() == 0


def test_neither_the_module_nor_the_registry_pulls_the_sdk():
    """doxa.engines' own rule, held: a module whose job is to run a session
    on something OTHER than Claude must not force claude_agent_sdk's
    404 ms to load. The operator projection imports it at the point of
    use, inside start(), and nowhere else -- so listing the engines (which
    `doxa --engine ...` does on every launch) stays free.

    A subprocess, because sys.modules in this one is already populated by
    the rest of the suite."""
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-c",
         "import sys, doxa.vendors, doxa.engines;"
         "doxa.engines.available();"
         "print('YES' if 'claude_agent_sdk' in sys.modules else 'NO')"],
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == "NO", out.stdout + out.stderr


async def test_a_usage_only_chunk_with_no_choices_is_absorbed_not_misread(tmp_path):
    """Measured, both vendors put usage on a chunk that ALSO carries a
    choice -- but an OpenAI-compatible server is allowed to send a
    usage-only chunk with ``choices: []``, and one that does must not be
    read as an empty delta. Tolerated here so a vendor changing that
    detail costs nothing."""
    script = [
        _chunk("deepseek-flash", {"content": "hi"}),
        {"object": "chat.completion.chunk", "model": "deepseek-flash",
         "choices": [], "usage": USAGE},
    ]
    eng = engine(tmp_path, transport=StubTransport(script))
    await eng.start()
    events = await run_turn(eng)
    assert [e.data["text"] for e in of_type(events, "text_delta")] == ["hi"]
    assert eng.usage_totals["input_tokens"] == 33
    assert of_type(events, "turn_done")[0].data["is_error"] is False


@pytest.mark.parametrize(
    "engine_id,spec", [("deepseek", DEEPSEEK), ("glm", GLM)], ids=["deepseek", "glm"]
)
def test_new_session_takes_exactly_the_kwargs_the_cli_passes(tmp_path, engine_id, spec):
    """The reachability guarantee for ``doxa --engine <id>``: the four
    factories doxa.cli builds for a non-default engine are these exact
    calls, and a provider ignores what its engine has no use for rather
    than making every caller branch on which engine it is talking to."""
    provider = engines_mod.get(engine_id)

    fresh = provider.new_session(cwd=str(tmp_path), model=None)
    assert isinstance(fresh, ChatApiEngine)
    assert fresh.spec is spec
    assert fresh.model == spec.default_model    # None means the vendor's own

    resumed = provider.new_session(
        cwd=str(tmp_path), model=None, session_id="s-1", resume="s-1",
    )
    assert resumed.session_id == "s-1"
    assert resumed.resume == "s-1"

    # Vocabulary this engine has no use for is ignored, not refused.
    assert provider.new_session(
        cwd=str(tmp_path), model=None, daemon_socket="/nope.sock",
        allowed_tools=["Bash"], client_factory=object(),
    ).spec is spec


def test_a_provider_cannot_be_talked_into_the_other_vendors_spec(tmp_path):
    """The one mix-up a randomised fleet would never notice: a session
    running GLM while the registry, the peer rail and the ledger all call
    it DeepSeek."""
    session = engines_mod.get("deepseek").new_session(cwd=str(tmp_path), spec=GLM)
    assert session.spec is DEEPSEEK


def test_the_effort_knob_is_the_one_doxa_already_has(tmp_path, monkeypatch):
    """One command drives both arms of a mixed fleet. `/effort` writes
    DOXA_EFFORT, and its choice list is Claude's -- "medium" and "xhigh"
    have no vendor equivalent and fall back VISIBLY, since self.effort is
    what the effort chip reads."""
    monkeypatch.setenv("DOXA_EFFORT", "max")
    assert engine(tmp_path).effort == "max"

    monkeypatch.setenv("DOXA_EFFORT", "medium")          # Claude-only
    assert engine(tmp_path).effort == vendors_mod.DEFAULT_EFFORT

    # The vendor-only override wins, for pinning one arm of an experiment
    # without moving the other.
    monkeypatch.setenv("DOXA_EFFORT", "medium")
    monkeypatch.setenv("DOXA_VENDOR_EFFORT", "high")
    assert engine(tmp_path).effort == "high"


async def test_the_configured_effort_reaches_the_request_body(tmp_path, monkeypatch):
    monkeypatch.setenv("DOXA_EFFORT", "high")
    transport = StubTransport(prose_script())
    eng = engine(tmp_path, transport=transport)
    await eng.start()
    await run_turn(eng)
    assert transport.last_body["thinking"]["reasoning_effort"] == "high"


# -- the scrubber, which is the last thing between a key and a log -----


def test_the_scrubber_removes_the_key_by_value_and_by_measured_echo():
    """Two layers, and the second one is measured rather than defensive.
    DeepSeek's real 401 body quotes a MASKED tail of the key it rejected
    ("your api key: ****nope is invalid"), which no pattern-based redactor
    can be expected to recognise as key material -- so the tail is removed
    where the key is known, by value."""
    from doxa.vendors import _scrub

    key = "sk-1234567890abcdefnope"
    assert key not in _scrub(f"Authentication Fails, Your api key: {key} is invalid", key)
    # The measured masked form, which contains no substring of the key
    # long enough for a pattern to catch.
    masked = _scrub("Authentication Fails, Your api key: ****nope is invalid", key)
    assert "****nope" not in masked
    assert "****" in masked              # the shape survives; the tail does not
    # A four-character sequence that is NOT the masked echo is ordinary
    # prose and stays: over-scrubbing an error message is its own way of
    # hiding what went wrong.
    assert "nope" in _scrub("the branch named nope does not exist", key)


def test_the_scrubber_survives_a_missing_key_and_empty_text():
    from doxa.vendors import _scrub

    assert _scrub("", None) == ""
    assert _scrub("plain text", None) == "plain text"


# -- the things a DOXA session owes regardless of engine ---------------


async def test_the_turn_is_written_to_a_lore_shaped_transcript(tmp_path):
    """/search and the session index see a vendor session like any other,
    because the transcript is LORE's shape and not a second one."""
    eng = engine(tmp_path, transport=StubTransport(prose_script(text=("hello",))))
    await eng.start()
    await run_turn(eng, "say hello")

    lines = [
        json.loads(line)
        for line in eng.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [row["type"] for row in lines] == ["user", "assistant"]
    assert lines[0]["message"]["content"] == "say hello"
    assert lines[0]["sessionId"] == eng.session_id
    assert lines[1]["message"]["content"][0]["text"] == "hello"


async def test_finalize_is_idempotent_and_reports_what_it_indexed(tmp_path):
    eng = engine(tmp_path, transport=StubTransport(prose_script()))
    await eng.start()
    await run_turn(eng)

    first = await eng.finalize()
    assert first.type == "session_done"
    assert isinstance(first.data["indexed"], int)
    assert isinstance(first.data["belief_count"], int)
    assert "review" in first.data          # the declared gap, stated

    second = await eng.finalize()
    assert second.data == {"already_finalized": True}


async def test_a_turn_that_outruns_its_wall_clock_budget_is_stopped(tmp_path, monkeypatch):
    """The other half of the "a turn must end" guarantee. MAX_TOOL_STEPS
    bounds the round trips; this bounds the clock, so a single request
    that never returns cannot hold the pane's exclusive worker forever."""
    monkeypatch.setattr(vendors_mod, "TURN_TIMEOUT_SECS", -1.0)
    eng = engine(tmp_path, transport=StubTransport(prose_script()))
    await eng.start()
    events = await run_turn(eng)
    done = of_type(events, "turn_done")[0].data
    assert done["is_error"] is True
    assert "limit" in done["error"]


async def test_switch_branch_refuses_because_this_engine_owns_no_worktree(tmp_path):
    with pytest.raises(NotImplementedError, match=r"does not manage its own worktree"):
        await engine(tmp_path).switch_branch("main")


# -- the memory switch (docwilde/doxa#39 follow-up) ------------------------


@pytest.mark.parametrize("lore", [True, False])
async def test_the_memory_switch_removes_the_snapshot_and_the_lore_tools(tmp_path, lore):
    """`lore=False` is three absences, not a refusal: no snapshot in the
    system message, no lore_* operator in the projection, and a gate whose
    allowed-set names only what was offered. Through v1.12.0 the flag
    reached the constructor through **_ignored and changed nothing, so a
    fleet's memory-off arm on this engine ran with memory on."""
    eng = engine(tmp_path, lore=lore)
    await eng.start()
    try:
        offered = {t["function"]["name"] for t in eng._tools}
        eng._system_message()
        lore_tools = {name for name in offered if name.startswith("lore_")}
        assert eng.lore is lore
        if lore:
            assert lore_tools, "memory on must offer the lore_* operators"
            assert eng.lore_root is not None
        else:
            assert not lore_tools, f"memory off still offered {sorted(lore_tools)}"
            assert eng.lore_snapshot_chars == 0
            assert eng.lore_root is None
        assert {"peer_list", "peer_history"} <= offered
        assert eng._gate is not None and eng._gate.allowed == offered
    finally:
        await eng.finalize()


async def test_a_lore_call_the_model_invents_is_refused_when_memory_is_off(tmp_path):
    """A name the model produces from training rather than from the tool
    list must not run against the store."""
    eng = engine(tmp_path, lore=False)
    await eng.start()
    try:
        result = eng._gate.execute("lore_memory_list", {"scope": "all"})
        assert "error" in result, result
        assert "entries" not in json.dumps(result)
    finally:
        await eng.finalize()
