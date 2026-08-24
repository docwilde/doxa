"""doxa.cli -- session lifecycle entry points.

`uv run doxa` and friends. The daemon split (doxa/daemon.py) makes a DOXA
session a process of its own, so the CLI's job is matchmaking between TUIs
and daemons through the ONE discovery surface that already exists -- the
peer registry, whose entries carry the ``daemon_socket`` marker:

* ``doxa``                -- spawn-or-attach: reattach to this project's most
                             recent live session, or spawn a fresh daemon.
* ``doxa new``            -- always spawn a fresh session daemon and attach.
* ``doxa attach [prefix]`` -- reattach to a live session anywhere (prefix
                             matches session id or title; recent history
                             replays, then the live tail follows).
* ``doxa stop [prefix]``  -- finalize a session NOW (LORE review + index)
                             and stop its daemon. No TUI.
* ``doxa doctor``         -- read-only health checks (doxa/doctor.py),
                             no TUI: pass/fail + fix command per check.
                             Exits 1 if anything failed.
* ``doxa --in-process``   -- the Phase 1 shape: engine inside the TUI
                             process, no daemon, quit finalizes immediately.

Quit semantics in the TUI, post-split: ctrl+q (and the palette's
"Quit: detach") detaches -- the daemon keeps running and finalizes only
after ``--linger`` seconds with no client attached; the palette's
"Quit: stop session" finalizes immediately, like ``doxa stop``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys

from . import config, peers
from .app import DoxaApp
from .daemon import spawn_daemon


def _resolve(entries: list[peers.PeerInfo], prefix: str | None) -> peers.PeerInfo:
    """Prefix-resolve one live daemon entry -- same never-guess posture as
    peers.resolve_peer: no match and ambiguity are both errors that LIST
    the contenders instead of picking one."""
    if prefix:
        entries = [
            p for p in entries
            if p.session_id.startswith(prefix) or p.title.startswith(prefix)
        ]
    if not entries:
        raise SystemExit(
            f"doxa: no live session matches {prefix!r}" if prefix
            else "doxa: no live sessions (start one with `doxa`)"
        )
    if len(entries) > 1:
        listing = "\n".join(
            f"  {p.title}  {p.session_id[:8]}  {p.cwd}" for p in entries
        )
        raise SystemExit(
            "doxa: more than one live session"
            + (f" matches {prefix!r}" if prefix else "")
            + f" -- pick one by prefix:\n{listing}"
        )
    return entries[0]


def _run_attached(
    socket_path: str, cwd: str, model: str | None, linger: float
) -> None:
    from .client import EngineClient

    def new_session_factory() -> EngineClient:
        _sid, dsock = spawn_daemon(cwd, model=model, linger_secs=linger)
        return EngineClient(dsock)

    app = DoxaApp(
        cwd=cwd,
        model=model,
        engine_factory=lambda: EngineClient(socket_path),
        new_session_factory=new_session_factory,
    )
    app.run()
    _maybe_restart(app)


def _maybe_restart(app: DoxaApp) -> None:
    """`/update --restart` asked for a relaunch. It happens HERE, after the
    app has given the terminal back -- exec'ing out from under a running
    Textual app would leave the terminal in raw mode."""
    if not getattr(app, "restart_requested", False):
        return
    print("doxa: relaunching after update…", file=sys.stderr)
    os.execv(sys.executable, [sys.executable, "-m", "doxa.cli", *sys.argv[1:]])


async def _stop(socket_path: str) -> "str | None":
    from .client import EngineClient

    client = EngineClient(socket_path)
    await client.start()
    event = await client.stop()
    note = event.data.get("note")
    return str(note) if note else None


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="doxa",
        description="DOXA -- a terminal for a Claude agent whose memory you can audit.",
    )
    parser.add_argument(
        "command", nargs="?", default=None,
        choices=["new", "attach", "stop", "doctor", "launcher"],
        help="new: fresh session; attach [prefix]: reattach; stop [prefix]: "
             "finalize now; doctor: read-only health checks, no TUI; "
             "launcher install|uninstall: the XDG start-menu entry. "
             "Default: spawn-or-attach in this project.",
    )
    parser.add_argument("prefix", nargs="?", default=None,
                        help="session id or title prefix (attach/stop)")
    # Flag > env > config file > default: argparse supplies the flag layer,
    # doxa.config supplies the two beneath it (see doxa/config.py).
    parser.add_argument("--model", default=config.model())
    parser.add_argument("--linger", type=float, default=config.linger_secs(),
                        help="seconds a spawned daemon outlives its last "
                             "client before finalizing (default %(default)s)")
    parser.add_argument("--in-process", action="store_true",
                        help="Phase 1 mode: engine inside the TUI, no daemon")
    parser.add_argument("--branch", default=None,
                        help="item S: fork the new session's worktree from "
                             "this ref instead of the launch cwd's own "
                             "checkout (spawn-time only: `doxa new "
                             "--branch <name>`, or plain `doxa` with "
                             "nothing live in this project)")
    parser.add_argument("--checkout", action="store_true",
                        help="with --branch and worktree_per_session OFF: "
                             "allow switching the ACTUAL checkout (refused "
                             "by default, and even then only on a clean "
                             "tree) instead of the default refusal")
    args = parser.parse_args(argv)

    cwd = os.getcwd()

    # One-shot relocation of a pre-~/.doxa config. Announced, never silent:
    # a settings file that moves without saying so is a settings file the
    # user will look for in the wrong place forever.
    moved = config.migrate_legacy()
    if moved is not None:
        print(f"doxa: settings moved to {moved}", file=sys.stderr)

    if args.in_process:
        DoxaApp(cwd=cwd, model=args.model).run()
        return 0

    if args.command == "launcher":
        from . import launcher as launcher_mod

        if args.prefix not in (None, "install", "uninstall"):
            print(f"doxa launcher: unknown action {args.prefix!r} "
                  "(install|uninstall)", file=sys.stderr)
            return 2
        action = (launcher_mod.uninstall if args.prefix == "uninstall"
                  else launcher_mod.install)
        print(action())
        return 0

    if args.command == "doctor":
        from . import doctor as doctor_mod

        checks = doctor_mod.run_checks()
        print(doctor_mod.report(checks))
        return 1 if doctor_mod.any_failing(checks) else 0

    if args.command == "stop":
        entry = _resolve(peers.list_daemons(), args.prefix)
        note = asyncio.run(_stop(entry.daemon_socket))
        print(f"stopped {entry.title} ({entry.session_id[:8]}) -- "
              "session finalized (LORE review + index).")
        if note:
            print(f"doxa: {note}", file=sys.stderr)
        return 0

    if args.command == "attach":
        entry = _resolve(peers.list_daemons(), args.prefix)
        _run_attached(entry.daemon_socket, entry.cwd, args.model, args.linger)
        return 0

    if args.command is None:
        # Spawn-or-attach: this project's scope only, newest session first.
        # main_repo_root_of -- not repo_root_of -- so running `doxa` from
        # inside a worktree-per-session worktree (doxa.worktrees) still
        # discovers sessions of the SAME repo hosted from other worktrees
        # or the main checkout, rather than treating the worktree as its
        # own separate project.
        scope = peers.main_repo_root_of(cwd) or cwd
        live = peers.list_daemons(scope_key=scope)
        if live:
            entry = live[0]
            print(f"attaching to {entry.title} ({entry.session_id[:8]})…",
                  file=sys.stderr)
            _run_attached(entry.daemon_socket, entry.cwd, args.model, args.linger)
            return 0

    # `doxa new`, or plain `doxa` with nothing live in this scope.
    base_branch = _resolve_branch_flag(cwd, args)
    if base_branch is _BRANCH_FLAG_FAILED:
        return 2
    _sid, dsock = spawn_daemon(
        cwd, model=args.model, linger_secs=args.linger, base_branch=base_branch,
    )
    _run_attached(dsock, cwd, args.model, args.linger)
    return 0


_BRANCH_FLAG_FAILED = object()  # sentinel: --branch was given and refused


def _resolve_branch_flag(cwd: str, args: argparse.Namespace):
    """``--branch`` (item S #1), validated up front with an actionable
    message -- worktrees.create()'s own contract stays permissive (``None``
    is always a safe "just run in cwd" fallback, see its docstring), so an
    explicit flag needs its OWN, stricter check here rather than silently
    riding that fallback into ignoring what the user asked for.

    Returns the base ref to hand to :func:`spawn_daemon`, ``None`` when
    ``--branch`` was not given at all, or the sentinel
    :data:`_BRANCH_FLAG_FAILED` after already printing why (the caller's
    cue to exit 2)."""
    from . import worktrees as worktrees_mod

    branch = args.branch
    if not branch:
        return None
    scope = peers.main_repo_root_of(cwd)
    if scope is None:
        print(f"doxa: --branch needs a git repo (none found at {cwd})",
              file=sys.stderr)
        return _BRANCH_FLAG_FAILED
    if worktrees_mod.enabled():
        resolved = worktrees_mod.resolve_ref(scope, branch)
        if resolved is None:
            print(f"doxa: no such branch: {branch!r}", file=sys.stderr)
            return _BRANCH_FLAG_FAILED
        return resolved
    # worktree_per_session is OFF: there is no isolated worktree for
    # --branch to fork -- it would move the ACTUAL checkout, which this
    # feature must never do silently. Refused by default; --checkout is
    # the explicit, narrower escape hatch (and even then only on a clean
    # tree -- never discard real uncommitted work because a flag asked).
    if not args.checkout:
        print(
            "doxa: worktree_per_session is off -- `--branch` would switch "
            "your ACTUAL checkout, not an isolated session worktree, which "
            "this refuses to do silently. Pass --checkout to allow that "
            "explicitly (needs a clean working tree), or turn "
            "worktree_per_session back on.",
            file=sys.stderr,
        )
        return _BRANCH_FLAG_FAILED
    if not worktrees_mod.is_clean(cwd):
        print(
            f"doxa: --checkout refuses on a dirty tree at {cwd} -- commit "
            "or stash first.",
            file=sys.stderr,
        )
        return _BRANCH_FLAG_FAILED
    resolved = worktrees_mod.resolve_ref(scope, branch)
    if resolved is None:
        print(f"doxa: no such branch: {branch!r}", file=sys.stderr)
        return _BRANCH_FLAG_FAILED
    checkout = subprocess.run(
        ["git", "checkout", resolved], cwd=cwd, capture_output=True, text=True,
    )
    if checkout.returncode != 0:
        print(f"doxa: git checkout {resolved} failed: {checkout.stderr.strip()}",
              file=sys.stderr)
        return _BRANCH_FLAG_FAILED
    print(f"doxa: checked out {resolved} (--checkout, worktree_per_session off)",
          file=sys.stderr)
    return None  # already switched in place -- no worktree left to fork


if __name__ == "__main__":
    raise SystemExit(main())
