# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.cli -- session lifecycle entry points.

`uv run doxa` and friends. The daemon split (doxa/daemon.py) makes a DOXA
session a process of its own, so the CLI's job is matchmaking between TUIs
and daemons through the ONE discovery surface that already exists -- the
peer registry, whose entries carry the ``daemon_socket`` marker:

* ``doxa``                -- item D: restore this project's whole saved tab
                             set (order, pinned names, active tab -- see
                             doxa.tabsets), reattaching every session still
                             live and reporting any that are gone; falls
                             back to spawn-or-attach (this project's most
                             recent live session, or a fresh daemon) when
                             there is no saved set, or ``restore_tabs`` is
                             off.
* ``doxa new``            -- always spawn a fresh session daemon and attach,
                             ignoring any saved tab set -- exactly one tab.
* ``doxa attach [prefix]`` -- reattach to a live session anywhere (prefix
                             matches session id or title; recent history
                             replays, then the live tail follows). The
                             single-session path, unaffected by item D.
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

Item D's spec text did not survive to the session that built it; see
doxa/tabsets.py's own docstring and CHANGELOG.md's 0.23.0 entry for the
re-derivation and the judgment calls it required.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from typing import Any, Callable  # noqa: F401 -- annotation-only

from . import config, peers, tabsets
from .app import DoxaApp, RestoreTabSpec
from . import history as history_mod
# Imported lazily by _spawn_daemon below: doxa.daemon pulls doxa.engine and
# therefore claude_agent_sdk (404 ms measured). `doxa doctor`, `doxa
# launcher install`, `--help` and an attached TUI never spawn a daemon, and
# until now every one of them paid that import before doing anything.


def _spawn_daemon(*args: object, **kwargs: object) -> tuple:
    """doxa.daemon.spawn_daemon, resolved at the moment of use.

    Through this module's own attribute rather than by importing the name
    directly: the suite substitutes a fake spawn with
    ``monkeypatch.setattr(cli_mod, "spawn_daemon", ...)``, and a direct
    import here would walk past the patch and really fork a daemon."""
    import sys

    return getattr(sys.modules[__name__], "spawn_daemon")(*args, **kwargs)


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
        _sid, dsock = _spawn_daemon(cwd, model=model, linger_secs=linger)
        return EngineClient(dsock)

    def new_session_factory_at(path: str) -> EngineClient:
        # The repo picker's own spawn call (doxa/app.py's item 4,
        # DoxaApp.open_tab_at): the SAME spawn_daemon call above, just
        # parametrized by an operator-chosen path instead of this
        # process's own launch cwd -- not a second daemon-spawning path.
        _sid, dsock = _spawn_daemon(path, model=model, linger_secs=linger)
        return EngineClient(dsock)

    def resume_session_factory(path: str, session_id: str) -> EngineClient:
        # /resume (v0.56.0): the same spawn primitive again, except the
        # daemon CONTINUES the conversation already recorded under
        # session_id instead of minting a new one -- spawn_daemon's own
        # `resume` argument, which also makes the resumed id the daemon's
        # session id (a resume keeps its id; see that function's
        # docstring). Threaded into every DoxaApp construction in this
        # module so /resume is daemon-backed wherever the TUI is.
        _sid, dsock = _spawn_daemon(
            path, model=model, linger_secs=linger, resume=session_id,
        )
        return EngineClient(dsock)

    app = DoxaApp(
        cwd=cwd,
        model=model,
        engine_factory=lambda: EngineClient(socket_path),
        new_session_factory=new_session_factory,
        new_session_factory_at=new_session_factory_at,
        resume_session_factory=resume_session_factory,
    )
    app.run()
    _maybe_restart(app)


def _restore_report_text(
    restored: int, skipped: int, archived: int = 0, resumed: int = 0
) -> "str | None":
    """"restored 3 tabs, 1 read-only transcript (session ended), skipped 1
    session no longer running." -- None when there is nothing to say (no
    saved record at all; see _run_restored's only caller). Each count gets a clause only
    when nonzero, so a clean restore never mentions the other two.

    The middle clause is v0.32.0's and it is the one the user must not
    miss: a read-only tab looks like a session until you try to type in
    it, so the difference between "this is attached" and "this is a
    transcript" is said out loud at the moment the window opens, not left
    to be discovered.

    v0.56.0 adds the ``resumed`` clause and it carries the same duty in
    the other direction: those tabs are LIVE sessions continuing a
    conversation that had ended, which is a strictly bigger claim than
    "restored" and must not hide inside it. Its counterpart is that the
    read-only clause now means "could not be resumed", and each such tab
    says which reason in its own first block."""
    if not restored and not skipped and not archived and not resumed:
        return None
    parts = []
    if restored:
        parts.append(f"restored {restored} tab{'s' if restored != 1 else ''}")
    if resumed:
        parts.append(
            f"resumed {resumed} ended "
            f"{'conversation' if resumed == 1 else 'conversations'}"
        )
    if archived:
        parts.append(
            f"{archived} read-only "
            f"{'transcript' if archived == 1 else 'transcripts'} "
            "(session ended)"
        )
    if skipped:
        parts.append(
            f"skipped {skipped} session{'s' if skipped != 1 else ''} "
            "no longer running"
        )
    return "tab restore: " + ", ".join(parts) + "."


