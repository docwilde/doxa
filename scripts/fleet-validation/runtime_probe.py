#!/usr/bin/env python3
"""Nonbillable installed-runtime fleet acceptance, isolated from owner state.

Actual installed Docker daemons, controller, transport and manifests are used.
The OpenAI reviewer credential is deliberately absent. Enforce mode must hold
initial dispatch before any worker prompt or external reviewer reservation.
This is not a live provider inference / supervision-quality acceptance test.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
import time

IMAGE = 'sha256:19df8ce57d082a1960191d9281c126a3562535c9910a9a02068e85ce420e415c'
ENGINE = 'unix:///run/user/1000/doxa-docker/engine.sock'


def private_write(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as out:
        out.write(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=Path.home()/'.local/bin/doxa')
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--native-menu', action='store_true')
    parser.add_argument('--native-menu-live', action='store_true', help='Explicit bounded $0.10 live menu case')
    args = parser.parse_args()
    state = args.state.resolve()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.stat().st_mode & 0o077:
        raise RuntimeError('State must be private')
    if len(str(state)) > 42:
        raise RuntimeError('Use a short real-disk state path for Unix socket names')
    binary = args.binary.resolve(strict=True)
    home, root, repo = (state/name for name in ('h', 'f', 'repo'))
    for path in (home, root, repo):
        path.mkdir(mode=0o700)
    private_write(repo/'README.md', '# Disposable fleet acceptance repository\nNo owner source or credentials.\n')
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'add', 'README.md'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=DOXA acceptance', '-c', 'user.email=acceptance@localhost',
                    'commit', '-q', '-m', 'test: create disposable fleet acceptance fixture'], check=True)
    env = dict(os.environ)
    for key in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'TYPESAFE_API_KEY', 'SUBQ_API_KEY', 'DOXA_SESSION_BUDGET_USD', 'DOXA_MODEL'):
        env.pop(key, None)
    # Resolve only presence/readiness for the worker. Never copy secrets into
    # state/proofs/argv; the host API adapter receives the same private key via env.
    key_path = Path.home()/'.doxa/credentials.json'
    meta = key_path.lstat()
    if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or stat.S_IMODE(meta.st_mode) != 0o600:
        raise RuntimeError('Private credential source is unsafe')
    saved_keys=json.loads(key_path.read_text())
    key = saved_keys.get('deepseek')
    if not isinstance(key, str) or not key:
        raise RuntimeError('Saved DeepSeek credential unavailable')
    env.update(DEEPSEEK_API_KEY=key, DOXA_HOME=str(home), DOXA_RUNTIME_DIR=str(state/'rt'),
        DOXA_DOCKER_HOST=ENGINE, DOXA_DOCKER_IMAGE=IMAGE, DOCKER_HOST=ENGINE,
        DOXA_LORE='0', DOXA_WORKTREE='1', TMPDIR='/home/docwilde/.t',
        LORE_ROOT=str(state/'lore'),LORE_PROJECTS_DIR=str(state/'projects'),LORE_CODEX_SESSIONS_DIR=str(state/'codex-sessions'), LORE_DISABLE_SYNC='1', LORE_DISABLE_REVIEW='1')
    if args.native_menu_live:
        glm=saved_keys.get('glm')
        if not isinstance(glm,str) or not glm:raise RuntimeError('Saved GLM credential unavailable for explicit live case')
        env['ZAI_API_KEY']=glm
    base = [str(binary), 'fleet']
    common = ['--pool', 'deepseek:deepseek-flash', '--supervisor', 'deepseek:deepseek-flash',
        '-n', '1', '--prompt', 'Disposable runtime acceptance only. Do not call tools, contact internet or change files.',
        '--cwd', str(repo), '--root', str(root), '--run-id', 'probe', '--run-budget', '0.1', '--review-budget', '0.05',
        '--alignment-supervisor', 'codex:gpt-6-astra', '--supervision-mode', 'enforce', '--review-max-calls', '1',
        '--quiescence-timeout', '300', '--quiet-dwell', '60', '--approve', 'none']
    report = {'mode': 'installed-runtime-only-no-inference', 'started_at': time.time(),
        'binary': str(binary), 'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
        'image': IMAGE, 'docker_engine': ENGINE, 'owner_sessions_modified': False,
        'workers_prompted': False, 'paid_calls': 0, 'observations': []}
    def run(extra, timeout=30):
        result = subprocess.run(base+extra, env=env, cwd=repo, text=True, capture_output=True, timeout=timeout)
        return {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
    report['version'] = subprocess.check_output([str(binary), '--version'], text=True)
    report['dry_run'] = run(['start']+common+['--dry-run'])
    log = open(state/'controller.log', 'w')
    child = subprocess.Popen(base+['start']+common, env=env, cwd=repo, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    manifest_path = root/'probe/manifest.json'
    try:
        deadline = time.monotonic()+90
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError('Controller ended before monitoring; see private controller log')
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text())
                if manifest.get('phase') == 'monitoring':
                    break
            time.sleep(.2)
        else:
            raise RuntimeError('Fleet did not reach monitoring before deadline')
        report['initial_manifest'] = manifest
        assert manifest['supervision']['paused'] is True
        assert manifest['supervision']['calls'] == 0
        assert all(row['phase'] == 'started' and row['isolation'] == 'docker-offline' for row in manifest['slots'])
        report['observations'].append('Initial review unavailable; both real Docker slots held before prompt dispatch, zero reviewer reservations')
        report['alignment'] = run(['alignment', 'probe', '--root', str(root)])
        report['status'] = run(['status', 'probe', '--root', str(root)])
        if args.native_menu or args.native_menu_live:
            from native_menu_probe import probe
            report['native_menu']=probe(binary,env,repo,root,state,live=args.native_menu_live)
        # Crash only this owned controller, preserving its verified live slots.
        child.kill(); child.wait(timeout=10)
        with open(state/'resume.log', 'w') as out:
            resumed = subprocess.Popen(base+['resume', 'probe', '--root', str(root)], env=env, cwd=repo,
                stdin=subprocess.DEVNULL, stdout=out, stderr=out)
        child = resumed
        time.sleep(2)
        assert child.poll() is None
        recovered = json.loads(manifest_path.read_text())
        assert [x['session_id'] for x in recovered['slots']] == [x['session_id'] for x in manifest['slots']]
        assert all(x['phase'] == 'started' for x in recovered['slots'])
        report['recovered_manifest'] = recovered
        report['observations'].append('Owned controller SIGKILL/resume retained exact session IDs and did not redeliver task prompts')
        report['migration_no_optin'] = run(['isolation', 'probe', '0', 'docker-open', '--root', str(root)])
        assert report['migration_no_optin']['returncode'] != 0
        report['migration'] = run(['isolation', 'probe', '0', 'docker-open', '--allow-internet', '--root', str(root)])
        if report['migration']['returncode'] == 0:
            deadline = time.monotonic()+45
            while time.monotonic() < deadline:
                migrated = json.loads(manifest_path.read_text())
                outcome=migrated.get('last_isolation_request',{})
                if outcome.get('ok') is False:
                    raise RuntimeError('Approved isolation migration failed: '+str(outcome.get('error')))
                if migrated['slots'][0]['isolation'] == 'docker-open':
                    break
                time.sleep(.2)
            else:
                raise RuntimeError('Approved idle isolation migration did not finish')
            assert migrated['slots'][1]['isolation'] == 'docker-offline'
            assert migrated['supervision']['paused'] is True
            report['migrated_manifest'] = migrated
            report['observations'].append('Approved supervisor-only idle migration left worker offline and fleet paused')
    except Exception as error:
        report['failure'] = str(error)
        raise
    finally:
        if manifest_path.exists():
            report['stop'] = run(['stop', 'probe', '--root', str(root)], timeout=75)
        if child.poll() is None:
            child.send_signal(signal.SIGINT)
            try: child.wait(timeout=15)
            except subprocess.TimeoutExpired: child.kill(); child.wait()
        log.close()
        if manifest_path.exists():
            final = json.loads(manifest_path.read_text())
            report['final_manifest'] = final
            report['cleanup_verified'] = final.get('phase') == 'finished' and final.get('live') is False and all(
                not Path(row['socket_path']).exists() for row in final.get('slots', []))
        report['finished_at'] = time.time()
        private_write(state/'report.json', json.dumps(report, indent=2)+'\n')
        print(json.dumps({'report': str(state/'report.json'), 'cleanup_verified': report.get('cleanup_verified'),
            'failure': report.get('failure'), 'observations': report['observations']}))

if __name__ == '__main__':
    main()
