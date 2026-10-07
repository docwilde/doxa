import {importKey} from './crypto.mjs';
import {DoxaRemoteClient, hubOrigin, validTarget} from './client.mjs';

const el = id => document.getElementById(id);
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
let client = null, active = null, generation = 0, stream = null, currentText = null;
let olderCursor = null, olderLoading = false, currentQuestion = null;
const pending = new Map();

function status(message) { el('status').textContent = message; }
function block(kind, content) {
  const entry = document.createElement('div');
  entry.className = `turn ${kind}`;
  entry.textContent = String(content ?? '').slice(0, 16_000);
  return entry;
}
function line(kind, content) {
  const entry = block(kind, content);
  el('turns').append(entry);
  el('conversation').scrollTop = el('conversation').scrollHeight;
  return entry;
}
function turnBlocks(turn) {
  const entries = [];
  if (turn.prompt) entries.push(block('user', turn.prompt));
  if (turn.text) entries.push(block('assistant', turn.text));
  for (const tool of (Array.isArray(turn.tools) ? turn.tools : []).slice(0, 8))
    entries.push(block('tool', `${tool.name || 'Tool'}${tool.result ? ` · ${tool.result}` : ''}`));
  return entries;
}
function resetSession() {
  generation++;
  stream?.abort(); stream = null; active = null; currentText = null;
  pending.clear(); currentQuestion = null; olderCursor = null; olderLoading = false;
  el('turns').replaceChildren(); el('question').hidden = true; el('older').hidden = true;
  el('prompt').hidden = true; el('prompt-text').value = '';
}

function showNextQuestion() {
  if (currentQuestion) return;
  const next = [...pending.values()].find(item => !item.answered);
  const box = el('question'); box.replaceChildren();
  if (!next || !active || !client) { box.hidden = true; return; }
  currentQuestion = next.id; box.hidden = false;
  const sessionId = active, mine = generation;
  const title = document.createElement('strong');
  title.textContent = String(next.title || next.input_summary || next.tool_name || 'Input needed').slice(0, 500);
  box.append(title);
  const answer = async value => {
    try {
      status('Sending answer…');
      await client.command(sessionId, 'answer', {id:next.id, answer:value});
      if (mine !== generation) return;
      next.answered = true; currentQuestion = null; showNextQuestion();
      status('Answer delivered');
    } catch (error) { if (mine === generation) status(error.message); }
  };
  if (next.kind === 'ask_user') {
    const questions = Array.isArray(next.questions) ? next.questions : [];
    const answers = {};
    let index = 0;
    function step() {
      box.replaceChildren(title);
      if (index >= questions.length) { answer({answers}); return; }
      const question = questions[index];
      const label = document.createElement('p');
      label.textContent = String(question.question || question.header || '').slice(0, 2000);
      box.append(label);
      for (const option of question.options || []) {
        const button = document.createElement('button');
        button.textContent = String(option.label || '').slice(0, 500);
        button.onclick = () => { answers[question.question || ''] = option.label || ''; index++; step(); };
        box.append(button);
      }
      const decline = document.createElement('button'); decline.textContent = 'Decline';
      decline.onclick = () => answer({declined:true}); box.append(decline);
    }
    step();
  } else {
    for (const [label, decision] of [['Allow','allow'],['Deny','deny']]) {
      const button = document.createElement('button'); button.textContent = label;
      button.onclick = () => answer({decision}); box.append(button);
    }
  }
}

function handleFrame(frame) {
  if (frame.type === 'hello') return;
  if (frame.type !== 'event') return;
  const event = frame.event || {}, data = event.data || {};
  switch (event.type) {
    case 'turn_started': currentText = null; if (data.prompt) line('user', data.prompt); break;
    case 'prompt_queued': line('notice', `Queued: ${data.text || 'prompt'}`); break;
    case 'text_delta':
      if (!currentText) currentText = line('assistant', '');
      currentText.textContent += String(data.text || data.delta || '').slice(0, 16_000);
      break;
    case 'turn_done': case 'turn_refused':
      currentText = null;
      if (data.error || data.reason) line('error', data.error || data.reason);
      status('Turn finished'); break;
    case 'needs_input': pending.set(data.id, {...data, answered:false}); showNextQuestion(); status('Input needed'); break;
    case 'needs_input_resolved':
      pending.delete(data.id); if (currentQuestion === data.id) currentQuestion = null;
      showNextQuestion(); break;
    case 'tool_call': line('tool', data.name || data.tool_name || 'Tool'); break;
    case 'tool_result': if (data.result) line('tool', data.result); break;
    case 'replay_gap': line('notice', 'Earlier live events expired. Reopen this session to reload its transcript.'); break;
  }
}

async function followEvents(sessionId, firstCursor, mine, controller) {
  let cursor = firstCursor;
  while (mine === generation && !controller.signal.aborted) {
    try {
      await client.events(sessionId, cursor, controller.signal, async frame => {
        if (mine !== generation) return;
        if (frame.type === 'event' && Number.isSafeInteger(frame.seq))
          cursor = Math.max(cursor, frame.seq + 1);
        handleFrame(frame);
      });
      if (mine === generation) status('Reconnecting to events…');
    } catch (error) {
      if (controller.signal.aborted || mine !== generation) return;
      status(`Events: ${error.message}`);
    }
    if (mine === generation && !controller.signal.aborted) await pause(1500);
  }
}

