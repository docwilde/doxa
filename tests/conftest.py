"""Test isolation: LORE_ROOT / LORE_PROJECTS_DIR must be pointed at a
throwaway directory BEFORE lore_core (or anything that imports it, i.e.
doxa.engine/doxa.app) is imported anywhere in this process -- config.py's
ROOT/PROJECTS_DIR constants are read from the environment at import time,
not per call. conftest.py is always collected before test modules, so this
is the one place that ordering is guaranteed.

Without this, the test suite would read from and write into the real
~/.claude/lore belief store / session index on whatever machine runs it.
"""

import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="doxa-test-lore-"))
os.environ["LORE_ROOT"] = str(_tmp / "lore")
os.environ["LORE_PROJECTS_DIR"] = str(_tmp / "projects")

# Peer layer isolation: doxa.peers reads DOXA_RUNTIME_DIR per call (not at
# import), but setting it here too guarantees no test -- including ones
# that start a real SessionEngine without thinking about peers -- ever
# registers in, reaps, or listens inside the machine's real registry.
os.environ["DOXA_RUNTIME_DIR"] = str(_tmp / "runtime")

# Image-mode kill switch: doxa.images probes the terminal for KGP/sixel
# support by writing escape queries to the REAL stdout and reading stdin --
# harmless headless (non-tty short-circuits) but rude and slow when the
# suite runs from an interactive terminal. Forcing the text tier suite-wide
# means no test ever probes; tests that exercise other tiers set
# DOXA_IMAGE_MODE themselves via monkeypatch.
os.environ["DOXA_IMAGE_MODE"] = "text"

# Keyboard-protocol kill switch, and the same reasoning one line up:
# doxa.keyboard asks the terminal whether it grants the kitty keyboard
# protocol by writing escape queries and reading the reply off stdin in raw
# mode. Headless that short-circuits before writing a byte, but a suite run
# with capture off (`pytest -s`) from a real terminal would put every worker
# into raw mode for a third of a second. "unknown" is the honest headless
# answer anyway; tests that exercise the kitty/legacy tiers set
# DOXA_KEYBOARD_PROTOCOL themselves via monkeypatch, and the ones that
# exercise the PROBE drive its seams directly.
os.environ["DOXA_KEYBOARD_PROTOCOL"] = "unknown"

# Identity isolation: doxa.identity reads the Claude Code CLI's own global
# config for the PRECISE plan tier (organizationRateLimitTier), resolving
# its path the way the CLI does -- CLAUDE_CONFIG_DIR first, home directory
# otherwise. Pointing it at the throwaway directory means no test ever
# reads the developer's real account block (and every test therefore sees
# the "no local precision" fallback path unless it writes a config itself).
os.environ["CLAUDE_CONFIG_DIR"] = str(_tmp / "claude-config")
Path(os.environ["CLAUDE_CONFIG_DIR"]).mkdir(parents=True, exist_ok=True)

# Review kill switch: SessionEngine.finalize()/PreCompact run the LORE
# deriver review, whose worker shells out to a headless `claude -p` when a
# transcript is long enough to build a job. The daemon tests finalize real
# engines after real (fake-client) turns -- no test run may ever spend
# tokens or depend on a `claude` binary, so the automatic-review stage is
# disabled suite-wide (stage_disabled("review") honors this, same as the
# LORE plugin's own hook path).
os.environ["LORE_DISABLE_REVIEW"] = "1"

# DOXA state-home isolation: doxa.config writes ~/.doxa/config.toml by
# default (DOXA_HOME overrides). No test may read or write the developer's
# real settings -- and the legacy XDG path is redirected too, so the
# one-shot migration cannot find a real file to move either.
os.environ["DOXA_HOME"] = str(_tmp / "doxa-home")
os.environ["XDG_CONFIG_HOME"] = str(_tmp / "xdg")

# Setup-wizard kill switch: /setup auto-runs once, on a GENUINE first
# launch (doxa.setup.needs_first_run -- no ~/.doxa/.setup-done marker
# yet). Plenty of tests point DOXA_HOME at their OWN fresh tmp_path for
# isolation reasons that have nothing to do with "has doxa ever run on
# this machine" (test_settings.py gives every test a new one), so a
# marker file alone cannot keep the auto-popup out of the suite -- an env
# var doxa.setup.needs_first_run honors explicitly can. Tests that
# exercise the auto-trigger itself clear this var first (test_setup.py).
os.environ["DOXA_SKIP_FIRST_RUN"] = "1"


# -- v0.53.0: the suite must not become a place errors hide -----------
#
# The error surface makes a caught exception SURVIVABLE, and a surface
# that turns a crash into a quiet block is one keystroke away from being a
# surface that turns a crash into a quietly passing test. Before v0.53.0
# an unhandled exception failed the test that provoked it, because Textual
# re-raises App._exception at run_test teardown; now a recoverable failure
# deliberately does not set that, so nothing would notice.
#
# So every failure reported through DoxaApp.report_failure -- the single
# door -- is collected per test, and a test that ends with unclaimed ones
# FAILS. A test that means to provoke a failure opts in by setting
# ``EXPECTS_FAILURES = True`` at module level and asserting on the block
# itself, which is what tests/test_errors.py does. Opting in is per module
# and by flag rather than by pytest marker so that no marker registration
# (and therefore no pyproject change, and therefore no unregistered-marker
# warning) rides along with it.

import pytest


@pytest.fixture(autouse=True)
def _errors_must_be_claimed(request):
    """Fail any test that quietly produced an error block."""
    from doxa.app import DoxaApp

    seen: list = []
    original = DoxaApp.report_failure

    def _record(self, failure):
        seen.append(failure)
        return original(self, failure)

    DoxaApp.report_failure = _record  # type: ignore[method-assign]
    try:
        yield seen
    finally:
        DoxaApp.report_failure = original  # type: ignore[method-assign]
    if seen and not getattr(request.module, "EXPECTS_FAILURES", False):
        raise AssertionError(
            "this test produced "
            + ", ".join(sorted({f.headline() for f in seen}))
            + " — a test that triggers an error block must assert it, not "
            "tolerate it (set EXPECTS_FAILURES = True in the module and "
            "assert on the block)"
        )