def ended_tab_spec(
    tab: "Any", launch_cwd: str, resume_factory: "Callable[[str, str], Any]",
) -> RestoreTabSpec:
    """One saved tab whose daemon no longer answers -> the spec that opens
    it: RESUMED (a live session continuing that conversation) where that is
    possible, read-only over its transcript where it is not.

    Through v0.44.0 there was only the second outcome. Reported: *"as long
    as a tab was open, when DOXA is started again, the tab should be
    resumed automatically"*. A daemon finalizing on its linger timer while
    the window is shut is the ORDINARY way a session ends, so the dead end
    was the ordinary result of a restart -- restore meant *display*.

    The question is answered from local file and registry reads only
    (:func:`doxa.history.resume_state`): no subprocess, nothing that can
    slow a launch measurably, and nothing that can fail a launch. It is
    also what keeps this honest -- the CLI holds no history under any
    session id DOXA minted before v0.56.0, when its ids and the CLI's were
    two different id spaces (see ``SessionEngine._build_options``), so
    those tabs come back exactly as they do today AND SAY WHY.

    EAGER, not deferred, and the cost argument is why: a resumed tab costs
    one process, not tokens. The CLI loads that conversation out of its
    own store at connect and DOXA sends nothing until the user types. That
    is the same per-tab cost restore ALREADY pays for every tab whose
    daemon is alive (a spawn or an attach each), so deferring it would buy
    a second, subtler tab lifecycle in exchange for a cost the existing
    one already accepts. ``resume_restored`` is the switch for anyone who
    would rather not pay it, and OFF is v0.32.0 exactly."""
    tab_cwd = tab.cwd or launch_cwd
    if not history_mod.resume_restored():
        # The setting doing what it says is not a failure, so there is no
        # reason to explain -- an unexplained read-only tab here is the
        # user's own choice looking exactly like itself.
        return RestoreTabSpec(
            session_id=tab.session_id, pinned_name=tab.pinned_name,
            cwd=tab_cwd, archived=True,
        )
    state, why = history_mod.resume_state(tab.session_id, tab_cwd)
    if state == history_mod.RESUME_OK:
        return RestoreTabSpec(
            session_id=tab.session_id,
            engine_factory=(
                lambda sid=tab.session_id, c=tab_cwd: resume_factory(c, sid)
            ),
            pinned_name=tab.pinned_name, cwd=tab_cwd, resume=True,
        )
    return RestoreTabSpec(
        session_id=tab.session_id, pinned_name=tab.pinned_name,
        cwd=tab_cwd, archived=True,
        resume_note=f"not resumed — {why}" if why else "",
    )


