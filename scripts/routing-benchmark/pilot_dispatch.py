"""Prepare and admit one frozen pilot call before sending the exact wire bytes."""

import http.client
import os
import subprocess
import time

import benchmark as b
from pilot_ledger import FIXED_ARMS, JEV_ARM, micros, policy

MAX_ADAPTER_PAYLOAD = 256 * 1024
MAX_DECISION_RESPONSE = 64 * 1024
ADAPTER_ENVIRONMENT = {"PATH": "/usr/bin:/bin"}

def wire(adapter, state, **input_fields):
    payload = b.encode(input_fields)
    b.require(len(payload) <= MAX_ADAPTER_PAYLOAD, "pilot request exceeds the adapter bound")
    result = subprocess.run(
        [str(adapter)], input=payload, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env={**ADAPTER_ENVIRONMENT, "DOXA_HOME": str(state)}, timeout=10,
    )
    b.require(result.returncode == 0 and len(result.stdout) <= MAX_ADAPTER_PAYLOAD,
              "pilot wire adapter refused")
    return b.parse(result.stdout)


def bound_wire(prepared, framing=0):
    wire_bytes = prepared["wire"].encode("utf-8")
    receipt = prepared["receipt"]
    b.require(receipt["request_sha256"] == b.digest(wire_bytes)
              and receipt["input_bound"] == len(wire_bytes) + framing,
              "forwarding receipt does not bind the exact wire request")
    return wire_bytes, receipt


def complete_usage(status, body):
    if status != 200 or len(body) > MAX_ADAPTER_PAYLOAD:
        return None
    try:
        response = b.parse(body)
        choices = response["choices"]
        usage = response["usage"]
        model = response["model"]
        prompt, completion, total = (usage[key] for key in
                                     ("prompt_tokens", "completion_tokens", "total_tokens"))
        if (response["object"] == "chat.completion" and isinstance(model, str) and model
                and isinstance(choices, list) and len(choices) == 1
                and choices[0]["finish_reason"] in {"stop", "length", "tool_calls", "content_filter"}
                and type(prompt) is int and type(completion) is int and type(total) is int
                and prompt > 0 and completion >= 0 and total == prompt + completion):
            return model, prompt, completion
    except (KeyError, IndexError, TypeError, ValueError, RecursionError):
        pass
    return None


def proposal_body(response):
    """Extract the complete assistant proposal independently from usage settlement."""
    try:
        choice, = b.parse(response)["choices"]
        message = choice["message"]
        if (choice["finish_reason"] == "stop" and message["role"] == "assistant"
                and not message.get("tool_calls") and not message.get("refusal")
                and isinstance(message.get("content"), str)):
            return message["content"].encode("utf-8")
    except (KeyError, TypeError, ValueError, RecursionError):
        pass
    return None


