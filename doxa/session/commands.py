# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.session.commands -- every slash command a pane executes.

The registry side lives in :mod:`doxa.commands` (what a command IS: name,
summary, usage, palette entry, binding); this module is the executor side
(what a command DOES). The two are kept closed against each other by a test
asserting ``pane._command_handlers().keys() == commands.interactive_names()``
-- the registry describes, the pane executes, and neither may grow a
command the other doesn't have.

docs/plans/plugin-api.md's first extension point attaches at :data:`PANE_COMMANDS`
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
from .. import layout as layout_mod
from .. import peers as peers_mod
from .. import version as version_mod
from .. import worktrees as worktrees_mod
from ..peers import PeerSendError, age_secs
from ..ui.dialogs import AboutDialog, PermissionModeConfirm
from ..ui.labels import (
    CONTEXT_UNAVAILABLE,
    MODE_EXPLAIN,
    MODEL_ALIASES,
    PICKER_PREFIX_WIDTH,
    PICKER_ROW_MAX,
    _fmt_age,
    format_picker_row,
    help_text,
    lore_created_text,
    memory_entries,
    memory_fill,
)
from ..ui.transcript import ContextBlock, ImageBlock, ImageShowcaseBlock
from .chips import PICKER_COLUMN_HEADER


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
    CommandBinding("/plugins", "_cmd_plugins"),
    CommandBinding("/reload-plugins", "_cmd_reload_plugins"),
    CommandBinding("/model", "_cmd_model"),
    CommandBinding("/branch", "_cmd_branch"),
    CommandBinding("/mode", "_cmd_mode"),
    CommandBinding("/effort", "_cmd_effort"),
    CommandBinding("/usage", "_cmd_usage"),
    CommandBinding("/context", "_cmd_context"),
    CommandBinding("/clear", "_cmd_clear"),
    CommandBinding("/split", "_cmd_split"),
    CommandBinding("/vsplit", "_cmd_vsplit"),
    CommandBinding("/diff", "_cmd_diff"),
    CommandBinding("/pane", "_cmd_pane"),
    CommandBinding("/movepane", "_cmd_movepane"),
    CommandBinding("/sidebar", "_cmd_sidebar"),
    CommandBinding("/collection", "_cmd_collection"),
    CommandBinding("/detach", "_cmd_detach"),
    CommandBinding("/attach", "_cmd_attach"),
    CommandBinding("/sessions", "_cmd_sessions"),
    CommandBinding("/rename", "_cmd_rename"),
    CommandBinding("/dir", "_cmd_dir"),
    CommandBinding("/cd", "_cmd_cd"),
    CommandBinding("/search", "_cmd_search"),
    CommandBinding("/beliefs", "_cmd_beliefs"),
    CommandBinding("/resume", "_cmd_resume"),
    CommandBinding("/pending", "_cmd_pending"),
    CommandBinding("/update", "_cmd_update"),
    CommandBinding("/help", "_cmd_help"),
    CommandBinding("/about", "_cmd_about"),
)


