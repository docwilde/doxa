const el = id => document.getElementById(id);
let source = null, active = null, generation = 0, currentText = null;
const pending = new Map();
const pendingPrompts = new Map();
let currentQuestion = null;
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function confirmed(response) {
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || 'Operation refused');
  if (!value.command_id) return value;
  for (let attempt = 0; attempt < 240; attempt++) {
    await pause(250);
    const status = await fetch(`/api/commands/${encodeURIComponent(value.command_id)}`, {cache:'no-store'});
    if (!status.ok) throw new Error('Command status unavailable; inspect the session before retrying');
    const command = await status.json();
    if (command.status === 'accepted') return command.result;
    if (command.status === 'refused') throw new Error(command.result?.error || 'Host refused command');
    if (command.status === 'expired') throw new Error('Command outcome uncertain; inspect the session before retrying');
  }
  throw new Error('Command acknowledgement timed out; inspect the session before retrying');
}

function line(kind, content) {
  const block = document.createElement('div');
  block.className = 'turn ' + kind;
  block.textContent = String(content ?? '');
  el('conversation').append(block);
  block.scrollIntoView({block: 'end'});
  return block;
}
function announce(title) {
  if (!('Notification' in window)) return;
  if (document.visibilityState === 'visible' || Notification.permission !== 'granted') return;
  try { new Notification(title, {body: 'Open DOXA to review the session', tag: active || 'doxa'}); } catch {}
}
el('notify').onclick = async () => {
  if ('Notification' in window && Notification.permission === 'default') await Notification.requestPermission();
  el('notify').textContent = Notification.permission === 'granted' ? 'Alerts on' : 'Notifications';
};
if (!('Notification' in window)) el('notify').hidden = true;

