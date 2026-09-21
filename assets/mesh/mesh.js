// SPDX-License-Identifier: AGPL-3.0-only
//
// The live message graph: nodes are sessions, edges are messages.
//
// THE ONE SECURITY RULE THIS FILE KEEPS. Message bodies are untrusted
// text. There is no `innerHTML`, no `outerHTML`, no `insertAdjacentHTML`,
// no `document.write` and no `eval` anywhere below, and there never may
// be -- tests/test_meshgraph.py asserts their absence as a standing
// property of this file, because it is precisely the invariant a later
// edit breaks without noticing. Text reaches the screen exactly two ways:
// canvas `fillText`, which cannot express markup at all, and `textContent`
// on an element built with `createElement`. A body of `<script>alert(1)`
// is therefore drawn, letter by letter, as the sixteen characters it is.
//
// WHY A CANVAS. At the experiment's N=32 a single broadcast lays down 31
// edges, and a busy run holds several hundred with per-frame alpha that
// depends on their age. That is a repaint of every element every frame,
// which is what canvas is for and what the DOM is not. It also means the
// graph has no DOM nodes at all, so there is no path by which a session
// title or a body could become an element in the first place.
//
// THE LAYOUT IS WRITTEN HERE RATHER THAN VENDORED. Fruchterman-Reingold,
// about seventy lines, no build step and no dependency, against
// d3-force's ~40KB for the same result at this size. O(N^2) repulsion at
// N=32 is 1024 pair calculations per frame, which is nothing; the comment
// on `step()` says what would have to change before that stops being
// true, and what the first, collapsing version of it got wrong.

"use strict";

// ---- tuning ---------------------------------------------------------------

const SIM = {
  ideal: 190,        // k: the distance two connected nodes settle at
  gravity: 0.028,    // pull toward the origin, per px of offset
  startTemp: 64,     // px a node may move in one frame, at the start
  cooling: 0.975,    // ...decaying by this each frame
  minTemp: 0.35,
  sleepBelow: 0.5,   // mean displacement (px) at which stepping stops
};

const VIEW = {
  nodeMin: 7,
  nodeMax: 21,
  restAlphaDirect: 0.16,    // an edge that has gone quiet stays faintly drawn:
  restAlphaBroadcast: 0.05, // the structure is the point, not just the flash
  pulseMs: 1500,            // travelling dot on a direct message
  ringMs: 1400,             // expanding ring on a broadcast
  maxPulses: 400,
  maxMessages: 4000,        // ring buffer; the ledger on disk stays complete
  feedRows: 40,
};

// Engine colours exist because the emergence experiment's mixed-vendor
// arms randomise model per agent -- "which vendor is this node" is a
// variable being controlled, not decoration. Unknown engines get a stable
// hashed hue rather than all collapsing to one grey.
const ENGINE_COLOURS = {
  claude: "#D97757",
  anthropic: "#D97757",
  codex: "#5FB3B3",
  openai: "#5FB3B3",
  gemini: "#7C9CE0",
  google: "#7C9CE0",
};

const COLOUR = {
  direct: "#D97757",
  broadcast: "#5FB3B3",
  // Traffic whose `kind` the ledger did not state. NEVER guessed from
  // the recipient count -- a broadcast in a two-session fleet reaches
  // one peer and is still a broadcast -- so it draws as neither.
  unknown: "#8A8073",
  running: "#E0A83C",
  node: "#2A251E",
  nodeEdge: "#3A3429",
  text: "#F2E9DD",
  textDim: "#8A8073",
  bg: "#171512",
};

// ---- state ----------------------------------------------------------------

/** session id -> node */
const nodes = new Map();
/** JSON.stringify([from, to]) -> directed pair aggregate */
const pairs = new Map();
/** every record we have seen, newest last, capped at VIEW.maxMessages */
const messages = [];
/** message ids already ingested -- the guard against a stream replay */
const seen = new Set();
/** transient visuals: travelling dots and broadcast rings */
let pulses = [];

const view = { x: 0, y: 0, k: 1 };
let selected = null;
let hovered = null;
let dragging = null;     // node being dragged
let panning = null;      // {x, y} pointer origin for a background drag
let energy = 1;
let temperature = SIM.startTemp;
let needsFit = true;
let fadeSecs = 60;
let showBroadcast = true;
let allLabels = false;
let statsDirty = true;
let feedDirty = true;

const el = {};
const canvas = document.getElementById("graph");
const ctx = canvas.getContext("2d");

// ---- small helpers --------------------------------------------------------

function byId(id) { return document.getElementById(id); }

/** Empty an element without touching innerHTML. */
function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

/** An element with text set the only safe way. */
function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }

/**
 * Milliseconds for a ledger timestamp, or 0.
 *
 * The ledger writes microsecond precision ("...:00.123456Z"), which
 * Date.parse accepts but truncates. A record with no parseable ts falls
 * back to its arrival time, so an edge still ages from *some* honest
 * origin rather than sitting permanently at maximum brightness.
 */
