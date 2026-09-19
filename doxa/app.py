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
from typing import Any, Callable

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
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
    TextArea,
)
from textual.widgets.markdown import MarkdownStream
from textual.widgets.option_list import Option

from . import auth as auth_mod
from . import clock as clock_mod
from . import collections as collections_mod
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
from .history import SessionSearch
from .identity import tier_short  # noqa: F401 -- re-exported: the status
# line's plan label lives in doxa.identity now (precise local tier first,
# SDK subscriptionType second); app.py keeps the name callers already use.
from .palette import DoxaCommandProvider
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
from .ui.diffview import DiffPane  # noqa: F401
from .ui.prompt import PromptInput  # noqa: F401
from .ui.sidebar import (  # noqa: F401
    LOOSE_HEADING,
    Row as SidebarRow,
    SessionSidebar,
    SidebarLine,
    build_rows,
)
from .ui.split import PaneGroup, PaneTab, SplitBox  # noqa: F401
from .ui import split as split_mod
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

# The error surface's app-level half is a mixin now, in
# doxa/appwindow/failures.py -- the first of DoxaApp's method families to
# move out. The two constants are imported back because they were
# ``doxa.app`` names before it and the suite reads one of them.
from .appwindow.failures import (  # noqa: F401 -- re-exported, see above
    FAILURE_ESCALATE,
    RENDER_FRAMES,
    WindowFailuresMixin,
)
# The tab set's two halves -- writing the record and composing the window
# back out of it -- are the second family out, in
# doxa/appwindow/restore.py. No constant went with them; ``compose`` stayed
# behind.
from .appwindow.restore import WindowRestoreMixin
# The session rail -- the groups it indexes, its own widths and refusals,
# the collections it holds -- is the third, in doxa/appwindow/sidebar.py.
# The six ``@on(SessionSidebar.*)`` handlers and the three sidebar actions
# stayed behind; no constant went with it either.
from .appwindow.sidebar import WindowSidebarMixin
# A tab's whole life -- opened, resumed, attached, described, renamed,
# closed, ended, stopped -- is the fourth, in doxa/appwindow/tabs.py. The
# four ``@on`` handlers that sit in the middle of it stayed behind, as did
# every ``action_*`` and ``_cmd_*`` that calls into it; no constant went
# with it, and ``_stop_session`` below is not one of its readers.
from .appwindow.tabs import WindowTabsMixin
# The tree of panes -- which groups, tabs, panes and surfaces are mounted,
# which one holds the keyboard, how a pane divides and how much room each
# half gets -- is the fifth, in doxa/appwindow/panetree.py (named for the
# tree rather than the layout, because :mod:`doxa.layout` is the geometry
# it calls). The two ``@on`` handlers in the middle of it stayed behind,
# as did every ``action_*`` and ``DIVIDER_STEP``; no constant went with it.
from .appwindow.panetree import WindowPaneTreeMixin
# Every ``action_*`` a key or the palette can dispatch to, the palette's
# own row list and the ``_cmd_*`` adapters those rows call are the sixth
# and last, in doxa/appwindow/actions.py. ``BINDINGS`` stayed: it is a
# class attribute of the window like ``CSS_PATH`` beside it, the suite
# reads it as ``DoxaApp.BINDINGS``, and it needs no change either way --
# Textual resolves a binding by ``getattr(app, "action_<name>")`` at press
# time, which walks the MRO. The fourteen ``@on`` handlers inside the span
# the family covers stayed too, as did ``GROUP_FLASH_SECS``; no constant
# moved.
from .appwindow.actions import WindowActionsMixin


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


