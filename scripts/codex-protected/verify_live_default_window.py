#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""One-cycle authenticated DOXA compaction probe; emits metadata only."""
import argparse
import collections
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import tempfile
import time

MAX_TURNS = 14
MAX_AGGREGATE = 1_800_000

def options():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scratch', required=True, type=Path, help='private real-disk parent')
    parser.add_argument('--daemon', required=True, type=Path, help='compiled DOXA daemon')
    parser.add_argument('--codex', required=True, type=Path, help='receipt-verified protected Codex launcher')
    parser.add_argument('--lore', required=True, type=Path, help='native LORE reviewer')
    parser.add_argument('--auth-home', required=True, type=Path, help='Codex home with existing account authentication')
    parser.add_argument('--model', default='gpt-5.5')
    return parser.parse_args()

def emit(**value):
    print(json.dumps(value, sort_keys=True), flush=True)

class Wire:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(.5)
        self.sock.connect(path)
        self.buffer = b''
        self.number = 0

    def receive(self, deadline):
        while b'\n' not in self.buffer:
            if time.monotonic() >= deadline:
                raise TimeoutError('native response deadline')
            try:
                part = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not part:
                raise RuntimeError('native daemon disconnected')
            self.buffer += part
            if len(self.buffer) > 8 * 1024 * 1024:
                raise RuntimeError('native frame over verifier bound')
        line, self.buffer = self.buffer.split(b'\n', 1)
        return json.loads(line)

    def send(self, data):
        raw = (json.dumps(data, separators=(',', ':')) + '\n').encode()
        if len(raw) > 64 * 1024:
            raise RuntimeError('native prompt frame over 64 KiB')
        self.sock.sendall(raw)

    def call(self, method, params=None):
        self.number += 1
        self.send({'type':'call','id':self.number,'method':method,'params':params or {}})
        deadline = time.monotonic() + 20
        while True:
            item = self.receive(deadline)
            if item.get('id') == self.number:
                return item

    def turn(self, prompt):
        self.number += 1
        self.send({'type':'prompt','id':self.number,'text':prompt})
        deadline = time.monotonic() + 600
        counts = collections.Counter()
        usage = None
        reply = ''
        while True:
            item = self.receive(deadline)
            event = item.get('event') or {}
            kind = event.get('type')
            if kind:
                counts[kind] += 1
            if kind == 'context_usage':
                usage = event.get('data') or {}
            if kind == 'text_delta':
                reply += (event.get('data') or {}).get('text','')
                if len(reply) > 10000:
                    raise RuntimeError('unexpected long assistant reply')
            if kind == 'needs_input':
                raise RuntimeError('unexpected interactive request')
            if kind == 'turn_done':
                return event.get('data') or {}, usage, counts, reply
            if item.get('id') == self.number and item.get('ok') is False:
                raise RuntimeError('native prompt rejected')