def _fmt_resume_row(hit: dict) -> str:
    """One /resume picker row, in the SAME fixed-column shape the
    beliefs/proposals pickers already share (item 4 -- the operator
    reported this exact defect class twice for those two: "the column
    widths for beliefs and proposal rows is not the same" / "the
    proposals view should be formatted that the columns have fixed
    width"). Before this it was a `` · ``-joined string that drifted
    with every field's own length -- the SAME shape the proposals row
    itself carried before v0.67.0 merged the two menus' formatters, and
    /resume was simply never brought along.

    ``format_picker_row``'s three fixed columns, reused rather than
    re-derived: STAMP (when the conversation last spoke, via
    ``history_mod.hit_age``'s own ``ts`` field -- ``lore_created_text``
    already tolerates a non-LORE ISO stamp, degrading to a date-only
    prefix rather than an empty column), STATUS (message count, this
    row's one substantive fact besides the title) and AGE (the same
    ``hit_age`` the command and the /search popup already date a
    conversation with, so the two never disagree). TEXT is the title,
    the same fallback-to-short-id ``_cmd_resume`` always used."""
    title = str(hit.get("title") or "").strip() or str(
        hit.get("session_id") or "?"
    )[:8]
    ts = str(hit.get("ts") or "").strip()
    stamp = lore_created_text({"created": ts}) if ts else ""
    count = hit.get("messages")
    status = (
        f"{count} msg{'' if count == 1 else 's'}"
        if isinstance(count, int) and count else ""
    )
    age = history_mod.hit_age(hit)
    return format_picker_row(stamp, status, age, title, width=PICKER_ROW_MAX)


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

    async def _cmd_resume(self, args: str) -> None:
        """``/resume [session-id]`` -- reopen a past conversation.

        The command form of the gesture Enter now makes on a ``/search``
        session header, and the ONLY route to a resume for the one result
        shape that has no header: a search matching a single session stays
        flat by design ("no pointless fold", item I), so there is no
        conversation row to press Enter on. That is why this is a command
        and not only a key.

        Bare, it offers the recent conversations in the SAME
        :class:`ChipPicker` every other list in this app uses -- rows
        carry title, age and message count, and DOXA's own recents query
        (``history.recent_sessions``) supplies them, so this list and the
        one an empty ``/search `` shows are the same list.

        With an argument it takes a session id by PREFIX, the same
        shorthand ``/sessions kill`` and ``doxa attach`` already accept:
        the ids in this app are uuids, and nobody types one in full. An
        ambiguous prefix is refused with the candidates rather than
        resolved by picking the first -- resuming the wrong conversation
        is not an error the user would notice quickly.

        Every eligibility question (running? cwd still there? does the CLI
        know this id?) belongs to :meth:`DoxaApp.resume_session`, which
        the search gesture calls too. This method finds a session; it does
        not decide anything about it."""
        cwd = str(getattr(self.engine, "cwd", None) or self.cwd)
        term = args.strip()
        if term:
            # A PREFIX QUERY, not a filter over the recents: the recents
            # are capped and this-project-first, and an id typed by hand
            # is usually an id from far enough back that it is not in
            # them -- which would have answered "not indexed" about a row
            # sitting in the table.
            matches = await asyncio.to_thread(
                history_mod.sessions_by_prefix, term
            )
            if not matches:
                await self._system(
                    f"resume: no indexed conversation whose id starts with "
                    f"{term!r}. bare /resume lists the recent ones."
                )
                return
            if len(matches) > 1:
                listed = "\n".join(
                    f"  {h['session_id']}  {h.get('title') or '(untitled)'}"
                    for h in matches[:8]
                )
                await self._system(
                    f"resume: {term!r} matches {len(matches)} conversations:"
                    f"\n{listed}\ngive more of the id."
                )
                return
            await self._resume_hit(matches[0])
            return
        rows = await asyncio.to_thread(history_mod.recent_sessions, cwd)
        if not rows:
            await self._system(
                "resume: nothing indexed to resume yet — LORE indexes a "
                "conversation when it ends."
            )
            return
        by_id = {str(h.get("session_id") or ""): h for h in rows}
        picker_rows = [
            (sid, _fmt_resume_row(hit)) for sid, hit in by_id.items() if sid
        ]
        self._open_chip_picker(
            picker_rows, None,
            lambda rid: self.run_worker(
                self._resume_hit(by_id[rid]), group="resume",
            ),
            title="resume",
            note="opens the chosen conversation in a NEW tab; this one keeps running",
            # Item 4: the SAME fixed-column shape and header the beliefs/
            # proposals pickers use (PICKER_COLUMN_HEADER,
            # PICKER_PREFIX_WIDTH) -- rows already newest-first from
            # history.recent_sessions' own ORDER BY last_ts DESC, which
            # by_id above preserves (dict insertion order), so this needed
            # no re-sort, only the column fix.
            column_header=PICKER_COLUMN_HEADER,
            row_prefix_width=PICKER_PREFIX_WIDTH,
        )

    async def _resume_hit(self, hit: dict) -> None:
        """One resolved conversation -> the app's one resume path. Both
        surfaces (this command and the /search gesture) end here, so
        neither can grow its own idea of what resuming means."""
        from ..app import DoxaApp  # deferred: doxa.app imports this package

        app = self.app
        if not isinstance(app, DoxaApp):
            return
        note = await app.resume_session(hit)
        if note:
            await self._system(note)

    async def _cmd_pending(self, args: str) -> None:
        """``/pending`` -- see :meth:`open_pending_picker` for what it
        opens and for why the dropdown itself stays read-only."""
        await self.open_pending_picker()

    async def _cmd_beliefs(self, args: str) -> None:
        """``/beliefs`` -- open the beliefs picker, the SAME surface the
        beliefs chip opens on a click.

        Through v0.68.0 this opened a second, full-height destination
        (item V's standalone browser tab); v0.69.0 removed that tab once
        the picker carried everything it did except the evidence trail --
        confirmed/contradicted/stale/retract inline on each row
        (:data:`doxa.session.chips.BELIEF_ROW_ACTIONS`), and now Right on
        a highlighted row expands its evidence trail in place. One door
        left, reachable from the prompt, the Ctrl+P palette and
        autocomplete, exactly like every other row in the registry. See
        :meth:`doxa.session.chips.PaneChipsMixin.open_beliefs_picker`."""
        await self.open_beliefs_picker()

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

    async def _cmd_plugins(self, args: str) -> None:
        """``/plugins`` -- docs/plans/plugins.md. Read-only, like
        ``/doctor``: what DOXA found under the operator's REAL
        ``~/.claude``, what is enabled there, what would be (or is)
        adopted, and what is refused and why. Off the event loop because
        discovery reads several JSON files and, for an adopted plugin,
        may copy one -- see ``doxa.claude_plugins.report``."""
        from .. import claude_plugins as claude_plugins_mod

        text = await asyncio.to_thread(claude_plugins_mod.report)
        await self._system(text)

    async def _cmd_reload_plugins(self, args: str) -> None:
        """``/reload-plugins`` -- re-scan the operator's ``~/.claude``
        plugins/skills now, without restarting DOXA.

        States plainly what a reload can and cannot reach (the task's own
        requirement): THIS session's CLI already spawned with the
        ``--plugin-dir`` flags its own connect resolved
        (``SessionEngine._build_options``, read once at ``start()``) --
        there is no live control request that can hand a running `claude`
        subprocess a new plugin, the same way there is none for a model
        switch's cousin questions. A reload only changes what the NEXT
        session (a new tab, ``/clear``, or a fresh ``doxa``) is spawned
        with. Re-runs discovery AND re-stages every adoptable plugin (if
        adoption is on) so the report reflects the freshly rebuilt staged
        copies, not a cached one."""
        from .. import claude_plugins as claude_plugins_mod

        discovered = await asyncio.to_thread(claude_plugins_mod.discover)
        staged = await asyncio.to_thread(claude_plugins_mod.adopt, discovered)
        report = await asyncio.to_thread(claude_plugins_mod.report, discovered)
        lines = [report, ""]
        lines.append(
            f"reload-plugins: re-scanned and re-staged {len(staged)} "
            "plugin(s). This takes effect for NEW sessions and tabs only "
            "-- this session's CLI already connected with whatever "
            "--plugin-dir flags its own start resolved, and nothing can "
            "hand a running claude process a new one. Open a new tab "
            "(ctrl+t) or /clear this one to pick up the change."
        )
        await self._system("\n".join(lines))

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

    async def _cmd_mode(self, args: str) -> None:
        """``/mode`` (v0.42.0) -- which tool calls still stop and ask you.

        The counterpart to ``/effort`` directly below, and deliberately
        the opposite story, because the SDK is different in the two cases:
        ``ClaudeAgentOptions.effort`` has no live setter and that command
        says so; ``ClaudeSDKClient.set_permission_mode`` IS a control
        request, so this one genuinely changes the running session, the
        way ``/model`` does.

        Bare ``/mode`` lists all six with what each one does and marks the
        current one. ``/mode <name>`` switches. **The three gated modes go
        through :class:`~doxa.ui.dialogs.PermissionModeConfirm` first**,
        and this method is the single place that gate lives -- the chip's
        picker and the Shift+Tab hotkey both land here rather than each
        carrying their own copy of the rule (the hotkey cannot even reach
        a gated mode; see ``engine.next_cycle_mode``).

        Nothing here writes to the settings file, and that is the
        persist-or-reset answer in one line: a mode is session state.
        ``/model`` saves because a model is a preference; a permission
        mode is a posture adopted for a piece of work, and a single
        Shift+Tab tap silently rewriting the default for every future
        session -- in repositories not yet cloned -- is not what that
        keystroke means. The persistent default is its own settings row
        (``permission_mode``), narrowed to the three cycle-safe modes."""
        from .. import engine as engine_mod

        engine = self.engine
        current = str(getattr(engine, "permission_mode", None) or
                      engine_mod.DEFAULT_PERMISSION_MODE)
        armed = bool(getattr(engine, "bypass_armed", False))
        offered = engine_mod.available_modes(armed)
        if not args:
            lines = [f"mode: {current}", ""]
            for name in offered:
                mark = "▸" if name == current else " "
                warn = "⚠ " if name in engine_mod.UNASKED_MODES else "  "
                gate = "  (asks first)" if name in engine_mod.GATED_MODES else ""
                lines.append(
                    f" {mark} {warn}{name:<18} {MODE_EXPLAIN.get(name, '')}{gate}"
                )
            lines += [
                "",
                "usage: /mode <name>   ·   Shift+Tab cycles "
                + " → ".join(engine_mod.cycle_modes(armed)) + " → (home)",
                # Intersected with what this session actually offers --
                # a global list here would leak the very mode the rest of
                # this command is careful not to mention.
                "⚠ marks a mode where DOXA will NOT ask you about a tool "
                "call: " + ", ".join(
                    m for m in engine_mod.UNASKED_MODES if m in offered
                ),
                ", ".join(m for m in engine_mod.GATED_MODES if m in offered)
                + " is not on the hotkey and confirms before it switches",
            ]
            # The settings row and the running session are different
            # things and can legitimately differ; say which is which rather
            # than letting the user infer it from one number.
            configured = config_mod.raw("DOXA_PERMISSION_MODE").strip()
            if configured and configured not in engine_mod.PERSISTABLE_MODES:
                lines.append(
                    f"note: permission_mode={configured!r} in your settings is "
                    "IGNORED — only "
                    + ", ".join(engine_mod.PERSISTABLE_MODES)
                    + " can be persisted. Shift+Tab can put THIS session in a "
                    "wider mode; a stored one would apply to every future "
                    "session, including in repos you have not read yet"
                )
            elif configured:
                lines.append(f"new sessions start in {configured}")
            await self._system("\n".join(lines))
            return
        wanted = args.split()[0]
        if wanted not in engine_mod.PERMISSION_MODES:
            await self._system(
                f"mode: unknown mode {wanted!r} — " + ", ".join(offered)
            )
            return
        if wanted not in offered:
            # The ONE place an unavailable mode may still be NAMED. It is
            # absent from every list, every group and the cycle; but a user
            # who types it deserves the reason rather than "unknown mode",
            # which would be a second lie on top of the first. Reported as
            # exactly this: "i get an error message that the session didnt
            # start with a specific parameter" -- so say which parameter,
            # and say how to get a session that has it.
            await self._system(
                f"mode: {wanted} is not available in this session.\n"
                "  Its CLI was started without "
                f"--{engine_mod.BYPASS_ARM_FLAG}, and that is decided at\n"
                "  launch -- it cannot be turned on for a session already "
                "running.\n"
                "  To make it available to NEW sessions: /settings → "
                "\"allow bypass\" (or DOXA_ALLOW_BYPASS=1),\n"
                "  then /clear or ctrl+t for a fresh one. Off by default, "
                "deliberately: it puts every\n"
                "  session it applies to one keystroke from running tools "
                "unapproved."
            )
            return
        setter = getattr(engine, "set_permission_mode", None)
        if setter is None:
            await self._system(
                "mode: this session's handle cannot switch permission modes"
            )
            return
        if wanted in engine_mod.GATED_MODES and wanted != current:
            accepted = await self.app.push_screen_wait(
                PermissionModeConfirm(wanted, current)
            )
            if not accepted:
                # Declined: no control request is issued, the engine's own
                # mode attribute is untouched and the status line does not
                # move -- the same nothing-happened contract the compact
                # confirm's decline path already keeps.
                await self._system(f"mode: unchanged ({current})")
                return
        try:
            resolved = await setter(wanted)
        except Exception as exc:  # noqa: BLE001 -- a refusal is information
            await self._system(f"mode: {type(exc).__name__}: {exc}")
            return
        self._refresh_status()
        # A transcript line for EVERY switch, and a loud one for the modes
        # where nothing will ask. The status chip is persistent but
        # peripheral -- it sits in the corner and a user who did not mean
        # to press the key is by definition not looking at it. A transcript
        # block is transient but central: it lands where the user's eyes
        # already are, in the same column as the work. Since v0.50.0 a
        # single keystroke reaches bypassPermissions, so this is the one
        # surface guaranteed to be in front of somebody who got there by
        # accident, and it names what stopped rather than just what
        # changed.
        if resolved in engine_mod.UNASKED_MODES:
            await self._system(
                f"⚠ permission mode: {current} → {resolved}\n"
                f"  {MODE_EXPLAIN.get(resolved, '')}.\n"
                "  DOXA will not show you a permission dialog for those "
                "calls — there is nothing left to decline.\n"
                f"  Shift+Tab again to move on, or /mode default to stop "
                "here. This session only; nothing was saved."
            )
            return
        await self._system(
            f"mode: {current} → {resolved}  ·  "
            f"{MODE_EXPLAIN.get(resolved, '')} (this session only)"
        )

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
        same way ``/usage`` and ``/help`` treat theirs.

        Mounts :class:`~doxa.ui.transcript.ContextBlock` directly rather
        than going through :meth:`_system` -- the same door
        :meth:`_cmd_shell`'s ``ShellBlock`` already uses for a block that
        is not plain text. ``ContextBlock`` leads with a 10x20 grid of the
        window (Claude Code's own look, one colored cell per 0.5% of it)
        and keeps every number this method has always printed right below
        it, unchanged; see that class's own docstring for why the grid has
        to be a widget that fits itself at paint time rather than a string
        computed once here."""
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
        if not breakdown:
            await self._system(CONTEXT_UNAVAILABLE)
            return
        block_list = self.query_one("#block-list", VerticalScroll)
        await block_list.mount(ContextBlock(breakdown))
        self.scroll_transcript_to_end(block_list)

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

    async def _cmd_split(self, args: str) -> None:
        """/split -- a second session STACKED below this pane, in the same
        tab. The named form of Ctrl+O; the command is the door that never
        depends on a key encoding at all, which is exactly what saved
        this feature when Alt+S turned out to reach only kitty-protocol
        terminals (v0.95.0).

        A refusal (the pane is already too small to halve, or it has spent
        its depth allowance) comes back as a block in THIS pane's
        transcript rather than a toast, because the pane it is about is
        the one you are looking at."""
        note = await self.app.split_active_pane(layout_mod.COLUMN)
        if note:
            await self._system(note)

    async def _cmd_vsplit(self, args: str) -> None:
        """/vsplit -- a second session SIDE BY SIDE with this pane, in the
        same tab. The named form of Ctrl+N. Same refusals, same
        place they are reported -- see :meth:`_cmd_split`."""
        note = await self.app.split_active_pane(layout_mod.ROW)
        if note:
            await self._system(note)

    async def _cmd_diff(self, args: str) -> None:
        """/diff -- this session's live diff, SIDE BY SIDE with it. The
        named form of F2, and a toggle: a second /diff closes it.

        Same refusals in the same place as :meth:`_cmd_split` -- a
        transcript block in the pane the refusal is about, never a toast
        floating over some other pane."""
        note = await self.app.toggle_diff_pane()
        if note:
            await self._system(note)

    async def _cmd_pane(self, args: str) -> None:
        """``/pane <n>`` -- put the keyboard in pane group ``n``, numbered
        in READING order: left to right, then top to bottom.

        The door that always works. ``Ctrl+1``..``Ctrl+9`` is the fast
        gesture, and under the legacy key encoding a terminal has no byte
        for ``Ctrl+<digit>`` at all (``doxa.keyboard``) -- so the command is
        not a convenience, it is the only way to reach this on a terminal
        without the kitty protocol. Same posture ``/settings`` takes beside
        ``Ctrl+,``.

        With no argument it just FLASHES the numbers, which is the honest
        answer to "which one is which" and costs nothing to ask."""
        raw = args.strip()
        if not raw:
            self.app._flash_group_numbers()
            count = len(self.app._group_order())
            await self._system(
                f"{count} pane group(s), numbered left to right then top to "
                "bottom — /pane <n> or Ctrl+<n> to jump to one"
                if count > 1 else
                "one pane group — /split or /vsplit makes a second"
            )
            return
        try:
            number = int(raw)
        except ValueError:
            await self._system(f"/pane wants a group number, not {raw!r}")
            return
        note = self.app.focus_group_number(number)
        if note:
            await self._system(note)

    async def _cmd_movepane(self, args: str) -> None:
        """``/movepane <n>`` -- move THIS group's active tab into pane
        group ``n``.

        Given a command from the start and deliberately no key of its own
        yet: ``Ctrl+Shift+←/→`` is taken by directional focus, and the
        pane-groups spec defers the spelling rather than contesting a
        binding that already means something. The command is not a
        placeholder for that key -- it is the form that will still work on
        the terminals where whatever key is chosen cannot be sent.

        The session does NOT restart, stop or fork: Textual cannot
        re-parent a mounted widget, so the tab is re-created at the
        destination and the live engine handle is re-seated onto it. See
        :meth:`doxa.app.DoxaApp._reseat_pane`."""
        raw = args.strip()
        if not raw:
            self.app._flash_group_numbers()
            await self._system(
                "/movepane <n> — which group? they are numbered left to "
                "right, then top to bottom"
            )
            return
        try:
            number = int(raw)
        except ValueError:
            await self._system(f"/movepane wants a group number, not {raw!r}")
            return
        note = await self.app.move_tab_to_group(number)
        if note:
            await self._system(note)

    async def _cmd_sidebar(self, args: str) -> None:
        """``/sidebar`` -- show or hide the session rail.

        The door that always works, beside ``F3``. Same posture
        ``/pane`` takes beside ``Ctrl+<digit>`` and ``/settings`` beside
        ``Ctrl+,``: the key is the fast gesture, the command is the one
        that survives a terminal that cannot send it -- and ``F3`` is
        tmux's default prefix, so on a tmux session this IS the door.

        With ``on`` or ``off`` it says which, rather than toggling: a
        command run from a script or a keybinding wants to assert a state,
        not flip whatever it finds."""
        raw = args.strip().lower()
        rail = self.app.sidebar()
        showing = bool(rail is not None and rail.styles.display != "none")
        if raw in ("on", "show", "open"):
            want = True
        elif raw in ("off", "hide", "close"):
            want = False
        elif not raw:
            want = not showing
        else:
            await self._system(
                f"/sidebar takes on, off or nothing at all — not {raw!r}"
            )
            return
        note = self.app.set_sidebar(want)
        if note:
            await self._system(note)
            return
        rail = self.app.sidebar()
        now = bool(rail is not None and rail.styles.display != "none")
        await self._system(
            "session sidebar shown — click a row to go to that session, a "
            "heading to fold it (F3, /collection)"
            if now else "session sidebar hidden (F3, /sidebar)"
        )

    async def _cmd_collection(self, args: str) -> None:
        """``/collection new|rename|delete|add|remove [name]`` -- the
        sidebar's user-named groupings of sessions.

        **collection, never group.** ``group`` is taken: a
        :class:`doxa.ui.split.PaneGroup` is a REGION of the screen owning
        its own tab strip. A collection groups sessions by NAME wherever
        they are shown -- two members may sit in different regions, and one
        region may show tabs from three collections.

        ``add`` is the verb docs/plans/session-sidebar.md asks for by
        description ("moving a session into a collection") and it moves
        THIS session, the one the command was typed in, because that is the
        session the user is looking at. It creates the collection if it
        does not exist: "put this session in ampiric" is one intention
        whether or not ampiric already exists.

        Every refusal comes from :mod:`doxa.collections` and is printed
        verbatim -- the model refuses, this prints; a second opinion here
        would be a second set of rules."""
        parts = args.strip().split(None, 1)
        verb = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        app = self.app
        if not verb or verb in ("list", "ls"):
            items = app.collections()
            if not items:
                await self._system(
                    "no collections yet — /collection add <name> puts this "
                    "session in one (and makes it)"
                )
                return
            lines = [
                f"{c.name} — {len(c.sessions)} session"
                f"{'' if len(c.sessions) == 1 else 's'}"
                f"{' (folded)' if c.collapsed else ''}"
                for c in items
            ]
            await self._system("\n".join(["collections:", *lines]))
            return
        if verb == "new":
            note = app.collection_new(rest)
            await self._system(note or f"collection {rest!r} — empty, so far")
            return
        if verb == "rename":
            names = rest.split(None, 1)
            if len(names) < 2:
                await self._system("/collection rename <old> <new>")
                return
            note = app.collection_rename(names[0], names[1])
            await self._system(
                note or f"{names[0]!r} is now {names[1].strip()!r}"
            )
            return
        if verb == "delete":
            note = app.collection_delete(rest)
            await self._system(
                note
                # Named out loud, because "delete" is the one verb here a
                # user could reasonably fear means "lose the sessions".
                or f"collection {rest!r} gone — its sessions are ungrouped, "
                   "not closed"
            )
            return
        if verb in ("add", "move", "put"):
            if not self._session_id:
                await self._system(
                    "this session has no id yet — try again once it has "
                    "connected"
                )
                return
            note = app.collection_assign(rest, self._session_id)
            await self._system(note or f"this session is now in {rest!r}")
            return
        if verb in ("remove", "rm", "out"):
            if not self._session_id:
                await self._system("this session has no id yet")
                return
            note = app.collection_unassign(self._session_id)
            await self._system(note or "this session is ungrouped again")
            return
        await self._system(
            f"/collection: no verb {verb!r} — new, rename, delete, add, "
            "remove, or nothing to list them"
        )

    async def _cmd_detach(self, args: str) -> None:
        """/detach -- the deliberate opposite of Ctrl+W: this tab closes,
        its session keeps running, and quitting will not come back for
        it."""
        await self.app.action_detach_tab()

    async def _cmd_attach(self, args: str) -> None:
        """/attach [prefix] -- the door back in, symmetric with /detach:
        /detach sends a session's tab OUT of the strip and it keeps
        running; /attach brings one back IN. Always a NEW tab, never the
        one /attach was typed in -- the same rule v0.45.0's /resume
        settled (a pane holds a live conversation; /clear is the verb for
        replacing one), and now the SAME rule the palette's "Attach: ..."
        entries and the sessions chip's picker follow too
        (:meth:`DoxaApp._cmd_attach`, fixed in this same release to reuse
        :meth:`DoxaApp._attach_in_new_tab` instead of switching the
        active pane's engine in place -- see that method's own docstring
        for the measured defect that was). Once resolved, this command
        hands its match to THAT same entry point (via :meth:`_attach_entry`
        below) rather than to _attach_in_new_tab directly -- one shared
        attach path for every surface that offers one, not a second
        primitive reached a second way.

        A PREFIX matches against every live, DAEMON-hosted session (id or
        title, same shorthand `/sessions kill` and `doxa attach` already
        take) regardless of whether some tab of this window already holds
        it -- refused by NAMING what it found on both "nothing" and "more
        than one", the same shape /resume's own prefix form uses, because
        attaching the wrong conversation is not a mistake a user would
        notice quickly either.

        Bare, the candidates narrow to DETACHED sessions -- live, but not
        open in any tab of this window (the same population
        `/sessions kill-detached` reaps): exactly one attaches outright,
        several open the SAME ChipPicker bare /resume opens onto its own
        list, so a THIRD picker never gets invented for this one."""
        from ..app import DoxaApp  # deferred: doxa.app imports this package

        app = self.app
        if not isinstance(app, DoxaApp):
            return
        open_by_sid = {
            str(getattr(p.engine, "session_id", "") or ""): p
            for p in app.panes()
        }
        term = args.strip()
        if term:
            matches = [
                e for e in peers_mod.list_daemons()
                if e.session_id.startswith(term) or e.title.startswith(term)
            ]
            if not matches:
                await self._system(f"attach: no live session matches {term!r}")
                return
            if len(matches) > 1:
                listed = "\n".join(
                    f"  {e.session_id[:8]}  {e.title}  {e.cwd}" for e in matches
                )
                await self._system(
                    f"attach: {term!r} matches {len(matches)} live sessions:"
                    f"\n{listed}\ngive more of the id."
                )
                return
            await self._attach_entry(matches[0], open_by_sid)
            return
        detached = [
            e for e in peers_mod.list_daemons() if e.session_id not in open_by_sid
        ]
        if not detached:
            await self._system(
                "attach: nothing detached to attach to — /sessions lists "
                "what is live"
            )
            return
        if len(detached) == 1:
            await self._attach_entry(detached[0], open_by_sid)
            return
        self._open_chip_picker(
            [(e.session_id, f"{e.title}  {e.session_id[:8]}") for e in detached],
            None,
            lambda rid: self.run_worker(
                self._attach_hit(rid, detached, open_by_sid), group="attach",
            ),
            title="attach",
            note="attaches in a NEW tab; this one keeps running",
        )

    async def _attach_entry(
        self, entry: "peers_mod.PeerInfo", open_by_sid: "dict[str, Any]",
    ) -> None:
        """One resolved live session -> :meth:`DoxaApp._cmd_attach`, the
        SAME entry point the palette's "Attach: ..." rows and the sessions
        chip's own picker use -- never a second attach primitive (see that
        method's own docstring for the v0.60.0 defect fixed there, which
        this command inherits the fix for by calling it rather than
        _attach_in_new_tab directly).

        Already open in ANOTHER tab of THIS window: switched to, never
        attached a second time -- the ONE exclusion _cmd_attach's own
        callers make BEFORE reaching it (the palette's Attach section
        drops these from its candidate list outright; the sessions chip's
        picker checks separately, same as here) rather than something
        _cmd_attach re-derives itself. /attach's own WITH-A-PREFIX
        candidates are not pre-filtered that way (a typed prefix can name
        anything live), so this is the one caller that has to make the
        check rather than rely on it having already been made."""
        from ..app import DoxaApp  # deferred: doxa.app imports this package

        app = self.app
        if not isinstance(app, DoxaApp):
            return
        other = open_by_sid.get(entry.session_id)
        if other is not None:
            app._switch_to_tab(getattr(other, "id", None) or "")
            return
        app._cmd_attach(entry)

    async def _attach_hit(
        self,
        rid: str,
        entries: "list[peers_mod.PeerInfo]",
        open_by_sid: "dict[str, Any]",
    ) -> None:
        """The bare-/attach ChipPicker's selection callback: resolve the
        row back to its PeerInfo (the picker only ever hands back the id)
        and hand it to the same path a resolved prefix takes."""
        entry = next((e for e in entries if e.session_id == rid), None)
        if entry is None:
            return
        await self._attach_entry(entry, open_by_sid)

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
            if ok:
                # v0.60.0: reaping is the ONE gesture in this app that
                # means "forget this conversation" -- a session Ctrl+W'd
                # earlier this run (still sitting in _detached_this_run)
                # or still attached in a tab of this very window must not
                # resurrect at the next launch just because a stop path
                # that keeps things resumable now exists. Vetoed by id
                # rather than reached for and popped out of those two
                # dicts: killing what a DIFFERENT window's tab is holding
                # (this registry is not scoped to one window) has nothing
                # local to pop, and the veto covers that case the same way.
                self.app._killed_this_run.add(entry.session_id)
        swept = await asyncio.to_thread(peers_mod.sweep_stale)
        if killed:
            # Write the veto NOW rather than waiting for some later,
            # unrelated tab change to trigger a persist -- a session
            # already written into today's saved record (by an earlier
            # detach, or simply because it was still open when the window
            # last saved) must not survive on disk past the kill that was
            # just asked for by name.
            self.app._persist_tabset()
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

    def _dir_line(self) -> str:
        """The one line both :meth:`_cmd_dir` and :meth:`_cmd_cd` show for
        "where THIS session actually is" -- the literal cwd
        :class:`~doxa.engine.SessionEngine` was booted with, which is what
        every one of its own tool calls resolves relative paths against.
        Since v0.17.0 that is usually a DOXA-managed worktree
        (:mod:`doxa.worktrees`), not the directory the user typed at
        launch, so a worktree session names both the worktree path AND the
        real repo/base it was forked from -- the same facts the git chip's
        own tooltip already carries (:meth:`GitLine.chip_hints`), read
        here from the sidecar directly rather than re-derived."""
        cwd = str(getattr(self.engine, "cwd", None) or self.cwd)
        meta = worktrees_mod.read_meta(cwd)
        if not meta:
            return cwd
        main_root = str(meta.get("main_root") or "")
        base_ref = str(meta.get("base_ref") or "")
        detail = " -- a DOXA worktree"
        if main_root:
            detail += f" of {main_root}"
        if base_ref:
            detail += f", based on {base_ref}"
        return cwd + detail

    async def _cmd_dir(self, args: str) -> None:
        """``/dir`` -- reports the session's working directory. Read-only,
        by design: this is the honest half of the pair the operator asked
        for, and it is what makes ``/cd``'s own honesty checkable -- the
        "unchanged" it reports after opening a new tab elsewhere is this
        exact line, unmoved."""
        await self._system(f"dir: {self._dir_line()}")

    async def _cmd_cd(self, args: str) -> None:
        """``/cd <path>`` -- what "change directory" can actually MEAN for
        a session that is already running, worked through and reported
        rather than assumed:

        The claude CLI subprocess behind this session was spawned once,
        with its OWN operating-system cwd (``ClaudeAgentOptions.cwd`` at
        connect, :meth:`doxa.engine.SessionEngine._build_options`) -- an
        OS-level fact a running process cannot be handed a new one for,
        the SDK exposes no such control request, and every tool call the
        model makes for the rest of THIS session resolves relative paths
        against that original directory regardless of anything DOXA does
        here. Repainting only this pane's own bookkeeping (``self.cwd``,
        the git chip, LORE's project scope) to point somewhere else would
        make the status line claim a location none of the session's own
        tool calls are actually touching -- which is worse than doing
        nothing: a status chip that lies about where the agent is working
        is exactly the "appears to work" failure this command must not
        become.

        So ``/cd`` does the one thing that is actually true instead: it
        opens the target directory in a NEW TAB, a real session rooted
        there from the moment it boots -- the SAME mechanism the repo-name
        chip's directory picker ("open here", any directory, not only a
        repo root -- see ``PaneChipsMixin._repo_picker_rows``) and
        ``/resume`` already use for "go somewhere else"
        (:meth:`DoxaApp.open_tab_at`). THIS session and this tab are left
        completely alone, and the reply says so every time, in the exact
        words ``/dir`` itself would report -- a command that silently
        changed something else instead is not a working ``/cd``, it is a
        surprise."""
        from ..app import DoxaApp  # deferred: doxa.app imports this package

        path = args.strip()
        if not path:
            await self._system(
                "cd: usage: /cd <path> — opens a NEW tab rooted there; "
                "this session's own directory cannot be changed once it "
                "is running (no such control exists in the CLI it runs). "
                f"this session stays at: {self._dir_line()}"
            )
            return
        target = os.path.abspath(os.path.expanduser(path))
        app = self.app
        if not isinstance(app, DoxaApp):
            return
        error = await app.open_tab_at(target)
        if error:
            await self._system(f"cd: {error}")
            return
        await self._system(
            f"cd: opened a new tab at {target} — this session keeps "
            f"running right where it was: {self._dir_line()}"
        )

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
        # Debug render site for image support -- see ImageBlock. With NO
        # argument it is the showcase rather than a usage error: this
        # command's whole reason to exist is "can this terminal draw
        # pictures", and it now answers that with the measurement plus a
        # render in every tier it may honestly draw. ImageShowcaseBlock's
        # docstring carries the argument for putting it here instead of in
        # /doctor or in a second, near-homonym /image.
        path = os.path.expanduser(args) if args else ""
        if not path:
            block_list = self.query_one("#block-list", VerticalScroll)
            await block_list.mount(ImageShowcaseBlock())
            self.scroll_transcript_to_end(block_list)
            return
        if not os.path.isfile(path):
            await self._system(f"img: no such file: {path}")
            return
        block_list = self.query_one("#block-list", VerticalScroll)
        await block_list.mount(ImageBlock(path))
        self.scroll_transcript_to_end(block_list)

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
        lore_bits.extend(self._lore_memory_bits())
        if lore_bits:
            lines.append(f"lore     {' · '.join(lore_bits)}")
        return "\n".join(lines)

    def _lore_memory_bits(self) -> "list[str]":
        """The rest of what LORE holds for this session, for the opening
        block's `lore` line (v0.56.0, reported): how many proposals are
        staged, how many entries each curated-memory scope holds, and how
        full each scope is.

        REUSED, not re-derived. The fill percentages come from
        :func:`doxa.ui.labels.memory_fill` -- v0.44.0's exact character
        count, read from the file lore_core itself writes and cached on
        mtime, so this line and the status bar's `mem u63% p39%` chip can
        never quote different numbers. The entry counts come from
        :func:`doxa.ui.labels.memory_entries`, which reads the same file
        through lore_core's own ``read_entries``. The pending count is
        whatever :meth:`_boot` already fetched into ``_pending_count``;
        see there for why the socket round trip is affordable at boot and
        would not be here.

        The project slug resolves through :meth:`_lore_slug`, which is the
        one detail that must not be re-implemented: it maps this session's
        WORKTREE back to its main checkout (``peers.main_repo_root_of``)
        because a raw-cwd slug owns no MEMORY.md, which is the v0.47.0
        defect that silently emptied the project half of the memory chip
        for every worktree session -- i.e. for the normal case.

        Absent means omitted, the same rule the rest of this block
        follows. A count of ZERO is not absent: `0 pending` says the
        review queue is clear, and a boot report that hides it leaves the
        reader unable to tell a clear queue from a broken lookup."""
        bits: list[str] = []
        pending = getattr(self, "_pending_count", None)
        if isinstance(pending, int):
            bits.append(f"{pending} pending")
        slug = self._lore_slug()
        for scope, label, project in (
            ("user", "user", None), ("project", "project", slug),
        ):
            fill = memory_fill(scope, project)
            count = memory_entries(scope, project)
            if fill is None or count is None:
                continue
            used, cap = fill
            pct = round(100 * used / cap) if cap else 0
            noun = "entry" if count == 1 else "entries"
            bits.append(f"{label} {count} {noun} {pct}%")
        return bits

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
