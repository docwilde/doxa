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
