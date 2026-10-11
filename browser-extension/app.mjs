import {importKey} from './crypto.mjs';
import {DoxaRemoteClient, hubOrigin, validTarget} from './client.mjs';

const el = id => document.getElementById(id);
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
let client = null, active = null, generation = 0, stream = null, currentText = null;
let olderCursor = null, olderLoading = false, currentQuestion = null;
let sessionReady = false, activeIncarnation = null;
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
function invalidateControls() {
  sessionReady = false; pending.clear(); currentQuestion = null;
  el('question').replaceChildren(); el('question').hidden = true; el('prompt').hidden = true;
}
function resetSession(keepDraft = false) {
  generation++;
  stream?.abort(); stream = null; active = null; currentText = null;
  invalidateControls(); activeIncarnation = null; olderCursor = null; olderLoading = false;
  el('turns').replaceChildren(); el('question').hidden = true; el('older').hidden = true;
  el('prompt').hidden = true; if (!keepDraft) el('prompt-text').value = '';
}

function showNextQuestion() {
  if (!sessionReady || currentQuestion) return;
  const next = [...pending.values()].find(item => !item.answered);
  const box = el('question'); box.replaceChildren();
  if (!next || !active || !client) { box.hidden = true; return; }
  currentQuestion = next.id; box.hidden = false;
  const sessionId = active, mine = generation;
  const title = document.createElement('strong');
  title.textContent = String(next.title || next.input_summary || next.tool_name || 'Input needed').slice(0, 500);
  const reviewed = document.createElement('pre');
  reviewed.textContent = JSON.stringify(next.reviewed, null, 2);
  box.append(title, reviewed);
  const answer = async value => {
    if (!sessionReady || mine !== generation || active !== sessionId || pending.get(next.id) !== next) return;
    try {
      status('Sending answer…');
      await client.command(sessionId, 'answer', {id:next.id, answer:value, incarnation:activeIncarnation, reviewed_request:next.reviewed});
      if (mine !== generation || pending.get(next.id) !== next) return;
      next.answered = true; currentQuestion = null; showNextQuestion();
      status('Answer delivered');
    } catch (error) { if (mine === generation) status(error.message); }
  };
  if (next.kind === 'ask_user') {
    const questions = Array.isArray(next.questions) ? next.questions : [];
    const answers = {};
    let index = 0;
    function step() {
      box.replaceChildren(title, reviewed);
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
  if (frame.type === 'hello') {
    if (Object.hasOwn(frame, 'pending_inputs')) {
      pending.clear(); currentQuestion = null;
      for (const item of frame.pending_inputs) if (item.id) pending.set(item.id, {...item, reviewed:item, answered:false});
      showNextQuestion();
    }
    return;
  }
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
    case 'needs_input':
      pending.set(data.id, {...data, reviewed:data, answered:false});
      if (currentQuestion === data.id) currentQuestion = null;
      showNextQuestion(); status('Input needed'); break;
    case 'needs_input_resolved':
      pending.delete(data.id); if (currentQuestion === data.id) currentQuestion = null;
      showNextQuestion(); break;
    case 'tool_call': line('tool', data.name || data.tool_name || 'Tool'); break;
    case 'tool_result': if (data.result) line('tool', data.result); break;
    case 'replay_gap': line('notice', 'Earlier live events expired. Reopen this session to reload its transcript.'); break;
  }
}

function recoverSession(sessionId, mine, message) {
  if (mine !== generation || active !== sessionId) return;
  stream?.abort(); generation++; invalidateControls();
  status(message);
  // Disable synchronously; inventory/snapshot failures leave the tab read-only.
  void (async () => {
    try {
      const sessions = await client.sessions();
      if (generation !== mine + 1 || active !== sessionId) return;
      const session = sessions.find(item => item.id === sessionId && item.encrypted);
      if (!session) throw new Error('Remote session offline');
      await selectSession(session, true);
    } catch (error) { if (generation === mine + 1) status(error.message); }
  })();
}
async function followEvents(sessionId, firstCursor, mine, controller) {
  let cursor = firstCursor;
  try {
    await client.events(sessionId, cursor, controller.signal, async frame => {
      if (mine !== generation || !sessionReady) return;
      if (frame.type === 'hello') {
        if (frame.incarnation !== activeIncarnation || (Object.hasOwn(frame, 'pending_inputs') &&
            (!Array.isArray(frame.pending_inputs) || frame.pending_inputs.length > 64 || frame.pending_inputs_complete !== true))) {
          recoverSession(sessionId, mine, 'Session changed; refreshing review'); return;
        }
      } else if (frame.type === 'event') {
        if (frame.event?.type === 'replay_gap' || !Number.isSafeInteger(frame.seq) || frame.seq < 0 || frame.seq > cursor) {
          recoverSession(sessionId, mine, 'Events changed; refreshing review'); return;
        }
        if (frame.seq < cursor) return;
        if (frame.seq === Number.MAX_SAFE_INTEGER) {
          recoverSession(sessionId, mine, 'Invalid event cursor'); return;
        }
        cursor = frame.seq + 1;
      }
      handleFrame(frame);
    });
    if (!controller.signal.aborted) recoverSession(sessionId, mine, 'Reconnecting; refreshing review');
  } catch (error) {
    if (!controller.signal.aborted) recoverSession(sessionId, mine, `Events: ${error.message}`);
  }
}

async function selectSession(session, keepDraft = false) {
  if (!client || !session.encrypted || !validTarget(session.id) || typeof session.incarnation !== 'string' || !session.incarnation || session.incarnation.length > 128) return;
  resetSession(keepDraft);
  const mine = generation, sessionId = session.id;
  active = sessionId; activeIncarnation = session.incarnation;
  for (const button of el('sessions').children)
    button.setAttribute('aria-current', String(button.dataset.sessionId === sessionId));
  status(`${session.title || sessionId} · loading`);
  try {
    const history = await client.command(sessionId, 'transcript', {});
    if (mine !== generation) return;
    if (history.ok === false || history.incarnation !== activeIncarnation || history.pending_inputs_complete !== true || !Array.isArray(history.turns) || history.turns.length > 80 ||
        !Array.isArray(history.pending_inputs) || history.pending_inputs.length > 64 ||
        !Number.isSafeInteger(history.next_seq) || history.next_seq < 0 || history.next_seq === Number.MAX_SAFE_INTEGER) throw new Error('Invalid remote transcript');
    const inventory = await client.sessions();
    if (mine !== generation) return;
    if (!inventory.some(item => item.id === sessionId && item.encrypted && item.incarnation === activeIncarnation))
      throw new Error('Session changed while loading');
    sessionReady = true;
    for (const item of history.pending_inputs) if (item.id) pending.set(item.id, {...item, reviewed:item, answered:false});
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
    if (active) {
      const selected = sessions.find(session => session.id === active && session.encrypted);
      if (!selected) resetSession(true);
      else if (selected.incarnation !== activeIncarnation) await selectSession(selected, true);
    }
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
    if (history.ok === false || history.incarnation !== activeIncarnation || history.pending_inputs_complete !== true || !Array.isArray(history.turns) || !Number.isSafeInteger(history.before) || history.before >= before)
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
  if (!client || !active || !sessionReady || !text) return;
  const sessionId = active, mine = generation, button = el('prompt').querySelector('button');
  button.disabled = true; status('Sending prompt…');
  try {
    await client.command(sessionId, 'prompt', {text, incarnation:activeIncarnation});
    if (mine === generation) { el('prompt-text').value = ''; status('Prompt delivered'); }
  } catch (error) { if (mine === generation) status(error.message); }
  finally { button.disabled = false; }
};
el('hub').value = localStorage.getItem('doxaRemoteHub') || '';
setInterval(() => { if (client) void loadSessions(); }, 30_000);
