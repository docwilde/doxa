# SPDX-License-Identifier: AGPL-3.0-only
"""No agent subprocess outlives the test that spawned it.

The defect this pins, measured on 2026-09-02 while diagnosing a
"different single test fails every full run" report on `feat/sidebar`:

`DoxaApp(...)` built without an `engine_factory` gets the real one -- an
in-process `SessionEngine`, which connects a `claude_agent_sdk` client
and SPAWNS the bundled `claude` CLI. About thirty tests in this suite
build an app that way, and nothing closes those clients:
`SessionEngine.finalize()` is the only caller of `_client.__aexit__`, and
`run_test()` tears an app down without ending its sessions. Every one of
those children therefore survived its test and was inherited by pytest
for the rest of the run -- 32 live `claude` processes by the 60% mark of
a full run, ~294 MB RSS and ~1.5% of a core each.

That is a load generator wearing a test suite's clothes, and it grows
monotonically: it is heaviest in the last quarter, which is exactly where
this suite's timing-marginal tests live and where every reported victim
sat (83%, 92%, 83% of the run). `tests/conftest.py`'s
`_no_agent_subprocess_outlives_its_test` is the floor that ends it.

Tested against a STAND-IN rather than the real CLI on purpose: a test
that had to spawn a genuine `claude` would depend on a binary and an
authenticated account, which is the one thing conftest.py's whole
preamble exists to prevent. What is under test is the reaper -- that it
recognises a child by the shape of its command line and that the child is
gone, and reaped, afterwards.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from tests.conftest import _agent_children, _reap


def _stand_in(tmp_path: Path) -> "subprocess.Popen":
    """A long-lived child whose argv looks like the SDK's bundled CLI.

    The reaper matches on `_bundled/claude` plus `stream-json` in the
    command line, so the stand-in has to carry both -- which is also the
    assertion that the matcher is narrow enough not to catch a test's own
    `git` or pty subprocess."""
    bundled = tmp_path / "_bundled"
    bundled.mkdir()
    fake = bundled / "claude"
    fake.write_text("#!/bin/sh\nsleep 300\n")
    fake.chmod(0o755)
    return subprocess.Popen([str(fake), "--output-format", "stream-json"])


def test_the_reaper_finds_an_agent_child_by_its_command_line(tmp_path):
    child = _stand_in(tmp_path)
    try:
        # /proc/<pid>/children is written by the kernel, not synchronously
        # with fork(), so give it a moment rather than asserting into a
        # race -- this is the ONE wait in this file and it is on the
        # kernel publishing a fact, not on the app reaching a state.
        found = set()
        for _ in range(100):
            found = _agent_children()
            if child.pid in found:
                break
            time.sleep(0.02)
        assert child.pid in found, (
            "the reaper cannot see a bundled-claude child, so it would "
            "never have reaped one either"
        )
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_a_reaped_child_is_gone_and_waited_for(tmp_path):
    """Ended AND reaped: a killed child nobody waits for is the same leak
    at a smaller price -- a zombie still holds a process table slot."""
    child = _stand_in(tmp_path)
    for _ in range(100):
        if child.pid in _agent_children():
            break
        time.sleep(0.02)

    _reap({child.pid})

    assert child.pid not in _agent_children()
    # Reaped, not merely signalled: waitpid on an already-collected child
    # raises ChildProcessError, which is what proves _reap collected it.
    try:
        os.waitpid(child.pid, os.WNOHANG)
    except ChildProcessError:
        pass
    else:  # pragma: no cover -- only on a regression
        raise AssertionError(
            "the child was signalled but never waited for; it is a zombie"
        )


def test_a_test_that_spawns_nothing_is_charged_nothing(tmp_path):
    """The reaper never touches a process it did not see appear."""
    before = _agent_children()
    _reap(_agent_children() - before)
    assert _agent_children() == before
