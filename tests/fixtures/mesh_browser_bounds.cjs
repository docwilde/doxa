// SPDX-License-Identifier: AGPL-3.0-only
// Run the production model and statistics without a browser, network or timer.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const script = fs.readFileSync(path.join(__dirname, "../../assets/mesh/mesh.js"), "utf8");
assert.match(script, /\nboot\(\);\s*$/);
const canvas = { getContext: () => ({}), classList: { toggle() {} } };
const context = vm.createContext({ document: { getElementById: () => canvas }, performance: { now: () => 0 }, assert });
vm.runInContext(script.replace(/\nboot\(\);\s*$/, "\n"), context, { timeout: 1000 });
vm.runInContext(`
function record(id, from, recipients = [], body = "message") {
  return { id, from, to: recipients, body, ts: "2026-09-27T10:00:00Z", kind: "direct",
    edges: recipients.filter(to => to !== from).map(to => ({ from, to, kind: "direct" })) };
}
function invariant() {
  assert(nodes.size <= VIEW.maxNodes);
  assert(pairs.size <= VIEW.maxPairs);
  assert(seen.size <= VIEW.maxSeen);
  assert(messages.length <= VIEW.maxMessages);
  assert(messageChars <= VIEW.maxMessageChars);
  for (const pair of pairs.values()) assert(nodes.has(pair.from) && nodes.has(pair.to));
  for (const pulse of pulses) assert(pulse.node ? nodes.has(pulse.node) : nodes.has(pulse.from) && nodes.has(pulse.to));
}
ingest(record("initial", "selected", ["dragged", "hovered"]), false);
select("selected");
dragging = nodes.get("dragged"); dragging.pinned = true;
hovered = "hovered";
const selectedObject = nodes.get("selected");
const draggedObject = dragging;
// Independent writer generations, all finite: no ledger/network dependency.
for (let rotation = 0; rotation < 5; rotation++) {
  for (let index = 0; index < 2000; index++) {
    ingest(record(rotation + "-" + index, "s-" + rotation + "-" + index, ["t-" + rotation + "-" + index]), false);
    if (index % 100 === 0) invariant();
  }
}
invariant();
assert.equal(nodes.get("selected"), selectedObject);
assert.equal(nodes.get("dragged"), draggedObject);
assert.equal(dragging, draggedObject);
assert.equal(hovered, null);
assert(limited.nodes && limited.messages && limited.replay);
// Recently evicted topology must not re-admit its still-retained replay ID.
assert.equal(nodes.has("s-4-1800"), false);
assert.equal(ingest(record("4-1800", "s-4-1800", ["t-4-1800"]), true), false);
assert.equal(nodes.has("s-4-1800"), false);
// Recent replay is still suppressed; the finite old replay window is explicit.
assert.equal(ingest(record("4-1999", "s-4-1999", ["t-4-1999"]), true), false);
// Dense traffic between retained nodes exercises independent tie eviction.
const ids = Array.from(nodes.keys()).slice(-32);
for (const from of ids) for (const to of ids) {
  if (from !== to) ingest(record("dense-" + from + "-" + to, from, [to]), true);
}
assert(limited.pairs);
invariant();
// Fan-out admission is bounded before simulation objects are created.
const recipients = Array.from({ length: 4096 }, (_, i) => "fan-" + i);
ingest(record("wide", "broadcaster", recipients), false);
invariant();
assert(nodes.has("broadcaster") && nodes.has("selected") && nodes.has("dragged"));
// Explicit removal cannot leave stale active objects or dangling projected ties.
removeNode("dragged"); assert.equal(dragging, null); assert.equal(draggedObject.pinned, false);
removeNode("selected"); assert.equal(selected, null);
invariant();
// Exact whole-body retention is constrained by bytes, not just record count.
for (let i = 0; i < 12; i++) ingest(record("large-" + i, "large", [], "x".repeat(1 << 20)), true);
invariant();
assert(messages.length < VIEW.maxMessages);
assert.equal(messages[messages.length - 1].body.length, 1 << 20);
// Oversized dedupe IDs never occupy retained replay capacity.
const seenBefore = seen.size;
ingest(record("i".repeat(1 << 20), "large"), true);
assert.equal(seen.size, seenBefore);
invariant();
for (const field of ["statNodes", "statMsgs", "statPairs", "statBcast", "empty", "limit"]) el[field] = {};
renderStats();
assert.equal(el.limit.hidden, false);
assert.match(el.limit.textContent, /Limited view/);
assert.match(el.limit.textContent, /Full ledger remains on disk/);
assert.match(el.limit.title, /64 sessions, 512 ties/);
assert.equal(el.statNodes.textContent, String(nodes.size));
`, context, { timeout: 10000 });
console.log("mesh browser capacities, rotations, eviction, active state and notice passed");
