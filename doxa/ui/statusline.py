"""doxa.ui.statusline -- the status bar, and the two chips that own state.

Extracted from ``doxa/app.py`` unchanged. :class:`StatusBar` renders the
markup a pane hands it and owns the click actions each chip names;
:class:`GitLine` keeps the repo/branch/sha chip event-driven (never on a
timer -- see its docstring for the idle-CPU rule this app measures);
:class:`ClockChip` is the one thing here that does tick.

What a chip SAYS is decided in :mod:`doxa.session.chips`, which builds an
ordered sequence of :class:`~doxa.session.chips.StatusChip` records and
hands their rendered markup to :meth:`StatusBar.update`. That sequence is
docs/plugin-api.md's second extension point.
"""

from __future__ import annotations

from pathlib import Path

from textual import events
from textual.content import Content
from textual.widgets import Static

from .. import clock as clock_mod
from .. import peers as peers_mod
from .. import worktrees as worktrees_mod
from .labels import _chip_span, git_branch_symbol


class GitLine:
    """The `repo ⎇ branch sha` chip for the status line.

    Cost discipline (this sits next to the idle-CPU fix for a reason): the
    repo root is resolved ONCE at construction (the only subprocess); after
    that a read is a couple of stats and at most two small file reads --
    ``.git/HEAD`` re-parsed only when its mtime moves (checkout/switch touch
    it), and the branch's ref file re-read only when ITS mtime moves (a
    commit touches the ref, not HEAD -- which is exactly why the sha needs
    its own stat rather than riding HEAD's). ``packed-refs`` is the
    fallback for a branch with no loose ref, cached the same way.
    render() is called from event-driven sites only (_refresh_status: boot,
    turn done, peer events) -- NEVER from a timer or per-frame hook, which
    would recreate the busy-idle bug this app just shed."""

    def __init__(self, cwd: str) -> None:
        self._cwd = cwd
        self.repo_root = peers_mod.repo_root_of(cwd)
        self.repo: str | None = None
        self._head: Path | None = None
        self._gitdir: Path | None = None
        self._mtime: float | None = None
        self._branch: str | None = None
        self._ref: str | None = None      # refs/heads/<branch>, when attached
        self._sha: str | None = None
        self._sha_mtime: float | None = None
        self.worktree: str | None = None
        # The gitdir that holds refs/heads/<branch> and packed-refs -- see
        # _read_sha's docstring for why this is NOT always self._gitdir.
        self._commondir: Path | None = None
        if self.repo_root:
            git = Path(self.repo_root) / ".git"
            if git.is_file():
                # Worktree/submodule: .git is a one-line pointer file.
                try:
                    for line in git.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines():
                        if line.startswith("gitdir:"):
                            gitdir = Path(line.split(":", 1)[1].strip())
                            if not gitdir.is_absolute():
                                gitdir = (Path(self.repo_root) / gitdir).resolve()
                            git = gitdir
                            break
                except OSError:
                    return
                # A linked worktree's gitdir is <main>/.git/worktrees/<name>
                # -- the last component IS the worktree's name, which is
                # what `git worktree list` calls it. A submodule's gitdir
                # sits under modules/ instead and leaves this None.
                if git.parent.name == "worktrees":
                    self.worktree = git.name
            self._gitdir = git
            self._head = git / "HEAD"
            self._commondir = self._resolve_commondir(git)
        # The repo NAME is always the MAIN checkout's, never a linked
        # worktree's own directory -- since v0.17 (worktree-per-session)
        # every session's cwd IS such a worktree, and `Path(repo_root).name`
        # there reads `doxa-<shortid>`, printing the session id twice
        # alongside the `doxa/<shortid>` branch chip beside it (reported).
        # self._commondir already resolves THROUGH the worktree's commondir
        # pointer for the sha read above -- its parent is the main repo root
        # in every case, worktree or not, and reusing it costs no extra
        # subprocess (pure filesystem reads, same "one subprocess total"
        # discipline this class already documents). Anything where that
        # resolution doesn't land on a plain ".git" (a submodule, a bare
        # repo) falls back to the worktree-root name, same as before.
        if self._commondir is not None and self._commondir.name == ".git":
            self.repo = self._commondir.parent.name
        elif self.repo_root:
            self.repo = Path(self.repo_root).name
        # Item S / the tab-label regression it surfaced: the worktree
        # sidecar's own base_ref (see doxa.worktrees), mtime-guarded the
        # SAME way HEAD/the ref file above are -- a live `/branch` switch
        # rewrites this file (worktrees.update_base), and the next event-
        # driven render sees it with no polling and no reconstructing this
        # GitLine. None outside a worktree-per-session session (no
        # sidecar): callers fall back to branch_label().
        self._base_meta_path = worktrees_mod.meta_file_path(cwd)
        self._base_mtime: float | None = None
        self._base_ref_cached: str | None = None

    @staticmethod
    def _resolve_commondir(gitdir: Path) -> Path:
        """A linked worktree's private gitdir (``<main>/.git/worktrees/
        <name>``) holds only its own HEAD, index and logs -- refs/heads/*
        and packed-refs are SHARED, and live under the ``commondir`` file's
        target (ordinarily ``../..``, i.e. the main repo's ``.git``). A
        normal (non-worktree) repo has no ``commondir`` file and its
        gitdir already IS the common one, so this returns it unchanged."""
        commondir_file = gitdir / "commondir"
        try:
            text = commondir_file.read_text(
                encoding="utf-8", errors="replace"
            ).strip()
        except OSError:
            return gitdir
        if not text:
            return gitdir
        common = Path(text)
        if not common.is_absolute():
            common = (gitdir / common).resolve()
        return common

    def render(self, *, clickable: bool = False) -> str | None:
        """`repo ⎇ branch sha`, or None outside a repo (no chip at all).

        The branch half is :meth:`tab_branch` -- the SAME string a tab
        shows -- so a linked worktree reads `repo ⎇ main@featureX @sha`
        here too: one source of truth for "how does a worktree spell its
        branch", inherited rather than re-derived.

        v0.28.0 (reported: "when i chose a branch and click on one, it is
        not changed") -- that invariant was BROKEN, and the broken half is
        the whole defect. This used :meth:`branch_label`, the branch
        actually checked out here, while the tab had moved to
        :meth:`tab_branch`, the BASE, back in item S. Inside a
        worktree-per-session session those are different strings and only
        one of them is what the branch picker changes: picking a branch
        runs ``doxa.worktrees.switch_base``, which rebases the session's
        own throwaway ``doxa/<id>`` branch onto the new base and rewrites
        the sidecar's ``base_ref`` -- it never renames what HEAD points
        at. So a switch that fully SUCCEEDED left this chip byte-identical
        (measured: `myrepo ⎇ doxa/abc123de@myrepo-abc123de @5016a09`
        before and after), which reads exactly like nothing happened.
        Showing the base restores the docstring's own promise, makes the
        picker's effect visible immediately, and drops a third printing of
        the session id from a bar that already carries it in its own
        handle chip -- the same reasoning item S applied to tab labels.
        The checked-out branch is not lost: it moves into this segment's
        tooltip (see :meth:`chip_hints`), which is where "and what is HEAD
        really on" belongs once the visible text answers "what am I
        working off".

        The short sha sits immediately right of the branch, because that is
        where "which commit am I actually on" belongs -- next to the branch
        it qualifies, not at the far end of the bar. Omitted when it would
        merely repeat the branch label (detached HEAD).

        `clickable` (status-chips, item Y; widened in v0.24.0's item 4)
        wraps the branch segment in the click-action span that opens the
        branch picker AND the repo-name segment in the one that opens the
        repo/path picker -- v0.22.0 called the repo name INERT; the
        operator's own follow-up report overrides that (see
        StatusBar.action_open_repo_picker and SessionPane.open_repo_picker
        for what selecting a row there actually does). The sha stays
        information, never a selector -- there is nothing to pick from one
        commit id. Default False keeps every other caller (the identity
        block's `/about`-style dump, every pre-chips test that asserts
        this string verbatim) exactly as it was; only
        `SessionPane._refresh_status` passes True."""
        if not self.repo:
            return None
        # branch_label() FIRST, unconditionally: it is what re-reads HEAD
        # (mtime-guarded) and therefore what keeps `self._ref` -- and so
        # _read_sha below -- alive. tab_branch()'s base half never touches
        # HEAD, so taking the base without this leaves the sha unresolved
        # on a worktree session's very first render.
        checked_out = self.branch_label()
        branch = self.base_branch() or checked_out
        repo_text = _chip_span(self.repo, "open_repo_picker") if clickable else self.repo
        if not branch:
            return repo_text
        branch_text = _chip_span(branch, "open_branch_picker") if clickable else branch
        chip = f"{repo_text} {git_branch_symbol()} {branch_text}"
        sha = self._read_sha()
        if sha and not branch.startswith(sha):
            # "@" marks this hex string as a COMMIT. The status bar also
            # carries the detached-session handle, another short hex-ish
            # id a few chips away, and two unlabelled hex strings in one
            # bar read as one commit id printed twice (reported as exactly
            # that). Neither is dropped -- both are labelled instead.
            chip += f" @{sha}"
        return chip

    def chip_hints(self) -> "list[tuple[str, str]]":
        """(plain_text, tooltip) for each segment :meth:`render` prints, in
        the SAME left-to-right order -- item 5's tooltip machinery reads
        this alongside the markup `render` builds, rather than re-parsing
        that markup back apart."""
        if not self.repo:
            return []
        hints = [(
            self.repo,
            "repo this session is rooted in -- click to open a "
            "different repo in a new tab",
        )]
        # Same order and same reason as render() above -- one string pair
        # per segment it prints, so this has to derive them the same way.
        head = self.branch_label()
        base = self.base_branch()
        branch = base or head
        if not branch:
            return hints
        # Inside a worktree-per-session session the visible text is the BASE
        # (see render's own v0.28.0 note); the checked-out branch is the
        # fact this tooltip still owes the reader, so it says both.
        checked_out = head if base and head != base else None
        hints.append((
            branch,
            "base branch this session works off -- click to switch it"
            + (f" (HEAD here is {checked_out})" if checked_out else ""),
        ))
        sha = self._read_sha()
        if sha and not branch.startswith(sha):
            hints.append((f"@{sha}", "short commit id of the branch tip"))
        return hints

    def branch_label(self) -> str | None:
        """The branch actually checked out HERE: `main`, or `main@featureX`
        inside a linked worktree. SESSION identity -- what render() (the
        status bar chip) and /about show, exactly what git has HEAD
        pointed at right now. NOT what a tab shows; see :meth:`tab_branch`
        for that (item S's fix for the v0.17 regression where the tab
        label started showing this SAME string -- `doxa/f13526d4` -- which
        is the session's own throwaway branch, not the base the operator
        is orienting by, and which repeats the session id already visible
        elsewhere).

        The worktree name is only added when it says something the label
        does not already carry -- `git worktree add ../foo -b foo` makes
        the worktree, the branch and the directory all "foo", and a label
        reading `foo ⎇ foo@foo` is three copies of one fact. So the suffix
        appears only when the worktree name differs from BOTH the branch
        and the repo slot beside it."""
        branch = self._read_branch()
        if not branch:
            return None
        if self.worktree and self.worktree not in (branch, self.repo):
            return f"{branch}@{self.worktree}"
        return branch

    def base_branch(self) -> str | None:
        """The worktree sidecar's recorded ``base_ref`` (doxa.worktrees),
        re-read on the SAME mtime-guard discipline as HEAD/the ref file
        above -- ``None`` outside a worktree-per-session session (no
        sidecar at all), in which case :meth:`tab_branch` falls back to
        :meth:`branch_label`, the checked-out branch, exactly as it always
        was before v0.17."""
        try:
            mtime = self._base_meta_path.stat().st_mtime
        except OSError:
            return self._base_ref_cached
        if mtime == self._base_mtime:
            return self._base_ref_cached
        self._base_mtime = mtime
        meta = worktrees_mod.read_meta(self._cwd)
        self._base_ref_cached = str(meta.get("base_ref") or "") or None if meta else None
        return self._base_ref_cached

    def tab_branch(self) -> "tuple[str | None, bool]":
        """What the TAB label's branch half actually shows, and whether
        this is a worktree-isolated session (the caller uses the second
        value to decide whether compose_tab_label's isolation marker
        earns its keep) -- item S / the v0.17 tab-label regression.

        Orientation, not identity: a worktree session's tab says `main`
        (what it is WORKING OFF), not `doxa/f13526d4` (branch_label's
        session handle, which the status bar keeps -- see that method's
        docstring). Falls back to branch_label() with no worktree sidecar,
        so worktree_per_session OFF (or a cwd that was never a doxa
        worktree) reads exactly as it did before this feature: the
        checked-out branch IS the base there."""
        base = self.base_branch()
        if base:
            return base, True
        return self.branch_label(), False

    def _read_branch(self) -> str | None:
        if self._head is None:
            return None
        try:
            mtime = self._head.stat().st_mtime
        except OSError:
            return self._branch  # HEAD briefly gone (rebase): keep last known
        if mtime == self._mtime:
            return self._branch
        self._mtime = mtime
        try:
            head = self._head.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return self._branch
        self._sha_mtime = None  # HEAD moved: the sha must be re-read too
        if head.startswith("ref:"):
            self._ref = head.split(":", 1)[1].strip()
            self._branch = self._ref.removeprefix("refs/heads/")
        else:
            self._ref = None
            self._sha = head[:7] or None
            self._branch = head[:8] or None  # detached HEAD: short sha
        return self._branch

    def _read_sha(self) -> str | None:
        """The short sha of the branch tip. A COMMIT moves the ref file,
        not HEAD, so this stats the ref in its own right -- still event-
        driven (a stat per status refresh), still never polled.

        Reads from ``self._commondir``, NOT ``self._gitdir``: inside a
        linked worktree the checked-out branch's ref file lives in the
        MAIN repo's ``refs/heads/``, shared via the worktree's ``commondir``
        pointer (see ``_resolve_commondir``) -- the worktree's own private
        gitdir never has it, which is why this used to come back None for
        every worktree session (pinned, then fixed, in
        tests/test_statusline.py)."""
        if self._commondir is None or self._ref is None:
            return self._sha
        ref_path = self._commondir / self._ref
        try:
            mtime = ref_path.stat().st_mtime
        except OSError:
            return self._read_packed_sha()
        if mtime == self._sha_mtime:
            return self._sha
        self._sha_mtime = mtime
        try:
            self._sha = ref_path.read_text(
                encoding="utf-8", errors="replace"
            ).strip()[:7] or None
        except OSError:
            pass
        return self._sha

    def _read_packed_sha(self) -> str | None:
        """A freshly cloned or gc'd repo keeps its branch tips in
        packed-refs and has no loose ref file. Same mtime discipline, and
        the same commondir redirection ``_read_sha`` needs -- packed-refs
        is shared across a repo's worktrees exactly like refs/heads/*."""
        if self._commondir is None or self._ref is None:
            return self._sha
        packed = self._commondir / "packed-refs"
        try:
            mtime = packed.stat().st_mtime
        except OSError:
            return self._sha
        if mtime == self._sha_mtime:
            return self._sha
        self._sha_mtime = mtime
        try:
            for line in packed.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                if line.endswith(f" {self._ref}"):
                    self._sha = line.split(" ", 1)[0].strip()[:7] or None
                    break
        except OSError:
            pass
        return self._sha