function parseTs(ts) {
  if (typeof ts !== "string" || !ts) return 0;
  const ms = Date.parse(ts);
  return Number.isFinite(ms) ? ms : 0;
}

/** A stable colour for an engine name we have no entry for. */
function hashHue(text) {
  let h = 0;
  for (let i = 0; i < text.length; i++) h = (h * 31 + text.charCodeAt(i)) | 0;
  return `hsl(${Math.abs(h) % 360}, 32%, 62%)`;
}

function engineColour(engine) {
  const key = (engine || "").toLowerCase();
  if (!key) return COLOUR.textDim;
  return ENGINE_COLOURS[key] || hashHue(key);
}

/** A session id short enough to label a node that has not identified itself. */
function shortId(id) { return id.length > 8 ? id.slice(0, 8) : id; }

function repoName(repo) {
  if (!repo) return "";
  const parts = repo.split("/").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : repo;
}

/** What to call a node: its title, else its repo, else its short id. */
function label(node) {
  return node.title || repoName(node.repo) || shortId(node.id);
}

function relTime(ms) {
  if (!ms) return "";
  const secs = Math.max(0, (Date.now() - ms) / 1000);
  if (secs < 60) return `${Math.floor(secs)}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h`;
  return `${Math.floor(secs / 86400)}d`;
}

// ---- ingest ---------------------------------------------------------------

/**
 * Fold one server record into the graph.
 *
 * The record arrives with its `edges` already derived server-side (see
 * doxa/meshgraph.py `edges_for`), so a broadcast's fan-out is not
 * recomputed here and cannot drift from what the tests pin.
 */
function ingest(record, atLoad) {
  // Dedupe by message id. A reconnecting EventSource resumes from
  // Last-Event-ID and should not replay, but "should not" is not a
  // guarantee worth drawing a doubled graph on.
  const key = record.id || `${record.from}|${record.ts}|${record.body.length}`;
  if (seen.has(key)) return false;
  seen.add(key);

  const when = parseTs(record.ts) || Date.now();
  record._t = when;

  const sender = touch(record.from);
  // Identity only ever rides on `from`, so this is the one moment a node
  // can learn what it is called. See the contract note in the README of
  // this change: a session that has only ever RECEIVED stays a short id.
  if (record.title) sender.title = record.title;
  if (record.repo) sender.repo = record.repo;
  if (record.model) sender.model = record.model;
  if (record.engine) sender.engine = record.engine;
  // The SENDER's turn state. One record with N recipients cannot
  // carry N receiver states, so this says nothing about anyone else.
  if (record.sender_turn_state) sender.turn = record.sender_turn_state;
  sender.out += 1;
  sender.last = Math.max(sender.last, when);

  const edges = Array.isArray(record.edges) ? record.edges : [];
  for (const edge of edges) {
    const target = touch(edge.to);
    target.in += 1;
    target.last = Math.max(target.last, when);

    // A JSON array as the key: unambiguous for any session id, and
    // no separator character that an id could itself contain.
    const pk = JSON.stringify([edge.from, edge.to]);
    let pair = pairs.get(pk);
    if (!pair) {
      pair = {
        from: edge.from, to: edge.to,
        // The UNDIRECTED key, computed once here rather than rebuilt for
        // every pair on every frame of the layout.
        ukey: JSON.stringify(edge.from < edge.to
          ? [edge.from, edge.to] : [edge.to, edge.from]),
        count: 0, direct: 0, broadcast: 0, unknown: 0, last: 0,
      };
      pairs.set(pk, pair);
      wake(); // a new edge changes the layout
    }
    pair.count += 1;
    pair.last = Math.max(pair.last, when);
    if (edge.kind === "broadcast") pair.broadcast += 1;
    else if (edge.kind === "direct") pair.direct += 1;
    else pair.unknown += 1;
  }

  // Only live traffic animates. Replaying a thousand historical records
  // on load must not fire a thousand rings at once -- the snapshot is
  // history, and history is drawn as accumulated weight, not as motion.
  if (!atLoad) {
    if (record.kind === "broadcast") {
      pulses.push({ type: "ring", node: record.from, t0: performance.now(), n: edges.length });
    }
    for (const edge of edges) {
      pulses.push({ type: "dot", from: edge.from, to: edge.to, kind: edge.kind, t0: performance.now() });
    }
    if (pulses.length > VIEW.maxPulses) pulses = pulses.slice(-VIEW.maxPulses);
  }

  messages.push(record);
  if (messages.length > VIEW.maxMessages) messages.shift();

  statsDirty = true;
  if (!selected || record.from === selected || edges.some((e) => e.to === selected)) {
    feedDirty = true;
  }
  return true;
}

/** Get or create a node. New nodes are seeded near the centre. */
function touch(id) {
  let node = nodes.get(id);
  if (node) return node;
  const angle = nodes.size * 2.399963; // golden angle -- no two start stacked
  const radius = 40 + nodes.size * 9;
  node = {
    id, title: "", repo: "", model: "", engine: "", turn: "",
    x: Math.cos(angle) * radius,
    y: Math.sin(angle) * radius,
    out: 0, in: 0, last: 0,
    pinned: false,
  };
  nodes.set(id, node);
  wake();
  needsFit = true;
  return node;
}