def _run_restored(resolved: "tabsets.ResolvedRestore", launch_cwd: str,
                   model: "str | None", linger: float) -> None:
    """Item D: open every LIVE resolved tab (doxa.tabsets.resolve already
    cross-checked each saved session id against the peer registry), in
    saved order, with saved pinned names, landing on the saved active tab.

    v0.32.0 restores two KINDS of tab from that one saved order: a live
    one reattaches its daemon, and one whose session has ended comes back
    read-only over its transcript (``resolved.archived`` -- see
    doxa.tabsets.resolve and doxa.app.ArchivedSessionTab). Both are built
    here from ``resolved.ordered()``, which is the saved strip order with
    the two interleaved; splitting them would silently reorder the user's
    tabs, which is not a restore.

    Nothing live AND nothing archived means every saved session is gone
    without a trace -- this still spawns exactly one fresh daemon (the
    same outcome as "nothing live in this scope" below) rather than
    leaving the user with no TUI at all, but the report ("skipped N,
    restored 0") still reaches them, on that one fresh tab, so a silently
    different startup never happens."""
    from .client import EngineClient

    report = _restore_report_text(
        len(resolved.tabs), resolved.skipped, len(resolved.archived),
    )

    def new_session_factory_at(path: str) -> EngineClient:
        # Repo picker (item 4): same spawn primitive as every
        # new_session_factory closure in this module, parametrized by an
        # explicit path -- threaded into BOTH restore call sites below so
        # a tab opened from the picker during a RESTORED launch is a real
        # daemon-backed session like every other tab in the window, not a
        # silent fallback to an in-process one (DoxaApp's own default).
        _sid, dsock = _spawn_daemon(path, model=model, linger_secs=linger)
        return EngineClient(dsock)

    def resume_session_factory(path: str, session_id: str) -> EngineClient:
        # /resume (v0.56.0): the same spawn primitive again, except the
        # daemon CONTINUES the conversation already recorded under
        # session_id instead of minting a new one -- spawn_daemon's own
        # `resume` argument, which also makes the resumed id the daemon's
        # session id (a resume keeps its id; see that function's
        # docstring). Threaded into every DoxaApp construction in this
        # module so /resume is daemon-backed wherever the TUI is.
        _sid, dsock = _spawn_daemon(
            path, model=model, linger_secs=linger, resume=session_id,
        )
        return EngineClient(dsock)

    if not resolved.tabs and not resolved.archived:
        _sid, dsock = _spawn_daemon(launch_cwd, model=model, linger_secs=linger)
        app = DoxaApp(
            cwd=launch_cwd, model=model,
            engine_factory=lambda: EngineClient(dsock),
            new_session_factory=lambda: EngineClient(
                spawn_daemon(launch_cwd, model=model, linger_secs=linger)[1]
            ),
            new_session_factory_at=new_session_factory_at,
            resume_session_factory=resume_session_factory,
            restore_report=report,
        )
        app.run()
        _maybe_restart(app)
        return

    # Same convention _run_attached uses: the app's shared cwd (what a
    # LATER Ctrl+T spawns its own fresh daemon against) is the daemon's
    # OWN reported cwd, not necessarily where `doxa` was invoked from --
    # with worktree_per_session on, that is the FIRST restored tab's
    # linked worktree, not the main checkout.
    app_cwd = resolved.tabs[0][1].cwd if resolved.tabs else launch_cwd

    def new_session_factory() -> EngineClient:
        _sid, dsock = _spawn_daemon(app_cwd, model=model, linger_secs=linger)
        return EngineClient(dsock)

    specs = []
    for tab, entry in resolved.ordered():
        if entry is None:
            specs.append(ended_tab_spec(
                tab, launch_cwd, resume_session_factory,
            ))
            continue
        specs.append(RestoreTabSpec(
            session_id=entry.session_id,
            # skip_backlog: the pane renders this session's conversation
            # from its persisted transcript (complete), so replaying the
            # daemon's 512-frame ring on top of it would double every turn
            # the ring still holds. See SessionPane._restore_transcript.
            engine_factory=(
                lambda sock=entry.daemon_socket: EngineClient(
                    sock, skip_backlog=True
                )
            ),
            pinned_name=tab.pinned_name,
            cwd=tab.cwd or entry.cwd,
        ))
    # Recomputed HERE, not from resolved.archived above: the spec loop is
    # where "its session ended" splits into resumed and read-only, and the
    # report has to say which happened rather than lumping both under the
    # count that was true before the question was asked.
    resumed_n = sum(1 for sp in specs if sp.resume)
    archived_n = sum(1 for sp in specs if sp.archived)
    report = _restore_report_text(
        len(specs) - resumed_n - archived_n, resolved.skipped,
        archived_n, resumed_n,
    )
    print(
        f"doxa: restoring {len(specs)} tab(s) in {launch_cwd}…", file=sys.stderr,
    )
    app = DoxaApp(
        cwd=app_cwd, model=model,
        # An all-archived restore has no live tab of its own; DoxaApp's
        # compose() adds ONE fresh session beside the archives so the
        # window is usable, and this is what it spawns against.
        engine_factory=lambda: EngineClient(
            spawn_daemon(app_cwd, model=model, linger_secs=linger)[1]
        ),
        new_session_factory=new_session_factory,
        new_session_factory_at=new_session_factory_at,
        resume_session_factory=resume_session_factory,
        restore_tabs=specs,
        restore_active_id=resolved.active_session_id,
        restore_report=report,
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
        # Item D: restore the WHOLE saved tab set for this scope, when
        # there is one and the setting allows it -- this REPLACES the
        # single-most-recent-session attach below, not just precedes it.
        # tabsets.resolve() returns None only when nothing was ever saved
        # for this scope; every other outcome (including "every saved
        # session is dead") still goes through _run_restored so the report
        # reaches the user instead of silently falling through to a plain
        # spawn-or-attach that differs from what they left.
        if tabsets.enabled():
            resolved = tabsets.resolve(scope)
            if resolved is not None:
                _run_restored(resolved, cwd, args.model, args.linger)
                return 0
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


def __getattr__(name: str) -> object:
    """``doxa.cli.spawn_daemon``, imported on first use (PEP 562).

    doxa.daemon pulls doxa.engine and with it claude_agent_sdk -- 404 ms
    before anything is on screen, for a command that may never spawn a
    daemon. The name stays reachable so existing imports and every
    ``monkeypatch.setattr(doxa.cli, "spawn_daemon", ...)`` keep working."""
    if name == "spawn_daemon":
        from .daemon import spawn_daemon

        return spawn_daemon
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
