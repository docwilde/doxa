"""Verified Codex hook lifetime with owned fake reviews; no provider calls."""
import importlib.util
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[3]
HOOK = ROOT / 'rust/doxa-engines/codex_compact_hook.py'
SUPERVISOR = ROOT / 'doxa/review_worker.py'
spec = importlib.util.spec_from_file_location('owned_review_fixtures', ROOT / 'tests/test_review_worker.py')
owned = importlib.util.module_from_spec(spec)
spec.loader.exec_module(owned)


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux stable process handles')
@pytest.mark.parametrize('completion', ['success', 'deadline', 'owner_killed'])
def test_pinned_hook_review_descendants_end_with_owner_or_completion(tmp_path, completion):
    metadata = owned.fixture(tmp_path)
    binary = tmp_path / 'lore-rs'
    binary.write_text(binary.read_text().replace("'claude'", "'codex'"))
    env = dict(os.environ, DOXA_LORE_RS=str(binary))
    # Matches CompactGate's generated source: the supervisor is inside the
    # source bytes whose digest the trusted bootstrap verifies, not imported.
    pinned = tmp_path / 'verified-hook.py'
    pinned.write_text('REVIEW_SUPERVISOR_SOURCE = ' + repr(SUPERVISOR.read_text()) + '\n' + HOOK.read_text())
    script = '''
import json,sys
namespace={'__name__':'doxa_compact_hook'}
exec(compile(open(sys.argv[1]).read(),sys.argv[1],'exec'),namespace)
sys.exit(0 if namespace['run_worker'](json.loads(sys.argv[2]),float(sys.argv[3])) else 1)
'''
    timeout = '0.4' if completion == 'deadline' else '30'
    parent = subprocess.Popen([sys.executable, '-I', '-c', script, str(pinned), json.dumps(metadata), timeout], env=env)
    handles = []
    try:
        handles = [owned.pidfd_open(pid) for pid in owned.wait_ready(tmp_path)]
        if completion == 'success':
            (tmp_path / 'release').touch()
        elif completion == 'owner_killed':
            parent.kill()
        parent.wait(timeout=5)
        if completion != 'owner_killed':
            assert parent.returncode == (0 if completion == 'success' else 1)
        deadline = time.monotonic() + 3
        for handle in handles:
            assert select.select([handle], [], [], max(0, deadline-time.monotonic()))[0], 'owned Codex review descendant survived'
    finally:
        if parent.poll() is None:
            parent.kill(); parent.wait()
        for handle in handles:
            if not select.select([handle], [], [], 0)[0]:
                owned.kill_handle(handle)
            os.close(handle)