// ---- layout ---------------------------------------------------------------

/**
 * One step of a Fruchterman-Reingold layout.
 *
 * Repulsion k^2/d between every pair, attraction d^2/k along every tie,
 * and a per-frame displacement cap that cools. The first version here
 * used springs whose force scaled with distance, and it collapsed:
 * measured on a seeded 32-session ledger with 125 connected pairs, a hub
 * carried 31 of them, the attraction summed faster than an inverse-square
 * repulsion could answer, and the whole graph balled up into a knot
 * a fifth of the canvas wide. FR's exponents are the fix -- equilibrium
 * sits exactly at d = k because k^2/d = d^2/k there, independently of how
 * many edges a node happens to carry.
 *
 * O(N^2) in the repulsion loop: 1024 pair calculations at the
 * experiment's N=32, which costs nothing at 60fps. It stays honest to
 * roughly N=300; past that this wants a Barnes-Hut quadtree, and the
 * place to notice is here rather than in a bug report about a slow page.
 *
 * The simulation SLEEPS. Once mean displacement drops below
 * SIM.sleepBelow the graph is settled and stepping stops entirely, so an
 * idle page costs one requestAnimationFrame of drawing rather than a
 * permanently hot core. Anything that changes the graph -- a new node, a
 * new edge, a drag -- calls `wake()` and starts it again.
 */
function step() {
  const list = Array.from(nodes.values());
  const n = list.length;
  if (n < 2) return;
  const k = SIM.ideal;

  for (let i = 0; i < n; i++) {
    const a = list[i];
    a.dx = 0; a.dy = 0;
    for (let j = 0; j < n; j++) {
      if (i === j) continue;
      const b = list[j];
      let ex = a.x - b.x, ey = a.y - b.y;
      let d = Math.hypot(ex, ey);
      if (d < 0.01) {
        // Exactly coincident: separate them deterministically, never
        // randomly -- a random nudge makes the layout unreproducible,
        // and this view is an instrument for an experiment.
        ex = (i - j) * 0.01; ey = 0.01; d = Math.hypot(ex, ey);
      }
      const f = (k * k) / d;
      a.dx += (ex / d) * f;
      a.dy += (ey / d) * f;
    }
  }

  // Attraction acts on the UNDIRECTED relation: a->b and b->a are one
  // structural tie and must not pull twice as hard as a one-way one.
  const pulled = new Set();
  for (const pair of pairs.values()) {
    if (pulled.has(pair.ukey)) continue;
    pulled.add(pair.ukey);
    const a = nodes.get(pair.from), b = nodes.get(pair.to);
    if (!a || !b) continue;
    const ex = a.x - b.x, ey = a.y - b.y;
    const d = Math.hypot(ex, ey) || 0.01;
    const f = (d * d) / k;
    const ux = (ex / d) * f, uy = (ey / d) * f;
    a.dx -= ux; a.dy -= uy;
    b.dx += ux; b.dy += uy;
  }

  let moved = 0;
  for (const node of list) {
    // Gravity toward the origin, so a session nobody has messaged does
    // not drift off the canvas on repulsion alone.
    node.dx -= node.x * SIM.gravity * k * 0.01;
    node.dy -= node.y * SIM.gravity * k * 0.01;

    if (node.pinned) continue;
    const d = Math.hypot(node.dx, node.dy);
    if (d < 0.0001) continue;
    // The temperature cap is what keeps FR stable: a node may never move
    // further in one frame than the current temperature, however large
    // the force on it is.
    const scale = Math.min(d, temperature) / d;
    node.x += node.dx * scale;
    node.y += node.dy * scale;
    moved += d * scale;
  }

  // A hard separation pass, after the forces have had their say. FR's
  // repulsion is a force, so it can be out-argued: two nodes both pulled
  // hard toward the same hub settle on top of each other, and two
  // overlapping discs are one unreadable node with two labels fighting
  // over the same pixels. This just refuses the overlap outright.
  for (let i = 0; i < n; i++) {
    const a = list[i];
    const ra = nodeRadius(a);
    for (let j = i + 1; j < n; j++) {
      const b = list[j];
      const minD = ra + nodeRadius(b) + 10;
      let ex = a.x - b.x, ey = a.y - b.y;
      let d = Math.hypot(ex, ey);
      if (d >= minD) continue;
      if (d < 0.01) { ex = (i - j) * 0.01; ey = 0.01; d = Math.hypot(ex, ey); }
      const push = (minD - d) / 2;
      const ux = (ex / d) * push, uy = (ey / d) * push;
      if (!a.pinned) { a.x += ux; a.y += uy; }
      if (!b.pinned) { b.x -= ux; b.y -= uy; }
    }
  }

  temperature = Math.max(SIM.minTemp, temperature * SIM.cooling);
  energy = moved / n;
}

/** Restart the layout: something about the graph changed. */
function wake() {
  temperature = Math.max(temperature, SIM.startTemp * 0.55);
  energy = 1;
}

