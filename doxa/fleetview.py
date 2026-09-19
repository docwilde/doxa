# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.fleetview -- a fleet run as text, read from its own files.

WHAT THIS IS FOR. ``doxa.fleet`` runs a fleet; this renders one. The two
are deliberately not the same module and the split is not tidiness: the
TUI's ``/fleet`` tab watches a run it did not necessarily start, from a
widget that must never touch a :class:`doxa.fleet.FleetRun` object. A
widget that reached into the run would be reading orchestration state
from a Textual timer -- across the barrier, the teardown escalation and
the slot phases the run mutates as it goes -- which is a data race with
nothing synchronising it and a tab that keeps working after the run's
task has gone.

So the coupling is TWO FILES and nothing else:

* ``<root>/<run-id>/manifest.json`` -- what the run is and what has
  happened to it so far. :meth:`doxa.fleet.FleetRun.write_manifest` is
  callable mid-run and leaves ``finished_at`` empty while ``live`` is
  true, which is what makes this readable before the run ends.
* ``<root>/<run-id>/home/peers/messages.jsonl`` -- the run's ledger,
  whole, because a fleet run gets its own ``DOXA_HOME``
  (``doxa.peerledger``'s "one file per DOXA_HOME, which is also how a
  harness collects a run").

NO TEXTUAL, NO doxa.fleet IMPORT. Every function here takes paths and
returns strings, so the rendering is testable from a fixture directory
with no run, no daemon and no terminal -- and so that reading a run
someone else's process is writing costs nothing but two file reads.

A MISSING OR HALF-WRITTEN FILE IS A STATE, NEVER AN ERROR. The manifest
is rewritten in place while N sessions append to the ledger, so a reader
on a timer WILL catch both mid-write. Every read here degrades to "not
yet" rather than raising: the tab's job is to keep saying what it knows
while a run happens, and a traceback in a watcher is the one outcome that
loses the run's own output as well as the watcher's.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "BODY_WIDTH",
    "LEDGER_TAIL",
    "RunSnapshot",
    "assignment_table",
    "ledger_tail",
    "list_runs",
    "mode_line",
    "read_manifest",
    "render",
    "resolve_run",
    "run_ledger_path",
]

#: How many ledger lines the tab shows. Thirty is what fits under the
#: assignment table on an ordinary terminal without the table scrolling
#: off -- the table is the run's shape and the tail is its traffic, and a
#: tail that pushes the shape off screen has the priority backwards. The
#: whole ledger is on disk either way, and ``/mesh`` draws all of it.
LEDGER_TAIL = 30

#: Ledger bodies are full and unbounded by design (the emergence plan
#: needs the content, untruncated, because the content is the
#: measurement). This truncates the DISPLAY only, to keep one message to
#: one line.
BODY_WIDTH = 60

MANIFEST_NAME = "manifest.json"

#: Where a run's ledger sits under its own run root. Spelled out rather
#: than imported from ``doxa.peerledger`` so this module stays free of
#: every import that one drags in; ``doxa.fleet.FleetSpec.ledger_path``
#: is the authority and derives the same path from its constants.
LEDGER_RELATIVE = ("home", "peers", "messages.jsonl")


# -- reading a run ---------------------------------------------------------


def run_ledger_path(run_root: "Path | str") -> Path:
    return Path(run_root).joinpath(*LEDGER_RELATIVE)


