// SPDX-License-Identifier: AGPL-3.0-only
// Exercise the packaged client without a browser account or network.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
import test from 'node:test';
const code = (await readFile(new URL('../app.mjs', import.meta.url), 'utf8')).replace(/^import .*;\n/gm, '');
class Element {
  children=[]; dataset={}; value=''; hidden=false; disabled=false; scrollHeight=0; scrollTop=0;
  append(...items) { this.children.push(...items); }
  prepend(...items) { this.children.unshift(...items); }
  replaceChildren(...items) { this.children=items; }
  setAttribute() {}
  querySelector() { return this.button ||= new Element(); }
}
const tick=async()=>{for(let i=0;i<30;i++) await Promise.resolve();};
async function extension() {
  const elements=new Map(), get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const original={id:'same-id',kind:'permission',title:'Review A',input_summary:'echo A'};
  const state={session:{id:'host~session',encrypted:true,incarnation:'first',title:'Fixture'},
    history:{ok:true,incarnation:'first',next_seq:10,pending_inputs_complete:true,pending_inputs:[original],turns:[]},fail:false};
  const writes=[],streams=[];
  const client={
    sessions:async()=>[structuredClone(state.session)],
    command:async(id,op,body)=>{
      if(op==='transcript') {if(state.fail)throw new Error('snapshot failed');return structuredClone(state.history);}
      writes.push({id,op,body:structuredClone(body)});return {ok:true};
    },
    events:(id,cursor,signal,emit)=>new Promise(resolve=>{
      streams.push({id,cursor,signal,emit,end:resolve}); signal.addEventListener('abort',resolve,{once:true});
    }),
  };
  const context=vm.createContext({document:{getElementById:get,createElement:()=>new Element(),createDocumentFragment:()=>new Element()},
    AbortController,setInterval:()=>{},setTimeout:()=>{},localStorage:{getItem:()=>''},
    validTarget:id=>id==='host~session',chrome:{},Object,console});
  vm.runInContext(code+'\nglobalThis.probe={selectSession,setClient:value=>client=value,get ready(){return sessionReady}};',context);
  context.probe.setClient(client);await context.probe.selectSession(state.session);await tick();
  return {get,state,writes,streams,original,context,
    allow:()=>get('question').children.find(child=>child.textContent==='Allow'),
    frame:async frame=>{await streams.at(-1).emit(frame);await tick();}};
}
test('packaged approval displays and sends the exact reviewed request and incarnation',async()=>{
  const b=await extension();assert.equal(b.context.probe.ready,true);
  assert.deepEqual(JSON.parse(b.get('question').children.find(child=>child.textContent?.startsWith('{')).textContent),b.original);
  await b.allow().onclick();assert.deepEqual(b.writes[0].body,{id:'same-id',answer:{decision:'allow'},incarnation:'first',reviewed_request:b.original});
});
test('same-ID change rebuilds controls and makes the old callback inert',async()=>{
  const b=await extension(), stale=b.allow();
  const changed={...b.original,title:'Review B',input_summary:'echo B'};
  await b.frame({type:'event',seq:10,event:{type:'needs_input',data:changed}});
  assert.equal(b.get('question').children[0].textContent,'Review B');await stale.onclick();assert.equal(b.writes.length,0);
  await b.allow().onclick();assert.deepEqual(b.writes[0].body.reviewed_request,changed);
});
for (const [name,frame] of [
  ['gap',{type:'event',seq:10,event:{type:'replay_gap'}}],
  ['missing interior event',{type:'event',seq:12,event:{type:'text_delta',data:{text:'lost'}}}],
  ['missing sequence',{type:'event',event:{type:'text_delta'}}],
  ['replacement incarnation',{type:'hello',incarnation:'second'}],
  ['incomplete hello',{type:'hello',incarnation:'first',pending_inputs:[],pending_inputs_complete:false}],
  ['malformed hello',{type:'hello',incarnation:'first',pending_inputs:null,pending_inputs_complete:true}],
]) test(`${name} disables stale controls before failed snapshot reload`,async()=>{
  const b=await extension(),stale=b.allow();b.state.fail=true;b.get('prompt-text').value='retained draft';
  await b.frame(frame);assert.equal(b.streams[0].signal.aborted,true);assert.equal(b.context.probe.ready,false);
  assert.equal(b.get('question').hidden,true);await stale.onclick();
  await b.get('prompt').onsubmit({preventDefault(){}});assert.equal(b.writes.length,0);assert.equal(b.get('prompt-text').value,'retained draft');
});
test('disconnect refreshes approvals before writes resume',async()=>{
  const b=await extension(),stale=b.allow();b.state.history.pending_inputs=[{...b.original,title:'Reconnect B'}];
  b.streams[0].end();await tick();assert.equal(b.context.probe.ready,true);assert.equal(b.streams.length,2);
  await stale.onclick();assert.equal(b.writes.length,0);await b.allow().onclick();
  assert.equal(b.writes[0].body.reviewed_request.title,'Reconnect B');
});
test('local hello replaces or removes snapshot approvals; hub hello needs no pending array',async()=>{
  const b=await extension(),stale=b.allow(),changed={...b.original,title:'Hello B'};
  await b.frame({type:'hello',incarnation:'first'});assert.equal(b.context.probe.ready,true);
  await b.frame({type:'hello',incarnation:'first',pending_inputs:[changed],pending_inputs_complete:true});
  await stale.onclick();assert.equal(b.writes.length,0);assert.equal(b.get('question').children[0].textContent,'Hello B');
  await b.frame({type:'hello',incarnation:'first',pending_inputs:[],pending_inputs_complete:true});assert.equal(b.get('question').hidden,true);
});
test('incomplete snapshot stays read-only and a duplicate event is ignored',async()=>{
  const b=await extension();await b.frame({type:'event',seq:9,event:{type:'needs_input_resolved',data:{id:'same-id'}}});
  assert.equal(b.get('question').hidden,false);b.state.history.pending_inputs_complete=false;
  await b.context.probe.selectSession(b.state.session);assert.equal(b.context.probe.ready,false);assert.equal(b.get('question').hidden,true);
});
test('successful replacement refresh binds prompts and approvals to the new incarnation',async()=>{
  const b=await extension(),stale=b.allow();b.state.session.incarnation='second';b.state.history.incarnation='second';
  await b.frame({type:'hello',incarnation:'second'});await stale.onclick();assert.equal(b.writes.length,0);
  b.get('prompt-text').value='new prompt';await b.get('prompt').onsubmit({preventDefault(){}});
  assert.equal(b.writes[0].body.incarnation,'second');await b.allow().onclick();assert.equal(b.writes[1].body.incarnation,'second');
});