class DoxaApp(
    WindowFailuresMixin, WindowRestoreMixin, WindowSidebarMixin, WindowTabsMixin,
    WindowPaneTreeMixin, WindowActionsMixin, App,
):
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
        # -- split panes (v0.91.0) ------------------------------------
        #
        # Ctrl+Up / Ctrl+Down move the IN-PANE divider: the status bar,
        # which SessionPane.compose puts literally between the transcript
        # and the prompt area. Up grows the transcript, down grows the
        # prompt. Re-verified free against THIS class's own binding set at
        # the moment they were added (the set changed in v0.85.0, when
        # Ctrl+C was freed for terminal copy): ctrl+p, ctrl+r, ctrl+comma,
        # ctrl+t, ctrl+w, ctrl+q, ctrl+left, ctrl+right, shift+tab,
        # ctrl+tab -- a VERTICAL pair reads as size against the horizontal
        # pair that already means "move between tabs". Textual's own
        # App/Screen defaults claim neither. tests/test_split_keys.py
        # asserts that, so the next release that adds a binding trips over
        # the collision instead of shipping it.
        #
        # priority=True for the reason every global here needs it: the
        # prompt is a focused TextArea (and TextArea binds ctrl+up/down to
        # cursor movement of its own), so without priority the widget eats
        # the key before the app ever sees it.
        Binding(
            "ctrl+up", "divider_up",
            "Grow the transcript (move the status-bar divider up)",
            show=False, priority=True,
        ),
        Binding(
            "ctrl+down", "divider_down",
            "Grow the prompt (move the status-bar divider down)",
            show=False, priority=True,
        ),
        # Directional focus between panes -- never "next pane": in a 2x2
        # grid "next" has no meaning a user can predict.
        Binding("ctrl+shift+left", "focus_pane_left", "Focus pane left",
                show=False, priority=True),
        Binding("ctrl+shift+right", "focus_pane_right", "Focus pane right",
                show=False, priority=True),
        Binding("ctrl+shift+up", "focus_pane_up", "Focus pane above",
                show=False, priority=True),
        Binding("ctrl+shift+down", "focus_pane_down", "Focus pane below",
                show=False, priority=True),
        # Creating a split. The COMMANDS follow vim -- `/split` is STACKED,
        # `/vsplit` is SIDE BY SIDE, vim's sense and the opposite of tmux's
        # `split-window -h`. The KEYS are positional (S and D adjacent under
        # the left hand), not mnemonic, and every description and summary
        # spells the direction out in words, because no letter resolves the
        # vim/tmux ambiguity for a reader who knows the other convention.
        #
        # THIRD attempt at this pair, and the first one measured against
        # the right thing. v0.91.0 rejected ctrl+shift+<letter> correctly
        # -- under the legacy encoding it sends the same byte as plain
        # ctrl+<letter>, so H/V and S/D alike were undeliverable, which is
        # why swapping between those pairs changed nothing. It then moved
        # to Alt on the reasoning that every terminal has sent Alt as an
        # ESC prefix since long before the kitty protocol. True, and beside
        # the point: the question is what TEXTUAL decodes, and it does not
        # decode that. Measured against textual 5.3.0's own parser
        # (doxa/keyboard.py carries the transcript):
        #
        #     XTermParser().feed("\x1bs")       -> Key('escape'), Key('s')
        #     XTermParser().feed("\x1b[115;3u") -> Key('alt+s')
        #
        # So alt+s arrived only on a terminal that granted the kitty
        # protocol, and on every other one it delivered a bare Escape and
        # then typed "s" into the prompt. Reported from live use as "the
        # hotkeys Alt+D and Alt+S are unresponsive", with `/split` and
        # `/vsplit` working -- exactly the signature of a key that never
        # reaches binding resolution at all.
        #
        # CTRL+<letter>, then, which is the one modified form the legacy
        # encoding was built around. Which letter is not a free choice;
        # subtracting everything already spoken for leaves exactly two:
        #
        #   * h, i, m       -- their C0 byte IS backspace/tab/enter, so
        #                      Textual reports that other key (doxa.keyboard
        #                      _SHADOWED_BY_C0).
        #   * a c d e f k u v w x y z
        #                   -- Textual's own TextArea.BINDINGS, and the
        #                      prompt IS a TextArea. priority=True would
        #                      win the key and break line editing with it;
        #                      v0.85.0's lesson (do not contest a binding
        #                      something else owns) applies to a widget as
        #                      much as to a terminal.
        #   * c z s q l b   -- the terminal's own: SIGINT, SIGTSTP, XOFF,
        #                      XON, redraw, and tmux's default prefix. A
        #                      tmux user cannot press ctrl+b at all.
        #   * j             -- literally the LF byte; \n is how Enter and a
        #                      pasted newline arrive.
        #   * p r t w q ,   -- already DoxaApp's above.
        #
        # Remainder: ctrl+n and ctrl+o. Deliberately NOT mnemonic, for the
        # same reason S/D were not -- no letter resolves the vim/tmux
        # disagreement about which word means which direction -- so the
        # description and the registry summary spell the direction out in
        # words, as they always have.
        Binding(
            "ctrl+o", "split_pane",
            "Split this pane — a second session STACKED BELOW it (/split)",
            show=False, priority=True,
        ),
        Binding(
            "ctrl+n", "vsplit_pane",
            "Split this pane — a second session SIDE BY SIDE with it (/vsplit)",
            show=False, priority=True,
        ),
        # The Alt pair rides beside them rather than being deleted, the
        # same arrangement Shift+Tab / Ctrl+Tab already has above: it is
        # real muscle memory for anyone on kitty, ghostty, WezTerm or foot,
        # where it always worked. /help marks it unsendable on a terminal
        # measured legacy (doxa.keyboard.is_unreachable now answers True
        # for alt+<character>, which through v0.94.0 it wrongly answered
        # False), so it is documented as conditional instead of documented
        # as working and silently dead.
        Binding(
            "alt+s", "split_pane",
            "Split stacked below (same as Ctrl+O; needs a kitty-protocol "
            "terminal)",
            show=False, priority=True,
        ),
        Binding(
            "alt+d", "vsplit_pane",
            "Split side by side (same as Ctrl+N; needs a kitty-protocol "
            "terminal)",
            show=False, priority=True,
        ),
        # The divider BETWEEN leaves. Its own gesture, deliberately: the
        # spec's own instruction is that Ctrl+Up/Down cannot mean two
        # things, and overloading them silently is the failure mode it
        # names. Alt+arrow moves the boundary between the focused pane and
        # its neighbour in that direction.
        #
        # These KEEP their Alt (v0.95.0 re-checked them while moving
        # alt+s/alt+d/alt+g off it) because a modified ARROW is a different
        # physical encoding from a modified LETTER: CSI 1;3<final>, the
        # same shape as the ctrl+arrow pairs above, which Textual's parser
        # decodes under both protocols. Measured, not assumed --
        # XTermParser().feed("\x1b[1;3D") -> Key('alt+left').
        Binding("alt+up", "grow_pane_up", "Grow this pane upward",
                show=False, priority=True),
        Binding("alt+down", "grow_pane_down", "Grow this pane downward",
                show=False, priority=True),
        Binding("alt+left", "grow_pane_left", "Grow this pane leftward",
                show=False, priority=True),
        Binding("alt+right", "grow_pane_right", "Grow this pane rightward",
                show=False, priority=True),
        # -- live diff (v0.92.0) ---------------------------------------
        #
        # Alt+G joined the family Alt+S / Alt+D established, and inherited
        # its defect with it: v0.95.0's measurement condemns all three at
        # once, so this one moves too rather than being left documented
        # and dead on every non-kitty terminal.
        #
        # It moves to F2 and not to a third ctrl+<letter> because there is
        # no third one left -- the subtraction in the split comment above
        # ends at exactly {ctrl+n, ctrl+o}, and the pair spent both. An
        # F-key is the next thing the legacy encoding delivers without
        # contest: SS3/CSI sequences older than the problem, claimed by
        # neither Textual's App, Screen nor TextArea, passed through by
        # tmux, and not one of the two most emulators bind (F10 menu, F11
        # fullscreen). Measured like everything else here --
        # XTermParser().feed("\x1bOQ") and feed("\x1b[12~") both give
        # Key('f2'). F2 rather than F1, which a terminal may treat as
        # help.
        Binding(
            "f2", "toggle_diff",
            "Live diff of this session's worktree, beside it (/diff)",
            show=False, priority=True,
        ),
        Binding(
            "alt+g", "toggle_diff",
            "Live diff beside this session (same as F2; needs a "
            "kitty-protocol terminal)",
            show=False, priority=True,
        ),
        # -- pane groups (v0.97.0) -------------------------------------
        #
        # Ctrl+1 .. Ctrl+9: jump to a group BY POSITION, numbered in
        # reading order -- left to right, then top to bottom -- so in a 2x2
        # Ctrl+1 is upper left, Ctrl+2 upper right, Ctrl+3 lower left,
        # Ctrl+4 lower right. Position is predictable in a way "next group"
        # is not, which is the same argument that made focus movement
        # directional rather than cyclic in v0.91.0.
        #
        # Chosen by the owner over the two alternatives, which were
        # rejected rather than overlooked: Alt+<digit> is terminal
        # tab-switching in GNOME Terminal and others, and a tmux-style
        # prefix chord costs two keystrokes for a gesture meant to be
        # instant.
        #
        # UNREACHABLE UNDER THE LEGACY ENCODING and shipped anyway: Ctrl
        # has a C0 code only for the 26 letters and @ [ \ ] ^ _ ? space, so
        # a digit produces no byte at all. doxa.keyboard says so
        # (`unreachable_under_legacy("ctrl+1") -> True`), /help and
        # /doctor mark it, and `/pane <n>` is the door that always works --
        # exactly the bargain Ctrl+, and Ctrl+Tab already ship on.
        #
        # priority=True for the reason every global here needs it: the
        # prompt is a focused TextArea and would otherwise eat the key.
        # -- the session sidebar (v1.0.0) ------------------------------
        #
        # F3: toggle the rail. RE-VERIFIED free against THIS class's own
        # resolved binding set at the moment it was added, which is the
        # check docs/plans/session-sidebar.md asks for because the set
        # moved three times in this release series: ctrl+p, ctrl+r,
        # ctrl+comma, ctrl+t, ctrl+w, ctrl+q, ctrl+left, ctrl+right,
        # ctrl+up, ctrl+down, ctrl+o, ctrl+n, ctrl+1..9,
        # ctrl+shift+arrows, shift+tab, ctrl+tab, f2, alt+s/d/g,
        # alt+arrows. tests/test_sidebar.py asserts the whole of that, so
        # the next release that adds a binding trips over a collision
        # instead of shipping one.
        #
        # F3, NOT Ctrl+B (owner's decision, 2026-09-02, reversing the
        # spec's own choice). Ctrl+B is tmux's default PREFIX: a tmux user
        # cannot press it at all, and the split-panes subtraction a few
        # hundred lines above had already listed ctrl+b among "the
        # terminal's own" for exactly that reason. The spec waved that off
        # ("tmux's prefix notwithstanding") on the grounds that /sidebar is
        # the always-works door -- true, and still the wrong trade for the
        # PRIMARY gesture of a permanent surface. This project has now
        # picked a contested or undeliverable key three times (Ctrl+C in
        # v0.85.0, alt+<letter> in v0.91.0, ctrl+shift+<letter> before it)
        # and walked back each one.
        #
        # F3 follows F2's precedent (/diff, v0.92.0): function keys go out
        # as CSI/SS3 sequences every terminal since xterm sends, so
        # doxa.keyboard.unreachable_under_legacy("f3") is False; Textual's
        # App/Screen defaults claim no F-key, TextArea claims none, and
        # tmux passes them through rather than swallowing them. Deliverable
        # under BOTH encodings and contested by nobody -- which is the bar
        # a letter could not clear here.
        #
        Binding(
            "f3", "toggle_sidebar",
            "Show or hide the session sidebar (/sidebar)",
            show=False, priority=True,
        ),
        # -- the rail's own divider (v1.5.0) ---------------------------
        #
        # The edge between the rail and the panes is draggable with the
        # mouse (SessionSidebar.on_mouse_down), and these are the other
        # half of it: a mouse-only control is unreachable for a keyboard
        # user, and this project has ruled on that twice.
        #
        # Alt+Shift+arrow, and the reason it is an ARROW is the reason
        # Alt+arrow survived v0.95.0's cull of alt+<letter>: a modified
        # arrow is a different physical encoding from a modified letter --
        # CSI 1;4<final>, the same shape as the ctrl+arrow and alt+arrow
        # pairs above -- which Textual's parser decodes under BOTH
        # protocols. Measured like everything else here, not assumed:
        # XTermParser().feed("\x1b[1;4D") -> Key('alt+shift+left'), and
        # doxa.keyboard.unreachable_under_legacy answers False for both.
        #
        # RE-VERIFIED free against this class's own resolved binding set,
        # which is the check every key added here since v0.91.0 has had to
        # pass: neither Textual's App/Screen defaults nor TextArea claims
        # an alt+shift+arrow, and tests/test_split_keys.py asserts it so
        # the next release that reaches for one trips over the collision.
        #
        # A HORIZONTAL pair, because the divider they move is vertical --
        # the same reading that made Ctrl+Up/Down the in-pane divider's
        # keys, one axis over. /sidebar width <n> is the door for a
        # terminal that sends neither.
        Binding(
            "alt+shift+left", "sidebar_narrower",
            "Narrow the session sidebar (/sidebar width)",
            show=False, priority=True,
        ),
        Binding(
            "alt+shift+right", "sidebar_wider",
            "Widen the session sidebar (/sidebar width)",
            show=False, priority=True,
        ),
        *[
            Binding(
                f"ctrl+{digit}", f"focus_group({digit})",
                f"Jump to pane group {digit} (reading order; needs a "
                "kitty-protocol terminal — /pane works everywhere)",
                show=False, priority=True,
            )
            for digit in range(1, 10)
        ],
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
        restore_layout: "list[Any] | None" = None,
        restore_groups: "Any" = None,
        restore_collections: "Any" = None,
        restore_rail_folded: "Any" = None,
    ) -> None:
        super().__init__()
        # One strip-id sequence per app (see doxa.ui.split.next_tabbed_id):
        # the FIRST group's TabbedContent is `#session-tabs` exactly, which
        # is what keeps an unsplit window's DOM identical to every release
        # before this one -- and identical for each app a suite builds,
        # rather than climbing across tests in one process.
        split_mod.reset_tabbed_ids()
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
        # v0.91.0: one :mod:`doxa.layout` tree per saved TAB, in saved tab
        # order -- the split structure ``_restore_tabs``' flat list cannot
        # express. ``None``/empty for every record written before this
        # release AND for every ordinary launch, and the absence is the
        # migration: :meth:`_restore_group_tree` turns each surviving spec
        # into a tab of one group, which is exactly the tab it was.
        self._restore_layout = list(restore_layout or [])
        # v0.97.0: the WINDOW's one tree, leaves holding
        # :class:`doxa.layout.Group`. ``None`` for every ordinary launch and
        # for every record written before this release; the absence is the
        # migration, and :meth:`_restore_group_tree` derives one from
        # whichever of the two older shapes did arrive (or from the flat
        # spec list alone) using exactly the composition rule
        # :func:`doxa.tabsets._layout_groups` documents -- shared with it
        # rather than restated, so a launch from doxa.cli and a launch from
        # a hand-built DoxaApp cannot disagree about what a record means.
        self._restore_groups = restore_groups
        # The user's SESSION COLLECTIONS (v1.0.0, doxa.collections) -- the
        # rail's model, and the ONE copy of it. Held on the app rather
        # than on the sidebar widget for two reasons: it survives the rail
        # being hidden (a hidden widget is still mounted, but a rail that
        # owned the model would make "the model exists" a fact about
        # chrome), and _persist_tabset writes it from here on every tab
        # lifecycle event without having to find a widget first.
        #
        # A collection groups sessions BY NAME regardless of which
        # PaneGroup shows them -- see doxa.collections' docstring on why
        # the word is not "group".
        self._collections: "tuple[collections_mod.Collection, ...]" = tuple(
            restore_collections or ()
        )
        # Which pane GROUPS are folded shut on the rail (v1.5.0), by
        # entry_key. Held here for the same two reasons the collections
        # are: it survives the rail being hidden, and _persist_tabset
        # writes it from here without having to find a widget.
        #
        # A SET of the exceptions, defaulting empty, because expanded is
        # the default and a fold is a thing the user did -- the same shape
        # ``Collection.collapsed`` has, which is written only when true. A
        # key naming a group this window no longer has costs nothing: it
        # is never asked about, and the layout changing under it is the
        # ordinary case rather than an error.
        self._rail_folded: "set[str]" = {
            str(key) for key in (restore_rail_folded or ()) if str(key or "")
        }
        # The rail width a DRAG is currently showing, or None when the
        # settings registry is the answer. A drag posts a width per mouse
        # move and only the last of them is written to disk, so this is
        # what keeps a refresh in between from snapping the rail back to
        # the stored value mid-gesture.
        self._sidebar_width_override: "int | None" = None
        # Whether the LAST attempt to open the rail was refused for width,
        # and what it said. Kept so on_resize can open it for free the
        # moment the terminal grows past the threshold, rather than making
        # the user press Ctrl+B again at a window they never chose to
        # shrink.
        self._sidebar_wanted = False
        # Which group the number overlay is showing on, and the ONE-SHOT
        # timer that takes it away. See _flash_group_numbers for why a
        # one-shot is inside DOXA's no-timer rule and an interval is not.
        self._group_flash_timer: "Any" = None
        # The group that had the keyboard last -- read only when focus is
        # somewhere that is not a group at all (a modal, the palette, the
        # rename field). See :meth:`focused_group`.
        self._last_group_id: "str | None" = None
        # Group widget ids are minted, never reused: an id that moved
        # between widgets would be a second answer to "which region is
        # this", and Ctrl+<digit> deliberately does NOT read them (it reads
        # the painted rectangles). They exist so a DOM dump is legible and
        # so _last_group_id can name one across a modal.
        self._group_serial = 0
        # Groups whose mount-time TabActivated has already been absorbed --
        # see _on_tab_activated for what that message is and why exactly
        # the first one per group must not move the keyboard.
        self._groups_activated: "set[str]" = set()
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
        # run: NOT a source _persist_tabset reads any more (v0.99.1 -- see
        # that method's own docstring). v0.60.0 fed this dict into the
        # persisted set on the theory that a finalized session was still a
        # RESUMABLE one, so losing the tab did not have to mean losing the
        # record of it -- reported live, in two acts: first as the fix
        # ("a Ctrl+Q'd tab used to vanish from the persisted set"), then as
        # the defect it actually was ("tabs closed with Ctrl+Q are
        # resurrected on the next start of DOXA anyway" -- and worse, LIVE,
        # not read-only, because finalize() never touches the CLI's own
        # history store, so the next launch's resume_state check found the
        # conversation and happily resumed it). Ctrl+Q ends the session,
        # full stop; that verb should not have a sequel. This dict is kept
        # for what it does NOT touch: within the current run, "this window
        # ended it" is still worth knowing on its own (the sidebar rail
        # dims an ended session's row for the rest of the run) -- it just
        # no longer has any say over what the NEXT launch restores.
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

    # -- groups own tabs now (v0.97.0) --------------------------------

    @on(TabbedContent.TabActivated)
    def _strip_visibility_on_tab_activated(
        self, _event: TabbedContent.TabActivated
    ) -> None:
        """The second door onto :meth:`refresh_strip_visibility`, and the
        reason it needs one.

        :meth:`_persist_tabset` is this app's hook for "the tab set
        changed", and it covers every tab a RESTART would bring back. It
        does not cover the tabs a restart would not: a subagent transcript
        tab (``SessionPane.open_transcript_tab``) is opened and closed
        without persisting anything, deliberately -- it is a view of a
        turn, not a session -- and it is still a second tab in its group,
        which is the only thing the strip is asking about.

        ``TabActivated`` is what those two paths do have in common: every
        way this app adds a tab activates it, and closing the active one
        activates whatever is left. It fires on ordinary tab SWITCHES too,
        which move no strip at all -- and cost nothing, because
        :meth:`~doxa.ui.split.PaneGroup.strip_should_hide` answers that for
        free before anything is scanned.

        Deliberately not the only door. A removal that leaves the active
        tab alone posts nothing, and ``_persist_tabset`` is what catches
        that -- neither hook is complete, and between them there is no gap
        this app can reach.

        AFTER the refresh, because of the order ``TabbedContent.
        remove_pane`` works in: it takes the Tab out of the strip (which
        is what posts this message, the removed one having been active)
        and only then detaches the ``TabPane`` itself, so a count taken
        synchronously here still sees the tab that is leaving and the
        strip would stay up after its second-to-last tab closed. One frame
        later the detach has landed and :meth:`~doxa.ui.split.PaneGroup.
        tabs`'s own ``parent`` check agrees with it."""
        with contextlib.suppress(Exception):
            self.call_after_refresh(self.refresh_strip_visibility)

    # -- focus ownership (v0.38.0) ------------------------------------

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

    # -- the session sidebar (v1.0.0) ---------------------------------
    #
    # Everything here is about a widget that is a SIBLING of the window
    # root, never a member of it. Nothing in this section touches the
    # tree, and nothing in the tree section above knows the rail exists --
    # that separation is the feature's whole design (see
    # doxa/ui/sidebar.py) and the reason _window_root() (now in
    # doxa/appwindow/panetree.py) needed no change at all.

    # -- painting -----------------------------------------------------

    def on_resize(self, event: "events.Resize") -> None:
        """The terminal changed size, so the width refusal may have.

        A rail refused for width is not a rail the user stopped wanting --
        they never chose to shrink the window -- so it opens again for
        free the moment there is room, and closes again when there is not.
        The same measured-not-remembered posture ``PaneGroup.on_resize``
        takes for its own tab strip one level down."""
        with contextlib.suppress(Exception):
            self.refresh_sidebar()

    # -- reveal -------------------------------------------------------

    @on(SessionSidebar.Revealed)
    def _on_sidebar_revealed(self, event: "SessionSidebar.Revealed") -> None:
        event.stop()
        note = self.reveal_session(event.session_id)
        if note:
            self.notify_sidebar(note)

    @on(SessionSidebar.GroupFocused)
    def _on_sidebar_group_focused(
        self, event: "SessionSidebar.GroupFocused"
    ) -> None:
        """A group heading was clicked. Focus the group -- or, when the
        entry is not a live group at all, fall back to revealing the
        session its state came from.

        The fallback is not a safety net, it is the honest answer for the
        entry :func:`doxa.triage.entries_for` invents: a detached or ended
        session has no pane group, so "focus the group" has no group to
        mean, and ``reveal_session`` already knows how to say ``/attach``
        to a row there is nowhere to go to."""
        event.stop()
        if event.entry_key and self.focus_group_by_key(event.entry_key):
            return
        note = self.reveal_session(event.session_id)
        if note:
            self.notify_sidebar(note)

    @on(SessionSidebar.GroupToggled)
    def _on_sidebar_group_toggled(
        self, event: "SessionSidebar.GroupToggled"
    ) -> None:
        event.stop()
        self.toggle_group_expanded(event.entry_key)

    @on(SessionSidebar.AttachStaged)
    def _on_sidebar_attach_staged(
        self, event: "SessionSidebar.AttachStaged"
    ) -> None:
        """A closed row was double-clicked: type ``/attach`` for the user
        and stop there.

        **Staged, never run.** Through :meth:`_cmd_prefill`, which is the
        same door ``Ctrl+R`` uses for ``/search `` -- so "the rail put
        something in my prompt" is a thing this app already does, in one
        place, and this is not a second way to do it.

        Which prompt: ``active_pane``'s, i.e. the pane holding the
        keyboard. That is what every other prefill in this file means by
        "the prompt", and it is the box the user's next keystroke was
        going to land in anyway. A window with no active pane -- one whose
        only tab is an archive -- has no prompt to stage into, and gets
        the sentence instead: :meth:`reveal_session`'s own, which names
        the same command in the only surface such a window has.

        The eight-character prefix and not the full id, because that is
        the form DOXA already tells people to type (see
        :meth:`reveal_session`) and ``/attach`` resolves it. A gesture
        that staged a different string from the one the transcript names
        would make the two read as two different commands."""
        event.stop()
        session_id = event.session_id
        if not session_id:
            return
        if self.active_pane is None:
            note = self.reveal_session(session_id)
            if note:
                self.notify_sidebar(note)
            return
        self._cmd_prefill(f"/attach {session_id[:8]}")

    @on(SessionSidebar.WidthDragged)
    def _on_sidebar_width_dragged(
        self, event: "SessionSidebar.WidthDragged"
    ) -> None:
        """The rail's right edge moved under the mouse.

        A refused width is simply not taken -- the rail stops at the floor
        and the pointer carries on -- rather than being reported: a drag
        is a continuous gesture and a notification per cell crossed would
        be the transcript filling up with a sentence the user is already
        being shown by the edge not moving. The KEYS say it instead, once
        per press (:meth:`action_sidebar_wider`)."""
        event.stop()
        self.resize_sidebar(event.width, persist=event.final)

    @on(SessionSidebar.CollectionToggled)
    def _on_sidebar_collection_toggled(
        self, event: "SessionSidebar.CollectionToggled"
    ) -> None:
        event.stop()
        held = collections_mod.find(self._collections, event.name)
        if held is None:
            return
        self._collections = collections_mod.set_collapsed(
            self._collections, event.name, not held.collapsed
        )
        self.refresh_sidebar(force=True)
        self._persist_tabset()

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
        # v1.0.0: the session rail is a SIBLING of the window root, never
        # a member of it. See doxa/ui/sidebar.py's module docstring for
        # the whole argument; the two consequences that matter HERE are:
        #
        # * ``_window_root()`` still returns the outermost ``SplitBox``,
        #   so it needs no change and no isinstance special case -- the
        #   rail is not one. Splits, Alt+arrow growth, directional focus
        #   and ``_pane_regions`` operate on the tree and never see it.
        # * the ``Horizontal`` exists from HERE and cannot be created on
        #   demand: Textual 5.3 cannot re-parent a mounted widget
        #   (measured, v0.91.0 -- a mount of a mounted widget is a silent
        #   no-op that orphans it), so wrapping the root at runtime is
        #   impossible. The rail is mounted hidden instead, exactly the
        #   reason ``split_mod.chain`` pre-makes empty boxes.
        with Horizontal(id="window-row"):
            yield SessionSidebar()
            if self._restore_tabs:
                yield self._compose_restored_root()
            else:
                pane = self._make_pane(self._engine_factory)
                # Item D fallback: every saved tab was dead (nothing to
                # reattach), but doxa.cli still has a report to show --
                # "restored 0, skipped N" -- on the one fresh tab it
                # spawned instead. self._restore_report is None on every
                # ordinary launch, so this is a no-op there.
                pane._boot_report = self._restore_report
                # ALWAYS inside the chain of empty SplitBoxes: that chain
                # is what a later split is created INTO, and it cannot be
                # created on demand (doxa/ui/split.py's own docstring says
                # why).
                yield split_mod.chain(self._make_group(self._make_tab(pane)))

    @on(TabbedContent.TabActivated)
    def _on_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self._jump_tab_marker()
        # **Only a group's SECOND activation onwards moves the keyboard**
        # (v0.97.0). Every group posts one ``TabActivated`` as it mounts --
        # Textual's ``Tabs`` defaults itself to its first tab and the
        # watcher posts the message -- so with N groups this handler used
        # to fire N times during boot and the LAST one to land won the
        # keyboard, whatever ``_activate_initial_tab`` had just said.
        # Measured as a restore with the saved active session in the middle
        # landing on the last group instead: the exact v0.23.0
        # "three restored tabs always land on the last one" defect,
        # re-created one level up.
        #
        # Skipping only the FIRST per group is what keeps the MOUSE path --
        # the one path with no keyboard site to hang focus on, and the only
        # reason this handler focuses at all -- working: a click on a
        # background group's tab header is never that group's first
        # activation.
        group = split_mod.group_of(event.pane) if event.pane is not None else None
        group_key = getattr(group, "id", None) or ""
        if group_key and group_key not in self._groups_activated:
            self._groups_activated.add(group_key)
            return
        tab = event.pane if isinstance(event.pane, PaneTab) else self._active_tab()
        if isinstance(tab, PaneTab):
            # Focus here as well as at every keyboard site (v0.38.0), for
            # the ONE path that has no keyboard site to hang it on: a
            # MOUSE click on a tab header produces no key event and runs no
            # action of ours -- Textual activates the tab and this is the
            # only thing we hear about it. Every other caller of
            # _focus_tab has already focused by the time this arrives, so
            # this is a no-op refocus for them.
            #
            # The "you missed something" clears ride along INSIDE
            # _focus_tab now (v0.91.0), scoped to the pane that actually
            # gets the keyboard -- see _clear_seen_marks. Doing it here,
            # per tab, would clear the marks of every visible pane in a
            # split, which is the reading the spec rejects.
            self._focus_tab(tab)
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

    @on(Input.Submitted, "#tab-rename")
    def _on_rename_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        pane_id = getattr(event.input, "pane_id", "")
        pane = next((p for p in self.panes() if p.tab_id == pane_id), None)
        if pane is not None:
            # Empty means "no name", which is how a pinned tab is un-pinned.
            pane.set_custom_name(event.value)
        self._end_rename(pane_id)

    @on(TabRenameCancelled)
    def _on_rename_cancelled(self, event: TabRenameCancelled) -> None:
        event.stop()
        self._end_rename(event.pane_id)

    # -- dividers (v0.91.0) -------------------------------------------

    #: How much of a split one Alt+arrow moves. A fifth of the smallest
    #: legal share, so the boundary is nudgeable rather than jumpy and a
    #: held key still crosses the range in a couple of seconds.
    DIVIDER_STEP = 0.03

    # -- pane groups: jump, flash, move (v0.97.0) ---------------------

    #: How long the ``Ctrl+<digit>`` number overlay stays up. Long enough
    #: to read a single digit and register where it was, short enough that
    #: it is gone before the next thought -- and it is CANCELLED by the
    #: next key either way, so a user who is already moving never waits for
    #: it.
    GROUP_FLASH_SECS = 1.2

    async def on_event(self, event: "Any") -> None:
        """Every input event passes through here on its way to the screen,
        which is the ONE place a key can be seen before some widget
        consumes it -- the focused prompt is a ``TextArea`` and stops the
        ``Key`` message dead, so an ``@on(events.Key)`` handler on this
        class never fires for an ordinary letter. Measured, not assumed:
        the first version of the number-overlay dismissal was written that
        way and the overlay simply stayed up.

        Kept to exactly one job for that reason. Anything more here would
        be a second event pipeline beside Textual's own."""
        if isinstance(event, events.Key):
            self._dismiss_group_numbers(event)
        await super().on_event(event)

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

        try:
            with window_mod.terminal_title(window_mod.title_for(self.cwd)):
                return super().run(*args, **kwargs)
        finally:
            # The one door, so the mesh graph's loopback port closes with
            # the window on every launch shape -- see
            # WindowActionsMixin.on_unmount for the other half and why
            # neither alone is enough.
            with contextlib.suppress(Exception):
                self.stop_mesh()

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

    @on(events.Click, "#inspector-close")
    def _on_inspector_close(self, event: events.Click) -> None:
        """The ✕ is a real target for the mouse the key toggle leaves out."""
        event.stop()
        self.query_one("#belief-inspector", BeliefInspector).display = False

    @on(Collapsible.Expanded)
    def _on_chip_expanded(self, event: Collapsible.Expanded) -> None:
        if isinstance(event.collapsible, ToolChip):
            event.collapsible.format_body()


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
