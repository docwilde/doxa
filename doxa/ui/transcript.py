"""doxa.ui.transcript -- the blocks one turn renders into.

Extracted from ``doxa/app.py`` unchanged: the turn fold and its body, the
tool-call chips and the section that compacts them, the reasoning fold and
its thinking marker, the out-of-band blocks (system notices, pasted images,
peer messages), the subagent row, and the two non-session tabs that show a
transcript without an engine behind it.

:func:`mount_transcript` lives here too, with the widgets it builds: a
restored tab is not a lookalike of the session it restores, it is the
session's own view rebuilt out of exactly these classes.

docs/plugin-api.md's third extension point ("a transcript block") attaches
here -- see :meth:`doxa.session.runtime.PaneRuntimeMixin._handle_event`'s
dispatch map for the side that chooses which block an event renders into.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from time import monotonic
from typing import Any, Callable  # noqa: F401 -- annotation-only, see below

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Collapsible, Markdown, Static, TabbedContent, TabPane
from textual.widgets.markdown import MarkdownStream

from .. import images as images_mod
from .. import tabsets as tabsets_mod
from .. import transcript as transcript_mod
from .labels import (
    CLICKABLE_CHIP_ACCENT,
    _escape_markup,
    _one_line,
    _write_tab_class,
    _write_tab_label,
)


@dataclass(frozen=True)
class RestoreTabSpec:
    """Item D: one tab to open at startup, reattaching a session
    doxa.tabsets.resolve() already cross-checked against the live peer
    registry -- ``engine_factory`` builds an EngineClient against that
    specific daemon socket, never a fresh session. ``session_id`` is known
    UP FRONT (from the registry entry the record resolved to), not learned
    from the engine after boot, which is what lets DoxaApp give the pane a
    deterministic id and pick the saved active tab before anything has
    connected.

    ``cwd`` and ``archived`` are v0.32.0's half of the same idea. ``cwd``
    is the session's OWN working directory (its linked worktree, with
    worktree_per_session on) -- what ``doxa.transcript`` needs to find the
    session's persisted conversation and put it back on screen, which is
    the whole of the reported defect. ``archived`` marks a tab whose
    session is GONE: there is nothing to attach to, ``engine_factory`` is
    never called, and DoxaApp builds an :class:`ArchivedSessionTab`
    (read-only, transcript from disk) instead of a SessionPane.

    ``resume`` (v0.56.0) is what makes "restore" mean restore. A tab whose
    session ENDED used to have exactly one outcome -- ``archived``, a
    read-only transcript and a dead end. Reported: *"as long as a tab was
    open, when DOXA is started again, the tab should be resumed
    automatically"*. So doxa.cli now asks
    :func:`doxa.history.resume_state` about each such tab BEFORE deciding
    its kind: resumable ones come back as real ``SessionPane``s whose
    engine continues the conversation (``engine_factory`` spawns with
    ``--resume``), and only the rest stay ``archived``. ``resume_note``
    carries the reason when one does -- the read-only tab is now the
    FALLBACK, and a fallback that cannot say why it happened is
    indistinguishable from the feature not existing."""

    session_id: str
    engine_factory: "Callable[[], Any] | None" = None
    pinned_name: "str | None" = None
    cwd: "str | None" = None
    archived: bool = False
    resume: bool = False
    resume_note: str = ""


def _restore_pane_id(session_id: str) -> str:
    """A stable, valid Textual widget id for a restored tab, derived from
    the session id it will hold -- so the app can set the saved ACTIVE tab
    (doxa.tabsets.TabSetRecord.active_session_id) before any pane has
    booted far enough to report its own session_id back."""
    return f"restore-{session_id}"


async def _composed(*widgets: Any) -> bool:
    """Wait until every one of ``widgets`` is really mounted.

    ``mount()`` resolves when the widget it was given is mounted; the
    children that widget yields from its own ``compose`` land a
    message-pump cycle later. A LIVE turn never notices -- its first
    ``text_delta`` arrives over a socket, cycles after the mount. A
    RESTORE writes into the block immediately, and hit exactly this:
    "Can't mount widget(s) before Markdown(classes='turn-body') is
    mounted", intermittently, because whether the pump had run yet was a
    matter of luck.

    Bounded: a widget that never composes costs one skipped turn, never a
    hung restore."""
    for _ in range(200):
        if all(widget.is_mounted for widget in widgets):
            return True
        await asyncio.sleep(0)
    return all(widget.is_mounted for widget in widgets)


async def mount_transcript(
    block_list: "VerticalScroll", snapshot: "transcript_mod.Transcript",
) -> int:
    """Render a persisted conversation into a block list, oldest turn
    first, and return how many turns were mounted.

    The SAME widgets a live turn builds -- ``TurnBlock`` for the prompt and
    the answer, ``ToolChip`` for each call -- so a restored tab is not a
    lookalike of the session it restores, it is the session's own view
    rebuilt. Every turn is closed with ``mark_done`` (no cost, no
    duration: those are turn-time measurements this file never claimed to
    keep), which also stops the Markdown stream each one opens -- a
    restore that left forty live streams behind would reintroduce exactly
    the idle-CPU leak ThinkingMarker's docstring exists to warn about.

    Mounted in batches with a yield between them: a forty-turn restore is
    forty Markdown parses, and doing them in one uninterrupted burst
    freezes the first paint of every OTHER tab in the window.

    Truncation is never silent -- ``dropped_turns`` gets a leading
    SystemBlock, a cut answer gets a trailing marker line, dropped tool
    chips get a counted one. A restored transcript may be shorter than the
    session; it may never LOOK complete when it is not."""
    mounted = 0
    if snapshot.dropped_turns:
        noun = "turn" if snapshot.dropped_turns == 1 else "turns"
        await block_list.mount(SystemBlock(
            f"⤒ {snapshot.dropped_turns} earlier {noun} not shown — "
            f"the full transcript is on disk (/search)"
        ))
    for index, turn in enumerate(snapshot.turns):
        block = TurnBlock(turn.prompt)
        await block_list.mount(block)
        # Hidden BEFORE a word of the restored answer is written (v0.56.0):
        # a restored turn finished long ago, and replaying its text through
        # the same append_text a live turn uses would otherwise tick the
        # spinner into "generating" on the way past. mark_done below hides
        # it anyway -- doing it first is the difference between a marker
        # that is torn down and one that was never shown.
        block.hide_thinking()
        if not await _composed(block.body, block.tools):
            continue
        if turn.text:
            await block.append_text(turn.text)
        if turn.text_truncated:
            await block.append_text(
                "\n\n*…answer truncated for restore — full text on disk*"
            )
        tools = list(turn.tools)
        if turn.tools_dropped:
            tools.append(transcript_mod.ToolRecord(
                call_id=f"restore-dropped-{index}",
                name="…more tool calls not shown",
                tool_input={"dropped": turn.tools_dropped},
            ))
        if tools:
            # The section is built and settled ONCE per turn rather than
            # lazily inside add_tool_chip, because its own chip holder is
            # a compose child too -- adding a chip the same cycle it is
            # created raised "Can't mount widget(s) before Vertical(classes=
            # 'tool-calls-list') is mounted".
            section = ToolCallsSection()
            block.tool_section = section
            await block.tools.mount(section)
            if await _composed(section.chips):
                for tool in tools:
                    chip = ToolChip(tool.call_id, tool.name, tool.tool_input)
                    await section.add_chip(chip)
                    if tool.result is not None:
                        # duration_ms None: the transcript records what a
                        # tool ANSWERED, never how long it took, and
                        # inventing a number here would be the one thing a
                        # restore must not do.
                        chip.update_result(tool.result[:280], tool.is_error, None)
        await block.mark_done(None, None, False)
        mounted += 1
        if mounted % 6 == 0:
            await asyncio.sleep(0)
    block_list.scroll_end(animate=False)
    return mounted


class SystemBlock(Static):
    """One block of doxa-generated (not model-generated) output -- slash
    command results, peer-layer errors. Same ▎ accent as turns; v0.13.0's
    restyle carries the role in the background tint instead of a border --
    the dimmer step on the surface ramp, one below the screen, with muted
    text (.system-block in the theme).

    ``link_label``/``on_link`` (v0.31.0) add ONE clickable trailing line --
    the same unprefixed ``[@click=...]`` markup span
    :class:`StatusBar`/:class:`SubagentLine` already use, resolved against
    the clicked widget itself by ``Widget.broker_event``, which is why
    :meth:`action_follow_link` lives right here and needs no ``app.``/
    ``screen.`` prefix. It exists so a notification block can BE the door
    to what it is announcing instead of naming a command and leaving the
    reader to retype it. No new widget kind for that: a system block with
    an affordance is still a system block.

    Callers embedding model-derived text in ``text`` must escape it
    themselves (:func:`_escape_markup`) -- this class does not, because
    every pre-existing caller passes doxa-authored strings and some of
    them (``/help``) rely on markup being live."""

    def __init__(
        self,
        text: str,
        *,
        link_label: str = "",
        on_link: "Callable[[], Any] | None" = None,
    ) -> None:
        self.text = text
        self._on_link = on_link
        body = f"▎ doxa\n{text}"
        if link_label and on_link is not None:
            body += (
                f"\n[@click=follow_link][{CLICKABLE_CHIP_ACCENT}]"
                f"{_escape_markup(link_label)}[/][/]"
            )
        super().__init__(body, classes="system-block")

    def action_follow_link(self) -> None:
        if self._on_link is not None:
            self._on_link()


class ErrorBlock(Collapsible):
    """Something broke, and here it is -- v0.56.0's whole point.

    Four defects reached the user in one day and none of them arrived as a
    legible error (see :mod:`doxa.errors` for the list). This is the block
    they should have arrived as: ONE line saying what failed and who failed
    it, and the whole scrubbed traceback behind a fold that starts
    **collapsed**. A user must be able to see that something broke without
    reading a wall of text, and reach the whole of it in one keystroke --
    which is exactly the trade :class:`ToolCallsSection` and
    :class:`ReasoningSection` already make, so this is their pattern rather
    than a fourth idiom.

    A KIND OF ITS OWN, on the same rule :class:`ShellBlock` established:
    a failure must never be mistakable for the assistant's words or for
    doxa's ordinary chatter. It carries neither the ``▎`` turn accent nor
    the ``▎ doxa`` prefix, but a red left rule (theme.tcss ``ErrorBlock``)
    -- the same ``#D9534F`` the context chip escalates to and the
    needs-input popup wears, the app's one "stop and look" color, worn by
    nothing that is merely chrome.

    The header names the ORIGIN (:attr:`doxa.errors.Failure.origin`).
    "TimeoutError … · textual_image" and "TimeoutError … · doxa" are
    different bug reports, and a plugin's crash reading as a DOXA bug is
    the specific outcome ``docs/plugin-api.md``'s failure policy exists to
    prevent.

    Repeats fold into ONE block. A widget that raises while painting
    raises again on every paint; :meth:`bump` puts the tally in the header
    instead of growing the transcript without bound (the caller,
    ``DoxaApp.report_failure``, is what decides this is a repeat -- see
    :attr:`doxa.errors.Failure.signature`).

    Everything interpolated is escaped (:func:`_escape_markup`): a
    traceback is a source listing, ``[`` in it is a list literal and not
    Rich markup, and a block about a failure that fails to render would be
    a joke at the user's expense. The text is ALREADY scrubbed -- see
    :func:`doxa.errors.scrub`; this class re-scrubs nothing and must not
    be handed anything raw."""

    #: Fold header for a failure the app survived, and for one it did not.
    #: A user has to be able to tell "this is broken" from "this is over"
    #: at a glance, and both blocks are on screen at the same time when a
    #: fatal failure follows a recoverable one.
    MARK = "✗"
    FATAL_MARK = "✗✗"

    def __init__(self, failure: "Any") -> None:
        self.failure = failure
        self.repeats = 1
        self.body = Static(
            _escape_markup(failure.detail or "(no further detail)"),
            classes="error-detail",
        )
        super().__init__(self.body, title=self._render_title(), collapsed=True)
        self.add_class("error-block")

    def _render_title(self) -> str:
        mark = self.FATAL_MARK if self.failure.fatal else self.MARK
        tally = f"  ·  ×{self.repeats}" if self.repeats > 1 else ""
        return f"{mark} {_one_line(self.failure.headline(), 150)}{tally}"

    def bump(self, repeats: int) -> None:
        """The same failure again. A title rewrite only -- as cheap as
        ToolCallsSection's own live "(N)" and, deliberately, no new widget
        and no re-parse: the repeat case is the one that fires hundreds of
        times, and it must cost a string."""
        self.repeats = repeats
        self.title = self._render_title()


class ImageBlock(Vertical):
    """The `/img <path>` debug block: a caption line plus whatever
    doxa.images.widget_for yields for this terminal -- a real image widget
    on a KGP/sixel/half-block tier, the "[image: ...]" Static otherwise.
    Exists so image support can be eyeballed without needing a tool call
    that happens to return a picture."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(classes="image-block")

    def compose(self) -> ComposeResult:
        yield Static(f"▎ img · {self.path}", classes="image-caption")
        yield images_mod.widget_for(self.path, self.path)


class BootBanner(Vertical):
    """The DOXA mark above a session's opening identity block.

    **The DRAWN mark is the normal path; the raster is the exception.**
    That reads backwards from v0.41.0 and is the point of v0.49.0. A
    terminal without a real pixel protocol gets
    :func:`doxa.banner.drawn_lines` -- a ring-and-triangle authored cell
    by cell in half-blocks, with the wordmark and strapline as plain text
    beside it. Only ``kgp``/``sixel``, which carry an actual bitmap, get
    ``logo.png``. :func:`doxa.banner.use_image` owns that decision and the
    ``boot_banner`` setting can pin it either way.

    Never the ``[image: ...]`` line: this widget is decoration, and a
    fallback line announcing a decoration that failed is worse than the
    decoration's absence.

    On the raster path WIDTH is set here and height is not, deliberately:
    the image widget derives its rows from the terminal's own cell aspect
    (see :mod:`doxa.banner`), so a terminal whose cells are not 2:1 gets
    the right number of rows rather than a letterboxed six.

    **The drawn form fills itself in on resize, not at compose time**, and
    that is not fussiness. The chrome between the terminal's width and this
    widget's content width is not a constant: the block list contributes
    padding and a vertical scrollbar comes and goes with the transcript,
    moving it by two. Laying the art out against a GUESSED chrome wrapped
    it into mush -- measured, at 40 and 20 columns. Art must not wrap, so
    it is fitted to :attr:`content_size`, which is the real number and is
    only knowable once Textual has done the layout."""

    #: Below this the explanation line is prose in a column too narrow to
    #: be prose, so it is dropped rather than wrapped into a paragraph.
    REASON_COLUMNS = 30

    def __init__(self, columns: int) -> None:
        self.columns = columns
        self._drawn: "Static | None" = None
        self._reason_widget: "Static | None" = None
        self._reason = ""
        super().__init__(classes="boot-banner")

    def compose(self) -> ComposeResult:
        from .. import banner as banner_mod

        mode = images_mod.detect_mode()
        if banner_mod.use_image(mode, self.columns):
            source = banner_mod.image_source()
            widget = images_mod.widget_for(source, "doxa logo", mode=mode)
            widget.styles.width = banner_mod.COLUMNS
            widget.add_class("banner-image")
            yield widget
            return
        # The drawn mark, plus -- only when the raster was ASKED for and
        # could not be given -- one line saying why (banner.fallback_reason
        # on why that is rare). Content is filled in by _lay_out, once a
        # real width exists.
        self._reason = banner_mod.fallback_reason(mode, self.columns)
        self._drawn = Static("", classes="banner-wordmark")
        yield self._drawn
        if self._reason:
            self._reason_widget = Static("", classes="banner-reason")
            yield self._reason_widget

    def on_mount(self) -> None:
        self._lay_out()

    def on_resize(self) -> None:
        self._lay_out()

    def _lay_out(self) -> None:
        """Fit the drawn banner to the width it actually has."""
        from .. import banner as banner_mod

        if self._drawn is None:
            return
        width = self.content_size.width or self.columns
        self._drawn.update("\n".join(banner_mod.drawn_lines(width)))
        if self._reason_widget is not None:
            self._reason_widget.display = width >= self.REASON_COLUMNS
            self._reason_widget.update(self._reason)


class ImageShowcaseBlock(Vertical):
    """``/img`` with no argument: what this terminal can ACTUALLY do with
    images, measured, and then demonstrated in every tier it may honestly
    draw (v0.41.0).

    It is one block rather than a `/doctor` section on purpose. `/doctor`
    is a text report with an exit code -- ``scripts/install.sh`` runs
    ``doxa doctor`` headless and reads pass/fail out of it, and a check
    that has to mount image widgets to mean anything cannot live there.
    It is `/img` rather than a new `/image` for the opposite reason: `/img`
    already exists, its registry summary already calls it "terminal
    image-support probe", and a second command one letter away from it is a
    coin flip at the autocomplete. ``/img <path>`` is untouched.

    Every rendered tier is one this terminal answered for
    (:func:`doxa.images.renderable_modes`). A tier that was never asked
    about is named in the report as never asked, and NOT drawn -- pushing a
    TGP escape at a terminal that has no TGP does not produce a picture, it
    produces litter, and a showcase implying kitty support where there is
    none is worse than no showcase."""

    def compose(self) -> ComposeResult:
        from .. import banner as banner_mod

        yield Static("▎ img · terminal image support", classes="image-caption")
        rows = images_mod.diagnostics()
        pad = max(len(label) for label, _ in rows)
        yield Static(
            "\n".join(f"{label.ljust(pad)}  {value}" for label, value in rows),
            classes="image-diagnostics",
        )
        if banner_mod.image_source() is None:
            yield Static(
                "assets/logo.png is not on disk — nothing to render with",
                classes="image-fallback",
            )
            return
        for mode in images_mod.renderable_modes():
            yield Static(f"── {mode} ──", classes="image-mode-label")
            widget = images_mod.widget_for(
                banner_mod.image_source(), "doxa logo", mode=mode
            )
            if mode != "text":
                # The text tier's demonstration IS the one-line fallback;
                # giving it the image geometry would be dressing it up as
                # something it is not.
                widget.styles.width = banner_mod.COLUMNS
                widget.add_class("banner-image")
            yield widget


class ShellBlock(Static):
    """One ``!`` command and what it printed -- item Q's transcript block.

    A KIND OF ITS OWN, and that is the requirement, not a decoration.
    Shell output must never be mistakable for the assistant's words: it
    carries neither the ``▎`` turn accent nor the ``▎ doxa`` prefix
    :class:`SystemBlock` uses, but its own ``❯`` prompt glyph and its own
    green left rule (theme.tcss ``ShellBlock``), a color no other block in
    the transcript wears. The command line, the exit code and the duration
    are always shown, including for a command that printed nothing at all
    -- a shell surface that hides how a command ended is one you cannot
    trust.

    Mounted in the RUNNING state the moment the key is pressed and updated
    in place by :meth:`complete`, so a slow command is visibly running
    instead of looking like a prompt that swallowed a keystroke.

    Everything interpolated here is escaped (:func:`_escape_markup`): the
    body is bytes an arbitrary program wrote, and an unescaped ``[`` in it
    would be read as Rich markup.

    Nothing this block shows is in the model's context -- see
    :mod:`doxa.shell`."""

    def __init__(self, command: str, cwd: str) -> None:
        self.command = command
        self.cwd = cwd
        self.result: "Any | None" = None
        super().__init__(self._render_text(), classes="shell-block")

    def _render_text(self, body: str = "", status: str = "running…") -> str:
        head = f"❯ {_escape_markup(_one_line(self.command, 200))}"
        parts = [head]
        if body:
            parts.append(_escape_markup(body.rstrip("\n")))
        parts.append(status)
        return "\n".join(parts)

    def complete(self, result: "Any") -> None:
        """Swap the running state for the finished one -- output, then the
        exit code line the block always ends on."""
        self.result = result
        status = result.status_line()
        if getattr(result, "truncated", False):
            status = (
                f"{status} · output capped, {result.dropped_bytes:,} more "
                "bytes not shown"
            )
        body = result.output or ""
        if not body.strip():
            status = f"(no output) · {status}"
            body = ""
        self.update(self._render_text(body, status))


class PeerMessageBlock(Static):
    """One incoming peer message. Visually distinct from turns (dim border,
    peer title in the header -- see PeerMessageBlock rules in theme.tcss);
    the body was scrubbed on receive in peers.PeerHost, the one choke point
    for peer input."""

    def __init__(self, frame: dict) -> None:
        self.frame = frame
        title = frame.get("from_title", "?")
        short_id = str(frame.get("from_id", ""))[:8]
        sent_at = frame.get("sent_at", "")
        body = str(frame.get("body", ""))
        super().__init__(
            f"✉ peer · {title} ({short_id}) · {sent_at}\n{body}",
            classes="peer-block",
        )


class ToolChip(Collapsible):
    """One tool call, collapsed by default. Body content (full args + full
    result) is formatted lazily -- only on first expand, per the "lazy-
    formatted" requirement -- so a turn with a dozen tool calls doesn't pay
    for a dozen JSON pretty-prints it may never look at. A tool result that
    carries the engine's ``image_path`` convention gets an image widget (or
    its guaranteed text fallback -- doxa/images.py) mounted into the media
    area, equally lazily: on first expand only.

    Trace tree: a Task-spawned subagent's own activity arrives tagged with
    ``parent_id`` = this chip's call id (the engine's subagent trace
    convention), and nests HERE -- child tool calls mount as further
    ToolChips into ``self.subcalls`` (foldable all the way down, each level
    as lazily formatted as the top), and the subagent's streamed text
    accumulates in a buffer that only renders on expand. Everything shown
    was scrubbed engine-side before it ever reached an event."""

    def __init__(self, call_id: str, name: str, input_data: dict) -> None:
        self.call_id = call_id
        self.tool_name = name
        self.tool_input = input_data
        self.tool_result: str | None = None
        self.tool_image_path: str | None = None
        self.is_error = False
        self.duration_ms: int | None = None
        self._formatted = False
        self._image_mounted = False
        self._sub_text = ""  # subagent streamed text, rendered lazily
        self._body = Static("", id=f"chip-body-{call_id}", classes="chip-body")
        self._subout = Static("", id=f"chip-subout-{call_id}", classes="chip-subout")
        # Hide-at-zero, the same convention ToolCallsSection/ReasoningSection
        # and the status chips already follow: an EMPTY Static is still one
        # row, and every expanded chip that never spawned a subagent (i.e.
        # almost all of them) was spending it on nothing. v0.56.0.
        self._subout.display = False
        self.subcalls = Vertical(
            id=f"chip-subcalls-{call_id}", classes="chip-subcalls"
        )
        self._media = Vertical(id=f"chip-media-{call_id}", classes="chip-media")
        super().__init__(
            self._body, self._subout, self.subcalls, self._media,
            title=self._chip_title(), collapsed=True,
        )

    def _chip_title(self) -> str:
        arg_summary = _one_line(json.dumps(self.tool_input, ensure_ascii=False), 60)
        if self.tool_result is None:
            status, dur = "…", "…"
        else:
            status = "✗" if self.is_error else "✓"
            dur = f"{self.duration_ms}ms" if self.duration_ms is not None else "?"
        return f"⚒ {self.tool_name}({arg_summary})  ·  {dur}  {status}"

    def update_result(
        self,
        result_summary: str,
        is_error: bool,
        duration_ms: int | None,
        image_path: str | None = None,
    ) -> None:
        self.tool_result = result_summary
        self.is_error = is_error
        self.duration_ms = duration_ms
        self.tool_image_path = image_path
        self.title = self._chip_title()
        if not self.collapsed:
            self.format_body()

    def append_subagent_text(self, chunk: str) -> None:
        """Streamed text from the subagent behind this Task call. Buffered
        always; RENDERED only while expanded (or on the next expand) --
        the same lazy discipline as the body, so a subagent that narrates
        for pages costs nothing until someone looks."""
        self._sub_text += chunk
        if not self.collapsed:
            self._render_subout()

    def _render_subout(self) -> None:
        if self._sub_text:
            self._subout.display = True
            self._subout.update("SUBAGENT:\n" + self._sub_text)

    def format_body(self) -> None:
        if not self._formatted:
            self._formatted = True
            text = "ARGS:\n" + json.dumps(self.tool_input, indent=2, ensure_ascii=False)
            if self.tool_result is not None:
                text += "\n\nRESULT:\n" + self.tool_result
            self._body.update(text)
        self._render_subout()
        if self.tool_image_path and not self._image_mounted:
            self._image_mounted = True
            # widget_for NEVER raises and never returns None: an unsupported
            # terminal or a bad file yields the "[image: ...]" Static.
            self._media.mount(
                images_mod.widget_for(self.tool_image_path, self.tool_image_path)
            )


async def _clone_chip(chip: "ToolChip") -> "ToolChip":
    """A read-only COPY of one ToolChip's current state -- name, input,
    result, error, duration, image, and (recursively) its own already-
    mounted subcalls -- for a subagent transcript tab's mirror tree
    (SubagentTranscriptTab, below). Never the live widget itself: a
    widget has exactly one parent, and the original stays exactly where
    the trace tree put it (inside its Task chip's ``subcalls``) whether or
    not anyone ever opens a transcript tab to look at a copy of it."""
    mirror = ToolChip(chip.call_id, chip.tool_name, chip.tool_input)
    if chip.tool_result is not None:
        mirror.update_result(
            chip.tool_result, chip.is_error, chip.duration_ms,
            image_path=chip.tool_image_path,
        )
    for sub in list(chip.subcalls.children):
        if isinstance(sub, ToolChip):
            await mirror.subcalls.mount(await _clone_chip(sub))
    return mirror


class ToolCallsSection(Collapsible):
    """The turn's own top-level tool chips, compacted behind ONE fold:
    "Tool calls (N)", collapsed by default. Created lazily -- on the
    FIRST top-level tool_call event of a turn (see TurnBlock.add_chip) --
    so a turn with no tool calls grows no section at all (hide-at-zero,
    same convention as the git/usage/peers/disabled-tools status chips).

    N updates live as chips mount mid-turn: a title rewrite only, as
    cheap as ToolChip's own title update on every result -- no repaint
    storm (see this app's idle-CPU comments elsewhere: ThinkingMarker,
    the leaked-timer fix in TurnBlock.hide_thinking). If the user expands
    this section mid-turn it STAYS expanded as further chips arrive --
    nothing here ever writes ``self.collapsed`` itself, so only a click
    (or a test poking the reactive directly) can change it; it never
    auto-collapses out from under the cursor.

    Chrome, not model content: this wraps chips, it does not replace
    them -- each chip inside keeps its own ToolChip fold, args/result
    formatting, and (for a Task call) its own nested subcalls tree,
    unchanged by living inside this wrapper."""

    def __init__(self) -> None:
        self.count = 0
        self.chips = Vertical(classes="tool-calls-list")
        super().__init__(self.chips, title=self._render_title(), collapsed=True)

    def _render_title(self) -> str:
        return f"⚒ Tool calls ({self.count})"

    async def add_chip(self, chip: "ToolChip") -> None:
        self.count += 1
        self.title = self._render_title()
        await self.chips.mount(chip)


class ReasoningSection(Collapsible):
    """The turn's own summarized reasoning (v0.25.0, DOXA_SHOW_REASONING /
    doxa.config's show_reasoning row), collapsed by default: "✻ Reasoning
    (N chars)". Created lazily -- on the FIRST reasoning_delta of a turn
    (see TurnBlock.append_reasoning) -- so a turn with no reasoning (the
    setting is off, or the model simply didn't think) grows no section at
    all (hide-at-zero, same convention as ToolCallsSection/the status
    chips). Mounted ABOVE the response body: reasoning precedes the answer.

    N updates live as chunks arrive -- a title rewrite only, the SAME
    cheap pattern ToolCallsSection's "Tool calls (N)" header already uses.
    If the user expands this section mid-turn it STAYS expanded as more
    reasoning streams in: nothing here ever writes ``self.collapsed``
    itself, so only a click (or a test poking the reactive directly) can
    change it -- it never auto-collapses out from under the cursor, same
    invariant ToolCallsSection holds.

    Streams live even WHILE COLLAPSED (unlike ToolChip's lazy args/result
    formatting, which waits for first expand): the spec for this feature
    is explicit that collapsed must not mean paused, so the header's count
    and an expand-at-any-point both see up-to-date content. Reasoning
    turns are short and bounded (one thinking block per turn, not one per
    tool call), so the cost profile this app's lazy-formatting discipline
    exists to avoid -- N never-opened JSON pretty-prints -- doesn't apply
    here the same way.

    Rendering reuses the SAME Markdown.get_stream append-only path
    TurnBlock's own response body uses (v0.13.0): summarized reasoning is
    prose that can carry the model's own light formatting (a numbered
    plan, an occasional bold term), and a second streamed-text idiom next
    to the one this app already ships and already tested (test_restyle.py)
    would be a second thing to get right for no benefit."""

    def __init__(self) -> None:
        self.chars = 0
        self.body = Markdown("", classes="reasoning-body")
        self._stream: MarkdownStream | None = None
        super().__init__(self.body, title=self._render_title(), collapsed=True)

    def _render_title(self) -> str:
        return f"✻ Reasoning ({self.chars} chars)"

    async def append(self, chunk: str) -> None:
        self.chars += len(chunk)
        self.title = self._render_title()
        if self._stream is None:
            self._stream = Markdown.get_stream(self.body)
        await self._stream.write(chunk)

    async def stop(self) -> None:
        """Mirrors TurnBlock.mark_done's own stream teardown -- a finished
        turn must not leave this section's background write task running
        any more than the response body's may (see test_restyle.py's
        no-stream-survives-mark_done assertion; this feature carries the
        same one for reasoning)."""
        if self._stream is not None:
            await self._stream.stop()


SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
"""The spinner's glyph cycle. Braille dots (U+2801..U+28FF) rather than
anything from doxa's own ▎/✻/⚒ vocabulary: those are all *marks* that name
a kind of block, and reusing one as motion would make a running turn look
like a section header. The braille cycle is the one spinner shape every
terminal font in wide use carries a glyph for, and it occupies exactly one
cell in all of them -- a two-cell frame would make the label jitter
sideways on every tick, which is worse than no spinner."""

SPINNER_MIN_INTERVAL = 0.1
"""Seconds between two spinner repaints, at most ~10 Hz.

Not a timer -- there is no clock here at all. This is a floor on how often
an ARRIVING DELTA is allowed to cost a repaint. A long answer streams
hundreds of deltas (see SessionPane's own note about a 700-delta answer),
and repainting the marker for each of them would trade the old
LoadingIndicator's fixed 16 Hz for something worse: a repaint rate set by
the model's token rate. The floor caps the cost while a turn runs; when no
turn is running no delta arrives, nothing calls advance(), and the cost is
exactly zero."""


class ThinkingMarker(Static):
    """The in-flight marker on a running turn -- a spinner since v0.56.0,
    and still TIMERLESS, which is the whole design.

    This originally replaced a ``LoadingIndicator``, whose 16 Hz
    auto-refresh animation was the same class of cost as the leaked-timer
    bug this app already fixed once: every in-flight turn armed a repaint
    tick, and a terminal that repaints sixteen times a second to say
    "working" is spending the user's CPU on reassurance. The reported
    request -- "a spinner while reasoning or generating the output" --
    reads at first like an argument to put that back. It is not, because
    the expensive part of the old indicator was never the animation, it
    was the CLOCK behind it: a timer ticks whether or not anything is
    happening, and DOXA's status line is built under an explicit
    no-timer, no-per-frame rule (see :class:`doxa.ui.statusline.GitLine`).

    So this spinner is driven by the DELTA STREAM instead. A token
    arriving is a tick; :meth:`advance` moves the glyph on one frame and
    names the phase it arrived from. When nothing is arriving nothing
    ticks, which is precisely the behaviour wanted: an idle DOXA has no
    turn in flight, no deltas, no repaints, and ``auto_refresh`` stays
    ``None`` on every widget in the app. :data:`SPINNER_MIN_INTERVAL`
    keeps the other end honest -- a fast model must not be able to buy
    itself a 200 Hz repaint loop.

    PHASES. The user named two, and both are real events on the wire:
    ``reasoning`` (reasoning_delta) and ``generating`` (text_delta). A
    third, ``working``, covers a tool call in flight -- between a
    ``tool_call`` and its ``tool_result`` no delta arrives at all, so the
    glyph legitimately stops moving, and it must not sit there claiming
    to be generating text while a Bash command runs. The opening state is
    the original static ``⋯ thinking``: before the first event there is
    genuinely nothing to tick on, and inventing motion for it would be
    the animation-for-its-own-sake this app keeps refusing.

    v0.25.0's decision is REVERSED here, deliberately. That release had
    the first reasoning_delta HIDE this marker, on the grounds that a live
    "Reasoning (N chars)" header is itself the sign of life. It is -- but
    only while reasoning is what is happening, and the reported gap is the
    rest of the turn: a collapsed reasoning fold whose count stopped
    moving looks identical to a finished one, and a streaming answer's own
    text is the one thing a reader cannot use as a progress signal,
    because they are trying to read it. One marker that lives for the
    whole turn and says which phase it is in replaces three different
    "is it still going?" tells with one. It is still not a THIRD widget
    saying "working": ThinkingMarker is the widget that already said it,
    given the whole turn instead of its first second.

    Teardown is unchanged and total: :meth:`TurnBlock.hide_thinking` --
    called from ``mark_done`` on turn_done, from ``mark_done`` on the
    error path in ``_run_turn``, and from the restore path -- sets
    ``display`` False and reasserts ``auto_refresh = None``."""

    def __init__(self) -> None:
        self.frame = 0
        self.phase = ""
        self._last_tick = 0.0
        super().__init__("⋯ thinking", classes="thinking")

    def advance(self, phase: str) -> None:
        """One delta (or one tool call) arrived: move the glyph on, and
        show ``phase``.

        A phase CHANGE always repaints, even inside the interval floor:
        the phase is information the user asked for, and swallowing the
        switch from "reasoning" to "generating" to save a repaint would
        hide the very transition the marker exists to show. Only the
        glyph's own motion is rate-limited.

        A HIDDEN marker stays hidden and stays at its opening phase. Gone
        has to mean gone: the peer pump replays events, and a text_delta
        arriving after this turn's turn_done must not be able to bring a
        spinner back to life on a turn that already printed its cost."""
        if not self.display:
            return
        now = monotonic()
        if phase == self.phase and now - self._last_tick < SPINNER_MIN_INTERVAL:
            return
        self._last_tick = now
        self.frame = (self.frame + 1) % len(SPINNER_FRAMES)
        self.phase = phase
        self.update(f"{SPINNER_FRAMES[self.frame]} {phase}")


class TurnBlock(Collapsible):
    """One user turn + the assistant's response, foldable. The user's
    prompt lives in the fold header (title) only -- it is never re-set
    after construction, so it stays literal plain text no matter what the
    response below does (typed text must not reflow). The response body
    streams as MARKDOWN: ``self.body`` is a ``Markdown`` widget fed
    through ``Markdown.get_stream`` (textual 5's append-only streaming
    path, built for exactly this -- LLM deltas arriving chunk by chunk),
    so tables/bold/fences/inline code render as they complete without a
    full-document re-parse on every ``text_delta``. Top-level tool chips
    compact into ``self.tool_section`` (a ``ToolCallsSection``, created
    lazily on the first one -- see its own docstring); a Task call's
    subagent chips still nest inside THAT chip's own ``subcalls``,
    unaffected by any of this. The turn's own summarized reasoning (v0.25.0)
    compacts the SAME way into ``self.reasoning_section`` (a
    ``ReasoningSection``, created lazily on the first reasoning_delta --
    see its own docstring), mounted above the response body."""

    def __init__(self, prompt: str) -> None:
        self.prompt_text = prompt
        self.assistant_text = ""
        self.thinking = ThinkingMarker()
        self.reasoning_holder = Vertical(classes="turn-reasoning")
        self.reasoning_section: ReasoningSection | None = None
        self.body = Markdown("", classes="turn-body")
        self._stream: MarkdownStream | None = None
        self.tools = Vertical(classes="turn-tools")
        self.tool_section: ToolCallsSection | None = None
        super().__init__(
            # The marker is LAST (v0.56.0; it used to lead). A spinner
            # nobody can see is not a spinner: the block list scroll_end()s
            # after every event, so the bottom of the running turn is what
            # is on screen, and a marker pinned above a streaming answer
            # scrolls out of view within a paragraph. Trailing the output
            # also matches how it reads -- "here is what has arrived, and
            # here is doxa still working" -- rather than announcing work
            # above material that has already landed.
            self.reasoning_holder, self.body, self.tools, self.thinking,
            title=self._render_title(), collapsed=False,
        )

    def _render_title(self, suffix: str = "") -> str:
        return f"▎ {_one_line(self.prompt_text)}{suffix}"

    def hide_thinking(self) -> None:
        if self.thinking.display:
            self.thinking.display = False
            # Belt and braces after the LoadingIndicator removal: the old
            # indicator armed a 16 Hz auto-refresh on mount that nothing
            # else stopped, so every finished turn leaked a live timer and
            # idle CPU grew linearly with scrollback (measured ~0.2%/turn
            # headless; clearing it took 40 idle turn blocks from ~8.7% CPU
            # to baseline). ThinkingMarker arms nothing at all -- this line
            # now guarantees the invariant rather than repairing a widget.
            self.thinking.auto_refresh = None

    async def append_text(self, chunk: str) -> None:
        # v0.56.0: the marker advances instead of hiding. A delta arriving
        # IS the spinner's tick (see ThinkingMarker) -- and the phase it
        # names, "generating", is one of the two the request asked for.
        self.thinking.advance("generating")
        self.assistant_text += chunk
        # Lazy, like everything else in this pane: the stream (and its one
        # background asyncio task -- an event-driven coroutine, NOT a
        # Textual auto-refresh timer) is only created once a turn actually
        # has text to show, and mark_done() below stops it the moment the
        # turn finishes, so nothing outlives the turn it belongs to.
        if self._stream is None:
            self._stream = Markdown.get_stream(self.body)
        await self._stream.write(chunk)

    async def append_reasoning(self, chunk: str) -> None:
        """Mount (on first use) and stream into this turn's ONE
        ``ReasoningSection`` -- same lazy-creation shape as
        ``add_tool_chip``, mirrored for reasoning instead of tool calls.

        v0.56.0: this used to call ``hide_thinking()`` -- v0.25.0's
        judgment that a live "Reasoning (N chars)" header made the marker
        redundant. It now advances the marker into the ``reasoning``
        phase instead; ThinkingMarker's docstring records why that call
        was reopened."""
        self.thinking.advance("reasoning")
        if self.reasoning_section is None:
            self.reasoning_section = ReasoningSection()
            await self.reasoning_holder.mount(self.reasoning_section)
        await self.reasoning_section.append(chunk)

    async def add_tool_chip(self, chip: "ToolChip") -> None:
        """Mount one top-level tool chip (no ``parent_id`` -- a subagent's
        own calls nest inside its Task chip instead, see ToolChip's
        docstring) into this turn's ONE ``ToolCallsSection``, created on
        first use so a turn with no tool calls grows no section at all."""
        if self.tool_section is None:
            self.tool_section = ToolCallsSection()
            await self.tools.mount(self.tool_section)
        await self.tool_section.add_chip(chip)

    async def mark_done(
        self,
        cost_usd: float | None,
        duration_ms: int | None,
        is_error: bool,
        tier: str | None = None,
    ) -> None:
        """``tier`` (item T): the SAME subscription-vs-API rule the status
        bar and /usage already apply to the session TOTAL applies here to
        the per-turn figure too -- a bare ``$`` on subscription auth reads
        as a real per-turn bill next to a status bar that just said this
        account pays no dollars. ``None`` (API-key auth, or no account info
        yet) keeps the plain ``$`` figure unchanged."""
        self.hide_thinking()
        if self._stream is not None:
            # Stops the stream's one background task and flushes anything
            # still buffered -- a finished turn must not leave a live
            # asyncio task behind any more than it may leave a timer.
            await self._stream.stop()
        if self.reasoning_section is not None:
            # Same rule, same reason, for the reasoning section's own
            # stream -- see ReasoningSection.stop()'s docstring.
            await self.reasoning_section.stop()
        bits = []
        if duration_ms is not None:
            bits.append(f"{duration_ms}ms")
        if cost_usd is not None:
            bits.append(f"≈${cost_usd:.4f} if API" if tier else f"${cost_usd:.4f}")
        if is_error:
            bits.append("✗ error")
        suffix = f"  ·  {'  ·  '.join(bits)}" if bits else ""
        self.title = self._render_title(suffix)


class SubagentLine(Static):
    """Second status row: one clickable ``⧉ <label>`` per RUNNING Task-
    spawned subagent, mounted directly below ``#status-bar`` -- and ONLY
    while at least one is running (SessionPane._sync_subagent_line mounts
    it on the first, unmounts it on the last, mirroring the house hide-at-
    zero convention the peers/git/usage chips already use in the status
    bar itself, just one level up: this is a whole ROW that costs nothing
    at idle rather than a chip that reads empty).

    Each span is Textual click-action markup, ``[@click=open_transcript
    ('id')]⧉ label[/]`` -- ``Widget.broker_event`` resolves an unprefixed
    action against the clicked widget itself (confirmed empirically, see
    tests/test_subagent_tracker.py), so ``action_open_transcript`` lives
    right here rather than needing an ``app.`` / ``screen.`` namespace
    prefix on every span."""

    def __init__(self, pane: "SessionPane") -> None:
        super().__init__("", id="subagent-line")
        self.pane = pane

    def refresh_labels(self, entries: "list[tuple[str, str]]") -> None:
        """`entries`: (call_id, label) pairs in arrival order -- the
        registry (a plain dict) already keeps them in the order they
        started, and that is the only ordering this row promises."""
        spans = [
            f"[@click=open_transcript({json.dumps(call_id)})]"
            f"⧉ {_escape_markup(label)}[/]"
            for call_id, label in entries
        ]
        self.update("  ·  ".join(spans))

    async def action_open_transcript(self, call_id: str) -> None:
        await self.pane.open_transcript(call_id)


class SubagentTranscriptTab(TabPane):
    """One running (or finished) subagent's OWN activity, read-only: a
    plain ``TabPane`` -- deliberately NOT a ``SessionPane`` -- with no
    engine and no prompt, living alongside the session tabs in the SAME
    ``#session-tabs`` strip. Opened by clicking its row on a
    :class:`SubagentLine`; titled from the subagent's own label.

    Two content sources, both routed through ``SessionPane``: at OPEN time
    :meth:`replay` mirrors whatever the live Task chip already buffered
    (narration text + its direct subcall chips, via :func:`_clone_chip`)
    without touching the original trace-tree widgets; from then on,
    ``SessionPane._handle_event`` forwards further parent_id-matching
    events here AS WELL AS to the live chip, one level of nesting deep --
    a grandchild subagent (a Task spawned BY this one) gets its own
    second-line row and its own transcript tab instead of recursing into
    this one, exactly like a top-level Task would."""

    def __init__(
        self, call_id: str, label: str, owner: "SessionPane", *, id: str | None = None,
    ) -> None:
        self.call_id = call_id
        self.base_label = label
        self.owner = owner
        self.done = False
        self._narration_text = ""
        self._narration = Static("", classes="transcript-narration")
        self.chips_area = Vertical(classes="transcript-chips")
        self.scroll = VerticalScroll(
            self._narration, self.chips_area, classes="transcript-scroll",
        )
        # id, not call_id, keyed: mirror chips are fresh ToolChip instances
        # (see _clone_chip) that happen to reuse the ORIGINAL call's id, so
        # a tool_result event (matched by id) can find the right one here.
        self.mirror_chips: dict[str, ToolChip] = {}
        super().__init__(label, id=id)

    def compose(self) -> ComposeResult:
        yield self.scroll

    def append_narration(self, text: str) -> None:
        self._narration_text += text
        self._narration.update(self._narration_text)

    async def mirror_chip(self, chip: "ToolChip") -> "ToolChip":
        """Add ONE cloned chip for a direct child call that just arrived
        live (see SessionPane._route_transcript_chip) -- fresh, with
        whatever the source chip already knows (usually nothing yet but
        its name/input; a same-event tool_result lands right after)."""
        mirror = await _clone_chip(chip)
        self.mirror_chips[chip.call_id] = mirror
        await self.chips_area.mount(mirror)
        return mirror

    async def replay(self, chip: "ToolChip") -> None:
        """Open-time snapshot: the Task chip's buffered narration plus its
        current direct subcalls (each cloned recursively, so nesting that
        already happened by the time someone clicks is not lost) -- called
        once, right after this tab mounts."""
        if chip._sub_text:
            self.append_narration(chip._sub_text)
        for sub in list(chip.subcalls.children):
            if isinstance(sub, ToolChip):
                await self.mirror_chip(sub)

    def _set_title(self, text: str) -> None:
        self._title = self.render_str(text)
        _write_tab_label(self.app, self.id or "", text)

    def mark_done(self) -> None:
        """The tracked subagent's own tool_result just landed: title gets
        the ✓ suffix, and -- the same convention every other tab-status
        signal in this app follows -- a -done-unseen dot if this tab is
        not the one currently active (cleared on activation, see
        DoxaApp._on_tab_activated)."""
        if self.done:
            return
        self.done = True
        self._set_title(f"{self.base_label} ✓")
        with contextlib.suppress(Exception):
            tabbed = self.app.query_one("#session-tabs", TabbedContent)
            if tabbed.active != (self.id or ""):
                self._set_tab_class("-done-unseen", True)

    def _set_tab_class(self, class_name: str, value: bool) -> None:
        _write_tab_class(self.app, self.id or "", class_name, value)


class ArchivedSessionTab(TabPane):
    """A restored tab whose SESSION IS GONE: its conversation, read-only.

    v0.32.0. Restore has always cross-checked the saved tab set against
    the live daemon registry and dropped whatever no longer answered
    (doxa.tabsets.resolve) -- correct, because a dead session must never
    be replaced by a fresh one wearing its tab. But a daemon finalizing on
    its linger timer while the window is shut is the ORDINARY way a
    session ends, so "the tabs and their content come back" quietly meant
    "some of them do", and the user got one line of arithmetic about the
    rest.

    So a saved tab with no daemon but a transcript on disk comes back
    HERE instead: same strip, same order, same pinned name, the whole
    conversation rendered by the same ``mount_transcript`` a live restore
    uses -- and no engine, no prompt, no way to type into a session that
    does not exist. Deliberately NOT a ``SessionPane``, exactly like
    :class:`SubagentTranscriptTab` before it: a pane with a prompt box
    that refuses every prompt is a worse answer than a pane that visibly
    has none.

    The user can always tell which they got. The tab label carries a
    ``⏺`` archive mark, the first block says the session ended and where
    the text came from, and the palette/Ctrl+T remain the way to start a
    real session in the same repo.

    v0.56.0 demoted this from OUTCOME to FALLBACK. Restoring a tab now
    tries to RESUME its conversation first (doxa.cli, over
    :func:`doxa.history.resume_state`), and this class is what is left
    when that is impossible -- the session is somehow still running, its
    cwd is gone, the CLI has no history under its id, or the user turned
    ``resume_restored`` off. It therefore states WHY (:attr:`resume_note`)
    rather than only what: "read-only" with no reason reads as the feature
    having silently not happened."""

    ARCHIVE_MARK = "⏺"

    def __init__(
        self,
        session_id: str,
        cwd: str,
        label: str,
        *,
        pinned_name: "str | None" = None,
        id: str | None = None,
        resume_note: str = "",
    ) -> None:
        self.session_id = session_id
        self.cwd = cwd
        # v0.56.0: WHY this tab is read-only rather than resumed. Empty
        # when auto-resume is switched off (then read-only is the setting
        # doing what it says, not a failure worth explaining).
        self.resume_note = resume_note
        self.custom_name = pinned_name
        self.base_label = pinned_name or label
        self.turns_restored = 0
        self.scroll = VerticalScroll(id=f"archive-blocks-{session_id}")
        super().__init__(f"{self.ARCHIVE_MARK} {self.base_label}", id=id)

    def compose(self) -> ComposeResult:
        yield self.scroll

    async def on_mount(self) -> None:
        self.run_worker(self._render_archive(), exclusive=True, group="archive")

    async def _render_archive(self) -> None:
        """Read the transcript off-loop, then mount it. Never raises: an
        archived tab that cannot read its own file still says what it is,
        which is strictly more than the tab that used to silently not
        exist.

        Not named ``_render``: that is ``textual.widget.Widget``'s own
        synchronous paint hook, and a coroutine in that slot is handed
        straight to the compositor as if it were a visual."""
        why = f"\n\n{self.resume_note}" if self.resume_note else ""
        await self.scroll.mount(SystemBlock(
            f"⏺ this session has ended — transcript restored from disk, "
            f"read-only.\nsession  {self.session_id}\ncwd      {self.cwd}\n"
            f"Ctrl+T starts a new session here.{why}"
        ))
        try:
            snapshot = await asyncio.to_thread(
                transcript_mod.read, self.session_id, self.cwd,
            )
        except Exception:  # noqa: BLE001 -- see the docstring
            return
        if not snapshot:
            await self.scroll.mount(SystemBlock(
                "no transcript could be read for this session."
            ))
            return
        self.turns_restored = await mount_transcript(self.scroll, snapshot)

    def _set_tab_class(self, class_name: str, value: bool) -> None:
        _write_tab_class(self.app, self.id or "", class_name, value)

    def as_record(self) -> "tabsets_mod.TabRecord":
        """What this tab contributes to the persisted set. An archived tab
        STAYS in the record: it is still one of the tabs the user has
        open, and dropping it here would mean a session survived one
        restart and vanished on the next."""
        return tabsets_mod.TabRecord(self.session_id, self.custom_name, self.cwd)
