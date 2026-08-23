"""doxa.history -- full-text session search, as the ``/search`` popup.

ONE search path, not two. The Phase 3 shape was a modal overlay on Ctrl+R;
it is gone, and Ctrl+R now prefills ``/search `` in the prompt -- so the
key, the slash command and the palette entry all land on the SAME surface
and cannot drift apart on what "history search" means or finds.

The surface is :class:`SessionSearch`: a popup list above the prompt that
opens the moment the prompt reads exactly ``/search `` (slash, the word,
one separating space) and re-queries LIVE on every further keystroke.
Focus never leaves the prompt -- the user keeps typing, the list keeps
updating underneath. That is the whole point: search is a mode of the
prompt line, not a screen you visit.

What it queries is unchanged and shared: ``doxa.operators._session_search``,
the registry operator whose SQL mirrors `lore search` (BM25 over the ``msg``
FTS5 table, AND-first-then-OR widening, current project first, then all).
The snippets it renders are FTS5's own ``snippet()`` output -- the matched
terms come back already bracketed by SQLite, so the highlighting is the
index's opinion of what matched, never a second guess at it.

Race discipline: keystrokes are debounced (:data:`DEBOUNCE_SECS`) and every
query carries a sequence number. A query whose number is no longer current
when it returns DROPS its results instead of painting them -- a slow query
for "au" must never overwrite a fresh one for "audit", which is exactly the
bug a debounce alone does not prevent.

Read-only throughout: the popup serves the EXISTING index and never grows
it (the same read-only contract the operator declares).
"""

from __future__ import annotations

import asyncio

from rich.text import Text
from textual.widgets import OptionList
from textual.widgets.option_list import Option

# The literal prefix that arms the popup. The trailing space is load-
# bearing: "/search" alone is a command the autocomplete is still offering
# to complete, and popping a result list over a half-typed command name
# would fight the dropdown for the same rows of the terminal.
SEARCH_PREFIX = "/search "

# Debounce: long enough that a fast typist runs one query per word rather
# than one per letter, short enough that the list feels live.
DEBOUNCE_SECS = 0.13
RESULT_LIMIT = 20

# The colour the matched terms wear inside a snippet -- the house accent,
# same as the highlighted row (theme.tcss).
MATCH_STYLE = "#D97757"
DIM_STYLE = "#8A8073"


def search_sessions(query: str, cwd: str, limit: int = RESULT_LIMIT) -> list[dict]:
    """BM25 hits from LORE's session index, via the registry operator's own
    implementation. Each hit: {session_id, project, ts, role, snippet}.
    Errors (empty query, no index yet) come back as no hits -- a popup
    must degrade to silence, never to a traceback."""
    from . import gate as gate_mod
    from .operators import _session_search

    op_ctx = gate_mod.OperatorContext(
        session_id="doxa-history-ui",
        cwd=cwd,
        repo_root=gate_mod.repo_root_of(cwd),
    )
    try:
        result = _session_search(query, limit=limit, op_ctx=op_ctx)
    except Exception:
        return []
    if not isinstance(result, dict) or result.get("error"):
        return []
    hits = [h for h in result.get("hits") or [] if isinstance(h, dict)]
    return with_titles(hits)


def recent_sessions(cwd: str, limit: int = RESULT_LIMIT) -> list[dict]:
    """What an EMPTY query shows: the most recent indexed sessions, this
    project first, then everywhere else. An empty box would teach the user
    that nothing is indexed; the recents say what there is to search."""
    from lore_core.config import project_slug
    from lore_core.store import db_connect

    try:
        conn = db_connect()
        slug = project_slug(cwd)
        rows: list = []
        seen: set[str] = set()
        for scope in (slug, None):
            sql = (
                "SELECT session_id, project, title, last_ts, messages"
                " FROM sessions"
            )
            params: list = []
            if scope:
                sql += " WHERE project = ?"
                params.append(scope)
            sql += " ORDER BY last_ts DESC LIMIT ?"
            params.append(limit)
            for row in conn.execute(sql, params).fetchall():
                if row[0] in seen:
                    continue
                seen.add(row[0])
                rows.append(row)
            if len(rows) >= limit:
                break
    except Exception:
        return []
    hits: list[dict] = []
    for session_id, project, title, last_ts, messages in rows[:limit]:
        count = int(messages or 0)
        hits.append({
            "session_id": session_id,
            "project": project,
            "ts": last_ts or "",
            "role": "",
            "title": (title or "").strip(),
            "snippet": f"{count} message{'' if count == 1 else 's'}",
        })
    return hits


def with_titles(hits: list[dict]) -> list[dict]:
    """Fill each hit's ``title`` from LORE's ``sessions`` table. The FTS
    table stores no title (it indexes messages), and "which conversation
    was this?" is the first thing a reader of a result list asks."""
    ids = [str(h.get("session_id") or "") for h in hits]
    ids = [i for i in ids if i]
    if not ids:
        return hits
    titles: dict[str, str] = {}
    try:
        from lore_core.store import db_connect

        conn = db_connect()
        placeholders = ",".join("?" * len(ids))
        for session_id, title in conn.execute(
            f"SELECT session_id, title FROM sessions WHERE session_id IN ({placeholders})",
            ids,
        ).fetchall():
            titles[str(session_id)] = (title or "").strip()
    except Exception:
        return hits
    for hit in hits:
        hit.setdefault("title", titles.get(str(hit.get("session_id") or ""), ""))
    return hits


