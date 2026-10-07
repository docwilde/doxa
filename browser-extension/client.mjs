import {seal, open} from './crypto.mjs';

const MAX_JSON = 128_000;
const MAX_SSE = 128_000;
const ID = /^[A-Za-z0-9][A-Za-z0-9-]{0,127}$/;
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

export function hubOrigin(raw) {
  let url;
  try { url = new URL(raw); } catch { throw new Error('Invalid hub URL'); }
  if (url.protocol !== 'https:' || !url.hostname.endsWith('.ts.net') ||
      url.hostname === '.ts.net' || url.username || url.password || url.search || url.hash ||
      url.pathname !== '/') throw new Error('Use a private https://*.ts.net hub origin');
  return url.origin;
}

export function validTarget(id) {
  if (typeof id !== 'string') return false;
  const parts = id.split('~');
  return parts.length === 2 && parts.every(part => ID.test(part));
}

async function boundedJson(response) {
  if (!response.ok) throw new Error(`Hub refused request (${response.status})`);
  const reader = response.body?.getReader();
  if (!reader) throw new Error('Hub response has no body');
  const chunks = [];
  let size = 0;
  while (true) {
    const {value, done} = await reader.read();
    if (done) break;
    size += value.length;
    if (size > MAX_JSON) { await reader.cancel(); throw new Error('Hub response exceeds bound'); }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  try { return JSON.parse(new TextDecoder('utf-8', {fatal:true}).decode(bytes)); }
  catch { throw new Error('Invalid hub response'); }
}

export class DoxaRemoteClient {
  constructor(origin, key) {
    this.origin = hubOrigin(origin);
    this.key = key;
    this.retries = new Map();
  }

  route(path) { return new URL(path, `${this.origin}/`); }
  async get(path) {
    return boundedJson(await fetch(this.route(path), {
      credentials:'include', redirect:'error', cache:'no-store', signal:AbortSignal.timeout(10_000)
    }));
  }
  async post(path, body) {
    return boundedJson(await fetch(this.route(path), {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body), credentials:'include', redirect:'error', cache:'no-store',
      signal:AbortSignal.timeout(10_000)
    }));
  }
  async sessions() {
    const result = await this.get('api/sessions');
    if (!Array.isArray(result.sessions) || result.sessions.length > 64 ||
        result.sessions.some(session => !validTarget(session.id)))
      throw new Error('Invalid hub session inventory');
    return result.sessions;
  }

  async command(target, operation, body) {
    if (!validTarget(target) || !['prompt','answer','transcript'].includes(operation))
      throw new Error('Invalid remote command');
    const retryKey = operation === 'transcript' ? null : `${target}|${operation}|${JSON.stringify(body)}`;
    let pending = retryKey && this.retries.get(retryKey);
    if (pending && Date.now() - pending.created >= 120_000)
      throw new Error('Command outcome uncertain; inspect the session before retrying');
    if (!pending) {
      const requestId = crypto.randomUUID();
      const plain = {...body, request_id:requestId, issued_at:Math.floor(Date.now()/1000)};
      pending = {requestId, created:Date.now(), request:{
        request_id:requestId,
        sealed:await seal(this.key, `${target}|command|${operation}`, plain)
      }};
      if (retryKey) this.retries.set(retryKey, pending);
    }
    const queued = await this.post(`api/sessions/${target}/${operation}`, pending.request);
    if (!ID.test(queued.command_id || '')) throw new Error('Hub returned no command ID');
    for (let attempt = 0; attempt < 240; attempt++) {
      await wait(250);
      const status = await this.get(`api/commands/${queued.command_id}`);
      if (status.status === 'accepted') {
        const result = await open(this.key,
          `${target}|result|${operation}|${pending.requestId}`, status.result?.sealed);
        if (retryKey) this.retries.delete(retryKey);
        if (result?.ok === false) throw new Error(result.error || 'Host refused command');
        return result;
      }
      if (status.status === 'refused') {
        if (retryKey) this.retries.delete(retryKey);
        throw new Error('Host refused command');
      }
      if (status.status === 'expired')
        throw new Error('Command outcome uncertain; inspect the session before retrying');
    }
    throw new Error('Command acknowledgement timed out; inspect the session before retrying');
  }

  async events(target, cursor, signal, onFrame) {
    if (!validTarget(target) || !Number.isSafeInteger(cursor) || cursor < 0)
      throw new Error('Invalid remote event cursor');
    const response = await fetch(this.route(`api/sessions/${target}/events?cursor=${cursor}`), {
      credentials:'include', redirect:'error', cache:'no-store', signal
    });
    if (!response.ok || !response.body) throw new Error('Remote event stream unavailable');
    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8', {fatal:true});
    let buffer = '';
    while (true) {
      const {value, done} = await reader.read();
      if (done) return;
      buffer += decoder.decode(value, {stream:true});
      if (buffer.length > MAX_SSE) throw new Error('Remote event exceeds bound');
      let end;
      while ((end = buffer.indexOf('\n\n')) !== -1) {
        const block = buffer.slice(0, end); buffer = buffer.slice(end + 2);
        const line = block.split('\n').find(line => line.startsWith('data: '));
        if (!line) continue;
        let frame;
        try { frame = JSON.parse(line.slice(6)); }
        catch { throw new Error('Invalid remote event'); }
        if (frame.type === 'event' && frame.event?.data?.sealed) {
          if (!Number.isSafeInteger(frame.seq) || frame.seq < 0 ||
              typeof frame.event.type !== 'string') throw new Error('Invalid encrypted event');
          frame.event.data = await open(this.key,
            `${target}|event|${frame.seq}|${frame.event.type}`, frame.event.data.sealed);
        } else if (frame.type === 'event' && frame.event?.type !== 'replay_gap') {
          throw new Error('Unencrypted remote event refused');
        }
        await onFrame(frame);
      }
    }
  }
}
