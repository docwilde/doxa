#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""How large a fleet does doxa.fleet actually survive, on this machine?

The question the harness cannot answer about itself. tests/test_fleet.py
proves the orchestration is correct with an injected backend and no
processes at all; that is the right shape for a test and it measures
nothing about ceilings. This script measures the ceiling, by running the
REAL :class:`doxa.fleet.FleetRun` against REAL ``doxa.daemon`` processes
over REAL Unix sockets with REAL registry entries -- and a scripted SDK
client in place of the Claude CLI.

WHAT THAT DOES AND DOES NOT MEASURE, stated plainly because the number is
only useful with its caveat:

  * MEASURED here: process spawn concurrency, file-descriptor pressure,
    AF_UNIX path budget, registry churn under N writers, the barrier's
    dispatch spread at N, quiescence polling cost, and whether teardown
    gets all N back. Every one of those is a harness property and every
    one of them is where a harness breaks first.
  * NOT measured here: the ~500-600 MB a live Claude CLI holds. A stub
    session is a few tens of MB, so this script's N is a HARNESS ceiling,
    not a FLEET ceiling. The fleet ceiling is arithmetic
    (:func:`doxa.fleet.capacity_note`) and the arithmetic is the honest
    answer for it -- running 32 real sessions to discover the number
    already on the box would cost real tokens to learn nothing.
  * NOT measured here: model behaviour, coordination, or anything the
    experiment is actually about.

Usage::

    uv run python scripts/fleet_scale.py --n 8
    uv run python scripts/fleet_scale.py --n 32 --n 64        # a sweep

Each N runs, reports, and tears down before the next begins.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import resource
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# A stub session must not run the LORE deriver on finalize. That review
# shells out to a headless `claude -p`, which costs real tokens and takes
# tens of seconds -- measured at N=4: 45 s of wall clock for a run whose
# turns were one second each. A scale probe may not spend money to learn
# how many sockets fit. (A REAL fleet run does run it, for the agents whose
# memory is on; this line is about the probe, not the harness.)
os.environ.setdefault("LORE_DISABLE_REVIEW", "1")

from doxa import fleet as fleet_mod  # noqa: E402


# =====================================================================
# The stub session: a real daemon, a scripted SDK client
# =====================================================================


class _FakeSDKClient:
    """Stands in for ``ClaudeSDKClient``: accepts a query, replays one
    short scripted turn, costs nothing.

    Inline rather than imported from tests/fakes.py because this process
    is spawned with ``python -m`` from an arbitrary cwd and must not
    depend on the test package being importable."""

    def __init__(self, options):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self._prompt = prompt

    async def receive_response(self):
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        # A beat of work, so a run has a quiescence transition to observe
        # rather than going quiet in the same tick it was dispatched.
        await asyncio.sleep(float(os.environ.get("DOXA_STUB_TURN_SECS", "1.0")))
        yield AssistantMessage(content=[TextBlock(text="ack")], model="stub")
        yield ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="stub", total_cost_usd=0.0,
        )

    async def set_permission_mode(self, mode: str) -> None:
        pass

    async def get_context_usage(self) -> dict:
        raise RuntimeError("no context usage in a stub session")

    async def get_server_info(self):
        return None


def _serve_stub(argv: "list[str]") -> int:
    """The stub daemon's own entry point -- what each spawned process runs.

    A REAL :class:`doxa.daemon.SessionDaemon`: real socket, real protocol,
    real ``PeerHost`` writing a real registry entry. Only the SDK client
    underneath the engine is fake, which is the one part that would cost
    money and the one part this measurement does not need."""
    from doxa.daemon import SessionDaemon, install_signal_handlers
    from doxa.engine import SessionEngine

    parser = argparse.ArgumentParser(prog="fleet-stub")
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--linger", type=float, default=30.0)
    parser.add_argument("--model", default=None)
    parser.add_argument("--no-lore", dest="lore", action="store_false", default=None)
    args, _unknown = parser.parse_known_args(argv)

    daemon = SessionDaemon(
        cwd=args.cwd,
        model=args.model,
        session_id=args.session_id,
        linger_secs=args.linger,
        lore=args.lore,
        engine_factory=lambda cwd, sid, dsock: SessionEngine(
            cwd=cwd, model=args.model, session_id=sid, daemon_socket=dsock,
            client_factory=_FakeSDKClient, lore=args.lore,
        ),
    )

    async def _chatter(session_id: str) -> None:
        """One peer message per stub, onto the real socket and the real
        ledger.

        Without this the scale probe never touches the two primitives most
        likely to break at N: the ledger's cross-process ``flock`` (N
        writers, one file) and the registry-plus-socket send path (N
        readers of a directory N writers are rewriting on a heartbeat).
        A scale run that only spawned and stopped would measure the easy
        half."""
        from doxa import peerledger as peerledger_mod
        from doxa import peers as peers_mod

        await asyncio.sleep(0.5)
        ledger = peerledger_mod.PeerLedger()
        for _ in range(3):
            live = [p for p in peers_mod.read_registry() if p.session_id != session_id]
            if live:
                target = live[hash(session_id) % len(live)]
                try:
                    await peers_mod.send_message(
                        target.socket_path,
                        from_id=session_id, from_title="stub",
                        body=f"ack from {session_id[:8]}",
                    )
                    await ledger.append_async(
                        sender=peerledger_mod.Sender(session=session_id, title="stub"),
                        to=[target.session_id],
                        body=f"ack from {session_id[:8]}",
                    )
                except Exception:
                    pass
                return
            await asyncio.sleep(0.5)

    async def _run() -> int:
        install_signal_handlers(daemon)
        if os.environ.get("DOXA_STUB_CHATTER"):
            asyncio.get_running_loop().create_task(_chatter(daemon.session_id))
        await daemon.serve()
        return 0

    return asyncio.run(_run())


