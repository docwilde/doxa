// Capture the real remote browser page after its API-backed transcript loads.
import { writeFile } from 'node:fs/promises';

const [port, target] = process.argv.slice(2);
if (!port || !target) throw new Error('usage: node capture_remote_cdp.mjs PORT OUTPUT');
const endpoint = `http://127.0.0.1:${port}`;
let tab;
for (let attempt = 0; attempt < 50; attempt++) {
  const tabs = await fetch(`${endpoint}/json/list`).then(response => response.json());
  tab = tabs.find(item => item.type === 'page' && item.url.startsWith('http://127.0.0.1:'));
  if (tab) break;
  await new Promise(resolve => setTimeout(resolve, 100));
}
if (!tab) throw new Error('remote browser page did not open');

const socket = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve, { once: true });
  socket.addEventListener('error', reject, { once: true });
});
let sequence = 0;
const pending = new Map();
socket.addEventListener('message', event => {
  const frame = JSON.parse(event.data);
  const receiver = pending.get(frame.id);
  if (!receiver) return;
  pending.delete(frame.id);
  if (frame.error) receiver.reject(new Error(frame.error.message));
  else receiver.resolve(frame.result);
});
function send(method, params = {}) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
await send('Page.enable');
await send('Emulation.setDeviceMetricsOverride', {
  width: 1534, height: 867, deviceScaleFactor: 2, mobile: false,
});
let ready = false;
for (let attempt = 0; attempt < 100; attempt++) {
  const result = await send('Runtime.evaluate', {
    expression: "document.querySelectorAll('#conversation .turn').length >= 4 && document.querySelectorAll('#sessions button').length === 2 && document.querySelector('#status').textContent.includes('codex')",
    returnByValue: true,
  });
  if (result.result.value === true) { ready = true; break; }
  await new Promise(resolve => setTimeout(resolve, 100));
}
if (!ready) throw new Error('remote browser transcript did not render');
const result = await send('Page.captureScreenshot', { format: 'png', fromSurface: true, captureBeyondViewport: false });
await writeFile(target, Buffer.from(result.data, 'base64'));
socket.close();
