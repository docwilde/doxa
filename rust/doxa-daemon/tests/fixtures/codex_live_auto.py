#!/usr/bin/env python3
"""Deterministic protocol peer for live policy/approval boundaries."""
import json
from pathlib import Path
import sys
import time
import tomllib

scenario = '__SCENARIO__'
release = Path('__RELEASE__')
log = Path('__LOG__')
turn_number = 0
pending = None
partial_tail = None
terminal_scenario = scenario in ('terminal-retry', 'terminal-failure', 'unsettled-error', 'terminal-corrupt')


def send(value):
    print(json.dumps(value), flush=True)


def complete():
    send({'method': 'item/completed', 'params': {'threadId': 'thread-auto',
          'turnId': 'turn-' + str(turn_number), 'item': {'type': 'agentMessage',
          'id': 'answer-' + str(turn_number), 'text': 'partial fixture completed' if scenario == 'partial' else 'fixture completed'}}})
    send({'method': 'turn/completed', 'params': {'threadId': 'thread-auto',
          'turn': {'id': 'turn-' + str(turn_number), 'status': 'completed', 'error': None}}})


def fixture_config():
    overrides = [sys.argv[i+1] for i, item in enumerate(sys.argv[:-1]) if item == '-c']
    for flag in ('features.token_budget=false', 'features.async_compaction=false',
                 'agents.enabled=false', 'features.multi_agent_v2=false'):
        assert flag in overrides, flag
    def merge(target, incoming):
        for key, value in incoming.items():
            if isinstance(value, dict):
                merge(target.setdefault(key, {}), value)
            else:
                target[key] = value
    config = {}
    for value in overrides:
        merge(config, tomllib.loads(value))
    return config


for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if method:
        with log.open('a') as output:
            output.write(json.dumps({'method': method, 'params': request.get('params', {})}) + '\n')
    if method == 'initialize':
        marker = '' if scenario == 'old' else 'doxa-midturn-auto-v1; '
        send({'id': request['id'], 'result': {'userAgent':
             'doxa_codex_rs/__DOXA_SUPPORTED_CODEX_VERSION__ (doxa-precompact-fail-closed-v1; ' + marker + 'fixture)'}})
    elif method == 'initialized':
        pass
    elif method == 'config/read':
        send({'id': request['id'], 'result': {'config': fixture_config()}})
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
        if method == 'thread/resume':
            assert request['params']['threadId'] == 'thread-auto'
            assert request['params']['approvalPolicy'] == ('on-request' if terminal_scenario else 'never')
        send({'id': request['id'], 'result': {'thread': {'id': 'thread-auto'}, 'model': 'account-model'}})
    elif method == 'turn/start':
        turn_number += 1
        send({'id': request['id'], 'result': {'turn': {'id': 'turn-' + str(turn_number)}}})
        if turn_number == 1 and terminal_scenario:
            params = {'threadId': 'thread-auto', 'turnId': 'turn-1'}
            send({'method': 'error', 'params': {**params, 'willRetry': scenario == 'terminal-retry',
                  'error': {'message': 'exceeded retry limit, last status: 429 Too Many Requests'}}})
            if scenario == 'unsettled-error':
                break
            send({'method': 'item/completed', 'params': {**params, 'item': {
                  'type': 'agentMessage', 'id': 'partial', 'text': 'public partial response'}}})
            if scenario == 'terminal-corrupt':
                while not release.exists():
                    time.sleep(.01)
            send({'method': 'turn/completed', 'params': {'threadId': 'thread-auto', 'turn': {
                  'id': 'turn-1', 'status': 'completed' if scenario == 'terminal-retry' else 'failed',
                  'error': None if scenario == 'terminal-retry' else {'message': 'exceeded retry limit, last status: 429 Too Many Requests'}}}})
            continue
        if turn_number > 1 or (request['params']['approvalPolicy'] == 'never' and scenario != 'fullaccess'):
            assert request['params']['approvalPolicy'] == ('on-request' if terminal_scenario else 'never')
            assert request['params']['sandboxPolicy']['type'] == 'workspaceWrite'
            complete()
            continue
        if scenario == 'partial':
            frame = json.dumps({'method': 'item/agentMessage/delta', 'params': {
                'threadId': 'thread-auto', 'turnId': 'turn-1', 'itemId': 'answer-1', 'delta': 'partial '}}) + '\n'
            cut = len(frame) // 2
            partial_tail = frame[cut:]
            sys.stdout.write(frame[:cut])
            sys.stdout.flush()
            release.with_name('partial-ready').write_text('ready')
            continue
        params = {'threadId': 'thread-auto', 'turnId': 'turn-1', 'itemId': 'command-1'}
        if scenario in ('question', 'fullaccess'):
            params['questions'] = [{'id': 'q', 'question': 'Pick a fixture option',
                                    'options': [{'label': 'yes', 'description': 'fixture'}]}]
            approval_method = 'item/tool/requestUserInput'
        elif scenario == 'operator':
            params.update({'callId': 'peer-1', 'namespace': None, 'tool': 'doxa_peer_list', 'arguments': {}})
            approval_method = 'item/tool/call'
        else:
            params.update({'command': 'python3 marker.py', 'cwd': request['params'].get('cwd')})
            approval_method = 'item/commandExecution/requestApproval'
        pending = 'approval-1'
        send({'id': pending, 'method': approval_method, 'params': params})
    elif method == 'turn/settings/update':
        assert request['params'] == {'threadId': 'thread-auto', 'turnId': 'turn-1', 'doxaAuto': True}
        if partial_tail is not None:
            sys.stdout.write(partial_tail)
            sys.stdout.flush()
            partial_tail = None

        if scenario == 'rejected':
            send({'id': request['id'], 'error': {'code': -32602, 'message': 'fixture managed policy'}})
        else:
            send({'id': request['id'], 'result': {'status': 'targetUnavailable' if scenario == 'gone' else 'applied'}})
        if scenario == 'partial':
            pending = 'approval-after-partial'
            send({'id': pending, 'method': 'item/commandExecution/requestApproval',
                  'params': {'threadId': 'thread-auto', 'turnId': 'turn-1',
                  'itemId': 'late-command', 'command': 'python3 late.py'}})
    elif method == 'account/rateLimits/read':
        send({'id': request['id'], 'result': {}})
    elif method == 'turn/interrupt':
        send({'id': request['id'], 'result': {}})
        send({'method': 'turn/completed', 'params': {'threadId': 'thread-auto',
              'turn': {'id': 'turn-1', 'status': 'interrupted', 'error': None}}})
    elif not method and request.get('id') == pending:
        if scenario in ('command', 'corrupt', 'late', 'partial'):
            assert request['result']['decision'] == 'decline'
        pending = None
        if scenario == 'late':
            pending = 'approval-late'
            send({'id': pending, 'method': 'item/commandExecution/requestApproval',
                  'params': {'threadId': 'thread-auto', 'turnId': 'turn-1',
                  'itemId': 'late-command', 'command': 'python3 late.py'}})
            scenario = 'command'
            continue
        if scenario in ('command', 'corrupt', 'partial'):
            deadline = time.monotonic() + 30
            while not release.exists():
                assert time.monotonic() < deadline
                time.sleep(.01)
        complete()