def run_call(adapter, state, ledger, scope, request, decision_transport, repair_transport):
    task, arm, slot = scope
    plan, _, _ = policy()
    if arm == JEV_ARM:
        config = b.router_config("flash-direct")
        config["candidates"] = [row for row in config["candidates"]
                                if (row["model"], row["effort"]) in FIXED_ARMS.values()]
        common = {"config": config, "request": request}
        decision_prepared = wire(adapter, state, operation="prepare", **common)
        decision_bytes, receipt = bound_wire(decision_prepared, plan["decision_input_framing_tokens"])
        decision = ledger.reserve_decision(
            task, slot, model=receipt["model"], input_bound=receipt["input_bound"],
            input_rate=micros(plan["decision_input_usd_per_million"]),
            request_sha256=receipt["request_sha256"],
        )
        b.require(decision.held_usd_micros == receipt["held_usd_micros"], "Jev hold differs from the wire bound")
        started = time.monotonic_ns()
        try:
            status, response = decision_transport(decision_bytes)
            if status != 200 or len(response) > MAX_DECISION_RESPONSE:
                return "ambiguous_decision", None
            selected = wire(adapter, state, operation="finish", **common,
                            decision_sha256=receipt["request_sha256"], response=response.decode("utf-8"))
        except (OSError, ValueError, subprocess.SubprocessError):
            return "ambiguous_decision", None
        observed = selected["decision"]
        ledger.settle_usage(
            decision.id, request_sha256=observed["request_sha256"], model=observed["model"],
            input_tokens=observed["usage"]["input_tokens"], output_tokens=observed["usage"]["output_tokens"],
            latency_ms=(time.monotonic_ns() - started) // 1_000_000,
        )
        prepared = selected["repair"]
        if prepared is None:
            return "unselected_decision", None
    else:
        prepared = wire(adapter, state, operation="fixed", arm=arm, request=request)
    repair_bytes, receipt = bound_wire(prepared)
    candidate = next(row for row in plan["candidates"]
                     if (row["model"], row["effort"]) == (receipt["model"], receipt["effort"]))
    repair = ledger.reserve_repair(
        task, arm, slot, model=receipt["model"], effort=receipt["effort"],
        input_bound=receipt["input_bound"], output_limit=receipt["output_limit"],
        input_rate=micros(candidate["input_usd_per_million"]),
        output_rate=micros(candidate["output_usd_per_million"]),
        request_sha256=receipt["request_sha256"],
    )
    started = time.monotonic_ns()
    try:
        status, response = repair_transport(repair_bytes)
    except OSError:
        return "ambiguous_repair", None
    usage = complete_usage(status, response)
    if usage is None:
        return "ambiguous_repair", response
    model, prompt, completion = usage
    proposal = proposal_body(response)
    ledger.settle_usage(
        repair.id, request_sha256=receipt["request_sha256"], model=model,
        input_tokens=prompt, output_tokens=completion,
        latency_ms=(time.monotonic_ns() - started) // 1_000_000,
        repair_receipt=(b.digest(response), b.digest(proposal)) if proposal else None,
    )
    return "settled", response


def valid_credential(key):
    return 0 < len(key) <= 4096 and key.isascii() and all(
        33 <= ord(character) <= 126 for character in key
    )


def post_exact(connection, path, body, *, key, response_limit):
    """Send one HTTP request without reserializing or redirecting it."""
    if type(body) is not bytes or not 0 < len(body) <= MAX_ADAPTER_PAYLOAD:
        raise ValueError("pilot provider request size is invalid")
    if not valid_credential(key):
        raise ValueError("pilot provider credential is unavailable or malformed")
    try:
        connection.request("POST", path, body=body, headers={
            "Authorization": "Bearer " + key,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
        })
        with connection.getresponse() as response:
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise OSError("pilot provider returned an unsupported response encoding")
            declared = response.length
            if declared is not None and declared > response_limit:
                raise OSError("pilot provider response exceeds the configured bound")
            reply = response.read(response_limit + 1)
            if len(reply) > response_limit:
                raise OSError("pilot provider response exceeds the configured bound")
            if declared is not None and len(reply) != declared:
                raise OSError("pilot provider response ended before its declared length")
            return response.status, reply
    except http.client.HTTPException:
        raise OSError("pilot provider HTTP response was incomplete or invalid") from None
    finally:
        connection.close()


def _owner_transport(host, path, credential_name, response_limit):
    key = os.environ.get(credential_name, "")
    if not valid_credential(key):
        raise ValueError(f"missing or malformed pilot credential {credential_name}")

    def send(body):
        connection = http.client.HTTPSConnection(host, timeout=30)
        return post_exact(connection, path, body, key=key, response_limit=response_limit)

    return send


def dispatch_call(adapter, state, ledger, scope, request):
    """Dispatch one admitted call using the pinned provider endpoints.

    The pilot coordinator must verify the account, model and price bounds
    before calling this function. Credentials are resolved only in this owner
    process and are not passed to the wire adapter.
    """
    _, arm, _ = scope
    repair = _owner_transport("api.deepseek.com", "/chat/completions",
                              "DEEPSEEK_API_KEY", MAX_ADAPTER_PAYLOAD)
    decision = None
    if arm == JEV_ARM:
        decision = _owner_transport("api.typesafe.ai", "/v1/systemone",
                                    "TYPESAFE_API_KEY", MAX_DECISION_RESPONSE)
    return run_call(adapter, state, ledger, scope, request, decision, repair)
