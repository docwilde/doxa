# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.app -- the Textual shell: N session tabs over N engine handles.

Phase 1 built this as one pane over an in-process SessionEngine; Phase 2's
daemon split swapped what sits behind the engine handle (an in-process
``SessionEngine`` or a ``doxa.client.EngineClient`` attached to a session
daemon -- the app consumes the same async-iterator surface either way).
Phase 3's tab step is exactly the README sketch: the single-session surface
became :class:`SessionPane` (a pure extraction -- block list, status bar,
prompt input, boot/pump workers, out-of-band rendering), and a
``TabbedContent`` hosts N of them, one engine handle EACH. Tabs are N
clients in one TUI, not N engines in one process: Ctrl+T spawns a fresh
daemon in the same repo scope (``new_session_factory``) and attaches it in
a new tab; Ctrl+W close-detaches just that tab's client. Worker groups are
scoped per pane node (Textual cancels by (node, group)), so an exclusive
pump dies with its tab, not with its neighbor. The peer layer needed zero
changes -- each daemon registers its own presence, so two tabs of the same
repo correctly see each other as peers.

Ctrl+C is deliberately UNBOUND (v0.85.0 -- see the BINDINGS comment on
DoxaApp), freed for the terminal emulator's own copy gesture over a
selection rather than claimed as a quit reflex; DOXA even pops Textual's
own default ``ctrl+c`` binding at init so nothing here answers it at
all. Quitting the whole window (every tab detached, or every tab
stopped) lives on the command palette (``action_quit`` /
``action_quit_stop``) instead; ending just the active tab is Ctrl+Q,
detaching just the active tab is Ctrl+W.

Each turn is a foldable Collapsible; its response streams as markdown
(Markdown.get_stream -- textual 5's append-only path for LLM deltas, no
full re-parse per chunk). Tool calls inside a turn render as compact
chips (name + one-line arg summary + duration + a check or cross) that
lazily expand into full args/result on first click -- the expensive JSON
pretty-printing only happens once, on demand, not for every tool call
that streams past -- and compact further behind ONE per-turn "Tool calls
(N)" fold (ToolCallsSection), created lazily on the first call.

Asyncio/Textual coexistence follows docs/phase0-findings.md §4 exactly:
``run_worker`` schedules the SDK-driving coroutine on Textual's own running
event loop (default ``thread=False``) -- proven by the phase-0
validation spike, whose result §4 records (the scripts themselves are gone;
the finding is what mattered)
proved out.

v0.34.0 split this file. It was 6,415 lines, 36% of the package, and every
feature of the last several releases landed in it -- which is also where
every rebase conflicted. The widgets moved to :mod:`doxa.ui` (one module
per surface: labels, transcript blocks, status line, dialogs, prompt) and
SessionPane's command, status-chip and engine-driven halves moved to
:mod:`doxa.session` as mixins on the same class. What is left here is
:class:`DoxaApp` -- the window, its tabs, its bindings -- and a facade that
re-exports every name this module exported before, unchanged, so no
importer and no CSS selector had to move with them.

The seams the split follows are the ones docs/plans/plugin-api.md names: the
command table (:data:`doxa.session.commands.PANE_COMMANDS`), the status
chips (:class:`doxa.session.chips.StatusChip`), the event dispatch map
(:data:`doxa.session.runtime.EVENT_RENDERERS`) and the model provider
(:mod:`doxa.providers`). Those are structures, not a loader: this release
gained no way to load third-party code, deliberately.

v0.56.0 (session resume) added one more spawn seam beside the two the
split already had: ``_resume_session_factory``, which builds a session
that CONTINUES a recorded conversation rather than starting one -- see
:meth:`DoxaApp.resume_session` for what it opens and for why a resume
gets its own tab instead of taking over the one it was asked from. Same
wrapping shape doxa.cli gives ``engine_factory`` and
``new_session_factory_at``, and the confirm dialog it opens is re-exported
through the facade below like every other name this module has ever
exported.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from functools import partial

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.fuzzy import Matcher
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Collapsible,
    Input,
    Markdown,
    OptionList,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)
from textual.widgets.markdown import MarkdownStream
from textual.widgets.option_list import Option

from . import auth as auth_mod
from . import clock as clock_mod
from . import commands as commands_mod
from . import config as config_mod
from . import errors as errors_mod
from . import identity as identity_mod
from . import images as images_mod
from . import keyboard as keyboard_mod
from . import naming as naming_mod
from . import notify as notify_mod
from . import paste as paste_mod
from . import peers as peers_mod
from . import providers as providers_mod
from . import tabsets as tabsets_mod
from . import transcript as transcript_mod
from . import version as version_mod
from . import worktrees as worktrees_mod
# The caps and the event record come from doxa.events; SessionEngine does
# NOT come from anywhere at import time. Importing doxa.engine pulls
# claude_agent_sdk (404 ms measured, 330 ms of it mcp.types building
# pydantic models) -- 74% of what it used to cost to import this module,
# paid before the first frame by every launch including `doxa doctor` and
# `doxa launcher install`, neither of which ever starts an agent. The
# three factories below import it when a session is actually built.
from .events import (  # noqa: F401 -- re-exported: callers use app.EngineEvent
    BELIEF_LIST_LIMIT,
    PENDING_LIST_LIMIT,
    EngineEvent,
)
from . import history as history_mod
from .history import SEARCH_PREFIX, SessionSearch
from .identity import tier_short  # noqa: F401 -- re-exported: the status
# line's plan label lives in doxa.identity now (precise local tier first,
# SDK subscriptionType second); app.py keeps the name callers already use.
from . import palette as palette_mod
from .palette import DoxaCommandProvider, PaletteEntry
from .peers import PeerSendError, age_secs


# -- compatibility facade ----------------------------------------------
#
# v0.34.0 moved the widgets into :mod:`doxa.ui` and two thirds of
# SessionPane into :mod:`doxa.session`; ``doxa.app`` did not stop being
# where the rest of the codebase looks for them. 39 modules, scripts and
# tests import 49 distinct names from here, ``doxa/theme.tcss`` matches
# several of the classes by TYPE selector, and the point of a refactor is
# that nothing downstream has to know it happened.
#
# So this module re-exports EVERY name it exported before the split, and
# the import block above is kept whole for the same reason: a module
# namespace that other modules read is a compatibility surface, and
# trimming it to "what DoxaApp itself still uses" would quietly break
# importers this file has no business knowing about. The guarantee is
# mechanical, not curated -- ``dir(doxa.app)`` is unchanged.
#
# v0.69.0 is the one exception the mechanism cannot cover: the beliefs
# browser's own six names (``BeliefRow``, ``BeliefsBrowserTab``,
# ``BrowserNote``, ``BrowserRow``, ``EvidenceTrail``, ``ProposalRow``,
# re-exported from the now-deleted ``doxa.ui.beliefs``) dropped OUT of
# this facade, because a removed feature has no module left to re-export
# from -- "nothing downstream has to know" holds for a refactor that
# moves code, not for one that deletes it.
from .session.pane import SessionPane  # noqa: F401
from .ui.dialogs import (  # noqa: F401
    _NEEDS_INPUT_DIGIT_KEYS,
    AboutDialog,
    BeliefInspector,
    ChipPicker,
    CloseWithTurnRunning,
    CompactConfirm,
    NeedsInputPopup,
    PermissionModeConfirm,
    ResumeConfirm,
    SlashComplete,
    TabRename,
    TabRenameCancelled,
)
from .ui.labels import (  # noqa: F401
    _belief_scope_label,
    _chip_span,
    _escape_markup,
    _fmt_age,
    _fmt_belief_row,
    _fmt_pending_row,
    _needs_input_summary,
    _one_line,
    as_proposal,
    belief_age_text,
    belief_created_text,
    belief_outcome_color,
    belief_outcome_kind,
    belief_outcome_tally,
    belief_outcome_text,
    belief_provenance,
    belief_sort_key,
    belief_stamp,
    belief_tooltip,
    belief_touched,
    NEVER_TESTED,
    OUTCOME_COLORS,
    OUTCOME_EVENTS,
    proposal_age_text,
    proposal_supersedes,
    proposal_target,
    proposal_text,
    proposal_tooltip,
    proposal_verdict,
    _pretty_key,
    _shrink,
    _subagent_label,
    _write_tab_class,
    _write_tab_label,
    app_bindings,
    CLICKABLE_CHIP_ACCENT,
    compose_tab_label,
    CONTEXT_UNAVAILABLE,
    context_breakdown_text,
    context_grid_text,
    context_sources_text,
    ctx_absolute_text,
    ctx_chip,
    ctx_text,
    CTX_ABSOLUTE_MIN_COLS,
    CTX_AMBER,
    CTX_AMBER_PCT,
    CTX_RED,
    CTX_RED_PCT,
    ellipsize,
    fmt_tokens,
    git_branch_symbol,
    help_text,
    MODE_BOLD,
    MODE_CHIP_MIN_COLS,
    MODE_COLOR,
    MODE_EXPLAIN,
    MODE_GLYPH,
    MODE_SHORT,
    mode_chip,
    mode_text,
    mode_tooltip,
    MODEL_ALIASES,
    provider_glyph,
    PROVIDER_GLYPH_COLOR,
    PROVIDER_GLYPHS,
    short_model,
    TAB_ISOLATION_MARKER,
    TAB_LABEL_MAX,
    TAB_MODEL_MIN,
    TAB_REPO_MIN,
)
from .ui.prompt import PromptInput  # noqa: F401
from .ui.statusline import ClockChip, GitLine, StatusBar  # noqa: F401
from .ui.transcript import (  # noqa: F401
    _clone_chip,
    _composed,
    _DrawnMark,
    _restore_pane_id,
    ArchivedSessionTab,
    BootBanner,
    ContextBlock,
    ErrorBlock,
    ImageBlock,
    ImageShowcaseBlock,
    mount_transcript,
    PeerMessageBlock,
    ReasoningSection,
    RestoreTabSpec,
    ShellBlock,
    SPINNER_FRAMES,
    SPINNER_MIN_INTERVAL,
    SubagentLine,
    SubagentTranscriptTab,
    SystemBlock,
    ThinkingMarker,
    ToolCallsSection,
    ToolChip,
    TurnBlock,
)


# -- v0.56.0: the error surface's app-level half -----------------------
#
# Textual 5.3.0 funnels EVERYTHING through ``App._handle_exception``:
# message-handler raises, compose/mount raises, idle handlers, next-
# callbacks (textual/message_pump.py:585,647,669,682), a widget's own
# ``_compose`` (widget.py:4521), a failed worker with the default
# ``exit_on_error=True`` (worker.py:384, wrapped in ``WorkerFailed``) and
# the compositor's paint loop (app.py:3656). ``textual/_compositor.py``
# has no ``except`` in it at all, so there is NO per-widget render guard
# to hook -- the paint of a whole frame is what fails, and the one place
# that hears about it is that method. Its own docstring says "Always
# results in the app exiting", which is precisely the behaviour the four
# defects of 2026-08-24 arrived as.
#
# So DoxaApp overrides it. See :meth:`DoxaApp._handle_exception`.

#: Frames that mean "this raise happened while Textual was PAINTING".
#: Read off the traceback rather than tracked with a flag, because a flag
#: would have to be set and cleared on a path that runs every frame -- and
#: nothing in this app is allowed to cost anything per frame (see GitLine's
#: docstring and _refresh_status's note on the idle-CPU regression).
RENDER_FRAMES = frozenset({
    "render", "render_line", "render_lines", "_render_content", "render_str",
    "get_content_width", "get_content_height", "__rich_console__",
    "__rich_measure__", "render_map", "_arrange",
})

#: The same failure this many times is a failure that is never going to
#: stop -- a widget raising on every paint, which is the reported crash's
#: exact shape. Quarantine (see :meth:`DoxaApp._quarantine`) normally ends
#: it at the source on the FIRST one; this is the backstop for when it
#: cannot, and it escalates to a clean fatal exit with a report rather than
#: letting the app spin repainting a block about its own inability to
#: paint.
FAILURE_ESCALATE = 25


def _stop_session(entry: "peers_mod.PeerInfo") -> bool:
    """End one live session by its registry entry -- the same path `doxa
    stop` takes: attach to its daemon socket, ask it to finalize (LORE
    review + index run there), let it exit. Returns whether it confirmed.

    Blocking, and deliberately so: callers hand it to a thread. A session
    without a daemon socket is in-process somewhere else and cannot be
    reached this way, which is reported as a failure rather than pretended
    away.

    Stayed in this module through the v0.34.0 split, on purpose. Its only
    caller moved (``/sessions kill``, now
    :meth:`doxa.session.commands.PaneCommandsMixin._kill_sessions`), but
    this is the APP-scope stop primitive -- the same one quit-stop and
    ``doxa stop`` reach -- and the suite swaps it by patching
    ``doxa.app._stop_session``. Moving the definition would have left that
    patch pointing at a name nothing reads, which fails as a silently
    passing test rather than an error. The caller imports it per call."""
    if not entry.daemon_socket:
        return False

    async def _stop() -> None:
        from .client import EngineClient

        client = EngineClient(entry.daemon_socket)
        await client.start()
        await client.stop()

    try:
        asyncio.run(_stop())
    except Exception:  # noqa: BLE001 -- a refusal is information, not a crash
        return False
    return True