async function selectSession(session) {
  if (!client || !session.encrypted || !validTarget(session.id)) return;
  resetSession();
  const mine = generation, sessionId = session.id;
  active = sessionId;
  for (const button of el('sessions').children)
    button.setAttribute('aria-current', String(button.dataset.sessionId === sessionId));
  status(`${session.title || sessionId} · loading`);
  try {
    const history = await client.command(sessionId, 'transcript', {});
    if (mine !== generation) return;
    if (!Array.isArray(history.turns) || history.turns.length > 80 ||
        !Array.isArray(history.pending_inputs) || history.pending_inputs.length > 64 ||
        !Number.isSafeInteger(history.next_seq)) throw new Error('Invalid remote transcript');
    for (const item of history.pending_inputs) if (item.id) pending.set(item.id, {...item, answered:false});
    showNextQuestion();
    for (const turn of history.turns)
      for (const entry of turnBlocks(turn)) el('turns').append(entry);
    el('conversation').scrollTop = el('conversation').scrollHeight;
    olderCursor = history.has_more && Number.isSafeInteger(history.before) ? history.before : null;
    el('older').hidden = olderCursor === null;
    el('prompt').hidden = false;
    status(`${session.title || sessionId} · connected`);
    stream = new AbortController();
    void followEvents(sessionId, history.next_seq, mine, stream);
  } catch (error) { if (mine === generation) status(error.message); }
}

async function loadSessions() {
  if (!client) return;
  try {
    const sessions = await client.sessions();
    const nav = el('sessions'); nav.replaceChildren();
    for (const session of sessions) {
      const button = document.createElement('button');
      button.textContent = `${session.encrypted ? '🔒' : '○'} ${session.title || session.id} · ${session.engine || 'session'}`;
      button.dataset.sessionId = session.id;
      button.disabled = !session.encrypted;
      button.title = session.encrypted ? '' : 'This session is not using encrypted transport';
      button.setAttribute('aria-current', String(session.id === active));
      button.onclick = () => selectSession(session);
      nav.append(button);
    }
    if (active && !sessions.some(session => session.id === active && session.encrypted)) resetSession();
    if (!active) {
      const first = sessions.find(session => session.encrypted);
      if (first) await selectSession(first);
      else status(sessions.length ? 'No encrypted sessions on this hub' : 'No live sessions');
    }
  } catch (error) { status(error.message); }
}

el('setup').onsubmit = async event => {
  event.preventDefault();
  const button = el('setup').querySelector('button');
  button.disabled = true;
  try {
    const origin = hubOrigin(el('hub').value.trim());
    // The permission prompt must originate in this user gesture.
    if (!await chrome.permissions.request({origins:[`${origin}/*`]}))
      throw new Error('Hub permission was not granted');
    const file = el('key-file').files[0];
    if (!file || file.size > 128) throw new Error('Choose a DOXA remote key file');
    const key = await importKey(await file.text());
    el('key-file').value = '';
    resetSession();
    client = new DoxaRemoteClient(`${origin}/`, key);
    await client.sessions();
    localStorage.setItem('doxaRemoteHub', `${origin}/`);
    await loadSessions();
    el('setup').hidden = true; el('disconnect').hidden = false;
  } catch (error) { client = null; status(error.message); }
  finally { button.disabled = false; }
};
el('disconnect').onclick = () => {
  resetSession(); client = null;
  el('sessions').replaceChildren(); el('setup').hidden = false; el('disconnect').hidden = true;
  status('Disconnected; key cleared from this tab');
};
el('older').onclick = async () => {
  if (!client || !active || olderCursor === null || olderLoading) return;
  const mine = generation, before = olderCursor, button = el('older');
  olderLoading = true; button.disabled = true;
  try {
    const history = await client.command(active, 'transcript', {before});
    if (mine !== generation) return;
    if (!Array.isArray(history.turns) || !Number.isSafeInteger(history.before) || history.before >= before)
      throw new Error('Invalid history page');
    const viewport = el('conversation'), turns = el('turns');
    const oldHeight = viewport.scrollHeight, oldTop = viewport.scrollTop;
    const fragment = document.createDocumentFragment();
    for (const turn of history.turns)
      for (const entry of turnBlocks(turn)) fragment.append(entry);
    turns.prepend(fragment);
    viewport.scrollTop = oldTop + viewport.scrollHeight - oldHeight;
    olderCursor = history.has_more ? history.before : null;
    button.hidden = olderCursor === null;
    status(olderCursor === null ? 'Beginning of transcript' : 'Older turns loaded');
  } catch (error) { if (mine === generation) status(error.message); }
  finally { if (mine === generation) { olderLoading = false; button.disabled = false; } }
};
el('prompt').onsubmit = async event => {
  event.preventDefault();
  const text = el('prompt-text').value.trim();
  if (!client || !active || !text) return;
  const sessionId = active, mine = generation, button = el('prompt').querySelector('button');
  button.disabled = true; status('Sending prompt…');
  try {
    await client.command(sessionId, 'prompt', {text});
    if (mine === generation) { el('prompt-text').value = ''; status('Prompt delivered'); }
  } catch (error) { if (mine === generation) status(error.message); }
  finally { button.disabled = false; }
};
el('hub').value = localStorage.getItem('doxaRemoteHub') || '';
setInterval(() => { if (client) void loadSessions(); }, 30_000);
