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
from textual.app import App, ComposeResult, ScreenStackError
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.css.query import NoMatches
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
from . import collections as collections_mod
from . import commands as commands_mod
from . import config as config_mod
from . import errors as errors_mod
from . import identity as identity_mod
from . import images as images_mod
from . import keyboard as keyboard_mod
from . import layout as layout_mod
from . import naming as naming_mod
from . import notify as notify_mod
from . import paste as paste_mod
from . import peers as peers_mod
from . import providers as providers_mod
from . import tabsets as tabsets_mod
from . import transcript as transcript_mod
from . import triage as triage_mod
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
from .ui import labels as labels_mod
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

    # -- groups own tabs now (v0.97.0) --------------------------------

    def groups(self) -> "list[PaneGroup]":
        """Every pane group in the window, in DOM order.

        DOM order is NOT the order ``Ctrl+1``..``Ctrl+9`` counts in --
        that is :meth:`_group_order`, derived from the painted rectangles,
        because what the user counts is what is on screen. Every caller
        that needs the numbering says so by calling the other one.

        ``[]`` rather than a raise once the window is gone, for the reason
        :meth:`_focused_node` gives: ``App.query`` resolves against
        ``self.screen`` (``DOMNode.query`` -> ``_get_dom_base``), so a
        handler drained after the screen stacks are cleared would get a
        ``ScreenStackError`` out of what reads as a plain inventory
        question. "No groups" is the true answer for a window that no
        longer exists, and it is the one every caller here already
        handles."""
        try:
            return list(self.query(PaneGroup))
        except ScreenStackError:
            return []

    def _group_order(self) -> "list[PaneGroup]":
        """Every VISIBLE group in READING order -- left to right, then top
        to bottom -- which is the order ``Ctrl+<digit>`` numbers them in and
        the order the number overlay paints.

        Derived from the painted rectangles, never from tree order: in a
        2x2 grid a tree order that puts the bottom-left region after the
        top-right one is indistinguishable from a bug, and it is the same
        argument :func:`doxa.layout.neighbour` is built on one level down.
        A group with no painted rectangle yet (mounted this frame, or
        collapsed to nothing) is not numbered, for the same reason
        :meth:`_pane_regions` skips it: an unpainted region is not a
        destination.

        Sorted on the TOP EDGE first and the LEFT EDGE second, so a row of
        three above a row of two numbers 1 2 3 / 4 5. Rows are compared by
        their actual y, not bucketed, because DOXA's own splits always
        align: every region in a row shares an edge by construction."""
        painted = [
            (group.region.y, group.region.x, index, group)
            for index, group in enumerate(self.groups())
            if group.region.width > 0 and group.region.height > 0
        ]
        # The DOM index is the final tie-break so the order is total and
        # deterministic even for two regions that somehow share a corner.
        return [group for _y, _x, _i, group in sorted(painted)]

    def group_of(self, widget: "Any") -> "PaneGroup | None":
        """The group a widget sits in."""
        return split_mod.group_of(widget)

    def _focused_node(self) -> "Any | None":
        """``App.focused`` -- the widget Textual says holds the keyboard --
        or ``None`` when there is no window left to ask.

        Every "which group / which pane / which surface" question below
        starts here, and every one of them is read from MESSAGE HANDLERS.
        That is what makes the raw property the wrong thing to call:
        ``App.focused`` reads ``self.screen``, and at teardown Textual
        clears the screen stacks in ``App._close_all`` and only THEN drains
        the app's own message queue (``_close_messages``). Any handler
        still in flight in that window -- the ``events.DescendantFocus``
        one every closing screen posts as focus comes off its widgets, for
        instance, which :meth:`_hold_focus_for_a_blocking_dialog` listens
        for -- runs against an app whose ``self.screen`` raises
        ``ScreenStackError``, and ``self.focused`` raises with it.

        Through v0.96.0 nothing met that: ``active_pane`` asked the STRIP
        first (``_active_tab``, which swallows) and answered None without
        ever reading ``self.focused``. v0.97.0 made the GROUP the first
        question -- correctly, since "the active tab" is a question about
        a group now -- and thereby moved an unguarded ``self.focused`` in
        front of every ``active_pane`` caller, the error surface included:
        :meth:`_failure_surface` calls ``active_pane`` to decide where to
        DRAW the block, so the raise took out the report of itself as
        well, escaped ``_process_messages`` and failed whichever test was
        running. Measured as exactly that, in the full suite only, where a
        busier loop lands the late event after the stacks are gone:
        tests/test_derive.py::test_looking_at_the_tab_clears_the_staged_tint
        failing at ``run_test`` EXIT with two ``ScreenStackError``s -- the
        handler's, and the surface's on top of it.

        So the guard belongs here rather than in the handler: "nothing
        holds the keyboard" is the honest answer for a window that no
        longer exists, it is the answer every caller was already written
        against, and putting it in one accessor keeps the next handler
        that asks after teardown from having to know any of this."""
        try:
            return self.focused
        except ScreenStackError:
            return None

    def focused_group(self) -> "PaneGroup | None":
        """The ONE group that holds the keyboard.

        Exactly one, which is the pane-groups spec's own focus rule: the
        status bar reflects that group's active tab, and every key that
        means "this tab" means a tab of this group. Derived from
        ``self.focused`` rather than from a flag this app maintains, for
        the reason :meth:`focused_pane` gives -- a flag is a second answer
        to a question the framework already answers, and the two drifting
        apart is how the v0.32.0 restored-active-tab defect happened.

        Falls back to the remembered last group, then to the first in
        reading order, for the case focus is legitimately somewhere that is
        not a group at all (a modal, the command palette, the rename
        field): a window always has an answer to "which group", and jumping
        to a different one while a dialog is up would move the user's work
        under them.

        **The DOM wins, and the remembered id is only a fallback.** That
        is a deliberate choice against the obvious alternative, which was
        tried and reverted. ``Widget.focus()`` is deferred in Textual 5.3
        (it schedules ``screen.set_focus`` with ``call_next``), so for one
        message-pump turn after a split this answers with the group the
        user came FROM -- and it would be tempting to believe
        :meth:`_focus_tab`'s synchronously-recorded intent instead, the way
        ``PaneTab.focused_leaf`` is believed one level down.

        Measured, that costs more than it buys. A group that has not been
        painted yet has a zero-area rectangle, so trusting the intent makes
        the NEXT ``split_active_pane`` in the same turn refuse ("not enough
        height to split: each pane needs 9 rows and this one has 0") and
        makes :meth:`active_pane` answer with a pane from a group the
        keyboard has demonstrably not reached. The window it would fix is
        one transient write that corrects itself the moment the new pane
        boots (``_note_pane_booted`` persists again), so the trade is a
        real refusal against a record nobody reads."""
        node: Any = self._focused_node()
        while node is not None:
            if isinstance(node, PaneGroup):
                self._last_group_id = node.id or self._last_group_id
                return node
            node = node.parent
        remembered = self._last_group_id
        groups = self.groups()
        if remembered:
            for group in groups:
                if group.id == remembered and group.is_mounted:
                    return group
        return next(iter(self._group_order() or groups), None)

    def tabbed_of(self, widget: "Any" = None) -> "TabbedContent | None":
        """The tab strip that owns ``widget``, or -- given nothing -- the
        FOCUSED group's own.

        This is what replaced ``query_one("#session-tabs")`` everywhere in
        this file. Through v0.95.0 a window had exactly one strip and an id
        was the right way to name it; a window has N now, and "the strip"
        is a question about which group, always. Returns ``None`` rather
        than raising, because most callers were already inside a
        ``contextlib.suppress`` for the mid-teardown case and the ones that
        were not read better with an explicit branch."""
        if widget is not None:
            return split_mod.tabbed_of(widget)
        group = self.focused_group()
        if group is None or not group.is_mounted:
            return None
        try:
            tabbed = group.tabbed
        except Exception:  # noqa: BLE001 -- group not composed yet
            return None
        # query_one succeeding is not the same as mounted -- the guard
        # SessionPane._system needed for exactly this, in v0.91.0.
        return tabbed if tabbed.is_mounted else None

    def _strip(self) -> TabbedContent:
        """The FOCUSED group's tab strip, raising when there is none.

        The drop-in for ``query_one("#session-tabs", TabbedContent)``: it
        raises the same way in the same states (nothing mounted, app
        mid-teardown), so every caller that was already wrapped in a
        ``contextlib.suppress`` keeps behaving exactly as it did."""
        group = self.focused_group()
        if group is None:
            raise NoMatches("no pane group is mounted")
        return group.tabbed

    def _strip_for(self, tab_id: str) -> TabbedContent:
        """The strip that HOLDS this tab, falling back to the focused
        group's.

        The fallback is not a shrug: a tab id that no strip holds is a tab
        being created (``add_pane`` has not landed) or one already removed,
        and in both cases the focused group is the only group the caller
        could have meant. Getting this wrong the other way -- defaulting to
        the focused group FIRST -- would let a status write aimed at a
        background group's tab land on the foreground one's."""
        holder = self.tabbed_holding(tab_id)
        return holder if holder is not None else self._strip()

    def tabbed_holding(self, tab_id: str) -> "TabbedContent | None":
        """The strip that holds the tab with this ID, across every group.

        The lookup the tab-status writers need (:func:`doxa.ui.labels.
        _write_tab_class` and friends): a pane writes ``-working`` onto its
        own header, and with N strips "the strip" no longer names one."""
        if not tab_id:
            return None
        for group in self.groups():
            try:
                tabbed = group.tabbed
            except Exception:  # noqa: BLE001 -- not composed yet
                continue
            if not tabbed.is_mounted:
                continue
            with contextlib.suppress(Exception):
                if tabbed.get_pane(tab_id) is not None:
                    return tabbed
        return None

    def _make_tab(self, pane: "SessionPane", *, id: "str | None" = None) -> PaneTab:
        """Wrap one pane in the tab that holds it.

        Every tab in this app is a :class:`~doxa.ui.split.PaneTab` holding
        exactly one surface -- which is what a tab was through v0.88.0 and
        is what it is again since v0.97.0 moved the layout tree up to the
        window. What a later split is built INTO is the empty
        :class:`~doxa.ui.split.SplitBox` chain around the GROUP now; see
        that module's docstring for why it cannot be created on demand.

        The TAB takes the id its pane used to carry, so
        :func:`_restore_pane_id`, :data:`_FALLBACK_PANE_ID`,
        :meth:`_initial_active_tab_id` and every ``tabbed.active =``
        assignment in this file keep naming the same strings."""
        tab = PaneTab(
            pane.born_title, pane,
            # Distinct from the leaf's own id, deliberately. Textual only
            # forbids duplicate ids among SIBLINGS, so a tab and the pane
            # inside it could legally share one -- and every id-selector
            # query in this app would then resolve to whichever the
            # breadth-first walk reached first, which is the tab, silently,
            # for the rest of the release. Derived from the pane's id so a
            # DOM dump still reads as a pair.
            id=id or f"tab-{pane.id}",
        )
        tab.focused_leaf = pane
        return tab

    def _make_group(self, *tabs: "Any", active_id: "str | None" = None) -> PaneGroup:
        """One pane group holding ``tabs``. The window's only leaf kind."""
        self._group_serial += 1
        return PaneGroup(
            *tabs, active_id=active_id, id=f"group-{self._group_serial}"
        )

    def _activate_tab(self, tab: "Any", *, retry: bool = True) -> None:
        """Make TAB the active tab, and survive the two moments Textual's
        own reactive refuses the assignment.

        ``TabbedContent.active`` validates through ``Tabs.validate_active``,
        which raises ``ValueError: No Tab with id …`` whenever the strip
        does not -- yet, or any longer -- hold a header for that pane. Two
        real states reach it, both measured on this branch rather than
        imagined:

        * the tab was added from a WORKER (``/attach``'s own
          ``_cmd_attach_worker``) and the header's mount into ``#tabs-list``
          has not landed by the time the next line runs;
        * the app is being torn down under a worker still finishing an
          attach -- the v0.85.0 defect class, which ``tests/conftest.py``'s
          ``_errors_must_be_claimed`` fixture correctly turns into a test
          failure rather than a silent error block.

        So the assignment is retried ONCE on the next refresh, then given
        up on. Retried rather than suppressed outright, because a new tab
        that silently fails to activate is the "it arrived by accident"
        failure v0.38.0 removed; given up on rather than looped, because
        the teardown case has no later moment in which it could succeed."""
        tab_id = getattr(tab, "id", "") or ""
        if not tab_id:
            return
        try:
            self._strip_for(tab_id).active = tab_id
        except Exception:  # noqa: BLE001 -- see the docstring
            if retry and getattr(tab, "is_mounted", False):
                self.call_after_refresh(self._activate_tab, tab, retry=False)

    def leaf_tabs(self) -> "list[PaneTab]":
        return list(self.query(PaneTab))

    def _tab_of(self, pane: "Any") -> "PaneTab | None":
        return getattr(pane, "tab", None) if isinstance(pane, SessionPane) else None

    def focused_pane(self) -> "SessionPane | None":
        """The ONE pane per window that holds the keyboard.

        Derived from ``self.focused`` -- the widget Textual says has focus
        -- rather than from a flag this app maintains, because a flag is a
        second answer to a question the framework already answers, and the
        two drifting apart is precisely how the v0.32.0 restored-active-tab
        defect happened. Falls back to the active tab's last focused leaf
        for the case where focus is legitimately somewhere that is not a
        pane at all (a modal, the command palette, the rename field): the
        status bar still has to reflect ONE pane, and jumping to the
        tab's first leaf while a dialog is up would move it under the
        user."""
        node: Any = self._focused_node()
        while node is not None:
            if isinstance(node, SessionPane):
                return node
            if isinstance(node, DiffPane):
                # The keyboard is in a diff. "Which session does this
                # keystroke mean" still has an answer, and it is the
                # session the diff is OF -- a key aimed at a session,
                # pressed while looking at that session's diff, is aimed
                # at that session. Falling through to the tab's last
                # focused leaf would usually give the same answer and
                # would give a WRONG one in a tab holding two sessions.
                owner = node.session_pane()
                if owner is not None:
                    return owner
                break
            node = node.parent
        # Focus is somewhere that is not a leaf at all (a modal, the
        # command palette, the rename field). The FOCUSED GROUP's active
        # tab is the answer: the status bar still has to reflect ONE pane,
        # and moving it to some other group's while a dialog is up would
        # change the subject under the user.
        group = self.focused_group()
        tab = group.active_tab() if group is not None else None
        if isinstance(tab, PaneTab):
            leaf = tab.focused_leaf
            if isinstance(leaf, SessionPane) and leaf.is_mounted:
                return leaf
            return next(iter(tab.leaves()), None)
        return None

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
        tabbed = self._strip()
        pane = self._make_pane_at(path, lambda: self._new_session_factory_at(path))
        tab = self._make_tab(pane)
        await tabbed.add_pane(tab)
        self._activate_tab(tab)
        self._focus_tab(tab)
        self._persist_tabset()
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
            return await self._resume_read_only(session_id, cwd, title, reason)
        # Already open in this window? Then the answer is the tab that has
        # it, not a second one beside it -- and since it is open, it is
        # also running, which the registry check above would normally have
        # caught; this covers the in-process (no registry entry) case.
        for pane in self.panes():
            if pane._session_id == session_id:
                self._focus_tab(pane)
                self._strip_for(pane.tab_id or "").active = (
                    pane.tab_id or ""
                )
                return f"{session_id[:8]} is already open in this window."
        tabbed = self._strip()
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
        tab = self._make_tab(pane)
        await tabbed.add_pane(tab)
        self._activate_tab(tab)
        self._focus_tab(tab)
        self._persist_tabset()
        return None

    async def _resume_read_only(
        self, session_id: str, cwd: str, title: str, reason: str,
    ) -> "str | None":
        """:meth:`resume_session`'s own fallback (v0.93.0), for the two
        states :func:`history.resume_state` answers when resuming truly
        cannot happen: ``RESUME_NO_CWD`` (the directory is gone) and
        ``RESUME_NO_HISTORY`` (a pre-v0.56.0 conversation the CLI's own
        store never learned this id under). Through v0.91.0 both landed
        here as a bare refusal string -- "cannot resume ... — <reason>" --
        an error where a real answer was sitting on disk the whole time:
        DOXA's OWN transcript (:mod:`doxa.transcript`, the same
        ``$LORE_PROJECTS_DIR/<slug>/<id>.jsonl`` /search already indexes)
        is a SEPARATE store from the CLI's own resume history that
        :func:`history.resume_state` just found lacking, and neither
        failure reason says anything about whether IT exists.

        So this reaches for the exact read-only surface a dead-daemon
        BOOT restore already falls back to -- :class:`ArchivedSessionTab`,
        the same ``mount_transcript`` call, the same ``resume_note``
        banner explaining WHY it is read-only (see that class's own
        v0.56.0 note: "read-only" with no reason reads as the feature
        having silently not happened) -- rather than building a second
        transcript viewer for the same fact. A session with no transcript
        on disk EITHER falls through to the plain refusal unchanged: there
        is truly nothing to show, and an empty archived tab would be a
        worse answer than the honest words that were already here.

        An already-open archived tab for this SAME session (from an
        earlier read-only resume, or from this window's own boot restore)
        is reused rather than duplicated -- the same "already open" rule
        the top of :meth:`resume_session` applies to a live pane."""
        if not await asyncio.to_thread(transcript_mod.exists, session_id, cwd):
            return f"cannot resume {session_id[:8]} — {reason}"
        existing = next(
            (t for t in self.archived_tabs() if t.session_id == session_id), None,
        )
        if existing is not None:
            self._activate_tab(existing)
            self._focus_tab(existing)
            return f"{session_id[:8]} is already open here, read-only."
        tabbed = self._strip()
        tab = ArchivedSessionTab(
            session_id, cwd, self._tab_title(cwd or self.cwd),
            pinned_name=(title[:40] if title else None),
            id=f"resume-ro-{session_id}",
            resume_note=reason,
        )
        await tabbed.add_pane(tab)
        self._activate_tab(tab)
        self._focus_tab(tab)
        self._persist_tabset()
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
        tabbed = self._strip()
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
        tab = self._make_tab(pane)
        await tabbed.add_pane(tab)
        self._activate_tab(tab)
        self._focus_tab(tab)
        self._persist_tabset()
        return (
            f"{session_id[:8]} is still running — attached to it in a new "
            "tab rather than resuming it. a live conversation has one "
            "writer, and a second would fork it."
        )

    @property
    def active_pane(self) -> SessionPane | None:
        """The session the user is driving: the FOCUSED leaf of the active
        tab (v0.91.0), which through v0.88.0 was the same thing as the
        active tab because a tab held exactly one pane.

        Every engine-touching caller in this file reads this -- the
        palette's actions, ``/mode``, the status refresh, the failure
        surface. With two panes visible, "the tab that is showing" is no
        longer an answer to "which session does this keystroke mean", and
        the pane holding the keyboard is: a key aimed at a session is
        aimed at the session you are typing into.

        **v0.97.0: :meth:`focused_pane` wins outright**, where through
        v0.95.0 its answer was cross-checked against the active tab and
        discarded if it belonged to another one. That check existed because
        a TAB held several panes and the active tab bounded the question.
        With the keyboard now able to sit in a v0.92.0 diff that is a tab
        of its OWN group, the check started discarding the right answer:
        the session a diff is of lives in a different group, ``pane.tab is
        tab`` was false, and ``active_pane`` came back None while a session
        was plainly on screen and being typed at. ``focused_pane`` already
        resolves the diff case deliberately (see its ``DiffPane`` branch);
        second-guessing it here was the defect -- but only for a pane in
        ANOTHER group. Inside the focused group the active tab still bounds
        the question, and it has to: a read-only tab showing (an archived
        session, a subagent transcript) means there IS no session pane
        here, and every caller reads that None as "ask
        ``_close_read_only_tab`` instead". Returning the live pane whose
        prompt still happened to hold focus made Ctrl+Q on an archived tab
        end the neighbouring session -- caught by
        tests/test_restore_view.py, which is exactly the pair of tests that
        distinction exists for."""
        group = self.focused_group()
        tab = group.active_tab() if group is not None else None
        pane = self.focused_pane()
        if (
            pane is not None
            and pane.is_mounted
            and split_mod.group_of(pane) is not group
        ):
            # The keyboard is in some other group -- a v0.92.0 diff is the
            # only way that happens, and the session it is a diff OF is the
            # right answer (focused_pane's own DiffPane branch decided so).
            return pane
        if not isinstance(tab, PaneTab):
            return None
        if pane is not None and pane.is_mounted and pane.tab is tab:
            return pane
        leaf = tab.focused_leaf
        if isinstance(leaf, SessionPane) and leaf.is_mounted:
            return leaf
        return next(iter(tab.leaves()), None)

    def panes(self) -> list[SessionPane]:
        """Every session leaf in the window, in DOM order -- across tabs
        AND across the splits inside one tab. Unchanged as a query; what
        changed is that one tab can now contribute more than one.

        Empty rather than raising on a window that is gone -- the same
        guard, and the same reasoning, as :meth:`groups`. This one is the
        load-bearing case: :meth:`_failure_surface` falls back to it when
        there is no active pane, so an unguarded query here would be the
        error surface failing on the one path it exists for."""
        try:
            return list(self.query(SessionPane))
        except ScreenStackError:
            return []

    def archived_tabs(self) -> "list[ArchivedSessionTab]":
        """Restored tabs whose session is gone (v0.32.0) -- read-only
        transcript tabs, deliberately NOT part of :meth:`panes`, which
        every caller in this file reads as "tabs with a session behind
        them" and must keep reading that way."""
        return list(self.query(ArchivedSessionTab))

    def _active_tab(self) -> "PaneTab | ArchivedSessionTab | None":
        """The FOCUSED GROUP's active tab, when it is one restore CARES
        about -- either kind. ``active_pane`` stays SessionPane-only on
        purpose (every engine-touching caller depends on that); this is
        the one question that spans both.

        "The active tab" is a question about a group since v0.97.0, and
        every caller of this means the group holding the keyboard: which
        tab the status bar reflects, which one Ctrl+W closes, which one
        the record calls active."""
        try:
            tab = self._strip().active_pane
        except Exception:
            return None
        return tab if isinstance(tab, (PaneTab, ArchivedSessionTab)) else None

    def _restorable_tabs(self) -> "list[Any]":
        """Every tab the persisted set is about, IN STRIP ORDER -- session
        panes and archived tabs interleaved exactly as the user sees them,
        because the record's order IS the tab-bar order it will restore
        to. Subagent transcript tabs are not sessions and never appear.

        Groups in LAYOUT order and, within each, strip order -- which is
        what a plain DOM walk gives, because the owner-first invariant
        makes a ``Split``'s children left-to-right / top-to-bottom by
        construction. Deliberately not :meth:`_group_order`'s painted
        reading order: this feeds the flat ``tabs`` list, whose companion
        is the tree written beside it, and the two must agree about order
        or a record disagrees with itself."""
        return [
            tab for tab in self.query(TabPane)
            if isinstance(tab, (PaneTab, ArchivedSessionTab))
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
            return not self._strip().active
        except Exception:
            return True

    # -- focus ownership (v0.38.0) ------------------------------------

    def _focus_tab(self, tab: "Any", *, retry: bool = True) -> None:
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

        A ``SessionPane`` focuses its prompt; the two read-only kinds
        (``ArchivedSessionTab``, ``SubagentTranscriptTab``) focus their own
        ``.scroll`` container instead, so keyboard scrolling works the
        moment you land on one -- v0.85.0, and load-bearing beyond that
        single convenience: :meth:`_cycle_tab` landing on a tab with
        NOTHING focusable left Textual's own ``AUTO_FOCUS`` (``App.
        AUTO_FOCUS = "*"``, fired on the next screen-resume tick while
        ``self.focused`` is ``None``) to pick the first focusable widget
        it could find ANYWHERE in the DOM -- unscoped by which tab is
        actually visible, so it landed back on the SessionPane's own
        prompt in a different, now-hidden tab. Focusing that prompt posts
        ``TabPane.Focused`` right back up to ``TabbedContent``, which
        reactively reassigns ``active`` to ITS tab -- so the cycle
        silently reverted itself one message-pump turn later, the exact
        shape of the reported defect ("only seems to work ... not between
        read-only finished sessions"). Giving every tab kind SOMETHING
        focusable closes the gap AUTO_FOCUS was falling into, at the
        source, for every caller in the list above, not just cycling.

        **v0.91.0: a tab may hold several panes, so this needs to name
        ONE.** It takes the tab's remembered focused leaf -- the pane the
        keyboard was in the last time the user was in this tab -- rather
        than its first, because coming back to a split tab and landing
        somewhere other than where you left is the same class of surprise
        as restoring onto the wrong tab. A brand-new tab's remembered leaf
        is the one it was built with. Accepts a PANE as well as a tab, for
        the callers that already have the leaf they mean (a split, a
        directional move)."""
        # Which GROUP this focus move means, remembered for the case
        # ``self.focused`` stops naming a group at all -- a modal, the
        # command palette, the rename field. NOT believed over the DOM:
        # see :meth:`focused_group` for the measurement that settled that.
        group = split_mod.group_of(tab)
        if group is not None and group.id:
            self._last_group_id = group.id
        if isinstance(tab, SessionPane):
            owner = tab.tab
            if isinstance(owner, PaneTab):
                owner.focused_leaf = tab
            try:
                tab.query_one("#prompt-input", PromptInput).focus()
            except Exception:  # noqa: BLE001 -- see below
                # A leaf mounted THIS turn has not composed its own
                # subtree yet: ``mount`` resolves when the widget is in
                # the DOM, and its children arrive on the next
                # message-pump turn. Suppressing that used to be
                # harmless, because the only caller was acting on a tab
                # that had been on screen for a while; a SPLIT focuses a
                # pane it created a moment ago, and swallowing the miss
                # would leave the keyboard in the pane the user split
                # AWAY from -- silently, and only sometimes. So the
                # intent is re-stated on the next refresh instead of
                # dropped.
                #
                # ONCE, and the bound is load-bearing rather than
                # defensive: a pane being torn down never grows a prompt,
                # so an unbounded re-state is a callback that schedules
                # itself every refresh forever -- an app that never goes
                # idle, which is the busy-idle bug GitLine's docstring
                # warns about with a tighter loop.
                if retry:
                    self.call_after_refresh(self._focus_tab, tab, retry=False)
                return
            self._clear_seen_marks(tab)
            return
        if isinstance(tab, PaneTab):
            leaf = tab.focused_leaf
            if not (isinstance(leaf, SessionPane) and leaf.is_mounted):
                leaf = next(iter(tab.leaves()), None)
            if leaf is not None:
                self._focus_tab(leaf)
            return
        if isinstance(tab, (ArchivedSessionTab, SubagentTranscriptTab)):
            with contextlib.suppress(Exception):
                tab.scroll.focus()

    def _clear_seen_marks(self, pane: "SessionPane") -> None:
        """"You are looking at this now" -- for the ONE pane that just got
        the keyboard, and never for its visible siblings (v0.91.0).

        The three affordances (`-done-unseen`, the needs-input blink, the
        `-staged` tint) all cleared on tab ACTIVATION through v0.88.0,
        which was the same event as "this pane got the keyboard" while a
        tab held one pane. It is not the same event any more, and the spec
        settles which of the two it should follow: the marker means *you
        have not looked at this*, and a pane in the corner of a 2x2 grid
        may genuinely be unread. So visible-but-unfocused does NOT count
        as seen; only focus clears. The panes beside it keep their marks
        until the keyboard actually arrives there."""
        pane._set_tab_class("-done-unseen", False)
        pane.set_needs_input(False)
        pane.set_staged(False)

    def _focus_active_tab(self) -> None:
        """:meth:`_focus_tab` for whichever tab is active RIGHT NOW --
        for the callers that set ``TabbedContent.active`` by id and would
        otherwise have to look the pane back up themselves. Safe to call
        immediately after that assignment: ``active`` is a plain reactive,
        so ``active_pane`` resolves synchronously once it is set (it is
        only the INITIAL value that arrives late -- see
        :meth:`_activation_pending`)."""
        with contextlib.suppress(Exception):
            tabbed = self._strip()
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

    def _persist_tabset(self, *, exclude_session_id: "str | None" = None) -> None:
        """Snapshot the CURRENT tab set to $DOXA_HOME/tabsets/<scope>.json
        (doxa.tabsets.save) -- called on every tab-set change (open,
        rename, close-detach, close-stop, app exit). Unconditional on the
        restore_tabs SETTING (that only gates whether a later launch
        READS this file, see doxa.tabsets.enabled/config's own note) --
        gated only on a restore still being in flight (_restore_pending).

        TWO sources, merged: panes still mounted (in tab-bar order, LIVE
        only -- a _stopped one is skipped, see below) and
        _detached_this_run (sessions Ctrl+W'd out of the strip earlier
        this run, which keep running and therefore STAY in the set per
        item D #4). _ended_this_run (Ctrl+Q, palette-stopped) is
        deliberately NOT a source here -- see that dict's own docstring.
        v0.55.0 dropped a _stopped mounted pane on the spot, because
        ending a session really did mean losing the tab for good. v0.56.0
        pinned the doxa session id to the CLI's own
        (SessionEngine._build_options), which is what makes --resume able
        to replay a transcript DOXA itself indexed, and v0.60.0 read that
        as license to stop excluding a _stopped pane here at all -- "the
        daemon is gone" no longer meant "the tab is gone", so why should
        ending a session cost the user the tab. It still can: v0.60.0
        never noticed that a pinned-id resume plays back LIVE, not
        read-only (finalize() never touches the CLI's own history store),
        so a session the user explicitly ended with Ctrl+Q came back next
        launch exactly as if it had never closed. v0.99.1 restores the
        v0.55.0 exclusion -- a mounted _stopped pane is skipped here again
        -- which is the one piece of this method that changed; the
        _resume-a-dead-session-as-archived_ path (doxa.cli.ended_tab_spec)
        this reverses nothing about, because that path is only ever
        reached for a session that IS still in the persisted set (a
        Ctrl+W detach whose daemon later died on its own), which is
        exactly the case v0.55.0 never touched either. The one thing that
        still has to win over both sources is an EXPLICIT reap
        (_killed_this_run, `/sessions kill`) -- checked below wherever a
        record could otherwise slip through.

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
        rather than relying on that safe-but-wasteful fallback.

        ``exclude_session_id`` (v0.85.0): one more session to leave out of
        every source above, for exactly one call -- :meth:`_close_pane`'s
        own ``is_last`` branch, which needs THIS snapshot to read as
        though the closing tab were already gone without actually
        unmounting it first. Still load-bearing for a Ctrl+W is_last close
        (the pane is only DETACHED, never marked _stopped, so nothing else
        here would exclude it); redundant but harmless for a Ctrl+Q
        is_last close since v0.99.1 -- pane.stop() already marked it
        _stopped by the time this runs, so the mounted-pane scan's own
        exclusion above would have caught it anyway. An earlier version of
        that fix called
        ``remove_pane`` before this method to get the same exclusion out
        of the mounted-pane scan -- which worked, but unmounted a pane
        with a still-running ``_peer_pump`` worker moments before
        ``action_quit`` tore the app down under it, a teardown race
        (measured as an intermittent ``AssertionError`` out of
        ``SessionPane._peer_pump``'s own ``assert self.engine is not
        None``, surfaced as a visible in-app error block on the way out)
        that plain exclusion does not create: the pane stays mounted,
        exactly as unconditional ``App.action_quit`` already handled it
        pre-v0.85.0, and only what gets WRITTEN changes."""
        if self._restore_pending > 0:
            return
        scope = peers_mod.main_repo_root_of(self.cwd) or self.cwd
        active_tab = self._active_tab()
        # WHICH LEAF is the active one, asked in the order that is true
        # synchronously. ``Widget.focus()`` is deferred in Textual 5.3 (it
        # schedules ``screen.set_focus`` with ``call_later``), so right
        # after Ctrl+T -- which activates the new tab and focuses its leaf
        # in the same handler, then persists -- ``active_pane`` still reads
        # the pane the user came FROM. ``PaneTab.focused_leaf`` is written
        # by ``_focus_tab`` synchronously, so it is the answer that is
        # already correct at this instant; ``active_pane`` is the fallback
        # for a tab nobody has focused into yet. Getting this backwards
        # saved the wrong active session -- the same class of defect as
        # v0.38.0's null active id, with a wrong value instead of a
        # missing one.
        active_leaf = None
        if isinstance(active_tab, PaneTab):
            leaf = active_tab.focused_leaf
            if isinstance(leaf, SessionPane) and leaf.tab is active_tab:
                active_leaf = leaf
            else:
                active_leaf = self.active_pane
        tabs: "list[tabsets_mod.TabRecord]" = []
        seen: set[str] = set()
        active_id: "str | None" = None
        # Tab-strip order within a group, groups in layout order, and BOTH
        # kinds of restorable tab: a live PaneTab and (v0.32.0) an
        # ArchivedSessionTab, which is one of the user's open tabs too and
        # must not evaporate on the next restart just because the session
        # behind it already has.
        for tab in self._restorable_tabs():
            if isinstance(tab, ArchivedSessionTab):
                if tab.session_id in seen or tab.session_id == exclude_session_id:
                    continue
                seen.add(tab.session_id)
                tabs.append(tab.as_record())
                if tab is active_tab:
                    active_id = tab.session_id
                continue
            for pane in tab.leaves():
                sid = pane._session_id
                if (
                    not sid or sid in seen or sid in self._killed_this_run
                    or sid == exclude_session_id or pane._stopped
                ):
                    continue
                # A _stopped pane (Ctrl+Q, palette stop) is excluded here
                # again as of v0.99.1 -- see this method's own docstring
                # for the v0.60.0 detour and why it did not hold. A pane
                # can still be MOUNTED and _stopped at once (pane.stop()
                # marks the flag and clears the engine handle, but nothing
                # unmounts the pane itself until _close_pane's caller gets
                # around to it, deliberately -- see is_last's own
                # comment), so this is reached mid-close, not just at
                # startup.
                pane_cwd = str(getattr(pane.engine, "cwd", None) or pane.cwd)
                pane_scope = peers_mod.main_repo_root_of(pane_cwd) or pane_cwd
                if pane_scope != scope:
                    continue
                seen.add(sid)
                tabs.append(tabsets_mod.TabRecord(sid, pane.custom_name, pane_cwd))
                if pane is active_leaf or (
                    active_leaf is None and tab is active_tab
                ):
                    active_id = sid  # noqa: E501 -- see active_leaf above
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
        # _detached_this_run ONLY (v0.99.1 -- _ended_this_run used to sit
        # here too; it no longer does, see that dict's own docstring): a
        # session whose tab already left the strip is in this flat dict
        # and nowhere else -- there is no layout left to remember for it,
        # and inventing one would put it back somewhere the user never had
        # it. _fill_group appends it to the first group at restore time,
        # which is "it comes back as a tab", the same answer v0.91.0 gave
        # with a single-leaf tree.
        for record in self._detached_this_run.values():
            if (
                record.session_id in seen
                or record.session_id in self._killed_this_run
                or record.session_id == exclude_session_id
            ):
                continue
            seen.add(record.session_id)
            tabs.append(record)
        # The WINDOW's layout, read off the widgets and then PRUNED to the
        # sessions that actually made it into the flat list above (a
        # cross-repo pane, a reaped one, the excluded last tab). A tree
        # that still named them would restore a pane-shaped hole; the
        # survivors take the space proportionally instead.
        groups = split_mod.tree_of(self._window_root())
        kept = [record.session_id for record in tabs]
        if groups is not None:
            groups = layout_mod.prune(groups, kept)
        # v1.0.0: the COLLECTIONS ride along, pruned to the same flat list
        # the tree is pruned to, so the three halves of the record agree
        # about which sessions exist. Pruned on the app's own copy too, not
        # only in the record -- a collection that kept naming a reaped
        # session would put a dead row back on the rail the moment
        # something else caused a refresh.
        self._collections = collections_mod.prune(self._collections, kept)
        with contextlib.suppress(Exception):
            tabsets_mod.save(
                scope, tabs, active_id, groups=groups,
                collections=self._collections,
                rail_folded=tuple(self._rail_folded),
            )
        # The rail is a view of exactly this snapshot, so the one method
        # that runs on every tab lifecycle event is the one place it needs
        # refreshing from. Suppressed and last: a persistence path must
        # never be the thing that fails because chrome could not repaint.
        with contextlib.suppress(Exception):
            self.refresh_sidebar()

    def _window_root(self) -> "SplitBox | None":
        """The OUTERMOST :class:`~doxa.ui.split.SplitBox` on the screen --
        the window's layout tree, which through v0.95.0 lived one per tab
        and now lives once per window."""
        for box in self.query(SplitBox):
            if not isinstance(box.parent, SplitBox):
                return box
        return None

    # -- the session sidebar (v1.0.0) ---------------------------------
    #
    # Everything here is about a widget that is a SIBLING of the window
    # root, never a member of it. Nothing in this section touches the
    # tree, and nothing in the tree section above knows the rail exists --
    # that separation is the feature's whole design (see
    # doxa/ui/sidebar.py) and the reason _window_root() one method up
    # needed no change at all.

    def sidebar(self) -> "SessionSidebar | None":
        """The rail, or ``None`` when there is no window left to ask.

        Guards BOTH conditions v0.97.0 learned to guard: ``NoMatches``
        (the rail is not composed yet) and ``ScreenStackError`` (the
        screen stacks are cleared before the app's queue is drained, so a
        late handler asking a plain inventory question gets a raise) --
        and then ``is_mounted`` on top, because ``query_one`` succeeding
        means the node is in the DOM, not that it is mounted."""
        try:
            rail = self.query_one(SessionSidebar)
        except (NoMatches, ScreenStackError):
            return None
        return rail if rail.is_mounted else None

    def sidebar_width(self) -> int:
        """The rail's width, clamped to what a rail can be: the width a
        drag is showing right now, else the configured one."""
        if self._sidebar_width_override is not None:
            return layout_mod.clamp_sidebar_width(self._sidebar_width_override)
        return config_mod.sidebar_width()

    def _window_width(self) -> int:
        """The whole window's width in cells, or 0 when nothing is
        painted. Read off the SCREEN and not off the rail: a hidden widget
        has no geometry (v0.99.0's whole defect), so measuring the thing
        that is about to be shown is measuring zero."""
        try:
            return int(self.size.width)
        except (ScreenStackError, AttributeError):
            return 0

    def _narrowest_group(self) -> int:
        """The narrowest PAINTED group, in cells -- what
        :func:`doxa.layout.sidebar_refusal` prices the refusal against.

        Painted, never structural: a group with a zero-area rectangle is
        one that has not been laid out yet, and it is not a region the
        rail can take columns from. The same rule ``_pane_regions`` and
        ``_group_order`` state."""
        widths = [
            group.region.width
            for group in self.groups()
            if group.region.width > 0 and group.region.height > 0
        ]
        return min(widths) if widths else 0

    def _narrowest_group_unrailed(self) -> int:
        """The narrowest painted group as it would be with NO rail at all.

        :func:`doxa.layout.sidebar_refusal` takes the tree's width before
        the rail costs it anything -- which is what ``_narrowest_group``
        measures when the rail is hidden, and is exactly what it does NOT
        measure when the rail is already open. Opening asks the question
        once, from the hidden state, so v1.0.0 never had to tell them
        apart; RESIZING asks it from the shown state, and feeding an
        already-shrunk number back in would price the rail's cost twice
        and refuse a width that fits.

        Undoing that shrink is the same proportion the refusal applies:
        the tree got ``total - rail`` of ``total``."""
        narrowest = self._narrowest_group()
        rail = self.sidebar()
        if narrowest <= 0 or rail is None or rail.styles.display == "none":
            return narrowest
        total = self._window_width()
        tree = total - int(rail.outer_size.width or 0)
        if tree <= 0 or total <= 0:
            return narrowest
        return narrowest * total // tree

    def sidebar_refusal(self, width: "int | None" = None) -> "str | None":
        """Why the rail cannot open -- or cannot be this WIDE -- right now,
        or ``None``.

        ``width`` is the candidate a drag or a key is proposing;
        :meth:`sidebar_width` is the default, which is the question
        ``F3`` asks. **One function answers both**, which is the whole
        point: a drag that refused at a looser floor than opening does
        would let the mouse build an arrangement the app will not create
        interactively, and the arrangement would then be the one thing
        neither ``F3`` nor a restart could reproduce."""
        return layout_mod.sidebar_refusal(
            self._window_width(),
            self._narrowest_group_unrailed(),
            self.sidebar_width() if width is None else
            layout_mod.clamp_sidebar_width(width),
        )

    def sidebar_has_something_to_say(
        self, order: "list[str] | None" = None
    ) -> bool:
        """HIDE AT ZERO, the question the ``auto`` setting asks.

        A rail listing one session under no heading is chrome that answers
        nothing -- the same judgment
        :data:`doxa.layout.GROUP_STRIP_MIN_COLS`,
        :data:`doxa.ui.labels.CTX_ABSOLUTE_MIN_COLS` and
        :data:`doxa.diff.SIDE_BY_SIDE_MIN_COLS` each make about their own
        surface. So: any collection at all, or more than one session.

        ``order`` is :meth:`_sidebar_order`'s answer when the caller
        already has it -- see :meth:`refresh_sidebar` on why that list is
        derived exactly once per refresh."""
        if self._collections:
            return True
        known = self._sidebar_order() if order is None else order
        return len(known) > 1

    def sidebar_should_show(self, order: "list[str] | None" = None) -> bool:
        """Should the rail be on screen, before width is considered?"""
        mode = config_mod.sidebar_mode()
        if mode == config_mod.SIDEBAR_OFF:
            return False
        if mode == config_mod.SIDEBAR_ON:
            return True
        return self.sidebar_has_something_to_say(order)

    # -- the rail's model ---------------------------------------------

    def _sidebar_order(self) -> "list[str]":
        """Every session the rail knows about, in the order LOOSE ones
        should appear.

        THREE sources, and the third is the design check
        docs/plans/session-sidebar.md asks this feature to answer: mounted
        session panes and archived tabs in strip order, then the sessions
        this window has open but does NOT currently show -- detached
        (Ctrl+W, ``/detach``) and ended (Ctrl+Q) ones, which stay in the
        persisted set and are exactly the peers a user loses track of.

        That third source is what makes the rail a session INDEX rather
        than a second tab strip. A reaped session (``/sessions kill``) is
        not in it: reaping is the one gesture in this app that means
        "forget this conversation", and it means it here too."""
        order: "list[str]" = []
        seen: "set[str]" = set()
        for tab in self._restorable_tabs():
            for session_id in self._tab_session_ids(tab):
                if session_id and session_id not in seen:
                    seen.add(session_id)
                    order.append(session_id)
        for record in (
            *self._detached_this_run.values(), *self._ended_this_run.values()
        ):
            sid = record.session_id
            if sid and sid not in seen and sid not in self._killed_this_run:
                seen.add(sid)
                order.append(sid)
        return [s for s in order if s not in self._killed_this_run]

    @staticmethod
    def _tab_session_ids(tab: "Any") -> "list[str]":
        """The session ids one restorable tab contributes. A ``PaneTab``
        answers through its leaves (a diff tab has none, correctly); an
        ``ArchivedSessionTab`` carries its own."""
        if isinstance(tab, ArchivedSessionTab):
            return [tab.session_id]
        return [
            leaf._session_id
            for leaf in getattr(tab, "leaves", list)()
            if getattr(leaf, "_session_id", "")
        ]

    def _sidebar_pane(self, session_id: str) -> "Any | None":
        """The mounted surface for a session, of either kind, or
        ``None``."""
        for pane in self.panes():
            if pane._session_id == session_id:
                return pane
        for tab in self.archived_tabs():
            if tab.session_id == session_id:
                return tab
        return None

    def _sidebar_surfaces(self) -> "dict[str, Any]":
        """Every session id on screen, mapped to its surface, in ONE pass.

        :meth:`_sidebar_pane` answers for one id and is the right shape
        for :meth:`reveal_session`, which asks once. A RAIL asks once per
        row, and ``panes()``/``archived_tabs()`` are ``self.query(...)``
        -- a full walk of the screen's widget tree, transcript blocks
        included. Per row that is a walk per session per refresh; this is
        the same answer derived once. Panes win over archived tabs on a
        tie for the same reason :meth:`_sidebar_pane` looks at them
        first: a live session outranks a read-only record of one."""
        surfaces: "dict[str, Any]" = {}
        for pane in self.panes():
            session_id = getattr(pane, "_session_id", "")
            if session_id and session_id not in surfaces:
                surfaces[session_id] = pane
        for tab in self.archived_tabs():
            if tab.session_id and tab.session_id not in surfaces:
                surfaces[tab.session_id] = tab
        return surfaces

    def _sidebar_panes(self) -> "list[triage_mod.PaneEntry]":
        """Every pane GROUP in this window, as the rail's entries.

        **A rail entry is a pane, not a session** (v1.2.0, Part 1b).
        Since v0.97.0 each :class:`doxa.ui.split.PaneGroup` owns its own
        tab strip, so one visible pane can hold three sessions of which
        two are invisible -- and the invisible tab needing input is
        exactly what v1.0.0's flat rail could not surface.

        Read off the widgets, once per refresh, in the same pass
        discipline :meth:`_sidebar_surfaces` states: ``groups()`` and
        ``tabs()`` are queries, and asking them per ROW would be the
        walk-per-session cost v1.0.0 measured at +22% layout time.
        Sessions no group claims -- detached, ended, archived -- are not
        invented here; :func:`doxa.triage.entries_for` gives each its own
        single-member entry, which is the honest reading: there is no
        visible tab because there is no pane."""
        entries: "list[triage_mod.PaneEntry]" = []
        for group in self.groups():
            members: "list[str]" = []
            for tab in group.tabs():
                if not isinstance(tab, (PaneTab, ArchivedSessionTab)):
                    continue
                for session_id in self._tab_session_ids(tab):
                    if session_id and session_id not in members:
                        members.append(session_id)
            if not members:
                continue
            active_tab = group.active_tab()
            active = ""
            if isinstance(active_tab, (PaneTab, ArchivedSessionTab)):
                ids = self._tab_session_ids(active_tab)
                active = ids[0] if ids else ""
            entries.append(
                triage_mod.PaneEntry(
                    group.entry_key, tuple(members), active
                )
            )
        return entries

    @staticmethod
    def _pane_ctx(pane: "Any") -> "float | None":
        """This session's context share, or ``None`` when its limit was
        never reported.

        The CLI's OWN accounting, read straight off the engine attribute
        the ctx chip prints (:attr:`doxa.engine.Engine.last_ctx_percentage`)
        -- not a second measurement, and not a guess. ``None`` stays
        ``None`` all the way to the glyph, where it renders nothing at
        all: ``/context``'s rule that an unreported limit reads ``?`` and
        stays ``?``, one level down. Treating it as 0% would turn an
        honesty rule into a wrong answer -- the rail would say "plenty of
        room" about a window it never measured."""
        engine = getattr(pane, "engine", None)
        value = getattr(engine, "last_ctx_percentage", None)
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _pane_repo_root(pane: "Any") -> str:
        """The project this session belongs to, as an identity key.

        ``GitLine.main_root`` (v1.2.0), which is the MAIN checkout's root
        even from inside a linked worktree and costs no subprocess: the
        pane's status line already resolved it at construction. Falling
        back to the pane's cwd rather than to "" keeps two sessions in
        one non-repo directory grouped together, which is the same answer
        :attr:`doxa.peers.PeerInfo.scope_key` gives (``repo_root or
        cwd``)."""
        git = getattr(pane, "_git", None)
        root = getattr(git, "main_root", None) if git is not None else None
        if root:
            return str(root)
        engine = getattr(pane, "engine", None)
        return str(getattr(engine, "cwd", None) or getattr(pane, "cwd", "") or "")

    def _describe_session(
        self, session_id: str, surfaces: "dict[str, Any] | None" = None
    ) -> "triage_mod.Facts":
        """One session's :class:`doxa.triage.Facts` for the rail.

        The label is rendered FRESH every time and never stored: a
        collection records session IDS because ``display_name()`` is not
        stable -- it changes when a session is renamed and again when its
        first prompt lands.

        The marks come from :func:`doxa.ui.labels.mark_over`, the same
        derivation a group's ``Tab`` header uses. Not a second reading of
        what a mark MEANS -- one source, read twice, which is the risk the
        spec names and this is the answer to it.

        An UNMOUNTED session still gets a label: its pinned name from the
        record this run kept, or its short id. It never gets marks (there
        is no pane to have earned one) and it is reported ``mounted=False``
        so the row can say so rather than pretend.

        ``surfaces`` is :meth:`_sidebar_surfaces`' one-pass map when the
        caller built one; ``None`` falls back to the single-id lookup, so
        this stays callable on its own."""
        surface = (
            self._sidebar_pane(session_id)
            if surfaces is None else surfaces.get(session_id)
        )
        if isinstance(surface, SessionPane):
            marks = tuple(
                name for name in labels_mod.TAB_STATE_MARKS
                if labels_mod.mark_over([surface], name)
            )
            return triage_mod.Facts(
                label=surface.display_name(),
                marks=marks,
                mounted=True,
                ctx_percentage=self._pane_ctx(surface),
                state=triage_mod.STATE_LIVE,
                repo_root=self._pane_repo_root(surface),
            )
        if surface is not None:  # an ArchivedSessionTab: read-only, no marks
            return triage_mod.Facts(
                label=getattr(surface, "base_label", "") or session_id[:8],
                mounted=True,
                # An archived tab is a read-only record of a session that
                # is already over: ENDED, and therefore old, whether or
                # not THIS run is the one that ended it.
                state=triage_mod.STATE_ENDED,
                repo_root=str(getattr(surface, "cwd", "") or ""),
            )
        detached = self._detached_this_run.get(session_id)
        record = detached or self._ended_this_run.get(session_id)
        pinned = getattr(record, "pinned_name", None) if record else None
        return triage_mod.Facts(
            label=(pinned or session_id[:8]),
            mounted=False,
            # DETACHED is not OLD. A detached session is live and may be
            # doing work right now -- see doxa.triage.OLD_STATES for the
            # whole of that decision and what it deliberately excludes.
            state=(
                triage_mod.STATE_DETACHED if detached is not None
                else triage_mod.STATE_ENDED
            ),
            repo_root=str(getattr(record, "cwd", "") or "") if record else "",
        )

    def sidebar_rows(
        self, order: "list[str] | None" = None
    ) -> "list[SidebarRow]":
        """The rail's contents, built by the pure function that decides
        what a rail shows -- see :func:`doxa.ui.sidebar.build_rows`."""
        surfaces = self._sidebar_surfaces()
        return build_rows(
            self._collections,
            self._sidebar_order() if order is None else order,
            lambda session_id: self._describe_session(session_id, surfaces),
            width=self.sidebar_width(),
            panes=self._sidebar_panes(),
            collapsed_groups=tuple(self._rail_folded),
        )

    # -- painting -----------------------------------------------------

    def refresh_sidebar(self, *, force: bool = False) -> None:
        """Re-derive the rail: visibility, width, contents.

        Called from ``_persist_tabset`` (every tab lifecycle event), from
        every collection edit, and from the rail's own ``on_show``.
        ``force`` only bypasses the widget's own "nothing changed" check
        and is what ``on_show`` passes: rows mounted while the rail was
        ``display: none`` were laid out against a zero box."""
        rail = self.sidebar()
        if rail is None:
            return
        # ONE derivation of "which sessions does this window know about"
        # per refresh. _sidebar_order() is self.query(TabPane) -- a full
        # walk of the screen's widget tree -- and this method used to run
        # it twice (through sidebar_should_show, then again through
        # sidebar_rows) with two more walks PER ROW inside
        # _describe_session. Textual's layout and stylesheet passes are
        # synchronous, so DOM work done on a paint path is paid for in
        # event-loop stall (tests/test_split_panes.py measures exactly
        # that), not in microseconds.
        order = self._sidebar_order()
        show = self.sidebar_should_show(order)
        note = self.sidebar_refusal() if show else None
        visible = show and note is None
        rail.set_width(self.sidebar_width())
        rail.styles.display = "block" if visible else "none"
        if not visible:
            # A HIDDEN rail is not built. Not an optimisation for its own
            # sake: this method runs on every tab lifecycle event and on
            # every terminal resize, and on the overwhelmingly common
            # window -- one session, no collections, rail off -- building
            # rows nobody can see would be work done on every keystroke's
            # worth of state change. The rail's own ``on_show`` forces the
            # build back the instant it gets geometry, which is the same
            # remembered-intent shape ``SessionPane.scroll_transcript_to_end``
            # uses for the same measured reason (v0.99.0): a hidden widget
            # has no geometry, so the work has to wait for the show.
            rail.set_rows([])
            return
        if force:
            rail._rows = []
        rail.set_rows(self.sidebar_rows(order))

    def refresh_sidebar_marks(self, pane: "Any") -> None:
        """One pane's marks moved. Update that ROW rather than the rail.

        Called from ``SessionPane._set_tab_class``, which is also what
        drives the needs-input blink -- at 2 Hz, per waiting session. A
        rebuild there would be a repaint of the whole rail per blink,
        which is the busy-idle cost this app measures and refuses. If the
        row is not there at all the structure moved, and the full refresh
        is the right answer once.

        **A rail that is not showing is not touched at all**, and that is
        the load-bearing half. ``display: none`` means the rail holds no
        rows (``refresh_sidebar`` empties it), so ``apply_marks`` could
        never find one and the fallback below fired on EVERY mark toggle
        -- ``-working`` on and off per turn, ``-done-unseen``,
        ``-staged``, ``-attention`` per blink -- in every window in the
        app, which is the overwhelmingly common one because the rail is
        hidden by default. Each of those ran the whole derivation. A mark
        moving is by definition NOT a structure change: the structure is
        refreshed by ``_persist_tabset`` on every tab lifecycle event, by
        every collection edit, and by the rail's own ``on_show`` the
        instant it gets geometry."""
        rail = self.sidebar()
        if rail is None or rail.styles.display == "none":
            return
        session_id = getattr(pane, "_session_id", "") or ""
        if not session_id:
            return
        marks = tuple(
            name for name in labels_mod.TAB_STATE_MARKS
            if labels_mod.mark_over([pane], name)
        )
        if not rail.apply_marks(session_id, marks, self._pane_ctx(pane)):
            self.refresh_sidebar()

    def on_resize(self, event: "events.Resize") -> None:
        """The terminal changed size, so the width refusal may have.

        A rail refused for width is not a rail the user stopped wanting --
        they never chose to shrink the window -- so it opens again for
        free the moment there is room, and closes again when there is not.
        The same measured-not-remembered posture ``PaneGroup.on_resize``
        takes for its own tab strip one level down."""
        with contextlib.suppress(Exception):
            self.refresh_sidebar()

    # -- toggling -----------------------------------------------------

    def set_sidebar(self, visible: bool) -> "str | None":
        """Show or hide the rail, and WRITE that choice. Returns the one
        line the user is told, or ``None``.

        The write is what ends hide-at-zero's guessing (see
        :func:`doxa.config.sidebar_mode`): a user who closed the rail must
        not have it reappear because they opened a second tab.

        A refusal does NOT write. The rail could not open at this width,
        which is a fact about the terminal and not a decision about the
        rail -- recording it as one would leave the user's next, wider
        window without the sidebar they asked for."""
        if visible:
            note = self.sidebar_refusal()
            if note:
                return note
        with contextlib.suppress(Exception):
            config_mod.save({"sidebar": "1" if visible else "0"})
        config_mod.invalidate()
        self.refresh_sidebar(force=True)
        return None

    def action_toggle_sidebar(self) -> None:
        """F3. Reports a width refusal where the user is looking --
        the active pane's transcript -- rather than doing nothing, which
        is the "documented key that silently does nothing" failure
        v0.39.0 exists to prevent."""
        rail = self.sidebar()
        showing = bool(rail is not None and rail.styles.display != "none")
        note = self.set_sidebar(not showing)
        if note:
            self.notify_sidebar(note)

    def notify_sidebar(self, note: str) -> None:
        """Put one rail message in front of the user. The active pane's
        transcript when there is one, Textual's own notification
        otherwise (a window whose only tab is an archive has no
        transcript to write into)."""
        pane = self.active_pane
        if pane is not None:
            pane.run_worker(pane._system(note), group="sidebar-note")
            return
        with contextlib.suppress(Exception):
            self.notify(note)

    # -- reveal -------------------------------------------------------

    def reveal_session(self, session_id: str) -> "str | None":
        """Take me to that session: focus its group, activate its tab.

        Returns the line to show when it cannot -- and "cannot" is a real
        answer here, not a defect. The rail lists sessions this window
        knows about, including ones that are not mounted in any group, so
        a row can genuinely name a session there is nowhere to go TO. It
        says so and names the door back in (``/attach``) rather than
        pretending a click did something."""
        if not session_id:
            return None
        surface = self._sidebar_pane(session_id)
        if isinstance(surface, SessionPane):
            # _switch_to_tab does the three beats every explicit switch in
            # this file does -- activate, move the marker, focus -- and
            # focusing a pane puts the keyboard in its GROUP, which is what
            # "focus its group" means since v0.97.0.
            self._switch_to_tab(surface.id or "")
            return None
        if surface is not None:  # an archived tab: activate it, focus its scroll
            self._activate_tab(surface)
            self._jump_tab_marker()
            self._focus_tab(surface)
            return None
        return (
            f"{session_id[:8]} is not open in this window — "
            f"/attach {session_id[:8]} brings it back in a new tab"
        )

    @on(SessionSidebar.Revealed)
    def _on_sidebar_revealed(self, event: "SessionSidebar.Revealed") -> None:
        event.stop()
        note = self.reveal_session(event.session_id)
        if note:
            self.notify_sidebar(note)

    def focus_group_by_key(self, entry_key: str) -> bool:
        """Put the keyboard in the group with that ``entry_key``, WITHOUT
        touching which of its tabs is active. Returns whether there was
        such a group.

        The distinction is the whole of option C's heading gesture. A
        group's heading summarises every tab it holds, including the ones
        that are not on screen, so a click on it that ALSO switched the
        active tab would be the rail changing what you are looking at as
        a side effect of asking about it -- and there would then be no
        gesture left that means "go there and leave it alone".

        Focusing the group's ACTIVE tab is what "focus the group" means
        since v0.97.0, and focusing a widget inside the tab that is
        already active cannot activate a different one."""
        for group in self.groups():
            if group.entry_key != entry_key:
                continue
            surface = next(iter(group.surfaces()), None)
            if surface is not None:
                self._focus_tab(surface)
            return True
        return False

    def toggle_group_expanded(self, entry_key: str) -> None:
        """Fold or unfold one pane group's tab rows, and REMEMBER it.

        Persisted in the tabset record beside the collapsed flag a
        collection already has (:mod:`doxa.tabsets`), because a fold is a
        statement about how the user wants to read this window and a
        window that forgot it on every restart would be asking them to
        make it again."""
        if not entry_key:
            return
        if entry_key in self._rail_folded:
            self._rail_folded.discard(entry_key)
        else:
            self._rail_folded.add(entry_key)
        self.refresh_sidebar(force=True)
        self._persist_tabset()

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

    def resize_sidebar(
        self, width: int, *, persist: bool = True
    ) -> "str | None":
        """Set the rail's width. Returns the refusal, or ``None``.

        **The same floor opening refuses at**, asked through the same
        :meth:`sidebar_refusal` -- see that method. ``persist`` writes it
        to the settings registry, which the drag defers to its last event
        and the keys do on every press."""
        want = layout_mod.clamp_sidebar_width(width)
        note = self.sidebar_refusal(want)
        if note:
            return note
        if persist:
            self._sidebar_width_override = None
            with contextlib.suppress(Exception):
                config_mod.save({"sidebar_width": str(want)})
            config_mod.invalidate()
        else:
            self._sidebar_width_override = want
        rail = self.sidebar()
        if rail is not None:
            rail.set_width(want)
        return None

    def action_sidebar_wider(self) -> None:
        self._nudge_sidebar(1)

    def action_sidebar_narrower(self) -> None:
        self._nudge_sidebar(-1)

    def _nudge_sidebar(self, step: int) -> None:
        """``Alt+Shift+←/→``: the rail divider from the KEYBOARD.

        A mouse-only divider is unreachable for a keyboard user, and it is
        also unreachable over an ssh session to a terminal with no mouse
        reporting -- this project has ruled on that twice, and the two
        dividers it already has (``Ctrl+↑/↓`` in-pane, ``Alt+arrow`` for a
        leaf) are both keyboard gestures first.

        Refusals are REPORTED here and swallowed in the drag: one press is
        one statement, and a user who pressed a key and saw nothing happen
        is owed the reason (the v0.39.0 rule about a documented key that
        silently does nothing)."""
        rail = self.sidebar()
        if rail is None or rail.styles.display == "none":
            self.notify_sidebar(
                "the session sidebar is hidden — F3 or /sidebar opens it"
            )
            return
        note = self.resize_sidebar(self.sidebar_width() + step)
        if note:
            self.notify_sidebar(note)

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

    # -- collections --------------------------------------------------
    #
    # One shape for all five: apply the pure function from
    # doxa.collections, keep its note, repaint, persist. The MODEL refuses
    # (a duplicate name, an unknown one, an empty one) and this layer
    # never second-guesses it -- the same division doxa.layout's
    # split_refusal keeps from DoxaApp.split_active_pane.

    def _apply_collections(
        self, result: "tuple[Any, str | None]"
    ) -> "str | None":
        items, note = result
        if note is None:
            self._collections = items
            self.refresh_sidebar(force=True)
            self._persist_tabset()
        return note

    def collection_new(self, name: str) -> "str | None":
        return self._apply_collections(
            collections_mod.new(self._collections, name)
        )

    def collection_rename(self, old: str, new_name: str) -> "str | None":
        return self._apply_collections(
            collections_mod.rename(self._collections, old, new_name)
        )

    def collection_delete(self, name: str) -> "str | None":
        return self._apply_collections(
            collections_mod.delete(self._collections, name)
        )

    def collection_assign(self, name: str, session_id: str) -> "str | None":
        return self._apply_collections(
            collections_mod.assign(self._collections, name, session_id)
        )

    def collection_unassign(self, session_id: str) -> "str | None":
        return self._apply_collections(
            collections_mod.unassign(self._collections, session_id)
        )

    def collections(self) -> "tuple[collections_mod.Collection, ...]":
        """The window's collections. A tuple of frozen records, so a
        caller reading them cannot edit them by accident -- every edit
        goes through the five methods above."""
        return self._collections

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

    def _restored_pane(self, spec: "RestoreTabSpec", leaf: "Any" = None) -> SessionPane:
        """One restored LIVE leaf, from the spec doxa.cli resolved and
        (v0.91.0) the layout leaf that says where in its tab it sits.

        Extracted from :meth:`compose` when a tab stopped being one pane:
        a split tab builds several of these, and every one of them needs
        the identical restore wiring -- the pinned name applied before
        boot, the resume-vs-reattach choice about where the scrollback
        comes from, the saved cwd, and (v0.91.0) the saved position of the
        pane's own status-bar divider."""
        pane = SessionPane(
            self._tab_title(), self.cwd, self.model,
            spec.engine_factory,
            # The TAB keeps ``restore-<session id>`` -- that is the string
            # _initial_active_tab_id, ``tabbed.active`` and the persisted
            # record's own lookups all name. The LEAF inside it needs an id
            # of its own now that the two are different widgets, and it is
            # derived from the same one so a DOM dump still reads.
            id=f"{_restore_pane_id(spec.session_id)}-leaf",
        )
        if spec.pinned_name:
            pane._initial_pinned_name = spec.pinned_name
        if spec.resume:
            # v0.56.0: this tab's session had ENDED, and it is coming back
            # LIVE, continuing that conversation (doxa.cli decided that;
            # the engine_factory above spawns with --resume). Its
            # scrollback comes from the same transcript file a reattach
            # reads, minus the backlog-skip precondition -- a freshly
            # spawned daemon has no ring to replay on top. See
            # SessionPane._restore_transcript.
            pane._resume_from = spec.session_id
        else:
            # v0.32.0: this pane's scrollback comes from the session's
            # persisted transcript, not the daemon's 512-frame ring (see
            # SessionPane._restore_transcript).
            pane._restore_transcript_wanted = True
        pane._restore_cwd = spec.cwd
        if leaf is not None:
            pane.prompt_ratio = layout_mod.clamp_prompt_ratio(leaf.prompt_ratio)
        return pane

    def _restore_group_tree(self) -> "layout_mod.Node | None":
        """The WINDOW's layout tree for this restore, pruned to the specs
        that actually came back and guaranteed to place every one of them.

        Answers for all three record eras by delegating to the ONE reader
        that knows them -- :func:`doxa.tabsets._fill_group`, the same
        function :func:`doxa.tabsets._layout_groups` ends every branch with
        -- so a launch through ``doxa.cli`` (which passes ``restore_groups``
        straight through) and a launch through a hand-built ``DoxaApp``
        (which may pass only the older ``restore_layout``) cannot disagree
        about what a saved record means.

        The pruning is what makes a restore honest: the saved tree names
        sessions, and by the time this runs some of them are dead. A tree
        that still named them would restore a region with nothing in it."""
        specs = self._restore_tabs
        if not specs:
            return None
        records = [
            tabsets_mod.TabRecord(s.session_id, s.pinned_name, s.cwd)
            for s in specs
        ]
        tree = self._restore_groups
        if tree is None and self._restore_layout:
            # A caller that only had the v0.91.0 shape. The composition
            # rule is doxa.tabsets' -- the ACTIVE tab's tree is the window,
            # the rest become its tabs -- restated here only as the choice
            # of WHICH tree, because that module's copy reads a raw record
            # and this one has already-parsed trees in hand.
            chosen = self._restore_layout[0]
            if self._restore_active_id:
                for candidate in self._restore_layout:
                    ids = {
                        leaf.session_id
                        for leaf in layout_mod.leaves(candidate)
                    }
                    if self._restore_active_id in ids:
                        chosen = candidate
                        break
            tree = layout_mod.groupify(chosen)
        alive = {s.session_id for s in specs}
        pruned = layout_mod.prune(tree, alive) if tree is not None else None
        return tabsets_mod._fill_group(pruned, records, self._restore_active_id)

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

    def _compose_restored_root(self) -> "Any":
        """The restored window: one tree of groups, each holding its own
        tabs, in saved order throughout.

        v0.32.0 mixes two kinds in that order -- a live spec reattaches its
        daemon (``SessionPane``), an archived one has no daemon left and
        renders its transcript read-only (``ArchivedSessionTab``) --
        v0.92.0 adds a third (a ``DiffPane`` tab, restored as a diff with
        nothing to reattach), and v0.97.0 adds no kind at all: it only
        changes which container they land in.

        No pane arms a mount-time focus (v0.38.0): a restored pane mounts
        in the BACKGROUND, and which group ends up focused is decided once,
        explicitly, in :meth:`_activate_initial_tab`. v0.23.0's "three
        restored tabs always land on the last one" defect was that same
        entanglement.

        The report block (if any) rides on the first LIVE pane -- an
        archived tab already opens with a block of its own explaining what
        it is."""
        specs = {s.session_id: s for s in self._restore_tabs}
        tree = self._restore_group_tree()
        placed: "set[str]" = set()
        first_pane: "list[SessionPane]" = []

        def _tab_for(leaf: "layout_mod.Leaf") -> "Any":
            spec = specs.get(leaf.session_id)
            if leaf.is_diff:
                # v0.92.0: the diff restores as a diff, with no session
                # behind it and nothing to reattach -- it re-reads
                # `git diff` on mount. A QUEUED-but-unapplied rejection
                # does NOT survive, because it is held on the widget and
                # the widget is new; it is discarded WITH the pane, and
                # the pane comes back showing the un-reverted hunk, which
                # is the truth.
                surface = DiffPane(
                    leaf.session_id,
                    leaf.cwd or (spec.cwd if spec else None) or self.cwd,
                    id=f"{_restore_pane_id(leaf.session_id)}-diff",
                )
                return PaneTab(
                    self._tab_title(), surface,
                    id=f"{_restore_pane_id(leaf.session_id)}-diff-tab",
                )
            if spec is None:
                return None
            if spec.archived:
                return ArchivedSessionTab(
                    spec.session_id,
                    spec.cwd or self.cwd,
                    self._tab_title(spec.cwd or self.cwd),
                    pinned_name=spec.pinned_name,
                    id=_restore_pane_id(spec.session_id),
                    # v0.56.0: read-only is now the FALLBACK, so the tab
                    # says which of the reasons it was.
                    resume_note=spec.resume_note,
                )
            pane = self._restored_pane(spec, leaf)
            if not first_pane:
                first_pane.append(pane)
                pane._boot_report = self._restore_report
            return PaneTab(
                self._tab_title(), pane, id=_restore_pane_id(spec.session_id),
            )

        def _group(node: "layout_mod.Group") -> "Any":
            tabs: "list[Any]" = []
            active_id = ""
            for index, leaf in enumerate(node.tabs):
                if leaf.session_id in placed and not leaf.is_diff:
                    continue
                tab = _tab_for(leaf)
                if tab is None:
                    continue
                placed.add(leaf.session_id)
                if index == node.active or not active_id:
                    # ``initial=`` must name a tab that EXISTS: v0.91.0
                    # measured what happens when it does not -- Textual's
                    # ContentSwitcher hangs waiting for it, surfacing as a
                    # Pilot timeout before a single assertion runs. So the
                    # saved active index only wins if its tab survived, and
                    # the first surviving tab is the standing fallback.
                    if index == node.active or not tabs:
                        active_id = tab.id or ""
                tabs.append(tab)
            return self._make_group(*tabs, active_id=active_id)

        if tree is None:
            pane = self._make_pane(self._engine_factory)
            pane._boot_report = self._restore_report
            return split_mod.chain(
                self._make_group(self._make_tab(pane, id=self._FALLBACK_PANE_ID))
            )
        root = split_mod.build(tree, _group)
        if not first_pane:
            # Every resolved tab was archived: the window would otherwise
            # have no session in it at all -- no prompt, nothing Ctrl+W
            # could close without closing the app. One fresh tab alongside
            # the archives, carrying the report, is the same answer
            # doxa.cli's own "everything is dead" branch gives. It joins
            # the FIRST group rather than opening a second one: an archive
            # and its replacement are not two regions of work.
            pane = self._make_pane(self._engine_factory)
            pane._boot_report = self._restore_report
            first = split_mod.first_group(root)
            if first is not None:
                first._tabs.append(
                    self._make_tab(pane, id=self._FALLBACK_PANE_ID)
                )
        return root

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

    def _pane_for_tab(self, tab: Any) -> "SessionPane | None":
        from textual.widgets._tabbed_content import ContentTab

        pane_id = ContentTab.sans_prefix(tab.id or "")
        for pane in self.panes():
            if pane.tab_id == pane_id:
                return pane
        return None

    async def _start_rename(self, pane: "SessionPane") -> None:
        """Mount the editor in the tab's own slot and hide the tab behind
        it, so the label is edited where the label IS."""
        if self.query("#tab-rename"):
            return  # one rename at a time
        with contextlib.suppress(Exception):
            tabbed = self._strip_for(pane.tab_id)
            tab = tabbed.get_tab(pane.tab_id)
            editor = TabRename(pane.tab_id, pane.display_name())
            editor.styles.width = max(len(editor.value) + 4, 14)
            await tab.parent.mount(editor, before=tab)
            tab.display = False
            editor.focus()

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

    def _end_rename(self, pane_id: str) -> None:
        with contextlib.suppress(Exception):
            tabbed = self._strip_for(pane_id)
            tabbed.get_tab(pane_id).display = True
        for editor in list(self.query(TabRename)):
            editor.remove()
        pane = next((p for p in self.panes() if p.tab_id == pane_id), None)
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

            tabs = self._strip().query_one(Tabs)
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

        All three steps are stated here, in order: mount, activate, focus
        -- and then PERSIST, which is the fourth (v0.91.0). Focus used to
        arrive on its own, from the pane's own mount, and activation used
        to arrive as a side effect of THAT -- so the keystroke's outcome
        was really a race with Textual's mount scheduling (v0.38.0).

        The explicit persist closes the last thread of that same race, at
        the other end: a pane whose engine answers instantly (every
        FakeEngine in the suite, and a warm daemon reattach) can finish
        booting INSIDE the ``add_pane`` await, and ``_note_pane_booted``
        then writes the tab set before the next two lines have said which
        tab is active. Nothing wrote it again, so the record kept naming
        the tab the user came from -- measured as a real failure of
        tests/test_tabsets.py's own append test under a full-suite run,
        and not reproducible on its own."""
        tabbed = self._strip()
        pane = self._make_pane(self._new_session_factory)
        tab = self._make_tab(pane)
        await tabbed.add_pane(tab)
        self._activate_tab(tab)
        self._focus_tab(tab)
        self._persist_tabset()

    # -- splits (v0.91.0) ---------------------------------------------

    async def action_split_pane(self) -> None:
        """Ctrl+O (Alt+S under the kitty protocol) / ``/split`` -- a fresh
        session STACKED BELOW this
        one, in the same tab. vim's sense of the word, which is the sense
        ``/split`` has always had here."""
        note = await self.split_active_pane(layout_mod.COLUMN)
        if note:
            self.notify(note, severity="warning", timeout=8)

    async def action_vsplit_pane(self) -> None:
        """Ctrl+N (Alt+D under the kitty protocol) / ``/vsplit`` -- a fresh
        session SIDE BY SIDE with
        this one, in the same tab."""
        note = await self.split_active_pane(layout_mod.ROW)
        if note:
            self.notify(note, severity="warning", timeout=8)

    async def split_active_pane(self, orientation: str) -> "str | None":
        """Divide the focused pane. Returns a refusal to show the user, or
        ``None`` when it happened.

        Two independent sessions side by side -- the spec's own reading of
        its second open question -- so this spawns through the SAME
        ``new_session_factory`` Ctrl+T uses. A split is a new session in
        the tab you are already in, not a second view onto the one that is
        there.

        **Focus goes to the NEW pane, and it goes there explicitly.** A
        leaf mounts unfocused (v0.38.0's rule, which splits inherit rather
        than re-litigate) and whatever creates it says where the keyboard
        goes; a user who just asked for a second pane is asking to work in
        it, the same way Ctrl+T's new tab takes the keyboard. The pane it
        was split off keeps rendering, keeps streaming, and keeps any
        "you missed something" mark it had -- visible is not focused, and
        neither is seen.

        Refused, with a message and no change at all, when the resulting
        panes would be below the floor (:func:`doxa.layout.split_refusal`)
        or when this pane has already spent its depth allowance
        (:data:`doxa.layout.SPLIT_SLOTS`). A refusal that performed a
        sliver would be worse than the refusal."""
        group = self.focused_group()
        if group is None:
            return "there is no pane group here to split"
        box = split_mod.free_box(group)
        if box is None:
            return (
                f"this pane is already split as deep as DOXA goes "
                f"({layout_mod.SPLIT_SLOTS} levels) — close a pane, or "
                "split one of its neighbours instead"
            )
        region = group.region
        refusal = layout_mod.split_refusal(region.width, region.height, orientation)
        if refusal is not None:
            return refusal
        new_pane = self._make_pane(self._new_session_factory)
        new_group = self._make_group(self._make_tab(new_pane))
        await box.mount(split_mod.chain(new_group))
        box.divide(orientation)
        self._focus_tab(new_pane)
        self._persist_tabset()
        return None

    # -- live diff (v0.92.0) ------------------------------------------

    async def action_toggle_diff(self) -> None:
        """F2 (Alt+G under the kitty protocol) / ``/diff``."""
        note = await self.toggle_diff_pane()
        if note:
            self.notify(note, severity="warning", timeout=8)

    def diff_pane_for(self, session_id: str) -> "DiffPane | None":
        """This session's diff leaf, if it has one open anywhere."""
        for pane in self.query(DiffPane):
            if pane.session_id == session_id:
                return pane
        return None

    async def toggle_diff_pane(
        self, pane: "SessionPane | None" = None,
    ) -> "str | None":
        """Open a session's live diff BESIDE it, or close it. Returns a
        refusal to show the user, or ``None`` when it happened.

        ``pane`` defaults to the focused session -- what F2 and ``/diff``
        mean by "this session". v1.0.1 gives it a caller-supplied
        alternative for the two doors that are aimed at a PARTICULAR
        session rather than at the keyboard's: the status chip (which is
        painted inside one pane's own bar) and the ``auto_diff``
        auto-open (whose tick can arrive from a session in a background
        tab, which is precisely the session it must not diff the wrong
        neighbour of).

        This is the spec's design check on v0.91.0's split, run for real:
        *session left, diff right, both live*. It reuses
        :meth:`split_active_pane`'s machinery verbatim -- the same free
        box, the same :func:`doxa.layout.split_refusal` floor, the same
        ``ROW`` orientation -- and differs in exactly one line, the
        widget that goes into the new half. Nothing about the split had
        to be special-cased for a non-session leaf ONCE
        :attr:`doxa.layout.Leaf.view` existed, which is the honest
        version of "the split could express it".

        Toggling closes rather than refusing a second one: a session has
        one diff, per the spec's answer to its own third open question
        (per-session, matching the isolation model -- two sessions in
        worktrees off the same branch have two different diffs)."""
        pane = pane if pane is not None else self.active_pane
        if pane is None:
            return "there is no session pane here to diff"
        tab = pane.tab
        if tab is None:
            return "this pane is not in a tab yet"
        existing = self.diff_pane_for(pane._session_id)
        if existing is not None:
            if existing.queued:
                return (
                    f"{len(existing.queued)} rejection(s) are still queued "
                    "in this diff — they apply when the turn ends. closing "
                    "the pane now would discard them."
                )
            await self._close_group_tab(existing)
            self._focus_tab(pane)
            self._persist_tabset()
            return None
        return await self._open_diff_beside(pane)

    async def _open_diff_beside(self, pane: "SessionPane") -> "str | None":
        """The OPEN half of :meth:`toggle_diff_pane`, extracted in v1.0.1
        so the auto-open setting reaches it without going through a
        toggle -- an automatic open must never be able to CLOSE a diff
        the user is reading, which is what calling the toggle blind would
        do the moment one was already there.

        Every rule the hand-driven open follows is here and nowhere else:
        the free box, the :func:`doxa.layout.split_refusal` floor, the
        ``ROW`` orientation, and the deliberate absence of a focus
        call."""
        if not pane._session_id:
            return "this session has not started yet — nothing to diff"
        # The group holding THIS pane, falling back to the focused one:
        # with a caller-supplied pane (the chip, the auto-open) the
        # keyboard may be somewhere else entirely, and the diff has to
        # land beside the session it is a diff of.
        group = split_mod.group_of(pane) or self.focused_group()
        if group is None:
            return "there is no pane group here to diff"
        box = split_mod.free_box(group)
        if box is None:
            return (
                f"this pane is already split as deep as DOXA goes "
                f"({layout_mod.SPLIT_SLOTS} levels) — close a pane and "
                "try again"
            )
        region = group.region
        refusal = layout_mod.split_refusal(
            region.width, region.height, layout_mod.ROW
        )
        if refusal is not None:
            return refusal
        diff = DiffPane(
            pane._session_id,
            str(getattr(pane.engine, "cwd", None) or pane.cwd),
            id=f"{pane.id}-diff",
        )
        # The spec's own design check, answered by construction: the diff
        # goes into a GROUP's tab, and nothing about the group had to be
        # special-cased for it -- a group's tab list is a list of surfaces
        # and a diff is a surface. Beside the session rather than in the
        # same group's strip, because the point of a live diff is looking
        # at it WHILE you type; the same tab list would hide one behind the
        # other. Both statements are true at once, and that is what makes
        # the model right rather than merely accommodating.
        diff_tab = PaneTab(self._tab_title(), diff, id=f"{pane.id}-diff-tab")
        await box.mount(split_mod.chain(self._make_group(diff_tab)))
        box.divide(layout_mod.ROW)
        # Focus STAYS in the session. A split spawns a session you asked
        # to work in, so v0.91.0 moves the keyboard there; a diff is
        # something you asked to LOOK at while you keep typing, and
        # moving the keyboard out of the prompt to open it would be the
        # opposite of the feature. "Visible and focused are different
        # states" cuts both ways, and this is the other way.
        #
        # v1.0.1 makes that load-bearing rather than merely tidy: the
        # `auto_diff` setting opens this pane while the user is typing,
        # unasked. A surface that took the keyboard on its way in would
        # eat the next characters of a prompt someone is mid-sentence
        # with -- so the absence of a `_focus_tab` call here is asserted
        # by tests/test_diff_chip.py, not just described.
        self._persist_tabset()
        return None

    async def _close_group_tab(self, surface: "Any") -> None:
        """Take ONE tab out of its group, and take the group with it when
        that was its last.

        The single teardown path for "a surface is going away" -- the diff
        toggle and :meth:`_close_pane` both reach it. Two levels of
        collapse, in order, because they are two different facts: a group
        that still has tabs keeps its region and shows another tab; a group
        with none is not a region any more, and the split above it collapses
        by exactly the rule v0.91.0 wrote for a leaf.

        Awaits each removal rather than firing and forgetting: the NEXT
        step reads the parent's child list, and Textual's ``Widget.remove``
        only takes effect when its ``AwaitRemove`` is awaited."""
        group = split_mod.group_of(surface)
        tab = surface.parent
        while tab is not None and not isinstance(tab, TabPane):
            tab = tab.parent
        if group is None or tab is None:
            with contextlib.suppress(Exception):
                await surface.remove()
            return
        remaining = [t for t in group.tabs() if t is not tab]
        with contextlib.suppress(Exception):
            await self._strip_for(tab.id or "").remove_pane(tab.id or "")
        if remaining:
            return
        box = split_mod.owning_box(group)
        with contextlib.suppress(Exception):
            await group.remove()
        await split_mod.prune_boxes(box)

    def _pane_regions(self) -> "dict[str, tuple[int, int, int, int]]":
        """Every VISIBLE surface's painted rectangle, keyed by widget id.

        Painted, not structural: the spec's testing bar says a split must
        render two panes with non-zero width and height, because the
        invisible-button defect passed every structural assertion for a
        whole release. Directional focus reads the same rectangles the
        user is looking at, so a pane that is not actually on screen is
        not a destination.

        Across every GROUP since v0.97.0, and only each group's ACTIVE tab:
        an inactive tab is mounted and running but is not painted, and the
        two facts have to stay apart here -- reading every tab would let
        ``Ctrl+Shift+→`` land the keyboard somewhere the user cannot see,
        which is the invisible-button defect in its keyboard form."""
        out: "dict[str, tuple[int, int, int, int]]" = {}
        for group in self.groups():
            # surfaces(), not leaves(): v0.92.0's diff pane is a surface
            # you can focus and scroll, so "rectangles the keyboard can
            # move to" is not the same list as "sessions".
            for leaf in group.surfaces():
                region = leaf.region
                if region.width > 0 and region.height > 0 and leaf.id:
                    out[leaf.id] = (region.x, region.y, region.width, region.height)
        return out

    def focus_pane_towards(self, direction: str) -> bool:
        """Move the keyboard to the geometrically adjacent pane. Returns
        whether it moved -- ``False`` at the edge of the layout, which is
        deliberately silent: an arrow key that has nowhere to go should do
        nothing, not complain."""
        here = self.focused_surface()
        if here is None or not here.id:
            return False
        target_id = layout_mod.neighbour(self._pane_regions(), here.id, direction)
        if target_id is None or target_id == here.id:
            return False
        # Across every GROUP (v0.97.0): the rectangles the keyboard can
        # move to are the ACTIVE tab of each region, which is exactly what
        # _pane_regions just answered with.
        surfaces = [
            surface for group in self.groups() for surface in group.surfaces()
        ]
        target = next((p for p in surfaces if p.id == target_id), None)
        if target is None:
            return False
        if isinstance(target, DiffPane):
            # A diff has no prompt to focus, so _focus_tab's "focus the
            # pane's prompt" contract does not apply; the widget itself
            # takes the keyboard, which is what makes it scrollable.
            target.focus()
            return True
        self._focus_tab(target)
        return True

    def focused_surface(self) -> "Any | None":
        """The LEAF holding the keyboard, of whatever kind.

        The geometric twin of :meth:`focused_pane`, which answers the
        different question "which session does this keystroke mean" and
        deliberately keeps answering with a session even while the
        keyboard is in a diff."""
        node: Any = self._focused_node()
        while node is not None:
            if isinstance(node, (SessionPane, DiffPane)):
                return node
            node = node.parent
        return self.focused_pane()

    def action_focus_pane_left(self) -> None:
        self.focus_pane_towards("left")

    def action_focus_pane_right(self) -> None:
        self.focus_pane_towards("right")

    def action_focus_pane_up(self) -> None:
        self.focus_pane_towards("up")

    def action_focus_pane_down(self) -> None:
        self.focus_pane_towards("down")

    # -- dividers (v0.91.0) -------------------------------------------

    def action_divider_up(self) -> None:
        """Ctrl+Up: grow the transcript, shrink the prompt area.

        Acts on the FOCUSED leaf's own status-bar divider -- each leaf has
        one, and that is how the spec resolves "Ctrl+Up/Down cannot mean
        two things" once splits exist. The divider BETWEEN leaves has its
        own gesture (Alt+arrow, :meth:`grow_pane_towards`) rather than
        being silently overloaded onto this pair."""
        pane = self.active_pane
        if pane is not None and pane.nudge_prompt(-1):
            self._persist_tabset()

    def action_divider_down(self) -> None:
        """Ctrl+Down: grow the prompt area, shrink the transcript."""
        pane = self.active_pane
        if pane is not None and pane.nudge_prompt(1):
            self._persist_tabset()

    def grow_pane_towards(self, direction: str) -> bool:
        """Alt+arrow: move the divider BETWEEN this pane and its
        neighbour, growing this pane in ``direction``.

        Finds the nearest ancestor split whose orientation matches the
        axis being asked about, and nudges the boundary on this pane's
        side of it. A drag changes weights and weights persist, so this
        writes the tab set like any other layout change.

        Reads :meth:`focused_surface`, not :meth:`active_pane`: with the
        keyboard in a v0.92.0 diff leaf, Alt+← must widen the DIFF, not
        the session it is a diff of. This is the gesture the live-diff
        spec asks for by name ("a left/right split needs the sibling
        gesture" to Ctrl+Up/Down) and it needed no new key at all --
        v0.91.0 had already built it; it only had to stop assuming every
        leaf was a session."""
        pane = self.focused_surface()
        if pane is None:
            return False
        want = (
            layout_mod.ROW if direction in ("left", "right")
            else layout_mod.COLUMN
        )
        # Start from the GROUP, not the surface (v0.97.0): the boxes that
        # divide the window sit above the group, and a surface's own parent
        # chain now runs through its tab and its strip first. Reading
        # ``focused_surface`` and then climbing from the pane -- what this
        # did through v0.95.0 -- found no SplitBox at all and the key went
        # silently dead, which is how it was caught.
        node: Any = split_mod.group_of(pane) or pane
        parent = node.parent
        while isinstance(parent, SplitBox):
            if parent.is_used and parent.orientation == want:
                kids = list(parent.children)
                index = kids.index(node)
                forward = direction in ("right", "down")
                # Growing FORWARD means pushing the divider after this
                # child; growing BACKWARD means pulling the divider before
                # it, which is the same divider seen from the other side.
                moved = (
                    parent.nudge(index, self.DIVIDER_STEP) if forward
                    else parent.nudge(index - 1, -self.DIVIDER_STEP)
                )
                if moved:
                    self._persist_tabset()
                return moved
            node = parent
            parent = node.parent
        return False

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

    def action_focus_group(self, number: int) -> None:
        """``Ctrl+<digit>`` -- put the keyboard in the group at that
        position, and flash every group's number.

        Both, always, and in that order. The jump happens IMMEDIATELY: the
        overlay is feedback and teaching, not a mode, and DOXA does not
        wait for a second keystroke the way tmux's ``display-panes`` does,
        because the numbering is meant to become muscle memory and a
        prompt-then-wait gesture never lets it.

        The flash fires even when the digit names NO group -- pressing
        Ctrl+7 in a two-group layout shows 1 and 2 and moves nothing. That
        is the case it earns the most in: it answers "what are my choices"
        for a user who guessed."""
        groups = self._group_order()
        self._flash_group_numbers()
        if 1 <= number <= len(groups):
            target = groups[number - 1]
            surface = next(iter(target.surfaces()), None)
            if surface is not None:
                self._focus_tab(surface)

    def _flash_group_numbers(self) -> None:
        """Paint each group's own number over its own region, briefly.

        **Nothing at all when there is only one group**: there is no choice
        to make, so there is nothing to teach. Hide-at-zero, as everywhere
        else in this app.

        **One-shot, never an interval.** DOXA has a no-timer rule and its
        target is IDLE CPU -- v0.78.0 already amended it for the turn
        spinner on the grounds that a timer existing only during a turn
        spends nothing. A ``set_timer`` armed by a keystroke and fired once
        is the same bargain: no interval, nothing running while idle, and
        the previous one is cancelled before a new one is armed so a held
        key cannot stack them.

        Drawn per group from the same rectangles the numbering is derived
        from, so what is numbered and what is painted cannot disagree."""
        self._cancel_group_flash()
        groups = self._group_order()
        if len(groups) < 2:
            return
        for index, group in enumerate(groups, start=1):
            group.show_number(index)
        with contextlib.suppress(Exception):
            self._group_flash_timer = self.set_timer(
                self.GROUP_FLASH_SECS, self._hide_group_numbers
            )

    def _cancel_group_flash(self) -> None:
        timer, self._group_flash_timer = self._group_flash_timer, None
        if timer is not None:
            with contextlib.suppress(Exception):
                timer.stop()

    def _hide_group_numbers(self) -> None:
        self._group_flash_timer = None
        for group in self.groups():
            group.hide_number()

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

    def _dismiss_group_numbers(self, event: "events.Key") -> None:
        """Any subsequent key takes the overlay away at once.

        The spec's own instruction ("cancelled on the next key"), and the
        reason the flash never outstays a user who is already moving. A
        ``Ctrl+<digit>`` is exempt because it is the key that arms one --
        Textual delivers the key event and runs the action from the same
        press, and without this exemption the flash would cancel itself."""
        if self._group_flash_timer is None:
            return
        key = event.key or ""
        if key.startswith("ctrl+") and key[-1].isdigit():
            return
        self._cancel_group_flash()
        self._hide_group_numbers()

    def focus_group_number(self, number: int) -> "str | None":
        """``/pane <n>`` -- the door that always works, for the terminals
        where ``Ctrl+<digit>`` produces no byte at all. Returns a refusal
        to show the user, or ``None`` when it happened."""
        groups = self._group_order()
        if len(groups) < 2:
            return "there is only one pane group — nothing to jump to"
        if not (1 <= number <= len(groups)):
            return (
                f"there is no pane group {number} — this window has "
                f"{len(groups)}, numbered left to right then top to bottom"
            )
        self.action_focus_group(number)
        return None

    async def move_tab_to_group(self, number: int) -> "str | None":
        """``/movepane <n>`` -- take the focused group's ACTIVE tab and put
        it in the group at that position. Returns a refusal, or ``None``.

        **This is the constraint the whole design turns on.** Textual 5.3
        cannot re-parent a mounted widget: ``mount`` of an already-mounted
        widget is a silent no-op that ORPHANS it (measured in v0.91.0, not
        assumed). So this does NOT move the tab. It builds a NEW tab and a
        NEW surface at the destination, hands the new surface the SESSION
        the old one was driving, and tears the old tab down.

        The session survives untouched because the session does not live in
        the widget: it lives in the daemon, behind an engine handle
        (``doxa.client.EngineClient``, or an in-process ``SessionEngine``),
        and ``SessionPane.adopt`` is what re-seats that handle. The pane is
        a VIEW of a session, and this is the first gesture in DOXA that
        makes the difference load-bearing rather than academic.

        Refused, with no change at all, when there is nowhere to move to,
        when the destination is where the tab already is, or when the tab
        is the only one in a group that would then have to close -- moving
        the last tab OUT of a group is a close and a move at once, and the
        two have different undo stories."""
        groups = self._group_order()
        if len(groups) < 2:
            return "there is only one pane group — nothing to move a tab to"
        if not (1 <= number <= len(groups)):
            return (
                f"there is no pane group {number} — this window has "
                f"{len(groups)}, numbered left to right then top to bottom"
            )
        source = self.focused_group()
        target = groups[number - 1]
        if source is None:
            return "there is no pane group here to move a tab out of"
        if source is target:
            return f"this tab is already in pane group {number}"
        tab = source.active_tab()
        if tab is None:
            return "there is no tab here to move"
        # SESSION tabs only, and the guard is on the METHOD rather than on
        # the result: an archived tab and a subagent transcript are both
        # ``TabPane``s in this strip and neither has ``leaves()`` at all,
        # so asking one would be an AttributeError rather than a refusal.
        leaves = getattr(tab, "leaves", None)
        pane = next(iter(leaves()), None) if callable(leaves) else None
        if pane is None:
            return (
                "only a session tab can be moved between groups today — "
                "a diff belongs beside the session it is a diff of, and a "
                "read-only tab has nothing to re-seat"
            )
        if len(source.tabs()) < 2:
            return (
                f"this is pane group {groups.index(source) + 1}'s last tab — "
                "moving it would close the group. close it with Ctrl+W, or "
                "split the destination instead"
            )
        return await self._reseat_pane(pane, target)

    async def _reseat_pane(self, pane: "SessionPane", target: "PaneGroup") -> "str | None":
        """Re-create ``pane`` as a tab of ``target`` and tear down the
        original, carrying the live session across.

        The order is the load-bearing part, and every step of it exists
        because of the no-re-parenting constraint:

        1. take the engine handle OFF the source pane, so its teardown
           cannot stop or detach a session that is about to keep running;
        2. mount the new pane in the destination, and only then hand it the
           handle -- a pane that boots before it is adopted would spawn a
           SECOND session, which is the failure this ordering prevents;
        3. remove the source tab, collapsing nothing (the source keeps its
           other tabs, which :meth:`move_tab_to_group` guaranteed).

        Never raises: a half-completed move would leave a session with no
        view onto it, which is worse than a refusal."""
        engine = pane.engine
        session_id = pane._session_id
        if engine is None or not session_id:
            return "this session has not started yet — nothing to move"
        name = pane.custom_name
        cwd = str(getattr(engine, "cwd", None) or pane.cwd)
        ratio = layout_mod.clamp_prompt_ratio(getattr(pane, "prompt_ratio", 0.0))
        marks = dict(getattr(pane, "_marks", {}))
        source_tab = pane.tab
        # 1. Release the session from the pane that is going away. From
        #    here the daemon has no view onto it, which is a state DOXA is
        #    already fluent in -- it is exactly what Ctrl+W leaves behind.
        pane.release_engine()
        # 2. The new view. Its "factory" hands back the handle that is
        #    already running rather than building one, and ``_adopted``
        #    is what stops ``_boot`` calling ``start()`` on it -- the one
        #    line between "the session moved" and "a second CLI is now
        #    writing this transcript".
        fresh = self._make_pane_at(cwd, lambda: engine)
        fresh._adopted = True
        fresh._session_id = session_id
        if name:
            fresh._initial_pinned_name = name
        fresh.prompt_ratio = ratio
        # The scrollback comes back from DISK, the same v0.32.0 path a
        # reattach uses: the widget is new, so the blocks the old one had
        # painted went with it, and re-reading the transcript is the only
        # honest way to put them back.
        fresh._restore_transcript_wanted = True
        new_tab = self._make_tab(fresh)
        try:
            await target.tabbed.add_pane(new_tab)
        except Exception:  # noqa: BLE001 -- destination went away mid-move
            pane.adopt_engine(engine, session_id)
            return "that pane group is no longer there"
        for class_name, value in marks.items():
            if value:
                fresh._marks[class_name] = True
        # 3. The source tab goes, and the session it was showing does not.
        with contextlib.suppress(Exception):
            await self._strip_for(source_tab.id or "").remove_pane(
                source_tab.id or ""
            )
        self._activate_tab(new_tab)
        self._focus_tab(fresh)
        self._persist_tabset()
        return None

    def action_grow_pane_up(self) -> None:
        self.grow_pane_towards("up")

    def action_grow_pane_down(self) -> None:
        self.grow_pane_towards("down")

    def action_grow_pane_left(self) -> None:
        self.grow_pane_towards("left")

    def action_grow_pane_right(self) -> None:
        self.grow_pane_towards("right")

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
            active = self._strip().active_pane
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
            await self._strip_for(tab.id or "").remove_pane(
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
            await self._strip_for(tab.id or "").remove_pane(
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
        once the session that spawned their subagents is gone.

        ``is_last`` (v0.85.0) is computed up front and, since v0.99.1, no
        longer decides much: Ctrl+Q now excludes the closing session from
        the persisted restore set unconditionally (below), so the ONLY
        thing tab arithmetic still changes is Ctrl+W's disposition -- see
        the branches below for why -- and :meth:`_cyclable_tabs`'s sibling
        fix for the OTHER half of the v0.85.0 report.

        **Ctrl+Q ends it, Ctrl+W parks it** is the rule as of v0.99.1: a
        terminated session leaves the persisted set no matter which tab it
        was, a detached one stays in it unless it was the last tab (see
        the ``is_last`` branch below for that one's own reasoning, shared
        with Ctrl+Q's last-tab case). Reported live: *"tabs that i had
        closed using CTRL+Q are resurrected on the next start of DOXA
        anyway"* -- and, once told a finalized session cannot be resumed,
        *"but all of those sessions are resumed and dont disappear ...
        there is no way to permanently close a tab."* Both true: v0.60.0
        kept an ended session's id in the persisted set on purpose (the
        `if not is_last` guard this replaces), reasoning that a finalized
        session was still a resumable one, so the record of the tab was
        worth keeping even once the daemon behind it was gone. What it
        missed is that finalize() never removes the conversation from the
        CLI's OWN history store -- so doxa.cli's restore triage
        (``ended_tab_spec`` -> ``history_mod.resume_state``) found it,
        answered RESUME_OK, and handed it back as a live, resumable tab,
        not the read-only one the fix's own comments describe. Nothing
        about the TRANSCRIPT changes here -- it is still on disk, still
        findable by /search and the resume picker -- only whether Ctrl+Q'd
        session ever lands in the file :meth:`_persist_tabset` reads on
        the NEXT launch."""
        for tab in list(pane._transcript_tabs.values()):
            await self._close_transcript_tab(tab)
        is_last = len(self.panes()) == 1
        if terminate:
            note = await pane.stop()
            if note:
                # The pane itself is about to be removed (or the whole app
                # quits, below) -- a toast is screen-level, not pane-level,
                # so it survives the tab it was about -- unlike a SystemBlock
                # mounted in the closing pane's own block list, which the
                # user would never get a chance to see.
                self.notify(note, severity="information", timeout=10)
            # Recorded regardless of is_last (v0.99.1): _ended_this_run no
            # longer feeds the persisted set (see its own docstring), so
            # there is no longer a reason to skip this on the last tab --
            # it is pure in-run bookkeeping now (the sidebar rail's dimmed
            # row for an ended session, for the rest of THIS run only).
            # pane.stop() above already marked the pane _stopped, which is
            # what actually keeps it out of the next launch's restore set
            # -- see _persist_tabset's own mounted-pane scan.
            self._record_after_close(pane, self._ended_this_run)
        else:
            # Detached ON PURPOSE: it is no longer this window's to end, so
            # a later quit-stop leaves it running.
            label = pane.display_name()
            pane.detached_on_purpose = True
            await pane.detach()
            # Reported live: "when the tab is detached with CTRL+W, there
            # should be a notification or message" -- Ctrl+W used to
            # detach in total silence, the tab just gone with no sign the
            # session was still alive anywhere. Same screen-level toast
            # mechanism as the "kept <worktree>" note above, naming the
            # tab and how to get it back -- true whether or not this is
            # the last tab (the daemon keeps lingering either way).
            self.notify(
                f"{label} detached — still running in the background; "
                "bring it back with /attach or the peers chip",
                severity="information", timeout=10,
            )
            if not is_last:
                # Item D #4: this session STAYS in the persisted tab set
                # even though its tab is about to leave the strip below --
                # record it here, before remove_pane takes
                # pane._session_id out of panes()'s own scan with it.
                # Skipped when this IS the last tab -- see below.
                self._record_after_close(pane, self._detached_this_run)
        if is_last:
            # The window's whole tab strip is about to go empty, and the
            # app quits right below -- the reported defect: "if the last
            # remaining open tab is closed with CTRL+Q, the next time doxa
            # is started should start with a fresh session. If last open
            # tab was closed with CTRL+W, we also start with a fresh
            # session, but the old session could be reattached." Both keys
            # close the LAST tab the same way here: the closing session is
            # excluded from _persist_tabset's own mounted-pane scan via
            # `exclude_session_id` -- NOT by removing the pane from the
            # strip first. An earlier version of this fix called
            # remove_pane() here before persisting, to get the same
            # exclusion out of the mounted-pane scan -- which worked, but
            # unmounted a pane with a still-running _peer_pump worker
            # moments before action_quit tore the app down under it: an
            # intermittent AssertionError out of that worker's own `assert
            # self.engine is not None`, surfaced as a visible in-app error
            # block on the way out. The pane now stays mounted, exactly as
            # App.action_quit already handled it before this whole feature
            # existed -- only what _persist_tabset WRITES changes.
            #
            # For Ctrl+Q this `exclude_session_id` is belt-and-suspenders
            # since v0.99.1: pane.stop() above already marked the pane
            # _stopped, which the mounted-pane scan now excludes on its
            # own (same rule as the non-last branch below). Still load-
            # bearing for Ctrl+W: a detached pane is never _stopped, so
            # nothing else here would keep it out of THIS one snapshot
            # before the pane is unmounted. The next launch reads an empty
            # (or unaffected-by-this-tab) record and starts fresh either
            # way; what differs is not the record, it is whether the
            # session is still THERE to /attach back to: Ctrl+Q's is gone
            # (pane.stop() above), Ctrl+W's keeps running (pane.detach()
            # above, and the toast just said so) -- reachable by NAME from
            # here on, never by an automatic restore. See doxa.tabsets'
            # module docstring for the restore side of this distinction.
            self._persist_tabset(exclude_session_id=pane._session_id)
            await App.action_quit(self)
            return
        # Ctrl+Q's pane is still mounted here (removed only below, by
        # _close_group_tab) but already _stopped -- this snapshot already
        # excludes it via _persist_tabset's own mounted-pane scan, no
        # `exclude_session_id` needed. A Ctrl+W'd pane is not _stopped, so
        # it is written here exactly as it was before -- still in the set,
        # per item D #4.
        self._persist_tabset()
        # **Closing a tab closes ONE session** (v0.97.0, and the third of
        # the three problems the inversion dissolves rather than patches).
        # Through v0.95.0 this pane's tab could hold two more sessions and
        # closing it ended all three; a tab holds one surface now, so the
        # question is only what INHERITS the keyboard.
        #
        # Two collapses, in order, and they are different facts:
        #   * the group has other tabs   -> it keeps its region, shows one
        #   * the group has none left    -> the region goes, the split
        #                                   above it collapses, and the
        #                                   nearest surviving group takes
        #                                   the keyboard.
        # The keyboard's destination is named explicitly for the reason
        # every other focus move in this file is (v0.38.0): a pane
        # disappearing is not a user saying where to go next.
        group = split_mod.group_of(pane)
        siblings = [t for t in group.tabs() if t is not pane.tab] if group else []
        heir: "Any" = None
        if not siblings:
            heir = self._closest_group_heir(group)
        await self._close_group_tab(pane)
        if heir is not None:
            self._focus_tab(heir)
        else:
            self._focus_active_tab()
        self._persist_tabset()

    def _closest_group_heir(self, closing: "PaneGroup | None") -> "Any":
        """Which surface inherits the keyboard when a whole GROUP closes:
        the active tab of the group nearest it on screen, measured from the
        rectangles the user was actually looking at.

        The group-level twin of :meth:`_closest_sibling`, and the same
        rule: nearest by painted position, falling back to the first
        remaining group when nothing has been painted yet."""
        if closing is None:
            return None
        here = closing.region
        best = None
        best_gap = None
        for other in self.groups():
            if other is closing:
                continue
            region = other.region
            if region.width <= 0 or region.height <= 0:
                continue
            gap = abs(region.x - here.x) + abs(region.y - here.y)
            if best_gap is None or gap < best_gap:
                best, best_gap = other, gap
        if best is None:
            best = next((g for g in self.groups() if g is not closing), None)
        if best is None:
            return None
        return next(iter(best.surfaces()), None)

    def _closest_sibling(
        self, pane: "SessionPane", siblings: "list[SessionPane]"
    ) -> "SessionPane":
        """Which pane inherits the keyboard when ``pane`` closes: the one
        nearest it on screen, measured from the rectangles the user was
        actually looking at, falling back to the first remaining leaf when
        nothing has been painted yet."""
        here = pane.region
        best = None
        best_gap = None
        for other in siblings:
            region = other.region
            if region.width <= 0 or region.height <= 0:
                continue
            gap = abs(region.x - here.x) + abs(region.y - here.y)
            if best_gap is None or gap < best_gap:
                best, best_gap = other, gap
        return best or siblings[0]

    def _cyclable_tabs(self) -> "list[Any]":
        """Every tab in the FOCUSED GROUP's strip, VISUAL (strip) order,
        for :meth:`_cycle_tab` -- deliberately NOT :meth:`panes` (session
        tabs only; every engine-touching caller needs that narrower list)
        and NOT :meth:`_restorable_tabs` (session + archived, but never a
        subagent transcript, because the persisted set has no use for
        one). Reported live: "CTRL+ArrowLeft ... only seems to work to
        switch among active sessions ... not between read-only finished
        sessions" -- Ctrl+Left/Right must reach every tab a user can SEE,
        an archived read-only tab and an open subagent transcript
        included, because both sit right there in the strip.

        **Scoped to one group since v0.97.0, and that is the whole point of
        the inversion.** The reported defect it fixes: *"if i switch tabs,
        the split out sessions go with the tab. Shouldn't the split out
        sessions be independent?"* -- Ctrl+←/→ cycles the tabs of the group
        holding the keyboard and leaves every other group exactly as it
        was."""
        group = self.focused_group()
        return group.tabs() if group is not None else []

    def _cycle_tab(self, delta: int) -> None:
        """Ctrl+← / Ctrl+→ -- move to the neighbouring tab, wrapping. One
        tab wraps to itself, which is the correct no-op.

        Focuses the tab it lands on, right here (v0.38.0). That used to be
        left to _on_tab_activated, one message-pump turn later -- and a
        pane mounting in the meantime could focus itself and take the
        activation back, which is exactly what made tests/test_tab_status.
        py's done-unseen test flaky after a Ctrl+T/Ctrl+← pair.

        :meth:`_cyclable_tabs`, not :meth:`panes` (v0.85.0 -- see that
        method's own docstring for the defect this fixes): a read-only
        tab has no prompt, so :meth:`_focus_tab` below is a no-op for one,
        same as it already is for a mouse click landing on one."""
        tabs = self._cyclable_tabs()
        if len(tabs) < 2:
            return
        tabbed = self._strip()
        ids = [t.id for t in tabs if t.id]
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
        """Take me to that pane, by id -- the palette's open-tab entries
        and a peer chip's jump to a session already open here. Same three
        beats as every other explicit switch: activate, move the marker,
        focus (v0.38.0).

        Accepts a LEAF id as well as a tab id (v0.91.0). Every caller
        passes a ``SessionPane``'s own id, and with splits that is no
        longer the same string as its tab's -- so this resolves the leaf,
        activates the tab that holds it, and lands the keyboard on THAT
        pane rather than on whichever leaf the tab was last in. Jumping to
        a peer and arriving at its neighbour would be the same defect as
        restoring onto the wrong tab, one level down."""
        leaf = next((p for p in self.panes() if p.id == pane_id), None)
        target_id = leaf.tab_id if leaf is not None else pane_id
        with contextlib.suppress(Exception):
            self._strip_for(target_id).active = target_id
        self._jump_tab_marker()
        if leaf is not None:
            self._focus_tab(leaf)
        else:
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
                # ``display_name()``, not the TabPane ``_title`` this read
                # through v0.88.0: a leaf is no longer the tab, and with
                # two sessions in one tab the header's title names only
                # the first of them. This names THIS pane -- which is
                # also what makes the palette the place two panes sharing
                # a tab are told apart, alongside the session id below.
                f"{pane.display_name()}" + (f"  ({sid})" if sid else "")
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

        The palette's own name for Ctrl+Q -- v0.99.1 makes that literal by
        delegating to :meth:`_close_pane` (``terminate=True``) instead of
        re-deriving its disposition here a second time. Through v0.99.0
        this method reimplemented a SUBSET of that logic directly (no
        transcript-tab teardown, no split/group-aware removal via
        _close_group_tab, no is_last handling at all) and inherited none
        of _close_pane's fixes as a result -- stopping the ONLY tab from
        the palette left its session in the persisted record even after
        v0.85.0 taught Ctrl+Q's own path not to, and even after v0.99.1
        taught it that a stopped pane never belongs in the persisted set
        regardless of tab position. One implementation now, reached both
        ways."""
        pane = self.active_pane
        if pane is None:
            return
        await self._close_pane(pane, terminate=True)

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
        id, purely so this method has something to call it.

        **v0.97.0: this is the id of the tab the group HOLDING the saved
        active session will open on**, and every other group opens on its
        own saved active tab. Each ``PaneGroup`` passes its own answer to
        its own ``TabbedContent``, so the race above is closed once per
        group by the same mechanism rather than once per window -- and a
        tab id is unique across the window, so this method's contract is
        unchanged for every caller that only ever had one group."""
        if not self._restore_tabs:
            return ""  # one pane; Tabs' own first-tab default is already right
        # A session in a group's tab list names its OWN tab (v0.97.0 --
        # through v0.95.0 it named its tab's FIRST leaf, because a tab held
        # a tree). The one indirection left is the diff surface, which has
        # a tab of its own and never answers for a session.
        tree = self._restore_group_tree()
        if tree is not None:
            for group in layout_mod.groups(tree):
                for leaf in group.tabs:
                    if leaf.is_diff:
                        continue
                    if (
                        self._restore_active_id
                        and leaf.session_id == self._restore_active_id
                    ):
                        return _restore_pane_id(leaf.session_id)
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
            tabbed = self._strip()
        except Exception:  # noqa: BLE001 -- no tab strip, nothing to choose
            return
        target: "Any" = None
        if self._restore_active_id:
            # The LEAF, not the tab (v0.91.0). A restored split puts three
            # sessions in one tab, and "restore the saved active tab"
            # under-specifies which of them the keyboard belongs to --
            # which is the same defect the saved active TAB had from
            # v0.23.0 to v0.32.0, one level down. The leaf carries a
            # derived id for exactly this lookup.
            leaf_id = f"{_restore_pane_id(self._restore_active_id)}-leaf"
            target = next((p for p in self.panes() if p.id == leaf_id), None)
            if target is None:
                with contextlib.suppress(Exception):
                    target = tabbed.get_pane(
                        _restore_pane_id(self._restore_active_id)
                    )
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
        # scan reads every one of them without help from either side dict.
        # A stopped pane is excluded there again as of v0.99.1 (see that
        # method's own docstring for the v0.60.0 detour) -- so this method
        # (palette 'Quit: stop session', all tabs -- Ctrl+C used to reach
        # it too, through v0.84.0) now matches ending them one at a time
        # with Ctrl+Q exactly: a detached pane is still written (item D
        # #4, unchanged), a stopped one is not. Nothing special had to
        # change HERE for that to be true -- the mounted-pane scan this
        # reads from is the one and only choke point, which is the point.
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