class DoxaApp(App):
    """The DOXA terminal."""

    CSS_PATH = "theme.tcss"
    TITLE = "DOXA"
    # Ctrl+P (App.COMMAND_PALETTE_BINDING's default) opens the built-in
    # CommandPalette; DoxaCommandProvider feeds it doxa_commands() below.
    COMMANDS = App.COMMANDS | {DoxaCommandProvider}

    #: The id of the one fresh pane compose() adds when EVERY restored tab
    #: was archived (see its own comment) -- fixed and distinct from
    #: :func:`_restore_pane_id`'s ``restore-<session id>`` shape so it can
    #: never collide with a real one, and known up front so
    #: :meth:`_initial_active_tab_id` can name that pane before compose()
    #: has actually built it.
    _FALLBACK_PANE_ID = "restore-fallback-pane"
    # Ctrl+R: prefills "/search " -- the live session-search popup
    # (doxa/history.py) is the one search surface; the key is a shortcut to
    # it, not a second door.
    # instant BM25 over every indexed session, not a scrollback scan.
    # Ctrl+T/Ctrl+W: tab lifecycle (new same-repo session / close-detach).
    # Ctrl+C: deliberately NOT bound here, and explicitly UNBOUND from
    # Textual's own default (App.BINDINGS carries `Binding("ctrl+c",
    # "help_quit", system=True)`) in __init__ below, right after
    # super().__init__() populates self._bindings. Through v0.84.0 DOXA
    # bound it itself (one press = quit-detach ALL tabs, two = quit-stop),
    # on the theory that Textual's own binding did "nothing quit-shaped"
    # with the prompt permanently focused. Reported the other way round
    # from live use: "remove the binding CTRL+C to close the TUI ... i
    # want to be able to copy and paste" -- a raw Ctrl+C is exactly what a
    # terminal emulator needs to see, unclaimed, to treat it as its own
    # copy gesture over a selection rather than a byte for the foreground
    # app to consume. Quitting the whole window now lives on the command
    # palette ("Quit: detach"/"Quit: stop session", action_quit /
    # action_quit_stop below) and on Ctrl+Q run down to the last tab
    # (action_end_session); neither needs Ctrl+C at all.
    BINDINGS = [
        Binding("ctrl+p", "command_palette", "Command palette", show=False),
        Binding("ctrl+r", "history_search", "Search past sessions (/search)"),
        Binding("ctrl+comma", "settings", "Settings", show=False, priority=True),
        Binding("ctrl+t", "new_tab", "New tab", show=False, priority=True),
        Binding(
            "ctrl+w", "close_tab",
            "Close tab — DETACHES: the session keeps running",
            show=False, priority=True,
        ),
        # Ctrl+Q is Textual's own quit-the-app binding; this overrides it
        # deliberately and scopes it to the TAB. Quitting the whole window
        # is the command palette's job now (Ctrl+C no longer is one --
        # v0.85.0), and a key that ends one session must not be the same
        # key that ends all of them. priority=True: the focused Input
        # would otherwise eat it. (Terminal flow control does not:
        # Textual's Linux driver clears IXON/IXOFF, i.e. `stty -ixon`, so
        # Ctrl+Q reaches the app.)
        Binding(
            "ctrl+q", "end_session",
            "End this session (finalize now) and close its tab — on a "
            "read-only tab, just closes it",
            show=False, priority=True,
        ),
        Binding("ctrl+left", "prev_tab", "Previous tab", show=False, priority=True),
        Binding("ctrl+right", "next_tab", "Next tab", show=False, priority=True),
        # Permission-mode cycle (v0.42.0). The operator asked for Ctrl+Tab.
        # doxa.keyboard, this project's own measurement of what a terminal
        # can physically send, answers `unreachable_under_legacy("ctrl+tab")
        # -> True` and `("shift+tab") -> False` (back-tab, CSI Z, older than
        # the problem that module is about) -- so Ctrl+Tab is deliverable
        # only under the kitty protocol and Shift+Tab is deliverable
        # everywhere. That is almost certainly why Claude Code, which this
        # feature adopts, uses Shift+Tab too. Shift+Tab is therefore the
        # PRIMARY binding; Ctrl+Tab rides beside it so the operator's own
        # muscle memory works where the terminal supports it, and /help
        # marks it unsendable where it does not (v0.39.0's whole point --
        # the alternative is a documented key that silently does nothing).
        #
        # priority=True for the reason every global here needs it: the
        # prompt is a focused TextArea and would otherwise eat the key.
        #
        # What this COSTS: Textual's Screen binds shift+tab to
        # `app.focus_previous` (non-priority, `show=False`), so taking it
        # here removes REVERSE focus traversal. Forward traversal is
        # untouched and wraps, so every focusable widget stays reachable by
        # Tab alone -- nobody is stranded, they just go the long way round.
        # A three-widget pane makes that a cheap trade; it would not be on
        # a form.
        Binding(
            "shift+tab", "cycle_permission_mode",
            "Cycle permission mode (default → acceptEdits → plan)",
            show=False, priority=True,
        ),
        Binding(
            "ctrl+tab", "cycle_permission_mode",
            "Cycle permission mode (same as Shift+Tab; needs a "
            "kitty-protocol terminal)",
            show=False, priority=True,
        ),
    ]

    def __init__(
        self,
        cwd: str | None = None,
        model: str | None = None,
        engine_factory: "Callable[[], Any] | None" = None,
        new_session_factory: "Callable[[], Any] | None" = None,
        new_session_factory_at: "Callable[[str], Any] | None" = None,
        resume_session_factory: "Callable[[str, str], Any] | None" = None,
        restore_tabs: "list[RestoreTabSpec] | None" = None,
        restore_active_id: "str | None" = None,
        restore_report: "str | None" = None,
    ) -> None:
        super().__init__()
        # Explicitly UNBIND Ctrl+C -- see the BINDINGS comment above for
        # why. `self._bindings` (textual.dom.DOMNode.__init__) starts as a
        # COPY of the class-level merge of every base's BINDINGS, App's own
        # `Binding("ctrl+c", "help_quit", system=True)` included; simply
        # not re-declaring "ctrl+c" in DoxaApp.BINDINGS is not enough to
        # remove it; because Textual's merge overwrites per-key rather
        # than unions (DOMNode._merge_bindings), the App-level entry would
        # still be there, resolved and system-shown, unless something
        # actively drops it. This instance-level pop is that something --
        # done once, here, rather than per key-press, and pinned by
        # tests/test_app.py's own assertion that "ctrl+c" is absent from
        # the resolved set, not merely rebound to a no-op.
        self._bindings.key_to_bindings.pop("ctrl+c", None)
        self.cwd = cwd or os.getcwd()
        self.model = model
        # The daemon-split seam: engine_factory builds whatever the first
        # tab drives (in-process SessionEngine by default; an EngineClient
        # when doxa.cli attached us to a daemon). new_session_factory builds
        # a FRESH session -- the palette's "new session", and every Ctrl+T
        # tab -- distinct because an attach-flavored engine_factory must not
        # be re-invoked to mean "new".
        # Imported HERE, not at module scope: this is the first place a
        # SessionEngine can actually be built, and only when no factory was
        # supplied (doxa.cli supplies one for every daemon-backed launch, so
        # an attached TUI never reaches this import at all).
        def _in_process(**kwargs: Any) -> Any:
            # Resolved through THIS module's attribute, never imported
            # directly: `monkeypatch.setattr(doxa.app, "SessionEngine", ...)`
            # is how most of the suite substitutes a fake engine, and a
            # direct `from .engine import SessionEngine` here would walk
            # straight past the patch. Unpatched, the module __getattr__
            # below does the real import, at this moment and not before.
            import sys

            return getattr(sys.modules[__name__], "SessionEngine")(**kwargs)

        self._engine_factory = engine_factory or (
            lambda: _in_process(cwd=self.cwd, model=self.model)
        )
        self._new_session_factory = new_session_factory or self._engine_factory
        # v0.24.0's item 4 (repo picker): the SAME spawn primitive as
        # new_session_factory above, just parametrized by an EXPLICIT path
        # instead of this app's own launch cwd -- doxa.cli's own
        # new_session_factory/engine_factory closures already wrap
        # spawn_daemon/EngineClient this identically for the fixed-cwd
        # case; this is that SAME wrapping shape with one more argument,
        # not a second spawn implementation. Defaults to an in-process
        # SessionEngine at the given path, mirroring _engine_factory's own
        # default, so `--in-process` mode (and every existing test's
        # DoxaApp(...) call, which passes neither) gets the repo picker's
        # "open in a new tab" for free rather than a silent dead end.
        self._new_session_factory_at = new_session_factory_at or (
            lambda path: _in_process(cwd=path, model=self.model)
        )
        # v0.56.0 (/resume): the third member of the same family -- spawn
        # a session at an explicit path, except this one CONTINUES the
        # conversation already recorded under ``session_id`` instead of
        # starting a new one. Same wrapping shape doxa.cli gives the other
        # two (spawn_daemon + EngineClient); the default is an in-process
        # SessionEngine so `--in-process` mode and every existing
        # DoxaApp(...) in the suite get /resume rather than a dead end.
        #
        # The id is passed TWICE and that is deliberate, not redundant: as
        # session_id (this engine IS that session -- same transcript file,
        # same registry entry, same /search row) and as resume (it is
        # continuing it rather than starting it). See
        # SessionEngine._build_options for the measured reason those are
        # one id and not two.
        self._resume_session_factory = resume_session_factory or (
            lambda path, session_id: _in_process(
                cwd=path, model=self.model,
                session_id=session_id, resume=session_id,
            )
        )
        # Item D: tabs doxa.cli already resolved to LIVE daemons (never a
        # raw saved record -- see doxa.tabsets.resolve), opened in compose()
        # instead of the single default pane below. Empty/None for every
        # ordinary launch -- attach, `doxa new`, spawn-new, in-process.
        self._restore_tabs = list(restore_tabs or [])
        self._restore_active_id = restore_active_id
        self._restore_report = restore_report
        # Guards _persist_tabset while a multi-tab restore is still
        # connecting: each restored pane's boot() completion decrements
        # this (see _note_pane_booted), and only the LAST one to finish
        # actually writes -- one consolidated save reflecting every
        # restored tab, rather than one truncated save per tab in whatever
        # order their daemons happen to answer in.
        # Counted over the tabs that actually BOOT: an ArchivedSessionTab
        # has no engine and never reports in, so counting it here would
        # leave the guard permanently armed and the restored set never
        # persisted at all. An all-archived restore still boots the one
        # fresh pane compose() adds beside them, which is why the floor is
        # 1 rather than 0 whenever there is anything to restore.
        live_specs = sum(1 for spec in self._restore_tabs if not spec.archived)
        self._restore_pending = (
            live_specs if live_specs or not self._restore_tabs else 1
        )
        # Sessions detached (Ctrl+W / "/detach") THIS run: no longer a
        # mounted pane (its _session_id would drop out of panes() once
        # removed), but still running -- item D #4 says a detached session
        # STAYS in the persisted set. Keyed by session_id so a pane that
        # gets detached twice (should never happen) doesn't duplicate.
        self._detached_this_run: "dict[str, tabsets_mod.TabRecord]" = {}
        # Sessions ENDED (Ctrl+Q, the palette's "Quit: stop session") THIS
        # run: same reason as _detached_this_run above -- once the tab
        # closes, the pane drops out of _restorable_tabs()'s scan and a
        # LATER persist call (a new tab opened, another tab renamed) would
        # silently lose it without this. v0.56.0's session-id pinning is
        # what makes keeping it worth doing at all: the daemon really is
        # gone, but --resume can replay the transcript, so a saved id with
        # no live daemon behind it now comes back ARCHIVED (or resumed
        # outright, per resume_restored) at the next launch instead of
        # just disappearing. Through v0.55.0 _persist_tabset excluded any
        # pane marked _stopped outright ("nothing survives but the
        # transcript" was true then); this dict, plus dropping that
        # exclusion below, is the whole of what changed.
        self._ended_this_run: "dict[str, tabsets_mod.TabRecord]" = {}
        # Sessions REAPED on purpose THIS run (`/sessions kill <prefix>`,
        # `kill-detached`, the palette's own kill path) -- the one gesture
        # in this app that means "forget this conversation", so it is the
        # one thing _persist_tabset ever has to VETO rather than just fail
        # to record. Without this, a session Ctrl+W'd earlier and killed
        # later would resurrect at the next launch: _detached_this_run
        # never hears about the kill (it stops the daemon over its own
        # socket, straight from the peer registry, never through a pane),
        # and neither does an attached pane whose daemon a same-prefix kill
        # happened to hit. Checked by session_id, in _persist_tabset, for
        # every source a record could otherwise come from -- a mounted
        # pane, _detached_this_run and _ended_this_run alike.
        self._killed_this_run: "set[str]" = set()
        self._tab_serial = 0
        # v0.56.0's error surface. Three pieces of state, and each is one
        # of the three things the brief for this feature asked for:
        #
        #   failures       -- the QUERYABLE record. docs/plans/plugin-api.md's
        #                     failure policy is written in states ("this
        #                     plugin is disabled for the run"), and a
        #                     widget in a scrollback cannot answer a
        #                     settings modal's question. See
        #                     doxa.errors.FailureLog.
        #   _error_blocks  -- signature -> the block already on screen for
        #                     it, so a failure that repeats every paint
        #                     becomes one block with a tally instead of an
        #                     unbounded column of identical blocks.
        #   _reporting     -- the re-entrancy latch. Reporting a failure
        #                     mounts a widget, and mounting a widget can
        #                     fail; without this, one broken theme rule
        #                     would recurse until the stack ran out.
        #
        # Built here rather than lazily because _handle_exception can fire
        # before on_mount -- a raise inside compose() is one of the paths
        # Textual routes through it.
        self.failures = errors_mod.FailureLog()
        self._error_blocks: "dict[str, ErrorBlock]" = {}
        self._reporting = False
        # Set by `/update --restart`: doxa.cli re-execs after the app exits,
        # which is the only place that can -- exec'ing out from under a
        # running Textual app would leave the terminal in raw mode.
        self.restart_requested = False
        # Terminal-window focus, for "auto" desktop notifications (only
        # notify while you are NOT looking at the terminal). Init True: a
        # window is assumed focused until an AppBlur says otherwise, which
        # matters on a terminal with no focus-reporting -- see the
        # AppFocus/AppBlur handlers below and doxa/notify.py's "auto"
        # docstring for what that degrades to there.
        self.app_has_focus = True
        # One-shot "has this run already told you about an update" latch --
        # the background checker in on_mount fires at most once per launch.
        self._update_notified = False
        # Item Z (/about): what that SAME boot check found, kept so the
        # about dialog can say "update available" without running a second
        # `git fetch` of its own -- reuse, not a duplicate checker. Three
        # states, and the third is load-bearing: True (something to pull),
        # False (checked, nothing to pull), None (nobody has looked yet, or
        # the check failed silently the way it is designed to).
        self.update_available: "bool | None" = None
        # Bring lore_core's own in-process notification (staged-proposal
        # review, fired synchronously from doxa.engine's review path) in
        # line with the notify_lore toggle. Also re-run whenever the
        # settings modal saves (action_settings) -- the knob is live, not
        # boot-only.
        notify_mod.sync_lore_notify_env()
        # One sweep of the registry per launch: a crash can always leave a
        # presence file behind, so the fleet needs a sweeper that does not
        # depend on anything shutting down cleanly. Here rather than in a
        # worker because it must be done before the first status line reads
        # the registry -- it is a handful of stats and local connects, the
        # same class of startup cost as the image-mode probe below. Silently
        # cleaning is fine; silently IGNORING is not, so the count shows up
        # in the session's identity block when it is nonzero.
        self.swept_at_boot = peers_mod.sweep_stale()
        # Nothing in DOXA's chrome animates -- and that has to include the
        # animations DOXA did not write. Textual's own tab underline slides
        # to the newly-activated tab over 0.3 s (textual.widgets._tabs:
        # _highlight_active -> underline.animate), which is felt as lag when
        # arrowing through tabs and measured as ~290-345 ms of extra wall
        # time PER SWITCH. This one attribute is the supported off switch
        # for every Textual animation (App.animation_level, the same value
        # TEXTUAL_ANIMATIONS sets), and it is off for the same reason the
        # thinking marker stopped spinning: motion the user did not ask for
        # is paid for in their latency.
        self.animation_level = "none"
        # Settle the image-mode probe NOW, while this process still owns the
        # terminal: textual-image's TGP/sixel queries read their answer from
        # stdin, which Textual's own reader thread will grab the moment
        # App.run() starts (doxa/images.py's detection discipline note).
        images_mod.detect_mode()
        # Same window, same reason (v0.41.0): textual-image resolves the
        # terminal's CELL SIZE with an ESC[16t query whenever ioctl cannot
        # answer, and reads that reply off stdin as well. Settling it here
        # keeps the query out of the opening banner's first render AND
        # gives /img a measured cell size to report rather than a guess.
        images_mod.cell_size()
        # Same window, same reason (item O): doxa.keyboard asks the terminal
        # whether it grants the kitty keyboard protocol and reads the reply
        # off stdin. Textual requests the protocol but never reports whether
        # it was granted (doxa/keyboard.py's docstring, with the file:line
        # evidence), so this query is the only measurement there is -- and
        # once App.run() has started, the reader thread would eat its answer
        # and the probe would honestly report "unknown" forever.
        keyboard_mod.detect_protocol()
        # background (v0.29.0): $doxa-base (theme.tcss) needs ansi_color
        # True to actually reach the terminal as ESC[49m instead of being
        # rewritten into an approximated opaque RGB by Textual's own
        # ANSIToTruecolor filter -- see get_theme_variable_defaults below.
        self._apply_background()

    def get_theme_variable_defaults(self) -> dict[str, str]:
        """Feeds theme.tcss's one custom variable, $doxa-base -- the
        sanctioned extension point (App.get_theme_variable_defaults' own
        docstring: "allows applications to define their own variables").
        DOXA never registers a Theme of its own, so this default always
        wins: "#171512", byte-identical to every release before it,
        or the CSS keyword "ansi_default" -- Color(ansi=-1), which Rich
        renders as the raw SGR "default background" reset rather than any
        RGB, letting an already-transparent terminal show through (see the
        background setting's note in doxa/config.py for the terminal-side
        prerequisite this can't do alone)."""
        transparent = config_mod.background_mode() == "transparent"
        return {"doxa-base": "ansi_default" if transparent else "#171512"}

    def _apply_background(self) -> None:
        """The other half of $doxa-base: ansi_color must be True for
        ansi_default to reach the terminal unconverted (confirmed against
        the installed Textual -- with it False, the ANSIToTruecolor filter
        silently substitutes an approximated OPAQUE rgb, the opposite of
        what "transparent" promises). Safe to flip unconditionally: DOXA
        never sets self.theme, so this cannot collide with Textual's own
        "textual-ansi" built-in theme, and every widget theme.tcss already
        styles explicitly keeps its own literal color regardless (a
        DEFAULT_CSS `&:ansi` rule never outranks a CSS_PATH rule for the
        same property -- verified empirically, not assumed)."""
        self.ansi_color = config_mod.background_mode() == "transparent"

    # -- pane plumbing -----------------------------------------------

    def _tab_title(self, cwd: "str | None" = None) -> str:
        """The label a pane is BORN with -- model plus directory, no git.
        Defaults to this app's own ``cwd``; the repo picker's "open in a
        new tab" (item 4) passes the CHOSEN path instead, via
        :meth:`_make_pane_at`, so that tab is never born labelled with the
        wrong directory for one boot.

        The pane replaces it with its own ``auto_label`` the moment its
        engine and GitLine exist (one boot later); this exists so a tab
        never flashes a differently-shaped label on the way there. Two tabs
        on the same repo, branch and model do read alike, deliberately:
        they ARE alike, and the palette's tab section carries the session
        id that tells them apart."""
        self._tab_serial += 1
        name = Path(cwd or self.cwd).name or "session"
        return ellipsize(f"{short_model(self.model)} · {name}")

    def _make_pane(self, engine_factory: "Callable[[], Any]") -> SessionPane:
        return SessionPane(
            self._tab_title(), self.cwd, self.model, engine_factory,
        )

    def _make_pane_at(
        self, path: str, engine_factory: "Callable[[], Any]"
    ) -> SessionPane:
        """Item 4's own ``_make_pane``: same shape, an explicit ``path``
        standing in for this app's own ``cwd`` everywhere it matters (the
        born-title AND the pane's own ``cwd`` fallback -- see SessionPane.
        _boot's "engine cwd wins over the pane's own" comment for why the
        engine's real cwd is still what ultimately decides the GitLine/tab
        label once it boots; this is only the correct BEFORE-boot guess)."""
        return SessionPane(self._tab_title(path), path, self.model, engine_factory)

    async def open_tab_at(self, path: str) -> "str | None":
        """The repo picker's own spawn call (item 4): a fresh session tab
        rooted at an EXPLICIT path, via ``_new_session_factory_at`` -- the
        SAME spawn primitive Ctrl+T (:meth:`action_new_tab`) uses, just
        parametrized by path instead of this app's own launch cwd. Returns
        an error string on a bad path (never raises, never half-creates a
        tab); None on success.

        Activates AND focuses the new tab, in that order and both
        explicitly -- picking a repo out of the picker is as much "take me
        there" as Ctrl+T is, and since v0.38.0 neither activation nor
        focus arrives on its own (see :meth:`_focus_tab`)."""
        if not os.path.isdir(path):
            return f"not a directory: {path}"
        tabbed = self.query_one("#session-tabs", TabbedContent)
        pane = self._make_pane_at(path, lambda: self._new_session_factory_at(path))
        await tabbed.add_pane(pane)
        tabbed.active = pane.id or tabbed.active
        self._focus_tab(pane)
        return None

    async def resume_session(self, group: dict) -> "str | None":
        """Reopen a past conversation (v0.56.0). Returns a note to show the
        user, or None when there is nothing left to say.

        NEW TAB, not this pane. A resumed conversation is a DIFFERENT
        conversation from the one the active pane is holding -- its own
        history, its own cost, its own transcript file -- and taking the
        pane over would either end that session or orphan it, on a
        keystroke whose stated subject was some other session entirely.
        DOXA already has a verb for "replace what is in this tab"
        (``/clear``, which says so and finalizes first) and a verb for "go
        somewhere else" (the repo picker's open-in-a-new-tab, which this
        mirrors down to the mount/activate/focus order). Resume is the
        second kind. It is also the reversible kind: Ctrl+W closes the tab
        and nothing was lost, whereas an in-pane takeover has no undo.

        A RUNNING session is ATTACHED, never resumed. Resuming means
        handing ``--resume <id>`` to a second CLI process while the first
        is still alive on that conversation, which is two writers on one
        transcript and two daemons under one registry id -- so this
        detects it (the peer registry, the same reaped view ``doxa
        attach`` reads) and does the thing the user actually wanted
        instead: attaches to the live daemon, in a new tab, and says so.
        Not a silent substitution and not a fork; a different, correct
        act, named.

        Every refusal comes back as a STRING the caller prints. Nothing
        here raises, and nothing here half-creates a tab."""
        session_id = str(group.get("session_id") or "")
        cwd = str(group.get("cwd") or "")
        title = str(group.get("title") or "").strip()
        state, reason = await asyncio.to_thread(
            history_mod.resume_state, session_id, cwd
        )
        if state == history_mod.RESUME_RUNNING:
            return await self._attach_in_new_tab(session_id, title)
        if state != history_mod.RESUME_OK:
            return f"cannot resume {session_id[:8]} — {reason}"
        # Already open in this window? Then the answer is the tab that has
        # it, not a second one beside it -- and since it is open, it is
        # also running, which the registry check above would normally have
        # caught; this covers the in-process (no registry entry) case.
        for pane in self.panes():
            if pane._session_id == session_id:
                self._focus_tab(pane)
                self.query_one("#session-tabs", TabbedContent).active = (
                    pane.id or ""
                )
                return f"{session_id[:8]} is already open in this window."
        tabbed = self.query_one("#session-tabs", TabbedContent)
        pane = self._make_pane_at(
            cwd, lambda: self._resume_session_factory(cwd, session_id)
        )
        # Born labelled with the conversation's own title where it has
        # one: a resumed tab whose label says "opus · doxa" like every
        # other tab makes the user find it by elimination. The pane's
        # auto_label takes over one boot later, exactly as for any tab.
        if title:
            pane.custom_name = title[:40]
        # Read once by _boot, which reuses v0.32.0's transcript restore to
        # draw the prior turns -- see SessionPane._restore_transcript.
        pane._resume_from = session_id
        await tabbed.add_pane(pane)
        tabbed.active = pane.id or tabbed.active
        self._focus_tab(pane)
        return None

    async def _attach_in_new_tab(
        self, session_id: str, title: str
    ) -> "str | None":
        """A resume aimed at a session that is still RUNNING: attach to its
        daemon instead, in a new tab.

        A new tab rather than the palette's own in-pane attach
        (``_cmd_attach``, which switches the ACTIVE pane's engine): the
        user arrived here from a search result, not from "put something
        else in this tab", and the promise the confirm dialog makes is a
        new tab either way. Same non-destructive property -- whatever the
        current pane holds is still there afterwards.

        An in-process session with no daemon socket cannot be attached to
        at all, and is refused in words rather than quietly resumed: a
        second CLI on a live conversation is exactly what this branch
        exists to avoid."""
        from .client import EngineClient  # deferred: no daemon, no import

        entry = next(
            (e for e in peers_mod.read_registry() if e.session_id == session_id),
            None,
        )
        socket_path = getattr(entry, "daemon_socket", "") if entry else ""
        if not socket_path:
            return (
                f"{session_id[:8]} is still running, but not behind a daemon "
                "this window can attach to (an in-process session). it is "
                "not resumable while it runs — end it first, or use the "
                "window that owns it."
            )
        tabbed = self.query_one("#session-tabs", TabbedContent)
        pane = self._make_pane_at(
            str(getattr(entry, "cwd", "") or self.cwd),
            lambda: EngineClient(socket_path),
        )
        if title:
            pane.custom_name = title[:40]
        # An ATTACH, so the v0.32.0 restore path applies with its own
        # precondition intact: the daemon has a ring it may replay, and
        # the transcript is only drawn once it has agreed to skip it.
        pane._restore_transcript_wanted = True
        await tabbed.add_pane(pane)
        tabbed.active = pane.id or tabbed.active
        self._focus_tab(pane)
        return (
            f"{session_id[:8]} is still running — attached to it in a new "
            "tab rather than resuming it. a live conversation has one "
            "writer, and a second would fork it."
        )

    @property
    def active_pane(self) -> SessionPane | None:
        try:
            pane = self.query_one("#session-tabs", TabbedContent).active_pane
        except Exception:
            return None
        return pane if isinstance(pane, SessionPane) else None

    def panes(self) -> list[SessionPane]:
        return list(self.query(SessionPane))

    def archived_tabs(self) -> "list[ArchivedSessionTab]":
        """Restored tabs whose session is gone (v0.32.0) -- read-only
        transcript tabs, deliberately NOT part of :meth:`panes`, which
        every caller in this file reads as "tabs with a session behind
        them" and must keep reading that way."""
        return list(self.query(ArchivedSessionTab))

    def _active_tab(self) -> "SessionPane | ArchivedSessionTab | None":
        """The active tab when it is one restore CARES about -- either
        kind. ``active_pane`` stays SessionPane-only on purpose (every
        engine-touching caller depends on that); this is the one question
        that spans both."""
        try:
            tab = self.query_one("#session-tabs", TabbedContent).active_pane
        except Exception:
            return None
        return tab if isinstance(tab, (SessionPane, ArchivedSessionTab)) else None

    def _restorable_tabs(self) -> "list[Any]":
        """Every tab the persisted set is about, IN STRIP ORDER -- session
        panes and archived tabs interleaved exactly as the user sees them,
        because the record's order IS the tab-bar order it will restore
        to. Subagent transcript tabs are not sessions and never appear."""
        return [
            tab for tab in self.query(TabPane)
            if isinstance(tab, (SessionPane, ArchivedSessionTab))
        ]

    def _activation_pending(self) -> bool:
        """Has Textual decided WHICH tab is active yet?

        ``TabbedContent.active`` is a reactive that starts as the empty
        string and is only filled in when the inner ``Tabs`` widget's own
        mount handler picks a tab and its watcher posts ``TabActivated``
        -- several message-pump turns after the panes themselves exist and
        can already be running. ``active_pane`` is None for that whole
        window, which is a DIFFERENT statement from "a tab that is not a
        session is active": one is "not yet", the other is an answer.
        :meth:`_persist_tabset` is the caller that has to tell them
        apart."""
        try:
            return not self.query_one("#session-tabs", TabbedContent).active
        except Exception:
            return True

    # -- focus ownership (v0.38.0) ------------------------------------

    def _focus_tab(self, tab: "Any") -> None:
        """Put the keyboard into TAB. The ONE place that decides what
        "focused" means for a tab, and the one place that does it.

        Until v0.38.0 nothing called this because nothing had to: a
        ``SessionPane`` focused its own prompt in ``on_mount``, and since
        focusing a widget inside a ``TabPane`` also ACTIVATES that pane
        (``TabbedContent._on_tab_pane_focused``), activation was a side
        effect of mounting -- it landed whenever Textual got round to the
        mount, which is a race against anything else deciding which tab is
        active. Focus now follows EXPLICIT user intent instead, so every
        site that moves the user to a tab on purpose calls this: Ctrl+T
        (:meth:`action_new_tab`), Ctrl+←/→ (:meth:`_cycle_tab`), the
        palette's tab entries and the peer chip's jump
        (:meth:`_switch_to_tab`), the repo picker's new tab
        (:meth:`open_tab_at`), and startup/restore
        (:meth:`_activate_initial_tab`). :meth:`_on_tab_activated` calls
        it too -- a MOUSE click on a tab produces no key event and has no
        handler of its own to hang this on, so the event is the only hook
        that path has.

        Only a ``SessionPane`` has a prompt to focus. An
        ``ArchivedSessionTab`` is read-only and was not focused by the old
        mount-time path either (it is not a SessionPane, so it never had
        one); a ``SubagentTranscriptTab`` focuses its own scroll container
        at the point it is opened, which is that path's own explicit
        intent (doxa.session.runtime.open_transcript_tab)."""
        if isinstance(tab, SessionPane):
            with contextlib.suppress(Exception):
                tab.query_one("#prompt-input", PromptInput).focus()

    def _focus_active_tab(self) -> None:
        """:meth:`_focus_tab` for whichever tab is active RIGHT NOW --
        for the callers that set ``TabbedContent.active`` by id and would
        otherwise have to look the pane back up themselves. Safe to call
        immediately after that assignment: ``active`` is a plain reactive,
        so ``active_pane`` resolves synchronously once it is set (it is
        only the INITIAL value that arrives late -- see
        :meth:`_activation_pending`)."""
        with contextlib.suppress(Exception):
            tabbed = self.query_one("#session-tabs", TabbedContent)
            self._focus_tab(tabbed.active_pane)

    @on(events.DescendantFocus)
    def _hold_focus_for_a_blocking_dialog(self, event: events.DescendantFocus) -> None:
        """While the active pane has a needs-input dialog up, the keyboard
        stays on that pane's prompt (v0.43.0).

        This is the net under :meth:`_focus_tab`, and it exists because the
        needs-input dialog is the one surface in this app where losing
        focus is not a cosmetic annoyance but a WEDGED SESSION: the dialog
        is ``can_focus = False`` and answered only through
        ``PromptInput.on_key``, the agent is blocked until it is answered,
        and Esc -- the documented way out -- is one of the keys that stops
        working. Measured routes into that state, each of them one ordinary
        gesture: clicking the transcript to scroll back and read before
        deciding (``#block-list`` is a focusable ``VerticalScroll``, and
        its own up/down bindings then eat the arrows), pressing Tab (the
        prompt's ``tab_behavior`` is "focus"), and -- the reported one --
        clicking the BLINKING TAB when it is already the active tab, which
        focuses the tab strip and posts no ``TabActivated``, so
        :meth:`_on_tab_activated`, the only hook the mouse path has, never
        runs.

        Not a retreat from v0.38.0: focus still moves only on explicit
        intent, and a request that has stopped the session is intent. The
        rule is narrow on purpose -- only while a dialog is actually open,
        only for the ACTIVE pane, and only on that pane's own screen, so a
        pushed modal keeps its own focus. :class:`ChipPicker` and
        :class:`TabRename` are the two widgets on this screen that
        deliberately take focus for themselves, so they are exempt rather
        than fought with -- an editor whose caret got pulled out from
        under it would be a new defect, not a fix. Mouse-wheel scrolling
        never needed focus and is unaffected."""
        pane = self.active_pane
        if pane is None or isinstance(event.widget, (ChipPicker, TabRename)):
            return
        with contextlib.suppress(Exception):
            if not pane.query_one("#needs-input-popup", NeedsInputPopup).is_open:
                return
            prompt = pane.query_one("#prompt-input", PromptInput)
            if event.widget is prompt or event.widget.screen is not prompt.screen:
                return
            prompt.focus()

    # -- item D: persisted tab set ------------------------------------

    def _note_pane_booted(self, pane: "SessionPane") -> None:
        """A pane's session id is stable now (first boot), or has just
        CHANGED (switch_engine -- a fresh /model session or a palette
        attach landing in this same tab). Either way the persisted set
        needs to know -- except mid-restore, where every restored pane
        boots concurrently and each one's session_id becomes known at an
        unpredictable moment: persisting after the FIRST to finish would
        write a truncated set missing every tab still connecting. This
        counts restored panes down and only calls through once all of
        them (if any) have reported in, so the very first write already
        reflects the complete restored set."""
        if self._restore_pending > 0:
            self._restore_pending -= 1
            if self._restore_pending > 0:
                return
        self._persist_tabset()

    def _persist_tabset(self) -> None:
        """Snapshot the CURRENT tab set to $DOXA_HOME/tabsets/<scope>.json
        (doxa.tabsets.save) -- called on every tab-set change (open,
        rename, close-detach, close-stop, app exit). Unconditional on the
        restore_tabs SETTING (that only gates whether a later launch
        READS this file, see doxa.tabsets.enabled/config's own note) --
        gated only on a restore still being in flight (_restore_pending).

        THREE sources, merged: panes still mounted (in tab-bar order --
        LIVE or _stopped alike, see below), _detached_this_run (sessions
        Ctrl+W'd out of the strip earlier this run, which keep running and
        therefore STAY in the set per item D #4) and _ended_this_run (the
        v0.60.0 counterpart: sessions Ctrl+Q'd or palette-stopped out of
        the strip, which do NOT keep running but stay in the set anyway --
        see that dict's own docstring for why). A mounted pane's own
        _stopped flag no longer excludes it here either, for the identical
        reason: through v0.55.0 a stopped pane was dropped on the spot,
        because ending a session really did mean losing the tab for good.
        v0.56.0 pinned the doxa session id to the CLI's own
        (SessionEngine._build_options), which is what makes --resume able
        to replay a transcript DOXA itself indexed -- so "the daemon is
        gone" stopped being the same fact as "the tab is gone", and
        excluding a _stopped pane here was still doing the OLD job. The
        one thing that still has to win over all three sources is an
        EXPLICIT reap (_killed_this_run, `/sessions kill`) -- checked
        below wherever a record could otherwise slip through.

        Cross-repo exclusion (item 4's repo picker, reconciled against
        this method): every tab used to share ONE scope by construction
        (Ctrl+T only ever spawned in THIS app's own cwd) -- the repo
        picker's "open in a new tab" is the first way a single window
        can host a tab rooted in a DIFFERENT repo. Such a pane's own
        session is scoped elsewhere already (its daemon's PeerHost wrote
        ITS OWN registry entry under ITS OWN scope key), so writing its
        id into THIS window's tabset file would be dead weight at best --
        doxa.tabsets.resolve cross-checks a saved id against
        list_daemons(scope_key=<this file's own scope>), and a daemon
        registered under a DIFFERENT scope key is invisible to that
        check, so the entry could only ever resolve to "gone" and get
        silently skipped, never to the wrong session. Excluded here
        rather than relying on that safe-but-wasteful fallback."""
        if self._restore_pending > 0:
            return
        scope = peers_mod.main_repo_root_of(self.cwd) or self.cwd
        active_tab = self._active_tab()
        tabs: "list[tabsets_mod.TabRecord]" = []
        seen: set[str] = set()
        active_id: "str | None" = None
        # Tab-strip order, and BOTH kinds of restorable tab: a live
        # SessionPane, and (v0.32.0) an ArchivedSessionTab, which is one of
        # the user's open tabs too and must not evaporate on the next
        # restart just because the session behind it already has.
        for tab in self._restorable_tabs():
            if isinstance(tab, ArchivedSessionTab):
                if tab.session_id in seen:
                    continue
                seen.add(tab.session_id)
                tabs.append(tab.as_record())
                if tab is active_tab:
                    active_id = tab.session_id
                continue
            pane = tab
            sid = pane._session_id
            if not sid or sid in seen or sid in self._killed_this_run:
                continue
            # A _stopped pane (Ctrl+Q, palette stop) falls straight through
            # to the same record the live branch below builds -- see this
            # method's own docstring for why that is now correct rather
            # than an oversight. Its engine is gone (stop() cleared it), so
            # the cwd read below already falls back to pane.cwd, same as
            # it does for a Ctrl+W-detached pane at this exact call site.
            pane_cwd = str(getattr(pane.engine, "cwd", None) or pane.cwd)
            pane_scope = peers_mod.main_repo_root_of(pane_cwd) or pane_cwd
            if pane_scope != scope:
                continue
            seen.add(sid)
            tabs.append(tabsets_mod.TabRecord(sid, pane.custom_name, pane_cwd))
            if pane is active_tab:
                active_id = sid
        if (
            active_id is None
            and self._restore_active_id is not None
            and self._restore_active_id in seen
            and self._activation_pending()
        ):
            # The write-ordering race, fixed in v0.38.0. A restore's FIRST
            # write is triggered by the last restored pane reporting its
            # session id (_note_pane_booted), and a pane can boot before
            # Textual has resolved which tab is active: TabbedContent.
            # active is still the empty string it starts as, active_pane
            # is therefore None, no tab matches `is active_tab`, and
            # active_id would be saved as null. Nothing writes again until
            # the tab set next changes, so that one racy write is what
            # lands on disk -- the tabs restore complete and in order, on
            # the WRONG tab, silently. Measured as 1 failure in 80 runs of
            # tests/test_tabsets.py's restore test with four suites in
            # parallel; the signature is a null active id, never a wrong
            # one.
            #
            # In exactly that window the record we restored FROM is the
            # answer, and it cannot be stale: no tab is active yet, so the
            # user cannot have switched away from one. The
            # _activation_pending() guard is what keeps this from firing
            # later, when a None active_id is a real answer -- a subagent
            # transcript tab is active, and no session tab is.
            active_id = self._restore_active_id
        for record in (*self._detached_this_run.values(), *self._ended_this_run.values()):
            if record.session_id in seen or record.session_id in self._killed_this_run:
                continue
            seen.add(record.session_id)
            tabs.append(record)
        with contextlib.suppress(Exception):
            tabsets_mod.save(scope, tabs, active_id)

    @property
    def engine(self) -> Any | None:
        """The ACTIVE tab's engine handle -- the single-session accessors
        (palette callbacks, history insertion, tests) read the app the way
        they always did; multi-tab awareness lives in panes()."""
        pane = self.active_pane
        return pane.engine if pane is not None else None

    @property
    def _git(self) -> GitLine | None:
        pane = self.active_pane
        return pane._git if pane is not None else None

    def _refresh_status(self) -> None:
        pane = self.active_pane
        if pane is not None:
            pane._refresh_status()

    # -- the error surface (v0.56.0) ---------------------------------

    def _failure_surface(self) -> "VerticalScroll | None":
        """WHERE a failure gets drawn: the active session's transcript,
        falling back to any session's.

        The active tab is the right answer when there is one -- a failure
        belongs next to whatever the user was doing when it happened. The
        fallback exists because the active tab is often not a SessionPane
        at all (a subagent transcript, an archive) and because
        ``active_pane`` is None for the whole window before Textual
        has decided which tab is active (see :meth:`_activation_pending`),
        which is exactly when a boot-time failure fires. A failure with
        nowhere at all to go is not dropped -- see
        :meth:`report_failure`."""
        pane = self.active_pane or next(iter(self.panes()), None)
        if pane is None:
            return None
        try:
            return pane.query_one("#block-list", VerticalScroll)
        except Exception:  # noqa: BLE001 -- a pane mid-compose has no list yet
            return None

    def report_failure(self, failure: "errors_mod.Failure") -> None:
        """Make one failure VISIBLE. The single door; everything else in
        this file and every future caller goes through it.

        Order matters and is the opposite of the obvious one: the block is
        mounted FIRST and the log written second. Persisting first would
        let a read-only home directory or a full disk decide whether the
        user gets told, and the visible copy is the one that is not
        optional. :func:`doxa.errors.append` returning None is therefore a
        degraded bug report, never a hidden failure.

        Never swallows. Every path through this method ends with the
        failure recorded in :attr:`failures` and, if there is any surface
        at all, on screen; if there is not, it goes to the terminal
        (:meth:`_fail_fatally`) rather than nowhere. A caught exception
        that only reaches a log file is worse than the crash it replaced,
        because the tests still pass and the user still learns nothing.

        Repeats collapse. The block for a signature already on screen gets
        a tally, not a sibling -- and past :data:`FAILURE_ESCALATE` of them
        this stops being a recoverable failure by definition and exits with
        a report.

        **What a future plugin loader calls.** This, with an explicit
        ``origin`` of ``plugin:<name>`` (see
        :func:`doxa.errors.from_exception` and
        :func:`doxa.errors.policy_failure` -- the second is there for the
        ``text()`` time budget, which breaks a promise without raising).
        Reading ``app.failures.failed("plugin:jira")`` afterwards is the
        "disabled for the run" state the spec's failure policy needs. This
        release builds neither the loader nor the allowlist, deliberately;
        it builds the surface they would fail into."""
        if self._reporting:
            # Re-entered: the failure below happened WHILE drawing a block
            # about another one. Persist it (the log is the surface that
            # cannot itself fail into this method) and get out.
            errors_mod.append(failure)
            return
        self._reporting = True
        try:
            count = self.failures.record(failure)
            if failure.fatal or count > FAILURE_ESCALATE:
                self._fail_fatally(failure, repeats=count)
                return
            existing = self._error_blocks.get(failure.signature)
            # ``is_attached`` rather than ``is_mounted``: ``Widget.mount``
            # registers the child synchronously and only DISPATCHES its
            # Mount event a pump cycle later, and the repeat case is
            # exactly the one that fires several times inside one cycle (a
            # widget raising on every paint). Keying on is_mounted would
            # have grown one block per repeat until the pump caught up --
            # a column of identical blocks, which is what this is for.
            # Attachment is also the right question when the tab holding
            # the block has been closed: a detached block is gone from the
            # screen, so the next failure gets a fresh one.
            if existing is not None and existing.is_attached:
                # A repeat: tally the header and stop. Writing the same
                # scrubbed traceback to the log on every paint of a broken
                # widget would spend the whole rotation budget on one
                # failure and push out everything that came before it.
                existing.bump(count)
                return
            errors_mod.append(failure)
            block_list = self._failure_surface()
            if block_list is None:
                # Nothing on screen can hold it, and a failure nobody can
                # SEE is the defect this release exists to remove -- so it
                # goes to the terminal instead of nowhere.
                self._fail_fatally(failure, repeats=count, logged=True)
                return
            block = ErrorBlock(failure)
            self._error_blocks[failure.signature] = block
            block_list.mount(block)
            block_list.scroll_end(animate=False)
        finally:
            self._reporting = False

    def report_exception(
        self,
        error: BaseException,
        *,
        origin: "str | None" = None,
        context: str = "",
        fatal: bool = False,
    ) -> None:
        """:meth:`report_failure` for a caught exception -- the form every
        ``except`` block in this codebase that wants to STOP swallowing
        should reach for. Scrubbing and attribution happen in
        :func:`doxa.errors.from_exception`; nothing raw reaches a
        widget."""
        self.report_failure(
            errors_mod.from_exception(
                error, origin=origin, context=context, fatal=fatal,
            )
        )

    def _culprit_widget(self, error: BaseException) -> "tuple[Any, bool]":
        """``(widget, was_painting)`` read off the traceback.

        ``was_painting`` is true when any frame in the stack is one of
        :data:`RENDER_FRAMES` or lives in Textual's compositor -- the
        reported crash's shape (``textual_image`` querying stdin from
        inside ``__rich_console__`` while Textual owned the terminal).
        ``widget`` is the DEEPEST frame whose ``self`` is a mounted Widget
        that is neither this app nor a Screen: for that crash it is the
        image widget itself, which is the thing that has to stop being
        asked to paint.

        Never raises: a frame that cannot be read is skipped rather than
        blamed, because this runs inside the handler of last resort."""
        from textual.screen import Screen
        from textual.widget import Widget

        tb = getattr(error, "__traceback__", None)
        frames = []
        while tb is not None:
            frames.append(tb.tb_frame)
            tb = tb.tb_next
        painting = False
        culprit: "Any" = None
        for frame in reversed(frames):
            try:
                name = frame.f_code.co_name
                filename = str(frame.f_code.co_filename)
                candidate = frame.f_locals.get("self")
            except Exception:  # noqa: BLE001 -- unreadable frame, not a suspect
                continue
            if name in RENDER_FRAMES or filename.endswith("_compositor.py"):
                painting = True
            if (
                culprit is None
                and isinstance(candidate, Widget)
                and not isinstance(candidate, (App, Screen))
                and candidate.is_mounted
            ):
                culprit = candidate
        return culprit, painting

    def _quarantine(self, error: BaseException) -> str:
        """Stop a painting widget from being painted again, and say what
        was done. Returns the context line for the block.

        This is the containment Textual does not offer. ``_compositor.py``
        has no exception handling whatsoever, so a widget that raises while
        rendering does not fail alone -- it takes the whole FRAME with it,
        every frame, forever. Merely surviving the raise would leave the
        app alive and unable to draw, spinning on a failure it re-hits on
        the next repaint; the tally in :meth:`report_failure` would count
        to :data:`FAILURE_ESCALATE` and give up.

        So the widget is hidden. ``display = False`` takes it out of the
        layout entirely, which is the one thing that ends the loop at its
        source, and the error block that replaces it says so -- half a
        widget silently missing is one of the four defects this release is
        about, and a whole widget silently missing would be the same
        defect wearing a fix.

        Only for a RENDER failure, and only for a widget we can actually
        name. A message-handler raise gets no quarantine: hiding an
        arbitrary widget because a keystroke handler threw would be a
        second defect, not containment.

        DOXA owns the general containment here. The specific cause of the
        reported crash -- textual-image probing stdin for the terminal's
        cell size during a paint -- is fixed where it belongs, in
        :mod:`doxa.images`/:mod:`doxa.banner` (the ``fix/banner-not-
        rendering`` work), and the two do not overlap: that one stops the
        probe happening, this one stops ANY render raise being fatal."""
        culprit, painting = self._culprit_widget(error)
        if not painting:
            return "handling an event"
        if culprit is None:
            return "painting the screen"
        name = type(culprit).__name__
        with contextlib.suppress(Exception):
            culprit.display = False
            return f"painting {name} — hidden so the rest of DOXA keeps working"
        return f"painting {name}"

    def _handle_exception(self, error: Exception) -> None:
        """Textual's one funnel for everything unhandled, overridden.

        In Textual 5.3.0 this method receives message-handler raises,
        compose/mount raises, idle and next-callback raises, failed workers
        (as ``WorkerFailed``, because ``run_worker``'s ``exit_on_error``
        defaults to True) and the compositor's own paint failures -- the
        file:line evidence is in the module-level note beside
        :data:`RENDER_FRAMES`. Its stock behaviour is documented as
        "Always results in the app exiting", and every one of the four
        defects of 2026-08-24 that did not simply vanish arrived that way:
        as a bare traceback on a terminal whose TUI had gone.

        Worker failures are the case worth stating separately, because the
        brief for this release asked whether a dying worker reaches
        anything today. It does -- it reaches HERE, and here used to mean
        the app exits. DOXA starts a worker for nearly everything
        (``_boot``, ``_peer_pump``, every slash command, ``_derive_once``,
        the update check), so "a worker died" was indistinguishable from
        "DOXA crashed", and a worker cancelled at teardown is a routine
        event. Now a failed worker is a visible block and the session it
        belonged to stays usable.

        A deliberate exit is not a defect and never arrives here:
        ``KeyboardInterrupt`` and ``SystemExit`` derive from
        BaseException, not Exception, so Textual's own ``except Exception``
        clauses do not catch them and this signature cannot receive them.
        (Ctrl+C itself is not bound to anything in this file as of
        v0.85.0 -- see the BINDINGS comment on DoxaApp -- so it no longer
        even reaches this question in the ordinary case; the guard below
        is belt-and-braces for whatever path DOES still raise one, a
        pre-app-start KeyboardInterrupt included, and hands it straight
        back to Textual/Python rather than swallowing it.)

        Fatal is still possible and still SEEN: a failure with nowhere to
        draw itself, or one that will not stop repeating, exits through
        :meth:`_fail_fatally`, which prints the same information to the
        terminal on the way out."""
        if not isinstance(error, Exception):  # pragma: no cover -- see docstring
            super()._handle_exception(error)
            return
        from textual.worker import WorkerFailed

        if isinstance(error, WorkerFailed):
            # Unwrap: WorkerFailed is Textual's envelope, and the
            # traceback that matters (and the attribution read off it) is
            # the worker body's own.
            inner = getattr(error, "error", None)
            if isinstance(inner, BaseException):
                error = inner  # type: ignore[assignment]
            context = "running a background task"
        else:
            context = self._quarantine(error)
        self.report_failure(
            # No explicit origin: nothing routed HERE knows whose code it
            # is, and errors.origin_of reads it off the traceback. An
            # explicit origin is what a CALLER passes -- a plugin loader
            # that knows it just called into plugin:jira, or the
            # needs-input path in doxa.session.runtime that knows what it
            # was doing.
            errors_mod.from_exception(error, context=context)
        )

    def _fail_fatally(
        self, failure: "errors_mod.Failure", repeats: int = 1, logged: bool = False,
    ) -> None:
        """DOXA cannot carry on -- so say the same thing on the way out.

        A user who has to file a bug report should not have to reconstruct
        which DOXA, which terminal and which operation from a bare Python
        traceback. ``/about`` already assembles exactly that block
        (:func:`doxa.version.about_text` -- version, sha, interpreter,
        textual, agent SDK, lore and where it was loaded from, platform,
        keyboard protocol, config path), it is the block the about dialog's
        copy door puts on the clipboard, and reusing it here means the
        crash report and the thing the user would have pasted are the same
        text rather than two divergent almost-truths.

        Scrubbed, like everything else that leaves this process: the
        traceback in ``failure.detail`` went through
        :func:`doxa.errors.scrub` at construction, and the about block is
        DOXA's own measurements rather than anything model- or
        environment-derived. Notably this replaces Textual's own
        ``_fatal_error``, which renders
        ``rich.traceback.Traceback(show_locals=True)`` -- the frame locals
        are where a credential actually lives, and printing them into a
        terminal a user is about to screenshot is the leak this release
        must not ship.

        ``self._exception`` is set so that a test harness still learns the
        app died (``App.run_test`` re-raises it at shutdown) -- a fatal
        failure that a suite could pass straight through would make this
        module a place errors hide, which is exactly what it is for."""
        if not logged:
            errors_mod.append(failure)
        tally = f"  (×{repeats})" if repeats > 1 else ""
        report = "\n".join((
            f"DOXA stopped: {failure.headline()}{tally}",
            "",
            version_mod.about_text(self.update_available),
            "",
            failure.detail or "(no further detail)",
            "",
            f"This was also written to {errors_mod.log_path()}",
        ))
        self._return_code = 1
        if self._exception is None:
            # The same two lines Textual's own _handle_exception writes, so
            # App.run_test's teardown re-raise and Pilot's exception wait
            # both behave exactly as they would have without this override.
            self._exception = errors_mod.FatalFailure(failure.headline())
            self._exception_event.set()
        with contextlib.suppress(Exception):
            from rich.text import Text

            self.panic(Text(report))

    def compose(self) -> ComposeResult:
        yield BeliefInspector()  # hidden stub, palette-toggled
        yield ClockChip()  # upper-right, own layer -- see theme.tcss
        with TabbedContent(id="session-tabs", initial=self._initial_active_tab_id()):
            if self._restore_tabs:
                # Item D: one tab per resolved saved tab, IN SAVED ORDER --
                # never the single default pane below. v0.32.0 mixes two
                # kinds in that one order: a live spec reattaches its
                # daemon (SessionPane), an archived one has no daemon left
                # to reattach and renders its transcript read-only
                # (ArchivedSessionTab). The report block (if any) rides on
                # the first LIVE pane -- an archived tab already opens with
                # a block of its own explaining what it is.
                report_placed = False
                # No pane arms a mount-time focus any more (v0.38.0): a
                # restored pane mounts in the BACKGROUND, and which tab
                # ends up active and focused is decided once, explicitly,
                # in _activate_initial_tab. v0.23.0's "three restored tabs
                # always land on the last one" defect was this same
                # entanglement -- one pane was allowed to focus on mount so
                # that exactly one activation-by-side-effect happened. The
                # side effect is gone, so the workaround is too.
                for spec in self._restore_tabs:
                    if spec.archived:
                        yield ArchivedSessionTab(
                            spec.session_id,
                            spec.cwd or self.cwd,
                            self._tab_title(spec.cwd or self.cwd),
                            pinned_name=spec.pinned_name,
                            id=_restore_pane_id(spec.session_id),
                            # v0.56.0: read-only is now the FALLBACK, so
                            # the tab says which of the reasons it was.
                            resume_note=spec.resume_note,
                        )
                        continue
                    pane = SessionPane(
                        self._tab_title(), self.cwd, self.model,
                        spec.engine_factory, id=_restore_pane_id(spec.session_id),
                    )
                    if spec.pinned_name:
                        pane._initial_pinned_name = spec.pinned_name
                    if spec.resume:
                        # v0.56.0: this tab's session had ENDED, and it is
                        # coming back LIVE, continuing that conversation
                        # (doxa.cli decided that; the engine_factory above
                        # spawns with --resume). Its scrollback comes from
                        # the same transcript file a reattach reads, minus
                        # the backlog-skip precondition -- a freshly
                        # spawned daemon has no ring to replay on top. See
                        # SessionPane._restore_transcript.
                        pane._resume_from = spec.session_id
                    else:
                        # v0.32.0: this pane's scrollback comes from the
                        # session's persisted transcript, not the daemon's
                        # 512-frame ring (see
                        # SessionPane._restore_transcript).
                        pane._restore_transcript_wanted = True
                    pane._restore_cwd = spec.cwd
                    if not report_placed:
                        pane._boot_report = self._restore_report
                        report_placed = True
                    yield pane
                if not report_placed:
                    # Every resolved tab was archived: the window would
                    # otherwise have no session in it at all -- no prompt,
                    # nothing Ctrl+W could close without closing the app.
                    # One fresh tab alongside the archives, carrying the
                    # report, is the same answer doxa.cli's own "everything
                    # is dead" branch gives. Explicit id -- _FALLBACK_
                    # PANE_ID -- so _initial_active_tab_id (which runs
                    # BEFORE this pane exists) can already name it.
                    pane = self._make_pane(self._engine_factory)
                    pane.id = self._FALLBACK_PANE_ID
                    pane._boot_report = self._restore_report
                    yield pane
            else:
                pane = self._make_pane(self._engine_factory)
                # Item D fallback: every saved tab was dead (nothing to
                # reattach), but doxa.cli still has a report to show --
                # "restored 0, skipped N" -- on the one fresh tab it spawned
                # instead. self._restore_report is None on every ordinary
                # launch, so this is a no-op there.
                pane._boot_report = self._restore_report
                yield pane

    @on(TabbedContent.TabActivated)
    def _on_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self._jump_tab_marker()
        pane = self.active_pane
        if pane is not None:
            # Looking at this tab now clears every "you missed something"
            # signal: the done-unseen dot from a turn that finished while
            # you were elsewhere, any attention blink (and its timer --
            # set_needs_input(False) is what stops that), and the staged-
            # proposal tint (the block announcing it is right there in the
            # transcript you just opened).
            pane._set_tab_class("-done-unseen", False)
            pane.set_needs_input(False)
            pane.set_staged(False)
            # Focus here as well as at every keyboard site (v0.38.0), for
            # the ONE path that has no keyboard site to hang it on: a
            # MOUSE click on a tab header produces no key event and runs no
            # action of ours -- Textual activates the tab and this is the
            # only thing we hear about it. Every other caller of
            # _focus_tab has already focused by the time this arrives, so
            # this is a no-op refocus for them.
            self._focus_tab(pane)
        elif isinstance(event.pane, SubagentTranscriptTab):
            # Same "you're looking at it now" clear, for a transcript tab
            # that finished (and picked up -done-unseen) while it sat in
            # the background -- it carries no -working/-attention, so
            # -done-unseen is the only class it ever needs cleared.
            event.pane._set_tab_class("-done-unseen", False)

    # -- window focus, for "auto" desktop notifications ---------------

    @on(events.AppFocus)
    def _on_app_focus(self, event: events.AppFocus) -> None:
        self.app_has_focus = True

    @on(events.AppBlur)
    def _on_app_blur(self, event: events.AppBlur) -> None:
        self.app_has_focus = False

    # -- renaming a tab in place -------------------------------------

    @on(events.Click)
    def _on_click_maybe_rename(self, event: events.Click) -> None:
        """Double-clicking a tab header turns it into a field.

        Textual counts click chains for us (``event.chain``), so this needs
        no timing of its own -- and a SINGLE click keeps meaning "switch to
        this tab", untouched."""
        if event.chain != 2:
            return
        from textual.widgets import Tab

        widget = event.widget
        while widget is not None and not isinstance(widget, Tab):
            widget = widget.parent
        if widget is None:
            return
        pane = self._pane_for_tab(widget)
        if pane is None:
            return
        event.stop()
        self.run_worker(self._start_rename(pane), group="rename")

    def _pane_for_tab(self, tab: Any) -> "SessionPane | None":
        from textual.widgets._tabbed_content import ContentTab

        pane_id = ContentTab.sans_prefix(tab.id or "")
        for pane in self.panes():
            if pane.id == pane_id:
                return pane
        return None

    async def _start_rename(self, pane: "SessionPane") -> None:
        """Mount the editor in the tab's own slot and hide the tab behind
        it, so the label is edited where the label IS."""
        if self.query("#tab-rename"):
            return  # one rename at a time
        with contextlib.suppress(Exception):
            tabbed = self.query_one("#session-tabs", TabbedContent)
            tab = tabbed.get_tab(pane.id or "")
            editor = TabRename(pane.id or "", pane.display_name())
            editor.styles.width = max(len(editor.value) + 4, 14)
            await tab.parent.mount(editor, before=tab)
            tab.display = False
            editor.focus()

    @on(Input.Submitted, "#tab-rename")
    def _on_rename_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        pane_id = getattr(event.input, "pane_id", "")
        pane = next((p for p in self.panes() if p.id == pane_id), None)
        if pane is not None:
            # Empty means "no name", which is how a pinned tab is un-pinned.
            pane.set_custom_name(event.value)
        self._end_rename(pane_id)

    @on(TabRenameCancelled)
    def _on_rename_cancelled(self, event: TabRenameCancelled) -> None:
        event.stop()
        self._end_rename(event.pane_id)

    def _end_rename(self, pane_id: str) -> None:
        with contextlib.suppress(Exception):
            tabbed = self.query_one("#session-tabs", TabbedContent)
            tabbed.get_tab(pane_id).display = True
        for editor in list(self.query(TabRename)):
            editor.remove()
        pane = next((p for p in self.panes() if p.id == pane_id), None)
        if pane is not None:
            with contextlib.suppress(Exception):
                pane.query_one("#prompt-input", PromptInput).focus()

    def _jump_tab_marker(self) -> None:
        """Put the active-tab underline at its destination on THIS frame.

        Textual's ``Tabs`` slides the marker: ``watch_active`` calls
        ``_highlight_active(animate=True)``, which arms a 0.02 s timer and
        then animates ``highlight_start``/``highlight_end`` over 0.3 s.
        ``animation_level = "none"`` (set in __init__) already takes the
        no-animate branch, but that branch still defers the move to
        ``call_after_refresh`` -- one frame late. Measured: the slide cost
        ~290-345 ms of WALL time per switch on top of the switch itself,
        constant regardless of scrollback, which is exactly the "tab
        switching is laggy" report.

        So the marker is placed directly, from the same geometry Textual's
        own mover reads. Failure is not an error: if Textual's internals
        move, this degrades to the built-in (still un-animated) path rather
        than breaking tab switching."""
        with contextlib.suppress(Exception):
            from textual.widgets import Tabs
            from textual.widgets._tabs import Underline

            tabs = self.query_one("#session-tabs", TabbedContent).query_one(Tabs)
            active = tabs.query_one("#tabs-list > Tab.-active")
            start, end = active.virtual_region.shrink(
                active.styles.gutter
            ).column_span
            if end <= start:
                return  # geometry not laid out yet: leave the marker alone
            underline = tabs.query_one(Underline)
            underline.highlight_start = start
            underline.highlight_end = end

    # -- tab lifecycle -----------------------------------------------

    async def action_new_tab(self) -> None:
        """Ctrl+T: a fresh session in the same repo scope (exactly
        new_session_factory -- a new daemon under the CLI, a new in-process
        engine otherwise), attached in a new tab and focused.

        All three steps are stated here, in order: mount, activate, focus.
        Focus used to arrive on its own, from the pane's own mount, and
        activation used to arrive as a side effect of THAT -- so the
        keystroke's outcome was really a race with Textual's mount
        scheduling (v0.38.0)."""
        tabbed = self.query_one("#session-tabs", TabbedContent)
        pane = self._make_pane(self._new_session_factory)
        await tabbed.add_pane(pane)
        tabbed.active = pane.id or tabbed.active
        self._focus_tab(pane)

    async def action_close_tab(self) -> None:
        """Ctrl+W: close-DETACH the active tab -- its daemon keeps running,
        by design (reattach via the palette's attach picker or `doxa
        attach`). The cheapest outcome to recover from is what a close key
        does; ENDING a session is Ctrl+Q, which says so.

        The three non-session tabs take the SAME key to a much simpler
        path -- :meth:`_close_read_only_tab`, which Ctrl+Q now shares.

        Closing the last SESSION tab closes the app, on the same detach
        semantics."""
        pane = self.active_pane
        if pane is not None:
            await self._close_pane(pane, terminate=False)
            return
        await self._close_read_only_tab()

    async def _close_read_only_tab(self) -> bool:
        """Close the active tab when it is one of the two READ-ONLY kinds,
        and say whether it closed one.

        The two -- a subagent transcript (SubagentTranscriptTab) and a
        restored archive (ArchivedSessionTab) -- share the property that
        makes this one method: neither is a session. ``self.active_pane``
        is SessionPane-only and comes back None for both, so there is no
        daemon to detach, no engine to stop and no turn-in-flight question
        to ask. Each still needs ITS own teardown (a transcript drops the
        owning pane's reference so reopening builds a fresh one; an
        archive re-persists the tab set so closing it is what takes it
        out) -- hence the dispatch rather than one remove_pane call.

        There is always at least one SessionPane beside them, so neither
        is ever "the last tab" and neither reaches the close-the-app
        branch :meth:`_close_pane` falls back to.

        Extracted in v0.58.0 so BOTH close keys reach it. Ctrl+W has
        called this path since these tabs existed; Ctrl+Q did not, and
        stopped dead on them (see :meth:`_end_session`). The boolean is
        the load-bearing part of the signature: it lets a caller tell
        "closed a read-only tab" from "there was nothing here I know how
        to close", which is what a NEW tab kind will hit -- v0.46.0 shipped
        the (now-removed) beliefs browser unclosable for exactly one
        release by not having a shared answer here."""
        active: "Any" = None
        with contextlib.suppress(Exception):
            active = self.query_one("#session-tabs", TabbedContent).active_pane
        if isinstance(active, SubagentTranscriptTab):
            await self._close_transcript_tab(active)
            return True
        if isinstance(active, ArchivedSessionTab):
            await self._close_archived_tab(active)
            return True
        return False

    async def _close_archived_tab(self, tab: "ArchivedSessionTab") -> None:
        """Ctrl+W on an archived tab (v0.32.0): nothing to detach, nothing
        to stop -- the session ended before this window opened. It closes,
        and closing it is the ONE way to take it out of the persisted set:
        an archived tab the user leaves open comes back at the next launch
        exactly like a live one, which is the whole point.

        Never the last tab: compose() guarantees a SessionPane beside any
        archive, so this never reaches the close-the-app branch."""
        with contextlib.suppress(Exception):
            await self.query_one("#session-tabs", TabbedContent).remove_pane(
                tab.id or ""
            )
        self._persist_tabset()

    def action_end_session(self) -> None:
        """Ctrl+Q: END this tab's session -- finalize NOW (LORE review +
        index run daemon-side), socket closed, presence file removed, the
        daemon child reaped -- and close the tab. Nothing survives but the
        transcript.

        Tab-scoped, never app-scoped: quitting the whole window lives on
        the command palette ("Quit: detach" / "Quit: stop session"), not
        on this key. A turn IN FLIGHT is the one case this refuses to
        decide by itself -- killing work silently is not a thing a
        keystroke should do -- so it asks; an idle session ends without a
        prompt.

        On a tab with NO session to end -- a subagent transcript, a
        restored archive -- it closes the tab, and that is the whole of
        what it does. Through v0.56.0 it did NOTHING there: ``_end_session``
        looked for a SessionPane, found None and returned, so the user sat
        on a read-only tab pressing the key they had been taught closes
        tabs. Same defect class as the (then still shipping) beliefs
        browser and Ctrl+W in v0.46.0, and it now takes the same shared
        answer, :meth:`_close_read_only_tab`.

        That does NOT make the two keys the same key. The distinction is
        about the SESSION -- Ctrl+W leaves it running, Ctrl+Q finalizes it
        -- and on a tab with no session there is no distinction left to
        draw: the archive's session ended before the window opened, the
        subagent's transcript is a copy. Two keys agreeing where the
        difference is meaningless is not ambiguity, it is the absence of a
        trap. What would be wrong is Ctrl+Q ending the tab's OWNING
        session -- a key aimed at the
        visible tab must never reach past it -- and it does not.

        Dispatched into a worker because awaiting a modal's answer
        (push_screen_wait) is only legal from one."""
        self.run_worker(self._end_session(), group="close")

    async def _end_session(self) -> None:
        pane = self.active_pane
        if pane is None:
            await self._close_read_only_tab()
            return
        if pane.turn_in_flight:
            choice = await self.push_screen_wait(CloseWithTurnRunning())
            if choice == "cancel":
                return
            if choice == "detach":
                await self._close_pane(pane, terminate=False)
                return
        await self._close_pane(pane, terminate=True)

    async def action_detach_tab(self) -> None:
        """`/detach` -- the named form of what Ctrl+W does."""
        await self.action_close_tab()

    async def _close_transcript_tab(self, tab: "SubagentTranscriptTab") -> None:
        """Ctrl+W (or the palette's Close tab) on a subagent transcript
        tab: no engine to stop, no daemon to detach -- just remove it and
        drop the owning pane's own reference to it."""
        tab.owner._transcript_tabs.pop(tab.call_id, None)
        with contextlib.suppress(Exception):
            await self.query_one("#session-tabs", TabbedContent).remove_pane(
                tab.id or ""
            )

    def _record_after_close(
        self, pane: "SessionPane", target: "dict[str, tabsets_mod.TabRecord]"
    ) -> None:
        """Scope-checked capture into ``_detached_this_run`` or
        ``_ended_this_run``, called BEFORE the caller's ``remove_pane``
        takes ``pane._session_id`` out of :meth:`panes`'s own scan with it
        -- the two dicts a pane leaving the strip this run can still need
        to be found in, and the same question either way: was this tab
        ever part of THIS window's own repo-scoped persisted set to begin
        with?

        Scope-checked (item 4's repo picker reconciliation, same reasoning
        as _persist_tabset's own exclusion): a cross-repo tab (opened via
        the repo picker) was never part of THIS window's own repo-scoped
        persisted set, so detaching or ending it must not add it there
        either -- its daemon's PeerHost already wrote its own registry
        entry under its own scope key."""
        if not pane._session_id:
            return
        pane_cwd = str(getattr(pane.engine, "cwd", None) or pane.cwd)
        pane_scope = peers_mod.main_repo_root_of(pane_cwd) or pane_cwd
        app_scope = peers_mod.main_repo_root_of(self.cwd) or self.cwd
        if pane_scope != app_scope:
            return
        target[pane._session_id] = tabsets_mod.TabRecord(
            pane._session_id, pane.custom_name,
        )

    async def _close_pane(self, pane: "SessionPane", terminate: bool) -> None:
        """One close path, two dispositions. Closing the LAST tab closes the
        app on the same disposition -- a window with no tabs is not a
        window, and the session's fate must not depend on tab arithmetic.

        A closing session takes its OWN open transcript tabs down with it
        first -- they have no engine and nothing left to route events into
        once the session that spawned their subagents is gone."""
        for tab in list(pane._transcript_tabs.values()):
            await self._close_transcript_tab(tab)
        if terminate:
            note = await pane.stop()
            if note:
                # The pane itself is about to be removed (or the whole app
                # quits, below) -- a toast is screen-level, not pane-level,
                # so it survives the tab it was about -- unlike a SystemBlock
                # mounted in the closing pane's own block list, which the
                # user would never get a chance to see.
                self.notify(note, severity="information", timeout=10)
            # v0.60.0: this session STAYS in the persisted set even though
            # its tab is about to leave the strip below -- Ctrl+Q finalizes
            # the SESSION, not the record of having had the tab. See
            # _ended_this_run's own docstring for why that is now the
            # right read of "end this session" and _record_after_close for
            # the scope check this shares with the detach branch below.
            self._record_after_close(pane, self._ended_this_run)
        else:
            # Detached ON PURPOSE: it is no longer this window's to end, so
            # a later quit-stop leaves it running.
            pane.detached_on_purpose = True
            await pane.detach()
            # Item D #4: this session STAYS in the persisted tab set even
            # though its tab is about to leave the strip below -- record it
            # here, before remove_pane takes pane._session_id out of
            # panes()'s own scan with it.
            self._record_after_close(pane, self._detached_this_run)
        self._persist_tabset()
        if len(self.panes()) == 1:
            await App.action_quit(self)
            return
        await self.query_one("#session-tabs", TabbedContent).remove_pane(
            pane.id or ""
        )

    def _cycle_tab(self, delta: int) -> None:
        """Ctrl+← / Ctrl+→ -- move to the neighbouring tab, wrapping. One
        tab wraps to itself, which is the correct no-op.

        Focuses the tab it lands on, right here (v0.38.0). That used to be
        left to _on_tab_activated, one message-pump turn later -- and a
        pane mounting in the meantime could focus itself and take the
        activation back, which is exactly what made tests/test_tab_status.
        py's done-unseen test flaky after a Ctrl+T/Ctrl+← pair."""
        panes = self.panes()
        if len(panes) < 2:
            return
        tabbed = self.query_one("#session-tabs", TabbedContent)
        ids = [p.id for p in panes if p.id]
        try:
            index = ids.index(tabbed.active)
        except ValueError:
            index = 0
        tabbed.active = ids[(index + delta) % len(ids)]
        # Textual's reactive watcher has already moved the `-active` class
        # by the time that assignment returns, so the marker can be placed
        # NOW -- one message-pump turn earlier than TabActivated arrives.
        # Held Ctrl+←/→ is the case this exists for.
        self._jump_tab_marker()
        self._focus_active_tab()

    def action_prev_tab(self) -> None:
        self._cycle_tab(-1)

    def action_next_tab(self) -> None:
        self._cycle_tab(1)

    def action_cycle_permission_mode(self) -> None:
        """Shift+Tab (and Ctrl+Tab where the terminal can send it): step
        the ACTIVE pane's session to the next permission mode.

        The key can only ever reach this SESSION's own ring --
        default → acceptEdits → plan → default. That is not a check
        performed here; it is a property of
        :func:`doxa.engine.next_cycle_mode`, which is total over that
        tuple and cannot return anything outside it whatever this pane's
        current mode happens to be. Putting the boundary in a pure
        function rather than in this handler is what makes it testable as
        a security assertion instead of as a UI behavior.

        A session parked on a gated mode (reached through ``/mode`` and a
        confirmation) is off the ring, so one press brings it home to
        ``default`` -- which is also the one thing a user reaching for a
        key to get out of bypass would want it to do.

        Dispatch goes through ``_cmd_mode`` like every other door, so the
        transcript records the switch in the same words a typed
        ``/mode`` would, and there is exactly one place that talks to the
        engine."""
        from .engine import next_cycle_mode

        pane = self.active_pane
        if pane is None or pane.engine is None:
            return
        # The ring is per-session since v0.58.0: a session not spawned
        # with the arming flag has no bypassPermissions in it, so the key
        # steps straight from auto back to default rather than offering a
        # mode the CLI would refuse.
        target = next_cycle_mode(
            getattr(pane.engine, "permission_mode", None),
            bool(getattr(pane.engine, "bypass_armed", False)),
        )
        pane.run_worker(pane._cmd_mode(target), group="command")

    def _switch_to_tab(self, pane_id: str) -> None:
        """Take me to that tab, by id -- the palette's open-tab entries
        and a peer chip's jump to a session already open here. Same three
        beats as every other explicit switch: activate, move the marker,
        focus (v0.38.0)."""
        with contextlib.suppress(Exception):
            self.query_one("#session-tabs", TabbedContent).active = pane_id
        self._jump_tab_marker()
        self._focus_active_tab()

    # -- palette (Ctrl+P) --------------------------------------------

    def doxa_commands(self) -> "list[PaletteEntry]":
        """The DOXA palette surface, as :class:`~doxa.palette.PaletteEntry`
        rows in display order.

        Rebuilt from live state on EVERY palette open (that is what the
        provider calls), so a tab opened or closed while the palette is up
        cannot leave a stale row behind.

        The order is the one doxa/palette.py documents: New tab, then the
        open tabs in tab-bar order, then the commands in the registry's own
        groups, then the attachable sessions. App-level entries that have
        no registry row (Close tab, the quits, the inspector) declare a
        registry GROUP like everything else -- there is one grouping in
        this app, not one per surface."""
        entries: list[PaletteEntry] = [
            PaletteEntry(
                palette_mod.SECTION_NEW,
                "New tab",
                "Open a fresh DOXA session in this repo scope in a new tab (ctrl+t)",
                self._cmd_new_tab,
            ),
        ]
        # Open tabs, LEFT TO RIGHT -- the palette mirrors the tab bar, so
        # the order the user sees along the top is the order they get here.
        # The active tab is marked rather than hidden: "where am I" is as
        # much a question as "where do I want to go".
        active = self.active_pane
        for position, pane in enumerate(self.panes()):
            if not pane.id:
                continue
            sid = str(getattr(pane.engine, "session_id", "") or "")[:8]
            is_active = pane is active
            entries.append(PaletteEntry(
                palette_mod.SECTION_TABS,
                f"{pane._title}" + (f"  ({sid})" if sid else "")
                + ("  · active" if is_active else ""),
                "This tab (already active)" if is_active
                else "Switch to this tab",
                partial(self._switch_to_tab, pane.id),
                sort_key=(position, ""),
            ))
        # App-level entries: no slash row of their own, but the SAME
        # registry groups -- they sort after the registry's rows inside a
        # group (sort_key (1, label) vs the registry's (0, name)).
        for group, label, help_text, callback in (
            ("Panes & tabs", "Close tab",
             "Close-detach the current tab; its session keeps running (ctrl+w)",
             self._cmd_close_tab),
            ("Session", "New session",
             "Start a fresh DOXA session and switch THIS tab to it",
             self._cmd_new_session),
            ("Panes & tabs", "Belief inspector: toggle",
             "Show/hide the belief inspector pane (stub until Phase 3)",
             self.action_toggle_inspector),
            ("Session", "Quit: detach",
             "Close this TUI; every session daemon keeps running "
             "(reattach with `doxa attach`)",
             self.action_quit),
            ("Session", "Quit: stop session",
             "Finalize the current tab's session now (LORE review + index) "
             "and close its tab",
             self._cmd_stop_active),
        ):
            entries.append(PaletteEntry(group, label, help_text, callback))
        # Slash registry, second surface: every row that declares a palette
        # label appears here too (doxa/commands.py is the single list --
        # the prompt's autocomplete reads the same rows), keeping
        # commands.ordered()'s sequence inside its group. Rows that need
        # arguments PREFILL the prompt instead of running blind.
        for index, command in enumerate(commands_mod.ordered()):
            if not command.palette:
                continue
            callback = (
                partial(self._cmd_prefill, command.name + " ")
                if command.palette_prefill
                else partial(self._cmd_run_slash, command.name)
            )
            entries.append(PaletteEntry(
                command.group, command.palette, command.summary, callback,
                sort_key=(0, f"{index:03d}"),
            ))
        # Attach: live daemon-hosted sessions from the shared peer/daemon
        # registry, newest first, never any session already open in a tab.
        open_ids = {
            str(getattr(p.engine, "session_id", "") or "") for p in self.panes()
        }
        for position, entry in enumerate(peers_mod.list_daemons()):
            if entry.session_id in open_ids:
                continue
            entries.append(PaletteEntry(
                palette_mod.SECTION_ATTACH,
                f"Attach: {entry.title} ({entry.session_id[:8]})",
                f"Reattach to the live session in {entry.cwd} (in this tab)",
                partial(self._cmd_attach, entry),
                sort_key=(position, ""),
            ))
        return palette_mod.ordered_entries(entries)

    def action_command_palette(self) -> None:
        """Ctrl+P -- DOXA's palette screen, which is Textual's plus the
        section headers (doxa/palette.py). Overridden rather than
        configured because Textual's App pushes its own CommandPalette
        class by name."""
        from textual.command import CommandPalette

        if self.use_command_palette and not CommandPalette.is_open(self):
            self.push_screen(palette_mod.DoxaPalette(id="--command-palette"))

    def _cmd_new_tab(self) -> None:
        self.run_worker(self.action_new_tab(), group="tabs")

    def _cmd_close_tab(self) -> None:
        self.run_worker(self.action_close_tab(), group="tabs")

    def _cmd_new_session(self) -> None:
        pane = self.active_pane
        if pane is not None:
            pane.run_worker(
                pane.switch_engine(self._new_session_factory),
                exclusive=True, group="switch",
            )

    def _cmd_attach(self, entry: peers_mod.PeerInfo) -> None:
        """The palette's "Attach: ..." entries and the sessions chip's own
        picker (:meth:`doxa.session.chips.PaneChipsMixin._select_session_row`)
        both land here -- attach to a live, DETACHED daemon session, in a
        NEW tab, through :meth:`_attach_in_new_tab` (the same door /resume
        already sends a still-running session through).

        v0.60.0, reported and MEASURED, not assumed: through v0.56.0 this
        switched the ACTIVE pane's engine in place instead (item 2's own
        original spec, two releases before /resume settled "a pane holds
        a live conversation; attaching is never a takeover"). Driven end
        to end against a real SessionDaemon over a real socket before this
        changed: the connection itself worked (the pane's engine really
        did become an EngineClient for the right session id) -- what did
        not was the CONTENT. switch_engine() never sets
        _restore_transcript_wanted, so a reattached pane's history came
        from the daemon's in-memory event ring alone (the pre-v0.32.0
        mechanism, capped at 512 frames -- see SessionPane._restore_
        transcript's own docstring for the exact defect that capacity
        already caused once). A session detached long enough to have
        scrolled its ring past that came back BLANK, in the tab the user
        was already looking at -- which is indistinguishable from
        "nothing happened" even though a socket really did connect.
        _attach_in_new_tab sets that flag and opens a tab with nothing
        else in it to confuse the result with.

        Both callers already exclude a session open in ANOTHER tab of
        this window from their own candidate list before this is ever
        reached (the palette's own Attach section, and _select_session_
        row's separate switch-instead branch above it) -- this is the one
        attach primitive, never re-derives that exclusion."""
        self.run_worker(self._cmd_attach_worker(entry), group="switch")

    async def _cmd_attach_worker(self, entry: "peers_mod.PeerInfo") -> None:
        note = await self._attach_in_new_tab(entry.session_id, entry.title)
        if note:
            # App-scoped, not pane-scoped: unlike /attach (typed IN a
            # pane, which can print its own note as a SystemBlock there),
            # this is reached from the palette and the sessions chip alike
            # with no "the pane this is about" to write into -- a toast is
            # the one surface both share.
            self.notify(note, severity="information", timeout=10)

    def _cmd_stop_active(self) -> None:
        self.run_worker(self._stop_active(), group="tabs")

    async def _stop_active(self) -> None:
        """Palette 'Quit: stop session', tab-scoped: finalize the ACTIVE
        tab's session NOW; the tab closes with it. Stopping the only tab
        closes the app (the Phase 2 behavior, per-app == per-tab then).

        The palette's own name for Ctrl+Q -- same disposition, same v0.60.0
        answer: stays in the persisted set (_record_after_close), because
        finalizing a session is no longer the same fact as losing its tab
        (see _ended_this_run's docstring)."""
        pane = self.active_pane
        if pane is None:
            return
        note = await pane.stop()
        if note:
            self.notify(note, severity="information", timeout=10)
        self._record_after_close(pane, self._ended_this_run)
        self._persist_tabset()
        if len(self.panes()) == 1:
            await App.action_quit(self)
            return
        await self.query_one("#session-tabs", TabbedContent).remove_pane(
            pane.id or ""
        )

    def _cmd_run_slash(self, name: str) -> None:
        """Palette -> the ACTIVE pane's slash handler. One dispatch path for
        both surfaces: the palette never reimplements a command."""
        pane = self.active_pane
        if pane is not None:
            pane.run_worker(pane._run_command(name), group="command")

    def _cmd_prefill(self, text: str) -> None:
        pane = self.active_pane
        if pane is None:
            return
        prompt = pane.query_one("#prompt-input", PromptInput)
        prompt.value = text  # the setter also moves the cursor to the end
        prompt.focus()

    def action_history_search(self) -> None:
        """Ctrl+R: prefill ``/search `` in the active tab's prompt, which
        IS the search surface (doxa/history.py's popup opens on that exact
        prefix). The modal overlay this used to push is gone: one key, one
        slash command and one palette entry now land on the same place, so
        there is nothing left for two search paths to disagree about."""
        self._cmd_prefill(SEARCH_PREFIX)

    def action_settings(self) -> None:
        """Ctrl+, / /settings / the palette's Settings entry -- one modal,
        three doors. Saving re-reads the affected surfaces immediately
        (the status line's branch glyph and the plan chip are the two that
        show without a new session); knobs the ENGINE reads take effect on
        its next read, which is per turn by construction."""
        from .settings import SettingsScreen

        def _saved(saved: "bool | None") -> None:
            if not saved:
                return
            config_mod.invalidate()
            notify_mod.sync_lore_notify_env()
            for pane in self.panes():
                pane._refresh_status()
            with contextlib.suppress(Exception):
                self.query_one(ClockChip).reconfigure()
            self._apply_background()
            self.refresh_css(animate=False)  # re-reads $doxa-base -- live

        engine = self.engine
        self.push_screen(
            SettingsScreen(
                session_model=getattr(engine, "model", None),
                account=getattr(engine, "account", None) or {},
            ),
            callback=_saved,
        )

    def action_setup(self) -> None:
        """/setup / the palette's Setup entry -- check state, fix findings
        one at a time. Also what a genuine first launch auto-triggers (see
        on_mount): the marker that stops it recurring is consumed there,
        not here, so this method itself is identical whether it was
        summoned on demand or by the app."""
        from .setup import ACTION_OPEN_SETTINGS, SetupScreen

        def _done(result: "str | None") -> None:
            config_mod.invalidate()
            notify_mod.sync_lore_notify_env()
            for pane in self.panes():
                pane._refresh_status()
            if result == ACTION_OPEN_SETTINGS:
                self.action_settings()

        self.push_screen(SetupScreen(), callback=_done)

    def _initial_active_tab_id(self) -> str:
        """Which tab should be ACTIVE on first mount -- decided here,
        before anything is mounted, and handed to ``TabbedContent``'s own
        ``initial=`` (see :meth:`compose`) rather than set reactively
        later from :meth:`_activate_initial_tab`, which is what this
        replaces and is where the OLD long version of this comment lived.

        **The race this closes, measured rather than assumed.** Textual's
        ``Tabs`` widget defaults itself to its first tab on ITS OWN mount
        (``Tabs._on_mount``) whenever nothing else names one at
        construction, and that default reaches ``TabbedContent.active`` as
        a MESSAGE -- ``Tabs.TabActivated``, handled by
        ``TabbedContent._on_tabs_tab_activated`` -- not a synchronous
        write. The old code set ``tabbed.active`` directly from
        ``App.on_mount``, which runs later than ``Tabs._on_mount`` and so
        USUALLY reads as "after the default, and therefore winning." But
        the queued default-tab message does not evaporate because
        something else wrote ``active`` in between: whenever THAT message
        is finally processed, its handler sets ``active`` to whatever tab
        IT named, unconditionally -- silently overwriting an explicit
        choice made after it was queued but before it was handled. Under
        load (CI: 1 failure; never reproduced locally at any rep count)
        that two-writer race can resolve either way, and the FakeEngine
        specs in ``tests/test_tabsets.py`` resolve fast enough for it to
        matter. The exact failure -- ``'sid-1' == 'sid-2'`` -- is a WRONG
        id, not the null v0.38.0 already fixed (:meth:`_persist_tabset`'s
        own ``_activation_pending`` guard), because this is a different
        mechanism: two competing writers, not one write that never came.

        The fix is not to win that race, it is to not run it: if the
        CORRECT tab is the one ``Tabs`` defaults to in the first place --
        because it was TOLD to, via ``initial=`` -- there is no stray
        message from a wrong default left to land later. One writer, one
        value, converges to the right answer however long it takes to
        propagate.

        Selection rule, unchanged from the method this replaces: the saved
        active tab if the record named one -- live pane or archived tab
        alike, it is where the user was -- otherwise the first SESSION
        spec. Read off :attr:`_restore_tabs` rather than mounted panes,
        because nothing is mounted yet; :data:`_FALLBACK_PANE_ID` is the
        one case that needs a name before it exists -- every restored tab
        archived, so :meth:`compose` adds one fresh pane under that fixed
        id, purely so this method has something to call it."""
        if not self._restore_tabs:
            return ""  # one pane; Tabs' own first-tab default is already right
        if self._restore_active_id:
            for spec in self._restore_tabs:
                if spec.session_id == self._restore_active_id:
                    return _restore_pane_id(spec.session_id)
        for spec in self._restore_tabs:
            if not spec.archived:
                return _restore_pane_id(spec.session_id)
        return self._FALLBACK_PANE_ID

    def _activate_initial_tab(self) -> None:
        """Startup's own explicit FOCUS -- activation itself is decided
        earlier now, by :meth:`_initial_active_tab_id` (handed to
        ``TabbedContent`` as ``initial=`` in :meth:`compose`, before
        anything mounts), so this is left with the half of the old
        combined method that a widget only has AFTER it exists: putting
        the keyboard on it.

        An ordinary launch, and a restore with no saved active tab, used
        to get their active tab and their focus as a side effect of the
        first pane focusing its own prompt on mount -- v0.38.0 removed
        that (see :meth:`_focus_tab`'s own docstring) because "the first
        prompt is focused because a widget we do not own happens to
        announce itself" is exactly the implicitness split-panes needs the
        startup leaf not to have. So this still runs from ``App.on_mount``
        and still says explicitly what v0.38.0 wanted said: focus the tab
        that ended up active.

        The lookup here is independent of whether ``TabbedContent.active``
        has itself finished propagating by this point (that is a SEPARATE
        question from the one ``_initial_active_tab_id`` answers, and this
        method does not need it resolved) -- it re-derives the same target
        by id, the same way :meth:`_initial_active_tab_id` chose it,
        querying the mounted tree instead of :attr:`_restore_tabs` because
        panes exist now."""
        try:
            tabbed = self.query_one("#session-tabs", TabbedContent)
        except Exception:  # noqa: BLE001 -- no tab strip, nothing to choose
            return
        target: "Any" = None
        if self._restore_active_id:
            with contextlib.suppress(Exception):
                target = tabbed.get_pane(_restore_pane_id(self._restore_active_id))
        if target is None:
            target = next(iter(self.panes()), None)
        if target is None or not target.id:
            return
        self._focus_tab(target)

    def run(self, *args: "Any", **kwargs: "Any") -> "Any":
        """``App.run``, wrapped in ownership of the TERMINAL's title.

        The window/taskbar title is not :attr:`App.title` -- that one is
        the Header widget's caption and never leaves the process. This is
        the OSC sequence the emulator reads, which Textual 5.3.0 offers no
        API for at all; :mod:`doxa.window` writes it, and this is the seam.

        Wrapped HERE, around ``run()``, rather than in ``on_mount`` /
        ``on_unmount`` or at each of :mod:`doxa.cli`'s four call sites:

        * ``on_unmount`` does not fire on every way out of a TUI, and when
          it does it fires while Textual still owns the screen -- the
          restore has to be the LAST thing written, after the driver has
          handed the terminal back.
        * ``run()`` is the one door. ``doxa new``, ``doxa attach``, a
          restore-from-tabset launch and ``--in-process`` all come through
          it, and so will any entry point added later -- which is what
          stops the next one shipping without the restore.

        ``run_test()`` does NOT come through here, deliberately: the suite
        has no terminal to title, and a test that emitted escapes into the
        captured output would be measuring its own harness."""
        from . import window as window_mod

        with window_mod.terminal_title(window_mod.title_for(self.cwd)):
            return super().run(*args, **kwargs)

    async def on_mount(self) -> None:
        """Auto-run /setup exactly once: a genuine first launch on this
        machine (doxa.setup.needs_first_run), never again after. The
        marker is written the moment this fires -- declining or Esc-ing
        out of the wizard must not make it reappear at the next launch;
        /setup still runs on demand any time."""
        self._activate_initial_tab()
        from . import setup as setup_mod

        if setup_mod.needs_first_run():
            setup_mod.mark_seen()
            self.call_after_refresh(self.action_setup)
        # Non-blocking: a `git fetch` (even a quiet, local one) must never
        # be on boot's critical path. Exclusive group of its own so a
        # pathological double-mount cannot stack two of these.
        self.run_worker(
            self._check_for_update(), exclusive=True, group="update-check"
        )

    async def _check_for_update(self) -> None:
        """Boot-time "is there something to pull" check -- see
        doxa.update.check_for_update for the git-level detail and its
        all-failures-are-silent posture. Notifies at most once per app run
        (the latch, not the checker, owns "once": the checker itself is
        stateless and could in principle be called again)."""
        from . import update as update_mod

        try:
            available = await asyncio.to_thread(update_mod.check_for_update)
        except Exception:  # noqa: BLE001 -- advisory only, never surfaces
            return
        # Item Z: record the ANSWER, not just the notification. /about
        # reads this rather than fetching again -- one `git fetch` per
        # launch, on a worker, is the whole budget for this question.
        self.update_available = bool(available)
        if available and not self._update_notified:
            self._update_notified = True
            notify_mod.notify_update_available(self.app_has_focus)

    def action_toggle_inspector(self) -> None:
        """Belief-inspector stub: Phase 3 owns the real pane (live STEER/
        CITE split, evidence trails); Phase 2 reserves the toggle, the dock
        and the count so the palette command and the muscle memory exist."""
        panel = self.query_one("#belief-inspector", BeliefInspector)
        if panel.display:
            panel.display = False
            return
        beliefs = self.engine.belief_count() if self.engine is not None else 0
        panel.set_text(
            f"{beliefs} active beliefs in the store.\n\n"
            "Phase 3 renders them here: STEER/CITE split,\n"
            "evidence trails, calibration. Until then use\n"
            "the lore_belief_search / lore_belief_show tools."
        )
        panel.display = True

    @on(events.Click, "#inspector-close")
    def _on_inspector_close(self, event: events.Click) -> None:
        """The ✕ is a real target for the mouse the key toggle leaves out."""
        event.stop()
        self.query_one("#belief-inspector", BeliefInspector).display = False

    @on(Collapsible.Expanded)
    def _on_chip_expanded(self, event: Collapsible.Expanded) -> None:
        if isinstance(event.collapsible, ToolChip):
            event.collapsible.format_body()

    # -- quit semantics (app-level, all tabs) ------------------------
    #
    # Neither of these lives on a key anymore (v0.85.0 dropped Ctrl+C --
    # see the BINDINGS comment). Both stay reachable from the command
    # palette ("Quit: detach" / "Quit: stop session") and action_quit_stop
    # doubles as `/update --restart`'s own shutdown path.

    async def action_quit_stop(self) -> None:
        """Quit-stop, ALL tabs -- finalize every session NOW. Over a daemon
        client this stops the daemon itself (LORE review + index run
        there); in-process it is plain finalize-and-quit.

        A pane the user DETACHED on purpose is not stopped: detaching is
        the explicit "keep this running" gesture, and a later quit must not
        quietly undo it. Those sessions outlive the window, which is what
        /sessions exists to show and reap."""
        for pane in self.panes():
            if pane.detached_on_purpose:
                await pane.detach()
            else:
                note = await pane.stop()
                if note:
                    # Best-effort: the app quits right after this loop, so
                    # this toast may not get a paint frame -- the daemon's
                    # own log line (doxa.daemon._finalize_worktree) is the
                    # channel actually guaranteed to survive quitting the
                    # TUI, exactly the "headless" case worktrees.finalize's
                    # docstring calls out.
                    self.notify(note, severity="information", timeout=10)
        # Item D: one snapshot after the loop -- every pane above is still
        # MOUNTED (detached or stop()-marked _stopped, neither removed:
        # the app quits right below), so _persist_tabset's own per-pane
        # scan picks all of them up without help from either side dict.
        # v0.60.0: a stopped pane is no longer excluded there either -- see
        # _ended_this_run's docstring -- so this method (palette 'Quit:
        # stop session', all tabs -- Ctrl+C used to reach it too, through
        # v0.84.0) now leaves every tab resumable, the same as ending them
        # one at a time with Ctrl+Q does; there is no reason the
        # all-at-once quit gesture should be the ONE way left to lose the
        # set for good.
        self._persist_tabset()
        await App.action_quit(self)

    async def action_quit(self) -> None:
        """palette 'Quit: detach' -- ALL tabs. Over a daemon client,
        finalize() only DETACHES: the daemon lingers and runs the
        session-end review + index itself once the last client has been
        gone for the linger window (or on `doxa stop`). In-process (Phase
        1 shape), finalize() still runs the review + index right here,
        host-driven (PHASE0 redesign item 1: no SessionEnd hook
        exists)."""
        for pane in self.panes():
            await pane.detach()
        # Item D: every pane stays mounted here (detach() only clears the
        # engine handle) -- the snapshot picks all of them up on its own.
        self._persist_tabset()
        await App.action_quit(self)


def main() -> None:
    DoxaApp().run()


if __name__ == "__main__":
    main()


def __getattr__(name: str) -> Any:
    """``doxa.app.SessionEngine``, imported on first use (PEP 562).

    The class is no longer imported at module scope -- doing so pulled
    claude_agent_sdk, 404 ms, before the first frame of a TUI that may
    never build an engine at all. It stays reachable under its old name so
    that ``from doxa.app import SessionEngine`` and every
    ``monkeypatch.setattr(doxa.app, "SessionEngine", ...)`` in the suite
    keep working unchanged."""
    if name == "SessionEngine":
        from .engine import SessionEngine

        return SessionEngine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
