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


async def _stop(socket_path: str) -> None:
    from .client import EngineClient

    client = EngineClient(socket_path)
    await client.start()
    await client.stop()


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="doxa",
        description="DOXA -- a terminal for a Claude agent whose memory you can audit.",
    )
    parser.add_argument(
        "command", nargs="?", default=None, choices=["new", "attach", "stop"],
        help="new: fresh session; attach [prefix]: reattach; stop [prefix]: "
             "finalize now. Default: spawn-or-attach in this project.",
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

    if args.command == "stop":
        entry = _resolve(peers.list_daemons(), args.prefix)
        asyncio.run(_stop(entry.daemon_socket))
        print(f"stopped {entry.title} ({entry.session_id[:8]}) -- "
              "session finalized (LORE review + index).")
        return 0

    if args.command == "attach":
        entry = _resolve(peers.list_daemons(), args.prefix)
        _run_attached(entry.daemon_socket, entry.cwd, args.model, args.linger)
        return 0

    if args.command is None:
        # Spawn-or-attach: this project's scope only, newest session first.
        scope = peers.repo_root_of(cwd) or cwd
        live = peers.list_daemons(scope_key=scope)
        if live:
            entry = live[0]
            print(f"attaching to {entry.title} ({entry.session_id[:8]})…",
                  file=sys.stderr)
            _run_attached(entry.daemon_socket, entry.cwd, args.model, args.linger)
            return 0

    # `doxa new`, or plain `doxa` with nothing live in this scope.
    _sid, dsock = spawn_daemon(cwd, model=args.model, linger_secs=args.linger)
    _run_attached(dsock, cwd, args.model, args.linger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