/** Frame the whole graph, with a margin, and centre it. */
function fit() {
  const list = Array.from(nodes.values());
  if (!list.length) { view.x = 0; view.y = 0; view.k = 1; return; }
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const node of list) {
    minX = Math.min(minX, node.x); maxX = Math.max(maxX, node.x);
    minY = Math.min(minY, node.y); maxY = Math.max(maxY, node.y);
  }
  const pad = 90;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  const spanX = Math.max(1, maxX - minX) + pad * 2;
  const spanY = Math.max(1, maxY - minY) + pad * 2;
  view.k = clamp(Math.min(w / spanX, h / spanY), 0.12, 1.6);
  view.x = w / 2 - ((minX + maxX) / 2) * view.k;
  view.y = h / 2 - ((minY + maxY) / 2) * view.k;
}

// ---- drawing --------------------------------------------------------------

function nodeRadius(node) {
  const traffic = node.out + node.in;
  return clamp(VIEW.nodeMin + Math.log2(1 + traffic) * 2.6, VIEW.nodeMin, VIEW.nodeMax);
}

/** 1 at the instant of the last message on this pair, 0 once fully aged. */
function recency(last) {
  if (!last) return 0;
  const age = (Date.now() - last) / 1000;
  return clamp(1 - age / fadeSecs, 0, 1);
}

/**
 * The arc for a directed pair.
 *
 * Bowed, and always to the same side for a given ordering, so a->b and
 * b->a are two distinguishable arcs rather than one line carrying two
 * meanings. Direction is the measurement here -- the emergence plan reads
 * in-degree against out-degree -- so it may not be flattened away.
 */
function arc(a, b) {
  const dx = b.x - a.x, dy = b.y - a.y;
  const d = Math.hypot(dx, dy) || 1;
  const bow = (a.id < b.id ? 1 : -1) * clamp(d * 0.13, 10, 44);
  return { cx: (a.x + b.x) / 2 + (-dy / d) * bow, cy: (a.y + b.y) / 2 + (dx / d) * bow };
}

function pointOnArc(a, b, c, t) {
  const u = 1 - t;
  return {
    x: u * u * a.x + 2 * u * t * c.cx + t * t * b.x,
    y: u * u * a.y + 2 * u * t * c.cy + t * t * b.y,
  };
}

function draw() {
  const w = canvas.clientWidth, h = canvas.clientHeight;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const dpr = window.devicePixelRatio || 1;
  ctx.scale(dpr, dpr);
  ctx.fillStyle = COLOUR.bg;
  ctx.fillRect(0, 0, w, h);
  ctx.translate(view.x, view.y);
  ctx.scale(view.k, view.k);

  const focus = selected ? neighbourhood(selected) : null;

  drawEdges(focus);
  drawPulses(focus);
  drawNodes(focus);
}

/** The selected node plus everything it has exchanged a message with. */
function neighbourhood(id) {
  const set = new Set([id]);
  for (const pair of pairs.values()) {
    if (pair.from === id) set.add(pair.to);
    if (pair.to === id) set.add(pair.from);
  }
  return set;
}

function drawEdges(focus) {
  ctx.lineCap = "round";
  for (const pair of pairs.values()) {
    const a = nodes.get(pair.from), b = nodes.get(pair.to);
    if (!a || !b) continue;

    // Three ways, because the ledger states two kinds and admits when
    // it knows neither. Any direct traffic makes this a direct edge; only
    // broadcast makes it a broadcast edge; only unstated-kind traffic
    // draws grey and claims nothing about which it was.
    const kind = pair.direct > 0 ? "direct"
      : pair.broadcast > 0 ? "broadcast" : "unknown";

    // A pair whose traffic is ENTIRELY broadcast disappears when the fan
    // is muted; one that also carries direct messages stays, drawn on its
    // direct weight alone. That is what makes the toggle a real control
    // for the experiment rather than a cosmetic filter: with it off, the
    // view is the pairwise-only condition. Unknown-kind traffic is never
    // muted, because muting it would assert it was a broadcast.
    if (kind === "broadcast" && !showBroadcast) continue;

    const weight = showBroadcast ? pair.count : pair.count - pair.broadcast;
    if (weight <= 0) continue;

    const isBroadcast = kind === "broadcast";
    const rest = isBroadcast ? VIEW.restAlphaBroadcast : VIEW.restAlphaDirect;
    let alpha = rest + (1 - rest) * recency(pair.last) * 0.85;
    if (focus && !(focus.has(pair.from) && focus.has(pair.to))) alpha *= 0.14;

    const c = arc(a, b);
    ctx.globalAlpha = alpha;
    ctx.strokeStyle = COLOUR[kind];
    // Thickness carries volume, logarithmically: the difference between 1
    // and 4 messages should read, and the difference between 200 and 400
    // should not dominate the canvas.
    ctx.lineWidth = clamp(0.8 + Math.log2(1 + weight) * 1.15, 0.8, 7) / (isBroadcast ? 2.2 : 1);
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.quadraticCurveTo(c.cx, c.cy, b.x, b.y);
    ctx.stroke();

    // An arrowhead only where it can be read: on a direct edge, with
    // enough alpha to be visible, short of the target's rim.
    if (kind !== "broadcast" && alpha > 0.28) {
      const rb = nodeRadius(b);
      const near = pointOnArc(a, b, c, 1 - (rb + 3) / Math.max(1, Math.hypot(b.x - a.x, b.y - a.y)));
      const back = pointOnArc(a, b, c, 1 - (rb + 13) / Math.max(1, Math.hypot(b.x - a.x, b.y - a.y)));
      const ang = Math.atan2(near.y - back.y, near.x - back.x);
      ctx.fillStyle = COLOUR[kind];
      ctx.beginPath();
      ctx.moveTo(near.x, near.y);
      ctx.lineTo(near.x - Math.cos(ang - 0.42) * 8, near.y - Math.sin(ang - 0.42) * 8);
      ctx.lineTo(near.x - Math.cos(ang + 0.42) * 8, near.y - Math.sin(ang + 0.42) * 8);
      ctx.closePath();
      ctx.fill();
    }
  }
  ctx.globalAlpha = 1;
}

