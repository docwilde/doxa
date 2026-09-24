# SPDX-License-Identifier: AGPL-3.0-only
"""CLI help resolves linger without loading the daemon or agent SDK."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("configured", "expected"),
    [(None, 120.0), ("9.25", 9.25)],
)
def test_cli_linger_default_is_lightweight_and_configurable(
    tmp_path, configured, expected
):
    """Use a fresh interpreter so prior test imports cannot mask regressions."""
    env = os.environ.copy()
    env["DOXA_HOME"] = str(tmp_path / "doxa-home")
    env["DOXA_RUNTIME_DIR"] = str(tmp_path / "runtime")
    if configured is None:
        env.pop("DOXA_LINGER_SECS", None)
    else:
        env["DOXA_LINGER_SECS"] = configured
    repo = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(repo)
    code = """
import contextlib, io, json, sys
from doxa import cli, config
try:
    with contextlib.redirect_stdout(io.StringIO()) as output:
        cli.main(['--help'])
except SystemExit as exc:
    assert exc.code == 0
print(json.dumps({
    'linger': config.linger_secs(),
    'help': output.getvalue(),
    'heavy_imports': [name for name in (
        'doxa.daemon', 'doxa.engine', 'claude_agent_sdk',
    ) if name in sys.modules],
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, cwd=repo,
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    assert data["linger"] == expected
    assert f"default {expected}" in data["help"]
    assert data["heavy_imports"] == []