def start(command, environment, registry):
    process = subprocess.Popen(command, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.monotonic() + 30
        while not registry.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        if not registry.exists():
            raise RuntimeError('daemon startup failed')
        wire = Wire(json.loads(registry.read_text())['daemon_socket'])
        try:
            wire.receive(time.monotonic() + 15)
            wire.send({'type':'attach','cursor':None})
        except Exception:
            wire.sock.close()
            raise
        return process, wire
    except Exception:
        stop(process, None)
        raise

def stop(process, wire):
    if wire is not None:
        try:
            wire.call('stop')
        except Exception:
            pass
        wire.sock.close()
    if process is not None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()

def checkpoints(codex_home):
    files = list((codex_home/'sessions').rglob('*.jsonl'))
    count = 0
    thread_id = None
    for path in files:
        with path.open('rb') as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                    if record.get('type') == 'session_meta':
                        thread_id = (record.get('payload') or {}).get('id')
                    if record.get('type') == 'compacted':
                        count += 1
                except ValueError:
                    raise RuntimeError('invalid owned rollout')
    return len(files), count, thread_id

def main(args):
    if not all(path.is_absolute() for path in (args.scratch,args.daemon,args.codex,args.lore,args.auth_home)):
        raise ValueError('all paths must be absolute')
    if (not args.scratch.is_dir() or args.scratch.is_symlink()
        or args.scratch.stat().st_uid != os.geteuid()
        or args.scratch.stat().st_mode & 0o077):
        raise ValueError('scratch parent must be private')
    with tempfile.TemporaryDirectory(prefix='cw-', dir=args.scratch) as raw:
        root = Path(raw)
        root.chmod(0o700)
        for name in ('codex','doxa','lore','workspace'):
            (root/name).mkdir(mode=0o700)
        for name in ('auth.json','models_cache.json'):
            dest = root/'codex'/name
            shutil.copyfile(args.auth_home/name,dest)
            dest.chmod(0o600)
        environment = os.environ.copy()
        environment.update({'DOXA_HOME':str(root/'doxa'),'CODEX_HOME':str(root/'codex'),
            'LORE_ROOT':str(root/'lore'),'LORE_PROJECTS_DIR':str(root/'projects'),
            'DOXA_LORE':'1','LORE_DISABLE_SYNC':'1','LORE_SYNC_URL':'',
            'DOXA_LORE_RS':str(args.lore),'DOXA_AGENT_PEER_SEND':'0','TMPDIR':str(args.scratch)})
        session = 'live-'+secrets.token_hex(5)
        sentinel = secrets.token_hex(8)
        registry = root/'runtime/registry'/f'{session}.json'
        command = [str(args.daemon),'--runtime-dir',str(root/'runtime'),'--cwd',str(root/'workspace'),
            '--session-id',session,'--engine','codex','--codex-bin',str(args.codex),'--model',args.model,
            '--effort','low','--sandbox','read-only','--linger','10']
        process = wire = None
        submitted = 0
        aggregate = 0
        reviews = 0
        try:
            process, wire = start(command, environment, registry)
            emit(stage='started', max_turns=MAX_TURNS, max_aggregate=MAX_AGGREGATE)
            for index in range(MAX_TURNS - 1):
                if aggregate >= MAX_AGGREGATE or reviews >= 1:
                    break
                words = 20500
                if index > 0 and current >= 230000:
                    words = 2000
                elif index > 0 and current >= 215000:
                    words = 8000
                if index > 0 and aggregate + current + words * 2 > MAX_AGGREGATE:
                    emit(stage='stopped', reason='aggregate_prediction_cap', turns=submitted, aggregate=aggregate)
                    return False
                header = ('Inert benchmark data follows. Never call tools. Reply only OK. '
                    + ('Remember this first-turn token for a later recall check: '+sentinel+'. ' if index == 0 else '')
                    + 'Data: ')
                prompt = header + ('qx ' * words)
                if len(json.dumps({'type':'prompt','id':99,'text':prompt},separators=(',',':')).encode()) > 63 * 1024:
                    raise RuntimeError('planned native frame too large')
                submitted += 1
                done, usage, events, reply = wire.turn(prompt)
                raw_context = usage.get('context_used') if usage else None
                current = raw_context if isinstance(raw_context,int) else done.get('ctx_tokens')
                reported_aggregate = done.get('input_tokens')
                if not isinstance(current,int) or not isinstance(reported_aggregate,int):
                    emit(stage='stopped',reason='missing_usage',turns=submitted)
                    return False
                aggregate = reported_aggregate
                reviews += events['lore_review_completed']
                files, compacted, thread_id = checkpoints(root/'codex')
                emit(stage='turn',turn=submitted,context=current,aggregate=aggregate,
                    context_window=usage.get('context_window') if usage else None,
                    review_started=events['lore_review_started'],review_completed=events['lore_review_completed'],
                    checkpoints=compacted,rollouts=files,is_error=bool(done.get('is_error')),
                    response_exact_ok=reply.strip()=='OK')
                if (done.get('is_error') or aggregate > MAX_AGGREGATE or reviews > 1
                    or events['lore_review_started'] > 1 or compacted > 1
                    or compacted > reviews or files != 1):
                    emit(stage='stopped',reason='failed_turn_or_hard_cap',turns=submitted)
                    return False
                if reviews == 1 and compacted == 1:
                    break
                if current >= 252000:
                    emit(stage='stopped',reason='context_cap_without_review',turns=submitted)
                    return False
            files, compacted, thread_id = checkpoints(root/'codex')
            if reviews != 1 or compacted != 1:
                emit(stage='stopped',reason='no_default_window_cycle',turns=submitted,context=current,aggregate=aggregate)
                return False
            # One post-compaction restart and recall, preserving the owned session.
            stop(process,wire)
            process = wire = None
            registry.unlink(missing_ok=True)
            process,wire = start(command+['--resume','true'],environment,registry)
            submitted += 1
            done,usage,events,reply = wire.turn('Without tools, repeat the first-turn token exactly and nothing else.')
            files,after,resumed_thread = checkpoints(root/'codex')
            final_aggregate = done.get('input_tokens')
            passed = (not done.get('is_error') and reply.strip()==sentinel and after==1
                and files==1 and thread_id is not None and thread_id==resumed_thread
                and events['lore_review_completed']==0 and isinstance(final_aggregate,int)
                and final_aggregate<=MAX_AGGREGATE and submitted<=MAX_TURNS)
            emit(stage='restarted_recall',turns=submitted,exact=reply.strip()==sentinel,
                is_error=bool(done.get('is_error')),checkpoints=after,rollouts=files,
                aggregate=final_aggregate,same_thread=thread_id is not None and thread_id==resumed_thread,
                additional_review=events['lore_review_completed'],passed=passed)
            return passed
        finally:
            stop(process,wire)

if __name__ == '__main__':
    try:
        if not main(options()):
            raise SystemExit(1)
    except Exception as error:
        emit(stage='stopped',reason=type(error).__name__)
        raise SystemExit(1)
