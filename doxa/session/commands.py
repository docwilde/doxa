"""doxa.session.commands -- every slash command a pane executes.

The registry side lives in :mod:`doxa.commands` (what a command IS: name,
summary, usage, palette entry, binding); this module is the executor side
(what a command DOES). The two are kept closed against each other by a test
asserting ``pane._command_handlers().keys() == commands.interactive_names()``
-- the registry describes, the pane executes, and neither may grow a
command the other doesn't have.

docs/plugin-api.md's first extension point attaches at :data:`PANE_COMMANDS`
below: the executor half is no longer a literal dict inside a method but an
ordered tuple of :class:`CommandBinding` records that
:meth:`PaneCommandsMixin._command_handlers` binds against a pane. A future
registry of plugin-contributed commands is a second sequence folded into
that same build step -- and the closure test then reads
``interactive_names()`` plus whatever those contributed, which is exactly
what the spec asks for. Nothing here loads anything.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable

from textual.containers import VerticalScroll

from .. import auth as auth_mod
from .. import config as config_mod
from .. import history as history_mod
from .. import identity as identity_mod
from .. import peers as peers_mod
from .. import version as version_mod
from ..peers import PeerSendError, age_secs
from ..ui.dialogs import AboutDialog
from ..ui.labels import (
    CONTEXT_UNAVAILABLE,
    MODEL_ALIASES,
    _fmt_age,
    context_breakdown_text,
    help_text,
)
from ..ui.transcript import ImageBlock


@dataclass(frozen=True)
class CommandBinding:
    """One interactive slash command wired to the pane method that runs it.

    ``name`` is the ``/name`` :mod:`doxa.commands` already describes -- this
    record never redefines a command's summary, usage or palette entry,
    because a second copy of those is how the two halves drift apart. It
    says only WHO RUNS IT.

    ``method`` is an attribute name on the pane rather than a function
    object, because the binding table is built once at import and the pane
    it binds to does not exist yet; ``args`` are the leading positional
    arguments baked in ahead of the argument string (``/login`` and
    ``/logout`` are one handler with two spellings, and this is where that
    is written down)."""

    name: str
    method: str
    args: tuple[Any, ...] = field(default_factory=tuple)

    def bind(self, pane: Any) -> "Callable[[str], Any]":
        """The handler as ``_command_handlers`` hands it out: a callable
        taking the argument string and nothing else."""
        handler = getattr(pane, self.method)
        return partial(handler, *self.args) if self.args else handler


# The executor half of the command surface, in the order it was written in
# ``doxa/app.py`` before the split -- order is not load-bearing (the dict
# is looked up by name), but keeping it makes the diff against the old
# literal readable, and gives a plugin-contributed command an obvious place
# to be appended later.
PANE_COMMANDS: "tuple[CommandBinding, ...]" = (
    CommandBinding("/peers", "_cmd_peers"),
    CommandBinding("/msg", "_cmd_msg"),
    CommandBinding("/img", "_cmd_img"),
    CommandBinding("/login", "_cmd_auth", ("login",)),
    CommandBinding("/logout", "_cmd_auth", ("logout",)),
    CommandBinding("/settings", "_cmd_settings"),
    CommandBinding("/setup", "_cmd_setup"),
    CommandBinding("/doctor", "_cmd_doctor"),
    CommandBinding("/model", "_cmd_model"),
    CommandBinding("/branch", "_cmd_branch"),
    CommandBinding("/effort", "_cmd_effort"),
    CommandBinding("/usage", "_cmd_usage"),
    CommandBinding("/context", "_cmd_context"),
    CommandBinding("/clear", "_cmd_clear"),
    CommandBinding("/detach", "_cmd_detach"),
    CommandBinding("/sessions", "_cmd_sessions"),
    CommandBinding("/rename", "_cmd_rename"),
    CommandBinding("/search", "_cmd_search"),
    CommandBinding("/pending", "_cmd_pending"),
    CommandBinding("/update", "_cmd_update"),
    CommandBinding("/help", "_cmd_help"),
    CommandBinding("/about", "_cmd_about"),
)


class PaneCommandsMixin:
    """SessionPane's slash-command half. Mixed into the pane, never used
    standalone: every method here reads pane state through ``self``."""

    def _command_handlers(self) -> "dict[str, Callable[[str], Any]]":
        """name -> coroutine handler, each taking the argument string.

        The keys of this dict and ``commands.interactive_names()`` are
        asserted equal by the test suite: the registry describes, the pane
        executes, and neither may grow a command the other doesn't have.

        Built from :data:`PANE_COMMANDS` rather than written out literally
        (v0.34.0): the table is the thing a plugin-contributed command
        would be folded into, and the closure assertion above stays exactly
        as true of a built dict as it was of a literal one."""
        return {entry.name: entry.bind(self) for entry in PANE_COMMANDS}

    async def _cmd_pending(self, args: str) -> None:
        """``/pending`` -- see :meth:`open_pending_picker` for what it
        opens and for why it is read-only."""
        await self.open_pending_picker()

    async def _cmd_settings(self, args: str) -> None:
        self.app.action_settings()

    async def _cmd_setup(self, args: str) -> None:
        self.app.action_setup()

    async def _cmd_doctor(self, args: str) -> None:
        """/doctor -- read-only, so this is the whole handler: run every
        check off the event loop (the claude CLI probes shell out) and
        print the report as an ordinary SystemBlock."""
        from .. import doctor as doctor_mod

        checks = await asyncio.to_thread(doctor_mod.run_checks)
        await self._system(doctor_mod.report(checks))

    async def _cmd_model(self, args: str) -> None:
        """/model -- switch the model for subsequent turns, in place.

        The SDK's set_model is a control request, so this is genuinely a
        switch and not a restart: the transcript, the daemon, the replay
        ring and every hook survive it untouched. The chosen model is also
        written to the settings file, because the settings modal's `model`
        row and this command are the SAME state -- one source of truth."""
        engine = self.engine
        current = str(getattr(engine, "model", None) or "default")
        if not args:
            lines = [f"model: {current}", ""]
            for alias in MODEL_ALIASES:
                mark = "▸" if alias in current.lower() else " "
                lines.append(f" {mark} {alias}")
            lines.append("")
            lines.append("usage: /model <alias or full model id>")
            await self._system("\n".join(lines))
            return
        wanted = args.split()[0]
        setter = getattr(engine, "set_model", None)
        if setter is None:
            await self._system(
                "model: this session's handle cannot switch models"
            )
            return
        try:
            resolved = await setter(wanted)
        except Exception as exc:  # noqa: BLE001 -- a refusal is information
            await self._system(f"model: {type(exc).__name__}: {exc}")
            return
        config_mod.save({"model": wanted})
        self._refresh_status()
        self._refresh_identity()
        await self._system(
            f"model: {current} → {resolved}  ·  transcript and session kept "
            "(SDK control request, no reconnect)"
        )

    async def _cmd_branch(self, args: str) -> None:
        """/branch -- no argument lists local branches (current base
        marked); an argument switches this session's base (item S #2).

        Only meaningful with worktree-per-session: a switch rebases the
        session's OWN worktree branch onto the new base -- free (a fast-
        forward, no history to replay) only when clean and zero commits
        ahead of the CURRENT base, the same rule
        doxa.worktrees.finalize's "kept doxa/<id> — merge when ready"
        convention already applies at session end; a dirty or committed-
        ahead worktree is refused in that same voice rather than silently
        carrying the diff across (doxa.worktrees.switch_base owns the
        exact wording). Without a session worktree at all (toggle off, or
        this handle just cannot switch), this refuses too: switching the
        ACTUAL checkout out from under a running session is exactly what
        worktree-per-session exists to prevent, so there is no `git
        checkout` fallback here."""
        engine = self.engine
        git = self._git
        if git is None or not git.repo:
            await self._system("branch: no repo here")
            return
        switcher = getattr(engine, "switch_branch", None)
        if switcher is None:
            await self._system(
                "branch: this session's handle cannot switch branches"
            )
            return
        target = args.split()[0] if args else None
        try:
            result = await switcher(target)
        except Exception as exc:  # noqa: BLE001 -- a refusal is information
            await self._system(f"branch: {type(exc).__name__}: {exc}")
            return
        if target is None:
            base = result.get("base")
            lines = [f"branch: {base or '(none)'}", ""]
            for name in result.get("branches") or []:
                mark = "▸" if name == base else " "
                lines.append(f" {mark} {name}")
            lines.append("")
            lines.append("usage: /branch <name>")
            await self._system("\n".join(lines))
            return
        if not result.get("ok"):
            await self._system(f"branch: {result.get('message') or 'switch refused'}")
            return
        self._refresh_status()
        await self._system(f"branch: {result.get('message') or 'switched'}")

    async def _cmd_effort(self, args: str) -> None:
        """/effort -- honest about a real SDK limit.

        ``ClaudeAgentOptions.effort`` (the CLI's --effort) is a CONNECT-TIME
        option; there is no control request for it the way there is for the
        model. So this sets the level for NEW sessions and says plainly
        that the running one keeps its own, rather than pretending to
        change something it cannot."""
        from .. import engine as engine_mod

        current = engine_mod.effort_level()
        if not args:
            lines = [f"effort: {current or '(CLI default)'}", ""]
            for level in engine_mod.EFFORT_LEVELS:
                lines.append(f" {'▸' if level == current else ' '} {level}")
            lines.append("")
            lines.append("usage: /effort <level>   ·   empty value clears it")
            lines.append(
                "the SDK sets effort at connect only — a change applies to "
                "NEW sessions (/clear, a new tab), never to this one"
            )
            await self._system("\n".join(lines))
            return
        level = args.split()[0].lower()
        if level not in engine_mod.EFFORT_LEVELS and level != "default":
            await self._system(
                f"effort: unknown level {level!r} — "
                + ", ".join(engine_mod.EFFORT_LEVELS)
            )
            return
        config_mod.save({"effort": "" if level == "default" else level})
        await self._system(
            f"effort: new sessions will use {level} — this session keeps "
            f"{current or 'the CLI default'} (the SDK has no live setter)"
        )

    async def _cmd_usage(self, args: str) -> None:
        await self._system(self._usage_text())

    async def _cmd_context(self, args: str) -> None:
        """``/context`` (item K) -- what is in the window RIGHT NOW, by
        component, so the ctx% chip stops being one opaque number.

        Every figure comes from the claude CLI's own context accounting
        (``ClaudeSDKClient.get_context_usage``, reached through the engine's
        single measurement path -- :meth:`SessionEngine._safe_context_usage`,
        the same call the ctx chip reads, so the two can never disagree).
        DOXA counts nothing itself: there is no second tokenizer here and no
        component whose size this command estimates. A session that cannot
        be asked prints ``labels.CONTEXT_UNAVAILABLE`` and stops -- an
        invented breakdown in a diagnostic surface is worse than a missing
        one.

        Takes no arguments; a stray one is ignored rather than refused, the
        same way ``/usage`` and ``/help`` treat theirs."""
        engine = self.engine
        fetch = getattr(engine, "context_usage", None)
        if fetch is None:
            await self._system(CONTEXT_UNAVAILABLE)
            return
        try:
            breakdown = await fetch()
        except Exception as exc:  # noqa: BLE001 -- a refusal is information
            await self._system(f"context: {type(exc).__name__}: {exc}")
            return
        await self._system(context_breakdown_text(breakdown))

    def _usage_text(self) -> str:
        """/usage: the session's REAL numbers, and the account's real
        headroom. Both sides are measured, neither is modelled -- the
        token counts are the CLI's own per-result usage block, and the
        percentages are the utilization snapshot the CLI itself fetched
        and cached (doxa.identity.usage). Anything absent is omitted."""
        engine = self.engine
        summary = {}
        if engine is not None and hasattr(engine, "usage_summary"):
            summary = engine.usage_summary() or {}
        rows: list[tuple[str, str]] = []
        session_id = str(summary.get("session_id") or "")
        if session_id:
            rows.append(("session", session_id[:8]))
        rows.append(("model", str(summary.get("model") or "default")))
        rows.append(("turns", f"{int(summary.get('num_turns') or 0):,}"))
        for key, label in (
            ("input_tokens", "tokens in"),
            ("output_tokens", "tokens out"),
            ("cache_read_input_tokens", "cache read"),
            ("cache_creation_input_tokens", "cache write"),
        ):
            if key in summary:
                rows.append((label, f"{int(summary.get(key) or 0):,}"))
        # Item X (ctx absolute): the percentage and its absolute halves are
        # ONE reading (SessionEngine._safe_ctx_usage) and print as one row.
        # An unreported window size says so out loud -- /usage is a surface
        # people paste into bug reports, and a guessed 200000 pasted into a
        # bug report is worse than a blank.
        ctx = summary.get("ctx_percentage")
        ctx_used = summary.get("ctx_tokens")
        ctx_limit = summary.get("ctx_max_tokens")
        ctx_bits: list[str] = []
        if ctx is not None:
            ctx_bits.append(f"{float(ctx):.0f}%")
        if ctx_used is not None or ctx_limit is not None:
            used_text = f"{int(ctx_used):,}" if ctx_used is not None else "?"
            ctx_bits.append(
                f"{used_text} / {int(ctx_limit):,} tokens"
                if ctx_limit is not None
                else f"{used_text} tokens (window size not reported)"
            )
        if ctx_bits:
            rows.append(("context", "  ".join(ctx_bits)))
        account = getattr(engine, "account", None) or {}
        tier = identity_mod.account_tier(account)
        cost = float(summary.get("total_cost_usd") or 0.0)
        if tier:
            rows.append(("plan", f"{tier}  (≈${cost:.4f} if API)"))
        else:
            rows.append(("cost", f"${cost:.4f}"))
        lines = [f"{label:<12} {value}" for label, value in rows]

        usage = identity_mod.usage()
        if usage is None:
            lines.append("")
            lines.append(
                "no subscription utilization cached by the claude CLI "
                "(API-key auth, or it has not fetched one yet)"
            )
            return "usage\n" + "\n".join(lines)
        lines.append("")
        for limit, label in (
            (usage.session, "session (5h)"),
            (usage.weekly, "weekly"),
            (usage.scoped, f"weekly ({usage.scope_label or 'model'})"),
        ):
            if limit is None:
                continue
            note = f"  ⚠ {limit.severity}" if limit.severity != "normal" else ""
            resets = f"  · resets {limit.resets_at[:16]}" if limit.resets_at else ""
            lines.append(f"{label:<12} {limit.percent}%{resets}{note}")
        age = usage.age_secs()
        if age is not None:
            lines.append("")
            lines.append(
                f"utilization cached by the claude CLI {_fmt_age(age)} ago"
                + (" — stale" if usage.is_stale() else "")
            )
        return "usage\n" + "\n".join(lines)

    async def _cmd_clear(self, args: str) -> None:
        """/clear -- a FRESH session in this tab, not a cleared screen.

        Distinct from Ctrl+T: the tab stays, its engine handle is
        finalized (LORE review + index, transcript rotated to the new
        session's file) and replaced. Distinct from scrolling away: the
        model's context is genuinely gone, because the session is."""
        factory = getattr(self.app, "_new_session_factory", None)
        if factory is None:
            await self._system("clear: no session factory on this app")
            return
        self.run_worker(
            self.switch_engine(factory), exclusive=True, group="switch"
        )

    async def _cmd_detach(self, args: str) -> None:
        """/detach -- the deliberate opposite of Ctrl+W: this tab closes,
        its session keeps running, and quitting will not come back for
        it."""
        await self.app.action_detach_tab()

    async def _cmd_sessions(self, args: str) -> None:
        """/sessions -- what is actually alive, and the way to end it.

        Live means all three checks pass: presence file, live pid, and a
        socket that accepts a connection. A file left behind by a crash is
        not a session, and this is the surface where that has to be true,
        because it is where the user comes to find out what is running."""
        parts = args.split()
        verb = parts[0].lower() if parts else ""
        if verb == "kill":
            if len(parts) < 2:
                await self._system("usage: /sessions kill <session prefix>")
                return
            await self._kill_sessions(prefix=parts[1])
            return
        if verb in ("kill-detached", "kill-all-detached"):
            await self._kill_sessions(detached_only=True)
            return
        if verb:
            await self._system(
                f"sessions: unknown action {verb!r} — "
                "usage: /sessions [kill <prefix> | kill-detached]"
            )
            return
        await self._system(self._sessions_text())

    def _sessions_text(self) -> str:
        entries = peers_mod.read_registry(probe=True)
        if not entries:
            return "sessions: none live"
        attached = {
            str(getattr(p.engine, "session_id", "") or "")
            for p in self.app.panes()
        }
        names = {
            str(getattr(p.engine, "session_id", "") or ""): p.display_name()
            for p in self.app.panes()
        }
        rows = []
        for entry in sorted(entries, key=lambda e: e.started_at):
            here = entry.session_id in attached
            label = names.get(entry.session_id) or entry.title
            rows.append(
                f"{entry.session_id[:8]}  {label[:28]:<28} "
                f"up {_fmt_age(age_secs(entry.started_at)):<5} "
                f"{'attached here' if here else 'detached'}"
            )
        return (
            "sessions\n" + "\n".join(rows)
            + "\n\nkill one: /sessions kill <prefix>   ·   "
            "kill every detached one: /sessions kill-detached"
        )

    async def _kill_sessions(
        self, prefix: str = "", detached_only: bool = False
    ) -> None:
        """Terminate live sessions by prefix, or every session no tab of
        this window is attached to. Same stop path as Ctrl+W and `doxa
        stop`: the daemon finalizes (LORE review + index) and exits."""
        # ``_stop_session`` stayed in doxa.app on purpose. It is the
        # app-scope stop primitive (the same one quit-stop and `doxa stop`
        # reach), and the suite swaps it by patching ``doxa.app``. Imported
        # HERE rather than at module top so the lookup happens per call,
        # against the module the patch actually writes to -- binding it at
        # import would turn that patch into a silent no-op.
        from ..app import _stop_session

        entries = peers_mod.read_registry(probe=True)
        attached = {
            str(getattr(p.engine, "session_id", "") or "")
            for p in self.app.panes()
        }
        if detached_only:
            targets = [e for e in entries if e.session_id not in attached]
        else:
            targets = [
                e for e in entries
                if e.session_id.startswith(prefix) or e.title.startswith(prefix)
            ]
        if not targets:
            await self._system(
                "sessions: nothing matched" if prefix
                else "sessions: nothing detached to kill"
            )
            return
        killed, failed = [], []
        for entry in targets:
            ok = await asyncio.to_thread(_stop_session, entry)
            (killed if ok else failed).append(entry.session_id[:8])
        swept = await asyncio.to_thread(peers_mod.sweep_stale)
        lines = []
        if killed:
            lines.append(f"stopped: {', '.join(killed)}")
        if failed:
            lines.append(f"could not stop: {', '.join(failed)}")
        if swept:
            lines.append(f"swept {swept} stale presence file(s)")
        await self._system("sessions\n" + "\n".join(lines))

    async def _cmd_rename(self, args: str) -> None:
        """/rename -- the keyboard door to what double-clicking a tab
        header does. An empty argument returns the tab to its automatic
        label, exactly like an emptied inline editor."""
        before = self.display_name()
        self.set_custom_name(args)
        if self.custom_name:
            await self._system(
                f"tab renamed: {before} → {self.custom_name}  ·  pinned "
                "(model and branch changes no longer rewrite it)"
            )
        else:
            await self._system(
                f"tab name cleared → {self.display_name()}  ·  back to the "
                "automatic label"
            )

    async def _cmd_search(self, args: str) -> None:
        """/search as a SUBMITTED command -- the fallback path.

        The command's real surface is the live popup, which opens the
        moment the separating space is typed and answers as you go. This
        runs when someone submits the line anyway (no hits highlighted, or
        Enter on an empty result): it prints the same hits as a block, so
        the command is never a no-op."""
        term = args.strip()
        if not term:
            await self._system(
                "search: type `/search ` and keep typing — results appear "
                "above the prompt as you type (↑/↓ to move, →/← to expand/"
                "collapse a session, enter to insert the excerpt, esc to "
                "close)"
            )
            return
        cwd = str(getattr(self.engine, "cwd", None) or self.cwd)
        hits = await asyncio.to_thread(history_mod.search_sessions, term, cwd)
        if not hits:
            await self._system(f"search: no matches for {term!r}")
            return
        lines = [
            f"{str(h.get('title') or h.get('session_id', '?'))[:30]:<30} "
            f"{str(h.get('ts', ''))[:16]}  {str(h.get('snippet', ''))}"
            for h in hits
        ]
        await self._system(f"search: {len(hits)} hit(s)\n" + "\n".join(lines))

    async def _cmd_update(self, args: str) -> None:
        """/update -- fast-forward the checkout DOXA runs from, and say what
        moved. `--restart` is the explicit opt-in that stops THIS window's
        sessions afterwards and relaunches; without it nothing running is
        touched, because a terminal that restarts your work to update
        itself has its priorities backwards."""
        from .. import update as update_mod

        restart = "--restart" in args.split()
        report = await asyncio.to_thread(update_mod.update)
        await self._system(report.text())
        if restart and report.status == "updated":
            await self._system(
                "update: stopping this window's sessions and relaunching…"
            )
            self.app.restart_requested = True
            self.app.run_worker(self.app.action_quit_stop(), group="tabs")
        elif restart:
            await self._system("update: nothing to restart for")

    async def _cmd_help(self, args: str) -> None:
        await self._system(help_text())

    async def _cmd_about(self, args: str) -> None:
        """``/about`` (item Z) -- the version, and the rest of what a bug
        report has to state, as a modal rather than a transcript block.

        A modal, not a SystemBlock, for two reasons: it is a property of
        the INSTALLATION rather than of this conversation (a block would
        be scrolled away by the next turn and then quoted back to the
        model as context it has no use for), and it needs a copy door,
        which a block has nowhere to put.

        The update flag is whatever the boot-time worker already found
        (``DoxaApp._check_for_update``); this opens no network call of its
        own, because a modal that fetches on the UI thread is a modal that
        hangs."""
        self.app.push_screen(
            AboutDialog(getattr(self.app, "update_available", None))
        )

    async def _cmd_img(self, args: str) -> None:
        # Debug render site for image support -- see ImageBlock.
        path = os.path.expanduser(args) if args else ""
        if not path:
            await self._system("usage: /img <path>")
            return
        if not os.path.isfile(path):
            await self._system(f"img: no such file: {path}")
            return
        block_list = self.query_one("#block-list", VerticalScroll)
        await block_list.mount(ImageBlock(path))
        block_list.scroll_end(animate=False)

    async def _cmd_peers(self, args: str) -> None:
        assert self.engine is not None
        peers = self.engine.list_peers()
        if not peers:
            await self._system("peers: none in this project right now")
            return
        lines = [
            f"{p.title}  {p.session_id[:8]}  {p.cwd}"
            f"  ·  up {_fmt_age(age_secs(p.started_at))}"
            for p in peers
        ]
        await self._system("peers:\n" + "\n".join(lines))

    async def _cmd_msg(self, args: str) -> None:
        assert self.engine is not None
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            await self._system("usage: /msg <session_prefix> <text>")
            return
        try:
            peer = await self.engine.send_peer_message(parts[0], parts[1])
        except PeerSendError as exc:
            await self._system(f"msg error: {exc}")
            return
        await self._system(f"sent to {peer.title} ({peer.session_id[:8]})")

    async def _cmd_auth(self, verb: str, args: str) -> None:
        """/login [provider] and /logout [provider].

        DOXA holds no credential and runs no auth logic: it suspends the
        TUI (App.suspend -- the supported way to hand the terminal over),
        execs the provider's OWN interactive auth CLI from the data table
        in doxa/auth.py, and on return re-reads identity so the block and
        the status chips reflect whoever is signed in NOW."""
        try:
            provider = auth_mod.resolve(args.split()[0] if args.split() else None)
        except auth_mod.AuthError as exc:
            await self._system(f"{verb}: {exc}")
            return
        cmd = provider.command_for(verb)
        try:
            with self.app.suspend():
                code = auth_mod.run_auth_command(cmd)
        except Exception as exc:  # noqa: BLE001 -- SuspendNotSupported and
            # friends must surface as an ordinary block, not a crashed TUI.
            await self._system(
                f"{verb}: cannot hand the terminal to {provider.binary} here "
                f"({exc}) — run `{' '.join(cmd)}` in another terminal instead"
            )
            return
        # The CLI may have rewritten its config within one mtime tick.
        identity_mod.invalidate()
        self._refresh_identity()
        self._refresh_status()
        shown = " ".join(cmd)
        if code == 0:
            await self._system(f"{shown} — done; identity re-read")
        else:
            await self._system(f"{shown} — exited {code}; identity re-read")

    def _identity_text(self, cwd: str) -> str:
        """The session-start identity summary. Every line renders a REAL
        field (the SDK's connect-time account block, the CLI's own local
        config for the precise plan tier, the engine handle, the git chip,
        LORE's store) -- absent fields are omitted, not invented.

        plan and org are SEPARATE lines on purpose: an organization name is
        informative, never the plan. Conflating the two is how a Max
        subscription can end up reading as somebody's "team subscription"."""
        engine = self.engine
        account = getattr(engine, "account", None) or {}
        local = identity_mod.local_account()
        # Version first: the one line that says WHICH DOXA this is. Its sha
        # is shown only when it differs from the sha the git chip below
        # already carries (or when the checkout is dirty, which the chip
        # never says) -- two identical hex strings in one block is the
        # confusion the @sha labelling exists to prevent.
        head_sha = self._git._read_sha() if self._git is not None else None
        lines: list[str] = [version_mod.version_line(head_sha)]
        if account.get("email"):
            lines.append(f"account  {account['email']}")
        elif local.get("emailAddress"):
            lines.append(f"account  {local['emailAddress']}")
        plan_line = self._plan_line(account, local)
        if plan_line:
            lines.append(f"plan     {plan_line}")
        org = identity_mod.organization(account, local)
        if org:
            role = local.get("organizationRole")
            lines.append(f"org      {org}" + (f" ({role})" if role else ""))
        lines.append(f"model    {getattr(engine, 'model', None) or 'default'}")
        lines.append(f"cwd      {cwd}")
        git_chip = self._git.render() if self._git is not None else None
        if git_chip:
            lines.append(f"repo     {git_chip}")
        swept = int(getattr(self.app, "swept_at_boot", 0) or 0)
        if swept:
            lines.append(
                f"swept    {swept} stale session presence file(s) — /sessions"
            )
        lore_bits = []
        if getattr(engine, "lore_root", None):
            lore_bits.append(str(engine.lore_root))
        if engine is not None:
            lore_bits.append(f"{engine.belief_count()} beliefs")
        if lore_bits:
            lines.append(f"lore     {' · '.join(lore_bits)}")
        return "\n".join(lines)

    @staticmethod
    def _plan_line(account: dict, local: dict) -> str | None:
        """`max 20x (Claude Max · firstParty)` -- the precise local tier
        leading, with the coarse SDK string kept visible as its provenance.
        Falls back to the SDK string alone, then to nothing at all."""
        tier = identity_mod.account_tier(account, local)
        if not tier:
            return None
        detail = [
            str(account[k]) for k in ("subscriptionType", "apiProvider")
            if account.get(k)
        ]
        # The SDK string stays visible as provenance -- it is what the
        # session actually reported -- unless it IS the label verbatim.
        if detail and detail[0].strip().lower() == tier:
            detail = detail[1:]
        return tier + (f" ({' · '.join(detail)})" if detail else "")
