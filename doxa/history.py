"""doxa.history -- Ctrl+R history search over LORE's session FTS index.

A modal overlay querying the SAME search surface everything else in the
house uses: ``doxa.operators._session_search`` -- the registry operator
whose SQL mirrors `lore search` (lore_core.store.cmd_search): BM25 over the
``msg`` FTS5 table, AND-first-then-OR widening, current project first, then
all projects. Reusing the operator (with a host-built OperatorContext, not
a model-supplied one) means the overlay, the model-facing tool, and the CLI
can never drift apart on what "history search" finds.

Search is instant-as-you-type: keystrokes are debounced
(:data:`DEBOUNCE_SECS`), then the SQLite query runs off the event loop.
Selecting a hit (Enter, or clicking a row) dismisses the overlay with the
hit; the app inserts a text reference -- session id + timestamp + snippet
-- into the prompt input, where the model can chase it with its
lore_session_search / lore_ask tooling. Read-only throughout: the overlay
serves the EXISTING index and never grows it (the same read-only contract
the operator declares).
"""

from __future__ import annotations

import asyncio

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

DEBOUNCE_SECS = 0.25
RESULT_LIMIT = 20


def search_sessions(query: str, cwd: str, limit: int = RESULT_LIMIT) -> list[dict]:
    """BM25 hits from LORE's session index, via the registry operator's own
    implementation. Each hit: {session_id, project, ts, role, snippet}.
    Errors (empty query, no index yet) come back as no hits -- an overlay
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
    return [h for h in result.get("hits") or [] if isinstance(h, dict)]


def hit_reference(hit: dict) -> str:
    """The text reference inserted into the prompt input for a chosen hit.
    Carries the full session id (so the model's lore tools can follow it)
    plus timestamp and the de-marked snippet."""
    snippet = str(hit.get("snippet", "")).replace("[", "").replace("]", "")
    sid = hit.get("session_id", "?")
    ts = str(hit.get("ts", ""))[:19]
    return f'[lore session {sid} · {ts}] "{snippet}"'


class HistorySearchScreen(ModalScreen["dict | None"]):
    """The Ctrl+R overlay. Dismisses with the chosen hit dict, or None."""

    BINDINGS = [("escape", "cancel_search", "Close")]

    def __init__(self, cwd: str) -> None:
        super().__init__()
        self.cwd = cwd
        self.hits: list[dict] = []
        self._debounce_timer = None

    def compose(self) -> ComposeResult:
        with Vertical(id="history-panel"):
            yield Static("▎ history — LORE session search", id="history-title")
            yield Input(
                placeholder="Search every past session (BM25)…",
                id="history-input",
            )
            yield OptionList(id="history-results")
            yield Static("enter: insert reference · esc: close", id="history-hint")

    def on_mount(self) -> None:
        self.query_one("#history-input", Input).focus()

    def action_cancel_search(self) -> None:
        self.dismiss(None)

    # -- debounced as-you-type query ----------------------------------

    @on(Input.Changed, "#history-input")
    def _on_query_changed(self, event: Input.Changed) -> None:
        if self._debounce_timer is not None:
            self._debounce_timer.stop()
        query = event.value
        self._debounce_timer = self.set_timer(
            DEBOUNCE_SECS, lambda: self._launch_query(query)
        )

    def _launch_query(self, query: str) -> None:
        # exclusive: a stale in-flight query never overwrites a newer one's
        # results -- the worker for the previous keystroke gets cancelled.
        self.run_worker(self._run_query(query), exclusive=True, group="history-query")

    async def _run_query(self, query: str) -> None:
        results = self.query_one("#history-results", OptionList)
        query = query.strip()
        if not query:
            self.hits = []
            results.clear_options()
            return
        hits = await asyncio.to_thread(search_sessions, query, self.cwd)
        self.hits = hits
        results.clear_options()
        for hit in hits:
            sid = str(hit.get("session_id", "?"))[:8]
            ts = str(hit.get("ts", ""))[:16]
            role = str(hit.get("role", "?"))[:4]
            snippet = str(hit.get("snippet", ""))
            results.add_option(Option(f"{sid}  {ts}  {role}: {snippet}"))
        if hits:
            results.highlighted = 0

    # -- selection ----------------------------------------------------

    @on(Input.Submitted, "#history-input")
    def _on_submit(self, event: Input.Submitted) -> None:
        """Enter in the input takes the highlighted (default: best) hit."""
        event.stop()  # never bubble to the app's prompt-submit handler
        if not self.hits:
            self.dismiss(None)
            return
        results = self.query_one("#history-results", OptionList)
        index = results.highlighted if results.highlighted is not None else 0
        self.dismiss(self.hits[index] if 0 <= index < len(self.hits) else None)

    @on(OptionList.OptionSelected, "#history-results")
    def _on_option_selected(self, event: OptionList.OptionSelected) -> None:
        index = event.option_index
        self.dismiss(self.hits[index] if 0 <= index < len(self.hits) else None)
