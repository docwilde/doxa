"""Shared capacity settings and exact private counts; no real memory content."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from doxa import _lore_bootstrap

KEYS = ('LORE_MEMORY_CAP', 'LORE_USER_CAP', 'LORE_MACHINE_CAP')


def configure(tmp_path, monkeypatch, value):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'claude'))
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / 'claude/settings.json'
    path.parent.mkdir()
    path.write_text(json.dumps(value))
    return path


def test_shared_capacity_settings_are_allowlisted_and_explicit_environment_wins(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch, {'env': {
        'LORE_MEMORY_CAP':'17600', 'LORE_USER_CAP':'12000', 'LORE_MACHINE_CAP':'6000',
        'ANTHROPIC_API_KEY':'fixture secret', 'LORE_ROOT':'/unrequested-store',
        'LORE_DISABLE_REVIEW':'0', 'DOXA_LORE':'0'}, 'hooks':{'SessionStart':'never run'}})
    monkeypatch.setenv('LORE_USER_CAP', '15000')
    before = dict(os.environ)
    _lore_bootstrap.export_shared_memory_caps()
    assert {key:os.environ[key] for key in KEYS} == {
        'LORE_MEMORY_CAP':'17600', 'LORE_USER_CAP':'15000', 'LORE_MACHINE_CAP':'6000'}
    assert {key:value for key,value in os.environ.items() if key not in KEYS} == {
        key:value for key,value in before.items() if key not in KEYS}


@pytest.mark.parametrize('invalid', [0, True, None, '-5', '0', '1.5', 'x', '1048577', '9'*50])
def test_invalid_configured_capacity_is_not_exported(tmp_path, monkeypatch, invalid):
    configure(tmp_path, monkeypatch, {'env':{'LORE_MEMORY_CAP':invalid}})
    _lore_bootstrap.export_shared_memory_caps()
    assert all(key not in os.environ for key in KEYS)


@pytest.mark.parametrize('unsafe', ['symlink', 'fifo', 'oversized'])
def test_capacity_reader_refuses_unsafe_or_unbounded_settings(tmp_path, monkeypatch, unsafe):
    path = configure(tmp_path, monkeypatch, {'env':{'LORE_MEMORY_CAP':'17600'}})
    if unsafe == 'symlink':
        target=tmp_path/'other.json'; path.rename(target); path.symlink_to(target)
    elif unsafe == 'fifo':
        path.unlink(); os.mkfifo(path)
    else:
        path.write_bytes(b' ' * (1024*1024+1))
    _lore_bootstrap.export_shared_memory_caps()
    assert all(key not in os.environ for key in KEYS)


@pytest.mark.parametrize('cap', [17600, 8800])
def test_sidecar_reports_shared_cap_and_preserves_true_over_capacity_counts(tmp_path, cap):
    from lore_core.config import project_slug
    cwd=tmp_path/'project';cwd.mkdir()
    root=tmp_path/'lore';memory=root/'projects'/project_slug(str(cwd))/'MEMORY.md'
    memory.parent.mkdir(parents=True)
    # 10208 rendered chars are 58% of the configured cap, 116% of the default.
    memory.write_text('- ' + 'x'*10205 + '\n')
    config=tmp_path/'claude';config.mkdir()
    (config/'settings.json').write_text(json.dumps({'env':{'LORE_MEMORY_CAP':str(cap)}}))
    env=dict(os.environ, LORE_ROOT=str(root), CLAUDE_CONFIG_DIR=str(config),
        DOXA_HOME=str(tmp_path/'doxa'), DOXA_LORE_SOURCE='package')
    for key in KEYS: env.pop(key,None)
    reply=subprocess.run([sys.executable,'-m','doxa.lore_bridge'],
        input=json.dumps({'id':1,'op':'memory_usage_v1','cwd':str(cwd)})+'\n',
        text=True,capture_output=True,env=env,
        cwd=Path(__file__).resolve().parents[1],timeout=10,check=True)
    frame=json.loads(reply.stdout.splitlines()[1])
    assert frame['value']['project_chars']==10208
    assert frame['value']['project_cap_chars']==cap
    assert round(100*frame['value']['project_chars']/cap)==(58 if cap==17600 else 116)
    assert 'x'*100 not in reply.stdout
