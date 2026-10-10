const el = id => document.getElementById(id);
let source = null, active = null, generation = 0, currentText = null;
let activeIncarnation = null, sessionReady = false, eventCursor = null;
let olderCursor = null, olderLoading = false;
const pending = new Map();
const pendingPrompts = new Map();
let currentQuestion = null;
let backgroundAlerts = false;
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
function invalidateControls() {
  sessionReady = false;
  pending.clear(); currentQuestion = null;
  el('question').replaceChildren(); el('question').hidden = true;
  el('prompt').querySelector('button').disabled = true;
}
invalidateControls();
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

function block(kind, content) {
  const entry = document.createElement('div');
  entry.className = 'turn ' + kind;
  entry.textContent = String(content ?? '');
  return entry;
}
function line(kind, content) {
  const entry = block(kind, content);
  el('turns').append(entry);
  entry.scrollIntoView({block: 'end'});
  return entry;
}
function turnBlocks(turn) {
  const entries = [];
  if (turn.prompt) entries.push(block('user', turn.prompt));
  if (turn.text) entries.push(block('assistant', turn.text));
  for (const tool of turn.tools || [])
    entries.push(block('tool', `${tool.name || 'Tool'}${tool.result ? ' · ' + tool.result : ''}`));
  return entries;
}
function announce(title) {
  if (backgroundAlerts) return;
  if (!('Notification' in window)) return;
  if (document.visibilityState === 'visible' || Notification.permission !== 'granted') return;
  try { new Notification(title, {body: 'Open DOXA to review the session', tag: active || 'doxa'}); } catch {}
}
function publicKeyBytes(encoded) {
  const base64 = encoded.replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base64.padEnd(Math.ceil(base64.length / 4) * 4, '='));
  return Uint8Array.from(raw, char => char.charCodeAt(0));
}
async function pushConfig() {
  const response = await fetch('/api/push/config', {cache:'no-store'});
  if (!response.ok) throw new Error('Push configuration unavailable');
  return response.json();
}
function foregroundAlerts(button) {
  button.textContent = Notification.permission === 'granted' ? 'Alerts in tab' : 'Notifications';
  button.onclick = async () => {
    if (Notification.permission === 'default') await Notification.requestPermission();
    button.textContent = Notification.permission === 'granted' ? 'Alerts in tab' : 'Notifications';
  };
}
async function setupAlerts(clicked = false) {
  const button = el('notify');
  if (!('Notification' in window)) { button.hidden = true; return; }
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    foregroundAlerts(button); return;
  }
  // Ask while the click still has user activation; registration and network
  // requests below can outlive the browser's permission-prompt gesture.
  if (clicked && Notification.permission === 'default') {
    await Notification.requestPermission();
  }
  let config;
  try { config = await pushConfig(); }
  catch { foregroundAlerts(button); return; }
  if (!config.enabled) { foregroundAlerts(button); return; }
  try {
    if (!clicked && Notification.permission !== 'granted') {
      button.textContent = 'Enable background alerts'; return;
    }
    const registration = await navigator.serviceWorker.register('/remote-sw.js', {scope:'/'});
    let subscription = await registration.pushManager.getSubscription();
    if (clicked && subscription) {
      const endpoint = subscription.endpoint;
      await subscription.unsubscribe();
      await fetch('/api/push/subscriptions', {method:'DELETE',
        headers:{'Content-Type':'application/json'}, body:JSON.stringify({endpoint})});
      backgroundAlerts = false; button.textContent = 'Enable background alerts'; return;
    }
    if (Notification.permission !== 'granted') {
      button.textContent = 'Notifications denied'; return;
    }
    if (!subscription) subscription = await registration.pushManager.subscribe({
      userVisibleOnly:true, applicationServerKey:publicKeyBytes(config.public_key)
    });
    const response = await fetch('/api/push/subscriptions', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(subscription.toJSON())});
    if (!response.ok) throw new Error('Subscription refused');
    backgroundAlerts = true; button.textContent = 'Background alerts on';
  } catch {
    backgroundAlerts = false; button.textContent = 'Enable background alerts';
  }
}
el('notify').onclick = () => setupAlerts(true);
setupAlerts();