function drawPulses(focus) {
  const now = performance.now();
  let live = false;
  for (const pulse of pulses) {
    if (pulse.type === "ring") {
      if (!showBroadcast) continue;
      const t = (now - pulse.t0) / VIEW.ringMs;
      if (t >= 1) continue;
      live = true;
      const node = nodes.get(pulse.node);
      if (!node) continue;
      // ONE ring for ONE broadcast. This is the whole reason a broadcast
      // does not read as 31 unrelated events: the fan-out is drawn as a
      // single expanding front from the sender, and the spokes below it
      // are deliberately faint.
      ctx.globalAlpha = (1 - t) * 0.6 * (focus && !focus.has(pulse.node) ? 0.2 : 1);
      ctx.strokeStyle = COLOUR.broadcast;
      ctx.lineWidth = 2.4 * (1 - t) + 0.5;
      ctx.beginPath();
      ctx.arc(node.x, node.y, nodeRadius(node) + t * 190, 0, Math.PI * 2);
      ctx.stroke();
    } else {
      const t = (now - pulse.t0) / VIEW.pulseMs;
      if (t >= 1) continue;
      live = true;
      if (pulse.kind === "broadcast" && !showBroadcast) continue;
      const a = nodes.get(pulse.from), b = nodes.get(pulse.to);
      if (!a || !b) continue;
      const c = arc(a, b);
      const p = pointOnArc(a, b, c, t);
      const dim = focus && !(focus.has(pulse.from) && focus.has(pulse.to)) ? 0.16 : 1;
      ctx.globalAlpha = (1 - t) * dim;
      ctx.fillStyle = pulse.kind === "broadcast" ? COLOUR.broadcast : COLOUR.direct;
      ctx.beginPath();
      ctx.arc(p.x, p.y, pulse.kind === "broadcast" ? 1.8 : 3.4, 0, Math.PI * 2);
      ctx.fill();
    }
  }
  ctx.globalAlpha = 1;
  if (!live && pulses.length) pulses = [];
}

function drawNodes(focus) {
  const now = performance.now();
  const drawn = [];      // label boxes already placed, for collision skipping
  const list = Array.from(nodes.values());

  // Selected and hovered last, so they sit on top of everything.
  list.sort((a, b) => {
    const rank = (n) => (n.id === selected ? 2 : n.id === hovered ? 1 : 0);
    return rank(a) - rank(b);
  });

  for (const node of list) {
    const r = nodeRadius(node);
    const dim = focus && !focus.has(node.id) ? 0.2 : 1;
    const accent = engineColour(node.engine);

    // A running turn breathes. It is the only animation on an otherwise
    // still node, so "which agents are working right now" is readable
    // without reading a single label.
    if (node.turn === "running") {
      const beat = 0.5 + 0.5 * Math.sin(now / 420);
      ctx.globalAlpha = 0.16 * beat * dim + 0.06;
      ctx.fillStyle = COLOUR.running;
      ctx.beginPath();
      ctx.arc(node.x, node.y, r + 7 + beat * 5, 0, Math.PI * 2);
      ctx.fill();
    }

    // A halo on whichever node last spoke, fading on the same clock as
    // its edges, so the eye lands on the active part of the graph.
    const heat = recency(node.last);
    if (heat > 0) {
      ctx.globalAlpha = heat * 0.22 * dim;
      ctx.fillStyle = accent;
      ctx.beginPath();
      ctx.arc(node.x, node.y, r + 5, 0, Math.PI * 2);
      ctx.fill();
    }

    ctx.globalAlpha = dim;
    ctx.fillStyle = COLOUR.node;
    ctx.beginPath();
    ctx.arc(node.x, node.y, r, 0, Math.PI * 2);
    ctx.fill();
    ctx.lineWidth = node.id === selected ? 2.6 : node.id === hovered ? 2 : 1.4;
    ctx.strokeStyle = node.id === selected ? COLOUR.text : accent;
    ctx.stroke();

    ctx.globalAlpha = 1;
  }

  // Labels in a second pass, so no node can be drawn over a label that
  // was already placed.
  for (let i = list.length - 1; i >= 0; i--) {
    const node = list[i];
    const forced = node.id === selected || node.id === hovered;
    if (focus && !focus.has(node.id) && !forced) continue;
    drawLabel(node, drawn, forced);
  }
}