class ClockChip(Static):
    """The upper-right clock (item M): fixed width, dock:right on its own
    layer (see ``#doxa-clock`` in theme.tcss for why that -- not a flow
    sibling -- is what keeps the tab bar from ever being displaced).

    ONE timer for its whole life, and only while enabled: it rides
    Textual's own ``auto_refresh`` -- the exact ``_auto_refresh_timer``
    slot the no-idle-timer guard tests already watch (see
    ``tests/test_chrome.py``'s ``_armed`` and the matching helper in
    ``tests/test_app.py``) -- but re-armed to a freshly computed,
    BOUNDARY-ALIGNED delay on every tick (:func:`doxa.clock.
    seconds_until_boundary`) rather than left at a fixed period. That is
    what makes it minute-aligned when seconds are hidden instead of a 1Hz
    timer silently redrawing an identical string sixty times for one
    visible change, and second-aligned when they are shown. Disabled
    config never sets ``auto_refresh`` at all: no config, no timer, full
    stop -- the same contract :meth:`reconfigure` restores on every
    settings save, so toggling the clock off leaves nothing armed."""

    def __init__(self) -> None:
        super().__init__("", id="doxa-clock")
        self.cfg = clock_mod.ClockConfig.load()

    def on_mount(self) -> None:
        self.reconfigure()

    def reconfigure(self) -> None:
        """Re-read settings and restart clean. Called at mount, and again
        after the settings modal saves -- the settings screen's `_saved`
        callback owns that second call, the same way it already refreshes
        every pane's status bar."""
        self.auto_refresh = None  # stop whatever the OLD config armed
        self.cfg = clock_mod.ClockConfig.load()
        self.display = self.cfg.show
        if self.cfg.show:
            self._tick()

    def _tick(self) -> None:
        now = clock_mod.now_utc()
        text, warning = clock_mod.render(now, self.cfg)
        self.update(text)
        self.tooltip = warning  # the "visible-error fallback": a bad
        # custom format or an unresolvable timezone still renders (the
        # built-in format, system-local time) -- the tooltip is where the
        # degradation is disclosed rather than swallowed.
        self.auto_refresh = clock_mod.seconds_until_boundary(
            now, self.cfg.show_seconds
        )

    def automatic_refresh(self) -> None:
        """Textual calls this when ``_auto_refresh_timer`` fires. The
        default implementation just repaints; this one repaints AND
        re-arms the next boundary -- that re-arm, from inside the very
        callback the old timer is finishing, is what makes this ONE
        self-rescheduling timer rather than a periodic one this class
        would otherwise need to stop and restart from outside."""
        if self.cfg.show:
            self._tick()


