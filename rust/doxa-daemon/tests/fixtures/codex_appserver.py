#!/usr/bin/env python3
"""App-server protocol peer preserving existing adversarial turn fixtures.

Only this test peer invokes the synthetic stream producer. The daemon sees
real initialize/config/hooks/thread/turn RPCs, never its legacy exec transport.
Children stay in the app-server process group for cancellation assertions.
"""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import tomllib
from pathlib import Path

def admit_fixture_owner():
    # Catalog discovery intentionally has no protected turn owner. When the
    # engine supplies one, no app-server work may precede explicit admission.
    descriptor = os.environ.pop('DOXA_CODEX_OWNER_FD', None)
    if descriptor is None:
        return
    owner = socket.socket(fileno=int(descriptor))
    owner.set_inheritable(False)
    owner.settimeout(5)
    owner.sendall(b'DOXA_PROVIDER_OWNER_V1\n')
    if owner.recv(1) != b'G':
        os._exit(80)
    owner.settimeout(None)
    fixture_group = os.getpgrp()

    def watch_owner():
        try:
            while owner.recv(1):
                pass
        finally:
            os.killpg(fixture_group, signal.SIGKILL)

    # Capture socket/group in this invocation; a repeated header cannot
    # replace the first watcher's socket after its environment FD is removed.
    threading.Thread(target=watch_owner, daemon=True).start()


admit_fixture_owner()

producer = Path(__file__).with_suffix('.turn')
source = producer.read_text()
match = re.search(r"thread_id[\"']\s*:\s*[\"']([A-Za-z0-9_-]+)", source)
thread_id = match.group(1) if match else 'thread-1'
resumed = False
turn_number = 0
log = Path(__file__).with_suffix('.rpc')


def send(row):
    print(json.dumps(row), flush=True)


def reply(request, value):
    send({'id': request['id'], 'result': value})


for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if not method:
        continue
    with log.open('a') as output:
        # No prompt content in control evidence; fixture secrets belong only
        # to their deliberately adversarial synthetic turn captures.
        params = request.get('params', {}).copy()
        params.pop('input', None)
        output.write(json.dumps({'method': method, 'params': params}) + '\n')
    if method == 'initialize':
        reply(request, {'userAgent': 'doxa_codex_rs/0.156.1 (doxa-precompact-fail-closed-v1; upstream fixture)'})
    elif method == 'initialized':
        continue
    elif method == 'config/read':
        assert 'features.token_budget=false' in sys.argv
        reply(request, {'config': {'features': {'token_budget': False}}, 'origins': {}, 'layers': None})
    elif method == 'hooks/list':
        overrides = [sys.argv[i+1] for i, item in enumerate(sys.argv[:-1]) if item == '-c']
        hooks = next(tomllib.loads(value)['hooks'] for value in overrides if value.startswith('hooks='))
        key = next(iter(hooks['state']))
        row = {'key': key, 'command': hooks['PreCompact'][0]['hooks'][0]['command'],
               'handlerType': 'command', 'enabled': True, 'trustStatus': 'trusted',
               'currentHash': hooks['state'][key]['trusted_hash'], 'eventName': 'preCompact',
               'source': 'sessionFlags', 'timeoutSec': 240, 'async': False}
        reply(request, {'data': [{'cwd': request['params']['cwds'][0], 'hooks': [row]}], 'errors': []})
    elif method == 'model/list':
        reply(request, {'data': [{'model': 'account-model', 'isDefault': True,
              'supportedReasoningEfforts': [{'reasoningEffort': 'low'}, {'reasoningEffort': 'high'}],
              'defaultReasoningEffort': 'high'}], 'nextCursor': None})
    elif method in ('thread/start', 'thread/resume'):
        resumed = method == 'thread/resume'
        if resumed:
            assert request['params']['threadId'] == thread_id
        reply(request, {'thread': {'id': thread_id}, 'model': request['params'].get('model')})
    elif method == 'turn/start':
        params = request['params']
        assert params['threadId'] == thread_id
        turn_number += 1
        turn_id = 'turn-' + str(turn_number)
        reply(request, {'turn': {'id': turn_id}})
        # Original fixture bodies retain their blocking writes, malformed
        # output and descendant processes. Synthetic argv selects their own
        # first/resumed behavior; daemon controls are asserted in the RPC log.
        argv = [str(producer), 'exec']
        if resumed:
            argv += ['resume', thread_id]
        if params.get('model'):
            argv += ['--model', params['model']]
        if params.get('effort'):
            argv += ['-c', 'model_reasoning_effort="' + params['effort'] + '"']
        child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        child.stdin.write(params['input'][0]['text'].encode())
        child.stdin.close()
        for event_line in child.stdout:
            event = json.loads(event_line)
            if event['type'] == 'item.completed' and event['item']['type'] == 'agent_message':
                send({'method': 'item/completed', 'params': {'threadId': thread_id, 'turnId': turn_id,
                      'item': {'type': 'agentMessage', 'id': 'answer-' + str(turn_number), 'text': event['item']['text']}}})
        status = child.wait()
        send({'method': 'turn/completed', 'params': {'threadId': thread_id,
              'turn': {'id': turn_id, 'status': 'completed' if status == 0 else 'failed',
                       'error': None if status == 0 else {'message': 'synthetic provider failure'}}}})
        resumed = True
    else:
        send({'id': request['id'], 'error': {'code': -32601, 'message': 'unknown fixture RPC'}})
