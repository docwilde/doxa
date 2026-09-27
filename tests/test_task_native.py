# SPDX-License-Identifier: AGPL-3.0-only
"""Task launches use this checkout's native carrier, without real Cargo/providers."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def fixture(tmp_path):
    source = tmp_path / "source with spaces"
    source.mkdir()
    shutil.copyfile(ROOT / "task", source / "task")
    for name in ("rust/doxa-tui/Cargo.toml", "rust/doxa-daemon/Cargo.toml", "rust/doxa-claude/claude_sidecar.py"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# owned fixture\n")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    cargo = fakebin / "cargo"
    cargo.write_text("""#!/usr/bin/python3
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['CAPTURE'], 'a') as out: out.write(json.dumps(args) + '\\n')
if '--bin' in args:
    binary = args[args.index('--bin') + 1]
    if binary == 'lore-rs' and os.environ.get('FAIL_NATIVE') == '1': sys.exit(71)
    target = pathlib.Path(os.environ['CARGO_TARGET_DIR']) / ('release' if '--release' in args else 'debug')
    target.mkdir(parents=True, exist_ok=True)
    path = target / binary
    path.write_text('''#!/usr/bin/python3
import json, os, sys
print(json.dumps({'args': sys.argv[1:], 'lore': os.environ.get('DOXA_LORE_RS'), 'daemon': os.environ.get('DOXA_DAEMON_BIN')}))
''')
    path.chmod(0o700)
""")
    cargo.chmod(0o700)
    python = fakebin / "owned-python"
    python.write_text("#!/bin/sh\nprintf '%s\\n' \"$DOXA_LORE_RS\" > \"$PYTHON_CAPTURE\"\n")
    python.chmod(0o700)
    env = dict(os.environ, PATH=str(fakebin) + ":/usr/bin:/bin", DOXA_TASK_TARGET_DIR=str(tmp_path / "target"),
               DOXA_LORE_PYTHON=str(python), DOXA_LORE_RS="/wrong-installed-carrier",
               CAPTURE=str(tmp_path / "cargo.jsonl"), PYTHON_CAPTURE=str(tmp_path / "python.txt"))
    return source, env


@pytest.mark.parametrize("profile", ["debug", "release"])
@pytest.mark.parametrize("verb", ["run", "new", "doctor"])
def test_task_builds_and_passes_exact_native_carrier(tmp_path, profile, verb):
    source, env = fixture(tmp_path)
    env['DOXA_TASK_PROFILE'] = profile
    result = subprocess.run(["sh", str(source / "task"), verb, "--engine", "claude"], env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    reply = json.loads(result.stdout)
    native = Path(env['DOXA_TASK_TARGET_DIR']) / profile / 'lore-rs'
    assert reply['lore'] == str(native) and os.access(native, os.X_OK)
    assert reply['daemon'] == str(native.with_name('doxa-daemon'))
    builds = [json.loads(line) for line in Path(env['CAPTURE']).read_text().splitlines()]
    assert len(builds) == 3
    native_build = builds[-1]
    expected_prefix = ['build', '--release'] if profile == 'release' else ['build', '--locked']
    assert native_build[:2] == expected_prefix
    assert '--locked' in native_build
    assert native_build[-4:] == ['--package', 'lore-core', '--bin', 'lore-rs']
    assert '--claude-python' in reply['args'] and '--lore-python' in reply['args']
    assert reply['args'][0] == (verb if verb != 'run' else '--engine')


def test_task_native_build_failure_never_launches_frontend(tmp_path):
    source, env = fixture(tmp_path)
    env['FAIL_NATIVE'] = '1'
    result = subprocess.run(['sh', str(source / 'task'), 'run', '--engine', 'claude'], env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 71 and not result.stdout


def test_task_tests_build_carrier_before_crates_and_export_to_python(tmp_path):
    source, env = fixture(tmp_path)
    result = subprocess.run(['sh', str(source / 'task'), 'test'], env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in Path(env['CAPTURE']).read_text().splitlines()]
    assert calls[0][-4:] == ['--package', 'lore-core', '--bin', 'lore-rs']
    assert all(call[0] == 'test' for call in calls[1:])
    assert Path(env['PYTHON_CAPTURE']).read_text().strip() == str(Path(env['DOXA_TASK_TARGET_DIR']) / 'debug/lore-rs')