async function loadSessions() {
  try {
    const response = await fetch('/api/sessions', {cache:'no-store'});
    if (!response.ok) throw new Error('Access refused');
    const sessions = (await response.json()).sessions;
    const nav = el('sessions'); nav.replaceChildren();
    for (const session of sessions) {
      const button = document.createElement('button');
      button.textContent = `${session.title} · ${session.engine || 'session'}`;
      button.dataset.sessionId = session.id;
      button.setAttribute('aria-current', String(session.id === active));
      button.onclick = () => selectSession(session);
      nav.append(button);
    }
    if (!sessions.length) {
      source?.close(); source = null; active = null;
      el('status').textContent = 'No live sessions';
    } else if (!sessions.some(session => session.id === active)) await selectSession(sessions[0]);
  } catch (error) { el('status').textContent = error.message || 'Disconnected'; }
}
function showNextQuestion() {
  if (currentQuestion) return;
  const next = [...pending.values()].find(item => !item.answered);
  const box = el('question'); box.replaceChildren();
  if (!next) { box.hidden = true; return; }
  currentQuestion = next.id; box.hidden = false;
  const sessionId = active;
  const title = document.createElement('strong');
  title.textContent = next.title || next.input_summary || next.tool_name || 'Input needed';
  box.append(title);
  const answer = async value => {
    try {
      const encoded = JSON.stringify(value);
      const requestId = next.uncertain?.encoded === encoded ? next.uncertain.id : crypto.randomUUID();
      next.uncertain = {encoded, id: requestId};
      const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/answer`, {
        method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id:next.id,answer:value,request_id:requestId})
      });
      await confirmed(response);
      if (active !== sessionId) return;
      next.answered = true; currentQuestion = null; showNextQuestion();
    } catch (error) { line('error',error.message); }
  };
  if (next.kind === 'ask_user') {
    const questions = next.questions || [], answers = {};
    let index = 0;
    function step() {
      box.replaceChildren(title);
      if (index >= questions.length) { answer({answers}); return; }
      const question = questions[index];
      const label = document.createElement('p');
      label.textContent = question.question || question.header || '';
      box.append(label);
      for (const option of question.options || []) {
        const button = document.createElement('button'); button.textContent = option.label || '';
        button.onclick = () => { answers[question.question || ''] = option.label || ''; index++; step(); };
        box.append(button);
      }
      const decline = document.createElement('button'); decline.textContent = 'Decline';
      decline.onclick = () => answer({declined:true}); box.append(decline);
    }
    step();
  } else {
    for (const [label,decision] of [['Allow','allow'],['Deny','deny']]) {
      const button = document.createElement('button'); button.textContent = label;
      button.onclick = () => answer({decision}); box.append(button);
    }
  }
}
function handle(frame) {
  if (frame.type === 'hello') {
    el('status').textContent = `${frame.engine || 'session'} · ${frame.model || 'default'}`;
    for (const item of frame.pending_inputs || []) pending.set(item.id,{...item,answered:false});
    showNextQuestion(); return;
  }
  if (frame.type !== 'event') return;
  const event = frame.event || {}, data = event.data || {};
  switch (event.type) {
    case 'turn_started': currentText = null; if (data.prompt) line('user',data.prompt); break;
    case 'prompt_queued': line('notice',`Queued: ${data.text || 'prompt'}`); break;
    case 'text_delta': if (!currentText) currentText = line('assistant',''); currentText.textContent += data.text || data.delta || ''; break;
    case 'turn_done': case 'turn_refused':
      currentText = null; if (data.error || data.reason) line('error',data.error || data.reason);
      announce('DOXA turn finished'); break;
    case 'needs_input': pending.set(data.id,{...data,answered:false}); showNextQuestion(); announce('DOXA needs input'); break;
    case 'needs_input_resolved': pending.delete(data.id); if (currentQuestion === data.id) currentQuestion = null; showNextQuestion(); break;
    case 'tool_call': line('tool',data.name || data.tool_name || 'Tool'); break;
    case 'tool_result': if (data.result) line('tool',data.result); break;
    case 'replay_gap': line('notice','Earlier live events expired; reload the session for its transcript'); break;
  }
}
async function selectSession(session) {
  const mine = ++generation;
  source?.close(); source = null; active = session.id; currentText = null;
  el('prompt-text').value = '';
  pending.clear(); currentQuestion = null;
  el('conversation').replaceChildren(); el('question').hidden = true;
  for (const button of el('sessions').children)
    button.setAttribute('aria-current', String(button.dataset.sessionId === session.id));
  el('status').textContent = `${session.title} · loading`;
  let cursor = null;
  try {
    const response = await fetch(`/api/sessions/${encodeURIComponent(session.id)}/transcript`,{
      method:'POST', headers:{'Content-Type':'application/json'}, body:'{}', cache:'no-store'
    });
    if (!response.ok) throw new Error('Transcript unavailable');
    const history = await confirmed(response); if (mine !== generation) return;
    if (history.dropped_turns) line('notice',`${history.dropped_turns} earlier turns omitted`);
    for (const item of history.pending_inputs || []) pending.set(item.id,{...item,answered:false});
    showNextQuestion();
    for (const turn of history.turns || []) {
      if (turn.prompt) line('user',turn.prompt);
      if (turn.text) line('assistant',turn.text);
      for (const tool of turn.tools || []) line('tool',`${tool.name || 'Tool'}${tool.result ? ' · ' + tool.result : ''}`);
    }
    cursor = history.next_seq;
  } catch (error) { if (mine !== generation) return; line('error',error.message); }
  if (mine !== generation) return;
  const query = Number.isSafeInteger(cursor) ? `?cursor=${cursor}` : '';
  source = new EventSource(`/api/sessions/${encodeURIComponent(session.id)}/events${query}`);
  source.onmessage = message => { if (mine === generation) { try { handle(JSON.parse(message.data)); } catch {} } };
  source.onerror = () => { if (mine === generation) el('status').textContent = `${session.title} · reconnecting`; };
}
el('prompt').onsubmit = async event => {
  event.preventDefault();
  const field = el('prompt-text'), text = field.value.trim();
  if (!text || !active) return;
  const sessionId = active;
  const previous = pendingPrompts.get(sessionId);
  const requestId = previous?.text === text ? previous.id : crypto.randomUUID();
  pendingPrompts.set(sessionId,{text,id:requestId});
  try {
    const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/prompt`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,request_id:requestId})
    });
    await confirmed(response);
    pendingPrompts.delete(sessionId);
    if (active === sessionId) field.value = '';
  } catch (error) { line('error',error.message); }
};
loadSessions(); setInterval(loadSessions,30000);
