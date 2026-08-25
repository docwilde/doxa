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

Ctrl+C stays APP-level, deliberately: one press arms the double-press
window and then detaches ALL tabs (every daemon keeps running -- the
cheapest outcome to recover from is always chosen on a reflex
keystroke); a second press inside the window stops EVERY tab's session
(finalize NOW). Per-tab stopping remains available where deliberation
lives: the palette's quit-stop and Ctrl+W.

Each turn is a foldable Collapsible; its response streams as markdown
(Markdown.get_stream -- textual 5's append-only path for LLM deltas, no
full re-parse per chunk). Tool calls inside a turn render as compact
chips (name + one-line arg summary + duration + a check or cross) that
lazily expand into full args/result on first click -- the expensive JSON
pretty-printing only happens once, on demand, not for every tool call
that streams past -- and compact further behind ONE per-turn "Tool calls
(N)" fold (ToolCallsSection), created lazily on the first call.

Asyncio/Textual coexistence follows PHASE0_FINDINGS.md §4 exactly:
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

The seams the split follows are the ones docs/plugin-api.md names: the
command table (:data:`doxa.session.commands.PANE_COMMANDS`), the status
chips (:class:`doxa.session.chips.StatusChip`), the event dispatch map
(:data:`doxa.session.runtime.EVENT_RENDERERS`) and the model provider
(:mod:`doxa.providers`). Those are structures, not a loader: this release
gained no way to load third-party code, deliberately.
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
from .engine import (
    BELIEF_LIST_LIMIT,
    PENDING_LIST_LIMIT,
    EngineEvent,
    SessionEngine,
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
from .session.pane import SessionPane  # noqa: F401
from .ui.beliefs import (  # noqa: F401
    BeliefRow,
    BeliefsBrowserTab,
    BrowserNote,
    BrowserRow,
    EvidenceTrail,
    ProposalRow,
)
from .ui.dialogs import (  # noqa: F401
    _NEEDS_INPUT_DIGIT_KEYS,
    AboutDialog,
    BeliefInspector,
    ChipPicker,
    CloseWithTurnRunning,
    CompactConfirm,
    NeedsInputPopup,
    PermissionModeConfirm,
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
    MODE_ACTIVE,
    MODE_CHIP_MIN_COLS,
    MODE_DANGER,
    MODE_EXPLAIN,
    MODE_SHORT,
    MODE_WARN_GLYPH,
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
    _restore_pane_id,
    ArchivedSessionTab,
    BootBanner,
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


# Ctrl+C quit semantics: the first press arms this window and then detaches;
# a second press inside it upgrades to quit-stop (finalize NOW).
CTRL_C_DOUBLE_SECS = 2.0


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
    # Ctrl+R: prefills "/search " -- the live session-search popup
    # (doxa/history.py) is the one search surface; the key is a shortcut to
    # it, not a second door.
    # instant BM25 over every indexed session, not a scrollback scan.
    # Ctrl+T/Ctrl+W: tab lifecycle (new same-repo session / close-detach).
    # Ctrl+C: quit. Textual 5 binds ctrl+c to a "press ctrl+q to quit"
    # notification app-side and to "copy" on a focused Input -- with the
    # prompt input permanently focused, Ctrl+C therefore did NOTHING
    # quit-shaped (the dogfooding bug). priority=True beats both: one press
    # = quit-detach ALL tabs (daemons keep running), double press within
    # CTRL_C_DOUBLE_SECS = quit-stop ALL -- see action_ctrl_c_quit.
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
        # deliberately and scopes it to the TAB. Quitting the window is
        # Ctrl+C (twice) here, and a key that ends one session must not be
        # the same key that ends all of them. priority=True for the same
        # reason Ctrl+C needs it: the focused Input would otherwise eat it.
        # (Terminal flow control does not: Textual's Linux driver clears
        # IXON/IXOFF, i.e. `stty -ixon`, so Ctrl+Q reaches the app.)
        Binding(
            "ctrl+q", "end_session",
            "End this session (finalize now) and close its tab",
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
        Binding(
            "ctrl+c", "ctrl_c_quit",
            "Quit: detach all tabs (twice = stop the sessions)",
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
        restore_tabs: "list[RestoreTabSpec] | None" = None,
        restore_active_id: "str | None" = None,
        restore_report: "str | None" = None,
    ) -> None:
        super().__init__()
        self.cwd = cwd or os.getcwd()
        self.model = model
        # The daemon-split seam: engine_factory builds whatever the first
        # tab drives (in-process SessionEngine by default; an EngineClient
        # when doxa.cli attached us to a daemon). new_session_factory builds
        # a FRESH session -- the palette's "new session", and every Ctrl+T
        # tab -- distinct because an attach-flavored engine_factory must not
        # be re-invoked to mean "new".
        self._engine_factory = engine_factory or (
            lambda: SessionEngine(cwd=self.cwd, model=self.model)
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
            lambda path: SessionEngine(cwd=path, model=self.model)
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
        self._tab_serial = 0
        # Ctrl+C double-press window (see action_ctrl_c_quit): the armed
        # timer that will quit-detach when it fires; a second Ctrl+C while
        # it is armed cancels it and quit-stops instead.
        self._ctrl_c_timer: Any = None
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

        Two sources, merged: panes still mounted (in tab-bar order,
        excluding any this run has already marked _stopped -- a stopped
        session must never reappear here even in the brief window before
        its pane is actually unmounted) and _detached_this_run (sessions
        Ctrl+W'd out of the strip earlier this run, which keeps running
        and therefore STAYS in the set per item D #4 -- see that dict's
        own docstring).

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
            if getattr(pane, "_stopped", False):
                continue
            sid = pane._session_id
            if not sid or sid in seen:
                continue
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
        for record in self._detached_this_run.values():
            if record.session_id not in seen:
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

    def compose(self) -> ComposeResult:
        yield BeliefInspector()  # hidden stub, palette-toggled
        yield ClockChip()  # upper-right, own layer -- see theme.tcss
        with TabbedContent(id="session-tabs"):
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
                        )
                        continue
                    pane = SessionPane(
                        self._tab_title(), self.cwd, self.model,
                        spec.engine_factory, id=_restore_pane_id(spec.session_id),
                    )
                    if spec.pinned_name:
                        pane._initial_pinned_name = spec.pinned_name
                    # v0.32.0: this pane's scrollback comes from the
                    # session's persisted transcript, not the daemon's
                    # 512-frame ring (see SessionPane._restore_transcript).
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
                    # is dead" branch gives.
                    pane = self._make_pane(self._engine_factory)
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

        The three non-session tabs -- a subagent transcript
        (SubagentTranscriptTab), a restored archive (ArchivedSessionTab)
        and item V's beliefs browser (BeliefsBrowserTab) -- take the SAME
        key to a much simpler path: none is a session (self.active_pane
        -- SessionPane-only -- comes back None for all three), so there is
        no daemon to detach and no turn-in-flight question to ask; they
        just close. There is always at least one SessionPane, so none of
        them is ever "the last tab" and none reaches the close-the-app
        branch _close_pane below falls back to.

        Closing the last SESSION tab closes the app, on the same detach
        semantics."""
        pane = self.active_pane
        if pane is not None:
            await self._close_pane(pane, terminate=False)
            return
        with contextlib.suppress(Exception):
            active = self.query_one("#session-tabs", TabbedContent).active_pane
            if isinstance(active, SubagentTranscriptTab):
                await self._close_transcript_tab(active)
            elif isinstance(active, ArchivedSessionTab):
                await self._close_archived_tab(active)
            elif isinstance(active, BeliefsBrowserTab):
                await self._close_beliefs_tab(active)

    async def _close_beliefs_tab(self, tab: "BeliefsBrowserTab") -> None:
        """Ctrl+W on the beliefs browser (item V): nothing to detach and
        nothing to stop -- it holds no engine of its own. Remove it and
        drop the owning pane's reference, the same two steps
        :meth:`_close_transcript_tab` takes, so reopening builds a fresh
        one instead of activating a tab that is no longer there.

        Never persisted either way: the browser is not a session, and
        _persist_tabset only ever records SessionPanes and archives."""
        if getattr(tab.owner, "_beliefs_tab", None) is tab:
            tab.owner._beliefs_tab = None
        with contextlib.suppress(Exception):
            await self.query_one("#session-tabs", TabbedContent).remove_pane(
                tab.id or ""
            )

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

        Tab-scoped, never app-scoped: quitting the whole window is Ctrl+C
        (twice). A turn IN FLIGHT is the one case this refuses to decide by
        itself -- killing work silently is not a thing a keystroke should
        do -- so it asks; an idle session ends without a prompt.

        Dispatched into a worker because awaiting a modal's answer
        (push_screen_wait) is only legal from one."""
        self.run_worker(self._end_session(), group="close")

    async def _end_session(self) -> None:
        pane = self.active_pane
        if pane is None:
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
        else:
            # Detached ON PURPOSE: it is no longer this window's to end, so
            # a later quit-stop leaves it running.
            pane.detached_on_purpose = True
            await pane.detach()
            # Item D #4: this session STAYS in the persisted tab set even
            # though its tab is about to leave the strip below -- record it
            # here, before remove_pane takes pane._session_id out of
            # panes()'s own scan with it. Scope-checked (item 4's repo
            # picker reconciliation, same reasoning as _persist_tabset's
            # own exclusion): a cross-repo tab (opened via the repo
            # picker, never possible before it existed) was never part of
            # THIS window's own repo-scoped persisted set, so detaching it
            # must not add it there either.
            if pane._session_id:
                pane_cwd = str(getattr(pane.engine, "cwd", None) or pane.cwd)
                pane_scope = peers_mod.main_repo_root_of(pane_cwd) or pane_cwd
                app_scope = peers_mod.main_repo_root_of(self.cwd) or self.cwd
                if pane_scope == app_scope:
                    self._detached_this_run[pane._session_id] = tabsets_mod.TabRecord(
                        pane._session_id, pane.custom_name,
                    )
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

        The key can only ever reach :data:`doxa.engine.CYCLE_MODES` --
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
        target = next_cycle_mode(getattr(pane.engine, "permission_mode", None))
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
        from .client import EngineClient  # deferred: tests without a daemon never import it

        socket_path = entry.daemon_socket
        if not socket_path:
            return
        pane = self.active_pane
        if pane is not None:
            pane.run_worker(
                pane.switch_engine(lambda: EngineClient(socket_path)),
                exclusive=True, group="switch",
            )

    def _cmd_stop_active(self) -> None:
        self.run_worker(self._stop_active(), group="tabs")

    async def _stop_active(self) -> None:
        """Palette 'Quit: stop session', tab-scoped: finalize the ACTIVE
        tab's session NOW; the tab closes with it. Stopping the only tab
        closes the app (the Phase 2 behavior, per-app == per-tab then)."""
        pane = self.active_pane
        if pane is None:
            return
        note = await pane.stop()
        if note:
            self.notify(note, severity="information", timeout=10)
        # Item D: stop() already marked the pane _stopped -- persisting now
        # drops it from the saved tab set, same as any other stop path.
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

    def _activate_initial_tab(self) -> None:
        """Startup's own explicit tab choice: ACTIVATE the tab this launch
        is about, and FOCUS it. Both halves, said once, here.

        Activation of a RESTORED set was already explicit (item D: land on
        the tab that was active when the set was saved, not whichever one
        Textual defaults to). The other two cases were not. An ordinary
        launch, and a restore with no saved active tab, got their active
        tab as a side effect of the first pane focusing its own prompt on
        mount -- and that focus is what v0.38.0 removed.

        The risk that raised, tested rather than assumed: with no
        mount-time focus, does the first pane still end up focused?
        Measured -- Textual DOES post TabActivated for the initially
        active tab (``Tabs._on_mount`` picks the first tab, its watcher
        posts it), so _on_tab_activated would in fact focus the prompt on
        its own. That is still not good enough to rely on: "the first
        prompt is focused because a widget we do not own happens to
        announce itself" is the same implicitness this release exists to
        remove, and docs/split-panes.md needs the startup leaf to be a
        DECISION before a window can hold two panes at once. So startup
        says what it means, and the event becomes a no-op refocus.

        Which tab: the saved active one if the record named one -- live
        pane or archived tab alike, it is where the user was -- otherwise
        the first SESSION pane in the strip. That second rule reproduces
        what the old mount-time focus picked, including its one non-obvious
        case: when every restored tab was archived, the tab that came up
        active was the FRESH pane compose() adds beside them, not the first
        archive, because the archives have no prompt to focus and the
        fresh pane does."""
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
        with contextlib.suppress(Exception):
            tabbed.active = target.id
        self._focus_tab(target)

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

    async def action_ctrl_c_quit(self) -> None:
        """Ctrl+C (priority binding), APP-level by design -- a reflex
        keystroke gets the cheapest-to-recover outcome across every tab.
        First press: arm the double-press window, then quit-DETACH when it
        expires -- every daemon-hosted session keeps running; in-process
        engines finalize right there, so Ctrl+C always exits cleanly.
        Second press inside the window: quit-STOP every tab's session
        (finalize NOW, daemons included). Per-tab ending lives on Ctrl+Q
        and the palette's 'Quit: stop session', where the choice is
        deliberate rather than reflexive."""
        if self._ctrl_c_timer is not None:
            self._ctrl_c_timer.stop()
            self._ctrl_c_timer = None
            await self.action_quit_stop()
            return
        self.notify(
            "detaching all tabs — Ctrl+C again to STOP the sessions (finalize now)",
            severity="warning",
            timeout=CTRL_C_DOUBLE_SECS,
        )
        self._ctrl_c_timer = self.set_timer(CTRL_C_DOUBLE_SECS, self.action_quit)

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
        # Item D: one snapshot after the loop -- every pane above is either
        # still mounted-and-detached (included) or stop()-marked _stopped
        # (excluded), which is exactly the set a later restore should see.
        self._persist_tabset()
        await App.action_quit(self)

    async def action_quit(self) -> None:
        """palette 'Quit: detach' (and the Ctrl+C window's expiry) -- ALL
        tabs. Over a daemon client, finalize() only DETACHES: the daemon
        lingers and runs the session-end review + index itself once the
        last client has been gone for the linger window (or on `doxa
        stop`). In-process (Phase 1 shape), finalize() still runs the
        review + index right here, host-driven (PHASE0 redesign item 1: no
        SessionEnd hook exists)."""
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
