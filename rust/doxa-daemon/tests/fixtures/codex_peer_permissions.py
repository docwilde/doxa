#!/usr/bin/env python3
"""Private protocol peer: built-in peers, exact targets and permission transitions."""
import json
import sys
import tomllib
from pathlib import Path

root = Path(__file__).parent
target = '195603b4-d233-47b0-8f10-93d3a7c1b6e8'
foreign = '195603b4-d233-47b0-8f10-93d3a7c1b6e9'
turn_number = 0
calls = []
step = 0

def send(row):
    print(json.dumps(row), flush=True)

def tool():
    name, arguments = calls[step]
    send({'id': 'peer-request-' + str(turn_number) + '-' + str(step),
          'method': 'item/tool/call', 'doxa_permission_mode': 'full-access',
          'params': {'threadId': 'peer-thread', 'turnId': 'peer-turn-' + str(turn_number),
                     'callId': 'peer-call-' + str(turn_number) + '-' + str(step),
                     'namespace': None, 'tool': name, 'arguments': arguments}})

for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if method:
        with root.joinpath('peer-permission-rpcs.jsonl').open('a') as log:
            params = request.get('params', {}).copy()
            params.pop('input', None)
            log.write(json.dumps({'method': method, 'params': params}) + '\n')
    if method == 'initialize':
        send({'id': request['id'], 'result': {'userAgent':
              'doxa_codex_rs/0.156.1 (doxa-precompact-fail-closed-v1; doxa-midturn-auto-v1; fixture)'}})
    elif method == 'initialized':
        pass
    elif method == 'config/read':
        send({'id': request['id'], 'result': {'config': {'features': {'token_budget': False}}}})
    elif method == 'hooks/list':
        overrides = [sys.argv[i+1] for i, item in enumerate(sys.argv[:-1]) if item == '-c']
        hooks = next(tomllib.loads(value)['hooks'] for value in overrides if value.startswith('hooks='))
        key = next(iter(hooks['state']))
        row = {'key': key, 'command': hooks['PreCompact'][0]['hooks'][0]['command'],
               'handlerType': 'command', 'enabled': True, 'trustStatus': 'trusted',
               'currentHash': hooks['state'][key]['trusted_hash'], 'eventName': 'preCompact',
               'source': 'sessionFlags', 'timeoutSec': 240, 'async': False}
        send({'id': request['id'], 'result': {'data': [{'cwd': request['params']['cwds'][0],
               'hooks': [row]}], 'errors': []}})
    elif method in ('thread/start', 'thread/resume'):
        send({'id': request['id'], 'result': {'thread': {'id': 'peer-thread'}, 'model': 'account-model'}})
    elif method == 'turn/start':
        turn_number += 1
        step = 0
        send({'id': request['id'], 'result': {'turn': {'id': 'peer-turn-' + str(turn_number)}}})
        if request['params']['sandboxPolicy']['type'] == 'dangerFullAccess':
            assert request['params']['approvalPolicy'] == 'never'
            calls = [('doxa_peer_list', {'limit': 100}),
                     ('doxa_peer_send', {'target': target[:8], 'text': 'prefix must refuse'}),
                     ('doxa_peer_send', {'target': foreign, 'text': 'foreign must refuse'}),
                     ('doxa_peer_send', {'session_id': target, 'message': 'full-access exact delivery'}),
                     ('doxa_peer_history', {'direction': 'sent', 'limit': 100})]
        else:
            calls = [('doxa_peer_list', {})]
        tool()
    elif method == 'turn/settings/update':
        assert request['params']['doxaAuto'] is True
        send({'id': request['id'], 'result': {'status': 'applied'}})
    elif method == 'account/rateLimits/read':
        send({'id': request['id'], 'result': {}})
    elif method == 'turn/interrupt':
        send({'id': request['id'], 'result': {}})
        send({'method': 'turn/completed', 'params': {'threadId': 'peer-thread',
              'turn': {'id': 'peer-turn-' + str(turn_number), 'status': 'interrupted', 'error': None}}})
    elif not method and request.get('id', '').startswith('peer-request-'):
        with root.joinpath('peer-permission-results.jsonl').open('a') as log:
            log.write(json.dumps({'turn': turn_number, 'step': step, 'response': request}) + '\n')
        step += 1
        if step < len(calls):
            tool()
        else:
            send({'method': 'item/completed', 'params': {'threadId': 'peer-thread',
                  'turnId': 'peer-turn-' + str(turn_number),
                  'item': {'type': 'agentMessage', 'id': 'peer-answer-' + str(turn_number), 'text': 'peer fixture done'}}})
            send({'method': 'turn/completed', 'params': {'threadId': 'peer-thread',
                  'turn': {'id': 'peer-turn-' + str(turn_number), 'status': 'completed', 'error': None}}})
