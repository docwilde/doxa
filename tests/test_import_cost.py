# SPDX-License-Identifier: AGPL-3.0-only
"""Importing DOXA must not import the Claude Agent SDK.

Measured on 2026-08-25, before this release: ``import doxa.app`` took
546 ms, of which ``claude_agent_sdk`` was 404 ms and ``mcp.types`` alone
was 330 ms building pydantic models. DOXA's own modules were 7% of it.

That cost is not a parse, so bytecode caching never touched it, and it was
paid by every launch before the first frame -- including ``doxa doctor``,
``doxa launcher install``, ``doxa --version`` and any TUI attached to a
daemon, none of which construct a SessionEngine at all.

A regression here is invisible: someone adds ``from .engine import X`` to
a module the TUI imports, everything still works, and startup silently
gets 400 ms slower again. So the invariant is a test, and it runs in a
subprocess because ``sys.modules`` in this one is already populated by the
rest of the suite.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

PROBE = (
    "import sys, {module};"
    "print('YES' if 'claude_agent_sdk' in sys.modules else 'NO')"
)


def _sdk_loaded_by(module: str) -> bool:
    out = subprocess.run(
        [sys.executable, "-c", PROBE.format(module=module)],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip() == "YES"


@pytest.mark.parametrize("module", ["doxa.app", "doxa.cli", "doxa.client"])
def test_the_sdk_is_not_imported_to_show_a_terminal(module):
    """The three modules every launch touches. ``doxa.client`` is the
    sharpest of them: it talks to a daemon over a socket and never builds
    an engine, so it has no reason to know the SDK exists."""
    assert not _sdk_loaded_by(module), (
        f"{module} imports claude_agent_sdk at module scope again -- that is "
        "404 ms before the first frame. Move the import to the point of use."
    )


def test_the_daemon_does_import_it():
    """The other half, so the test above cannot be satisfied by breaking
    the engine: the process that actually runs a session loads the SDK
    eagerly, where the cost buys something."""
    assert _sdk_loaded_by("doxa.daemon")


def test_events_carries_no_engine_machinery():
    """doxa.events exists to be cheap. If it ever grows an engine import
    the split has quietly undone itself."""
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys, doxa.events;"
         "print('YES' if 'doxa.engine' in sys.modules else 'NO')"],
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == "NO"


def test_no_call_site_uses_the_bare_lazy_name():
    """The v0.61.0 regression, and the reason the suite missed it.

    Deferring ``spawn_daemon`` left three call sites still naming it
    bare -- inside the closures ``Ctrl+T`` and the palette's "new session"
    call. In production that is a ``NameError`` at the moment a user opens
    a tab. The suite passed anyway, because every test that reaches those
    closures does ``monkeypatch.setattr(cli_mod, "spawn_daemon", ...)``:
    the patch CREATES the module global the bare name needs, so the tests
    were the only thing making the code work.

    A source check rather than a behavioural one, deliberately. Any test
    that exercised the path would have to either patch the name (and
    reintroduce the blind spot) or really fork a daemon.
    """
    import pathlib
    import re

    import doxa.cli

    source = pathlib.Path(doxa.cli.__file__).read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in source.splitlines()
        # A call, not a mention: skip docstrings and comments.
        if re.search(r"(?<![_\w])spawn_daemon\(", line)
        and not line.lstrip().startswith(("#", '"', "'"))
        and "getattr(" not in line
    ]
    assert offenders == [], (
        "these call the lazily-imported name directly, which is a NameError "
        f"outside a test that patches it in: {offenders}"
    )
