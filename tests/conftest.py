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

# Review kill switch: SessionEngine.finalize()/PreCompact run the LORE
# deriver review, whose worker shells out to a headless `claude -p` when a
# transcript is long enough to build a job. The daemon tests finalize real
# engines after real (fake-client) turns -- no test run may ever spend
# tokens or depend on a `claude` binary, so the automatic-review stage is
# disabled suite-wide (stage_disabled("review") honors this, same as the
# LORE plugin's own hook path).
os.environ["LORE_DISABLE_REVIEW"] = "1"