def read_manifest(run_root: "Path | str") -> "dict[str, Any] | None":
    """The run's manifest, or None while there is not a readable one.

    None covers three states that are all "not yet" and none of which is
    a failure: the run root does not exist, the manifest has not been
    written (the run is still in :meth:`doxa.fleet.FleetRun.prepare`), or
    the file is mid-rewrite and holds half a JSON document. A reader on a
    250 ms timer meets the third one routinely."""
    path = Path(run_root) / MANIFEST_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_ledger(path: "Path | str", limit: int = LEDGER_TAIL) -> "list[dict[str, Any]]":
    """The last ``limit`` usable records, oldest first.

    Reads the whole file rather than seeking a tail: a run's ledger is
    bounded by what N agents said to each other, the largest measured run
    wrote 128 records, and a seek-and-resynchronise would be a second
    parser for a format that already has one. A line that does not parse
    is skipped -- the file is being appended to by N other processes and
    the last line is regularly half of one."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: "list[dict[str, Any]]" = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out[-limit:] if limit else out


class RunSnapshot:
    """One read of one run: its manifest, its ledger tail, and where both
    came from. A value, taken once per refresh, so every line the tab
    paints describes the same instant."""

    def __init__(
        self,
        run_root: "Path | str",
        manifest: "dict[str, Any] | None",
        ledger: "list[dict[str, Any]]",
    ) -> None:
        self.run_root = Path(run_root)
        self.manifest = manifest or {}
        self.ledger = ledger

    @classmethod
    def read(cls, run_root: "Path | str", limit: int = LEDGER_TAIL) -> "RunSnapshot":
        run_root = Path(run_root)
        return cls(
            run_root,
            read_manifest(run_root),
            read_ledger(run_ledger_path(run_root), limit),
        )

    @property
    def run_id(self) -> str:
        return str(self.manifest.get("run_id") or self.run_root.name)

    @property
    def live(self) -> bool:
        return bool(self.manifest.get("live"))

    @property
    def slots(self) -> "list[dict[str, Any]]":
        rows = self.manifest.get("slots")
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    @property
    def manifest_path(self) -> Path:
        return self.run_root / MANIFEST_NAME

    def slot(self, index: int) -> "dict[str, Any] | None":
        for row in self.slots:
            if row.get("index") == index:
                return row
        return None


# -- rendering -------------------------------------------------------------


def _iso_epoch(stamp: str) -> "float | None":
    """A manifest/ledger timestamp as epoch seconds, or None.

    Both writers emit UTC with a trailing ``Z``; the manifest's is second
    resolution (``_iso_now``) and the ledger's carries microseconds.
    ``fromisoformat`` handles both on 3.11+ once the ``Z`` is spelled the
    way it expects."""
    text = str(stamp or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _short(session_id: Any, width: int = 8) -> str:
    return str(session_id or "?")[:width]


def _ago(seconds: "float | None") -> str:
    if seconds is None:
        return "?"
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def _role_of(row: "dict[str, Any]") -> str:
    """This slot's role, from the manifest, defaulting to ``worker``.

    Defaulted rather than shown as ``?`` because a manifest written
    before roles existed describes a symmetric run, and every slot of a
    symmetric run IS a worker -- reading an old run should not paint a
    column of question marks over a fact that is known."""
    assignment = row.get("assignment")
    assignment = assignment if isinstance(assignment, dict) else {}
    return str(row.get("role") or assignment.get("role") or "worker")


def assignment_table(snapshot: "RunSnapshot") -> "list[str]":
    """slot, role, engine, model, memory, phase, session, error -- one row
    per participant.

    Every column is a fact the run is uninterpretable without: "slot 7
    coordinated" says nothing until this table says what slot 7 was
    running and whether it had memory. ROLE joins them for the same
    reason in a supervisor run, where the supervisor's transcript is the
    only one that ever saw the operator's prompt -- a reader who cannot
    tell which row that was is reading four workers' silence as failure.
    Fixed columns rather than a joined string, for the same reason every
    picker in this app uses fixed columns: a reader scans one column
    down, not a sentence across."""
    rows = sorted(snapshot.slots, key=lambda r: int(r.get("index") or 0))
    if not rows:
        return ["  (no assignment yet)"]
    lines = [
        "  slot  role        engine      model           mem  phase       "
        "session   error",
    ]
    for row in rows:
        assignment = row.get("assignment") if isinstance(row.get("assignment"), dict) else {}
        engine = str(assignment.get("engine") or "?")
        model = str(assignment.get("model") or "-")
        memory = "on" if assignment.get("lore", True) else "OFF"
        phase = str(row.get("phase") or "?")
        session = _short(row.get("session_id")) if row.get("session_id") else "-"
        error = str(row.get("error") or "")
        lines.append(
            f"  {int(row.get('index') or 0):>4}  {_role_of(row):<10.10}  "
            f"{engine:<10.10}  {model:<14.14}  "
            f"{memory:<3}  {phase:<10.10}  {session:<8}  {error[:52]}"
        )
    return lines


def mode_line(snapshot: "RunSnapshot") -> str:
    """Which of the two shapes this run is, in one line.

    ABOVE the assignment table rather than below it, because it changes
    how the table reads: in a supervisor run one row is the session that
    received the operator's words and every other row is a session that
    did not, and a reader who learns that afterwards has already
    misread. A manifest with no ``mode`` key predates the modes and is a
    symmetric run -- stated, not guessed at, because that is what those
    runs were.

    The interactive case gets its own sentence, since the thing an
    operator most needs to know about such a run is that nothing will
    happen until they attach and type."""
    manifest = snapshot.manifest
    mode = str(manifest.get("mode") or "symmetric")
    if mode != "supervisor":
        return (
            "mode symmetric — every session got the identical prompt at one "
            "instant; no session directs another"
        )
    supervisor = manifest.get("supervisor")
    supervisor = supervisor if isinstance(supervisor, dict) else {}
    slot = supervisor.get("slot")
    who = _short(supervisor.get("session_id")) if supervisor.get("session_id") else "-"
    label = str(supervisor.get("engine") or "?")
    if supervisor.get("model"):
        label = f"{label}:{supervisor['model']}"
    spec = manifest.get("spec") if isinstance(manifest.get("spec"), dict) else {}
    line = (
        f"mode supervisor — slot {slot if slot is not None else '?'} "
        f"({label}, {who}) holds the prompt and hands work to "
        f"{spec.get('n', '?')} worker(s) over peer messages"
    )
    if manifest.get("interactive"):
        line += (
            "\ninteractive — no prompt was given: attach to the supervisor "
            "(`/fleet attach 0`) and type the task. This run does NOT end on "
            "quiet; `/fleet stop` ends it."
        )
    return line


def ledger_tail(
    snapshot: "RunSnapshot", *, limit: int = LEDGER_TAIL
) -> "list[str]":
    """``t+s  from -> to  body`` for the last messages the run wrote.

    ``t+`` is seconds since the run STARTED rather than a wall clock: the
    question a reader has about a fleet's traffic is when it happened
    relative to the one prompt everybody got, and two absolute timestamps
    make that a subtraction the reader has to do. A record whose stamp
    cannot be read against the run's own keeps its raw stamp rather than
    being dropped -- it is still traffic."""
    records = snapshot.ledger[-limit:] if limit else snapshot.ledger
    if not records:
        return ["  (no messages yet)"]
    base = _iso_epoch(str(snapshot.manifest.get("started_at") or ""))
    lines: "list[str]" = []
    for record in records:
        when = _iso_epoch(str(record.get("ts") or ""))
        if base is not None and when is not None:
            stamp = f"t+{max(0.0, when - base):>6.1f}s"
        else:
            stamp = f"{str(record.get('ts') or '?')[:15]:>9}"
        sender = record.get("from")
        who = _short(sender.get("session")) if isinstance(sender, dict) else "?"
        targets = record.get("to")
        targets = [t for t in targets if isinstance(t, str)] if isinstance(targets, list) else []
        if str(record.get("kind")) == "broadcast":
            to = f"all({len(targets)})"
        else:
            to = ",".join(_short(t) for t in targets) or "?"
        body = " ".join(str(record.get("body") or "").split())
        lines.append(f"  {stamp}  {who} → {to:<11.11}  {body[:BODY_WIDTH]}")
    return lines


def render(
    snapshot: "RunSnapshot",
    *,
    now: "float | None" = None,
    mesh_url: str = "",
    detached: bool = False,
    note: str = "",
) -> str:
    """The whole tab, as text.

    ``note`` is the refusal path: :func:`doxa.fleet.check_socket_budget`,
    :class:`doxa.fleet.CapacityRefused` and
    :class:`doxa.fleet.BudgetRefused` all fire BEFORE a manifest exists,
    so the tab's FIRST LINE is where that lands. A refusal that reached
    the user as a traceback in a worker would be a run that did not start
    for a reason nobody was told."""
    now = time.time() if now is None else now
    manifest = snapshot.manifest
    spec = manifest.get("spec") if isinstance(manifest.get("spec"), dict) else {}
    lines: "list[str]" = []

    if note:
        lines += [note, ""]

    state = (
        "running" if snapshot.live
        else ("finished" if manifest else "starting")
    )
    lines.append(f"fleet {snapshot.run_id} — {state}")
    if snapshot.live:
        lines.append(
            "closing this tab tears the run down. `/fleet detach` first to "
            "leave it running; a fleet is not a background service."
            if not detached else
            "detached: closing this tab leaves the run going. `/fleet stop` "
            "ends it."
        )
    lines.append("")

    if not manifest:
        lines.append(
            "no manifest yet — the run is preparing (capacity, budget, "
            "directories). This tab re-reads "
            f"{snapshot.manifest_path} as soon as there is one."
        )
        return "\n".join(lines)

    # -- what the run was allowed to be --------------------------------
    if manifest.get("capacity"):
        lines.append(str(manifest["capacity"]))
    if manifest.get("budget"):
        lines.append(str(manifest["budget"]))
    if manifest.get("forced"):
        lines.append("capacity arithmetic OVERRIDDEN (--force)")
    if manifest.get("unbudgeted"):
        lines.append("run accepted with NO spend ceiling (--allow-unbudgeted)")
    lines.append(
        f"cwd {spec.get('cwd', '?')}  ·  seed {spec.get('seed', '?')}  ·  "
        f"memory off on {spec.get('memory_off', 0)} of {spec.get('n', '?')}"
    )
    lines.append(mode_line(snapshot))
    lines.append("")

    # -- who was dealt what --------------------------------------------
    lines += assignment_table(snapshot)
    lines.append("")

    # -- the symmetric start, measured ---------------------------------
    spread = manifest.get("dispatch_spread_s")
    if isinstance(spread, (int, float)):
        lines.append(
            f"dispatch spread {float(spread) * 1000:.0f} ms across "
            f"{len(manifest.get('dispatch_order') or [])} sessions"
        )
    else:
        lines.append("dispatch spread — not dispatched yet")

    # -- quiescence, with the clock ------------------------------------
    started = _iso_epoch(str(manifest.get("started_at") or ""))
    elapsed = (now - started) if started is not None else None
    quiescence_s = manifest.get("quiescence_s")
    if manifest.get("stopped"):
        stop_state = "ENDED on request (/fleet stop)"
    elif manifest.get("quiesced"):
        stop_state = "quiesced"
    elif snapshot.live:
        stop_state = "waiting for quiet"
    else:
        stop_state = "NOT quiesced — the deadline ran out"
    detail = (
        f" after {float(quiescence_s):.0f}s"
        if isinstance(quiescence_s, (int, float)) else ""
    )
    lines.append(f"quiescence: {stop_state}{detail}  ·  t+{_ago(elapsed)}")
    lines.append("")

    # -- the traffic ---------------------------------------------------
    # The manifest's own count is written by collect(), which runs once,
    # at the end. While the run is LIVE there is no total to name -- and
    # naming the tail's own length as if it were one would tell a reader
    # the traffic had stopped growing. So a live run counts what it shows
    # and says nothing it cannot know yet.
    ledger_obj = manifest.get("ledger")
    total = ledger_obj.get("messages") if isinstance(ledger_obj, dict) else None
    shown = len(snapshot.ledger)
    if snapshot.live or not isinstance(total, int):
        lines.append(f"ledger — last {shown} message(s)")
    else:
        lines.append(f"ledger — last {shown} of {total} message(s)")
    lines += ledger_tail(snapshot)
    lines.append("")

    # -- what a run must always be able to say about itself ------------
    leaked = manifest.get("leaked_pids")
    leaked = [p for p in leaked if isinstance(p, int)] if isinstance(leaked, list) else []
    lines.append(
        "leaked pids: none — teardown left nothing running" if not leaked
        else "LEAKED " + str(len(leaked)) + " process(es) teardown could not "
             "kill: " + ", ".join(str(p) for p in leaked)
    )
    if mesh_url:
        lines.append(f"mesh     {mesh_url}")
    lines.append(f"manifest {snapshot.manifest_path}")
    lines.append(f"ledger   {run_ledger_path(snapshot.run_root)}")
    return "\n".join(lines)


# -- the runs under a root -------------------------------------------------


def list_runs(root: "Path | str") -> "list[dict[str, Any]]":
    """Every run directory under ``root`` that has a manifest, newest
    first.

    Read from the manifests rather than from any index: a run root is
    whatever directories the runs made, there is no registry of them, and
    inventing one would be a second source of truth for a fact the
    directory already holds. A directory without a readable manifest is
    skipped -- a run that died in prepare() left no interpretation."""
    root = Path(root)
    out: "list[dict[str, Any]]" = []
    try:
        children = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return []
    for child in children:
        manifest = read_manifest(child)
        if manifest is None:
            continue
        spec = manifest.get("spec") if isinstance(manifest.get("spec"), dict) else {}
        ledger = manifest.get("ledger") if isinstance(manifest.get("ledger"), dict) else {}
        out.append({
            "run_id": str(manifest.get("run_id") or child.name),
            "root": child,
            "started_at": str(manifest.get("started_at") or ""),
            "n": spec.get("n"),
            "quiesced": bool(manifest.get("quiesced")),
            "stopped": bool(manifest.get("stopped")),
            "live": bool(manifest.get("live")),
            "messages": ledger.get("messages"),
        })
    out.sort(key=lambda row: row["started_at"], reverse=True)
    return out


def runs_table(rows: "list[dict[str, Any]]") -> str:
    if not rows:
        return "no runs recorded under this root"
    lines = ["  run                     started               n  state      ledger"]
    for row in rows:
        state = (
            "LIVE" if row["live"]
            else ("stopped" if row["stopped"]
                  else ("quiesced" if row["quiesced"] else "timed out"))
        )
        messages = row["messages"]
        lines.append(
            f"  {row['run_id']:<22.22}  {row['started_at'] or '?':<19.19}  "
            f"{str(row['n'] or '?'):>3}  {state:<9}  "
            f"{messages if isinstance(messages, int) else '?'}"
        )
    return "\n".join(lines)


def resolve_run(root: "Path | str", run_id: str) -> "Path | None":
    """A run directory from an id or an unambiguous prefix, or None.

    Prefix matching because a run id is ``20260918T104355-3f2a`` -- a
    stamp nobody retypes. An AMBIGUOUS prefix returns None rather than the
    first match: two runs started in the same second are exactly the case
    where guessing shows the wrong run's ledger."""
    root = Path(root)
    run_id = str(run_id or "").strip()
    if not run_id:
        return None
    exact = root / run_id
    if (exact / MANIFEST_NAME).exists() or exact.is_dir():
        return exact
    try:
        hits = [p for p in root.iterdir() if p.is_dir() and p.name.startswith(run_id)]
    except OSError:
        return None
    return hits[0] if len(hits) == 1 else None