async function loadSessions() {
  try {
    const response = await fetch('/api/sessions', {cache:'no-store'});
    if (!response.ok) throw new Error('Access refused');
    const sessions = (await response.json()).sessions;
    if (sessions.some(session => session.id === active && session.encrypted)) {
      source?.close(); source = null; active = null;
      generation++; invalidateControls();
      el('turns').replaceChildren();
    }
    const nav = el('sessions'); nav.replaceChildren();
    for (const session of sessions) {
      const button = document.createElement('button');
      button.textContent = `${session.encrypted ? '🔒 ' : ''}${session.title} · ${session.engine || 'session'}`;
      button.dataset.sessionId = session.id;
      button.setAttribute('aria-current', String(session.id === active));
      button.onclick = () => session.encrypted
        ? (el('status').textContent = 'Encrypted session: use the native DOXA TUI with its shared key')
        : selectSession(session);
      nav.append(button);
    }
    if (!sessions.length) {
      source?.close(); source = null; active = null;
      generation++; invalidateControls();
      el('status').textContent = 'No live sessions';
    } else if (!sessions.some(session => session.id === active)) {
      const usable = sessions.find(session => !session.encrypted);
      if (usable) await selectSession(usable);
      else el('status').textContent = 'Encrypted sessions require a separately trusted native client';
    } else if (sessionReady && sessions.find(session => session.id === active)?.incarnation !== activeIncarnation) {
      await selectSession(sessions.find(session => session.id === active), true);
    }
  } catch (error) { el('status').textContent = error.message || 'Disconnected'; }
}
function showNextQuestion() {
  if (!sessionReady) return;
  if (currentQuestion) return;
  const next = [...pending.values()].find(item => !item.answered);
  const box = el('question'); box.replaceChildren();
  if (!next) { box.hidden = true; return; }
  currentQuestion = next.id; box.hidden = false;
  const sessionId = active;
  const mine = generation;
  const title = document.createElement('strong');
  title.textContent = next.title || next.input_summary || next.tool_name || 'Input needed';
  box.append(title);
  const answer = async value => {
    if (!sessionReady || mine !== generation || active !== sessionId || pending.get(next.id) !== next) return;
    try {
      const encoded = JSON.stringify(value);
      const requestId = next.uncertain?.encoded === encoded ? next.uncertain.id : crypto.randomUUID();
      next.uncertain = {encoded, id: requestId};
      const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/answer`, {
        method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id:next.id,answer:value,
          request_id:requestId,incarnation:activeIncarnation,reviewed_request:next.reviewed})
      });
      await confirmed(response);
      if (mine !== generation || active !== sessionId) return;
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
    if (Array.isArray(frame.pending_inputs)) {
      pending.clear(); currentQuestion = null;
      for (const item of frame.pending_inputs) pending.set(item.id,{...item,reviewed:item,answered:false});
    }
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
    case 'needs_input':
      pending.set(data.id,{...data,reviewed:data,answered:false});
      if (currentQuestion === data.id) currentQuestion = null;
      showNextQuestion(); announce('DOXA needs input'); break;
    case 'needs_input_resolved': pending.delete(data.id); if (currentQuestion === data.id) currentQuestion = null; showNextQuestion(); break;
    case 'tool_call': line('tool',data.name || data.tool_name || 'Tool'); break;
    case 'tool_result': if (data.result) line('tool',data.result); break;
    case 'replay_gap': line('notice','Earlier live events expired; reload the session for its transcript'); break;
  }
}
async function loadOlder() {
  if (!active || olderCursor === null || olderLoading) return;
  const mine = generation, sessionId = active, before = olderCursor;
  const button = el('older');
  olderLoading = true; button.disabled = true; button.textContent = 'Loading earlier turns…';
  try {
    const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/transcript`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({before}), cache:'no-store'
    });
    const history = await confirmed(response);
    if (mine !== generation) return;
    if (!Number.isSafeInteger(history.before) || history.before >= before)
      throw new Error('Invalid history cursor');
    const viewport = el('conversation'), turns = el('turns');
    const height = viewport.scrollHeight, top = viewport.scrollTop;
    const fragment = document.createDocumentFragment();
    for (const turn of history.turns || [])
      for (const entry of turnBlocks(turn)) fragment.append(entry);
    turns.prepend(fragment);
    viewport.scrollTop = top + viewport.scrollHeight - height;
    olderCursor = history.has_more ? history.before : null;
    button.hidden = olderCursor === null;
    button.title = '';
  } catch (error) {
    if (mine === generation) button.title = error.message;
  } finally {
    if (mine === generation) {
      olderLoading = false; button.disabled = false;
      button.textContent = button.title ? 'Retry earlier turns' : 'Load earlier turns';
    }
  }
}
async function refreshSession(session, mine) {
  if (mine !== generation) return;
  const refresh = ++generation;
  source?.close(); source = null;
  invalidateControls();
  el('status').textContent = `${session.title} · refreshing transcript`;
  try {
    await pause(1500);
    if (refresh !== generation) return;
    const response = await fetch('/api/sessions', {cache:'no-store'});
    if (!response.ok) throw new Error('Session inventory unavailable');
    const current = (await response.json()).sessions.find(item => item.id === session.id && !item.encrypted);
    if (refresh !== generation) return;
    if (!current) throw new Error('Session is no longer available');
    await selectSession(current, true);
  } catch (error) {
    if (refresh === generation) el('status').textContent = `${error.message}; select the session to retry`;
  }
}
async function selectSession(session, keepDraft = false) {
  const mine = ++generation;
  source?.close(); source = null; active = session.id; currentText = null;
  activeIncarnation = session.incarnation; eventCursor = null;
  invalidateControls();
  olderCursor = null; olderLoading = false;
  if (!keepDraft) el('prompt-text').value = '';
  el('turns').replaceChildren(); el('older').hidden = true;
  el('older').title = ''; el('question').hidden = true;
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
    if (history.ok === false || history.pending_inputs_complete !== true || !Array.isArray(history.pending_inputs)
        || !Number.isSafeInteger(history.next_seq) || history.next_seq < 0
        || !activeIncarnation || history.incarnation !== activeIncarnation)
      throw new Error('Complete current session snapshot required');
    const inventory = await fetch('/api/sessions', {cache:'no-store'});
    if (!inventory.ok) throw new Error('Session inventory unavailable');
    const current = (await inventory.json()).sessions.find(item => item.id === session.id);
    if (mine !== generation) return;
    if (!current || current.encrypted || current.incarnation !== activeIncarnation)
      throw new Error('Session changed while loading transcript');
    sessionReady = true;
    el('prompt').querySelector('button').disabled = false;
    for (const item of history.pending_inputs) pending.set(item.id,{...item,reviewed:item,answered:false});
    showNextQuestion();
    for (const turn of history.turns || [])
      for (const entry of turnBlocks(turn)) el('turns').append(entry);
    el('conversation').scrollTop = el('conversation').scrollHeight;
    olderCursor = history.has_more && Number.isSafeInteger(history.before) ? history.before : null;
    el('older').hidden = olderCursor === null;
    cursor = history.next_seq;
  } catch (error) {
    if (mine !== generation) return;
    invalidateControls(); line('error',error.message);
    el('status').textContent = `${session.title} · snapshot unavailable; select the session to retry`;
    return;
  }
  if (mine !== generation) return;
  const query = Number.isSafeInteger(cursor) ? `?cursor=${cursor}` : '';
  source = new EventSource(`/api/sessions/${encodeURIComponent(session.id)}/events${query}`);
  eventCursor = cursor;
  source.onmessage = message => {
    if (mine !== generation || !sessionReady) return;
    try {
      const frame = JSON.parse(message.data);
      if (frame.type === 'hello' && frame.incarnation !== activeIncarnation)
        return void refreshSession(session, mine);
      if (frame.type === 'hello' && frame.pending_inputs !== undefined
          && (!Array.isArray(frame.pending_inputs) || frame.pending_inputs_complete !== true))
        return void refreshSession(session, mine);
      if (frame.type === 'event') {
        if (frame.event?.type === 'replay_gap' || !Number.isSafeInteger(frame.seq) || frame.seq < 0 || frame.seq === Number.MAX_SAFE_INTEGER)
          return void refreshSession(session, mine);
        if (frame.seq < eventCursor) return;
        if (frame.seq !== eventCursor) return void refreshSession(session, mine);
        eventCursor = frame.seq + 1;
      }
      handle(frame);
    } catch { void refreshSession(session, mine); }
  };
  source.onerror = () => { void refreshSession(session, mine); };
}
el('older').onclick = loadOlder;
el('prompt').onsubmit = async event => {
  event.preventDefault();
  const field = el('prompt-text'), text = field.value.trim();
  if (!text || !active || !sessionReady) return;
  const sessionId = active;
  const mine = generation;
  const previous = pendingPrompts.get(sessionId);
  const requestId = previous?.text === text ? previous.id : crypto.randomUUID();
  pendingPrompts.set(sessionId,{text,id:requestId});
  try {
    const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/prompt`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,request_id:requestId,incarnation:activeIncarnation})
    });
    await confirmed(response);
    pendingPrompts.delete(sessionId);
    if (mine === generation && active === sessionId) field.value = '';
  } catch (error) { line('error',error.message); }
};
loadSessions(); setInterval(loadSessions,30000);