/**
 * A node's label, skipped when it would collide with one already drawn.
 *
 * This is what keeps the view readable at 32 nodes rather than 4. Every
 * label drawn unconditionally turns a moderately dense graph into a wall
 * of overlapping text; dropping the ones that collide keeps the legible
 * majority and loses only the ones nobody could have read anyway. The
 * "all labels" toggle turns the skipping off for a reader who would
 * rather zoom than guess, and hover/selection always wins a slot.
 */
function drawLabel(node, drawn, forced) {
  const r = nodeRadius(node);
  const text = label(node);
  const size = 12 / Math.max(0.55, Math.min(1.35, view.k));
  ctx.font = `500 ${size}px ui-sans-serif, -apple-system, "Segoe UI", sans-serif`;
  const w = ctx.measureText(text).width;
  const x = node.x - w / 2;
  const y = node.y + r + size + 2;
  const box = { x: x - 3, y: y - size, w: w + 6, h: size + 4 };

  if (!forced && !allLabels) {
    for (const other of drawn) {
      if (box.x < other.x + other.w && box.x + box.w > other.x &&
          box.y < other.y + other.h && box.y + box.h > other.y) return;
    }
  }
  drawn.push(box);

  // A backing plate rather than a stroke outline: text over a dense edge
  // bundle is unreadable without one, and an outline thick enough to fix
  // that makes the glyphs mushy.
  ctx.globalAlpha = 0.72;
  ctx.fillStyle = COLOUR.bg;
  ctx.fillRect(box.x, box.y, box.w, box.h);
  ctx.globalAlpha = 1;

  ctx.fillStyle = forced ? COLOUR.text : "#B9AE9B";
  ctx.textBaseline = "alphabetic";
  ctx.fillText(text, x, y);

  // The repository only for the node under the pointer or the selection
  // -- it is the second line that would double the collision rate.
  if (forced) {
    const sub = repoName(node.repo) || node.id;
    const subSize = size * 0.85;
    ctx.font = `400 ${subSize}px ui-monospace, monospace`;
    const sw = ctx.measureText(sub).width;
    ctx.globalAlpha = 0.72;
    ctx.fillStyle = COLOUR.bg;
    ctx.fillRect(node.x - sw / 2 - 3, y + 2, sw + 6, subSize + 4);
    ctx.globalAlpha = 1;
    ctx.fillStyle = COLOUR.textDim;
    ctx.fillText(sub, node.x - sw / 2, y + subSize + 3);
  }
}

// ---- the frame loop -------------------------------------------------------

function frame() {
  const settling = energy > SIM.sleepBelow || dragging;
  if (settling) step();
  // Re-frame on every frame WHILE the layout is still moving, rather
  // than once at some guessed moment. The graph visibly expands into the
  // window as it settles, and -- the actual reason -- there is no instant
  // to guess wrong: a single fit() fired too early frames a knot that is
  // still unfolding, which is what the first version did.
  if (needsFit && nodes.size) {
    fit();
    if (!settling) needsFit = false;
  }
  draw();
  if (statsDirty) { renderStats(); statsDirty = false; }
  if (feedDirty) { renderPanel(); feedDirty = false; }
  requestAnimationFrame(frame);
}

// ---- DOM: stats and the traffic panel -------------------------------------

function renderStats() {
  let broadcasts = 0;
  for (const m of messages) if (m.kind === "broadcast") broadcasts++;
  el.statNodes.textContent = String(nodes.size);
  el.statMsgs.textContent = String(messages.length);
  el.statPairs.textContent = String(pairs.size);
  el.statBcast.textContent = String(broadcasts);
  el.empty.hidden = messages.length > 0;
}

/**
 * The selected session's recent traffic.
 *
 * Every string here lands through `textContent` or `make()`. A body is
 * put into a <p> as text and CSS clamps its height; clicking expands it.
 * There is no formatting pass, no markdown, and no link detection --
 * each of those would be a place where untrusted text becomes markup,
 * and none of them is worth that.
 */
