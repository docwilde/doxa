// SPDX-License-Identifier: AGPL-3.0-only
// No network, browser account, or notification service is used by this harness.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
import test from 'node:test';

const code = await readFile(new URL('../../rust/doxa-remote/assets/remote.js', import.meta.url), 'utf8');
class Element {
  children = []; dataset = {}; value = ''; hidden = false; disabled = false; title = '';
  append(...children) { this.children.push(...children); }
  prepend(...children) { this.children.unshift(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute() {}
  scrollIntoView() {}
  querySelector() { return this.button ||= new Element(); }
}
const tick = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
async function browser() {
  const elements = new Map();
  const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const session = {id:'host~session', title:'Fixture', incarnation:'first', encrypted:false};
  const original = {id:'input-1', tool_name:'Bash', title:'Review original'};
  const state = {session, history:{ok:true, incarnation:'first', next_seq:10,
    pending_inputs_complete:true, pending_inputs:[original], turns:[]}, fail:false};
  const calls = [], sources = [], timers = [];
  const context = vm.createContext({
    document:{getElementById:get, createElement:() => new Element(), createDocumentFragment:() => new Element()},
    window:{}, navigator:{}, crypto:{randomUUID:() => 'fixture-request-id'},
    setTimeout:fn => timers.push(fn), setInterval:() => {},
    fetch:async (url, options = {}) => {
      const path = new URL(url, 'https://hub.ts.net').pathname;
      calls.push({path, method:options.method || 'GET', body:options.body ? JSON.parse(options.body) : null});
      let data;
      if (path === '/api/sessions') data = {sessions:[state.session]};
      else if (path.endsWith('/transcript')) {
        if (state.fail) return {ok:false, json:async () => ({error:'snapshot failed'})};
        data = state.history;
      } else data = {ok:true};
      return {ok:true, json:async () => JSON.parse(JSON.stringify(data))};
    },
    EventSource:class {
      constructor(url) { this.url = url; this.closed = false; sources.push(this); }
      close() { this.closed = true; }
      emit(frame) { this.onmessage({data:JSON.stringify(frame)}); }
    },
  });
  vm.runInContext(code + '\nglobalThis.probe = {selectSession, get ready() {return sessionReady}, get generation() {return generation}};', context);
  await tick();
  assert.equal(context.probe.ready, true);
  const writes = () => calls.filter(call => /\/(prompt|answer)$/.test(call.path));
  const flush = async () => { for (const fn of timers.splice(0)) fn(); await tick(); };
  return {context, state, calls, sources, get, writes, original, flush};
}

test('replay gap removes approvals before refresh and binds new answers to the snapshot', async () => {
  const b = await browser();
  const stale = b.get('question').children.find(child => child.textContent === 'Allow');
  b.state.history.pending_inputs = [{...b.original, title:'Review refreshed'}];
  const stream = b.sources.at(-1);
  stream.emit({type:'event', seq:10, event:{type:'replay_gap', data:{}}});
  assert.equal(stream.closed, true);
  assert.equal(b.context.probe.ready, false);
  assert.equal(b.get('question').hidden, true);
  await stale.onclick();
  b.get('prompt-text').value = 'draft';
  await b.get('prompt').onsubmit({preventDefault() {}});
  assert.equal(b.writes().length, 0);
  await b.flush();
  assert.equal(b.context.probe.ready, true);
  assert.equal(b.get('prompt-text').value, 'draft');
  await stale.onclick();
  assert.equal(b.writes().length, 0);
  await b.get('question').children.find(child => child.textContent === 'Allow').onclick();
  assert.deepEqual(b.writes()[0].body.reviewed_request, b.state.history.pending_inputs[0]);
  assert.equal(b.writes()[0].body.incarnation, 'first');
});

test('disconnect reloads host state even when the restarted hub has no replay events', async () => {
  const b = await browser();
  b.get('prompt-text').value = 'unsent';
  b.state.history.pending_inputs = [];
  const before = b.calls.filter(call => call.path.endsWith('/transcript')).length;
  const stream = b.sources.at(-1);
  stream.onerror();
  assert.equal(b.context.probe.ready, false);
  await b.flush();
  assert.equal(stream.closed, true);
  assert.equal(b.context.probe.ready, true);
  assert.equal(b.get('question').hidden, true);
  assert.equal(b.get('prompt-text').value, 'unsent');
  assert.equal(b.calls.filter(call => call.path.endsWith('/transcript')).length, before + 1);
  assert.equal(b.writes().length, 0);
});

test('failed reload remains read only and does not reconnect from a stale cursor', async () => {
  const b = await browser();
  const stale = b.get('question').children.find(child => child.textContent === 'Allow');
  b.state.fail = true;
  b.sources.at(-1).onerror();
  await b.flush();
  assert.equal(b.context.probe.ready, false);
  assert.equal(b.get('prompt').querySelector('button').disabled, true);
  assert.equal(b.sources.length, 1);
  await stale.onclick();
  b.get('prompt-text').value = 'must not send';
  await b.get('prompt').onsubmit({preventDefault() {}});
  assert.equal(b.writes().length, 0);
});

test('interior sequence gap and mismatched hello trigger snapshot recovery', async () => {
  const b = await browser();
  let stream = b.sources.at(-1);
  stream.emit({type:'event', seq:9, event:{type:'needs_input', data:{id:'duplicate'}}});
  assert.equal(stream.closed, false);
  stream.emit({type:'event', seq:11, event:{type:'needs_input', data:{id:'after-hole'}}});
  assert.equal(stream.closed, true);
  assert.equal(b.get('question').hidden, true);
  await b.flush();
  assert.equal(b.context.probe.ready, true);
  stream = b.sources.at(-1);
  b.state.session.incarnation = 'second'; b.state.history.incarnation = 'second';
  stream.emit({type:'hello', incarnation:'second'});
  assert.equal(stream.closed, true);
  await b.flush();
  assert.equal(b.context.probe.ready, true);
  assert.equal(b.writes().length, 0);
});

test('inventory change or incomplete pending inputs prevents a usable snapshot', async () => {
  const b = await browser();
  b.state.session.incarnation = 'second';
  await b.context.probe.selectSession({...b.state.session, incarnation:'first'}, true);
  assert.equal(b.context.probe.ready, false);
  assert.equal(b.sources.length, 1);
  b.state.history.incarnation = 'second'; b.state.history.pending_inputs_complete = false;
  await b.context.probe.selectSession(b.state.session, true);
  assert.equal(b.context.probe.ready, false);
  assert.equal(b.writes().length, 0);
});

test('same ID with changed question invalidates an existing approval callback', async () => {
  const b = await browser();
  const stale = b.get('question').children.find(child => child.textContent === 'Allow');
  b.sources.at(-1).emit({type:'event', seq:10, event:{type:'needs_input', data:{...b.original, title:'Different request'}}});
  await stale.onclick();
  assert.equal(b.writes().length, 0);
  await b.get('question').children.find(child => child.textContent === 'Allow').onclick();
  assert.equal(b.writes()[0].body.reviewed_request.title, 'Different request');
});

test('local adapter hello refreshes approval callbacks and refuses incomplete review', async () => {
  const b = await browser();
  const stale = b.get('question').children.find(child => child.textContent === 'Allow');
  b.sources.at(-1).emit({type:'hello', incarnation:'first', pending_inputs_complete:true, pending_inputs:[b.original]});
  await stale.onclick();
  assert.equal(b.writes().length, 0);
  await b.get('question').children.find(child => child.textContent === 'Allow').onclick();
  assert.deepEqual(b.writes()[0].body.reviewed_request, b.original);
  b.sources.at(-1).emit({type:'hello', incarnation:'first', pending_inputs_complete:false, pending_inputs:[]});
  assert.equal(b.context.probe.ready, false);
  assert.equal(b.get('question').hidden, true);
});

test('same-ID changed input between snapshot and local hello rebuilds the displayed review', async () => {
  const b = await browser();
  const stale = b.get('question').children.find(child => child.textContent === 'Allow');
  const changed = {...b.original, title:'Changed since snapshot', options:['deny','allow once']};
  b.sources.at(-1).emit({type:'hello', incarnation:'first', pending_inputs_complete:true, pending_inputs:[changed]});
  assert.equal(b.get('question').children[0].textContent, 'Changed since snapshot');
  await stale.onclick();
  assert.equal(b.writes().length, 0);
  await b.get('question').children.find(child => child.textContent === 'Allow').onclick();
  assert.deepEqual(b.writes()[0].body.reviewed_request, changed);
});

test('resolved input absent from local hello removes its old approval controls', async () => {
  const b = await browser();
  const stale = b.get('question').children.find(child => child.textContent === 'Allow');
  b.sources.at(-1).emit({type:'hello', incarnation:'first', pending_inputs_complete:true, pending_inputs:[]});
  assert.equal(b.get('question').hidden, true);
  assert.equal(b.get('question').children.length, 0);
  assert.equal(b.context.probe.ready, true);
  await stale.onclick();
  assert.equal(b.writes().length, 0);
});

test('hub hello without pending-input snapshot keeps the reviewed host snapshot usable', async () => {
  const b = await browser();
  b.sources.at(-1).emit({type:'hello', incarnation:'first', engine:'remote'});
  assert.equal(b.context.probe.ready, true);
  assert.equal(b.get('question').hidden, false);
  await b.get('question').children.find(child => child.textContent === 'Allow').onclick();
  assert.deepEqual(b.writes()[0].body.reviewed_request, b.original);
});

test('present but malformed local hello pending inputs cannot retain old approvals', async () => {
  for (const pending_inputs of [null, {id:'not-an-array'}]) {
    const b = await browser();
    const stale = b.get('question').children.find(child => child.textContent === 'Allow');
    const stream = b.sources.at(-1);
    stream.emit({type:'hello', incarnation:'first', pending_inputs_complete:true, pending_inputs});
    assert.equal(stream.closed, true);
    assert.equal(b.context.probe.ready, false);
    assert.equal(b.get('question').hidden, true);
    await stale.onclick();
    assert.equal(b.writes().length, 0);
  }
});
