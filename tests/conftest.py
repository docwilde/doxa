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
