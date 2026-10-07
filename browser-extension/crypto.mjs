// DOXA remote envelope v1. This file is packaged with the extension; it is
// never fetched from the hub. Keep the bounds and AAD in sync with doxa-remote-wire.
const MAX_PLAIN = 128_000;
const MAX_CIPHER = 128_000;
const BUCKET = 4_096;
const encoder = new TextEncoder();
const decoder = new TextDecoder('utf-8', {fatal: true});

function reject(message) { throw new Error(message); }

export function decodeBase64(text, maxBytes) {
  if (typeof text !== 'string' || !/^[A-Za-z0-9+/]*$/.test(text) || text.length > maxBytes * 2)
    reject('Invalid remote base64');
  const raw = atob(text.padEnd(Math.ceil(text.length / 4) * 4, '='));
  if (raw.length > maxBytes) reject('Remote field exceeds bound');
  return Uint8Array.from(raw, char => char.charCodeAt(0));
}

function encodeBase64(bytes) {
  let raw = '';
  for (let offset = 0; offset < bytes.length; offset += 8192)
    raw += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
  return btoa(raw).replace(/=+$/, '');
}

export async function importKey(text) {
  const bytes = decodeBase64(String(text).trim(), 32);
  if (bytes.length !== 32 || bytes.every(byte => byte === 0)) reject('Expected a nonzero 32-byte DOXA key');
  try {
    return await crypto.subtle.importKey('raw', bytes, 'AES-GCM', false, ['encrypt', 'decrypt']);
  } finally { bytes.fill(0); }
}

async function transform(data, stream) {
  const reader = new Blob([data]).stream().pipeThrough(stream).getReader();
  const chunks = [];
  let size = 0;
  while (true) {
    const {value, done} = await reader.read();
    if (done) break;
    size += value.length;
    if (size > MAX_PLAIN) { await reader.cancel(); reject('Remote plaintext exceeds bound'); }
    chunks.push(value);
  }
  const result = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.length; }
  return result;
}

export async function seal(key, context, value) {
  const plain = encoder.encode(JSON.stringify(value));
  if (plain.length > MAX_PLAIN) reject('Remote plaintext exceeds bound');
  let data = plain, compressed = false;
  if (plain.length >= 1024 && typeof CompressionStream !== 'undefined') {
    try {
      const zipped = await transform(plain, new CompressionStream('deflate-raw'));
      if (zipped.length + 32 < plain.length) { data = zipped; compressed = true; }
    } catch (error) {
      if (!(error instanceof TypeError)) throw error;
    }
  }
  const length = Math.ceil((data.length + 5) / BUCKET) * BUCKET;
  if (length > MAX_CIPHER) reject('Remote envelope exceeds bound');
  const padded = new Uint8Array(length);
  padded[0] = compressed ? 1 : 0;
  new DataView(padded.buffer).setUint32(1, data.length, false);
  padded.set(data, 5);
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = new Uint8Array(await crypto.subtle.encrypt({
    name: 'AES-GCM', iv: nonce, additionalData: encoder.encode(`doxa-remote-v1|${context}`), tagLength: 128
  }, key, padded));
  return {v: 1, alg: 'A256GCM', nonce: encodeBase64(nonce), data: encodeBase64(ciphertext)};
}

export async function open(key, context, envelope) {
  if (!envelope || envelope.v !== 1 || envelope.alg !== 'A256GCM') reject('Unknown remote envelope');
  const nonce = decodeBase64(envelope.nonce, 12);
  const ciphertext = decodeBase64(envelope.data, MAX_CIPHER + 16);
  if (nonce.length !== 12 || ciphertext.length < 16) reject('Invalid remote envelope');
  let padded;
  try {
    padded = new Uint8Array(await crypto.subtle.decrypt({
      name: 'AES-GCM', iv: nonce, additionalData: encoder.encode(`doxa-remote-v1|${context}`), tagLength: 128
    }, key, ciphertext));
  } catch { reject('Remote authentication failed'); }
  if (padded.length < 5 || padded.length % BUCKET !== 0 || padded.length > MAX_CIPHER)
    reject('Invalid remote padding');
  if (padded[0] !== 0 && padded[0] !== 1) reject('Invalid compression flag');
  const length = new DataView(padded.buffer).getUint32(1, false);
  if (length > padded.length - 5 || padded.subarray(length + 5).some(byte => byte !== 0))
    reject('Invalid remote payload length');
  let plain = padded.subarray(5, 5 + length);
  if (padded[0] === 1) {
    if (typeof DecompressionStream === 'undefined') reject('Raw DEFLATE is unavailable in this browser');
    plain = await transform(plain, new DecompressionStream('deflate-raw'));
  }
  if (plain.length > MAX_PLAIN) reject('Remote plaintext exceeds bound');
  try { return JSON.parse(decoder.decode(plain)); }
  catch { reject('Invalid remote plaintext'); }
}