function renderPanel() {
  if (!selected || !nodes.has(selected)) {
    el.panel.classList.add("is-empty");
    el.panelBody.hidden = true;
    return;
  }
  const node = nodes.get(selected);
  el.panel.classList.remove("is-empty");
  el.panelBody.hidden = false;

  el.selTitle.textContent = node.title || shortId(node.id);
  el.selRepo.textContent = node.repo || "(repository not yet reported)";
  const meta = [node.engine, node.model, node.turn ? `own turn ${node.turn}` : ""]
    .filter(Boolean).join("  ·  ");
  el.selMeta.textContent = meta || node.id;

  // Distinct endpoints, not pair-map entries. `pairs` is keyed
  // directionally (see the comment on the map's declaration above), so a
  // session that both sent to and received from the same peer holds TWO
  // entries here -- (selected, other) and (other, selected) -- and
  // counting entries counted that peer twice. A session is never its own
  // peer: no self-edge reaches this data today (doxa/meshgraph.py's
  // edges_for drops self-delivery before a record is ever served), but
  // the `!== selected` guards keep that true even if that upstream
  // contract ever changes, rather than trusting it silently.
  const peerIds = new Set();
  for (const pair of pairs.values()) {
    if (pair.from === selected && pair.to !== selected) peerIds.add(pair.to);
    else if (pair.to === selected && pair.from !== selected) peerIds.add(pair.from);
  }
  const peers = peerIds.size;
  el.selOut.textContent = String(node.out);
  el.selIn.textContent = String(node.in);
  el.selPeers.textContent = String(peers);

  const involved = [];
  for (let i = messages.length - 1; i >= 0 && involved.length < VIEW.feedRows; i--) {
    const m = messages[i];
    if (m.from === selected || m.to.indexOf(selected) !== -1) involved.push(m);
  }

  clear(el.feed);
  for (const m of involved) el.feed.appendChild(messageRow(m, node));

  const total = messages.reduce(
    (n, m) => n + (m.from === selected || m.to.indexOf(selected) !== -1 ? 1 : 0), 0);
  el.selMore.textContent = total > involved.length
    ? `showing ${involved.length} of ${total}` : "";
}

function messageRow(m, node) {
  const outgoing = m.from === node.id;
  const row = make("li", "msg is-" + (m.kind === "broadcast" ? "broadcast"
    : m.kind === "unknown" ? "unknown" : "direct"));

  const head = make("div", "msg-head");
  head.appendChild(make("span", "msg-dir", outgoing ? "→" : "←"));

  const counterpart = outgoing
    ? (m.to.length > 1 ? `${m.to.length} recipients` : nameOf(m.to[0]))
    : nameOf(m.from);
  head.appendChild(make("span", "msg-peer", counterpart));
  if (m.kind === "broadcast") head.appendChild(make("span", "msg-tag", "BCAST"));
  else if (m.kind === "unknown") head.appendChild(make("span", "msg-tag", "KIND?"));
  head.appendChild(make("span", "msg-time", relTime(m._t)));
  row.appendChild(head);

  // The body. textContent, and nothing else, ever.
  const body = make("p", "msg-body", m.body || "(empty)");
  body.addEventListener("click", () => body.classList.toggle("is-open"));
  row.appendChild(body);

  // Sender-side, and labelled as such: this is how long the sender took
  // to COMPOSE the message, measured from whatever `in_reply_to` names --
  // not time in transit. Null means there was no reference point, which
  // is common and emphatically not zero, so the row omits it entirely.
  const bits = [];
  if (typeof m.sender_latency_ms === "number") {
    bits.push(`${Math.round(m.sender_latency_ms)}ms to compose`);
  }
  if (m.model) bits.push(m.model);
  if (m.in_reply_to) bits.push("reply");
  if (bits.length) row.appendChild(make("p", "msg-foot", bits.join("  ·  ")));

  return row;
}

/** A readable name for a session id we may or may not have identity for. */
function nameOf(id) {
  const node = nodes.get(id);
  return node ? label(node) : shortId(id || "");
}

// ---- interaction ----------------------------------------------------------

function toWorld(px, py) {
  const rect = canvas.getBoundingClientRect();
  return {
    x: (px - rect.left - view.x) / view.k,
    y: (py - rect.top - view.y) / view.k,
  };
}

function nodeAt(px, py) {
  const p = toWorld(px, py);
  let best = null, bestD = Infinity;
  for (const node of nodes.values()) {
    const d = Math.hypot(node.x - p.x, node.y - p.y);
    const r = nodeRadius(node) + 6;
    if (d < r && d < bestD) { best = node; bestD = d; }
  }
  return best;
}

function resize() {
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(canvas.clientWidth * dpr);
  canvas.height = Math.round(canvas.clientHeight * dpr);
}