class StatusBar(Static):
    """The top status row -- SAME click-action-span pattern SubagentLine
    (below) already established for its own row: an unprefixed
    `[@click=...]` markup span resolves against the CLICKED widget itself
    (`Widget.broker_event`, confirmed empirically there), so each action
    method just needs to live on this class and delegate to the owning
    pane.

    v0.24.0 widened the tiers the release-notes' "for every chip?" answer
    drew: model/branch/effort/the repo name (item 4 -- overrides v0.22.0's
    "repo name is INERT") open the shared :class:`ChipPicker`; peers/ctx%/
    the session handle/beliefs are ACTIONABLE (peers -> /sessions, ctx% ->
    a confirm THEN /compact -- see :class:`CompactConfirm`, the session
    handle -> a sessions picker, beliefs -> a beliefs picker); cost, sha
    and usage headroom stay plain -- INERT never meant "unexplained", see
    :meth:`set_chip_hints` below. One action per chip rather than a single
    dispatcher taking an argument: simpler markup (no `json.dumps`-escaped
    action params to get wrong), and every action here is a fixed, known
    operation anyway.

    Tooltips (item 5): Textual's ``Widget.tooltip`` is read fresh by the
    screen's own hover timer every time the mouse moves over a widget
    (``Screen._handle_tooltip_timer`` re-reads ``widget.tooltip``, and the
    setter re-triggers ``Screen._update_tooltip`` immediately if this
    widget is already the one being shown) -- so ONE ``Static`` can serve a
    DIFFERENT tooltip for different chips under the cursor, the same way it
    already serves a different click action for different chips, without
    splitting the bar into N widgets. That would have been the "clean"
    fix if the bar were not already built this way, but it would also mean
    rewriting every existing click-span action into a real per-widget click
    handler and re-pinning every markup/order assertion in
    tests/test_status_chips.py for a purely mechanical reason -- this is
    the smaller, evidence-backed diff for the SAME requirement (every chip,
    including the inert ones, gets a one-sentence hover explanation), and
    it changes nothing about `_refresh_status`'s string-building, so chip
    order/spacing/colors are untouched by construction, not by discipline."""

    def __init__(self, pane: "SessionPane") -> None:
        super().__init__("doxa · connecting…", id="status-bar")
        self.pane = pane
        # (plain_chip_text, tooltip) in DISPLAY order -- rebuilt by
        # SessionPane._refresh_status alongside the markup string itself,
        # so the two can never drift out of sync with each other.
        self._chip_hints: "list[tuple[str, str]]" = []

    def set_chip_hints(self, hints: "list[tuple[str, str]]") -> None:
        self._chip_hints = hints

    def _tooltip_for_x(self, x: int) -> "str | None":
        """Which chip's tooltip covers status-bar-relative `x` -- the SAME
        `#status-bar { padding: 0 2 }` left offset
        tests/test_status_chips.py's own `_offset_of` helper already
        subtracts for click coordinates; a MouseMove event's `x` is
        region-relative the identical way (`Screen._translate_mouse_move_
        event`), so one constant serves both."""
        if not self._chip_hints:
            return None
        pos = x - 2
        if pos < 0:
            return None
        plain = Content.from_markup(str(self.renderable)).plain
        cursor = 0
        for text, hint in self._chip_hints:
            idx = plain.find(text, cursor)
            if idx == -1:
                continue
            end = idx + len(text)
            if idx <= pos < end:
                return hint
            cursor = end
        return None

    def _on_mouse_move(self, event: events.MouseMove) -> None:
        self.tooltip = self._tooltip_for_x(event.x)

    async def action_open_model_picker(self) -> None:
        await self.pane.open_model_picker()

    async def action_open_branch_picker(self) -> None:
        await self.pane.open_branch_picker()

    async def action_open_effort_picker(self) -> None:
        await self.pane.open_effort_picker()

    async def action_open_mode_picker(self) -> None:
        await self.pane.open_mode_picker()

    def action_open_repo_picker(self) -> None:
        self.pane.open_repo_picker()

    def action_open_sessions(self) -> None:
        self.pane.run_status_command("/sessions")

    def action_compact_now(self) -> None:
        self.pane.run_compact_now()

    def action_open_sessions_picker(self) -> None:
        self.pane.open_sessions_picker()

    async def action_open_beliefs_picker(self) -> None:
        await self.pane.open_beliefs_picker()

    async def action_open_pending_picker(self) -> None:
        """The staged-proposals chip (v0.57.0). Same one-action-per-chip
        shape every other clickable chip here follows."""
        await self.pane.open_pending_picker()
