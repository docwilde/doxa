// SPDX-License-Identifier: AGPL-3.0-only
const element = id => document.getElementById(id);
let socket = null;
let active = null;
let generation = 0;
let currentText = null;
let currentQuestion = null;
const pendingQuestions = new Map();

function line(kind, content) {
  const block = document.createElement('div');
  block.className = 'turn ' + kind;
  block.textContent = content;
  element('conversation').append(block);
  block.scrollIntoView({block: 'end'});
  return block;
}

async function loadSessions() {
  let response;
  try {
    response = await fetch('/api/sessions');
  } catch {
    element('status').textContent = 'Disconnected';
    return;
  }
  if (!response.ok) {
    element('status').textContent = 'Access refused';
    return;
  }
  const sessions = (await response.json()).sessions;
  const nav = element('sessions');
  nav.replaceChildren();
  for (const session of sessions) {
    const button = document.createElement('button');
    button.textContent = session.title + ' · ' + (session.engine || 'session');
    button.dataset.sessionId = session.id;
    button.setAttribute('aria-current', String(session.id === active));
    button.onclick = () => selectSession(session);
    nav.append(button);
  }
  if (!sessions.length) {
    if (socket) socket.close();
    socket = null;
    active = null;
    element('status').textContent = 'No live sessions';
  } else if (!sessions.some(session => session.id === active)) {
    await selectSession(sessions[0]);
  }
}

async function selectSession(session) {
  const mine = ++generation;
  if (socket) socket.close();
  socket = null;
  active = session.id;
  currentText = null;
  currentQuestion = null;
  pendingQuestions.clear();
  element('conversation').replaceChildren();
  element('question').hidden = true;
  for (const button of element('sessions').children) {
    button.setAttribute('aria-current', String(button.dataset.sessionId === session.id));
  }
  element('status').textContent = session.title + ' · ' + (session.model || 'default');
  try {
    const response = await fetch('/api/sessions/' + encodeURIComponent(session.id) + '/transcript');
    if (mine !== generation) return;
    if (response.ok) {
      const history = await response.json();
      if (mine !== generation) return;
      if (history.dropped_turns) line('notice', history.dropped_turns + ' earlier turns omitted');
      for (const turn of history.turns) {
        line('user', turn.prompt);
        if (turn.text) line('assistant', turn.text);
        for (const tool of turn.tools) {
          line('tool', tool.name + (tool.result ? ' · ' + tool.result : ''));
        }
      }
    }
  } catch {
    if (mine !== generation) return;
    line('error', 'Could not load the transcript');
  }
  if (mine !== generation) return;
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const connection = new WebSocket(protocol + '//' + location.host +
    '/api/sessions/' + encodeURIComponent(session.id) + '/events');
  socket = connection;
  connection.onmessage = event => {
    if (socket === connection && active === session.id) handle(JSON.parse(event.data));
  };
  connection.onclose = event => {
    if (socket !== connection) return;
    socket = null;
    if (event.code === 1008) {
      element('status').textContent = 'Access refused';
      return;
    }
    element('status').textContent = session.title + ' · reconnecting';
    setTimeout(() => {
      if (active === session.id && generation === mine) selectSession(session);
    }, 3000);
  };
  connection.onerror = () => {
    if (socket === connection) line('error', 'Connection error');
  };
}

function showQuestion(data) {
  currentQuestion = data.id;
  const box = element('question');
  box.replaceChildren();
  box.hidden = false;
  const heading = document.createElement('div');
  heading.textContent = data.title || data.input_summary || data.tool_name || 'Input needed';
  box.append(heading);
  const answer = payload => {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({op: 'answer', id: data.id, answer: payload}));
      const pending = pendingQuestions.get(data.id);
      if (pending) pending.answered = true;
      currentQuestion = null;
      box.hidden = true;
      showNextQuestion();
    }
  };
  if (data.kind === 'ask_user') {
    const questions = data.questions || [];
    const answers = {};
    let index = 0;
    const step = () => {
      box.replaceChildren(heading);
      if (index >= questions.length) {
        answer({answers});
        return;
      }
      const question = questions[index];
      const label = document.createElement('div');
      label.textContent = question.header || question.question || '';
      box.append(label);
      for (const option of question.options || []) {
        const button = document.createElement('button');
        button.textContent = option.label || '';
        button.onclick = () => {
          answers[question.question || ''] = option.label || '';
          index += 1;
          step();
        };
        box.append(button);
      }
      const decline = document.createElement('button');
      decline.textContent = 'Decline';
      decline.onclick = () => answer({declined: true});
      box.append(decline);
    };
    step();
  } else {
    for (const [label, decision] of [['Allow', 'allow'], ['Deny', 'deny']]) {
      const button = document.createElement('button');
      button.textContent = label;
      button.onclick = () => answer({decision});
      box.append(button);
    }
  }
}

function showNextQuestion() {
  if (currentQuestion) return;
  for (const pending of pendingQuestions.values()) {
    if (!pending.answered) {
      showQuestion(pending);
      return;
    }
  }
  element('question').hidden = true;
}

function handle(message) {
  if (message.type === 'error') {
    line('error', message.message);
    return;
  }
  if (message.type === 'hello') {
    element('status').textContent = (message.engine || 'session') + ' · ' +
      (message.model || 'default');
    return;
  }
  const event = message.event;
  if (!event) return;
  const data = event.data || {};
  switch (event.type) {
    case 'turn_started':
      currentText = null;
      if (data.prompt) line('user', data.prompt);
      break;
    case 'prompt_queued':
      line('notice', 'Queued: ' + (data.text || 'prompt'));
      break;
    case 'text_delta':
      if (!currentText) currentText = line('assistant', '');
      currentText.textContent += data.text || data.delta || '';
      break;
    case 'turn_done':
    case 'turn_refused':
      currentText = null;
      if (data.error || data.reason) line('error', data.error || data.reason);
      break;
    case 'needs_input':
      if (!pendingQuestions.has(data.id)) {
        pendingQuestions.set(data.id, {...data, answered: false});
      }
      showNextQuestion();
      break;
    case 'needs_input_resolved':
      pendingQuestions.delete(data.id);
      if (currentQuestion === data.id) {
        element('question').hidden = true;
        currentQuestion = null;
      }
      showNextQuestion();
      break;
    case 'tool_call':
      line('tool', data.name || data.tool_name || 'Tool');
      break;
    case 'tool_result':
      if (data.result) line('tool', data.result);
      break;
    case 'model_changed':
      element('status').textContent = 'model ' + data.model;
      break;
  }
}

element('prompt').onsubmit = event => {
  event.preventDefault();
  const field = element('prompt-text');
  const prompt = field.value.trim();
  if (prompt && socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({op: 'prompt', text: prompt}));
    field.value = '';
  }
};
loadSessions();
setInterval(loadSessions, 30000);