function wire() {
  canvas.addEventListener("pointerdown", (ev) => {
    // Pointer capture keeps drag events coming when the pointer leaves
    // the canvas -- a convenience, not a requirement. It throws for a
    // pointer id that is not active, and an exception here would abort
    // the handler before anything is selected, so selection would fail
    // for a reason that has nothing to do with selection.
    try { canvas.setPointerCapture(ev.pointerId); } catch (err) { /* not fatal */ }
    const hit = nodeAt(ev.clientX, ev.clientY);
    if (hit) {
      dragging = hit;
      hit.pinned = true;
      select(hit.id);
    } else {
      panning = { x: ev.clientX - view.x, y: ev.clientY - view.y };
      canvas.classList.add("is-dragging");
    }
  });

  canvas.addEventListener("pointermove", (ev) => {
    if (dragging) {
      const p = toWorld(ev.clientX, ev.clientY);
      dragging.x = p.x; dragging.y = p.y;
      wake();
      return;
    }
    if (panning) {
      view.x = ev.clientX - panning.x;
      view.y = ev.clientY - panning.y;
      return;
    }
    const hit = nodeAt(ev.clientX, ev.clientY);
    const id = hit ? hit.id : null;
    if (id !== hovered) { hovered = id; canvas.classList.toggle("is-over-node", !!hit); }
  });

  const release = () => {
    if (dragging) { dragging.pinned = false; dragging = null; wake(); }
    panning = null;
    canvas.classList.remove("is-dragging");
  };
  canvas.addEventListener("pointerup", release);
  canvas.addEventListener("pointercancel", release);

  canvas.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    // Zoom about the pointer, not the origin: the thing under the cursor
    // stays under the cursor, which is the only zoom that feels like
    // moving rather than jumping.
    const rect = canvas.getBoundingClientRect();
    const mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
    const before = { x: (mx - view.x) / view.k, y: (my - view.y) / view.k };
    const k = clamp(view.k * Math.exp(-ev.deltaY * 0.0014), 0.1, 4.5);
    view.k = k;
    view.x = mx - before.x * k;
    view.y = my - before.y * k;
  }, { passive: false });

  canvas.addEventListener("dblclick", (ev) => {
    if (!nodeAt(ev.clientX, ev.clientY)) { select(null); fit(); }
  });

  window.addEventListener("resize", () => { resize(); needsFit = true; });

  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") select(null);
    else if (ev.key === "f") fit();
    else if (ev.key === "b") { el.optBroadcast.checked = !el.optBroadcast.checked; showBroadcast = el.optBroadcast.checked; }
  });

  el.optBroadcast.addEventListener("change", () => { showBroadcast = el.optBroadcast.checked; });
  el.optLabels.addEventListener("change", () => { allLabels = el.optLabels.checked; });
  el.optFade.addEventListener("input", () => {
    fadeSecs = Number(el.optFade.value) || 60;
    el.optFadeRead.textContent = `${fadeSecs}s`;
  });
  el.btnFit.addEventListener("click", () => fit());
  el.panelClose.addEventListener("click", () => select(null));
}

function select(id) {
  selected = id;
  feedDirty = true;
}

// ---- transport ------------------------------------------------------------

function setConn(state, text) {
  el.conn.className = state;
  el.conn.textContent = text;
}

/**
 * Snapshot first, then stream from exactly where the snapshot stopped.
 *
 * The offset is the whole point of doing it in this order: a record
 * appended between the two requests sits after that offset and arrives on
 * the stream, and nothing already in the snapshot can arrive twice. The
 * urls are relative, which is what keeps them inside the capability token
 * in the path without this file ever naming it.
 */
async function connect() {
  let offset = 0;
  try {
    const res = await fetch("ledger", { credentials: "omit" });
    if (!res.ok) throw new Error(`ledger ${res.status}`);
    const data = await res.json();
    for (const record of data.records) ingest(record, true);
    offset = data.offset || 0;
    if (nodes.size) { wake(); needsFit = true; }
  } catch (err) {
    setConn("conn-dead", "ledger unavailable");
    return;
  }

  const stream = new EventSource(`events?from=${offset}`);
  stream.onopen = () => setConn("conn-live", "live");
  stream.onmessage = (ev) => {
    let record;
    try {
      record = JSON.parse(ev.data);
    } catch (err) {
      return; // a frame we cannot read is a dropped frame, never a dead page
    }
    ingest(record, false);
  };
  // EventSource reconnects by itself, and the server honours
  // Last-Event-ID, so a reconnect resumes with neither a gap nor a
  // replay. Nothing to do here but say so on the bar.
  stream.onerror = () => {
    setConn(stream.readyState === 2 ? "conn-dead" : "conn-wait",
            stream.readyState === 2 ? "disconnected" : "reconnecting");
  };
}

// ---- boot -----------------------------------------------------------------

function boot() {
  el.statNodes = byId("stat-nodes");
  el.statMsgs = byId("stat-msgs");
  el.statPairs = byId("stat-pairs");
  el.statBcast = byId("stat-bcast");
  el.conn = byId("conn");
  el.empty = byId("empty");
  el.panel = byId("panel");
  el.panelBody = byId("panel-body");
  el.panelClose = byId("panel-close");
  el.selTitle = byId("sel-title");
  el.selRepo = byId("sel-repo");
  el.selMeta = byId("sel-meta");
  el.selOut = byId("sel-out");
  el.selIn = byId("sel-in");
  el.selPeers = byId("sel-peers");
  el.selMore = byId("sel-more");
  el.feed = byId("sel-feed");
  el.optBroadcast = byId("opt-broadcast");
  el.optLabels = byId("opt-labels");
  el.optFade = byId("opt-fade");
  el.optFadeRead = byId("opt-fade-read");
  el.btnFit = byId("btn-fit");

  resize();
  wire();
  renderStats();
  requestAnimationFrame(frame);
  connect();
}

boot();
