import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {importKey, open, seal} from '../crypto.mjs';
import {DoxaRemoteClient, eventContext, hubOrigin, validTarget} from '../client.mjs';

const fixture = name => JSON.parse(readFileSync(new URL(name, import.meta.url), 'utf8'));
const keyText = 'BwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwc';

test('Rust compressed envelope opens in the packaged browser client', async () => {
  const sample = fixture('rust-envelope.json');
  const key = await importKey(sample.key);
  assert.deepEqual(await open(key, sample.context, sample.envelope), sample.value);
  await assert.rejects(open(key, 'other~session|result|transcript|request-1', sample.envelope));
  const forged = {...sample.envelope, data:'AAAA'};
  await assert.rejects(open(key, sample.context, forged));
});

test('browser compressed envelope is stable fixture and rejects wrong key', async () => {
  const sample = fixture('js-envelope.json');
  const key = await importKey(sample.key);
  assert.deepEqual(await open(key, sample.context, sample.envelope), sample.value);
  assert.equal((await open(key, sample.context, await seal(key, sample.context, sample.value))).text,
    sample.value.text);
  const otherKey = btoa(String.fromCharCode(...new Uint8Array(32).fill(8))).replace(/=+$/, '');
  await assert.rejects(open(await importKey(otherKey),
    sample.context, sample.envelope));
  await assert.rejects(importKey('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'));
});

test('hub and target validation is fail closed', () => {
  assert.equal(hubOrigin('https://machine.tailnet.ts.net/'), 'https://machine.tailnet.ts.net');
  assert.throws(() => hubOrigin('https://machine.tailnet.ts.net.evil.example/'));
  assert.throws(() => hubOrigin('http://machine.tailnet.ts.net/'));
  assert.throws(() => hubOrigin('https://machine.tailnet.ts.net/path'));
  assert.equal(validTarget('host~session-1'), true);
  assert.equal(validTarget('host~../session'), false);
});

test('prompt and result stay sealed across the hub', async () => {
  const key = await importKey(keyText);
  const client = new DoxaRemoteClient('https://machine.tailnet.ts.net/', key);
  const original = globalThis.fetch;
  let posted;
  globalThis.fetch = async (url, options) => {
    if (String(url).endsWith('/prompt')) {
      assert.equal(options.credentials, 'include');
      posted = JSON.parse(options.body);
      assert.ok(!options.body.includes('private prompt'));
      assert.deepEqual((await open(key, 'host~session|command|prompt', posted.sealed)).text,
        'private prompt');
      return new Response(JSON.stringify({command_id:'command-1'}), {status:200});
    }
    assert.ok(String(url).endsWith('/api/commands/command-1'));
    const sealed = await seal(key, `host~session|result|prompt|${posted.request_id}`, {ok:true});
    return new Response(JSON.stringify({status:'accepted',result:{sealed}}), {status:200});
  };
  try { assert.deepEqual(await client.command('host~session','prompt',{text:'private prompt'}), {ok:true}); }
  finally { globalThis.fetch = original; }
});

test('live event content is authenticated before delivery', async () => {
  const key = await importKey(keyText);
  const client = new DoxaRemoteClient('https://machine.tailnet.ts.net/', key);
  const incarnation = '0123456789abcdef0123456789abcdef';
  const sealed = await seal(key, eventContext('host~session', incarnation, 5, 'text_delta'), {text:'private delta'});
  const frame = {type:'event',incarnation,seq:5,event:{type:'text_delta',data:{sealed}}};
  const original = globalThis.fetch;
  globalThis.fetch = async () => new Response(`data: ${JSON.stringify({type:'hello',incarnation})}\n\ndata: ${JSON.stringify(frame)}\n\n`, {status:200});
  const received = [];
  try { await client.events('host~session', incarnation, 5, AbortSignal.timeout(1000), frame => received.push(frame)); }
  finally { globalThis.fetch = original; }
  assert.deepEqual(received[1].event.data, {text:'private delta'});
});

test('captured event from an old incarnation is rejected after a restart', async () => {
  const key = await importKey(keyText);
  const client = new DoxaRemoteClient('https://machine.tailnet.ts.net/', key);
  const target = 'host~session';
  const incarnationA = '0123456789abcdef0123456789abcdef';
  const incarnationB = 'fedcba9876543210fedcba9876543210';
  const captured = await seal(key, eventContext(target, incarnationA, 5, 'text_delta'),
    {text:'stale incarnation-A event'});
  // Public metadata can be edited by the broker; force verification to rely
  // on the expected B context rather than trusting this envelope field.
  const replay = {type:'event',incarnation:incarnationB,seq:5,
    event:{type:'text_delta',data:{sealed:captured}}};
  const original = globalThis.fetch;
  globalThis.fetch = async () => new Response(
    `data: ${JSON.stringify({type:'hello',incarnation:incarnationB})}\n\ndata: ${JSON.stringify(replay)}\n\n`,
    {status:200});
  const received = [];
  try {
    await assert.rejects(client.events(target, incarnationB, 5, AbortSignal.timeout(1000), frame => received.push(frame)));
  } finally { globalThis.fetch = original; }
  assert.equal(received.some(frame => frame.type === 'event'), false);
});

test('shared event vector uses the versioned incarnation context', async () => {
  const sample = fixture('../../test-vectors/remote-event-v2.json');
  const key = await importKey(sample.key);
  assert.equal(eventContext(sample.target, sample.incarnation, sample.seq, sample.event_type), sample.context);
  assert.deepEqual(await open(key, sample.context, sample.envelope), sample.value);
  await assert.rejects(open(key, eventContext(sample.target,
    'fedcba9876543210fedcba9876543210', sample.seq, sample.event_type), sample.envelope));
});
