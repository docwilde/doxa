#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Opt-in macOS provider lifecycle receipt; emits only allowlisted metadata.

The Claude probe uses the production native daemon with synthetic text and
DOXA's normal isolated CLI login. It never reads, accepts, or prints a token.
Protected Codex is Linux-only and remains unknown here.
"""

import argparse
import json
import sys

CHECKS = ("launch", "event_delivery", "resume", "stop")
SAFE_REASONS = {
    "missing_native_daemon_or_claude_cli",
    "model_catalog_unavailable_no_paid_turns",
    "first_turn_failed",
    "resumed_model_unknown_no_second_turn",
    "resume_turn_failed",
}


def run_claude():
    # Import only after platform and explicit opt-in gates. The legacy vendor
    # verifier imported by this module snapshots provider key environment.
    from verify_native_claude_live import run
    return run()


def receipt(provider, *, live, platform, claude_runner=run_claude):
    checks = {name: "unknown" for name in CHECKS}
    known_provider = provider if provider in ("claude", "codex") else "unknown"
    result = {"provider": known_provider, "status": "unknown", "checks": checks,
              "submitted_turns": 0}
    if known_provider == "unknown":
        result["reason"] = "unsupported_provider"
        return result
    if not live:
        result["reason"] = "explicit_live_opt_in_required"
        return result
    if platform != "darwin":
        result["reason"] = "macos_host_required"
        return result
    if provider == "codex":
        result["reason"] = "protected_codex_linux_only"
        return result
    try:
        evidence = claude_runner()
    except Exception:
        # Provider and daemon errors can contain account material. The receipt
        # deliberately retains neither exception text nor arbitrary fields.
        result["reason"] = "verifier_exception"
        return result
    if not isinstance(evidence, dict):
        result["reason"] = "malformed_verifier_result"
        return result
    turns = evidence.get("submitted_turns")
    if type(turns) is int and 0 <= turns <= 2:
        result["submitted_turns"] = turns
    if evidence.get("launch_attached") is True:
        checks["launch"] = "pass"
    first, second = evidence.get("first"), evidence.get("second")
    if all(isinstance(turn, dict) and turn.get("ok") is True
           and type(turn.get("text_deltas")) is int and turn["text_deltas"] > 0
           for turn in (first, second)):
        checks["event_delivery"] = "pass"
    if evidence.get("resume_attached") is True and isinstance(second, dict) \
            and second.get("ok") is True:
        checks["resume"] = "pass"
    if evidence.get("first_stop_exited") is True \
            and evidence.get("final_stop_exited") is True:
        checks["stop"] = "pass"
    if evidence.get("result") == "passed" and result["submitted_turns"] == 2 \
            and all(value == "pass" for value in checks.values()):
        result["status"] = "passed"
    else:
        code = evidence.get("result")
        result["reason"] = code if isinstance(code, str) and code in SAFE_REASONS \
            else "incomplete_lifecycle"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="explicitly allow two short Claude subscription turns")
    parser.add_argument("--provider", choices=("claude", "codex"), default="claude")
    args = parser.parse_args(argv)
    result = receipt(args.provider, live=args.live, platform=sys.platform)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