def hit_reference(hit: dict) -> str:
    """The text reference the prompt receives for a chosen hit. Carries the
    full session id (so the model's lore tools can follow it) plus timestamp
    and the de-marked snippet."""
    snippet = str(hit.get("snippet", "")).replace("[", "").replace("]", "")
    sid = hit.get("session_id", "?")
    ts = str(hit.get("ts", ""))[:19]
    return f'[lore session {sid} · {ts}] "{snippet}"'


def snippet_markup(snippet: str) -> Text:
    """FTS5's ``snippet()`` brackets the matched terms; render those
    brackets as HIGHLIGHT rather than as punctuation. Nothing here decides
    what matched -- SQLite already did, and this only paints its answer."""
    text = Text()
    rest = snippet
    while True:
        open_at = rest.find("[")
        close_at = rest.find("]", open_at + 1) if open_at >= 0 else -1
        if open_at < 0 or close_at < 0:
            text.append(rest)
            return text
        text.append(rest[:open_at])
        text.append(rest[open_at + 1:close_at], style=MATCH_STYLE)
        rest = rest[close_at + 1:]


def row_label(hit: dict) -> Text:
    """One result row: session title (or its id), date, then the snippet
    with its matched terms lit up."""
    title = str(hit.get("title") or "").strip() or str(hit.get("session_id", "?"))[:8]
    ts = str(hit.get("ts", ""))[:16].replace("T", " ")
    label = Text()
    label.append(f"{title[:30]:<30} ", style="bold")
    label.append(f"{ts:<16} ", style=DIM_STYLE)
    label.append_text(snippet_markup(str(hit.get("snippet", ""))))
    return label


class SessionSearch(OptionList):
    """The live ``/search`` popup, above the prompt input.

    Never focusable, exactly like the slash autocomplete it sits beside: a
    result list that stole the caret would end the typing it exists to
    serve. The prompt drives it through :meth:`sync` (one call per
    keystroke) and the prompt's key protocol drives its selection.

    Dismissal latches like the autocomplete's: Esc closes it and keeps it
    closed until the ``/search `` prefix is gone from the line, so a user
    who dismissed the list can finish typing their sentence in peace."""

    can_focus = False

    def __init__(self, cwd: str) -> None:
        super().__init__(id="session-search")
        self.display = False
        self.cwd = cwd
        self.hits: list[dict] = []
        self.query_text: str | None = None  # what the SHOWN hits are for
        self._seq = 0
        self._timer = None
        self._dismissed = False

    @property
    def is_open(self) -> bool:
        return bool(self.display)

    # -- prompt-driven lifecycle --------------------------------------

    def sync(self, value: str) -> None:
        """React to the prompt's current text. The ONE entry point: open on
        the prefix, re-query on every further keystroke, close when the
        prefix is gone (which is also what a Backspace over the separating
        space does -- no special key handling needed for it)."""
        if not value.startswith(SEARCH_PREFIX):
            self._dismissed = False  # the prefix is gone: the latch clears
            self.close()
            return
        if self._dismissed:
            return
        query = value[len(SEARCH_PREFIX):]
        if self.display and query == self.query_text:
            return  # nothing about the query changed (e.g. a cursor move)
        self.display = True
        self._schedule(query)

    def _schedule(self, query: str) -> None:
        if self._timer is not None:
            self._timer.stop()
        self._timer = self.set_timer(DEBOUNCE_SECS, lambda: self.launch(query))

    def launch(self, query: str) -> None:
        """Start one query. Public so a caller (and the test suite) can run
        the query without waiting out the debounce."""
        self._seq += 1
        self.run_worker(self._run(query, self._seq), group="session-search")

    async def _run(self, query: str, seq: int) -> None:
        term = query.strip()
        if term:
            hits = await asyncio.to_thread(
                search_sessions, term, self.cwd, RESULT_LIMIT
            )
        else:
            hits = await asyncio.to_thread(recent_sessions, self.cwd, RESULT_LIMIT)
        if seq != self._seq or not self.display:
            # A newer keystroke has already queried (or the popup closed
            # while this ran): these results are stale by construction and
            # painting them would show an answer to a question the user has
            # already moved on from.
            return
        self._render(query, hits)

    def _render(self, query: str, hits: list[dict]) -> None:
        self.hits = hits
        self.query_text = query
        self.clear_options()
        if not hits:
            # Quiet, not an error: an index with no hit for "zzz" is a
            # normal answer to a normal question.
            self.add_option(Option(Text("no matches", style=DIM_STYLE), disabled=True))
            self.highlighted = None
            return
        for hit in hits:
            self.add_option(Option(row_label(hit)))
        self.highlighted = 0

    def close(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if self.display:
            self.display = False
        self.hits = []
        self.query_text = None
        self._seq += 1  # invalidate anything still in flight

    def dismiss_for_this_line(self) -> None:
        """Esc: close, keep the typed text, and stay shut until the
        ``/search `` prefix leaves the line."""
        self._dismissed = True
        self.close()

    # -- selection ----------------------------------------------------

    def move(self, delta: int) -> None:
        if not self.hits:
            return
        current = self.highlighted if self.highlighted is not None else 0
        self.highlighted = (current + delta) % len(self.hits)

    def chosen(self) -> "dict | None":
        if not self.hits:
            return None
        index = self.highlighted if self.highlighted is not None else 0
        return self.hits[index] if 0 <= index < len(self.hits) else None
