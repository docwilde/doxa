# Fleets: N sessions, one task — symmetric or supervised — and more than one machine

Three things landed together, because the experiment in
[`plans/emergent-organization.md`](plans/emergent-organization.md) needs all
three and none of them is useful alone:

1. **`doxa.fleet`** — spawn N sessions, hand them the identical prompt at the
   same instant, wait for quiescence, collect the ledger, tear down.
2. **`doxa.peernet`** — peers on other machines, so several boxes can each run
   a fleet and the fleets can see and message each other.
3. **Memory as a per-agent variable** — `doxa.daemon --no-lore`, because a
   shared LORE store is a coordination channel the message ledger cannot see.

A fourth came later and is deliberately **not** part of the experiment.
`--supervisor` inverts a run: one session receives the operator's task and
hands the pieces to the others over peer messages, because ordinary software
work wants a head and the experiment wants none. It is the same harness —
same barrier, same manifest, same teardown — with a different dispatch, so it
lives inside section 1 rather than in a section of its own.

This document was written about the symmetric shape and still is. Every
sentence below that says *every session gets the identical prompt* is a
statement about the **default** shape;
[supervisor mode](#supervisor-mode-one-session-takes-the-task) is where each
of those sentences stops being true, and says so.

---

## 1. The local fleet harness

```bash
uv run python -m doxa.fleet \
    --prompt "$(cat task.txt)" \
    --pool 'claude:sonnet@8,claude:opus@1,deepseek:deepseek-chat@4' \
    -n 8 --seed 1 --memory-off 2 \
    --root /tmp/dx
```

`--dry-run` prints the capacity arithmetic and the assignment this seed deals,
and spawns nothing. Run it first.

### The same run, from inside DOXA

`/fleet start` takes the **same flags**, parsed by the same
`doxa.fleet.build_parser` — there is no second grammar, so a line that works
in a shell works in a session and the two cannot drift apart. `--cwd` is the
one thing they differ on, and it is an argument rather than a parser default:
the shell's is the process's directory, the TUI's is the session's own repo.

```
/fleet start --pool claude:sonnet@1 -n 4 --run-budget 5 --prompt "…"
```

The run goes on the TUI's own event loop and opens a **read-only tab** named
for its run id, which shows the capacity arithmetic and the budget note it
started under, the assignment table, the dispatch spread, the quiescence state
with elapsed time, the last thirty ledger lines as `t+s  from → to  body`, and
at the end the leaked-pid report and the manifest path. The tab reads the run's
**manifest and ledger** and nothing else — `write_manifest()` is called on a
heartbeat while the run is live, so the same file `doxa-fleet` prints at the end
is readable during — which is what keeps a Textual timer away from state the
orchestration is mutating. A refusal that fires before any manifest exists
(`check_capacity`, `check_run_budget`, `check_socket_budget`) is the tab's
first line rather than a traceback.

The other verbs: `/fleet status` prints the same report into the transcript;
`/fleet stop` ends the run through the same teardown the quiescence deadline
takes, and the tab keeps the final report; `/fleet runs` lists what has been
run under the root, read from the manifests themselves; `/fleet attach <slot>`
opens one slot's session in a live tab — through the run's manifest, because a
run's peer registry is under its own `DOXA_RUNTIME_DIR` and this machine's
cannot see it; `/fleet mesh` graphs this run's ledger. Bare `/fleet` lists the
verbs and says whether a run is live here.

**Closing the tab tears the run down.** A fleet is not a background service: it
keeps N daemons alive, arms every one of them to be woken by another's message,
and therefore keeps spending with nobody typing — the same failure
`check_run_budget` refuses to let an operator reach by forgetting a flag, and it
must not be reachable by forgetting a tab either. `/fleet detach` is the
explicit "keep this running" gesture, and the tab's header says which of the two
states it is in.

### What makes the start symmetric

The experiment's methodological core is one sentence: *every participant
receives the same task description at the same moment, and none is privileged
in any way.* "The same moment" is not implementable, so here is the honest
approximation, in the order the properties matter:

| | mechanism |
|---|---|
| **Nobody is prompted until everybody is armed** | every session is spawned *and* has a client attached before the first prompt frame is written. Absolute, not approximate: no session can begin work in a world where another does not yet exist. |
| **The text is byte-identical** | one string, handed to every session unmodified. No per-session formatting exists in `FleetRun._dispatch_symmetric` — "you are agent 7 of 32" would hand a participant its own position, and a position is a privilege. |
| **Dispatch order is randomised per run** | drawn from the run seed, so slot 0 is not systematically first across a cell's five replications. |
| **The residual spread is measured** | `dispatch_spread_s` in the manifest. Observed: **1–11 ms across N = 4…32** on a 16-core laptop. A paper states that number rather than claiming simultaneity. |

### Supervisor mode: one session takes the task

Everything above is an instrument, and an instrument is the wrong tool for a
refactor. Four agents each deciding independently how to rename a symbol is
four conflicting answers and three wasted sessions — the symmetry that makes
the experiment interpretable is exactly what makes ordinary work collide.
`--supervisor <engine[:model]>` is the second shape: the operator's prompt
reaches **one** session, which divides the job, hands the pieces out over peer
messages, and integrates the replies.

```bash
uv run python -m doxa.fleet \
    --supervisor claude:opus --pool 'claude:sonnet@1' -n 3 \
    --run-budget 5 --root /tmp/dx \
    --prompt "$(cat task.txt)"
```

The flag takes **one** `engine[:model]`, parsed by the same grammar one
`--pool` entry uses, and a comma in it is refused rather than truncated — a
run has exactly one supervisor. The supervisor is **not drawn from the pool**:
`--pool` deals the workers and nothing else, so the coordinator's model is a
choice rather than a draw, which is what lets a strong model supervise cheap
ones.

**`-n` counts workers.** `-n 3 --supervisor claude:opus` starts four sessions:
the supervisor at **slot 0**, workers at slots 1, 2 and 3. Slot 0 is fixed
rather than last so that `/fleet attach 0` reaches the supervisor whatever `-n`
was. Everything that counts sessions counts four — `check_capacity`'s
arithmetic, the `--run-budget` division into per-session shares, and teardown's
evidence that nothing survived. `FleetSpec.session_count` is that number, and
the manifest records it as `spec.sessions`.

`--memory-off K` draws from the **workers** only, and the supervisor always
keeps memory. It is the session that has to hold the shape of the whole job
across every worker's reply, which is the continuity a memory-off agent does
not have; a run that silently dealt the coordinator no memory would fail as
*the supervisor forgot what it had already handed out*. The draw is otherwise
the same seeded one — `assign_for` shifts the symmetric assignment to slots
1..n and inserts the supervisor at 0, so one seed deals the same workers in
either shape.

**The briefings, and why they are turns.** The barrier is unchanged: nothing
is prompted until every session is armed. What changes is the order after it,
and **the order is the protocol** — every
worker is briefed *and has acknowledged the write* before the supervisor is
prompted at all, so there is no instant at which the supervisor could hand a
task to a session that has not yet been told a task is coming. It is
deterministic (workers in slot order, supervisor last) and lands in the
manifest as `dispatch_order`, the same field the shuffle writes. Deterministic
rather than shuffled for the opposite of the shuffle's reason: a protocol whose
order varies per run is a protocol whose failures do not reproduce.

| gets | what `doxa.fleet` composes |
|---|---|
| **each worker** (`worker_briefing`) | its own slot and session id, its supervisor's session id and model, that tasks arrive as peer messages from that session and start a turn, that it carries each one out *in this checkout* and reports back with `peer_send` to that id, and that it does nothing until a task arrives. It closes `reply now with a single line: ready`. |
| **the supervisor** (`supervisor_briefing`) | the roster — every worker's slot, session id, engine and model — the protocol from the other side, that each worker has its own checkout and which branches and files each task should name, then `--- task ---` and the operator's prompt verbatim. |

A worker is told **nothing about the job**. It has not seen the operator's
prompt and cannot guess at it, which is the point: a worker that starts work it
was not given is the failure this shape exists to remove. The closing `ready`
is not ceremony either — it is the only evidence, in the run's own transcript,
that a worker received its briefing and is listening.

Both are **dispatched as turns**, through the same `FleetBackend.dispatch` the
operator's own prompt goes through, rather than injected as a system preamble.
A preamble would have to be plumbed through each of the four engines' own
notion of one, would differ between them in ways nothing here could test, and
would be invisible afterwards. A turn costs one short exchange per worker and
buys a run whose entire instruction set is readable in the transcript.

**Each session works in its own checkout.** `worktree_per_session` is on by
default and a run's worktrees are rooted under the run's own `DOXA_HOME`, so
every session that starts in a git repository already holds a git worktree of
it on its own branch — two workers never edit one file in one tree. The
manifest records each slot's effective `cwd`, read from the registry entry the
daemon wrote, because nothing outside the run can find those worktrees by
looking.

That checkout is where a worker's work is delivered from, so a worker has to
be able to **commit** in it. A Codex slot could not through v1.14.0: Codex
sandboxes the commands its model runs to the session's own directory, and a
linked worktree keeps its index in the main repository, outside it. Both
Codex workers of supervisor run `20260919T160458-539e` created their file,
failed the commit and reported the failure back over `peer_send` — the run
itself healthy, the delivery gone. DOXA now grants that turn the four git
directories a commit needs and no more; see [What a Codex turn may
write](manual.md#what-a-codex-turn-may-write).

**An interactive run has no prompt, and quiet does not end it.** `--prompt`
is optional in supervisor mode and only there; a symmetric run without one is
still refused in the same words. Omitting it starts the fleet, briefs the
workers, and tells the supervisor that the operator will attach and give it
the task. Such a run **is not ended by quiet**: every session goes idle
seconds after being briefed, so the usual `--quiet-dwell` would tear the whole
fleet down while the operator was still reading the tab. It runs until `/fleet
stop` (or SIGINT), or until an explicitly passed `--quiescence-timeout` — which
is why that flag's parser default is now unset rather than 1800 seconds:
*asked for half an hour* and *asked for nothing* have to be different answers,
and they are only different in this one shape.

Because the shape needs somebody to attach, it is a **TUI shape**. `/fleet
start --supervisor … -n 3` opens the run's tab and prints the exact line to run
next:

```
/fleet attach 0
```

The attach is printed rather than performed, because at the instant the command
returns nothing has spawned yet and the supervisor has no socket — waiting for
the spawn phase inside a slash command would hide the tab and swallow a
capacity or budget refusal that belongs on its first line. `doxa-fleet`
**refuses** an interactive run outright: that process blocks inside its own run
for the duration and cannot attach to the session it just spawned, so it names
the TUI instead of starting a fleet nobody can reach.

**What the manifest records.** `mode` (`symmetric` or `supervisor`) and
`interactive` sit at the top level, because every other field means something
slightly different under the other one — `dispatch_order` is a shuffle in the
first and a protocol in the second, `quiesced` is a measurement in the first
and an impossibility in an interactive run. Beside them: a `supervisor` block
naming its slot, session id, engine, model and worktree; a `role` on every slot
and every assignment; `spec.sessions`; and `spec.quiescence_timeout_s` as it
was actually in force. A manifest with no `mode` key predates the modes and is
read as the symmetric run it was.

The [manual's supervisor-mode section](manual.md#supervisor-mode) is the same
material from the operator's side, with the `/fleet` verbs and the tab's mode
line.

### Per-run isolation

Each run gets its **own `DOXA_HOME`** and its **own `DOXA_RUNTIME_DIR`**:

```
<root>/<run-id>/home/peers/messages.jsonl    the run's ledger — the whole file
<root>/<run-id>/rt/registry/                 the run's peer registry
<root>/<run-id>/manifest.json                what the run was, and what happened
```

`doxa.peerledger` puts the ledger at `$DOXA_HOME/peers/messages.jsonl`
deliberately, so a harness collects a run by pointing `DOXA_HOME` at a per-run
directory — no filtering, nothing to separate afterwards. A shared ledger
filtered by time works until two runs overlap, and then it works wrongly and
silently.

The runtime dir is per-run for a different reason: peer *discovery* reads the
registry, so a fleet sharing the machine's registry would discover the
operator's own editor session and count it as a participant. N is a measured
quantity; it has to be the N that was dealt.

> **Keep `--root` short.** An AF_UNIX path is 108 bytes and a per-run directory
> under a per-run root is what spends them. `check_socket_budget` refuses up
> front with the arithmetic rather than letting the bind fail per session,
> minutes into a spawn loop. `/tmp/dx` is a good root; `$HOME/…/pytest-tmp/…`
> is not.

### N, and the arithmetic

```
live CLI process, resident   425–545 MB   (measured)
per session incl. daemon     ~600 MB
N = 32                       ~19 GB
N = 8                        ~4.8 GB
```

`DEFAULT_N` is **4**, not 32. A default is what somebody runs by accident, and
19 GB of new anonymous memory on a 30 GB laptop is a swap storm rather than a
fleet — a swapping fleet does not measure coordination, it measures paging.
`check_capacity` refuses an N that does not fit and names the numbers;
`--force` overrides and is recorded in the manifest. It counts every session
the run starts, so a supervisor run is checked against `n + 1` — a
coordinating session costs the same ~600 MB as a working one.

### What a run may spend

Every session in a run is armed to be woken by another session's message,
which is the point — an agent cannot answer another when nobody is
typing. It also means the run keeps spending with nobody watching, so a
run has to say what it may spend before it starts one.

`--run-budget <dollars>` is a **run-wide total**, not a per-session one:
thirty-two individually reasonable limits multiply into one unreasonable
one, and the number an operator can reason about overnight is "this run
may cost fifty dollars". It is enforced by division — each session is
handed `run-budget / N` as its own `DOXA_SESSION_BUDGET_USD`, and N
separately bounded sessions can together spend at most the total, because
the bounds add. In a supervisor run N is `n + 1`: the supervisor spends
too, and a ceiling that had not counted it would be one whole share short.

`check_run_budget` **refuses a run that arms inbound turn-starting with no
budget**, before any directory is made and long before any process is,
naming what to set. Launching the experiment unbounded by omission — by
forgetting a flag at 2am — is the failure that refusal exists to prevent.
`--allow-unbudgeted` overrides it, is recorded in the manifest as
`unbudgeted`, and is deliberately **not** `--force`: "this machine's
memory numbers are wrong" and "I accept a swarm with nothing bounding its
spend" are two different claims. Its exit code is `3`, not capacity's `2`.

Two limits the arithmetic does not hide, both printed by `budget_note`
before the run and kept in the manifest:

* **Unused share is not reallocated.** A quiet session's unspent half is
  not available to a busy one, so a run spends at most the total and
  typically less.
* **The one-turn overshoot is per session.** A ceiling stops a turn from
  *starting*; a turn already running is never interrupted, because the
  only dollar figure that exists arrives with the message that ends it.
  The true worst case is therefore `total + N x (one turn)` — at N=32,
  thirty-two turns of slack.

And one that is not a limit but a hole: **slots dealt an engine that
reports no cost are not bounded at all.** `codex`, `deepseek` and `glm`
report token counts and no dollars, so their spend reads as `$0.00` and
their share is never enforced. `budget_note` names them in the line it
prints before the run rather than letting the total look like it covers
the whole pool. Since 1.13.0 those slots really do run on their engine
(the daemon takes `--engine`, and `DaemonBackend.spawn` passes the slot's),
so the note describes sessions that exist. A slot whose engine refuses to
start — a vendor key missing from the run's environment — is recorded
failed with the reason in the manifest and the run goes on. Measured:
`--pool deepseek@1,glm@1,codex@1 -n 5 --seed 1` dealt 2/2/1, all five
spawned, 17 messages crossed vendors, quiesced in 37 s, nothing leaked; the
Codex slots received but did not send, because at 1.13.0 a Codex model had
no `peer_send`. Since 1.14.0 it has one, forwarded from the MCP sidecar to
the engine and sent on the session's own limiter and ledger.

### What a run may approve

A session's engine stops and asks a human about some tool calls — the ones the
Claude CLI would have shown its own permission prompt for, and any
`AskUserQuestion` the model raises. In a session that is a dialog in the pane.
In a fleet there is nobody at the keyboard, and until 1.15.0 nothing answered:
the harness consumed the ask and discarded it, so the slot waited for a
decision that was never coming. Measured on 1.14.0, a supervisor sat **6 min
45 s** on `mcp__doxa__peer_list`; an operator then answered ten asks by hand
and the whole exchange finished in 25 s.

`--approve` is how a run says what it may decide on your behalf. It is a
separate flag from `--allow-unbudgeted` on purpose — accepting an unbounded
bill says nothing about accepting tool calls you never saw — and like that one
it is recorded in the manifest, so a run's approval posture is readable
afterwards instead of reconstructed from a shell history.

| Value | What it auto-approves |
|---|---|
| `none` *(default)* | Nothing. |
| `peer` | This run's own peer tools: `peer_list`, `peer_history`, `peer_send`. |
| `all` | Every tool call the CLI asks about. |

`peer` is the narrow value and the one most runs want. A supervisor reaches for
`peer_list` and `peer_send` in its first turn, because that is how it finds its
workers and hands out the task — those two are reads and writes of the run's
own ledger, not of your repository. Everything else in the run still asks.

**An unanswered ask is refused, not allowed.** A harness that quietly said yes
would be handing every spawned session an approval the same operator declined
to give one interactive session, from a flag nobody typed. A refusal is an
ordinary tool result: the model reads it, says so in its reply and carries on,
which is the behaviour `doxa.engine._no_answer_deny` already relies on. So the
refusal is written for the person who was not watching — it names the tool, the
run and the slot, and it names the flag that would have allowed the call:

```
nobody answered within 300s: mcp__doxa__peer_list was called inside a DOXA
fleet run 20260919T..., slot 0, where nobody is at the keyboard. This run
auto-approves nothing (--approve none, the default). --approve peer would
have allowed this run's own peer tools, --approve all every tool the CLI
asks about; `/fleet attach 0` answers one by hand.
```

Two kinds of ask are never auto-approved, whatever `--approve` says. A question
is not a permission — `allow` is not a reply to *which branch?* — so an
`AskUserQuestion` is declined, which is the graceful path the engine already
documents. A `spawn_session` keeps its own gate for the reason that gate was
built: a fleet spawning further fleet with nobody watching is the one outcome
nobody asked for.

### Answering one by hand

`--approval-grace` is how long a parked ask waits for you before the policy
decides it. The default is 300 seconds, which is long enough to read the tab,
attach and choose, and far short of the 1800-second quiescence deadline. Zero
refuses immediately and never waits.

Inside that window the run is answerable. A parked slot is idle in every way a
watcher can measure — no turn, no ledger line, `is_quiet` says yes — so the
fleet tab is the only place it can be told apart from a finished agent, and it
says so directly under the assignment table:

```
WAITING ON YOU — 1 permission ask(s) parked. A parked session looks idle from
outside; it is not.
  slot 0   permission mcp__doxa__peer_list        asked 12s ago, refused in 288s
       answer it: /fleet attach 0
```

`/fleet attach 0` opens that session in a live tab and names the ask on
arrival. The dialog itself arrives on its own: an attach replays the daemon's
event ring, and a `needs_input` still in it opens the pane's question or
permission popup exactly as it would in the session that asked. Answering
there cancels the grace timer, and the run records that a **person** decided
it rather than the policy.

A run started from the shell has the same record but no answer path, which is
the honest shape: `doxa-fleet` blocks inside `asyncio.run()` for the length of
the run and cannot attach to anything. It writes the manifest out whenever the
set of parked asks changes, so the run is readable from another terminal while
it is blocked.

### What the manifest says about it

Every run carries an `approvals` block beside `capacity` and `budget`, because
spending money and granting a permission are the two things a run does on an
operator's behalf and both have to survive the run:

```json
"approvals": {
  "policy": "peer",
  "grace_s": 300.0,
  "posture": "--approve peer: this run's own peer tools are auto-approved, …",
  "asked": 4, "auto_approved": 3, "answered": 0,
  "refused": 1, "ended_unanswered": 0, "pending": 0
}
```

Each slot carries the evidence those counts are summed from: `approvals` is the
decision log — tool, kind, decision, and `by` naming what decided it (`policy`,
`operator`, `timeout` or `teardown`) — and `pending_asks` is what that slot is
blocked on right now. `RunReport.summary()` prints the refusals in its one line
and stays quiet when there were none, so a run nobody asked anything of reads
exactly as it always has.

### Nothing hangs, nothing is left behind

Every phase has a deadline and every deadline has an escalation.

* A session that cannot answer a status call inside two poll intervals is
  marked `hung` and **stops being waited on**. It is still torn down, still
  killed if it has to be, and still named in the manifest.
* Quiescence requires the quiet to *hold* for `--quiet-dwell` seconds. A
  session is idle between its own turn and the turn an arriving peer message
  starts, so the first moment everything is idle is routinely the middle of an
  exchange rather than the end of one.
* A permission ask nobody answers becomes a decision inside `--approval-grace`
  seconds, and an ask still open when the run ends is written into the manifest
  as `by: teardown` rather than dropped. A session waiting on a human is the
  one kind of stall a status poll cannot see, because a parked session is idle.
* Teardown is `stop` → SIGTERM → SIGKILL, then a second pass that asks the OS
  whether the process is *actually* gone. `teardown()` returns the pids that
  survived all of it — normally empty, and loud when it is not.
* `stop` waits for the session's own finalize to actually finish — the LORE
  review/index for a memory-enabled session, the worktree decision, the SDK
  client's own teardown — not just for the daemon to acknowledge the
  request. The daemon closes the socket only once that work is done, and
  the client-side call does not return before then, bounded by
  `FleetSpec.stop_timeout_s` (60 s default) rather than by a fixed clock.
  Before this (issue #58), a LORE-enabled session's slow-but-clean
  shutdown could outlast the short second-pass grace window and get
  SIGKILLed anyway — correctly torn down, but the manifest called it
  `killed` when it had actually `stopped` cleanly. `FleetSpec.kill_grace_s`
  now covers only the residual gap between the socket closing and the OS
  reporting the pid gone, which is why it can stay small while
  `stop_timeout_s` carries the real budget.

### Reproducing a run

The manifest carries the seed, the pool, the prompt and its sha256, and the
assignment. `fleet.assign(n, pool, seed=…)` replays the draw exactly; for a
supervisor run, `fleet.assign_for(spec)` replays the whole slot list, the
supervisor at 0 included.

---

## 2. Memory, per agent

```bash
doxa.daemon --no-lore        # this session only
DOXA_LORE=0                  # this machine's default
```

**Why it is not hygiene.** A LORE store shared by every session is a
communication channel that appears in no ledger. Thirty-two agents reading and
writing one belief store can coordinate through memory instead of through
messages, and the structure the experiment measures *is* the communication
structure — so a hierarchy negotiated through shared memory would be reported
as emergence with nothing to show for it. Per-agent control turns that confound
into a variable: `--memory-off K` runs K of N without memory and asks whether
shared memory substitutes for messaging.

**What off means, strictly:**

* no snapshot in the system prompt, and no per-turn refresh, consult or graph
  block;
* the `lore_*` operators are **absent from the model's tool list** — not
  present and refusing, absent, the way `peer_send` is absent when unarmed;
* no writes: no beliefs, no staged proposals, no session index, no file map, no
  deriver review, no op-log sync.

**What off does not mean.** `lore_core` is still imported and still used:
`scrub_secrets` runs on every received peer frame, every persisted transcript
line and every ledger body, and `project_slug` / `PROJECTS_DIR` derive the
transcript path itself. Nine modules under `doxa/` import it at module level.
The transcript is still written — it is DOXA's own session record, and
`/resume`, `/search`'s local half and the transcript pane read it. Turning
memory off stops DOXA putting anything *into* the store or taking anything
*out* of it; it does not, and cannot today, mean the package is uninstalled.

---

## 3. Peers on another machine

`doxa.peers` finds sessions through a 0700 directory of presence files. Its
security model is the filesystem's — same uid, same machine — which is a good
model and also means two machines cannot see each other at all.

`doxa.peernet` is the bridge across that gap, and it is track R2 of
[`plans/remote.md`](plans/remote.md). **Every authorization question it asks
goes to `doxa.remote_policy`** (track R1, already shipped). There is no second
policy: no allow-list of its own, no opinion about what a remote caller may do,
no opinion about whether listening is on.

### Turning it on

```toml
# ~/.doxa/config.toml
remote_enabled = true
remote_allowed_logins = "you@example.com"
remote_peers = "workstation=ws.tail1234.ts.net:47600"
```

```bash
tailscale serve --bg --https 443 http://127.0.0.1:47600
```

### The four non-negotiables, and where each lives

| rule | where |
|---|---|
| **Loopback is the default.** Nothing binds until `remote_enabled` is on, *and* the bind address is `127.0.0.1` unless changed. Two independent gates. | `peernet.bind_host`, `PeerNetServer.start` → `remote_policy.remote_listening_decision` |
| **No new credential store.** Nothing here reads or writes a token, password or key file. Identity is the `Tailscale-User-Login` header `tailscale serve` attaches; the tailnet vouches for it. An endpoint is a hostname and a name to show — the `Endpoint` dataclass has three fields and none is a secret. | `peernet.Endpoint`, `peernet.request` |
| **The allow-list is DOXA's own.** Empty by default, and an empty list refuses everyone rather than everyone. | `remote_policy.allowed_logins` |
| **Say who is connected.** A peer fetched over the wire carries `PeerInfo.origin` naming the machine, and every surface that shows a local peer shows it — `/peers`, and the `peer_list` tool the model sees. | `peers.PeerInfo.origin`, `ui.labels.peer_origin` |

**The header is believed on one path only.** `tailscale serve` terminates TLS
and forwards to loopback; a header arriving anywhere else was written by
whoever connected. The bridge computes `from_loopback` from the *socket's* peer
address — never from anything in the request — and `identity_decision` refuses
unconditionally when it is false, before it looks at the login at all.

**Origin is established, not received.** `fetch_roster` stamps `origin` from
the endpoint DOXA actually dialled, overwriting whatever the reply said. A
machine cannot return rows claiming to be local. It is the one field in a
`PeerInfo` the reader establishes rather than the writer.

### Three ops, and the request kinds they already are

| op | `remote_policy` kind | why |
|---|---|---|
| `roster` | `read_status` | it is a status read: who is here |
| `history` | `read_transcript` | it returns recorded conversation |
| `deliver` | `send_prompt` | an arriving peer message can *start* a turn, so it spends the receiving machine's budget exactly as a prompt does |

No op maps to `shell_bang` or `set_permission_mode`. `!` shell and
`bypassPermissions` are therefore unreachable from the bridge by construction,
not merely refused by a check. A string this bridge does not recognise is
refused before the policy is consulted at all.

A message that crosses a machine boundary lands on the *same* Unix socket,
through the *same* `PeerHost` receive path, scrubbed by the *same*
`scrub_secrets` call, as one from the session next door. There is no second
delivery path and no second scrub.

---

## Measured

`scripts/fleet_scale.py` runs the real `FleetRun` against real `doxa.daemon`
processes over real Unix sockets with real registry entries, with a scripted
SDK client in place of the Claude CLI — so it measures the *harness's* ceiling
(spawn concurrency, fd pressure, AF_UNIX budget, registry churn, ledger
contention, teardown) rather than the fleet's, which is arithmetic.

On a 16-core / 30 GB laptop with ~11 GB already resident (and another test
suite running beside it, which is part of why 128 went the way it did):

| N | wall | dispatch spread | ledger | teardown |
|---|---|---|---|---|
| 4 | 5.0 s | 3 ms | — | 4 stopped cleanly |
| 8 | 5.7 s | 1 ms | 8 | 8 stopped cleanly |
| 16 | 7.5 s | 8 ms | — | 16 stopped cleanly |
| 32 | 12.0 s | 1 ms | 32 | 32 stopped cleanly |
| 64 | 19.7 s | 2 ms | 64 | 64 stopped cleanly |
| **128** | **475 s** | 2 ms | 128 | **62 needed SIGTERM/SIGKILL** |

**The harness ceiling on this box is between 64 and 128, and what broke first
was not what you would guess.** The dispatch barrier held at 2 ms even at 128,
and the ledger's cross-process `flock` delivered all 128 records with none
lost. What failed was the back half: sessions stopped answering their status
calls inside the poll window, the run rode its 180 s quiescence deadline to
the end, and 62 of 128 then failed to `stop` in time and had to be killed.

That is the harness working as designed -- the run still ended, the ledger was
still collected, the manifest still named every session, and nothing was left
running -- but it is the point past which a run's own timings stop being
measurements of anything. Two real defects were found by getting there, both
now fixed and both now tested: a zombie child reading as a leaked session, and
a quiescence check that asked the wrong question when a peer message had
queued the prompt behind it.

Note the memory column the table does not have. A stub session is ~57 MB
resident, so 128 of them is ~7 GB. A hundred and twenty-eight REAL sessions
would be ~75 GB and would not have run at all. The harness ceiling and the
fleet ceiling are different numbers and only one of them is arithmetic.