# =====================================================================
# The backend that spawns them
# =====================================================================


class StubBackend(fleet_mod.DaemonBackend):
    """:class:`doxa.fleet.DaemonBackend` with one method changed.

    Everything else -- arming a real ``EngineClient``, the dispatch
    barrier, the status-driven quiescence poll, stop/SIGTERM/SIGKILL
    teardown -- is the production code path, unmodified. That is the
    point: a scale run that used a scale-specific orchestration would
    measure the scale harness rather than the harness."""

    async def spawn(self, slot, spec) -> None:
        import contextlib
        import json
        import subprocess
        import uuid

        from doxa.peers import registry_dir, runtime_dir

        env = spec.env_for(slot.assignment)
        session_id = str(uuid.uuid4())
        reg = registry_dir(env)
        log = runtime_dir(env) / f"stub-{session_id[:8]}.log"
        cmd = [
            sys.executable, str(Path(__file__).resolve()), "--serve-stub",
            "--cwd", spec.cwd, "--session-id", session_id, "--linger", "30",
        ]
        if slot.assignment.model:
            cmd += ["--model", slot.assignment.model]
        if not slot.assignment.lore:
            cmd += ["--no-lore"]
        with open(log, "ab") as handle:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=handle, stderr=handle,
                start_new_session=True, cwd=spec.cwd, env=env,
            )
        entry = reg / f"{session_id}.json"
        deadline = time.monotonic() + spec.spawn_timeout_s
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                tail = ""
                with contextlib.suppress(OSError):
                    tail = log.read_text(errors="replace")[-1500:]
                raise RuntimeError(f"stub exited ({proc.returncode}): {tail}")
            if entry.exists():
                with contextlib.suppress(OSError, ValueError, KeyError):
                    data = json.loads(entry.read_text(encoding="utf-8"))
                    sock = data.get("daemon_socket")
                    if sock and Path(sock).exists():
                        slot.session_id = session_id
                        slot.socket_path = str(sock)
                        slot.pid = int(data["pid"])
                        return
            await asyncio.sleep(0.05)
        raise RuntimeError(f"stub {session_id[:8]} never became ready")


# =====================================================================
# The sweep
# =====================================================================


def _fds() -> int:
    try:
        return len(os.listdir(f"/proc/{os.getpid()}/fd"))
    except OSError:
        return -1


async def _one(n: int, root: Path, seconds: float) -> None:
    spec = fleet_mod.FleetSpec(
        prompt="Say ack. This is a harness scale probe, not a task.",
        cwd=str(REPO),
        n=n,
        pool=(fleet_mod.ModelSlot(engine="claude", model="stub"),),
        seed=n,
        memory=fleet_mod.MemoryPolicy(off_count=max(1, n // 4)),
        root=root,
        run_id=f"s{n}",
        quiescence_timeout_s=seconds,
        quiet_dwell_s=2.0,
        poll_interval_s=0.5,
        stop_timeout_s=20.0,
        spawn_concurrency=16,
    )
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    print(f"\n=== N={n}  (RLIMIT_NOFILE soft={soft} hard={hard}, fds now {_fds()}) ===")
    print("   ", fleet_mod.capacity_note(n))
    started = time.monotonic()
    # force=True: the arithmetic is about REAL sessions at ~600 MB, and a
    # stub is a fraction of that. Overriding it is correct here and would
    # be wrong for a real run, which is why it is not the default.
    report = await fleet_mod.run_fleet(spec, StubBackend(), force=True)
    print(f"    {report.summary()}")
    print(f"    wall {time.monotonic() - started:.1f}s, fds after {_fds()}")
    phases: "dict[str, int]" = {}
    for slot in report.slots:
        phases[slot.phase] = phases.get(slot.phase, 0) + 1
    for slot in report.slots:
        if slot.error:
            print(f"    slot {slot.index}: {slot.phase}: {slot.error[:160]}")
            break
    print(f"    phases {phases}")
    if report.leaked_pids:
        print(f"    !! LEAKED {report.leaked_pids}")


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(prog="fleet-scale")
    parser.add_argument("--n", type=int, action="append", default=None)
    parser.add_argument("--root", default="/tmp/dxs")
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument("--quiet", action="store_true",
                        help="skip the peer-message round; measures spawn "
                             "and teardown only")
    args = parser.parse_args(argv)
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    if not args.quiet:
        os.environ["DOXA_STUB_CHATTER"] = "1"
    for n in args.n or [4, 8, 16, 32]:
        asyncio.run(_one(n, root, args.seconds))
    return 0


if __name__ == "__main__":
    if "--serve-stub" in sys.argv:
        raise SystemExit(_serve_stub([a for a in sys.argv[1:] if a != "--serve-stub"]))
    raise SystemExit(main())
